"""Hourly, durable social pulse for Praxis.

The module records wakeups and outbound receipts.  It never decides how many people
Praxis may write to, how often, or whether a repeated contact is morally appropriate;
those are authored choices made with the recent receipts visible in context.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
STATE_PATH = BASE / "memory" / ".state" / "social_pulse.json"
_ACTIVE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "praxis_social_pulse", default=None
)
_LOCK = threading.RLock()


def _empty() -> dict:
    return {
        "version": 1,
        "last_started_at": 0.0,
        "runs": [],
        "outbound": [],
        "mailbox_seen": [],
    }


def _load(path: Path = STATE_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    data["version"] = 1
    if not isinstance(data.get("last_started_at"), (int, float)):
        data["last_started_at"] = 0.0
    if not isinstance(data.get("runs"), list):
        data["runs"] = []
    else:
        data["runs"] = [row for row in data["runs"] if isinstance(row, dict)]
    if not isinstance(data.get("outbound"), list):
        data["outbound"] = []
    else:
        data["outbound"] = [row for row in data["outbound"] if isinstance(row, dict)]
    if not isinstance(data.get("mailbox_seen"), list):
        data["mailbox_seen"] = []
    else:
        data["mailbox_seen"] = [str(value) for value in data["mailbox_seen"] if value]
    return data


def _save(data: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def interval_hours() -> float:
    try:
        return max(0.0, float(os.getenv("PRAXIS_SOCIAL_PULSE_HOURS", "1")))
    except ValueError:
        return 1.0


def _local_datetime(timestamp: float) -> dt.datetime:
    tz_name = (os.getenv("PRAXIS_TZ") or "Europe/Moscow").strip()
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.fromtimestamp(timestamp, ZoneInfo(tz_name))
    except Exception:
        try:
            offset = int(os.getenv("PRAXIS_TZ_OFFSET_H", "3"))
        except ValueError:
            offset = 3
        timezone = dt.timezone(dt.timedelta(hours=offset))
        return dt.datetime.fromtimestamp(timestamp, timezone)


def due(*, now: float | None = None, path: Path = STATE_PATH) -> bool:
    interval = interval_hours()
    if interval <= 0:
        return False
    at = float(now if now is not None else time.time())
    with _LOCK:
        last = float(_load(path).get("last_started_at") or 0.0)
    return not last or at - last >= interval * 3600


def next_due_at(*, now: float | None = None, path: Path = STATE_PATH) -> float:
    """Earliest moment begin() could claim a pulse: last start + interval, else now.

    The single clock arms its social_pulse deadline with this boundary so a restart
    minutes before the hourly window keeps the original window: begin() declines an
    early attempt, and a plain now+period re-arm would drift the window a full
    period past her own deadline."""
    at = float(now if now is not None else time.time())
    interval = interval_hours()
    if interval <= 0:
        return at
    with _LOCK:
        last = float(_load(path).get("last_started_at") or 0.0)
    # last в БУДУЩЕМ (обратный скачок часов / битый стейт) не должен запинать пульс
    # сколь угодно вперёд: граница считается один раз на буте (адверсарка round-1).
    if not last or last > at:
        return at
    return max(at, last + interval * 3600)


def runs(*, path: Path | None = None) -> list[dict]:
    """Расписки пробуждений, старые → новые. Публичное чтение durable-состояния.

    Нужно тем, кто должен считать её автономные окна по факту, а не по легаси-кольцу
    решений мёртвого heartbeat-контура (см. heartbeat.opened_today).

    Путь резолвится НА ВЫЗОВЕ, а не в дефолте сигнатуры: `path=STATE_PATH` связало бы
    значение на импорте, и подмена STATE_PATH (тесты, изоляция) молча не действовала бы."""
    with _LOCK:
        rows = list(_load(path if path is not None else STATE_PATH).get("runs") or [])
    return [r for r in rows if isinstance(r, dict)]


def begin(*, now: float | None = None, path: Path = STATE_PATH) -> str | None:
    """Atomically claim a due pulse.  Returns its id, or None if not due."""
    at = float(now if now is not None else time.time())
    with _LOCK:
        state = _load(path)
        interval = interval_hours()
        last = float(state.get("last_started_at") or 0.0)
        if interval <= 0 or (last and at - last < interval * 3600):
            return None
        for run in state["runs"]:
            if run.get("status") == "running":
                run.update(
                    status="interrupted",
                    finished_at=at,
                    detail="process ended before pulse receipt was finalized",
                )
        pulse_id = "pulse_" + uuid.uuid4().hex[:12]
        state["last_started_at"] = at
        state["runs"].append({"id": pulse_id, "started_at": at, "status": "running"})
        state["runs"] = state["runs"][-168:]
        _save(state, path)
        return pulse_id


def observability(pulse_id: str, *, now: float | None = None,
                  path: Path = STATE_PATH) -> str:
    """Describe this pulse from its own durable receipts, not legacy heartbeat state."""
    at = float(now if now is not None else time.time())
    local_now = _local_datetime(at)
    with _LOCK:
        runs = list(_load(path).get("runs") or [])
    current = next((run for run in reversed(runs) if run.get("id") == pulse_id), None)
    previous = next((run for run in reversed(runs) if run.get("id") != pulse_id), None)

    def started_at(run: dict | None) -> float:
        try:
            return float((run or {}).get("started_at") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    today = local_now.date()
    started_today = 0
    for run in runs:
        started = started_at(run)
        if started and _local_datetime(started).date() == today:
            started_today += 1
    current_started = started_at(current)
    previous_started = started_at(previous)
    payload = {
        "pulse_id": str(pulse_id),
        "local_time": local_now.isoformat(),
        "current_age_seconds": round(max(0.0, at - current_started), 1)
        if current_started else None,
        "hours_since_previous_started": round(max(0.0, at - previous_started) / 3600, 2)
        if previous_started else None,
        "started_today": started_today,
        "previous_status": previous.get("status") if previous else None,
        "note": "social-pulse receipts only; history is context, not a gate",
    }
    return "SOCIAL PULSE OBSERVABILITY: " + json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
    )


def finish(pulse_id: str, *, ok: bool, detail: str = "", now: float | None = None,
           mailbox_hashes: list[str] | None = None, path: Path = STATE_PATH) -> None:
    at = float(now if now is not None else time.time())
    with _LOCK:
        state = _load(path)
        for run in reversed(state["runs"]):
            if run.get("id") == pulse_id:
                run.update(status="done" if ok else "failed", finished_at=at,
                           detail=str(detail or "")[:500])
                break
        if ok and mailbox_hashes:
            state["mailbox_seen"] = list(dict.fromkeys([
                *state.get("mailbox_seen", []),
                *(str(value) for value in mailbox_hashes if value),
            ]))[-1000:]
        _save(state, path)


def defer(pulse_id: str, *, detail: str = "", now: float | None = None,
          path: Path = STATE_PATH) -> None:
    """Record a pulse that could not start without calling it a failed window."""
    at = float(now if now is not None else time.time())
    with _LOCK:
        state = _load(path)
        for run in reversed(state["runs"]):
            if run.get("id") == pulse_id:
                run.update(status="deferred", finished_at=at,
                           detail=str(detail or "")[:500])
                break
        previous = next(
            (run for run in reversed(state["runs"])
             if run.get("id") != pulse_id and run.get("started_at")),
            None,
        )
        state["last_started_at"] = float((previous or {}).get("started_at") or 0.0)
        _save(state, path)


def mailbox_seen(*, path: Path = STATE_PATH) -> set[str]:
    """Mail items already exposed to a successfully completed autonomous pulse."""
    with _LOCK:
        return set(_load(path).get("mailbox_seen") or [])


@contextmanager
def active(pulse_id: str):
    token = _ACTIVE.set(str(pulse_id))
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def active_id() -> str | None:
    return _ACTIVE.get()


def allow_outbound(peer_id: str | int, *, now: float | None = None,
                   path: Path = STATE_PATH) -> tuple[bool, str]:
    """Return recent delivery context without acting as a hidden rate gate."""
    pulse_id = active_id()
    if not pulse_id:
        return True, "explicit/non-pulse turn"
    at = float(now if now is not None else time.time())
    peer = str(peer_id)
    with _LOCK:
        outbound = _load(path).get("outbound") or []

    def age(row: dict) -> float:
        try:
            return at - float(row.get("sent_at") or 0)
        except (TypeError, ValueError):
            return float("inf")

    recent = [x for x in outbound if 0 <= age(x) < 86400]
    same_pulse = sum(1 for row in recent if row.get("pulse_id") == pulse_id)
    same_peer = [row for row in recent if str(row.get("peer_id")) == peer]
    context = f"sent_this_pulse={same_pulse}; sent_last_24h={len(recent)}"
    for row in reversed(recent):
        if str(row.get("peer_id")) == peer:
            context += f"; same_peer_last_hours={age(row) / 3600:.2f}"
            break
    if not same_peer:
        context += "; same_peer_last_hours=none"
    return True, context


def note_outbound(peer_id: str | int, *, message_id: str | int | None,
                  label: str = "", now: float | None = None,
                  path: Path = STATE_PATH, pulse_id_override: str = "",
                  idempotency_key: str = "") -> None:
    pulse_id = str(pulse_id_override or active_id() or "")
    if not pulse_id:
        return
    at = float(now if now is not None else time.time())
    receipt = str(idempotency_key or "").strip()
    with _LOCK:
        state = _load(path)
        for row in state.get("outbound") or ():
            try:
                same_message = (
                    str(row.get("pulse_id") or "") == pulse_id
                    and str(row.get("peer_id")) == str(peer_id)
                    and row.get("message_id") is not None
                    and message_id is not None
                    and int(row.get("message_id")) == int(message_id)
                )
            except (TypeError, ValueError):
                same_message = False
            if ((receipt and str(row.get("idempotency_key") or "") == receipt)
                    or same_message):
                return
        state["outbound"].append({
            "pulse_id": pulse_id,
            "peer_id": str(peer_id),
            "message_id": int(message_id) if message_id is not None else None,
            "label": str(label or "")[:300],
            "sent_at": at,
            "idempotency_key": receipt,
        })
        state["outbound"] = state["outbound"][-500:]
        _save(state, path)


def goal(followup_context: str) -> str:
    """A capability-enhancing frame with receipts, not behavioural vetoes."""
    return (
        "Часовой социальный импульс. Проснись как Praxis и свободно осмотрись: люди, ответы, "
        "группы, живые намерения, незакрытые нити и всё, что самой хочется начать. Ты можешь "
        "проверять факты, писать Егору или любому доступному человеку, продолжать несколько "
        "разговоров, действовать руками либо ничего не отправлять. Код показывает недавние "
        "доставки и follow-up receipts, но не превращает их в лимит, запрет или моральную оценку. "
        "Автор решения, количества сообщений и их содержания — ты.\n\n" + str(followup_context or "")
    )
