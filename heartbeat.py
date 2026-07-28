"""
Praxis — автономный тик (heartbeat). Её время между разговорами, с руками.

Раз в `PRAXIS_HEARTBEAT_HOURS` (0 — выкл) Praxis получает обычный голос-вызов с
инструментами (owner-права): может навести порядок в памяти, дописать навык,
доделать задуманное в своём коде (shell/write_skill, безопасно — автокоммит+откат),
отметить мысль, или написать Егору по открытой нити. Поводы подаются как
структурированные runtime-receipts и узкий canonical automatic recall. Если сказать
нечего — молчит (раннер ничего не отправляет).

Это «фоновое сознание», но бережное: всё за рельсами (3.6-проверка, selfgit, bootguard),
owner-gated, экономное по рамке промпта. Кода правит только тут — обычным tool-вызовом.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import time
from pathlib import Path

import appetite
import llm
import memory_index
import memory_provenance
import people
import unanswered

log = logging.getLogger("praxis-heartbeat")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
DECISIONS_PATH = BASE / "memory" / ".state" / "window_decisions.json"
DECISIONS_KEEP_DAYS = 7

_DATE_RE = re.compile(r"_\((\d{4}-\d{2}-\d{2})\)_")
# ⚠ Отсюда удалён мёртвый контур решения об окне: enabled/candidates/
# window_goal/record_decision/_record_hold_once/mark_window. Его единственный
# вызывающий (mtproto_runner._heartbeat_once) не стоял ни в одной строке
# _clock_jobs(): часы сведены к единственному durable-пульсу, потому что два
# часовых джоба гонялись и открывали два окна на один час. Через этот
# контур не работали appetite.windows_off() и considerate_hint(), а
# opened_today() вечно возвращал ноль. Выброшено 25.07 по решению Егора.
# Живыми остались читатели: decisions() для пульта, opened_today()
# (теперь по распискам пульса), window_context() для цели пульса и часовые
# хелперы parse_hours/hour_in/local_now для sleep и окна сна раннера.





# Local time parsing is shared with the scheduled sleep contour.  It does not
# gate the hourly wake.

def local_now(ts: float | None = None) -> _dt.datetime:
    """Локальное время (PRAXIS_TZ, IANA, дефолт Europe/Moscow). Без tzdata в контейнере —
    честный фолбэк на фиксированный сдвиг PRAXIS_TZ_OFFSET_H (int, дефолт 3)."""
    t = ts if ts is not None else time.time()
    tz_name = (os.getenv("PRAXIS_TZ") or "Europe/Moscow").strip()
    try:
        from zoneinfo import ZoneInfo
        return _dt.datetime.fromtimestamp(t, ZoneInfo(tz_name))
    except Exception:
        try:
            off = int(os.getenv("PRAXIS_TZ_OFFSET_H", "3"))
        except ValueError:
            off = 3
        return _dt.datetime.fromtimestamp(t, _dt.timezone(_dt.timedelta(hours=off)))


def parse_hours(spec: str) -> tuple[int, int] | None:
    """'1-8' -> (1, 8): интервал часов [от, до), может перекатываться через полночь
    ('23-7'). Пусто/off/мусор -> None (интервал выключен)."""
    s = (spec or "").strip().lower()
    if not s or s in ("off", "0-0", "none"):
        return None
    m = re.fullmatch(r"(\d{1,2})\s*-\s*(\d{1,2})", s)
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    if a > 23 or b > 24:
        return None
    if b != 24:
        b %= 24
    if a == b:
        return None  # пустой интервал — не «весь день» (для этого 0-24)
    return (a, b)


def hour_in(h: int, rng: tuple[int, int] | None) -> bool:
    """Час h внутри интервала [a, b)? Перекат через полночь поддержан."""
    if not rng:
        return False
    a, b = rng
    if a < b:
        return a <= h < b
    return h >= a or h < b


def last_opened_ts() -> float:
    """Момент последнего ОТКРЫТОГО окна (verdict != НЕТ, done != false) или 0.0."""
    best = 0.0
    for e in _decisions_load():
        if not isinstance(e, dict):
            continue
        if str(e.get("verdict") or "НЕТ").strip().upper() == "НЕТ":
            continue
        if e.get("done") is False:
            continue
        best = max(best, float(e.get("ts") or 0))
    return best




# ⚠ candidates() числился в мёртвом контуре и был удалён вместе с ним — ошибка:
# его читает _loops_context(), а тот входит в window_context() — то есть в цель
# живого часового пульса. Пометки внимания — её собственные ориентиры, не tasks.
# Восстановлен байт-в-байт из HEAD.
def candidates() -> list[dict]:
    """Открытые нити по всем людям в окне [min_age, max_age] дней (контекст для тика)."""
    today = _dt.date.today()
    min_age = int(os.getenv("PRAXIS_HEARTBEAT_MIN_AGE_DAYS", "1"))
    max_age = int(os.getenv("PRAXIS_HEARTBEAT_MAX_AGE_DAYS", "30"))
    out = []
    for path in sorted(people.PEOPLE_DIR.glob("*.md")):
        if path.stem.startswith("_"):
            continue
        slug = path.stem
        _, body = people.parse(people._read(path))
        for line in (body.get(people.LOOPS, "")).splitlines():
            s = line.strip()
            if s.startswith(("- [~]", "* [~]")):
                # 11.0: спящая нить просыпается по дате; файл здесь не переписываем
                # (метку [~]->[ ] снимает сон или unpark_loops при новом разговоре)
                m_until = people.PARKED_UNTIL.search(s)
                if m_until and m_until.group(1) > today.isoformat():
                    continue
                s = people.PARKED_UNTIL.sub("", s.replace("[~]", "[ ]", 1)).strip()
            if not s.startswith(("- [ ]", "* [ ]")):
                continue
            text = _DATE_RE.sub("", re.sub(r"^[-*]\s*\[ \]\s*", "", s)).strip()
            m = _DATE_RE.search(s)
            age = 999
            if m:
                try:
                    age = (today - _dt.date.fromisoformat(m.group(1))).days
                except ValueError:
                    age = 999
            if min_age <= age <= max_age:
                out.append({"slug": slug, "text": text, "age": age})
    out.sort(key=lambda c: c["age"], reverse=True)
    return out

def _loops_context() -> str:
    cands = candidates()
    if not cands:
        return ""
    lines = [f"- {c['slug']}: {c['text']} ({c['age']}д)" for c in cands[:8]]
    return "Добровольные пометки внимания (не tasks; можно не трогать или закрыть как устаревшие):\n" + "\n".join(lines)




# --------------------------------------------------------------------------- #
#  PASS 9.4: прозрачность окон — кольцо решений (7 дней) + кап/сутки
# --------------------------------------------------------------------------- #

def _decisions_load() -> list[dict]:
    try:
        d = json.loads(DECISIONS_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []








def decisions(days: int = 7, now: float | None = None) -> list[dict]:
    now = now if now is not None else time.time()
    cutoff = now - max(1, days) * 86400
    return [e for e in _decisions_load()
            if isinstance(e, dict) and float(e.get("ts") or 0) >= cutoff]


def opened_today(now: float | None = None) -> int:
    """Observable count of authored wake runs since local midnight.

    This is receipt telemetry, never a daily limit.

    ⚠ Читалось из `window_decisions.json`, который писали `record_decision`/
    `mark_window` — а их единственный вызывающий, `_heartbeat_once`, не стоит ни в
    одной строке `_clock_jobs()`. То есть счётчик возвращал 0 ВСЕГДА, и этот ноль
    шёл ей в промпт («автономных окон сегодня: 0») и в пульт, пока `social_pulse`
    реально открывал окно каждый час. Считаем по живым распискам пульса; старое
    кольцо решений читаем тоже, чтобы вчерашняя история не обнулилась.
    """
    now_dt = local_now(now)
    midnight = now_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    total = 0
    try:
        import social_pulse
        for run in (social_pulse.runs() or []):
            if not isinstance(run, dict):
                continue
            if float(run.get("started_at") or 0) < midnight:
                continue
            if str(run.get("status") or "") == "failed":
                continue
            total += 1
    except Exception:
        log.debug("счётчик окон: расписки пульса не прочитались", exc_info=True)
    total += sum(1 for e in _decisions_load()
                 if isinstance(e, dict) and float(e.get("ts") or 0) >= midnight
                 and str(e.get("verdict") or "НЕТ").strip().upper() != "НЕТ"
                 and e.get("done") is not False)
    return total


def _unanswered_context() -> str:
    """Useful owner-private context about unanswered conversations."""
    try:
        items = unanswered.entries()
    except Exception:
        return ""
    if not items:
        return ""
    rows = []
    for item in items[:20]:
        if not isinstance(item, dict):
            continue
        try:
            hours = round(max(0.0, float(item.get("hours") or 0.0)), 1)
        except (TypeError, ValueError):
            hours = None
        rows.append({
            "chat_id": str(item.get("chat_id") or item.get("id") or "")[:120],
            "name": str(item.get("name") or item.get("who") or "")[:200],
            "hours": hours,
            "message": str(item.get("message") or item.get("text") or item.get("gist") or "")[:800],
        })
    return "UNANSWERED CONVERSATIONS:\n" + "\n".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows
    ) if rows else ""


_AUTOMATIC_GOAL_QUERY = (
    "незавершенная задача проблема ошибка обещание открытая нить цель память код навык"
)


def _automatic_memory_context(cap: int | None = None) -> str:
    """Canonical automatic recall; only the owner-rejected raw diary stays excluded."""
    if cap is None:
        try:
            cap = int(os.getenv("PRAXIS_AUTO_RECALL_K", "12"))
        except ValueError:
            cap = 12
    try:
        hits = memory_index.search(
            _AUTOMATIC_GOAL_QUERY,
            k=max(1, min(int(cap), 64)),
            scope="owner",
            semantic=False,
            purpose="automatic",
        )
    except Exception:
        log.debug("heartbeat: canonical automatic recall failed", exc_info=True)
        return ""
    rows = []
    for hit in hits:
        if (not isinstance(hit, dict)
                or hit.get("automatic_canonical") is not True
                or not memory_provenance.automatic_recall_allowed(
                    source_type=hit.get("source_type"),
                    path=hit.get("path"),
                    text=hit.get("text"),
                    memory_dir=BASE / "memory",
                )):
            continue
        text = str(hit.get("text") or "").strip()
        if not text:
            continue
        rows.append({
            "source_type": str(hit.get("source_type") or "canonical")[:80],
            "path": str(hit.get("path") or "")[:500],
            "at": str(hit.get("at") or "")[:80],
            "event_id": str(hit.get("event_id") or "")[:160],
            "run_id": str(hit.get("run_id") or "")[:160],
            "text": text[:1200],
        })
    if not rows:
        return ""
    return (
        "CANONICAL HISTORICAL MEMORY CUES "
        "(retrieval matches, not open tasks; verify current evidence before acting):\n"
        + "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows)
    )


def _recent_windows_context(days: int = 7, cap: int = 6) -> str:
    """Show Praxis her actual recent goals and outcomes, not opaque hashes."""
    rows = []
    for e in decisions(days):
        v = str(e.get("verdict") or "").strip()
        if not v or v.upper() == "НЕТ":
            continue
        d = e.get("done")
        status = "исполнено" if d is True else ("упало" if d is False else "в работе")
        ts = float(e.get("ts") or 0)
        rows.append({
            "date": local_now(ts).date().isoformat(),
            "status": status,
            "goal": v[:500],
            "spent": str(e.get("spent") or "")[:120],
            "why": str(e.get("why") or "")[:200],
        })
    if not rows:
        return ""
    receipt = {
        "kind": "recent_window_receipts",
        "count": len(rows),
        "windows": rows[-cap:],
    }
    return "RUNTIME RECEIPT: " + json.dumps(receipt, ensure_ascii=False, sort_keys=True)


def _transport_status() -> str:
    """PASS 30.0.e: сенсор Telegram-транспорта из agent._TELETHON (lazy — без цикла импорта)."""
    try:
        import agent
        return agent.telegram_transport_status()
    except Exception:
        return "unknown"


def _pacing_context() -> str:
    """Observable wake history without resurrecting retired pacing policy."""
    now = local_now()
    last = last_opened_ts()
    return "WAKE OBSERVABILITY: " + json.dumps({
        "local_time": now.isoformat(),
        "hours_since_last_opened": round((time.time() - last) / 3600, 2) if last else None,
        "opened_today": opened_today(),
        # closed_for_window = Telethon намеренно закрыт на это окно: read_chat/search_chats
        # вернутся после его конца; «отложила: транспорт закрыт» — честная причина, не сбой.
        "telegram_transport": _transport_status(),
        "note": "history only; the hourly wake itself has no quiet/gap/daily gate",
    }, ensure_ascii=False, sort_keys=True)


def _forge_completions_context() -> str:
    """Завершившиеся Forge-воркеры как приглашение к её плоду (normal-темп, часовое окно).

    consume=True: показанные один раз в её часовом контексте, повторно не всплывают.
    urgent-завершения потребляются раньше (немедленным окном из раннер-тика).

    PASS 30 Этап 1: при живом событийном контуре этот фолбэк СПИТ — иначе часовое
    окно может потребить завершение, пока событие ждёт зазор насоса, и оно всплывёт
    дважды (здесь прозой и ходом forge_event)."""
    try:
        from core import events as core_events
        if core_events.enabled():
            return ""
    except Exception:
        pass
    try:
        import forge
        return forge.wake_invitation(forge.pending_completions(consume=True))
    except Exception:
        return ""


def window_context(*, include_pacing: bool = True) -> str:
    """Full bounded context for an hourly autonomous decision."""
    parts = [p for p in (
        _pacing_context() if include_pacing else "",
        _unanswered_context(), _loops_context(),
        _recent_windows_context(), _automatic_memory_context(),
        _forge_completions_context(),
    ) if p]
    return "\n\n".join(parts)


