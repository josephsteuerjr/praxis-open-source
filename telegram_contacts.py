"""Durable Telegram address book for natural, restart-proof addressing.

The Telethon session remains the authority for access hashes.  This file stores only
human routing metadata (id, names, aliases, recency), so Praxis can understand
"напиши маме/Евгению" without asking for a username on every restart.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path


BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
CONTACTS_PATH = BASE / "memory" / ".state" / "telegram_contacts.json"
_LOCK = threading.RLock()
_CACHE: dict[str, dict] | None = None


def _norm(value: str) -> str:
    text = str(value or "").casefold().replace("ё", "е").lstrip("@").strip()
    return re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).strip()


def _load() -> dict[str, dict]:
    global _CACHE
    with _LOCK:
        if _CACHE is not None:
            return _CACHE
        try:
            raw = json.loads(CONTACTS_PATH.read_text(encoding="utf-8"))
            rows = raw.get("contacts") if isinstance(raw, dict) else None
            _CACHE = {str(k): dict(v) for k, v in (rows or {}).items() if isinstance(v, dict)}
        except (OSError, ValueError, TypeError):
            _CACHE = {}
        return _CACHE


def save() -> None:
    with _LOCK:
        rows = _load()
        CONTACTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONTACTS_PATH.with_name(CONTACTS_PATH.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"version": 1, "contacts": rows}, ensure_ascii=False,
                                  indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, CONTACTS_PATH)


def reset_cache() -> None:
    global _CACHE
    with _LOCK:
        _CACHE = None


def _entity_fields(entity) -> tuple[str, str, str, list[str]] | None:
    ident = getattr(entity, "id", None)
    if ident is None:
        return None
    first = str(getattr(entity, "first_name", None) or "").strip()
    last = str(getattr(entity, "last_name", None) or "").strip()
    # Dialog groups/channels remain in the dialog cache; the address book is people.
    if not first and not last and getattr(entity, "title", None):
        return None
    display = " ".join(x for x in (first, last) if x).strip()
    display = display or str(getattr(entity, "name", None) or "").strip()
    username = str(getattr(entity, "username", None) or "").lstrip("@").strip()
    aliases = [display, first, last, username]
    for row in getattr(entity, "usernames", None) or ():
        if getattr(row, "active", True) and getattr(row, "username", None):
            aliases.append(str(row.username).lstrip("@"))
    return str(ident), display, username, aliases


def observe(entity, *, aliases=(), contact: bool = False, dialog: bool = False,
            interacted: bool = False, seen_at: float | None = None, persist: bool = True) -> dict | None:
    fields = _entity_fields(entity)
    if fields is None:
        return None
    ident, display, username, entity_aliases = fields
    with _LOCK:
        rows = _load()
        old = dict(rows.get(ident) or {})
        alias_set = {str(x).strip() for x in (old.get("aliases") or []) if str(x).strip()}
        alias_set.update(str(x).strip() for x in [*entity_aliases, *aliases] if str(x).strip())
        now = float(seen_at if seen_at is not None else time.time())
        row = {
            **old,
            "id": ident,
            "display_name": display or old.get("display_name", ""),
            "username": username or old.get("username", ""),
            "aliases": sorted(alias_set, key=lambda x: (_norm(x), x)),
            "contact": bool(contact or old.get("contact")),
            "dialog": bool(dialog or old.get("dialog")),
            "last_seen": max(float(old.get("last_seen") or 0), now),
            "interactions": int(old.get("interactions") or 0) + (1 if interacted else 0),
        }
        rows[ident] = row
    if persist:
        save()
    return row


def add_alias(ident: str | int, alias: str, *, persist: bool = True) -> None:
    key = str(ident)
    with _LOCK:
        rows = _load()
        row = dict(rows.get(key) or {"id": key, "aliases": [], "last_seen": 0,
                                     "interactions": 0, "contact": False, "dialog": False})
        aliases = {str(x).strip() for x in row.get("aliases") or [] if str(x).strip()}
        if str(alias or "").strip():
            aliases.add(str(alias).strip())
        row["aliases"] = sorted(aliases, key=lambda x: (_norm(x), x))
        rows[key] = row
    if persist:
        save()


def mark_outbound(ident: str | int, *, idempotency_key: str = "",
                  at: float | None = None) -> None:
    key = str(ident)
    receipt = str(idempotency_key or "").strip()
    with _LOCK:
        rows = _load()
        row = rows.get(key)
        if not row:
            return
        applied = [str(value) for value in row.get("outbound_receipts") or ()]
        if receipt and receipt in applied:
            return
        row["last_outbound"] = float(at if at is not None else time.time())
        row["interactions"] = int(row.get("interactions") or 0) + 1
        if receipt:
            applied.append(receipt)
            row["outbound_receipts"] = applied[-200:]
    save()


def candidates(query: str, limit: int = 8) -> list[dict]:
    q = _norm(query)
    if not q:
        return []
    q_tokens = set(q.split())
    ranked: list[tuple[float, dict]] = []
    now = time.time()
    for row in _load().values():
        aliases = [row.get("display_name", ""), row.get("username", ""), *(row.get("aliases") or [])]
        norms = {_norm(x) for x in aliases if _norm(x)}
        if q == _norm(str(row.get("id") or "")):
            lexical = 200
        elif q in norms:
            lexical = 140
        elif any(q_tokens and q_tokens <= set(alias.split()) for alias in norms):
            lexical = 105
        elif any(alias.startswith(q) for alias in norms):
            lexical = 90
        elif any(q in alias for alias in norms):
            lexical = 75
        else:
            continue
        age_days = max(0.0, (now - float(row.get("last_seen") or 0)) / 86400)
        recency = max(0.0, 20.0 - min(20.0, age_days / 7.0))
        score = (lexical + (15 if row.get("contact") else 0) +
                 (10 if row.get("dialog") else 0) + min(12, int(row.get("interactions") or 0)) +
                 recency + (8 if row.get("last_outbound") else 0))
        ranked.append((score, {**row, "score": round(score, 3)}))
    ranked.sort(key=lambda item: (-item[0], -float(item[1].get("last_outbound") or 0),
                                  -float(item[1].get("last_seen") or 0), item[1].get("id", "")))
    return [row for _, row in ranked[:max(1, int(limit or 8))]]


def count() -> int:
    return len(_load())
