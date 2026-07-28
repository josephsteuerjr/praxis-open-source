"""
Конверт контекста — ТЕНЕВОЙ. Ничего не решает, никого не гейтит, никем не читается
на исполнении. Его единственная работа — записывать факты, которые сегодня система
теряет или подменяет, чтобы решение о переходе принималось по измерению, а не по спору.

Почему набор полей именно такой (пункт 4, после разбора замысла):

- `place_key` НЕ включён: он выводится из (audience_root, forum_status, focus_key) и
  добавляет только второго производителя одного факта — а два производителя уже
  расходятся сегодня на корневом сообщении темы.
- `focus_key` НЕ включён: `thread_root_for_message` в обычной супергруппе возвращает
  РОДИТЕЛЯ, а не корень (Telegram не заполняет reply_to_top_id вне форумов), а
  сообщение, которое никому не отвечает, становится собственной дорожкой. Замер:
  шесть ambient-сообщений — шесть дорожек. Если бы этим ключом когда-нибудь
  зарулили cooldown, она просыпалась бы на каждое сообщение в каждой комнате.
- `delivery_route` НЕ включён: это АДРЕС, а системе нужна ПОПЫТКА. Живых пространств
  идентичности попыток уже три (чанки текста, прямой аутбокс, медиа-очередь), у каждого
  свой стабильный random_id и свой счётчик. Адрес не ключует расписку.
- `run_id` НЕ включён: каждый ChannelContext строится РАНЬШЕ своего рана. Поле было бы
  либо None в момент чеканки, либо дозаполнялось бы позже — и «неизменяемый» стало бы
  неправдой. Ран ДЕРЖИТ конверт, а не наоборот.
- `is_forum` не трогаем: он выглядит однострочной правкой и на самом деле перекладывает
  ключ всего слоя хранения (буферы, имена файлов, sha256-токен медиа, durable chat_id).
  `forum_status="unknown"` воспроизводит сегодняшнее поведение байт-в-байт.

И главное: `actor_principal` нельзя определить как «то, что лежит в ctx.principal_id» —
там уже стоит подменённое значение, и тень совпадала бы с продом на 100%, ничего не
измеряя. Поэтому здесь два поля: сырой отправитель и флаг подмены.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("praxis-envelope")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
# `.state` НЕ подпадает ни под одно правило memory_fts._selected_jsonl — теневые
# замеры не должны попадать ни в её recall, ни в промпт. Это приборы, не память.
SHADOW_PATH = BASE / "memory" / ".state" / "envelope_shadow.jsonl"
SHADOW_KEEP = 4000

SCHEMA = "praxis.context.envelope.v1"

TRIGGER_KINDS = ("single", "coalesced", "synthetic", "none")
DISCLOSURE_TIERS = ("self", "owner_private", "person_private", "room_public")
PROVENANCE = ("measured", "reconstructed", "absent")

_LOCK = threading.Lock()


@dataclass(frozen=True)
class Trigger:
    """Кто и чем вызвал ход. Множественное число намеренно: групповое пробуждение
    отвечает K отправителям, а называет одного; forge-событие коалесцирует N расписок."""

    principal_id: str | None = None
    message_id: int | None = None
    ts: float | None = None
    kind: str = ""


@dataclass(frozen=True)
class RunEnvelope:
    # кто действует
    actor_principal_raw: str | None = None   # НАСТОЯЩИЙ отправитель, всегда
    actor_synthesized: bool = False          # прод подставил бы здесь praxis:self
    # кто вызвал
    triggers: tuple[Trigger, ...] = ()
    trigger_kind: str = "none"
    # по чьему поручению
    on_behalf_of: str | None = None
    delegation_ref: str = ""                 # absence:<окно> | wake:not_addressed | ...
    # куда
    audience_root: str | None = None
    forum_status: str = "unknown"            # true | false | unknown
    route_epoch: int = 0
    # что именно разбудило
    origin_message_id: int | None = None     # строго одно или ничего, без фолбэков
    origin_addressed: bool = False
    # кому можно показывать
    disclosure_tier: str = "room_public"
    # насколько этой записи можно верить
    provenance: str = "absent"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["triggers"] = [asdict(t) for t in self.triggers]
        d["schema"] = SCHEMA
        return d


def _tier(*, chat_id, is_dm: bool, owner: bool, praxis_self: bool) -> str:
    """Тир раскрытия — из (аудитория, актор), а НЕ из scope.

    `scope` сегодня одновременно ярлык аудитории, полномочие на resume, компонент пути
    на диске и приватный фильтр. Пятое мнение ему не нужно — нужен отдельный факт."""
    if chat_id is None:
        return "self"
    if not is_dm:
        return "room_public"
    if owner or praxis_self:
        return "owner_private"
    return "person_private"


def measure(*, chat_id=None, room_id=None, is_dm: bool = True, owner: bool = False,
            praxis_self: bool = False, actor_raw=None, synthesized: bool = False,
            triggers: tuple[Trigger, ...] = (), on_behalf_of=None,
            delegation_ref: str = "", origin_message_id=None,
            origin_addressed: bool = False, forum_status: str = "unknown",
            route_epoch: int = 0) -> RunEnvelope:
    """Собрать конверт из фактов, ИЗМЕРЕННЫХ на месте (provenance=measured)."""
    kind = "none"
    if triggers:
        kind = "single" if len(triggers) == 1 else "coalesced"
    elif synthesized:
        kind = "synthetic"
    return RunEnvelope(
        actor_principal_raw=(str(actor_raw) if actor_raw is not None else None),
        actor_synthesized=bool(synthesized),
        triggers=tuple(triggers),
        trigger_kind=kind,
        on_behalf_of=(str(on_behalf_of) if on_behalf_of is not None else None),
        delegation_ref=str(delegation_ref or ""),
        audience_root=(str(room_id) if room_id is not None
                       else (str(chat_id) if chat_id is not None else None)),
        forum_status=(forum_status if forum_status in ("true", "false", "unknown")
                      else "unknown"),
        route_epoch=int(route_epoch or 0),
        origin_message_id=(int(origin_message_id)
                           if str(origin_message_id or "").lstrip("-").isdigit() else None),
        origin_addressed=bool(origin_addressed),
        disclosure_tier=_tier(chat_id=chat_id, is_dm=is_dm, owner=owner,
                              praxis_self=praxis_self),
        provenance="measured",
    )


def record(envelope: RunEnvelope, *, run_id: str = "", note: str = "") -> None:
    """Записать теневой замер. Никогда не бросает и ничего не гейтит.

    Первый ожидаемый результат — доля ходов с `actor_synthesized=True`, которым при этом
    выданы суверенные руки, с разбивкой по комнатам. Это и есть предмет решения о
    переходе; сам конверт пока не читает никто."""
    try:
        row = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace(
                "+00:00", "Z"),
            "run_id": str(run_id or ""),
            "note": str(note or "")[:200],
            "envelope": envelope.to_dict(),
        }
        with _LOCK:
            SHADOW_PATH.parent.mkdir(parents=True, exist_ok=True)
            with SHADOW_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        log.debug("теневой замер конверта не записался", exc_info=True)


PROBE_PATH = BASE / "memory" / ".state" / "probes.jsonl"


def record_probe(kind: str, payload: dict) -> None:
    """Показание прибора. Тот же принцип, что и у теневого конверта: измерения живут
    в `.state`, вне правил индексации, и не становятся её памятью. Никогда не бросает."""
    try:
        row = {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace(
                "+00:00", "Z"),
            "kind": str(kind or "")[:64],
            "payload": payload if isinstance(payload, dict) else {"value": payload},
        }
        with _LOCK:
            PROBE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with PROBE_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        log.debug("показание прибора не записалось", exc_info=True)


def _load(limit: int = SHADOW_KEEP) -> list[dict]:
    try:
        lines = SHADOW_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def divergence(limit: int = SHADOW_KEEP) -> dict:
    """Сводка для решения о переходе: сколько ходов действовали под подменённым актором,
    в каких комнатах, и сколько раз происхождение вообще не удалось измерить."""
    rows = _load(limit)
    by_room: dict[str, int] = {}
    synthesized = unknown_forum = no_origin = 0
    for r in rows:
        env = r.get("envelope") or {}
        if env.get("actor_synthesized"):
            synthesized += 1
            room = str(env.get("audience_root") or "?")
            by_room[room] = by_room.get(room, 0) + 1
        if str(env.get("forum_status")) == "unknown":
            unknown_forum += 1
        if env.get("origin_message_id") is None:
            no_origin += 1
    return {
        "runs": len(rows),
        "actor_synthesized": synthesized,
        "actor_synthesized_by_room": dict(sorted(
            by_room.items(), key=lambda kv: kv[1], reverse=True)[:20]),
        "forum_status_unknown": unknown_forum,
        "origin_missing": no_origin,
    }
