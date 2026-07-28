"""
Praxis — кто есть кто и видимая статистика первых контактов.

Три категории (без легаси-иерархии): owner / known / unknown.
- owner   — `==PRAXIS_OWNER_ID`.
- known   — id есть в `memory/known_ids.json` (впущенные владельцем).
- unknown — все прочие.

`memory/.unknown_counts.json` помнит первый контакт и объём входящего потока. Это
наблюдаемость, не admission gate: незнакомец попадает в обычный разговор без owner-апрува
и без скрытого дневного cap; собственную границу Praxis ставит `freeze_contact`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
MEM_DIR = BASE / "memory"
KNOWN_IDS = MEM_DIR / "known_ids.json"
UNKNOWN_COUNTS = MEM_DIR / ".unknown_counts.json"


def owner_id() -> str:
    """Читать лениво: на импорте .env может быть ещё не загружен (был баг — owner стал unknown)."""
    return os.getenv("PRAXIS_OWNER_ID", "0")


# --------------------------------------------------------------------------- #
#  known_ids
# --------------------------------------------------------------------------- #

def _load_known() -> dict:
    try:
        d = json.loads(KNOWN_IDS.read_text(encoding="utf-8"))
        return {str(k): v for k, v in d.items()} if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_known(d: dict) -> None:
    MEM_DIR.mkdir(parents=True, exist_ok=True)
    KNOWN_IDS.write_text(json.dumps(d, ensure_ascii=False, indent=0), encoding="utf-8")


def known_ids() -> dict:
    return _load_known()


def category(sender_id: str | int) -> str:
    sid = str(sender_id)
    oid = owner_id()
    if oid and oid != "0" and sid == oid:
        return "owner"
    if sid in _load_known():
        return "known"
    return "unknown"


def role_of(sender_id: str | int) -> str:
    """PASS 10.10: owner | family | known | unknown.

    family = известный id, чьё досье (по имени, данному владельцем при впуске) несёт
    `role: family`. Цепочка целиком владельческая: id впускает он, имя в known_ids даёт
    он, роль в файле ставит он — самоназвание «мама» family не делает."""
    cat = category(sender_id)
    if cat != "known":
        return cat
    name = _load_known().get(str(sender_id)) or ""
    if not name.strip():
        return "known"
    try:
        import people
        if people.role(people._slug(name)) == "family":
            return "family"
    except Exception:
        pass
    return "known"


def is_family(sender_id: str | int) -> bool:
    return role_of(sender_id) == "family"


def add_known(telegram_id: str | int, name: str) -> bool:
    """Впустить id как known. -> True, если это новый id."""
    d = _load_known()
    key = str(telegram_id)
    existed = key in d
    d[key] = name
    _save_known(d)
    return not existed


# --------------------------------------------------------------------------- #
#  first-contact / traffic state (наблюдаемость, не gate)
# --------------------------------------------------------------------------- #

def _load_counts() -> dict:
    try:
        d = json.loads(UNKNOWN_COUNTS.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_counts(d: dict) -> None:
    MEM_DIR.mkdir(parents=True, exist_ok=True)
    UNKNOWN_COUNTS.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def note_unknown(sender_id: str | int, today: str) -> tuple[bool, int]:
    """Зарегистрировать сообщение от unknown.

    -> (first_contact_ever, count_today_после_инкремента).
    `first_contact_ever` True ровно один раз на id (для её журнала/адресной книги).
    """
    d = _load_counts()
    key = str(sender_id)
    rec = d.get(key) or {}
    first = not rec.get("first")
    if first:
        rec["first"] = today
    if rec.get("day") != today:
        rec["day"] = today
        rec["count"] = 0
    rec["count"] = int(rec.get("count", 0)) + 1
    d[key] = rec
    _save_counts(d)
    return first, rec["count"]


def admission_check(sender_id: str | int, today: str, cap: int) -> dict:
    """Legacy helper для старых тестов/миграций; живой раннер больше не режет unknown по cap."""
    first, count = note_unknown(sender_id, today)
    return {"first": first, "count": count, "over_cap": count > cap}
