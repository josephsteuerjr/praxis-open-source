"""Immutable execution context for durable Praxis runs.

The Windows body and transport clients remain brainless.  A :class:`RunContext`
is created on the server, persisted by ``run_manager.py`` and bound to the
current execution with a ``ContextVar``.  Tool code may read the bound context,
but must never infer authority or a delivery address from process-global state.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from typing import Iterator


RUN_STATUSES = frozenset({
    "pending", "running", "paused", "blocked", "in_doubt",
    "done", "cancelled", "failed",
})


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_run_id(now: dt.datetime | None = None) -> str:
    """Return a sortable, filesystem-safe run identifier."""
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    current = current.astimezone(dt.timezone.utc)
    return f"run-{current:%Y%m%dT%H%M%S%fZ}-{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True, slots=True)
class RunContext:
    """Authority, provenance and delivery address of one server-side run.

    ``status`` is the status observed when the context was loaded.  The durable
    source of truth is ``manifest.json``; :mod:`run_manager` returns a replaced
    frozen value when the manifest advances instead of mutating an object which
    may already be executing in another context.
    """

    run_id: str
    kind: str
    goal: str
    principal_id: str
    scope: str
    origin_chat_id: str | None = None
    origin_message_ids: tuple[int, ...] = ()
    delivery_chat_id: str | None = None
    context_snapshot: str = ""
    model_profile: str = ""
    status: str = "pending"
    created_at: str = ""
    forge_task_id: str = ""

    def __post_init__(self) -> None:
        if not self.run_id or any(ch in self.run_id for ch in ("/", "\\", "\0")):
            raise ValueError("run_id must be a non-empty filesystem-safe identifier")
        if not self.kind.strip():
            raise ValueError("kind must not be empty")
        if not self.goal.strip():
            raise ValueError("goal must not be empty")
        if not self.principal_id.strip():
            raise ValueError("principal_id must not be empty")
        if not self.scope.strip():
            raise ValueError("scope must not be empty")
        if self.status not in RUN_STATUSES:
            raise ValueError(f"unknown run status {self.status!r}")
        if any(not isinstance(value, int) for value in self.origin_message_ids):
            raise TypeError("origin_message_ids must contain integers")

    @classmethod
    def create(
        cls,
        *,
        kind: str,
        goal: str,
        principal_id: str,
        scope: str,
        origin_chat_id: str | int | None = None,
        origin_message_ids: tuple[int, ...] | list[int] = (),
        delivery_chat_id: str | int | None = None,
        model_profile: str = "",
        forge_task_id: str = "",
        run_id: str = "",
    ) -> "RunContext":
        return cls(
            run_id=run_id or new_run_id(),
            kind=str(kind).strip(),
            goal=str(goal).strip(),
            principal_id=str(principal_id).strip(),
            scope=str(scope).strip(),
            origin_chat_id=(str(origin_chat_id) if origin_chat_id is not None else None),
            origin_message_ids=tuple(int(value) for value in origin_message_ids),
            delivery_chat_id=(str(delivery_chat_id) if delivery_chat_id is not None else None),
            model_profile=str(model_profile or "").strip(),
            status="pending",
            created_at=_utc_now(),
            forge_task_id=str(forge_task_id or "").strip(),
        )

    def with_status(self, status: str) -> "RunContext":
        if status not in RUN_STATUSES:
            raise ValueError(f"unknown run status {status!r}")
        return replace(self, status=status)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "goal": self.goal,
            "principal_id": self.principal_id,
            "scope": self.scope,
            "origin_chat_id": self.origin_chat_id,
            "origin_message_ids": list(self.origin_message_ids),
            "delivery_chat_id": self.delivery_chat_id,
            "context_snapshot": self.context_snapshot,
            "model_profile": self.model_profile,
            "status": self.status,
            "created_at": self.created_at,
            "forge_task_id": self.forge_task_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RunContext":
        return cls(
            run_id=str(data.get("run_id") or ""),
            kind=str(data.get("kind") or ""),
            goal=str(data.get("goal") or ""),
            principal_id=str(data.get("principal_id") or ""),
            scope=str(data.get("scope") or ""),
            origin_chat_id=(str(data["origin_chat_id"])
                            if data.get("origin_chat_id") is not None else None),
            origin_message_ids=tuple(int(value) for value in (data.get("origin_message_ids") or ())),
            delivery_chat_id=(str(data["delivery_chat_id"])
                              if data.get("delivery_chat_id") is not None else None),
            context_snapshot=str(data.get("context_snapshot") or ""),
            model_profile=str(data.get("model_profile") or ""),
            status=str(data.get("status") or "pending"),
            created_at=str(data.get("created_at") or ""),
            forge_task_id=str(data.get("forge_task_id") or ""),
        )


_CURRENT_RUN: ContextVar[RunContext | None] = ContextVar("praxis_run_context", default=None)


def current_run(*, required: bool = False) -> RunContext | None:
    """Return the run bound to this execution context."""
    current = _CURRENT_RUN.get()
    if required and current is None:
        raise RuntimeError("no RunContext is bound to this execution")
    return current


def set_run(context: RunContext) -> Token:
    """Low-level binding primitive for call sites which already own a ``try/finally``."""
    if not isinstance(context, RunContext):
        raise TypeError("context must be RunContext")
    return _CURRENT_RUN.set(context)


def reset_run(token: Token) -> None:
    _CURRENT_RUN.reset(token)


@contextlib.contextmanager
def bind_run(context: RunContext) -> Iterator[RunContext]:
    """Bind one immutable run and restore the previous binding afterwards."""
    token = set_run(context)
    try:
        yield context
    finally:
        reset_run(token)
