"""PASS 30 Этап 2 — наррация по ходу: короткие сообщения в тред между командами.

Её собственное желание («рассказывала бы, как идёт процесс») и просьба Егора
совпали. Класс narration — ЛЁГКИЙ исходящий: гейт — только твёрдые полы (креды
механически не текут — закон 3; больше НИЧЕГО: ни трибунала, ни анти-повтора-
оценщика — они убивали готовые доставки, см. privacy-kill 21.07). Дедуп по
содержимому (прогресс-сообщения похожи по природе — это не повод молчать, но
дословный повтор в тот же тред бессмыслен). Частота (зазор) и выключатель — ЕЁ:
perception knob narration_gap_sec + PRAXIS_NARRATION. Стиль — её текст как есть.

Этот модуль — чистая механика (без импорта agent): леджер, дедуп, зазор, кред-пол.
Отправку делает тул narrate в agent.py тем же durable direct-outbox путём, что и
send_message (топики поддержаны селектором <peer>#topic:<id>).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

log = logging.getLogger("praxis-narration")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent.parent)
LEDGER = BASE / "memory" / ".state" / "narration.jsonl"

KEEP = 200               # кольцо леджера (как turns)
TEXT_CAP = 800           # наррация — короткая строка процесса, не отчёт
DEDUP_WINDOW_SEC = 2 * 3600.0

_LOCK = threading.Lock()


def enabled() -> bool:
    """Выключатель класса — её (и Егора через env). Живое чтение в точке вызова."""
    value = (os.getenv("PRAXIS_NARRATION", "1") or "1").strip().lower()
    return value not in {"0", "off", "false", "no"}


def credential_floor(text: str) -> str:
    """'' если чисто; иначе метка похожего на кред паттерна (пол, не совет).

    Общий пол вынесен в core.secrets — им же пользуется outbound-гард голоса
    (диагностика 23.07): один список, а не два расходящихся."""
    from core import secrets
    return secrets.credential_floor(text)


def _norm_key(dest: str, text: str) -> str:
    norm = re.sub(r"\s+", " ", str(text or "").strip().lower())
    return hashlib.sha256(f"{dest}|{norm}".encode("utf-8")).hexdigest()[:16]


def _load() -> list[dict]:
    out: list[dict] = []
    try:
        with LEDGER.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                    if isinstance(d, dict) and d.get("ts"):
                        out.append(d)
                except Exception:
                    continue
    except OSError:
        pass
    return out


def is_duplicate(dest: str, text: str, now: float | None = None) -> bool:
    """Дословный (нормализованный) повтор в тот же тред в окне DEDUP_WINDOW_SEC."""
    key = _norm_key(dest, text)
    cut = (now if now is not None else time.time()) - DEDUP_WINDOW_SEC
    with _LOCK:
        return any(r.get("key") == key and float(r.get("ts") or 0) >= cut
                   for r in _load())


def gap_remaining(dest: str, gap_sec: float, now: float | None = None) -> float:
    """Сколько секунд осталось до следующей наррации в этот тред (0 = можно)."""
    if gap_sec <= 0:
        return 0.0
    now = now if now is not None else time.time()
    with _LOCK:
        last = max((float(r.get("ts") or 0) for r in _load()
                    if str(r.get("dest") or "") == str(dest)), default=0.0)
    return max(0.0, last + float(gap_sec) - now)


def note(dest: str, text: str, now: float | None = None,
         delivery: str = "accepted") -> None:
    """Записать наррацию с ЕЁ ИСХОДОМ (кольцо; никогда не бросает).

    `delivery` — единственная честная разница между «ушло» и «поставлено в очередь».
    Раньше оба случая писались одинаково, и наррация, оставшаяся в durable-очереди,
    навсегда числилась состоявшейся: `is_duplicate` глушил повтор два часа даже если
    очередь потом умирала в dead-letter.

    Дедуп при этом СОЗНАТЕЛЬНО учитывает и `pending`: идемпотентность прямого аутбокса
    привязана к call_id, поэтому повторный narrate тем же текстом создал бы ВТОРУЮ
    запись очереди и после переподключения ушёл бы дважды. То есть запись обязана
    существовать — врать она не обязана. Читатели (`recent`, дневник) видят исход.
    """
    try:
        rec = {"ts": now if now is not None else time.time(),
               "dest": str(dest), "key": _norm_key(dest, text),
               "delivery": str(delivery or "accepted"),
               "gist": re.sub(r"\s+", " ", str(text or ""))[:120]}
        with _LOCK:
            LEDGER.parent.mkdir(parents=True, exist_ok=True)
            rows = _load()
            rows.append(rec)
            if len(rows) > KEEP * 2:
                rows = rows[-KEEP:]
                tmp = LEDGER.with_suffix(".jsonl.tmp")
                tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                                       for r in rows), encoding="utf-8")
                tmp.replace(LEDGER)
            else:
                with LEDGER.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        log.debug("narration.note не записалась", exc_info=True)


def resolve(dest: str, text: str, *, delivery: str = "accepted") -> bool:
    """Довести запись наррации до фактического исхода. -> нашлась ли она.

    Без этого `pending` оставался бы навсегда: наррация, поставленная в durable-очередь
    и потом успешно доставленная, вечно числилась бы недоставленной. Это ровно та же
    ложь, что и «сказала» до приёмки, только вывернутая наизнанку, и заводить её вместо
    прежней не было смысла."""
    key = _norm_key(dest, text)
    try:
        with _LOCK:
            rows = _load()
            hit = None
            for row in reversed(rows):
                if row.get("key") == key:
                    hit = row
                    break
            if hit is None:
                return False
            if hit.get("delivery") == delivery:
                return True
            hit["delivery"] = str(delivery or "accepted")
            tmp = LEDGER.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                           encoding="utf-8")
            tmp.replace(LEDGER)
            return True
    except Exception:
        log.debug("исход наррации не записался", exc_info=True)
        return False


def recent(n: int = 5, dest: str | None = None) -> list[dict]:
    with _LOCK:
        rows = [r for r in _load() if dest is None or str(r.get("dest")) == str(dest)]
    return rows[-max(0, int(n)):]
