"""PASS 30.0.b: обещание = задача (драйвер follow-through).

«Сейчас добью и пришлю» в чате — не воздух: такой исходящий ход рождает её же
намерение (tasks kind=window) с пробуждением через N минут, где она сама решает —
закрыла / довожу / переобещаю / снимаю. Это не новый диспетчер и не обязательство
извне: запись видна в my_agenda, снимается unschedule, детект целиком выключается
рычагом PRAXIS_PROMISE_WAKE=0 (и она вольна править сами паттерны — это её код).

Детект — дешёвые регэкспы по ЕЁ исходящему тексту. Ложное срабатывание стоит одного
лишнего окна («уже сделала, закрываю»); потерянное обещание стоит доверия — поэтому
recall важнее precision. Одна нить — один открытый promise (дедуп по target).
"""
from __future__ import annotations

import logging
import os
import re

import tasks

log = logging.getLogger("praxis-promises")

PROMISE_PREFIX = "[обещание]"

_ACT = (
    r"(?:сделаю|добью|доведу|проверю|посмотрю|разберу(?:сь)?|соберу|запущу|пришлю|"
    r"отправлю|принесу|напишу|допилю|доделаю|починю|исправлю|верну(?:сь)?|скину|"
    r"подниму|перепроверю|расскажу|покажу|выложу|перекину|переброшу|"
    # 23.07: живое НАСТОЯЩЕЕ время — «сейчас чиню», «сейчас правлю», «щас смотрю».
    # Диагностика простоя показала: «Сейчас чиню проверку полноты» (06:17) не была
    # распознана, окна-возврата не возникло, и после увода в другой разговор
    # объявленное действие не вернулось 5 часов.
    r"чиню|правлю|делаю|добиваю|довожу|проверяю|смотрю|разбираю(?:сь)?|собираю|"
    r"запускаю|пишу|допиливаю|доделываю|исправляю|поднимаю|перепроверяю|дописываю)"
)

_PROMISE_PATTERNS = (
    # «сейчас добью просмотр», «щас проверю и скажу», «теперь сделаю зеркало»
    # (промежуточный пасс A: «теперь …» — то же объявление следующего действия;
    # 17:37 23.07 «теперь обратно к audit-demo» ушло в простой без окна-возврата)
    re.compile(rf"(?is)\b(?:сейчас|щас|ща|теперь)\b[^.!?\n]{{0,60}}?\b{_ACT}"),
    # «добью сейчас», «пришлю сегодня»
    re.compile(rf"(?is)\b{_ACT}\b[^.!?\n]{{0,40}}?\b(?:сейчас|щас|ща|сегодня|теперь)\b"),
    # «вернусь с кадром через пару минут», «через 10 минут доложу»
    re.compile(rf"(?is)\b{_ACT}\b[^.!?\n]{{0,50}}?\bчерез\s+(?:пару|несколько|\d+)\s*мин"),
    re.compile(rf"(?is)\bчерез\s+(?:пару|несколько|\d+)\s*мин[^.!?\n]{{0,50}}?\b{_ACT}\b"),
    # промежуточный пасс A: срок числом — «принесу до 18:00», «сделаю к 19»
    # (14:34 23.07 «принесу до 18:00» не было распознано — и не принеслось)
    re.compile(rf"(?is)\b{_ACT}\b[^.!?\n]{{0,40}}?\b(?:до|к)\s+\d{{1,2}}(?:[:.]\d{{2}})?\b"),
    re.compile(rf"(?is)\b(?:до|к)\s+\d{{1,2}}(?:[:.]\d{{2}})?\b[^.!?\n]{{0,40}}?\b{_ACT}\b"),
    # «иду чинить», «берусь делать», «пошла разбираться»
    re.compile(r"(?is)\b(?:иду|пошла|беру(?:сь)?)\s+(?:делать|чинить|разбираться|"
               r"смотреть|проверять|собирать|копать)"),
    # промежуточный пасс A: «обратно к audit-demo», «возвращаюсь к пакету» —
    # НО не разговорное «возвращаюсь к твоему вопросу / к тебе / к теме / к сказанному»
    # (адверсарка round-1: голый паттерн ложно бил по обычной речи-филлеру). Стоп-стемы
    # поглощают хвост слова ([а-яё]*), иначе «сказанн» не покрыл бы «сказанному».
    re.compile(r"(?is)\b(?:обратно|возвращаюсь|вернусь)\s+к\s+"
               r"(?!(?:тебе|вам|нам|тво|ваш|наш|вопрос|тем|разговор|бесед|это|том|"
               r"нему|ней|нёй|неё|сказанн|предыдущ|сут|исходн|началу|начал)[а-яё]*\b)\S"),
    # «доложу», «отчитаюсь», «скажу, как только», «вернусь с результатом»
    re.compile(r"(?is)\b(?:доложу|отчитаюсь|скажу,?\s+как\s+только|вернусь\s+с)\b"),
)

_QUOTE_PAIRS = {"«": "»", "“": "”", '"': '"'}


def _unquoted_text(text: str) -> str:
    """Заменить содержимое обычных цитат пробелами, сохранив смещения."""
    chars = list(text)
    stack: list[str] = []
    for index, char in enumerate(text):
        if stack:
            chars[index] = " "
            if char == stack[-1]:
                stack.pop()
            continue
        closing = _QUOTE_PAIRS.get(char)
        if closing:
            chars[index] = " "
            stack.append(closing)
    return "".join(chars)


def enabled() -> bool:
    value = (os.getenv("PRAXIS_PROMISE_WAKE", "1") or "1").strip().lower()
    return value not in {"0", "off", "false", "no"}


def wake_minutes() -> int:
    try:
        return max(1, int(os.getenv("PRAXIS_PROMISE_WAKE_MIN", "15") or 15))
    except ValueError:
        return 15


def detect(text: str) -> str | None:
    """Гист обещания (кусок фразы вокруг совпадения) или None."""
    value = str(text or "")
    if not value.strip():
        return None
    searchable = _unquoted_text(value)
    for pattern in _PROMISE_PATTERNS:
        found = pattern.search(searchable)
        if found:
            start = max(0, found.start() - 40)
            gist = value[start:found.end() + 80]
            return re.sub(r"\s+", " ", gist).strip()[:200]
    return None


def open_for(chat_id: str) -> dict | None:
    """Открытое promise-намерение этой нити (для дедупа), если есть.

    Дедуп идёт по КОМНАТЕ, а не по строке ключа. Пока ключ расщеплялся, одно обещание
    в комнате не мешало завести второе в соседней ветке того же чата: она обещала
    «сейчас проверю» дважды за минуту и получала два пробуждения об одном и том же.
    Псевдонимы только там, где комната доказанно не форум.
    """
    target = f"promise:{chat_id}"
    try:
        import telegram_routes
    except Exception:
        telegram_routes = None
    for item in tasks.list_open():
        # window — как заводились promise-пробуждения до 26.07; такие могут быть ещё
        # открыты в момент выката, и дедуп обязан их видеть, иначе одно обещание получит
        # два возврата.
        if item.get("kind") not in ("wake", "window"):
            continue
        got = str(item.get("target") or "")
        if got == target:
            return item
        if telegram_routes is not None and got.startswith("promise:"):
            if telegram_routes.same_room(got[len("promise:"):], chat_id):
                return item
    return None


# tool_remind_self на успехе возвращает «Наметила #<id> …» (agent.tool_remind_self);
# отказные ветки («Не нахожу @ник…», «мост недоступен», неизвестный after_run) дают
# строку БЕЗ этого маркера и НЕ создают задачу. Сеть должна молчать только когда её
# рука реально сработала — иначе упавшая напоминалка = обещание вообще без драйвера
# (ровно инцидент 14:34 23.07, ради которого пасс A и делался). Адверсарка round-1.
_REMIND_OK = re.compile(r"remind_self\([^)]*\)\s*→\s*Наметила\s+#")


def self_scheduled(tools) -> bool:
    """Она в этом же ходе сама УСПЕШНО взяла себя за руку (remind_self)? Тогда сеть молчит.

    Промежуточный пасс A, анти-костыль «не двойной драйвер»: её собственная
    напоминалка — ровно та диспозиция, которую растим; дублировать её окном —
    и спам, и подмена руки. Но только если задача реально создана: проверяем маркер
    успеха «→ Наметила #», а не одно имя тула (отказ тула не гасит сеть)."""
    for line in tools or ():
        if _REMIND_OK.search(str(line)):
            return True
    return False


def _note(chat_key: str, where: str, text: str, tools=()) -> dict | None:
    """Общий шов чата и её собственных окон: детект → дедуп → окно. Никогда не бросает."""
    try:
        if not enabled():
            return None
        chat = str(chat_key or "").strip()
        if not chat:
            return None
        if self_scheduled(tools):
            return None
        gist = detect(text)
        if gist is None:
            return None
        if open_for(chat) is not None:
            return None  # одна нить — один открытый promise, не спамим окнами
        # Ненавязчивая напоминалка себе — не препятствие и не подталкивание к выбору.
        goal = (
            f"{PROMISE_PREFIX} напоминание себе: ~{wake_minutes()} мин назад {where} "
            f"я сказала «{gist}». Ещё актуально? Закрыла / довожу / переобещаю / снимаю — "
            f"по твоему усмотрению."
        )
        # 26.07: вид сменён window → wake. Смысл возврата — довести обещанное ЧЕЛОВЕКУ в
        # его нити: прочитать, что там теперь, и ответить. В окне Telegram закрыт, то есть
        # ровно эта работа в нём и невозможна — она просыпалась к обещанию без связи с тем,
        # кому обещала. Имя `promise-wake` было честнее механики; теперь совпало.
        task = tasks.add(
            "wake", goal, when=f"in {wake_minutes()}m",
            target=f"promise:{chat}", author="praxis",
        )
        log.info("promise-wake #%s: нить %s, «%s»", task.get("id"), chat, gist[:80])
        return task
    except Exception:
        log.debug("promise-wake не завёлся", exc_info=True)
        return None


def note_outbound(chat_id: str, text: str, tools=()) -> dict | None:
    """Ход ушёл в чат: если в нём обещание — наметить пробуждение. Никогда не бросает."""
    return _note(chat_id, f"в чате {str(chat_id or '').strip()}", text, tools)


def note_self_intent(kind: str, text: str, tools=()) -> dict | None:
    """Промежуточный пасс A: само-направленное «теперь сделаю X» из её собственного окна
    (task_window / forge_event) — тоже не воздух. Одна нить self на все окна; окно-возврат
    мягкое, решает она. Приватный час (heartbeat) сюда не заведён сознательно: будильник
    на размышления наедине — путь к руминации, не к доведению."""
    where = ("в своём окне" if kind == "task_window" else
             "в своём пробуждении" if kind == "wake" else f"в ходе {kind}")
    return _note("self", where, text, tools)
