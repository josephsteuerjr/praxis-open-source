"""
Praxis — трекер неотвеченных ЛС (PASS 9.0, кейс Евгения). Stdlib only, без telethon.

`memory/.state/unanswered.json`: {chat_id: {"name": str, "since": epoch}}.
Запись рождается, когда в личке появляется чужая реплика, которую она собиралась
разобрать (раннер зовёт note_incoming перед взводом прохода). Снимается её ответом
ИЛИ осознанным молчанием ([молчу] / вердикт оценщика / анти-повтор) — решённое
молчание ≠ неотвеченность. Запись переживает рестарт — в этом весь смысл: армед-дебаунс
умирает с процессом, файл нет.

Читатели: STATE (owner tier-0), «Пульс» в панели, heartbeat.window_goal (неотвеченное —
хороший повод для окна). Показываются только записи старше кулдауна DM: пока идёт
обычный проход, человек не «неотвеченный».
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
STATE_DIR = BASE / "memory" / ".state"
PATH = STATE_DIR / "unanswered.json"


def _min_age() -> float:
    """Порог «дольше кулдауна»: раньше него запись не считается неотвеченностью.

    PASS 21: источник тот же, что у фактического темпа (perception.cooldown_dm), —
    иначе порог разъезжается с живым кулдауном."""
    try:
        import perception
        return float(perception.value("cooldown_dm"))
    except Exception:
        try:
            return float(os.getenv("PRAXIS_COOLDOWN_DM", "8"))
        except ValueError:
            return 8.0


def _load() -> dict:
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(PATH)


def note_incoming(chat_id, name: str = "", ts: float | None = None) -> None:
    """Чужая реплика в ЛС: завести/освежить запись. `since` — первая неотвеченная (не двигаем)."""
    data = _load()
    key = str(chat_id)
    cur = data.get(key) if isinstance(data.get(key), dict) else None
    if cur is None:
        data[key] = {"name": str(name or ""), "since": float(ts if ts is not None else time.time())}
    elif name and not cur.get("name"):
        cur["name"] = str(name)
    else:
        return  # запись уже есть — since честно держит первую неотвеченную
    _save(data)


def resolve(chat_id) -> None:
    """Ответ или решённое молчание: снять запись (нет записи — тихий no-op)."""
    data = _load()
    if str(chat_id) in data:
        data.pop(str(chat_id), None)
        _save(data)


def entries(now: float | None = None) -> list[dict]:
    """Неотвеченные старше кулдауна, старые сверху: [{chat_id, name, since, hours}]."""
    now = now if now is not None else time.time()
    out = []
    for cid, e in _load().items():
        if not isinstance(e, dict):
            continue
        try:
            since = float(e.get("since") or 0)
        except (TypeError, ValueError):
            continue
        age = now - since
        if age < _min_age():
            continue
        out.append({"chat_id": cid, "name": str(e.get("name") or ""),
                    "since": since, "hours": age / 3600.0})
    out.sort(key=lambda x: x["since"])
    return out


def _fmt_age(hours: float) -> str:
    if hours < 1:
        return f"{max(1, int(hours * 60))}м"
    if hours < 48:
        return f"{int(hours)}ч"
    return f"{int(hours / 24)}д"


def state_line(now: float | None = None, cap: int = 5) -> str:
    """Одна строка для STATE/контекстов: «Евгений (3ч), Аня (26ч)» или ''."""
    items = entries(now)
    if not items:
        return ""
    parts = [f"{e['name'] or e['chat_id']} ({_fmt_age(e['hours'])})" for e in items[:cap]]
    more = f" и ещё {len(items) - cap}" if len(items) > cap else ""
    return ", ".join(parts) + more
