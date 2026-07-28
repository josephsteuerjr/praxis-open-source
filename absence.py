"""
Praxis — движок ответа в явно открытое владельцем окно отсутствия.

Состояние (окно отсутствия + важные контакты + schedule_note) живёт в `memory/absence.json`
— тот же файл, что пишет панель (panel.absence_start/stop, contact_add, schedule_note). Здесь
ТОЛЬКО чтение этого файла + receipts автономных отправок (`memory/.state/absence_sent.json`).
Сам голос ответа — agent.compose_absence_reply (узкий проход, без тулов). Оркестрацию (обход,
отправка, heads-up после) делает раннер: absence.py — stdlib, без telethon и без модели.

Триггер — пересечение трёх УЖЕ существующих сигналов, не новый детектор:
  unanswered.entries (старше кулдауна DM)  ∩  отправитель в важных  ∩  активное окно отсутствия.
Каждое новое сообщение создаёт новую незакрытую нить; отдельный поведенческий кап не нужен.
Не выполнено хотя бы одно — тишина/обычный путь, как сейчас.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
ABSENCE_FILE = BASE / "memory" / "absence.json"
SENT_FILE = BASE / "memory" / ".state" / "absence_sent.json"


def _load(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _state() -> dict:
    return _load(ABSENCE_FILE)


def window() -> dict | None:
    """Активное окно отсутствия -> {until, started, hours} или None (нет/истекло)."""
    w = _state().get("window") or {}
    try:
        until = float(w.get("until", 0) or 0)
    except (TypeError, ValueError):
        return None
    return w if until > time.time() else None


def is_active() -> bool:
    return window() is not None


def contacts() -> list[dict]:
    return _state().get("contacts") or []


def important(chat_id) -> dict | None:
    """Важный контакт по его telegram id (== chat_id в личке) -> запись или None."""
    cid = str(chat_id)
    for c in contacts():
        if str(c.get("id") or "") == cid:
            return c
    return None


def schedule_note() -> str:
    """Свободный текст, который владелец заранее оставил на окно ('' если нет)."""
    return str(_state().get("schedule_note") or "").strip()


def cap() -> None:
    """Compatibility API: PASS 24 removed the hidden per-person send cap."""
    return None


def _window_key(w: dict) -> str:
    """Ключ окна для счётчика — стабилен внутри окна, меняется от окна к окну."""
    return str(w.get("started") or w.get("until") or "")


def sent_count(chat_id, w: dict) -> int:
    rec = _load(SENT_FILE).get(_window_key(w))
    if not isinstance(rec, dict):
        return 0
    try:
        return int(rec.get(str(chat_id), 0))
    except (TypeError, ValueError):
        return 0


def note_sent(chat_id, w: dict) -> int:
    """Зафиксировать одну автономную отправку. Держим только текущее окно (старые выкидываем)."""
    key = _window_key(w)
    rec = _load(SENT_FILE).get(key)
    rec = dict(rec) if isinstance(rec, dict) else {}
    rec[str(chat_id)] = int(rec.get(str(chat_id), 0) or 0) + 1
    data = {key: rec}
    SENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SENT_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SENT_FILE)
    return rec[str(chat_id)]


def due(now: float | None = None) -> list[dict]:
    """Кого пинговать сейчас: пересечение трёх сигналов.
    -> [{chat_id, name, since, hours, person}] (старые сверху); [] если окна нет."""
    import unanswered
    w = window()
    if w is None:
        return []
    out = []
    for e in unanswered.entries(now):
        person = important(e["chat_id"])
        if not person:
            continue  # неотвеченный, но не из важных — обычный путь, не наше дело
        out.append({**e, "person": person})
    return out
