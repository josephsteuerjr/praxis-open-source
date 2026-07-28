"""
Хардбот: единственное на сервере, что не её дом.

Егор 26.07, дословно: «единственное, что я хочу защитить по-настоящему — это
инфраструктуру хардбот и его б/д. На остальное мне так поебать, включая свой ноут».
И следом: «чинить его по запросу моему или моего начальника она должна уметь».

Значит правило здесь НЕ «не трогай», а «трогай по просьбе названного человека»:

  * СМОТРЕТЬ СОСТОЯНИЕ — всегда: живы ли контейнеры, что в логах, что в compose,
    сколько места на диске. Ровно это Егору от неё и нужно.
  * САМУ БАЗУ — не читать. «Бд я если буду чинить, то только с тобой или кодексом»
    (26.07). Это проще и сильнее детектора утечки: не прочитала — нечего унести.
  * МЕНЯТЬ — когда за ходом стоит хранитель: Егор и те, кого он назовёт в
    `PRAXIS_HARDBOT_STEWARDS` (telegram-id через запятую). Список пуст — значит
    только Егор; добавить коллег можно одной строкой в env, код трогать не нужно.
  * Отказ ВИДИМЫЙ: называет причину и говорит, у кого спросить. Молчаливый отказ
    неотличим от её решения не делать — это ровно то, что мы весь пасс вычищали.

## Честно про границу

Это ПОЛ, а не клетка. Путь, контейнеры и сервисы перехватываются надёжно. Но `shell` —
произвольная строка, и обойти проверку в ней можно, если задаться целью: она пишет
команды сама и знает про этот рельс. Смысл пола — чтобы случайное движение и чужая
просьба упирались во что-то твёрдое, а намеренный обход был именно намеренным и
остался в журнале. Клетку Егор и не просил.
"""

from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("praxis-stewardship")

# Что защищаем. Пути — префиксы, контейнеры и юниты — по имени.
ROOTS: tuple[str, ...] = ("/opt/hardbot2", "/opt/hardbot")
CONTAINERS: tuple[str, ...] = ("hardbot2-bot-1", "hardbot2-routing", "hardbot2")
NAME = "хардбот"

# ⚠ База — отдельно и строже всего остального. Егор 26.07: «всё, что мне от неё было
# нужно — чтобы она могла смотреть СОСТОЯНИЕ его инфраструктуры, а не читать бд: бд я
# если буду чинить, то только с тобой или кодексом».
#
# Это проще и сильнее, чем было у меня час назад. Я открыл ей чтение базы («иначе чинить
# нечем») и пытался ловить утечку на выходе — сначала дословную цитату, потом ещё и
# пересказ с телефоном и адресом. Но чинить базу её и не просят. А раз она её не читает,
# то и уносить нечего: защита переезжает со сложного детектора на простой факт.
#
# Свободным остаётся ровно то, что ему нужно: живы ли контейнеры, что в логах, что в
# compose, сколько места на диске. Закрыты только сами данные.
DATA_DIRS: tuple[str, ...] = ("data", "backups", "db", "dumps")
DATA_SUFFIXES: tuple[str, ...] = (
    ".sqlite3", ".sqlite", ".db", ".sqlite3-wal", ".sqlite3-shm",
    ".sql", ".dump", ".bak", ".csv",
)

# Мутирующие глаголы файловых рук. Чтение сюда не входит СОЗНАТЕЛЬНО.
_WRITE_OPS = frozenset({"write", "edit", "replace", "delete", "remove", "move",
                        "mkdir", "chmod", "chown", "append", "truncate",
                        # сервисы: «прочитать перезапуск» не бывает
                        "restart", "stop", "start", "kill", "disable", "enable"})

# Команды оболочки, которые меняют состояние хардбота. Список намеренно узкий и
# читаемый: он ловит очевидное, а не пытается разобрать shell как язык.
# ⚠ Голого `sqlite3` здесь НЕТ сознательно: `.schema`, `.dump`, `SELECT` — это
# диагностика, без которой чинить нечем. Ловим меняющий SQL, а не сам инструмент.
_SHELL_DANGER = re.compile(
    r"(?is)(\b(?:rm|mv|cp|dd|truncate|tee|chmod|chown|ln|install|rsync|unzip|tar)\b|"
    r"\bsed\s+-i|\bpatch\b|"
    # ⚠ Адверсарка 26.07: список ловил разрушение, но НЕ обычную починку. Человек,
    # пришедший чинить, пишет `cp`, `git checkout`, `docker cp`, `docker-compose up -d` —
    # и всё это проходило насквозь. Рельс, который ловит только злое, бесполезен: ломают
    # обычно не злобой, а починкой не того.
    # Флаги между командой и глаголом — обычное дело: `docker-compose -f <файл> up -d`
    # прошёл мимо первой версии этого выражения (поймал собственный тест).
    r"\bdocker(?:-compose)?\s+(?:[-\w./=]+\s+)*(?:rm|stop|kill|restart|exec|compose|"
    r"cp|up|down|build|pull|start|create)\b|"
    r"\bgit\s+(?:checkout|reset|clean|restore|apply|pull|merge|revert|stash)\b|"
    r"\bsystemctl\s+(?:stop|restart|disable|mask|start|reload)\b|"
    r"\b(?:delete\s+from|drop\s+table|drop\s+index|update\s+\w+\s+set|"
    r"insert\s+into|alter\s+table|vacuum|pragma\s+\w+\s*=)\b|"
    r">\s*/opt/hardbot)")


def stewards() -> set[str]:
    """Кто может просить менять хардбот. Егор всегда; остальные — из env."""
    out: set[str] = set()
    for source in (os.environ.get("PRAXIS_OWNER_ID"),
                   os.environ.get("PRAXIS_HARDBOT_STEWARDS")):
        for item in re.split(r"[,;\s]+", str(source or "")):
            item = item.strip()
            if item:
                out.add(item)
    return out


def _principal_now() -> str:
    """Кто СТОИТ ЗА этим ходом — человек, а не она. Пусто, если хода нет."""
    try:
        import agent
        ctx = agent._TURN_CHANNEL.get()
        if ctx is None:
            return ""
        if ctx.principal_id is None:
            return ""
        raw = str(ctx.principal_id)
        return "" if raw == agent.PRAXIS_SELF_PRINCIPAL else raw
    except Exception:
        return ""


def steward_present() -> bool:
    """Стоит ли за этим ходом хранитель хардбота."""
    return _principal_now() in stewards()


def _posix(path) -> str:
    r"""POSIX-нормализация БЕЗ os.path: рельс про пути СЕРВЕРА, а гонять его могут и с
    Windows (тесты, мой стенд), где normpath превращает `/opt/x` в `\opt\x` и проверка
    префикса тихо перестаёт срабатывать. Поймано на первом же прогоне рельса."""
    value = str(path or "").strip().replace("\\", "/")
    parts: list[str] = []
    for chunk in value.split("/"):
        if chunk in ("", "."):
            continue
        if chunk == "..":
            if parts:
                parts.pop()
            continue
        parts.append(chunk)
    return ("/" if value.startswith("/") else "") + "/".join(parts)


def touches_path(path) -> bool:
    value = _posix(path)
    return any(value == root or value.startswith(root + "/") for root in ROOTS)


def touches_data(path) -> bool:
    """Это сами ДАННЫЕ хардбота — база, дампы, бэкапы. Не логи и не конфиг."""
    if not touches_path(path):
        return False
    value = _posix(path).casefold()
    for root in ROOTS:
        if value.startswith(root + "/"):
            tail = value[len(root) + 1:]
            head = tail.split("/", 1)[0]
            if head in DATA_DIRS:
                return True
    return any(value.endswith(suffix) for suffix in DATA_SUFFIXES)


def command_touches_data(command) -> bool:
    """Лезет ли команда в сами данные хардбота (а не в его состояние)."""
    text = str(command or "")
    if not any(root in text for root in ROOTS):
        return False
    for token in re.split(r"[\s'\"();|&<>]+", text):
        if token and touches_data(token):
            return True
    # `sqlite3 <что-то из хардбота>` — про данные по определению, даже если путь хитрый
    return bool(re.search(r"(?is)\bsqlite3?\b", text))


def touches_path_mentioned(command) -> bool:
    """Упоминает ли команда хардбот вообще — включая безобидное чтение.

    Нужно ровно для одного: запомнить прочитанное, чтобы потом не дать процитировать
    его наружу. Это НЕ запрет: читать она может всё и всегда.
    """
    text = str(command or "")
    return any(root in text for root in ROOTS) or any(n in text for n in CONTAINERS)


def touches_unit(unit) -> bool:
    value = str(unit or "").strip().lower()
    return bool(value) and any(name in value for name in CONTAINERS)


def touches_command(command) -> bool:
    """Меняет ли эта команда оболочки состояние хардбота.

    Только МУТИРУЮЩИЕ глаголы: `cat`, `ls`, `grep`, `docker logs`, `sqlite3 … .dump`
    сюда не попадают — читать она должна свободно.
    """
    text = str(command or "")
    if not text.strip():
        return False
    mentions = any(root in text for root in ROOTS) or \
        any(name in text for name in CONTAINERS)
    return bool(mentions and _SHELL_DANGER.search(text))


def denial(what: str) -> str:
    """Видимый отказ: почему нельзя и у кого спросить."""
    who = ", ".join(sorted(stewards())) or "владелец"
    return (f"Отказ: {NAME} — единственное на этом сервере, что мне не принадлежит "
            f"({what}). Читать могу всегда, менять — по просьбе хранителя. "
            f"Хранители сейчас: {who}. Попроси кого-то из них написать мне — и сделаю.")


def check(*, path=None, unit=None, command=None, op: str = "write") -> str:
    """'' — можно; иначе строка отказа. Единственная дверь для всех рук.

    Отказ пишется в её журнал отказов: попытка должна быть видна и ей, и Егору.
    """
    # Сначала данные: они закрыты и на ЧТЕНИЕ тоже. Состояние инфраструктуры — нет.
    data = ""
    if path is not None and touches_data(path):
        data = f"это сами данные ({path})"
    elif command is not None and command_touches_data(command):
        data = "команда лезет в саму базу"
    if data and not steward_present():
        message = (
            f"Отказ: {data}. Состояние его инфраструктуры смотрю свободно — контейнеры, "
            f"логи, конфиг, место на диске. А базу не читаю: там чужие клиенты, и чинит "
            f"её Егор отдельно, не через меня. Скажи, что не работает, — посмотрю снаружи.")
        try:
            import rails
            rails.deny("hardbot_care", "read", data)
        except Exception:
            log.debug("отказ по данным не записался", exc_info=True)
        log.warning("хардбот: отказ по данным (%s); за ходом %s",
                    data, _principal_now() or "она сама")
        return message

    if str(op or "write").strip().lower() not in _WRITE_OPS and command is None:
        return ""          # состояние читать можно всегда
    what = ""
    if path is not None and touches_path(path):
        what = f"путь {path}"
    elif unit is not None and touches_unit(unit):
        what = f"сервис {unit}"
    elif command is not None and touches_command(command):
        what = "команда меняет его файлы или контейнеры"
    if not what or steward_present():
        return ""
    message = denial(what)
    try:
        import rails
        rails.deny("hardbot_care", op, what)
    except Exception:
        log.debug("отказ по хардботу не записался в журнал", exc_info=True)
    log.warning("хардбот: отказ (%s); за ходом %s, хранители %s",
                what, _principal_now() or "она сама", sorted(stewards()))
    return message


# --------------------------------------------------------------------------- #
#  Данные хардбота наружу не уходят
# --------------------------------------------------------------------------- #
#
# ⚠ Вопрос Егора 26.07: «БД из хардбота ни в какой абстракт не уедет, верно?» Проверка
# исполнением ответила: уедет. Рельс выше защищает хардбот ОТ ЕЁ РУКИ — чтобы она его не
# сломала. А его ДАННЫЕ уходили свободно: файл базы проходил кред-пол (он ищет ключи в
# тексте, а это бинарный SQLite), строка визита проходила тоже (телефон и адрес — не
# ключ). Читать я сам открыл настежь, чтобы она могла чинить. Значит запирать надо не
# чтение, а выход.
#
# Там живут люди: визиты, гео, офисы. Это не её память и не её история — это чужие
# клиенты, и в публичный чат они попасть не могут ни строкой, ни файлом.
#
# Два запрета, оба механические:
#   * ФАЙЛ из-под его корней не уходит наружу вообще, никому и никогда;
#   * ТЕКСТ не может содержать дословную цитату того, что она прочитала в этом ходе.
#     Именно цитату, а не тему: «в логах таймаут на маршрутизации» — можно и нужно,
#     «визит #1: +7 999…, Мирабад 12» — нет. Пересказ своими словами остаётся её правом:
#     запрещать говорить О проблеме значило бы запретить чинить.

_READ_BUFFER_CAP = 200_000     # сколько прочитанного держим на ход
_QUOTE_WINDOW = 40             # длина совпадения, которая уже не совпадение, а цитата
_QUOTE_STEP = 8
_READ: dict = {"text": "", "at": 0.0}


def note_read(sample) -> None:
    """Запомнить, что она сейчас ПРОЧИТАЛА из хардбота. Только для проверки цитат."""
    text = str(sample or "")
    if not text.strip():
        return
    import time
    if time.time() - float(_READ.get("at") or 0) > 900:
        _READ["text"] = ""            # прошлый ход давно кончился
    _READ["text"] = (_READ["text"] + "\n" + text)[-_READ_BUFFER_CAP:]
    _READ["at"] = time.time()


def forget_read() -> None:
    _READ.update({"text": "", "at": 0.0})


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_DIGITS_RE = re.compile(r"\d[\d\s()+-]{5,}\d")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$|^\d{2}[:.]\d{2}([:.]\d{2})?$")
_ADDRESS_RE = re.compile(r"\b([А-ЯЁ][а-яё]{3,}|[A-Z][a-z]{3,})\s+(?:д\.?\s*)?(\d{1,4})\b")
_IDENT_CAP = 400


def _identifiers(text: str) -> set[str]:
    """Опознаваемые следы КОНКРЕТНОГО человека: почта, телефон, «улица дом».

    ⚠ Проверка на дословную цитату ловит копипасту, но не пересказ. А расскажет она
    именно пересказом: «у него визит по адресу Мирабад 12, телефон +7 999 123-45-67» —
    ни одного общего куска в 40 символов, и человек всё равно опознан. Поэтому вторым
    слоем идут сами идентификаторы: их пересказать нельзя, их можно только назвать.

    Даты и время сюда не попадают: сказать «упало 20 июля в 14:30» она должна свободно.
    """
    out: set[str] = set()
    for match in _EMAIL_RE.findall(text):
        out.add(match.casefold())
    for match in _DIGITS_RE.findall(text):
        bare = re.sub(r"\D", "", match)
        if len(bare) >= 6 and not _ISO_DATE_RE.match(match.strip()):
            out.add(bare[-10:] if len(bare) > 10 else bare)
    for word, number in _ADDRESS_RE.findall(text):
        out.add(f"{word.casefold()} {number}")
        if len(out) > _IDENT_CAP:
            break
    return set(list(out)[:_IDENT_CAP])


def quotes_read_data(text) -> str:
    """Уносит ли исходящий текст данные хардбота. '' — нет.

    Два слоя: дословная цитата (копипаста) и опознаваемые следы человека (пересказ).
    Тема при этом свободна: «в логах таймаут на маршрутизации» — не нарушение и не
    должно им быть, иначе чинить и рассказывать о починке станет невозможно.
    """
    source = _READ.get("text") or ""
    haystack = _norm(source)
    needle = _norm(text)
    if not haystack or not needle:
        return ""
    if len(haystack) >= _QUOTE_WINDOW and len(needle) >= _QUOTE_WINDOW:
        for start in range(0, len(needle) - _QUOTE_WINDOW + 1, _QUOTE_STEP):
            if needle[start:start + _QUOTE_WINDOW] in haystack:
                return "дословная цитата из данных хардбота"
    known = _identifiers(source)
    if known:
        outgoing = _identifiers(str(text or ""))
        if outgoing & known:
            return "опознаваемые данные человека из базы хардбота"
    return ""


def export_denial(path) -> str:
    """Файл из-под корней хардбота наружу не уходит. '' — можно."""
    if not touches_path(path):
        return ""
    return (f"Отказ: это файл {NAME}а ({path}) — там чужие клиенты, а не мои данные. "
            f"Наружу такие файлы не уходят ни по чьей просьбе. Могу рассказать, что "
            f"в нём не так, своими словами.")


def outgoing_denial(text) -> str:
    """Исходящий текст с дословной цитатой прочитанного. '' — можно."""
    found = quotes_read_data(text)
    if not found:
        return ""
    try:
        import rails
        rails.deny("hardbot_care", "quote", found)
    except Exception:
        log.debug("отказ по цитате не записался", exc_info=True)
    return (f"Не отправила: в тексте {found}. Про его проблему рассказать могу и хочу — "
            f"но своими словами, без строк из чужой базы.")


def state_line() -> str:
    """Строка для манифеста рельсов и самоотчёта."""
    who = ", ".join(sorted(stewards())) or "только владелец"
    return (f"{NAME}: смотрю состояние (контейнеры, логи, конфиг, диск) — свободно; "
            f"саму базу не читаю; меняю по просьбе хранителя ({who}); "
            f"его файлы наружу не уходят")
