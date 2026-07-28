"""PASS 30 Этап 1 — журнал событий: завершение работы возвращается в её петлю.

Схема praxis.event.v1: {schema, id, ts, kind, source, payload, dedup_key}.

Два разных гаранта, две разных механики:
- ЗАПИСЬ (emit) — межпроцессная: пишут и раннер, и детачнутые Forge-процессы
  (воркер сигналит своё завершение сам, из своего процесса). Поэтому O_APPEND +
  ОДНА os.write-строка + fsync (образец run_manager._append_jsonl); никакой
  буферизованной 'a'-записи (антиобразец forge._event). emit никогда не бросает:
  падение журнала не имеет права уронить завершающийся воркер.
- ДОСТАВКА (undelivered/bump_attempts/mark_delivered) — однопроцессная (только
  раннер), идемпотентная по dedup_key, at-least-once с капом. Порядок обязателен:
  durable append (продюсер) → взят _ONE_MIND и мозг жив → bump_attempts
  (крашеустойчивый счёт) → ход → ТОЛЬКО ПОТОМ mark_delivered. Это закрывает гэп
  старого wake-пути с обеих сторон: пометка до хода при мёртвом мозге/краше
  съедала бы завершение навсегда, а безлимитный повтор ядовитого события молотил
  бы её вечно (кап MAX_DELIVERY_ATTEMPTS, гашение громкое, не тихое).

Компакт: только раннер, редкий (порог 4×KEEP), последними KEEP строками, атомарно.
Кросс-процессное окно гонки при компакте (чужой fd на старом inode) микроскопично
и лечится реконсайлером Forge — событие будет сфабриковано заново по result.json.
Ключи delivered живут, пока их событие живо в журнале (иначе чистятся).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger("praxis-core-events")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent.parent)
STATE_DIR = BASE / "memory" / ".state"
JOURNAL = STATE_DIR / "core_events.jsonl"
DELIVERED = STATE_DIR / "core_events_delivered.json"

SCHEMA = "praxis.event.v1"
KEEP = 400                 # компакт журнала: файл >4×KEEP строк → последние KEEP
PAYLOAD_CHARS = 6000       # потолок сериализованного payload НА ЗАПИСИ (как turns._clip)

_LOCK = threading.Lock()   # внутрипроцессная сериализация; межпроцессность — O_APPEND


def enabled() -> bool:
    """Её выключатель источника: PRAXIS_FORGE_EVENTS=0 глушит доставку (журнал живёт).
    Читается живо в точке вызова (образец promises.enabled; не module-level)."""
    value = (os.getenv("PRAXIS_FORGE_EVENTS", "1") or "1").strip().lower()
    return value not in {"0", "off", "false", "no"}


def _clip_payload(payload: dict) -> dict:
    """Потолок на записи: событие — сигнал со ссылками, не контейнер артефактов."""
    try:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return {"unserializable": str(payload)[:200]}
    if len(raw) <= PAYLOAD_CHARS:
        return payload
    clipped = dict(payload)
    # длинные строковые поля режем по очереди, пока не влезет
    for key in sorted(clipped, key=lambda k: -len(str(clipped.get(k) or ""))):
        if isinstance(clipped.get(key), str) and len(clipped[key]) > 200:
            clipped[key] = clipped[key][:200] + "…"
        raw = json.dumps(clipped, ensure_ascii=False, separators=(",", ":"))
        if len(raw) <= PAYLOAD_CHARS:
            return clipped
    return {"clipped": raw[:PAYLOAD_CHARS]}


def emit(kind: str, source: str, payload: dict | None = None,
         dedup_key: str = "", ts: float | None = None) -> dict | None:
    """Записать событие durable (кросс-процессно). -> событие или None. НИКОГДА не бросает.

    ts обязателен в записи (квирк-урок: result.json без finished вечно «свежий»);
    dedup_key — ключ идемпотентной ДОСТАВКИ (дубли строк в журнале допустимы,
    дедуп на потребителе)."""
    try:
        now = float(ts) if ts else time.time()
        event = {
            "schema": SCHEMA,
            "id": f"evt-{int(now * 1000):x}-{uuid.uuid4().hex[:8]}",
            "ts": now,
            "kind": str(kind or "").strip() or "unknown",
            "source": str(source or "").strip() or "unknown",
            "payload": _clip_payload(payload or {}),
            "dedup_key": str(dedup_key or "").strip(),
        }
        line = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with _LOCK:
            JOURNAL.parent.mkdir(parents=True, exist_ok=True)
            # Оборванный хвост без \n (краш посреди записи) не должен склеить и убить
            # СЛЕДУЮЩЕЕ событие — превентивный перевод строки. Гонка двух процессов
            # даёт максимум пустую строку (читатель её пропускает).
            try:
                with JOURNAL.open("rb") as fh:
                    fh.seek(-1, os.SEEK_END)
                    if fh.read(1) not in (b"\n", b""):
                        line = b"\n" + line
            except OSError:
                pass  # файла ещё нет / пустой
            fd = os.open(str(JOURNAL), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)
        return event
    except Exception:
        log.warning("core.events.emit не записался (%s/%s)", kind, dedup_key, exc_info=True)
        return None


def _read_journal() -> list[dict]:
    out: list[dict] = []
    try:
        with JOURNAL.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                    if isinstance(d, dict) and d.get("ts"):
                        out.append(d)
                except Exception:
                    continue  # оборванный хвост/битая строка — не слепим весь журнал
    except OSError:
        pass
    return out


MAX_DELIVERY_ATTEMPTS = 2   # ядовитое событие (ход падает/крашится) не молотит её вечно


def _load_state() -> dict:
    """{"delivered": {key: ts}, "attempts": {key: n}}; легаси-плоский dict = delivered."""
    try:
        d = json.loads(DELIVERED.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            return {"delivered": {}, "attempts": {}}
        if "delivered" in d or "attempts" in d:
            return {"delivered": dict(d.get("delivered") or {}),
                    "attempts": dict(d.get("attempts") or {})}
        return {"delivered": d, "attempts": {}}
    except Exception:
        return {"delivered": {}, "attempts": {}}


def _load_delivered() -> dict:
    return _load_state()["delivered"]


def _save_state(state: dict) -> None:
    DELIVERED.parent.mkdir(parents=True, exist_ok=True)
    tmp = DELIVERED.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(DELIVERED)


def _key_of(event: dict) -> str:
    return str(event.get("dedup_key") or event.get("id") or "")


def known_keys() -> set:
    """Все ключи журнала (доставленные и нет) — для реконсайлера («уже есть событие?»)."""
    with _LOCK:
        return {_key_of(e) for e in _read_journal() if _key_of(e)}


def undelivered(kinds: set | None = None, limit: int = 50) -> list[dict]:
    """Недоставленные события (по одному на dedup_key, старые → новые). Только раннер."""
    with _LOCK:
        delivered = _load_delivered()
        seen_here: set = set()
        out: list[dict] = []
        for e in _read_journal():
            key = _key_of(e)
            if not key or key in delivered or key in seen_here:
                continue
            if kinds and e.get("kind") not in kinds:
                continue
            seen_here.add(key)
            out.append(e)
        return out[:max(1, int(limit))]


def mark_delivered(keys) -> None:
    """Пометить доставленными — ПОСЛЕ того, как ход реально прожит (или событие
    осознанно тихое/ядовитое). НЕ до модели: пометка до хода при мёртвом мозге
    съедала бы завершение навсегда (урок скептиков Этапа 1)."""
    keys = [str(k) for k in (keys or []) if k]
    if not keys:
        return
    with _LOCK:
        state = _load_state()
        now = time.time()
        for k in keys:
            state["delivered"][k] = now
            state["attempts"].pop(k, None)
        _save_state(state)


def bump_attempts(keys) -> dict:
    """Durable-счёт попыток доставки ДО хода (крашеустойчиво). -> {key: n}.

    Краш посреди хода не теряет событие (оно не delivered) и не молотит её вечно:
    ключ с n > MAX_DELIVERY_ATTEMPTS насос помечает delivered ГРОМКО (лог), не тихо."""
    keys = [str(k) for k in (keys or []) if k]
    if not keys:
        return {}
    with _LOCK:
        state = _load_state()
        out = {}
        for k in keys:
            state["attempts"][k] = int(state["attempts"].get(k) or 0) + 1
            out[k] = state["attempts"][k]
        _save_state(state)
        return out


def compact() -> None:
    """Редкий компакт (только раннер): журнал → последние KEEP строк; delivered —
    только ключи, чьё событие ещё в журнале (недоставленные НЕ выбрасываются)."""
    with _LOCK:
        try:
            n = 0
            with JOURNAL.open(encoding="utf-8") as fh:
                for n, _ in enumerate(fh, 1):
                    pass
        except OSError:
            return
        if n <= KEEP * 4:
            return
        rows = _read_journal()
        delivered = _load_delivered()
        keep: list[dict] = []
        for e in rows[-KEEP:]:
            keep.append(e)
        # недоставленное старше окна не теряем — переносим в хвост
        kept_keys = {_key_of(e) for e in keep}
        for e in rows[:-KEEP]:
            key = _key_of(e)
            if key and key not in delivered and key not in kept_keys:
                keep.insert(0, e)
                kept_keys.add(key)
        tmp = JOURNAL.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n"
                               for e in keep), encoding="utf-8")
        tmp.replace(JOURNAL)
        state = _load_state()
        _save_state({"delivered": {k: v for k, v in state["delivered"].items() if k in kept_keys},
                     "attempts": {k: v for k, v in state["attempts"].items() if k in kept_keys}})


def state_line() -> str:
    """Одна строка для наблюдаемости (capabilities): журнал/недоставлено."""
    try:
        with _LOCK:
            rows = _read_journal()
            delivered = _load_delivered()
        waiting = sum(1 for e in rows if _key_of(e) and _key_of(e) not in delivered)
        if not rows:
            return ""
        return f"события: журнал {len(rows)}, ждут доставки {waiting}"
    except Exception:
        return ""
