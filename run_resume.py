"""Pure recovery planning for durable Praxis runs.

The planner in this module is intentionally unable to resume anything.  It
does not call an LLM, Telegram, a tool implementation, or a process API.  It
only reduces the public, durable :class:`run_manager.RunManager` views into a
strict hand-off plan for an executor which owns those effects.

The important boundary is that a persisted ``telegram.deliver`` intent is
always transport-owned.  Model execution must never author the answer again
once that intent exists.  Conversely, an owner pause/cancel is never converted
to an automatic resume merely because a usable checkpoint also exists.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, Sequence

from run_context import RunContext
import tool_offerings


PLAN_SCHEMA = "praxis.run.resume-plan.v1"
CHECKPOINT_SCHEMA = "praxis.tool-loop-checkpoint.v1"
RESULT_SCHEMA = "praxis.result-ref.v1"
EVENT_SCHEMA = "praxis.run.event.v1"
STATUS_SCHEMA = "praxis.run.status.v1"
TOOL_RESULT_METADATA_SCHEMA = "praxis.tool-result.metadata.v1"
OUTBOUND_GUARD_SCHEMA = "praxis.outbound-guard-result.v3"
PREVIOUS_OUTBOUND_GUARD_SCHEMA = "praxis.outbound-guard-result.v2"
OUTBOUND_GUARD_CALL_PREFIX = "outbound-guard-v2"
LEGACY_OUTBOUND_GUARD_CALL_PREFIX = "outbound-guard"
LEGACY_OUTBOUND_GUARD_SCHEMA = "praxis.outbound-guard-result.v1"
OUTBOUND_GUARD_INPUT_SCHEMA = "praxis.outbound-guard-input.v1"

# These guard one recovery reconstruction, not the live execution loop.  A run
# may make as many tool calls as needed; if its durable history eventually
# exceeds one planning pass, recovery stops explicitly instead of exhausting
# the process.  The owner can raise the budgets for a deliberate audit/replay.
DEFAULT_MAX_EVIDENCE_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_EVENTS = 250_000
DEFAULT_MAX_PLANNING_SECONDS = 60.0

PlanKind = Literal[
    "transport_owned",
    "authored_output",
    "continue_checkpoint",
    "replay_model_tool_response",
    "blocked",
    "in_doubt",
    "not_resumable",
]
ToolState = Literal["pending_start", "completed", "outstanding"]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RESULT_ID = re.compile(r"^result-(\d{4,12})$")
_RESULT_NAME = re.compile(r"^(\d{4,12})-[A-Za-z0-9_.-]+\.(?:log|bin)$")
_QUEUE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MIME = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+(?:\s*;[^\r\n]*)?$")
_TERMINAL = frozenset({"done", "cancelled", "failed"})
_KNOWN_STATUSES = frozenset({
    "pending", "running", "paused", "blocked", "in_doubt",
    "done", "cancelled", "failed",
})
_RECOVERY_PAUSE_REASONS = frozenset({
    "process restarted; no uncertain side effect observed",
    "recovery executor stopped before transport intent",
})
_DIRECT_OUTBOX_PAUSE = re.compile(
    # PASS 30 Этап 2: narrate — третий законный хозяин direct-outbox паузы
    # (расширение альтернативой; существующие строки-протоколы не тронуты)
    r"^durable (?:send_message|send_file|narrate) intent awaits Telegram acceptance$"
)
_ALLOWED_OUTBOUND_KINDS = frozenset({"photo", "audio", "document"})
_ALLOWED_OUTBOUND_SCOPES = frozenset({
    "owner", "family", "known", "unknown", "group",
})


class ResumeEvidenceError(ValueError):
    """Durable evidence is incomplete, inconsistent, or tampered with."""


@dataclass(slots=True)
class _EvidenceBudget:
    """Cumulative, side-effect-free bounds for one recovery reconstruction."""

    max_bytes: int
    deadline_monotonic: float
    used_bytes: int = 0
    seen_results: set[tuple[str, str, str]] = field(default_factory=set)
    seen_outbound: set[tuple[object, ...]] = field(default_factory=set)
    verified_outbound: set[tuple[object, ...]] = field(default_factory=set)

    def check_time(self, phase: str) -> None:
        if time.monotonic() > self.deadline_monotonic:
            raise ResumeEvidenceError(
                f"planning-time budget exceeded while {phase}"
            )

    def consume(self, count: int, phase: str) -> None:
        self.check_time(phase)
        amount = _integer(count, f"{phase}.bytes", minimum=0)
        projected = self.used_bytes + amount
        if projected > self.max_bytes:
            raise ResumeEvidenceError(
                f"cumulative evidence byte budget exceeded while {phase} "
                f"({projected} > {self.max_bytes})"
            )
        self.used_bytes = projected

    def account_events(self, events: Sequence[dict]) -> None:
        for index, row in enumerate(events, 1):
            size = len(json.dumps(
                row, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")) + 1
            self.consume(size, f"event evidence at row {index}")

    def reserve_result(self, run_id: str, ref: dict) -> None:
        key = (
            run_id,
            str(ref.get("result_id") or ""),
            str(ref.get("sha256") or ""),
        )
        if key in self.seen_results:
            self.check_time(f"rechecking {key[1]}")
            return
        self.consume(
            _integer(ref.get("size"), "ResultRef.size"),
            f"reading result evidence {key[1]}",
        )
        self.seen_results.add(key)

    def reserve_outbound(self, path: Path, size: int, digest: str,
                         identity: tuple[int, ...]) -> tuple[object, ...]:
        """Reserve one unique filesystem snapshot and return its cache key."""
        key: tuple[object, ...] = (
            os.path.normcase(str(path)), size, digest, *identity,
        )
        if key in self.seen_outbound:
            self.check_time(f"rechecking outbound evidence {path.name}")
            return key
        self.consume(size, f"reading outbound evidence {path.name}")
        self.seen_outbound.add(key)
        return key

    def outbound_is_verified(self, key: tuple[object, ...]) -> bool:
        self.check_time("reusing verified outbound evidence")
        return key in self.verified_outbound

    def mark_outbound_verified(self, key: tuple[object, ...]) -> None:
        self.check_time("recording verified outbound evidence")
        self.verified_outbound.add(key)


class RunManagerReader(Protocol):
    """Read-only public surface consumed by :func:`plan_resume`."""

    def manifest(self, run_id: str) -> dict: ...
    def status(self, run_id: str, *, max_events: int | None = None,
               max_bytes: int | None = None,
               deadline_monotonic: float | None = None) -> dict: ...
    def context(self, run_id: str) -> RunContext: ...
    def events(self, run_id: str, *, strict: bool = False,
               max_events: int | None = None,
               max_bytes: int | None = None,
               deadline_monotonic: float | None = None) -> list[dict]: ...
    def outstanding_tools(self, run_id: str, *, max_events: int | None = None,
                          max_bytes: int | None = None,
                          deadline_monotonic: float | None = None) -> dict[str, dict]: ...
    def read_result(self, run_id: str, result: str, *, byte_offset: int = 0,
                    byte_limit: int = 65536, line_start: int | None = None,
                    line_count: int | None = None, verify_sha256: bool = True,
                    deadline_monotonic: float | None = None) -> dict: ...


@dataclass(frozen=True, slots=True)
class OutboundDescriptor:
    """Hash-verified outbound state restored from a checkpoint, not queued."""

    kind: str
    path: str
    mime: str
    size: int
    target_chat_id: str | int
    scope: str
    caption: str
    reply_to_message_id: str | int | None
    voice_note: bool
    queue_id: str
    run_id: str
    sha256: str
    guard_note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "mime": self.mime,
            "size": self.size,
            "target_chat_id": self.target_chat_id,
            "scope": self.scope,
            "caption": self.caption,
            "reply_to_message_id": self.reply_to_message_id,
            "voice_note": self.voice_note,
            "queue_id": self.queue_id,
            "run_id": self.run_id,
            "sha256": self.sha256,
            "guard_note": self.guard_note,
        }


@dataclass(frozen=True, slots=True)
class ToolCallResume:
    """One exact tool block and its durable execution state."""

    call_id: str
    name: str
    input: dict[str, Any]
    state: ToolState
    replayable: bool = False
    replay_basis: str = ""
    result_ref: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "name": self.name,
            "input": copy.deepcopy(self.input),
            "state": self.state,
            "replayable": self.replayable,
            "replay_basis": self.replay_basis,
            "result_ref": copy.deepcopy(self.result_ref),
        }


@dataclass(frozen=True, slots=True)
class ResumePlan:
    """A side-effect-free, revision-bound instruction for a future executor."""

    run_id: str
    kind: PlanKind
    status: str
    reason: str
    revision: int
    event_seq: int
    auto_resume: bool = False
    context: RunContext | None = None
    transport_intent: dict[str, Any] | None = None
    model_input: dict[str, Any] | None = None
    model_output: dict[str, Any] | None = None
    checkpoint: dict[str, Any] | None = None
    outbound: tuple[OutboundDescriptor, ...] = ()
    tool_calls: tuple[ToolCallResume, ...] = ()
    diagnostics: tuple[str, ...] = field(default_factory=tuple)
    schema: str = PLAN_SCHEMA

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "run_id": self.run_id,
            "kind": self.kind,
            "status": self.status,
            "reason": self.reason,
            "revision": self.revision,
            "event_seq": self.event_seq,
            "auto_resume": self.auto_resume,
            "context": self.context.to_dict() if self.context is not None else None,
            "transport_intent": copy.deepcopy(self.transport_intent),
            "model_input": copy.deepcopy(self.model_input),
            "model_output": copy.deepcopy(self.model_output),
            "checkpoint": copy.deepcopy(self.checkpoint),
            "outbound": [item.as_dict() for item in self.outbound],
            "tool_calls": [item.as_dict() for item in self.tool_calls],
            "diagnostics": list(self.diagnostics),
        }


def _integer(value: Any, field_name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResumeEvidenceError(f"{field_name} must be an integer")
    if value < minimum:
        raise ResumeEvidenceError(f"{field_name} must be >= {minimum}")
    return value


def _validate_result_ref(run_id: str, ref: Any, *, require_json: bool = False) -> dict:
    if not isinstance(ref, dict) or ref.get("schema") != RESULT_SCHEMA:
        raise ResumeEvidenceError("invalid ResultRef schema")
    normalized = copy.deepcopy(ref)
    if str(normalized.get("run_id") or "") != run_id:
        raise ResumeEvidenceError("ResultRef belongs to another run")
    result_id = str(normalized.get("result_id") or "")
    result_match = _RESULT_ID.fullmatch(result_id)
    path = str(normalized.get("path") or "")
    if "\\" in path or path.count("/") != 1 or not path.startswith("results/"):
        raise ResumeEvidenceError("ResultRef path is not a strict results/<file> path")
    filename = path.removeprefix("results/")
    name_match = _RESULT_NAME.fullmatch(filename)
    if result_match is None or name_match is None:
        raise ResumeEvidenceError("ResultRef id/path is malformed")
    if result_match.group(1) != name_match.group(1) or int(result_match.group(1)) < 1:
        raise ResumeEvidenceError("ResultRef id does not match its path")
    digest = str(normalized.get("sha256") or "")
    if not _SHA256.fullmatch(digest):
        raise ResumeEvidenceError("ResultRef sha256 is malformed")
    _integer(normalized.get("size"), "ResultRef.size", minimum=0)
    media_type = str(normalized.get("media_type") or "")
    if not media_type or "\r" in media_type or "\n" in media_type:
        raise ResumeEvidenceError("ResultRef media_type is malformed")
    if require_json:
        if media_type.split(";", 1)[0].strip().lower() != "application/json":
            raise ResumeEvidenceError("JSON evidence has a non-JSON media type")
        if normalized.get("encoding") != "utf-8":
            raise ResumeEvidenceError("JSON evidence is not declared as UTF-8")
    return normalized


def _validate_inline(ref: dict, payload: bytes) -> None:
    inline = ref.get("inline")
    if not isinstance(inline, dict):
        raise ResumeEvidenceError("ResultRef inline projection is missing")
    if ref.get("encoding") == "utf-8":
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ResumeEvidenceError("UTF-8 ResultRef contains invalid bytes") from exc
        if ref.get("line_count") != len(text.splitlines()):
            raise ResumeEvidenceError("ResultRef line_count does not match its payload")
        head = inline.get("head")
        tail = inline.get("tail")
        truncated = inline.get("truncated")
        omitted = inline.get("omitted_chars")
        if not isinstance(head, str) or not isinstance(tail, str) or not isinstance(truncated, bool):
            raise ResumeEvidenceError("text ResultRef inline projection is malformed")
        if not truncated:
            if head != text or tail != "" or _integer(omitted, "inline.omitted_chars") != 0:
                raise ResumeEvidenceError("untruncated ResultRef inline text does not match")
        else:
            omitted_count = _integer(omitted, "inline.omitted_chars")
            if (not text.startswith(head) or not text.endswith(tail)
                    or len(head) + len(tail) + omitted_count != len(text)):
                raise ResumeEvidenceError("truncated ResultRef inline text does not match")


def read_full_result_bytes(manager: RunManagerReader, run_id: str, ref: dict, *,
                           max_bytes: int = 64 * 1024 * 1024,
                           budget: _EvidenceBudget | None = None) -> bytes:
    """Read and hash-check a complete ResultRef via public cursor APIs only."""

    checked = _validate_result_ref(run_id, ref)
    size = _integer(checked["size"], "ResultRef.size")
    cap = _integer(max_bytes, "max_bytes", minimum=1)
    if size > cap:
        raise ResumeEvidenceError(f"ResultRef exceeds planner cap ({size} > {cap})")
    if budget is not None:
        budget.reserve_result(run_id, checked)
    offset = 0
    chunks: list[bytes] = []
    while True:
        if budget is not None:
            budget.check_time(f"reading result evidence {checked['result_id']}")
        page = manager.read_result(
            run_id, checked["result_id"], byte_offset=offset,
            byte_limit=min(4 * 1024 * 1024, max(1, size - offset)),
            verify_sha256=False,
            deadline_monotonic=(budget.deadline_monotonic if budget is not None else None),
        )
        if not isinstance(page, dict) or page.get("mode") != "bytes":
            raise ResumeEvidenceError("RunManager returned a non-byte result cursor")
        if str(page.get("run_id") or "") != run_id:
            raise ResumeEvidenceError("result cursor crossed run boundary")
        if str(page.get("path") or "") != checked["path"]:
            raise ResumeEvidenceError("result cursor path changed")
        # A manager may still include a cached/full-file digest, but recovery
        # does not require one per page.  The one assembled payload is hashed
        # below against the durable ResultRef.
        page_digest = page.get("sha256")
        if page_digest not in (None, "") and str(page_digest) != checked["sha256"]:
            raise ResumeEvidenceError("result cursor checksum differs from ResultRef")
        if _integer(page.get("size"), "cursor.size") != size:
            raise ResumeEvidenceError(
                "result cursor size or checksum differs from ResultRef"
            )
        if _integer(page.get("offset"), "cursor.offset") != offset:
            raise ResumeEvidenceError("result cursor offset is discontinuous")
        encoded = page.get("data_base64")
        if not isinstance(encoded, str):
            raise ResumeEvidenceError("result cursor has no base64 payload")
        try:
            chunk = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ResumeEvidenceError("result cursor base64 is malformed") from exc
        if _integer(page.get("bytes"), "cursor.bytes") != len(chunk):
            raise ResumeEvidenceError("result cursor byte count is inconsistent")
        next_offset = _integer(page.get("next_offset"), "cursor.next_offset")
        if next_offset != offset + len(chunk) or next_offset > size:
            raise ResumeEvidenceError("result cursor next offset is inconsistent")
        chunks.append(chunk)
        offset = next_offset
        eof = page.get("eof")
        if not isinstance(eof, bool):
            raise ResumeEvidenceError("result cursor EOF flag is malformed")
        if eof:
            if offset != size:
                raise ResumeEvidenceError("result cursor ended before declared size")
            break
        if not chunk:
            raise ResumeEvidenceError("result cursor made no progress")
    payload = b"".join(chunks)
    if len(payload) != size:
        raise ResumeEvidenceError("ResultRef payload failed final size verification")
    if hashlib.sha256(payload).hexdigest() != checked["sha256"]:
        raise ResumeEvidenceError("ResultRef payload checksum differs from its durable hash")
    _validate_inline(checked, payload)
    if budget is not None:
        budget.check_time(f"verifying result evidence {checked['result_id']}")
    return payload


def read_full_json_result(manager: RunManagerReader, run_id: str, ref: dict, *,
                          max_bytes: int = 64 * 1024 * 1024,
                          budget: _EvidenceBudget | None = None) -> dict:
    """Return one complete hash-verified JSON object from a ResultRef."""

    checked = _validate_result_ref(run_id, ref, require_json=True)
    payload = read_full_result_bytes(
        manager, run_id, checked, max_bytes=max_bytes, budget=budget,
    )
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ResumeEvidenceError("ResultRef is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ResumeEvidenceError("resume JSON evidence must be an object")
    return value


def _file_identity(path: Path) -> tuple[int, int, int, int, int]:
    """Return a cacheable identity for one exact filesystem snapshot."""

    metadata = path.stat()
    return (
        int(metadata.st_size),
        int(getattr(metadata, "st_mtime_ns", metadata.st_mtime * 1_000_000_000)),
        int(getattr(metadata, "st_ctime_ns", metadata.st_ctime * 1_000_000_000)),
        int(getattr(metadata, "st_dev", 0)),
        int(getattr(metadata, "st_ino", 0)),
    )


def _file_sha256(path: Path, *, budget: _EvidenceBudget | None = None,
                 phase: str = "hashing outbound evidence") -> str:
    digest = hashlib.sha256()
    if budget is not None:
        budget.check_time(phase)
    with path.open("rb") as stream:
        while True:
            if budget is not None:
                budget.check_time(phase)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    if budget is not None:
        budget.check_time(phase)
    return digest.hexdigest()


def _strict_outbound_path(raw_value: Any, allowed_roots: tuple[Path, ...]) -> Path:
    raw_text = str(raw_value or "")
    if not raw_text or "\0" in raw_text:
        raise ResumeEvidenceError("checkpoint outbound path is empty")
    raw = Path(raw_text).expanduser()
    if not raw.is_absolute() or ".." in raw.parts:
        raise ResumeEvidenceError("checkpoint outbound path must be absolute and traversal-free")
    if raw.is_symlink():
        raise ResumeEvidenceError("checkpoint outbound path must not be a symlink")
    try:
        lexical = Path(os.path.abspath(raw))
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise ResumeEvidenceError("checkpoint outbound file is missing") from exc
    if lexical != resolved:
        raise ResumeEvidenceError("checkpoint outbound path traverses a symlink")
    if not resolved.is_file():
        raise ResumeEvidenceError("checkpoint outbound path is not a regular file")
    for root in allowed_roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise ResumeEvidenceError("checkpoint outbound path is outside allowed roots")


def restore_outbound_descriptors(
    checkpoint: dict,
    *,
    run_id: str,
    allowed_roots: Sequence[str | os.PathLike[str]],
    budget: _EvidenceBudget | None = None,
) -> tuple[OutboundDescriptor, ...]:
    """Verify checkpoint media and return inert descriptors without queue/send effects."""

    raw_items = checkpoint.get("outbound")
    if not isinstance(raw_items, list):
        raise ResumeEvidenceError("checkpoint outbound must be a list")
    if not raw_items:
        return ()
    if not allowed_roots:
        raise ResumeEvidenceError("outbound recovery requires explicit allowed roots")
    roots: list[Path] = []
    for raw_root in allowed_roots:
        try:
            root = Path(raw_root).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ResumeEvidenceError("an allowed outbound root does not exist") from exc
        if not root.is_dir():
            raise ResumeEvidenceError("an allowed outbound root is not a directory")
        roots.append(root)
    restored: list[OutboundDescriptor] = []
    queue_ids: set[str] = set()
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise ResumeEvidenceError(f"outbound[{index}] is not an object")
        kind = str(item.get("kind") or "")
        if kind not in _ALLOWED_OUTBOUND_KINDS:
            raise ResumeEvidenceError(f"outbound[{index}] has an invalid kind")
        path = _strict_outbound_path(item.get("path"), tuple(roots))
        mime = str(item.get("mime") or "")
        if not _MIME.fullmatch(mime):
            raise ResumeEvidenceError(f"outbound[{index}] has an invalid MIME type")
        if kind == "photo" and not mime.lower().startswith("image/"):
            raise ResumeEvidenceError("photo descriptor is not image MIME")
        if kind == "audio" and not mime.lower().startswith("audio/"):
            raise ResumeEvidenceError("audio descriptor is not audio MIME")
        size = _integer(item.get("size"), f"outbound[{index}].size")
        digest = str(item.get("sha256") or "")
        if not _SHA256.fullmatch(digest):
            raise ResumeEvidenceError(f"outbound[{index}] has an invalid sha256")
        try:
            identity_before = _file_identity(path)
        except OSError as exc:
            raise ResumeEvidenceError(
                f"outbound[{index}] file metadata changed"
            ) from exc
        if identity_before[0] != size:
            raise ResumeEvidenceError(f"outbound[{index}] file metadata changed")
        cache_key: tuple[object, ...] | None = None
        already_verified = False
        if budget is not None:
            cache_key = budget.reserve_outbound(
                path, size, digest, identity_before,
            )
            already_verified = budget.outbound_is_verified(cache_key)
        if not already_verified:
            try:
                actual_digest = _file_sha256(
                    path, budget=budget,
                    phase=f"hashing outbound evidence {path.name}",
                )
                identity_after = _file_identity(path)
            except OSError as exc:
                raise ResumeEvidenceError(
                    f"outbound[{index}] file metadata changed"
                ) from exc
            if identity_after != identity_before or actual_digest != digest:
                raise ResumeEvidenceError(f"outbound[{index}] file metadata changed")
            if budget is not None:
                assert cache_key is not None
                budget.mark_outbound_verified(cache_key)
        target = item.get("target_chat_id")
        if isinstance(target, bool) or not isinstance(target, (str, int)) or not str(target).strip():
            raise ResumeEvidenceError(f"outbound[{index}] has an invalid target chat")
        scope = str(item.get("scope") or "")
        if scope not in _ALLOWED_OUTBOUND_SCOPES:
            raise ResumeEvidenceError(f"outbound[{index}] has an invalid scope")
        caption = str(item.get("caption") or "")
        if len(caption) > 1024:
            raise ResumeEvidenceError(f"outbound[{index}] caption is too long")
        reply_to = item.get("reply_to_message_id")
        if (reply_to is not None
                and (isinstance(reply_to, bool) or not isinstance(reply_to, (str, int))
                     or not str(reply_to).strip())):
            raise ResumeEvidenceError(f"outbound[{index}] has an invalid reply target")
        voice_note = item.get("voice_note")
        if not isinstance(voice_note, bool) or (voice_note and kind != "audio"):
            raise ResumeEvidenceError(f"outbound[{index}] has an invalid voice_note flag")
        queue_id = str(item.get("queue_id") or "")
        if not _QUEUE_ID.fullmatch(queue_id) or queue_id in queue_ids:
            raise ResumeEvidenceError(f"outbound[{index}] has an invalid/duplicate queue id")
        queue_ids.add(queue_id)
        descriptor_run = str(item.get("run_id") or "")
        if descriptor_run != run_id:
            raise ResumeEvidenceError(f"outbound[{index}] belongs to another run")
        guard_note = item.get("guard_note", "")
        if not isinstance(guard_note, str) or len(guard_note) > 64 * 1024:
            raise ResumeEvidenceError(f"outbound[{index}] has an invalid guard note")
        restored.append(OutboundDescriptor(
            kind=kind, path=str(path), mime=mime, size=size,
            target_chat_id=target, scope=scope, caption=caption,
            reply_to_message_id=reply_to, voice_note=voice_note,
            queue_id=queue_id, run_id=descriptor_run, sha256=digest,
            guard_note=guard_note,
        ))
    return tuple(restored)


def _validate_events(run_id: str, events: list[dict], manifest: dict, *,
                     budget: _EvidenceBudget | None = None) -> None:
    if not events:
        raise ResumeEvidenceError("run has no event stream")
    reduced_status = "pending"
    model_state: dict[str, dict[str, int]] = {}
    for expected_seq, row in enumerate(events, 1):
        if budget is not None and expected_seq % 512 == 1:
            budget.check_time("validating event evidence")
        if not isinstance(row, dict) or row.get("schema") != EVENT_SCHEMA:
            raise ResumeEvidenceError(f"event {expected_seq} has an invalid schema")
        if row.get("run_id") != run_id:
            raise ResumeEvidenceError(f"event {expected_seq} crossed run boundary")
        seq = _integer(row.get("seq"), f"event[{expected_seq}].seq", minimum=1)
        if seq != expected_seq or row.get("id") != f"{run_id}:evt:{seq:08d}":
            raise ResumeEvidenceError("event sequence/id is not contiguous")
        if not str(row.get("at") or ""):
            raise ResumeEvidenceError(f"event {seq} has no timestamp")
        kind = str(row.get("kind") or "")
        if expected_seq == 1 and kind != "run_created":
            raise ResumeEvidenceError("first event is not run_created")
        if kind == "status_changed":
            before = str(row.get("from_status") or "")
            after = str(row.get("to_status") or "")
            if before != reduced_status or after not in _KNOWN_STATUSES:
                raise ResumeEvidenceError("status event ordering is inconsistent")
            reduced_status = after
        call_id = str(row.get("call_id") or "")
        if kind in {"model_input", "model_started", "model_output", "model_completed"}:
            if not call_id:
                raise ResumeEvidenceError(f"{kind} has no call_id")
            state = model_state.setdefault(call_id, {})
            if kind in state:
                raise ResumeEvidenceError(f"duplicate {kind} for {call_id}")
            if kind == "model_started" and "model_input" not in state:
                raise ResumeEvidenceError("model_started precedes model_input")
            if kind == "model_output" and "model_started" not in state:
                raise ResumeEvidenceError("model_output precedes model_started")
            if kind == "model_completed" and "model_output" not in state:
                raise ResumeEvidenceError("model_completed precedes model_output")
            state[kind] = seq
        if "result" in row:
            _validate_result_ref(run_id, row.get("result"))
    if _integer(manifest.get("event_seq"), "manifest.event_seq") != len(events):
        raise ResumeEvidenceError("manifest event cursor differs from WAL")
    if str(manifest.get("status") or "") != reduced_status:
        raise ResumeEvidenceError("manifest status differs from WAL reduction")


def _validate_snapshot(run_id: str, manifest: dict, summary: dict,
                       context: RunContext, events: list[dict],
                       outstanding: dict[str, dict], *,
                       budget: _EvidenceBudget | None = None) -> None:
    if str((manifest.get("context") or {}).get("run_id") or "") != run_id:
        raise ResumeEvidenceError("manifest context belongs to another run")
    if context.run_id != run_id:
        raise ResumeEvidenceError("public RunContext belongs to another run")
    status = str(manifest.get("status") or "")
    if context.status != status:
        raise ResumeEvidenceError("RunContext status differs from manifest")
    if summary.get("schema") != STATUS_SCHEMA or summary.get("run_id") != run_id:
        raise ResumeEvidenceError("public status view is malformed")
    if summary.get("status") != status:
        raise ResumeEvidenceError("public status differs from manifest")
    if dict(summary.get("requested_control") or {}) != dict(manifest.get("control") or {}):
        raise ResumeEvidenceError("control view differs from manifest")
    if sorted(outstanding) != sorted(summary.get("outstanding_call_ids") or ()):
        raise ResumeEvidenceError("outstanding tool views disagree")
    if summary.get("unknown_result_call_ids") or summary.get("conflicting_start_call_ids"):
        raise ResumeEvidenceError("tool receipt ledger is inconsistent")
    _validate_events(run_id, events, manifest, budget=budget)


def _event_result_json(manager: RunManagerReader, run_id: str, row: dict, *,
                       expected_name: str, max_result_bytes: int,
                       budget: _EvidenceBudget | None = None) -> dict:
    if row.get("name") != expected_name:
        raise ResumeEvidenceError(f"{row.get('kind')} has an unexpected result name")
    return read_full_json_result(
        manager, run_id, row.get("result"), max_bytes=max_result_bytes,
        budget=budget,
    )


def _validate_model_input(value: dict) -> dict:
    if not isinstance(value.get("messages"), list) or not isinstance(value.get("tools"), list):
        raise ResumeEvidenceError("model input lacks exact messages/tools lists")
    if "system" not in value:
        raise ResumeEvidenceError("model input lacks its exact system value")
    return copy.deepcopy(value)


def _offered_tool_names(model_input: dict) -> frozenset[str]:
    try:
        return tool_offerings.local_function_names(model_input.get("tools") or ())
    except tool_offerings.ToolOfferingError as exc:
        raise ResumeEvidenceError(str(exc)) from exc


def _validate_model_output(value: dict) -> dict:
    if not isinstance(value.get("text"), str):
        raise ResumeEvidenceError("model output text is malformed")
    if not isinstance(value.get("blocks"), list):
        raise ResumeEvidenceError("model output blocks are malformed")
    if not isinstance(value.get("stop_reason"), str):
        raise ResumeEvidenceError("model output stop_reason is malformed")
    return copy.deepcopy(value)


def _checkpoint_value(manager: RunManagerReader, run_id: str, row: dict, *,
                      max_result_bytes: int,
                      budget: _EvidenceBudget | None = None) -> dict:
    value = _event_result_json(
        manager, run_id, row, expected_name="tool-loop-checkpoint",
        max_result_bytes=max_result_bytes, budget=budget,
    )
    if value.get("schema") != CHECKPOINT_SCHEMA:
        raise ResumeEvidenceError("tool-loop checkpoint has an invalid schema")
    _integer(value.get("iteration"), "checkpoint.iteration", minimum=1)
    if "system" not in value or not isinstance(value.get("messages"), list):
        raise ResumeEvidenceError("checkpoint lacks exact system/messages")
    if not isinstance(value.get("tools"), list) or not isinstance(value.get("outbound"), list):
        raise ResumeEvidenceError("checkpoint lacks exact tools/outbound lists")
    return copy.deepcopy(value)


def _result_outbound_descriptors(
    row: dict,
    *,
    run_id: str,
    allowed_roots: Sequence[str | os.PathLike[str]],
    budget: _EvidenceBudget | None = None,
) -> tuple[OutboundDescriptor, ...]:
    metadata = row.get("metadata")
    if metadata is None:
        return ()
    if (not isinstance(metadata, dict)
            or metadata.get("schema") != TOOL_RESULT_METADATA_SCHEMA
            or not isinstance(metadata.get("outbound"), list)):
        raise ResumeEvidenceError("tool_result carries malformed durability metadata")
    return restore_outbound_descriptors(
        {"outbound": copy.deepcopy(metadata["outbound"])},
        run_id=run_id, allowed_roots=allowed_roots, budget=budget,
    )


def _same_descriptor(left: OutboundDescriptor, right: OutboundDescriptor) -> bool:
    return left.as_dict() == right.as_dict()


def _merge_result_outbound(
    checkpoint_outbound: tuple[OutboundDescriptor, ...],
    result_outbound: tuple[OutboundDescriptor, ...],
    *,
    checkpoint_includes_results: bool,
) -> tuple[OutboundDescriptor, ...]:
    by_id = {item.queue_id: item for item in checkpoint_outbound}
    if len(by_id) != len(checkpoint_outbound):
        raise ResumeEvidenceError("checkpoint has duplicate outbound queue ids")
    if checkpoint_includes_results:
        for item in result_outbound:
            existing = by_id.get(item.queue_id)
            if existing is None or not _same_descriptor(existing, item):
                raise ResumeEvidenceError(
                    "post-tool checkpoint omitted or changed atomic tool outbound")
        return checkpoint_outbound
    merged = list(checkpoint_outbound)
    for item in result_outbound:
        if item.queue_id in by_id:
            raise ResumeEvidenceError(
                "uncheckpointed tool outbound duplicates an older queue id")
        by_id[item.queue_id] = item
        merged.append(item)
    return tuple(merged)


def _guarded_media_ids(
    manager: RunManagerReader,
    run_id: str,
    events: list[dict],
    *,
    output_row: dict,
    model_output: dict,
    scope: str,
    max_result_bytes: int,
    budget: _EvidenceBudget | None = None,
) -> tuple[str, ...] | None:
    draft = str(model_output.get("text") or "")
    draft_sha = hashlib.sha256(draft.encode("utf-8")).hexdigest()
    input_rows = [row for row in events if row.get("kind") == "outbound_guard_input"]
    all_receipt_rows = [row for row in events if row.get("kind") == "outbound_guard_result"]
    current_call_id = f"{OUTBOUND_GUARD_CALL_PREFIX}:{run_id}"
    legacy_call_id = f"{LEGACY_OUTBOUND_GUARD_CALL_PREFIX}:{run_id}"
    receipt_rows = [row for row in all_receipt_rows if row.get("call_id") == current_call_id]
    legacy_receipt_rows = [row for row in all_receipt_rows
                           if row.get("call_id") == legacy_call_id]
    unknown_receipts = [row for row in all_receipt_rows
                        if row.get("call_id") not in {current_call_id, legacy_call_id}]
    if unknown_receipts:
        raise ResumeEvidenceError("outbound guard receipt has invalid identity")
    if (len(input_rows) > 1 or len(receipt_rows) > 1
            or (scope == "group" and len(legacy_receipt_rows) > 1)):
        raise ResumeEvidenceError("outbound guard evidence is duplicated")

    input_ids: tuple[str, ...] | None = None
    if input_rows:
        row = input_rows[0]
        if (int(row.get("seq") or 0) <= int(output_row["seq"])
                or row.get("call_id") != f"outbound-guard-input:{run_id}"):
            raise ResumeEvidenceError("outbound guard input has invalid ordering/identity")
        value = _event_result_json(
            manager, run_id, row, expected_name="outbound-guard-input",
            max_result_bytes=max_result_bytes, budget=budget,
        )
        ids = value.get("media_queue_ids")
        if (value.get("schema") != OUTBOUND_GUARD_INPUT_SCHEMA
                or value.get("draft_sha256") != draft_sha
                or not isinstance(ids, list)
                or any(not isinstance(item, str) or not _QUEUE_ID.fullmatch(item)
                       for item in ids)
                or len(ids) != len(set(ids))):
            raise ResumeEvidenceError("outbound guard input is malformed")
        input_ids = tuple(ids)

    receipt_ids: tuple[str, ...] | None = None
    selected_receipts = receipt_rows or (legacy_receipt_rows if scope == "group" else [])
    if selected_receipts:
        row = selected_receipts[0]
        selected_legacy = row.get("call_id") == legacy_call_id
        expected_call_id = legacy_call_id if selected_legacy else current_call_id
        expected_schemas = ({LEGACY_OUTBOUND_GUARD_SCHEMA} if selected_legacy else {
            OUTBOUND_GUARD_SCHEMA, PREVIOUS_OUTBOUND_GUARD_SCHEMA,
        })
        if (int(row.get("seq") or 0) <= int(output_row["seq"])
                or (input_rows and int(row["seq"]) <= int(input_rows[0]["seq"]))
                or row.get("call_id") != expected_call_id):
            raise ResumeEvidenceError("outbound guard receipt has invalid ordering/identity")
        value = _event_result_json(
            manager, run_id, row, expected_name="outbound-guard",
            max_result_bytes=max_result_bytes, budget=budget,
        )
        ids = value.get("media_queue_ids")
        decision_fields_ok = (selected_legacy
                              or value.get("schema") == PREVIOUS_OUTBOUND_GUARD_SCHEMA
                              or all(
                                  isinstance(value.get(field), str)
                                  for field in ("advisor", "advisor_verdict", "advisor_reason",
                                                "praxis_decision")
                              ))
        if (value.get("schema") not in expected_schemas
                or value.get("draft_sha256") != draft_sha
                or not isinstance(value.get("text"), str)
                or not decision_fields_ok
                or not isinstance(ids, list)
                or any(not isinstance(item, str) or not _QUEUE_ID.fullmatch(item)
                       for item in ids)
                or len(ids) != len(set(ids))):
            raise ResumeEvidenceError("outbound guard receipt is malformed")
        receipt_ids = tuple(ids)
    if input_ids is not None and receipt_ids is not None and input_ids != receipt_ids:
        raise ResumeEvidenceError("outbound guard input/result media sets differ")
    return receipt_ids if receipt_ids is not None else input_ids


def _checkpoint_for_media_ids(checkpoint: dict, queue_ids: Sequence[str]) -> dict:
    wanted = list(queue_ids)
    wanted_set = set(wanted)
    filtered = [
        item for item in checkpoint.get("outbound") or ()
        if isinstance(item, dict) and str(item.get("queue_id") or "") in wanted_set
    ]
    if [str(item.get("queue_id") or "") for item in filtered] != wanted:
        raise ResumeEvidenceError("durable media ids differ from checkpoint order/content")
    return {**checkpoint, "outbound": filtered}


def _completed_transport_media(
    manager: RunManagerReader,
    run_id: str,
    events: list[dict],
    *,
    max_result_bytes: int,
    budget: _EvidenceBudget | None = None,
) -> frozenset[str]:
    completed: set[str] = set()
    for index, row in enumerate(events, 1):
        if budget is not None and index % 512 == 1:
            budget.check_time("reconstructing Telegram media receipts")
        if row.get("kind") != "tool_result" or row.get("name") != "telegram-media":
            continue
        call_id = str(row.get("call_id") or "")
        if not call_id.startswith("delivery-media:"):
            raise ResumeEvidenceError("Telegram media receipt has an invalid call_id")
        value = _event_result_json(
            manager, run_id, row, expected_name="telegram-media",
            max_result_bytes=max_result_bytes, budget=budget,
        )
        queue_id = str(value.get("queue_id") or "")
        if (not _QUEUE_ID.fullmatch(queue_id)
                or call_id != f"delivery-media:{queue_id}"
                or value.get("ok") is not True):
            raise ResumeEvidenceError("Telegram media receipt is malformed")
        if queue_id in completed:
            raise ResumeEvidenceError("Telegram media receipt is duplicated")
        completed.add(queue_id)
    return frozenset(completed)


def _latest_status_change(events: list[dict], status: str) -> dict | None:
    for row in reversed(events):
        if row.get("kind") == "status_changed" and row.get("to_status") == status:
            return row
    return None


def _is_recovery_pause(events: list[dict], status: str, control: dict) -> bool:
    if status != "paused" or control:
        return False
    row = _latest_status_change(events, "paused")
    reason = str((row or {}).get("reason") or "")
    recovered = bool(
        row
        and not row.get("control_action")
        and not row.get("requested_by")
        and (reason in _RECOVERY_PAUSE_REASONS
             or _DIRECT_OUTBOX_PAUSE.fullmatch(reason))
    )
    if recovered:
        return True
    # A human pause is resumable only after a later, durable owner
    # authorization cleared the control request.  The executor still has to
    # acquire the ordinary revision/event cursor lease before any effects.
    if not row:
        return False
    authorization = next(
        (item for item in reversed(events)
         if item.get("kind") == "resume_authorized"),
        None,
    )
    return bool(
        authorization
        and int(authorization.get("seq") or 0) > int(row.get("seq") or 0)
        and str(authorization.get("requested_by") or "").strip()
    )


def _transport_intent(run_id: str, context: RunContext, events: list[dict]) -> dict | None:
    found = [row for row in events
             if row.get("kind") == "tool_started" and row.get("tool") == "telegram.deliver"]
    if not found:
        return None
    if len(found) != 1:
        raise ResumeEvidenceError("run has multiple Telegram delivery intents")
    row = found[0]
    if row.get("call_id") != f"delivery:{run_id}":
        raise ResumeEvidenceError("Telegram delivery intent has an invalid call_id")
    if row.get("side_effect") is not True or row.get("idempotent") is not True:
        raise ResumeEvidenceError("Telegram delivery intent is not marked idempotent")
    if row.get("idempotency_key") != f"telegram-delivery:{run_id}":
        raise ResumeEvidenceError("Telegram delivery intent has an invalid idempotency key")
    args = row.get("args")
    route = str(args.get("chat_id") or "").strip() if isinstance(args, dict) else ""
    if not isinstance(args, dict) or not route or route.lower() in {"none", "null"}:
        raise ResumeEvidenceError("Telegram delivery intent has no exact route")
    if context.delivery_chat_id is not None and str(args["chat_id"]) != context.delivery_chat_id:
        raise ResumeEvidenceError("Telegram delivery route differs from RunContext")
    for later in events[int(row["seq"]):]:
        if later.get("kind") in {"model_input", "model_started", "model_output", "run_checkpoint"}:
            raise ResumeEvidenceError("model authoring continued after Telegram delivery intent")
    return copy.deepcopy(row)


def _model_pair(manager: RunManagerReader, run_id: str, output_row: dict,
                events: list[dict], *, max_result_bytes: int,
                budget: _EvidenceBudget | None = None) -> tuple[dict, dict]:
    call_id = str(output_row.get("call_id") or "")
    inputs = [row for row in events
              if row.get("kind") == "model_input" and row.get("call_id") == call_id]
    if len(inputs) != 1 or int(inputs[0]["seq"]) >= int(output_row["seq"]):
        raise ResumeEvidenceError("model output has no unique earlier model input")
    model_input = _validate_model_input(_event_result_json(
        manager, run_id, inputs[0], expected_name="model-input",
        max_result_bytes=max_result_bytes, budget=budget,
    ))
    model_output = _validate_model_output(_event_result_json(
        manager, run_id, output_row, expected_name="model-output",
        max_result_bytes=max_result_bytes, budget=budget,
    ))
    return model_input, model_output


def _tool_calls_for_output(manager: RunManagerReader, run_id: str, output_row: dict,
                           output: dict, events: list[dict], offered_tools: frozenset[str],
                           outstanding: dict[str, dict], *,
                           max_result_bytes: int,
                           outbound_roots: Sequence[str | os.PathLike[str]],
                           budget: _EvidenceBudget | None = None,
                           ) -> tuple[tuple[ToolCallResume, ...],
                                      tuple[OutboundDescriptor, ...]]:
    blocks = output.get("blocks") or []
    raw_calls = [block for block in blocks
                 if isinstance(block, dict) and block.get("type") == "tool_use"]
    if output.get("stop_reason") == "tool_use" and not raw_calls:
        raise ResumeEvidenceError("tool_use model output has no tool_use blocks")
    ids: set[str] = set()
    plans: list[ToolCallResume] = []
    result_outbound: list[OutboundDescriptor] = []
    result_queue_ids: set[str] = set()
    output_seq = int(output_row["seq"])
    for index, block in enumerate(raw_calls, 1):
        if budget is not None and index % 128 == 1:
            budget.check_time("reconstructing model tool evidence")
        call_id = str(block.get("id") or "")
        name = str(block.get("name") or "")
        raw_input = block.get("input")
        if (not call_id or call_id in ids or not name or not isinstance(raw_input, dict)):
            raise ResumeEvidenceError("model tool block id/name/input is malformed")
        if name not in offered_tools:
            raise ResumeEvidenceError(
                f"model requested tool {name!r} that was not in its exact input")
        # Live execution removes explicit JSON nulls so Python tool defaults
        # remain authoritative.  Preserve the verbatim block in model_output,
        # but plan and compare the exact normalized durable invocation.
        call_input = {key: value for key, value in raw_input.items()
                      if value is not None}
        ids.add(call_id)
        starts = [row for row in events
                  if row.get("kind") == "tool_started" and row.get("call_id") == call_id]
        if not starts:
            plans.append(ToolCallResume(
                call_id=call_id, name=name, input=copy.deepcopy(call_input),
                state="pending_start",
            ))
            continue
        if len(starts) != 1 or int(starts[0]["seq"]) <= output_seq:
            raise ResumeEvidenceError(f"tool {call_id} has invalid start ordering")
        started = starts[0]
        if started.get("tool") != name or started.get("args") != call_input:
            raise ResumeEvidenceError(f"tool {call_id} intent differs from model block")
        if call_id in outstanding:
            read_only = started.get("side_effect") is False
            keyed_idempotent = (
                started.get("side_effect") is True
                and started.get("idempotent") is True
                and bool(str(started.get("idempotency_key") or "").strip())
            )
            plans.append(ToolCallResume(
                call_id=call_id, name=name, input=copy.deepcopy(call_input),
                state="outstanding", replayable=bool(read_only or keyed_idempotent),
                replay_basis=("read_only" if read_only else
                              "idempotency_key" if keyed_idempotent else ""),
            ))
            continue
        results = [row for row in events
                   if (row.get("kind") == "tool_result" and row.get("call_id") == call_id
                       and int(row.get("seq") or 0) > int(started["seq"]))]
        if len(results) != 1:
            raise ResumeEvidenceError(f"completed tool {call_id} has no unique ResultRef")
        ref = _validate_result_ref(run_id, results[0].get("result"))
        read_full_result_bytes(
            manager, run_id, ref, max_bytes=max_result_bytes, budget=budget,
        )
        if results[0].get("name") != name:
            raise ResumeEvidenceError(f"tool {call_id} result name differs from intent")
        plans.append(ToolCallResume(
            call_id=call_id, name=name, input=copy.deepcopy(call_input),
            state="completed", result_ref=ref,
        ))
        for descriptor in _result_outbound_descriptors(
                results[0], run_id=run_id, allowed_roots=outbound_roots,
                budget=budget):
            if descriptor.queue_id in result_queue_ids:
                raise ResumeEvidenceError(
                    "atomic tool results duplicate an outbound queue id")
            result_queue_ids.add(descriptor.queue_id)
            result_outbound.append(descriptor)
    unrelated = sorted(set(outstanding).difference(ids))
    if unrelated:
        raise ResumeEvidenceError(
            "outstanding tools do not belong to the latest model response: "
            + ", ".join(unrelated)
        )
    return tuple(plans), tuple(result_outbound)


def _base_plan(run_id: str, kind: PlanKind, status: str, reason: str, *,
               manifest: dict, context: RunContext | None, auto_resume: bool = False,
               **kwargs: Any) -> ResumePlan:
    return ResumePlan(
        run_id=run_id, kind=kind, status=status, reason=reason,
        revision=int(manifest.get("revision") or 0),
        event_seq=int(manifest.get("event_seq") or 0),
        auto_resume=auto_resume, context=context, **kwargs,
    )


def plan_resume(manager: RunManagerReader, run_id: str, *,
                outbound_roots: Sequence[str | os.PathLike[str]] = (),
                max_result_bytes: int = 64 * 1024 * 1024,
                max_evidence_bytes: int = DEFAULT_MAX_EVIDENCE_BYTES,
                max_events: int = DEFAULT_MAX_EVENTS,
                max_planning_seconds: float = DEFAULT_MAX_PLANNING_SECONDS) -> ResumePlan:
    """Reduce durable evidence to a strict plan without changing run state.

    The returned ``revision`` and ``event_seq`` are a lease, not decoration: an
    executor must reject the plan if either value changed before it acquires
    execution ownership.
    """

    context: RunContext | None = None
    manifest: dict = {"revision": 0, "event_seq": 0, "status": "unknown"}
    try:
        evidence_cap = _integer(
            max_evidence_bytes, "max_evidence_bytes", minimum=1,
        )
        event_cap = _integer(max_events, "max_events", minimum=1)
        if (isinstance(max_planning_seconds, bool)
                or not isinstance(max_planning_seconds, (int, float))
                or not math.isfinite(float(max_planning_seconds))
                or max_planning_seconds <= 0):
            raise ResumeEvidenceError("max_planning_seconds must be > 0")
        budget = _EvidenceBudget(
            max_bytes=evidence_cap,
            deadline_monotonic=time.monotonic() + float(max_planning_seconds),
        )
        budget.check_time("starting resume planning")
        before = manager.manifest(run_id)
        budget.check_time("reading manifest")
        manifest = copy.deepcopy(before)
        events = manager.events(
            run_id, strict=True, max_events=event_cap,
            max_bytes=evidence_cap,
            deadline_monotonic=budget.deadline_monotonic,
        )
        budget.account_events(events)
        context = manager.context(run_id)
        budget.check_time("reading RunContext")
        summary = manager.status(
            run_id, max_events=event_cap, max_bytes=evidence_cap,
            deadline_monotonic=budget.deadline_monotonic,
        )
        budget.check_time("reducing public status")
        outstanding = manager.outstanding_tools(
            run_id, max_events=event_cap, max_bytes=evidence_cap,
            deadline_monotonic=budget.deadline_monotonic,
        )
        budget.check_time("reducing outstanding tools")
        _validate_snapshot(
            run_id, manifest, summary, context, events, outstanding,
            budget=budget,
        )
        budget.check_time("validating durable snapshot")
        status = str(manifest.get("status") or "")
        control = dict(manifest.get("control") or {})

        after = manager.manifest(run_id)
        budget.check_time("checking planning lease")
        stable_fields = ("revision", "event_seq", "status", "updated_at")
        if any(after.get(key) != manifest.get(key) for key in stable_fields):
            raise ResumeEvidenceError("run changed while its resume evidence was read")

        if status in _TERMINAL:
            return _base_plan(
                run_id, "not_resumable", status, f"run is terminal ({status})",
                manifest=manifest, context=context,
            )
        if control.get("action"):
            return _base_plan(
                run_id, "blocked", status,
                f"durable {control.get('action')} control is pending",
                manifest=manifest, context=context,
                diagnostics=(str(control.get("requested_by") or "unknown actor"),),
            )
        pause_row = _latest_status_change(events, "paused") if status == "paused" else None
        recovery_pause = _is_recovery_pause(events, status, control)
        if (pause_row and not recovery_pause
                and pause_row.get("control_action") in {
                    "pause", "cancel_pending", "resolve_in_doubt",
                }):
            return _base_plan(
                run_id, "blocked", status,
                f"paused by audited control {pause_row.get('control_action')}",
                manifest=manifest, context=context,
                diagnostics=(str(pause_row.get("requested_by") or "unknown actor"),),
            )
        if status == "running":
            return _base_plan(
                run_id, "not_resumable", status, "run is already executing",
                manifest=manifest, context=context,
            )
        if status == "pending":
            return _base_plan(
                run_id, "not_resumable", status, "run has not started",
                manifest=manifest, context=context,
            )

        intent = _transport_intent(run_id, context, events)
        if intent is not None:
            non_transport = [
                call_id for call_id, row in outstanding.items()
                if (row.get("tool") not in {"telegram.deliver", "telegram.send_media"})
            ]
            if non_transport:
                raise ResumeEvidenceError(
                    "Telegram intent coexists with unfinished model tools: "
                    + ", ".join(sorted(non_transport))
                )
            # Transport may have crashed after persisting its parent intent but
            # before copying every checkpointed media descriptor into the
            # durable media queue.  Restore inert, hash-verified descriptors so
            # the transport reconciler can finish that handoff without a model.
            transport_checkpoint: dict | None = None
            transport_outbound: tuple[OutboundDescriptor, ...] = ()
            intent_args = dict(intent.get("args") or {})
            expected_ids = [str(value) for value
                            in intent_args.get("media_queue_ids") or ()]
            expected_count = _integer(
                intent_args.get("media_count"), "telegram.deliver.media_count",
            )
            if not expected_count and expected_ids:
                raise ResumeEvidenceError(
                    "Telegram zero-media intent carries queue ids")
            if expected_count and (len(expected_ids) != expected_count
                                   or len(expected_ids) != len(set(expected_ids))):
                raise ResumeEvidenceError(
                    "Telegram media intent has no exact ordered queue ids")
            completed_ids = _completed_transport_media(
                manager, run_id, events, max_result_bytes=max_result_bytes,
                budget=budget,
            )
            if completed_ids.difference(expected_ids):
                raise ResumeEvidenceError(
                    "Telegram media receipt does not belong to the parent intent")
            pending_ids = [item for item in expected_ids if item not in completed_ids]
            checkpoint_rows = [row for row in events if row.get("kind") == "run_checkpoint"]
            if checkpoint_rows:
                transport_checkpoint = _checkpoint_value(
                    manager, run_id, checkpoint_rows[-1],
                    max_result_bytes=max_result_bytes, budget=budget,
                )
                if pending_ids:
                    checkpoint_media = _checkpoint_for_media_ids(
                        transport_checkpoint, pending_ids,
                    )
                    transport_outbound = restore_outbound_descriptors(
                        checkpoint_media, run_id=run_id,
                        allowed_roots=outbound_roots, budget=budget,
                    )
            elif pending_ids:
                raise ResumeEvidenceError(
                    "Telegram media intent has no durable outbound checkpoint")
            return _base_plan(
                run_id, "transport_owned", status,
                "Telegram delivery intent already owns the authored output",
                manifest=manifest, context=context,
                transport_intent=intent, checkpoint=transport_checkpoint,
                outbound=transport_outbound,
            )
        if status == "in_doubt":
            # ⚠ План говорил «требуется явное сведение» и не называл ни ЧТО повисло, ни
            # чем это закрывают. Живьём такой ран висел пятые сутки
            # (run-20260722T161431426971Z-4387367e, незакрытый `narrate`), молча светясь
            # в её list_active_runs. Имена рук и имя её собственного инструмента едут в
            # `reason`; `diagnostics` остаётся ровно списком call_id — это машинное поле,
            # на его форму опираются вызывающие.
            named = ", ".join(
                f"{call_id} ({str((row or {}).get('tool') or 'unknown tool')})"
                for call_id, row in sorted(outstanding.items())
            )
            return _base_plan(
                run_id, "in_doubt", status,
                "uncertain non-transport effect requires explicit reconciliation"
                + (f"; outstanding: {named}; Praxis closes these with reconcile_run"
                   if named else ""),
                manifest=manifest, context=context,
                diagnostics=tuple(sorted(outstanding)),
            )
        if status == "blocked":
            return _base_plan(
                run_id, "blocked", status,
                "run is blocked without a transport-owned recovery intent",
                manifest=manifest, context=context,
            )
        if status != "paused" or not recovery_pause:
            return _base_plan(
                run_id, "blocked", status,
                "pause is not an automatic process-recovery pause",
                manifest=manifest, context=context,
            )

        outputs = [row for row in events if row.get("kind") == "model_output"]
        checkpoints = [row for row in events if row.get("kind") == "run_checkpoint"]
        latest_output = outputs[-1] if outputs else None
        latest_checkpoint = checkpoints[-1] if checkpoints else None
        checkpoint: dict | None = None
        outbound: tuple[OutboundDescriptor, ...] = ()
        if latest_checkpoint is not None:
            checkpoint = _checkpoint_value(
                manager, run_id, latest_checkpoint,
                max_result_bytes=max_result_bytes, budget=budget,
            )

        # A model_input/model_started newer than the latest durable output is a
        # crashed model request, not permission to fall back to an older answer.
        # A preceding checkpoint is the only supported exact restart boundary.
        model_frontier = [row for row in events
                          if row.get("kind") in {"model_input", "model_started", "model_output"}]
        newest_model_event = model_frontier[-1] if model_frontier else None
        if (newest_model_event is not None
                and newest_model_event.get("kind") != "model_output"
                and (latest_output is None
                     or int(newest_model_event["seq"]) > int(latest_output["seq"]))):
            if latest_checkpoint is None:
                return _base_plan(
                    run_id, "not_resumable", status,
                    "interrupted model request has no preceding tool-loop checkpoint",
                    manifest=manifest, context=context,
                )
            if int(latest_checkpoint["seq"]) >= int(newest_model_event["seq"]):
                raise ResumeEvidenceError("checkpoint ordering crosses an unfinished model request")
            if outstanding:
                raise ResumeEvidenceError("unfinished model request coexists with outstanding tools")
            outbound = restore_outbound_descriptors(
                checkpoint, run_id=run_id, allowed_roots=outbound_roots,
                budget=budget,
            )
            return _base_plan(
                run_id, "continue_checkpoint", status,
                "retry the interrupted model request from its preceding exact checkpoint",
                manifest=manifest, context=context, auto_resume=True,
                checkpoint=checkpoint, outbound=outbound,
            )

        if latest_output is None:
            if latest_checkpoint is None:
                return _base_plan(
                    run_id, "not_resumable", status,
                    "recovery pause has no persisted model output or checkpoint",
                    manifest=manifest, context=context,
                )
            if outstanding:
                raise ResumeEvidenceError("checkpoint coexists with outstanding tools")
            outbound = restore_outbound_descriptors(
                checkpoint, run_id=run_id, allowed_roots=outbound_roots,
                budget=budget,
            )
            return _base_plan(
                run_id, "continue_checkpoint", status,
                "continue from the latest hash-verified tool-loop checkpoint",
                manifest=manifest, context=context, auto_resume=True,
                checkpoint=checkpoint, outbound=outbound,
            )

        model_input, model_output = _model_pair(
            manager, run_id, latest_output, events,
            max_result_bytes=max_result_bytes, budget=budget,
        )
        output_seq = int(latest_output["seq"])
        checkpoint_seq = int(latest_checkpoint["seq"]) if latest_checkpoint else 0
        stop_reason = str(model_output.get("stop_reason") or "")

        if stop_reason != "tool_use":
            if checkpoint_seq > output_seq:
                raise ResumeEvidenceError("checkpoint appears after a terminal model output")
            if outstanding:
                raise ResumeEvidenceError("terminal model output coexists with outstanding tools")
            guarded_ids = _guarded_media_ids(
                manager, run_id, events, output_row=latest_output,
                model_output=model_output, scope=context.scope,
                max_result_bytes=max_result_bytes,
                budget=budget,
            )
            if checkpoint is not None:
                media_checkpoint = (checkpoint if guarded_ids is None
                                    else _checkpoint_for_media_ids(checkpoint, guarded_ids))
                outbound = restore_outbound_descriptors(
                    media_checkpoint, run_id=run_id,
                    allowed_roots=outbound_roots, budget=budget,
                )
            elif guarded_ids:
                raise ResumeEvidenceError(
                    "outbound guard receipt has media but no durable checkpoint")
            return _base_plan(
                run_id, "authored_output", status,
                "terminal model output is durable and must be delivered without re-authoring",
                manifest=manifest, context=context, auto_resume=True,
                model_input=model_input, model_output=model_output,
                checkpoint=checkpoint, outbound=outbound,
            )

        checkpoint_outbound = (restore_outbound_descriptors(
            checkpoint, run_id=run_id, allowed_roots=outbound_roots,
            budget=budget,
        ) if checkpoint is not None else ())
        tool_calls, result_outbound = _tool_calls_for_output(
            manager, run_id, latest_output, model_output, events,
            _offered_tool_names(model_input), outstanding,
            max_result_bytes=max_result_bytes, outbound_roots=outbound_roots,
            budget=budget,
        )
        if checkpoint_seq > output_seq:
            if outstanding:
                raise ResumeEvidenceError("post-response checkpoint has outstanding tools")
            outbound = _merge_result_outbound(
                checkpoint_outbound, result_outbound,
                checkpoint_includes_results=True,
            )
            return _base_plan(
                run_id, "continue_checkpoint", status,
                "latest checkpoint includes the persisted tool response batch",
                manifest=manifest, context=context, auto_resume=True,
                model_input=model_input, model_output=model_output,
                checkpoint=checkpoint, outbound=outbound, tool_calls=tool_calls,
            )

        outbound = _merge_result_outbound(
            checkpoint_outbound, result_outbound,
            checkpoint_includes_results=False,
        )
        unsafe = [item.call_id for item in tool_calls
                  if item.state == "outstanding" and not item.replayable]
        if unsafe:
            return _base_plan(
                run_id, "in_doubt", status,
                "uncheckpointed model response has non-idempotent outstanding effects",
                manifest=manifest, context=context,
                model_input=model_input, model_output=model_output,
                checkpoint=checkpoint, outbound=outbound, tool_calls=tool_calls,
                diagnostics=tuple(unsafe),
            )
        return _base_plan(
            run_id, "replay_model_tool_response", status,
            "replay exact persisted model blocks; only marked outstanding calls may re-execute",
            manifest=manifest, context=context, auto_resume=True,
            model_input=model_input, model_output=model_output,
            checkpoint=checkpoint, outbound=outbound, tool_calls=tool_calls,
        )
    except ResumeEvidenceError as exc:
        return _base_plan(
            run_id, "blocked", str(manifest.get("status") or "unknown"),
            f"resume evidence rejected: {exc}", manifest=manifest, context=context,
            diagnostics=(type(exc).__name__,),
        )
    except Exception as exc:
        # A strict planner cannot turn an unreadable WAL/result into permission
        # to execute.  Preserve the failure as a blocked plan for owner audit.
        return _base_plan(
            run_id, "blocked", str(manifest.get("status") or "unknown"),
            f"resume evidence unreadable: {type(exc).__name__}: {exc}",
            manifest=manifest, context=context,
            diagnostics=(type(exc).__name__,),
        )


__all__ = [
    "CHECKPOINT_SCHEMA",
    "DEFAULT_MAX_EVIDENCE_BYTES",
    "DEFAULT_MAX_EVENTS",
    "DEFAULT_MAX_PLANNING_SECONDS",
    "OutboundDescriptor",
    "PLAN_SCHEMA",
    "ResumeEvidenceError",
    "ResumePlan",
    "ToolCallResume",
    "plan_resume",
    "read_full_json_result",
    "read_full_result_bytes",
    "restore_outbound_descriptors",
]
