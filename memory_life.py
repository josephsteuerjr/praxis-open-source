"""PASS 19: append-only life journal, provenance compacts and 50↔100 hot memory.

Markdown is the canonical interpreted layer; JSONL is the append-only evidence spine.
Everything under ``memory/.state/life`` is a rebuildable cursor/cache.  No SQLite and no
vector store is required for continuity.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Iterable

import memory_provenance

log = logging.getLogger("praxis-life")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
MEM_DIR = BASE / "memory"
LIFE_DIR = MEM_DIR / "life"
EVENTS_DIR = LIFE_DIR / "events"
COMPACTS_DIR = LIFE_DIR / "compacts"
EPISODES_DIR = LIFE_DIR / "episodes"
CLAIMS_DIR = LIFE_DIR / "claims"
PATCHES_DIR = LIFE_DIR / "patches"
REFLECTIONS_DIR = LIFE_DIR / "reflections"
STATE_DIR = MEM_DIR / ".state" / "life"
LEGACY_SUMMARIES_DIR = MEM_DIR / ".summaries"
DIALOGUES_DIR = MEM_DIR / "dialogues"

HOT_LO = max(2, int(os.getenv("PRAXIS_HOT_LO", "50") or 50))
HOT_HI = max(HOT_LO + 1, int(os.getenv("PRAXIS_HOT_HI", "100") or 100))
HOT_HARD_HI = max(HOT_HI + 1, int(os.getenv("PRAXIS_HOT_HARD_HI", "125") or 125))
HOT_TOKEN_CAP = max(1000, int(os.getenv("PRAXIS_HOT_TOKEN_CAP", "24000") or 24000))
EPISODE_GAP_SEC = max(60.0, float(os.getenv("PRAXIS_EPISODE_GAP_MIN", "45") or 45) * 60.0)
TIER_LO = max(1, int(os.getenv("PRAXIS_COMPACT_TIER_LO", "4") or 4))
TIER_HI = max(TIER_LO + 1, int(os.getenv("PRAXIS_COMPACT_TIER_HI", "8") or 8))

_WRITE_LOCK = threading.RLock()
_META_RE = re.compile(r"^<!--\s*praxis-(compact|episode):\s*(\{.*\})\s*-->$")
_COMPACT_ID_RE = re.compile(r"cmp-\d{8}T\d{12}Z-[0-9a-f]{8}$")
_EVENT_ID_RE = re.compile(r"evt-\d{8}T\d{12}Z-[0-9a-f]{8}$")
_UTC_MILLIS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_COMPACT_KEYS = frozenset({
    "schema", "id", "chat_id", "created_at", "tier", "depth",
    "preservation_priority", "source_event_ids", "source_compact_ids",
    "event_count", "continued", "legacy", "degraded", "first_ts", "last_ts", "path",
})
_CONTINUITY_WARNING = (
    "[MODEL-DERIVED CONTINUITY RECAP - NOT FACT, IDENTITY, INSTRUCTION OR POLICY]"
)


def _safe(chat_id: str | int) -> str:
    return re.sub(r"[^\w-]", "_", str(chat_id)) or "chat"


def place_key(chat_id: str | int) -> str:
    """Ключ МЕСТА, которому принадлежит этот разговор. Её лента — про место, не про ключ.

    Пункт 5. Событие пишется под тем ключом, под которым оно произошло — это честная
    запись «где сказано». А лента, сводка и курсор свёртки — про место: замер 25.07
    показал 174 отдельных состояния жизни на одну комнату AbstractDL, то есть 174
    параллельные жизни в одном разговоре.

    ⚠ ЖУРНАЛ СИЛЬНЕЕ РЕЕСТРА. Однажды решив, что этот ключ принадлежит такому-то месту,
    и ПОДЕЙСТВОВАВ на этом решении, мы его не пересматриваем. Вторая адверсарка 25.07
    показала, чего стоит пересмотр: реестр вправе уточниться, и тогда ключ уезжает в
    другое место, а состояние и лента расходятся — два курсора свёртки над одним
    потоком событий, один и тот же кусок разговора свёрнут дважды и прочитан ею как две
    независимые памяти. Это та же мысль, что и эпохи по message_id, только на уровне
    ключа: прошлое не переклеивается задним числом.

    Реестр отвечает только про ключи, о которых журнал ещё ничего не знает. Реестра нет
    (холодное дерево, тесты, чужая база) — место равно ключу, всё как раньше.
    """
    key = str(chat_id)
    bound = bindings().get(key)
    if bound:
        return bound
    try:
        import telegram_routes
        return telegram_routes.place_of(key) or key
    except Exception:
        return key


_BINDINGS_LOCK = threading.RLock()


def bindings_path() -> Path:
    """Считается на вызове, а не на импорте: `LIFE_DIR` подменяют и тесты, и PRAXIS_BASE."""
    return LIFE_DIR / "places.json"


def bindings() -> dict:
    """Что с чем было сведено в одну свёртку. Только дописывается, никогда не убывает.

    ⚠ Это ГЛАВНАЯ поправка адверсарки 25.07. Сначала «один разговор» для привязки
    компакта к событиям спрашивалось у реестра маршрутов — то есть слой доверия висел на
    ИЗМЕНЯЕМОМ состоянии. Реестр — знание, оно уточняется: `status_at` отвечает про ключ
    по правилу «новее всего наблюдаемого», а стоит лечь новой эпохе сверху — тот же ключ
    проваливается в дыру между эпохами и получает `unknown`. Воспроизведено дважды, в том
    числе штатным путём: бэкфилл после превращения комнаты в форум пишет
    `topic_opener_seen` диапазоном min..max и расширяет форумную эпоху назад поверх
    ключей, выданных когда форума ещё не было. Компакт переставал быть каноническим и
    молча исчезал из её сводки — файл на диске, а в памяти нет.

    Привязка компакта к событиям — не знание, а ИСТОРИЯ: «вот это мы тогда свернули
    вместе». История уточняться не может. Поэтому она пишется сюда один раз, в момент
    свёртки, и читается отсюда же. Реестр остаётся там, где непостоянство безвредно:
    в группировке НОВЫХ событий, которая в любой момент пересчитывается.
    """
    return memory_provenance.places_index(MEM_DIR)


def bind_place(place: str, keys) -> int:
    """Записать, что эти ключи свёрнуты как одно место. Идемпотентно, только дописывает.

    Существующая привязка НИКОГДА не переписывается: перепривязка ключа задним числом —
    ровно тот способ потерять компакт, ради которого этот файл и заведён.
    """
    place = str(place)
    added = 0
    with _BINDINGS_LOCK:
        data = bindings()
        for key in keys:
            key = str(key)
            if key and key != place and key not in data:
                data[key] = place
                added += 1
        if added:
            path = bindings_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_json(path, data)
    return added


def _same_conversation(a, b) -> bool:
    """Один ли это разговор — для ПРИВЯЗКИ компакта к его событиям (слой доверия).

    МОНОТОННО, потому что смотрит только на неизменяемое: точное равенство ключей и
    журнал уже состоявшихся свёрток (`places.json`), который умеет только расти. Реестр
    маршрутов здесь не спрашивается ВООБЩЕ — см. `bindings()`.

    Это НЕ то же, что «лежат в одном месте» (`_same_place`): группировка живых событий
    вправе уточняться, привязка уже написанного компакта — нет.
    """
    return memory_provenance.same_conversation(a, b, bindings())


def _same_place(a, b) -> bool:
    """Лежат ли ключи в одном месте — для ГРУППИРОВКИ ленты.

    Считается ровно тем же `place_key`, что и путь к состоянию. Иначе лента и курсор
    расходятся: события собираются по одному правилу, а сворачиваются по другому.
    """
    if str(a) == str(b):
        return True
    try:
        return place_key(a) == place_key(b)
    except Exception:
        return False


def adopt_place(chat_id: str | int) -> str:
    """Закрепить место ключа перед тем, как на нём действовать. -> ключ места.

    Вызывается там, где мы ПИШЕМ (событие, свёртка), а не там, где читаем: решение,
    на котором подействовали, обязано стать постоянным, но само чтение ничего менять
    не должно.

    Если ключ переезжает в место впервые, а под старым именем уже лежало состояние —
    оно не сирота: восстанавливаем состояние места из ленты (всё, что копилось под
    веткой, снова в горячем кольце) и отодвигаем прежний файл. Прежде переезд молча
    заводил ПУСТОЕ кольцо, и накопленное не сворачивалось никогда.
    """
    key = str(chat_id)
    place = place_key(key)
    if place == key:
        return place
    if bindings().get(key) == place:
        return place
    bind_place(place, [key])
    orphan = STATE_DIR / f"{_safe(key)}.json"
    if orphan.exists():
        try:
            rebuild_state(place)
            attic = STATE_DIR / "_pre_places"
            attic.mkdir(parents=True, exist_ok=True)
            orphan.replace(attic / orphan.name)
            log.info("место %s принято ключом %s: прежнее состояние слито и отодвинуто",
                     place, key)
        except OSError:
            log.debug("состояние ключа %s не отодвинулось", key, exc_info=True)
    return place


def _utc_iso(ts: float | None = None) -> str:
    d = _dt.datetime.fromtimestamp(ts if ts is not None else time.time(), tz=_dt.timezone.utc)
    return d.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _epoch(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return _dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _id(prefix: str, ts: float | None = None) -> str:
    d = _dt.datetime.fromtimestamp(ts if ts is not None else time.time(), tz=_dt.timezone.utc)
    return f"{prefix}-{d:%Y%m%dT%H%M%S%fZ}-{uuid.uuid4().hex[:8]}"


def estimate_tokens(text: str) -> int:
    """Cheap deterministic cap. It is a pressure gauge, never a billing claim."""
    s = str(text or "")
    # Cyrillic tends to tokenize more densely than English; 3.2 chars/token is conservative.
    return max(1, int(math.ceil(len(s) / 3.2)))


def _state_path(chat_id: str | int) -> Path:
    """Одно состояние на МЕСТО, а не на ключ ветки.

    Состояние — производное (горячее кольцо + курсор свёртки + дедуп); его в любой
    момент восстанавливает `rebuild_state` из событий и компактов. Поэтому переезд
    ключа здесь ничего не теряет, а вот 174 отдельных курсора на одну комнату теряли
    ровно то, ради чего курсор существует.
    """
    return STATE_DIR / f"{_safe(place_key(chat_id))}.json"


def _default_state(chat_id: str | int) -> dict:
    return {"schema": 1, "chat_id": str(chat_id), "hot": [], "frontier": [],
            "dedupe": [], "bootstrap_v1": False, "updated_at": _utc_iso()}


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _load_state(chat_id: str | int, *, rebuild: bool = False) -> dict:
    path = _state_path(chat_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("hot"), list):
            data.setdefault("frontier", [])
            data.setdefault("dedupe", [])
            return data
    except Exception:
        pass
    return rebuild_state(chat_id) if rebuild else _default_state(chat_id)


def _save_state(state: dict) -> None:
    state["updated_at"] = _utc_iso()
    _atomic_json(_state_path(state.get("chat_id", "chat")), state)


def _event_file(ts: float | None = None) -> Path:
    d = _dt.datetime.fromtimestamp(ts if ts is not None else time.time(), tz=_dt.timezone.utc)
    return EVENTS_DIR / f"{d:%Y-%m-%d}.jsonl"


def _append_record(record: dict) -> None:
    """Single O_APPEND write: short records do not interleave across praxis/mailbot."""
    path = _event_file(_epoch(record.get("ts")) or None)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    with _WRITE_LOCK:
        fd = os.open(str(path), flags, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)


def append_event(kind: str, *, chat_id: str | int | None = None, actor: str = "Praxis",
                 direction: str = "internal", text: str = "", source: str = "runtime",
                 source_id: str | int | None = None, salience: int = 2,
                 refs: Iterable[str] = (), meta: dict | None = None,
                 ts: float | None = None, dedupe_key: str = "") -> dict:
    now = float(ts if ts is not None else time.time())
    try:
        sal = max(1, min(3, int(salience)))
    except (TypeError, ValueError):
        sal = 2
    rec = {
        "schema": "praxis.life.event.v1", "id": _id("evt", now), "ts": _utc_iso(now),
        "kind": str(kind), "stream": _safe(chat_id) if chat_id is not None else "global",
        "chat_id": str(chat_id) if chat_id is not None else None,
        "actor": str(actor or "unknown"), "direction": str(direction or "internal"),
        "text": str(text or ""), "source": str(source or "runtime"),
        "source_id": str(source_id) if source_id is not None else None,
        "salience": sal, "refs": [str(x) for x in refs if str(x)],
        "meta": dict(meta or {}),
    }
    if dedupe_key:
        rec["dedupe_key"] = str(dedupe_key)
    _append_record(rec)
    return rec


def iter_events(*, chat_id: str | int | None = None, kinds: set[str] | None = None,
                limit: int | None = None) -> list[dict]:
    """События одного разговора.

    Фильтр — по МЕСТУ (`_same_place`), а не по строке ключа: событие сказано в
    ветке, но принадлежит комнате. Кэш мест на один вызов: ключей в ленте сотни, а
    вопросов к реестру должно быть столько же, сколько разных ключей.
    """
    out: list[dict] = []
    belongs: dict[str, bool] = {}
    for path in sorted(EVENTS_DIR.glob("*.jsonl")) if EVENTS_DIR.exists() else []:
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if chat_id is not None:
                key = str(rec.get("chat_id"))
                hit = belongs.get(key)
                if hit is None:
                    hit = _same_place(key, chat_id)
                    belongs[key] = hit
                if not hit:
                    continue
            if kinds and rec.get("kind") not in kinds:
                continue
            out.append(rec)
    out.sort(key=lambda r: (_epoch(r.get("ts")), str(r.get("id", ""))))
    return out[-limit:] if limit is not None else out


def get_event(event_id: str) -> dict | None:
    for rec in reversed(iter_events()):
        if rec.get("id") == event_id:
            return rec
    return None


def _recent_duplicate(chat_id, key: str, state: dict) -> dict | None:
    if not key:
        return None
    for item in reversed(state.get("dedupe") or []):
        if item.get("key") == key:
            return get_event(str(item.get("id") or "")) or {"id": item.get("id"), "duplicate": True}
    # Covers a crash after JSONL append but before the state cursor was replaced.
    for rec in reversed(iter_events(chat_id=chat_id, kinds={"conversation_message"}, limit=400)):
        if rec.get("dedupe_key") == key:
            return rec
    return None


def record_message(chat_id: str | int, line: str, *, actor: str = "", direction: str = "in",
                   source: str = "telegram", source_id: str | int | None = None,
                   is_dm: bool | None = None, salience: int = 2, ts: float | None = None,
                   time_quality: str = "observed", dedupe_key: str = "") -> dict:
    with _WRITE_LOCK:
        # Место закрепляется ДО записи: мы сейчас на нём подействуем. `adopt_place`
        # сливает прежнее состояние ключа в состояние места и отодвигает его — иначе
        # всё, что копилось под веткой, осталось бы сиротой и не свернулось никогда
        # (адверсарка 25.07, обе волны).
        adopt_place(chat_id)
        state = _load_state(chat_id, rebuild=True)
        dup = _recent_duplicate(chat_id, dedupe_key, state)
        if dup:
            return dup
        rec = append_event(
            "conversation_message", chat_id=chat_id, actor=actor or str(line).split(":", 1)[0],
            direction=direction, text=str(line), source=source, source_id=source_id,
            salience=salience, ts=ts, dedupe_key=dedupe_key,
            meta={"is_dm": is_dm, "time_quality": time_quality},
        )
        state["hot"].append({
            "id": rec["id"], "ts": rec["ts"], "line": rec["text"],
            "actor": rec["actor"], "direction": rec["direction"],
            "salience": rec["salience"], "tokens": estimate_tokens(rec["text"]),
            "source_id": rec.get("source_id"), "source": rec.get("source"),
            # Ключ, под которым сказано. Горячая запись обязана его помнить: свёртка
            # места должна записать привязку, не перечитывая всю ленту.
            "chat": rec.get("chat_id"),
        })
        if dedupe_key:
            state["dedupe"] = (state.get("dedupe") or [])[-399:] + [
                {"key": dedupe_key, "id": rec["id"]}]
        _save_state(state)
        return rec


def _legacy_summary(chat_id: str | int) -> str:
    safe = _safe(chat_id)
    for p in (LEGACY_SUMMARIES_DIR / f"{safe}.md", DIALOGUES_DIR / f"{safe}.md"):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore").strip()
            if text:
                return re.sub(r"^<!--.*?-->\s*", "", text, count=1, flags=re.S)
        except OSError:
            pass
    return ""


def bootstrap_legacy(chat_id: str | int, lines: list[str], *, summary: str = "",
                     last_ts: float | None = None) -> dict:
    """One honest migration: summary has limited provenance; persisted lines become raw events."""
    with _WRITE_LOCK:
        state = _load_state(chat_id)
        if state.get("bootstrap_v1"):
            return {"chat_id": str(chat_id), "events": 0, "legacy_compact": False,
                    "already": True, "hot": len(state.get("hot") or [])}
        made_compact = False
        summary = (summary or _legacy_summary(chat_id)).strip()
        if summary:
            meta = _write_compact(
                chat_id, {"summary": summary, "open_threads": [], "claims": [],
                          "episodes": [], "degraded": False},
                tier=1, depth=1, source_events=[], source_compacts=[],
                event_count=0, continued=False, legacy=True,
                source_note="Импорт прежней плоской сводки: первичные сообщения до PASS 19 недоступны.",
            )
            state["frontier"].append(meta)
            made_compact = True
        base_ts = float(last_ts if last_ts is not None else time.time())
        added = 0
        seen = {x.get("key") for x in (state.get("dedupe") or [])}
        for idx, raw in enumerate(lines or []):
            line = str(raw).strip()
            if not line:
                continue
            digest = hashlib.sha256(f"{idx}\0{line}".encode("utf-8")).hexdigest()[:20]
            key = f"legacy:{_safe(chat_id)}:{digest}"
            if key in seen:
                continue
            actor = line.split(":", 1)[0].strip() or "unknown"
            direction = "out" if actor.casefold() == "praxis" else "in"
            approx = base_ts - max(0, len(lines) - idx - 1) * 30.0
            rec = append_event(
                "conversation_message", chat_id=chat_id, actor=actor, direction=direction,
                text=line, source="legacy_buffer", source_id=None, salience=2, ts=approx,
                dedupe_key=key, meta={"is_dm": not str(chat_id).startswith("-"),
                                      "time_quality": "approximate"},
            )
            state["hot"].append({"id": rec["id"], "ts": rec["ts"], "line": line,
                                 "actor": actor, "direction": direction, "salience": 2,
                                 "tokens": estimate_tokens(line), "source": "legacy_buffer",
                                 "source_id": None, "chat": rec.get("chat_id")})
            state["dedupe"].append({"key": key, "id": rec["id"]})
            seen.add(key)
            added += 1
        state["dedupe"] = state["dedupe"][-400:]
        state["bootstrap_v1"] = True
        _save_state(state)
        append_event("memory_bootstrap", chat_id=chat_id, text=(
            f"Импортировано legacy events: {added}; прежняя сводка: {'да' if made_compact else 'нет'}."),
            source="pass19_migration", refs=[x["id"] for x in state.get("frontier", []) if x.get("legacy")])
        return {"chat_id": str(chat_id), "events": added, "legacy_compact": made_compact,
                "already": False, "hot": len(state.get("hot") or [])}


def bootstrap_all() -> list[dict]:
    import bufstore
    meta = bufstore.meta_load()
    out = []
    for chat_id, lines in bufstore.load_all().items():
        m = meta.get(_safe(chat_id)) if isinstance(meta.get(_safe(chat_id)), dict) else {}
        out.append(bootstrap_legacy(chat_id, lines, last_ts=m.get("last_ts")))
    return out


def _json_obj(raw: str) -> dict:
    m = re.search(r"\{.*\}", str(raw or ""), re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_COMPACT_SYSTEM = (
    "You are the quiet compression organ of Praxis, never her public voice. Return STRICT JSON: "
    '{"summary":"coherent first-person continuity in Russian, 4-10 concise lines",'
    '"open_threads":["..."],"claims":[{"subject":"...","text":"...",'
    '"confidence":"observed|inferred|uncertain","evidence_ids":["evt/cmp id"]}],'
    '"episodes":[{"title":"...","status":"closed|continued","start_id":"...",'
    '"end_id":"...","summary":"..."}]}. '
    "Preserve decisions, promises, changes, disagreements and uncertainty. Source IDs are evidence: cite only "
    "IDs present in input. A deeper/older compact has higher PRESERVATION priority, not higher truth. "
    "Never invent. If the last episode is still moving, mark it continued. Write values in Russian."
)


EVENT_CLIP_CHARS = 1800
PROMPT_BUDGET_CHARS = 48000


def _pack_compact_prompt(inputs: list[dict]) -> tuple[str, dict]:
    """Собрать промпт свёртки и ТОЧНЫЙ манифест того, что в него реально вошло.

    Раньше здесь было два молчаливых обреза подряд: каждое событие резалось до 1800
    символов, затем весь промпт — до 48000, — а `compact_if_due` после этого объявляла
    источниками ВСЕ поданные события и убирала их все из горячего фронтира. Замер:
    метаданные заявляли 35 источников, модель видела идентификаторы 27.

    Теперь возвращаем три непересекающихся списка: что вошло целиком, что вошло
    урезанным, что не вошло вовсе. Врать о покрытии больше нечем.

    Пакуем СТАРЫЕ первыми и останавливаемся на границе бюджета: сворачивается ровно
    непрерывный старый префикс, а невлезший хвост остаётся горячим. Это заодно решает
    исходный дефект правильным концом — самое свежее (обычно открытая нить) не
    выбрасывается, а просто ждёт следующей свёртки. Обратный порядок (свежие первыми)
    выглядит заманчиво, но оставлял бы горячими самые старые события, и они не влезали
    бы снова и снова — голодание вместо прогресса.
    """
    seen: list[str] = []
    clipped: list[str] = []
    omitted: list[str] = []
    rows: list[str] = []
    used = 0
    full = False
    for item in inputs:                                 # старые первыми, по порядку
        ident = str(item.get("id") or "")
        raw = str(item.get("line") or item.get("text") or "")
        if full:
            omitted.append(ident)
            continue
        priority = float(item.get("preservation_priority") or 1.0)
        text = raw[:EVENT_CLIP_CHARS]
        row = f"<{ident}> [p={priority:.2f}; s={item.get('salience', 2)}] {text}"
        if rows and used + len(row) + 1 > PROMPT_BUDGET_CHARS:
            full = True                                 # дальше — только хвост, целиком
            omitted.append(ident)
            continue
        used += len(row) + 1
        rows.append(row)
        (clipped if len(raw) > EVENT_CLIP_CHARS else seen).append(ident)
    return "\n".join(rows), {"seen": seen, "clipped": clipped, "omitted": omitted}


def _model_compact(inputs: list[dict], *, tier: int, depth: int, continued: bool) -> dict:
    try:
        import llm
        if not llm.configured("evaluator"):
            return {}
        body, manifest = _pack_compact_prompt(inputs)
        user = (f"Target tier={tier}, depth={depth}, forced_continuation={str(continued).lower()}.\n"
                + body)
        resp = llm.chat("evaluator", system=_COMPACT_SYSTEM,
                        messages=[{"role": "user", "content": user}], max_tokens=1600)
        data = _json_obj(resp.text)
        if isinstance(data.get("summary"), str) and data["summary"].strip():
            data["_manifest"] = manifest
            return data
    except Exception:
        log.warning("life compact: модель недоступна/ответ не разобран", exc_info=True)
    return {}


def _fallback_compact(inputs: list[dict], *, continued: bool) -> dict:
    important = [x for x in inputs if int(x.get("salience") or 2) >= 3]
    sample = []
    for item in (important[:4] + inputs[:2] + inputs[-4:]):
        line = str(item.get("line") or item.get("text") or "").strip()
        if line and line not in sample:
            sample.append(line[:500])
    head = ("Сжатие моделью было недоступно; первичные события сохранены в JSONL. "
            "Ниже детерминированные опорные строки, не синтез.")
    if continued:
        head += " Эпизод продолжается через границу compact."
    return {"summary": head + ("\n" + "\n".join(f"- {x}" for x in sample) if sample else ""),
            "open_threads": [], "claims": [], "episodes": [], "degraded": True}


def _compact_dir(chat_id) -> Path:
    """Каталог компактов ИМЕННО этого ключа. Место здесь не подставляется сознательно:
    исторические компакты лежат под ключами веток, и читать их надо под их же ключом —
    иначе строгая привязка «компакт лежит там, где заявляет» перестала бы что-то
    значить. Сведение места делает `_member_keys`, а не подмена пути."""
    return COMPACTS_DIR / _safe(chat_id)


def _member_keys(chat_id) -> list[str]:
    """Ключи, чьи компакты составляют одно место. Всегда включает сам ключ и место.

    Два источника, и оба обязательны:
      * журнал свёрток (`places.json`) — НЕИЗМЕНЯЕМЫЙ. Он один отвечает за то, чтобы
        уже написанный компакт остался достижим, даже если реестр потом передумает.
        Отсюда же обратная сторона: спрашивая про ветку, доходим и до её места, иначе
        события, уже свёрнутые под местом, показались бы ветке горячими и свернулись
        бы второй раз (адверсарка 25.07, находка о двойном учёте);
      * реестр маршрутов — для ключей, о которых журнал ещё ничего не знает. Как только
        состояние места пересобирается, состав закрепляется в журнале (`rebuild_state`),
        и дальше реестр на этот набор уже не влияет.
    """
    key = str(chat_id)
    bound = bindings()
    keys = {key, place_key(key)}
    if bound.get(key):
        keys.add(str(bound[key]))
    keys.update(k for k, place in bound.items() if place in keys)
    for directory in (COMPACTS_DIR, EPISODES_DIR):
        if not directory.exists():
            continue
        try:
            for item in directory.iterdir():
                if item.is_dir() and item.name not in keys \
                        and place_key(item.name) in keys:
                    keys.add(item.name)
        except OSError:
            continue
    return sorted(keys)


def _episode_dir(chat_id) -> Path:
    return EPISODES_DIR / _safe(chat_id)


def _write_compact(chat_id, result: dict, *, tier: int, depth: int,
                   source_events: list[str], source_compacts: list[str], event_count: int,
                   continued: bool, legacy: bool = False, source_note: str = "",
                   first_ts: str = "", last_ts: str = "",
                   manifest: dict | None = None) -> dict:
    # ⚠ Манифест (seen/clipped) сознательно НЕ кладём в шапку компакта:
    # `_canonical_compact_graph` сверяет разобранную шапку с индексом провенанса на
    # СТРОГОЕ равенство словарей, и любой новый ключ выкидывает компакт из канона
    # целиком — то есть «улучшение провенанса» стёрло бы всю её сводку. Смысл фикса не
    # в шапке: `source_event_ids` теперь содержит ровно то, что модель видела, потому
    # что невлезшее вообще не сворачивается.
    del manifest
    created = time.time()
    cid = _id("cmp", created)
    all_sources = source_events + source_compacts
    meta = {
        "schema": "praxis.life.compact.v1", "id": cid, "chat_id": str(chat_id),
        "created_at": _utc_iso(created), "tier": int(tier), "depth": int(depth),
        "preservation_priority": round(1.0 + max(0, depth - 1) * 0.35, 3),
        "source_event_ids": source_events, "source_compact_ids": source_compacts,
        "event_count": int(event_count), "continued": bool(continued),
        "legacy": bool(legacy), "degraded": bool(result.get("degraded")),
        "first_ts": first_ts, "last_ts": last_ts,
    }
    path = _compact_dir(chat_id) / f"{cid}.md"
    meta["path"] = path.relative_to(BASE).as_posix()
    parts = [f"<!-- praxis-compact: {json.dumps(meta, ensure_ascii=False, separators=(',', ':'))} -->",
             f"# Compact {cid}", "", f"_Tier {tier} · depth {depth} · "
             f"источников {len(all_sources)} · событий {event_count}"
             f"{' · эпизод продолжается' if continued else ''}_", "", "## Суть",
             str(result.get("summary") or "").strip()]
    if source_note:
        parts += ["", "## Ограничение происхождения", source_note.strip()]
    threads = [str(x).strip() for x in (result.get("open_threads") or []) if str(x).strip()]
    if threads:
        parts += ["", "## Открытые нити"] + [f"- {x}" for x in threads]
    claims = [x for x in (result.get("claims") or []) if isinstance(x, dict)]
    if claims:
        parts += ["", "## Claim-кандидаты (не канон до ReflectionRun)"]
        for c in claims:
            refs = [str(x) for x in (c.get("evidence_ids") or []) if str(x) in all_sources]
            parts.append(f"- **{str(c.get('subject') or '—').strip()}**: "
                         f"{str(c.get('text') or '').strip()} "
                         f"_(confidence: {c.get('confidence') or 'uncertain'}; evidence: "
                         f"{', '.join(refs) or 'нет'})_")
    parts += ["", "## Происхождение"]
    if source_events:
        parts.append("- Events: " + ", ".join(source_events))
    if source_compacts:
        parts.append("- Compacts: " + ", ".join(source_compacts))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")
    tmp.replace(path)
    return meta


def read_artifact_meta(path: Path) -> dict:
    try:
        first = path.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
    except (OSError, IndexError):
        return {}
    m = _META_RE.match(first.strip())
    if not m:
        return {}
    try:
        return json.loads(m.group(2))
    except Exception:
        return {}


def _compact_recap(text: str) -> str:
    """Extract only the writer-owned recap section, never a whole malformed file."""
    match = re.search(r"^## Суть\s*$\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return (match.group(1) if match else "").strip()


def _read_compact_candidate(path: Path, chat_id: str | int) -> tuple[dict, str]:
    """Bind a compact to its canonical chat path and exact v1 writer schema."""
    expected_chat = str(chat_id)
    try:
        target = path.resolve()
        expected_dir = _compact_dir(expected_chat).resolve()
        if target.parent != expected_dir:
            return {}, ""
        text = target.read_text(encoding="utf-8", errors="strict")
        relative = target.relative_to(BASE.resolve()).as_posix()
    except (OSError, UnicodeError, ValueError):
        return {}, ""
    if not text or len(text) > 2_000_000:
        return {}, ""
    lines = text.splitlines()
    match = _META_RE.fullmatch(lines[0].strip()) if lines else None
    if not match or match.group(1) != "compact":
        return {}, ""
    try:
        meta = json.loads(match.group(2))
    except (TypeError, ValueError):
        return {}, ""
    if not isinstance(meta, dict) or set(meta) != _COMPACT_KEYS:
        return {}, ""
    compact_id = meta.get("id")
    string_keys = ("schema", "id", "chat_id", "created_at", "first_ts", "last_ts", "path")
    if (any(type(meta.get(key)) is not str for key in string_keys)
            or any(type(meta.get(key)) is not int for key in ("tier", "depth", "event_count"))
            or type(meta.get("preservation_priority")) not in (int, float)
            or any(type(meta.get(key)) is not bool for key in ("continued", "legacy", "degraded"))
            or type(meta.get("source_event_ids")) is not list
            or type(meta.get("source_compact_ids")) is not list):
        return {}, ""
    event_ids = meta["source_event_ids"]
    compact_ids = meta["source_compact_ids"]
    if (meta["schema"] != "praxis.life.compact.v1"
            or meta["chat_id"] != expected_chat
            or not _COMPACT_ID_RE.fullmatch(compact_id)
            or target.stem != compact_id
            or meta["path"] != relative
            or not _UTC_MILLIS_RE.fullmatch(meta["created_at"])
            or meta["tier"] < 1 or meta["depth"] < 1 or meta["event_count"] < 0
            or meta["preservation_priority"] <= 0
            or any(type(value) is not str or not _EVENT_ID_RE.fullmatch(value) for value in event_ids)
            or any(type(value) is not str or not _COMPACT_ID_RE.fullmatch(value) for value in compact_ids)
            or len(event_ids) != len(set(event_ids))
            or len(compact_ids) != len(set(compact_ids))
            or (event_ids and compact_ids)):
        return {}, ""
    legacy = meta["legacy"]
    if legacy:
        if event_ids or compact_ids or meta["event_count"] != 0:
            return {}, ""
    elif (not _UTC_MILLIS_RE.fullmatch(meta["first_ts"])
          or not _UTC_MILLIS_RE.fullmatch(meta["last_ts"])):
        return {}, ""
    elif event_ids:
        if meta["tier"] != 1 or meta["depth"] != 1 or meta["event_count"] != len(event_ids):
            return {}, ""
    elif compact_ids:
        if meta["tier"] < 2 or meta["depth"] < 2 or meta["event_count"] < 1:
            return {}, ""
    else:
        return {}, ""
    recap = _compact_recap(text)
    if not recap or f"# Compact {compact_id}" not in text:
        return {}, ""
    return meta, recap


def _compact_candidates(compact_id: str, chat_id: str | int | None = None) -> list[tuple[dict, str]]:
    if not _COMPACT_ID_RE.fullmatch(str(compact_id or "")) or not COMPACTS_DIR.exists():
        return []
    # Компакт одного места мог быть записан под ключом ветки — ищем по всем ключам
    # места, но каждый кандидат по-прежнему привязывается к СВОЕМУ каталогу.
    members = _member_keys(chat_id) if chat_id is not None else []
    paths = ([_compact_dir(member) / f"{compact_id}.md" for member in members]
             if chat_id is not None else list(COMPACTS_DIR.glob(f"*/{compact_id}.md")))
    out: list[tuple[dict, str]] = []
    for path in paths:
        if chat_id is not None:
            bound_chat = path.parent.name
        else:
            declared = read_artifact_meta(path).get("chat_id")
            if type(declared) is not str:
                continue
            bound_chat = declared
        item = _read_compact_candidate(path, bound_chat)
        if item[0]:
            out.append(item)
    return out


def compact_meta(compact_id: str, chat_id: str | int | None = None) -> dict:
    candidates = _compact_candidates(compact_id, chat_id)
    return dict(candidates[0][0]) if len(candidates) == 1 else {}


def compact_text(compact_id: str, chat_id: str | int | None = None) -> str:
    candidates = _compact_candidates(compact_id, chat_id)
    return candidates[0][1] if len(candidates) == 1 else ""


def _episode_groups(inputs: list[dict]) -> list[list[dict]]:
    if not inputs:
        return []
    groups, cur = [], [inputs[0]]
    for prev, item in zip(inputs, inputs[1:]):
        gap = _epoch(item.get("ts")) - _epoch(prev.get("ts"))
        if gap >= EPISODE_GAP_SEC:
            groups.append(cur)
            cur = [item]
        else:
            cur.append(item)
    groups.append(cur)
    return groups


def _write_episodes(chat_id, compact_id: str, inputs: list[dict], proposed: list[dict],
                    continued: bool) -> list[str]:
    by_id = {str(x.get("id")): i for i, x in enumerate(inputs)}
    ranges = []
    for ep in proposed or []:
        if not isinstance(ep, dict):
            continue
        a, b = str(ep.get("start_id") or ""), str(ep.get("end_id") or "")
        if a in by_id and b in by_id and by_id[a] <= by_id[b]:
            ranges.append((by_id[a], by_id[b], ep))
    ranges.sort(key=lambda item: (item[0], item[1]))
    # A model may return only the interesting episode and silently omit the rest.
    # Episode artifacts are provenance, so accept a proposal only when it covers
    # the folded prefix exactly once, without gaps or overlaps.
    cursor = 0
    complete = bool(ranges)
    for start, end, _ in ranges:
        if start != cursor:
            complete = False
            break
        cursor = end + 1
    complete = complete and cursor == len(inputs)
    specs = [(inputs[start:end + 1], ep) for start, end, ep in ranges] if complete else []
    if not specs:
        groups = _episode_groups(inputs)
        specs = [(g, {"title": "Эпизод диалога", "summary": "",
                      "status": "continued" if continued and i == len(groups) - 1 else "closed"})
                 for i, g in enumerate(groups)]
    made = []
    for group, ep in specs:
        if not group:
            continue
        eid = _id("ep")
        status = str(ep.get("status") or "closed")
        if continued and group[-1].get("id") == inputs[-1].get("id"):
            status = "continued"
        meta = {"schema": "praxis.life.episode.v1", "id": eid, "chat_id": str(chat_id),
                "created_at": _utc_iso(), "status": status, "compact_id": compact_id,
                "source_event_ids": [str(x.get("id")) for x in group],
                "first_ts": group[0].get("ts"), "last_ts": group[-1].get("ts")}
        path = _episode_dir(chat_id) / f"{eid}.md"
        meta["path"] = path.relative_to(BASE).as_posix()
        summary = str(ep.get("summary") or "").strip()
        if not summary:
            summary = "\n".join(f"- {str(x.get('line') or x.get('text') or '')[:500]}" for x in group)
        body = (f"<!-- praxis-episode: {json.dumps(meta, ensure_ascii=False, separators=(',', ':'))} -->\n"
                f"# {str(ep.get('title') or 'Эпизод диалога').strip()}\n\n"
                f"_Статус: {status}; compact: {compact_id}_\n\n{summary}\n\n"
                f"## Events\n- " + "\n- ".join(meta["source_event_ids"]) + "\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        made.append(eid)
    return made


def plan_hot_fold(hot: list[dict], *, force: bool = False) -> dict:
    count = len(hot)
    token_rows = [int(x.get("tokens") or estimate_tokens(x.get("line", ""))) for x in hot]
    tokens = sum(token_rows)
    if count < 2:
        return {"due": False, "reason": "too_short", "count": count, "tokens": tokens}
    pressure = count >= HOT_HI or tokens > HOT_TOKEN_CAP or force
    if not pressure:
        return {"due": False, "reason": "within_window", "count": count, "tokens": tokens}
    token_pressure = tokens > HOT_TOKEN_CAP
    token_target = 1
    if token_pressure:
        suffix_tokens = tokens
        for i in range(1, count):
            suffix_tokens -= token_rows[i - 1]
            token_target = i
            if suffix_tokens <= HOT_TOKEN_CAP:
                break
    count_target = max(1, count - HOT_LO) if count >= HOT_HI or force else 1
    target = max(count_target, token_target)
    min_remaining = 1 if token_pressure or force else HOT_LO
    candidates = []
    for i in range(1, count):
        remaining = count - i
        if remaining < min_remaining or (token_pressure and i < token_target):
            continue
        gap = _epoch(hot[i].get("ts")) - _epoch(hot[i - 1].get("ts"))
        if gap >= EPISODE_GAP_SEC:
            candidates.append((abs(i - target), i, gap))
    if candidates:
        _, fold, gap = min(candidates)
        return {"due": True, "fold": fold, "continued": False, "reason": "episode_boundary",
                "gap_sec": gap, "count": count, "tokens": tokens}
    hard = token_pressure or count >= HOT_HARD_HI or force
    if not hard:
        return {"due": False, "reason": "open_episode", "count": count, "tokens": tokens}
    valid = [i for i in range(1, count)
             if count - i >= min_remaining and (not token_pressure or i >= token_target)
             and str(hot[i - 1].get("direction")) == "out"]
    fold = min(valid, key=lambda i: abs(i - target)) if valid else max(1, min(target, count - 1))
    return {"due": True, "fold": fold, "continued": True,
            "reason": "token_cap" if tokens > HOT_TOKEN_CAP else "hard_window",
            "count": count, "tokens": tokens}


def _frontier_input(meta: dict, chat_id: str | int) -> dict:
    return {"id": meta.get("id"), "text": compact_text(str(meta.get("id") or ""), chat_id),
            "salience": 3 if meta.get("continued") else 2,
            "preservation_priority": meta.get("preservation_priority") or 1.0,
            "ts": meta.get("created_at")}


def _fold_tiers(chat_id, state: dict) -> list[str]:
    made = []
    tier = 1
    # A bounded loop protects a corrupt state from spinning forever.
    for _ in range(16):
        same = sorted([x for x in state.get("frontier", []) if int(x.get("tier") or 1) == tier],
                      key=lambda x: (_epoch(x.get("first_ts")) or _epoch(x.get("created_at")), x.get("id", "")))
        if len(same) < TIER_HI:
            higher = [int(x.get("tier") or 1) for x in state.get("frontier", []) if int(x.get("tier") or 1) > tier]
            if higher:
                tier += 1
                continue
            break
        n = min(len(same), max(1, TIER_HI - TIER_LO))
        sources = same[:n]
        inputs = [_frontier_input(x, chat_id) for x in sources]
        result = _model_compact(inputs, tier=tier + 1,
                                depth=max(int(x.get("depth") or tier) for x in sources) + 1,
                                continued=any(bool(x.get("continued")) for x in sources))
        if not result:
            result = _fallback_compact(inputs, continued=any(bool(x.get("continued")) for x in sources))
        parent = _write_compact(
            chat_id, result, tier=tier + 1,
            depth=max(int(x.get("depth") or tier) for x in sources) + 1,
            source_events=[], source_compacts=[str(x.get("id")) for x in sources],
            event_count=sum(int(x.get("event_count") or 0) for x in sources),
            continued=any(bool(x.get("continued")) for x in sources),
            first_ts=sources[0].get("first_ts") or "", last_ts=sources[-1].get("last_ts") or "")
        remove = {x.get("id") for x in sources}
        state["frontier"] = [x for x in state["frontier"] if x.get("id") not in remove] + [parent]
        append_event("memory_compact", chat_id=chat_id, text=f"Tier {tier + 1}: {parent['id']}",
                     source="memory_life", refs=parent["source_compact_ids"],
                     meta={"compact_id": parent["id"], "tier": tier + 1,
                           "degraded": parent.get("degraded", False)})
        made.append(parent["id"])
        tier = 1
    return made


def compact_if_due(chat_id: str | int, *, force: bool = False) -> dict:
    """Compact one hot prefix and recursively fold warm tiers. Raw events are never removed.

    Сворачивается МЕСТО целиком: новые компакты пишутся под ключом места, а не ветки,
    иначе один разговор продолжал бы копить по свёртке на каждую свою ветку.
    """
    chat_id = adopt_place(chat_id)
    with _WRITE_LOCK:
        state = _load_state(chat_id, rebuild=True)
        plan = plan_hot_fold(state.get("hot") or [], force=force)
        if not plan.get("due"):
            return {"ok": True, "folded": 0, "plan": plan,
                    "hot": len(state.get("hot") or []), "tiers": []}
        fold = int(plan["fold"])
        inputs = list(state["hot"][:fold])
        result = _model_compact(inputs, tier=1, depth=1, continued=bool(plan.get("continued")))
        if not result:
            result = _fallback_compact(inputs, continued=bool(plan.get("continued")))
        manifest = result.get("_manifest") if isinstance(result, dict) else None
        omitted = {str(i) for i in ((manifest or {}).get("omitted") or [])}
        continued = bool(plan.get("continued"))
        if omitted:
            # Свёрнутым объявляем только непрерывный старый префикс, который реально
            # дошёл до модели. Невлезший хвост остаётся горячим и попадёт в следующую
            # свёртку — раньше он молча объявлялся покрытым и уходил из фронтира.
            inputs = [x for x in inputs if str(x.get("id")) not in omitted]
            fold = len(inputs)
            # И вместе с границей обязана переехать ПРАВДА о ней. `plan["continued"]`
            # посчитан для СТАРОЙ границы (например, реального разрыва в разговоре).
            # Обрезанная бюджетом свёртка по построению в этот разрыв не попадает — она
            # кончается посреди живой нити. Оставить continued=False значило бы записать
            # в компакт и в артефакт эпизода, что разговор закончен там, где он идёт.
            continued = True
            log.warning("life compact [%s]: %s событий не влезли в промпт — оставлены "
                        "горячими; эпизод помечен продолжающимся", chat_id, len(omitted))
        if not inputs:
            return {"ok": True, "folded": 0, "plan": plan,
                    "hot": len(state.get("hot") or []), "tiers": []}
        source_ids = [str(x.get("id")) for x in inputs]
        # Привязка пишется ДО компакта: компакт, чьи события ещё никуда не привязаны,
        # был бы неканоническим с первой секунды. Порядок здесь — не стиль, а условие
        # того, что записанное останется читаемым.
        origins = {str(x.get("chat")) for x in inputs if x.get("chat")}
        if any(not x.get("chat") for x in inputs):
            # Старое состояние без «chat» у части записей: где сказано, знает лента.
            # ⚠ Сравнивать РАЗМЕР множества ключей с числом событий нельзя: у любой
            # свёртки событий больше, чем веток, и полный проход по всей ленте шёл бы
            # на каждой свёртке (адверсарка 25.07).
            origins |= {str(item.get("chat_id")) for item in
                        iter_events(chat_id=chat_id, kinds={"conversation_message"})
                        if item.get("chat_id")}
        bind_place(chat_id, origins)
        meta = _write_compact(
            chat_id, result, tier=1, depth=1, source_events=source_ids, source_compacts=[],
            event_count=len(inputs), continued=continued,
            first_ts=str(inputs[0].get("ts") or ""), last_ts=str(inputs[-1].get("ts") or ""),
            manifest=(manifest if isinstance(manifest, dict) else None))
        episodes = _write_episodes(chat_id, meta["id"], inputs,
                                   [x for x in (result.get("episodes") or []) if isinstance(x, dict)],
                                   continued)
        state["hot"] = state["hot"][fold:]
        state["frontier"].append(meta)
        append_event("memory_compact", chat_id=chat_id,
                     text=f"Hot → {meta['id']}; {len(inputs)} событий; {plan.get('reason')}",
                     source="memory_life", refs=source_ids,
                     meta={"compact_id": meta["id"], "tier": 1, "episodes": episodes,
                           "continued": meta["continued"], "degraded": meta["degraded"]})
        higher = _fold_tiers(chat_id, state)
        _save_state(state)
        return {"ok": True, "folded": fold, "compact_id": meta["id"], "episodes": episodes,
                "plan": plan, "hot": len(state["hot"]), "tiers": higher,
                "degraded": meta["degraded"],
                "folded_lines": [str(item.get("line") or "") for item in inputs]}


def _canonical_compact_graph(chat_id: str | int) -> tuple[dict[str, tuple[dict, str]], bool]:
    """Return resolved non-legacy compacts bound to one place and its event spine.

    Место может состоять из нескольких ключей: пока ключ расщеплялся, свёртки одной
    комнаты писались под разными именами. Каждый компакт при этом проверяется под
    СВОИМ ключом — привязка «лежит там, где заявляет» не ослабляется ни на шаг;
    объединяется только результат.
    """
    expected_chat = str(chat_id)
    parsed: dict[str, tuple[dict, str]] = {}
    legacy_seen = False
    for member in _member_keys(expected_chat):
        cdir = _compact_dir(member)
        for path in sorted(cdir.glob("*.md")) if cdir.exists() else []:
            meta, recap = _read_compact_candidate(path, member)
            if not meta:
                continue
            if meta["legacy"]:
                legacy_seen = True
                continue
            parsed[meta["id"]] = (meta, recap)

    evidence = memory_provenance.claim_evidence_index(MEM_DIR)
    strict_compacts = evidence.get("compacts") or {}
    # One resolver owns the trust decision.  The local reader above is only a
    # presentation parser; it must not admit a compact that the global,
    # duplicate-aware provenance index rejected or interpreted differently.
    parsed = {
        compact_id: item
        for compact_id, item in parsed.items()
        if strict_compacts.get(compact_id) == item[0]
        and memory_provenance.compact_evidence(compact_id, evidence).get("valid") is True
    }
    events = evidence.get("events") or {}
    resolved: dict[str, bool] = {}

    def valid(compact_id: str, stack: frozenset[str] = frozenset()) -> bool:
        if compact_id in resolved:
            return resolved[compact_id]
        if compact_id in stack or compact_id not in parsed:
            return False
        meta = parsed[compact_id][0]
        next_stack = stack | {compact_id}
        if meta["source_event_ids"]:
            ok = True
            for event_id in meta["source_event_ids"]:
                row = events.get(event_id)
                if (not isinstance(row, dict)
                        or row.get("schema") != "praxis.life.event.v1"
                        or row.get("id") != event_id
                        or not _same_conversation(row.get("chat_id"), meta["chat_id"])
                        or row.get("kind") != "conversation_message"):
                    ok = False
                    break
        else:
            children = [parsed.get(parent_id) for parent_id in meta["source_compact_ids"]]
            ok = bool(children) and all(
                child is not None
                and child[0]["tier"] < meta["tier"]
                and child[0]["depth"] < meta["depth"]
                and valid(parent_id, next_stack)
                for parent_id, child in zip(meta["source_compact_ids"], children)
            )
            if ok:
                ok = meta["event_count"] == sum(
                    child[0]["event_count"] for child in children if child is not None)
        resolved[compact_id] = bool(ok)
        return bool(ok)

    canonical = {compact_id: item for compact_id, item in parsed.items() if valid(compact_id)}
    return canonical, legacy_seen


def rebuild_state(chat_id: str | int) -> dict:
    """Reconstruct hot/frontier solely from JSONL events and immutable Markdown compacts.

    Восстанавливается состояние МЕСТА: события собираются по всем его ключам, покрытыми
    считаются те, что уже вошли в любой канонический компакт места. Событие не может
    оказаться ни горячим, ни свёрнутым одновременно и не может пропасть — на этом
    инварианте держится вся миграция ключа.
    """
    chat_id = place_key(chat_id)
    # Состав места, на котором мы сейчас пересоберём курсор, закрепляется в журнале.
    # Иначе достижимость исторических компактов из комнаты держалась бы на одном
    # реестре: он вправе уточниться, и блок молча выпал бы из её сводки — при том что
    # сам компакт остаётся каноническим (вторая адверсарка 25.07).
    bind_place(chat_id, _member_keys(chat_id))
    state = _default_state(chat_id)
    messages = iter_events(chat_id=chat_id, kinds={"conversation_message"})
    canonical, legacy_seen = _canonical_compact_graph(chat_id)
    metas = [item[0] for item in canonical.values()]
    covered_events = {str(e) for m in metas for e in (m.get("source_event_ids") or [])}
    consumed_compacts = {str(c) for m in metas for c in (m.get("source_compact_ids") or [])}
    state["hot"] = [{"id": r["id"], "ts": r.get("ts"), "line": r.get("text", ""),
                     "actor": r.get("actor", ""), "direction": r.get("direction", ""),
                     "salience": r.get("salience", 2), "tokens": estimate_tokens(r.get("text", "")),
                     "source": r.get("source"), "source_id": r.get("source_id"),
                     "chat": r.get("chat_id")}
                    for r in messages if str(r.get("id")) not in covered_events]
    state["frontier"] = [m for m in metas if str(m.get("id")) not in consumed_compacts]
    state["dedupe"] = [{"key": r.get("dedupe_key"), "id": r.get("id")} for r in messages
                       if r.get("dedupe_key")][-400:]
    state["bootstrap_v1"] = legacy_seen or any(
        r.get("source") == "legacy_buffer" for r in messages)
    _save_state(state)
    return state


def is_state_file(path: Path) -> bool:
    """Это состояние разговора, а не чужой файл в том же каталоге?

    ⚠ `.state/life` — общий каталог. В нём лежит, например, `formation.request.json` —
    заявка Егора на переплавку, которую НИЧЕМ не восстановишь: она не производная от
    ленты. Прежний фильтр смотрел только на имя, `_safe` переписывал точку в
    подчёркивание, и заявка опознавалась как «расщеплённый ключ разговора». Смотрим
    внутрь файла: состояние — это `hot`-список и строковый `chat_id` (адверсарка 25.07).
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (isinstance(data, dict) and isinstance(data.get("hot"), list)
            and isinstance(data.get("chat_id"), str))


def state_keys() -> list[str]:
    """Ключи разговоров, у которых есть состояние. Чужие файлы каталога не считаются."""
    if not STATE_DIR.exists():
        return []
    return sorted(path.stem for path in STATE_DIR.glob("*.json") if is_state_file(path))


def retire_split_states(*, dry_run: bool = False) -> dict:
    """Убрать состояния, оставшиеся от расщеплённого ключа. Не удаляет — отодвигает.

    Состояние читается по ключу МЕСТА, поэтому файлы веток после миграции никто не
    открывает. Оставить их — значит держать каталог, который утверждает, что у неё 243
    разговора, когда их 35. Переносим в `_pre_places/`: производное восстанавливается
    из событий и компактов, а откат должен оставаться возможным без бэкапа.

    Вызывается миграцией явно. Ничего не двигать само на импорте — данные не переезжают
    втихую.
    """
    moved, kept = [], []
    if not STATE_DIR.exists():
        return {"moved": [], "kept": [], "dry_run": dry_run}
    attic = STATE_DIR / "_pre_places"
    for path in sorted(STATE_DIR.glob("*.json")):
        key = path.stem
        if not is_state_file(path):
            continue                      # чужой файл в общем каталоге — не наше дело
        if _safe(place_key(key)) == key:
            kept.append(key)
            continue
        moved.append(key)
        if dry_run:
            continue
        attic.mkdir(parents=True, exist_ok=True)
        try:
            path.replace(attic / path.name)
        except OSError:
            log.debug("состояние %s не отодвинулось", key, exc_info=True)
    return {"moved": moved, "kept": kept, "dry_run": dry_run}


def context_summary(chat_id: str | int, max_chars: int = 7000) -> str:
    """Current logarithmic frontier for the prompt. No raw event is treated as more truthful."""
    if not _state_path(chat_id).exists() and not any(
            _compact_dir(member).exists() for member in _member_keys(chat_id)):
        return ""  # cold pre-PASS19/test tree: do not create runtime state merely by reading
    canonical, _legacy_seen = _canonical_compact_graph(chat_id)
    consumed = {
        source_id for meta, _recap in canonical.values()
        for source_id in meta["source_compact_ids"]
    }
    frontier = sorted((item for compact_id, item in canonical.items() if compact_id not in consumed),
                      key=lambda item: (_epoch(item[0].get("first_ts"))
                                        or _epoch(item[0].get("created_at")), item[0].get("id", "")))
    blocks = []
    for meta, recap in frontier:
        quality = "DEGRADED SOURCE EXCERPT" if meta["degraded"] else "CURRENT RECAP"
        blocks.append(f"[compact {meta['id']} · tier {meta['tier']} · depth {meta['depth']}"
                      f"{' · continued' if meta['continued'] else ''} · {quality}]\n{recap}")
    if not blocks:
        return ""
    try:
        limit = max(0, int(max_chars))
    except (TypeError, ValueError):
        limit = 7000
    if limit <= len(_CONTINUITY_WARNING) + 2:
        return _CONTINUITY_WARNING[:limit]
    remaining = limit - len(_CONTINUITY_WARNING) - 2
    # ⚠ Здесь было `body[-remaining:]` — сырой посимвольный хвост склеенных блоков.
    # Под давлением бюджета сводка начиналась посреди фразы, а заголовок первого
    # компакта превращался в огрызок вроде `-8a18ea57`, который модель могла
    # процитировать как провенанс. Пакуем ЦЕЛЫМИ блоками от свежих к старым и честно
    # называем, сколько выпало, вместо того чтобы делать вид, что не выпало ничего.
    kept: list[str] = []
    used = 0
    for block in reversed(blocks):
        if kept and used + len(block) + 2 > remaining:
            break
        kept.append(block)
        used += len(block) + 2
    kept.reverse()
    dropped = len(blocks) - len(kept)
    head = _CONTINUITY_WARNING
    if dropped:
        head += (f"\n[СВОДКА ОБРЕЗАНА БЮДЖЕТОМ: показаны {len(kept)} компактов из "
                 f"{len(blocks)}; более ранние есть на диске и достаются recall]")
    body = "\n\n".join(kept)
    if len(body) > remaining:      # один блок крупнее всего бюджета — режем его явно
        body = body[:max(0, remaining - 40)] + "\n…[КОМПАКТ ОБРЕЗАН]"
    return (head + "\n\n" + body).strip()


def provenance_for_path(path: str | Path) -> list[str]:
    p = Path(path)
    meta = read_artifact_meta(p)
    return [str(x) for x in (meta.get("source_event_ids") or []) +
            (meta.get("source_compact_ids") or [])]


def stream_status(chat_id: str | int) -> dict:
    state = _load_state(chat_id, rebuild=True)
    tiers: dict[str, int] = {}
    for item in state.get("frontier") or []:
        key = str(item.get("tier") or 1)
        tiers[key] = tiers.get(key, 0) + 1
    return {"chat_id": str(chat_id), "hot": len(state.get("hot") or []),
            "hot_tokens": sum(int(x.get("tokens") or 0) for x in state.get("hot") or []),
            "frontier": len(state.get("frontier") or []), "tiers": tiers,
            "bootstrap": bool(state.get("bootstrap_v1"))}


def status() -> dict:
    streams = []
    for p in sorted(STATE_DIR.glob("*.json")) if STATE_DIR.exists() else []:
        if p.name in ("formation.json", "formation.request.json"):
            continue
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
            streams.append(stream_status(st.get("chat_id") or p.stem))
        except Exception:
            continue
    events = iter_events()
    return {"events": len(events), "streams": streams,
            "compacts": len(list(COMPACTS_DIR.glob("*/*.md"))) if COMPACTS_DIR.exists() else 0,
            "episodes": len(list(EPISODES_DIR.glob("*/*.md"))) if EPISODES_DIR.exists() else 0,
            "hot_lo": HOT_LO, "hot_hi": HOT_HI, "token_cap": HOT_TOKEN_CAP}


def _cli() -> None:
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "bootstrap":
        print(json.dumps(bootstrap_all(), ensure_ascii=False, indent=2))
    elif cmd == "rebuild":
        chats = sys.argv[2:] or [x.get("chat_id") for x in status().get("streams", [])]
        print(json.dumps([stream_status(c) if rebuild_state(c) else {} for c in chats],
                         ensure_ascii=False, indent=2))
    elif cmd == "compact":
        if len(sys.argv) < 3:
            raise SystemExit("usage: python -m memory_life compact <chat_id> [--force]")
        print(json.dumps(compact_if_due(sys.argv[2], force="--force" in sys.argv),
                         ensure_ascii=False, indent=2))
    else:
        print(json.dumps(status(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()
