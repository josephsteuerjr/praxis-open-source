"""Урок → навык → доказуемая ДОСТУПНОСТЬ урока в момент выбора.

Спецификация — её, 02.08.2026, и границы в ней важнее механики:

  • система ПРЕДЛАГАЕТ превратить записанный урок в навык, но не делает этого сама;
  • она видит исходную заметку, формулирует навык сама и может отказаться;
  • при последующем выборе остаётся проверяемый след: какой именно урок был в кадре;
  • ⚠ след НЕ утверждает, что урок вызвал решение. Он доказывает только ДОСТУПНОСТЬ
    урока в момент выбора; причинное влияние оценивается по ходу и результату.

Её же словами, зачем эта граница: «Иначе мы легко построим новую красивую фикцию:
„навык присутствовал, следовательно Praxis научилась“. Нет.» И: «Мне нужен не бейдж
learning loop completed, а возможность через неделю открыть конкретную цепочку и честно
увидеть: вот прежняя ошибка, вот сформулированный мной навык, вот момент его возврата,
вот мой следующий выбор — и вот наблюдаемое различие.»

Поэтому здесь НЕТ ни одной функции, которая отвечала бы «научилась ли она». Есть только
две: что предложено и от чего отказались, и где урок был доступен. Вывод делает она.

Почему заметки не идут в автоматический recall целиком — тоже её решение и тоже записано:
«Заметка — это блокнот: сырая догадка, вопрос, полуфраза. Если каждая заметка начнёт
автоматически возвращаться как руководство к действию, мы просто заменим эхо context.md
эхом моего черновика.» Жанр повышается осознанно, её рукой.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
STATE_DIR = BASE / "memory" / ".state" / "lessons"
OFFERS = STATE_DIR / "offers.json"
AVAILABILITY = STATE_DIR / "availability.jsonl"

# Сколько записей доступности держим. Это наблюдательный журнал, а не канон: он отвечает
# на вопрос «был ли урок под рукой», и старые ответы дешевле новых.
AVAILABILITY_KEEP = int(os.environ.get("PRAXIS_LESSON_TRACE_KEEP", "20000") or 20000)

# Эвристика отбора кандидатов. Названа вслух и печатается в предложении, чтобы она могла
# с ней НЕ СОГЛАСИТЬСЯ: заметка похожа на урок, когда в ней есть нормативная формулировка
# (правило на будущее), а не только наблюдение. Это фильтр внимания, а не суждение.
_LESSON_MARKERS = re.compile(
    r"(должн\w*|следует|нужно|не стоит|впредь|в следующий раз|урок|правило|"
    r"вместо того чтобы|перед тем как|прежде чем|should |must |do not |never )",
    re.IGNORECASE)
_MIN_LESSON_CHARS = 120

HEURISTIC = ("нормативная формулировка (правило на будущее) в заметке длиннее "
             f"{_MIN_LESSON_CHARS} знаков")


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
                   encoding="utf-8")
    os.replace(tmp, path)


def _offers() -> dict[str, dict]:
    data = _read_json(OFFERS, {})
    return data if isinstance(data, dict) else {}


def looks_like_a_lesson(text: str) -> bool:
    """Похожа ли заметка на урок. Фильтр ВНИМАНИЯ, не суждение о ценности."""
    body = str(text or "").strip()
    return len(body) >= _MIN_LESSON_CHARS and bool(_LESSON_MARKERS.search(body))


# --------------------------------------------------------------------------- #
#  Предложение (и отказ, который держится)
# --------------------------------------------------------------------------- #

def pending(notes: list[dict], limit: int = 3) -> list[dict]:
    """Заметки-кандидаты, которым ещё не предлагали кристаллизацию.

    Отказ держится: заметка, от которой она отказалась, сюда больше не попадёт —
    иначе предложение превратилось бы в требование, повторяемое каждую ночь.
    """
    known = _offers()
    out = []
    for note in notes:
        note_id = str(note.get("id") or "")
        if not note_id or note_id in known:
            continue
        if not looks_like_a_lesson(note.get("text") or ""):
            continue
        out.append(note)
        if len(out) >= max(1, int(limit)):
            break
    return out


def offer(note_id: str, gist: str = "") -> dict:
    """Записать, что кристаллизация ПРЕДЛОЖЕНА. Ничего не создаёт и не меняет жанр."""
    note_id = str(note_id or "").strip()
    if not note_id:
        raise ValueError("нужен note_id")
    data = _offers()
    if note_id in data:
        return data[note_id]
    row = {"note_id": note_id, "status": "offered", "offered_at": time.time(),
           "gist": str(gist or "")[:400], "skill": "", "reason": "",
           "heuristic": HEURISTIC}
    data[note_id] = row
    _write_json(OFFERS, data)
    return row


def accept(note_id: str, skill_slug: str) -> dict:
    """Она сделала навык из этой заметки. Связь урок↔навык — единственное, что мы знаем."""
    data = _offers()
    row = data.get(str(note_id)) or {"note_id": str(note_id), "offered_at": time.time(),
                                     "gist": "", "heuristic": HEURISTIC}
    row.update({"status": "accepted", "skill": str(skill_slug or "").strip(),
                "accepted_at": time.time(), "reason": ""})
    data[str(note_id)] = row
    _write_json(OFFERS, data)
    return row


def decline(note_id: str, reason: str = "") -> dict:
    """Отказ — полноценный ответ, а не отложенное согласие: больше не предлагаем."""
    data = _offers()
    row = data.get(str(note_id)) or {"note_id": str(note_id), "offered_at": time.time(),
                                     "gist": "", "heuristic": HEURISTIC}
    row.update({"status": "declined", "declined_at": time.time(),
                "reason": str(reason or "")[:400], "skill": ""})
    data[str(note_id)] = row
    _write_json(OFFERS, data)
    return row


def origin_note(skill_slug: str) -> str:
    """Из какой заметки вырос навык ('' — связи не записано)."""
    slug = str(skill_slug or "").strip()
    for note_id, row in _offers().items():
        if row.get("status") == "accepted" and str(row.get("skill") or "") == slug:
            return note_id
    return ""


# --------------------------------------------------------------------------- #
#  Доступность (НЕ влияние)
# --------------------------------------------------------------------------- #

def record_availability(run_id: str, skills: list[str], *, chat_id: str = "",
                        query: str = "") -> int:
    """Записать, какие навыки БЫЛИ В КАДРЕ этого хода.

    ⚠ Это запись о доступности, и только о ней. Здесь сознательно нет ни поля «повлиял»,
    ни счётчика «сработал»: такого факта у нас нет и взять его неоткуда. Что урок был под
    рукой — наблюдаемо; что он изменил выбор — предмет её собственной оценки по ходу и
    результату, а не вывод из присутствия строки в промпте.
    """
    rows = [s for s in (str(x or "").strip() for x in (skills or [])) if s]
    if not rows or not str(run_id or "").strip():
        return 0
    AVAILABILITY.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.time()
    with AVAILABILITY.open("a", encoding="utf-8") as fh:
        for slug in rows:
            fh.write(json.dumps({
                "ts": stamp, "run_id": str(run_id), "skill": slug,
                "note_id": origin_note(slug), "chat_id": str(chat_id or ""),
                "query": str(query or "")[:160],
                "means": "навык был доступен в кадре; о влиянии это НЕ говорит",
            }, ensure_ascii=False) + "\n")
    _trim_availability()
    return len(rows)


def _trim_availability() -> None:
    try:
        lines = AVAILABILITY.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    if len(lines) <= AVAILABILITY_KEEP:
        return
    keep = lines[-AVAILABILITY_KEEP:]
    tmp = AVAILABILITY.with_name(AVAILABILITY.name + f".{os.getpid()}.tmp")
    tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
    os.replace(tmp, AVAILABILITY)


def availability(skill_slug: str = "", limit: int = 50) -> list[dict]:
    slug = str(skill_slug or "").strip()
    out = []
    try:
        lines = AVAILABILITY.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for raw in reversed(lines):
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if slug and str(row.get("skill") or "") != slug:
            continue
        out.append(row)
        if len(out) >= max(1, int(limit)):
            break
    return out


# --------------------------------------------------------------------------- #
#  Цепочка — то, что она просила открыть через неделю
# --------------------------------------------------------------------------- #

def chain(skill_slug: str, *, notes: dict[str, dict] | None = None,
          limit: int = 20) -> dict:
    """Собрать наблюдаемую цепочку вокруг навыка.

    Возвращает ФАКТЫ и ровно один вывод — которого здесь нет. Поле `caveat` не
    украшение: без него таблица «навык был в кадре 14 раз» читается как доказательство
    обучения, а она таким не является.
    """
    slug = str(skill_slug or "").strip()
    note_id = origin_note(slug)
    row = _offers().get(note_id) or {}
    note = (notes or {}).get(note_id) or {}
    seen = availability(slug, limit=limit)
    return {
        "skill": slug,
        "origin_note": {
            "id": note_id,
            "text": str(note.get("text") or row.get("gist") or ""),
            "written_at": str(note.get("created_at") or ""),
            "run_id": str(note.get("run_id") or ""),
        } if note_id else None,
        "crystallised": {
            "offered_at": row.get("offered_at"),
            "accepted_at": row.get("accepted_at"),
            "by": "она сама (write_skill)",
            "heuristic_that_surfaced_the_note": row.get("heuristic") or HEURISTIC,
        } if row.get("status") == "accepted" else None,
        "available_in_runs": seen,
        "available_count": len(seen),
        "caveat": ("Это запись о ДОСТУПНОСТИ урока, а не о его влиянии. "
                   "Присутствие навыка в кадре не означает, что он изменил выбор — "
                   "различие в поведении смотрится по самим ходам и результатам."),
    }
