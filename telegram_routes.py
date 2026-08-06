"""
Реестр маршрутов: что мы ЗНАЕМ о природе комнаты и с каких пор.

Пункт 5, теневая половина. Ничего не маршрутизирует и никем не читается на
исполнении — только копит свидетельства, чтобы перекладка ключа однажды делалась по
знанию, а не по догадке.

## Зачем эпохи, а не один флаг

Обычная супергруппа может быть превращена в форум в любой момент. В истории Telegram
**нет служебного сообщения об этом переключении** (в семействе MessageAction есть
TopicCreate и TopicEdit, но нет ToggleForum; факт виден только в admin-log, который
доступен админу и хранится ограниченное время). Значит момент превращения из истории
невосстановим.

Отсюда единственный честный вывод: всё, что было ДО первого наблюдения, — `unknown`, и
оно обязано таким остаться. Сегодняшнее состояние чата нельзя задним числом
распространять на всю его историю: ключи, выданные полгода назад, минтились в другом
режиме. Поэтому реестр хранит не флаг, а ленту эпох с границей по message_id —
идентификаторы сообщений внутри одного peer монотонны, и этого достаточно, чтобы
спросить «а что было в силе, когда минтился вот этот ключ».

## Почему свидетельства ранжированы

`GetForumTopics` либо отвечает списком тем (это форум), либо падает с
`CHANNEL_FORUM_MISSING` (это не форум) — оба ответа authoritative. А вот `Channel.forum`
из объекта, пришедшего в апдейте с флагом `min`, документирован как ненадёжный: такой
объект не имеет права перебивать прямой ответ Telegram. Поэтому слабое свидетельство
никогда не понижает сильное.

Модуль намеренно без зависимостей от Telethon: факты в него ПЕРЕДАЮТ, как и в
`telegram_topics`. Так его можно читать из `agent`, где Telethon нет вовсе.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger("praxis-routes")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
DIR = BASE / "memory" / ".state" / "group_context"

SCHEMA = "praxis.group.route.v1"
KEEP_EPOCHS = 32
KEEP_BRANCHES = 1024

TRUE, FALSE, UNKNOWN = "true", "false", "unknown"

# Разделитель ключа разговора. Дублируется из telegram_topics сознательно: этот модуль
# намеренно без зависимостей, чтобы его мог читать agent, где Telethon нет вовсе.
_SEP = "__topic__"

# Свидетельство -> (вердикт, вес). Вес решает, кто кого имеет право переписать.
EVIDENCE = {
    "get_forum_topics_ok":   (TRUE, 3),    # Telegram отдал список тем
    "channel_forum_missing": (FALSE, 3),   # CHANNEL_FORUM_MISSING на тот же запрос
    "topic_opener_seen":     (TRUE, 3),    # MessageActionTopicCreate в живой ленте
    "entity_forum_flag":     (None, 2),    # гидратированный Channel.forum
    "legacy_chat":           (FALSE, 2),   # старая базовая группа форумом не бывает
    # Свидетельство об ИСТОРИИ, а не о «сейчас»: прошли диапазон сообщений и не нашли
    # ни одного служебного «создана тема». У настоящего форума они есть — так и был
    # опознан форум Грибницы (18 openers) против AbstractDL (ноль на 4138 сообщений).
    # Слабее прямого ответа Telegram, потому что вывод, а не ответ; но именно оно
    # покрывает прошлое, о котором прямой запрос ничего сказать не может.
    "no_topic_openers_in_range": (FALSE, 2),
    "update_min_entity":     (None, 1),    # тот же флаг, но объект пришёл min=True
    "rpc_unavailable":       (None, 0),    # сеть/флуд — не говорит НИЧЕГО
}

_LOCK = threading.Lock()


def _slug(peer_id) -> str:
    """Тот же вид имени, что у соседних per-peer receipts группы."""
    raw = str(peer_id or "unknown")
    return re.sub(r"[^0-9A-Za-z_-]+", "_", raw)[:64] or "unknown"


def _path(peer_id) -> Path:
    return DIR / f"{_slug(peer_id)}.route.json"


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def read(peer_id) -> dict:
    """Лента эпох комнаты. Пустая — значит мы про неё ещё ничего не знаем."""
    try:
        data = json.loads(_path(peer_id).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {"schema": SCHEMA, "peer_id": str(peer_id), "epochs": []}
    if not isinstance(data, dict) or not isinstance(data.get("epochs"), list):
        return {"schema": SCHEMA, "peer_id": str(peer_id), "epochs": []}
    return data


def _save(peer_id, data: dict) -> None:
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        path = _path(peer_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        log.debug("реестр маршрутов не записался [%s]", peer_id, exc_info=True)


def _mid(value):
    return int(value) if str(value or "").lstrip("-").isdigit() else None


def observe(peer_id, *, kind: str, forum: bool | None = None,
            message_id=None, since_message_id=None, until_message_id=None,
            detail: str = "") -> dict:
    """Записать одно свидетельство о природе комнаты. -> текущая эпоха.

    Диапазон — это то, ЗА КАКИЕ сообщения свидетельство отвечает. Одиночный
    `message_id` = диапазон из одного. Наблюдение на более раннем сообщении РАСШИРЯЕТ
    эпоху назад: мы узнали, что режим действовал уже тогда.

    ⚠ Раньше `since_message_id` ставился первым попавшимся наблюдением и никогда не
    опускался. Из-за этого ответ зависел от порядка загрузки: если первым приходило
    живое сообщение с номером 93900, все 279 исторических веток комнаты оказывались
    РАНЬШЕ границы и получали «не знаю» — то есть слой B не включался никогда.
    Воспроизведено: одни и те же свидетельства в разном порядке давали разный ответ.

    Новая эпоха заводится только когда вердикт меняется и новое свидетельство не
    слабее держащего текущую: `min`-объект из апдейта не отменяет прямой ответ Telegram.
    """
    verdict, weight = EVIDENCE.get(str(kind), (None, 0))
    if verdict is None:
        verdict = TRUE if forum is True else FALSE if forum is False else UNKNOWN

    lo = _mid(since_message_id if since_message_id is not None else message_id)
    hi = _mid(until_message_id if until_message_id is not None else message_id)
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo

    def _widen(epoch: dict) -> None:
        cur_lo, cur_hi = epoch.get("since_message_id"), epoch.get("until_message_id")
        if lo is not None:
            epoch["since_message_id"] = lo if cur_lo is None else min(int(cur_lo), lo)
        if hi is not None:
            epoch["until_message_id"] = hi if cur_hi is None else max(int(cur_hi), hi)

    with _LOCK:
        data = read(peer_id)
        epochs = data["epochs"]
        current = epochs[-1] if epochs else None

        if current is not None and current.get("forum_status") == verdict:
            current["last_seen_at"] = _now_iso()
            current["observations"] = int(current.get("observations") or 0) + 1
            if weight > int(current.get("weight") or 0):
                current["weight"] = weight
                current["evidence"] = str(kind)
            _widen(current)
            _save(peer_id, data)
            return dict(current)

        if current is not None and weight < int(current.get("weight") or 0):
            # Слабое свидетельство не отменяет сильное — только отмечает, что смотрели.
            current["last_seen_at"] = _now_iso()
            current["contested"] = int(current.get("contested") or 0) + 1
            _save(peer_id, data)
            return dict(current)

        epoch = {
            "epoch": (int(current.get("epoch") or 0) + 1) if current else 1,
            "forum_status": verdict,
            "evidence": str(kind),
            "weight": weight,
            "detail": str(detail or "")[:200],
            "since_message_id": lo,
            "until_message_id": hi,
            "since": _now_iso(),
            "last_seen_at": _now_iso(),
            "observations": 1,
        }
        epochs.append(epoch)
        del epochs[:-KEEP_EPOCHS]
        data["schema"] = SCHEMA
        data["peer_id"] = str(peer_id)
        _save(peer_id, data)
        log.info("реестр маршрутов [%s]: эпоха %s — forum=%s по свидетельству %s "
                 "(диапазон %s..%s)", peer_id, epoch["epoch"], verdict, kind, lo, hi)
        return dict(epoch)


def note_writing(peer_id, *, broadcast=None, linked_chat_id=None,
                 linked_title: str = "") -> dict:
    """Записать, что Telegram ответил про ПИСЬМО в это место. -> текущая запись.

    ⚠ Живёт РЯДОМ с эпохами форума, а не внутри них — намеренно. Эпохи отвечают на
    вопрос «форум ли это», со своей шкалой весов и своей историей смен. «Могу ли я сюда
    писать» — другой вопрос, и втискивать его в ту же машину значило бы снова назвать
    одним именем два разных понятия (03.08.2026 мы поймали это дважды за вечер: единицу
    окна записки и `chat_id`, который был и тиром приватности, и фильтром места).

    Пустое значение НЕ затирает известное: свидетельство приходит откуда придётся, и
    «я не посмотрел» не должно выглядеть как «я посмотрел и там пусто».
    """
    with _LOCK:
        data = read(peer_id)
        rec = data.get("writing")
        if not isinstance(rec, dict):
            rec = {}
        if broadcast is not None:
            rec["broadcast"] = bool(broadcast)
        if linked_chat_id is not None:
            rec["linked_chat_id"] = str(linked_chat_id)
        if linked_title:
            rec["linked_title"] = str(linked_title)[:120]
        rec["last_seen_at"] = _now_iso()
        data["writing"] = rec
        data["peer_id"] = str(peer_id)
        _save(peer_id, data)
        return dict(rec)


def writing_of(peer_id) -> dict:
    """Что мы знаем про письмо в это место. Пусто — не знаем ничего."""
    rec = read(peer_id).get("writing")
    return dict(rec) if isinstance(rec, dict) else {}


def writing_line(peer_id) -> str:
    """Фраза для её ориентации. Пусто — сказать нечего, и молчание тут честно.

    Форма ЕЁ условия (28.07): адрес виден ДО выбора, квитанция — после. Поэтому здесь
    только адрес и природа места; никакой переадресации за неё не делается. Ответить или
    промолчать — её ход, а не наш.
    """
    rec = writing_of(peer_id)
    if not rec.get("broadcast"):
        return ""
    linked, title = rec.get("linked_chat_id"), rec.get("linked_title")
    if linked:
        where = (str(title) + " [" + str(linked) + "]") if title else str(linked)
        return ("Это ВЕЩАТЕЛЬНЫЙ КАНАЛ: читать я тут могу, писать в него — нет. Ответы на "
                "его посты живут в связанном обсуждении: " + where + ". Хочешь ответить — "
                "адресуй туда явно; отправка в сам канал вернётся отказом, и человек "
                "ответа не увидит.")
    return ("Это ВЕЩАТЕЛЬНЫЙ КАНАЛ: читать я тут могу, писать в него — нет. Какое "
            "обсуждение с ним связано, я пока не знаю — значит и куда уйдёт ответ, "
            "сказать не могу. Спроси у Егора адрес, а не отправляй наугад.")


def current(peer_id) -> dict:
    """Что мы знаем о комнате сейчас."""
    epochs = read(peer_id)["epochs"]
    return dict(epochs[-1]) if epochs else {"forum_status": UNKNOWN, "epoch": 0}


def status_at(peer_id, message_id=None) -> tuple[str, int]:
    """Режим, действовавший когда минтился ключ этого сообщения. -> (статус, эпоха).

    Правила ровно три, и все три — про честность:

    1. Сообщение попало в известный диапазон эпохи — отвечаем её режимом.
    2. Сообщение новее всего, что мы наблюдали, — отвечаем последним режимом: режимы
       держатся, пока не сменятся, а смену мы бы увидели.
    3. Всё остальное — `unknown`. Это и сообщения старше первого наблюдения (момент
       превращения чата в форум из истории Telegram невосстановим), и дыры между
       эпохами (смена случилась где-то там, но где именно — мы не знаем).

    ⚠ Раньше пункт 3 отсутствовал: отсутствие границы трактовалось как «действует на
    всё», и наблюдение без диапазона, записанное последним, переклеивало ярлык на всю
    историю — включая стирание настоящей эпохи форума.
    """
    epochs = read(peer_id)["epochs"]
    if not epochs:
        return UNKNOWN, 0
    mid = _mid(message_id)
    if mid is None:
        return str(epochs[-1]["forum_status"]), int(epochs[-1]["epoch"])

    for epoch in reversed(epochs):
        lo, hi = epoch.get("since_message_id"), epoch.get("until_message_id")
        if lo is None or hi is None:
            continue
        if int(lo) <= mid <= int(hi):
            return str(epoch["forum_status"]), int(epoch["epoch"])

    known_hi = [int(e["until_message_id"]) for e in epochs
                if e.get("until_message_id") is not None]
    if known_hi and mid > max(known_hi):
        last = epochs[-1]
        return str(last["forum_status"]), int(last["epoch"])
    return UNKNOWN, 0


def observe_branches(peer_id, mapping: dict) -> int:
    """Записать, какие ветки на самом деле не места, а наши артефакты.

    `mapping` — {ветка: место}, где место это id настоящей темы или None для General.
    Считает его тот, у кого есть архив (`group_context.branch_containers`): здесь
    зависимостей нет и быть не должно, сюда факты ПЕРЕДАЮТ.

    Записываются только артефакты. Настоящая тема форума — место сама по себе, и
    отсутствие записи о ней и значит «место».
    """
    if not isinstance(mapping, dict) or not mapping:
        return 0
    with _LOCK:
        data = read(peer_id)
        branches = data.get("branches")
        if not isinstance(branches, dict):
            branches = {}
        for branch, container in mapping.items():
            key = str(_mid(branch))
            if key == "None":
                continue
            branches[key] = None if container is None else int(container)
        # Кап — защита файла от разрастания, не политика. Режем самые старые ветки:
        # у них меньше шансов оказаться живым разговором.
        if len(branches) > KEEP_BRANCHES:
            for old in sorted(branches, key=lambda k: int(k))[:len(branches) - KEEP_BRANCHES]:
                branches.pop(old, None)
        data["branches"] = branches
        data.setdefault("schema", SCHEMA)
        data.setdefault("peer_id", str(peer_id))
        data.setdefault("epochs", [])
        _save(peer_id, data)
    return len(mapping)


def room_of(conversation_id) -> str:
    """Корневая комната ключа разговора: '<peer>__topic__N' -> '<peer>'."""
    return str(conversation_id or "").split(_SEP, 1)[0]


def topic_of(conversation_id):
    """Номер ветки в ключе или None."""
    raw = str(conversation_id or "")
    if _SEP not in raw:
        return None
    tail = raw.split(_SEP, 1)[1]
    return int(tail) if tail.lstrip("-").isdigit() else None


def place_of(conversation_id) -> str:
    """Какому МЕСТУ принадлежит этот ключ разговора. Одно определение на все органы.

    Место — это то, что разделил Telegram. Всё остальное разделили мы сами.

    Telegram делит ровно одним способом — темами форума. Значит:

      * комната не форум          -> место одно на всю комнату;
      * форум, ветка = тема       -> место сама тема;
      * форум, ветка — наш артефакт (доказано `observe_branches`)
                                  -> место то, где лежит корень: General или та тема;
      * ничего не доказано        -> место = сам ключ, то есть прежнее поведение.

    O(1) на чтении: реестр уже посчитан, здесь только просмотр. При любой ошибке
    вырождается в сам ключ — не знаем значит как раньше, а не как удобнее.
    """
    key = str(conversation_id or "")
    if not key:
        return ""
    room = room_of(key)
    topic = topic_of(key)
    if topic is None:
        return room
    try:
        # Спрашиваем вердикт для эпохи, в которую ключ минтился, а не для «сейчас».
        status, _epoch = status_at(room, topic)
        if status == FALSE:
            return room
        if status == TRUE:
            branches = read(room).get("branches")
            if isinstance(branches, dict) and str(topic) in branches:
                container = branches[str(topic)]
                return room if container is None else f"{room}{_SEP}{int(container)}"
    except Exception:
        log.debug("реестр маршрутов: место не определилось [%s]", key, exc_info=True)
    return key


def same_room(a, b) -> bool:
    """Один ли это разговор? Псевдонимы на ЧТЕНИИ (слой B пункта 5).

    Тонкая обёртка над `place_of`: два ключа — один разговор, когда за ними одно место.
    Имя оставлено прежним, потому что его читают полдюжины органов, а смысл у него был
    ровно этот с самого начала.
    """
    sa, sb = str(a or ""), str(b or "")
    if sa == sb:
        return True
    if not sa or not sb:
        return False
    if room_of(sa) != room_of(sb):
        return False
    return place_of(sa) == place_of(sb)


def artifacts_of(peer_id) -> frozenset:
    """Ветки комнаты, про которые ДОКАЗАНО, что их породили мы, а не Telegram.

    Нужны карте веток: она про комнату целиком, и каждая строка обязана называться по
    своей природе, а не по природе места, где она сейчас проснулась.
    """
    try:
        branches = read(room_of(peer_id) or str(peer_id)).get("branches")
        if isinstance(branches, dict):
            return frozenset(int(key) for key in branches if str(key).lstrip("-").isdigit())
    except Exception:
        log.debug("реестр маршрутов: состав артефактов не прочитан [%s]", peer_id, exc_info=True)
    return frozenset()


def reads_whole_room(peer_id, topic_id=None) -> bool:
    """Читать ли комнату целиком, просыпаясь в её ветке. Одно решение на всех читателей.

    Спрашивать реестр каждый читатель может и сам — но тогда их становится несколько, и
    расходятся они молча. Так уже было: снимок пробуждения спрашивал про `topic_id`
    вместо `peer_id` и только при непустой ветке, из-за чего слой B не включался вовсе,
    а тесты этого не видели, потому что проверяли реестр, а не читателя.

    Ошибку реестра тоже решаем здесь и одинаково: не знаем — ведём себя как раньше.
    """
    try:
        return status_at(peer_id, topic_id)[0] == FALSE
    except Exception:
        log.debug("реестр маршрутов не ответил [%s]", peer_id, exc_info=True)
        return False


class ReadScope(NamedTuple):
    """Как читать место, в котором она сейчас проснулась."""

    whole_room: bool          # комната не форум — место одно на всю комнату
    members: frozenset | None  # ветки, составляющие место (None для General)
    thread_word: str          # как называть ветки в этих строках
    place: str                # ключ места


def read_scope(peer_id, topic_id=None) -> ReadScope:
    """Границы одного места — для тех, кто читает архив.

    Три случая, и все три названы:

      * комната не форум — читаем комнату целиком, ветки зовём цепочками;
      * форум — читаем ОДНО место: настоящую тему или General вместе с её артефактами.
        Настоящие темы форума не смешиваются никогда: их разделил Telegram;
      * ничего не доказано — прежнее поведение, ветка как была.

    Замер 25.07: в General Грибницы 631 сообщение плюс 437 в 37 наших псевдоветках —
    больше половины комнаты, разложенной на 38 кусков внутри настоящего форума. То есть
    вердикт «это форум» сам по себе комнату не чинит.
    """
    room = room_of(peer_id) or str(peer_id or "")
    topic = _mid(topic_id)
    try:
        status, _epoch = status_at(room, topic)
    except Exception:
        log.debug("реестр маршрутов не ответил [%s]", peer_id, exc_info=True)
        return ReadScope(False, None, "topic", f"{room}{_SEP}{topic}" if topic else room)
    if status == FALSE:
        return ReadScope(True, None, "thread", room)
    if status != TRUE:
        return ReadScope(False, None, "topic",
                         f"{room}{_SEP}{topic}" if topic is not None else room)

    branches = read(room).get("branches")
    branches = branches if isinstance(branches, dict) else {}
    container = branches.get(str(topic)) if topic is not None else None
    # Место, к которому принадлежит эта ветка: сама тема, либо то, куда её отнесли.
    home = container if (topic is not None and str(topic) in branches) else topic
    members = {home}
    for branch, target in branches.items():
        if target == home:
            members.add(int(branch))
    place = f"{room}{_SEP}{home}" if home is not None else room
    # В General все ветки — наши; в настоящей теме форума ветка и есть тема.
    word = "thread" if home is None else "topic"
    return ReadScope(False, frozenset(members), word, place)


def orientation_line(peer_id, topic_id, selector: str, forum_status: str) -> str:
    """Как назвать ей эту ветку — по свидетельству, а не по умолчанию.

    Раньше промпт БЕЗУСЛОВНО сообщал «Telegram forum topic … isolated from every other
    topic in the same group». В обычной супергруппе ложна каждая часть: форума нет,
    «тема» — это одна цепочка ответов в той же комнате, а «изоляция» — артефакт
    расщеплённого ключа, а не Telegram.

    Идентификаторы одинаковы во всех трёх случаях: адресует она ветку одинаково, и
    менять словарь вместе с утверждением было бы отдельной поломкой. Меняется только
    то, чем мы эту ветку называем.

    Функция живёт здесь, а не в раннере, ровно чтобы её можно было проверить поведением,
    а не грепом по исходнику: греп ломался на переносе строки трижды подряд.
    """
    head = (f"peer_id={peer_id}, top_msg_id={topic_id}, selector={selector}. "
            "Use that selector with Telegram read/send tools when addressing this "
            "exact thread.")
    if forum_status == TRUE:
        # ⚠ Здесь безусловно утверждалась изоляция «от любой другой темы группы». В
        # General форума это ложь ровно там, где мы сами склеиваем: General и наши
        # псевдоветки — один разговор, и её лента, заметки, обещания и ходы уже сведены.
        # Спрашиваем состав МЕСТА, а не вердикт комнаты (адверсарка 25.07).
        scope = read_scope(peer_id, topic_id)
        if scope.members is not None and len(scope.members) > 1:
            if scope.place == room_of(peer_id) or topic_of(scope.place) is None:
                return (f"General of a Telegram forum: {head} General is ONE conversation: "
                        "this thread and the neighbouring reply chains of General are the "
                        "same place, and you are seeing part of it — read the rest with "
                        "group_context before concluding that anything was silent. The "
                        "group's real forum topics ARE separate places and are not merged "
                        "into it.")
            return (f"Telegram forum topic: {head} The topic is isolated from every other "
                    "topic in the same group; reply chains inside it are part of it, not "
                    "separate places.")
        return (f"Telegram forum topic: {head} This turn, its buffer and reply map "
                "are isolated from every other topic in the same group.")
    if forum_status == FALSE:
        return (f"Reply thread inside ONE ordinary room: {head} This is NOT a Telegram "
                "forum topic — the room is not a forum, and this thread is not a "
                "separate room. Your buffer and reply map are scoped to the thread, so "
                "you are seeing PART of a room: read the rest with group_context before "
                "concluding that anything was silent.")
    return (f"Thread of unverified kind: {head} Whether this room is a Telegram forum "
            "has not been observed yet, so treat this thread's isolation as "
            "unconfirmed — it may be one branch of a single ordinary room.")


def describe(peer_id) -> str:
    """Короткая человеческая строка — для панели и отчётов."""
    epochs = read(peer_id)["epochs"]
    if not epochs:
        return "о природе комнаты ничего не известно"
    last = epochs[-1]
    word = {TRUE: "форум", FALSE: "обычная супергруппа", UNKNOWN: "неизвестно"}.get(
        str(last.get("forum_status")), "неизвестно")
    return (f"{word} (эпоха {last.get('epoch')}, по свидетельству "
            f"{last.get('evidence')}, наблюдений {last.get('observations')})")
