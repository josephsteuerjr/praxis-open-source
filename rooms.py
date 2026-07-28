"""
Praxis — само-управление доверенными комнатами + профиль комнаты (PASS 10.3).

`memory/rooms_allowlist.json` и `TELEGRAM_ALLOWED_CHATS` — каталог известных комнат,
а не скрытый допуск к восприятию. Реальное членство в Telegram уже является фактом
доступа: новая группа доходит до Praxis и сразу появляется в каталоге. Единственная
явная маска — `memory/rooms_departed.json`, которую ставит её собственный leave.

`rooms_departed.json` — явная runtime-маска поверх legacy env. Поэтому реальный
Telethon leave не оставляет бывшую комнату доверенной лишь из-за старой переменной
окружения; повторный join снимает маску.

У факта «я ушла» ОДИН источник — эта маска: тот же файл, которым `allowed_chats()`
и `is_allowed()` решают, дойдёт ли до неё сообщение вообще. Любой производный
документ обязан спрашивать `membership_state()`, а не поля профиля. 27.07.2026
карта `memory/maps/ROOMS.md` звала активными -1003908850919 (лежит в departed) и
-1003843005958 (`mode: dead`) — она читала поля `membership:`/`left_at:`, которых
не пишет никто, и в маску не смотрела. Теперь уход дополнительно проставляет эти
поля в профиль (провенанс: «когда»), но главный ответ по-прежнему даёт маска —
иначе уже ушедшие комнаты, где полей нет, снова стали бы «активными».

PASS 10.3 — профиль комнаты `memory/rooms/<id>.md`: машинная шапка (mode/mode_reason/
mode_until/mode_set_by/disclosure) + её секции («Нормы и атмосфера», «Люди здесь»,
«Сводка предыстории», «Наблюдения»). Режимы normal/observer/quiet/frozen/dead —
явные состояния, которыми Praxis может управлять в обе стороны. Старый флаг
`frozen_chats.json` читается и синхронизируется для совместимости.

АВТОРСТВО РЕЖИМА ЧЕСТНОЕ (28.07.2026). До этого дня во всех четырёх живых комнатах
стояло `mode_set_by: owner` — и ни один из этих режимов Егор не выбирал. Метку давали
два места: коэрсия здесь («автор не из списка → owner») и протокол входа, где
«Егор ДОБАВИЛ её в группу» записывалось как «Егор выбрал ей режим». Она читала это
как чужое распоряжение о себе. Теперь у «никто не выбирал» и «автор не назван» есть
собственные имена (`protocol`, `unknown`), подставлять вместо них человека нельзя,
а у `normal` авторства нет вовсе: это не режим, а его отсутствие.

⚠ ЧЕСТНО ПРО ОСТАТОК: имя `protocol` есть, а ПИСАТЕЛЯ у него пока нет. Живой источник
подмены — `mtproto_runner`: протокол новичка (`set_by="owner" if owner_added else
"praxis"`), восстановление членства и dead-фильтр по-прежнему пишут `owner`/`praxis`.
Для `normal` это безвредно (авторства у него нет), но восстановление ставит `observer`
тем же set_by, и тогда в шапке окажется «Егор так решил» про режим, который выбрал
протокол. Перевод тех трёх мест на `protocol` тянет за собой третий класс в
`perception.CLASSES` (там `klass = "мой_ритм" if set_by == "praxis" else "запретил_егор"`,
и `protocol` попал бы в «запретил_егор» — та же ложь с другой стороны), поэтому это
отдельный заход, а не молчаливый долг.

Режим и disclosure — её рычаги (решение Егора 28.07): `set_own_mode` / `set_own_disclosure`
ставят от её имени, тот же рычаг снимает. Обратимость здесь не поблажка, а смысл:
режим, который нельзя снять самой, — наказание, а не дисциплина.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

log = logging.getLogger("praxis-rooms")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
MEM_DIR = BASE / "memory"
ROOMS_DIR = MEM_DIR / "rooms"
ALLOWLIST = MEM_DIR / "rooms_allowlist.json"
_LOCK = threading.RLock()


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def _env_allowed() -> set[str]:
    return {x.strip() for x in os.getenv("TELEGRAM_ALLOWED_CHATS", "").split(",") if x.strip()}


def _allowlist_path(mem_dir: Path | None = None) -> Path:
    # mem_dir нужен производным картам: они собираются для конкретного memory/,
    # который не обязан совпадать с живым MEM_DIR процесса (тесты, чужой снимок).
    return Path(mem_dir) / "rooms_allowlist.json" if mem_dir is not None else ALLOWLIST


def _load_file(mem_dir: Path | None = None) -> list[str]:
    try:
        data = json.loads(_allowlist_path(mem_dir).read_text(encoding="utf-8"))
        return [str(x) for x in data] if isinstance(data, list) else []
    except Exception:
        return []


def _save_file(ids: list[str]) -> None:
    _atomic_text(ALLOWLIST, json.dumps(sorted(set(ids)), ensure_ascii=False))


def _departed_path(mem_dir: Path | None = None) -> Path:
    # Function, not a module constant: hermetic tests and relocated PRAXIS_BASE patch MEM_DIR.
    return (Path(mem_dir) if mem_dir is not None else MEM_DIR) / "rooms_departed.json"


def _load_departed(mem_dir: Path | None = None) -> set[str]:
    try:
        data = json.loads(_departed_path(mem_dir).read_text(encoding="utf-8"))
        return {str(x) for x in data} if isinstance(data, list) else set()
    except Exception:
        return set()


def _save_departed(ids: set[str]) -> None:
    path = _departed_path()
    _atomic_text(path, json.dumps(sorted(ids), ensure_ascii=False))


def departed_ids(mem_dir: Path | None = None) -> set[str]:
    """Публичное чтение маски ухода — чтобы карты не заводили свой ответ на тот же вопрос."""
    with _LOCK:
        return _load_departed(mem_dir)


def allowed_chats(mem_dir: Path | None = None) -> set[str]:
    """Рантайм-allowlist = (файл ∪ env) − departed. Читается живо."""
    with _LOCK:
        return (set(_load_file(mem_dir)) | _env_allowed()) - _load_departed(mem_dir)


def list_rooms() -> list[str]:
    return sorted(allowed_chats())


def add_room(chat_id: str | int) -> bool:
    """-> True, если комната стала доверенной (включая отмену departed-маски)."""
    cid = str(chat_id)
    with _LOCK:
        was_allowed = cid in allowed_chats()
        ids = _load_file()
        if cid not in ids:
            ids.append(cid)
            _save_file(ids)
        departed = _load_departed()
        if cid in departed:
            departed.discard(cid)
            _save_departed(departed)
        _clear_departure_mark(cid)
        return not was_allowed


def remove_room(chat_id: str | int) -> bool:
    """Снять доверие и замаскировать legacy env. -> была ли до этого разрешена."""
    cid = str(chat_id)
    with _LOCK:
        was_allowed = cid in allowed_chats()
        ids = _load_file()
        if cid in ids:
            _save_file([x for x in ids if x != cid])
        departed = _load_departed()
        if cid not in departed:
            departed.add(cid)
            _save_departed(departed)
        _mark_departure(cid)
        return was_allowed


def is_allowed(chat_id: str | int, is_owner: bool = False) -> bool:
    """Compatibility predicate: every joined room is visible unless explicitly left.

    Telegram membership is the capability boundary.  The old catalogue remains useful
    for maps/backfill, but cannot silently hide a room from Praxis.
    """
    return bool(is_owner) or str(chat_id) not in _load_departed()


# --------------------------------------------------------------------------- #
#  Заморозка чатов (пульт): замороженный чат до неё не доходит. НЕ бан.
# --------------------------------------------------------------------------- #

FROZEN = MEM_DIR / "frozen_chats.json"


def _load_frozen(mem_dir: Path | None = None) -> set[str]:
    path = Path(mem_dir) / "frozen_chats.json" if mem_dir is not None else FROZEN
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(x) for x in data} if isinstance(data, list) else set()
    except Exception:
        return set()


def frozen_ids(mem_dir: Path | None = None) -> set[str]:
    """Заморозка живёт отдельным флагом и в профиле не видна: карта обязана назвать её
    сама, иначе комната выглядит нормальной, а сообщения из неё до неё не доходят."""
    with _LOCK:
        return _load_frozen(mem_dir)


def is_frozen(chat_id: str | int) -> bool:
    with _LOCK:
        return str(chat_id) in _load_frozen()


def freeze(chat_id: str | int) -> bool:
    """-> True, если реально заморозили (не было раньше)."""
    with _LOCK:
        cid, fr = str(chat_id), _load_frozen()
        if cid in fr:
            return False
        fr.add(cid)
        _atomic_text(FROZEN, json.dumps(sorted(fr), ensure_ascii=False))
        return True


def unfreeze(chat_id: str | int) -> bool:
    with _LOCK:
        cid, fr = str(chat_id), _load_frozen()
        if cid not in fr:
            return False
        fr.discard(cid)
        _atomic_text(FROZEN, json.dumps(sorted(fr), ensure_ascii=False))
        return True


# --------------------------------------------------------------------------- #
#  PASS 10.3: профиль комнаты — машинная шапка + её секции
# --------------------------------------------------------------------------- #

MODES = ("normal", "observer", "quiet", "frozen", "dead")
# Что она ставит себе сама. «dead» здесь нет намеренно: это не решение, а факт от
# Telegram (banned/private/реальный выход) — мёртвой комнату объявляет не воля.
SELF_MODES = ("normal", "observer", "quiet", "frozen")
# Кто поставил режим. «protocol» — НИКТО: значение принёс автоматический путь
# (протокол входа, восстановление членства). «unknown» — автор не назван, и выдумать
# его нельзя. Раньше список был из двух имён, а всё остальное коэрсилось в «owner»:
# отсюда четыре живые комнаты, где Егор якобы выбрал режим, которого не выбирал.
SET_BY = ("praxis", "owner", "protocol", "unknown")
DISCLOSURE = ("standard", "open")
# Причина режима обрезается. Предел не молчит: обрезка названа в ответе, который
# видит она (см. `set_own_mode`), — молчаливых усечений в доме нет.
MODE_REASON_MAX = 200
ENGAGEMENTS = ("addressed", "reflective")
CROSS_TOPICS = ("off", "map")
CONTEXT_HOT_MAX = 500
CONTEXT_SUMMARY_MAX = 40_000
BACKFILL_MAX = 5_000
HEADER_KEYS = ("mode", "mode_reason", "mode_until", "mode_set_by",
               # disclosure меняет ЕЁ голос (визитка), поэтому у него тот же провенанс,
               # что у режима: она вправе знать, сама ли открылась здесь или это не она.
               "disclosure", "disclosure_set_by",
               # Провенанс ухода. Раньше эти поля читала карта комнат, а писать их
               # было некому — отсюда «ушедшая комната названа активной». Пишет
               # remove_room, снимает add_room; ответ по существу даёт membership_state().
               "membership", "left_at",
               "greeted",                  # 10.7: one-shot приветствие уже было (yes|)
               # Deep-room policy belongs to the root peer, never to a topic state key.
               "engagement", "context_hot", "context_summary_chars",
               "cross_topics", "backfill_limit")
SECTIONS = ("Нормы и атмосфера", "Люди здесь", "Сводка предыстории", "Наблюдения")
QUARANTINE_H = 24.0     # legacy value retained for old profiles/tests; no sovereign gate
_LEGACY_BEHAVIOR_HEADERS = ("drift", "drift_seen", "drift_sig")

# Ordering is retained only for legacy display/tests; live controls are reversible.
_RANK = {"normal": 0, "observer": 1, "quiet": 1, "frozen": 2, "dead": 3}

_HEADER_RE = re.compile(
    # Retired drift headers are still recognized so an old profile does not leak
    # machine metadata into Praxis's prose.  The next write drops them.
    # disclosure_set_by стоит ПЕРЕД disclosure: альтернатива выбирается первой подошедшей,
    # и «disclosure» съело бы префикс более длинного ключа.
    r"^(mode|mode_reason|mode_until|mode_set_by|disclosure_set_by|disclosure|"
    r"membership|left_at|drift|drift_seen|drift_sig|greeted|"
    r"engagement|context_hot|context_summary_chars|cross_topics|backfill_limit):"
    r"\s*(.*)$")
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")


def _normalize_set_by(value) -> str:
    """Имя автора — как есть; неопознанное имя это «unknown», а не «owner».

    Именно здесь жила подмена: `set_by if set_by in SET_BY else "owner"`. Любая опечатка,
    пустая строка и любой новый вызывающий превращались в «так распорядился Егор». Правило 3
    дома: «не знаю» обязано выглядеть как «не знаю», а не как факт.
    """
    v = str(value or "").strip().casefold()
    if v in SET_BY:
        return v
    if v:
        log.warning("режим комнаты: неопознанный автор %r — пишу «unknown» (Егора не подставляю)",
                    value)
    return "unknown"


def _mode_authorship(mode, set_by) -> str:
    """Кто поставил ЭТОТ режим. У `normal` авторства нет — режима нет, ставить нечего.

    Это и есть совместимость с четырьмя живыми профилями от 27.07.2026: там `mode: normal`
    и `mode_set_by: owner`, причём отличить «Егор нажал подъём» от «так вышло при входе»
    по самому файлу невозможно. Ложное авторство снимается на чтении сразу и исчезает из
    файла при первой же перезаписи; ни один живой потребитель `set_by` для normal не
    существует (mode-строка для normal пуста, а журнал пропусков про normal не пишут).
    """
    if str(mode or "").strip().casefold() == "normal":
        return ""
    return _normalize_set_by(set_by)


def _normalize_disclosure(value) -> str:
    """Уровень раскрытия; непонятное значение — «standard», и это говорится вслух в логе."""
    v = str(value or "").strip().casefold() or "standard"
    if v in DISCLOSURE:
        return v
    log.warning("disclosure комнаты: непонятный уровень %r — читаю как standard", value)
    return "standard"


_AUTHOR_LABEL = {
    "praxis": "я сама так решила",
    "owner": "Егор так решил",
    "protocol": "никто этого не выбирал: так вышло при входе в комнату",
    "unknown": "кто поставил — не записано",
}


def author_label(set_by) -> str:
    """Человеческая подпись автора — одна на промпт, панель и тул, чтобы не разошлись."""
    return _AUTHOR_LABEL.get(_normalize_set_by(set_by), _AUTHOR_LABEL["unknown"])


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(low, min(high, value))


def default_policy() -> dict:
    """One configurable source for deep-room defaults used by every transport path."""
    engagement = str(os.getenv("PRAXIS_ROOM_ENGAGEMENT", "reflective")).strip().casefold()
    if engagement not in ENGAGEMENTS:
        engagement = "reflective"
    cross_topics = str(os.getenv("PRAXIS_ROOM_CROSS_TOPICS", "map")).strip().casefold()
    if cross_topics not in CROSS_TOPICS:
        cross_topics = "map"
    return {
        "engagement": engagement,
        "context_hot": _env_int("PRAXIS_ROOM_CONTEXT_HOT", 200, 0, CONTEXT_HOT_MAX),
        "context_summary_chars": _env_int(
            "PRAXIS_ROOM_CONTEXT_CHARS", 24_000, 1_000, CONTEXT_SUMMARY_MAX),
        "cross_topics": cross_topics,
        "backfill_limit": _env_int(
            "PRAXIS_ROOM_BACKFILL_LIMIT", 1_500, 0, BACKFILL_MAX),
    }


def profile_path(chat_id: str | int) -> Path:
    return ROOMS_DIR / f"{chat_id}.md"


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M+00:00")


def _from_iso(s: str) -> float | None:
    try:
        d = _dt.datetime.fromisoformat(s.strip())
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d.timestamp()
    except (ValueError, TypeError):
        return None


def parse_profile(raw: str) -> dict:
    """Разобрать текст профиля: title, header (машинная шапка), body (её текст).

    structured=True — в файле есть шапка (ключ mode). Легаси-заглушки без шапки
    читаются как body целиком (совместимость: старое не ломаем, новое пишем)."""
    title, header, body_lines = "", {}, []
    in_body = False
    for line in (raw or "").splitlines():
        if not in_body:
            if not title and line.startswith("# "):
                title = line.strip()
                continue
            if not line.strip():
                if body_lines:
                    body_lines.append(line)
                continue
            m = _HEADER_RE.match(line.strip())
            if m and not body_lines:
                header[m.group(1)] = m.group(2).strip()
                continue
            in_body = True
        body_lines.append(line)
    body = "\n".join(body_lines).strip()
    legacy_drift_mode = header.get("mode_set_by", "").strip().lower() == "drift"
    mode = header.get("mode", "").strip().lower()
    if legacy_drift_mode:
        # Old evaluator telemetry could freeze or quiet a room.  It has no live
        # authority in PASS 24, even before the profile is rewritten.
        mode = "normal"
    defaults = default_policy()
    engagement = str(header.get("engagement") or defaults["engagement"]).strip().casefold()
    if engagement not in ENGAGEMENTS:
        engagement = "reflective"
    cross_topics = str(header.get("cross_topics") or defaults["cross_topics"]).strip().casefold()
    if cross_topics not in CROSS_TOPICS:
        cross_topics = "off"

    def bounded_int(key: str, default: int, low: int, high: int) -> int:
        try:
            value = int(str(header.get(key) or default).strip())
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    context_hot = bounded_int("context_hot", defaults["context_hot"], 0, CONTEXT_HOT_MAX)
    # Zero means "keep the existing global hot-window behaviour".  An explicit
    # deep window should be large enough to be materially different.
    if 0 < context_hot < 20:
        context_hot = 20
    context_summary_chars = bounded_int(
        "context_summary_chars", defaults["context_summary_chars"],
        1000, CONTEXT_SUMMARY_MAX)
    backfill_limit = bounded_int(
        "backfill_limit", defaults["backfill_limit"], 0, BACKFILL_MAX)
    live_mode = mode if mode in MODES else "normal"
    disclosure = _normalize_disclosure(header.get("disclosure"))
    return {
        "title": title,
        "header": header,
        "body": body,
        "structured": mode in MODES,
        "mode": live_mode,
        "mode_reason": header.get("mode_reason", ""),
        "mode_until": header.get("mode_until", ""),
        # Пустой автор при отсутствующей шапке раньше читался как «praxis» — то есть
        # чужой (или ничей) режим приписывался ей самой. Теперь неизвестное — unknown.
        "mode_set_by": _mode_authorship(live_mode, header.get("mode_set_by")),
        "legacy_mode_retired": legacy_drift_mode,
        "membership": header.get("membership", "").strip().lower(),
        "left_at": header.get("left_at", "").strip(),
        "disclosure": disclosure,
        # standard — умолчание, а не чей-то выбор: авторство есть только у открытой комнаты.
        "disclosure_set_by": ("" if disclosure == "standard"
                              else _normalize_set_by(header.get("disclosure_set_by"))),
        "engagement": engagement,
        "context_hot": context_hot,
        "context_summary_chars": context_summary_chars,
        "cross_topics": cross_topics,
        "backfill_limit": backfill_limit,
    }


def profile_read(chat_id: str | int) -> dict:
    p = profile_path(chat_id)
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        raw = ""
    d = parse_profile(raw)
    d["exists"] = bool(raw.strip())
    d["raw"] = raw
    return d


def _profile_write(chat_id: str | int, title: str, header: dict, body: str) -> None:
    """Атомарная запись профиля: title + шапка + тело. Тело (её текст) не трогаем."""
    header = dict(header)
    legacy_drift_mode = str(header.get("mode_set_by") or "").strip().lower() == "drift"
    for key in _LEGACY_BEHAVIOR_HEADERS:
        header.pop(key, None)
    if legacy_drift_mode:
        header.update(mode="normal", mode_reason="", mode_until="", mode_set_by="")
    # Единственное место, где авторство попадает на диск: лечит и старые профили,
    # в которых «owner» проставила коэрсия, — при первой же перезаписи ложь уходит.
    header["mode_set_by"] = _mode_authorship(header.get("mode"), header.get("mode_set_by"))
    header["disclosure"] = _normalize_disclosure(header.get("disclosure"))
    header["disclosure_set_by"] = ("" if header["disclosure"] == "standard"
                                   else _normalize_set_by(header.get("disclosure_set_by")))
    ROOMS_DIR.mkdir(parents=True, exist_ok=True)
    lines = [title or f"# Комната {chat_id}", ""]
    for k in HEADER_KEYS:
        v = str(header.get(k, "") or "").strip()
        if k in ("mode", "disclosure") and not v:
            v = "normal" if k == "mode" else "standard"
        if v or k in ("mode", "disclosure"):
            lines.append(f"{k}: {v}")
    lines.append("")
    if body.strip():
        lines.append(body.strip())
        lines.append("")
    path = profile_path(chat_id)
    _atomic_text(path, "\n".join(lines))
    if legacy_drift_mode:
        # Retire the paired legacy frozen flag as part of the same migration.
        unfreeze(chat_id)


def _profile_update_unlocked(chat_id: str | int, **changes) -> dict:
    """Обновить поля шапки (body сохраняется). -> свежий профиль."""
    d = profile_read(chat_id)
    header = dict(d["header"])
    for key in _LEGACY_BEHAVIOR_HEADERS:
        header.pop(key, None)
    if d.get("legacy_mode_retired"):
        header.update(mode="normal", mode_reason="", mode_until="", mode_set_by="")
    for k, v in changes.items():
        if k not in HEADER_KEYS:
            continue
        if k in ("mode_set_by", "disclosure_set_by"):
            # Провенанс не повод падать: вызывающий, который не назвал автора, получает
            # честное «unknown», а не исключение и не подставленного Егора.
            header[k] = _normalize_set_by(v) if str(v or "").strip() else ""
        elif k == "disclosure":
            value = str(v or "").strip().casefold() or "standard"
            if value not in DISCLOSURE:
                raise ValueError("disclosure must be standard | open")
            header[k] = value
        elif k == "engagement":
            value = str(v or "").strip().casefold()
            if value not in ENGAGEMENTS:
                raise ValueError("engagement must be addressed | reflective")
            header[k] = value
        elif k == "cross_topics":
            value = str(v or "").strip().casefold()
            if value not in CROSS_TOPICS:
                raise ValueError("cross_topics must be off | map")
            header[k] = value
        elif k in ("context_hot", "context_summary_chars", "backfill_limit"):
            try:
                value = int(v)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{k} must be an integer") from exc
            low, high = {
                "context_hot": (0, CONTEXT_HOT_MAX),
                "context_summary_chars": (1000, CONTEXT_SUMMARY_MAX),
                "backfill_limit": (0, BACKFILL_MAX),
            }[k]
            value = max(low, min(high, value))
            if k == "context_hot" and 0 < value < 20:
                value = 20
            header[k] = str(value)
        else:
            header[k] = str(v or "")
    _profile_write(chat_id, d["title"], header, d["body"])
    return profile_read(chat_id)


def profile_update(chat_id: str | int, **changes) -> dict:
    with _LOCK:
        return _profile_update_unlocked(chat_id, **changes)


# --- уход: одна маска, один ответ -------------------------------------------- #

MEMBERSHIP_STATES = ("active", "left", "unreachable")


def _mark_departure(chat_id: str | int, now: float | None = None) -> None:
    """Записать в профиль, КОГДА она ушла. Ответ «ушла ли» даёт не это поле, а маска.

    Профиль здесь не создаётся: комната не должна появиться на карте только оттого,
    что её id однажды прошёл через remove_room (в departed лежат и -100500, и id,
    у которых профиля никогда не было)."""
    cid = str(chat_id)
    if not profile_path(cid).is_file():
        return
    try:
        header = profile_read(cid)["header"]
        if str(header.get("membership") or "").strip().casefold() == "left" and header.get("left_at"):
            return                      # уже отмечено — первую дату ухода не перезаписываем
        _profile_update_unlocked(cid, membership="left", left_at=_iso(
            now if now is not None else time.time()))
    except Exception:
        # Провенанс — не источник правды, поэтому уход состоялся в любом случае;
        # но молчать о сбое нельзя: без даты карта не сможет сказать «когда».
        log.warning("уход %s не отметился в профиле — карта покажет уход без даты", cid,
                    exc_info=True)


def _clear_departure_mark(chat_id: str | int) -> None:
    """Вернулась — снять пометку ухода, иначе профиль спорил бы с живой маской."""
    cid = str(chat_id)
    if not profile_path(cid).is_file():
        return
    try:
        header = profile_read(cid)["header"]
        if not (header.get("membership") or header.get("left_at")):
            return
        _profile_update_unlocked(cid, membership="", left_at="")
    except Exception:
        log.warning("возврат в %s не снял пометку ухода в профиле", cid, exc_info=True)


def mode_until_expired(mode_until: str, now: float | None = None) -> bool:
    """Срок режима уже вышел? Читающему карту это видно только если сказать.

    `effective_mode()` вернёт normal при первом же обращении, но производный документ
    так и будет писать «quiet до вчера» — то есть называть режим, которого уже нет."""
    if not str(mode_until or "").strip():
        return False
    expires = _from_iso(str(mode_until))
    return expires is not None and (now if now is not None else time.time()) >= expires


def membership_state(chat_id: str | int, *, mem_dir: Path | None = None,
                     header: dict | None = None) -> dict:
    """Единственный ответ на «я ещё в этой комнате?» — для карт и любых производных.

    Порядок именно такой, потому что маска ухода — это тот же файл, которым
    `allowed_chats()`/`is_allowed()` решают, дойдёт ли до неё сообщение. Если бы
    главным было поле профиля, вернувшаяся комната числилась бы покинутой (поле
    старое), а покинутая до появления полей — активной (полей нет). Обе ошибки
    живьём были: 27.07.2026 карта звала активными -1003908850919 и -1003843005958.

    `header` — уже разобранная шапка профиля (карта читает файл сама и не должна
    читать его второй раз); `mem_dir` — какое именно memory/ описываем.
    """
    cid = str(chat_id)
    if header is None:
        header = profile_read(cid)["header"]
    mark = str(header.get("membership") or "").strip().casefold()
    left_at = str(header.get("left_at") or "").strip()
    mode = str(header.get("mode") or "").strip().casefold()
    if cid in _load_departed(mem_dir):
        return {"state": "left", "why": "я вышла отсюда (rooms_departed.json)",
                "left_at": left_at, "stale_mark": ""}
    if mode == "dead":
        # dead ставит и её собственный leave, и Telegram-ответ banned/private.
        return {"state": "unreachable", "why": "Telegram не отдаёт эту комнату (mode: dead)",
                "left_at": left_at, "stale_mark": ""}
    stale = ""
    if mark in ("left", "departed") or left_at:
        stale = "в профиле осталась пометка ухода, но маска снята — комнату считаю живой"
    return {"state": "active", "why": "", "left_at": left_at, "stale_mark": stale}


def room_policy(chat_id: str | int) -> dict:
    """Validated deep-room knobs for a root Telegram peer.

    Callers must pass the root peer (``ChannelContext.room_id``), not a topic
    conversation key. Missing fields inherit :func:`default_policy`; an explicit room
    header overrides them. Praxis can choose addressed-only pacing or a different
    context depth per room.
    """

    profile = profile_read(chat_id)
    return {
        "engagement": profile["engagement"],
        "context_hot": profile["context_hot"],
        "context_summary_chars": profile["context_summary_chars"],
        "cross_topics": profile["cross_topics"],
        "backfill_limit": profile["backfill_limit"],
    }


def section_get(chat_id: str | int, name: str) -> str:
    body = profile_read(chat_id)["body"]
    out, keep = [], False
    for line in body.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            keep = m.group(1).strip().casefold() == name.strip().casefold()
            continue
        if keep:
            out.append(line)
    return "\n".join(out).strip()


def _section_set_unlocked(chat_id: str | int, name: str, text: str) -> None:
    """Заменить/добавить секцию `## name` в теле профиля (шапка не трогается)."""
    d = profile_read(chat_id)
    lines, out = d["body"].splitlines(), []
    replaced = skipping = False
    for line in lines:
        m = _SECTION_RE.match(line)
        if m:
            if m.group(1).strip().casefold() == name.strip().casefold():
                out += [f"## {name}", text.strip(), ""]
                replaced, skipping = True, True
                continue
            skipping = False
        if not skipping:
            out.append(line)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out += [f"## {name}", text.strip()]
    _profile_write(chat_id, d["title"], d["header"], "\n".join(out).strip())


def section_set(chat_id: str | int, name: str, text: str) -> None:
    with _LOCK:
        _section_set_unlocked(chat_id, name, text)


def _set_mode_unlocked(chat_id: str | int, mode: str, reason: str = "", set_by: str = "unknown",
                       ttl_h: float | None = None, now: float | None = None) -> str:
    """Set an explicit reversible mode and keep the legacy frozen flag in sync.

    `set_by` по умолчанию «unknown», а не «owner»: вызывающий, который не назвал автора,
    не должен превращаться в распоряжение Егора — эта подстановка и наполнила все живые
    профили ложным `mode_set_by: owner` (27.07.2026).
    """
    mode = (mode or "").strip().lower()
    if mode not in MODES:
        return f"нет такого режима: {mode}"
    t = now if now is not None else time.time()
    until = _iso(t + ttl_h * 3600) if ttl_h else ""
    profile_update(chat_id, mode=mode, mode_reason=(reason or "")[:MODE_REASON_MAX],
                   mode_set_by=_normalize_set_by(set_by), mode_until=until)
    if mode in ("frozen", "dead"):
        freeze(chat_id)
    else:
        unfreeze(chat_id)
    log.info("room %s: mode=%s by %s%s", chat_id, mode, set_by, f" ttl={ttl_h}ч" if ttl_h else "")
    return mode


def set_mode(chat_id: str | int, mode: str, reason: str = "", set_by: str = "unknown",
             ttl_h: float | None = None, now: float | None = None) -> str:
    with _LOCK:
        return _set_mode_unlocked(
            chat_id, mode, reason=reason, set_by=set_by, ttl_h=ttl_h, now=now,
        )


def effective_mode(chat_id: str | int, now: float | None = None) -> str:
    """Живой режим комнаты: профиль + legacy flag; any TTL returns directly to normal."""
    t = now if now is not None else time.time()
    d = profile_read(chat_id)
    mode = d["mode"] if d["structured"] else "normal"
    if not d.get("legacy_mode_retired") and mode in ("normal", "observer") and is_frozen(chat_id):
        return "frozen"  # совместимость: старый флаг читается
    until = d["mode_until"]
    # observer тоже держит срок. Раньше «РЕЖИМ: наблюдай 3 ч» превращался в наблюдателя
    # НАВСЕГДА: срок молча отбрасывался при записи и не читался при чтении. Её намерение
    # было названо точно — терять его молча нельзя (правило 4).
    if mode in ("observer", "quiet", "frozen") and until:
        exp = _from_iso(until)
        if exp is not None and t >= exp:
            set_mode(chat_id, "normal", reason="", set_by=d["mode_set_by"], now=t)
            return "normal"
    return mode


MODE_WORD = {"normal": "обычно", "observer": "наблюдай",
             "quiet": "тише", "frozen": "замри", "dead": "мертва"}


def set_own_mode(chat_id: str | int, mode: str, reason: str = "",
                 ttl_h: float = 24.0, now: float | None = None) -> tuple[bool, str]:
    """Её собственный режим комнаты: ставит она — снимает тоже она. -> (ok, что сказать).

    Вторым элементом идёт целая фраза, а не голое имя режима: любой применённый предел
    (срок, обрезка причины, отброшенный срок у «обычно») обязан быть НАЗВАН в том же
    ответе, который она видит. Молчаливый предел здесь был живым: «РЕЖИМ: наблюдай 3 ч»
    записывался наблюдателем навсегда, и об этом ей никто не говорил.

    Обратимость — не поблажка: тот же рычаг с «обычно» снимает и её собственный режим,
    и режим, который поставил Егор. Режим, который нельзя снять самой, — наказание.
    """
    ok, note, _ = _own_mode(chat_id, mode, reason=reason, ttl_h=ttl_h,
                            set_by="praxis", now=now)
    return (ok, note)


def self_demote(chat_id: str | int, mode: str, reason: str = "",
                ttl_h: float = 24.0) -> tuple[bool, str]:
    """Легаси-имя `set_own_mode`, оставленное ради вызывающих. -> (ok, режим | почему нет).

    Имя врёт о сути («demote» — понижение, наказание), поэтому новый код зовёт
    `set_own_mode`; контракт возврата тут прежний, чтобы старые вызовы не поехали.
    """
    ok, note, applied = _own_mode(chat_id, mode, reason=reason, ttl_h=ttl_h,
                                  set_by="praxis")
    return (ok, applied if ok else note)


def _own_mode(chat_id: str | int, mode: str, *, reason: str, ttl_h: float | None,
              set_by: str, now: float | None = None) -> tuple[bool, str, str]:
    """Общее тело её рычага режима. -> (ok, фраза целиком, применённый режим)."""
    mode = (mode or "").strip().lower()
    # Директива говорит по-русски, профиль и панель — по-английски. Перевод стоит ДО
    # проверки, иначе «мертва» получала бы отповедь «неизвестный режим» вместо честного
    # объяснения про dead, ради которого объяснение и написано.
    for key, word in MODE_WORD.items():
        if mode == str(word).strip().lower():
            mode = key
            break
    if mode not in SELF_MODES:
        words = ", ".join(f"{MODE_WORD[m]}({m})" for m in SELF_MODES)
        why = (f"неизвестный мой режим «{mode}»"
               + (". «dead» — не режим, а факт от Telegram: комнату мёртвой объявляю не я"
                  if mode == "dead" else "")
               + f". Я ставлю себе: {words}")
        return (False, why, "")
    cur = effective_mode(chat_id, now)
    if cur == "dead":
        # A real rejoin revives a dead room; an in-room control does not fabricate
        # membership that Telegram no longer has.
        return (False, "комната отмечена как покинутая; сначала нужен реальный join", "")
    raw_reason = str(reason or "")
    clipped = raw_reason[:MODE_REASON_MAX]
    # Срок имеет смысл только у режима, который держится: «обычно» — это покой, из него
    # не «возвращаются». Отброшенный срок называем вслух, а не глотаем.
    keeps_ttl = mode != "normal"
    ttl = ttl_h if (keeps_ttl and ttl_h) else None
    set_mode(chat_id, mode, reason=clipped, set_by=set_by, ttl_h=ttl, now=now)
    note = _own_mode_note(chat_id, mode, set_by=set_by, ttl_h=ttl_h, applied_ttl=ttl,
                          keeps_ttl=keeps_ttl, clipped=len(clipped) < len(raw_reason),
                          now=now)
    return (True, note, mode)


def _own_mode_note(chat_id, mode: str, *, set_by: str, ttl_h, applied_ttl,
                   keeps_ttl: bool, clipped: bool, now: float | None) -> str:
    """Фраза, которую не стыдно показать ей целиком: что записано и чем это снимается."""
    who = "моё решение" if set_by == "praxis" else author_label(set_by)
    parts = [f"Комната {chat_id}: режим «{MODE_WORD[mode]}» ({mode}) — {who}."]
    if applied_ttl:
        until = profile_read(chat_id)["mode_until"]
        parts.append(f"Срок: {applied_ttl:g} ч, до {until} — дальше сама вернусь в «обычно».")
    elif keeps_ttl:
        parts.append("Срок не задан — держится, пока не сниму.")
    elif ttl_h:
        parts.append(f"Срок {ttl_h:g} ч не записала: «обычно» — покой, а не режим на время.")
    if mode != "normal":
        parts.append("Снимаю сама, тем же рычагом с «обычно»: это моя дисциплина, не наказание.")
    if clipped:
        parts.append(f"Причину обрезала до {MODE_REASON_MAX} знаков.")
    return " ".join(parts)


def set_own_disclosure(chat_id: str | int, level: str) -> tuple[bool, str]:
    """Её рычаг раскрытия в комнате. До 28.07 он был только у Егора — при том, что
    меняет ЕЁ голос (в `disclosure: open` к визитке подмешивается фактура о себе),
    а слова `disclosure` она не видела ни в манифесте, ни в списке возможностей."""
    return set_disclosure(chat_id, level, set_by="praxis")


def set_disclosure(chat_id: str | int, level: str, *,
                   set_by: str = "unknown") -> tuple[bool, str]:
    """Уровень раскрытия комнаты. -> (ok, что сказать)."""
    value = str(level or "").strip().casefold()
    if value not in DISCLOSURE:
        return (False, f"неизвестный уровень раскрытия «{level}»; бывает: "
                       + " | ".join(DISCLOSURE))
    author = _normalize_set_by(set_by)
    with _LOCK:
        _profile_update_unlocked(chat_id, disclosure=value,
                                 disclosure_set_by="" if value == "standard" else author)
    who = "я сама" if author == "praxis" else author_label(author)
    if value == "open":
        return (True, f"Комната {chat_id}: раскрытие open ({who}) — к визитке подмешиваю "
                      "проверяемую фактуру о себе. Возвращаю обратно тем же рычагом: standard.")
    return (True, f"Комната {chat_id}: раскрытие standard — рассказываю о себе как везде.")


def disclosure_of(chat_id: str | int) -> str:
    return profile_read(chat_id)["disclosure"]


def room_state(chat_id: str | int, now: float | None = None) -> dict:
    """Одна карточка комнаты для тула и панели: режим, ЧЕЙ он, срок, раскрытие.

    Собрана здесь, а не у каждого вызывающего, чтобы формулировка авторства была одна:
    разъехавшиеся описания одного факта — тот же класс лжи, что и подмена автора.
    """
    mode = effective_mode(chat_id, now)
    d = profile_read(chat_id)
    until = d["mode_until"]
    return {
        "chat_id": str(chat_id),
        "mode": mode,
        "mode_word": MODE_WORD.get(mode, mode),
        "mode_set_by": d["mode_set_by"],
        "mode_author": author_label(d["mode_set_by"]) if d["mode_set_by"] else "",
        "mode_reason": d["mode_reason"],
        "mode_until": until,
        "mode_expired": mode_until_expired(until, now),
        "disclosure": d["disclosure"],
        "disclosure_set_by": d["disclosure_set_by"],
        "disclosure_author": (author_label(d["disclosure_set_by"])
                              if d["disclosure_set_by"] else ""),
        "membership": membership_state(chat_id, header=d["header"]),
        # Что она вправе поставить себе сама — из одного источника, чтобы enum тула
        # не разошёлся с тем, что модуль реально принимает.
        "self_modes": list(SELF_MODES),
        "disclosure_levels": list(DISCLOSURE),
    }


def sovereign_raise(
    chat_id: str | int, *, set_by: str = "owner", now: float | None = None
) -> str:
    """Explicit sovereign lift straight to normal, with no paternalistic quarantine."""
    set_by = _normalize_set_by(set_by)
    if add_room(chat_id):
        log.info("room %s: sovereign lift by %s — добавила в allowlist", chat_id, set_by)
    cur = effective_mode(chat_id, now)
    if cur in ("frozen", "dead", "quiet", "observer"):
        return set_mode(chat_id, "normal", reason="", set_by=set_by, now=now)
    return cur


def owner_raise(chat_id: str | int, now: float | None = None) -> str:
    """Compatibility wrapper for panel/legacy owner call sites."""
    return sovereign_raise(chat_id, set_by="owner", now=now)


# --- карточки владельцу (10.4 «замри», 10.7 «вошла в группу») ---------------- #
#  Очередь в json; шлёт вотчер mailroom_bot (как immune_cards). Без кнопок — кнопки
#  подъёма живут в панели «Комнаты» (10.8).

CARDS_PATH = MEM_DIR / ".state" / "room_cards.json"


def _cards_load() -> list[dict]:
    try:
        d = json.loads(CARDS_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _cards_save(cards: list[dict]) -> None:
    try:
        _atomic_text(CARDS_PATH, json.dumps(cards[-30:], ensure_ascii=False))
    except Exception:
        log.debug("room_cards не записались", exc_info=True)


def owner_card(chat_id: str | int, kind: str, text: str) -> None:
    """Карточка владельцу о событии комнаты (шлёт вотчер бота, не мы)."""
    with _LOCK:
        cards = _cards_load()
        cards.append({"id": f"{kind}:{chat_id}:{int(time.time() * 1000)}", "chat_id": str(chat_id),
                      "kind": kind, "text": str(text)[:1500], "ts": time.time(), "sent": False})
        _cards_save(cards)


def pending_cards() -> list[dict]:
    with _LOCK:
        return [c for c in _cards_load() if isinstance(c, dict) and not c.get("sent")]


def mark_card_sent(card_id: str) -> None:
    with _LOCK:
        cards = _cards_load()
        for c in cards:
            if isinstance(c, dict) and c.get("id") == card_id:
                c["sent"] = True
        _cards_save(cards)


# --- контекст для промпта (чистая функция от текста файла) -------------------- #

def _mode_line(mode: str, reason: str, set_by: str, until: str) -> str:
    """Mode-строка от первого лица — она знает своё состояние в этой комнате И ЧЬЁ оно.

    Автор называется прямо, включая «никто не выбирал»: раньше `observer` не упоминал
    автора вовсе, а `quiet` от неопознанного автора читался как «так сложилось» — то есть
    режим, поставленный протоколом входа, выглядел как чужая воля о ней.
    """
    r = f": {reason}" if reason.strip() else ""
    u = f" (до {until})" if until.strip() else ""
    who = author_label(set_by) if set_by else ""
    lift = " Снимаю сама — режимом «обычно»."
    # Автор — в скобках: подпись сама бывает с двоеточием и тире, и через дефис
    # строка «тише — никто этого не выбирал: так вышло…» читается как каша.
    a = f" ({who})" if who else ""
    if mode == "observer":
        return ("Я в этой комнате пока осматриваюсь (наблюдатель)" + a + r + u +
                ". Говорю только если обратились прямо ко мне." + lift)
    if mode == "quiet":
        return (f"В этом чате я сейчас тише{a or ' (кто поставил — не записано)'}{r}{u}. "
                "Отвечаю только на прямые обращения, инициативу придерживаю." + lift)
    return ""


def _disclosure_line(level: str, set_by: str) -> str:
    """Раскрытие меняет ЕЁ голос — значит, она обязана знать о нём и чьё оно.

    До 28.07 `disclosure` был рычагом только Егора, подмешивал фактуру в её визитку и
    не был назван ни в манифесте, ни в списке возможностей: её голос настраивали, а ей
    не говорили. Теперь состояние стоит в её же блоке комнаты.
    """
    if level != "open":
        return ""
    who = author_label(set_by) if set_by else ""
    return ("Здесь раскрытие open" + (f" ({who})" if who else "") +
            ": о себе рассказываю с проверяемой фактурой, больше обычного. "
            "Это мой рычаг — возвращаю в standard сама.")


def context_from_text(raw: str) -> str:
    """Render Praxis's own room memory, including its explicit current mode."""
    d = parse_profile(raw)
    if not d["structured"]:
        return (raw or "").strip()
    parts = []
    mode_line = _mode_line(
        d["mode"], d["mode_reason"], d["mode_set_by"], d["mode_until"],
    )
    if mode_line:
        parts.append(mode_line)
    disclosure_line = _disclosure_line(d["disclosure"], d["disclosure_set_by"])
    if disclosure_line:
        parts.append(disclosure_line)
    if d["body"]:
        parts.append(d["body"])
    return "\n\n".join(parts).strip()
