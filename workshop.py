"""
Praxis — мастерская: настоящие кодинг-руки вместо heredoc-увечий (PASS 8.5).

Всё в её контейнере — новых привилегий НЕТ, только точность:
  * проекты: workspace/projects/<slug>/ — СОБСТВЕННЫЙ git у каждого (main-репо их
    игнорирует: workspace/ в .gitignore), README с брифом, venv, квота 200 МБ;
  * файловые тулы: fs_read (номера строк), fs_write (только НОВЫЙ файл),
    fs_edit (точное уникальное вхождение — главный апгрейд против heredoc),
    fs_search / fs_ls;
  * зоны: читать весь дом; имя файла и gitignore сами по себе ничего не скрывают
    с shell); писать — workspace/** soul/** memory/**; файлы ЯДРА — только внутри
    worktree предложения (proposal_id), иначе отказ «ядро — через предложение»;
  * run/run_tests/pip_install — subprocess с потолками вывода и таймаутами;
  * send_file — документ в текущий Telegram-чат (мост _TELETHON);
  * code_map — ast-карта кода (проверяемое «как я устроена»), кэш по mtime;
  * code_outline — скелет одного файла (PASS 16.5).

PASS 16.3: рельсы этой мастерской продублированы в компилируемом полу (`hands/`,
бинарь praxis-hands). Если он собран — путь/зоны/таймауты/потолки решает ОН,
и её правки этого файла пола не двигают. Не собран — всё работает как раньше, той же
семантикой. Одна семантика, две реализации; расхождение ловят тесты.

Модель здесь не зовётся. agent импортируется лениво (тулы регистрируются в agent).
"""

from __future__ import annotations

import ast
import datetime as _dt
import hashlib
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import hands
import rails
import selfdev

log = logging.getLogger("praxis-workshop")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
REPO = Path(__file__).resolve().parent
PROJECTS = BASE / "workspace" / "projects"
INBOX = BASE / "workspace" / "inbox"
INBOX_FILE_MB = int(os.getenv("PRAXIS_INBOX_FILE_MB", "2048") or 2048)
INBOX_TOTAL_MB = int(os.getenv("PRAXIS_INBOX_TOTAL_MB", "10240") or 10240)

QUOTA_MB = int(os.getenv("PRAXIS_PROJECT_QUOTA_MB", "200") or 200)
RUN_TIMEOUT = 120
PIP_TIMEOUT = 300
RUN_OUT_CAP = 8000          # потолок вывода run/pip (голова+хвост)
READ_CAP_LINES = 400        # потолок fs_read за раз
SEARCH_CAP = 60             # строк-совпадений fs_search
# 04.08: потолки ОБХОДА, а не только попаданий. fs_search("10 млн", glob="**/*",
# root="/app/memory") не вернулся 4.5 часа: SEARCH_CAP ограничивает совпадения, а обход
# дерева не ограничивал никто. Ход держал целое ядро, память росла, а прогон качался
# running<->paused 230 раз. Потолки читаются из окружения — это её рычаги, а не мой забор:
# 0 в любом из них снимает соответствующий предел совсем.
SEARCH_SCAN_CAP = int(os.getenv("PRAXIS_SEARCH_SCAN_CAP", "40000") or 0)   # путей за обход
SEARCH_SECONDS = float(os.getenv("PRAXIS_SEARCH_SECONDS", "45") or 0)      # секунд на поиск
MAP_CAP = 14000             # символов code_map

# Метасимволы, которые вправду делают строку регуляркой. `re.escape` с Python 3.7 экранирует
# ещё и ПРОБЕЛ (и `#`, `&`, `~`), поэтому проверка «литерал == re.escape(литерал)» объявляла
# нелитеральным любой запрос из двух слов — и он проваливался мимо быстрого бинарного пути
# с его потолками в медленный питоновский обход. Проверяем то, что действительно значимо.
_REGEX_META = re.compile(r"[\\^$.|?*+()\[\]{}]")

WRITE_ZONES = ("workspace", "soul", "memory")   # как у shell: её дом для записи
# Совместимость с генератором Rust-пола и старыми тестовыми импортами. После решения
# владельца имя файла больше никогда не делает его секретом.
_SECRET_NAME = re.compile(r"(?!)")
_SKIP_DIRS = {".git", ".venv", "__pycache__", ".proposals", "node_modules", ".vectors"}


def _slug(name: str) -> str:
    s = re.sub(r"[^\w\s-]", "", str(name or "").strip().lower(), flags=re.UNICODE)
    return re.sub(r"[\s-]+", "-", s) or "project"


def _is_secret(p: Path) -> bool:
    return False


def _resolve_read(path: str) -> Path | None:
    """Путь для чтения внутри дома (BASE или REPO — в проде это одно)."""
    raw = str(path or "").strip()
    if not raw:
        return None
    p = Path(raw)
    cand = p if p.is_absolute() else (BASE / p)
    try:
        cand = cand.resolve()
    except OSError:
        return None
    for root in (BASE, REPO):
        try:
            cand.relative_to(root.resolve())
            break
        except ValueError:
            continue
    else:
        return None
    return cand


def _resolve_write(path: str, proposal_id: str = "") -> tuple[Path | None, str]:
    """Путь для записи. С proposal_id корень = worktree предложения (правка ядра);
    без него — только workspace/soul/memory. -> (Path|None, причина отказа)."""
    raw = str(path or "").strip()
    if not raw:
        return None, "пустой путь"
    if proposal_id:
        wt = selfdev.worktree_path(str(proposal_id).strip())
        if not wt.exists():
            return None, f"нет worktree предложения {proposal_id} — открой start_proposal"
        p = Path(raw)
        cand = p if p.is_absolute() else (wt / p)
        try:
            cand = cand.resolve()
            cand.relative_to(wt.resolve())
        except (OSError, ValueError):
            return None, f"путь вне worktree предложения {proposal_id}"
        return cand, ""
    p = Path(raw)
    cand = p if p.is_absolute() else (BASE / p)
    try:
        cand = cand.resolve()
        rel = cand.relative_to(BASE.resolve())
    except (OSError, ValueError):
        return None, "путь вне дома"
    first = rel.parts[0] if rel.parts else ""
    if first not in WRITE_ZONES:
        return None, ("ядро — через предложение: start_proposal → правки в его worktree "
                      "(передай proposal_id) → submit_proposal. Без него пишу только в "
                      "workspace/, soul/, memory/.")
    return cand, ""


# --------------------------------------------------------------------------- #
#  Telegram inbox — все документы допущенных групп/личек, разложенные по чатам
# --------------------------------------------------------------------------- #

def inbox_safe_name(name: str) -> str:
    """Санитизация имени файла: только базовое имя, без путей/спецсимволов, с расширением."""
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip().strip(".")
    base = re.sub(r"[^\w.\-]+", "_", base, flags=re.UNICODE).strip("._")
    return base[:80] or "file"


def _inbox_chat_directory(*, scope: str = "", chat_id: str | int | None = None,
                          chat_kind: str = "", chat_label: str = "") -> Path:
    """Stable, human-readable Telegram inbox directory.

    The first level is an audience kind (``groups`` or ``private``), never a
    privacy tier such as owner/family/unknown.  The second level identifies one
    concrete conversation, so files with the same name from different chats do
    not collide and a human can find them without reversing a hash.
    """
    if chat_id is None and not scope and not chat_kind:
        return INBOX  # legacy/local callers keep the flat inbox
    raw_kind = str(chat_kind or "").strip().lower()
    if raw_kind in ("group", "groups") or (not raw_kind and str(scope).lower() == "group"):
        bucket = "groups"
    else:
        bucket = "private"
    label = re.sub(r"[^\w.-]+", "_", str(chat_label or "").strip(), flags=re.UNICODE)
    label = label.strip("._")[:48]
    ident = re.sub(r"[^\w-]+", "_", str(chat_id or "unknown"), flags=re.UNICODE)
    ident = ident.strip("_")[:48] or hashlib.sha256(
        str(chat_id or "unknown").encode("utf-8")).hexdigest()[:12]
    room = f"{label}_{ident}" if label else f"chat_{ident}"
    return INBOX / bucket / room[:96]


def inbox_accept(name: str, size_bytes: int, day: str | None = None, *,
                 scope: str = "", chat_id: str | int | None = None,
                 chat_kind: str = "", chat_label: str = "") -> tuple[Path | None, str]:
    """Судьба входящего Telegram-файла: (путь для сохранения, '') или честная причина.

    По умолчанию поддерживает старый плоский inbox. Живой раннер передаёт тип/имя/chat id и
    получает ``groups/<чат>`` либо ``private/<личка>``: одноимённые файлы разных разговоров
    не смешиваются, а каталог остаётся понятным человеку.
    Физические дисковые капы широкие и настраиваемые env."""
    size_mb = max(0, int(size_bytes or 0)) / (1024 * 1024)
    if size_mb > INBOX_FILE_MB:
        return None, f"файл {size_mb:.0f}МБ больше лимита {INBOX_FILE_MB}МБ — не качаю"
    used = _du_mb(INBOX) if INBOX.exists() else 0.0
    if used + size_mb > INBOX_TOTAL_MB:
        return None, (f"квота inbox исчерпана ({used:.0f}МБ из {INBOX_TOTAL_MB}МБ) — "
                      "не качаю, пора прибраться в workspace/inbox")
    stamp = day or _dt.date.today().strftime("%Y%m%d")
    safe = inbox_safe_name(name)
    directory = _inbox_chat_directory(scope=scope, chat_id=chat_id,
                                      chat_kind=chat_kind, chat_label=chat_label)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stamp}_{safe}"
    n = 2
    while path.exists():  # коллизии — честный суффикс, не перезапись
        stem, dot, ext = safe.partition(".")
        path = directory / f"{stamp}_{stem}_{n}{dot and '.'}{ext}"
        n += 1
    return path, ""


def _resolve_inbox(path: str) -> Path | None:
    """Resolve a read-only path strictly inside ``workspace/inbox``."""
    raw = str(path or "").strip()
    if not raw:
        return INBOX.resolve()
    p = Path(raw)
    cand = p if p.is_absolute() else BASE / p
    try:
        cand = cand.resolve()
        cand.relative_to(INBOX.resolve())
    except (OSError, ValueError):
        return None
    return cand


def inbox_list(path: str = "") -> str:
    """Read-only listing exposed in every Telegram channel."""
    p = _resolve_inbox(path)
    if p is None or not p.is_dir():
        return "Нет такой папки внутри workspace/inbox."
    rows = []
    for child in sorted(p.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold())):
        mark = "/" if child.is_dir() else ""
        size = "" if child.is_dir() else f" · {child.stat().st_size}Б"
        rows.append(f"{child.relative_to(BASE).as_posix()}{mark}{size}")
    return "\n".join(rows[:250]) or "(пусто)"


def inbox_read(path: str, start: int = 0, end: int = 0) -> str:
    """Read a text-like Telegram attachment without granting general filesystem hands."""
    p = _resolve_inbox(path)
    if p is None:
        return "Не читаю: путь должен быть внутри workspace/inbox."
    if not p.is_file():
        return f"Нет файла {path}."
    try:
        sample = p.read_bytes()[:4096]
    except OSError as exc:
        return f"Не прочитала файл: {type(exc).__name__}."
    if b"\x00" in sample:
        return (f"{p.relative_to(BASE).as_posix()}: бинарный файл, {p.stat().st_size}Б. "
                "Текстовым просмотрщиком его не разбираю.")
    return fs_read(p.relative_to(BASE).as_posix(), start=start, end=end)


def _du_mb(path: Path) -> float:
    total = 0
    for root, dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total / (1024 * 1024)


def _cap_output(out: str, cap: int = RUN_OUT_CAP) -> str:
    if len(out) <= cap:
        return out
    head, tail = out[: int(cap * 0.6)], out[-int(cap * 0.35):]
    return f"{head}\n… (вырезано {len(out) - len(head) - len(tail)} символов) …\n{tail}"


# --------------------------------------------------------------------------- #
#  Проекты
# --------------------------------------------------------------------------- #

def _project_dir(name: str) -> Path | None:
    slug = _slug(name)
    d = PROJECTS / slug
    return d if d.is_dir() else None


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=60)


def project_create(name: str, brief: str = "") -> str:
    slug = _slug(name)
    d = PROJECTS / slug
    if d.exists():
        return f"Проект {slug} уже есть — project_status(\"{slug}\")."
    d.mkdir(parents=True)
    (d / "README.md").write_text(
        f"# {name}\n\n{(brief or '').strip() or '(бриф не задан)'}\n\n"
        f"_создан {_dt.date.today().isoformat()} мастерской Praxis_\n", encoding="utf-8")
    r = _git(d, "init", "-q")
    if r.returncode == 0:
        _git(d, "add", "-A")
        subprocess.run(["git", "-C", str(d), "-c", "user.name=Praxis", "-c", "user.email=praxis@local",
                        "commit", "-q", "-m", f"init: {name}"], capture_output=True, text=True, timeout=60)
    log.info("project_create %s", slug)
    return f"Проект создан: workspace/projects/{slug} (свой git, README с брифом)."


def project_list() -> str:
    if not PROJECTS.is_dir():
        return "Проектов пока нет — project_create(имя, бриф)."
    out = []
    for d in sorted(PROJECTS.iterdir()):
        if d.is_dir():
            out.append(f"- {d.name} · {_du_mb(d):.1f} МБ")
    return "Проекты:\n" + "\n".join(out) if out else "Проектов пока нет."


def project_status(name: str) -> str:
    d = _project_dir(name)
    if d is None:
        return f"Нет проекта {_slug(name)} — см. project_list()."
    size = _du_mb(d)
    st = _git(d, "status", "--porcelain", "-b")
    stat = st.stdout.strip() if st.returncode == 0 else "(git недоступен)"
    quota = f" ⚠ квота {QUOTA_MB} МБ превышена!" if size > QUOTA_MB else ""
    return f"{d.name}: {size:.1f} МБ{quota}\n{stat}"


# --------------------------------------------------------------------------- #
#  Файловые руки
# --------------------------------------------------------------------------- #

def sniff_image_mime(head: bytes) -> str | None:
    """Магия первых байтов → mime картинки (PNG/JPEG/WebP/GIF) или None."""
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def fs_probe_image(path: str) -> tuple[str, str, int] | None:
    """(resolved, mime, size), если файл — картинка; None — не картинка или гарды против.

    PASS 30.0.c. Гарды ровно те же, что у fs_read (компилируемый пол, затем дом): при
    любом отказе возвращаем None, чтобы вызвавший ушёл в обычный fs_read и получил
    честный текст отказа тем же путём. Никакого собственного решения о доступе здесь нет."""
    try:
        g = hands.guard(path, op="read", base=BASE)
        if g is not None and not g.get("ok"):
            return None
        p = _resolve_read(path)
        if p is None or not p.is_file():
            return None
        with p.open("rb") as fh:
            head = fh.read(16)
        mime = sniff_image_mime(head)
        if mime is None:
            return None
        return str(p), mime, p.stat().st_size
    except Exception:
        log.debug("fs_probe_image упал — считаю не-картинкой", exc_info=True)
        return None


def fs_read(path: str, start: int = 0, end: int = 0) -> str:
    # 16.3+: решение «можно ли читать» — у компилируемого пола, если он собран.
    # Строже двух реализаций побеждает: отказ бинаря режет даже то, что питон бы пустил.
    g = hands.guard(path, op="read", base=BASE)
    if g is not None and not g.get("ok"):
        return f"Не читаю: {g.get('msg') or 'рельсы против'}"
    p = _resolve_read(path)
    if p is None:
        rails.deny("hands_floor", "fs_read", f"вне дома: {str(path)[:120]}")
        return "Не читаю: путь вне дома. Для хоста используй Forge scope='host'."
    if not p.is_file():
        return f"Нет файла {path}."
    lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    s = max(1, int(start) or 1)
    e = min(len(lines), int(end) or (s + READ_CAP_LINES - 1))
    body = "\n".join(f"{i:>5}\t{lines[i - 1]}" for i in range(s, e + 1))
    tail = f"\n… (в файле {len(lines)} строк; дальше — fs_read(start={e + 1}))" if e < len(lines) else ""
    return body + tail if body else "(пустой файл)"


SHRINK_GUARD_RATIO = 0.7    # PASS 16.5: новый текст < 70% старого = похоже на усечение


def _wt_root(proposal_id: str) -> str:
    """Корень worktree предложения строкой (для бинаря) или '' — обычный дом."""
    return str(selfdev.worktree_path(str(proposal_id).strip())) if proposal_id else ""


def fs_write(path: str, content: str, proposal_id: str = "", overwrite: bool = False,
             force: bool = False) -> str:
    """Создать файл; overwrite=True — осознанно переписать существующий.

    PASS 16.5: перезапись возможна, но под гардом усечения (новый текст меньше 70%
    старого → отказ, force=true снимает). PASS 16.3: если собран компилируемый пол —
    решение принимает он (та же семантика, рельсы вне самоизменяемого питона)."""
    r = hands.write(path, content, root=_wt_root(proposal_id), overwrite=overwrite,
                    force=force, base=BASE)
    if r is not None:
        if r.get("ok"):
            log.info("fs_write(hands) %s%s", path, f" [proposal {proposal_id}]" if proposal_id else "")
        return r.get("msg") or ("Записала." if r.get("ok") else "Не пишу.")

    p, err = _resolve_write(path, proposal_id)
    if p is None:
        rails.deny("hands_floor", "fs_write", f"{err}: {str(path)[:100]}")
        return f"Не пишу: {err}"
    if p.exists():
        if not overwrite:
            return (f"Файл {path} уже существует — правь точечно через fs_edit, "
                    "или передай overwrite=true, если правда хочешь переписать целиком.")
        if not force:
            old_len = len(p.read_text(encoding="utf-8", errors="ignore"))
            if old_len > 0 and len(content) < old_len * SHRINK_GUARD_RATIO:
                pct = round(len(content) / old_len * 100)
                rails.deny("hands_floor", "fs_write", f"shrink-guard {pct}%: {str(path)[:100]}")
                return (f"Стоп: новый текст {path} — {pct}% от старого ({old_len} → {len(content)} симв.). "
                        "Похоже на случайное усечение. Точечно — fs_edit; если перезапись "
                        "осознанная — force=true.")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    log.info("fs_write %s (%d байт)%s", path, len(content.encode("utf-8", "ignore")),
             f" [proposal {proposal_id}]" if proposal_id else "")
    return f"Записала {path} ({len(content)} симв.)."


def fs_edit(path: str, old: str, new: str, proposal_id: str = "") -> str:
    """Точная правка: `old` должен встречаться РОВНО один раз. 0 или >1 — отказ с подсказкой.

    PASS 16.5: при >1 совпадении называем номера строк первых вхождений — меньше слепых
    итераций. PASS 16.3: при собранном поле правку делает бинарь (одна семантика)."""
    r = hands.edit(path, old, new, root=_wt_root(proposal_id), base=BASE)
    if r is not None:
        if r.get("ok"):
            log.info("fs_edit(hands) %s%s", path, f" [proposal {proposal_id}]" if proposal_id else "")
        return r.get("msg") or ("Поправила." if r.get("ok") else "Не правлю.")

    p, err = _resolve_write(path, proposal_id)
    if p is None:
        rails.deny("hands_floor", "fs_edit", f"{err}: {str(path)[:100]}")
        return f"Не правлю: {err}"
    if not p.is_file():
        return f"Нет файла {path} — новый файл создаётся fs_write."
    if not old:
        return "Пустой old — так не правлю (для нового файла есть fs_write)."
    text = p.read_text(encoding="utf-8", errors="ignore")
    n = text.count(old)
    if n == 0:
        return ("Вхождение не найдено (0 совпадений). Проверь точный текст через fs_read — "
                "пробелы/табуляция/кавычки должны совпадать байт в байт.")
    if n > 1:
        pos, start = [], 0
        for _ in range(min(n, 5)):
            i = text.index(old, start)
            pos.append(f"строка {text[:i].count(chr(10)) + 1}")
            start = i + 1
        return (f"Неоднозначно: {n} совпадений (первые: {', '.join(pos)}). Расширь old "
                "несколькими строками контекста вокруг нужного места, чтобы вхождение "
                "стало уникальным.")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    log.info("fs_edit %s (-%d +%d симв.)%s", path, len(old), len(new),
             f" [proposal {proposal_id}]" if proposal_id else "")
    return f"Поправила {path}: 1 вхождение заменено."


def code_outline(path: str) -> str:
    """Скелет python-файла: классы/функции со строками. Дешёвая ориентация вместо
    чтения 2000 строк ради одной функции (PASS 16.5). Бинарь — если собран, иначе ast."""
    r = hands.outline(path, base=BASE)
    if r is not None and r.get("ok"):
        items = r.get("items") or []
        if not items:
            return f"{path}: ни одного класса или функции."
        rows = ["  " * (int(i.get("indent", 0)) // 4) + f"{i['kind']} {i['name']}  · строка {i['line']}"
                for i in items]
        return f"{path} — скелет ({len(items)}):\n" + "\n".join(rows[:400])
    if r is not None:
        return r.get("msg") or "Не смотрю."

    import ast
    p = _resolve_read(path)
    if p is None or not p.is_file():
        return f"Нет файла {path} (или он вне дома)."
    try:
        tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError as e:
        return f"{path}: не разбирается как python ({e.msg}, строка {e.lineno})."
    rows: list[str] = []

    def walk(node, depth: int) -> None:
        for child in getattr(node, "body", []):
            if isinstance(child, ast.ClassDef):
                rows.append("  " * depth + f"class {child.name}  · строка {child.lineno}")
                walk(child, depth + 1)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "async def" if isinstance(child, ast.AsyncFunctionDef) else "def"
                rows.append("  " * depth + f"{kind} {child.name}  · строка {child.lineno}")

    walk(tree, 0)
    if not rows:
        return f"{path}: ни одного класса или функции."
    return f"{path} — скелет ({len(rows)}):\n" + "\n".join(rows[:400])


def fs_search(pattern: str, glob: str = "**/*.py", root: str = "") -> str:
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"Плохой regex: {e}"
    # 16.3+: чистый литерал (без regex-метасимволов) ищет бинарь — рельсы и расписка его.
    # Его диалект маски: `**/имя` или просто `имя`; поддиректорию в маске оставляем питону.
    mask = glob.replace("**/", "", 1)
    if (pattern and not _REGEX_META.search(pattern) and not pattern.startswith("-")
            and "/" not in mask and "\\" not in mask):
        r = hands.search(pattern, glob=glob, root=root or "", cap=SEARCH_CAP, base=BASE)
        if r is not None and r.get("ok"):
            hits = r.get("hits") or []
            if not hits:
                return f"Ничего не нашла ({r.get('files_seen', '?')} файлов по {glob})."
            return "\n".join(hits) + (f"\n… (потолок {SEARCH_CAP} — сузь паттерн)"
                                      if r.get("capped") else "")
    base = _resolve_read(root) if root else BASE
    if base is None or not base.is_dir():
        return "Не ищу: плохой корень поиска."
    # Обход ЛЕНИВЫЙ и ограниченный. Прежде здесь стоял `sorted(base.glob(glob))`: он
    # материализует и сортирует всё дерево ДО первого чтения — на memory с гигабайтами
    # прогонов это часы и гигабайты ОЗУ ещё до того, как найдётся первое совпадение.
    # Порядок выдачи сохранён: сортируем НАЙДЕННОЕ, а не всё дерево.
    found: list[tuple] = []
    files_seen = scanned = 0
    stop = ""
    deadline = (time.monotonic() + SEARCH_SECONDS) if SEARCH_SECONDS > 0 else None
    for p in base.glob(glob):
        scanned += 1
        if SEARCH_SCAN_CAP > 0 and scanned > SEARCH_SCAN_CAP:
            stop = f"обошла {SEARCH_SCAN_CAP} путей и остановилась"
            break
        # Часы спрашиваем не на каждом пути: сам вызов дороже проверки маски.
        if deadline is not None and not scanned % 256 and time.monotonic() > deadline:
            stop = f"искала {SEARCH_SECONDS:.0f}с и остановилась"
            break
        if not p.is_file() or _is_secret(p):
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        files_seen += 1
        try:
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if rx.search(line):
                    found.append((str(p.relative_to(base)), i, line.strip()[:180]))
        except OSError:
            continue
        if len(found) >= SEARCH_CAP:
            stop = stop or f"потолок {SEARCH_CAP} совпадений"
            break
    found.sort(key=lambda row: (row[0], row[1]))
    hits = [f"{rel}:{i}: {text}" for rel, i, text in found[:SEARCH_CAP]]
    if not hits:
        tail = f" — {stop}" if stop else ""
        return f"Ничего не нашла ({files_seen} файлов по {glob}, путей обошла {scanned}{tail})."
    if len(found) > SEARCH_CAP:
        stop = stop or f"потолок {SEARCH_CAP} совпадений"
    # Усечение НАЗЫВАЕТСЯ. Молчаливое «ничего не нашла» после обрыва обхода — это ложь:
    # она бы решила, что искомого нет, а мы просто не дошли.
    return "\n".join(hits) + (f"\n… ({stop}; путей обошла {scanned}, файлов прочла "
                              f"{files_seen} — сузь маску или корень)" if stop else "")


def fs_ls(path: str = "") -> str:
    p = _resolve_read(path or str(BASE))
    if p is None or not p.is_dir():
        return "Нет такой директории (или вне дома)."
    out = []
    for c in sorted(p.iterdir()):
        if _is_secret(c):
            continue
        mark = "/" if c.is_dir() else ""
        size = "" if c.is_dir() else f" · {c.stat().st_size}Б"
        out.append(f"{c.name}{mark}{size}")
    return "\n".join(out[:200]) or "(пусто)"


# --------------------------------------------------------------------------- #
#  Запуск и зависимости
# --------------------------------------------------------------------------- #

def _venv_python(d: Path) -> Path | None:
    for cand in (d / ".venv" / "bin" / "python", d / ".venv" / "Scripts" / "python.exe"):
        if cand.exists():
            return cand
    return None


def run(cmd: str, project: str, timeout: int = RUN_TIMEOUT) -> str:
    """Запуск в проекте мастерской. PASS 16.3: при собранном полу — через бинарь
    (тайм-аут, джейл cwd, потолок вывода и расписка зашиты в нём)."""
    d = _project_dir(project)
    if d is None:
        return f"Нет проекта {_slug(project)} — run работает в проектах мастерской."
    timeout = max(1, min(int(timeout or RUN_TIMEOUT), 600))
    log.info("workshop run [%s] $ %s", d.name, cmd[:120])
    r = hands.execute(cmd, cwd=str(d), timeout=timeout, cap=RUN_OUT_CAP, base=BASE)
    if r is not None and ("out" in r or r.get("msg")):
        if "out" in r:
            out = (r.get("out") or "").strip() or "(пустой вывод)"
            code = int(r.get("code", 0))
            return out + (f"\n[exit {code}]" if code != 0 and not r.get("killed") else "")
        return f"[ошибка запуска] {r.get('msg')}"
    try:
        r2 = subprocess.run(cmd, shell=True, cwd=str(d), capture_output=True, text=True,
                            timeout=timeout)
        out = (r2.stdout or "") + (r2.stderr or "")
        tail = f"\n[exit {r2.returncode}]" if r2.returncode != 0 else ""
    except subprocess.TimeoutExpired:
        return f"[прервано по тайм-ауту {timeout}с]"
    except Exception as e:
        return f"[ошибка запуска] {type(e).__name__}: {e}"
    return (_cap_output(out.strip()) or "(пустой вывод)") + tail


def run_tests(project: str = "self") -> str:
    """self → selfdev.run_tests в АКТУАЛЬНОМ worktree предложения; проект → pytest||unittest."""
    if (project or "self").strip().lower() == "self":
        open_props = [t for t in selfdev.all_items(50) if t.get("status") == "building"]
        if not open_props:
            return ("Нет открытого предложения: тесты ядра гоняются в его worktree "
                    "(start_proposal → правки → run_tests(\"self\") → submit_proposal).")
        pid = open_props[-1]["id"]
        res = selfdev.run_tests(pid)
        return f"[предложение {pid}] {res['summary']}"
    d = _project_dir(project)
    if d is None:
        return f"Нет проекта {_slug(project)}."
    py = _venv_python(d) or Path(sys.executable)
    try:
        r = subprocess.run([str(py), "-m", "pytest", "-q"], cwd=str(d), capture_output=True,
                           text=True, timeout=RUN_TIMEOUT)
        out = (r.stdout or "") + (r.stderr or "")
        if "No module named pytest" in out:
            r = subprocess.run([str(py), "-m", "unittest", "discover", "-q"], cwd=str(d),
                               capture_output=True, text=True, timeout=RUN_TIMEOUT)
            out = (r.stdout or "") + (r.stderr or "")
        return _cap_output(out.strip()) + ("" if r.returncode == 0 else f"\n[exit {r.returncode}]")
    except subprocess.TimeoutExpired:
        return f"[тесты не уложились в {RUN_TIMEOUT}s]"


_PKG_RE = re.compile(r"^[A-Za-z0-9_.\[\]=<>!,~-]+$")


def pip_install(project: str, packages: str) -> str:
    d = _project_dir(project)
    if d is None:
        return f"Нет проекта {_slug(project)} — сначала project_create."
    pkgs = [p for p in str(packages or "").split() if p]
    if not pkgs:
        return "Скажи, что ставить: pip_install(project, \"requests rich\")."
    bad = [p for p in pkgs if not _PKG_RE.match(p) or p.startswith("-")]
    if bad:
        return f"Подозрительные аргументы (не пакеты): {', '.join(bad)} — не ставлю."
    size = _du_mb(d)
    if size > QUOTA_MB:
        return (f"Квота проекта превышена ({size:.0f}/{QUOTA_MB} МБ) — сначала почисти "
                "(.venv пересоздать, артефакты удалить), потом ставь.")
    py = _venv_python(d)
    if py is None:
        try:
            r = subprocess.run([sys.executable, "-m", "venv", str(d / ".venv")],
                               capture_output=True, text=True, timeout=PIP_TIMEOUT)
            if r.returncode != 0:
                return f"venv не создался: {_cap_output((r.stderr or r.stdout or '').strip(), 500)}"
        except subprocess.TimeoutExpired:
            return "venv не создался (таймаут)."
        py = _venv_python(d)
        if py is None:
            return "venv не создался (python не нашёлся внутри)."
    try:
        r = subprocess.run([str(py), "-m", "pip", "install", "-q", *pkgs], cwd=str(d),
                           capture_output=True, text=True, timeout=PIP_TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"[pip не уложился в {PIP_TIMEOUT}s]"
    out = _cap_output(((r.stdout or "") + (r.stderr or "")).strip(), 1500)
    size = _du_mb(d)
    over = f"\n⚠ Квота: {size:.0f}/{QUOTA_MB} МБ — превышена, почисти проект!" if size > QUOTA_MB \
        else f"\nДиск проекта: {size:.0f}/{QUOTA_MB} МБ."
    if r.returncode != 0:
        return f"pip упал:\n{out}{over}"
    return f"Поставила: {' '.join(pkgs)} (в .venv проекта).{over}"


# --------------------------------------------------------------------------- #
#  Доставка и карта кода
# --------------------------------------------------------------------------- #

def send_file(path: str, caption: str = "", to: str = "") -> str:
    """Документ в текущий Telegram-чат; photo/audio идут через live-turn spool."""
    p = _resolve_read(path)
    if p is None:
        return "Не отправляю: путь вне дома."
    if not p.is_file():
        return f"Нет файла {path}."
    import agent  # лениво: мост живёт в agent._TELETHON, кладёт его раннер
    import media as media_core
    kind = media_core.media_kind(media_core.sniff_mime(p))
    explicit_to = bool(str(to or "").strip())
    live_turn = (agent._TURN_CHANNEL.get() is not None
                 and agent._TURN_OUTBOUND.get() is not None)
    execution = agent.current_tool_execution()
    execution_key = (str(execution.get("idempotency_key") or "")
                     if isinstance(execution, dict) else "")
    stage_owned = execution_key.startswith("turn-media-stage:")
    direct_owned = execution_key.startswith("telegram-outbox:")
    if not explicit_to and (stage_owned or (live_turn and not direct_owned)):
        if not live_turn:
            return "Медиа можно подготовить только из живого хода Telegram."
        if kind in ("photo", "audio"):
            return agent.tool_send_media(str(p), kind, caption=caption or "")
        # A normal document authored/requested in a live Telegram turn belongs to
        # that turn's guarded durable outbox.  Auxiliary task windows without a
        # live channel retain the explicit synchronous owner-delivery fallback.
        return agent.tool_send_media(str(p), "document", caption=caption or "")
    fn = agent._TELETHON.get("send_file")
    if not fn:
        return "Недоступно (нет связи с Telethon)."
    try:
        if str(to or "").strip():
            return str(fn(str(p), caption or "", str(to).strip()))
        return str(fn(str(p), caption or ""))
    except Exception as e:
        if isinstance(e, agent.DurableSideEffectPending):
            raise
        return f"Не отправился: {type(e).__name__}: {str(e)[:150]}"


_MAP_CACHE: dict = {"key": None, "text": None}


def _map_fingerprint(root: Path) -> str:
    parts = [str(root)]
    for p in sorted(root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        try:
            parts.append(f"{p.name}:{p.stat().st_mtime_ns}")
        except OSError:
            pass
    return "|".join(parts)


def code_map(scope: str = "self") -> str:
    """ast-карта: модуль → классы/def (строки, первая строка докстринга). Кэш по mtime."""
    scope = (scope or "self").strip()
    if scope.lower() == "self":
        root = REPO
    else:
        d = _project_dir(scope)
        if d is None:
            return f"Нет проекта {_slug(scope)} (для своего кода — scope=\"self\")."
        root = d
    fp = _map_fingerprint(root)
    if _MAP_CACHE["key"] == fp and _MAP_CACHE["text"]:
        return _MAP_CACHE["text"]
    out = []
    for p in sorted(root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        rel = p.relative_to(root)
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        except (SyntaxError, OSError):
            out.append(f"{rel} — (не разобрался)")
            continue
        doc = (ast.get_docstring(tree) or "").strip().splitlines()
        out.append(f"{rel}" + (f" — {doc[0][:100]}" if doc else ""))
        for n in tree.body:
            if isinstance(n, ast.ClassDef):
                cdoc = (ast.get_docstring(n) or "").strip().splitlines()
                out.append(f"  class {n.name} :{n.lineno}" + (f" — {cdoc[0][:80]}" if cdoc else ""))
                for m in n.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        out.append(f"    def {m.name} :{m.lineno}")
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fdoc = (ast.get_docstring(n) or "").strip().splitlines()
                out.append(f"  def {n.name} :{n.lineno}" + (f" — {fdoc[0][:80]}" if fdoc else ""))
    text = "\n".join(out)[:MAP_CAP] or "(нет .py файлов)"
    _MAP_CACHE.update(key=fp, text=text)
    return text
