"""Durable server-side execution spine for Praxis runs.

One run owns a human-readable directory under ``memory/runs/YYYY-MM``::

    manifest.json
    context.md
    events.jsonl
    results/
    artifacts/
    RECAP.md

The manifest is replaced atomically.  Events are append-only, full tool results
are externalised before an inline slice is returned, and recovery never guesses
that an interrupted side effect did or did not happen.  This module contains no
LLM and no Telegram lifecycle code; those layers bind :class:`RunContext` and
drive the state machine explicitly.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterator

from process_liveness import is_process_alive
from run_context import RUN_STATUSES, RunContext


SCHEMA = "praxis.run.v1"
EVENT_SCHEMA = "praxis.run.event.v1"
RESULT_SCHEMA = "praxis.result-ref.v1"
ARTIFACT_SCHEMA = "praxis.artifact-ref.v1"
STATUS_SCHEMA = "praxis.run.status.v1"
LISTING_SCHEMA = "praxis.run.listing.v1"

TERMINAL_STATUSES = frozenset({"done", "cancelled", "failed"})
# These are the statuses surfaced by the Praxis owner UI's attention filter.
# ``failed`` is terminal but must not disappear behind a recent-history limit.
ATTENTION_STATUSES = frozenset({"paused", "blocked", "in_doubt", "failed"})
NONTERMINAL_STATUSES = frozenset(RUN_STATUSES).difference(TERMINAL_STATUSES)
TOOL_OUTCOME_KINDS = frozenset({
    "tool_result", "tool_completed", "tool_failed", "tool_reconciled",
})
TOOL_RESOLUTION_OUTCOMES = frozenset({"completed", "failed", "not_applied"})
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_TRANSITIONS = {
    "pending": frozenset({"running", "cancelled", "failed"}),
    "running": frozenset({"paused", "blocked", "in_doubt", "done", "cancelled", "failed"}),
    "paused": frozenset({"running", "blocked", "cancelled", "failed"}),
    "blocked": frozenset({"running", "paused", "cancelled", "failed"}),
    # An uncertain effect may only become paused through ``resolve_in_doubt``.
    # A provably clean legacy trap may become failed through the separately
    # audited ``recover_clean_in_doubt`` control operation. Generic transition()
    # rejects every attempt to leave in_doubt.
    "in_doubt": frozenset({"paused", "failed"}),
    "done": frozenset(),
    "cancelled": frozenset(),
    "failed": frozenset(),
}

PromotionHook = Callable[[RunContext, Path, dict], str | dict | None]


# Advisory file locks provide the cross-process exclusion.  They are released
# by the kernel when a process dies, so recovery never has to unlink a path
# which another waiter may already have acquired.  A striped local guard is
# still required: POSIX and Windows differ in how byte/file locks owned by two
# descriptors in the *same* process interact, and separate RunManager
# instances must have identical semantics on both platforms.
_RUN_LOCK_GUARDS = tuple(threading.Lock() for _ in range(257))


class RunError(RuntimeError):
    pass


class RunNotFound(RunError):
    pass


class RunConflict(RunError):
    pass


class InvalidTransition(RunError):
    pass


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _chmod(path, 0o700)


def _atomic_bytes(path: Path, payload: bytes, mode: int = 0o600) -> None:
    _ensure_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(str(tmp), flags, mode)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short atomic write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    _chmod(tmp, mode)
    try:
        os.replace(tmp, path)
        _chmod(path, mode)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _atomic_json(path: Path, data: dict) -> None:
    raw = (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_bytes(path, raw)


def _append_jsonl(path: Path, row: dict) -> None:
    _ensure_dir(path.parent)
    payload = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    # A process can die inside its final append.  Appending straight after an
    # unterminated fragment would hide the next (possibly safety-critical)
    # tool_started event inside one invalid line. Preserve the fragment as
    # evidence, then restore the last complete JSONL boundary before writing.
    try:
        with path.open("r+b") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            if size:
                stream.seek(-1, os.SEEK_END)
                terminated = stream.read(1) == b"\n"
                if not terminated:
                    # Find the last complete boundary in bounded reverse
                    # chunks; never load a long-lived run log as one blob.
                    cursor = size
                    boundary = 0
                    while cursor > 0:
                        take = min(cursor, 65536)
                        cursor -= take
                        stream.seek(cursor)
                        block = stream.read(take)
                        index = block.rfind(b"\n")
                        if index >= 0:
                            boundary = cursor + index + 1
                            break
                    stream.seek(boundary)
                    orphan = stream.read(size - boundary)
                    if orphan:
                        _atomic_bytes(
                            path.parent / f"events.corrupt-tail-{uuid.uuid4().hex[:12]}.bin",
                            orphan,
                        )
                    stream.truncate(boundary)
                    stream.flush()
                    os.fsync(stream.fileno())
    except FileNotFoundError:
        pass
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(str(path), flags, 0o600)
    try:
        # One os.write under O_APPEND keeps a short JSON event indivisible across processes.
        written = os.write(fd, payload)
        if written != len(payload):
            raise OSError(f"short append: {written}/{len(payload)} bytes")
        os.fsync(fd)
    finally:
        os.close(fd)
    _chmod(path, 0o600)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RunNotFound(str(path)) from exc
    except (TypeError, ValueError) as exc:
        raise RunError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RunError(f"expected object in {path}")
    return data


def _owner_alive(pid: int) -> bool:
    return is_process_alive(pid)


def _run_lock_guard(path: Path) -> threading.Lock:
    identity = os.path.normcase(os.path.abspath(str(path)))
    digest = hashlib.sha256(identity.encode("utf-8", "surrogatepass")).digest()
    return _RUN_LOCK_GUARDS[int.from_bytes(digest[:4], "big") % len(_RUN_LOCK_GUARDS)]


def _try_advisory_lock(fd: int) -> bool:
    """Try one exclusive kernel-managed lock without blocking."""

    try:
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _release_advisory_lock(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _slug(value: str, fallback: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip("-.")[:80]
    return clean or fallback


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _check_read_deadline(deadline_monotonic: float | None, phase: str) -> None:
    if (deadline_monotonic is not None
            and time.monotonic() > float(deadline_monotonic)):
        raise RunError(f"{phase} planning-time budget exceeded")


def _file_sha256(path: Path, *, deadline_monotonic: float | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            _check_read_deadline(deadline_monotonic, "file evidence")
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    _check_read_deadline(deadline_monotonic, "file evidence")
    return digest.hexdigest()


def _stage_file_copy(directory: Path, source: Path) -> tuple[Path, str, int]:
    """Copy one regular file to a private stage while hashing it incrementally."""
    _ensure_dir(directory)
    stage = directory / f".artifact.{os.getpid()}.{uuid.uuid4().hex[:12]}.tmp"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(str(stage), flags, 0o600)
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as input_stream, os.fdopen(fd, "wb", closefd=True) as output:
            fd = -1
            before = os.fstat(input_stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise RunConflict("artifact source is not a regular file")
            while True:
                chunk = input_stream.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            output.flush()
            os.fsync(output.fileno())
            after = os.fstat(input_stream.fileno())
        observed = source.stat()
        signature = lambda row: (
            row.st_dev, row.st_ino, row.st_size,
            getattr(row, "st_mtime_ns", int(row.st_mtime * 1_000_000_000)),
        )
        if signature(before) != signature(after) or signature(after) != signature(observed):
            raise RunConflict("artifact source changed while it was copied")
        _chmod(stage, 0o600)
        return stage, digest.hexdigest(), size
    except Exception:
        try:
            stage.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        if fd >= 0:
            os.close(fd)


def _relative(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


class RunManager:
    """Create, journal, recover and close durable runs."""

    def __init__(self, base: str | Path | None = None, *,
                 promotion_hook: PromotionHook | None = None) -> None:
        self.base = Path(base or os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent).resolve()
        self.root = self.base / "memory" / "runs"
        self.promotion_hook = promotion_hook
        self._thread_lock = threading.RLock()
        self._paths: dict[str, Path] = {}

    def _validate_id(self, run_id: str) -> str:
        value = str(run_id or "").strip()
        # ``.`` and ``..`` satisfy the character allow-list but are path
        # navigation segments, not identifiers.  Reject them before any
        # create/glob/cache operation can interpret them as directories.
        if not value or value in {".", ".."} or not _SAFE_ID.fullmatch(value):
            raise ValueError("invalid run_id")
        return value

    def _month(self, created_at: str) -> str:
        try:
            current = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            current = dt.datetime.now(dt.timezone.utc)
        return f"{current:%Y-%m}"

    def _find(self, run_id: str) -> Path:
        run_id = self._validate_id(run_id)
        cached = self._paths.get(run_id)
        if cached is not None and (cached / "manifest.json").is_file():
            return cached
        hits = list(self.root.glob(f"*/{run_id}/manifest.json")) if self.root.exists() else []
        if not hits:
            raise RunNotFound(run_id)
        if len(hits) != 1:
            raise RunConflict(f"run_id {run_id} exists in more than one month")
        path = hits[0].parent
        self._paths[run_id] = path
        return path

    @contextlib.contextmanager
    def _locked(self, run_dir: Path, timeout: float = 30.0) -> Iterator[None]:
        """Cross-thread/process lock whose stale ownership dies with its process.

        ``.run.lock`` is intentionally persistent.  The previous O_EXCL token
        protocol recovered a dead owner by unlinking the path; two waiters
        could both classify the old token as stale, then one could unlink the
        *new* owner's file and enter the critical section beside it.  Kernel
        advisory ownership has no such pathname race and is automatically
        released when a process exits or its descriptor is closed.
        """

        deadline = time.monotonic() + max(0.1, float(timeout))
        lock_path = run_dir / ".run.lock"
        guard = _run_lock_guard(lock_path)
        remaining = max(0.0, deadline - time.monotonic())
        if not guard.acquire(timeout=remaining):
            raise TimeoutError(f"run lock busy: {run_dir.name}")
        fd: int | None = None
        acquired = False
        try:
            _ensure_dir(run_dir)
            try:
                before = os.lstat(lock_path)
            except FileNotFoundError:
                before = None
            if before is not None and (
                    stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1):
                raise RunConflict(
                    f"run lock is not a single regular file: {run_dir.name}"
                )

            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(str(lock_path), flags, 0o600)
            opened = os.fstat(fd)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1):
                raise RunConflict(
                    f"run lock is not a single regular file: {run_dir.name}"
                )
            _chmod(lock_path, 0o600)
            if opened.st_size == 0:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.write(fd, b"\0") != 1:
                    raise OSError("short run lock initialization")
                os.fsync(fd)

            while True:
                if _try_advisory_lock(fd):
                    acquired = True
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"run lock busy: {run_dir.name}")
                time.sleep(min(0.02, max(0.001, deadline - time.monotonic())))

            # Diagnostic ownership only; correctness never depends on parsing
            # or deleting it.  The leading PID keeps a rolling deployment safe
            # from older token-lock code, which will wait rather than steal a
            # live advisory owner's path.
            token = f"{os.getpid()}:advisory-v1:{uuid.uuid4().hex}\n".encode("ascii")
            os.lseek(fd, 0, os.SEEK_SET)
            view = memoryview(token)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short run lock owner write")
                view = view[written:]
            os.fsync(fd)
            try:
                yield
            finally:
                if acquired:
                    with contextlib.suppress(OSError):
                        _release_advisory_lock(fd)
                    acquired = False
        finally:
            if fd is not None:
                os.close(fd)
            guard.release()

    def create(self, context: RunContext, context_markdown: str) -> RunContext:
        """Create one immutable context snapshot and an empty durable event stream."""
        if not isinstance(context, RunContext):
            raise TypeError("context must be RunContext")
        run_id = self._validate_id(context.run_id)
        created_at = context.created_at or _utc_now()
        run_dir = self.root / self._month(created_at) / run_id
        _ensure_dir(run_dir.parent)
        try:
            run_dir.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise RunConflict(f"run already exists: {run_id}") from exc
        _chmod(run_dir, 0o700)
        _ensure_dir(run_dir / "results")
        _ensure_dir(run_dir / "artifacts")
        context_path = run_dir / "context.md"
        context_payload = str(context_markdown or "").encode("utf-8")
        _atomic_bytes(context_path, context_payload)
        # Create the append target eagerly so its permissions do not depend on process umask.
        _atomic_bytes(run_dir / "events.jsonl", b"")
        persisted_context = replace(
            context,
            context_snapshot=_relative(context_path, self.base),
            status="pending",
            created_at=created_at,
        )
        manifest = {
            "schema": SCHEMA,
            "revision": 1,
            "context": persisted_context.to_dict(),
            "status": "pending",
            "created_at": created_at,
            "updated_at": created_at,
            "started_at": "",
            "terminal_at": "",
            "event_seq": 0,
            "result_seq": 0,
            "artifact_seq": 0,
            "terminal": {},
            "recap": {"status": "not_due"},
            "recovery": {},
            "control": {},
            "context_snapshot_ref": {
                "schema": "praxis.run.context-snapshot.v1",
                "path": "context.md",
                "sha256": _sha256(context_payload),
                "size": len(context_payload),
            },
        }
        _atomic_json(run_dir / "manifest.json", manifest)
        self._paths[run_id] = run_dir
        self.append_event(
            run_id, "run_created", status="pending",
            context_snapshot=dict(manifest["context_snapshot_ref"]),
        )
        return persisted_context

    def path(self, run_id: str) -> Path:
        return self._find(run_id)

    def run_ids(self) -> list[str]:
        """Return all durable run ids in stable creation-directory order."""
        if not self.root.exists():
            return []
        result: list[str] = []
        for manifest_path in sorted(self.root.glob("*/*/manifest.json")):
            run_id = manifest_path.parent.name
            try:
                self._validate_id(run_id)
            except ValueError:
                continue
            self._paths[run_id] = manifest_path.parent
            result.append(run_id)
        return result

    def manifest(self, run_id: str) -> dict:
        run_dir = self._find(run_id)
        with self._locked(run_dir):
            return self._manifest_locked(run_dir)

    def status(self, run_id: str, *, max_events: int | None = None,
               max_bytes: int | None = None,
               deadline_monotonic: float | None = None) -> dict:
        """Return a compact control-plane view derived from durable evidence."""
        run_dir = self._find(run_id)
        with self._locked(run_dir):
            manifest = self._manifest_locked(run_dir)
            ledger = self._tool_ledger_locked(
                run_dir, max_events=max_events, max_bytes=max_bytes,
                deadline_monotonic=deadline_monotonic,
            )
            return self._status_locked(manifest, ledger)

    def list_runs(self, *, statuses: set[str] | tuple[str, ...] | list[str] | None = None,
                  kind: str = "", limit: int | None = None) -> list[dict]:
        """List compact run statuses newest-first without exposing private paths.

        Selection is manifest-first: all manifests are locked and reduced from
        only their uncommitted WAL tail, then filters and the recent-history
        limit are applied before the selected runs' full tool ledgers are
        reduced.  A positive ``limit`` is therefore a history budget, not a
        safety cap: non-terminal and attention runs remain visible even when
        their number exceeds it.  An explicit zero still requests no rows.
        """
        return self.run_listing(statuses=statuses, kind=kind, limit=limit)["items"]

    @staticmethod
    def _manifest_listing_row(run_id: str, manifest: dict) -> dict:
        """Return the fields needed to filter/sort a run without reading its WAL."""
        context = dict(manifest.get("context") or {})
        status = str(manifest.get("status") or "pending")
        attention = status in ATTENTION_STATUSES
        return {
            "run_id": str(context.get("run_id") or run_id),
            "status": status,
            "kind": str(context.get("kind") or ""),
            "created_at": str(
                manifest.get("created_at") or context.get("created_at") or ""
            ),
            "attention": attention,
            "retain_in_limited_listing": (
                status in NONTERMINAL_STATUSES or attention
            ),
        }

    @staticmethod
    def _select_listing_rows(rows: list[dict], limit: int | None) -> list[dict]:
        """Apply a soft history limit while retaining operationally live rows."""
        if limit is None:
            return rows
        budget = max(0, int(limit))
        if budget == 0:
            return []
        retained = [row for row in rows if row["retain_in_limited_listing"]]
        retained_ids = {row["run_id"] for row in retained}
        slots = max(0, budget - len(retained))
        if slots:
            retained.extend(
                row for row in rows
                if row["run_id"] not in retained_ids
            )
            retained = retained[:budget]
        retained.sort(
            key=lambda row: (str(row.get("created_at") or ""), row["run_id"]),
            reverse=True,
        )
        return retained

    def run_listing(self, *,
                    statuses: set[str] | tuple[str, ...] | list[str] | None = None,
                    kind: str = "", limit: int | None = None) -> dict:
        """Project runs plus exact all-history counts from durable manifests.

        ``counts`` and ``total`` cover every manifest matching ``kind``; a
        status filter affects only returned items.  Full event-ledger reduction
        happens after filtering, sorting and selection, keeping old terminal
        history cheap while preserving the detailed status contract for every
        visible card.
        """
        wanted = set(statuses or ())
        unknown = wanted.difference(RUN_STATUSES)
        if unknown:
            raise ValueError(f"unknown run statuses: {sorted(unknown)}")
        kind_filter = str(kind or "").strip()
        indexed: list[dict] = []
        for run_id in self.run_ids():
            # ``manifest`` holds the per-run lock and reconciles only a crash
            # tail newer than its durable event cursor. It does not build the
            # historical tool-call ledger used by ``status``.
            row = self._manifest_listing_row(run_id, self.manifest(run_id))
            if kind_filter and row["kind"] != kind_filter:
                continue
            indexed.append(row)

        counts = {status: 0 for status in sorted(RUN_STATUSES)}
        for row in indexed:
            status = row["status"]
            counts[status] = counts.get(status, 0) + 1

        candidates = [
            row for row in indexed if not wanted or row["status"] in wanted
        ]
        candidates.sort(
            key=lambda row: (str(row.get("created_at") or ""), row["run_id"]),
            reverse=True,
        )
        selected = self._select_listing_rows(candidates, limit)
        items = [self.status(row["run_id"]) for row in selected]
        return {
            "schema": LISTING_SCHEMA,
            "items": items,
            "counts": counts,
            "total": len(indexed),
            "visible": len(items),
            "limited": len(items) < len(candidates),
            "active": sum(counts.get(status, 0) for status in NONTERMINAL_STATUSES),
            "attention": sum(counts.get(status, 0) for status in ATTENTION_STATUSES),
        }

    def list(self, *, statuses: set[str] | tuple[str, ...] | list[str] | None = None,
             kind: str = "", limit: int | None = None) -> list[dict]:
        """Control-plane alias for :meth:`list_runs`."""
        return self.list_runs(statuses=statuses, kind=kind, limit=limit)

    def context(self, run_id: str) -> RunContext:
        manifest = self.manifest(run_id)
        return RunContext.from_dict(manifest.get("context") or {}).with_status(
            str(manifest.get("status") or "pending"))

    def events(self, run_id: str, *, strict: bool = False,
               max_events: int | None = None,
               max_bytes: int | None = None,
               deadline_monotonic: float | None = None) -> list[dict]:
        """Read events with optional reconstruction-resource bounds.

        Ordinary callers retain the complete historical view.  Recovery
        planning supplies generous explicit bounds so a corrupt or
        adversarially large WAL fails closed before it is materialised as an
        unbounded Python list.  These are evidence-read budgets, not limits on
        how many tool calls a live run may make.
        """
        return self._read_events(
            self._find(run_id), strict=strict,
            max_events=max_events, max_bytes=max_bytes,
            deadline_monotonic=deadline_monotonic,
        )

    def iter_events(self, run_id: str, *, reverse: bool = False,
                    strict: bool = False) -> Iterator[dict]:
        """Stream events without materialising a long-lived run log.

        Reverse iteration is used by receipt lookup and WAL repair: those
        callers normally need only the newest few records and must not pay an
        ever-growing full-log read on every delivery.
        """
        run_dir = self._find(run_id)
        yield from (self._iter_events_reverse(run_dir, strict=strict)
                    if reverse else self._iter_events(run_dir, strict=strict))

    def _read_events(self, run_dir: Path, *, strict: bool = False,
                     max_events: int | None = None,
                     max_bytes: int | None = None,
                     deadline_monotonic: float | None = None) -> list[dict]:
        return list(self._iter_events(
            run_dir, strict=strict, max_events=max_events,
            max_bytes=max_bytes, deadline_monotonic=deadline_monotonic,
        ))

    def _iter_events(self, run_dir: Path, *, strict: bool = False,
                     max_events: int | None = None,
                     max_bytes: int | None = None,
                     deadline_monotonic: float | None = None) -> Iterator[dict]:
        path = run_dir / "events.jsonl"
        event_cap = None if max_events is None else max(1, int(max_events))
        byte_cap = None if max_bytes is None else max(1, int(max_bytes))
        byte_count = 0
        event_count = 0
        try:
            with path.open(encoding="utf-8") as stream:
                for line_no, line in enumerate(stream, 1):
                    if (deadline_monotonic is not None
                            and time.monotonic() > float(deadline_monotonic)):
                        raise RunError(
                            "event evidence planning-time budget exceeded"
                        )
                    byte_count += len(line.encode("utf-8"))
                    if byte_cap is not None and byte_count > byte_cap:
                        raise RunError(
                            f"event evidence byte budget exceeded "
                            f"({byte_count} > {byte_cap})"
                        )
                    if not line.endswith("\n"):
                        if strict:
                            raise RunError(f"unterminated event JSON at {path}:{line_no}")
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        if strict:
                            raise RunError(f"invalid event JSON at {path}:{line_no}")
                        continue
                    if isinstance(row, dict):
                        event_count += 1
                        if event_cap is not None and event_count > event_cap:
                            raise RunError(
                                f"event evidence count budget exceeded "
                                f"({event_count} > {event_cap})"
                            )
                        yield row
        except OSError as exc:
            raise RunError(f"cannot read event stream for {run_dir.name}: {exc}") from exc

    def _iter_events_reverse(self, run_dir: Path, *, strict: bool = False) -> Iterator[dict]:
        """Yield JSONL records newest-first using bounded reverse chunks."""
        path = run_dir / "events.jsonl"
        try:
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                cursor = stream.tell()
                drop_unterminated_tail = False
                if cursor:
                    stream.seek(-1, os.SEEK_END)
                    drop_unterminated_tail = stream.read(1) != b"\n"
                remainder = b""
                while cursor > 0:
                    take = min(cursor, 65536)
                    cursor -= take
                    stream.seek(cursor)
                    block = stream.read(take) + remainder
                    pieces = block.split(b"\n")
                    remainder = pieces[0]
                    complete = pieces[1:]
                    if drop_unterminated_tail and complete:
                        complete = complete[:-1]
                        drop_unterminated_tail = False
                    for raw in reversed(complete):
                        if not raw:
                            continue
                        try:
                            row = json.loads(raw)
                        except (UnicodeDecodeError, ValueError):
                            if strict:
                                raise RunError(f"invalid event JSON in {path}")
                            continue
                        if isinstance(row, dict):
                            yield row
                if remainder and not drop_unterminated_tail:
                    try:
                        row = json.loads(remainder)
                    except (UnicodeDecodeError, ValueError):
                        if strict:
                            raise RunError(f"invalid event JSON in {path}")
                    else:
                        if isinstance(row, dict):
                            yield row
        except OSError as exc:
            raise RunError(f"cannot read event stream for {run_dir.name}: {exc}") from exc

    @staticmethod
    def _max_numbered_file(path: Path) -> int:
        highest = 0
        for item in path.iterdir() if path.exists() else ():
            match = re.match(r"^(\d+)-", item.name)
            if match:
                highest = max(highest, int(match.group(1)))
        return highest

    def _manifest_locked(self, run_dir: Path) -> dict:
        """Load a manifest and replay any WAL events not committed before a crash."""
        path = run_dir / "manifest.json"
        manifest = _read_json(path)
        known_seq = int(manifest.get("event_seq") or 0)
        changed = False
        pending_rows: list[dict] = []
        for row in self._iter_events_reverse(run_dir):
            seq = int(row.get("seq") or 0)
            if seq <= known_seq:
                break
            pending_rows.append(row)
        # Reverse scanning stops at the manifest's durable cursor. Replay only
        # the crash tail, in original order, rather than rescanning all history.
        for row in reversed(pending_rows):
            seq = int(row.get("seq") or 0)
            kind = str(row.get("kind") or "")
            if kind == "status_changed":
                status = str(row.get("to_status") or manifest.get("status") or "pending")
                manifest["status"] = status
                if status == "running" and not manifest.get("started_at"):
                    manifest["started_at"] = row.get("at") or _utc_now()
                if status in TERMINAL_STATUSES:
                    manifest["terminal_at"] = row.get("at") or _utc_now()
                    manifest["terminal"] = {
                        "status": status, "reason": str(row.get("reason") or ""),
                    }
                    manifest["control"] = {}
                    recap = dict(manifest.get("recap") or {})
                    if recap.get("status") in (None, "not_due"):
                        recap["status"] = "pending"
                    manifest["recap"] = recap
            elif kind == "tool_result":
                result_id = str((row.get("result") or {}).get("result_id") or "")
                match = re.fullmatch(r"result-(\d+)", result_id)
                if match:
                    manifest["result_seq"] = max(
                        int(manifest.get("result_seq") or 0), int(match.group(1)))
            elif kind == "artifact_created":
                artifact_id = str((row.get("artifact") or {}).get("artifact_id") or "")
                match = re.fullmatch(r"artifact-(\d+)", artifact_id)
                if match:
                    manifest["artifact_seq"] = max(
                        int(manifest.get("artifact_seq") or 0), int(match.group(1)))
            elif kind in {"recap_written", "recap_recovered"}:
                recap = dict(manifest.get("recap") or {})
                recap.update({
                    "status": "written", "path": "RECAP.md",
                    "sha256": str(row.get("sha256") or recap.get("sha256") or ""),
                    "size": int(row.get("size") or recap.get("size") or 0),
                    "written_at": str(row.get("at") or recap.get("written_at") or ""),
                })
                recap.setdefault("promotion", {
                    "status": "pending" if self.promotion_hook is not None else "not_configured",
                })
                manifest["recap"] = recap
            elif kind == "run_promoted":
                recap = dict(manifest.get("recap") or {})
                promotion = dict(recap.get("promotion") or {})
                promotion.update({
                    "status": "done", "event_id": str(row.get("event_id") or ""),
                    "promoted_at": str(row.get("at") or ""),
                })
                recap["promotion"] = promotion
                manifest["recap"] = recap
            elif kind == "control_requested":
                manifest["control"] = {
                    "action": str(row.get("action") or ""),
                    "requested_by": str(row.get("requested_by") or ""),
                    "reason": str(row.get("reason") or ""),
                    "requested_at": str(row.get("at") or ""),
                }
            manifest["event_seq"] = max(int(manifest.get("event_seq") or 0), seq)
            manifest["updated_at"] = str(row.get("at") or manifest.get("updated_at") or _utc_now())
            changed = True

        result_seq = self._max_numbered_file(run_dir / "results")
        artifact_seq = self._max_numbered_file(run_dir / "artifacts")
        if result_seq > int(manifest.get("result_seq") or 0):
            manifest["result_seq"] = result_seq
            recovery = dict(manifest.get("recovery") or {})
            recovery["orphan_result_files"] = result_seq
            manifest["recovery"] = recovery
            changed = True
        if artifact_seq > int(manifest.get("artifact_seq") or 0):
            manifest["artifact_seq"] = artifact_seq
            recovery = dict(manifest.get("recovery") or {})
            recovery["orphan_artifact_files"] = artifact_seq
            manifest["recovery"] = recovery
            changed = True

        recap_path = run_dir / "RECAP.md"
        recap = dict(manifest.get("recap") or {})
        if recap_path.is_file() and recap.get("status") != "written":
            payload = recap_path.read_bytes()
            row = self._event_locked(run_dir, manifest, "recap_recovered", {
                "path": "RECAP.md", "sha256": _sha256(payload), "size": len(payload),
            })
            recap.update({
                "status": "written", "path": "RECAP.md", "sha256": _sha256(payload),
                "size": len(payload), "written_at": row["at"],
                "promotion": {
                    "status": "pending" if self.promotion_hook is not None else "not_configured",
                },
            })
            manifest["recap"] = recap
            changed = True

        if changed:
            manifest["revision"] = int(manifest.get("revision") or 0) + 1
            manifest["updated_at"] = _utc_now()
            _atomic_json(path, manifest)
        return manifest

    def _last_event_seq(self, run_dir: Path) -> int:
        for row in self._iter_events_reverse(run_dir):
            try:
                return int(row.get("seq") or 0)
            except (TypeError, ValueError):
                continue
        return 0

    def _event_locked(self, run_dir: Path, manifest: dict, kind: str, data: dict) -> dict:
        seq = max(int(manifest.get("event_seq") or 0), self._last_event_seq(run_dir)) + 1
        row = {
            **data,
            "schema": EVENT_SCHEMA,
            "id": f"{run_dir.name}:evt:{seq:08d}",
            "run_id": run_dir.name,
            "seq": seq,
            "at": _utc_now(),
            "kind": str(kind),
        }
        _append_jsonl(run_dir / "events.jsonl", row)
        manifest["event_seq"] = seq
        return row

    def append_event(self, run_id: str, kind: str, **data: Any) -> dict:
        run_dir = self._find(run_id)
        with self._locked(run_dir):
            manifest = self._manifest_locked(run_dir)
            row = self._event_locked(run_dir, manifest, kind, data)
            manifest["revision"] = int(manifest.get("revision") or 0) + 1
            manifest["updated_at"] = row["at"]
            _atomic_json(run_dir / "manifest.json", manifest)
            return row

    def append_event_once(self, run_id: str, kind: str, receipt_key: str,
                          **data: Any) -> dict:
        """Append one keyed receipt, or return the byte-equivalent prior one.

        The lookup and append share the run lock, so concurrent reconciliation
        workers cannot create two success/failure receipts for one effect.
        """
        key = str(receipt_key or "").strip()
        if not key:
            raise ValueError("receipt_key must not be empty")
        payload = {**data, "receipt_key": key}
        run_dir = self._find(run_id)
        with self._locked(run_dir):
            manifest = self._manifest_locked(run_dir)
            for row in self._iter_events_reverse(run_dir):
                if row.get("kind") != str(kind) or row.get("receipt_key") != key:
                    continue
                observed = {field: row.get(field) for field in payload}
                if observed != payload:
                    raise RunConflict(
                        f"{run_id}: receipt {kind}/{key} already has different content")
                return row
            row = self._event_locked(run_dir, manifest, kind, payload)
            manifest["revision"] = int(manifest.get("revision") or 0) + 1
            manifest["updated_at"] = row["at"]
            _atomic_json(run_dir / "manifest.json", manifest)
            return row

    def _tool_ledger_locked(self, run_dir: Path, *,
                            max_events: int | None = None,
                            max_bytes: int | None = None,
                            deadline_monotonic: float | None = None) -> dict[str, Any]:
        starts: dict[str, dict] = {}
        outcomes: dict[str, dict] = {}
        unknown_results: list[dict] = []
        conflicting_starts: list[dict] = []
        for row in self._iter_events(
                run_dir, max_events=max_events, max_bytes=max_bytes,
                deadline_monotonic=deadline_monotonic):
            kind = str(row.get("kind") or "")
            call_id = str(row.get("call_id") or "")
            if not call_id:
                continue
            if kind == "tool_started":
                previous = starts.get(call_id)
                if previous is None:
                    starts[call_id] = row
                elif any(previous.get(key) != row.get(key) for key in (
                        "tool", "args", "side_effect", "idempotent", "idempotency_key")):
                    conflicting_starts.append(row)
            elif kind in TOOL_OUTCOME_KINDS:
                if call_id not in starts:
                    unknown_results.append(row)
                else:
                    # Retries may add a later observed outcome for the same
                    # durable intent (for example failed delivery, then an
                    # accepted idempotent retry). The newest receipt is the
                    # reducer's evidence; either one closes the started call.
                    outcomes[call_id] = row
        outstanding = {
            call_id: row for call_id, row in starts.items() if call_id not in outcomes
        }
        return {
            "starts": starts,
            "outcomes": outcomes,
            "outstanding": outstanding,
            "unknown_results": unknown_results,
            "conflicting_starts": conflicting_starts,
        }

    @staticmethod
    def _status_locked(manifest: dict, ledger: dict[str, Any]) -> dict:
        context = dict(manifest.get("context") or {})
        outstanding = dict(ledger.get("outstanding") or {})
        unknown_results = list(ledger.get("unknown_results") or ())
        conflicting_starts = list(ledger.get("conflicting_starts") or ())
        return {
            "schema": STATUS_SCHEMA,
            "run_id": str(context.get("run_id") or ""),
            "status": str(manifest.get("status") or "pending"),
            "revision": int(manifest.get("revision") or 0),
            "kind": str(context.get("kind") or ""),
            "goal": str(context.get("goal") or ""),
            "principal_id": str(context.get("principal_id") or ""),
            "scope": str(context.get("scope") or ""),
            "created_at": str(manifest.get("created_at") or context.get("created_at") or ""),
            "updated_at": str(manifest.get("updated_at") or ""),
            "terminal_at": str(manifest.get("terminal_at") or ""),
            "requested_control": dict(manifest.get("control") or {}),
            "outstanding_call_ids": sorted(outstanding),
            "unknown_result_call_ids": sorted({
                str(row.get("call_id") or "") for row in unknown_results
                if row.get("call_id")
            }),
            "conflicting_start_call_ids": sorted({
                str(row.get("call_id") or "") for row in conflicting_starts
                if row.get("call_id")
            }),
            "terminalizable": not (outstanding or unknown_results or conflicting_starts),
        }

    def outstanding_tools(self, run_id: str, *, max_events: int | None = None,
                          max_bytes: int | None = None,
                          deadline_monotonic: float | None = None) -> dict[str, dict]:
        """Return unresolved ``tool_started`` receipts keyed by call id."""
        run_dir = self._find(run_id)
        with self._locked(run_dir):
            self._manifest_locked(run_dir)
            outstanding = self._tool_ledger_locked(
                run_dir, max_events=max_events, max_bytes=max_bytes,
                deadline_monotonic=deadline_monotonic,
            )["outstanding"]
            # Event rows contain only JSON values; round-tripping prevents a
            # control caller from mutating a shared in-memory projection.
            return json.loads(json.dumps(outstanding, ensure_ascii=False))

    @staticmethod
    def _terminal_blockers(ledger: dict[str, Any]) -> dict[str, list[str]]:
        return {
            "outstanding_call_ids": sorted((ledger.get("outstanding") or {}).keys()),
            "unknown_result_call_ids": sorted({
                str(row.get("call_id") or "")
                for row in (ledger.get("unknown_results") or ()) if row.get("call_id")
            }),
            "conflicting_start_call_ids": sorted({
                str(row.get("call_id") or "")
                for row in (ledger.get("conflicting_starts") or ()) if row.get("call_id")
            }),
        }

    def _assert_terminalizable_locked(self, run_dir: Path) -> None:
        blockers = self._terminal_blockers(self._tool_ledger_locked(run_dir))
        if any(blockers.values()):
            rendered = ", ".join(
                f"{name}={values}" for name, values in blockers.items() if values
            )
            raise RunConflict(f"{run_dir.name}: terminalization blocked; {rendered}")

    def _transition_locked(self, run_dir: Path, manifest: dict, status: str, *,
                           reason: str = "",
                           expected: str | set[str] | tuple[str, ...] | None = None,
                           details: dict | None = None,
                           audit: dict | None = None) -> dict:
        before = str(manifest.get("status") or "pending")
        if before == status:
            return manifest
        if expected is not None:
            allowed_expected = {expected} if isinstance(expected, str) else set(expected)
            if before not in allowed_expected:
                raise RunConflict(
                    f"{run_dir.name}: expected {sorted(allowed_expected)}, found {before}")
        if status not in _TRANSITIONS.get(before, frozenset()):
            raise InvalidTransition(f"{run_dir.name}: {before} -> {status}")
        if status in TERMINAL_STATUSES:
            self._assert_terminalizable_locked(run_dir)
        payload: dict[str, Any] = {"from_status": before, "to_status": status}
        if reason:
            payload["reason"] = str(reason)
        if details:
            payload["details"] = dict(details)
        if audit:
            payload.update(dict(audit))
        row = self._event_locked(run_dir, manifest, "status_changed", payload)
        now = row["at"]
        manifest["status"] = status
        manifest["updated_at"] = now
        manifest["revision"] = int(manifest.get("revision") or 0) + 1
        if status == "running" and not manifest.get("started_at"):
            manifest["started_at"] = now
        if status in TERMINAL_STATUSES:
            manifest["terminal_at"] = now
            manifest["terminal"] = {"status": status, "reason": str(reason or "")}
            manifest["control"] = {}
            recap = dict(manifest.get("recap") or {})
            if recap.get("status") in (None, "not_due"):
                recap["status"] = "pending"
            manifest["recap"] = recap
        _atomic_json(run_dir / "manifest.json", manifest)
        return manifest

    def recover_clean_in_doubt(self, run_id: str, *, evidence: dict,
                               reason: str, actor: str) -> dict:
        """Terminalize a legacy ``in_doubt`` run with a provably clean tool ledger."""
        proof = dict(evidence or {})
        if not proof:
            raise ValueError("clean in_doubt recovery requires non-empty evidence")
        why = str(reason or "").strip()
        if not why:
            raise ValueError("clean in_doubt recovery requires a reason")
        recovered_by = self._control_actor(actor)
        run_dir = self._find(run_id)
        with self._locked(run_dir):
            manifest = self._manifest_locked(run_dir)
            if str(manifest.get("status") or "") != "in_doubt":
                raise RunConflict(f"{run_id}: expected in_doubt")
            self._assert_terminalizable_locked(run_dir)
            return self._transition_locked(
                run_dir, manifest, "failed", expected="in_doubt", reason=why,
                details={"evidence": proof},
                audit={"control_action": "recover_clean_in_doubt",
                       "requested_by": recovered_by},
            )

    def transition(self, run_id: str, status: str, *, reason: str = "",
                   expected: str | set[str] | tuple[str, ...] | None = None,
                   details: dict | None = None) -> dict:
        """Atomically advance an ordinary reducer transition.

        Control-plane resume and reconciliation deliberately cannot be
        expressed through this generic method; they have dedicated audited
        operations below. Repeating an observed status remains idempotent.
        """
        if status not in RUN_STATUSES:
            raise ValueError(f"unknown run status {status!r}")
        run_dir = self._find(run_id)
        with self._locked(run_dir):
            manifest = self._manifest_locked(run_dir)
            before = str(manifest.get("status") or "pending")
            if before == status:
                return manifest
            if before == "paused" and status == "running":
                raise InvalidTransition(f"{run_id}: use resume() for paused -> running")
            if before == "in_doubt":
                raise InvalidTransition(
                    f"{run_id}: use resolve_in_doubt() before leaving in_doubt")
            return self._transition_locked(
                run_dir, manifest, status, reason=reason,
                expected=expected, details=details,
            )

    @staticmethod
    def _control_actor(actor: str) -> str:
        value = str(actor or "").strip()
        if not value:
            raise ValueError("control actor must not be empty")
        return value

    @staticmethod
    def _expected_revision(manifest: dict, expected_revision: int | None) -> None:
        """Fail a stale control-plane mutation before it can change durable state."""
        if expected_revision is None:
            return
        try:
            expected = int(expected_revision)
        except (TypeError, ValueError) as exc:
            raise ValueError("expected_revision must be an integer") from exc
        observed = int(manifest.get("revision") or 0)
        if expected != observed:
            raise RunConflict(
                f"stale run revision: expected {expected}, found {observed}"
            )

    def request_pause(self, run_id: str, *, actor: str, reason: str = "",
                      expected_revision: int | None = None) -> dict:
        """Pause running/blocked work and journal who requested the control."""
        requested_by = self._control_actor(actor)
        why = str(reason or "").strip() or "pause requested"
        run_dir = self._find(run_id)
        with self._locked(run_dir):
            manifest = self._manifest_locked(run_dir)
            self._expected_revision(manifest, expected_revision)
            before = str(manifest.get("status") or "pending")
            if before == "paused":
                pending = dict(manifest.get("control") or {})
                if pending.get("action") == "pause":
                    return manifest
                if pending:
                    raise RunConflict(
                        f"{run_id}: another control request is already pending")
                # A recovery pause has no owner-control provenance.  Recording
                # the later human pause matters even though the reducer status
                # itself is already ``paused``: transport/recovery workers must
                # not silently resume it.
                row = self._event_locked(run_dir, manifest, "control_requested", {
                    "action": "pause", "requested_by": requested_by, "reason": why,
                })
                manifest["control"] = {
                    "action": "pause", "requested_by": requested_by,
                    "reason": why, "requested_at": row["at"],
                }
                manifest["revision"] = int(manifest.get("revision") or 0) + 1
                manifest["updated_at"] = row["at"]
                _atomic_json(run_dir / "manifest.json", manifest)
                return manifest
            if before not in {"running", "blocked"}:
                raise InvalidTransition(f"{run_id}: cannot request pause from {before}")
            pending = dict(manifest.get("control") or {})
            if pending:
                raise RunConflict(
                    f"{run_id}: another control request is already pending")
            manifest["control"] = {
                "action": "pause", "requested_by": requested_by,
                "reason": why, "requested_at": _utc_now(),
            }
            return self._transition_locked(
                run_dir, manifest, "paused", expected=before,
                reason=why,
                audit={"control_action": "pause", "requested_by": requested_by},
            )

    def request_cancel(self, run_id: str, *, actor: str, reason: str = "",
                       expected_revision: int | None = None) -> dict:
        """Request cancellation and terminalize only after all calls are known.

        If a tool is still outstanding (or its receipt ledger is inconsistent),
        the request is persisted and the run is paused instead of lying that it
        is already cancelled. Calling this method again after reconciliation
        commits the terminal transition.
        """
        requested_by = self._control_actor(actor)
        why = str(reason or "").strip() or "cancellation requested"
        run_dir = self._find(run_id)
        with self._locked(run_dir):
            manifest = self._manifest_locked(run_dir)
            self._expected_revision(manifest, expected_revision)
            before = str(manifest.get("status") or "pending")
            if before == "cancelled":
                return manifest
            if before in TERMINAL_STATUSES:
                raise InvalidTransition(f"{run_id}: cannot cancel terminal {before} run")
            desired = {
                "action": "cancel", "requested_by": requested_by,
                "reason": why,
            }
            pending = dict(manifest.get("control") or {})
            # An explicit cancellation supersedes an earlier pause.  It still
            # cannot override another pending cancellation with different
            # semantics or bypass an in-doubt tool outcome.
            if pending.get("action") == "pause":
                pending = {}
                manifest["control"] = {}
            if pending:
                observed = {key: pending.get(key) for key in desired}
                if observed != desired:
                    raise RunConflict(
                        f"{run_id}: another control request is already pending")
            ledger = self._tool_ledger_locked(run_dir)
            blockers = self._terminal_blockers(ledger)
            if any(blockers.values()) or before == "in_doubt":
                if not pending:
                    row = self._event_locked(run_dir, manifest, "control_requested", desired)
                    manifest["control"] = {**desired, "requested_at": row["at"]}
                if before in {"running", "blocked"}:
                    return self._transition_locked(
                        run_dir, manifest, "paused", expected=before,
                        reason="cancellation requested; waiting for durable tool outcomes",
                        details={key: value for key, value in blockers.items() if value},
                        audit={"control_action": "cancel_pending",
                               "requested_by": requested_by},
                    )
                if not pending:
                    manifest["revision"] = int(manifest.get("revision") or 0) + 1
                    manifest["updated_at"] = manifest["control"]["requested_at"]
                    _atomic_json(run_dir / "manifest.json", manifest)
                return manifest
            manifest["control"] = {}
            return self._transition_locked(
                run_dir, manifest, "cancelled", expected=before,
                reason=why,
                audit={"control_action": "cancel", "requested_by": requested_by},
            )

    def resume(self, run_id: str, *, actor: str, reason: str = "",
               expected_revision: int | None = None) -> dict:
        """Explicitly resume a paused run; never bypass ``in_doubt``.

        A stored pause request is cleared only here.  Automatic transport and
        process-recovery callers must first inspect ``requested_control`` and
        refrain from calling ``resume`` when a human pause is present.
        """
        requested_by = self._control_actor(actor)
        run_dir = self._find(run_id)
        with self._locked(run_dir):
            manifest = self._manifest_locked(run_dir)
            self._expected_revision(manifest, expected_revision)
            before = str(manifest.get("status") or "pending")
            if before != "paused":
                raise InvalidTransition(f"{run_id}: resume requires paused, found {before}")
            pending = dict(manifest.get("control") or {})
            if pending.get("action") == "cancel":
                raise RunConflict(
                    f"{run_id}: cannot resume while control request is pending")
            if pending and pending.get("action") != "pause":
                raise RunConflict(
                    f"{run_id}: cannot resume while unknown control request is pending")
            manifest["control"] = {}
            return self._transition_locked(
                run_dir, manifest, "running", expected="paused",
                reason=reason or "resume requested",
                audit={
                    "control_action": "resume", "requested_by": requested_by,
                    "resumed_pause_requested_by": str(pending.get("requested_by") or ""),
                },
            )

    def authorize_resume(self, run_id: str, *, actor: str, reason: str = "",
                         expected_revision: int | None = None) -> dict:
        """Release an explicit pause for the exact recovery executor.

        ``resume()`` is retained for callers that already own an execution
        worker.  A web control surface does not own one: changing the manifest
        to ``running`` there would create an orphan.  This method instead
        leaves the run paused, clears only the matching pause request and
        journals an auditable authorization.  :mod:`run_resume` can then claim
        the normal revision/event-seq lease and start effects exactly once.
        """
        requested_by = self._control_actor(actor)
        why = str(reason or "").strip() or "resume requested"
        run_dir = self._find(run_id)
        with self._locked(run_dir):
            manifest = self._manifest_locked(run_dir)
            self._expected_revision(manifest, expected_revision)
            before = str(manifest.get("status") or "pending")
            if before != "paused":
                raise InvalidTransition(
                    f"{run_id}: authorize_resume requires paused, found {before}"
                )
            pending = dict(manifest.get("control") or {})
            if pending.get("action") == "cancel":
                raise RunConflict(
                    f"{run_id}: cannot resume while cancellation is pending"
                )
            if pending and pending.get("action") != "pause":
                raise RunConflict(
                    f"{run_id}: cannot resume while unknown control request is pending"
                )
            row = self._event_locked(run_dir, manifest, "resume_authorized", {
                "requested_by": requested_by,
                "reason": why,
                "paused_by": str(pending.get("requested_by") or ""),
            })
            manifest["control"] = {}
            manifest["revision"] = int(manifest.get("revision") or 0) + 1
            manifest["updated_at"] = row["at"]
            _atomic_json(run_dir / "manifest.json", manifest)
            return manifest

    def claim_resume(self, run_id: str, *, expected_revision: int,
                     expected_event_seq: int, actor: str,
                     reason: str = "") -> dict:
        """Atomically claim one recovery-planned automatic resume.

        The planner binds its evidence to both the manifest revision and the
        append-only event cursor.  The executor must echo both values here.
        Any intervening event, owner control, or status change makes the plan
        stale and therefore incapable of starting effects.

        This is deliberately separate from :meth:`resume`: an explicit owner
        may clear an owner pause, while a recovery executor may only claim a
        control-free recovery pause.
        """
        claimed_by = self._control_actor(actor)
        try:
            revision = int(expected_revision)
            event_seq = int(expected_event_seq)
        except (TypeError, ValueError) as exc:
            raise ValueError("resume claim cursors must be integers") from exc
        if revision < 1 or event_seq < 0:
            raise ValueError("resume claim cursors are out of range")

        run_dir = self._find(run_id)
        with self._locked(run_dir):
            manifest = self._manifest_locked(run_dir)
            observed_revision = int(manifest.get("revision") or 0)
            observed_event_seq = int(manifest.get("event_seq") or 0)
            if (observed_revision != revision
                    or observed_event_seq != event_seq):
                raise RunConflict(
                    f"{run_id}: stale resume plan; expected "
                    f"revision/event_seq {revision}/{event_seq}, found "
                    f"{observed_revision}/{observed_event_seq}")
            before = str(manifest.get("status") or "pending")
            if before != "paused":
                raise RunConflict(
                    f"{run_id}: stale resume plan; expected paused, found {before}")
            pending = dict(manifest.get("control") or {})
            if pending:
                raise RunConflict(
                    f"{run_id}: automatic resume forbidden while control is pending")
            return self._transition_locked(
                run_dir, manifest, "running", expected="paused",
                reason=reason or "recovery resume claimed",
                audit={
                    "control_action": "resume_claim",
                    "requested_by": claimed_by,
                    "planned_revision": revision,
                    "planned_event_seq": event_seq,
                },
            )

    def claim_transport_reconcile(self, run_id: str, *, expected_revision: int,
                                  expected_event_seq: int, actor: str,
                                  reason: str = "") -> dict:
        """CAS-claim one local transport handoff without resuming model work.

        The claim is an append-only cursor change, not a status transition.  It
        prevents a stale transport plan from queueing bytes after an owner
        control or another reconciler changed the run.  Telegram send paths
        still consult the run status before crossing the external boundary.
        """

        claimed_by = self._control_actor(actor)
        try:
            revision = int(expected_revision)
            event_seq = int(expected_event_seq)
        except (TypeError, ValueError) as exc:
            raise ValueError("transport claim cursors must be integers") from exc
        if revision < 1 or event_seq < 0:
            raise ValueError("transport claim cursors are out of range")
        run_dir = self._find(run_id)
        with self._locked(run_dir):
            manifest = self._manifest_locked(run_dir)
            observed_revision = int(manifest.get("revision") or 0)
            observed_event_seq = int(manifest.get("event_seq") or 0)
            if (observed_revision != revision
                    or observed_event_seq != event_seq):
                raise RunConflict(
                    f"{run_id}: stale transport plan; expected "
                    f"revision/event_seq {revision}/{event_seq}, found "
                    f"{observed_revision}/{observed_event_seq}")
            status = str(manifest.get("status") or "pending")
            if status not in {"paused", "blocked", "in_doubt"}:
                raise RunConflict(
                    f"{run_id}: transport reconcile cannot claim status {status}")
            if manifest.get("control"):
                raise RunConflict(
                    f"{run_id}: transport reconcile forbidden while control is pending")
            row = self._event_locked(
                run_dir, manifest, "transport_reconcile_claimed", {
                    "control_action": "transport_reconcile_claim",
                    "requested_by": claimed_by,
                    "reason": str(reason or "transport handoff claimed"),
                    "planned_revision": revision,
                    "planned_event_seq": event_seq,
                },
            )
            manifest["revision"] = int(manifest.get("revision") or 0) + 1
            manifest["updated_at"] = row["at"]
            _atomic_json(run_dir / "manifest.json", manifest)
            return manifest

    def resolve_in_doubt(self, run_id: str, call_id: str, outcome: str, *,
                         evidence: dict, reason: str, actor: str) -> dict:
        """Reconcile one uncertain call from explicit evidence.

        Every outstanding call needs a ``completed``, ``failed`` or
        ``not_applied`` receipt. Once the final call is reconciled the run moves
        to ``paused``; execution still requires a separately audited
        :meth:`resume`.
        """
        call = str(call_id or "").strip()
        if not call:
            raise ValueError("call_id must not be empty")
        resolved_as = str(outcome or "").strip()
        if resolved_as not in TOOL_RESOLUTION_OUTCOMES:
            raise ValueError(
                f"outcome must be one of {sorted(TOOL_RESOLUTION_OUTCOMES)}")
        proof = dict(evidence or {})
        if not proof:
            raise ValueError("in_doubt resolution requires non-empty evidence")
        why = str(reason or "").strip()
        if not why:
            raise ValueError("in_doubt resolution requires a reason")
        resolved_by = self._control_actor(actor)
        semantic = {
            "call_id": call, "outcome": resolved_as, "evidence": proof,
            "reason": why, "resolved_by": resolved_by,
        }
        run_dir = self._find(run_id)
        with self._locked(run_dir):
            manifest = self._manifest_locked(run_dir)
            prior = None
            for row in self._iter_events_reverse(run_dir):
                if row.get("kind") == "tool_reconciled" and row.get("call_id") == call:
                    prior = row
                    break
            if prior is not None:
                observed = {key: prior.get(key) for key in semantic}
                if observed != semantic:
                    raise RunConflict(
                        f"{run_id}: call {call} already has a different reconciliation")
                if str(manifest.get("status") or "") != "in_doubt":
                    return manifest

            if str(manifest.get("status") or "") != "in_doubt":
                raise InvalidTransition(
                    f"{run_id}: reconciliation requires in_doubt, "
                    f"found {manifest.get('status')}")
            ledger = self._tool_ledger_locked(run_dir)
            blockers = self._terminal_blockers(ledger)
            if blockers["unknown_result_call_ids"] or blockers["conflicting_start_call_ids"]:
                raise RunConflict(
                    f"{run_id}: tool ledger is inconsistent; blockers={blockers}")
            if prior is None:
                started = (ledger.get("outstanding") or {}).get(call)
                if started is None:
                    raise RunConflict(f"{run_id}: call {call} is not outstanding")
                self._event_locked(run_dir, manifest, "tool_reconciled", {
                    **semantic, "tool": str(started.get("tool") or ""),
                    "side_effect": started.get("side_effect"),
                    "idempotency_key": str(started.get("idempotency_key") or ""),
                })

            after = self._tool_ledger_locked(run_dir)
            remaining = sorted((after.get("outstanding") or {}).keys())
            if not remaining:
                return self._transition_locked(
                    run_dir, manifest, "paused", expected="in_doubt",
                    reason="all uncertain tool outcomes explicitly reconciled",
                    details={"resolved_call_id": call, "outcome": resolved_as},
                    audit={"control_action": "resolve_in_doubt",
                           "requested_by": resolved_by},
                )
            if prior is not None:
                return manifest
            manifest["revision"] = int(manifest.get("revision") or 0) + 1
            manifest["updated_at"] = _utc_now()
            _atomic_json(run_dir / "manifest.json", manifest)
            return manifest

    def start_tool(self, run_id: str, call_id: str, name: str, args: dict | None = None,
                   *, side_effect: bool | None = None, idempotency_key: str = "") -> dict:
        payload: dict[str, Any] = {
            "call_id": str(call_id), "tool": str(name), "args": dict(args or {}),
        }
        if side_effect is not None:
            payload["side_effect"] = bool(side_effect)
            payload["idempotent"] = bool(idempotency_key) or not bool(side_effect)
        if idempotency_key:
            payload["idempotency_key"] = str(idempotency_key)
        if not payload["call_id"]:
            raise ValueError("call_id must not be empty")
        run_dir = self._find(run_id)
        with self._locked(run_dir):
            manifest = self._manifest_locked(run_dir)
            current_status = str(manifest.get("status") or "pending")
            if current_status in TERMINAL_STATUSES or current_status in {"paused", "in_doubt"}:
                raise InvalidTransition(
                    f"{run_id}: cannot start a tool while run is {current_status}")
            if (manifest.get("control") or {}).get("action"):
                raise RunConflict(f"{run_id}: cannot start a tool with pending control request")
            for row in self._iter_events_reverse(run_dir):
                if row.get("kind") != "tool_started" or row.get("call_id") != payload["call_id"]:
                    continue
                observed = {key: row.get(key) for key in payload}
                if observed != payload:
                    raise RunConflict(
                        f"{run_id}: call_id {payload['call_id']} already started differently")
                return row
            row = self._event_locked(run_dir, manifest, "tool_started", payload)
            manifest["revision"] = int(manifest.get("revision") or 0) + 1
            manifest["updated_at"] = row["at"]
            _atomic_json(run_dir / "manifest.json", manifest)
            return row

    @staticmethod
    def _inline_text(text: str, budget: int) -> dict:
        budget = max(128, int(budget))
        if len(text) <= budget:
            return {"head": text, "tail": "", "truncated": False, "omitted_chars": 0}
        head_len = max(1, int(budget * 0.62))
        tail_len = max(1, budget - head_len)
        return {
            "head": text[:head_len],
            "tail": text[-tail_len:],
            "truncated": True,
            "omitted_chars": len(text) - head_len - tail_len,
        }

    def store_result(self, run_id: str, value: str | bytes, *, call_id: str = "",
                     name: str = "result", media_type: str = "text/plain; charset=utf-8",
                     inline_chars: int = 2000, event_kind: str = "tool_result",
                     idempotent: bool = False,
                     metadata: dict[str, Any] | None = None) -> dict:
        """Persist a full result and return a compact, hash-addressed ``ResultRef``."""
        payload = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        text_value = value if isinstance(value, str) else None
        digest = _sha256(payload)
        normalized_metadata: dict[str, Any] | None = None
        if metadata is not None:
            if not isinstance(metadata, dict):
                raise TypeError("result metadata must be a JSON object")
            try:
                encoded_metadata = json.dumps(
                    metadata, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False,
                )
                normalized_metadata = json.loads(encoded_metadata)
            except (TypeError, ValueError) as exc:
                raise ValueError("result metadata must be strict JSON") from exc
            if len(encoded_metadata.encode("utf-8")) > 1024 * 1024:
                raise ValueError("result metadata exceeds 1 MiB")
        if idempotent and not call_id:
            raise ValueError("idempotent result requires call_id")
        run_dir = self._find(run_id)
        with self._locked(run_dir):
            manifest = self._manifest_locked(run_dir)
            if idempotent:
                for row in self._iter_events_reverse(run_dir):
                    if (row.get("kind") != event_kind
                            or row.get("call_id") != str(call_id)
                            or row.get("name") != str(name)):
                        continue
                    ref = dict(row.get("result") or {})
                    if str(ref.get("sha256") or "") != digest:
                        raise RunConflict(
                            f"{run_id}: receipt {call_id}/{name} already has different content")
                    observed_metadata = (row.get("metadata")
                                         if "metadata" in row else None)
                    if observed_metadata != normalized_metadata:
                        raise RunConflict(
                            f"{run_id}: receipt {call_id}/{name} already has "
                            "different metadata")
                    return ref
            seq = max(int(manifest.get("result_seq") or 0),
                      self._max_numbered_file(run_dir / "results")) + 1
            suffix = ".log" if text_value is not None else ".bin"
            filename = f"{seq:04d}-{_slug(name, 'result')}{suffix}"
            path = run_dir / "results" / filename
            _atomic_bytes(path, payload)
            ref: dict[str, Any] = {
                "schema": RESULT_SCHEMA,
                "run_id": run_id,
                "result_id": f"result-{seq:04d}",
                "path": f"results/{filename}",
                "sha256": digest,
                "size": len(payload),
                "media_type": str(media_type or "application/octet-stream"),
                "encoding": "utf-8" if text_value is not None else None,
                "line_count": (len(text_value.splitlines()) if text_value is not None else None),
                "inline": (self._inline_text(text_value, inline_chars)
                           if text_value is not None else {
                               "head_base64": base64.b64encode(payload[:96]).decode("ascii"),
                               "tail_base64": base64.b64encode(
                                   payload[-96:] if len(payload) > 96 else b""
                               ).decode("ascii"),
                               "truncated": len(payload) > 192,
                           }),
            }
            event_data: dict[str, Any] = {"result": ref, "name": str(name)}
            if call_id:
                event_data["call_id"] = str(call_id)
            if normalized_metadata is not None:
                event_data["metadata"] = normalized_metadata
            self._event_locked(run_dir, manifest, event_kind, event_data)
            manifest["result_seq"] = seq
            manifest["updated_at"] = _utc_now()
            manifest["revision"] = int(manifest.get("revision") or 0) + 1
            _atomic_json(run_dir / "manifest.json", manifest)
            return ref

    def _result_path(self, run_id: str, result: str) -> Path:
        run_dir = self._find(run_id)
        results = (run_dir / "results").resolve()
        value = str(result or "").strip().replace("\\", "/")
        match = re.fullmatch(r"result-(\d{1,12})", value)
        if match:
            hits = list(results.glob(f"{int(match.group(1)):04d}-*"))
            if len(hits) != 1:
                raise RunNotFound(f"{run_id}/{value}")
            return hits[0]
        if value.startswith("results/"):
            value = value[len("results/"):]
        candidate = (results / value).resolve()
        try:
            candidate.relative_to(results)
        except ValueError as exc:
            raise ValueError("result path escaped the run") from exc
        if not candidate.is_file():
            raise RunNotFound(f"{run_id}/{result}")
        return candidate

    def read_result(self, run_id: str, result: str, *, byte_offset: int = 0,
                    byte_limit: int = 65536, line_start: int | None = None,
                    line_count: int | None = None, verify_sha256: bool = True,
                    deadline_monotonic: float | None = None) -> dict:
        """Read a byte or one-based line cursor from an externalised result.

        Ordinary callers receive the historical per-call SHA-256 projection.
        Recovery reconstruction passes ``verify_sha256=False`` and verifies the
        one completely assembled, bounded payload against its durable ResultRef.
        That avoids hashing the whole file once per cursor page while retaining
        end-to-end tamper detection.  Optional deadline checks bound both cursor
        I/O and the legacy full-file verification path.
        """
        if not isinstance(verify_sha256, bool):
            raise TypeError("verify_sha256 must be a boolean")
        _check_read_deadline(deadline_monotonic, "result evidence")
        path = self._result_path(run_id, result)
        size = path.stat().st_size
        common = {
            "run_id": run_id,
            "path": f"results/{path.name}",
            "size": size,
        }
        if verify_sha256:
            common["sha256"] = _file_sha256(
                path, deadline_monotonic=deadline_monotonic,
            )
        if line_start is not None:
            start = max(1, int(line_start))
            count = max(1, min(1000, int(line_count if line_count is not None else 200)))
            selected: list[str] = []
            eof = True
            with path.open(encoding="utf-8", errors="replace") as stream:
                iterator = enumerate(stream, 1)
                for number, line in iterator:
                    if number % 256 == 1:
                        _check_read_deadline(deadline_monotonic, "result evidence")
                    if number < start:
                        continue
                    if len(selected) >= count:
                        eof = False
                        break
                    selected.append(line)
            _check_read_deadline(deadline_monotonic, "result evidence")
            next_line = start + len(selected)
            return {
                **common,
                "mode": "lines",
                "line_start": start,
                "line_count": len(selected),
                "next_line": next_line,
                "eof": eof,
                "text": "".join(selected),
            }
        offset = max(0, int(byte_offset))
        limit = max(1, min(4 * 1024 * 1024, int(byte_limit or 65536)))
        with path.open("rb") as stream:
            stream.seek(min(offset, size))
            chunk = stream.read(limit)
        _check_read_deadline(deadline_monotonic, "result evidence")
        next_offset = offset + len(chunk)
        return {
            **common,
            "mode": "bytes",
            "offset": offset,
            "bytes": len(chunk),
            "next_offset": next_offset,
            "eof": next_offset >= size,
            "data_base64": base64.b64encode(chunk).decode("ascii"),
            "text": chunk.decode("utf-8", "replace"),
        }

    def store_artifact(
        self,
        run_id: str,
        value: str | Path | bytes,
        *,
        name: str = "artifact",
        media_type: str = "application/octet-stream",
        idempotency_key: str = "",
        expected_sha256: str = "",
        expected_size: int | None = None,
    ) -> dict:
        """Persist an artifact without loading file-backed values into memory.

        ``idempotency_key`` binds one durable artifact receipt to exact content
        and metadata.  File-backed idempotent callers provide the hash and size
        they already trust, allowing a retry to return the receipt before doing
        another large copy.
        """
        source: Path | None = None
        payload: bytes | None = None
        if isinstance(value, Path):
            source = value
            if name == "artifact":
                name = value.name
        elif isinstance(value, str):
            payload = value.encode("utf-8")
        else:
            payload = bytes(value)

        normalized_name = str(name)
        normalized_media_type = str(media_type or "application/octet-stream")
        key = str(idempotency_key or "")
        if key and (len(key) > 512 or any(not char.isprintable() for char in key)):
            raise ValueError("artifact idempotency_key must be at most 512 printable characters")
        expected_digest = str(expected_sha256 or "").lower()
        if expected_digest and not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
            raise ValueError("expected_sha256 must be a lowercase SHA-256 digest")
        if (expected_size is not None
                and (not isinstance(expected_size, int)
                     or isinstance(expected_size, bool) or expected_size < 0)):
            raise ValueError("expected_size must be a non-negative integer")
        normalized_expected_size = expected_size

        known_digest = _sha256(payload) if payload is not None else expected_digest
        known_size = len(payload) if payload is not None else normalized_expected_size
        if payload is not None:
            if expected_digest and expected_digest != known_digest:
                raise RunConflict("artifact content differs from expected_sha256")
            if normalized_expected_size is not None and normalized_expected_size != known_size:
                raise RunConflict("artifact content differs from expected_size")
        elif key and (not expected_digest or normalized_expected_size is None):
            raise ValueError(
                "idempotent file artifact requires expected_sha256 and expected_size"
            )

        run_dir = self._find(run_id)
        artifacts_dir = run_dir / "artifacts"
        with self._locked(run_dir):
            manifest = self._manifest_locked(run_dir)
            if key:
                for row in self._iter_events_reverse(run_dir, strict=True):
                    if (row.get("kind") != "artifact_created"
                            or row.get("idempotency_key") != key):
                        continue
                    ref = dict(row.get("artifact") or {})
                    observed = (
                        str(ref.get("sha256") or ""), int(ref.get("size") or 0),
                        str(ref.get("name") or ""), str(ref.get("media_type") or ""),
                    )
                    expected = (
                        str(known_digest or ""), int(known_size or 0),
                        normalized_name, normalized_media_type,
                    )
                    if observed != expected:
                        raise RunConflict(
                            f"{run_id}: artifact idempotency key already has different content"
                        )
                    relative = Path(str(ref.get("path") or ""))
                    stored = (run_dir / relative).resolve()
                    try:
                        stored.relative_to(artifacts_dir.resolve())
                    except ValueError as exc:
                        raise RunConflict("artifact receipt escapes its run directory") from exc
                    if not stored.is_file() or stored.stat().st_size != expected[1]:
                        raise RunConflict("artifact receipt points to missing or truncated content")
                    return ref

            staged: Path | None = None
            try:
                if source is not None:
                    staged, digest, size = _stage_file_copy(artifacts_dir, source)
                else:
                    assert payload is not None
                    digest, size = str(known_digest), int(known_size or 0)
                if expected_digest and digest != expected_digest:
                    raise RunConflict(
                        f"artifact hash mismatch: expected {expected_digest}, got {digest}"
                    )
                if normalized_expected_size is not None and size != normalized_expected_size:
                    raise RunConflict(
                        f"artifact size mismatch: expected {normalized_expected_size}, got {size}"
                    )

                seq = max(int(manifest.get("artifact_seq") or 0),
                          self._max_numbered_file(artifacts_dir)) + 1
                filename = f"{seq:04d}-{digest[:12]}-{_slug(normalized_name, 'artifact')}"
                path = artifacts_dir / filename
                if staged is not None:
                    os.replace(staged, path)
                    staged = None
                    _chmod(path, 0o600)
                else:
                    _atomic_bytes(path, payload)
                ref = {
                    "schema": ARTIFACT_SCHEMA,
                    "run_id": run_id,
                    "artifact_id": f"artifact-{seq:04d}",
                    "path": f"artifacts/{filename}",
                    "sha256": digest,
                    "size": size,
                    "media_type": normalized_media_type,
                    "name": normalized_name,
                }
                event = {"artifact": ref}
                if key:
                    event["idempotency_key"] = key
                self._event_locked(run_dir, manifest, "artifact_created", event)
                manifest["artifact_seq"] = seq
                manifest["updated_at"] = _utc_now()
                manifest["revision"] = int(manifest.get("revision") or 0) + 1
                _atomic_json(run_dir / "manifest.json", manifest)
                return ref
            finally:
                if staged is not None:
                    try:
                        staged.unlink(missing_ok=True)
                    except OSError:
                        pass

    def recover(self) -> list[dict]:
        """Recover interrupted manifests without replaying an uncertain side effect."""
        reports: list[dict] = []
        if not self.root.exists():
            return reports
        for manifest_path in sorted(self.root.glob("*/*/manifest.json")):
            run_id = manifest_path.parent.name
            self._paths[run_id] = manifest_path.parent
            try:
                run_dir = manifest_path.parent
                with self._locked(run_dir):
                    manifest = self._manifest_locked(run_dir)
                status = str(manifest.get("status") or "pending")
                if status == "running":
                    outstanding = self.outstanding_tools(run_id)
                    uncertain = [
                        call_id for call_id, row in outstanding.items()
                        if row.get("side_effect") is not False and row.get("idempotent") is not True
                    ]
                    target = "in_doubt" if uncertain else "paused"
                    reason = ("process restarted with an unobserved side effect"
                              if uncertain else "process restarted; no uncertain side effect observed")
                    recovered = self.transition(
                        run_id, target, expected="running", reason=reason,
                        details={"outstanding_call_ids": sorted(outstanding),
                                 "uncertain_call_ids": sorted(uncertain)},
                    )
                    reports.append({"run_id": run_id, "from": "running", "to": target,
                                    "revision": recovered.get("revision")})
                    manifest = recovered
                recap = manifest.get("recap") or {}
                promotion = recap.get("promotion") or {}
                if promotion.get("status") == "running":
                    with self._locked(run_dir):
                        fresh = self._manifest_locked(run_dir)
                        fresh_recap = dict(fresh.get("recap") or {})
                        fresh_promotion = dict(fresh_recap.get("promotion") or {})
                        if fresh_promotion.get("status") == "running":
                            fresh_promotion["status"] = "pending"
                            fresh_promotion["recovered_at"] = _utc_now()
                            fresh_recap["promotion"] = fresh_promotion
                            fresh["recap"] = fresh_recap
                            fresh["revision"] = int(fresh.get("revision") or 0) + 1
                            fresh["updated_at"] = _utc_now()
                            self._event_locked(run_dir, fresh, "recap_promotion_recovered", {})
                            _atomic_json(manifest_path, fresh)
                            reports.append({"run_id": run_id, "recap_promotion": "pending"})
                            manifest = fresh
                if (self.promotion_hook is not None and manifest.get("status") in TERMINAL_STATUSES
                        and (manifest.get("recap") or {}).get("status") == "written"
                        and ((manifest.get("recap") or {}).get("promotion") or {}).get("status") == "pending"):
                    self.promote_recap(run_id)
            except Exception as exc:
                reports.append({"run_id": run_id, "error": f"{type(exc).__name__}: {exc}"})
        return reports

    def write_recap(self, run_id: str, recap_markdown: str, *, promote: bool = True) -> dict:
        """Write an immutable terminal recap and optionally run the promotion hook."""
        recap_text = str(recap_markdown or "").strip()
        if not recap_text:
            raise ValueError("RECAP must not be empty")
        recap_payload = (recap_text + "\n").encode("utf-8")
        digest = _sha256(recap_payload)
        run_dir = self._find(run_id)
        with self._locked(run_dir):
            manifest = self._manifest_locked(run_dir)
            if manifest.get("status") not in TERMINAL_STATUSES:
                raise InvalidTransition(f"{run_id}: RECAP requires a terminal run")
            path = run_dir / "RECAP.md"
            if path.exists():
                current = path.read_bytes()
                if current != recap_payload:
                    raise RunConflict(f"{run_id}: RECAP already exists with different content")
                newly_written = False
            else:
                _atomic_bytes(path, recap_payload)
                newly_written = True
            recap = dict(manifest.get("recap") or {})
            if newly_written or recap.get("status") != "written":
                recap.update({
                    "status": "written", "path": "RECAP.md", "sha256": digest,
                    "size": len(recap_payload), "written_at": _utc_now(),
                })
                recap.setdefault("promotion", {
                    "status": "pending" if self.promotion_hook is not None else "not_configured",
                })
                manifest["recap"] = recap
                self._event_locked(run_dir, manifest, "recap_written", {
                    "path": "RECAP.md", "sha256": digest, "size": len(recap_payload),
                })
                manifest["revision"] = int(manifest.get("revision") or 0) + 1
                manifest["updated_at"] = _utc_now()
                _atomic_json(run_dir / "manifest.json", manifest)
        return self.promote_recap(run_id) if promote and self.promotion_hook is not None else self.manifest(run_id)

    def promote_recap(self, run_id: str) -> dict:
        """Invoke the configured idempotent promotion hook at most once per manifest."""
        if self.promotion_hook is None:
            return self.manifest(run_id)
        run_dir = self._find(run_id)
        with self._locked(run_dir):
            manifest = self._manifest_locked(run_dir)
            if manifest.get("status") not in TERMINAL_STATUSES:
                raise InvalidTransition(f"{run_id}: promotion requires a terminal run")
            recap = dict(manifest.get("recap") or {})
            if recap.get("status") != "written" or not (run_dir / "RECAP.md").is_file():
                raise RunConflict(f"{run_id}: RECAP is not written")
            promotion = dict(recap.get("promotion") or {})
            if promotion.get("status") == "done":
                return manifest
            if promotion.get("status") == "running":
                return manifest
            promotion.update({"status": "running", "attempted_at": _utc_now()})
            recap["promotion"] = promotion
            manifest["recap"] = recap
            manifest["revision"] = int(manifest.get("revision") or 0) + 1
            manifest["updated_at"] = _utc_now()
            _atomic_json(run_dir / "manifest.json", manifest)
            context = RunContext.from_dict(manifest.get("context") or {}).with_status(
                str(manifest.get("status") or "done"))
            hook_manifest = json.loads(json.dumps(manifest))
        try:
            promoted = self.promotion_hook(context, run_dir / "RECAP.md", hook_manifest)
            if isinstance(promoted, dict):
                event_id = str(promoted.get("id") or promoted.get("event_id") or "")
                hook_data = promoted
            else:
                event_id = str(promoted or "")
                hook_data = {}
        except Exception as exc:
            with self._locked(run_dir):
                manifest = self._manifest_locked(run_dir)
                recap = dict(manifest.get("recap") or {})
                promotion = dict(recap.get("promotion") or {})
                promotion.update({"status": "pending", "last_error": f"{type(exc).__name__}: {exc}"})
                recap["promotion"] = promotion
                manifest["recap"] = recap
                self._event_locked(run_dir, manifest, "recap_promotion_failed", {
                    "error": f"{type(exc).__name__}: {exc}",
                })
                manifest["revision"] = int(manifest.get("revision") or 0) + 1
                manifest["updated_at"] = _utc_now()
                _atomic_json(run_dir / "manifest.json", manifest)
            raise
        with self._locked(run_dir):
            manifest = self._manifest_locked(run_dir)
            recap = dict(manifest.get("recap") or {})
            promotion = dict(recap.get("promotion") or {})
            # A racing recovery may already have committed the same idempotent hook result.
            if promotion.get("status") != "done":
                promotion.update({"status": "done", "event_id": event_id, "promoted_at": _utc_now()})
                if hook_data:
                    promotion["receipt"] = hook_data
                recap["promotion"] = promotion
                manifest["recap"] = recap
                self._event_locked(run_dir, manifest, "run_promoted", {"event_id": event_id})
                manifest["revision"] = int(manifest.get("revision") or 0) + 1
                manifest["updated_at"] = _utc_now()
                _atomic_json(run_dir / "manifest.json", manifest)
            return manifest


def life_event_promotion(context: RunContext, recap_path: Path, manifest: dict) -> str:
    """Idempotently promote a terminal run into the existing append-only life spine.

    This hook is opt-in so isolated tests or alternate server roots cannot
    accidentally write to the process-global Praxis memory tree.
    """
    import memory_life

    for row in reversed(memory_life.iter_events(kinds={"run_episode"})):
        if str(row.get("source_id") or "") == context.run_id:
            return str(row.get("id") or "")
    recap_rel = str((manifest.get("recap") or {}).get("path") or recap_path.name)
    text = (f"Run {context.run_id} завершён ({manifest.get('status')}): {context.goal}; "
            f"recap={context.context_snapshot.rsplit('/', 1)[0]}/{recap_rel}")
    event = memory_life.append_event(
        "run_episode", chat_id=context.delivery_chat_id or context.origin_chat_id,
        actor="Praxis", direction="internal", text=text,
        source="run_manager", source_id=context.run_id, salience=2,
        refs=(), dedupe_key=f"run_episode:{context.run_id}",
        meta={
            "run_id": context.run_id, "kind": context.kind,
            "status": manifest.get("status"), "recap": recap_rel,
            "context_snapshot": context.context_snapshot,
            "forge_task_id": context.forge_task_id,
        },
    )
    return str(event.get("id") or "")
