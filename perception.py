"""
Praxis — восприятие как её орган (PASS 21): рычаги вместо хардкода, причины пропуска видимы.

Хартия §6: ничто не держит меня насильно; дебаунс, кулдауны и opt-in порог ACK-шума —
видимые настраиваемые механические рычаги (manage_perception), а не моральные фильтры.
Env остаётся дефолтом (слово Егора при старте), мой персист лежит поверх и читается ЖИВО в
точке решения — смена темпа не требует рестарта. У каждого рычага — широкие физические
границы (⚙ защита от крэша, не поведение) и честный источник значения: default | env |
praxis | egor.

Каждый пропуск до голоса получает причину — это разные состояния, не одно молчание:
  * не_увидела      — сообщение не дошло до меня физически (сюда же сломанный сенсор);
  * не_сочла_важным — увидела и решила не просыпаться (reflex-шум, ответ на границу);
  * отложила        — кулдаун/занятость: вернусь (defer, коллизия хода, устаревший wake);
  * запретил_егор   — его рычаг: freeze, allowlist, режим комнаты, panic.
Фоновый групповой поток (не адресовано мне) — не пропуск, а среда: он копится в буфере как
контекст; здесь только агрегированный счётчик, не по-событийная запись. Удержания ПОСЛЕ
голоса уже видимы в turns.jsonl (held+why) — этот журнал закрывает дыру ДО голоса.

Журнал пропусков — кольцевой jsonl (образец rails.denials): дешёвый append, никогда не
роняет вызвавшего; частые одинаковые пропуски (spам в не-allowlisted группе) схлопываются
в одну запись с счётчиком за окно.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("praxis-perception")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
STATE_PATH = BASE / "memory" / ".state" / "perception.json"
SKIPS_PATH = BASE / "memory" / ".state" / "perception_skips.jsonl"
JOURNAL_DIR = BASE / "memory" / "journal"

SKIPS_KEEP = 300
_DETAIL_MAX = 160
_COALESCE_SEC = 600.0      # одинаковые (stage, chat) в окне схлопываются в одну запись
_AMBIENT_FLUSH_SEC = 60.0  # фоновый счётчик персистится не чаще раза в минуту

# «мой_ритм» — пропуск по МОЕЙ собственной границе (окно сна, режим комнаты, который
# поставила я). До этого такие случаи классифицировались как «запретил_егор», и я
# читала собственное решение как чужой запрет — ровно та подмена авторства, ради
# которой этот словарь и заводился.
CLASSES = ("не_увидела", "не_сочла_важным", "отложила", "запретил_егор", "мой_ритм")

# Рычаги: env — дефолт Егора, мой персист поверх; границы — физика (широкие), не поведение.
KNOBS: dict[str, dict] = {
    "debounce_sec": dict(env="PRAXIS_DEBOUNCE_SEC", default=4.0, lo=0.0, hi=120.0, kind="float",
                         desc="склейка всплеска сообщений перед ходом, сек"),
    "cooldown_dm": dict(env="PRAXIS_COOLDOWN_DM", default=8.0, lo=0.0, hi=3600.0, kind="float",
                        desc="мой темп в личках, сек между ходами"),
    "cooldown_group": dict(env="PRAXIS_COOLDOWN_GROUP", default=300.0, lo=0.0, hi=7200.0,
                           kind="float", desc="мой темп фоновых групповых ходов, сек (quiet ×2)"),
    "cooldown_addressed": dict(env="PRAXIS_COOLDOWN_ADDRESSED", default=180.0, lo=0.0, hi=7200.0,
                               kind="float", desc="мой темп после @mention/reply в группе, сек"),
    "group_ack_max_len": dict(env="PRAXIS_GROUP_ACK_MAX_LEN", default=0, lo=0, hi=200,
                              kind="int", desc="порог «шума» в группе: короче — reflex не будит "
                              "(0 = по длине не резать)"),
    "missed_dm_hours": dict(env="PRAXIS_MISSED_DM_HOURS", default=48.0, lo=1.0, hi=720.0,
                            kind="float", desc="как далеко смотрю пропущенные ЛС после рестарта, ч"),
    "forge_event_gap_sec": dict(env="PRAXIS_FORGE_EVENT_GAP", default=15.0, lo=0.0, hi=3600.0,
                                kind="float", desc="зазор между forge-event ходами, сек "
                                "(события коалесцируются, не теряются)"),
    "narration_gap_sec": dict(env="PRAXIS_NARRATION_GAP", default=20.0, lo=0.0, hi=3600.0,
                              kind="float", desc="зазор между наррациями в один тред, сек "
                              "(мой темп рассказа по ходу работы)"),
}

_CACHE = {"mtime": None, "data": {}}
_LAST_SKIP: dict[tuple, dict] = {}     # (stage, chat) -> {ts, n} — схлопывание повторов
_AMBIENT: dict[str, dict] = {}         # date -> {chat: n} — фоновый групповой поток (RAM)
_AMBIENT_FLUSHED = {"ts": 0.0}


# --------------------------------------------------------------------------- #
#  персист и значения
# --------------------------------------------------------------------------- #
def _state() -> dict:
    """Живое чтение персиста с кэшем по mtime — дёшево на каждое сообщение."""
    try:
        m = STATE_PATH.stat().st_mtime_ns
    except OSError:
        _CACHE.update(mtime=None, data={})
        return {}
    if _CACHE["mtime"] != m:
        try:
            d = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            _CACHE.update(mtime=m, data=d if isinstance(d, dict) else {})
        except Exception:
            _CACHE.update(mtime=m, data={})
    return _CACHE["data"]


def _save_state(d: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)
    _CACHE.update(mtime=None)  # перечитать при следующем value()


def _parse(knob: str, raw) -> object | None:
    meta = KNOBS[knob]
    try:
        if meta["kind"] == "float":
            return float(raw)
        if meta["kind"] == "int":
            return int(float(raw))
        if meta["kind"] == "hours":
            s = str(raw).strip()
            import heartbeat
            heartbeat.parse_hours(s)  # валидация формата «1-8» / «22-6» / «»
            return s
    except Exception:
        return None
    return None


def _in_bounds(knob: str, val) -> bool:
    meta = KNOBS[knob]
    if meta["kind"] == "hours":
        return True
    return meta["lo"] <= val <= meta["hi"]


def value(knob: str):
    """Эффективное значение рычага: персист (praxis/egor) → env → default. Живо."""
    meta = KNOBS.get(knob)
    if meta is None:
        raise KeyError(knob)
    ov = (_state().get("overrides") or {}).get(knob)
    if isinstance(ov, dict) and "value" in ov:
        parsed = _parse(knob, ov["value"])
        if parsed is not None and _in_bounds(knob, parsed):
            return parsed
    raw = os.getenv(meta["env"])
    if raw not in (None, ""):
        parsed = _parse(knob, raw)
        if parsed is not None:
            return parsed
    return meta["default"]


def source_of(knob: str) -> str:
    ov = (_state().get("overrides") or {}).get(knob)
    if isinstance(ov, dict) and "value" in ov:
        return str(ov.get("by") or "praxis")
    if os.getenv(KNOBS[knob]["env"]) not in (None, ""):
        return "env"
    return "default"


def effective() -> list[dict]:
    """Полная таблица рычагов для тула/пульта/rails: значение, источник, границы, описание."""
    out = []
    for knob, meta in KNOBS.items():
        row = {"knob": knob, "value": value(knob), "source": source_of(knob),
               "env": meta["env"], "default": meta["default"], "desc": meta["desc"]}
        if meta["kind"] != "hours":
            row["bounds"] = [meta["lo"], meta["hi"]]
        ov = (_state().get("overrides") or {}).get(knob) or {}
        if ov.get("reason"):
            row["reason"] = str(ov["reason"])[:200]
        if ov.get("ts"):
            row["since"] = ov["ts"]
        out.append(row)
    return out


def set_knob(knob: str, raw_value, *, by: str = "praxis", reason: str = "") -> dict:
    """Поставить рычаг (живо, без рестарта). by: praxis | egor — источник честно виден."""
    if knob not in KNOBS:
        return {"ok": False, "error": f"не знаю рычага «{knob}»; мои: {', '.join(KNOBS)}"}
    parsed = _parse(knob, raw_value)
    if parsed is None:
        return {"ok": False, "error": f"значение «{raw_value}» не разбирается для {knob} "
                                      f"({KNOBS[knob]['kind']})"}
    if not _in_bounds(knob, parsed):
        meta = KNOBS[knob]
        return {"ok": False, "error": f"{parsed} вне физических границ [{meta['lo']}..{meta['hi']}] "
                                      "— это защита от крэша, не поведение"}
    st = dict(_state())
    ov = dict(st.get("overrides") or {})
    old = value(knob)
    ov[knob] = {"value": parsed, "by": by, "reason": (reason or "")[:200],
                "ts": _dt.datetime.now().isoformat(timespec="minutes")}
    st["overrides"] = ov
    try:
        _save_state(st)
    except Exception as e:
        log.warning("perception.set_knob: персист не удался", exc_info=True)
        return {"ok": False, "error": f"персист не удался: {type(e).__name__}"}
    _journal(f"рычаг {knob}: {old} → {parsed} ({by}"
             + (f", {reason[:120]}" if reason else "") + ")")
    _spine(f"{knob}: {old} → {parsed}", {"knob": knob, "old": old, "new": parsed,
                                         "by": by, "reason": (reason or "")[:200]})
    return {"ok": True, "old": old, "new": parsed}


def reset_knob(knob: str, *, by: str = "praxis") -> dict:
    if knob not in KNOBS:
        return {"ok": False, "error": f"не знаю рычага «{knob}»"}
    st = dict(_state())
    ov = dict(st.get("overrides") or {})
    if knob not in ov:
        return {"ok": False, "error": f"{knob} и так на дефолте (env/базовый)"}
    old = value(knob)
    ov.pop(knob, None)
    st["overrides"] = ov
    _save_state(st)
    _journal(f"рычаг {knob}: {old} → дефолт ({by})")
    _spine(f"{knob}: сброс к дефолту", {"knob": knob, "old": old, "by": by})
    return {"ok": True, "old": old, "new": value(knob)}


def _journal(msg: str) -> None:
    try:
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        p = JOURNAL_DIR / f"{_dt.date.today().isoformat()}.md"
        if not p.exists():
            p.write_text(f"# {_dt.date.today().isoformat()}\n\n", encoding="utf-8")
        with p.open("a", encoding="utf-8") as fh:
            fh.write(f"- {_dt.datetime.now():%H:%M} [восприятие] {msg}\n")
    except Exception:
        log.debug("journal perception не удался", exc_info=True)


def _spine(text: str, meta: dict) -> None:
    try:
        import memory_life as life
        life.append_event(kind="perception_change", chat_id=None, actor="Praxis",
                          direction="internal", text=text[:300], source="perception",
                          salience=1, meta=meta)
    except Exception:
        log.debug("perception: событие в spine не записалось", exc_info=True)


# --------------------------------------------------------------------------- #
#  журнал пропусков
# --------------------------------------------------------------------------- #
def _skip_meta(meta: dict | None) -> dict:
    """Small allowlisted provenance payload for pre-voice decisions.

    Список белый ради размера записи, а не ради запрета: журнал — кольцо, и произвольный
    payload вытеснил бы из него причины пропусков. Новую провенанс-строку сюда добавляют
    вместе с её потребителем (`dropped`/`owner_lost` читает canary.dropped_addresses).
    """
    if not isinstance(meta, dict):
        return {}
    clipped = {}
    limits = {"mode": 20, "mode_set_by": 40, "mode_reason": 200, "mode_until": 40,
              "dropped": 24, "owner_lost": 8}
    for key, limit in limits.items():
        value = meta.get(key)
        if value not in (None, ""):
            clipped[key] = str(value)[:limit]
    return clipped


def note_skip(stage: str, klass: str, *, chat_id=None, detail: str = "",
              meta: dict | None = None) -> None:
    """Причина пропуска — дешёвая строка в кольцо. Повторы (stage, chat) схлопываются."""
    try:
        if klass not in CLASSES:
            klass = "не_увидела"
        skip_meta = _skip_meta(meta)
        signature = (
            stage,
            str(chat_id) if chat_id is not None else "",
            klass,
            str(detail or "")[:_DETAIL_MAX],
            json.dumps(skip_meta, ensure_ascii=False, sort_keys=True),
        )
        now = time.time()
        prev = _LAST_SKIP.get(signature)
        if prev and now - prev["ts"] < _COALESCE_SEC:
            prev["n"] += 1  # только действительно одинаковые причины схлопываются
            return
        n_prev = (prev or {}).get("n", 0)
        _LAST_SKIP[signature] = {"ts": now, "n": 1}
        rec = {"ts": round(now, 1), "stage": str(stage)[:40], "class": klass,
               "detail": str(detail or "")[:_DETAIL_MAX]}
        if skip_meta:
            rec["meta"] = skip_meta
        if chat_id is not None:
            rec["chat"] = str(chat_id)
        if n_prev > 1:
            rec["prev_n"] = n_prev  # сколько таких же схлопнулось в прошлое окно
        SKIPS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SKIPS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _maybe_compact_skips()
    except Exception:
        log.debug("perception.note_skip не записал", exc_info=True)


def _maybe_compact_skips() -> None:
    try:
        lines = SKIPS_PATH.read_text(encoding="utf-8").splitlines()
        if len(lines) > 2 * SKIPS_KEEP:
            tmp = SKIPS_PATH.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(lines[-SKIPS_KEEP:]) + "\n", encoding="utf-8")
            tmp.replace(SKIPS_PATH)
    except Exception:
        log.debug("компакт пропусков не удался", exc_info=True)


def note_ambient(chat_id) -> None:
    """Фоновый групповой поток: среда, не пропуск. RAM-счётчик, персист раз в минуту."""
    try:
        day = _dt.date.today().isoformat()
        d = _AMBIENT.setdefault(day, {})
        d[str(chat_id)] = d.get(str(chat_id), 0) + 1
        for stale in [k for k in _AMBIENT if k != day]:
            _AMBIENT.pop(stale, None)
        now = time.time()
        if now - _AMBIENT_FLUSHED["ts"] >= _AMBIENT_FLUSH_SEC:
            _AMBIENT_FLUSHED["ts"] = now
            st = dict(_state())
            st["ambient"] = {day: dict(d)}
            _save_state(st)
    except Exception:
        log.debug("perception.note_ambient не записал", exc_info=True)


def ambient_today() -> dict:
    day = _dt.date.today().isoformat()
    ram = _AMBIENT.get(day) or {}
    disk = (_state().get("ambient") or {}).get(day) or {}
    out = dict(disk)
    for k, v in ram.items():
        out[k] = max(out.get(k, 0), v)
    return out


def recent_skips(n: int = 12) -> list[dict]:
    try:
        out = []
        for line in SKIPS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        return out[-n:]
    except Exception:
        return []


def skips_today() -> dict:
    """Счёт пропусков за сегодня по классам (по записям кольца; схлопнутые — по счётчику)."""
    midnight = _dt.datetime.combine(_dt.date.today(), _dt.time.min).timestamp()
    acc: dict[str, int] = {}
    for rec in recent_skips(SKIPS_KEEP):
        if float(rec.get("ts") or 0) >= midnight:
            acc[rec.get("class", "?")] = acc.get(rec.get("class", "?"), 0) + 1 + int(rec.get("prev_n") or 0)
    return acc


# --------------------------------------------------------------------------- #
#  наружу: тул, STATE, пульт
# --------------------------------------------------------------------------- #
def describe() -> str:
    lines = ["Мои рычаги восприятия (env — дефолт Егора, моё значение поверх, живо без рестарта):"]
    for row in effective():
        src = {"default": "дефолт", "env": "env Егора"}.get(row["source"], row["source"])
        b = f" [{row['bounds'][0]}..{row['bounds'][1]}]" if "bounds" in row else ""
        why = f" — {row['reason']}" if row.get("reason") else ""
        lines.append(f"- {row['knob']} = {row['value']} ({src}){b}: {row['desc']}{why}")
    sk = skips_today()
    if sk:
        lines.append("Пропуски сегодня: " + ", ".join(f"{k} {v}" for k, v in sorted(sk.items())))
    amb = ambient_today()
    if amb:
        total = sum(amb.values())
        lines.append(f"Фон групп сегодня (среда, не пропуск): {total} сообщений в {len(amb)} чатах.")
    lines.append("Каждый пропуск имеет класс причины: не_увидела / не_сочла_важным / отложила / "
                 "запретил_егор. Менять рычаги — set из owner-скоупа; это мои рычаги, "
                 "Егор ставит свои через пульт (источник виден).")
    return "\n".join(lines)


def skips_text(n: int = 12, *, chat_id=None, include_provenance: bool = True) -> str:
    recs = recent_skips(n)
    if chat_id is not None:
        recs = [r for r in recs if str(r.get("chat") or "") == str(chat_id)]
    if not recs:
        return "Журнал пропусков пуст — всё, что доходило, я видела и решала."
    lines = ["Последние пропуски до голоса (класс · этап · чат · деталь):"]
    for r in recs:
        ts = _dt.datetime.fromtimestamp(float(r.get("ts") or 0)).strftime("%d.%m %H:%M")
        extra = f" ×{1 + int(r.get('prev_n') or 0)}" if r.get("prev_n") else ""
        meta = r.get("meta") if include_provenance and isinstance(r.get("meta"), dict) else {}
        provenance = []
        if meta.get("mode_set_by"):
            provenance.append(f"задал {meta['mode_set_by']}")
        if meta.get("mode_reason"):
            provenance.append(f"причина: {meta['mode_reason']}")
        if meta.get("mode_until"):
            provenance.append(f"до {meta['mode_until']}")
        lines.append(f"- {ts} {r.get('class')} · {r.get('stage')}{extra}"
                     + (f" · чат {r.get('chat')}" if r.get("chat") else "")
                     + (f" · {r.get('detail')}" if r.get("detail") else "")
                     + (f" · {'; '.join(provenance)}" if provenance else ""))
    return "\n".join(lines)


def state_line() -> str:
    """Строка для STATE: только если сегодня были пропуски или мои переопределения."""
    parts = []
    ov = _state().get("overrides") or {}
    if ov:
        parts.append(f"рычагов переопределено {len(ov)} (manage_perception list)")
    sk = skips_today()
    if sk:
        parts.append("пропуски: " + ", ".join(f"{k} {v}" for k, v in sorted(sk.items())))
    return "; ".join(parts)


def panel_state() -> dict:
    return {"knobs": effective(), "skips": list(reversed(recent_skips(20))),
            "skips_today": skips_today(), "ambient_today": ambient_today()}
