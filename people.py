"""
Praxis — структурированный файл человека (память как связный портрет).

Markdown остаётся источником правды, человекочитаемым и правимым руками. Секции:
  ## Кто            — кто это и кем приходится кругу
  ## Характер       — как звучит, какой по характеру (наблюдения, не диагнозы)
  ## Сейчас         — чем сейчас живёт / текущее состояние
  ## Факты          — устойчивые факты: `- [public|private] (sN) текст _(дата)_`
  ## Открытые нити  — незакрытое: `- [ ] текст _(дата)_` (закрытое: `- [x] ...`;
                      спящее (PASS 11.0): `- [~] текст _(дата)_ _(спит до YYYY-MM-DD)_`)

Старый плоский формат (булиты сразу под `# Имя`) читается как ## Факты — миграция мягкая,
файл переписывается в секционный вид при первой же записи.

Модуль не импортирует agent (избегаем цикла) — держит свои крошечные хелперы.
"""

from __future__ import annotations

import os

import datetime as _dt
import re
from pathlib import Path

import memory_provenance

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
PEOPLE_DIR = BASE / "memory" / "people"

WHO, CHARACTER, NOW, FACTS, LOOPS = "Кто", "Характер", "Сейчас", "Факты", "Открытые нити"
LINKS = "Связи"
SECTION_ORDER = [WHO, CHARACTER, NOW, FACTS, LOOPS, LINKS]

# PASS 8.6: алиасы — строка `Алиасы: Егор, Yegor, ...` сразу под заголовком досье.
# Хранится в body под служебным ключом (не секция), render ставит её после `# Имя`.
ALIASES_KEY = "_aliases"
_ALIAS_LINE = re.compile(r"^Алиасы:\s*(.+)$", re.IGNORECASE)

# Automatic participant memory never treats a Telegram display name, username or
# dossier alias as identity.  Only this explicit, numeric header may bind a dossier
# to an authenticated Telegram principal.  It intentionally lives outside prose
# sections so parse/render preserves it without promoting an inferred fact.
TELEGRAM_ID_KEY = "_telegram_id"
_TELEGRAM_ID_PREFIX = re.compile(r"^telegram_id\s*:", re.IGNORECASE)
_TELEGRAM_ID_LINE = re.compile(r"^telegram_id\s*:\s*([1-9][0-9]*)\s*$", re.IGNORECASE)

# PASS 12.0.a: роль (`role: family`) — тоже строка-заголовок. Раньше её роняло parse→render
# (не булит → выпадала из преамбулы при первой же консолидации, и family молча слетал через
# ближайший сон). Теперь round-trip-safe: parse ловит её в _role, render ставит обратно.
ROLE_KEY = "_role"
KNOWN_ROLES = ("family",)  # пока осмысленна одна роль

_BULLET = re.compile(r"^[-*]\s+")


def _today() -> str:
    return _dt.date.today().isoformat()


def _slug(name: str) -> str:
    s = re.sub(r"[^\w\s-]", "", name.strip().lower(), flags=re.UNICODE)
    return re.sub(r"[\s-]+", "-", s) or "unknown"


# --------------------------------------------------------------------------- #
#  Ключ СРАВНЕНИЯ имён (никогда не имя файла)
# --------------------------------------------------------------------------- #
#
# 01.08.2026. Один человек приходил под тремя ярлыками сразу: её словом («Егор»),
# display-именем из Telegram («Yegor Kosyrev») и подписью квитанции
# («Yegor Kosyrev (@tatarskiy_e4pochmak)») — и КАЖДЫЙ заводил своё досье. На живом
# дереве это пять файлов на одного Егора и три на Арета: память о человеке
# расщеплялась ровно там, где он был назван иначе, а не там, где о нём узнали новое.
#
# Ключ складывает написания ОДНОГО имени: снимает хвост-хэндл (это подпись, а не
# имя), диакритику, регистр, переводит кириллицу в латиницу и гасит расхождения
# транслитерации, у которых нет смыслового веса (Ye/E, Kh/H, Ts/C, ё/е).
#
# Чего ключ НЕ делает намеренно: он не сближает РАЗНЫЕ буквы. «Kosyrev» и «Kosyrew» —
# разные написания, и они по-прежнему расходятся в два досье с предложением алиаса в
# дневник (`consolidate._suggest_alias_if_similar`). Молчаливое слияние похожих имён —
# не наше решение: у неё уже стоит стойка «молча НЕ сливаю», и она остаётся в силе.
# Здесь только нормализация написания, того же класса, что casefold и ё→е, которые в
# этом файле уже делались молча.
#
# Ключ используется ТОЛЬКО для поиска уже существующего досье. Имя нового файла
# по-прежнему делает _slug из живого имени — транслит в имена файлов не течёт.

_HANDLE_TAIL = re.compile(r"\s*[(\[]\s*@?[A-Za-z0-9_]{3,32}\s*[)\]]\s*$")

_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    # украинские/белорусские соседи по кругу — тот же класс
    "і": "i", "ї": "i", "є": "e", "ґ": "g", "ў": "u",
})

# Пары, где две латиницы читаются как одна кириллица. Порядок важен: длинные первыми.
# `x`→`ks` — того же класса: «Алекс»/Alex, «Максим»/Maxim. Проверено на живых данных
# (43 досье + 299 строк адресной книги): новых склеек, кроме одной пары вида Alex_*/«Алекс …»,
# правило не даёт.
_TRANSLIT_VARIANTS = (("shch", "sch"), ("kh", "h"), ("ts", "c"),
                      ("yo", "e"), ("jo", "e"), ("ye", "e"), ("je", "e"), ("x", "ks"))


def identity_key(name: str) -> str:
    """Ключ сравнения имён: «Егор», «Yegor», «Yegor Kosyrev (@handle)» → сравнимые строки.

    Пусто, если сравнивать нечего. Никогда не используется как имя файла.
    """
    import unicodedata

    text = _HANDLE_TAIL.sub("", str(name or "").strip())
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.translate(_TRANSLIT)
    for src, dst in _TRANSLIT_VARIANTS:
        text = text.replace(src, dst)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def slug_by_identity(name: str) -> str:
    """Слаг ЕДИНСТВЕННОГО досье с таким же ключом имени ('' — нет или неоднозначно).

    Неоднозначность возвращает пусто: два досье с одинаковым ключом — это уже
    расщепление, и угадывать, в какое дописать, здесь нельзя.
    """
    key = identity_key(name)
    if not key or not PEOPLE_DIR.exists():
        return ""
    matches: list[str] = []
    for path in sorted(PEOPLE_DIR.glob("*.md")):
        if path.stem.startswith("_"):
            continue
        title, body = read(path.stem)
        keys = {identity_key(path.stem.replace("-", " ")), identity_key(title)}
        keys.update(identity_key(a) for a in re.split(r"[,;]", body.get(ALIASES_KEY, "")))
        if key in {k for k in keys if k}:
            matches.append(path.stem)
    return matches[0] if len(matches) == 1 else ""


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""


def path_for(slug: str) -> Path:
    return PEOPLE_DIR / f"{slug}.md"


# PASS 10.10: роль в шапке досье (`role: family`). Назначает ТОЛЬКО владелец — панель
# или его правка файла; её selfdev/самокоммит с такой строкой ловит иммунитет (red).
_ROLE_LINE = re.compile(r"(?im)^role:\s*(\w+)\s*$")


def _normalise_telegram_id(value: object) -> str:
    """Canonical positive Telegram user id, never a chat/name/scope surrogate."""
    raw = str(value or "").strip()
    return raw if re.fullmatch(r"[1-9][0-9]*", raw) else ""


def role(slug: str) -> str:
    """Роль из шапки досье ('' если не задана). Сейчас осмысленна одна: family."""
    m = _ROLE_LINE.search(_read(path_for(slug)))
    return m.group(1).strip().lower() if m else ""


# --------------------------------------------------------------------------- #
#  Парсинг / рендер
# --------------------------------------------------------------------------- #

def parse(text: str) -> tuple[str, dict[str, str]]:
    """-> (имя, {секция: тело}). Преамбульные булиты (старый формат) -> Факты;
    строка `Алиасы: …` (PASS 8.6) -> body[ALIASES_KEY].

    A single canonical ``telegram_id: <positive integer>`` preamble header is
    round-tripped.  Duplicate or malformed headers deliberately produce no binding.
    """
    name = ""
    pre: list[str] = []
    sections: dict[str, list[str]] = {}
    cur: str | None = None
    aliases_line = ""
    role_line = ""
    telegram_id_lines: list[str] = []
    for line in text.splitlines():
        if cur is None and not name and line.startswith("# "):
            name = line[2:].strip()
            continue
        m = _ALIAS_LINE.match(line.strip())
        if m and not aliases_line:
            aliases_line = m.group(1).strip()
            continue
        if cur is None and not role_line:
            mr = _ROLE_LINE.match(line.strip())
            if mr:
                role_line = mr.group(1).strip()
                continue
        if cur is None and _TELEGRAM_ID_PREFIX.match(line.strip()):
            telegram_id_lines.append(line.strip())
            continue
        if line.startswith("## "):
            cur = line[3:].strip()
            sections.setdefault(cur, [])
            continue
        (sections[cur] if cur is not None else pre).append(line)

    body = {k: "\n".join(v).strip() for k, v in sections.items()}
    if aliases_line:
        body[ALIASES_KEY] = aliases_line
    if role_line:
        body[ROLE_KEY] = role_line.lower()
    if len(telegram_id_lines) == 1:
        mt = _TELEGRAM_ID_LINE.fullmatch(telegram_id_lines[0])
        if mt:
            body[TELEGRAM_ID_KEY] = mt.group(1)
    pre_facts = [l for l in pre if _BULLET.match(l.strip())]
    if pre_facts and FACTS not in body:
        body[FACTS] = "\n".join(pre_facts).strip()
    return name, body


def render(name: str, body: dict[str, str]) -> str:
    parts = [f"# {name}", ""]
    hdr: list[str] = []
    if (body.get(ROLE_KEY) or "").strip():
        hdr.append(f"role: {body[ROLE_KEY].strip().lower()}")
    telegram_id = _normalise_telegram_id(body.get(TELEGRAM_ID_KEY))
    if telegram_id:
        hdr.append(f"telegram_id: {telegram_id}")
    if (body.get(ALIASES_KEY) or "").strip():
        hdr.append(f"Алиасы: {body[ALIASES_KEY].strip()}")
    if hdr:
        parts += hdr + [""]
    for title in SECTION_ORDER:
        b = (body.get(title) or "").strip()
        if b:
            parts += [f"## {title}", b, ""]
    # неизвестные секции (рукоправка владельца) — сохраняем как есть; служебные "_"-ключи не секции
    for title, b in body.items():
        if title not in SECTION_ORDER and not title.startswith("_") and (b or "").strip():
            parts += [f"## {title}", b.strip(), ""]
    return "\n".join(parts).rstrip() + "\n"


def read(slug: str) -> tuple[str, dict[str, str]]:
    return parse(_read(path_for(slug)))


def read_text(slug: str) -> str:
    return _read(path_for(slug))


def write(slug: str, name: str, body: dict[str, str]) -> None:
    p = path_for(slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render(name, body), encoding="utf-8")


def set_role(slug: str, name: str, role: str) -> None:
    """PASS 12.0.a: прописать `role:` в шапку досье через parse/render — так, чтобы строка
    пережила консолидацию (создаёт файл, если его ещё нет). Пустой role снимает роль."""
    nm, body = read(slug)
    role = (role or "").strip().lower()
    if role:
        body[ROLE_KEY] = role
    else:
        body.pop(ROLE_KEY, None)
    write(slug, nm or name, body)


def telegram_id(slug: str) -> str:
    """Return one strict dossier binding, or ``""`` when absent/ambiguous.

    The raw preamble is checked instead of trusting the permissive Markdown body:
    duplicate and malformed ``telegram_id:`` lines therefore fail closed even if
    another parse/render consumer could otherwise normalise the file later.
    """
    text = _read(path_for(slug))
    header_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            break
        stripped = line.strip()
        if _TELEGRAM_ID_PREFIX.match(stripped):
            header_lines.append(stripped)
    if len(header_lines) != 1:
        return ""
    match = _TELEGRAM_ID_LINE.fullmatch(header_lines[0])
    return match.group(1) if match else ""


def set_telegram_id(slug: str, name: str, principal_id: str | int | None) -> None:
    """Set/remove an explicit dossier-to-Telegram binding through parse/render.

    This is a low-level persistence helper, analogous to :func:`set_role`; its
    caller remains responsible for owner authorization.  A non-empty invalid id is
    rejected instead of silently weakening or changing the binding.
    """
    raw = "" if principal_id is None else str(principal_id).strip()
    canonical = _normalise_telegram_id(raw)
    if raw and not canonical:
        raise ValueError("principal_id must be a positive Telegram user id")
    if canonical and PEOPLE_DIR.exists():
        owners = [
            path.stem for path in sorted(PEOPLE_DIR.glob("*.md"))
            if not path.stem.startswith("_") and path.stem != slug
            and telegram_id(path.stem) == canonical
        ]
        if owners:
            raise ValueError(
                f"telegram principal {canonical} is already bound to {', '.join(owners)}"
            )
    nm, body = read(slug)
    if canonical:
        body[TELEGRAM_ID_KEY] = canonical
    else:
        body.pop(TELEGRAM_ID_KEY, None)
    write(slug, nm or name, body)


def slug_for_principal(principal_id: str | int | None) -> str:
    """Resolve an authenticated Telegram principal by an exact unique binding.

    Display names, usernames, filenames and aliases never participate.  Duplicate
    bindings are authority ambiguity, so neither dossier wins.
    """
    canonical = _normalise_telegram_id(principal_id)
    if not canonical or not PEOPLE_DIR.exists():
        return ""
    matches: list[str] = []
    for path in sorted(PEOPLE_DIR.glob("*.md")):
        if path.stem.startswith("_") or telegram_id(path.stem) != canonical:
            continue
        name, _body = read(path.stem)
        if name:
            matches.append(path.stem)
    return matches[0] if len(matches) == 1 else ""


def slug_for_owner_name(name: str) -> str:
    """Resolve one owner-authored known-person label to a unique dossier.

    This is migration/navigation, not Telegram authority: callers must already have an
    authenticated principal and the label from ``known_ids.json``.  Ambiguity returns
    empty instead of silently choosing a similarly named person.
    """
    key = _slug(name)
    if not key or not PEOPLE_DIR.exists():
        return ""
    matches: list[str] = []
    for path in sorted(PEOPLE_DIR.glob("*.md")):
        if path.stem.startswith("_"):
            continue
        title, body = read(path.stem)
        candidates = {path.stem, _slug(title)}
        candidates.update(_slug(value) for value in re.split(",|;", body.get(ALIASES_KEY, "")))
        if key in {candidate for candidate in candidates if candidate}:
            matches.append(path.stem)
    return matches[0] if len(matches) == 1 else ""


def bind_known_principal(principal_id: str | int | None, known_name: str) -> str:
    """Idempotently migrate an owner-rooted known-id label into a dossier binding."""
    canonical = _normalise_telegram_id(principal_id)
    if not canonical:
        return ""
    existing = slug_for_principal(canonical)
    if existing:
        return existing
    slug = slug_for_owner_name(known_name)
    if not slug:
        return ""
    title, _body = read(slug)
    set_telegram_id(slug, title or known_name, canonical)
    return slug


# --------------------------------------------------------------------------- #
#  Нормализация для дедупа
# --------------------------------------------------------------------------- #

def fact_body(line: str) -> str:
    """Голый текст факта без visibility/salience/даты/пометок."""
    s = _BULLET.sub("", line.strip())
    s = re.sub(r"^\[(public|private)\]\s*", "", s, flags=re.I)
    s = re.sub(r"^\(s[123]\)\s*", "", s)
    s = re.sub(r"\s*~~устарело~~.*$", "", s)
    s = re.sub(r"\s*\[superseded.*?\]\s*$", "", s)
    s = re.sub(r"\s*\[source:[^\]]+\]\s*$", "", s, flags=re.I)
    s = re.sub(r"\s*_\(.*?\)_\s*$", "", s)
    return s.strip().lower()


def _loop_key(line: str) -> str:
    s = re.sub(r"^[-*]\s*\[[ xX~]\]\s*", "", line.strip())
    s = re.sub(r"\s*_\(.*?\)_\s*$", "", s)
    s = re.sub(r"\s*_\(.*?\)_\s*$", "", s)  # спящая нить несёт две метки: _(дата)_ _(спит до …)_
    return s.strip().lower()


# --------------------------------------------------------------------------- #
#  Факты
# --------------------------------------------------------------------------- #

def append_fact(slug: str, name: str, fact: str, visibility: str = "public", salience: int = 2,
                source_ref: str = "") -> bool:
    """Дописать факт в ## Факты (дедуп по тексту). -> True, если файл создан заново.

    PASS 19: ``source_ref`` — короткий claim/provenance id. Он остаётся читаемым в Markdown,
    но не участвует в дедупе текста факта.
    """
    visibility = "private" if str(visibility).lower().startswith("priv") else "public"
    try:
        salience = int(salience)
    except (TypeError, ValueError):
        salience = 2
    if salience not in (1, 2, 3):
        salience = 2

    nm, body = read(slug)
    fresh = not nm and not body
    nm = nm or name
    facts = body.get(FACTS, "")
    key = fact.strip().lower()
    for l in facts.splitlines():
        if _BULLET.match(l.strip()) and "~~устарело~~" not in l and fact_body(l) == key:
            return fresh  # дубль — не плодим
    source = re.sub(r"[^\w:.-]", "", str(source_ref or ""))[:120]
    bullet = f"- [{visibility}] (s{salience}) {fact.strip()} _({_today()})_"
    if source:
        bullet += f" [source:{source}]"
    body[FACTS] = (facts + "\n" + bullet).strip() if facts else bullet
    write(slug, nm, body)
    return fresh


def mark_superseded(slug: str, match: str) -> bool:
    """Пометить устаревшим факт, содержащий match (не удаляя)."""
    nm, body = read(slug)
    facts = body.get(FACTS, "")
    if not facts or not match.strip():
        return False
    mk = match.strip().lower()
    out, changed = [], False
    for l in facts.splitlines():
        s = l.strip()
        if _BULLET.match(s) and "~~устарело~~" not in s and mk in s.lower():
            out.append(l.rstrip() + f"  ~~устарело~~ [superseded {_today()}]")
            changed = True
        else:
            out.append(l)
    if changed:
        body[FACTS] = "\n".join(out)
        write(slug, nm, body)
    return changed


# --------------------------------------------------------------------------- #
#  Открытые нити
# --------------------------------------------------------------------------- #

def open_loops(slug: str) -> list[str]:
    _, body = read(slug)
    return [
        re.sub(r"^[-*]\s*\[ \]\s*", "", l.strip())
        for l in (body.get(LOOPS, "")).splitlines()
        if l.strip().startswith(("- [ ]", "* [ ]"))
    ]


def add_open_loop(slug: str, name: str, text: str) -> bool:
    """Добавить незакрытую нить (дедуп). -> True, если добавили."""
    if not text.strip():
        return False
    nm, body = read(slug)
    nm = nm or name
    loops = body.get(LOOPS, "")
    key = _loop_key(text)
    for l in loops.splitlines():
        if _loop_key(l) == key:
            return False
    bullet = f"- [ ] {text.strip()} _({_today()})_"
    body[LOOPS] = (loops + "\n" + bullet).strip() if loops else bullet
    write(slug, nm, body)
    return True


def close_open_loop(slug: str, match: str) -> bool:
    """Пометить нить закрытой `[x]` (не удаляя)."""
    nm, body = read(slug)
    loops = body.get(LOOPS, "")
    if not loops or not match.strip():
        return False
    mk = _loop_key(match)
    out, changed = [], False
    for l in loops.splitlines():
        s = l.strip()
        if s.startswith(("- [ ]", "* [ ]")) and mk and mk in _loop_key(s):
            out.append(re.sub(r"\[ \]", "[x]", l, count=1).rstrip() + f" _(закрыто {_today()})_")
            changed = True
        else:
            out.append(l)
    if changed:
        body[LOOPS] = "\n".join(out)
        write(slug, nm, body)
    return changed


# --- PASS 11.0: парковка — нить спит до даты или до нового разговора ---------- #

PARKED_UNTIL = re.compile(r"\s*_\(спит до (\d{4}-\d{2}-\d{2})\)_")
# PASS 16: счётчик снов — метка живёт на ПРОСНУВШЕЙСЯ строке. Нить, которая спала
# дважды и всплыла снова, — жвачка: третью парковку tool_manage_loop не принимает
# (инцидент 09.07: «доставка ответа по трём идеям» перепарковывалась каждые сутки).
SLEPT_N = re.compile(r"\s*_\(спала ×(\d+)\)_")


def park_count(slug: str, match: str) -> int:
    """Сколько раз ОТКРЫТАЯ нить уже спала (метка _(спала ×N)_). Нет нити/метки — 0."""
    _nm, body = read(slug)
    mk = _loop_key(match)
    for l in (body.get(LOOPS, "") or "").splitlines():
        s = l.strip()
        if s.startswith(("- [ ]", "* [ ]")) and mk and mk in _loop_key(s):
            m = SLEPT_N.search(l)
            return int(m.group(1)) if m else 0
    return 0


def park_loop(slug: str, match: str, until: str) -> bool:
    """Усыпить нить: `[ ]` -> `[~] … _(спит до даты)_` (решение «отложить»).
    until — ISO-дата; мусор -> False. -> нашлась ли нить."""
    try:
        until_d = _dt.date.fromisoformat(str(until or "").strip())
    except (TypeError, ValueError):
        return False
    nm, body = read(slug)
    loops = body.get(LOOPS, "")
    if not loops or not match.strip():
        return False
    mk = _loop_key(match)
    out, changed = [], False
    for l in loops.splitlines():
        s = l.strip()
        if s.startswith(("- [ ]", "* [ ]")) and mk and mk in _loop_key(s):
            out.append(re.sub(r"\[ \]", "[~]", l, count=1).rstrip()
                       + f" _(спит до {until_d.isoformat()})_")
            changed = True
        else:
            out.append(l)
    if changed:
        body[LOOPS] = "\n".join(out)
        write(slug, nm, body)
    return changed


def unpark_loops(slug: str, only_expired: bool = False) -> int:
    """Разбудить спящие нити: `[~]` -> `[ ]`, метка сна снимается.
    only_expired — только с истёкшей датой ([~] без даты — сирота, будим всегда).
    -> сколько разбудила."""
    nm, body = read(slug)
    loops = body.get(LOOPS, "")
    if not loops:
        return 0
    today = _today()
    out, n = [], 0
    for l in loops.splitlines():
        s = l.strip()
        if s.startswith(("- [~]", "* [~]")):
            m = PARKED_UNTIL.search(l)
            if not only_expired or m is None or m.group(1) <= today:
                m2 = SLEPT_N.search(l)          # PASS 16: сон посчитан на строке
                slept = (int(m2.group(1)) if m2 else 0) + 1
                l = PARKED_UNTIL.sub("", re.sub(r"\[~\]", "[ ]", l, count=1))
                l = SLEPT_N.sub("", l).rstrip() + f" _(спала ×{slept})_"
                n += 1
        out.append(l)
    if n:
        body[LOOPS] = "\n".join(out)
        write(slug, nm, body)
    return n


def loops_stats() -> dict:
    """Сводка нитей по всем людям: {open, parked, oldest_open_days|None} — для STATE."""
    today = _dt.date.today()
    open_n = parked = 0
    oldest: int | None = None
    date_re = re.compile(r"_\((\d{4}-\d{2}-\d{2})\)_")
    for p in sorted(PEOPLE_DIR.glob("*.md")):
        if p.stem.startswith("_"):
            continue
        _, body = parse(_read(p))
        for l in (body.get(LOOPS, "")).splitlines():
            s = l.strip()
            if s.startswith(("- [~]", "* [~]")):
                parked += 1
            elif s.startswith(("- [ ]", "* [ ]")):
                open_n += 1
                m = date_re.search(s)
                if m:
                    try:
                        age = (today - _dt.date.fromisoformat(m.group(1))).days
                        oldest = age if oldest is None else max(oldest, age)
                    except ValueError:
                        pass
    return {"open": open_n, "parked": parked, "oldest_open_days": oldest}


# --------------------------------------------------------------------------- #
#  Алиасы (PASS 8.6): «Егор» и «yegor-kosyrev» — один узел графа/резолва
# --------------------------------------------------------------------------- #

def aliases(slug: str) -> list[str]:
    """Список алиасов из строки `Алиасы: …` досье (пусто, если нет)."""
    _, body = read(slug)
    raw = body.get(ALIASES_KEY, "")
    return [a.strip() for a in raw.split(",") if a.strip()]


def add_alias(slug: str, alias: str, name: str = "") -> bool:
    """Дописать алиас в досье (дедуп по casefold). -> добавили ли."""
    alias = (alias or "").strip()
    if not alias:
        return False
    nm, body = read(slug)
    nm = nm or name or slug
    cur = [a.strip() for a in (body.get(ALIASES_KEY, "")).split(",") if a.strip()]
    if alias.casefold() in {a.casefold() for a in cur} or alias.casefold() == nm.casefold():
        return False
    body[ALIASES_KEY] = ", ".join(cur + [alias])
    write(slug, nm, body)
    return True


# --------------------------------------------------------------------------- #
#  Связи (рёбра графа памяти; markdown — источник правды, graph.py — обход)
# --------------------------------------------------------------------------- #

_LINK_LINE = re.compile(
    r"^[-*]\s*\[\[([^\]\n]+)\]\]\s*(?:[—–:-]\s*)?(.*?)\s*(?:_\((\d{4}-\d{2}-\d{2})\)_)?\s*$"
)


def links(slug: str) -> list[tuple[str, str]]:
    """[(slug_другого, подпись-связи), …] из ## Связи. Подпись может быть пустой."""
    _, body = read(slug)
    out: list[tuple[str, str]] = []
    for l in (body.get(LINKS, "")).splitlines():
        m = _LINK_LINE.match(l.strip())
        if m:
            out.append((_slug(m.group(1)), (m.group(2) or "").strip()))
    return out


def add_link(slug: str, name: str, dst_slug: str, label: str = "") -> bool:
    """Дописать связь `- [[dst]] — label _(дата)_` в ## Связи (дедуп по dst+label). -> добавили ли."""
    dst_slug = _slug(dst_slug)
    if not dst_slug or dst_slug == slug:
        return False
    nm, body = read(slug)
    nm = nm or name
    key = (dst_slug, (label or "").strip().lower())
    for d, lab in links(slug):
        if (d, lab.lower()) == key:
            return False
    line = f"- [[{dst_slug}]]" + (f" — {label.strip()}" if (label or "").strip() else "") + f" _({_today()})_"
    cur = body.get(LINKS, "")
    body[LINKS] = (cur + "\n" + line).strip() if cur else line
    write(slug, nm, body)
    return True


# --------------------------------------------------------------------------- #
#  Прозаичные секции (портрет) + срез для бюджета контекста
# --------------------------------------------------------------------------- #

def set_prose(slug: str, name: str, *, who: str | None = None,
              character: str | None = None, now: str | None = None) -> None:
    nm, body = read(slug)
    nm = nm or name
    if who is not None:
        body[WHO] = who.strip()
    if character is not None:
        body[CHARACTER] = character.strip()
    if now is not None:
        body[NOW] = now.strip()
    write(slug, nm, body)


def _trusted_claim_facts(slug: str, include_private: bool = True) -> list[dict]:
    """Current person-claim projections rendered from claims, never dossier text."""
    name, _ = read(slug)
    # Aliases are navigation aids, never automatic identity authority.  Claims may
    # bind to the canonical dossier slug or to a unique canonical dossier title;
    # an ambiguous title fails closed just like a duplicate telegram_id binding.
    names = {_slug(slug)}
    title_key = _slug(name) if name else ""
    if title_key:
        title_owners = []
        for path in sorted(PEOPLE_DIR.glob("*.md")) if PEOPLE_DIR.exists() else []:
            if path.stem.startswith("_"):
                continue
            other_name, _other_body = read(path.stem)
            if other_name and _slug(other_name) == title_key:
                title_owners.append(path.stem)
        if title_owners == [slug]:
            names.add(title_key)
    claims = BASE / "memory" / "life" / "claims"
    evidence = memory_provenance.claim_evidence_index(BASE / "memory")
    rows: list[dict] = []
    for path in sorted(claims.glob("*.md")) if claims.exists() else []:
        declared, meta = memory_provenance.claim_source(path, evidence_index=evidence)
        if (declared not in memory_provenance.AUTOMATIC_CLAIM_KINDS
                or not meta.get("_automatic_eligible")
                or meta.get("kind") != "person"
                or _slug(str(meta.get("subject") or "")) not in names
                or (meta.get("visibility") == "private" and not include_private)):
            continue
        _projection, effective, eligible = memory_provenance.claim_projection(meta)
        if not eligible:
            continue
        rows.append({
            "id": str(meta.get("id") or ""), "text": str(meta.get("_statement") or ""),
            "visibility": str(meta.get("visibility") or "private"),
            "salience": int(meta.get("salience") or 1),
            "updated_at": str(meta.get("updated_at") or ""), "source_type": effective,
        })
    rows.sort(key=lambda row: (row["salience"], row["updated_at"], row["id"]), reverse=True)
    return rows


def portrait_and_loops(slug: str, include_private: bool = True, *,
                       trusted_only: bool = False) -> tuple[str, str]:
    """(портрет без нитей, текст открытых нитей) — для раздельного бюджета в промпте.

    ``include_private=False`` remains an explicit projection for external/legacy readers.
    Praxis's live prompt calls the full or compact form with private labels retained and
    enforces disclosure at the outbound advisor. Прозу (Кто/Характер/Сейчас) считаем публичной.
    """
    name, body = read(slug)
    if trusted_only:
        facts = _trusted_claim_facts(slug, include_private)
        rendered = []
        if name:
            rendered.append(f"# {name}")
        if facts:
            rendered.append("## Canonical claim projections")
            for row in facts:
                label = memory_provenance.claim_prompt_label(row["source_type"])
                rendered.append(
                    f"- [{row['visibility']}] [{label}] {row['text']} [source:{row['id']}]"
                )
        return "\n".join(rendered), ""
    prose = []
    for t in (WHO, CHARACTER, NOW, FACTS):
        b = (body.get(t) or "").strip()
        if not b:
            continue
        if t == FACTS and not include_private:
            b = "\n".join(l for l in b.splitlines()
                          if not re.match(r"^\s*[-*]\s*\[private\]", l, re.I)).strip()
            if not b:
                continue
        prose.append(f"## {t}\n{b}")
    loops_lines = [l for l in (body.get(LOOPS, "")).splitlines() if l.strip().startswith(("- [ ]", "* [ ]"))]
    return "\n\n".join(prose), "\n".join(loops_lines).strip()


def compact_profile(slug: str, *, include_private: bool = True,
                    max_facts: int = 4, max_chars: int = 700,
                    trusted_only: bool = False) -> str:
    """A short, labelled participant card for the live system prompt.

    Markdown remains canonical.  This is only a bounded projection: durable prose,
    the strongest active facts, and at most one open thread.  Visibility markers are
    deliberately retained so the outbound advisor can distinguish private material
    from facts that are safe to repeat.
    """
    name, body = read(slug)
    if not name and not body:
        return ""

    def clip(value: str, cap: int = 180) -> str:
        value = re.sub(r"\s+", " ", str(value or "")).strip()
        return value if len(value) <= cap else value[: cap - 1].rstrip() + "…"

    rows = [name or slug]
    if trusted_only:
        trusted = _trusted_claim_facts(slug, include_private)
        for row in trusted[:max(1, int(max_facts))]:
            label = memory_provenance.claim_prompt_label(row["source_type"])
            rows.append(
                f"[{row['visibility']}] [{label}] {clip(row['text'], 210)} "
                f"[source:{row['id']}]"
            )
        # A name alone adds no memory beyond the authenticated live speaker label.
        if len(rows) == 1:
            return ""
        out = " | ".join(rows)
        return out if len(out) <= max_chars else out[: max_chars - 1].rstrip() + "…"
    for title, label in ((WHO, "кто"), (NOW, "сейчас"), (CHARACTER, "характер")):
        value = clip(body.get(title, ""))
        if value:
            rows.append(f"{label} [legacy-unverified]: {value}")

    facts = []
    for order, raw in enumerate((body.get(FACTS, "") or "").splitlines()):
        line = raw.strip()
        if not _BULLET.match(line) or "~~устарело~~" in line:
            continue
        private = bool(re.match(r"^[-*]\s*\[private\]", line, re.I))
        if private and not include_private:
            continue
        sal = re.search(r"\(s([123])\)", line, re.I)
        score = int(sal.group(1)) if sal else 1
        source = re.search(r"\[source:([^\]]+)\]\s*$", line, re.I)
        clean = re.sub(r"^[-*]\s*", "", line)
        clean = re.sub(r"\s*_\(\d{4}-\d{2}-\d{2}\)_\s*", " ", clean)
        clean = re.sub(r"\s*\[source:[^\]]+\]\s*$", "", clean, flags=re.I)
        clean = ((f"[source:{source.group(1)}] " + clean) if source else
                 f"[legacy-unverified] {clean}")
        facts.append((score, order, clip(clean, 210)))
    # Salience first; among equal facts the newest (later Markdown line) wins.
    facts.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if facts:
        rows.append("факты: " + "; ".join(item[2] for item in facts[:max(1, int(max_facts))]))

    loops = open_loops(slug)
    if loops:
        rows.append("нить [legacy-unverified]: " + clip(loops[-1], 180))
    out = " | ".join(rows)
    return out if len(out) <= max_chars else out[: max_chars - 1].rstrip() + "…"
