"""Durable owner inbox shared by Telegram and the Praxis app.

The append-only ledger is the source of truth for messages Praxis wants the
owner to see.  Telegram is one delivery transport; the mini-app projects the
same items.  Creating an inbox item never calls a model and never performs the
action described by the item.
"""

from __future__ import annotations

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
from pathlib import Path
from typing import Iterator


SCHEMA = "praxis.owner-delivery.v2"
HEAD_SCHEMA = "praxis.owner-delivery.head.v1"
DELIVERY_TYPES = frozenset({
    "attention",
    "followup_answer",
    "run_result",
    "file_ready",
    "system_alert",
    "social_suggestion",
})
STATES = frozenset({"queued", "delivered", "read", "acted", "superseded"})
URGENCIES = frozenset({"low", "normal", "high", "critical"})
OUTCOMES = frozenset({"info", "success", "partial", "blocked", "failure", "needs_input"})
_ACTIVE = frozenset({"queued", "delivered", "read"})
_EVENT_KEYS = frozenset({
    "schema", "seq", "prev_sha256", "event_id", "delivery_id", "at", "op", "data",
})
_EVENT_RE = re.compile(r"delivery-event-[0-9a-f]{32}")
_DELIVERY_RE = re.compile(r"delivery-[0-9a-f]{32}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_GENESIS = "0" * 64


class OwnerDeliveryCorruption(RuntimeError):
    """The committed owner-attention provenance stream is ambiguous."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _text(value: object, *, limit: int, required: bool = False) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ValueError("required delivery text is empty")
    if len(result) > limit:
        raise ValueError(f"delivery text exceeds {limit} characters")
    return result


def _small_object(value: object, *, name: str, limit: int = 8_000) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    try:
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )
        clone = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite JSON") from exc
    if len(raw.encode("utf-8")) > limit:
        raise ValueError(f"{name} is too large")
    return clone


def _json_object(line: str) -> dict | None:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(line, object_pairs_hook=unique)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _valid_event(value: object) -> dict | None:
    if not isinstance(value, dict) or frozenset(value) != _EVENT_KEYS:
        return None
    if value.get("schema") != SCHEMA:
        return None
    if (not isinstance(value.get("seq"), int) or isinstance(value.get("seq"), bool)
            or value["seq"] < 1):
        return None
    if (not isinstance(value.get("prev_sha256"), str)
            or _SHA256_RE.fullmatch(value["prev_sha256"]) is None):
        return None
    if not isinstance(value.get("event_id"), str) or not _EVENT_RE.fullmatch(
        value["event_id"]
    ):
        return None
    if not isinstance(value.get("delivery_id"), str) or not _DELIVERY_RE.fullmatch(
        value["delivery_id"]
    ):
        return None
    if (not _timestamp(value.get("at"))
            or value.get("op") not in {"create", "transition", "repair"}):
        return None
    data = value.get("data")
    if not isinstance(data, dict):
        return None
    if value["op"] == "create":
        expected = {
            "type", "title", "body", "urgency", "outcome", "thread_key", "correlation",
            "reason", "provenance", "expectation", "action", "result",
            "dedupe_key", "coalesce_key", "transports",
        }
        if set(data) != expected:
            return None
        if (data.get("type") not in DELIVERY_TYPES
                or data.get("urgency") not in URGENCIES
                or data.get("outcome") not in OUTCOMES):
            return None
        if not isinstance(data.get("title"), str) or not data["title"]:
            return None
        for key in ("body", "thread_key", "reason", "expectation", "result",
                    "dedupe_key", "coalesce_key"):
            if not isinstance(data.get(key), str):
                return None
        if not all(isinstance(data.get(key), dict)
                   for key in ("correlation", "provenance", "action")):
            return None
        transports = data.get("transports")
        if (not isinstance(transports, list) or not transports
                or transports != sorted(set(transports))
                or any(item not in {"pwa", "telegram"} for item in transports)):
            return None
    elif value["op"] == "transition":
        if set(data) != {"from", "to", "expected_revision", "detail"}:
            return None
        if data.get("from") not in STATES or data.get("to") not in STATES:
            return None
        if not isinstance(data.get("expected_revision"), int) or data["expected_revision"] < 1:
            return None
        if not isinstance(data.get("detail"), dict):
            return None
    else:
        if set(data) != {"torn_sha256", "torn_bytes"}:
            return None
        if (not isinstance(data.get("torn_sha256"), str)
                or _SHA256_RE.fullmatch(data["torn_sha256"]) is None):
            return None
        if not isinstance(data.get("torn_bytes"), int) or data["torn_bytes"] < 1:
            return None
    return value


class OwnerDeliveryLedger:
    """Append-only owner attention ledger with idempotency and coalescing."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        if path is None:
            base = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
            path = base / "memory" / ".state" / "owner_delivery" / "events.jsonl"
        self.path = Path(path)
        self.head_path = self.path.with_name(self.path.name + ".head")
        self.lock_path = self.path.parent / ".lock"
        self._thread_lock = threading.RLock()

    @staticmethod
    def _nofollow_flags() -> int:
        return getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)

    def _secure_directory(self) -> None:
        directory = self.path.parent
        try:
            info = os.lstat(directory)
        except FileNotFoundError:
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            info = os.lstat(directory)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OSError("owner-delivery directory must be a real directory")
        if os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | self._nofollow_flags()
        fd = os.open(str(directory), flags)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISDIR(opened.st_mode):
                raise OSError("owner-delivery directory changed while opening")
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)

    def _open_regular(self, path: Path, flags: int, *, mode: int = 0o600) -> int:
        """Open one private file without following or accepting aliases."""
        try:
            before = os.lstat(path)
        except FileNotFoundError:
            before = None
        if before is not None and (
                stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1):
            raise OSError(f"owner-delivery private path is not a single regular file: {path.name}")
        open_flags = flags | self._nofollow_flags()
        if hasattr(os, "O_BINARY"):
            open_flags |= os.O_BINARY
        fd = os.open(str(path), open_flags, mode)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise OSError(
                    f"owner-delivery private path is not a single regular file: {path.name}"
                )
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _fsync_directory(self) -> None:
        if os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | self._nofollow_flags()
        fd = os.open(str(self.path.parent), flags)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISDIR(opened.st_mode):
                raise OSError("owner-delivery directory changed while syncing")
            os.fsync(fd)
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def _lock(self, *, timeout: float = 30.0) -> Iterator[None]:
        """Serialize projectors and writers across threads and processes."""
        with self._thread_lock:
            self._secure_directory()
            fd = self._open_regular(self.lock_path, os.O_CREAT | os.O_RDWR)
            locked = False
            try:
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                    os.fsync(fd)
                os.lseek(fd, 0, os.SEEK_SET)
                deadline = time.monotonic() + max(0.1, timeout)
                if os.name == "nt":
                    import msvcrt

                    while True:
                        try:
                            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                            locked = True
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError("owner-delivery ledger lock timed out")
                            time.sleep(0.01)
                else:
                    import fcntl

                    while True:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            locked = True
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                raise TimeoutError("owner-delivery ledger lock timed out")
                            time.sleep(0.01)
                yield
            finally:
                if locked:
                    os.lseek(fd, 0, os.SEEK_SET)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    @staticmethod
    def _decode_raw(raw: bytes) -> dict | None:
        try:
            line = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None
        return _valid_event(_json_object(line))

    @staticmethod
    def _read_all(fd: int) -> bytes:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    def _read_ledger_unlocked(self) -> bytes:
        try:
            fd = self._open_regular(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return b""
        try:
            return self._read_all(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _head_payload(seq: int, digest: str) -> bytes:
        return (json.dumps({
            "schema": HEAD_SCHEMA, "seq": seq, "sha256": digest,
        }, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")

    def _read_head_unlocked(self) -> tuple[int, str] | None:
        try:
            fd = self._open_regular(self.head_path, os.O_RDONLY)
        except FileNotFoundError:
            return None
        try:
            opened = os.fstat(fd)
            if opened.st_size < 1 or opened.st_size > 4096:
                raise OwnerDeliveryCorruption("owner-delivery head anchor size is invalid")
            raw = self._read_all(fd)
        finally:
            os.close(fd)
        try:
            decoded = raw.decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise OwnerDeliveryCorruption("owner-delivery head anchor is invalid") from exc
        value = _json_object(decoded)
        if (not isinstance(value, dict)
                or set(value) != {"schema", "seq", "sha256"}
                or value.get("schema") != HEAD_SCHEMA
                or not isinstance(value.get("seq"), int)
                or isinstance(value.get("seq"), bool)
                or value["seq"] < 0
                or not isinstance(value.get("sha256"), str)
                or _SHA256_RE.fullmatch(value["sha256"]) is None
                or (value["seq"] == 0 and value["sha256"] != _GENESIS)):
            raise OwnerDeliveryCorruption("owner-delivery head anchor is invalid")
        return value["seq"], value["sha256"]

    def _write_head_unlocked(self, seq: int, digest: str) -> None:
        # Do not silently replace an unsafe alias even though os.replace itself
        # would not follow it; an unexpected head object is provenance evidence.
        try:
            existing = self._open_regular(self.head_path, os.O_RDONLY)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            os.close(existing)
        temporary = self.head_path.with_name(
            f".{self.head_path.name}.{uuid.uuid4().hex}.tmp"
        )
        fd = self._open_regular(
            temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        )
        try:
            self._write_all(fd, self._head_payload(seq, digest))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(temporary, self.head_path)
            self._fsync_directory()
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _reconcile_head_unlocked(self, digests: list[str]) -> None:
        end_seq = len(digests) - 1
        end_digest = digests[-1]
        head = self._read_head_unlocked()
        if head is None:
            # The only legitimate missing-head window is creation or a crash
            # after ledger fsync and before the atomic head replacement.
            self._write_head_unlocked(end_seq, end_digest)
            return
        head_seq, head_digest = head
        if head_seq > end_seq:
            raise OwnerDeliveryCorruption("owner-delivery ledger was truncated")
        if digests[head_seq] != head_digest:
            raise OwnerDeliveryCorruption("owner-delivery ledger differs from its head anchor")
        if head_seq < end_seq:
            # A fully verified chain extension is the recoverable crash window:
            # ledger fsync completed, but the durable head update did not.
            self._write_head_unlocked(end_seq, end_digest)

    def _scan_unlocked(self) -> tuple[list[dict], list[str], bytes, bytes | None, bool]:
        raw = self._read_ledger_unlocked()
        if not raw:
            digests = [_GENESIS]
            self._reconcile_head_unlocked(digests)
            return [], digests, raw, None, False
        parts = raw.split(b"\n")
        final_unterminated = not raw.endswith(b"\n")
        torn_tail: bytes | None = None
        needs_newline = False
        if not final_unterminated:
            parts.pop()  # delimiter after the final committed row
        elif parts:
            tail = parts.pop()
            parsed_tail = self._decode_raw(tail)
            # A fully parseable final record is recoverable even when only its
            # newline was torn.  An unparseable final fragment stays uncommitted
            # until the next writer records an explicit repair marker.
            if parsed_tail is not None:
                parts.append(tail)
                needs_newline = True
            else:
                try:
                    tail_object = _json_object(tail.decode("utf-8", errors="strict"))
                except UnicodeDecodeError:
                    tail_object = None
                if ((isinstance(tail_object, dict) and tail_object.get("schema") != SCHEMA)
                        or b"praxis.owner-delivery.v1" in tail):
                    raise OwnerDeliveryCorruption(
                        "ambiguous legacy owner-delivery tail requires explicit migration"
                    )
                torn_tail = tail

        events: list[dict] = []
        digests: list[str] = [_GENESIS]
        seen: set[str] = set()

        def accept(event: dict, line: bytes) -> None:
            expected = len(events) + 1
            if (event["seq"] != expected
                    or event["prev_sha256"] != digests[-1]):
                raise OwnerDeliveryCorruption("owner-delivery event chain is broken")
            if event["event_id"] in seen:
                raise OwnerDeliveryCorruption("duplicate owner-delivery event id")
            seen.add(event["event_id"])
            events.append(event)
            digests.append(hashlib.sha256(line).hexdigest())

        index = 0
        while index < len(parts):
            line = parts[index]
            event = self._decode_raw(line)
            if event is None:
                if index + 1 >= len(parts):
                    raise OwnerDeliveryCorruption(
                        f"invalid committed owner-delivery row {index + 1}"
                    )
                repair = self._decode_raw(parts[index + 1])
                digest = hashlib.sha256(line).hexdigest()
                if (repair is None or repair.get("op") != "repair"
                        or repair["data"].get("torn_sha256") != digest
                        or repair["data"].get("torn_bytes") != len(line)):
                    raise OwnerDeliveryCorruption(
                        f"invalid committed owner-delivery row {index + 1}"
                    )
                accept(repair, parts[index + 1])
                index += 2
                continue
            if event.get("op") == "repair":
                raise OwnerDeliveryCorruption("orphan owner-delivery repair marker")
            accept(event, line)
            index += 1
        self._reconcile_head_unlocked(digests)
        return events, digests, raw, torn_tail, needs_newline

    def _events_unlocked(self) -> list[dict]:
        return self._scan_unlocked()[0]

    @staticmethod
    def _allowed(current: str, target: str) -> bool:
        return target in {
            "queued": {"delivered", "superseded"},
            "delivered": {"read", "superseded"},
            "read": {"acted", "superseded"},
            "acted": set(),
            "superseded": set(),
        }.get(current, set())

    def _states_unlocked(self) -> dict[str, dict]:
        states: dict[str, dict] = {}
        sequence = 0
        for event in self._events_unlocked():
            sequence += 1
            delivery_id = event["delivery_id"]
            data = event["data"]
            if event["op"] == "repair":
                continue
            if event["op"] == "create":
                if delivery_id in states:
                    raise OwnerDeliveryCorruption("duplicate owner-delivery create")
                states[delivery_id] = {
                    "id": delivery_id,
                    **json.loads(json.dumps(data, ensure_ascii=False)),
                    "status": "queued",
                    "revision": 1,
                    "created_at": event["at"],
                    "updated_at": event["at"],
                    "delivered_at": "",
                    "read_at": "",
                    "acted_at": "",
                    "superseded_at": "",
                    "delivery": {},
                    "last_detail": {},
                    "_sequence": sequence,
                }
                continue
            state = states.get(delivery_id)
            if state is None:
                raise OwnerDeliveryCorruption("owner-delivery transition precedes create")
            if (state["status"] != data["from"]
                    or state["revision"] != data["expected_revision"]
                    or not self._allowed(state["status"], data["to"])):
                raise OwnerDeliveryCorruption("invalid owner-delivery state transition")
            target = data["to"]
            state["status"] = target
            state["revision"] += 1
            state["updated_at"] = event["at"]
            state[f"{target}_at"] = event["at"]
            state["last_detail"] = dict(data["detail"])
            if target == "delivered":
                state["delivery"] = dict(data["detail"])
            state["_sequence"] = sequence
        return states

    @staticmethod
    def _write_all(fd: int, raw: bytes) -> None:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short owner-delivery ledger write")
            view = view[written:]

    @staticmethod
    def _event_raw(row: dict) -> bytes:
        return json.dumps(
            row, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    @staticmethod
    def _new_event(delivery_id: str, op: str, data: dict, *,
                   seq: int, prev_sha256: str) -> dict:
        return {
            "schema": SCHEMA,
            "seq": seq,
            "prev_sha256": prev_sha256,
            "event_id": f"delivery-event-{uuid.uuid4().hex}",
            "delivery_id": delivery_id,
            "at": _now(),
            "op": op,
            "data": data,
        }

    def _append_unlocked(self, delivery_id: str, op: str, data: dict) -> dict:
        events, digests, existing_raw, torn_tail, needs_newline = self._scan_unlocked()
        del events  # the chain length and end digest are carried by `digests`
        payload = bytearray()
        if existing_raw and (needs_newline or torn_tail is not None):
            payload.extend(b"\n")
        if torn_tail is not None:
            repair = self._new_event(
                delivery_id, "repair", {
                    "torn_sha256": hashlib.sha256(torn_tail).hexdigest(),
                    "torn_bytes": len(torn_tail),
                },
                seq=len(digests), prev_sha256=digests[-1],
            )
            repair_raw = self._event_raw(repair)
            payload.extend(repair_raw + b"\n")
            digests.append(hashlib.sha256(repair_raw).hexdigest())

        row = self._new_event(
            delivery_id, op, data,
            seq=len(digests), prev_sha256=digests[-1],
        )
        row_raw = self._event_raw(row)
        payload.extend(row_raw + b"\n")
        digests.append(hashlib.sha256(row_raw).hexdigest())

        fd = self._open_regular(
            self.path, os.O_APPEND | os.O_CREAT | os.O_RDWR,
        )
        try:
            if os.fstat(fd).st_size != len(existing_raw):
                raise OwnerDeliveryCorruption("owner-delivery ledger changed while locked")
            self._write_all(fd, bytes(payload))
            os.fsync(fd)
        finally:
            os.close(fd)
        self._fsync_directory()
        # This order is intentional: a crash here leaves a fully chained ledger
        # ahead of its anchor, which _reconcile_head_unlocked can safely advance.
        self._write_head_unlocked(len(digests) - 1, digests[-1])
        return row

    @staticmethod
    def _public(state: dict) -> dict:
        return {key: json.loads(json.dumps(value, ensure_ascii=False))
                for key, value in state.items() if not key.startswith("_")}

    def emit(
        self,
        kind: str,
        *,
        title: str,
        body: str = "",
        urgency: str = "normal",
        outcome: str = "info",
        thread_key: str = "",
        correlation: dict | None = None,
        reason: str = "",
        provenance: dict | None = None,
        expectation: str = "",
        action: dict | None = None,
        result: str = "",
        dedupe_key: str = "",
        coalesce_key: str = "",
        transports: tuple[str, ...] | list[str] = ("pwa", "telegram"),
    ) -> dict:
        kind = str(kind or "").strip().lower()
        urgency = str(urgency or "normal").strip().lower()
        outcome = str(outcome or "info").strip().lower()
        if kind not in DELIVERY_TYPES:
            raise ValueError(f"unsupported owner delivery type: {kind}")
        if urgency not in URGENCIES:
            raise ValueError(f"unsupported owner delivery urgency: {urgency}")
        if outcome not in OUTCOMES:
            raise ValueError(f"unsupported owner delivery outcome: {outcome}")
        target_transports = sorted(set(str(item).strip().lower() for item in transports))
        if not target_transports or any(
            item not in {"pwa", "telegram"} for item in target_transports
        ):
            raise ValueError("owner delivery transports must be pwa and/or telegram")
        data = {
            "type": kind,
            "title": _text(title, limit=240, required=True),
            "body": _text(body, limit=12_000),
            "urgency": urgency,
            "outcome": outcome,
            "thread_key": _text(thread_key, limit=500),
            "correlation": _small_object(correlation, name="correlation"),
            "reason": _text(reason, limit=2_000),
            "provenance": _small_object(provenance, name="provenance"),
            "expectation": _text(expectation, limit=2_000),
            "action": _small_object(action, name="action"),
            "result": _text(result, limit=4_000),
            "dedupe_key": _text(dedupe_key, limit=500),
            "coalesce_key": _text(coalesce_key, limit=500),
            "transports": target_transports,
        }
        with self._lock():
            states = self._states_unlocked()
            if data["dedupe_key"]:
                for state in states.values():
                    if state["dedupe_key"] == data["dedupe_key"]:
                        # A crash may have happened after the newer create row but
                        # before its older coalesced sibling was superseded.  An
                        # idempotent replay repairs that projection without ever
                        # hiding the newer item first.
                        if data["coalesce_key"] and state["status"] in _ACTIVE:
                            for other in list(states.values()):
                                if (other["id"] != state["id"]
                                        and other["coalesce_key"] == data["coalesce_key"]
                                        and other["status"] in _ACTIVE):
                                    self._transition_unlocked(
                                        other, "superseded",
                                        {"reason": "coalesced_by_newer_item"},
                                    )
                            state = self._states_unlocked()[state["id"]]
                        return self._public(state)
            delivery_id = f"delivery-{uuid.uuid4().hex}"
            self._append_unlocked(delivery_id, "create", data)
            states = self._states_unlocked()
            if data["coalesce_key"]:
                for state in list(states.values()):
                    if (state["id"] != delivery_id
                            and state["coalesce_key"] == data["coalesce_key"]
                            and state["status"] in _ACTIVE):
                        self._transition_unlocked(
                            state, "superseded", {"reason": "coalesced_by_newer_item"}
                        )
            return self._public(self._states_unlocked()[delivery_id])

    def _transition_unlocked(self, state: dict, target: str, detail: dict) -> None:
        self._append_unlocked(state["id"], "transition", {
            "from": state["status"],
            "to": target,
            "expected_revision": state["revision"],
            "detail": _small_object(detail, name="transition detail"),
        })

    def _advance_unlocked(self, state: dict, target: str, detail: dict) -> dict:
        order = {
            "queued": 0, "delivered": 1, "read": 2, "acted": 3,
            "superseded": 4,
        }
        if state["status"] == "superseded":
            if target == "superseded":
                return state
            raise ValueError("superseded owner delivery is terminal")
        if state["status"] == "acted":
            if target == "acted":
                return state
            raise ValueError("acted owner delivery is terminal")
        if target == "superseded":
            self._transition_unlocked(state, target, detail)
            return self._states_unlocked()[state["id"]]
        if order[target] < order[state["status"]]:
            return state
        path = ("delivered", "read", "acted")
        while state["status"] != target:
            next_state = path[order[state["status"]]]
            transition_detail = detail if next_state == target else {
                "transport": "pwa", "implicit": True,
            }
            self._transition_unlocked(state, next_state, transition_detail)
            state = self._states_unlocked()[state["id"]]
        return state

    def transition(
        self,
        delivery_id: str,
        target: str,
        *,
        expected_revision: int,
        detail: dict | None = None,
    ) -> dict:
        target = str(target or "").strip().lower()
        if target not in STATES - {"queued"}:
            raise ValueError("owner delivery target state is invalid")
        if (not isinstance(expected_revision, int)
                or isinstance(expected_revision, bool) or expected_revision < 1):
            raise ValueError("expected_revision must be a positive integer")
        with self._lock():
            state = self._states_unlocked().get(str(delivery_id or ""))
            if state is None:
                raise KeyError(delivery_id)
            if state["revision"] != expected_revision:
                raise ValueError("stale owner delivery revision")
            state = self._advance_unlocked(
                state, target, _small_object(detail, name="transition detail")
            )
            return self._public(state)

    def mark_delivered(self, delivery_id: str, *, transport: str,
                       receipt: dict | None = None) -> dict:
        with self._lock():
            state = self._states_unlocked().get(str(delivery_id or ""))
            if state is None:
                raise KeyError(delivery_id)
            detail = {
                "transport": _text(transport, limit=40, required=True),
                "receipt": _small_object(receipt, name="delivery receipt", limit=4_000),
            }
            return self._public(self._advance_unlocked(state, "delivered", detail))

    def get(self, delivery_id: str) -> dict | None:
        with self._lock():
            state = self._states_unlocked().get(str(delivery_id or ""))
            return self._public(state) if state else None

    def list(self, *, limit: int = 100, include_superseded: bool = False) -> list[dict]:
        with self._lock():
            states = list(self._states_unlocked().values())
        if not include_superseded:
            states = [state for state in states if state["status"] != "superseded"]
        states.sort(key=lambda state: state["_sequence"], reverse=True)
        return [self._public(state) for state in states[:max(0, int(limit))]]

    def pending(self, *, limit: int = 100) -> list[dict]:
        rows = [row for row in self.list(limit=100_000, include_superseded=False)
                if row["status"] == "queued" and "telegram" in row["transports"]]
        rows.reverse()
        return rows[:max(0, int(limit))]

    def snapshot(self, *, limit: int = 80) -> dict:
        with self._lock():
            states = list(self._states_unlocked().values())
            event_ids = [event["event_id"] for event in self._events_unlocked()]
        visible = [state for state in states if state["status"] != "superseded"]
        visible.sort(key=lambda state: state["_sequence"], reverse=True)
        # The limit is a history budget, never an attention budget.  Every
        # item that is unread or read-but-not-acted stays visible even after a
        # large acted history; recent terminal rows only fill the remainder.
        actionable = [
            state for state in visible
            if state["status"] in {"queued", "delivered", "read"}
        ]
        terminal = [
            state for state in visible
            if state["status"] not in {"queued", "delivered", "read"}
        ]
        budget = max(0, int(limit))
        selected = actionable + terminal[:max(0, budget - len(actionable))]
        counts = {state: 0 for state in STATES}
        for state in states:
            counts[state["status"]] += 1
        revision = hashlib.sha256("\n".join(event_ids).encode("ascii")).hexdigest()[:24]
        return {
            "items": [self._public(state) for state in selected],
            "counts": counts,
            "unread": sum(state["status"] in {"queued", "delivered"} for state in visible),
            "queued": counts["queued"],
            "revision": revision,
        }


def format_telegram(item: dict) -> str:
    """Render one ledger item as a compact human receipt, without raw ids."""
    if not isinstance(item, dict) or item.get("type") not in DELIVERY_TYPES:
        raise ValueError("valid owner delivery item is required")
    icon = {
        "attention": "👀",
        "followup_answer": "↩️",
        "run_result": "🧭",
        "file_ready": "📎",
        "system_alert": "⚠️",
        "social_suggestion": "💭",
    }[item["type"]]
    outcome = str(item.get("outcome") or "info")
    status = {
        "info": "информация",
        "success": "готово",
        "partial": "частично",
        "blocked": "заблокировано",
        "failure": "не получилось",
        "needs_input": "нужно твоё решение",
    }.get(outcome, outcome)
    title = _text(item.get("title"), limit=240, required=True)
    body = _text(item.get("body"), limit=12_000)
    result = _text(item.get("result"), limit=4_000)
    if item["type"] == "followup_answer":
        answer = (body or result or "(без текста)")[:3400]
        rendered = f"{icon} {title}:\n{answer}"
        return rendered if len(rendered) <= 3900 else rendered[:3897].rstrip() + "…"

    reason = _text(item.get("reason"), limit=2_000)
    expectation = _text(item.get("expectation"), limit=2_000)
    action = item.get("action") if isinstance(item.get("action"), dict) else {}
    action_label = _text(action.get("label"), limit=240)

    lines = [f"{icon} {title}", f"Статус: {status}"]
    if body and body != result:
        lines.extend(("", f"Сводка: {body[:1200]}"))
    if result or body:
        result_label = "Ответ" if item["type"] == "followup_answer" else "Результат"
        lines.extend(("", f"{result_label}: {(result or body)[:2200]}"))
    if reason:
        lines.append(f"Почему: {reason[:500]}")
    if expectation:
        label = "Нужно от тебя" if outcome == "needs_input" else "Дальше"
        lines.append(f"{label}: {expectation[:600]}")
    if action_label:
        lines.append(f"Действие: {action_label}")
    rendered = "\n".join(lines)
    return rendered if len(rendered) <= 3900 else rendered[:3897].rstrip() + "…"


LEDGER = OwnerDeliveryLedger()
