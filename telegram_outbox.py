"""Durable storage core for direct and scheduled Telegram side effects.

The module deliberately knows nothing about Telethon, asyncio, an LLM, or the
agent loop.  A caller resolves the exact Telegram destination first and then
persists an immutable send intent here *before* touching the network.  Retrying
the same caller-owned key always yields the same signed MTProto ``random_id``.

Each intent owns a small append-only JSONL journal.  File payloads are copied to
private immutable staging before the intent is fsynced.  A damaged complete
line or an unterminated crash tail is preserved in ``quarantine/`` and the last
valid state remains replayable.  Accepted and dead-letter entries are retained;
there is intentionally no implicit pruning or deletion API.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import re
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator


EVENT_SCHEMA = "praxis.telegram.outbox.event.v1"
DEFAULT_MAX_FILE_BYTES = 250 * 1024 * 1024
DEFAULT_MAX_TEXT_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_ATTEMPTS = 12
DEFAULT_BASE_BACKOFF_SECONDS = 5.0
DEFAULT_MAX_BACKOFF_SECONDS = 3600.0

_EVENT_KINDS = frozenset({"intent", "retry", "accepted", "dead_letter", "requeue"})
_ENTRY_RE = re.compile(r"^outbox-[0-9a-f]{64}$")
_EVENT_ID_RE = re.compile(r"^outbox-event-[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MIME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+\-]{0,126}/"
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+\-]{0,126}$"
)
_LOCAL_LOCK = threading.RLock()


class TelegramOutboxError(RuntimeError):
    """Base class for durable Telegram outbox failures."""


class TelegramOutboxValidationError(TelegramOutboxError, ValueError):
    """A caller supplied a value that cannot be persisted safely."""


class TelegramOutboxSecurityError(TelegramOutboxError):
    """A path or filesystem object violates the outbox boundary."""


class TelegramOutboxConflict(TelegramOutboxError):
    """An idempotency key was reused for a different immutable intent."""


class TelegramOutboxIntegrityError(TelegramOutboxError):
    """A staged payload no longer matches its durable size/SHA-256 receipt."""


def _iso_utc(epoch: float) -> str:
    return (
        dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise TelegramOutboxValidationError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TelegramOutboxValidationError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise TelegramOutboxValidationError(f"{label} must be a finite number")
    return number


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TelegramOutboxValidationError(f"{label} must be a positive integer")
    return value


def _strict_text(value: object, label: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TelegramOutboxValidationError(f"{label} must be text")
    if not value or len(value) > maximum:
        raise TelegramOutboxValidationError(
            f"{label} must contain 1..{maximum} characters"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise TelegramOutboxValidationError(f"{label} contains control characters")
    return value


def _optional_body_text(value: object, label: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise TelegramOutboxValidationError(f"{label} must be text")
    if len(value) > maximum or "\x00" in value:
        raise TelegramOutboxValidationError(f"invalid {label}")
    return value


def _telegram_id(value: object, label: str, *, positive: bool) -> int:
    if isinstance(value, bool):
        raise TelegramOutboxValidationError(f"{label} must be an integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value):
        result = int(value)
        if str(result) != value:
            raise TelegramOutboxValidationError(f"{label} must be canonical decimal")
    else:
        raise TelegramOutboxValidationError(f"{label} must be an integer")
    if result < -(1 << 63) or result > (1 << 63) - 1:
        raise TelegramOutboxValidationError(f"{label} is outside signed int64")
    if positive and result <= 0:
        raise TelegramOutboxValidationError(f"{label} must be positive")
    if not positive and result == 0:
        raise TelegramOutboxValidationError(f"{label} must not be zero")
    return result


def _optional_message_id(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _telegram_id(value, label, positive=True)


def _key(value: object) -> str:
    return _strict_text(value, "idempotency key", maximum=512)


def stable_random_id(idempotency_key: str) -> int:
    """Return a deterministic, non-zero signed int64 MTProto random id."""

    key = _key(idempotency_key)
    digest = hashlib.sha256(
        b"praxis.telegram.outbox.random-id.v1\0" + key.encode("utf-8")
    ).digest()
    unsigned = int.from_bytes(digest[:8], "big", signed=False)
    signed = unsigned - (1 << 64) if unsigned >= (1 << 63) else unsigned
    return signed or 1


def _entry_id(key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"outbox-{digest}"


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short Telegram outbox write")
        view = view[written:]


def _atomic_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(str(temp), flags, mode)
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        try:
            os.chmod(temp, mode)
        except OSError:
            pass
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


class _InterProcessLock:
    """Advisory OS lock; process death releases it without PID probing."""

    def __init__(self, path: Path, timeout: float = 10.0):
        self.path = path
        self.timeout = max(0.1, float(timeout))
        self.fd: int | None = None

    def __enter__(self) -> "_InterProcessLock":
        if self.path.is_symlink():
            raise TelegramOutboxSecurityError("outbox lock must not be a symlink")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        self.fd = os.open(str(self.path), flags, 0o600)
        if os.fstat(self.fd).st_size == 0:
            os.write(self.fd, b"0")
            os.fsync(self.fd)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._try_lock()
                return self
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    self.fd = None
                    raise TimeoutError("Telegram outbox is busy")
                time.sleep(0.02)

    def _try_lock(self) -> None:
        assert self.fd is not None
        if os.name == "nt":
            import msvcrt

            os.lseek(self.fd, 0, os.SEEK_SET)
            msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None


def _unique_json(payload: bytes) -> dict[str, Any]:
    def unique(pairs):
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    value = json.loads(payload.decode("utf-8"), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("event must be an object")
    return value


class TelegramOutbox:
    """Crash-safe intent/receipt store for exact Telegram sends."""

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_text_bytes: int = DEFAULT_MAX_TEXT_BYTES,
        max_attempts: int | None = DEFAULT_MAX_ATTEMPTS,
        base_backoff_seconds: float = DEFAULT_BASE_BACKOFF_SECONDS,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        clock: Callable[[], float] = time.time,
        lock_timeout: float = 10.0,
    ) -> None:
        if root is None:
            base = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
            root = base / "memory" / ".state" / "telegram_outbox"
        raw_root = Path(root).expanduser()
        if raw_root.exists() and raw_root.is_symlink():
            raise TelegramOutboxSecurityError("outbox root must not be a symlink")
        raw_root.mkdir(parents=True, exist_ok=True)
        self.root = raw_root.resolve()
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self.entries_dir = self._directory("entries")
        self.files_dir = self._directory("files")
        self.quarantine_dir = self._directory("quarantine")
        self.lock_path = self.root / ".lock"
        self.max_file_bytes = _positive_int(max_file_bytes, "max_file_bytes")
        self.max_text_bytes = _positive_int(max_text_bytes, "max_text_bytes")
        if max_attempts is not None:
            max_attempts = _positive_int(max_attempts, "max_attempts")
        self.max_attempts = max_attempts
        self.base_backoff_seconds = _finite_number(
            base_backoff_seconds, "base_backoff_seconds"
        )
        self.max_backoff_seconds = _finite_number(
            max_backoff_seconds, "max_backoff_seconds"
        )
        if self.base_backoff_seconds <= 0 or self.max_backoff_seconds <= 0:
            raise TelegramOutboxValidationError("backoff values must be positive")
        if self.max_backoff_seconds < self.base_backoff_seconds:
            raise TelegramOutboxValidationError(
                "max_backoff_seconds must be >= base_backoff_seconds"
            )
        self.clock = clock
        self.lock_timeout = max(0.1, _finite_number(lock_timeout, "lock_timeout"))
        self.recover()

    def _directory(self, name: str) -> Path:
        path = self.root / name
        if path.exists() and path.is_symlink():
            raise TelegramOutboxSecurityError(f"outbox {name} must not be a symlink")
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
        return path.resolve()

    @contextlib.contextmanager
    def _guard(self) -> Iterator[None]:
        with _LOCAL_LOCK, _InterProcessLock(self.lock_path, self.lock_timeout):
            yield

    def _event_path(self, entry_id: str) -> Path:
        if not _ENTRY_RE.fullmatch(entry_id):
            raise TelegramOutboxValidationError("invalid outbox entry id")
        return self.entries_dir / f"{entry_id}.jsonl"

    def _blob_path(self, entry_id: str) -> Path:
        if not _ENTRY_RE.fullmatch(entry_id):
            raise TelegramOutboxValidationError("invalid outbox entry id")
        return self.files_dir / f"{entry_id}.blob"

    def _new_event(self, entry_id: str, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        now = _finite_number(self.clock(), "clock result")
        return {
            "schema": EVENT_SCHEMA,
            "event_id": f"outbox-event-{uuid.uuid4().hex}",
            "entry_id": entry_id,
            "at": _iso_utc(now),
            "at_epoch": now,
            "kind": kind,
            "data": data,
        }

    @staticmethod
    def _validate_event(row: dict[str, Any], expected_entry_id: str) -> None:
        if set(row) != {
            "schema", "event_id", "entry_id", "at", "at_epoch", "kind", "data"
        }:
            raise ValueError("event has unexpected fields")
        if row.get("schema") != EVENT_SCHEMA:
            raise ValueError("invalid event schema")
        if not isinstance(row.get("event_id"), str) or not _EVENT_ID_RE.fullmatch(
            row["event_id"]
        ):
            raise ValueError("invalid event id")
        if row.get("entry_id") != expected_entry_id:
            raise ValueError("entry id/path mismatch")
        if row.get("kind") not in _EVENT_KINDS or not isinstance(row.get("data"), dict):
            raise ValueError("invalid event kind/data")
        if not isinstance(row.get("at"), str) or not row["at"].endswith("Z"):
            raise ValueError("invalid event timestamp")
        dt.datetime.fromisoformat(row["at"][:-1] + "+00:00")
        _finite_number(row.get("at_epoch"), "event timestamp")

    def _append_event_locked(self, path: Path, row: dict[str, Any]) -> None:
        payload = (
            json.dumps(
                row, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(str(path), flags, 0o600)
        try:
            _write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_directory(path.parent)

    def _quarantine_bytes_locked(self, source: str, payload: bytes, reason: str) -> None:
        token = f"{int(self.clock() * 1000):013d}-{uuid.uuid4().hex}"
        raw_path = self.quarantine_dir / f"{token}.bin"
        meta_path = self.quarantine_dir / f"{token}.json"
        _atomic_bytes(raw_path, payload)
        metadata = {
            "schema": "praxis.telegram.outbox.quarantine.v1",
            "at": _iso_utc(float(self.clock())),
            "source": str(source)[:300],
            "reason": str(reason)[:1000],
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "artifact": raw_path.name,
        }
        _atomic_bytes(
            meta_path,
            (json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
        )

    def _quarantine_path_locked(self, path: Path, reason: str) -> None:
        token = f"{int(self.clock() * 1000):013d}-{uuid.uuid4().hex}"
        target = self.quarantine_dir / f"{token}.artifact"
        os.replace(path, target)
        _fsync_directory(path.parent)
        _fsync_directory(self.quarantine_dir)
        metadata = {
            "schema": "praxis.telegram.outbox.quarantine.v1",
            "at": _iso_utc(float(self.clock())),
            "source": path.name,
            "reason": str(reason)[:1000],
            "artifact": target.name,
        }
        _atomic_bytes(
            self.quarantine_dir / f"{token}.json",
            (json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
        )

    def _intent_state(self, row: dict[str, Any]) -> dict[str, Any]:
        data = row["data"]
        if set(data) != {"key", "kind", "intent", "delivery", "payload", "random_id"}:
            raise ValueError("invalid intent fields")
        key = _key(data.get("key"))
        entry_id = _entry_id(key)
        if entry_id != row["entry_id"]:
            raise ValueError("idempotency key hash mismatch")
        kind = data.get("kind")
        if kind not in {"text", "file"}:
            raise ValueError("invalid payload kind")
        intent = data.get("intent")
        if not isinstance(intent, dict) or set(intent) != {"run_id", "call_id", "purpose"}:
            raise ValueError("invalid intent metadata")
        run_id = _strict_text(intent.get("run_id"), "run_id", maximum=256)
        call_id = _strict_text(intent.get("call_id"), "call_id", maximum=256)
        purpose = _strict_text(intent.get("purpose"), "purpose", maximum=256)
        delivery = data.get("delivery")
        if not isinstance(delivery, dict) or set(delivery) != {
            "peer_id", "topic_id", "reply_to"
        }:
            raise ValueError("invalid delivery fields")
        peer_id = _telegram_id(delivery.get("peer_id"), "peer_id", positive=False)
        topic_id = _optional_message_id(delivery.get("topic_id"), "topic_id")
        reply_to = _optional_message_id(delivery.get("reply_to"), "reply_to")
        expected_random_id = stable_random_id(key)
        if data.get("random_id") != expected_random_id:
            raise ValueError("random id/key mismatch")
        payload = data.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("invalid payload")
        if kind == "text":
            if set(payload) != {"text"}:
                raise ValueError("invalid text payload")
            text = _optional_body_text(payload.get("text"), "text", maximum=self.max_text_bytes)
            if not text or len(text.encode("utf-8")) > self.max_text_bytes:
                raise ValueError("text is empty or exceeds configured byte limit")
            canonical_payload = {"text": text}
        else:
            if set(payload) != {
                "staged_relpath", "visible_filename", "mime", "sha256", "size", "caption"
            }:
                raise ValueError("invalid file payload")
            expected_relpath = f"files/{row['entry_id']}.blob"
            if payload.get("staged_relpath") != expected_relpath:
                raise ValueError("staged path/entry mismatch")
            visible = self._visible_filename(payload.get("visible_filename"))
            mime = self._mime(payload.get("mime"))
            sha256 = payload.get("sha256")
            if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
                raise ValueError("invalid staged SHA-256")
            size = _positive_int(payload.get("size"), "staged size")
            if size > self.max_file_bytes:
                raise ValueError("staged file exceeds configured byte limit")
            caption = _optional_body_text(payload.get("caption"), "caption", maximum=4096)
            canonical_payload = {
                "staged_relpath": expected_relpath,
                "visible_filename": visible,
                "mime": mime,
                "sha256": sha256,
                "size": size,
                "caption": caption,
            }
        return {
            "id": row["entry_id"],
            "key": key,
            "kind": kind,
            "state": "pending",
            "run_id": run_id,
            "call_id": call_id,
            "purpose": purpose,
            "peer_id": peer_id,
            "topic_id": topic_id,
            "reply_to": reply_to,
            "random_id": expected_random_id,
            "payload": canonical_payload,
            "attempts": 0,
            "next_attempt_at": float(row["at_epoch"]),
            "last_error": "",
            "receipt": None,
            "created_at": row["at"],
            "updated_at": row["at"],
        }

    def _apply_event(
        self, state: dict[str, Any] | None, row: dict[str, Any]
    ) -> dict[str, Any]:
        kind = row["kind"]
        data = row["data"]
        if state is None:
            if kind != "intent":
                raise ValueError("journal does not begin with intent")
            return self._intent_state(row)
        result = dict(state)
        if kind == "intent":
            raise ValueError("duplicate intent")
        if kind == "accepted":
            if set(data) != {"message_id", "random_id"}:
                raise ValueError("invalid acceptance receipt")
            message_id = _telegram_id(data.get("message_id"), "message_id", positive=True)
            random_id = _telegram_id(data.get("random_id"), "random_id", positive=False)
            if random_id != state["random_id"]:
                raise ValueError("acceptance random id mismatch")
            if state["state"] == "accepted":
                if state["receipt"] != {"message_id": message_id, "random_id": random_id}:
                    raise ValueError("conflicting acceptance receipt")
                return state
            result["state"] = "accepted"
            result["receipt"] = {"message_id": message_id, "random_id": random_id}
            result["next_attempt_at"] = None
            result["last_error"] = ""
        elif kind == "retry":
            if state["state"] not in {"pending", "retry"}:
                raise ValueError("retry after terminal state")
            if set(data) != {"attempt", "error", "delay_seconds", "next_attempt_at"}:
                raise ValueError("invalid retry fields")
            attempt = _positive_int(data.get("attempt"), "attempt")
            if attempt != state["attempts"] + 1:
                raise ValueError("non-sequential retry attempt")
            error = _optional_body_text(data.get("error"), "error", maximum=4000)
            delay = _finite_number(data.get("delay_seconds"), "retry delay")
            next_at = _finite_number(data.get("next_attempt_at"), "next attempt")
            if delay < 0 or delay > self.max_backoff_seconds:
                raise ValueError("retry delay outside configured bound")
            result["state"] = "retry"
            result["attempts"] = attempt
            result["last_error"] = error
            result["next_attempt_at"] = next_at
        elif kind == "dead_letter":
            if state["state"] == "accepted":
                raise ValueError("dead-letter after acceptance")
            if set(data) != {"reason", "attempts", "automatic"}:
                raise ValueError("invalid dead-letter fields")
            reason = _optional_body_text(
                data.get("reason"), "dead-letter reason", maximum=4000
            )
            if not reason:
                raise ValueError("dead-letter reason must not be empty")
            attempts = data.get("attempts")
            if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
                raise ValueError("invalid dead-letter attempt count")
            if attempts < state["attempts"] or not isinstance(data.get("automatic"), bool):
                raise ValueError("invalid dead-letter transition")
            result["state"] = "dead_letter"
            result["attempts"] = attempts
            result["last_error"] = reason
            result["next_attempt_at"] = None
        elif kind == "requeue":
            if state["state"] != "dead_letter":
                raise ValueError("only a dead-letter can be requeued")
            if set(data) != {"reason"}:
                raise ValueError("invalid requeue fields")
            _strict_text(data.get("reason"), "requeue reason", maximum=1000)
            result["state"] = "pending"
            result["attempts"] = 0
            result["last_error"] = ""
            result["next_attempt_at"] = float(row["at_epoch"])
        result["updated_at"] = row["at"]
        return result

    def _load_state_locked(self, path: Path) -> dict[str, Any] | None:
        if path.is_symlink():
            self._quarantine_path_locked(path, "journal is a symlink")
            return None
        entry_id = path.name[:-6] if path.name.endswith(".jsonl") else ""
        if not _ENTRY_RE.fullmatch(entry_id) or path.parent.resolve() != self.entries_dir:
            self._quarantine_path_locked(path, "invalid journal path")
            return None
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise TelegramOutboxIntegrityError(f"cannot read outbox journal: {exc}") from exc
        dirty = False
        if raw and not raw.endswith(b"\n"):
            boundary = raw.rfind(b"\n") + 1
            self._quarantine_bytes_locked(path.name, raw[boundary:], "unterminated crash tail")
            raw = raw[:boundary]
            dirty = True
        good: list[bytes] = []
        state: dict[str, Any] | None = None
        seen: set[str] = set()
        for line in raw.splitlines(keepends=True):
            try:
                row = _unique_json(line)
                self._validate_event(row, entry_id)
                if row["event_id"] in seen:
                    raise ValueError("duplicate event id")
                candidate = self._apply_event(state, row)
            except (ValueError, TypeError, UnicodeError) as exc:
                self._quarantine_bytes_locked(path.name, line, f"invalid event: {exc}")
                dirty = True
                continue
            state = candidate
            seen.add(row["event_id"])
            good.append(line)
        if dirty:
            if good:
                _atomic_bytes(path, b"".join(good))
            else:
                path.unlink(missing_ok=True)
                _fsync_directory(path.parent)
        return state

    def _state_for_key_locked(self, key: str) -> dict[str, Any] | None:
        path = self._event_path(_entry_id(key))
        if not path.exists():
            return None
        return self._load_state_locked(path)

    @staticmethod
    def _visible_filename(value: object) -> str:
        if not isinstance(value, str) or not value or len(value) > 240:
            raise TelegramOutboxValidationError("invalid visible filename")
        if value in {".", ".."} or any(
            char in "/\\" or ord(char) < 32 or ord(char) == 127 for char in value
        ):
            raise TelegramOutboxValidationError("visible filename must be one basename")
        return value

    @staticmethod
    def _mime(value: object) -> str:
        if not isinstance(value, str) or not _MIME_RE.fullmatch(value):
            raise TelegramOutboxValidationError("invalid MIME type")
        return value.lower()

    def _base_intent(
        self,
        *,
        key: str,
        kind: str,
        peer_id: object,
        topic_id: object,
        reply_to: object,
        run_id: object,
        call_id: object,
        purpose: object,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "key": key,
            "kind": kind,
            "intent": {
                "run_id": _strict_text(run_id, "run_id", maximum=256),
                "call_id": _strict_text(call_id, "call_id", maximum=256),
                "purpose": _strict_text(purpose, "purpose", maximum=256),
            },
            "delivery": {
                "peer_id": _telegram_id(peer_id, "peer_id", positive=False),
                "topic_id": _optional_message_id(topic_id, "topic_id"),
                "reply_to": _optional_message_id(reply_to, "reply_to"),
            },
            "payload": payload,
            "random_id": stable_random_id(key),
        }

    @staticmethod
    def _immutable_view(state: dict[str, Any]) -> dict[str, Any]:
        return {
            "key": state["key"],
            "kind": state["kind"],
            "intent": {
                "run_id": state["run_id"], "call_id": state["call_id"],
                "purpose": state["purpose"],
            },
            "delivery": {
                "peer_id": state["peer_id"], "topic_id": state["topic_id"],
                "reply_to": state["reply_to"],
            },
            "payload": dict(state["payload"]),
            "random_id": state["random_id"],
        }

    def _public(self, state: dict[str, Any], *, verify_file: bool = False) -> dict[str, Any]:
        result = json.loads(json.dumps(state, ensure_ascii=False))
        if result["kind"] == "file":
            path = self._blob_path(result["id"])
            result["payload"]["staged_path"] = str(path)
            if verify_file:
                self._verify_staged_state(result)
        return result

    def prepare_text(
        self,
        idempotency_key: str,
        *,
        peer_id: int | str,
        text: str,
        run_id: str,
        call_id: str,
        purpose: str,
        topic_id: int | str | None = None,
        reply_to: int | str | None = None,
    ) -> dict[str, Any]:
        key = _key(idempotency_key)
        body = _optional_body_text(text, "text", maximum=self.max_text_bytes)
        if not body or len(body.encode("utf-8")) > self.max_text_bytes:
            raise TelegramOutboxValidationError("text is empty or exceeds max_text_bytes")
        data = self._base_intent(
            key=key, kind="text", peer_id=peer_id, topic_id=topic_id,
            reply_to=reply_to, run_id=run_id, call_id=call_id, purpose=purpose,
            payload={"text": body},
        )
        with self._guard():
            state = self._state_for_key_locked(key)
            if state is not None:
                if self._immutable_view(state) != data:
                    raise TelegramOutboxConflict("idempotency key already owns another intent")
                return self._public(state)
            entry_id = _entry_id(key)
            row = self._new_event(entry_id, "intent", data)
            self._append_event_locked(self._event_path(entry_id), row)
            state = self._apply_event(None, row)
            return self._public(state)

    def _source_info(self, source: str | os.PathLike[str], destination: Path | None) -> tuple[str, int]:
        raw = Path(source).expanduser()
        if raw.is_symlink():
            raise TelegramOutboxSecurityError("source file must not be a symlink")
        try:
            resolved = raw.resolve(strict=True)
        except OSError as exc:
            raise TelegramOutboxValidationError(f"source file is unavailable: {exc}") from exc
        try:
            resolved.relative_to(self.root)
        except ValueError:
            pass
        else:
            raise TelegramOutboxSecurityError("source file must be outside the outbox")
        flags = os.O_RDONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            source_fd = os.open(str(resolved), flags)
        except OSError as exc:
            raise TelegramOutboxValidationError(f"cannot open source file: {exc}") from exc
        destination_fd: int | None = None
        try:
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
                raise TelegramOutboxValidationError("source must be a non-empty regular file")
            if before.st_size > self.max_file_bytes:
                raise TelegramOutboxValidationError("source exceeds max_file_bytes")
            if destination is not None:
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
                if hasattr(os, "O_BINARY"):
                    flags |= os.O_BINARY
                destination_fd = os.open(str(destination), flags, 0o600)
            digest = hashlib.sha256()
            copied = 0
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > self.max_file_bytes:
                    raise TelegramOutboxValidationError("source exceeds max_file_bytes")
                digest.update(chunk)
                if destination_fd is not None:
                    _write_all(destination_fd, chunk)
            after = os.fstat(source_fd)
            stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
            if any(getattr(before, field, None) != getattr(after, field, None)
                   for field in stable_fields) or copied != before.st_size:
                raise TelegramOutboxIntegrityError("source changed while being staged")
            if destination_fd is not None:
                os.fsync(destination_fd)
            return digest.hexdigest(), copied
        finally:
            if destination_fd is not None:
                os.close(destination_fd)
            os.close(source_fd)

    def prepare_file(
        self,
        idempotency_key: str,
        *,
        peer_id: int | str,
        source: str | os.PathLike[str],
        visible_filename: str | None,
        mime: str,
        caption: str,
        run_id: str,
        call_id: str,
        purpose: str,
        topic_id: int | str | None = None,
        reply_to: int | str | None = None,
    ) -> dict[str, Any]:
        key = _key(idempotency_key)
        entry_id = _entry_id(key)
        raw_source = Path(source).expanduser()
        filename = self._visible_filename(visible_filename or raw_source.name)
        mime_value = self._mime(mime)
        caption_value = _optional_body_text(caption, "caption", maximum=4096)
        common = self._base_intent(
            key=key, kind="file", peer_id=peer_id, topic_id=topic_id,
            reply_to=reply_to, run_id=run_id, call_id=call_id, purpose=purpose,
            payload={},
        )
        with self._guard():
            state = self._state_for_key_locked(key)
            if state is not None:
                if state["kind"] != "file":
                    raise TelegramOutboxConflict("idempotency key already owns another intent")
                sha256, size = self._source_info(source, None)
                expected_payload = {
                    "staged_relpath": f"files/{entry_id}.blob",
                    "visible_filename": filename,
                    "mime": mime_value,
                    "sha256": sha256,
                    "size": size,
                    "caption": caption_value,
                }
                common["payload"] = expected_payload
                if self._immutable_view(state) != common:
                    raise TelegramOutboxConflict("idempotency key already owns another intent")
                return self._public(state, verify_file=True)
            blob = self._blob_path(entry_id)
            temp = blob.with_name(f".{blob.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            try:
                sha256, size = self._source_info(source, temp)
                if blob.exists() or blob.is_symlink():
                    self._quarantine_path_locked(blob, "orphan staged file replaced by new intent")
                os.replace(temp, blob)
                _fsync_directory(self.files_dir)
            finally:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
            common["payload"] = {
                "staged_relpath": f"files/{entry_id}.blob",
                "visible_filename": filename,
                "mime": mime_value,
                "sha256": sha256,
                "size": size,
                "caption": caption_value,
            }
            row = self._new_event(entry_id, "intent", common)
            self._append_event_locked(self._event_path(entry_id), row)
            state = self._apply_event(None, row)
            return self._public(state, verify_file=True)

    def _verify_staged_state(self, state: dict[str, Any]) -> None:
        if state["kind"] != "file":
            return
        path = self._blob_path(state["id"])
        if path.is_symlink() or not path.is_file():
            raise TelegramOutboxIntegrityError(f"staged file is missing: {state['id']}")
        payload = state["payload"]
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise TelegramOutboxIntegrityError(f"cannot stat staged file: {exc}") from exc
        if size != payload["size"] or _sha256_path(path) != payload["sha256"]:
            raise TelegramOutboxIntegrityError(f"staged file integrity mismatch: {state['id']}")

    def get(self, idempotency_key: str, *, verify_file: bool = True) -> dict[str, Any] | None:
        key = _key(idempotency_key)
        with self._guard():
            state = self._state_for_key_locked(key)
            return self._public(state, verify_file=verify_file) if state else None

    def pending(
        self, *, due_only: bool = False, now: float | None = None,
        verify_files: bool = True,
    ) -> tuple[dict[str, Any], ...]:
        current = _finite_number(self.clock() if now is None else now, "pending time")
        with self._guard():
            states: list[dict[str, Any]] = []
            for path in sorted(self.entries_dir.glob("*.jsonl")):
                state = self._load_state_locked(path)
                if state is None or state["state"] not in {"pending", "retry"}:
                    continue
                if due_only and float(state["next_attempt_at"]) > current:
                    continue
                states.append(self._public(state, verify_file=verify_files))
        return tuple(sorted(states, key=lambda row: (row["created_at"], row["id"])))

    def dead_letters(self, *, verify_files: bool = False) -> tuple[dict[str, Any], ...]:
        with self._guard():
            states = []
            for path in sorted(self.entries_dir.glob("*.jsonl")):
                state = self._load_state_locked(path)
                if state is not None and state["state"] == "dead_letter":
                    states.append(self._public(state, verify_file=verify_files))
        return tuple(sorted(states, key=lambda row: (row["updated_at"], row["id"])))

    def accepted(self, *, verify_files: bool = False) -> tuple[dict[str, Any], ...]:
        """Enumerate durable network receipts for cross-ledger reconciliation."""

        with self._guard():
            states = []
            for path in sorted(self.entries_dir.glob("*.jsonl")):
                state = self._load_state_locked(path)
                if state is not None and state["state"] == "accepted":
                    states.append(self._public(state, verify_file=verify_files))
        return tuple(sorted(states, key=lambda row: (row["updated_at"], row["id"])))

    def mark_accepted(
        self, idempotency_key: str, *, message_id: int | str,
        random_id: int | str | None = None,
    ) -> dict[str, Any]:
        key = _key(idempotency_key)
        with self._guard():
            state = self._state_for_key_locked(key)
            if state is None:
                raise KeyError(key)
            message = _telegram_id(message_id, "message_id", positive=True)
            random_value = state["random_id"] if random_id is None else _telegram_id(
                random_id, "random_id", positive=False
            )
            receipt = {"message_id": message, "random_id": random_value}
            if state["state"] == "accepted":
                if state["receipt"] != receipt:
                    raise TelegramOutboxConflict("accepted receipt is immutable")
                return self._public(state)
            if random_value != state["random_id"]:
                raise TelegramOutboxConflict("accepted random_id does not match intent")
            row = self._new_event(state["id"], "accepted", receipt)
            self._append_event_locked(self._event_path(state["id"]), row)
            return self._public(self._apply_event(state, row))

    def record_retry(
        self, idempotency_key: str, error: object, *,
        retry_after_seconds: float | None = None, now: float | None = None,
    ) -> dict[str, Any]:
        key = _key(idempotency_key)
        error_text = str(error or "unknown Telegram send failure").replace("\x00", "")[:4000]
        if not error_text:
            error_text = "unknown Telegram send failure"
        current = _finite_number(self.clock() if now is None else now, "retry time")
        with self._guard():
            state = self._state_for_key_locked(key)
            if state is None:
                raise KeyError(key)
            if state["state"] in {"accepted", "dead_letter"}:
                return self._public(state)
            attempt = int(state["attempts"]) + 1
            if self.max_attempts is not None and attempt >= self.max_attempts:
                row = self._new_event(state["id"], "dead_letter", {
                    "reason": error_text, "attempts": attempt, "automatic": True,
                })
            else:
                if retry_after_seconds is None:
                    delay = min(
                        self.max_backoff_seconds,
                        self.base_backoff_seconds * (2 ** min(attempt - 1, 62)),
                    )
                else:
                    delay = max(
                        0.0,
                        min(
                            self.max_backoff_seconds,
                            _finite_number(retry_after_seconds, "retry_after_seconds"),
                        ),
                    )
                row = self._new_event(state["id"], "retry", {
                    "attempt": attempt,
                    "error": error_text,
                    "delay_seconds": delay,
                    "next_attempt_at": current + delay,
                })
            self._append_event_locked(self._event_path(state["id"]), row)
            return self._public(self._apply_event(state, row))

    def dead_letter(self, idempotency_key: str, reason: str) -> dict[str, Any]:
        key = _key(idempotency_key)
        reason_value = _optional_body_text(reason, "dead-letter reason", maximum=4000)
        if not reason_value:
            raise TelegramOutboxValidationError("dead-letter reason must not be empty")
        with self._guard():
            state = self._state_for_key_locked(key)
            if state is None:
                raise KeyError(key)
            if state["state"] in {"accepted", "dead_letter"}:
                return self._public(state)
            row = self._new_event(state["id"], "dead_letter", {
                "reason": reason_value, "attempts": state["attempts"], "automatic": False,
            })
            self._append_event_locked(self._event_path(state["id"]), row)
            return self._public(self._apply_event(state, row))

    def requeue(self, idempotency_key: str, *, reason: str) -> dict[str, Any]:
        key = _key(idempotency_key)
        reason_value = _strict_text(reason, "requeue reason", maximum=1000)
        with self._guard():
            state = self._state_for_key_locked(key)
            if state is None:
                raise KeyError(key)
            if state["state"] != "dead_letter":
                return self._public(state)
            row = self._new_event(state["id"], "requeue", {"reason": reason_value})
            self._append_event_locked(self._event_path(state["id"]), row)
            return self._public(self._apply_event(state, row), verify_file=True)

    def recover(self) -> dict[str, int]:
        """Repair journals and quarantine orphan/torn artifacts without deleting evidence."""

        with self._guard():
            states: list[dict[str, Any]] = []
            quarantined_before = len(list(self.quarantine_dir.glob("*.json")))
            for path in list(self.entries_dir.iterdir()):
                if path.is_symlink() or not path.is_file() or not re.fullmatch(
                    r"outbox-[0-9a-f]{64}\.jsonl", path.name
                ):
                    self._quarantine_path_locked(path, "unexpected journal artifact")
                    continue
                state = self._load_state_locked(path)
                if state is not None:
                    states.append(state)
            referenced = {
                f"{state['id']}.blob" for state in states if state["kind"] == "file"
            }
            for path in list(self.files_dir.iterdir()):
                if path.name not in referenced or path.is_symlink() or not path.is_file():
                    self._quarantine_path_locked(path, "orphan or invalid staged artifact")
            quarantined_after = len(list(self.quarantine_dir.glob("*.json")))
            return {
                "entries": len(states),
                "pending": sum(s["state"] in {"pending", "retry"} for s in states),
                "accepted": sum(s["state"] == "accepted" for s in states),
                "dead_letters": sum(s["state"] == "dead_letter" for s in states),
                "quarantined": max(0, quarantined_after - quarantined_before),
            }

    def quarantine_records(self) -> tuple[dict[str, Any], ...]:
        records: list[dict[str, Any]] = []
        with self._guard():
            for path in sorted(self.quarantine_dir.glob("*.json")):
                try:
                    row = _unique_json(path.read_bytes())
                except (OSError, ValueError, UnicodeError):
                    continue
                if row.get("schema") == "praxis.telegram.outbox.quarantine.v1":
                    records.append(row)
        return tuple(records)


__all__ = [
    "DEFAULT_BASE_BACKOFF_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_BACKOFF_SECONDS",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_TEXT_BYTES",
    "EVENT_SCHEMA",
    "TelegramOutbox",
    "TelegramOutboxConflict",
    "TelegramOutboxError",
    "TelegramOutboxIntegrityError",
    "TelegramOutboxSecurityError",
    "TelegramOutboxValidationError",
    "stable_random_id",
]
