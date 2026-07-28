"""Owner-rooted, non-delegable access control for execution bodies.

The JSONL event stream is canonical.  ``TRUST.md`` is only a rebuildable human
projection: deleting it never changes authority.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Iterator


SCOPES = frozenset({
    "computer.read",
    "computer.files",
    "computer.process",
    "computer.apps",
})
_SCHEMA = "praxis.access.v1"
_EVENT_KEYS = frozenset({"schema", "id", "at", "action", "principal", "name", "scopes", "by"})
_EVENT_ID_RE = re.compile(r"access-[0-9a-f]{32}")
_THREAD_LOCK = threading.RLock()


def _base() -> Path:
    return Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)


def _dir() -> Path:
    return _base() / "memory" / "access"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _owner() -> str:
    """Return a configured Telegram *user* id, or the disabled sentinel ``0``."""
    raw = str(os.environ.get("PRAXIS_OWNER_ID") or "").strip()
    return raw if re.fullmatch(r"[1-9][0-9]*", raw) else "0"


def principal(value: str | int) -> str:
    """Normalize a positive Telegram user id; chat/channel ids are not principals."""
    raw = str(value or "").strip()
    if raw.startswith("telegram:"):
        raw = raw.split(":", 1)[1]
    if not re.fullmatch(r"[1-9][0-9]*", raw):
        raise ValueError("principal must be a positive numeric Telegram user id")
    return f"telegram:{raw}"


def _owner_principal() -> str | None:
    owner = _owner()
    return f"telegram:{owner}" if owner != "0" else None


def is_owner(actor: str | int | None) -> bool:
    owner = _owner_principal()
    if owner is None:
        return False
    try:
        return principal(actor or "") == owner
    except ValueError:
        return False


@contextlib.contextmanager
def _access_lock(*, timeout: float = 30.0) -> Iterator[None]:
    """Serialize writers across threads and processes, releasing on process death."""
    with _THREAD_LOCK:
        directory = _dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ".access.lock"
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(str(path), flags, 0o600)
        locked = False
        try:
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                deadline = time.monotonic() + max(0.1, timeout)
                while True:
                    try:
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        locked = True
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f"timed out locking {path}")
                        time.sleep(0.01)
            else:
                import fcntl

                deadline = time.monotonic() + max(0.1, timeout)
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f"timed out locking {path}")
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


def _json_object(line: str) -> dict | None:
    """Reject duplicate JSON keys instead of accepting an ambiguous authority row."""
    def unique(pairs: list[tuple[str, object]]) -> dict:
        result: dict = {}
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


def _valid_event(row: dict) -> dict | None:
    """Accept only rows that the owner-authorized writer itself could produce."""
    if frozenset(row) != _EVENT_KEYS or row.get("schema") != _SCHEMA:
        return None
    if not isinstance(row.get("id"), str) or not _EVENT_ID_RE.fullmatch(row["id"]):
        return None
    timestamp = row.get("at")
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        return None
    try:
        parsed = dt.datetime.fromisoformat(timestamp[:-1] + "+00:00")
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    try:
        target = principal(row.get("principal") or "")
    except ValueError:
        return None
    if target != row.get("principal") or row.get("by") != _owner_principal():
        return None
    if not isinstance(row.get("name"), str) or not row["name"]:
        return None
    action = row.get("action")
    scopes = row.get("scopes")
    if not isinstance(scopes, list) or any(not isinstance(scope, str) for scope in scopes):
        return None
    if action == "grant":
        if not scopes or scopes != sorted(set(scopes)) or not set(scopes).issubset(SCOPES):
            return None
    elif action == "revoke":
        if scopes:
            return None
    else:
        return None
    return row


def _events_unlocked() -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    directory = _dir() / "events"
    paths = sorted(directory.glob("????-??-??.jsonl")) if directory.exists() else []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            row = _valid_event(_json_object(line) or {})
            if row is None or row["id"] in seen:
                continue
            seen.add(row["id"])
            rows.append(row)
    return rows


def _effective_unlocked() -> dict[str, dict]:
    state: dict[str, dict] = {}
    for row in _events_unlocked():
        target = row["principal"]
        if row["action"] == "grant":
            state[target] = {
                "principal": target,
                "name": row["name"],
                "scopes": list(row["scopes"]),
                "granted_at": row["at"],
                "event_id": row["id"],
            }
        else:
            state.pop(target, None)
    return state


def effective(*, actor: str | int | None) -> dict[str, dict]:
    """Return the structured grant list to the owner only."""
    if not is_owner(actor):
        raise PermissionError("only the owner can list computer access grants")
    with _access_lock():
        return _effective_unlocked()


def _write_all(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short access-log write")
        view = view[written:]


def _append_unlocked(row: dict) -> None:
    path = _dir() / "events" / f"{row['at'][:10]}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(
        row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ) + "\n").encode("utf-8")
    flags = os.O_APPEND | os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(str(path), flags, 0o600)
    try:
        size = os.fstat(fd).st_size
        if size:
            os.lseek(fd, -1, os.SEEK_END)
            if os.read(fd, 1) != b"\n":
                # Preserve a torn tail as evidence, but isolate it from the next valid row.
                _write_all(fd, b"\n")
        _write_all(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)


def _projection_name(value: object) -> str:
    """Keep one grant on one Markdown line; the JSONL retains the exact owner label."""
    visible = "".join(
        char if char >= " " and char != "\x7f" else " "
        for char in str(value or "").replace("`", "'")
    )
    return " ".join(visible.split()) or "без имени"


def _render_unlocked(grants: dict[str, dict] | None = None) -> Path:
    grants = _effective_unlocked() if grants is None else grants
    path = _dir() / "TRUST.md"
    lines = [
        "# Доступ к компьютерам Praxis",
        "",
        "> Корень доверия — владелец. Только владелец может выдавать и отзывать доступ; доверие не делегируется.",
        "",
        f"- Owner: `telegram:{_owner()}` — все известные scopes, включая управление доверием.",
        "",
        "## Доверенные люди",
        "",
    ]
    if not grants:
        lines.append("- Никому не выдан доступ.")
    for grant in sorted(grants.values(), key=lambda item: str(item["name"]).casefold()):
        lines.append(
            f"- **{_projection_name(grant['name'])}** (`{grant['principal']}`) — "
            f"{', '.join(grant['scopes'])}; с {grant['granted_at']}"
        )
    lines += [
        "",
        "## Инварианты",
        "",
        "- Доверенный человек не может grant/revoke/list и не может передать свой доступ другому.",
        "- Проверяется стабильный Telegram user id, а не имя, username или id чата.",
        "- Неизвестный scope всегда запрещён, в том числе владельцу.",
        "- SYSTEM-контекст доступен владельцу и praxis:self; человеческий grant его не расширяет.",
        "- Этот файл — только проекция. Канонический источник: `events/*.jsonl`.",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
    return path


def _refresh_catalog() -> None:
    try:
        import memory_catalog

        memory_catalog.rebuild()
    except Exception:
        pass


def rebuild_projection() -> Path:
    """Rebuild the disposable Markdown projection from canonical validated events."""
    with _access_lock():
        path = _render_unlocked()
    _refresh_catalog()
    return path


def change(action: str, target: str | int, *, name: str = "",
           scopes: list[str] | tuple[str, ...] = (), actor: str | int) -> dict:
    if not is_owner(actor):
        return {"ok": False, "code": "owner_only", "error": "только владелец может менять доверие"}
    try:
        target_principal = principal(target)
    except ValueError as exc:
        return {"ok": False, "code": "bad_principal", "error": str(exc)}
    if target_principal == _owner_principal():
        return {"ok": False, "code": "owner_implicit", "error": "владелец уже является корнем доверия"}
    action = str(action or "").strip().lower()
    if action not in {"grant", "revoke"}:
        return {"ok": False, "code": "bad_action", "error": "action: grant | revoke"}
    if scopes is None:
        selected = []
    elif isinstance(scopes, (str, bytes)):
        selected = [str(scopes)]
    else:
        selected = sorted(set(str(item) for item in scopes if str(item)))
    unknown = sorted(set(selected) - SCOPES)
    if action == "grant" and (not selected or unknown):
        return {
            "ok": False,
            "code": "bad_scopes",
            "error": f"scopes must be a non-empty exact subset of {sorted(SCOPES)}; unknown={unknown}",
        }
    if action == "revoke" and selected:
        return {"ok": False, "code": "bad_scopes", "error": "revoke does not accept scopes"}
    with _access_lock():
        # Timestamp and id are allocated at the serialization point, so concurrent
        # grant/revoke calls have the same order in the log and in effective state.
        row = {
            "schema": _SCHEMA,
            "id": f"access-{uuid.uuid4().hex}",
            "at": _now(),
            "action": action,
            "principal": target_principal,
            "name": str(name or target_principal),
            "scopes": selected if action == "grant" else [],
            "by": _owner_principal(),
        }
        _append_unlocked(row)
        grants = _effective_unlocked()
        _render_unlocked(grants)
        current = grants.get(target_principal)
    _refresh_catalog()
    return {"ok": True, **row, "effective": current}


def allowed(actor: str | int | None, scope: str) -> bool:
    """Exact capability check.  Unknown names never become implicit owner powers."""
    if scope not in SCOPES:
        return False
    if is_owner(actor):
        return True
    try:
        actor_principal = principal(actor or "")
    except ValueError:
        return False
    with _access_lock():
        grant = _effective_unlocked().get(actor_principal)
    return bool(grant and scope in set(grant["scopes"]))


def describe(*, actor: str | int | None) -> str:
    """Owner-only user-facing listing; trusted grants cannot enumerate each other."""
    if not is_owner(actor):
        return "Только владелец может видеть и менять карту доверия."
    with _access_lock():
        grants = _effective_unlocked()
        _render_unlocked(grants)
    _refresh_catalog()
    if not grants:
        return "Владелец — единственный с доступом к компьютерам; доверенных людей пока нет."
    return "\n".join(
        f"{grant['name']} ({grant['principal']}): {', '.join(grant['scopes'])}"
        for grant in grants.values()
    )


if __name__ == "__main__":
    print("computer_access: owner-authorized listing is available through the Praxis tool")
