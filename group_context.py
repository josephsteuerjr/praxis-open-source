"""Durable, topic-safe context for admitted Telegram groups.

The append-only JSONL archive is canonical.  The topic/participant map under
``memory/.state`` and its grep-friendly Markdown twin are disposable projections:
deleting them never deletes a message and :func:`rebuild_projection` recreates both.

This module deliberately knows nothing about Telethon or the model.  The live runner
feeds it already-authorised messages and may use the bounded read APIs to assemble a
turn.  Every read is rooted in one explicit Telegram peer, so a topic selector can
never drift into a neighbouring room.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import threading
from collections import deque
from pathlib import Path
from typing import Iterable


SCHEMA_MESSAGE = "praxis.group.message.v1"
SCHEMA_TOPIC = "praxis.group.topic.v1"
PROJECTION_SCHEMA = "praxis.group.map.v1"

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
MEM_DIR = BASE / "memory"
GROUPS_DIR = MEM_DIR / "groups"
STATE_DIR = MEM_DIR / ".state" / "group_context"

MAX_HOT = 500
# Сколько последних сообщений показывать почти целиком и каким потолком тела.
# Собеседники в её комнатах пишут структурными простынями (вердикты, протоколы),
# и общий потолок 1200 резал ровно ту реплику, на которую она отвечает.
FULL_TEXT_TAIL = 4
FULL_TEXT_CHARS = 4000
MAX_SEARCH_RESULTS = 50
MAX_READ_RECORDS = 50_000
# ⚠ Санитарный потолок, а НЕ настройка поведения. Здесь стояло 40_000, и оно молча
# переписывало политику комнаты: Егор выставил `context_summary_chars=24000`, раннер
# просит вдвое (48000), а код отдавал 40000 — то есть 120 строк из 200, которые ей уже
# разрешены, и 80 сообщений её собственного места за бортом. Побочно это делало мёртвой
# верхнюю половину её же ручки: 20000, 24000 и 40000 давали байт-в-байт одно и то же.
# Теперь решает политика комнаты, а это число ловит только явную бессмыслицу.
MAX_CONTEXT_CHARS = 200_000
MAX_MAP_TOPICS = 500
MAX_MAP_PARTICIPANTS = 200
MAX_TOPIC_PARTICIPANTS = 200
MAX_PARTICIPANT_TOPICS = 50

_LOCK = threading.RLock()
_WORD_RE = re.compile(r"[\wа-яё]+", re.I)
_KEY_CACHE: dict[str, tuple[int, int, set[tuple]]] = {}


def _peer(value: str | int) -> str:
    peer = str(value).strip()
    if not peer:
        raise ValueError("peer_id is empty")
    return peer


def _positive(value, *, optional: bool = True) -> int | None:
    if value in (None, "") and optional:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("identifier must be a positive integer") from exc
    if number <= 0:
        raise ValueError("identifier must be a positive integer")
    return number


def _signed_identifier(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("sender identifier must be an integer") from exc
    return number or None


def _slug(peer_id: str | int) -> str:
    peer = _peer(peer_id)
    readable = re.sub(r"[^A-Za-z0-9_.-]+", "_", peer).strip("._")[:48] or "peer"
    digest = hashlib.sha256(peer.encode("utf-8")).hexdigest()[:10]
    return f"{readable}-{digest}"


def _rel(path: Path) -> str:
    try:
        return path.relative_to(BASE).as_posix()
    except ValueError:
        return path.as_posix()


def archive_path(peer_id: str | int) -> Path:
    return GROUPS_DIR / _slug(peer_id) / "archive.jsonl"


def projection_path(peer_id: str | int) -> Path:
    return STATE_DIR / f"{_slug(peer_id)}.json"


def projection_markdown_path(peer_id: str | int) -> Path:
    return GROUPS_DIR / _slug(peer_id) / "MAP.md"


def backfill_state_path(peer_id: str | int) -> Path:
    return STATE_DIR / f"{_slug(peer_id)}.backfill.json"


def _iso(value=None) -> str:
    if isinstance(value, _dt.datetime):
        stamp = value
    elif isinstance(value, str) and value.strip():
        try:
            stamp = _dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            stamp = _dt.datetime.now(_dt.timezone.utc)
    elif value is None:
        stamp = _dt.datetime.now(_dt.timezone.utc)
    else:
        stamp = _dt.datetime.fromtimestamp(float(value), _dt.timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=_dt.timezone.utc)
    return stamp.astimezone(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _line_key(row: dict) -> tuple:
    kind = str(row.get("kind") or "message")
    if kind == "topic":
        return (kind, str(row.get("peer_id")), int(row.get("topic_id") or 0),
                str(row.get("title") or ""))
    # Telegram keeps the message id when text is edited.  The immutable archive
    # treats each edit timestamp as a revision event while ordinary replay of the
    # original message still collapses to the empty revision.
    edited_at = str(row.get("edited_at") or "")
    # Telegram edit dates have second precision.  Two rapid edits can therefore
    # share the same timestamp; bind an edit revision to its visible payload so a
    # replay collapses but a genuinely different same-second edit is not lost.
    payload = ""
    if edited_at:
        payload = hashlib.sha256(json.dumps(
            [str(row.get("text") or ""), str(row.get("media") or "")],
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()[:20]
    return (kind, str(row.get("peer_id")), int(row.get("topic_id") or 0),
            int(row.get("message_id") or 0), edited_at, payload)


def _iter_lines(path: Path, max_records: int | None = None) -> Iterable[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            if max_records is None:
                yield from stream
            else:
                yield from deque(stream, maxlen=max(1, int(max_records)))
    except OSError:
        return


def _valid_record(row: dict, peer: str) -> bool:
    if str(row.get("peer_id")) != peer:
        return False
    kind = row.get("kind")
    if kind == "topic":
        return bool(
            row.get("schema") == SCHEMA_TOPIC
            and type(row.get("topic_id")) is int and row["topic_id"] > 0
            and isinstance(row.get("title"), str)
            and isinstance(row.get("timestamp"), str)
            and (row.get("message_id") is None
                 or type(row.get("message_id")) is int and row["message_id"] > 0)
        )
    if kind != "message" or row.get("schema") != SCHEMA_MESSAGE:
        return False
    topic = row.get("topic_id")
    sender = row.get("sender_id")
    reply_to = row.get("reply_to_message_id")
    return bool(
        (topic is None or type(topic) is int and topic > 0)
        and type(row.get("message_id")) is int and row["message_id"] > 0
        and (sender is None or type(sender) is int and sender != 0)
        and (reply_to is None or type(reply_to) is int and reply_to > 0)
        and isinstance(row.get("sender_name"), str)
        and isinstance(row.get("timestamp"), str)
        and (row.get("edited_at") is None or isinstance(row.get("edited_at"), str))
        and isinstance(row.get("text"), str)
        and isinstance(row.get("media"), str)
        and type(row.get("outgoing")) is bool
    )


def iter_records(peer_id: str | int, *, max_records: int | None = MAX_READ_RECORDS) -> Iterable[dict]:
    """Yield valid canonical rows, ignoring (but never overwriting) a torn tail."""

    peer = _peer(peer_id)
    for raw in _iter_lines(archive_path(peer), max_records=max_records):
        try:
            row = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(row, dict) and _valid_record(row, peer):
            yield row


def _known_keys(peer_id: str | int) -> set[tuple]:
    peer = _peer(peer_id)
    path = archive_path(peer)
    try:
        stat = path.stat()
        cached = _KEY_CACHE.get(peer)
        if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return set(cached[2])
    except OSError:
        stat = None
    keys = {_line_key(row) for row in iter_records(peer, max_records=None)}
    if stat is not None:
        _KEY_CACHE[peer] = (stat.st_mtime_ns, stat.st_size, set(keys))
    return keys


def _append(peer_id: str | int, row: dict) -> bool:
    peer = _peer(peer_id)
    path = archive_path(peer)
    key = _line_key(row)
    encoded = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with _LOCK:
        known = _known_keys(peer)
        if key in known:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        needs_separator = False
        try:
            if path.stat().st_size:
                with path.open("rb") as source:
                    source.seek(-1, os.SEEK_END)
                    needs_separator = source.read(1) != b"\n"
        except OSError:
            pass
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            if needs_separator:
                stream.write("\n")
            stream.write(encoded + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        stat = path.stat()
        known.add(key)
        _KEY_CACHE[peer] = (stat.st_mtime_ns, stat.st_size, known)
    return True


def observe_message(*, peer_id: str | int, topic_id: int | None,
                    message_id: int, sender_id: int | None,
                    sender_name: str, reply_to_message_id: int | None,
                    timestamp, text: str, topic_title: str = "",
                    media: str = "", outgoing: bool = False,
                    edited_at=None) -> bool:
    """Append one exact admitted Telegram message, idempotently."""

    peer = _peer(peer_id)
    topic = _positive(topic_id) if topic_id is not None else None
    message = _positive(message_id, optional=False)
    sender = _signed_identifier(sender_id)
    reply_to = _positive(reply_to_message_id) if reply_to_message_id is not None else None
    row = {
        "schema": SCHEMA_MESSAGE,
        "kind": "message",
        "peer_id": peer,
        "topic_id": topic,
        "topic_title": str(topic_title or "").strip()[:500],
        "message_id": message,
        "sender_id": sender,
        "sender_name": str(sender_name or "").strip()[:500],
        "reply_to_message_id": reply_to,
        "timestamp": _iso(timestamp),
        "edited_at": (_iso(edited_at) if edited_at is not None else None),
        "text": str(text or ""),
        "media": str(media or "")[:1000],
        "outgoing": bool(outgoing),
    }
    return _append(peer, row)


def record_topic(peer_id: str | int, topic_id: int, title: str, *,
                 timestamp=None, message_id: int | None = None) -> bool:
    """Persist topic metadata discovered from an opener or Telegram's forum list."""

    peer = _peer(peer_id)
    topic = _positive(topic_id, optional=False)
    message = _positive(message_id) if message_id is not None else None
    row = {
        "schema": SCHEMA_TOPIC,
        "kind": "topic",
        "peer_id": peer,
        "topic_id": topic,
        "title": str(title or "").strip()[:500] or f"topic #{topic}",
        "message_id": message,
        "timestamp": _iso(timestamp),
    }
    return _append(peer, row)


def _empty_projection(peer_id: str) -> dict:
    return {
        "schema": PROJECTION_SCHEMA,
        "peer_id": peer_id,
        "archive": _rel(archive_path(peer_id)),
        "message_count": 0,
        "revision_count": 0,
        "topics": {},
        "participants": {},
    }


def _projection_from_rows(peer_id: str | int) -> dict:
    peer = _peer(peer_id)
    result = _empty_projection(peer)
    logical_messages: set[tuple[str, int]] = set()
    for row in iter_records(peer, max_records=MAX_READ_RECORDS):
        topic_raw = row.get("topic_id")
        topic = str(int(topic_raw)) if topic_raw not in (None, "") else "root"
        stamp = str(row.get("edited_at") or row.get("timestamp") or "")
        topics = result["topics"]
        state = topics.setdefault(topic, {
            "topic_id": None if topic == "root" else int(topic),
            "title": "" if topic == "root" else f"topic #{topic}",
            "message_count": 0,
            "revision_count": 0,
            "participants": [],
            "last_timestamp": "",
            "last_message_id": None,
        })
        title = str(row.get("title") or row.get("topic_title") or "").strip()
        if title:
            state["title"] = title[:500]
        if stamp >= str(state.get("last_timestamp") or ""):
            state["last_timestamp"] = stamp
            if row.get("message_id") is not None:
                state["last_message_id"] = int(row["message_id"])
        if row.get("kind") != "message":
            continue
        message_key = (topic, int(row.get("message_id") or 0))
        first_observation = message_key not in logical_messages
        logical_messages.add(message_key)
        if first_observation:
            result["message_count"] += 1
            state["message_count"] += 1
        if row.get("edited_at"):
            result["revision_count"] += 1
            state["revision_count"] += 1
        sender_raw = row.get("sender_id")
        sender_key = str(sender_raw) if sender_raw is not None else (
            "name:" + str(row.get("sender_name") or "unknown").casefold())
        participant = result["participants"].setdefault(sender_key, {
            "sender_id": int(sender_raw) if sender_raw is not None else None,
            "name": str(row.get("sender_name") or "unknown")[:500],
            "message_count": 0,
            "revision_count": 0,
            "topics": [],
            "last_timestamp": "",
        })
        if first_observation:
            participant["message_count"] += 1
        if row.get("edited_at"):
            participant["revision_count"] += 1
        if stamp >= str(participant.get("last_timestamp") or ""):
            participant["last_timestamp"] = stamp
            participant["name"] = str(row.get("sender_name") or participant["name"])[:500]
        if first_observation and topic not in participant["topics"]:
            participant["topics"].append(topic)
        if first_observation and sender_key not in state["participants"]:
            state["participants"].append(sender_key)
    for state in result["topics"].values():
        participants = sorted(set(state.get("participants") or []), key=str.casefold)
        state["participant_count"] = len(participants)
        state["participants"] = participants[:MAX_TOPIC_PARTICIPANTS]
    for participant in result["participants"].values():
        participant_topics = list(participant.get("topics") or [])
        participant["topic_count"] = len(participant_topics)
        participant["topics"] = participant_topics[-MAX_PARTICIPANT_TOPICS:]
    topic_items = sorted(
        result["topics"].items(),
        key=lambda pair: (
            str(pair[1].get("last_timestamp") or ""),
            int(pair[1].get("message_count") or 0),
            pair[0],
        ),
        reverse=True,
    )
    participant_items = sorted(
        result["participants"].items(),
        key=lambda pair: (
            int(pair[1].get("message_count") or 0),
            str(pair[1].get("last_timestamp") or ""),
            pair[0],
        ),
        reverse=True,
    )
    result["topic_count"] = len(topic_items)
    result["participant_count"] = len(participant_items)
    result["topics"] = dict(topic_items[:MAX_MAP_TOPICS])
    result["participants"] = dict(participant_items[:MAX_MAP_PARTICIPANTS])
    try:
        stat = archive_path(peer).stat()
        result["archive_mtime_ns"] = stat.st_mtime_ns
        result["archive_size"] = stat.st_size
    except OSError:
        result["archive_mtime_ns"] = 0
        result["archive_size"] = 0
    return result


def _projection_markdown(projection: dict) -> str:
    rows = [
        "<!-- praxis-generated: group-context-map-v1 -->",
        f"# Group context map: {projection['peer_id']}",
        "",
        f"Messages: {projection.get('message_count', 0)}",
        f"Edit revisions: {projection.get('revision_count', 0)}",
        f"Topics in map: {len(projection.get('topics') or {})} of "
        f"{projection.get('topic_count', len(projection.get('topics') or {}))}",
        f"Participants in map: {len(projection.get('participants') or {})} of "
        f"{projection.get('participant_count', len(projection.get('participants') or {}))}",
        "",
        "## Topics",
    ]
    topics = sorted(
        projection.get("topics", {}).values(),
        key=lambda item: (
            str(item.get("last_timestamp") or ""),
            int(item.get("message_count") or 0),
            str(item.get("topic_id") or "root"),
        ),
        reverse=True,
    )[:MAX_MAP_TOPICS]
    for item in topics:
        ident = item.get("topic_id")
        selector = "root" if ident is None else f"topic:{ident}"
        rows.append(
            f"- {selector} — {item.get('title') or selector}; "
            f"messages={item.get('message_count', 0)}; "
            f"edits={item.get('revision_count', 0)}; "
            f"participants={item.get('participant_count', len(item.get('participants') or []))}; "
            f"last={item.get('last_timestamp') or '-'}"
        )
    rows.extend(("", "## Participants"))
    participants = sorted(
        projection.get("participants", {}).values(),
        key=lambda item: (
            int(item.get("message_count") or 0),
            str(item.get("last_timestamp") or ""),
            str(item.get("sender_id") or item.get("name") or ""),
        ),
        reverse=True,
    )
    for item in participants[:MAX_MAP_PARTICIPANTS]:
        rows.append(
            f"- {item.get('name') or 'unknown'}"
            + (f" (id {item.get('sender_id')})" if item.get("sender_id") else "")
            + f"; messages={item.get('message_count', 0)}; "
            + f"edits={item.get('revision_count', 0)}; "
            + "topics=" + ",".join(str(x) for x in item.get("topics") or [])
        )
    return "\n".join(rows).rstrip() + "\n"


def _rebuild_projection_unlocked(peer_id: str | int) -> dict:
    """Recreate the JSON and Markdown maps solely from canonical JSONL."""

    peer = _peer(peer_id)
    projection = _projection_from_rows(peer)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    GROUPS_DIR.mkdir(parents=True, exist_ok=True)
    target = projection_path(peer)
    temp = target.with_suffix(".json.tmp")
    temp.write_text(json.dumps(projection, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8")
    temp.replace(target)
    md_target = projection_markdown_path(peer)
    md_target.parent.mkdir(parents=True, exist_ok=True)
    md_temp = md_target.with_suffix(".md.tmp")
    md_temp.write_text(_projection_markdown(projection), encoding="utf-8")
    md_temp.replace(md_target)
    return projection


def rebuild_projection(peer_id: str | int) -> dict:
    with _LOCK:
        return _rebuild_projection_unlocked(peer_id)


def projection(peer_id: str | int) -> dict:
    with _LOCK:
        peer = _peer(peer_id)
        target = projection_path(peer)
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            # «Архива нет» — законное состояние комнаты (ни одного сохранённого сообщения),
            # а не сбой чтения. Раньше `stat()` падал OSError и уносил ВСЮ проверку в
            # `except: pass`: проекция считалась несвежей ВСЕГДА, и каждый холостой заход
            # безусловно переписывал `-*.json` и `MAP.md`. Живьём 26.07: у мёртвой фикстуры
            # `-100500` (комната из прогона стенда 06.07 по живой памяти, архива у неё нет
            # вовсе) mtime обоих файлов совпадал с текущей минутой — 4865 холостых тиков
            # backfill с 20.07, и любой обзор читал её как самую свежую живую комнату.
            # Читаем отсутствие архива ровно так же, как его записывает
            # `_projection_from_rows`: (0, 0). Появится архив — stat даст не ноль, и
            # пересборка случится по делу.
            try:
                stat_result = archive_path(peer).stat()
                archive_mtime_ns, archive_size = stat_result.st_mtime_ns, stat_result.st_size
            except OSError:
                archive_mtime_ns, archive_size = 0, 0
            if (isinstance(data, dict) and data.get("schema") == PROJECTION_SCHEMA
                    and isinstance(data.get("revision_count"), int)
                    and isinstance(data.get("topics"), dict)
                    and all(isinstance(item, dict) and "revision_count" in item
                            for item in data["topics"].values())
                    and int(data.get("archive_mtime_ns") or 0) == archive_mtime_ns
                    and int(data.get("archive_size") or 0) == archive_size):
                return data
        except (OSError, TypeError, ValueError):
            pass
        return _rebuild_projection_unlocked(peer)


def topics(peer_id: str | int, *, limit: int = 50) -> list[dict]:
    cap = max(1, min(MAX_SEARCH_RESULTS, int(limit)))
    rows = list(projection(peer_id).get("topics", {}).values())
    rows.sort(key=lambda item: (
        str(item.get("last_timestamp") or ""),
        int(item.get("message_count") or 0),
        str(item.get("topic_id") or "root"),
    ), reverse=True)
    return rows[:cap]


def branch_containers(peer_id: str | int) -> dict:
    """Для каждой нашей ветки — место, которому она на самом деле принадлежит.

    Правило одно, и оно не требует ни сети, ни служебных сообщений:

        ветка N — настоящее место Telegram тогда и только тогда, когда её корневое
        сообщение N само лежит в ветке N.

    Тему форума открывает служебное сообщение, и оно само лежит в этой теме — корень
    совпадает с веткой. А наша псевдоветка рождается иначе: Telegram не ставит
    `reply_to_top_id` на первый ответ, мы берём номер сообщения-корня за «тему», и тогда
    корень лежит НЕ в ней, а там, где он и был сказан.

    Проверено на живых архивах 25.07 против независимого свидетельства (служебные
    «создана тема»): расхождений ноль на обеих комнатах. Грибница — 16 настоящих тем,
    37 артефактов General, 8 без корня в архиве; AbstractDL — 0 настоящих, 266
    артефактов, 13 без корня.

    Возвращает ТОЛЬКО артефакты: {ветка: место (id темы или None для General)}.
    Ветка без корня в архиве не попадает сюда вовсе — про неё мы честно не знаем.
    """
    branches: set[int] = set()
    own: dict[int, int | None] = {}
    for row in iter_records(peer_id):
        if row.get("kind") != "message":
            continue
        topic = row.get("topic_id")
        if topic is not None:
            branches.add(int(topic))
        mid = int(row.get("message_id") or 0)
        if mid:
            own[mid] = None if topic is None else int(topic)
    out: dict[int, int | None] = {}
    for branch in sorted(branches):
        if branch not in own:
            continue                      # корня нет — не знаем, и не выдумываем
        container = own[branch]
        if container != branch:
            out[branch] = container
    return out


def topic_title(peer_id: str | int, topic_id: int | None) -> str:
    key = "root" if topic_id is None else str(_positive(topic_id, optional=False))
    return str(projection(peer_id).get("topics", {}).get(key, {}).get("title") or "")


def _tokens(value: str) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(token.casefold().replace("ё", "е")
                                 for token in _WORD_RE.findall(str(value or ""))
                                 if len(token) > 1))
    return values[:32]


def search(peer_id: str | int, query: str, *, topic_id: int | None = None,
           limit: int = 12) -> list[dict]:
    """Bounded lexical search inside one root peer, optionally one exact topic."""

    terms = _tokens(query)
    if not terms:
        return []
    wanted_topic = _positive(topic_id) if topic_id is not None else None
    cap = max(1, min(MAX_SEARCH_RESULTS, int(limit)))
    ranked: list[tuple[float, str, dict]] = []
    latest: dict[tuple[int | None, int], dict] = {}
    phrase = str(query or "").strip().casefold().replace("ё", "е")
    for row in iter_records(peer_id):
        if row.get("kind") != "message":
            continue
        current_topic = row.get("topic_id")
        if topic_id is not None and current_topic != wanted_topic:
            continue
        latest[(current_topic, int(row.get("message_id") or 0))] = row
    # Search is an orientation surface, not the audit log: rank the latest known
    # revision so text explicitly corrected away cannot return as a current fact.
    for row in latest.values():
        hay = (str(row.get("text") or "") + " " + str(row.get("media") or "")
               + " " + str(row.get("sender_name") or "")).casefold().replace("ё", "е")
        hits = sum(hay.count(term) for term in terms)
        if not hits:
            continue
        score = float(hits) + (3.0 if phrase and phrase in hay else 0.0)
        ranked.append((score, str(row.get("timestamp") or ""), row))
    ranked.sort(
        key=lambda item: (
            item[0], item[1], int(item[2].get("message_id") or 0),
            int(item[2].get("topic_id") or 0),
        ),
        reverse=True,
    )
    return [dict(item[2]) for item in ranked[:cap]]


def _format_message(row: dict, *, max_text: int = 1200,
                    thread_word: str = "topic", as_self: bool = False) -> str:
    """Одна строка архива.

    `thread_word` — самое частое утверждение во всём её групповом контексте: оно стоит
    в начале КАЖДОЙ строки. Пока комната читалась как форум, каждая строка заявляла
    «topic #93707» — и никакая одна строка ориентации не перевешивает четыре сотни
    таких. Меняется слово, идентификатор остаётся прежним: он её адрес, а не мнение.

    `as_self` — та же строка, но без поля отправителя: она применяется только к ЕЁ
    собственным сообщениям, когда они едут в модель ролью `assistant`. Подпись «Praxis
    [id …]» перед своей же репликой превращает речь в сообщение о речи; кто говорит,
    уже сказано ролью. Всё остальное — ветка, id, время, метка правки, адрес ответа —
    остаётся на месте: это её координаты в комнате, а не украшение.
    """
    topic = row.get("topic_id")
    title = str(row.get("topic_title") or "").strip()
    topic_mark = ("root" if topic is None
                  else f"{thread_word} #{topic}" + (f" «{title}»" if title else ""))
    sender = str(row.get("sender_name") or "unknown")
    if row.get("sender_id") is not None:
        sender += f" [id {row['sender_id']}]"
    reply = (f" reply_to=#{row['reply_to_message_id']}"
             if row.get("reply_to_message_id") is not None else "")
    edited = f" edited={row['edited_at']}" if row.get("edited_at") else ""
    text = str(row.get("text") or "")
    media = str(row.get("media") or "")
    body = " ".join(part for part in (media, text) if part).strip() or "[service event]"
    body = body.replace("\x00", "")
    cap = max(80, int(max_text))
    if len(body) > cap:
        # Обрез посреди фразы БЕЗ пометки — молчаливая ложь о полноте: срез выглядит
        # как всё сообщение целиком. Живой случай 24.07: Арет прислал два вердикта
        # одним сообщением на 2709 символов, она увидела 1200 и честно ответила
        # «приняла первый вердикт» — второй лежал в отрезанной половине, и ничто
        # на неё не указывало. Полный текст всегда есть в архиве.
        body = (body[:cap]
                + f"…[ОБРЕЗАНО: показано {cap} из {len(body)} символов; целиком — "
                  f"group_context(action=\"message\", limit={row.get('message_id')})]")
    if as_self:
        head = (f"[{topic_mark}; message #{row.get('message_id')}; "
                f"{row.get('timestamp')}{edited}")
        head += ("; " + reply.strip()) if reply else ""
    else:
        head = (f"[{topic_mark}; message #{row.get('message_id')}; "
                f"{row.get('timestamp')}{edited}; {sender}{reply}")
    return f"{head}] {body}"


def context(peer_id: str | int, *, topic_id: int | None, limit: int = 80,
            max_chars: int = 20_000, whole_room: bool = False,
            members: frozenset | set | None = None,
            thread_word: str | None = None) -> str:
    """Лента комнаты одним текстом — ровно то, чем она была всегда.

    Отбор, потолки и порядок живут в `context_rows`; здесь только склейка. Разделение
    сделано, чтобы у ленты появился второй вид — по репликам, с сохранённым авторством,
    — не заводя ВТОРОГО отбора: два независимых прохода разошлись бы молча, и модель
    читала бы не тот разговор, за который мы потом отвечаем распиской.
    """
    return "\n".join(row["line"] for row in context_rows(
        peer_id, topic_id=topic_id, limit=limit, max_chars=max_chars,
        whole_room=whole_room, members=members, thread_word=thread_word))


def context_rows(peer_id: str | int, *, topic_id: int | None, limit: int = 80,
                 max_chars: int = 20_000, whole_room: bool = False,
                 members: frozenset | set | None = None,
                 thread_word: str | None = None) -> list[dict]:
    """Та же лента, но строками-записями: `{"self": bool, "line": str, "role_line": str}`.

    `line` — строка архива как была и есть (на ней стоят расписки и прожитый ход).
    `role_line` — она же для модели: у ЕЁ собственных сообщений без подписи её именем,
    у чужих совпадает с `line`. `self` — из поля `outgoing` записи, снятого в момент
    наблюдения; здесь ничего не угадывается по префиксу.

    Служебные строки ленты (корень ветки, пометка обреза) идут с `self=False`: они не
    чья-то реплика, и приписывать их ей нельзя.

    Chronological tail of latest revisions for one conversation branch.

    The branch is the reply CHAIN, not the ``topic_id`` column — because the column is
    exactly what Telegram is unreliable about.  Outside a real forum, Telegram omits
    ``reply_to_top_id`` on the first reply, so a chain's root message and the first
    answer to it are archived with ``topic_id=None`` while every later reply is archived
    under ``topic_id=<root>``.  Filtering on the column alone therefore dropped the two
    messages the thread is *about*, and the resulting slice looked complete: nothing
    marked it partial.  Observed 2026-07-23 in -1001240718803, where she said «у меня в
    топик попал только твой ответ, без сообщения #93708» and another agent had to paste
    the missing message back by hand.

    So a row belongs to this branch when it carries the topic, IS the root, or replies
    to the root.  In a real forum that is the same set as before: the root is the
    ``MessageActionTopicCreate`` opener and its direct replies are already in the topic,
    so the exact-topic contract — one topic is a separate place, never crossed — holds.
    """

    wanted = _positive(topic_id) if topic_id is not None else None
    cap = max(1, min(MAX_HOT, int(limit)))
    budget = max(500, min(MAX_CONTEXT_CHARS, int(max_chars)))
    # Слово в начале каждой строки. Оно не косметика: строк — сотни, и все они говорят
    # ей, из чего состоит эта комната.
    word = str(thread_word or ("thread" if whole_room else "topic"))

    def _in_branch(row: dict) -> bool:
        # Пункт 5, слой B. `whole_room` включается ТОЛЬКО когда реестр маршрутов
        # получил от Telegram прямой ответ «эта комната не форум». Тогда деление на
        # «темы» — артефакт нашего же ключа, а не структура Telegram, и сужать её до
        # одной ветки значит показывать часть комнаты как целую.
        #
        # Замер на живом архиве 25.07: 4138 сообщений AbstractDL разложены на 279
        # «тем»; с корневого ключа видно 1661 сообщение, то есть 40% комнаты, и это
        # мешанина зачинов без продолжений — 53 ключа содержат ровно одно сообщение.
        # Настоящий форум (комната Грибницы, 18 реальных openers) сюда не попадает:
        # там вердикт `true`, и разделение тем остаётся, как ему и положено.
        if whole_room:
            return True
        # `members` — ветки, из которых состоит ОДНО место (General форума вместе с
        # нашими псевдоветками, либо настоящая тема). Строго добавление: старое правило
        # цепочки продолжает работать под ним, поэтому увидеть меньше, чем раньше,
        # нельзя ни при каком составе `members`.
        if members is not None and row.get("topic_id") in members:
            return True
        return _narrow(row)

    def _narrow(row: dict) -> bool:
        """Прежнее правило цепочки — то, что она видела ДО расширения."""
        if row.get("topic_id") == wanted:
            return True
        if wanted is None:
            return False
        return (int(row.get("message_id") or 0) == wanted
                or row.get("reply_to_message_id") == wanted)

    latest: dict[int, dict] = {}
    for row in iter_records(peer_id):
        if row.get("kind") == "message" and _in_branch(row):
            latest[int(row.get("message_id") or 0)] = row

    # Порядок — по времени, КОГДА СКАЗАНО, а не когда отредактировано. Иначе правка
    # недельной давности переезжает в конец ленты и вытесняет сегодняшний разговор:
    # `latest` и так держит свежую ревизию, а `_format_message` ставит метку edited=.
    def _order(row: dict) -> tuple:
        return (str(row.get("timestamp") or ""), int(row.get("message_id") or 0))

    ordered = sorted(latest.values(), key=_order)
    # ⚠ 04.08. Резервирование ветки применяется ТОЛЬКО там, где ветку провёл Telegram.
    # Оно появилось 25.07 против настоящей беды: расширение места складывало в один срез
    # сотни соседних строк, и общий потолок начинал выбрасывать её собственную ветку,
    # чтобы уместить соседей — в Грибнице из 97 строк General оставалось 61. Лечение
    # было верным для ФОРУМА.
    #
    # Но в не-форуме «ветка» — не всегда цепочка под постом. AbstractDL — это ГРУППА
    # ОБСУЖДЕНИЯ КАНАЛА: 411 веток, корни которых — посты @abstractDL, плюс общий чат
    # группы (`topic_id=None`) на сотню тысяч сообщений, из которых в её архиве 2609.
    # Проснувшись в общем чате, она резервировала ЕГО — и остаток соседям выходил
    # нулевым, то есть живое обсуждение под постом не попадало в кадр НИКОГДА.
    # 03.08 в 21:37 Егор спросил в общий чат «о чём они говорят?» — про обсуждение под
    # постом #96051, которого она не видела ни строкой, и ответила по последнему, что
    # было в общем чате: разговору суточной давности.
    #
    # Решение Егора 04.08: в не-форуме брать хронологический хвост МЕСТА. Гарантия
    # «увидеть меньше прежнего нельзя» этим не нарушается — она была про форум, где
    # `whole_room` не включается вовсе.
    if whole_room:
        # Хронологический хвост МЕСТА — решение Егора 04.08. Но с ограниченным полом:
        # у зеркальной беды та же цена. Общий чат AbstractDL — под сотню тысяч сообщений
        # и ревёт постоянно; проснувшись под постом канала, она получила бы хвост из
        # одного общего чата и потеряла бы ту самую ветку, в которой отвечает.
        # Поэтому её ветке гарантирована ТРЕТЬ потолка (её свежие строки), остальное —
        # честная хронология комнаты. Ни один из двух провалов больше не достижим.
        floor = max(1, cap // 3)
        reserved = [row for row in ordered if _narrow(row)][-floor:]
        keys = {int(row.get("message_id") or 0) for row in reserved}
        rest = [row for row in ordered if int(row.get("message_id") or 0) not in keys]
        selected = sorted(reserved + rest[-(cap - len(reserved)):], key=_order)
    elif members is not None:
        # ⚠ Оговорка `wanted is not None` здесь была дырой ровно в том случае, ради
        # которого правка и делалась: в General форума ветка — корневая, `topic_id`
        # пуст, и резервирование выключалось целиком. `_narrow` про None знает сам.
        reserved = [row for row in ordered if _narrow(row)][-cap:]
        keys = {int(row.get("message_id") or 0) for row in reserved}
        left = cap - len(reserved)
        # ⚠ И `[-max(0, cap - room):]` — это `[-0:]`, то есть ВЕСЬ список, а не пустой,
        # когда её ветка заняла потолок целиком. Потолок переставал быть потолком ровно
        # на активной ветке (обе находки — вторая адверсарка 25.07).
        extra = ([row for row in ordered
                  if int(row.get("message_id") or 0) not in keys][-left:]
                 if left > 0 else [])
        selected = sorted(reserved + extra, key=_order)
    else:
        reserved = []
        selected = ordered[-cap:]
    reserved_ids = {int(row.get("message_id") or 0) for row in reserved}

    # Корень — самое старое сообщение цепочки, поэтому обрез «от новых к старым»
    # выбрасывает его первым. Но это не старая строка, это ТЕМА: без неё ветка из
    # 33 реплик читается как разговор без предмета (живой случай — ветка #93759,
    # корень «у Праксис сейчас проблемы с памятью», срез упирался в бюджет и терял
    # именно его). Тема закрепляется до общего обреза, остаток бюджета — хвосту.
    root_line = ""
    if wanted is not None:
        root_row = latest.get(wanted)
        if root_row is not None:
            root_line = _format_message(root_row, thread_word=word)

    # Свежие сообщения — это те, на которые она сейчас отвечает, и резать их общим
    # потолком тела хуже всего: 24.07 Арет прислал два вердикта одним сообщением на
    # 2709 символов, она увидела 1200 и ответила «приняла первый вердикт». Поэтому
    # последним FULL_TEXT_TAIL строкам даём широкий потолок, остальным — обычный.
    # Широкий потолок ограничен долей бюджета: иначе четыре длинных свежих реплики
    # съедают весь срез и молча выбивают остальную ветку — включая прямой вопрос
    # владельца. Чиня обрез тела, нельзя завести такой же обрез этажом выше.
    wide = max(1200, min(FULL_TEXT_CHARS, budget // (FULL_TEXT_TAIL + 2)))
    order_newest = list(reversed(selected))
    # Широкий рендер — это тоже ресурс, и его тоже нельзя отдать соседям вперёд неё.
    # ⚠ Ранг считался по объединённому срезу: четыре свежих соседских строки занимали
    # все широкие места, и её собственная последняя реплика рендерилась обрезком в 1200
    # символов — то есть «увидеть меньше прежнего нельзя» нарушалось не числом строк, а
    # их полнотой (вторая адверсарка 25.07). Ранг считаем внутри каждой группы.
    rank: dict[int, int] = {}
    for group in (reserved_ids, None):
        position = 0
        for row in order_newest:
            mid = int(row.get("message_id") or 0)
            if (mid in reserved_ids) != (group is not None):
                continue
            rank[mid] = position
            position += 1
    used = len(root_line) + 1 if root_line else 0
    picked: dict[int, str] = {}
    dropped_own = dropped_near = 0

    # Два прохода: сначала её собственная ветка, потом соседи по месту. Порядок проходов
    # и есть гарантия «увидеть меньше прежнего нельзя»: соседи занимают ТОЛЬКО остаток
    # бюджета. Без расширения первый проход пуст, и всё идёт ровно как раньше.
    for own_pass in (True, False):
        for row in order_newest:
            mid = int(row.get("message_id") or 0)
            if root_line and mid == wanted:
                continue
            if mid in picked or (mid in reserved_ids) != own_pass:
                continue
            width = wide if rank.get(mid, 0) < FULL_TEXT_TAIL else 1200
            line = _format_message(row, max_text=width, thread_word=word)
            if used + len(line) + 1 > budget:
                # Не молчаливый break: пробуем узкий рендер, и только если и он не влез —
                # считаем сообщение выпавшим и говорим об этом вслух ниже.
                width = 1200
                line = _format_message(row, max_text=width, thread_word=word)
                if used + len(line) + 1 > budget:
                    rest = sum(1 for other in order_newest
                               if int(other.get("message_id") or 0) not in picked
                               and (int(other.get("message_id") or 0) in reserved_ids) == own_pass
                               and not (root_line and int(other.get("message_id") or 0) == wanted))
                    if own_pass:
                        dropped_own = rest
                    else:
                        dropped_near = rest
                    break
            picked[mid] = {
                "self": row.get("outgoing") is True,
                "line": line,
                # Второй рендер снимается только с ЕЁ строк и тем же потолком, каким
                # уже отрисована первая: иначе роль показывала бы больше или меньше
                # текста, чем расписка.
                "role_line": (_format_message(row, max_text=width, thread_word=word,
                                              as_self=True)
                              if row.get("outgoing") is True else line),
            }
            used += len(line) + 1

    lines = [picked[int(row.get("message_id") or 0)] for row in selected
             if int(row.get("message_id") or 0) in picked]
    # Обрез ЛЕНТЫ помечается так же честно, как обрез тела: срез без пометки выглядит
    # как «вот вся ветка», и она отвечает, не зная, что выше есть ещё.
    #
    # ⚠ Считать надо от ВСЕГО места, а не от уже обрезанного среза. В первой версии
    # (моей, сегодняшней) числа брались из `selected`, урезанного потолком `cap`: она
    # читала «в соседних ветках ещё 49», когда там было несколько тысяч. И совет был
    # ложный: упирается не `limit`, а бюджет символов, и рост limit ничего не даёт.
    shown = len(picked) + (1 if root_line else 0)
    rest_own = max(0, len([r for r in ordered if _narrow(r)]) - shown)
    rest_all = max(0, len(ordered) - shown)
    marks = []
    if rest_own:
        marks.append(f"в этой ветке выше ещё {rest_own}")
    if rest_all - rest_own > 0:
        marks.append(f"в соседних ветках этого же места ещё {rest_all - rest_own}")
    if marks:
        mark_line = (f"…[ЛЕНТА ОБРЕЗАНА: показано {shown} сообщений, "
                     f"{'; '.join(marks)}. Упирается бюджет символов, не limit: "
                     f"чтобы увидеть больше, поднимай context_summary_chars комнаты "
                     f"(manage_room) или спрашивай узко — "
                     f"group_context(action=\"search\", query=…)]")
        lines.insert(0, {"self": False, "line": mark_line, "role_line": mark_line})
    if root_line:
        # Корень ветки — это её ТЕМА. Он может быть и её собственным сообщением, но
        # стоит отдельной строкой впереди ленты, вне хронологии: приписать его роли
        # `assistant` значило бы начать разговор её репликой, которой в этом месте
        # ленты не было. Оставляем строкой обстановки.
        lines.insert(0, {"self": False, "line": root_line, "role_line": root_line})
    return lines


def map_text(peer_id: str | int, *, current_topic: int | None = None,
             limit: int = 30, not_a_forum: bool = False,
             artifacts: frozenset | set | None = None) -> str:
    """Карта веток комнаты.

    Слово у каждой строки СВОЁ. Строка ориентации уже сказала ей, чем является это
    место; карта, которая сразу за этим называет всё подряд одним словом, возвращает
    ложную модель обратно тем же промптом — в обе стороны. Прежде слово бралось от
    места, где она проснулась, и настоящая тема форума в карте становилась «reply
    thread», то есть единственная граница, которую Telegram действительно провёл,
    объявлялась нашей выдумкой (адверсарка 25.07).

    `artifacts` — ветки, про которые ДОКАЗАНО, что их породили мы. `not_a_forum` — тот
    же факт про всю комнату сразу. Про что не доказано ничего — то и не переименовываем.
    """
    known = frozenset(artifacts or ())
    rows = []
    for item in topics(peer_id, limit=limit):
        topic = item.get("topic_id")
        ours = not_a_forum or (topic in known)
        word = "reply thread" if ours else "topic"
        mark = "root" if topic is None else f"{word} #{topic}"
        current = " [CURRENT]" if topic == current_topic else ""
        # Заголовок у настоящей темы форума — её имя; у нашей же ветки он синтетический
        # («topic #300») и повторяет ярлык другим словом. Такой заголовок не показываем:
        # он ничего не сообщает, а модель комнаты подменяет.
        title = str(item.get("title") or "").strip()
        if not title or title == f"topic #{topic}":
            title = mark
        rows.append(
            f"- {mark}{current}: {title}; "
            f"messages={item.get('message_count', 0)}; "
            f"edits={item.get('revision_count', 0)}; "
            f"participants={item.get('participant_count', len(item.get('participants') or []))}; "
            f"last={item.get('last_timestamp') or '-'}"
        )
    # ⚠ Единственное место во всей сборке, где обрезка молчала совсем: карта отдавала 30
    # веток из 280 и ничем не показывала, что остальные есть. Молчаливый срез выглядит
    # как полный список — и она отвечает, будто это вся комната.
    total = len(projection(peer_id).get("topics", {}) or {})
    if total > len(rows):
        rows.append(f"- …ещё {total - len(rows)} веток этой комнаты не показано "
                    f"(карта отдаёт {len(rows)} самых свежих из {total}; "
                    f"нужную ищи через group_context(action=\"search\", query=…))")
    return "\n".join(rows) or "(topics not archived yet)"


def orientation_bundle(peer_id: str | int, *, current_topic: int | None,
                       query: str = "", cross_topics: str = "off",
                       max_chars: int = 10_000, not_a_forum: bool = False,
                       artifacts: frozenset | set | None = None) -> str:
    """Compact cross-topic orientation for a live turn.

    Raw history is never merged.  ``map`` adds only aggregate topic rows plus a few
    explicitly marked lexical matches from *other* topics.

    В комнате, про которую Telegram прямо ответил «не форум», меняются только слова:
    ветки — не темы, и «другая тема» — не другое место, а соседняя цепочка ответов в
    той же комнате. Что попадает в бандл, `not_a_forum` не меняет.
    """

    if str(cross_topics or "off").casefold() != "map":
        return ""
    budget = max(1000, min(MAX_CONTEXT_CHARS, int(max_chars)))
    head = ("Map of reply threads in this ONE room (aggregate only; no history merged):\n"
            if not_a_forum else
            "Cross-topic map (aggregate only; no topic history was merged):\n")
    chunks = [head + map_text(peer_id, current_topic=current_topic,
                              not_a_forum=not_a_forum, artifacts=artifacts)]
    if query.strip():
        hits = [row for row in search(peer_id, query, limit=12)
                if row.get("topic_id") != current_topic][:6]
        if hits:
            chunks.append(
                ("Query-relevant excerpts from OTHER threads of this same room "
                 if not_a_forum else
                 "Query-relevant excerpts from OTHER topics ")
                + "(marked; use group_context before relying on their wider context):\n"
                + "\n".join("[CROSS-TOPIC EXCERPT] " + _format_message(row, max_text=500)
                            for row in hits)
            )
    value = "\n\n".join(chunks)
    return value[:budget]


def describe(peer_id: str | int, *, action: str, topic_id: int | None = None,
             query: str = "", limit: int = 20, max_chars: int = 20_000,
             whole_room: bool = False, members: frozenset | set | None = None,
             thread_word: str = "topic",
             artifacts: frozenset | set | None = None) -> str:
    """Одна дверь ко всем чтениям архива — и одно место, где решается «что такое ветка».

    Границы места считает реестр маршрутов (`telegram_routes.read_scope`) — здесь их
    только применяют. `topic_id` остаётся веткой, в которой она проснулась; расширяют
    чтение `whole_room` и `members`, и оба приходят снаружи. Ничего не передали —
    прежнее поведение до последней строки.
    """
    action = str(action or "context").strip().casefold()
    threads = str(thread_word or "topic") != "topic"
    if action == "topics":
        # Карта — про КОМНАТУ, а не про место, где она проснулась: каждая ветка
        # называется по своей природе. `whole_room` (доказанный не-форум) означает, что
        # наши все; иначе смотрим поимённо.
        return map_text(peer_id, current_topic=topic_id, limit=limit,
                        not_a_forum=whole_room, artifacts=artifacts)
    if action == "search":
        rows = search(peer_id, query, topic_id=topic_id, limit=limit)
        return "\n".join(_format_message(row, thread_word=thread_word) for row in rows) \
            or "(nothing found in this group)"
    if action == "context":
        wide = bool(whole_room or members is not None)
        return context(peer_id, topic_id=topic_id, limit=limit, max_chars=max_chars,
                       whole_room=whole_room, members=members,
                       thread_word=thread_word) or (
            "(this place has no archived messages yet)" if wide
            else "(this exact topic has no archived messages yet)")
    if action == "message":
        return message_text(peer_id, message_id=limit)
    return "action: topics | search | context | message"


def message_text(peer_id: str | int, *, message_id: int) -> str:
    """Одно сообщение из архива ЦЕЛИКОМ — то, что обещает маркер обрезки.

    Без этого маркер «полностью — group_context(...)» был бы инструкцией, которая
    возвращает тот же обрезок: все прочие действия рендерят через ``_format_message``
    и применяют тот же потолок тела. Обещание в записи обязано быть выполнимым, иначе
    молчаливая ложь просто меняется на громкую.
    """

    try:
        wanted = _positive(message_id, optional=False)
    except ValueError as exc:
        return f"message: {exc}"
    latest = None
    for row in iter_records(peer_id):
        if row.get("kind") == "message" and int(row.get("message_id") or 0) == wanted:
            latest = row
    if latest is None:
        return f"(message #{wanted} is not in this group's archive)"
    return _format_message(latest, max_text=MAX_CONTEXT_CHARS)


def archived_message_count(peer_id: str | int) -> int:
    return int(projection(peer_id).get("message_count") or 0)


def backfill_due(peer_id: str | int, limit: int) -> bool:
    requested = max(0, min(5_000, int(limit)))
    if requested <= 0:
        return False
    if archived_message_count(peer_id) >= requested:
        return False
    try:
        state = json.loads(backfill_state_path(peer_id).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return True
    return not (bool(state.get("complete")) and int(state.get("limit") or 0) >= requested)


def mark_backfill(peer_id: str | int, *, limit: int, scanned: int,
                  complete: bool) -> dict:
    """Write a disposable resume receipt after a canonical backfill batch."""

    with _LOCK:
        peer = _peer(peer_id)
        row = {
            "schema": "praxis.group.backfill.v1", "peer_id": peer,
            "limit": max(0, min(5_000, int(limit))),
            "scanned": max(0, int(scanned)), "complete": bool(complete),
            "message_count": archived_message_count(peer), "updated_at": _iso(),
        }
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        target = backfill_state_path(peer)
        temp = target.with_suffix(".json.tmp")
        temp.write_text(json.dumps(row, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                        encoding="utf-8")
        temp.replace(target)
        return row
