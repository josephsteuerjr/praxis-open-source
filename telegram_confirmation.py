"""One-use owner confirmations for account-critical Telethon requests.

The registry already binds a critical request to its exact constructor, validated
process-local parameter commitment and principal.  This module supplies the missing durable
issue/consume primitive without knowing anything about prompts or Telegram updates.
The runner calls :meth:`ConfirmationStore.issue` only after a separate durable
owner-message challenge has been claimed; model text or tool arguments alone are
not such an interaction.

Only a process-ephemeral keyed commitment of the opaque token is persisted.
Consumption is fsynced before success is returned, so a crash can burn a proof
but can never reuse it.  A process restart deliberately invalidates an issued
but unconsumed proof; the encrypted challenge envelope remains the recovery path.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from cryptography.fernet import Fernet, InvalidToken

from telegram_registry import ConfirmationBinding, CriticalConfirmation, to_jsonable


SCHEMA = "praxis.telegram.confirmation.v2"
_KINDS = frozenset({"issued", "consumed", "cancelled"})
_EVENT_RE = re.compile(r"confirmation-event-[0-9a-f]{32}")
_PROOF_RE = re.compile(r"confirmation-[0-9a-f]{32}")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_LOCK = threading.RLock()
# Never persist this key.  A plain token/parameter SHA would be an offline
# verifier; a keyed commitment remains useful only to this live process.
_PROOF_COMMITMENT_KEY = os.urandom(32)

CHALLENGE_SCHEMA = "praxis.telegram.critical-challenge.v3"
_CHALLENGE_EVENT_RE = re.compile(r"critical-event-[0-9a-f]{32}")
_CHALLENGE_RE = re.compile(r"tgcritical_[0-9a-f]{16}")
_SECRET_REF_RE = re.compile(r"tgcritical_[0-9a-f]{16}\.sealed")
_CHALLENGE_KINDS = frozenset({"prepared", "claimed", "completed", "failed", "cancelled"})
_SAFE_EXCEPTION_TYPE_RE = re.compile(
    r"(?:[A-Za-z_][A-Za-z0-9_]*\.)*[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Failure)"
)
_SAFE_POLICY_TYPES = frozenset({
    "ConfirmationRequired", "ConfirmationRejected", "PermissionDenied",
})
_DEFAULT_EXCEPTION_TYPE = "CriticalDispatchError"


def _is_safe_exception_type(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) <= 120
        and (_SAFE_EXCEPTION_TYPE_RE.fullmatch(value) or value in _SAFE_POLICY_TYPES)
    )


def _safe_exception_type(error: object) -> str:
    """Extract only an explicit ``Type: detail`` prefix from an error string.

    A colonless value is detail, not trustworthy type metadata.  This prevents a
    secret-only exception message from becoming the durable ``type`` field.
    """
    prefix, separator, _detail = str(error or "").partition(":")
    candidate = prefix.strip()
    if separator and _is_safe_exception_type(candidate):
        return candidate
    return _DEFAULT_EXCEPTION_TYPE


def _utc_iso(now: float) -> str:
    return (dt.datetime.fromtimestamp(now, dt.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def _owner(value: str | int) -> str:
    result = str(value).strip()
    if not result:
        raise ValueError("confirmation owner_id is empty")
    return result


def _commitment(value: str, *, label: str) -> str:
    result = str(value or "").strip().lower()
    if not _SHA_RE.fullmatch(result):
        raise ValueError(f"{label} must be an opaque keyed commitment")
    return result


def _token_commitment(token: str) -> str:
    return hmac.new(
        _PROOF_COMMITMENT_KEY, str(token).encode("utf-8"), hashlib.sha256,
    ).hexdigest()


class ConfirmationStore:
    """Append-only one-use proof store; one live Telegram runner is the writer."""

    def __init__(
        self,
        *,
        owner_id: str | int,
        path: str | os.PathLike[str] | None = None,
        ttl_seconds: int = 300,
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[], str] | None = None,
        confirmable_principals: Iterable[str | int] = (),
    ) -> None:
        if path is None:
            base = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
            path = base / "memory" / ".state" / "telegram_confirmations.jsonl"
        self.path = Path(path)
        self.owner_id = _owner(owner_id)
        self.confirmable_principals = frozenset(
            {self.owner_id, *(
                str(value).strip() for value in confirmable_principals
                if str(value).strip()
            )}
        )
        self.ttl_seconds = max(1, min(900, int(ttl_seconds)))
        self.clock = clock
        self.token_factory = token_factory or (lambda: secrets.token_urlsafe(32))

    @staticmethod
    def _decode(line: str) -> dict | None:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate key: {key}")
                result[key] = value
            return result

        try:
            row = json.loads(line, object_pairs_hook=unique)
        except (TypeError, ValueError):
            return None
        required = {
            "schema", "event_id", "proof_id", "kind", "at", "at_epoch",
            "owner_id", "request_name", "parameter_commitment", "principal",
            "scope", "token_commitment", "expires_at",
        }
        if not isinstance(row, dict) or set(row) != required or row.get("schema") != SCHEMA:
            return None
        if not isinstance(row.get("event_id"), str) or not _EVENT_RE.fullmatch(row["event_id"]):
            return None
        if not isinstance(row.get("proof_id"), str) or not _PROOF_RE.fullmatch(row["proof_id"]):
            return None
        if row.get("kind") not in _KINDS:
            return None
        if not all(isinstance(row.get(key), str) and row[key]
                   for key in ("at", "owner_id", "request_name", "principal", "scope")):
            return None
        try:
            at = float(row.get("at_epoch"))
            expires = float(row.get("expires_at"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(at) or not math.isfinite(expires) or expires < at:
            return None
        try:
            _commitment(
                row.get("parameter_commitment"), label="parameter_commitment"
            )
            _commitment(row.get("token_commitment"), label="token_commitment")
            parsed = dt.datetime.fromisoformat(row["at"].replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            return None
        return row

    def _events_unlocked(self) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        result, seen = [], set()
        for line in lines:
            row = self._decode(line)
            if row is None or row["event_id"] in seen:
                continue
            seen.add(row["event_id"])
            result.append(row)
        return result

    def _states_unlocked(self) -> dict[str, dict]:
        states: dict[str, dict] = {}
        for row in self._events_unlocked():
            proof_id = row["proof_id"]
            state = states.get(proof_id)
            if state is None:
                if row["kind"] != "issued":
                    continue
                states[proof_id] = {**row, "status": "issued"}
                continue
            identity = (
                "owner_id", "request_name", "parameter_commitment", "principal",
                "scope", "token_commitment", "expires_at",
            )
            if any(row[key] != state[key] for key in identity):
                continue
            if state["status"] != "issued" or row["kind"] == "issued":
                continue
            state["status"] = row["kind"]
            state["terminal_at"] = row["at"]
        return states

    @staticmethod
    def _write_all(fd: int, raw: bytes) -> None:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short Telegram confirmation ledger write")
            view = view[written:]

    def _append_unlocked(self, *, kind: str, state: dict, now: float) -> None:
        row = {
            "schema": SCHEMA,
            "event_id": "confirmation-event-" + uuid.uuid4().hex,
            "proof_id": state["proof_id"],
            "kind": kind,
            "at": _utc_iso(now),
            "at_epoch": now,
            "owner_id": state["owner_id"],
            "request_name": state["request_name"],
            "parameter_commitment": state["parameter_commitment"],
            "principal": state["principal"],
            "scope": state["scope"],
            "token_commitment": state["token_commitment"],
            "expires_at": state["expires_at"],
        }
        raw = (json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        created = not self.path.exists()
        flags = os.O_APPEND | os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(str(self.path), flags, 0o600)
        try:
            size = os.fstat(fd).st_size
            if size:
                os.lseek(fd, -1, os.SEEK_END)
                if os.read(fd, 1) != b"\n":
                    self._write_all(fd, b"\n")
            self._write_all(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        if created and os.name != "nt":
            directory_fd = os.open(
                str(self.path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)

    @staticmethod
    def _binding_fields(binding: ConfirmationBinding) -> tuple[str, str, str, str]:
        request_name = str(binding.request_name or "").strip()
        principal = _owner(binding.principal)
        scope = str(binding.scope or "").strip()
        if not request_name or not scope:
            raise ValueError("confirmation binding is incomplete")
        parameter_commitment = _commitment(
            binding.parameter_commitment, label="parameter_commitment"
        )
        return request_name, parameter_commitment, principal, scope

    def issue(
        self, binding: ConfirmationBinding, *, principal: str | int
    ) -> CriticalConfirmation:
        """Issue a short-lived proof after an external fresh-owner confirmation."""

        actor = _owner(principal)
        request_name, parameter_commitment, bound_principal, scope = self._binding_fields(binding)
        if actor != self.owner_id:
            raise PermissionError("only the owner may issue a Telegram critical confirmation")
        if bound_principal not in self.confirmable_principals:
            raise PermissionError("critical confirmation principal is not sovereign")
        now = float(self.clock())
        if not math.isfinite(now):
            raise ValueError("confirmation clock is not finite")
        with _LOCK:
            existing_commitments = {
                row["token_commitment"] for row in self._states_unlocked().values()
            }
            token = ""
            token_commitment = ""
            for _ in range(8):
                token = str(self.token_factory() or "")
                if len(token) < 32:
                    raise ValueError("confirmation token must contain at least 32 characters")
                token_commitment = _token_commitment(token)
                if token_commitment not in existing_commitments:
                    break
            else:
                raise RuntimeError("could not allocate a unique confirmation token")
            state = {
                "proof_id": "confirmation-" + uuid.uuid4().hex,
                "owner_id": self.owner_id,
                "request_name": request_name,
                "parameter_commitment": parameter_commitment,
                "principal": bound_principal,
                "scope": scope,
                "token_commitment": token_commitment,
                "expires_at": now + self.ttl_seconds,
            }
            self._append_unlocked(kind="issued", state=state, now=now)
        return CriticalConfirmation(token=token)

    def verify_and_consume(
        self, confirmation: CriticalConfirmation, binding: ConfirmationBinding
    ) -> bool:
        """Atomically consume one exact, unexpired proof for dispatcher injection."""

        try:
            request_name, parameter_commitment, principal, scope = self._binding_fields(binding)
            if principal not in self.confirmable_principals:
                return False
            if not confirmation.token:
                return False
            token_commitment = _token_commitment(confirmation.token)
            now = float(self.clock())
            if not math.isfinite(now):
                return False
        except (AttributeError, TypeError, ValueError):
            return False
        with _LOCK:
            match = None
            for state in self._states_unlocked().values():
                if not hmac.compare_digest(
                    state["token_commitment"], token_commitment
                ):
                    continue
                match = state
                break
            if match is None or match.get("status") != "issued":
                return False
            expected = (
                request_name, parameter_commitment, principal, scope, self.owner_id,
            )
            actual = (
                match["request_name"], match["parameter_commitment"], match["principal"],
                match["scope"], match["owner_id"],
            )
            if actual != expected or now >= float(match["expires_at"]):
                return False
            self._append_unlocked(kind="consumed", state=match, now=now)
            return True

    def cancel(self, confirmation: CriticalConfirmation, *, principal: str | int) -> bool:
        actor = _owner(principal)
        if actor != self.owner_id:
            raise PermissionError("Telegram critical confirmation is owner-only")
        token_commitment = _token_commitment(confirmation.token)
        now = float(self.clock())
        with _LOCK:
            for state in self._states_unlocked().values():
                if (state.get("status") == "issued"
                        and hmac.compare_digest(
                            state["token_commitment"], token_commitment
                        )):
                    self._append_unlocked(kind="cancelled", state=state, now=now)
                    return True
        return False

    def pending(self) -> list[dict]:
        """Return non-secret metadata for diagnostics; expired proofs are not pending."""

        now = float(self.clock())
        with _LOCK:
            states = self._states_unlocked().values()
            rows = [
                {
                    "proof_id": state["proof_id"],
                    "request_name": state["request_name"],
                    "principal": state["principal"],
                    "scope": state["scope"],
                    "expires_at": state["expires_at"],
                }
                for state in states
                if state.get("status") == "issued" and now < float(state["expires_at"])
            ]
        return sorted(rows, key=lambda row: (row["expires_at"], row["proof_id"]))


@dataclass(frozen=True, slots=True)
class OwnerOrigin:
    """Immutable evidence captured from a durable run, never from model arguments."""

    run_id: str
    chat_id: str
    message_id: int
    principal_id: str
    is_dm: bool
    raw_text: str

    def __post_init__(self) -> None:
        if not self.run_id or any(char in self.run_id for char in ("/", "\\", "\0")):
            raise ValueError("owner origin run_id is invalid")
        if not self.chat_id:
            raise ValueError("owner origin chat_id is empty")
        if isinstance(self.message_id, bool) or int(self.message_id) <= 0:
            raise ValueError("owner origin message_id must be positive")
        if not self.principal_id:
            raise ValueError("owner origin principal_id is empty")
        if type(self.is_dm) is not bool:
            raise TypeError("owner origin is_dm must be boolean")
        if not isinstance(self.raw_text, str):
            raise TypeError("owner origin raw_text must be text")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "OwnerOrigin":
        if not isinstance(value, Mapping):
            raise TypeError("owner origin must be an object")
        message_id = value.get("message_id")
        if isinstance(message_id, bool):
            raise TypeError("owner origin message_id must be an integer")
        return cls(
            run_id=str(value.get("run_id") or ""),
            chat_id=str(value.get("chat_id") or ""),
            message_id=int(message_id),
            principal_id=str(value.get("principal_id") or ""),
            is_dm=value.get("is_dm"),
            raw_text=value.get("raw_text"),
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "principal_id": self.principal_id,
            "is_dm": self.is_dm,
        }


@dataclass(frozen=True, slots=True)
class CriticalIntentOrigin:
    """Immutable durable tool intent which asks the owner for a critical confirmation.

    Human-owner intents retain their exact private Telegram message evidence.  Praxis-self
    intents have no invented message: their durable run/call identity is canonical instead.
    """

    run_id: str
    call_id: str
    principal_id: str
    confirmation_owner_id: str
    chat_id: str = ""
    message_id: int = 0
    is_dm: bool = False
    raw_text: str = ""

    def __post_init__(self) -> None:
        for label, value in (("run_id", self.run_id), ("call_id", self.call_id)):
            if not value or any(char in value for char in ("/", "\\", "\0")):
                raise ValueError(f"critical intent {label} is invalid")
        if not self.principal_id:
            raise ValueError("critical intent principal_id is empty")
        if not self.confirmation_owner_id:
            raise ValueError("critical intent confirmation_owner_id is empty")
        if isinstance(self.message_id, bool) or int(self.message_id) < 0:
            raise ValueError("critical intent message_id must be non-negative")
        if type(self.is_dm) is not bool:
            raise TypeError("critical intent is_dm must be boolean")
        if not isinstance(self.raw_text, str):
            raise TypeError("critical intent raw_text must be text")

    @classmethod
    def from_owner(cls, origin: OwnerOrigin, *, call_id: str) -> "CriticalIntentOrigin":
        return cls(
            run_id=origin.run_id,
            call_id=str(call_id or ""),
            principal_id=origin.principal_id,
            confirmation_owner_id=origin.principal_id,
            chat_id=origin.chat_id,
            message_id=origin.message_id,
            is_dm=origin.is_dm,
            raw_text=origin.raw_text,
        )

    @classmethod
    def background(
        cls, *, run_id: str, call_id: str, principal_id: str,
        confirmation_owner_id: str,
    ) -> "CriticalIntentOrigin":
        return cls(
            run_id=str(run_id or ""), call_id=str(call_id or ""),
            principal_id=str(principal_id or ""),
            confirmation_owner_id=str(confirmation_owner_id or ""),
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "call_id": self.call_id,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "principal_id": self.principal_id,
            "confirmation_owner_id": self.confirmation_owner_id,
            "tool": "telegram_account",
            "is_dm": self.is_dm,
        }


class CriticalChallengeError(RuntimeError):
    pass


class CriticalChallengeStore:
    """Durable two-run owner challenge for account-critical requests.

    A pending challenge may be claimed exactly once.  Once claimed, an interrupted
    operation is deliberately ``in_doubt`` and cannot be auto-retried: Telegram's
    account mutations do not offer a universal exactly-once key.
    """

    def __init__(
        self,
        *,
        owner_id: str | int,
        path: str | os.PathLike[str] | None = None,
        ttl_seconds: int = 300,
        clock: Callable[[], float] = time.time,
        initiator_principals: Iterable[str | int] = (),
        secret_dir: str | os.PathLike[str] | None = None,
        key_path: str | os.PathLike[str] | None = None,
    ) -> None:
        if path is None:
            base = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
            path = base / "memory" / ".state" / "telegram_critical_challenges.jsonl"
        self.path = Path(path)
        self.secret_dir = Path(secret_dir) if secret_dir is not None else (
            self.path.parent / "telegram_critical_secrets"
        )
        self.key_path = Path(key_path) if key_path is not None else (
            self.path.parent / "telegram_critical.key"
        )
        self.owner_id = _owner(owner_id)
        self.initiator_principals = frozenset(
            {self.owner_id, *(
                str(value).strip() for value in initiator_principals
                if str(value).strip()
            )}
        )
        self.ttl_seconds = max(30, min(900, int(ttl_seconds)))
        self.clock = clock

    @staticmethod
    def _decode(line: str) -> dict | None:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate key: {key}")
                result[key] = value
            return result

        try:
            row = json.loads(line, object_pairs_hook=unique)
        except (TypeError, ValueError):
            return None
        if (not isinstance(row, dict)
                or set(row) != {"schema", "event_id", "challenge_id", "kind",
                                    "at", "at_epoch", "data"}
                or row.get("schema") != CHALLENGE_SCHEMA
                or not isinstance(row.get("event_id"), str)
                or not _CHALLENGE_EVENT_RE.fullmatch(row["event_id"])
                or not isinstance(row.get("challenge_id"), str)
                or not _CHALLENGE_RE.fullmatch(row["challenge_id"])
                or row.get("kind") not in _CHALLENGE_KINDS
                or not isinstance(row.get("at"), str)
                or not isinstance(row.get("data"), dict)):
            return None
        try:
            epoch = float(row.get("at_epoch"))
            parsed = dt.datetime.fromisoformat(row["at"].replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(epoch) or parsed.tzinfo is None:
            return None
        return row

    def _events_unlocked(self) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        result, seen = [], set()
        for line in lines:
            row = self._decode(line)
            if row is None or row["event_id"] in seen:
                continue
            seen.add(row["event_id"])
            result.append(row)
        return result

    def _valid_public_origin(self, value: object) -> bool:
        if not isinstance(value, dict) or set(value) != {
            "run_id", "chat_id", "message_id", "principal_id", "is_dm",
        }:
            return False
        message_id = value.get("message_id")
        return bool(
            isinstance(value.get("run_id"), str)
            and value["run_id"]
            and not any(char in value["run_id"] for char in ("/", "\\", "\0"))
            and isinstance(value.get("chat_id"), str)
            and value["chat_id"]
            and type(message_id) is int
            and message_id > 0
            and value.get("principal_id") == self.owner_id
            and value.get("chat_id") == self.owner_id
            and value.get("is_dm") is True
        )

    def _valid_public_intent_origin(self, value: object) -> bool:
        if not isinstance(value, dict) or set(value) != {
            "run_id", "call_id", "chat_id", "message_id", "principal_id",
            "confirmation_owner_id", "tool", "is_dm",
        }:
            return False
        principal = value.get("principal_id")
        message_id = value.get("message_id")
        common = bool(
            isinstance(value.get("run_id"), str)
            and value["run_id"]
            and not any(char in value["run_id"] for char in ("/", "\\", "\0"))
            and isinstance(value.get("call_id"), str)
            and value["call_id"]
            and not any(char in value["call_id"] for char in ("/", "\\", "\0"))
            and principal in self.initiator_principals
            and value.get("confirmation_owner_id") == self.owner_id
            and value.get("tool") == "telegram_account"
            and isinstance(value.get("chat_id"), str)
            and type(message_id) is int
        )
        if not common:
            return False
        if principal == self.owner_id:
            return bool(
                value["chat_id"] == self.owner_id
                and message_id > 0
                and value.get("is_dm") is True
            )
        return bool(
            value["chat_id"] == ""
            and message_id == 0
            and value.get("is_dm") is False
        )

    def _valid_prepared(self, challenge_id: str, row: dict) -> bool:
        data = row["data"]
        required = {
            "idempotency_key", "request_name", "parameters_ref",
            "principal", "scope", "origin", "exact_phrase", "expires_at",
        }
        if set(data) != required:
            return False
        if not all(isinstance(data.get(key), str) and data[key]
                   for key in (
                       "idempotency_key", "request_name", "parameters_ref",
                       "principal", "scope",
                   )):
            return False
        if (not _SECRET_REF_RE.fullmatch(data["parameters_ref"])
                or data["parameters_ref"] != f"{challenge_id}.sealed"):
            return False
        origin = data.get("origin")
        if (data["principal"] not in self.initiator_principals
                or not self._valid_public_intent_origin(origin)
                or data["principal"] != origin.get("principal_id")):
            return False
        if data.get("exact_phrase") != f"ПОДТВЕРЖДАЮ TELEGRAM {challenge_id}":
            return False
        try:
            expires = float(data.get("expires_at"))
            prepared = float(row.get("at_epoch"))
        except (TypeError, ValueError):
            return False
        return bool(
            math.isfinite(expires) and expires > prepared
            and expires <= prepared + 900
        )

    def _states_unlocked(self) -> dict[str, dict]:
        states: dict[str, dict] = {}
        for row in self._events_unlocked():
            challenge_id = row["challenge_id"]
            state = states.get(challenge_id)
            if state is None:
                if row["kind"] != "prepared":
                    continue
                data = row["data"]
                if not self._valid_prepared(challenge_id, row):
                    continue
                state = {
                    "challenge_id": challenge_id,
                    "status": "pending",
                    "prepared_at": row["at"],
                    **data,
                }
                states[challenge_id] = state
                continue
            kind = row["kind"]
            status = state.get("status")
            data = row["data"]
            if (status == "pending" and kind == "claimed"
                    and set(data) == {"origin"}
                    and self._valid_public_origin(data.get("origin"))):
                state["status"] = "in_doubt"
                state["claim"] = dict(data)
                state["claimed_at"] = row["at"]
            elif (status == "pending" and kind == "cancelled"
                  and set(data) == {"origin"}
                  and self._valid_public_origin(data.get("origin"))):
                state["status"] = "cancelled"
                state["terminal"] = dict(data)
                state["terminal_at"] = row["at"]
            elif (status == "in_doubt" and kind == "completed"
                  and set(data) == {"receipt"}
                  and self._valid_safe_receipt(data.get("receipt"))):
                state["status"] = kind
                state["terminal"] = dict(data)
                state["terminal_at"] = row["at"]
            elif (status == "in_doubt" and kind == "failed"
                  and set(data) == {"error"} and isinstance(data.get("error"), dict)):
                error_data = data["error"]
                new_shape = set(error_data) == {"type"}
                legacy_shape = (
                    set(error_data) == {"type", "sha256"}
                    and _SHA_RE.fullmatch(str(error_data.get("sha256") or ""))
                )
                if new_shape or legacy_shape:
                    error_type = str(error_data.get("type") or "")
                    state["status"] = kind
                    # Normalize legacy rows in memory too: neither their digest nor
                    # an unsafe historical type is projected into current state.
                    state["terminal"] = {"error": {
                        "type": (error_type if _is_safe_exception_type(error_type)
                                 else _DEFAULT_EXCEPTION_TYPE),
                    }}
                    state["terminal_at"] = row["at"]
        return states

    @staticmethod
    def _write_all(fd: int, raw: bytes) -> None:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short critical challenge ledger write")
            view = view[written:]

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        directory_fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _fernet_unlocked(self) -> Fernet:
        try:
            key = self.key_path.read_bytes().strip()
            if os.name != "nt":
                os.chmod(self.key_path, 0o600)
        except FileNotFoundError:
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key()
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            try:
                fd = os.open(str(self.key_path), flags, 0o600)
            except FileExistsError:
                key = self.key_path.read_bytes().strip()
            else:
                try:
                    self._write_all(fd, key + b"\n")
                    os.fsync(fd)
                finally:
                    os.close(fd)
                self._fsync_directory(self.key_path.parent)
        try:
            return Fernet(key)
        except (TypeError, ValueError) as exc:
            raise CriticalChallengeError("critical parameter key is malformed") from exc

    def _secret_path(self, reference: str) -> Path:
        reference = str(reference or "")
        if not _SECRET_REF_RE.fullmatch(reference):
            raise CriticalChallengeError("critical parameter reference is malformed")
        return self.secret_dir / reference

    def _write_parameters_unlocked(self, challenge_id: str, encoded: bytes) -> str:
        reference = f"{challenge_id}.sealed"
        destination = self._secret_path(reference)
        self.secret_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.secret_dir, 0o700)
        sealed = self._fernet_unlocked().encrypt(encoded)
        temporary = self.secret_dir / f".{reference}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(str(temporary), flags, 0o600)
        try:
            self._write_all(fd, sealed)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(temporary, destination)
            self._fsync_directory(self.secret_dir)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        # Fernet authenticates the complete plaintext.  Verify the new envelope
        # directly; never persist a plain digest that could test guessed codes.
        try:
            restored = self._fernet_unlocked().decrypt(destination.read_bytes())
        except (OSError, InvalidToken) as exc:
            destination.unlink(missing_ok=True)
            raise CriticalChallengeError(
                "critical parameter envelope verification failed"
            ) from exc
        if not hmac.compare_digest(restored, encoded):
            destination.unlink(missing_ok=True)
            raise CriticalChallengeError("critical parameter envelope changed")
        return reference

    def _read_parameters_unlocked(self, reference: str) -> dict[str, Any]:
        try:
            sealed = self._secret_path(reference).read_bytes()
            encoded = self._fernet_unlocked().decrypt(sealed)
        except (OSError, InvalidToken) as exc:
            raise CriticalChallengeError("sealed critical parameters are unavailable") from exc
        try:
            parameters = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CriticalChallengeError("sealed critical parameters are malformed") from exc
        if not isinstance(parameters, dict):
            raise CriticalChallengeError("sealed critical parameters are not an object")
        return parameters

    def _delete_parameters_unlocked(self, reference: object) -> None:
        try:
            self._secret_path(str(reference or "")).unlink(missing_ok=True)
        except (OSError, CriticalChallengeError):
            pass

    def _prune_orphan_secrets_unlocked(self, now: float) -> None:
        try:
            candidates = tuple(self.secret_dir.glob("tgcritical_*.sealed"))
        except OSError:
            return
        for candidate in candidates:
            try:
                if candidate.stat().st_mtime + 900 < now:
                    candidate.unlink(missing_ok=True)
            except OSError:
                continue

    def _cleanup_secret_refs_unlocked(self, states: Mapping[str, dict], now: float) -> None:
        """Keep envelopes only for this owner's still-pending, unexpired challenges."""
        active = {
            str(state.get("parameters_ref") or "")
            for state in states.values()
            if (state.get("status") == "pending"
                and now < float(state.get("expires_at") or 0))
        }
        try:
            candidates = tuple(self.secret_dir.glob("tgcritical_*.sealed"))
        except OSError:
            return
        for candidate in candidates:
            if candidate.name in active:
                continue
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                continue

    def _safe_receipt_unlocked(
        self, state: Mapping[str, Any], receipt: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Validate a full dispatch receipt, then persist only non-secret evidence."""
        payload = to_jsonable(dict(receipt))
        if not isinstance(payload, dict) or payload.get("action") != "call":
            raise CriticalChallengeError("critical dispatch receipt has no call envelope")
        inner = payload.get("receipt")
        if not isinstance(inner, dict):
            raise CriticalChallengeError("critical dispatch receipt body is missing")
        claim = state.get("claim") if isinstance(state.get("claim"), dict) else {}
        claim_origin = claim.get("origin") if isinstance(claim.get("origin"), dict) else {}
        confirmed_by = str(claim_origin.get("principal_id") or "")
        delivery = (inner.get("delivery_context")
                    if isinstance(inner.get("delivery_context"), dict) else {})
        expected = {
            "request_name": str(state.get("request_name") or ""),
            "principal": str(state.get("principal") or ""),
        }
        actual = {key: str(inner.get(key) or "") for key in expected}
        if actual != expected:
            raise CriticalChallengeError("critical dispatch receipt does not match its intent")
        delivery_expected = {
            "challenge_id": str(state.get("challenge_id") or ""),
            "requested_by": str(state.get("principal") or ""),
            "confirmed_by": confirmed_by,
        }
        delivery_actual = {key: str(delivery.get(key) or "") for key in delivery_expected}
        if delivery_actual != delivery_expected or confirmed_by != self.owner_id:
            raise CriticalChallengeError("critical dispatch receipt provenance is incomplete")
        if any(inner.get(key) is not None for key in (
            "submitted_parameters", "serialized_parameters", "result", "result_sha256",
        )):
            raise CriticalChallengeError("critical dispatch receipt exposed secret material")
        try:
            _commitment(
                inner.get("parameter_commitment"), label="parameter_commitment"
            )
        except ValueError as exc:
            raise CriticalChallengeError(
                "critical dispatch receipt has no opaque parameter binding"
            ) from exc
        if inner.get("identifiers") not in ({}, None):
            raise CriticalChallengeError("critical dispatch receipt exposed result identifiers")
        receipt_error = inner.get("error")
        if receipt_error is not None and (
            not isinstance(receipt_error, dict)
            or set(receipt_error) != {"type"}
            or not _is_safe_exception_type(str(receipt_error.get("type") or ""))
        ):
            raise CriticalChallengeError("critical dispatch receipt exposed error detail")
        receipt_id = str(inner.get("receipt_id") or "")
        status = str(inner.get("status") or "")
        if not receipt_id or not status:
            raise CriticalChallengeError("critical dispatch receipt identity is incomplete")
        return {
            "receipt_id": receipt_id,
            "status": status,
            "request_name": expected["request_name"],
            "principal": expected["principal"],
            "requested_by": delivery_expected["requested_by"],
            "confirmed_by": confirmed_by,
            "challenge_id": delivery_expected["challenge_id"],
        }

    @staticmethod
    def _valid_safe_receipt(value: object) -> bool:
        required = {
            "receipt_id", "status", "request_name", "principal",
            "requested_by", "confirmed_by", "challenge_id",
        }
        if not isinstance(value, dict) or set(value) != required:
            return False
        if not all(isinstance(value.get(key), str) and value[key] for key in (
            "receipt_id", "status", "request_name", "principal",
            "requested_by", "confirmed_by", "challenge_id",
        )):
            return False
        return bool(
            _CHALLENGE_RE.fullmatch(value["challenge_id"])
        )

    def _append_unlocked(self, challenge_id: str, kind: str, data: dict) -> None:
        now = float(self.clock())
        if not math.isfinite(now):
            raise ValueError("challenge clock is not finite")
        row = {
            "schema": CHALLENGE_SCHEMA,
            "event_id": "critical-event-" + uuid.uuid4().hex,
            "challenge_id": challenge_id,
            "kind": kind,
            "at": _utc_iso(now),
            "at_epoch": now,
            "data": to_jsonable(data),
        }
        raw = (json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        created = not self.path.exists()
        flags = os.O_APPEND | os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(str(self.path), flags, 0o600)
        try:
            size = os.fstat(fd).st_size
            if size:
                os.lseek(fd, -1, os.SEEK_END)
                if os.read(fd, 1) != b"\n":
                    self._write_all(fd, b"\n")
            self._write_all(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        if created:
            self._fsync_directory(self.path.parent)

    def _validate_origin(self, origin: OwnerOrigin) -> None:
        if (origin.principal_id != self.owner_id or origin.chat_id != self.owner_id
                or not origin.is_dm):
            raise PermissionError(
                "Telegram critical confirmation requires the configured owner in a private chat"
            )

    def _validate_intent_origin(self, origin: CriticalIntentOrigin) -> None:
        public = origin.public_dict()
        if not self._valid_public_intent_origin(public):
            raise PermissionError("Telegram critical intent is not a sovereign durable origin")

    @staticmethod
    def _public(state: dict) -> dict:
        claim = state.get("claim") if isinstance(state.get("claim"), dict) else {}
        claim_origin = claim.get("origin") if isinstance(claim.get("origin"), dict) else {}
        return {
            "challenge_id": state["challenge_id"],
            "status": state["status"],
            "request_name": state["request_name"],
            "scope": state["scope"],
            "requested_by": state["principal"],
            **({"confirmed_by": str(claim_origin.get("principal_id") or "")}
               if claim_origin.get("principal_id") else {}),
            "origin": dict(state["origin"]),
            "exact_phrase": state["exact_phrase"],
            "expires_at": state["expires_at"],
            **({"claim": dict(state.get("claim") or {})} if state.get("claim") else {}),
            **({"terminal": dict(state.get("terminal") or {})} if state.get("terminal") else {}),
        }

    def prepare(
        self,
        binding: ConfirmationBinding,
        parameters: Mapping[str, Any] | None,
        *,
        origin: CriticalIntentOrigin,
        idempotency_key: str,
    ) -> dict:
        self._validate_intent_origin(origin)
        request_name, _parameter_commitment, principal, scope = (
            ConfirmationStore._binding_fields(binding)
        )
        if principal != origin.principal_id or principal not in self.initiator_principals:
            raise PermissionError("critical challenge binding does not match its sovereign initiator")
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("critical challenge requires a durable tool idempotency key")
        serial_parameters = to_jsonable(dict(parameters or {}))
        if not isinstance(serial_parameters, dict):
            raise TypeError("critical challenge parameters must be an object")
        encoded = json.dumps(
            serial_parameters, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > 256 * 1024:
            raise ValueError("critical challenge parameters exceed 256 KiB")
        now = float(self.clock())
        with _LOCK:
            self._prune_orphan_secrets_unlocked(now)
            states = self._states_unlocked()
            self._cleanup_secret_refs_unlocked(states, now)
            for state in states.values():
                if state.get("idempotency_key") == key:
                    expected = {
                        "request_name": request_name,
                        "principal": principal,
                        "scope": scope,
                        "origin": origin.public_dict(),
                    }
                    actual = {name: state.get(name) for name in expected}
                    if actual != expected:
                        raise CriticalChallengeError(
                            "critical challenge idempotency key was reused for a different intent"
                        )
                    public = self._public(state)
                    if (state.get("status") == "pending"
                            and now >= float(state.get("expires_at") or 0)):
                        self._delete_parameters_unlocked(state.get("parameters_ref"))
                        public["status"] = "expired"
                        return public
                    if state.get("status") == "pending":
                        try:
                            restored = self._read_parameters_unlocked(state["parameters_ref"])
                        except CriticalChallengeError as exc:
                            # Without the authenticated envelope there is no safe
                            # durable verifier for a guessed password/code.  Never
                            # reconstruct it merely because an idempotency key matches.
                            raise CriticalChallengeError(
                                "sealed critical parameters are unavailable; intent cannot be repaired"
                            ) from exc
                        if restored != serial_parameters:
                            raise CriticalChallengeError(
                                "critical challenge idempotency key was reused for a different intent"
                            )
                    return public
            challenge_id = "tgcritical_" + uuid.uuid4().hex[:16]
            exact_phrase = f"ПОДТВЕРЖДАЮ TELEGRAM {challenge_id}"
            parameters_ref = self._write_parameters_unlocked(
                challenge_id, encoded,
            )
            data = {
                "idempotency_key": key,
                "request_name": request_name,
                "parameters_ref": parameters_ref,
                "principal": principal,
                "scope": scope,
                "origin": origin.public_dict(),
                "exact_phrase": exact_phrase,
                "expires_at": now + self.ttl_seconds,
            }
            try:
                self._append_unlocked(challenge_id, "prepared", data)
            except Exception:
                self._delete_parameters_unlocked(parameters_ref)
                raise
            return self._public(self._states_unlocked()[challenge_id])

    def claim(self, challenge_id: str, *, origin: OwnerOrigin) -> dict:
        self._validate_origin(origin)
        challenge_id = str(challenge_id or "").strip()
        if not _CHALLENGE_RE.fullmatch(challenge_id):
            raise CriticalChallengeError("unknown critical challenge")
        now = float(self.clock())
        with _LOCK:
            state = self._states_unlocked().get(challenge_id)
            if state is None:
                raise CriticalChallengeError("unknown critical challenge")
            if state.get("status") != "pending":
                raise CriticalChallengeError(
                    f"critical challenge is {state.get('status')}; it cannot be replayed"
                )
            if now >= float(state.get("expires_at") or 0):
                raise CriticalChallengeError("critical challenge expired")
            prepared_origin = state["origin"]
            same_prepared_message = (
                prepared_origin.get("principal_id") == self.owner_id
                and origin.message_id == int(prepared_origin.get("message_id") or 0)
            )
            if (origin.run_id == str(prepared_origin.get("run_id") or "")
                    or same_prepared_message):
                raise CriticalChallengeError(
                    "confirmation must come from a new owner message in a new durable run"
                )
            if origin.raw_text != str(state.get("exact_phrase") or ""):
                raise CriticalChallengeError(
                    "raw owner message does not exactly match the challenge phrase"
                )
            parameters = self._read_parameters_unlocked(state["parameters_ref"])
            self._append_unlocked(
                challenge_id, "claimed", {"origin": origin.public_dict()},
            )
            claimed = self._states_unlocked()[challenge_id]
            # A claimed effect is never replayed automatically.  Parameters remain only
            # in this stack frame for the immediate dispatch; durable state has no verifier.
            self._delete_parameters_unlocked(state.get("parameters_ref"))
            return {
                **self._public(claimed),
                "parameters": parameters,
                "principal": claimed["principal"],
            }

    def finish(self, challenge_id: str, *, receipt: Mapping[str, Any] | None = None,
               error: str = "") -> dict:
        challenge_id = str(challenge_id or "").strip()
        with _LOCK:
            state = self._states_unlocked().get(challenge_id)
            if state is None:
                raise CriticalChallengeError("unknown critical challenge")
            if state.get("status") != "in_doubt":
                return self._public(state)
            kind = "failed" if error else "completed"
            if error:
                message = str(error)
                data = {"error": {"type": _safe_exception_type(message)}}
            else:
                if not isinstance(receipt, Mapping):
                    raise CriticalChallengeError("critical dispatch receipt is required")
                data = {"receipt": self._safe_receipt_unlocked(state, receipt)}
            self._append_unlocked(challenge_id, kind, data)
            finished = self._public(self._states_unlocked()[challenge_id])
            self._delete_parameters_unlocked(state.get("parameters_ref"))
            return finished

    def cancel(self, challenge_id: str, *, origin: OwnerOrigin) -> bool:
        self._validate_origin(origin)
        challenge_id = str(challenge_id or "").strip()
        with _LOCK:
            state = self._states_unlocked().get(challenge_id)
            if state is None or state.get("status") != "pending":
                return False
            self._append_unlocked(
                challenge_id, "cancelled", {"origin": origin.public_dict()},
            )
            self._delete_parameters_unlocked(state.get("parameters_ref"))
            return True

    def list(self) -> list[dict]:
        now = float(self.clock())
        with _LOCK:
            rows = []
            states = self._states_unlocked()
            self._cleanup_secret_refs_unlocked(states, now)
            for state in states.values():
                public = self._public(state)
                if public["status"] == "pending" and now >= float(public["expires_at"]):
                    public["status"] = "expired"
                    self._delete_parameters_unlocked(state.get("parameters_ref"))
                rows.append(public)
            self._prune_orphan_secrets_unlocked(now)
        return sorted(rows, key=lambda row: (row["expires_at"], row["challenge_id"]),
                      reverse=True)
