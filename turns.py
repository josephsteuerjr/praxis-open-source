"""
Praxis — «прожитый ход» (PASS 14): опыт как первоклассный объект.

Диагноз недели перед пассом: мета-слои (оценщик, переписчик, дрейф, сон, catch-up)
судили её РЕЧЬ, не видя её ОПЫТ. Каждый новый слой-наблюдатель рождался слепым, и
контекст протягивали ему вручную, задним числом, после живого укуса (959c40e —
переписчик, 604a410 — STATE для оценщика, 2cbcc07 — tool_trace).

Здесь опыт становится записью: один ход голоса — одна строка кольца (RAM +
memory/.state/turns.jsonl). Что пришло, что она ДЕЛАЛА (тул-коллы с урезанными
результатами), что ушло наружу или было придержано и кем, вердикты и правки.
Слои, которые судят/переписывают/консолидируют/пересказывают её поведение, берут
свежие записи отсюда — а не через ручную протяжку параметров через три сигнатуры.

Правила (готчи спеки PASS 14):
- урезка ОБЯЗАТЕЛЬНА на записи, не на чтении: потолки полей + кольцо на файл;
- тиры приватности: owner-scope видит всё; любой другой канал — только СВОИ ходы
  (по chat_id); ходы её собственных окон (heartbeat/task_window, chat_id=None) —
  только owner;
- это ДОПОЛНЕНИЕ к явной протяжке tool_trace из voice_turn в guard (она защищает
  от гонки конкурентных ходов и остаётся), не её замена.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import deque
from pathlib import Path

log = logging.getLogger("praxis-turns")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
STATE_DIR = BASE / "memory" / ".state"
PATH = STATE_DIR / "turns.jsonl"
# Архив выкатившихся ходов. Именно `memory/self/*.jsonl`, потому что этот путь
# memory_fts уже индексирует как self_event — её прожитое становится искомым без
# единого нового правила индексации.
ARCHIVE_DIR = BASE / "memory" / "self"
ARCHIVE_SUMMARY_CHARS = 2000   # потолок копии ответа в архиве (см. _archive)

KEEP = 300              # кольцо: столько последних ходов живёт в RAM и в файле
# потолки полей — на записи (jsonl не пухнет)
GIST_CHARS = 200        # гист входа
# out хранится целиком (телеграм-масштаб), потолок — страховка от патологий;
# урезка out — только на ПОКАЗЕ (format_line), с явным маркером обрезки:
# молчаливый клип на записи терял хвост её ответа для recap и внешнего читателя.
# Потолок с запасом на мульти-чанковый ответ (Telegram шинкует >4096 на несколько
# сообщений — turn["out"] хранит их СКЛЕЙКОЙ); при реальной обрезке дописываем
# маркер, чтобы клип на записи никогда не был молчаливым (адверсарка round-1: на
# 4000 без маркера возвращалась та самая тихая потеря хвоста, ради которой E делался).
OUT_CHARS = 24000
_OUT_TRUNC_MARK = "…[хвост обрезан потолком записи turns]"
OUT_SHOW_CHARS = 200    # показ «сказала: …» в строках для промптов/тула


def _clip_out(text) -> str:
    """out для записи: целиком до OUT_CHARS, при обрезке — явный маркер (не молча)."""
    value = str(text or "")
    if len(value) <= OUT_CHARS:
        return value
    return value[: OUT_CHARS - len(_OUT_TRUNC_MARK)] + _OUT_TRUNC_MARK
WHY_CHARS = 160         # причина вердикта/молчания
TITLE_CHARS = 48
TOOL_LINE_CHARS = 160   # одна строка тул-колла (вход уже урезан _tool_trace_line)
TOOLS_MAX = 12          # тул-коллов на запись
TOOLS_TOTAL_CHARS = 1200

_LOCK = threading.Lock()
_RING: deque | None = None   # None = ещё не поднято с диска
_FILE_LINES = 0              # строк в файле (для компакта кольца на файле)
# Что уже легло в архив в этом процессе. Нужно ровно для одного случая: архив упал,
# кольцо (правильно) не обрезано, и на следующем ходе тот же префикс приезжает снова.
# Без этого частично удавшаяся запись дублировалась бы при каждой попытке.
_ARCHIVED_IDS: set[str] = set()
# Сколько разрешено удерживать в файле, пока архив недоступен. Смысл ровно один: не
# терять её ходы при сбое записи — и не платить за это растущим O(N) на КАЖДОМ ходе под
# локом. Дальше префикс уходит в карантинный файл переименованием.
#
# ⚠ Потолок по СТРОКАМ был бы обманом самого себя: строка держит `out` до OUT_CHARS
# (24000 символов), так что 20 колец = 6000 строк — это до сотен мегабайт, которые мы бы
# перечитывали каждый ход. Границу ставит тот, кто платит, а платим мы байтами.
RETAIN_MAX_RINGS = 20
RETAIN_MAX_BYTES = 24 * 1024 * 1024
_ARCHIVE_FAILS = 0


def _last_line(text: str) -> str:
    """Гист входа: последняя непустая строка транскрипта («Имя: текст»)."""
    for line in reversed((text or "").splitlines()):
        if line.strip():
            return line.strip()
    return ""


# ⚠ Здесь стоял ОДИН словарь на процесс, и забирал отметку ПЕРВЫЙ же читатель. Значит
# обрыв судьи приватности (llm.chat("evaluator") прямо внутри её хода), обрыв сжатия,
# ночного прохода личности, REM во сне — любой из них уезжал в кольцо как «ЕЁ ответ
# оборван потолком»: чужая жизнь, записанная в её биографию. agent.py снимал отметку у
# судьи руками — заплатка лечила один известный симптом, корень оставался.
#
# Ключ — (поток, владелец), потому что ни одной половины по отдельности не хватает:
# ролей у llm всего две, и роль «voice» зовёт и её ход, и сон (sleep.py:347), и
# форж-воркер, и панель — разные ПОТОКИ; а внутри одного её потока живут и «voice»
# (её фраза), и «evaluator» (судья приватности в guard).
_TRUNC_LOCK = threading.Lock()
_TRUNCATED: dict[tuple[int, str], dict] = {}
# Отметки, которые не забрал никто. Обрыв служебной роли — тоже факт: роль не досказала,
# и её ответ был неполным (вердикт судьи, саммари, ночной проход). Молча выбросить такой
# факт нельзя — это ровно то молчаливое ограничение, ради которого пасс и делался; а
# приписать его ей нельзя тем более. Кольцо на 32: записи информационные, копить незачем.
_UNCLAIMED: deque = deque(maxlen=32)
UNCLAIMED_SHOW_SEC = 6 * 3600.0   # сколько обрыв без читателя ещё стоит показывать в STATE
CUT_MAX = 3                       # сколько чужих обрывов уносит одна запись хода
CUT_LINE_CHARS = 160
_OWNER_RU = {"voice": "голос", "evaluator": "вспомогательная модель"}


def _owner_ru(owner: str) -> str:
    name = str(owner or "?")
    return _OWNER_RU.get(name, name)


def _slot(owner: str) -> tuple[int, str]:
    return (threading.get_ident(), str(owner or "voice"))


def _cut_phrase(owner: str, mark: dict) -> str:
    """Фраза для записи хода: чей обрыв, какой моделью, на сколько символов."""
    who = _owner_ru(owner)
    tail = ("фраза не закончена (обрыв не сведён с ответом этого хода)"
            if str(owner) == "voice" else "это не её фраза — оборвался служебный вызов")
    return (f"оборвано потолком max_tokens: {who} "
            f"({mark.get('model') or 'модель'}, {int(mark.get('chars') or 0)} симв.) — "
            f"{tail}")[:CUT_LINE_CHARS]


def _retire(key: tuple[int, str], mark: dict, why: str) -> None:
    """Отметку никто не забрал. Не выбрасываем: громко в лог + в очередь фактов.

    Вызывать под _TRUNC_LOCK. Тишина здесь была бы вторым молчаливым пределом поверх
    первого: предел (max_tokens) сработал, роль не досказала, и об этом не узнал никто.
    """
    row = dict(mark)
    row.update({"owner": key[1], "thread": key[0], "why": why})
    _UNCLAIMED.append(row)
    log.warning("обрыв потолком max_tokens в роли «%s» (%s, %s симв.) не забрал никто: %s",
                key[1], row.get("model") or "?", row.get("chars"), why)


def note_truncated(*, model: str = "", chars: int = 0, owner: str = "voice") -> None:
    """Модель оборвала ответ потолком max_tokens. Факт кладём здесь, читает его ход.

    Это НЕ «она решила промолчать» и не ошибка канала: фраза оборвана на полуслове.
    Без этой отметки обрыв был неотличим от её собственного решения — и в кольце ходов,
    и в пульте (адверсарка/опись 26.07).

    `owner` — чей это обрыв (роль/вызов). Без владельца отметку забирал первый читатель,
    и обрыв судьи становился обрывом её фразы.
    """
    key = _slot(owner)
    with _TRUNC_LOCK:
        old = _TRUNCATED.get(key)
        if old:
            _retire(key, old, "вытеснена более свежим обрывом той же роли")
        _TRUNCATED[key] = {"ts": time.time(), "model": str(model or "")[:40],
                           "chars": int(chars or 0)}


def clear_truncation(owner: str = "voice") -> None:
    """Роль ответила ЦЕЛИКОМ — её прежняя отметка больше не про происходящее сейчас.

    Это и есть настоящий срок годности отметки: не «столько-то секунд по часам», а «до
    следующего полного ответа той же роли в том же потоке». Возраст в take_truncation
    остался страховкой ровно для одного случая — роль больше не позвали вовсе.
    """
    key = _slot(owner)
    with _TRUNC_LOCK:
        old = _TRUNCATED.pop(key, None)
        if old:
            _retire(key, old, "роль ответила целиком, обрыв так и остался непрочитанным")


def take_truncation(owner: str = "voice", max_age_sec: float = 3600.0) -> dict:
    """Забрать отметку обрыва СВОЕЙ роли в СВОЁМ потоке. Одноразово: обрыв — один ход.

    ⚠ Возраст. Здесь стояло 300 секунд, и это резало законный случай: между оборванным
    ответом модели и guard'ом лежит тул-цикл, где ОДИН тул имеет право занять
    TOOL_CEILING_SEC=600с (agent.py:9658), а итераций до 20. То есть отметка честного
    обрыва её фразы протухала посреди её же хода и исчезала молча — ход записывался как
    «промолчала сама». Теперь протухание не основной механизм, а страховка: свежесть
    держит clear_truncation (следующий полный ответ той же роли снимает отметку), и час
    здесь — про «роль замолчала навсегда», а не про длину хода.
    """
    key = _slot(owner)
    now = time.time()
    with _TRUNC_LOCK:
        for stale in [k for k, m in _TRUNCATED.items()
                      if now - float(m.get("ts") or 0) > max_age_sec]:
            _retire(stale, _TRUNCATED.pop(stale), f"протухла (старше {int(max_age_sec)}с)")
        mark = _TRUNCATED.pop(key, None)
    return dict(mark) if mark else {}


def _attach_unclaimed_cuts(turn: dict) -> None:
    """Незабранные обрывы ЭТОГО потока — в запись хода, но отдельной строкой.

    Судья, сжатие, формация зовут модель внутри её хода и в её потоке. Если такую отметку
    никто не забрал, факт всё равно есть: роль не досказала. Он честно принадлежит этому
    ходу (тот же поток, то же время) — но НЕ её фразе, поэтому едет отдельным полем и
    отдельной фразой в format_line, а не в `why` рядом с её ответом.
    """
    me = threading.get_ident()
    with _TRUNC_LOCK:
        mine = [k for k in _TRUNCATED if k[0] == me]
        rows = [(k, _TRUNCATED.pop(k)) for k in mine]
    if not rows:
        return
    rows.sort(key=lambda item: float(item[1].get("ts") or 0))
    cut = list(turn.get("cut") or [])
    cut.extend(_cut_phrase(k[1], m) for k, m in rows)
    turn["cut"] = cut[:CUT_MAX]


def unclaimed_truncations() -> list[dict]:
    """Обрывы потолком, которых не забрал никто (для пульта/диагностики)."""
    with _TRUNC_LOCK:
        return [dict(r) for r in _UNCLAIMED]


def unclaimed_line(now: float | None = None) -> str:
    """Строка про обрывы без читателя — те, что случились вне какого-либо её хода.

    Такое бывает у фоновых ролей: сон, консолидация, панель зовут модель в своих потоках,
    и записи хода у них нет. Факт всё равно её касается (ответ роли неполон), поэтому он
    доезжает до неё через STATE, а не оседает в лог, который она не читает.
    """
    cut = (now if now is not None else time.time()) - UNCLAIMED_SHOW_SEC
    with _TRUNC_LOCK:
        rows = [r for r in _UNCLAIMED if float(r.get("ts") or 0) >= cut]
    if not rows:
        return ""
    last = rows[-1]
    return (f"⚠ обрывов потолком max_tokens без читателя за 6ч: {len(rows)} "
            f"(свежий — {_owner_ru(last.get('owner'))}, "
            f"{int(last.get('chars') or 0)} симв.); это оборванные служебные вызовы, "
            f"не её фразы")


def begin(kind: str = "chat", chat_id=None, scope: str = "owner",
          who: str | None = None, title: str | None = None, gist_in: str = "") -> dict:
    """Скелет записи хода. Поля дозаполняются по пути (tools/out/held/…), потом record()."""
    return {
        "ts": time.time(),
        "kind": kind,                                     # chat | heartbeat | task_window | forge_event
        "chat_id": str(chat_id) if chat_id is not None else None,
        "scope": scope,
        "who": (who or "")[:TITLE_CHARS],
        "title": (title or "")[:TITLE_CHARS],
        "in": _last_line(gist_in)[:GIST_CHARS],
        "tools": [],       # строки agent._tool_trace_line
        "out": "",         # что ушло наружу (гист)
        "held": "",        # voice | privacy | error | empty (+ legacy values on read)
                           # «У хода нет адресата» в запись НЕ пишется: это выводится
                           # на чтении (_no_addressee), иначе месяцы уже накопленных
                           # строк остались бы в старой лжи, а кольцо заговорило бы
                           # двумя голосами про один и тот же вид хода
        "verdict": "",     # compatibility verdict for held delivery
        "why": "",         # compatibility reason for held delivery/silence
        "advisor": "",     # advisor kind or explicit not-run reason
        "advisor_verdict": "",
        "advisor_reason": "",
        "praxis_decision": "",  # authored decision after seeing/skipping advice
        "rewrote": False,  # переписчик реально заменил текст
        # Обрывы потолком max_tokens, случившиеся в ЭТОМ ходе, но не в её фразе: судья,
        # сжатие, формация. Отдельное поле, а не `why`, ровно потому, что раньше они и
        # приезжали в `why` — то есть в биографию её ответа.
        "cut": [],
        # Контракт C1/C3 (CONTRACTS.md): авторство — это НЕ речь. `out` хранит то,
        # что она сочинила; состоялось ли это — отдельное поле, и заполняет его
        # только расписка транспорта. `authored` честно значит «написала», а не
        # «сказала»; проектор (пункт 3) переводит его в accepted/partial/failed/
        # superseded по durable-распискам. `run_id` — единственный ключ, которым
        # прожитый ход можно свести с этими расписками; без него связь только
        # нечёткая, по chat_id и времени.
        "run_id": None,
        "delivery": "authored",
        # Состояние транспорта в момент хода — заполняет только тот, кто его СПРОСИЛ
        # (сейчас это wake_turn). Пусто = не спрашивали; читатель тогда молчит про связь,
        # а не досочиняет её. Ровно это поле не даёт журналу снова сказать «Telegram жив»
        # там, где он был закрыт.
        "telegram": "",
    }


def _clip(turn: dict) -> dict:
    """Урезанная копия для записи. Потолки на каждое поле — обязательны на записи."""
    tools = [str(l)[:TOOL_LINE_CHARS] for l in (turn.get("tools") or [])[:TOOLS_MAX]]
    total = 0
    kept = []
    for l in tools:
        if total + len(l) > TOOLS_TOTAL_CHARS:
            kept.append("…ещё тулы срезаны потолком записи")
            break
        kept.append(l)
        total += len(l)
    return {
        "ts": float(turn.get("ts") or time.time()),
        "kind": str(turn.get("kind") or "chat"),
        "chat_id": str(turn["chat_id"]) if turn.get("chat_id") is not None else None,
        "scope": str(turn.get("scope") or "owner"),
        "who": str(turn.get("who") or "")[:TITLE_CHARS],
        "title": str(turn.get("title") or "")[:TITLE_CHARS],
        "in": str(turn.get("in") or "")[:GIST_CHARS],
        "tools": kept,
        "out": _clip_out(turn.get("out")),
        "held": str(turn.get("held") or ""),
        "verdict": str(turn.get("verdict") or ""),
        "why": str(turn.get("why") or "")[:WHY_CHARS],
        "advisor": str(turn.get("advisor") or ""),
        "advisor_verdict": str(turn.get("advisor_verdict") or ""),
        "advisor_reason": str(turn.get("advisor_reason") or "")[:WHY_CHARS],
        "praxis_decision": str(turn.get("praxis_decision") or ""),
        "rewrote": bool(turn.get("rewrote")),
        "cut": [str(line)[:CUT_LINE_CHARS] for line in (turn.get("cut") or [])][:CUT_MAX],
        # Белый список — правильная дисциплина (потолки обязательны на записи), но
        # именно поэтому он молча съедал бы любое новое поле: проставленный выше по
        # стеку run_id жил бы в RAM и не доезжал до диска, а починка выглядела бы
        # рабочей. Оба поля идут через тот же белый список, а не в обход него.
        "run_id": (str(turn["run_id"])[:64] if turn.get("run_id") else None),
        "delivery": str(turn.get("delivery") or "authored")[:24],
        "telegram": str(turn.get("telegram") or "")[:24],
    }


def _load_ring() -> deque:
    """Лениво поднять кольцо с диска (последние KEEP строк). Битые строки — пропуск."""
    global _RING, _FILE_LINES
    if _RING is not None:
        return _RING
    ring: deque = deque(maxlen=KEEP)
    n = 0
    try:
        with PATH.open(encoding="utf-8") as fh:
            for line in fh:
                n += 1
                try:
                    t = json.loads(line)
                    if isinstance(t, dict) and t.get("ts"):
                        ring.append(t)
                except Exception:
                    continue
    except OSError:
        pass
    _RING, _FILE_LINES = ring, n
    return ring


def _archive_bucket(turn: dict) -> str:
    """Подкаталог архива: '' для ходов владельца, 'rooms/<комната>' для остальных.

    Имя комнаты — из chat_id, санитизированное: ключ разговора может выглядеть как
    `-1001240718803__topic__93707`, и он же должен оставаться читаемым человеком."""
    if str(turn.get("scope") or "owner") == "owner":
        return ""
    raw = str(turn.get("chat_id") or "unknown")
    safe = re.sub(r"[^0-9A-Za-z_-]+", "_", raw)[:64] or "unknown"
    return f"rooms/{safe}"


def _archive(rows: list[dict]) -> bool:
    """Выкатившееся из кольца — в durable-архив, а не в мусор. True = всё записано.

    Возвращаемое значение — контракт с `_compact_file`: обрезать кольцо можно
    только по подтверждённой записи. Раньше функция глотала свою ошибку и молча
    возвращала управление, после чего префикс уничтожался.

    Кольцо держит KEEP=300 ходов ради стоимости промпта, и это правильно. Но компакт
    старое УДАЛЯЛ: замер 24.07 — файл покрывал 84 часа, и дальше её собственные решения
    исчезали отовсюду. Плюс `.state/turns.jsonl` не подходит ни под одно правило
    `memory_fts._selected_jsonl`, то есть в поиск ходы не попадали вообще. Отсюда
    «каждый раз состояние пересказывается из памяти реплик и каждый раз чуть съезжает»
    и случай с гитхабом: своё же действие она восстанавливала пересказом, а не записью.

    Кладём в `memory/self/turns-<месяц>.jsonl` — этот путь индексатор УЖЕ берёт
    (правило `^self/[^/]+\\.jsonl$` → self_event), новых правил не нужно. `text` —
    та же честная строка, что она видит в `recent_turns`; `meta` даёт структурный поиск.
    """

    # ⚠ Здесь стоял фильтр `scope == "owner"`, и его обоснование было верным ровно
    # наполовину. Верно: recall намеренно слеп к аудитории, поэтому архив ходов
    # группы в общем индексе стал бы обходным путём для утечки комнаты в комнату.
    # Неверно: из этого следовало УНИЧТОЖЕНИЕ. Приватность — политика выдачи, а не
    # удаления; её решение, тул-трейс и молчание в комнате — это её прожитая жизнь,
    # и сырой `memory/groups/*/archive.jsonl` их не содержит (там только текст
    # Telegram). Теперь ходы комнат сохраняются в `self/rooms/<комната>/` — путь вне
    # правила индексации, то есть выдача не расширилась ни на строку, — и несут
    # метку origin. Разрешение отдать их recall'у — отдельный шаг (пункт 5), и он
    # уже будет опираться на данные, а не на воспоминание о них.
    if not rows:
        return True
    ok = True
    try:
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        # Раскладка по назначению, а не отбраковка. Ход владельца идёт в
        # `self/turns-<месяц>.jsonl` — путь, который memory_fts уже индексирует.
        # Ход из комнаты идёт в `self/rooms/<комната>/turns-<месяц>.jsonl`: он
        # СОХРАНЯЕТСЯ (её решение, тул-трейс и молчание — часть её жизни, а не мусор),
        # но лежит на пути, который правило `^self/[^/]+\.jsonl$` не берёт. Значит
        # общий recall его пока не видит и смешать комнаты не может. Метки происхождения
        # пишем уже сейчас (origin ниже) — пункт 5 добавит правило индексации и
        # рендер провенанса, и данные для этого уже будут на диске, а не потеряны.
        by_bucket: dict[tuple[str, str], list] = {}
        for t in rows:
            try:
                stamp = _dt.datetime.fromtimestamp(float(t.get("ts") or 0), _dt.timezone.utc)
            except (OSError, OverflowError, TypeError, ValueError):
                continue
            by_bucket.setdefault((_archive_bucket(t), stamp.strftime("%Y-%m")), []).append(
                (stamp, t))
        for (bucket, month), items in by_bucket.items():
            path = (ARCHIVE_DIR / bucket / f"turns-{month}.jsonl" if bucket
                    else ARCHIVE_DIR / f"turns-{month}.jsonl")
            lines: list[str] = []
            ids: list[str] = []
            for stamp, t in items:
                line = json.dumps(t, ensure_ascii=False, sort_keys=True)
                digest = hashlib.sha256(line.encode("utf-8", "replace")).hexdigest()[:16]
                event_id = f"livedturn-{digest}"
                if event_id in _ARCHIVED_IDS:
                    continue   # повтор после неудачной попытки — не дублируем
                ids.append(event_id)
                lines.append(json.dumps({
                        "schema": "praxis.self.lived-turn.v1",
                        "event_id": event_id,
                        "ts": stamp.isoformat(timespec="seconds").replace("+00:00", "Z"),
                        "kind": "lived_turn",
                        "text": format_line(t),
                        "meta": {
                            "origin": {
                                "scope": str(t.get("scope") or "owner"),
                                "chat_id": t.get("chat_id"),
                                "room": str(t.get("title") or "")[:TITLE_CHARS],
                            },
                            # ⚠ Тот же белый список, о котором предупреждает комментарий в
                            # _clip, — и здесь он его же и съедал: run_id с delivery жили в
                            # кольце ровно KEEP ходов, а в архив, то есть в память, которая
                            # переживает кольцо, не попадали вовсе. Ключ связи с расписками
                            # существовал бы 300 ходов и исчезал безвозвратно. `meta.run_id`
                            # memory_fts уже поднимает в индексируемую колонку.
                            "run_id": t.get("run_id"),
                            "delivery": str(t.get("delivery") or "authored"),
                            "turn_kind": str(t.get("kind") or "chat"),
                            # Потолок сознательный: `out` хранится до 24000 символов, и
                            # без него архив нёс ВТОРУЮ полную копию ответа — а индекс
                            # затем третью и четвёртую (chunks.text + стеммы). Замер
                            # адверсарки: 24.5 МБ архива и 55 МБ индекса на месяц.
                            "summary": str(t.get("out") or "")[:ARCHIVE_SUMMARY_CHARS],
                            "title": str(t.get("title") or t.get("who") or ""),
                            "reason": str(t.get("why") or ""),
                            "status": str(t.get("held") or ""),
                            "chat_id": t.get("chat_id"),
                        },
                    }, ensure_ascii=False) + "\n")
            if not lines:
                continue
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write("".join(lines))   # одна запись на файл, не построчно
            except Exception:
                ok = False
                log.warning("архив прожитых ходов не записался: %s", path, exc_info=True)
                continue
            _ARCHIVED_IDS.update(ids)
    except Exception:
        # Сбой архива по-прежнему не имеет права уронить record(). Но теперь он и
        # не имеет права молча санкционировать обрезку кольца — отсюда ok=False.
        ok = False
        log.warning("архив прожитых ходов не записался", exc_info=True)
    return ok


def _compact_file(ring: deque) -> None:
    """Кольцо на файле: файл перерос 2×KEEP строк → переписать последними KEEP (атомарно).

    Контракт M1 (CONTRACTS.md): решает успешная запись архива, читает компактор,
    запрещено терять префикс без подтверждения. Раньше `_archive` глотал свою
    ошибку и возвращал управление как ни в чём не бывало, а мы БЕЗУСЛОВНО
    переписывали файл кольцом — недоступный том или битый каталог означал, что её
    собственные ходы исчезали отовсюду. Теперь при неуспехе файл просто растёт до
    следующей попытки: лишние строки дешевле утраченной жизни."""
    global _FILE_LINES
    try:
        with PATH.open(encoding="utf-8") as fh:
            existing = [l for l in fh if l.strip()]
    except OSError:
        existing = []
    drop = max(0, len(existing) - len(ring))
    if drop:
        dropped = []
        for line in existing[:drop]:
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(row, dict) and row.get("ts"):
                dropped.append(row)
        if not _archive(dropped):
            global _ARCHIVE_FAILS
            _ARCHIVE_FAILS += 1
            _FILE_LINES = len(existing)
            held_bytes = sum(len(l.encode("utf-8", "replace")) for l in existing)
            if (len(existing) <= KEEP * RETAIN_MAX_RINGS
                    and held_bytes <= RETAIN_MAX_BYTES):
                return
            # Потолок. Удерживать непрожёванный префикс правильно, но не бесконечно:
            # без границы каждый ход перечитывал бы и перехэшировал растущий файл ПОД
            # ЛОКОМ на синхронном пути ответа (адверсарка: линейный рост, десятки МБ и
            # сотни мс на ход за пару недель). Уводим префикс в карантинный файл
            # ПЕРЕИМЕНОВАНИЕМ — ни одна строка не теряется, никакой пересборки, — и
            # оставляем громкий след, чтобы карантин не стал тихой могилой.
            try:
                stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                quarantine = PATH.with_name(f"turns.quarantine-{stamp}.jsonl")
                quarantine.write_text("".join(existing[:drop]), encoding="utf-8")
                log.error("архив недоступен %s раз подряд: префикс из %s ходов (%.1f МБ) "
                          "уведён в %s — он НЕ потерян, но и не в архиве; нужен разбор",
                          _ARCHIVE_FAILS, drop, held_bytes / 1048576, quarantine.name)
            except OSError:
                log.warning("карантин префикса не записался", exc_info=True)
                return
        else:
            _ARCHIVE_FAILS = 0
    tmp = PATH.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in ring),
                   encoding="utf-8")
    tmp.replace(PATH)
    _FILE_LINES = len(ring)


def record(turn: dict) -> None:
    """Записать прожитый ход (RAM + jsonl, урезка на записи). НИКОГДА не роняет ход."""
    global _FILE_LINES
    try:
        # Отметки обрыва, оставшиеся в этом потоке незабранными, — часть того же хода.
        # Забрать их обязан кто-то: иначе они протухнут молча, и предел, который реально
        # сработал, не увидит никто.
        _attach_unclaimed_cuts(turn)
        t = _clip(turn)
        with _LOCK:
            ring = _load_ring()
            ring.append(t)
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(t, ensure_ascii=False) + "\n")
            _FILE_LINES += 1
            if _FILE_LINES > KEEP * 2:
                _compact_file(ring)
    except Exception:
        log.debug("turns.record не записался", exc_info=True)


# Приёмка Telegram — ФАКТ, а не мнение последнего писавшего. Исход доставки может
# только расти по этой решётке, и никогда не откатываться назад.
#
# ⚠ Без этого (адверсарка 25.07, воспроизведено): она отвечает текстом с вложением,
# весь текст Telegram принимает, Егор его читает — а одна картинка уходит в очередь на
# повтор. Раннер зовёт run_delivery_blocked, и ход перештамповывается в «не сказала:
# доставка застряла». Хуже: чистильщик медиа каждые 60 секунд проходит по последним
# ста просроченным записям и переклеивает этот ярлык на давно завершённые ходы. То
# есть новая правда доставки превращалась ровно в прежнюю ложь, только наизнанку.
_DELIVERY_RANK = {"": 0, "authored": 0, "blocked": 1, "superseded": 1,
                  "failed": 1, "partial": 2, "accepted": 3}


def _delivery_upgrade(current, want: str) -> bool:
    """Можно ли записать исход `want` поверх `current`? Только вверх по решётке."""
    have = str(current or "")
    if have == want:
        return False
    return _DELIVERY_RANK.get(want, 0) >= _DELIVERY_RANK.get(have, 0)


def update_delivery(run_id: str, delivery: str, *, out: str | None = None) -> dict | None:
    """Проставить прожитому ходу ФАКТИЧЕСКИЙ исход доставки.

    Возвращает запись ТОЛЬКО если исход действительно изменился, и None иначе. Это
    контракт идемпотентности для вызывающего: побочные эффекты приёмки (заметка «сказала»,
    обещание) обязаны случиться ровно один раз — на переходе. Живой путь и путь
    восстановления оба доходят до «accepted», и без этого они написали бы их дважды.

    Контракт C1 (CONTRACTS.md): решает расписка транспорта, а не авторство. `record()`
    кладёт ход как `authored` — «написала». Сюда приходит то, что случилось на самом
    деле: accepted / partial / superseded / failed / blocked.

    Перезаписываем файл ПОСТРОЧНО, сохраняя все строки. Соблазн переписать файл из
    кольца (как делает _compact_file) здесь смертелен: файл держит до 2×KEEP строк, и
    такой «апдейт» молча снёс бы хвост, который ещё не уехал в архив, — ровно та
    потеря, ради которой этот пасс и затевался.
    """
    rid = str(run_id or "").strip()
    if not rid:
        return None
    want = str(delivery or "")[:24]
    row: dict | None = None
    with _LOCK:
        for t in reversed(_load_ring()):          # свежие сначала: ход обычно последний
            if str(t.get("run_id") or "") == rid:
                if not _delivery_upgrade(t.get("delivery"), want):
                    return None      # ничего не изменилось — см. контракт возврата
                t["delivery"] = want
                if out is not None:
                    t["out"] = _clip_out(out)
                row = t
                break
        try:
            lines = PATH.read_text(encoding="utf-8").splitlines() if PATH.exists() else []
        except OSError:
            lines = []
        touched = False
        for i in range(len(lines) - 1, -1, -1):
            try:
                disk = json.loads(lines[i])
            except (TypeError, ValueError):
                continue
            if not isinstance(disk, dict) or str(disk.get("run_id") or "") != rid:
                continue
            if not _delivery_upgrade(disk.get("delivery"), want):
                return None
            disk["delivery"] = want
            if out is not None:
                disk["out"] = _clip_out(out)
            lines[i] = json.dumps(disk, ensure_ascii=False)
            row = row or disk
            touched = True
            break
        if touched:
            try:
                tmp = PATH.with_suffix(".jsonl.tmp")
                tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
                tmp.replace(PATH)
            except OSError:
                log.warning("исход доставки не записался на диск [%s]", rid, exc_info=True)
    if row is None:
        log.debug("исход доставки: ход для run_id=%s не найден", rid)
    return row


def _visible(t: dict, scope: str, chat_id) -> bool:
    """Тир приватности на чтении: owner видит всё; другой канал — только свои ходы.

    «Свои» считается по КОМНАТЕ, а не по строке ключа. Пока ключ расщеплялся на
    псевдо-темы, её собственные ходы в одной и той же комнате были невидимы друг для
    друга: проснувшись в ветке A, она не помнила, что отвечала в ветке B того же чата
    десять минут назад. Псевдонимы включаются только там, где Telegram прямо ответил,
    что комната не форум; в настоящем форуме темы остаются разными местами.
    """
    if scope == "owner":
        return True
    return same_place(t, chat_id)


def same_place(t: dict, chat_id) -> bool:
    """Принадлежит ли ход ЭТОМУ месту — с учётом веток одной комнаты.

    Вынесено из `_visible` 03.08.2026, потому что там это условие отвечало на другой
    вопрос. См. `in_room` ниже: тир приватности и фильтр места — разные вещи, и одно
    имя `chat_id` на оба стоило нам подписи «Место: X» над чужой лентой.
    """
    cid = t.get("chat_id")
    if cid is None or chat_id is None:
        return False
    if str(cid) == str(chat_id):
        return True
    try:
        import telegram_routes
        return telegram_routes.same_room(cid, chat_id)
    except Exception:
        return False


def recent(n: int = 6, scope: str = "owner", chat_id=None) -> list[dict]:
    """Последние n видимых ходов, старые → новые. Только внутренние слои и scope-честный тул."""
    with _LOCK:
        rows = [t for t in _load_ring() if _visible(t, scope, chat_id)]
    return rows[-max(0, n):]


def in_room(n: int = 6, chat_id=None) -> list[dict]:
    """Ходы ИМЕННО в этом месте, старые → новые.

    ⚠ Не путать с `recent(chat_id=…)`. Там `chat_id` — ТИР ПРИВАТНОСТИ, ответ на вопрос
    «что мне позволено видеть»: при `scope="owner"` он игнорируется вовсе, потому что
    владельцу видно всё. Здесь `chat_id` — ФИЛЬТР МЕСТА, ответ на вопрос «что было вот
    здесь». Одно имя на два разных понятия уже дало нам подпись «Место: X» над лентой,
    в которой лежали ходы из других мест и её собственные окна (03.08.2026, поймано
    живой пробой, а не тестом).

    Гейт раскрытия здесь НЕ живёт: он стоит выше, у вызывающего
    (`agent._may_name_other_rooms`). Эта функция только фильтрует.
    """
    with _LOCK:
        rows = [t for t in _load_ring() if same_place(t, chat_id)]
    return rows[-max(0, n):]


def describe_room(n: int = 6, chat_id=None) -> str:
    """Текст про КОНКРЕТНОЕ место для её руки `recent_turns(room=…)`."""
    try:
        n = max(1, min(int(n or 6), 20))
    except (TypeError, ValueError):
        n = 6
    rows = in_room(n=n, chat_id=chat_id)
    if not rows:
        return "Ни одного записанного хода в этом месте."
    return ("Мои прожитые ходы здесь (лог кодом, не по памяти; новые снизу):" + chr(10)
            + chr(10).join("- " + format_line(t) for t in rows))


_SEND_MARKERS = ("send_message(", "narrate(", "send_file(", "Отправила")


def _no_addressee(t: dict) -> bool:
    """У хода нет канала доставки: его текст — запись себе, а не реплика наружу.

    Выводится НА ЧТЕНИИ, а не пишется в запись. Так честными становятся и месяцы уже
    накопленных строк: кольцо иначе заговорило бы двумя голосами — старые ходы того же
    вида по-прежнему «сказала», новые «записала себе», в одном блоке перед её глазами.
    Данные при этом не переписываются: это интерпретация, а не правка прошлого.
    """

    if t.get("held"):
        return False
    return (str(t.get("kind") or "chat") in ("forge_event", "task_window", "heartbeat", "wake")
            and t.get("chat_id") is None)


# Глагол по факту доставки. Пустая строка/отсутствие поля — легаси-запись, которую
# читаем по-старому. «authored» — черновик, который ещё не встретился с транспортом:
# честнее всего сказать, что он написан, а не произнесён.
_DELIVERY_VERB = {
    "authored": "написала (доставка не подтверждена)",
    "accepted": "сказала",
    "partial": "сказала частично (принят не весь текст)",
    "superseded": "не сказала: перебила себя и переписала ход",
    "failed": "не сказала: доставка не удалась",
    "blocked": "не сказала: доставка застряла, ждёт повтора",
}


def _trace_sent_out(t: dict) -> bool:
    """Ушло ли что-то наружу тулом внутри хода — по следу, каким он сохранился."""

    return any(any(m in str(line) for m in _SEND_MARKERS) for line in (t.get("tools") or []))


def format_line(t: dict) -> str:
    """Одна строка записи для блоков/тула: время [где] вошло | делала | итог."""
    try:
        when = _dt.datetime.fromtimestamp(float(t.get("ts") or 0)).strftime("%d.%m %H:%M")
    except (OSError, OverflowError, ValueError):
        when = "?"
    kind = t.get("kind") or "chat"
    if kind == "heartbeat":
        where = "тихий час, наедине с собой"
    elif kind == "forge_event":
        # ⚠ «(Telegram жив)» стояло здесь безусловно — третий контур с той же ложью, что
        # уже вычищали из окна и из пробуждения. Праксис назвала её сама; я починил рамку,
        # которую она читает В МОМЕНТ хода, и забыл строку, которую она читает ПОТОМ. Так
        # она узнавала правду сейчас и неправду через час — то есть ровно то же самое.
        state = str(t.get("telegram") or "")
        where = ("разбужена событием своего субагента (Telegram жив)" if state == "connected"
                 else f"разбужена событием своего субагента (связи не было: {state})"
                 if state else "разбужена событием своего субагента")
    elif kind == "wake":
        # Её собственный будильник. Разница с окном ровно в связи — и связь берётся ИЗ
        # ЗАПИСИ, снятой в момент подъёма. Утверждать её задним числом нельзя: ровно так
        # («Telegram жив» безусловно) врал этот журнал про окна, пока не поймали.
        state = str(t.get("telegram") or "")
        where = ("своё пробуждение по будильнику (Telegram открыт)" if state == "connected"
                 else f"своё пробуждение по будильнику (связи не было: {state})" if state
                 else "своё пробуждение по будильнику")
    elif kind == "task_window":
        # ⚠ Здесь стояло «(Telegram жив)», и это было неправдой безусловно:
        # вызывающий — mtproto_runner._task_window — на время окна НАМЕРЕННО отключал
        # Telethon. Её собственный журнал говорил ей, что связь была, когда связи не было.
        # 26.07 у окна появился режим БЕЗ разрыва (часовой пульс), и «закрыт» стало такой
        # же безусловной неправдой с обратным знаком. Поэтому связь читается ИЗ ЗАПИСИ,
        # снятой в момент хода; пусто — легаси-строка, читаем по-старому.
        state = str(t.get("telegram") or "")
        where = ("свой фоновый run (связь была открыта)" if state == "connected"
                 else f"свой фоновый run (связи не было: {state})"
                 if state and state != "closed_for_window"
                 else "свой фоновый run (Telegram закрыт на время окна)")
    else:
        place = t.get("title") or t.get("who") or t.get("chat_id") or "?"
        where = f"{place} ({t.get('scope') or '?'})"
    parts = [f"{when} [{where}]"]
    if t.get("in"):
        parts.append(f"вошло: «{t['in']}»")
    if t.get("tools"):
        parts.append("делала: " + "; ".join(t["tools"]))
    held, why = t.get("held") or "", t.get("why") or ""
    if held == "voice":
        parts.append("промолчала сама" + (f" ({why})" if why else ""))
    elif held == "privacy":
        parts.append(f"доставка удержана по полномочию данных ({why or 'без причины'})")
    elif held == "evaluator":
        parts.append(f"legacy-оценщик придержал ({why or 'без причины'})")
    elif held == "anti-repeat":
        parts.append("промолчала (анти-повтор: чуть не повторилась)")
    elif held == "drift":
        parts.append("оборвано: комната заморожена дрейфом")
    elif held == "error":
        parts.append("ход упал с ошибкой")
    elif held == "empty":
        parts.append("ответ не родился (пусто)")
    elif _no_addressee(t):
        # У хода нет канала доставки (chat_id=None): его ТЕКСТ — запись себе, а не
        # реплика наружу. Раньше он падал в ветку «сказала», и журнал утверждал, что
        # она сказала то, чего никто не слышал.
        # Утверждение строго об этом поле: отправки тулом внутри хода бывают (её
        # forge-фрейм прямо предлагает narrate), и объявлять весь ход немым — та же
        # ложь наизнанку. Поэтому про «наружу» говорим только когда след это
        # подтверждает, а иначе отсылаем к следу, ничего не обещая.
        note = str(t.get("out") or "")
        if len(note) > OUT_SHOW_CHARS:
            note = note[: OUT_SHOW_CHARS - 1] + "…[обрезано для показа; полный текст в записи хода]"
        if not note:
            parts.append("ничего не ушло наружу")
        elif _trace_sent_out(t):
            parts.append(f"записала себе (это текст хода, не сообщение; что ушло — "
                         f"см. «делала»): «{note}»")
        else:
            parts.append(f"записала себе (у хода нет канала доставки; отправок в "
                         f"следе нет): «{note}»")
    elif t.get("out"):
        said = str(t["out"])
        if len(said) > OUT_SHOW_CHARS:
            said = said[: OUT_SHOW_CHARS - 1] + "…[обрезано для показа; полный текст в записи хода]"
        # Контракт C1 на ЧТЕНИИ. Здесь безусловно стояло «сказала» — то есть строка,
        # которую она перечитывает в промпте и в дневнике, утверждала речь по факту
        # наличия черновика. Теперь глагол берётся из исхода доставки. Старые записи
        # (без поля) читаются как раньше: переписывать прошлое задним числом нельзя,
        # но и новую ложь копить незачем.
        line = f"{_DELIVERY_VERB.get(str(t.get('delivery') or ''), 'сказала')}: «{said}»"
        if t.get("rewrote"):
            line += f" (правлено оценщиком: {why or t.get('verdict') or '?'})"
        parts.append(line)
    else:
        parts.append("ничего не ушло наружу")
    # Чужой обрыв — отдельной частью строки, ПОСЛЕ её итога и с названным владельцем.
    # Раньше он приезжал в `why` вплотную к её ответу, и прочесть это можно было только
    # как «её фразу оборвало».
    parts.extend(str(line) for line in (t.get("cut") or [])[:CUT_MAX])
    return " | ".join(parts)


def tail_block(n: int = 3, scope: str = "owner", chat_id=None, budget: int = 900) -> str:
    """Свежие ходы строками под бюджет — для промптов мета-слоёв (без заголовка,
    заголовок даёт потребитель). Строки режутся поровну, как _clip_tool_trace."""
    rows = recent(n=n, scope=scope, chat_id=chat_id)
    if not rows:
        return ""
    lines = ["- " + format_line(t) for t in rows]
    per = max(80, budget // len(lines))
    clipped = [l if len(l) <= per else l[: per - 1] + "…" for l in lines]
    return "\n".join(clipped)[:budget]


def state_line(boot_ts: float | None = None) -> str:
    """Одна строка для STATE (owner tier-0): самое свежее прожитое — факт, не припоминание."""
    with _LOCK:
        ring = _load_ring()
        last = ring[-1] if ring else None
    # Обрывы фоновых ролей (сон, консолидация) не принадлежат ни одному её ходу, и их
    # некому забрать. Единственный канал tier-0, которым владеет этот модуль, — вот эта
    # строка; факт едет ОТДЕЛЬНОЙ строкой, чтобы не выдать себя за прожитый ход.
    # (Правильнее дать ему собственную метку в STATE — это уже на стороне agent.py.)
    cut = unclaimed_line()
    if not last:
        return cut
    pre = "(до рестарта) " if boot_ts and float(last.get("ts") or 0) < boot_ts else ""
    line = f"последний прожитый ход {pre}— {format_line(last)}"
    return f"{line}\n{cut}" if cut else line


def chat_catchup_line(chat_id, boot_ts: float) -> str:
    """Catch-up после рестарта: последний ход в ЭТОМ чате из прошлой жизни процесса.
    Ответ на «что я делала перед сном» — фактом, не припоминанием модели."""
    if chat_id is None:
        return ""
    with _LOCK:
        rows = [t for t in _load_ring()
                if str(t.get("chat_id")) == str(chat_id) and float(t.get("ts") or 0) < boot_ts]
    return format_line(rows[-1]) if rows else ""


def day_line(now: float | None = None, hours: float = 24.0) -> str:
    """Сводка прожитых суток КОДОМ — сну для дневника (меньше конфабуляций в её памяти)."""
    cut = (now if now is not None else time.time()) - hours * 3600.0
    with _LOCK:
        rows = [t for t in _load_ring() if float(t.get("ts") or 0) >= cut]
    if not rows:
        return ""
    # «Сказала» = ушло наружу. Ходы без канала доставки (forge_event / своё окно /
    # тихий час) сюда не попадают: раньше попадали, и суточная сводка — та, что
    # sleep.py каждую ночь пишет в дневник, — ежедневно завышала сказанное её же
    # внутренним текстом. Счётчик записей себе требует НЕПУСТОЙ текст, иначе он
    # завышал бы ровно так же: большинство таких пробуждений текста не рождает.
    # ⚠ Счётчик считал «сказала» по НАЛИЧИЮ текста — то есть по авторству, ровно как
    # старый format_line. Получилось бы два голоса об одном дне: построчно «не сказала»,
    # а в ночном дневнике, который и уезжает в долгую память, — «сказала». Легаси-строки
    # без поля delivery по-прежнему считаются сказанными, иначе в ночь выката из счётчика
    # молча выпал бы весь накопленный день.
    def _spoken(t: dict) -> bool:
        if not t.get("out") or t.get("held") or _no_addressee(t):
            return False
        d = str(t.get("delivery") or "")
        return d in ("", "accepted", "partial")

    said = sum(1 for t in rows if _spoken(t))
    undelivered = sum(1 for t in rows
                      if t.get("out") and not t.get("held") and not _no_addressee(t)
                      and str(t.get("delivery") or "") in
                      ("authored", "superseded", "failed", "blocked"))
    self_notes = sum(1 for t in rows if _no_addressee(t) and t.get("out"))
    quiet = sum(1 for t in rows if t.get("held") == "voice")
    held_privacy = sum(1 for t in rows if t.get("held") in ("privacy", "evaluator"))
    held_other = sum(1 for t in rows if t.get("held") in ("anti-repeat", "drift"))
    errors = sum(1 for t in rows if t.get("held") in ("error", "empty"))
    rewrites = sum(1 for t in rows if t.get("rewrote"))  # legacy observability only
    tools_n = sum(len(t.get("tools") or []) for t in rows)
    # «своих окон» здесь было неточно и до 26.07 (сюда же падали heartbeat и forge_event),
    # а с появлением вида wake стало прямой неправдой: пробуждение — не окно, связь в нём
    # не рвётся. Считаем то, что действительно считается: ходы без собеседника.
    alone = sum(1 for t in rows if (t.get("kind") or "chat") != "chat")
    line = (f"ходов {len(rows)} (своих, без собеседника {alone}): сказала наружу {said}, "
            f"записала себе {self_notes}, "
            f"промолчала сама {quiet}, удержано по data-authority {held_privacy}, "
            f"legacy-удержаний {held_other}, legacy-правок {rewrites}, тул-вызовов {tools_n}")
    if undelivered:
        line += f", не дошло {undelivered}"
    if errors:
        line += f", ходов с ошибкой {errors}"
    return line


def describe(n: int = 6, scope: str = "owner", chat_id=None) -> str:
    """Текст для её тула recent_turns — scope-честно (вне owner виден только этот канал)."""
    try:
        n = max(1, min(int(n or 6), 20))
    except (TypeError, ValueError):
        n = 6
    rows = recent(n=n, scope=scope, chat_id=chat_id)
    if not rows:
        return ("Пока ни одного записанного хода"
                + ("" if scope == "owner" else " в этом канале") + ".")
    head = ("Мои последние прожитые ходы (лог кодом, не по памяти; новые снизу"
            + ("" if scope == "owner" else "; вне лички Егора виден только этот канал") + "):")
    return head + "\n" + "\n".join("- " + format_line(t) for t in rows)


def _reset() -> None:
    """Только для тестов: сбросить RAM-кольцо (файл трогает сам тест через PATH)."""
    global _RING, _FILE_LINES
    with _LOCK:
        _RING, _FILE_LINES = None, 0
        # Тот же класс готчи, что и кольцо: процесс-глобал, переживающий тест.
        # Без сброса второй тест видел бы «уже заархивировано» от первого.
        _ARCHIVED_IDS.clear()
    with _TRUNC_LOCK:
        # Слоты обрыва — такой же процесс-глобал; без сброса отметка одного теста
        # приезжала бы в запись хода следующего (ровно та болезнь, что чинится выше).
        _TRUNCATED.clear()
        _UNCLAIMED.clear()
