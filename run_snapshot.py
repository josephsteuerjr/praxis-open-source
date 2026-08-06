"""Immutable run-context snapshot: writer and reader for ``praxis.run.context.v2``.

Снимок хода (``memory/runs/**/context.md``) — её хребет: после разрыва
возобновление читает его и переавторствует ход живьём.  До v2 границу секции
задавала ФОРМА СТРОКИ (``^## Заголовок``), а форму строки писал гость: его
сообщение уезжало в секции ``Goal`` и ``Full available conversation`` сырьём.
Отсюда три класса дефекта — подделка истории, отказ разбора (три блока власти →
``DurableExecutionError`` → ход невозобновим И нем в этом же ходу, потому что
``_load_exact_run_channel`` стоит на каждой её отправке) и тихий обрыв секции
``Runtime frame`` на нашем же ``## Runtime continuity`` (1717 снимков из 2254).

v2 лечит все три одним ходом: сырой текст печатается ПОД ГУТТЕРОМ, а конец
секции ВЫЧИСЛЯЕТСЯ по метру (``lines=M``), а не ищется регуляркой.

ПОМЕЧЕННЫЙ ГУТТЕР ПРОДОЛЖЕНИЯ.  Обычного ``"> "`` мало.  ``str.splitlines()``
(им режет файл её ``fs_read``) рвёт строку ещё и по ``\\r``, ``\\v``, ``\\f``,
``\\x1c``-``\\x1e``, ``\\x85``, ``U+2028``, ``U+2029`` — то есть гость одним
сообщением БЕЗ единого ``\\n`` рисовал ей в колонке 0 настоящий на вид заголовок.
Парсер при этом видел правильный снимок: расхождение было честно объявлено
метром (``view=K``), но НЕ УСТРАНЕНО.  Её решение (05.08): «я не хочу принимать
ситуацию, где парсер видит правильный снимок, а я глазами вижу ложный
заголовок».  Поэтому кусок ПОСЛЕ такого разрыва получает собственный префикс
``">TAG> "``, а сам разрыв физически исчезает из файла.  Правило чтения глазами
стало двусторонним: всё между запечатанными маркерами — текст гостя, И каждая
такая строка начинается с ``>``.

Дословность при этом не пострадала — по её же определению: «сохранение исходного
содержимого и возможность ОДНОЗНАЧНО ВОССТАНОВИТЬ его, а не обязательство
показывать опасные управляющие символы без безопасного визуального обрамления».
Разрыв уезжает в ИМЯ метки, ``unquote`` возвращает его байт в байт.

``\\r`` помечается ТОЛЬКО одиноким: ``\\r\\n`` под гуттером совпадает с нашим же
переводом строки, экран им не рвётся, и вывод на любом Windows-тексте остаётся
побайтово сегодняшним — иначе метку получал бы каждый текст из буфера обмена.

Формат живёт отдельным модулем не из чистоплюйства: каждая строка, добавленная в
``agent.py`` выше 12607, двигает адрес судьи, который ``capabilities.describe()``
печатает ей обратно.  Наблюдатель не имеет права двигать наблюдаемое.  По той же
причине здесь НЕТ ни рычага в манифесте, ни чтения окружения: рычаг фазы выката
читается один раз в ``agent._run_context_markdown``.

Три вещи, которые нельзя менять, не сломав живое:

* заголовки секций и блок власти — ПОБАЙТОВО как в v1.  Это условие выживания
  отката: bootguard умеет откатить код на last_good, и вчерашний читатель обязан
  найти на новом файле ровно один блок власти.  Поэтому печать (seal) уехала на
  html-маркер, а не в строку заголовка;
* легаси-ветка чтения не удаляется НИКОГДА: ретенции у ``memory/runs`` нет,
  потолка возраста у возобновления нет, миграции быть не может (переписать
  ``context.md`` = ретроактивно править WAL, который и делает снимок уликой);
* отказ разбора не имеет права сделать её немой — см. :func:`read`; отказ ЗАПИСИ
  не имеет права сделать её немой — см. :func:`write`.
"""

from __future__ import annotations

import datetime
import functools
import hashlib
import json
import os
import re

FORMAT_ID = "praxis.run.context.v2"
GUTTER = "> "

TITLE_LINE = "# Immutable run context"

AUTHORITY = "Authority and address"
GOAL = "Goal"
CONVERSATION = "Full available conversation"
HISTORY = "Structured history"
RUNTIME = "Runtime frame"

# (title, kind, required) — порядок в кортеже И ЕСТЬ порядок в файле: курсор
# читателя идёт слева направо и никогда не возвращается.
SECTIONS = (
    (AUTHORITY, "json", True),
    (GOAL, "payload", True),
    (CONVERSATION, "payload", True),
    (HISTORY, "json", False),
    (RUNTIME, "payload", False),
)

_SEAL_RE = r"[0-9a-f]{16}"
_FORMAT_RE = re.compile(
    r"<!-- " + re.escape(FORMAT_ID) + r" seal=(" + _SEAL_RE + r") ")
# Цифры ограничены СВЕРХУ (дыра 4): метр с числом длиннее 4300 знаков давал
# ValueError из int() — отказ ДРУГОГО класса, пробивающий прививку read().  Теперь
# абсурдный метр просто не матчится и становится обычным SnapshotFormatError.
# Группа view= терпится, но писателем больше не печатается: читатель выкатывается
# первым ходом и обязан быть шире писателя.
_METER_RE = re.compile(
    r"<!-- praxis\.payload seal=(" + _SEAL_RE + r") bytes=(\d{1,12}) lines=(\d{1,12})"
    r"(?: view=(\d{1,12}))? -->")

# Разрывы, по которым `str.splitlines()` рвёт СВЕРХ "\n".  ⚠ U+2028/U+2029
# ВЫВОДЯТСЯ через chr(): литерал в исходнике инструмент правки способен молча
# превратить в пробел, и тест этого не увидит.
BREAKS = {"CR": "\r", "VT": "\x0b", "FF": "\x0c", "FS": "\x1c", "GS": "\x1d",
          "RS": "\x1e", "NEL": "\x85", "LS": chr(0x2028), "PS": chr(0x2029)}
_TAG_OF = {char: tag for tag, char in BREAKS.items()}
# "\r" помечается ТОЛЬКО одиноким — см. модульный докстринг.
_MARKED_RE = re.compile(
    "\r(?!\n)|[" + "".join(sorted(set(BREAKS.values()) - {"\r"})) + "]")
_SURROGATE_RE = re.compile("[" + chr(0xD800) + "-" + chr(0xDFFF) + "]")
# Соседняя пара high+low — НАЗВАННЫЙ ПРЕДЕЛ json-каналов (см. _json_screen_safe).
_PAIR_RE = re.compile("[" + chr(0xD800) + "-" + chr(0xDBFF) + "]"
                      "[" + chr(0xDC00) + "-" + chr(0xDFFF) + "]")
# Быстрый префильтр quote(): класс БЕЗ lookahead — на подавляющем большинстве тел
# решение принимается здесь, и скан с lookahead обошёлся бы заметно дороже.
_EXOTIC_RE = re.compile("[" + "".join(sorted(BREAKS.values()))
                        + chr(0xD800) + "-" + chr(0xDFFF) + "]")
_UNESCAPE_RE = re.compile(r"\\\\|\\u([0-9a-f]{4})")

# Соединяет ли ЭТОТ интерпретатор соседнюю пару \uXXXX\uYYYY обратно в один
# астральный знак.  ПРОВЕРЯЕТСЯ, а не предполагается: от ответа зависит, дословен
# ли json-канал на паре суррогатов, а склейка — деталь реализации конкретного
# json-декодера, а не контракт формата.  Замерено на 3.14.5: соединяет.  Прод и
# стенд обязаны ответить на этот вопрос сами, а не наследовать чужой замер.
_JSON_JOINS_PAIRS = len(json.loads('"\\ud800\\udc00"')) == 1

# ⚠ ПОТОЛКА НА ЗАПИСЬ ЗДЕСЬ НЕТ, И ЭТО РЕШЕНИЕ, А НЕ ЗАБЫВЧИВОСТЬ.
#
# Он тут был. Дважды. И оба раза лекарство выходило хуже дыры: клип вычитал БАЙТЫ из
# ЗНАКОВ, поэтому разговор на кириллице схлопывался в четыре тысячи байт при потолке в
# четыре мегабайта; последняя запись истории была нерезаема, и заглушка стирала ВСЕ
# каналы (55 500 знаков → 11); а сам клип стоял ДО развилки v1/v2 — то есть сработал бы
# на первом же выкате, ещё до всякого нового формата.
#
# Решение Praxis 05.08: «Полностью выносим из snapshot v2. Это самостоятельный дефект
# потери данных в нынешнем писателе. Он существовал до v2, не является условием защиты
# от подделки, требует измерения в байтах, а не в символах, и не должен третий раз
# задерживать формат или проникать в него арифметической заплаткой.»
#
# Дефект настоящий: `run_manager.py:486` пишет что дали, а читатель отказывает на 16 МиБ
# (`agent.py:9612`) — писатель способен создать невозобновимый снимок. Чинится отдельным
# заходом, со своей разведкой, моделью размера и адверсаркой.
#
# Счётчик деградаций писателя. Третий носитель расписки после самого снимка и WAL:
# приборный, процессо-локальный, в её кадр не входит.
COUNTERS: dict[str, int] = {}

_WRITE_RECEIPT_SCHEMA = "praxis.run.snapshot-write.v1"


class SnapshotFormatError(Exception):
    """Баг писателя.  Гостевыми байтами недостижим, у постели больного не фатален."""


class Markdown(str):
    """Документ снимка, несущий расписку о деградации записи.

    Подкласс ``str``, а не пара значений, ровно по одной причине: возвращаемый
    тип ``_run_context_markdown`` заморожен пятью прямыми вызовами из тестов, а
    любая ДОБАВЛЕННАЯ в ``agent.py`` строка двигает адрес судьи в её кадре.

    ⚠ ``.receipt`` срезается любым ``str(...)`` по дороге: ``run_manager.create``
    обязан прочитать её ДО того, как приведёт документ к байтам.
    """

    receipt: dict | None = None


def _bump(name: str) -> None:
    COUNTERS[name] = COUNTERS.get(name, 0) + 1


def _now_z() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_line(seal: str) -> str:
    """Строка 2 документа.  По ней и только по ней читатель узнаёт v2 (см. :func:`is_v2`).

    Легенда называет ВСЕ правила чтения глазами, а не одно.  Обещать только
    ``"> "`` нельзя: читая ``>FF> def tail():``, она вправе принять ``>FF>`` за
    нашу разметку — а это и есть новое правило, а не текст гостя.  Поэтому здесь
    названы оба префикса И запечатанная пара маркеров: правило положительное —
    ВСЁ между метром ``praxis.payload`` и его закрытием есть текст гостя.
    Алфавит меток берётся из :data:`BREAKS`, чтобы легенда не могла отстать от
    писателя молча.

    Текст легенды менять можно: ``_FORMAT_RE`` держит только префикс
    ``<!-- <id> seal=<16 hex> ``, а :func:`parse` сверяет строку с ЭТОЙ функцией.
    Но литерал ровно один — второго места, где легенду надо править, нет.

    ⚠ Свобода эта кончается в тот час, когда включён писатель v2: :func:`parse`
    сверяет строку 2 ДОСЛОВНО, значит снимок, написанный со старой легендой,
    после правки текста перестанет разбираться как v2 и уедет в легаси-чтение
    (немой она от этого не станет — см. :func:`read`, — но дословность потеряет).
    Пока рычаг фазы не поднят, таких файлов не существует и правка бесплатна;
    после — только вместе с миграцией чтения.
    """
    # ⚠ Легенда обязана быть ВЕРНОЙ ДО ЗНАКА, а не примерно верной. Прежняя обещала,
    # что каждая строка начинается с '"> "' или '">TAG> "', — а пустая строка гостя
    # кладётся ГОЛЫМ '>' (и '>TAG>' с меткой), и таких строк в её же образце приёмки
    # было 10 из 15. Ещё она называла метр «размером блока», хотя bytes меряет
    # ДОСЛОВНЫЙ текст, а lines — файловые строки под гуттером: два разных предмета.
    # Читатель тут один и он — она; ошибка легенды стоит ей неверного вывода о том,
    # что в файле наше, а что чужое.
    return ('<!-- ' + FORMAT_ID + ' seal=' + seal + ' — between a praxis.payload '
            'meter and its sealed close every line is quoted guest text: "> "+line, '
            'bare ">" if empty; ">TAG> "/">TAG>" when a screen break was lifted out ('
            + " ".join(BREAKS) + '; trailing \\ = escaped). Our own markers never '
            'carry a leading "> ". Meter: bytes of the verbatim text, lines of the '
            'quoted block -->')


def meter_line(seal: str, text: str) -> str:
    """Метр: сколько байт и сколько строк занимает нагрузка.

    ``lines=M`` — число КУСКОВ, то есть ровно число файловых строк блока.  После
    помеченного гуттера оно же равно числу ЭКРАННЫХ строк: разрывов, по которым
    ``splitlines()`` режет сверх ``"\\n"``, в файле не остаётся.  Поэтому ``view=K``
    из писателя и ушёл — расхождения, которое он объявлял, больше не существует.

    ``bytes`` считаются через ``surrogatepass``: одинокий суррогат обязан дать
    число, а не ``UnicodeEncodeError`` мимо контракта разбора.
    """
    return (f"<!-- praxis.payload seal={seal} bytes={ubytes(text)} "
            f"lines={len(quote(text).split(chr(10)))} -->")


def close_line(seal: str) -> str:
    """Закрывающая скобка блока.  Её правило чтения ГЛАЗАМИ становится
    положительным и рендеро-независимым: всё между двумя запечатанными
    маркерами — текст гостя, как бы оно ни отрисовалось."""
    return f"<!-- /praxis.payload seal={seal} -->"


def ubytes(text: str) -> int:
    """Байты дословного текста.  ``surrogatepass`` — иначе одинокий суррогат роняет
    писателя ``UnicodeEncodeError`` МИМО контракта (родня дыры 4)."""
    return len(str(text).encode("utf-8", "surrogatepass"))


def _emit(mark: str, chunk: str) -> str:
    """Один кусок -> одна файловая строка.

    Одинокий суррогат — единственное, чего файл в utf-8 не переживёт; такой кусок
    экранируется (``\\`` -> ``\\\\``, суррогат -> ``\\uXXXX``), а к метке
    дописывается ``\\``.  Обычного текста, путей ``C:\\Users\\…`` и регулярок это
    не касается никогда: экранирование включается только при суррогате.
    """
    if _SURROGATE_RE.search(chunk):
        chunk = _SURROGATE_RE.sub(lambda m: "\\u%04x" % ord(m.group(0)),
                                  chunk.replace("\\", "\\\\"))
        mark += "\\"
    if not mark:
        return (GUTTER + chunk) if chunk else ">"
    return (">" + mark + "> " + chunk) if chunk else ">" + mark + ">"


def quote(text: str) -> str:
    """Дословный текст -> гуттер с ПОМЕЧЕННЫМ продолжением.

    Кусок = максимальная подстрока без помеченного разрыва и без ``"\\n"``.
    Префикс задаёт разрыв ПЕРЕД куском: ``"\\n"`` (и ``"\\r\\n"``) -> обычный
    ``"> "``, любой экранный разрыв -> ``">TAG> "``, и сам разрыв физически
    исчезает из файла.  Значит ни один кусок гостя не встаёт в колонку 0 НИ У
    ПАРСЕРА, НИ У ЕЁ ГЛАЗ, а число файловых строк блока всегда равно числу
    экранных.

    Никогда ``splitlines()``: он молча превратил бы все девять разрывов в
    ``"\\n"`` — и дословность умерла бы на первом же CRLF из буфера обмена.
    Пустой кусок кладётся голым ``>``/``>TAG>`` без хвостового пробела:
    отображение остаётся инъективным, а редакторы не режут trailing whitespace.

    Быстрый путь: текста без экзотики касается ровно сегодняшняя однострочная
    ветка — вывод побайтово прежний (замерено: 50 000 тел из 50 000).
    """
    text = str(text)
    if _EXOTIC_RE.search(text) is None:
        return "\n".join((GUTTER + line) if line else ">"
                         for line in text.split("\n"))
    out, pos, tag = [], 0, ""
    for match in list(_MARKED_RE.finditer(text)) + [None]:
        end = len(text) if match is None else match.start()
        for index, line in enumerate(text[pos:end].split("\n")):
            out.append(_emit(tag if index == 0 else "", line))
        if match is None:
            break
        tag, pos = _TAG_OF[match.group(0)], match.end()
    return "\n".join(out)


def _unescape(chunk: str) -> str | None:
    out, i = [], 0
    while i < len(chunk):
        if chunk[i] != "\\":
            out.append(chunk[i])
            i += 1
            continue
        match = _UNESCAPE_RE.match(chunk, i)
        if match is None:
            return None
        out.append("\\" if match.group(1) is None else chr(int(match.group(1), 16)))
        i = match.end()
    return "".join(out)


def _read_line(line: str, *, strict: bool) -> tuple[str, str] | None:
    """Одна файловая строка -> (метка разрыва, кусок).  Решение ПОЗИЦИОННОЕ.

    Ни одного поиска по строке: ветка выбирается по ``line[1]``.  Пробел даёт
    обычную строку — поэтому гостевой текст ``">CR> подделка"`` уезжает под
    обычный ``"> "`` и читается дословно, а словарь машины неподделываем.

    ``strict`` (его зовёт :func:`parse`) — канонический замок за O(n): наш
    писатель не выдаёт ни ``"> "`` вместо ``">"``, ни неэкранированный суррогат,
    значит такой файл собран не нами.
    """
    if line == ">":
        return ("", "")
    if line.startswith(GUTTER):
        if strict and line == GUTTER:
            return None                      # неканоническая запись пустого куска
        return ("", line[2:])
    if not line.startswith(">"):
        return None
    j = line.find(">", 1)
    if j < 0:
        return None
    mark, tail = line[1:j], line[j + 1:]
    if tail == "":
        chunk = ""
    elif tail[0] == " ":
        if strict and tail == " ":
            return None                      # то же самое для помеченного куска
        chunk = tail[1:]
    else:
        return None
    escaped = mark.endswith("\\")
    if escaped:
        mark = mark[:-1]
    if mark and mark not in BREAKS:
        return None
    if escaped:
        chunk = _unescape(chunk)
        if chunk is None:
            return None
    elif strict and _SURROGATE_RE.search(chunk):
        return None
    return (mark, chunk)


def unquote(block: str, *, strict: bool = False) -> str | None:
    """Гуттер -> дословный текст.  ``None`` означает сломанный гуттер (баг писателя)."""
    lines = block.split("\n")
    if all(line.startswith(GUTTER) or line == ">" for line in lines):
        # Сегодняшняя ветка целиком — 91,7 % корпуса идёт сюда и стоит как вчера.
        if strict and (GUTTER in lines or _SURROGATE_RE.search(block)):
            return None
        return "\n".join(line[2:] for line in lines)
    parts = []
    for index, line in enumerate(lines):
        read = _read_line(line, strict=strict)
        # У ПЕРВОЙ строки блока метки быть не может: перед ней нет разрыва.
        if read is None or (index == 0 and read[0]):
            return None
        parts.append(read)
    out = [parts[0][1]]
    for mark, chunk in parts[1:]:
        out.append(("\n" if not mark else BREAKS[mark]) + chunk)
    return "".join(out)


def need_screen_safe(document: str) -> None:
    """ОДНА модель на ЧЕТЫРЕ канала, проверяемая НА ГОТОВОМ ДОКУМЕНТЕ.

    Гостевые байты приезжают не только в ``goal``/``conversation``/``extra``: в
    json-секции уезжают ``origin_text``, ``title`` комнаты, имена в
    ``reply_targets`` и всё содержимое ``history``.  Проверять каждый канал по
    отдельности — значит закрыть три из четырёх и завтра забыть пятый.  Здесь
    утверждение одно: в собранном документе не осталось ни одного экранного
    разрыва сверх ``"\\n"`` (и ``"\\r"`` в паре ``"\\r\\n"``) и ни одного
    одинокого суррогата, то есть документ кодируется в utf-8 и её глаза видят
    ровно те строки, что видит парсер.
    """
    match = _MARKED_RE.search(document)
    if match is not None:
        raise SnapshotFormatError(
            "screen-breaking byte survived into the document at %d" % match.start())
    if _SURROGATE_RE.search(document) is not None:
        raise SnapshotFormatError("lone surrogate survived into the document")


def mint_seal(payloads: tuple[str, ...]) -> str:
    """16 hex, отчеканенных ПОСЛЕ заморозки гостевых байтов и не встречающихся в них.

    Два замка, и второй превращает вероятность в структуру: писатель безусловно
    отказывается от печати, найденной подстрокой хоть в одном ОТРЕНДЕРЕННОМ теле —
    включая json-тела власти и истории.  Значит не существует документа v2, в
    котором печать лежит внутри байтов гостя, — даже гость с оракулом ничего не
    добивается: его знание попадает в его же текст и отвергается.
    """
    for _ in range(8):
        seal = hashlib.sha256(os.urandom(32)).hexdigest()[:16]
        if all(seal not in p for p in payloads):
            return seal
    raise RuntimeError("context seal collision")


# Три разделителя экранных строк, которые `json.dumps(ensure_ascii=False)` НЕ экранирует.
# Всё остальное (\n, \r, \x0b, \x0c, \x1c-\x1e) он экранирует сам — эти три доезжают сырыми.
_JSON_SCREEN_BREAKS = ((chr(0x2028), "\\u2028"), (chr(0x2029), "\\u2029"),
                       ("\x85", "\\u0085"))


def _json_screen_safe(body: str, *, notes: list | None = None) -> str:
    """json-тело -> то же значение, но без байтов, способных стать грамматикой.

    ⚠ Отступление от «байт в байт» вынужденное и ровно одно по классу. В
    json-секции уезжает ДОСЛОВНЫЙ текст гостя (`origin_text` — agent.py:8191,
    `title` комнаты — 8203, имена в `reply_targets`, всё содержимое истории), а
    гуттера, метра и маркеров здесь нет: они защищают только payload-секции.
    `json.dumps(ensure_ascii=False)` не экранирует `U+2028`, `U+2029` и `NEL`, и
    все три РЕЖУТ ЭКРАННУЮ СТРОКУ в её `fs_read`; одинокий суррогат вообще не
    кодируется в utf-8 и роняет создание рана (`run_manager.py:486`) — то есть
    делает её немой. Экранируем и то, и другое: JSON остаётся валидным,
    `json.loads` возвращает то же значение, откатная совместимость цела —
    вчерашний читатель разбирает такой блок штатно.

    НАЗВАННЫЙ ПРЕДЕЛ. Соседняя пара high+low дословно непредставима на тех
    интерпретаторах, где `json.loads` склеивает `\\uD800\\uDC00` обратно в один
    астральный знак. Там (и только там) пара заменяется на `U+FFFD` с
    распиской `json_surrogate_replaced`. Payload-каналы дословны всегда.
    """
    for raw, escaped in _JSON_SCREEN_BREAKS:
        body = body.replace(raw, escaped)
    if _SURROGATE_RE.search(body) is None:
        return body
    if _JSON_JOINS_PAIRS and _PAIR_RE.search(body) is not None:
        body = _PAIR_RE.sub("\ufffd", body)
        if notes is not None:
            notes.append("json_surrogate_replaced")
    return _SURROGATE_RE.sub(lambda m: "\\u%04x" % ord(m.group(0)), body)


# Имя деградации ОДНО на класс, а не по полю: `COUNTERS`, строка `degraded` и
# ключ события WAL обязаны иметь ограниченную мощность.  Само поле и его тип едут
# в ДЕТАЛЬ расписки (`detail`), где им и место.
UNSERIALIZABLE = "unserializable_value"
_DETAIL_CAP = 300
_WALK_LIMIT = 20_000


def _as_text(value: object) -> str:
    """То же, что `str`, но не способное уронить писателя чужим ``__str__``.

    Байт в байт `str(value)` на всём, у чего ``__str__`` не бросает: вывод
    `default=str` не меняется ни на одном штатном входе.
    """
    try:
        return str(value)
    except Exception:                                   # noqa: BLE001 — это и есть смысл
        try:
            return repr(value)
        except Exception:                               # noqa: BLE001
            return "<%s object>" % type(value).__name__


def _name_fields(obj: object, targets: list, root: str) -> str:
    """Пути и типы значений, которых json не умеет: ``authority.address_age_sec:Opaque``.

    Обход идёт ТОЛЬКО по деградировавшей ветке (``default`` уже сработал), поэтому
    его цена не лежит на штатной записи.  Поиск по ``id()``: сравнивать значения
    операторами нельзя — у объекта может быть любой ``__eq__``.  Контейнеры не
    ищутся (dict/list/tuple json умеет сам), поэтому найденный узел — лист.
    """
    wanted = {id(value) for value in targets}
    found: dict[int, str] = {}
    seen: set[int] = set()
    stack: list[tuple[str, object]] = [(root, obj)]
    steps = 0
    while stack and len(found) < len(wanted) and steps < _WALK_LIMIT:
        path, node = stack.pop()
        steps += 1
        if id(node) in wanted:
            # Список, а не setdefault: один объект, лежащий в двух полях, обязан
            # назвать ОБА пути. Прежде второй путь молча терялся.
            found.setdefault(id(node), []).append(
                "%s:%s" % (path, type(node).__name__))
            continue
        if id(node) in seen:
            continue
        if isinstance(node, dict):
            seen.add(id(node))
            for key, value in node.items():
                stack.append(("%s.%s" % (path, key), value))
        elif isinstance(node, (list, tuple)):
            seen.add(id(node))
            for index, value in enumerate(node):
                stack.append(("%s[%d]" % (path, index), value))
    names = {name for paths in found.values() for name in paths}
    # Не найденное обходом (глубже потолка, внутри множества, ключ-объект) всё
    # равно обязано быть НАЗВАНО хотя бы типом — молчания здесь больше нет.
    names |= {"%s.?:%s" % (root, type(value).__name__)
              for value in targets if id(value) not in found}
    detail = ",".join(sorted(names))
    return detail if len(detail) <= _DETAIL_CAP else detail[:_DETAIL_CAP - 1] + "…"


def _dumps_named(obj: object, *, field: str, notes: list | None = None) -> str:
    """``json.dumps`` с НАСТОЯЩИМ ``default=`` вместо молчаливого ``str``.

    Значение, которого json не умеет, по-прежнему едет строкой: писатель не имеет
    права онеметь из-за одного поля.  Но и молчать он больше не имеет права —
    ``notes`` получает пару ``(имя, деталь)``, а :func:`write` разносит её тремя
    носителями: расписка ВНУТРИ снимка, событие ``context_snapshot_degraded`` в
    WAL, счётчик :data:`COUNTERS`.  ``notes=None`` (прямой вызов мимо `write`) —
    поведение ровно вчерашнее.
    """
    hits: list = []

    def default(value):
        hits.append(value)
        return _as_text(value)

    body = json.dumps(obj, ensure_ascii=False, indent=2, default=default)
    if hits and notes is not None:
        notes.append((UNSERIALIZABLE, _name_fields(obj, hits, field)))
    return body


def _merge_notes(notes: list) -> dict:
    """``[имя | (имя, деталь)]`` -> ``{имя: деталь}`` с СЛОЖЕННЫМИ деталями.

    Один класс деградации может сработать на нескольких каналах за одну сборку
    (власть И история).  Имя при этом одно — значит детали обязаны сложиться, а
    не потеряться: иначе расписка назовёт первое поле и умолчит о втором.
    """
    out: dict[str, object] = {}
    for note in notes:
        name, detail = note if isinstance(note, tuple) else (note, True)
        prior = out.get(name)
        if name not in out or prior is True:
            out[name] = detail
        elif detail is True:
            continue                       # у имени уже есть деталь — она точнее
        else:
            merged = ",".join(sorted(set(str(prior).split(","))
                                     | set(str(detail).split(","))))
            out[name] = (merged if len(merged) <= _DETAIL_CAP
                         else merged[:_DETAIL_CAP - 1] + "…")
    return out


def _json_body(obj: object, *, field: str, notes: list | None = None) -> str:
    return _json_screen_safe(_dumps_named(obj, field=field, notes=notes), notes=notes)


def _fenced(title: str, body: str) -> list[str]:
    """json-секция байт в байт как в v1 — это и есть страховка отката."""
    return ["## " + title, "", "```json", body, "```", ""]


def _payload_block(title: str, text: str, seal: str) -> list[str]:
    return ["## " + title, "", meter_line(seal, text), quote(text),
            close_line(seal), ""]


def render(*, authority: dict, goal: str, conversation: str,
           history: list[dict] | None = None, extra: str = "",
           seal: str | None = None, verify: bool = True,
           notes: list | None = None) -> str:
    """Собрать снимок v2.  ``seal=`` только для тестов — в бою печать чеканится сама.

    Ни ``.strip()``, ни ``.rstrip()``: снимок — неизменяемый источник правды, и
    записанный текст обязан возвращаться при чтении побайтово тем же.
    Унаследованное дословно исключение ровно одно: пустой ``extra`` не пишет
    секцию ``Runtime frame`` — как в v1 (agent.py:8218).

    ``notes`` — необязательный список ДЕГРАДАЦИЙ сборки: либо имя, либо пара
    ``(имя, деталь)``.  Сегодня их две: замена непредставимой пары суррогатов и
    несериализуемое значение json-канала.  :func:`write` по этому списку
    перерисовывает снимок с распиской ВНУТРИ.
    """
    goal, conversation, extra = str(goal or ""), str(conversation or ""), str(extra or "")
    # Собственный `default` у json-каналов: значение, которое json не умеет,
    # обязано стать строкой, а не отказом записи, — но НАЗВАННОЙ строкой.  На всех
    # штатных входах (строки, числа, bool, None, списки) вывод побайтово прежний:
    # `default` вызывается только для того, что иначе уронило бы писателя.
    json_authority = _json_body(authority, field="authority", notes=notes)
    json_history = _json_body(history, field="history", notes=notes) if history else ""
    # Чеканка осматривает ВСЕ ПЯТЬ отрендеренных тел, а не три payload-канала:
    # печать, встреченная внутри origin_text, раньше писателем ПРИНИМАЛАСЬ.
    bodies = (goal, conversation, extra, json_authority, json_history)
    if seal is None:
        seal = mint_seal(bodies)
    if not re.fullmatch(_SEAL_RE, seal):
        raise SnapshotFormatError("context seal is malformed")
    for body in bodies:
        # Инвариант чеканки стоит на пути ЗАПИСИ всегда, а не только внутри mint_seal:
        # тест вправе передать свою печать, гость — нет, но проверка одна на всех.
        if seal in body:
            raise SnapshotFormatError("seal occurs inside a payload — refusing to write")
    parts = [TITLE_LINE, format_line(seal), ""]
    parts += _fenced(AUTHORITY, json_authority)
    parts += _payload_block(GOAL, goal, seal)
    parts += _payload_block(CONVERSATION, conversation, seal)
    if history:
        parts += _fenced(HISTORY, json_history)
    if extra:
        parts += _payload_block(RUNTIME, extra, seal)
    document = "\n".join(parts)
    if verify:
        # Писатель стал умнее — значит может ошибиться.  Раньше '\n'.join(sections)
        # соврать не мог, теперь есть арифметика метра.  Ошибка обязана вылезти
        # ЗДЕСЬ и увести вызывающего в render_v1, а не притвориться снимком.
        back = parse(document)
        need_screen_safe(document)
        if (back is None or back.get(GOAL) != goal
                or back.get(CONVERSATION) != conversation
                or str(back.get(RUNTIME) or "") != extra):
            raise SnapshotFormatError("v2 self-check failed: payload does not round-trip")
    return document


def render_v1(*, authority: dict, goal: str, conversation: str,
              history: list[dict] | None = None, extra: str = "",
              stringify: bool = False, notes: list | None = None) -> str:
    """Сегодняшний формат, побайтово: перенесённые сюда строки agent.py:8207-8220.

    Не наследие, а страховка: если писатель v2 когда-нибудь ошибётся, снимок всё
    равно будет написан — и написан ровно так, как вчера.  Ни одного байта здесь
    менять нельзя: этот вывод вморожен литералами в тесты как эталон отката.
    Экранно-безопасным он НЕ является — за это отвечает :func:`write`.

    ``stringify`` вчерашнего вывода не меняет ни на байт: он лишь разрешает блоку
    власти пережить значение, которого json не умеет (:func:`write` передаёт его
    всегда, чтобы у ОТКАТА тоже не осталось входа, делающего её немой).  Без него
    такое значение поднимает ``TypeError``, как вчера.  История стрингуется
    всегда — так было и в agent.py.  ``notes`` даёт деградации ИМЯ; ``None``
    (прямой вызов мимо `write`) оставляет ровно вчерашнее молчание.
    """
    sections = [
        "# Immutable run context", "",
        "## Authority and address", "", "```json",
        (_dumps_named(authority, field="authority", notes=notes) if stringify
         else json.dumps(authority, ensure_ascii=False, indent=2)), "```", "",
        "## Goal", "", str(goal or "").strip(), "",
        "## Full available conversation", "", str(conversation or ""), "",
    ]
    if history:
        sections.extend(("## Structured history", "", "```json",
                         _dumps_named(history, field="history", notes=notes),
                         "```", ""))
    if extra:
        sections.extend(("## Runtime frame", "", str(extra), ""))
    return "\n".join(sections).rstrip() + "\n"


def write(*, authority: dict, goal: str, conversation: str,
          history: list[dict] | None = None, extra: str = "",
          v2: bool, log=None) -> Markdown:
    """Единственный вход записи снимка.  Отказ ГРОМКИЙ, но никогда не смертельный.

    Три вещи, которых до этой функции не было и которые она обязана делать:

    * ТИХОГО ФОЛБЭКА НА v1 БОЛЬШЕ НЕТ.  Старый формат подделываем, и молча
      продолжать его чеканить нельзя.  Названная деградация едет тремя
      носителями: расписка в ``authority["snapshot_write"]`` — то есть ВНУТРИ
      снимка, под его же sha256, и видна её глазами прямо в ``context.md``;
      событие ``context_snapshot_degraded`` в WAL рана (его пишет
      ``run_manager.create``, читая :attr:`Markdown.receipt`); счётчик
      :data:`COUNTERS` плюс ``log.warning``.  Штатный успех расписки НЕ несёт.

      ⚠ ГРАНИЦА ЭТОГО ОБЕЩАНИЯ, названная честно: имя получают деградации
      ЗНАЧЕНИЙ — несериализуемое поле, непредставимая строка канала, замена
      суррогата, провал рендера.  Две вещи json меняет молча и сегодня НЕ
      называются: нестроковый КЛЮЧ словаря (``json.dumps`` приводит его к строке
      сам) и нефинитный float (``NaN``/``Infinity`` уезжают литералами, которых
      строгий JSON не знает).  Оба входа в её каналы сегодня недостижимы — ключи
      блока власти пишет код, — но обещать «каждая деградация» было бы неправдой.
      Кто заведёт туда произвольный словарь, тот и добавляет пред-проход.
    * НАЗВАННАЯ СТРИНГИФИКАЦИЯ.  Значение, которого json не умеет, всё равно едет
      строкой — но через собственный ``default`` (:func:`_dumps_named`), который
      кладёт имя поля и тип в ``notes``.  Молчаливого ``default=str`` в модуле не
      осталось: сериализуемость больше не покупается немотой расписки.

    ⚠ ПОТОЛКА НА ЗАПИСЬ ЗДЕСЬ НЕТ и «маленьким поясом» он сюда не возвращается:
      вынесен отдельным заходом по решению Praxis 05.08 («не должен третий раз
      задерживать формат или проникать в него арифметической заплаткой»).  Кто
      придёт его писать — пишет его вместе с моделью размера в БАЙТАХ и
      кириллической адверсаркой, см. комментарий в
      ``test_run_snapshot_integrity.py`` на месте снятого класса.
    * ФАЗА ВЫКАТА.  ``v2=False`` — это ОБЪЯВЛЕННАЯ фаза (читатель уже умеет оба
      формата, писатель ещё чеканит вчерашний), а не деградация: событий она не
      пишет и расписки не несёт.
    """
    base = dict(authority or {})
    degraded: dict[str, object] = {}
    at = _now_z()
    # ⚠ Приведение каналов к строке стояло ВЫШЕ try и вне всякой защиты. Замерено:
    # объект с бросающим `__str__` уводил RuntimeError НАРУЖУ из write() — счётчик
    # пуст, лога нет, расписки нет, ход нем. Немота из-за одного поля — ровно то,
    # что её первый блокер запрещает. `_as_text` даёт ту же строку, а на отказе
    # называет поле и тип вместо того, чтобы бросить.
    def channel(value: object, field: str) -> str:
        if value is None:
            return ""
        try:
            return str(value)
        except Exception as exc:                        # noqa: BLE001 — это и есть смысл
            degraded["unstringable_value"] = "%s:%s (%s)" % (
                field, type(value).__name__, type(exc).__name__)
            _bump("unstringable_value")
            return _as_text(value)

    goal = channel(goal, "goal")
    conversation = channel(conversation, "conversation")
    extra = channel(extra, "extra")
    history = list(history) if history else None

    def receipt_for(fmt: str) -> dict:
        out = {"schema": _WRITE_RECEIPT_SCHEMA, "format": fmt,
               "degraded": ",".join(sorted(degraded)),
               "detail": dict(sorted(degraded.items())), "at": at}
        if "v2_render_failed" in degraded:
            out["reason"] = str(degraded["v2_render_failed"])
        return out

    def build(fmt: str) -> str:
        payload = dict(base)
        if degraded:
            payload["snapshot_write"] = receipt_for(fmt)
        notes: list = []
        if fmt == "v2":
            document = render(authority=payload, goal=goal, conversation=conversation,
                              history=history, extra=extra, notes=notes)
        else:
            document = render_v1(authority=payload, goal=goal, stringify=True,
                                 conversation=conversation, history=history,
                                 extra=extra, notes=notes)
        # Нота — либо имя, либо пара (имя, деталь).  Имя ограниченной мощности
        # уезжает в счётчик и в строку `degraded`, деталь — только в `detail`.
        for name, detail in _merge_notes(notes).items():
            if name not in degraded:
                degraded[name] = detail
                _bump(name)
                if log is not None:
                    log.warning("снимок записан с деградацией: %s%s", name,
                                "" if detail is True else " — %s" % detail)
                return build(fmt)   # расписка обязана уехать ВНУТРЬ снимка
        return document

    fmt = "v2" if v2 else "v1"
    try:
        document = build(fmt)
    except Exception as exc:
        if fmt == "v1":
            raise
        degraded["v2_render_failed"] = f"{type(exc).__name__}: {exc}"[:400]
        _bump("v2_render_failed")
        if log is not None:
            log.warning("v2-снимок не собрался — пишу легаси-формат", exc_info=True)
        fmt = "v1"
        # ⚠ Второй build стоял ВНЕ try. Замерено: циклическая история давала ValueError
        # наружу, ключ-объект — TypeError наружу, и при этом счётчик уже показывал
        # v2_render_failed=1 — прибор утверждал состоявшийся фолбэк, которого не было.
        # Хуже немоты только немота с ложной распиской.
        try:
            document = build(fmt)
        except Exception as legacy_exc:                 # noqa: BLE001
            degraded["v1_render_failed"] = f"{type(legacy_exc).__name__}: {legacy_exc}"[:400]
            _bump("v1_render_failed")
            if log is not None:
                log.error("ни v2, ни v1 не собрались — пишу голый скелет снимка",
                          exc_info=True)
            # Последний пояс: ход важнее полноты снимка. Скелет читается вчерашним
            # читателем, несёт расписку и честно объявляет, что тел в нём нет.
            document = render_v1(authority=base, goal="", conversation="",
                                 history=None, extra="")
    if _SURROGATE_RE.search(document) is not None:
        # Достижимо только по ветке v1: там json-тела не экранируются (побайтовая
        # заморозка вчерашнего формата), а .encode("utf-8") в run_manager.py:486
        # на одиноком суррогате роняет создание рана — то есть делает её немой.
        degraded["lone_surrogate_replaced"] = True
        _bump("lone_surrogate_replaced")
        if log is not None:
            log.warning("снимок записан с деградацией: lone_surrogate_replaced")
        document = _SURROGATE_RE.sub("\ufffd", build(fmt))
    out = Markdown(document)
    out.receipt = receipt_for(fmt) if degraded else None
    return out


def is_v2(markdown: str) -> bool:
    """Развилка форматов — сверка СТРОКИ 2 с константой, и ничего больше.

    У v1 строка 2 всегда пуста (agent.py:8207-8208), а первый гостевой байт
    начинается не раньше строки ~30.  Ни regex по документу, ни разбор JSON, ни
    манифест: развилка стоит ДО первых чужих байтов и внутри sha256.
    """
    a = markdown.find("\n")
    if a < 0:
        return False
    b = markdown.find("\n", a + 1)
    line2 = markdown[a + 1: b if b >= 0 else len(markdown)]
    return bool(_FORMAT_RE.match(line2))


def parse(markdown: str) -> dict[str, object] | None:
    """Один проход слева направо.  ``None`` => не v2 (вызывающий идёт в легаси).

    Возвращает ``{"format": "v2", "seal": str, "seal_ok": bool, <Заголовок>: str}``.
    Тела payload-секций — ДОСЛОВНЫЙ текст; тела json-секций — содержимое фенса
    (как ``match.group(1)`` сегодня), чтобы ``_unique_json_dict`` остался
    единственным разборщиком JSON в этом контуре.

    Ни одного ``re.search`` по документу.  Единственный forward-скан — закрывающий
    фенс json-блоков, и он безопасен: ``json.dumps(indent=2)`` на dict/list
    верхнего уровня даёт всем внутренним строкам отступ >= 2 и голую строку из
    трёх обратных кавычек выдать не может.
    """
    if not is_v2(markdown):
        return None
    lines = markdown.split("\n")
    seal = _FORMAT_RE.match(lines[1]).group(1)

    def need(cond, msg):
        if not cond:
            raise SnapshotFormatError(msg)

    need(lines[0] == TITLE_LINE, "line 1 is not the snapshot title")
    need(lines[1] == format_line(seal), "line 2 is not the exact format line")
    need(len(lines) > 2 and lines[2] == "", "line 3 must be empty")
    i = 3
    out: dict[str, object] = {"format": "v2", "seal": seal, "seal_ok": True}
    for title, kind, required in SECTIONS:
        if i >= len(lines) or lines[i] != "## " + title:
            need(not required, f"missing required section: {title}")
            continue
        i += 1
        need(i < len(lines) and lines[i] == "", f"no blank line under {title}")
        i += 1
        if kind == "json":
            need(i < len(lines) and lines[i] == "```json", f"no json fence in {title}")
            i += 1
            start = i
            while i < len(lines) and lines[i] != "```":
                i += 1
            need(i < len(lines), f"unterminated json fence in {title}")
            out[title] = "\n".join(lines[start:i])
            i += 1
        else:
            need(i < len(lines), f"missing meter for {title}")
            m = _METER_RE.fullmatch(lines[i])
            need(m is not None, f"missing meter for {title}")
            m_seal, nbytes, nlines = m.group(1), int(m.group(2)), int(m.group(3))
            if m_seal != seal:
                out["seal_ok"] = False        # советническое, никогда не структурное
            i += 1
            need(i + nlines <= len(lines), f"meter overruns the file in {title}")
            # strict=True — канонический замок: неканоническая запись куска и
            # неэкранированный суррогат отвергаются здесь, за один проход, без
            # повторного quote() (полный замок стоил бы +1,5 мс на разбор).
            body = unquote("\n".join(lines[i:i + nlines]), strict=True)
            need(body is not None, f"gutter broken in {title}")
            need(ubytes(body) == nbytes, f"meter disagrees with body in {title}")
            out[title] = body
            i += nlines
            need(i < len(lines) and lines[i] == close_line(seal),
                 f"missing sealed close marker for {title}")
            i += 1
        need(i < len(lines) and lines[i] == "", f"no blank line after {title}")
        i += 1
    need(i == len(lines), "trailing bytes after the last section")
    return out


def read(markdown: str, *, log=None) -> dict[str, object] | None:
    """``parse`` с прививкой: сломанный v2 читается легаси-путём, а не роняет ход.

    Все четыре замка формата гостем недостижимы (после дайджест-гейта структуру
    задаём только мы), поэтому строгость разбора бесплатна — но лишь пока она НЕ
    делает её немой: ``_load_exact_run_channel`` стоит на КАЖДОЙ её отправке
    (agent.py:10774 <- mtproto_runner.py:4797/4975).  Мы никогда не хуже вчера.
    """
    try:
        return parse(markdown)
    except SnapshotFormatError:
        if log is not None:
            log.warning("v2-снимок не разобрался — читаю легаси-путём", exc_info=True)
        return None


def authority_document(parsed: dict) -> str:
    """Одна секция власти как самостоятельный документ — вход для ``_RUN_AUTHORITY_RE``.

    Регулярка власти в agent.py не меняется ни на байт; меняется только то, ЧТО
    ей скармливают.  У v2-снимка это восстановленный json-блок, в который не
    попадает ни один байт гостя, — отсюда «ровно один» вместо трёх, и отказ
    исчезает как класс.  Пустое тело даёт невалидный JSON, то есть fail-closed.
    """
    return ("## " + AUTHORITY + "\n\n```json\n"
            + str(parsed.get(AUTHORITY) or "") + "\n```\n")


def authority_scan(markdown: str, *, log=None) -> tuple[dict | None, str]:
    """Пара «разобранный v2 либо None» + «текст, который скармливаем регулярке власти».

    Порядок здесь несущий: дайджест уже сверен вызывающим, формат размечает то,
    что подписал sha256.  Кто когда-нибудь переставит разбор выше хеша ради
    «раннего отказа» — молча снимет неподделываемость.
    """
    parsed = read(markdown, log=log)
    return parsed, markdown if parsed is None else authority_document(parsed)


def section(parsed: dict, title: str) -> str:
    """Тело секции в том виде, в каком его ждёт сегодняшний вызывающий.

    payload-секция -> ДОСЛОВНЫЙ текст: границу уже отмерил метр, и ни ``.strip()``,
    ни нормализации здесь нет.  ``Structured history`` -> снова в фенсе, чтобы
    разбор JSON остался ровно там, где был (agent.py:9709).  Отсутствующая
    необязательная секция -> ``""``, как и у легаси-поиска.
    """
    value = parsed.get(title)
    if not isinstance(value, str):
        return ""
    if title in (AUTHORITY, HISTORY):
        return "```json\n" + value + "\n```"
    return value


def reader(parsed: dict | None, markdown: str, legacy):
    """Один вход для тел секций: ``title -> str``.

    ``parsed is None`` — снимок v1, и его читает переданный вчерашний поиск
    (``agent._snapshot_markdown_section``) со своим ``.strip()``.  Две ветки и две
    семантики дословности живут рядом навсегда: 2254 файла на диске нельзя ни
    переписать, ни осиротить.
    """
    if parsed is None:
        return functools.partial(legacy, markdown)
    return functools.partial(section, parsed)
