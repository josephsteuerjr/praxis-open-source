"""Framework-neutral execution of strict :mod:`run_resume` plans.

This module is the effect boundary paired with ``run_resume.plan_resume``.  It
does not know about RunManager internals, an LLM client, Telegram, or Praxis
tool implementations.  Those capabilities are injected as narrowly typed
callbacks.  A plan is completely preflighted before its revision/event lease
is acquired, and no callback at all is invoked for non-executable plan kinds.

Integration contract
--------------------
``acquire_lease`` must atomically compare both durable cursors in
:class:`ResumeLease` and claim the paused run for one executor.  A separate
read followed by ``resume()`` is not sufficient.  An accepted grant must echo
the compared cursors and carry an opaque, non-``None`` ownership token.

``postprocess_authored_output`` may guard, format, archive, and prepare durable
delivery of the already persisted answer.  It must never call a model.

``continue_checkpoint`` receives the exact persisted system/messages/tools and
the planner's hash-verified outbound descriptors.  It is the only callback in
that branch which may continue model authoring.

For a replayed tool response, completed calls are supplied as their existing
ResultRefs and are never executed.  An outstanding call reaches the replay
callback only when the planner marked it read-only or keyed-idempotent.  A
pending-start callback must durably append ``tool_started`` before invoking its
implementation.  Every effect callback owns journalling its result/failure and
must move an uncertain side effect to ``in_doubt`` before returning failure.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from run_context import RunContext
import tool_offerings
from run_resume import (
    CHECKPOINT_SCHEMA,
    PLAN_SCHEMA,
    OutboundDescriptor,
    ResumePlan,
    ToolCallResume,
)


EXECUTION_SCHEMA = "praxis.run.resume-execution.v1"

NON_EXECUTABLE_KINDS = frozenset({
    "transport_owned", "blocked", "in_doubt", "not_resumable",
})
EXECUTABLE_KINDS = frozenset({
    "authored_output", "continue_checkpoint", "replay_model_tool_response",
})
REPLAY_BASES = frozenset({"read_only", "idempotency_key"})

ExecutionStatus = Literal[
    "noop", "invalid_plan", "lease_rejected", "completed", "failed",
]
ResolutionSource = Literal[
    "durable_result_ref", "pending_start", "replayed_outstanding",
]


INTEGRATION_CONTRACT = (
    "lease acquisition is one atomic compare-and-claim on revision and event_seq",
    "authored_output is postprocessed without another model call",
    "checkpoint continuation receives exact persisted system messages tools and outbound",
    "completed tool calls reuse durable ResultRefs without implementation execution",
    "only planner-marked read-only or keyed-idempotent outstanding calls are replayed",
    "pending tool callbacks persist intent before implementation and all callbacks journal outcomes",
)


class ResumeExecutionError(ValueError):
    """The supplied plan/callback surface cannot be executed safely."""


@dataclass(frozen=True, slots=True)
class ResumeLease:
    """Exact durable snapshot which an integration must atomically claim."""

    run_id: str
    revision: int
    event_seq: int

    @classmethod
    def from_plan(cls, plan: ResumePlan) -> "ResumeLease":
        revision = _strict_int(plan.revision, "plan.revision", minimum=1)
        event_seq = _strict_int(plan.event_seq, "plan.event_seq", minimum=1)
        if not str(plan.run_id or "").strip():
            raise ResumeExecutionError("plan.run_id is empty")
        return cls(str(plan.run_id), revision, event_seq)


@dataclass(frozen=True, slots=True)
class LeaseGrant:
    """Result of the integration's atomic compare-and-claim operation."""

    lease: ResumeLease
    accepted: bool
    observed_revision: int
    observed_event_seq: int
    owner_token: Any = None
    reason: str = ""

    @classmethod
    def accept(cls, lease: ResumeLease, *, owner_token: Any) -> "LeaseGrant":
        return cls(
            lease=lease,
            accepted=True,
            observed_revision=lease.revision,
            observed_event_seq=lease.event_seq,
            owner_token=owner_token,
        )

    @classmethod
    def reject(
        cls,
        lease: ResumeLease,
        *,
        observed_revision: int,
        observed_event_seq: int,
        reason: str = "run changed before claim",
    ) -> "LeaseGrant":
        return cls(
            lease=lease,
            accepted=False,
            observed_revision=observed_revision,
            observed_event_seq=observed_event_seq,
            reason=reason,
        )


@dataclass(frozen=True, slots=True)
class AuthoredOutputRequest:
    """Persisted terminal answer for non-model postprocessing and delivery."""

    lease: ResumeLease
    owner_token: Any
    context: RunContext
    model_input: dict[str, Any]
    model_output: dict[str, Any]
    checkpoint: dict[str, Any] | None
    outbound: tuple[OutboundDescriptor, ...]


@dataclass(frozen=True, slots=True)
class CheckpointContinuationRequest:
    """Exact checkpoint state from which model/tool authoring may continue."""

    lease: ResumeLease
    owner_token: Any
    context: RunContext
    iteration: int
    system: Any
    messages: list[Any]
    tools: list[Any]
    outbound: tuple[OutboundDescriptor, ...]
    checkpoint: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PendingToolRequest:
    """A tool block for which no durable execution intent exists yet."""

    lease: ResumeLease
    owner_token: Any
    context: RunContext
    call_id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ReplayToolRequest:
    """A planner-approved replay of an already-started safe tool call."""

    lease: ResumeLease
    owner_token: Any
    context: RunContext
    call_id: str
    name: str
    input: dict[str, Any]
    replay_basis: Literal["read_only", "idempotency_key"]


@dataclass(frozen=True, slots=True)
class ToolResolution:
    """Ordered input supplied to the continued model/tool loop."""

    call_id: str
    name: str
    source: ResolutionSource
    result_ref: dict[str, Any] | None = None
    callback_value: Any = None
    replay_basis: str = ""


@dataclass(frozen=True, slots=True)
class ToolResponseContinuationRequest:
    """Exact persisted response plus durable/fresh tool resolutions."""

    lease: ResumeLease
    owner_token: Any
    context: RunContext
    model_input: dict[str, Any]
    model_output: dict[str, Any]
    resolutions: tuple[ToolResolution, ...]
    checkpoint: dict[str, Any] | None
    outbound: tuple[OutboundDescriptor, ...]


@dataclass(frozen=True, slots=True)
class ResumeExecutorCallbacks:
    """Injected framework effects; only callbacks needed by a plan are required."""

    acquire_lease: Callable[[ResumeLease], LeaseGrant]
    postprocess_authored_output: Callable[[AuthoredOutputRequest], Any] | None = None
    continue_checkpoint: Callable[[CheckpointContinuationRequest], Any] | None = None
    execute_pending_tool: Callable[[PendingToolRequest], Any] | None = None
    replay_outstanding_tool: Callable[[ReplayToolRequest], Any] | None = None
    continue_tool_response: Callable[[ToolResponseContinuationRequest], Any] | None = None


@dataclass(frozen=True, slots=True)
class ResumeExecutionOutcome:
    """Typed, non-throwing report for one attempted plan execution."""

    run_id: str
    plan_kind: str
    status: ExecutionStatus
    reason: str
    lease: ResumeLease | None = None
    lease_acquired: bool = False
    effects_started: bool = False
    phase: str = "preflight"
    callback_value: Any = None
    resolutions: tuple[ToolResolution, ...] = field(default_factory=tuple)
    error_type: str = ""
    error_message: str = ""
    schema: str = EXECUTION_SCHEMA

    @property
    def completed(self) -> bool:
        return self.status == "completed"


def _strict_int(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ResumeExecutionError(f"{name} must be an integer >= {minimum}")
    return value


def _context(plan: ResumePlan) -> RunContext:
    context = plan.context
    if not isinstance(context, RunContext) or context.run_id != plan.run_id:
        raise ResumeExecutionError("plan context is missing or belongs to another run")
    return context


def _checkpoint(plan: ResumePlan) -> dict[str, Any]:
    value = plan.checkpoint
    if not isinstance(value, dict) or value.get("schema") != CHECKPOINT_SCHEMA:
        raise ResumeExecutionError("continue_checkpoint lacks a valid exact checkpoint")
    if "system" not in value or not isinstance(value.get("messages"), list):
        raise ResumeExecutionError("checkpoint lacks exact system/messages")
    if not isinstance(value.get("tools"), list) or not isinstance(value.get("outbound"), list):
        raise ResumeExecutionError("checkpoint lacks exact tools/outbound")
    _strict_int(value.get("iteration"), "checkpoint.iteration", minimum=1)
    return copy.deepcopy(value)


def _result_ref(run_id: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != "praxis.result-ref.v1":
        raise ResumeExecutionError("completed tool lacks a durable ResultRef")
    if value.get("run_id") != run_id:
        raise ResumeExecutionError("completed tool ResultRef belongs to another run")
    if not str(value.get("result_id") or "") or not str(value.get("sha256") or ""):
        raise ResumeExecutionError("completed tool ResultRef is incomplete")
    return copy.deepcopy(value)


def _tool_blocks(plan: ResumePlan) -> list[dict[str, Any]]:
    output = plan.model_output
    if not isinstance(output, dict) or output.get("stop_reason") != "tool_use":
        raise ResumeExecutionError("tool replay lacks an exact tool_use model output")
    blocks = output.get("blocks")
    if not isinstance(blocks, list):
        raise ResumeExecutionError("tool replay model blocks are malformed")
    return [block for block in blocks
            if isinstance(block, dict) and block.get("type") == "tool_use"]


def _offered_tool_names(plan: ResumePlan) -> frozenset[str]:
    model_input = plan.model_input
    tools = model_input.get("tools") if isinstance(model_input, dict) else None
    if not isinstance(tools, list):
        raise ResumeExecutionError("tool replay lacks its exact offered-tool list")
    try:
        return tool_offerings.local_function_names(tools)
    except tool_offerings.ToolOfferingError as exc:
        raise ResumeExecutionError(str(exc)) from exc


def _preflight_tool_replay(plan: ResumePlan, callbacks: ResumeExecutorCallbacks) -> None:
    if not isinstance(plan.model_input, dict):
        raise ResumeExecutionError("tool replay lacks exact model input")
    blocks = _tool_blocks(plan)
    calls = tuple(plan.tool_calls)
    if len(blocks) != len(calls) or not calls:
        raise ResumeExecutionError("tool replay plan does not match model tool blocks")
    seen: set[str] = set()
    offered = _offered_tool_names(plan)
    needs_pending = False
    needs_replay = False
    for block, call in zip(blocks, calls):
        if not isinstance(call, ToolCallResume):
            raise ResumeExecutionError("tool replay contains an invalid call plan")
        raw_input = block.get("input")
        normalized_input = ({key: value for key, value in raw_input.items()
                             if value is not None}
                            if isinstance(raw_input, dict) else None)
        if (not call.call_id or call.call_id in seen or block.get("id") != call.call_id
                or block.get("name") != call.name or normalized_input != call.input):
            raise ResumeExecutionError("tool replay call order/content differs from model output")
        if call.name not in offered:
            raise ResumeExecutionError(
                f"tool {call.name!r} was not offered in the exact model input")
        seen.add(call.call_id)
        if call.state == "completed":
            if call.replayable or call.replay_basis:
                raise ResumeExecutionError("completed tool is incorrectly marked replayable")
            _result_ref(plan.run_id, call.result_ref)
        elif call.state == "pending_start":
            if call.replayable or call.replay_basis or call.result_ref is not None:
                raise ResumeExecutionError("pending tool carries replay/result evidence")
            needs_pending = True
        elif call.state == "outstanding":
            if (not call.replayable or call.replay_basis not in REPLAY_BASES
                    or call.result_ref is not None):
                raise ResumeExecutionError(
                    f"outstanding tool {call.call_id} is not safely replayable"
                )
            needs_replay = True
        else:
            raise ResumeExecutionError(f"unknown tool state {call.state!r}")
    if callbacks.continue_tool_response is None:
        raise ResumeExecutionError("continue_tool_response callback is required")
    if needs_pending and callbacks.execute_pending_tool is None:
        raise ResumeExecutionError("execute_pending_tool callback is required")
    if needs_replay and callbacks.replay_outstanding_tool is None:
        raise ResumeExecutionError("replay_outstanding_tool callback is required")


def _preflight(plan: ResumePlan, callbacks: ResumeExecutorCallbacks) -> ResumeLease:
    if not isinstance(plan, ResumePlan) or plan.schema != PLAN_SCHEMA:
        raise ResumeExecutionError("invalid resume plan schema/type")
    if plan.kind not in EXECUTABLE_KINDS:
        raise ResumeExecutionError(f"unknown executable plan kind {plan.kind!r}")
    if plan.auto_resume is not True:
        raise ResumeExecutionError("executable plan is not marked auto_resume")
    if not callable(callbacks.acquire_lease):
        raise ResumeExecutionError("acquire_lease callback is required")
    lease = ResumeLease.from_plan(plan)
    _context(plan)
    if plan.kind == "authored_output":
        if callbacks.postprocess_authored_output is None:
            raise ResumeExecutionError("postprocess_authored_output callback is required")
        if not isinstance(plan.model_input, dict) or not isinstance(plan.model_output, dict):
            raise ResumeExecutionError("authored output lacks its exact model pair")
        if plan.model_output.get("stop_reason") == "tool_use":
            raise ResumeExecutionError("authored output is not terminal")
    elif plan.kind == "continue_checkpoint":
        if callbacks.continue_checkpoint is None:
            raise ResumeExecutionError("continue_checkpoint callback is required")
        _checkpoint(plan)
    else:
        _preflight_tool_replay(plan, callbacks)
    return lease


def _validate_grant(lease: ResumeLease, value: Any) -> LeaseGrant:
    if not isinstance(value, LeaseGrant) or value.lease != lease:
        raise ResumeExecutionError("lease callback returned a foreign/malformed grant")
    if value.accepted:
        if (value.observed_revision != lease.revision
                or value.observed_event_seq != lease.event_seq):
            raise ResumeExecutionError("accepted lease did not compare both exact cursors")
        if value.owner_token is None:
            raise ResumeExecutionError("accepted lease has no ownership token")
    return value


def _failed(
    plan: ResumePlan,
    *,
    lease: ResumeLease | None,
    acquired: bool,
    effects_started: bool,
    phase: str,
    exc: Exception,
    resolutions: tuple[ToolResolution, ...] = (),
) -> ResumeExecutionOutcome:
    return ResumeExecutionOutcome(
        run_id=str(getattr(plan, "run_id", "") or ""),
        plan_kind=str(getattr(plan, "kind", "") or ""),
        status="failed" if acquired else "invalid_plan",
        reason=f"{phase} failed: {type(exc).__name__}: {exc}",
        lease=lease,
        lease_acquired=acquired,
        effects_started=effects_started,
        phase=phase,
        resolutions=resolutions,
        error_type=type(exc).__name__,
        error_message=str(exc),
    )


def execute_resume(
    plan: ResumePlan,
    callbacks: ResumeExecutorCallbacks,
) -> ResumeExecutionOutcome:
    """Execute one strict plan through injected effects and return a typed outcome.

    The four non-executable kinds return before preflight and therefore cannot
    acquire a lease or invoke any effect callback.  All replay safety checks run
    before lease acquisition, preventing a malformed plan from producing a
    partial batch.
    """

    run_id = str(getattr(plan, "run_id", "") or "")
    kind = str(getattr(plan, "kind", "") or "")
    if isinstance(plan, ResumePlan) and plan.schema == PLAN_SCHEMA and kind in NON_EXECUTABLE_KINDS:
        return ResumeExecutionOutcome(
            run_id=run_id,
            plan_kind=kind,
            status="noop",
            reason=plan.reason or f"{kind} is owned by another recovery/control path",
        )

    try:
        lease = _preflight(plan, callbacks)
    except Exception as exc:
        return _failed(
            plan, lease=None, acquired=False, effects_started=False,
            phase="preflight", exc=exc,
        )

    try:
        grant = _validate_grant(lease, callbacks.acquire_lease(lease))
    except Exception as exc:
        return _failed(
            plan, lease=lease, acquired=False, effects_started=False,
            phase="acquire_lease", exc=exc,
        )
    if not grant.accepted:
        return ResumeExecutionOutcome(
            run_id=plan.run_id,
            plan_kind=plan.kind,
            status="lease_rejected",
            reason=grant.reason or "run changed before lease acquisition",
            lease=lease,
            phase="acquire_lease",
        )

    context = _context(plan)
    if plan.kind == "authored_output":
        request = AuthoredOutputRequest(
            lease=lease,
            owner_token=grant.owner_token,
            context=context,
            model_input=copy.deepcopy(plan.model_input),
            model_output=copy.deepcopy(plan.model_output),
            checkpoint=copy.deepcopy(plan.checkpoint),
            outbound=tuple(plan.outbound),
        )
        try:
            value = callbacks.postprocess_authored_output(request)  # type: ignore[misc]
        except Exception as exc:
            return _failed(
                plan, lease=lease, acquired=True, effects_started=True,
                phase="postprocess_authored_output", exc=exc,
            )
        return ResumeExecutionOutcome(
            run_id=plan.run_id, plan_kind=plan.kind, status="completed",
            reason="persisted authored output postprocessed without re-authoring",
            lease=lease, lease_acquired=True, effects_started=True,
            phase="postprocess_authored_output", callback_value=value,
        )

    if plan.kind == "continue_checkpoint":
        checkpoint = _checkpoint(plan)
        request = CheckpointContinuationRequest(
            lease=lease,
            owner_token=grant.owner_token,
            context=context,
            iteration=checkpoint["iteration"],
            system=copy.deepcopy(checkpoint["system"]),
            messages=copy.deepcopy(checkpoint["messages"]),
            tools=copy.deepcopy(checkpoint["tools"]),
            outbound=tuple(plan.outbound),
            checkpoint=checkpoint,
        )
        try:
            value = callbacks.continue_checkpoint(request)  # type: ignore[misc]
        except Exception as exc:
            return _failed(
                plan, lease=lease, acquired=True, effects_started=True,
                phase="continue_checkpoint", exc=exc,
            )
        return ResumeExecutionOutcome(
            run_id=plan.run_id, plan_kind=plan.kind, status="completed",
            reason="continued from exact persisted checkpoint",
            lease=lease, lease_acquired=True, effects_started=True,
            phase="continue_checkpoint", callback_value=value,
        )

    resolutions: list[ToolResolution] = []
    for call in plan.tool_calls:
        if call.state == "completed":
            resolutions.append(ToolResolution(
                call_id=call.call_id,
                name=call.name,
                source="durable_result_ref",
                result_ref=_result_ref(plan.run_id, call.result_ref),
            ))
            continue
        if call.state == "pending_start":
            request = PendingToolRequest(
                lease=lease, owner_token=grant.owner_token, context=context,
                call_id=call.call_id, name=call.name, input=copy.deepcopy(call.input),
            )
            phase = f"execute_pending_tool:{call.call_id}"
            try:
                value = callbacks.execute_pending_tool(request)  # type: ignore[misc]
            except Exception as exc:
                return _failed(
                    plan, lease=lease, acquired=True, effects_started=True,
                    phase=phase, exc=exc, resolutions=tuple(resolutions),
                )
            resolutions.append(ToolResolution(
                call_id=call.call_id, name=call.name, source="pending_start",
                callback_value=value,
            ))
            continue
        # Preflight proved every outstanding call is planner-marked safe.
        request = ReplayToolRequest(
            lease=lease, owner_token=grant.owner_token, context=context,
            call_id=call.call_id, name=call.name, input=copy.deepcopy(call.input),
            replay_basis=call.replay_basis,  # type: ignore[arg-type]
        )
        phase = f"replay_outstanding_tool:{call.call_id}"
        try:
            value = callbacks.replay_outstanding_tool(request)  # type: ignore[misc]
        except Exception as exc:
            return _failed(
                plan, lease=lease, acquired=True, effects_started=True,
                phase=phase, exc=exc, resolutions=tuple(resolutions),
            )
        resolutions.append(ToolResolution(
            call_id=call.call_id, name=call.name, source="replayed_outstanding",
            callback_value=value, replay_basis=call.replay_basis,
        ))

    continuation = ToolResponseContinuationRequest(
        lease=lease,
        owner_token=grant.owner_token,
        context=context,
        model_input=copy.deepcopy(plan.model_input),
        model_output=copy.deepcopy(plan.model_output),
        resolutions=tuple(resolutions),
        checkpoint=copy.deepcopy(plan.checkpoint),
        outbound=tuple(plan.outbound),
    )
    try:
        value = callbacks.continue_tool_response(continuation)  # type: ignore[misc]
    except Exception as exc:
        return _failed(
            plan, lease=lease, acquired=True, effects_started=True,
            phase="continue_tool_response", exc=exc,
            resolutions=tuple(resolutions),
        )
    return ResumeExecutionOutcome(
        run_id=plan.run_id, plan_kind=plan.kind, status="completed",
        reason="replayed persisted tool response with strict result reuse",
        lease=lease, lease_acquired=True, effects_started=True,
        phase="continue_tool_response", callback_value=value,
        resolutions=tuple(resolutions),
    )


__all__ = [
    "AuthoredOutputRequest",
    "CheckpointContinuationRequest",
    "EXECUTION_SCHEMA",
    "INTEGRATION_CONTRACT",
    "LeaseGrant",
    "PendingToolRequest",
    "ReplayToolRequest",
    "ResumeExecutionError",
    "ResumeExecutionOutcome",
    "ResumeExecutorCallbacks",
    "ResumeLease",
    "ToolResolution",
    "ToolResponseContinuationRequest",
    "execute_resume",
]
