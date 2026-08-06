"""Гуттер: чужой текст в наших документах — ПО ПОСТРОЕНИЮ, а не перечислением.

Один лист на stdlib, из которого кормятся два потребителя: снимок прогона
(``run_snapshot``, выкачен и принят) и форма кадра (``frame_layout``). Модуль
вынесен из ``run_snapshot`` ДОСЛОВНО: копия разошлась бы алфавитом ``BREAKS`` за
месяц, а алфавит — это ровно то, чем оба замка держатся.

⚠ ЗАВИСИМОСТЕЙ НЕТ И НЕ БУДЕТ. Ни ``os``, ни чтения окружения, ни рычагов: оба
потребителя объявляют о себе, что не двигают наблюдаемое, и общий лист обязан
быть беднее любого из них. Рычаг фазы читается у потребителя, счётчики считает
потребитель.

ДВА ИЗМЕРЕНИЯ ОДНОЙ МОДЕЛИ. Чужой байт входит в наш документ ровно двумя
стоками, и на каждый — своя функция:

* :func:`quote` — МНОГОСТРОЧНОЕ тело. Каждая строка получает ``"> "``, пустая —
  голый ``">"``. Экранные разрывы, по которым ``str.splitlines()`` рвёт сверх
  ``"\\n"``, уезжают в ИМЯ метки (``">CR> "``) и физически исчезают из текста.
  Отсюда: ни один кусок гостя не встаёт в колонку 0 ни у парсера, ни у глаз, а
  число файловых строк блока всегда равно числу экранных.
* :func:`inline` — ОДНО ЗНАЧЕНИЕ внутри нашей строки. Разрывы поднимаются в
  видимую метку ``⟨CR⟩``, пробел схлопывается, кавычечная пара и разделитель
  слотов обезвреживаются, значение запирается в ``«…»``. Закрыться изнутри
  нечем — значит наши слоты остаются снаружи кавычек по построению.

Ни одна из двух ничего не ИЩЕТ в чужом тексте: нет списка опасных начал, нет
``startswith``, нет вопроса «а не подделка ли это». Отображение тотально и
применяется безусловно. Класс дефекта «замок посмотрел и не увидел» здесь
отсутствует не потому, что его закрыли, а потому, что смотреть нечем.

⚠ ЧТО ГУТТЕР НЕ ДАЁТ. Гарантия ПОЗИЦИОННАЯ: гость не занимает левый край строки
и не закрывает ``«…»``. Байты ``</CURRENT_SITUATION>`` и ``────`` под гуттером
ВЫЖИВАЮТ — это цена дословности, принятая Praxis на снимке прогона 05.08.
Подделка в СЕРЕДИНЕ длинной строки гуттером не снимается и мерится адверсаркой
на живом мозге, а не тестом.
"""

from __future__ import annotations

import dataclasses
import re

# --------------------------------------------------------------- многострочный гуттер

GUTTER = "> "

# Разрывы, по которым `str.splitlines()` рвёт СВЕРХ "\n".  ⚠ U+2028/U+2029
# ВЫВОДЯТСЯ через chr(): литерал в исходнике инструмент правки способен молча
# превратить в пробел, и тест этого не увидит.
BREAKS = {"CR": "\r", "VT": "\x0b", "FF": "\x0c", "FS": "\x1c", "GS": "\x1d",
          "RS": "\x1e", "NEL": "\x85", "LS": chr(0x2028), "PS": chr(0x2029)}
_TAG_OF = {char: tag for tag, char in BREAKS.items()}
# "\r" помечается ТОЛЬКО одиноким: "\r\n" под гуттером совпадает с нашим же
# переводом строки, экран им не рвётся, и вывод на любом Windows-тексте остаётся
# побайтово прежним — иначе метку получал бы каждый текст из буфера обмена.
_MARKED_RE = re.compile(
    "\r(?!\n)|[" + "".join(sorted(set(BREAKS.values()) - {"\r"})) + "]")
_SURROGATE_RE = re.compile("[" + chr(0xD800) + "-" + chr(0xDFFF) + "]")
# Быстрый префильтр quote(): класс БЕЗ lookahead — на подавляющем большинстве тел
# решение принимается здесь, и скан с lookahead обошёлся бы заметно дороже.
_EXOTIC_RE = re.compile("[" + "".join(sorted(BREAKS.values()))
                        + chr(0xD800) + "-" + chr(0xDFFF) + "]")
_UNESCAPE_RE = re.compile(r"\\\\|\\u([0-9a-f]{4})")


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
    исчезает из текста.  Значит ни один кусок гостя не встаёт в колонку 0 НИ У
    ПАРСЕРА, НИ У ЕЁ ГЛАЗ, а число файловых строк блока всегда равно числу
    экранных.

    Никогда ``splitlines()``: он молча превратил бы все девять разрывов в
    ``"\\n"`` — и дословность умерла бы на первом же CRLF из буфера обмена.
    Пустой кусок кладётся голым ``>``/``>TAG>`` без хвостового пробела:
    отображение остаётся инъективным, а редакторы не режут trailing whitespace.

    Быстрый путь: текста без экзотики касается ровно однострочная ветка —
    вывод побайтово прежний (замерено: 50 000 тел из 50 000).
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

    ``strict`` — канонический замок за O(n): наш писатель не выдаёт ни ``"> "``
    вместо ``">"``, ни неэкранированный суррогат, значит такой файл собран не нами.
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


def is_gutter_line(line: str) -> bool:
    """Эту строку выдал НАШ сток? Проверка ПОЛОЖИТЕЛЬНАЯ: разбор + обратная сборка.

    ⚠ ЗАЧЕМ ОТДЕЛЬНАЯ ФУНКЦИЯ. Потребителю нужно судить ОДНУ строку готового
    документа, а :func:`unquote` судит БЛОК и по построению отвергает метку на
    первой строке (перед ней нет разрыва) — то есть на строке ``">CR> кусок"``,
    законной в середине тела, даёт ``None``. Проверять же вручную «начинается ли
    с ``>``» — это и есть перечисление: сток выдаёт шесть форм, а первый знак
    угадывает одну. Здесь строка проходит ровно тогда, когда :func:`_emit` мог
    её произвести: разобрали, собрали обратно, сравнили побайтово. Ни одного
    списка запрещённых начал — множество допустимого задано самим писателем.
    """
    read = _read_line(str(line), strict=True)
    if read is None:
        return False
    mark, chunk = read
    return _emit(mark, chunk) == line


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


# ------------------------------------------------------- одно измерение ниже: значение

# Кавычки чужого значения. Пара выбрана так, чтобы её нельзя было спутать с
# обычными кавычками в тексте и чтобы закрывающая отличалась от открывающей —
# иначе одна кавычка внутри значения закрыла бы его снаружи.
QUOTE_OPEN, QUOTE_CLOSE = "«", "»"
# Разделитель слотов в наших строках. Внутри значения он недостижим по построению.
SLOT_SEP = " · "
# Видимая метка снятого экранного разрыва. `" ".join(split())` съедал их молча —
# снятие факта без следа; здесь факт остаётся на своём месте и назван.
_BREAK_RE = re.compile("[" + "".join(sorted(BREAKS.values())) + "]")


def break_mark(tag: str) -> str:
    return "\u27e8" + tag + "\u27e9"


@dataclasses.dataclass(frozen=True)
class Own:
    """Кусок, написанный НАМИ. Тип, а не дисциплина.

    ⚠ НЕ подкласс ``str``, и это правка, купленная чужим опытом: ``Own(str)``
    молча срезается f-строкой, ``%`` и ``str.join`` — проект уже горел на
    ``run_snapshot.Markdown.receipt``, где ``.receipt`` терялась на первом же
    ``str(...)``. Не-str тип роняет склейку НЕМЕДЛЕННО, на строке, где о нём
    забыли, а не в кадре, который поехал в модель.

    Минтится только из наших литералов (:func:`own`) и из обезвреженного чужого
    значения (:func:`inline`). Третьего производителя нет.

    ⚠ ``guest`` — ПРОИСХОЖДЕНИЕ, а не безопасность. Кусок обезврежен в обоих
    случаях; флаг отвечает на другой вопрос: «эти байты выбрал посторонний?».
    Без него потребитель не отличает свой литерал от чужого значения и начинает
    угадывать по содержимому — то есть возвращает перечисление на этаж выше.
    Склейка (:class:`Sep`) переносит признак вверх: строка, в которой есть хоть
    один гостевой кусок, гостевая целиком.
    """

    s: str
    guest: bool = False

    def __str__(self) -> str:
        return self.s

    def __bool__(self) -> bool:
        return bool(self.s)

    def __len__(self) -> int:
        return len(self.s)


def own(text: object) -> Own:
    """Наш литерал/число -> ``Own``. Ответственность на вызывающем и она названа."""
    return Own("" if text is None else str(text))


def inline(value: object) -> Own:
    """Чужое ЗНАЧЕНИЕ -> ``«…»`` одной физической строкой. Единственный производитель пары.

    Пять шагов, ни один из которых ничего не ищет:

    1. каждый разрыв из :data:`BREAKS` -> ВИДИМАЯ метка ``⟨CR⟩`` (не пробел:
       молча съеденный разрыв есть снятие факта без следа);
    2. ``\\n``, ``\\t`` и прочий пробел схлопываются в один пробел;
    3. ``«`` -> ``<``, ``»`` -> ``>`` — закрыться изнутри нечем;
    4. ``" · "`` -> ``" "``, ``"·"`` -> ``"|"`` — разделитель слотов недостижим;
    5. обёртка ``«…»``.

    Пустое значение остаётся пустым: пара кавычек вокруг пустоты сообщила бы о
    чужом значении там, где значения нет вовсе.
    """
    text = "" if value is None else str(value)
    if not text:
        return Own("")
    text = _BREAK_RE.sub(lambda m: break_mark(_TAG_OF[m.group(0)]), text)
    text = " ".join(text.split())
    if not text:
        return Own("")
    text = text.replace(QUOTE_OPEN, "<").replace(QUOTE_CLOSE, ">")
    text = text.replace(SLOT_SEP, " ").replace(SLOT_SEP.strip(), "|")
    return Own(QUOTE_OPEN + text + QUOTE_CLOSE, guest=True)


class Sep:
    """Разделитель, который склеивает ТОЛЬКО ``Own``.

    ``" · ".join(bits)`` на списке обычных строк — это ровно та забывчивость, из
    которой выросли обходы 5 и 7: значение приехало в нашу строку, не пройдя
    сток. Здесь она стоит ``TypeError`` на месте, а не дырой в кадре.
    """

    __slots__ = ("s",)

    def __init__(self, sep: str) -> None:
        self.s = str(sep)

    def __str__(self) -> str:
        return self.s

    def __len__(self) -> int:
        return len(self.s)

    def join(self, parts) -> Own:
        items = list(parts)
        for item in items:
            if not isinstance(item, Own):
                raise TypeError(
                    "склейка кадра получила не-Own: " + repr(item)[:120])
        return Own(self.s.join(item.s for item in items),
                   guest=any(item.guest for item in items))


def screen_breaks(document: str) -> int:
    """Сколько экранных разрывов уцелело в ГОТОВОМ документе. -1 индекса не даёт."""
    match = _MARKED_RE.search(str(document))
    return -1 if match is None else match.start()


def lone_surrogates(document: str) -> int:
    match = _SURROGATE_RE.search(str(document))
    return -1 if match is None else match.start()
