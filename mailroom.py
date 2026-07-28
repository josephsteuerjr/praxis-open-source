"""
Praxis mailroom — толстый клиент почты: поллинг, хэши, хранилище. Stdlib + mailer.

Идея: отделить её «тонкий» дорогой голос от тупой-дешёвой почтовой комнаты. Mailroom
поллит IMAP (через mailer.fetch), каждому НОВОМУ письму присваивает короткий хэш, хранит
`memory/mailbox.json`, и шлёт SMTP только по одобрению Егора. Токенов LLM не тратит.

Состояние письма (`status`):
    new       — пришло, ждёт её черновика
    drafted   — она составила ответ (mail_draft_reply), ждёт approve Егора
    approved  — Егор одобрил из mini-app/кнопкой → отправитель подхватит и зашлёт
    sent      — отправлено по SMTP
    rejected  — Егор отклонил черновик

Её тонкие тулы (в agent.py): `mail_read(hash)` — тело письма по хэшу (только то, что он дал);
`mail_draft_reply(hash, body)` — ставит черновик (status=drafted). НЕ отправляет — это делает
mailroom по approve. Так она остаётся тонкой и безопасной (human-in-the-loop на отправке).

Честный индекс `index_block()` инжектится ей в контекст (хартбит/таск-окно/owner-личка): она
ЗНАЕТ, что в ящике, не тратя tool-call на «проверить». Тул — только чтобы ДЕЙСТВОВАТЬ.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import logging
import os
import re
from pathlib import Path

import mailer

log = logging.getLogger("praxis-mailroom")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
MAILBOX = BASE / "memory" / "mailbox.json"

HASH_LEN = 4  # короткий base32 (32^4 ≈ 1M) — не слишком длинный, коллизии редки и обрабатываются
OPEN_STATUSES = ("new", "drafted", "approved")  # «в ящике» для индекса
_B32 = "abcdefghijklmnopqrstuvwxyz234567"  # base32 без padding, нижний регистр


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="minutes")


def _load() -> dict:
    try:
        d = json.loads(MAILBOX.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(box: dict) -> None:
    MAILBOX.parent.mkdir(parents=True, exist_ok=True)
    tmp = MAILBOX.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(box, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(MAILBOX)


def _dedup_key(msg: dict) -> str:
    """Стабильный ключ письма для дедупа между поллами: Message-ID или хэш заголовков+тела."""
    mid = (msg.get("msg_id") or "").strip()
    if mid:
        return mid
    raw = "|".join([msg.get("from", ""), msg.get("subject", ""),
                    msg.get("date", ""), (msg.get("body", "") or "")[:200]])
    return "sha:" + hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]


def _new_hash(box: dict) -> str:
    used = set(box.keys())
    for _ in range(64):
        h = "".join(_B32[b % 32] for b in os.urandom(HASH_LEN))
        if h not in used:
            return h
    # практически недостижимо; деградируем на длинный хэш
    return base64.b32encode(os.urandom(5)).decode("ascii").lower()[:HASH_LEN + 2]


def _short_from(frm: str) -> str:
    """«Имя <a@b>» -> короткое читаемое (имя или адрес)."""
    frm = (frm or "").strip()
    m = re.match(r"\s*\"?([^\"<]+?)\"?\s*<", frm)
    if m and m.group(1).strip():
        return m.group(1).strip()[:32]
    m = re.search(r"<([^>]+)>", frm)
    if m:
        return m.group(1).strip()[:32]
    return frm[:32] or "?"


def ingest(messages: list[dict]) -> list[dict]:
    """Завести НОВЫЕ письма (дедуп по Message-ID). -> список добавленных записей (для уведомления).

    messages — как из mailer.fetch(): [{from, subject, date, body, msg_id}].
    """
    box = _load()
    seen = {e.get("key") for e in box.values()}
    added = []
    for msg in messages or []:
        key = _dedup_key(msg)
        if key in seen:
            continue
        h = _new_hash(box)
        entry = {
            "hash": h,
            "key": key,
            "from": (msg.get("from") or "").strip(),
            "subject": (msg.get("subject") or "").strip() or "(без темы)",
            "body": (msg.get("body") or "").strip(),
            "date": (msg.get("date") or "").strip(),
            "msg_id": (msg.get("msg_id") or "").strip(),
            "status": "new",
            "draft": "",
            "added": _now(),
        }
        box[h] = entry
        seen.add(key)
        added.append(entry)
    if added:
        _save(box)
    return added


def get(h: str) -> dict | None:
    return _load().get((h or "").strip().lower())


def set_draft(h: str, body: str) -> bool:
    """Её черновик ответа: status -> drafted, ждёт approve Егора."""
    h = (h or "").strip().lower()
    box = _load()
    e = box.get(h)
    if not e or e.get("status") == "sent":
        return False
    e["draft"] = (body or "").strip()
    e["status"] = "drafted"
    e["drafted_at"] = _now()
    _save(box)
    return True


def set_status(h: str, status: str) -> bool:
    h = (h or "").strip().lower()
    box = _load()
    e = box.get(h)
    if not e:
        return False
    e["status"] = status
    e[f"{status}_at"] = _now()
    _save(box)
    return True


def approve(h: str) -> bool:
    """Approve Егора из mini-app/кнопкой: drafted -> approved (отправитель подхватит)."""
    e = get(h)
    if not e or e.get("status") != "drafted" or not (e.get("draft") or "").strip():
        return False
    return set_status(h, "approved")


def reject(h: str) -> bool:
    e = get(h)
    if not e or e.get("status") not in ("drafted", "new"):
        return False
    return set_status(h, "rejected")


def mark_sent(h: str) -> bool:
    return set_status(h, "sent")


def pending_approved() -> list[dict]:
    """Одобренные черновики, готовые к SMTP-отправке (для цикла-отправителя)."""
    return [e for e in _load().values() if e.get("status") == "approved" and (e.get("draft") or "").strip()]


def list_open() -> list[dict]:
    """«В ящике»: new/drafted/approved, новые сверху."""
    items = [e for e in _load().values() if e.get("status") in OPEN_STATUSES]
    items.sort(key=lambda e: e.get("added", ""), reverse=True)
    return items


def reply_address(h: str) -> str:
    """Адрес для ответа на письмо по хэшу (из заголовка From)."""
    e = get(h)
    if not e:
        return ""
    frm = e.get("from", "")
    m = re.search(r"<([^>]+)>", frm)
    return (m.group(1) if m else frm).strip()


def send_approved(h: str) -> str:
    """По approve Егора: SMTP-отправить черновик на адрес-ответ письма, пометить sent. Token-free.

    Это «толстый» путь отправки — никакого LLM. -> человекочитаемый статус."""
    e = get(h)
    if not e:
        return f"Нет письма с хэшем {h}."
    draft = (e.get("draft") or "").strip()
    if not draft:
        return "Нет черновика для отправки."
    if e.get("status") == "sent":
        return "Это письмо уже отправлено."
    to = reply_address(h)
    if "@" not in to:
        return f"Не разобрала адрес отправителя: {to!r}."
    subj = (e.get("subject") or "").strip()
    if not subj.lower().startswith("re:"):
        subj = "Re: " + subj if subj else "Re:"
    out = mailer.send(to, subj, draft)
    if out.startswith("Отправлено"):
        mark_sent(h)
    else:
        log.warning("send_approved %s не ушло: %s", h, out)
    return out


def index_block(max_items: int = 12, *, exclude_hashes: set[str] | None = None) -> str:
    """Честный компактный индекс ящика для её контекста (хэш · от · тема · статус).

    Она видит это всегда-свежим и потому ДЕЙСТВУЕТ тулом (mail_read/mail_draft_reply),
    а не ПРОВЕРЯЕТ тулом. Пусто — вернуть '' (блок не инжектится)."""
    excluded = exclude_hashes or set()
    items = [entry for entry in list_open() if entry.get("hash") not in excluded][:max_items]
    if not items:
        return ""
    lines = []
    for e in items:
        subj = (e.get("subject") or "")[:60]
        lines.append(f"- {e['hash']} · {_short_from(e.get('from',''))} · «{subj}» · {e.get('status')}")
    return "\n".join(lines)
