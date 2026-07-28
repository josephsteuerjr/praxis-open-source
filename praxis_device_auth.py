"""Owner-rooted, persistent device enrollment for the Praxis PWA.

This module performs no HTTP work.  The integrating route authenticates the
human owner and passes an :class:`OwnerPrincipal` to owner-only methods; a
redeem route may call :meth:`DeviceAuthStore.redeem` without authentication.
Secrets are returned once, never logged, and never written to the event ledger.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
from typing import Callable, Iterable
import unicodedata
import urllib.parse
import uuid


SCHEMA = "praxis.device-auth.event.v1"
DEFAULT_SCOPES = ("praxis.events", "praxis.snapshot")
ALLOWED_DEVICE_SCOPES = frozenset({
    "praxis.snapshot",
    "praxis.events",
    "praxis.runs.control",
    "praxis.work",
    "praxis.telegram",
    "praxis.system.read",
    "praxis.system.control",
    "computer.read",
    "computer.files",
    "computer.process",
    "computer.apps",
})
MAX_TTL_SECONDS = 24 * 60 * 60
_ZERO_MAC = "0" * 64
_MAC_RE = re.compile(r"^[0-9a-f]{64}$")
_ENROLLMENT_ID_RE = re.compile(r"^enr_[0-9a-f]{32}$")
_DEVICE_ID_RE = re.compile(r"^dev_[0-9a-f]{32}$")
_ENROLLMENT_TOKEN_RE = re.compile(
    r"^praxis_enroll_(enr_[0-9a-f]{32})\.([A-Za-z0-9_-]{40,})$"
)
_DEVICE_TOKEN_RE = re.compile(
    r"^praxis_device_(dev_[0-9a-f]{32})\.([A-Za-z0-9_-]{40,})$"
)
_LOCAL_LOCK = threading.RLock()


class DeviceAuthError(RuntimeError):
    pass


class DeviceAuthPermissionError(DeviceAuthError):
    pass


class DeviceAuthCorruption(DeviceAuthError):
    pass


class InvalidEnrollment(DeviceAuthError):
    pass


class EnrollmentExpired(InvalidEnrollment):
    pass


class EnrollmentConsumed(InvalidEnrollment):
    pass


@dataclass(frozen=True)
class OwnerPrincipal:
    """An already-authenticated owner identity supplied by the HTTP boundary."""

    owner_id: str

    def __post_init__(self) -> None:
        owner_id = str(self.owner_id or "").strip()
        if (not owner_id or len(owner_id) > 200
                or any(unicodedata.category(char).startswith("C") for char in owner_id)):
            raise ValueError("owner_id is empty")
        object.__setattr__(self, "owner_id", owner_id)


@dataclass(frozen=True)
class DevicePrincipal:
    device_id: str
    label: str
    platform: str
    scopes: tuple[str, ...]
    issued_at: str

    def allows(self, scope: str) -> bool:
        return str(scope or "") in self.scopes


@dataclass(frozen=True, repr=False)
class EnrollmentCredential:
    enrollment_id: str
    label: str
    scopes: tuple[str, ...]
    expires_at: str
    enrollment_token: str

    def __repr__(self) -> str:
        return (
            "EnrollmentCredential("
            f"enrollment_id={self.enrollment_id!r}, label={self.label!r}, "
            f"scopes={self.scopes!r}, expires_at={self.expires_at!r}, "
            "enrollment_token=<redacted>)"
        )

    def url(self, base_url: str) -> str:
        return build_enrollment_url(base_url, self.enrollment_token)


@dataclass(frozen=True, repr=False)
class DeviceCredential:
    principal: DevicePrincipal
    bearer_token: str

    def __repr__(self) -> str:
        return f"DeviceCredential(principal={self.principal!r}, bearer_token=<redacted>)"


def build_enrollment_url(base_url: str, enrollment_token: str) -> str:
    """Put the one-time secret in a URL fragment, which is absent from HTTP logs."""
    token = str(enrollment_token or "")
    if _ENROLLMENT_TOKEN_RE.fullmatch(token) is None:
        raise ValueError("invalid enrollment token")
    parsed = urllib.parse.urlsplit(str(base_url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("enrollment base URL must be absolute HTTP(S)")
    fragment = urllib.parse.urlencode({"enroll": token})
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, fragment)
    )


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError as exc:
        if os.name != "nt":
            raise DeviceAuthCorruption(f"cannot make {path.name} private") from exc
        return
    if os.name != "nt":
        try:
            actual = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
        except OSError as exc:
            raise DeviceAuthCorruption(f"cannot inspect {path.name} permissions") from exc
        if actual != mode:
            raise DeviceAuthCorruption(f"{path.name} permissions are not private")


def _ensure_private_dir(path: Path) -> None:
    if path.is_symlink():
        raise DeviceAuthCorruption(f"private directory {path.name} is a symlink")
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise DeviceAuthCorruption(f"private path {path.name} is not a directory")
    _chmod(path, 0o700)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
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
            raise OSError("short secure write")
        view = view[written:]


class _ProcessLock:
    """Cross-thread and cross-process advisory lock; no secret is stored in it."""

    def __init__(self, path: Path, timeout: float = 10.0):
        self.path = path
        self.timeout = max(0.1, float(timeout))
        self.fd: int | None = None
        self.local = False

    @staticmethod
    def _try_lock(fd: int) -> bool:
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

    @staticmethod
    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)

    def __enter__(self) -> "_ProcessLock":
        _LOCAL_LOCK.acquire()
        self.local = True
        try:
            _ensure_private_dir(self.path.parent)
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            self.fd = os.open(str(self.path), flags, 0o600)
            _chmod(self.path, 0o600)
            if os.fstat(self.fd).st_size == 0:
                _write_all(self.fd, b"\0")
                os.fsync(self.fd)
            deadline = time.monotonic() + self.timeout
            while not self._try_lock(self.fd):
                if time.monotonic() >= deadline:
                    raise TimeoutError("device-auth store is busy")
                time.sleep(0.01)
            return self
        except Exception:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            if self.local:
                _LOCAL_LOCK.release()
                self.local = False
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self.fd is not None:
                with contextlib.suppress(OSError):
                    self._unlock(self.fd)
                os.close(self.fd)
        finally:
            self.fd = None
            if self.local:
                _LOCAL_LOCK.release()
                self.local = False


def _canonical(value: dict) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _keyed_mac(key: bytes, purpose: str, value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hmac.new(key, purpose.encode("ascii") + b"\0" + raw, hashlib.sha256).hexdigest()


def _utc_iso(epoch: float) -> str:
    return (
        dt.datetime.fromtimestamp(epoch, dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _parse_iso(value: object) -> float:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp is missing")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed.timestamp()


def _clean_text(value: object, *, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid device {field}")
    text = value.strip()
    if (not text or len(text) > limit
            or any(unicodedata.category(char).startswith("C") for char in text)):
        raise ValueError(f"invalid device {field}")
    return text


def _clean_scopes(values: Iterable[str] | None) -> tuple[str, ...]:
    if values is None:
        return DEFAULT_SCOPES
    if isinstance(values, (str, bytes)):
        raise ValueError("device scopes must be a collection")
    raw = tuple(values)
    if any(not isinstance(value, str) for value in raw):
        raise ValueError("device scope is not allowed")
    scopes = tuple(sorted({value.strip() for value in raw}))
    if any(not scope or scope not in ALLOWED_DEVICE_SCOPES for scope in scopes):
        raise ValueError("device scope is not allowed")
    return scopes


def _unique_json(line: str) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    value = json.loads(line, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("device event is not an object")
    return value


class DeviceAuthStore:
    """Canonical HMAC-authenticated enrollment/device event store."""

    def __init__(self, *, base: str | Path | None = None, owner_id: str | int | None = None,
                 clock: Callable[[], float] = time.time, lock_timeout: float = 10.0):
        self.base = Path(base or os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
        configured_owner = (
            owner_id if owner_id is not None else os.environ.get("PRAXIS_OWNER_ID") or ""
        )
        try:
            self.owner_id = OwnerPrincipal(configured_owner).owner_id
        except ValueError as exc:
            raise ValueError("device-auth owner_id is not configured")
        self.clock = clock
        self.key_path = self.base / "memory" / ".state" / "praxis_device_auth.key"
        self.lock_path = self.base / "memory" / ".state" / "praxis_device_auth.lock"
        self.events_path = self.base / "memory" / "access" / "devices" / "events.jsonl"
        self.lock_timeout = lock_timeout
        with self._locked():
            key = self._read_key(initial=True)
            self._load_state(key, repair_tail=False)

    def __repr__(self) -> str:
        return f"DeviceAuthStore(base={str(self.base)!r}, owner_id=<configured>)"

    def _locked(self) -> _ProcessLock:
        return _ProcessLock(self.lock_path, self.lock_timeout)

    def _now(self) -> float:
        value = float(self.clock())
        if not math.isfinite(value):
            raise DeviceAuthError("device-auth clock is invalid")
        return value

    def _require_owner(self, actor: object) -> OwnerPrincipal:
        if type(actor) is not OwnerPrincipal:
            raise DeviceAuthPermissionError("device credentials cannot manage device authority")
        supplied = str(actor.owner_id)
        if not hmac.compare_digest(supplied.encode("utf-8"), self.owner_id.encode("utf-8")):
            raise DeviceAuthPermissionError("owner authority does not match this store")
        return actor

    def _read_key(self, *, initial: bool = False) -> bytes:
        if self.key_path.is_symlink():
            raise DeviceAuthCorruption("device-auth key path is not a regular private file")
        try:
            key = self.key_path.read_bytes()
        except FileNotFoundError:
            has_events = False
            try:
                has_events = self.events_path.stat().st_size > 0
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise DeviceAuthCorruption("device-auth events are unreadable") from exc
            if not initial or has_events:
                raise DeviceAuthCorruption("device-auth key is missing")
            _ensure_private_dir(self.key_path.parent)
            key = secrets.token_bytes(32)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            try:
                fd = os.open(str(self.key_path), flags, 0o600)
            except FileExistsError:
                key = self.key_path.read_bytes()
            else:
                try:
                    _write_all(fd, key)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                _chmod(self.key_path, 0o600)
                _fsync_dir(self.key_path.parent)
        except OSError as exc:
            raise DeviceAuthCorruption("device-auth key is unreadable") from exc
        if len(key) != 32:
            raise DeviceAuthCorruption("device-auth key is corrupt")
        try:
            key_stat = self.key_path.lstat()
        except OSError as exc:
            raise DeviceAuthCorruption("device-auth key is unreadable") from exc
        if not stat.S_ISREG(key_stat.st_mode) or key_stat.st_nlink != 1:
            raise DeviceAuthCorruption("device-auth key is not a private regular file")
        _chmod(self.key_path, 0o600)
        return key

    def _read_ledger(self) -> tuple[list[bytes], bytes, bool]:
        _ensure_private_dir(self.events_path.parent)
        if self.events_path.is_symlink():
            raise DeviceAuthCorruption("device-auth ledger path is not regular")
        try:
            ledger_stat = self.events_path.lstat()
        except FileNotFoundError:
            return [], b"", False
        except OSError as exc:
            raise DeviceAuthCorruption("device-auth ledger is unreadable") from exc
        if not stat.S_ISREG(ledger_stat.st_mode) or ledger_stat.st_nlink != 1:
            raise DeviceAuthCorruption("device-auth ledger is not a private regular file")
        _chmod(self.events_path, 0o600)
        try:
            raw = self.events_path.read_bytes()
        except OSError as exc:
            raise DeviceAuthCorruption("device-auth ledger is unreadable") from exc
        if not raw:
            return [], b"", False
        boundary = len(raw) if raw.endswith(b"\n") else raw.rfind(b"\n") + 1
        complete, tail = raw[:boundary], raw[boundary:]
        lines = complete[:-1].split(b"\n") if complete else []
        complete_tail = False
        if tail:
            try:
                json.loads(tail.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                pass
            else:
                lines.append(tail)
                complete_tail = True
        return lines, tail, complete_tail

    def _terminate_complete_tail(self, tail: bytes) -> None:
        """Make an authenticated complete final event appendable after a short write."""
        if not tail:
            return
        try:
            with self.events_path.open("r+b") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                if size < len(tail):
                    raise DeviceAuthCorruption("device-auth complete tail moved")
                stream.seek(size - len(tail))
                if stream.read(len(tail)) != tail:
                    raise DeviceAuthCorruption("device-auth complete tail changed")
                stream.seek(0, os.SEEK_END)
                stream.write(b"\n")
                stream.flush()
                os.fsync(stream.fileno())
        except DeviceAuthCorruption:
            raise
        except OSError as exc:
            raise DeviceAuthCorruption(
                "device-auth complete tail could not be terminated"
            ) from exc

    def _repair_tail(self, tail: bytes) -> None:
        if not tail:
            return
        _ensure_private_dir(self.events_path.parent)
        evidence = self.events_path.parent / f"events.torn-{uuid.uuid4().hex[:16]}.bin"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(str(evidence), flags, 0o600)
        try:
            _write_all(fd, tail)
            os.fsync(fd)
        finally:
            os.close(fd)
        _chmod(evidence, 0o600)
        _fsync_dir(self.events_path.parent)
        try:
            with self.events_path.open("r+b") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                if size < len(tail):
                    raise DeviceAuthCorruption("device-auth torn tail moved")
                stream.seek(size - len(tail))
                if stream.read(len(tail)) != tail:
                    raise DeviceAuthCorruption("device-auth torn tail changed")
                stream.truncate(size - len(tail))
                stream.flush()
                os.fsync(stream.fileno())
        except DeviceAuthCorruption:
            raise
        except OSError as exc:
            raise DeviceAuthCorruption("device-auth torn tail could not be repaired") from exc

    @staticmethod
    def _common_event(row: dict, *, expected_seq: int, previous_mac: str,
                      key: bytes, seen_ids: set[str]) -> None:
        common = {"schema", "seq", "event_id", "kind", "at", "prev_event_mac", "event_mac"}
        kind_fields = {
            "enrollment_created": {
                "enrollment_id", "enrollment_mac", "label", "scopes", "expires_at", "owner_id",
            },
            "enrollment_redeemed": {
                "enrollment_id", "device_id", "bearer_mac", "platform",
            },
            "device_revoked": {"device_id", "owner_id", "reason"},
        }
        expected = common | kind_fields.get(row.get("kind"), set())
        if not kind_fields.get(row.get("kind")) or set(row) != expected:
            raise DeviceAuthCorruption("device-auth event shape is invalid")
        if row.get("schema") != SCHEMA or type(row.get("seq")) is not int:
            raise DeviceAuthCorruption("device-auth event header is invalid")
        if row["seq"] != expected_seq:
            raise DeviceAuthCorruption("device-auth event sequence is broken")
        event_id = row.get("event_id")
        if (not isinstance(event_id, str) or not re.fullmatch(r"dae_[0-9a-f]{32}", event_id)
                or event_id in seen_ids):
            raise DeviceAuthCorruption("device-auth event id is invalid")
        if row.get("prev_event_mac") != previous_mac:
            raise DeviceAuthCorruption("device-auth event chain is broken")
        try:
            parsed_at = _parse_iso(row.get("at"))
        except (TypeError, ValueError) as exc:
            raise DeviceAuthCorruption("device-auth event timestamp is invalid") from exc
        if row["at"] != _utc_iso(parsed_at):
            raise DeviceAuthCorruption("device-auth event timestamp is not canonical")
        event_mac = row.get("event_mac")
        unsigned = dict(row)
        unsigned.pop("event_mac", None)
        try:
            expected_mac = _keyed_mac(key, "event", _canonical(unsigned))
        except (TypeError, ValueError, UnicodeError) as exc:
            raise DeviceAuthCorruption("device-auth event cannot be authenticated") from exc
        if (not isinstance(event_mac, str) or _MAC_RE.fullmatch(event_mac) is None
                or not hmac.compare_digest(event_mac, expected_mac)):
            raise DeviceAuthCorruption("device-auth event authentication failed")
        seen_ids.add(event_id)

    def _load_state(self, key: bytes, *, repair_tail: bool) -> dict:
        lines, tail, complete_tail = self._read_ledger()
        state = {
            "seq": 0, "last_mac": _ZERO_MAC, "enrollments": {}, "devices": {},
        }
        seen_ids: set[str] = set()
        for raw in lines:
            if not raw:
                raise DeviceAuthCorruption("device-auth ledger contains a blank event")
            try:
                row = _unique_json(raw.decode("utf-8"))
            except (UnicodeError, ValueError) as exc:
                raise DeviceAuthCorruption("device-auth ledger JSON is corrupt") from exc
            try:
                canonical = _canonical(row)
            except (TypeError, ValueError, UnicodeError) as exc:
                raise DeviceAuthCorruption("device-auth ledger event is invalid") from exc
            if raw != canonical:
                raise DeviceAuthCorruption("device-auth ledger event is not canonical")
            self._common_event(
                row, expected_seq=state["seq"] + 1, previous_mac=state["last_mac"],
                key=key, seen_ids=seen_ids,
            )
            kind = row["kind"]
            if kind == "enrollment_created":
                enrollment_id = row["enrollment_id"]
                try:
                    label = _clean_text(row["label"], field="label", limit=100)
                    if type(row["scopes"]) is not list:
                        raise ValueError("scopes are not a list")
                    scopes = _clean_scopes(row["scopes"])
                    created = _parse_iso(row["at"])
                    expires = _parse_iso(row["expires_at"])
                except (TypeError, ValueError) as exc:
                    raise DeviceAuthCorruption("enrollment event metadata is invalid") from exc
                if (not isinstance(enrollment_id, str)
                        or _ENROLLMENT_ID_RE.fullmatch(enrollment_id) is None
                        or enrollment_id in state["enrollments"]
                        or list(scopes) != row["scopes"]
                        or not isinstance(row["enrollment_mac"], str)
                        or _MAC_RE.fullmatch(row["enrollment_mac"]) is None
                        or not isinstance(row["owner_id"], str) or not row["owner_id"]
                        or not hmac.compare_digest(
                            row["owner_id"].encode("utf-8"), self.owner_id.encode("utf-8"),
                        )
                        or row["expires_at"] != _utc_iso(expires)
                        or expires <= created
                        or expires - created > MAX_TTL_SECONDS + 0.001):
                    raise DeviceAuthCorruption("enrollment event is inconsistent")
                state["enrollments"][enrollment_id] = {
                    "enrollment_mac": row["enrollment_mac"], "label": label,
                    "scopes": scopes, "created_at": row["at"],
                    "expires_at": row["expires_at"], "consumed": False,
                }
            elif kind == "enrollment_redeemed":
                enrollment = state["enrollments"].get(row["enrollment_id"])
                device_id = row["device_id"]
                try:
                    platform = _clean_text(row["platform"], field="platform", limit=64)
                    redeemed = _parse_iso(row["at"])
                except (TypeError, ValueError) as exc:
                    raise DeviceAuthCorruption("device event metadata is invalid") from exc
                if (enrollment is None or enrollment["consumed"]
                        or redeemed < _parse_iso(enrollment["created_at"])
                        or redeemed >= _parse_iso(enrollment["expires_at"])
                        or not isinstance(device_id, str)
                        or _DEVICE_ID_RE.fullmatch(device_id) is None
                        or device_id in state["devices"]
                        or not isinstance(row["bearer_mac"], str)
                        or _MAC_RE.fullmatch(row["bearer_mac"]) is None):
                    raise DeviceAuthCorruption("redeemed enrollment event is inconsistent")
                enrollment["consumed"] = True
                enrollment["device_id"] = device_id
                state["devices"][device_id] = {
                    "bearer_mac": row["bearer_mac"], "label": enrollment["label"],
                    "platform": platform, "scopes": enrollment["scopes"],
                    "issued_at": row["at"], "revoked_at": "", "reason": "",
                }
            else:
                device = state["devices"].get(row["device_id"])
                try:
                    reason = _clean_text(row["reason"], field="revoke reason", limit=200)
                except ValueError as exc:
                    raise DeviceAuthCorruption("revoke event metadata is invalid") from exc
                if (device is None or device["revoked_at"]
                        or not isinstance(row["owner_id"], str) or not row["owner_id"]
                        or not hmac.compare_digest(
                            row["owner_id"].encode("utf-8"), self.owner_id.encode("utf-8"),
                        )
                        or _parse_iso(row["at"]) < _parse_iso(device["issued_at"])):
                    raise DeviceAuthCorruption("device revoke event is inconsistent")
                device["revoked_at"] = row["at"]
                device["reason"] = reason
            state["seq"] = row["seq"]
            state["last_mac"] = row["event_mac"]
        if repair_tail and tail:
            if complete_tail:
                self._terminate_complete_tail(tail)
            else:
                self._repair_tail(tail)
        return state

    def _append(self, key: bytes, state: dict, kind: str, fields: dict, *, now: float) -> None:
        row = {
            "schema": SCHEMA,
            "seq": state["seq"] + 1,
            "event_id": "dae_" + uuid.uuid4().hex,
            "kind": kind,
            "at": _utc_iso(now),
            "prev_event_mac": state["last_mac"],
            **fields,
        }
        row["event_mac"] = _keyed_mac(key, "event", _canonical(row))
        payload = _canonical(row) + b"\n"
        _ensure_private_dir(self.events_path.parent)
        if self.events_path.is_symlink():
            raise DeviceAuthCorruption("device-auth ledger path is not regular")
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        created = not self.events_path.exists()
        fd = os.open(str(self.events_path), flags, 0o600)
        try:
            ledger_stat = os.fstat(fd)
            if not stat.S_ISREG(ledger_stat.st_mode) or ledger_stat.st_nlink != 1:
                raise DeviceAuthCorruption(
                    "device-auth ledger is not a private regular file"
                )
            written = os.write(fd, payload)
            if written != len(payload):
                raise OSError("short device-auth event append")
            os.fsync(fd)
        finally:
            os.close(fd)
        _chmod(self.events_path, 0o600)
        if created:
            _fsync_dir(self.events_path.parent)

    @staticmethod
    def _new_id(prefix: str, existing: dict) -> str:
        for _ in range(16):
            value = prefix + secrets.token_hex(16)
            if value not in existing:
                return value
        raise DeviceAuthError("could not allocate an opaque device identifier")

    def create_enrollment(self, actor: OwnerPrincipal, *, label: str,
                          scopes: Iterable[str] | None = None,
                          ttl_seconds: int = 900) -> EnrollmentCredential:
        self._require_owner(actor)
        clean_label = _clean_text(label, field="label", limit=100)
        clean_scopes = _clean_scopes(scopes)
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
            raise ValueError(f"ttl_seconds must be 1..{MAX_TTL_SECONDS}")
        with self._locked():
            now = self._now()
            expires_at = _utc_iso(now + ttl_seconds)
            key = self._read_key()
            state = self._load_state(key, repair_tail=True)
            enrollment_id = self._new_id("enr_", state["enrollments"])
            token = f"praxis_enroll_{enrollment_id}.{secrets.token_urlsafe(32)}"
            token_mac = _keyed_mac(key, "enrollment-token", token)
            self._append(key, state, "enrollment_created", {
                "enrollment_id": enrollment_id,
                "enrollment_mac": token_mac,
                "label": clean_label,
                "scopes": list(clean_scopes),
                "expires_at": expires_at,
                "owner_id": actor.owner_id,
            }, now=now)
        return EnrollmentCredential(
            enrollment_id=enrollment_id, label=clean_label, scopes=clean_scopes,
            expires_at=expires_at, enrollment_token=token,
        )

    def redeem(self, enrollment_token: str, *, platform: str) -> DeviceCredential:
        token = str(enrollment_token or "")
        match = _ENROLLMENT_TOKEN_RE.fullmatch(token)
        if match is None:
            raise InvalidEnrollment("enrollment credential is invalid")
        enrollment_id = match.group(1)
        clean_platform = _clean_text(platform, field="platform", limit=64)
        with self._locked():
            now = self._now()
            key = self._read_key()
            state = self._load_state(key, repair_tail=True)
            enrollment = state["enrollments"].get(enrollment_id)
            candidate_mac = _keyed_mac(key, "enrollment-token", token)
            expected_mac = enrollment["enrollment_mac"] if enrollment else _ZERO_MAC
            valid = hmac.compare_digest(candidate_mac, expected_mac)
            if not valid or enrollment is None:
                raise InvalidEnrollment("enrollment credential is invalid")
            if enrollment["consumed"]:
                raise EnrollmentConsumed("enrollment credential is already used")
            created_at = _parse_iso(enrollment["created_at"])
            if now < created_at or now >= _parse_iso(enrollment["expires_at"]):
                raise EnrollmentExpired("enrollment credential has expired")
            device_id = self._new_id("dev_", state["devices"])
            bearer = f"praxis_device_{device_id}.{secrets.token_urlsafe(32)}"
            bearer_mac = _keyed_mac(key, "device-token", bearer)
            self._append(key, state, "enrollment_redeemed", {
                "enrollment_id": enrollment_id,
                "device_id": device_id,
                "bearer_mac": bearer_mac,
                "platform": clean_platform,
            }, now=now)
        principal = DevicePrincipal(
            device_id=device_id, label=enrollment["label"], platform=clean_platform,
            scopes=tuple(enrollment["scopes"]), issued_at=_utc_iso(now),
        )
        return DeviceCredential(principal=principal, bearer_token=bearer)

    def validate_bearer(self, bearer_token: str, *,
                        required_scope: str | None = None) -> DevicePrincipal | None:
        token = str(bearer_token or "")
        match = _DEVICE_TOKEN_RE.fullmatch(token)
        if match is None:
            return None
        device_id = match.group(1)
        with self._locked():
            key = self._read_key()
            state = self._load_state(key, repair_tail=False)
            device = state["devices"].get(device_id)
            candidate_mac = _keyed_mac(key, "device-token", token)
            expected_mac = device["bearer_mac"] if device else _ZERO_MAC
            valid = hmac.compare_digest(candidate_mac, expected_mac)
            if not valid or device is None or device["revoked_at"]:
                return None
            principal = DevicePrincipal(
                device_id=device_id, label=device["label"], platform=device["platform"],
                scopes=tuple(device["scopes"]), issued_at=device["issued_at"],
            )
        if required_scope is not None and not principal.allows(required_scope):
            return None
        return principal

    def active_device_principal(self, device_id: str, *,
                                required_scope: str | None = None) -> DevicePrincipal | None:
        """Return current active state for an already-authenticated device id.

        This is an internal revalidation primitive, not request authentication:
        callers must first authenticate a bearer or a server-issued capability.
        It lets short-lived capabilities fail closed immediately after the owner
        revokes their originating device without retaining the bearer secret.
        """
        clean_id = str(device_id or "")
        if _DEVICE_ID_RE.fullmatch(clean_id) is None:
            return None
        with self._locked():
            key = self._read_key()
            state = self._load_state(key, repair_tail=False)
            device = state["devices"].get(clean_id)
            if device is None or device["revoked_at"]:
                return None
            principal = DevicePrincipal(
                device_id=clean_id, label=device["label"], platform=device["platform"],
                scopes=tuple(device["scopes"]), issued_at=device["issued_at"],
            )
        if required_scope is not None and not principal.allows(required_scope):
            return None
        return principal

    def list_devices(self, actor: OwnerPrincipal, *, include_revoked: bool = True) -> list[dict]:
        self._require_owner(actor)
        with self._locked():
            key = self._read_key()
            state = self._load_state(key, repair_tail=False)
        result = []
        for device_id, device in state["devices"].items():
            if device["revoked_at"] and not include_revoked:
                continue
            result.append({
                "device_id": device_id,
                "label": device["label"],
                "platform": device["platform"],
                "scopes": list(device["scopes"]),
                "issued_at": device["issued_at"],
                "revoked_at": device["revoked_at"] or None,
                "status": "revoked" if device["revoked_at"] else "active",
            })
        return sorted(result, key=lambda row: (row["issued_at"], row["device_id"]))

    def revoke_device(self, actor: OwnerPrincipal, device_id: str, *,
                      reason: str = "owner revoke") -> bool:
        self._require_owner(actor)
        device_id = str(device_id or "")
        if _DEVICE_ID_RE.fullmatch(device_id) is None:
            return False
        clean_reason = _clean_text(reason, field="revoke reason", limit=200)
        with self._locked():
            now = self._now()
            key = self._read_key()
            state = self._load_state(key, repair_tail=True)
            device = state["devices"].get(device_id)
            if device is None or device["revoked_at"]:
                return False
            self._append(key, state, "device_revoked", {
                "device_id": device_id, "owner_id": actor.owner_id, "reason": clean_reason,
            }, now=now)
        return True


__all__ = [
    "ALLOWED_DEVICE_SCOPES", "DEFAULT_SCOPES", "DeviceAuthCorruption",
    "DeviceAuthError", "DeviceAuthPermissionError", "DeviceAuthStore",
    "DeviceCredential", "DevicePrincipal", "EnrollmentConsumed",
    "EnrollmentCredential", "EnrollmentExpired", "InvalidEnrollment",
    "OwnerPrincipal", "build_enrollment_url",
]
