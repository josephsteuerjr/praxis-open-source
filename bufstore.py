"""Персист/восстановление per-chat буфера сообщений — restart-proof бэкстоп. §1 пакета 2.

Отдельный модуль без telethon, чтобы логику можно было тестировать в отрыве от раннера.
Файлы: `memory/.buffers/<chat>.json` — список строк "Имя: текст" (хронология). gitignored.
Основной источник контекста — Telegram live (§4); буфер — быстрый кэш + офлайн-фолбэк.

PASS 9.0: буфер строк времени не хранит — рядом живёт `memory/.state/buf_meta.json`
(chat_id → {last_ts, last_author, is_dm, name}). По нему boot-sweep находит ЛС,
оборванные рестартом (последняя строка не её и свежая), — кейс Евгения 03.07.
"""
from __future__ import annotations

import os

import json
import re
import time
from pathlib import Path

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
BUF_DIR = BASE / "memory" / ".buffers"
STATE_DIR = BASE / "memory" / ".state"
META_PATH = STATE_DIR / "buf_meta.json"


def _safe(chat_id) -> str:
    return re.sub(r"[^\w-]", "_", str(chat_id)) or "chat"


def path_for(chat_id) -> Path:
    return BUF_DIR / f"{_safe(chat_id)}.json"


def save(chat_id, lines) -> None:
    """Атомарно записать буфер чата (список строк) на диск."""
    BUF_DIR.mkdir(parents=True, exist_ok=True)
    data = [str(l) for l in lines]
    p = path_for(chat_id)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def load(chat_id) -> list[str]:
    p = path_for(chat_id)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return [str(x) for x in data] if isinstance(data, list) else []
    except Exception:
        return []


def load_all() -> dict[str, list[str]]:
    """Все сохранённые буферы: {chat_id: [строки]}. Ключ = имя файла (= sanitized chat_id)."""
    out: dict[str, list[str]] = {}
    if not BUF_DIR.exists():
        return out
    for f in BUF_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, list):
                out[f.stem] = [str(x) for x in data]
        except Exception:
            continue
    return out


# --------------------------------------------------------------------------- #
#  PASS 9.0: мета буферов (время/автор последней строки) + кандидаты boot-sweep
# --------------------------------------------------------------------------- #

def meta_load() -> dict:
    """{chat_id: {"last_ts": epoch, "last_author": str, "is_dm": bool, "name": str}}.
    Битый файл — честный старт с нуля (мета восстановима из живого потока)."""
    try:
        data = json.loads(META_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def meta_update(chat_id, author: str = "", is_dm: bool | None = None,
                name: str | None = None, ts: float | None = None) -> None:
    """Отметить последнюю строку буфера чата (атомарно, дёшево — файл маленький)."""
    meta = meta_load()
    key = _safe(chat_id)
    cur = meta.get(key) if isinstance(meta.get(key), dict) else {}
    entry = {"last_ts": float(ts if ts is not None else time.time()),
             "last_author": str(author or "")}
    entry["is_dm"] = bool(is_dm) if is_dm is not None else bool(cur.get("is_dm", not key.startswith("-")))
    entry["name"] = str(name) if name is not None else str(cur.get("name") or "")
    meta[key] = entry
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = META_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    tmp.replace(META_PATH)


def missed_dm_candidates(max_age_hours: float = 48.0, now: float | None = None) -> list[dict]:
    """ЛС, оборванные даунтаймом: последняя строка буфера НЕ её («Praxis:») и свежая.

    Только DM (группа не ждёт ответа лично от неё; DM-ность — из меты, фолбэк — знак id:
    у групп Telegram id отрицательный). Возраст — из меты, фолбэк — mtime файла буфера.
    -> [{"chat_id", "name", "age_hours"}], без мутаций."""
    now = now if now is not None else time.time()
    meta = meta_load()
    out: list[dict] = []
    if not BUF_DIR.exists():
        return out
    for f in BUF_DIR.glob("*.json"):
        cid = f.stem
        m = meta.get(cid) if isinstance(meta.get(cid), dict) else {}
        is_dm = bool(m.get("is_dm")) if "is_dm" in m else not cid.startswith("-")
        if not is_dm:
            continue
        lines = load(cid)
        if not lines or lines[-1].startswith("Praxis:"):
            continue
        try:
            last_ts = float(m.get("last_ts") or f.stat().st_mtime)
        except (TypeError, ValueError, OSError):
            continue
        age_h = max(0.0, (now - last_ts) / 3600.0)
        if age_h >= max_age_hours:
            continue  # поезд ушёл давно — не дёргаем человека спустя двое суток
        out.append({"chat_id": cid, "name": str(m.get("name") or ""), "age_hours": age_h})
    return out
