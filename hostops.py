"""
hostops — её сторона правок хоста (PASS 17.C). Она умеет ровно ОДНО: СТЕЙДЖИТЬ заявку.

Применяет правку не она, а hostagent на хосте — и только когда Егор сам набрал `apply`.
Здесь — сборка заявки, зеркало allowlist (чтобы отказать сразу, не гоняя Егора зря) и
чтение расписок (исход своих заявок она видит, как и по остальной лестнице).

ВАЖНО про доверие: allowlist ниже — ЗЕРКАЛО для UX, не гейт. Настоящий забор — в hostagent
на хосте (её контейнер его не видит). Даже если она подделает этот список, hostagent
проверит путь своим зашитым HOST_ALLOW и всё равно откажет. Два списка сверяются в тестах.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import uuid
from pathlib import Path

log = logging.getLogger("praxis-hostops")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
STAGE_DIR = BASE / "memory" / ".host_ops"
RECEIPTS = BASE / "memory" / ".state" / "hostops.jsonl"

# Зеркало hostagent.HOST_ALLOW — держится синхронно тестом test_allowlist_mirrors_hostagent.
# Пусто у hostagent = она реально ничего не может; здесь тот же узкий список для мгновенного «нет».
HOST_ALLOW_MIRROR: tuple[str, ...] = (
    "/etc/praxis-hostagent/test.conf",
    # "/etc/caddy/Caddyfile",
)
MAX_CONTENT_BYTES = 256 * 1024


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _norm(path: str) -> str:
    return os.path.normpath(str(path or "").strip())


def allowed(path: str) -> str:
    """'' если путь можно предлагать; иначе причина (зеркало рельсов hostagent)."""
    p = _norm(path)
    if not p or not os.path.isabs(p):
        return "нужен абсолютный путь хоста, например /etc/caddy/Caddyfile"
    if p not in HOST_ALLOW_MIRROR:
        return (f"{p} мне править нельзя. Разрешены: {', '.join(HOST_ALLOW_MIRROR) or '(ничего)'}. "
                "Список расширяет только Егор — правкой hostagent на хосте.")
    return ""


def stage(path: str, content: str, reason: str = "") -> tuple[str, str]:
    """Записать заявку. -> (id|'', сообщение ей). Применит её потом Егор на хосте."""
    path = _norm(path)
    problem = allowed(path)
    if problem:
        return "", problem
    if not content:
        return "", "Пустой контент — заявку не создаю (для удаления файла руки не даю)."
    if len(content.encode("utf-8", "replace")) > MAX_CONTENT_BYTES:
        return "", "Контент больше лимита — так не стейджу."
    oid = uuid.uuid4().hex[:8]
    try:
        STAGE_DIR.mkdir(parents=True, exist_ok=True)
        (STAGE_DIR / f"{oid}.content").write_text(content, encoding="utf-8")
        (STAGE_DIR / f"{oid}.json").write_text(json.dumps({
            "id": oid, "path": path, "reason": (reason or "").strip(),
            "status": "pending", "created": _now(),
        }, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        return "", f"Не смогла записать заявку: {e}"
    log.info("host-op staged %s -> %s", oid, path)
    return oid, (f"Заявка {oid} на правку {path} готова и ушла Егору карточкой. "
                 "Сам файл я не трогаю — применит он на хосте, посмотрев дифф. "
                 "Исход увидишь в list_host_changes.")


def pending() -> list[dict]:
    if not STAGE_DIR.is_dir():
        return []
    out = []
    for meta in sorted(STAGE_DIR.glob("*.json")):
        try:
            d = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("status") == "pending":
            out.append(d)
    return out


def unnotified() -> list[dict]:
    """Заявки, о которых Егору ещё не отправляли карточку (для вотчера mailbot)."""
    return [d for d in pending() if not d.get("notified")]


def mark_notified(oid: str) -> None:
    meta = STAGE_DIR / f"{oid}.json"
    try:
        d = json.loads(meta.read_text(encoding="utf-8"))
        d["notified"] = True
        meta.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError):
        log.debug("mark_notified %s не удалось", oid, exc_info=True)


def card_text(op: dict, content_preview: str = "") -> str:
    """Текст карточки Егору. Без кнопок: апрув — команда на хосте (её процесс его не подделает)."""
    oid = op.get("id", "?")
    return ("🖥 Заявка на правку ХОСТА от Praxis (применяешь ты, на хосте)\n"
            f"#{oid} · {op.get('path')}\n"
            f"Зачем: {op.get('reason') or '—'}\n\n"
            + (f"Превью:\n{content_preview[:900]}\n\n" if content_preview else "")
            + "Посмотреть дифф и применить — на хосте:\n"
            f"  hostagent show {oid}\n"
            f"  hostagent apply {oid}      (или reject {oid} причина)\n"
            "Я сам файл не трогаю — это твоя команда, не моя.")


def _receipts(limit: int = 12) -> list[dict]:
    if not RECEIPTS.is_file():
        return []
    rows = []
    for line in RECEIPTS.read_text(encoding="utf-8", errors="ignore").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def describe() -> str:
    """Список её заявок и их судьба — для тула list_host_changes."""
    pend = pending()
    rec = _receipts()
    out = []
    if pend:
        out.append("Ждут решения Егора (на хосте):")
        for d in pend:
            out.append(f"  {d['id']} · {d.get('path')} · {(d.get('reason') or '—')[:70]}")
    else:
        out.append("Заявок в очереди нет.")
    if rec:
        out.append("\nПоследние решения:")
        for r in rec:
            mark = "применено" if r.get("ok") else "отклонено/отказ"
            out.append(f"  {'✓' if r.get('ok') else '✗'} {r.get('path')}: {mark}"
                       + (f" — {r.get('note','')[:80]}" if r.get("note") else ""))
    out.append(f"\nЧто мне вообще разрешено предлагать: {', '.join(HOST_ALLOW_MIRROR) or '(ничего)'}.")
    return "\n".join(out)


def state_line() -> str:
    """Строка для my_capabilities: что она может на самом верху лестницы."""
    allow = ", ".join(HOST_ALLOW_MIRROR) or "ничего (пустой allowlist)"
    return ("правки хоста: только СТЕЙДЖУ заявку, применяет Егор на хосте своей командой "
            f"(мой контейнер к хосту не пишет); разрешено предлагать: {allow}")
