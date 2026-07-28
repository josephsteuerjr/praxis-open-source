"""Durable intent/accept/apply ledger for Telegram account membership changes.

Joining or leaving belongs to the human owner or Praxis herself.  The append-only JSONL is
canonical and is fsynced before any state-changing MTProto request is issued.
Local room projections are applied only from a recorded Telegram acceptance and
can therefore be rebuilt idempotently after a crash.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import uuid
from pathlib import Path


SCHEMA = "praxis.telegram.membership.v1"
_KINDS = frozenset({"intent", "prepared", "accepted", "in_doubt", "applied", "failed"})
_TERMINAL = frozenset({"applied", "failed"})
_TX_RE = re.compile(r"membership-[0-9a-f]{32}")
_EVENT_RE = re.compile(r"membership-event-[0-9a-f]{32}")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _principal(value: object) -> str:
    raw = str(value or "").strip()
    if raw == "praxis:self":
        return raw
    if raw.startswith("telegram:"):
        raw = raw.split(":", 1)[1]
    if not re.fullmatch(r"[1-9][0-9]*", raw):
        raise ValueError("membership principal must be praxis:self or a positive Telegram user id")
    return raw


class MembershipLedger:
    """Small append-only state machine; one live Telethon process is the writer."""

    def __init__(self, path: str | os.PathLike[str] | None = None):
        if path is None:
            base = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
            path = base / "memory" / ".state" / "telegram_membership.jsonl"
        self.path = Path(path)
        self._lock = threading.RLock()

    @staticmethod
    def _decode(line: str) -> dict | None:
        def unique(pairs):
            out = {}
            for key, value in pairs:
                if key in out:
                    raise ValueError(f"duplicate key: {key}")
                out[key] = value
            return out

        try:
            row = json.loads(line, object_pairs_hook=unique)
        except (TypeError, ValueError):
            return None
        if not isinstance(row, dict) or row.get("schema") != SCHEMA:
            return None
        if set(row) != {"schema", "event_id", "tx_id", "at", "kind", "action",
                       "target", "principal_id", "data"}:
            return None
        if not isinstance(row.get("event_id"), str) or not _EVENT_RE.fullmatch(row["event_id"]):
            return None
        if not isinstance(row.get("tx_id"), str) or not _TX_RE.fullmatch(row["tx_id"]):
            return None
        if row.get("kind") not in _KINDS or row.get("action") not in {"join", "leave"}:
            return None
        if not isinstance(row.get("target"), str) or not row["target"].strip():
            return None
        try:
            if _principal(row.get("principal_id")) != row.get("principal_id"):
                return None
        except ValueError:
            return None
        if not isinstance(row.get("at"), str) or not row["at"].endswith("Z"):
            return None
        try:
            parsed = dt.datetime.fromisoformat(row["at"][:-1] + "+00:00")
        except ValueError:
            return None
        if parsed.tzinfo is None or not isinstance(row.get("data"), dict):
            return None
        return row

    def _events_unlocked(self) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        rows, seen = [], set()
        for line in lines:
            row = self._decode(line)
            if row is None or row["event_id"] in seen:
                continue
            seen.add(row["event_id"])
            rows.append(row)
        return rows

    @staticmethod
    def _transition_allowed(state: dict, kind: str) -> bool:
        current = str(state.get("status") or "")
        if kind == "intent" or current in _TERMINAL:
            return False
        if current in {"intent", "prepared", "in_doubt"}:
            return kind in {"prepared", "accepted", "in_doubt", "failed"}
        if current == "accepted":
            pending_approval = (state.get("result") or {}).get("status") == "request_sent"
            if pending_approval:
                return kind in {"prepared", "accepted", "failed"}
            return kind in {"applied", "failed"}
        return False

    def _states_unlocked(self) -> dict[str, dict]:
        states: dict[str, dict] = {}
        for row in self._events_unlocked():
            tx_id = row["tx_id"]
            state = states.get(tx_id)
            if state is None:
                if row["kind"] != "intent":
                    continue
                state = {
                    "id": tx_id,
                    "action": row["action"],
                    "target": row["target"],
                    "principal_id": row["principal_id"],
                    "status": "intent",
                    "intent_at": row["at"],
                    "updated_at": row["at"],
                    "prepared": {},
                    "result": {},
                    "error": "",
                }
                states[tx_id] = state
                continue
            if (row["action"], row["target"], row["principal_id"]) != (
                    state["action"], state["target"], state["principal_id"]):
                continue
            if state["status"] in _TERMINAL:
                continue
            kind = row["kind"]
            if not self._transition_allowed(state, kind):
                continue
            data = row["data"]
            if kind == "prepared":
                state["prepared"] = dict(data)
            elif kind == "accepted":
                state["result"] = dict(data)
                state["error"] = ""
            elif kind in {"in_doubt", "failed"}:
                state["error"] = str(data.get("error") or "")[:1000]
            state["status"] = kind
            state["updated_at"] = row["at"]
        return states

    @staticmethod
    def _write_all(fd: int, raw: bytes) -> None:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short membership ledger write")
            view = view[written:]

    def _append_unlocked(self, *, tx_id: str, kind: str, action: str, target: str,
                         principal_id: str, data: dict | None = None) -> dict:
        row = {
            "schema": SCHEMA,
            "event_id": f"membership-event-{uuid.uuid4().hex}",
            "tx_id": tx_id,
            "at": _now(),
            "kind": kind,
            "action": action,
            "target": target,
            "principal_id": principal_id,
            "data": dict(data or {}),
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
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(str(self.path.parent), directory_flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return row

    def begin(self, action: str, target: str, principal_id: str | int) -> dict:
        action = str(action or "").strip().casefold()
        target = str(target or "").strip()
        principal_id = _principal(principal_id)
        if action not in {"join", "leave"}:
            raise ValueError("membership action must be join or leave")
        if not target:
            raise ValueError("membership target must not be empty")
        with self._lock:
            states = self._states_unlocked()
            for state in states.values():
                if (state["status"] not in _TERMINAL
                        and state["action"] == action
                        and state["target"] == target
                        and state["principal_id"] == principal_id):
                    return dict(state)
            tx_id = f"membership-{uuid.uuid4().hex}"
            self._append_unlocked(
                tx_id=tx_id, kind="intent", action=action, target=target,
                principal_id=principal_id,
            )
            return dict(self._states_unlocked()[tx_id])

    def _record(self, tx_id: str, kind: str, data: dict | None = None) -> dict:
        if kind not in _KINDS - {"intent"}:
            raise ValueError(f"invalid membership transition {kind}")
        with self._lock:
            state = self._states_unlocked().get(str(tx_id))
            if state is None:
                raise KeyError(tx_id)
            if state["status"] in _TERMINAL:
                return dict(state)
            if not self._transition_allowed(state, kind):
                raise ValueError(
                    f"invalid membership transition {state['status']} -> {kind}"
                )
            self._append_unlocked(
                tx_id=state["id"], kind=kind, action=state["action"],
                target=state["target"], principal_id=state["principal_id"], data=data,
            )
            return dict(self._states_unlocked()[state["id"]])

    def prepared(self, tx_id: str, data: dict) -> dict:
        return self._record(tx_id, "prepared", data)

    def accepted(self, tx_id: str, result: dict) -> dict:
        return self._record(tx_id, "accepted", result)

    def in_doubt(self, tx_id: str, error: object) -> dict:
        return self._record(tx_id, "in_doubt", {"error": str(error)[:1000]})

    def applied(self, tx_id: str) -> dict:
        return self._record(tx_id, "applied")

    def failed(self, tx_id: str, error: object) -> dict:
        return self._record(tx_id, "failed", {"error": str(error)[:1000]})

    def get(self, tx_id: str) -> dict | None:
        with self._lock:
            state = self._states_unlocked().get(str(tx_id))
            return dict(state) if state is not None else None

    def pending(self) -> list[dict]:
        with self._lock:
            states = [dict(state) for state in self._states_unlocked().values()
                      if state["status"] not in _TERMINAL]
        return sorted(states, key=lambda state: (state["intent_at"], state["id"]))
