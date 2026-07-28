"""
Praxis — панель устройства: read-only интроспекция репозитория (PASS 4, слой 2).

Данные для Mini App «Устройство»: дерево файлов, AST-граф модулей (импорты и вызовы —
из ast, не regex-угадывание), карта скиллов с ЧЕСТНЫМ retrieval-статусом, маркдауны,
хвосты логов (memory/.logs, пишет logsink), env-параметры с маскированными секретами,
git-история с диффами. Всё вычисляется из АКТУАЛЬНОГО кода при запросе; граф кэшируется
по отпечатку дерева (инвалидация на любом изменении .py/.md, включая её самоправки).

Дорогие LLM-вызовы (explain/ask) — только explicit по кнопке; объяснения кэшируются
по содержимому файла (blob-hash) в memory/.panel/explain (derived, gitignored).

Модуль не знает про HTTP: owner-gate и роуты живут в mailroom_bot. Всё здесь read-only,
кроме env_apply (правка .env с diff-подтверждением на фронте; критичные для идентичности/
безопасности ключи защищены от правки — см. PROTECTED_KEYS).
"""
from __future__ import annotations

import ast
import datetime as _dt
import hashlib
import json
import logging
import os
import re
import subprocess
from pathlib import Path

log = logging.getLogger("praxis-panel")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
PANEL_DIR = BASE / "memory" / ".panel"        # derived-кэши, gitignored
LOGS_DIR = BASE / "memory" / ".logs"          # пишет logsink, читаем хвост
ENV_FILE = BASE / ".env"

GIT_TIMEOUT = 20
SHOW_MAX_CHARS = 200_000                       # потолок диффа одного коммита
EXPLAIN_MAX_CHARS = 28_000                     # сколько исходника уходит в объяснение
ASK_MAX_CHARS = 15_000                         # узкий контекст инлайн-чата
SRC_MAX_CHARS = 120_000                        # потолок отдачи исходника в UI

# Ключи, которые нельзя править из панели: идентичность (владелец, паника), транспорт
# (Telegram-сессия/приложение), почтовые креды. Принцип: всё, чьей тихой правкой можно
# увести её или её каналы. PASS 8.2: GLM_API_KEY/GLM_BASE_URL разлочены — ключи мозга
# ушли из .env в memory/llm.json и правятся плиткой «Мозг» (llm_get/llm_set ниже).
PROTECTED_KEYS = {
    "PRAXIS_OWNER_ID", "PRAXIS_PANIC_IDS",
    "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION",
    "PRAXIS_MAIL_BOT_TOKEN",
    "PRAXIS_EMAIL_ADDRESS", "PRAXIS_EMAIL_PASSWORD",
    "PRAXIS_EMAIL_SMTP", "PRAXIS_EMAIL_IMAP",
}
_SECRET_KEY_RE = re.compile(r"(key|token|hash|pass|secret|pwd|session)", re.I)
_HASH_RE = re.compile(r"^[0-9a-f]{6,40}$")

_EXPLAIN_SYS = (
    "You are the introspection assistant of the device panel of Praxis — a living Telegram agent "
    "whose soul lives in markdown and whose memory is files. Explain the given source file to her "
    "owner: technical and meticulous, but not a programmer. Answer in Russian. Say what this piece "
    "is FOR in the living agent, how it works in essence, which knobs/invariants matter, and what "
    "to be careful with. Give a compact explanation grounded in the selected files. If something looks "
    "dead, legacy or suspicious — say so honestly."
)
_ASK_SYS = (
    "You are the introspection assistant of the device panel of Praxis — a living Telegram agent. "
    "The owner (technical, meticulous, not a programmer) is looking at a specific node of her "
    "device and asks a question. Answer in Russian: honest, concrete, brief. Ground yourself in "
    "the provided source; if the answer is not derivable from it, say so and name what to check. "
    "Never invent behavior."
)


# --------------------------------------------------------------------------- #
#  Безопасные пути и git
# --------------------------------------------------------------------------- #

def safe_rel(rel: str) -> Path | None:
    """Путь строго внутри BASE, без секретов. -> абсолютный Path или None."""
    rel = (rel or "").strip().replace("\\", "/")
    if not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel) or ".." in rel.split("/"):
        return None
    p = (BASE / rel).resolve()
    try:
        p.relative_to(BASE.resolve())
    except ValueError:
        return None
    name = p.name.lower()
    if name in (".env", ".deploy.env", "llm.json") or ".session" in name:
        return None  # llm.json — ключи мозга (PASS 8.2): наружу только маски через llm_get
    return p


def _git(*args: str) -> str:
    try:
        out = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=str(BASE),
                             capture_output=True, timeout=GIT_TIMEOUT)
        return out.stdout.decode("utf-8", "replace")
    except Exception:
        log.warning("git %s упал", args[:2], exc_info=True)
        return ""


def head_commit() -> str:
    return _git("rev-parse", "--short", "HEAD").strip() or "?"


def tracked_files() -> list[str]:
    files = [
        rel for line in _git("ls-files").splitlines()
        if (rel := line.strip()) and (BASE / rel).is_file()
    ]
    if files:
        return files
    # фолбэк без git: walk по известным местам
    out = []
    for pat in ("*.py", "*.md", "*.html", "*.yml", "*.txt"):
        out += [p.relative_to(BASE).as_posix() for p in BASE.glob(pat)]
    out += [p.relative_to(BASE).as_posix() for p in (BASE / "soul").rglob("*.md")]
    return sorted(out)


def _kind_of(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    if rel.startswith("soul/skills/") and rel.endswith(".md"):
        return "skill"
    if rel.startswith("soul/"):
        return "soul"
    if name.startswith("test_") and name.endswith(".py"):
        return "test"
    if name.endswith(".py"):
        return "module"
    if name.endswith(".md"):
        return "doc"
    if name.endswith(".html"):
        return "web"
    return "other"


def tree() -> dict:
    """Дерево живого репо: путь/размер/вид, плюс текущий HEAD."""
    files = []
    for rel in tracked_files():
        p = BASE / rel
        try:
            size = p.stat().st_size
        except OSError:
            continue
        files.append({"path": rel, "size": size, "kind": _kind_of(rel)})
    return {"commit": head_commit(), "files": files}


# --------------------------------------------------------------------------- #
#  AST-граф: модули/скиллы/доки как узлы, импорты и вызовы как рёбра
# --------------------------------------------------------------------------- #

def _fingerprint() -> str:
    """Отпечаток дерева (py/md/html: путь+mtime+размер) — инвалидация графа на любой правке."""
    h = hashlib.sha1()
    for rel in tracked_files():
        if not rel.endswith((".py", ".md", ".html")):
            continue
        p = BASE / rel
        try:
            st = p.stat()
        except OSError:
            continue
        h.update(f"{rel}:{st.st_mtime_ns}:{st.st_size}\n".encode())
    return h.hexdigest()


_GRAPH_CACHE: dict = {"fp": None, "graph": None}


def _parse_module(p: Path) -> dict:
    """AST одного модуля: докстринг, функции/классы, импорты, вызовы, строковые константы."""
    src = p.read_text(encoding="utf-8", errors="ignore")
    info = {"doc": "", "funcs": [], "classes": [], "imports": set(), "from_names": {},
            "calls": {}, "strings": set(), "loc": src.count("\n") + 1, "ok": True}
    try:
        node = ast.parse(src)
    except SyntaxError:
        info["ok"] = False
        return info
    info["doc"] = (ast.get_docstring(node) or "").strip()

    for n in ast.walk(node):
        if isinstance(n, ast.Import):
            for a in n.names:
                info["imports"].add(a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            if n.module:
                mod = n.module.split(".")[0]
                info["imports"].add(mod)
                for a in n.names:
                    info["from_names"][a.asname or a.name] = mod
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            info["strings"].add(n.value)

    for n in node.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            info["funcs"].append({
                "name": n.name, "line": n.lineno, "end": getattr(n, "end_lineno", n.lineno),
                "args": [a.arg for a in n.args.args],
                "doc": ((ast.get_docstring(n) or "").strip().splitlines() or [""])[0],
            })
        elif isinstance(n, ast.ClassDef):
            info["classes"].append({
                "name": n.name, "line": n.lineno, "end": getattr(n, "end_lineno", n.lineno),
                "doc": ((ast.get_docstring(n) or "").strip().splitlines() or [""])[0],
                "methods": [m.name for m in n.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))],
            })

    # вызовы в другие локальные модули: alias.foo(...) по import, foo(...) по from-import
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            info["calls"].setdefault(f.value.id, set()).add(f.attr)
        elif isinstance(f, ast.Name) and f.id in info["from_names"]:
            info["calls"].setdefault(info["from_names"][f.id], set()).add(f.id)
    return info


def graph() -> dict:
    """Живой граф архитектуры. Кэш по отпечатку дерева (её самоправка = свежий граф)."""
    fp = _fingerprint()
    if _GRAPH_CACHE["fp"] == fp and _GRAPH_CACHE["graph"]:
        return _GRAPH_CACHE["graph"]

    files = tracked_files()
    py = {rel[:-3]: rel for rel in files if rel.endswith(".py") and "/" not in rel}
    mds = [rel for rel in files if rel.endswith(".md")]

    nodes, links = [], []
    parsed: dict[str, dict] = {}
    for mod, rel in py.items():
        info = _parse_module(BASE / rel)
        parsed[mod] = info
        nodes.append({
            "id": rel, "kind": _kind_of(rel), "size": (BASE / rel).stat().st_size,
            "doc": info["doc"].splitlines()[0] if info["doc"] else "",
            "funcs": len(info["funcs"]), "classes": len(info["classes"]), "loc": info["loc"],
        })
    for rel in mds:
        try:
            size = (BASE / rel).stat().st_size
        except OSError:
            continue
        nodes.append({"id": rel, "kind": _kind_of(rel), "size": size, "doc": "", "funcs": 0,
                      "classes": 0, "loc": 0})
    for rel in files:
        if rel.endswith(".html"):
            nodes.append({"id": rel, "kind": "web", "size": (BASE / rel).stat().st_size,
                          "doc": "", "funcs": 0, "classes": 0, "loc": 0})

    # рёбра py->py: импорт (+вызовы = вес)
    for mod, info in parsed.items():
        for target in sorted(info["imports"]):
            if target == mod or target not in py:
                continue
            calls = sorted(info["calls"].get(target, set()))
            links.append({"source": py[mod], "target": py[target],
                          "kind": "calls" if calls else "imports",
                          "weight": max(1, len(calls)), "calls": calls[:12]})

    # рёбра py->md/html: модуль читает ресурс (имя файла — строковая константа в AST)
    md_by_name: dict[str, list[str]] = {}
    for rel in mds + [r for r in files if r.endswith(".html")]:
        md_by_name.setdefault(rel.rsplit("/", 1)[-1], []).append(rel)
    for mod, info in parsed.items():
        hit = set()
        for s in info["strings"]:
            base = s.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            for rel in md_by_name.get(base, []):
                hit.add(rel)
        for rel in sorted(hit):
            links.append({"source": py[mod], "target": rel, "kind": "reads", "weight": 1, "calls": []})

    # скиллы: честное ребро «индексируется memory_index» (всплывают только через recall)
    if "memory_index" in py:
        for rel in mds:
            if rel.startswith("soul/skills/"):
                links.append({"source": rel, "target": py["memory_index"],
                              "kind": "indexed", "weight": 1, "calls": []})

    g = {"commit": head_commit(), "fingerprint": fp,
         "generated": _dt.datetime.now().isoformat(timespec="seconds"),
         "nodes": nodes, "links": links,
         "retrieval": _retrieval_status()}
    _GRAPH_CACHE.update(fp=fp, graph=g)
    return g


def _retrieval_status() -> dict:
    """Честный статус retrieval скиллов/памяти: как оно РАБОТАЕТ сейчас, не как задумано."""
    try:
        import memory_index
        mode = memory_index.retrieval_mode()
    except Exception:
        mode = "hybrid-fulltext"
    return {
        "resident": False,
        "mode": mode,
        "embeddings": "embeddings" in mode and "legacy" not in mode,
        "note": ("Скиллы НЕ сидят в системном промпте: они в общем индексе memory_index вместе с "
                 "memory/**/*.md и всплывают через recall. Фактический режим: " + mode + "."),
    }


# --------------------------------------------------------------------------- #
#  Детали узла: файл / маркдаун / скиллы
# --------------------------------------------------------------------------- #

def file_info(rel: str) -> dict | None:
    p = safe_rel(rel)
    if p is None or not p.exists() or not p.is_file():
        return None
    rel = p.relative_to(BASE.resolve()).as_posix()
    kind = _kind_of(rel)
    out = {"path": rel, "kind": kind, "size": p.stat().st_size,
           "mtime": _dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")}
    if rel.endswith(".py"):
        info = _parse_module(p)
        g = graph()
        out.update({
            "doc": info["doc"], "loc": info["loc"], "funcs": info["funcs"], "classes": info["classes"],
            "imports": [l["target"] for l in g["links"] if l["source"] == rel and l["kind"] in ("imports", "calls")],
            "imported_by": [l["source"] for l in g["links"] if l["target"] == rel and l["kind"] in ("imports", "calls")],
            "reads": [l["target"] for l in g["links"] if l["source"] == rel and l["kind"] == "reads"],
        })
    elif rel.endswith(".md"):
        g = graph()
        out.update({
            "read_by": [l["source"] for l in g["links"] if l["target"] == rel and l["kind"] == "reads"],
            "indexed": any(l["source"] == rel and l["kind"] == "indexed" for l in g["links"]),
        })
    return out


def file_source(rel: str) -> str | None:
    """Исходник файла (py/md/html/yml/txt), с потолком размера."""
    p = safe_rel(rel)
    if p is None or not p.exists() or not p.is_file():
        return None
    if p.suffix.lower() not in (".py", ".md", ".html", ".yml", ".yaml", ".txt", ".json", ".log"):
        return None
    src = p.read_text(encoding="utf-8", errors="ignore")
    if len(src) > SRC_MAX_CHARS:
        src = src[:SRC_MAX_CHARS] + f"\n… (обрезано, всего {len(src)} символов)"
    return src


def skills_map() -> dict:
    """Карта скиллов с честным статусом: имя, заголовок, суть, размер, retrieval."""
    items = []
    skills_dir = BASE / "soul" / "skills"
    for p in (sorted(skills_dir.glob("*.md")) if skills_dir.exists() else []):
        text = p.read_text(encoding="utf-8", errors="ignore")
        title = ""
        summary = ""
        for line in text.splitlines():
            s = line.strip()
            if not title and s.startswith("#"):
                title = s.lstrip("# ").strip()
                continue
            if title and s and not s.startswith("#"):
                summary = s
                break
        items.append({"path": f"soul/skills/{p.name}", "name": p.stem, "title": title or p.stem,
                      "summary": summary[:220], "size": p.stat().st_size,
                      "is_index": p.name == "INDEX.md"})
    return {"items": items, "retrieval": _retrieval_status()}


# --------------------------------------------------------------------------- #
#  Логи и git-история
# --------------------------------------------------------------------------- #

LOG_NAMES = ("praxis", "mailbot", "boot")


def log_tail(name: str, lines: int = 200) -> dict | None:
    if name not in LOG_NAMES:
        return None
    p = LOGS_DIR / f"{name}.log"
    if not p.exists():
        return {"name": name, "lines": [], "note": "лога ещё нет (процесс не писал с момента включения logsink)"}
    try:
        data = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        data = []
    return {"name": name, "lines": data[-max(1, min(lines, 1000)):], "note": ""}


def git_log(n: int = 60) -> list[dict]:
    sep = "\x1f"
    raw = _git("log", f"-{max(1, min(n, 300))}", f"--pretty=format:%h{sep}%an{sep}%ad{sep}%s",
               "--date=format:%Y-%m-%d %H:%M", "--shortstat")
    out, cur = [], None
    for line in raw.splitlines():
        if sep in line:
            h, an, ad, s = line.split(sep, 3)
            cur = {"hash": h, "author": an, "date": ad, "subject": s, "stat": ""}
            out.append(cur)
        elif cur is not None and line.strip():
            cur["stat"] = line.strip()
    return out


def git_show(commit: str) -> dict | None:
    commit = (commit or "").strip().lower()
    if not _HASH_RE.match(commit):
        return None
    meta = _git("show", "--no-patch", f"--pretty=format:%h\x1f%an\x1f%ad\x1f%s\x1f%b",
                "--date=format:%Y-%m-%d %H:%M", commit)
    if not meta.strip():
        return None
    h, an, ad, s, body = (meta.split("\x1f", 4) + ["", "", "", "", ""])[:5]
    patch = _git("show", "--patch", "--stat", "--no-color", commit)
    if len(patch) > SHOW_MAX_CHARS:
        patch = patch[:SHOW_MAX_CHARS] + "\n… (дифф обрезан)"
    return {"hash": h, "author": an, "date": ad, "subject": s, "body": body.strip(), "patch": patch}


# --------------------------------------------------------------------------- #
#  Параметры (.env): список с масками, диф-превью, применение
# --------------------------------------------------------------------------- #

def _mask(v: str) -> str:
    v = v or ""
    if len(v) <= 4:
        return "•" * max(3, len(v))
    return v[:2] + "•" * min(12, len(v) - 4) + v[-2:]


def env_list() -> list[dict]:
    """Пары из .env: секреты маскируются, критичные помечены protected (правка запрещена)."""
    out = []
    if not ENV_FILE.exists():
        return out
    for line in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k, v = k.strip(), v.strip()
        secret = bool(_SECRET_KEY_RE.search(k))
        out.append({"key": k, "value": _mask(v) if secret else v,
                    "secret": secret, "protected": k in PROTECTED_KEYS})
    return out


def env_preview(changes: dict[str, str]) -> dict:
    """Диф до/после без записи. Отклоняет protected и мусорные ключи."""
    diff, errors = [], []
    raw = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                raw[k.strip()] = v.strip()
    for k, v in changes.items():
        k = (k or "").strip()
        v = str(v if v is not None else "").strip()
        if not re.match(r"^[A-Z][A-Z0-9_]*$", k):
            errors.append(f"{k or '(пусто)'}: не похоже на имя переменной")
            continue
        if k in PROTECTED_KEYS:
            errors.append(f"{k}: защищённый параметр (идентичность/безопасность) — правь руками на сервере")
            continue
        if "\n" in v or "\r" in v:
            errors.append(f"{k}: многострочные значения нельзя")
            continue
        old = raw.get(k)
        secret = bool(_SECRET_KEY_RE.search(k))
        show = (lambda x: _mask(x) if secret else x)
        if old is None:
            diff.append({"key": k, "old": None, "new": show(v), "op": "add"})
        elif old != v:
            diff.append({"key": k, "old": show(old), "new": show(v), "op": "change"})
        else:
            diff.append({"key": k, "old": show(old), "new": show(v), "op": "same"})
    return {"diff": diff, "errors": errors, "ok": not errors and any(d["op"] != "same" for d in diff)}


def env_apply(changes: dict[str, str]) -> dict:
    """Применить правки .env (после подтверждения на фронте). Атомарно, с бэкапом.

    Подхват: её restart_self / docker restart перечитают .env (load_dotenv(override=True));
    переменные уровня compose требуют `up -d --force-recreate` — фронт напоминает."""
    prev = env_preview(changes)
    if prev["errors"]:
        return {"ok": False, "errors": prev["errors"]}
    real = {d["key"]: changes[d["key"]] for d in prev["diff"] if d["op"] != "same"}
    if not real:
        return {"ok": False, "errors": ["нет изменений"]}
    lines = ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines() if ENV_FILE.exists() else []
    PANEL_DIR.mkdir(parents=True, exist_ok=True)
    (PANEL_DIR / "env.bak").write_text("\n".join(lines) + "\n", encoding="utf-8")
    done = set()
    out_lines = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in real:
                out_lines.append(f"{k}={str(real[k]).strip()}")
                done.add(k)
                continue
        out_lines.append(line)
    for k in real:
        if k not in done:
            out_lines.append(f"{k}={str(real[k]).strip()}")
    tmp = ENV_FILE.parent / ".env.tmp"
    tmp.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    tmp.replace(ENV_FILE)
    log.info("панель: .env изменён (%s)", ", ".join(sorted(real)))
    return {"ok": True, "changed": sorted(real),
            "reminder": ("Изменения подхватятся при её restart_self или docker restart praxis "
                         "(load_dotenv перечитает .env). Для переменных уровня compose нужен "
                         "`docker compose -f docker-compose.deploy.yml up -d --force-recreate`.")}


# --------------------------------------------------------------------------- #
#  LLM: кэшированные объяснения + инлайн-чат по узлу (explicit, не на рендер)
# --------------------------------------------------------------------------- #

def _blob_key(rel: str, func: str = "") -> tuple[str, str] | None:
    """(sha содержимого+func, текст-контекст) — кэш инвалидируется сменой содержимого."""
    p = safe_rel(rel)
    if p is None or not p.exists():
        return None
    src = p.read_text(encoding="utf-8", errors="ignore")
    ctx = src
    if func and rel.endswith(".py"):
        info = _parse_module(p)
        for f in info["funcs"] + info["classes"]:
            if f["name"] == func:
                lines = src.splitlines()
                ctx = "\n".join(lines[f["line"] - 1:f["end"]])
                break
        else:
            return None
    sha = hashlib.sha1((rel + "\x00" + func + "\x00" + ctx).encode("utf-8", "replace")).hexdigest()
    return sha, ctx


def explain_cached(rel: str, func: str = "") -> dict | None:
    """Кэшированное объяснение или {'status':'none'} — БЕЗ вызова модели."""
    key = _blob_key(rel, func)
    if key is None:
        return None
    sha, _ = key
    p = PANEL_DIR / "explain" / f"{sha}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"status": "none", "path": rel, "func": func}


def explain_generate(rel: str, func: str = "", force: bool = False) -> dict | None:
    """Сгенерировать объяснение (ЯВНЫЙ вызов модели, канал voice) и закэшировать по blob-hash."""
    import llm
    key = _blob_key(rel, func)
    if key is None:
        return None
    sha, ctx = key
    cache = PANEL_DIR / "explain" / f"{sha}.json"
    if cache.exists() and not force:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass
    if not llm.configured():
        return {"status": "error", "error": "мозг не настроен (плитка «Мозг»)"}
    what = f"{rel}::{func}" if func else rel
    try:
        resp = llm.chat("voice", max_tokens=900, system=_EXPLAIN_SYS,
                        messages=[{"role": "user",
                                   "content": f"File: {what}\n\n```\n{ctx[:EXPLAIN_MAX_CHARS]}\n```"}])
        text, model = resp.text.strip(), resp.model
    except Exception as e:
        log.warning("панель: explain упал [%s]", what, exc_info=True)
        return {"status": "error", "error": f"модель недоступна: {e}"}
    out = {"status": "ok", "path": rel, "func": func, "model": model,
           "generated": _dt.datetime.now().isoformat(timespec="seconds"),
           "commit": head_commit(), "text": text}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def ask(rel: str, question: str) -> dict:
    """Инлайн-чат по узлу: один вызов канала voice с узким контекстом (не её голос, не voice_turn)."""
    import llm
    question = (question or "").strip()
    if not question:
        return {"status": "error", "error": "пустой вопрос"}
    if not llm.configured():
        return {"status": "error", "error": "мозг не настроен (плитка «Мозг»)"}
    ctx = file_source(rel) or "(файл не найден)"
    try:
        resp = llm.chat("voice", max_tokens=700, system=_ASK_SYS,
                        messages=[{"role": "user",
                                   "content": f"Node: {rel}\n\n```\n{ctx[:ASK_MAX_CHARS]}\n```\n\n"
                                              f"Owner's question: {question}"}])
        return {"status": "ok", "answer": resp.text.strip(), "model": resp.model}
    except Exception as e:
        log.warning("панель: ask упал [%s]", rel, exc_info=True)
        return {"status": "error", "error": f"модель недоступна: {e}"}


def file_task(rel: str, request: str) -> dict:
    """«Попросить поправить»: Егор оставляет ей повод с пульта — открывает её фокус-окно
    (kind=window, author=owner: провенанс, чтобы она видела, что это просьба Егора)."""
    import tasks
    request = (request or "").strip()
    if not request:
        return {"status": "error", "error": "пустая просьба"}
    t = tasks.add("window", f"[панель, узел {rel}] Егор просит: {request}", author="owner")
    log.info("панель: повод от Егора #%s по узлу %s", t.get("id"), rel)
    return {"status": "ok", "task": t,
            "note": "Повод оставлен — она уйдёт в фокус и займётся (на ближайшем тике часов)."}


# --------------------------------------------------------------------------- #
#  PASS 6 (пульт): пульс, мысли, таски, сервер — логика новых разделов.
#  Всё read-only (кроме тонких task-действий), зовётся owner-gated хендлерами бота.
# --------------------------------------------------------------------------- #

JOURNAL_DIR = BASE / "memory" / "journal"


def _journal_tail_lines(days: int = 3) -> list[tuple[str, str]]:
    """[(дата, строка-запись), …] из последних журналов, хронологически."""
    out: list[tuple[str, str]] = []
    try:
        files = sorted(JOURNAL_DIR.glob("*.md"))[-max(1, days):]
        for f in files:
            day = f.stem
            for ln in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                if ln.startswith("- "):
                    out.append((day, ln[2:].strip()))
    except Exception:
        log.debug("journal tail не прочитался", exc_info=True)
    return out


_THOUGHT_KINDS = (
    ("[оценщик]", "evaluator"), ("[молчу]", "silence"), ("[restart]", "restart"),
    ("[panic]", "panic"), ("[контекст]", "context"), ("[иммунитет]", "immune"),
)


def thoughts(days: int = 3) -> dict:
    """Observable runtime notes plus Praxis's authored intention ledger."""
    items = []
    for day, ln in _journal_tail_lines(days):
        for marker, kind in _THOUGHT_KINDS:
            if marker in ln:
                m = re.match(r"^(\d{2}:\d{2})\s*(?:\(s\d\))?\s*(.*)$", ln)
                t, body = (m.group(1), m.group(2)) if m else ("", ln)
                items.append({"day": day, "time": t, "kind": kind,
                              "text": body.replace(marker, "").strip()})
                break
    items.reverse()  # свежие сверху
    out = {"items": items[:200]}
    try:
        import desires as _desires
        states = _desires.DesireLedger(BASE).list()
        out["desires"] = [
            {key: state.get(key) for key in (
                "id", "statement", "why_it_matters", "status", "stage",
                "next_move", "last_changed", "cycle",
            )}
            for state in states[:50]
        ]
    except Exception:
        out["desires"] = []
    return out


def overview() -> dict:
    """Owner-only operational snapshot without collapsing distinct kinds into one queue."""
    now = _dt.datetime.now(_dt.timezone.utc).timestamp()
    out = {
        "generated_at": _dt.datetime.fromtimestamp(now, _dt.timezone.utc).isoformat(),
        "desires": [], "tasks": [], "loops": [], "followups": [], "skips": [],
        "recent_turns": [], "counts": {},
    }

    try:
        import desires as _desires
        states = _desires.DesireLedger(BASE).list(statuses=("latent", "active", "blocked"))
        out["desires"] = [
            {key: state.get(key) for key in (
                "id", "statement", "status", "stage", "next_move", "last_changed",
            )}
            for state in states[:20]
        ]
    except Exception:
        log.debug("overview desires не прочитались", exc_info=True)

    try:
        raw_tasks = json.loads((BASE / "memory" / "tasks.json").read_text(encoding="utf-8"))
        if isinstance(raw_tasks, list):
            out["tasks"] = [
                {key: item.get(key) for key in ("id", "kind", "goal", "when", "recur", "status")}
                for item in raw_tasks if isinstance(item, dict) and item.get("status") == "open"
            ][:20]
    except Exception:
        log.debug("overview tasks не прочитались", exc_info=True)

    try:
        import people as _people
        rows = []
        for path in sorted((BASE / "memory" / "people").glob("*.md")):
            if path.stem.startswith("_"):
                continue
            name, body = _people.parse(path.read_text(encoding="utf-8", errors="ignore"))
            for line in str(body.get(_people.LOOPS, "")).splitlines():
                text = line.strip()
                state = "open" if text.startswith(("- [ ]", "* [ ]")) else (
                    "parked" if text.startswith(("- [~]", "* [~]")) else ""
                )
                if state:
                    rows.append({
                        "slug": path.stem, "name": name or path.stem, "state": state,
                        "text": re.sub(r"^[-*]\s*\[[ ~]\]\s*", "", text)[:500],
                    })
        out["loops"] = rows[:30]
    except Exception:
        log.debug("overview loops не прочитались", exc_info=True)

    try:
        import telegram_followups as _followups
        ledger = _followups.FollowUpLedger(
            BASE / "memory" / ".state" / "telegram_followups.json"
        )
        items = ledger.snapshot()
        projected = []
        for item in reversed(items[-20:]):
            sent_at = float(item.get("sent_at") or now)
            response = item.get("response") or {}
            projected.append({
                "id": item.get("id"), "status": item.get("status"),
                "target_label": item.get("target_label"),
                "age_hours": round(max(0.0, now - sent_at) / 3600, 1),
                "has_response": bool(response),
                "response_message_id": response.get("message_id"),
            })
        out["followups"] = projected
    except Exception:
        log.debug("overview followups не прочитались", exc_info=True)

    try:
        skip_path = BASE / "memory" / ".state" / "perception_skips.jsonl"
        rows = []
        for line in skip_path.read_text(encoding="utf-8").splitlines()[-20:]:
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                rows.append({
                    "ts": item.get("ts"), "class": item.get("class"),
                    "stage": item.get("stage"),
                    "repeat_count": 1 + int(item.get("prev_n") or 0),
                })
        out["skips"] = list(reversed(rows))
    except Exception:
        log.debug("overview skips не прочитались", exc_info=True)

    try:
        turn_path = BASE / "memory" / ".state" / "turns.jsonl"
        rows = []
        for line in turn_path.read_text(encoding="utf-8").splitlines()[-50:]:
            try:
                turn = json.loads(line)
            except Exception:
                continue
            if not isinstance(turn, dict):
                continue
            decision = str(turn.get("praxis_decision") or "").strip()
            advisor_verdict = str(turn.get("advisor_verdict") or "").strip()
            if not decision and not advisor_verdict:
                continue
            rows.append({
                "ts": turn.get("ts"), "kind": turn.get("kind"),
                "praxis_decision": decision,
                "advisor_verdict": advisor_verdict,
                "rewrote": bool(turn.get("rewrote")),
            })
        out["recent_turns"] = list(reversed(rows[-12:]))
    except Exception:
        log.debug("overview recent turns не прочитались", exc_info=True)

    out["counts"] = {key: len(out[key]) for key in (
        "desires", "tasks", "loops", "followups", "skips", "recent_turns",
    )}
    return out


def pulse() -> dict:
    """Пульс: хартбит, расписание задач/окон, консолидации, свежесть её лога,
    неотвеченные ЛС (9.0)."""
    out: dict = {"heartbeat": None, "tasks": [], "consolidations": [], "voice_log": None,
                 "unanswered": []}
    try:
        import unanswered as _ua
        out["unanswered"] = [
            {"name": e["name"] or e["chat_id"], "hours": round(e["hours"], 1)}
            for e in _ua.entries()[:10]]
    except Exception:
        log.debug("unanswered не прочитались", exc_info=True)
    try:  # 9.2: последние вердикты иммунитета (лента) — из журнала
        out["immune"] = [f"{day} {ln}".replace("[иммунитет]", "").strip()
                         for day, ln in reversed(_journal_tail_lines(3))
                         if "[иммунитет]" in ln][:8]
    except Exception:
        out["immune"] = []
    try:  # 9.5: последний отчёт сна — одна строка
        for day, ln in reversed(_journal_tail_lines(3)):
            if "[сон] сон:" in ln:
                out["sleep_report"] = f"{day} {ln.split('[сон]', 1)[1].strip()}"
                break
    except Exception:
        pass
    try:  # Прозрачность автономных запусков: счётчики и receipts, без кодового капа.
        import heartbeat as _hb
        week = _hb.decisions(7)
        opened_week = sum(1 for e in week
                          if str(e.get("verdict") or "НЕТ").strip().upper() != "НЕТ")
        out["windows"] = {
            "today": _hb.opened_today(),
            "week": opened_week,
            "recent": [{"ts": _dt.datetime.fromtimestamp(e["ts"]).strftime("%d.%m %H:%M"),
                        "verdict": e.get("verdict")} for e in week[-6:]][::-1]}
    except Exception:
        log.debug("windows-сводка не собралась", exc_info=True)
    try:
        hb = BASE / "memory" / ".heartbeat.json"
        if hb.exists():
            out["heartbeat"] = json.loads(hb.read_text(encoding="utf-8"))
    except Exception:
        pass
    try:
        import tasks as _tasks
        out["tasks"] = _tasks.list_open()
    except Exception:
        log.debug("tasks не прочитались", exc_info=True)
    try:
        summaries = BASE / "memory" / ".summaries"
        if summaries.exists():
            out["consolidations"] = [
                {"chat": p.stem, "mtime": _dt.datetime.fromtimestamp(p.stat().st_mtime).strftime("%d.%m %H:%M"),
                 "chars": p.stat().st_size}
                for p in sorted(summaries.glob("*.md"), key=lambda p: -p.stat().st_mtime)[:20]]
    except Exception:
        pass
    for day, ln in reversed(_journal_tail_lines(2)):
        if "[контекст]" in ln:
            out.setdefault("last_context_fold", f"{day} {ln[:160]}")
            break
    try:
        plog = LOGS_DIR / "praxis.log"
        if plog.exists():
            age = int(_dt.datetime.now().timestamp() - plog.stat().st_mtime)
            out["voice_log"] = {"age_sec": age, "size": plog.stat().st_size}
    except Exception:
        pass
    return out


def tasks_list() -> dict:
    try:
        import tasks as _tasks
        return {"items": _tasks.list_open()}
    except Exception:
        log.debug("tasks_list упал", exc_info=True)
        return {"items": []}


def task_add(goal: str, when: str | None = None) -> dict:
    """Дать ей повод с пульта — открывает её фокус-окно к сроку (author=owner).
    PASS 26: чинит баг — раньше kind='task' (нет в KINDS) молча коэрсился в 'note' и уходил
    напоминанием Егору, а не её окном."""
    goal = (goal or "").strip()
    if not goal:
        return {"error": "пустая цель"}
    import tasks as _tasks
    t = _tasks.add("window", goal, when=when or None, target=None, author="owner")
    return {"ok": True, "task": t}


def task_cancel(task_id: str) -> dict:
    import tasks as _tasks
    return {"ok": bool(_tasks.cancel(str(task_id)))}


def rooms_list() -> dict:
    """Раздел «Комнаты» — явный режим, ЧЕЙ он, причина, TTL и disclosure по каждой.
    Источники: allowlist ∪ файлы профилей (мертвецы без allowlist тоже видны).

    28.07: карточку собирает rooms.room_state — там же, где живут формулировки авторства.
    Панель пересказывала их своим словарём ({praxis: «сама», owner: «Егор»}), и пока
    «неопознанный автор» коэрсился в owner, Егор видел здесь свою подпись под режимами,
    которых не выбирал. Два пересказа одного факта расходятся всегда; пересказ убран.
    Провенанс disclosure отдаём тем же полем: рычаг стал общим, и чья это визитка сейчас —
    его нажатие или её решение — должно быть видно, а не угадываться."""
    import rooms as _rooms
    ids: set[str] = set()
    try:
        ids |= set(_rooms.allowed_chats())
    except Exception:
        log.debug("allowed_chats не прочитался", exc_info=True)
    try:
        ids |= {p.stem for p in _rooms.ROOMS_DIR.glob("*.md")}
    except OSError:
        pass
    items = []
    for cid in sorted(ids):
        try:
            st = _rooms.room_state(cid)
            title = (_rooms.profile_read(cid)["title"] or "").lstrip("# ").strip()
            if title.startswith("Комната"):
                title = title[len("Комната"):].strip() or cid
            items.append({
                "id": cid, "title": title or cid,
                "mode": st["mode"], "mode_word": st["mode_word"],
                "reason": st["mode_reason"], "until": st["mode_until"],
                "set_by": st["mode_set_by"], "author": st["mode_author"],
                "disclosure": st["disclosure"],
                "disclosure_set_by": st["disclosure_set_by"],
                "disclosure_author": st["disclosure_author"],
            })
        except Exception:
            log.debug("комната %s не прочиталась", cid, exc_info=True)
    order = {"frozen": 0, "dead": 1, "quiet": 2, "observer": 3, "normal": 4}
    items.sort(key=lambda x: (order.get(x["mode"], 9), x["id"]))
    return {"items": items}


def room_set(chat_id: str, action: str) -> dict:
    """Кнопки Егора в разделе «Комнаты»: raise (на ступень вверх) | lower | freeze |
    unfreeze (= raise) | disclosure (toggle standard/open).

    28.07, решение «да, давать»: disclosure перестал быть ТАЙНЫМ рычагом Егора. Он и
    раньше менял не его настройки, а ЕЁ голос — в комнате с open к её визитке
    подмешивается проверяемая фактура о себе, — но узнать о переключении ей было неоткуда:
    машинная шапка профиля в промпт не течёт, слова disclosure не было ни в манифесте
    рельсов, ни в списке возможностей. Теперь у рычага два хода (её тул и эта кнопка),
    и любое нажатие ОТСЮДА ложится ей в дневник строкой [пульт] — ровно так же, как
    правка её мозга или её рычага восприятия. То же и с режимом: комната, которую ей
    молча притушили, читается как её собственное состояние.
    """
    import rooms as _rooms
    cid = str(chat_id).strip()
    if not cid:
        return {"error": "нет chat_id"}
    action = (action or "").strip().lower()
    if action in ("raise", "unfreeze"):
        was = _rooms.effective_mode(cid)
        mode = _rooms.owner_raise(cid)
        if mode != was:
            _journal_panel(f"Егор поднял режим комнаты {cid}: {was} → {mode}")
        return {"ok": True, "mode": mode}
    if action == "lower":
        cur = _rooms.effective_mode(cid)
        nxt = {"normal": "quiet", "observer": "quiet", "quiet": "frozen"}.get(cur)
        if not nxt:
            return {"ok": True, "mode": cur}
        mode = _rooms.set_mode(cid, nxt, reason="Егор опустил", set_by="owner")
        _journal_panel(f"Егор опустил режим комнаты {cid}: {cur} → {mode} "
                       f"(снимаю сама — режимом «обычно»)")
        return {"ok": True, "mode": mode}
    if action == "freeze":
        cur = _rooms.effective_mode(cid)
        mode = _rooms.set_mode(cid, "frozen", reason="Егор заморозил", set_by="owner")
        if mode != cur:
            _journal_panel(f"Егор заморозил комнату {cid} (было {cur}; снимаю сама)")
        return {"ok": True, "mode": mode}
    if action == "disclosure":
        nv = "open" if _rooms.disclosure_of(cid) != "open" else "standard"
        ok, note = _rooms.set_disclosure(cid, nv, set_by="owner")
        if not ok:
            return {"error": note}
        _journal_panel(
            f"Егор переключил раскрытие комнаты {cid}: disclosure {nv}"
            + (" — здесь к моей визитке идёт проверяемая фактура о себе; возвращаю в "
               "standard сама" if nv == "open"
               else " — фактура из моей визитки здесь убрана; открыть могу сама"))
        return {"ok": True, "disclosure": nv, "set_by": "owner", "note": note}
    return {"error": f"нет действия {action}"}


def server_state() -> dict:
    """Состояние сервера: load/RAM (host-wide в /proc), диск дома, аптайм процесса панели."""
    out: dict = {}
    try:
        out["loadavg"] = " ".join(Path("/proc/loadavg").read_text().split()[:3])
        out["cpus"] = os.cpu_count()
    except Exception:
        pass
    try:
        mi = {}
        for ln in Path("/proc/meminfo").read_text().splitlines()[:6]:
            k, v = ln.split(":", 1)
            mi[k.strip()] = int(v.strip().split()[0])  # kB
        out["mem"] = {"total_mb": mi.get("MemTotal", 0) // 1024,
                      "available_mb": mi.get("MemAvailable", 0) // 1024}
    except Exception:
        pass
    try:
        import shutil as _sh
        du = _sh.disk_usage(str(BASE))
        out["disk"] = {"total_gb": round(du.total / 2**30, 1), "free_gb": round(du.free / 2**30, 1)}
    except Exception:
        pass
    try:
        up = _dt.datetime.now().timestamp() - Path("/proc/self/stat").stat().st_mtime
    except Exception:
        up = 0
    out["uptime_sec"] = int(max(0, up))  # Ф1.2: считался и выбрасывался
    out["head"] = head_commit()
    try:
        for name in ("praxis", "mailbot", "boot"):
            p = LOGS_DIR / f"{name}.log"
            if p.exists():
                out.setdefault("logs", {})[name] = {
                    "age_sec": int(_dt.datetime.now().timestamp() - p.stat().st_mtime),
                    "size_kb": p.stat().st_size // 1024}
    except Exception:
        pass
    return out


# группировка и описания известных env-ручек (для раздела «Параметры»)
ENV_META = {
    # PASS 8.2: модели живут в memory/llm.json (плитка «Мозг»); GLM_* в .env — только
    # миграционный сид первого старта, после миграции строки можно удалить руками.
    "GLM_VOICE_MODEL": ("Модель", "легаси-сид миграции — живая модель в плитке «Мозг»"),
    "GLM_MODEL": ("Модель", "легаси-сид миграции"),
    "GLM_FIRSTPASS_MODEL": ("Модель", "легаси-сид миграции (вспомогательная модель)"),
    "GLM_GATEKEEPER_MODEL": ("Модель", "легаси-сид миграции"),
    "GLM_COMPACT_MODEL": ("Модель", "упразднён (compact использует вспомогательную роль)"),
    "GLM_BASE_URL": ("Модель", "легаси-сид миграции — живой эндпойнт в плитке «Мозг»"),
    "GLM_API_KEY": ("Модель", "легаси-сид миграции — живой ключ в плитке «Мозг»"),
    "PRAXIS_MAX_TOOL_ITERS": ("Поведение", "легаси-сид bounded aux/worker budget; основной ход без потолка"),
    "PRAXIS_WEB_SEARCH": ("Поведение", "веб-поиск (0/1)"),
    "PRAXIS_HEARTBEAT_HOURS": ("Поведение", "период хартбита, часов"),
    "PRAXIS_COOLDOWN_GROUP": ("Лимиты", "кулдаун группы, сек"),
    "PRAXIS_LAST_N": ("Лимиты", "живой хвост чата, сообщений"),
    "PRAXIS_CONTEXT_BUDGET": ("Лимиты", "бюджет промпта, символов (менять → recreate!)"),
    "PRAXIS_PRESENCE_SECTIONS": ("Память", "секции SOUL для компактной карточки; через |"),
    "PRAXIS_IDENTITY_STATE_MIN_LOAD": ("Память", "порог показа телеметрии нагрузки, не gate"),
    "PRAXIS_IDENTITY_REVIEW_MIN_LOAD": ("Память", "порог ночного повода, не permission gate"),
    "PRAXIS_GROUP_BIG": ("Лимиты", "порог «большой публичной» группы"),
    "PRAXIS_PANEL_GUEST_IDS": ("Поведение", "id гостей пульта через запятую — урезанный доступ "
                               "(без людей/дневника/почты/env; подхват живой, без рестарта)"),
    "PRAXIS_FLOOR_SKIP": ("Поведение", "паттерны через запятую, снятые с high-risk "
                          "классификации proposal; полномочия merge не меняет"),
}


def env_grouped() -> dict:
    """env_list() + группы/описания для UI. Неизвестные ключи — в «Прочее»."""
    items = env_list()
    for it in items:
        grp, desc = ENV_META.get(it.get("key", ""), ("Прочее", ""))
        it["group"], it["desc"] = grp, desc
    return {"items": items}


# --------------------------------------------------------------------------- #
#  PASS 6.1: контакты + окно отсутствия (конфиг «ответа в отсутствие»).
#  Только настройка: список важных, заметки (в её память о людях), ручной таймер.
#  Саму механику ответа это НЕ включает — движок появится отдельным пассом.
# --------------------------------------------------------------------------- #

ABSENCE_FILE = BASE / "memory" / "absence.json"
_SLUG_RE = re.compile(r"[^\w\s-]", re.UNICODE)


def _slugify(name: str) -> str:
    s = _SLUG_RE.sub("", (name or "").strip().lower())
    return re.sub(r"[\s-]+", "-", s) or "unknown"


def _absence_load() -> dict:
    try:
        return json.loads(ABSENCE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _absence_save(d: dict) -> None:
    ABSENCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ABSENCE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(ABSENCE_FILE)


def absence_state() -> dict:
    import time as _t
    d = _absence_load()
    w = d.get("window") or {}
    until = float(w.get("until", 0) or 0)
    active = until > _t.time()
    contacts = []
    for c in (d.get("contacts") or []):
        c = dict(c)
        try:  # PASS 12.0.a: показать в панели текущий статус (не хранится, вычисляется)
            import social as _social
            c["family"] = bool(c.get("id")) and _social.role_of(c["id"]) == "family"
        except Exception:
            c["family"] = False
        contacts.append(c)
    return {"active": active,
            "until": _dt.datetime.fromtimestamp(until).strftime("%H:%M %d.%m") if active else None,
            "hours": w.get("hours"),
            "schedule_note": str(d.get("schedule_note") or ""),   # PASS 12.1
            "contacts": contacts}


def absence_start(hours) -> dict:
    """Ручной таймер отсутствия: запускает ТОЛЬКО Егор, длительность — его. 0.5–72 ч."""
    import time as _t
    try:
        h = float(hours)
    except (TypeError, ValueError):
        return {"error": "часы — число"}
    if not (0.5 <= h <= 72):
        return {"error": "разумные пределы: 0.5–72 часа"}
    d = _absence_load()
    d["window"] = {"until": _t.time() + h * 3600, "hours": h,
                   "started": _dt.datetime.now().isoformat(timespec="seconds")}
    _absence_save(d)
    return absence_state()


def absence_schedule_note(text: str) -> dict:
    """PASS 12.1: свободный текст-записка на окно отсутствия — из неё грузится узкий контекст,
    когда она отвечает важному человеку от своего лица («если срочное — вечером на связи»)."""
    d = _absence_load()
    d["schedule_note"] = str(text or "").strip()[:1000]
    _absence_save(d)
    return absence_state()


def absence_stop() -> dict:
    d = _absence_load()
    d["window"] = None
    _absence_save(d)
    return absence_state()


def contacts_suggest() -> list[dict]:
    """Подсказки «из контактов»: её люди (memory/people) + впущенные (known_ids)."""
    out, seen = [], set()
    try:
        for p in sorted((BASE / "memory" / "people").glob("*.md")):
            if p.stem.startswith("_"):
                continue
            name = p.stem
            try:
                first = p.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
                if first.startswith("#"):
                    name = first.lstrip("# ").strip() or p.stem
            except Exception:
                pass
            out.append({"slug": p.stem, "name": name, "src": "people"})
            seen.add(_slugify(name))
    except Exception:
        pass
    try:
        known = json.loads((BASE / "memory" / "known_ids.json").read_text(encoding="utf-8"))
        for uid, name in (known.items() if isinstance(known, dict) else []):
            if _slugify(str(name)) not in seen:
                out.append({"slug": _slugify(str(name)), "name": str(name), "id": str(uid), "src": "known"})
    except Exception:
        pass
    return out[:60]


def contact_add(name: str, username: str = "", note: str = "", uid: str = "") -> dict:
    name = (name or "").strip()
    if not name:
        return {"error": "имя пустое"}
    slug = _slugify(name)
    d = _absence_load()
    contacts = d.get("contacts") or []
    if any(c.get("slug") == slug for c in contacts):
        return {"error": f"{name} уже в списке"}
    contacts.append({"slug": slug, "name": name, "username": (username or "").lstrip("@").strip(),
                     "id": (uid or "").strip(),
                     "added": _dt.datetime.now().isoformat(timespec="seconds")})
    d["contacts"] = contacts
    _absence_save(d)
    if (note or "").strip():
        contact_note(slug, note, name=name)
    return {"ok": True, "state": absence_state()}


def contact_remove(slug: str) -> dict:
    d = _absence_load()
    before = len(d.get("contacts") or [])
    d["contacts"] = [c for c in (d.get("contacts") or []) if c.get("slug") != slug]
    _absence_save(d)
    return {"ok": len(d["contacts"]) < before, "state": absence_state()}


def contact_note(slug: str, note: str, name: str = "") -> dict:
    """Заметка о важном человеке → в ЕЁ память о людях (people/<slug>.md, private).
    Один источник правды: то же место, куда пишет её remember; портрет подтянется сам."""
    note = (note or "").strip()
    if not note:
        return {"error": "пустая заметка"}
    import people as _people
    nm = name or next((c.get("name") for c in (_absence_load().get("contacts") or [])
                       if c.get("slug") == slug), slug)
    _people.append_fact(
        slug, nm, f"{note} _(от Егора, для связи в его отсутствие)_",
        "private", 2, source_ref="owner:panel",
    )
    return {"ok": True}


def contact_make_family(slug: str) -> dict:
    """PASS 12.0.a: сделать уже впущенного важного контакта семьёй одним нажатием.
    Закрывает обе дыры разом: кнопка «важный» писала только absence.json/досье, но не
    known_ids — здесь пишем и его (иначе role_of даже не дойдёт до чтения роли); и панель
    роль вообще не ставила — здесь ставим role: family. Нужен telegram id контакта."""
    import social as _social
    import people as _people
    c = next((x for x in (_absence_load().get("contacts") or []) if x.get("slug") == slug), None)
    if not c:
        return {"error": "нет такого контакта"}
    uid = (c.get("id") or "").strip()
    name = c.get("name") or slug
    if not uid:
        return {"error": f"у «{name}» не записан telegram id — семьёй по имени не сделать; "
                         f"добавь его заново, поделившись контактом (кнопка «важный»)."}
    _social.add_known(uid, name)
    # роль читается по people._slug(name) (тот же путь, что social.role_of), не по slug панели
    _people.set_role(_people._slug(name), name, "family")
    return {"ok": True, "state": absence_state()}


# --------------------------------------------------------------------------- #
#  PASS 6.2: читалка её памяти (заполненные маркдауны) + лента действий из лога
# --------------------------------------------------------------------------- #

MEM_READ_MAX = 80_000


def memory_tree() -> dict:
    """Обзор канонической памяти + PASS 19 life artifacts."""
    def _items(paths) -> list[dict]:
        out = []
        for p in paths:
            try:
                out.append({"path": str(p.relative_to(BASE)).replace("\\", "/"), "name": p.stem,
                            "size": p.stat().st_size,
                            "mtime": _dt.datetime.fromtimestamp(p.stat().st_mtime).strftime("%d.%m %H:%M")})
            except OSError:
                continue
        return out
    mem = BASE / "memory"
    life_dir = mem / "life"
    sections = [
        ("Жизнь · компакты", sorted((life_dir / "compacts").glob("*/*.md"), reverse=True)),
        ("Жизнь · эпизоды", sorted((life_dir / "episodes").glob("*/*.md"), reverse=True)),
        ("Жизнь · claims", sorted((life_dir / "claims").glob("*.md"), reverse=True)),
        ("Жизнь · патчи", sorted((life_dir / "patches").glob("*.md"), reverse=True)),
        ("Жизнь · рефлексии", sorted((life_dir / "reflections").glob("*.md"), reverse=True)),
        ("Люди", sorted((mem / "people").glob("*.md"))),
        ("Дневник", sorted((mem / "journal").glob("*.md"), reverse=True)),
        ("Комнаты", sorted((mem / "rooms").glob("*.md"))),
        ("Душа", [p for n in ("SOUL.md", "VOICE.md", "self.md") if (p := BASE / "soul" / n).exists()]),
        ("Скиллы", sorted((BASE / "soul" / "skills").glob("*.md"))),
        ("Архив души", sorted((BASE / "soul" / "archive").glob("*.md"))),
        ("Прочее", [p for n in ("reflections.md", "INDEX.md") if (p := mem / n).exists()]),
    ]
    return {"sections": [{"title": t, "items": _items(ps)} for t, ps in sections if ps],
            "life": life_state()}


def life_state() -> dict:
    """Digital-twin slice: facts only, all derived from JSONL/Markdown/state cursors."""
    try:
        import formation
        import memory_index
        import memory_life
        out = memory_life.status()
        out["formation"] = formation.status()
        out["recall_mode"] = memory_index.retrieval_mode()
        return out
    except Exception as e:
        log.warning("life_state не собрался", exc_info=True)
        return {"error": f"{type(e).__name__}: {str(e)[:160]}"}


def life_request(depth: str = "full") -> dict:
    """Mailbot writes a request; the main Praxis clock executes it outside live replies."""
    import formation
    if depth not in ("light", "full"):
        return {"ok": False, "error": "depth должен быть light или full"}
    req = formation.request(depth, reason="пульт памяти")
    return {"ok": True, "request": req, "state": life_state()}


def memory_read(rel: str) -> dict | None:
    """Содержимое одного файла памяти/души. Жёсткий path-guard: только memory/ и soul/, только .md/.json."""
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if not (rel.startswith("memory/") or rel.startswith("soul/")):
        return None
    p = (BASE / rel).resolve()
    try:
        p.relative_to(BASE.resolve())
    except ValueError:
        return None  # вылез за дом
    if p.suffix.lower() not in (".md", ".json") or not p.is_file():
        return None
    if p.name.lower() == "llm.json":
        return None  # ключи мозга — только маски через llm_get (PASS 8.2)
    text = p.read_text(encoding="utf-8", errors="ignore")
    if len(text) > MEM_READ_MAX:
        text = text[:MEM_READ_MAX] + f"\n… (обрезано, всего {len(text)} символов)"
    return {"path": rel, "name": p.stem, "text": text,
            "mtime": _dt.datetime.fromtimestamp(p.stat().st_mtime).strftime("%d.%m %H:%M")}


_ACTION_MARKS = (
    ("SHELL core-edit blocked", "blocked"), ("SHELL blocked", "blocked"),
    ("SHELL $", "shell"), ("self-commit", "commit"), ("RESTART_SELF", "restart"),
    ("manage_room", "room"), ("admit", "admit"), ("write_skill", "skill"),
)
_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}):\d{2}[,.]\d+\s+\w+\s+(.*)$")


def actions(limit: int = 200) -> dict:
    """Её действия руками — из praxis.log: shell, self-commit, рестарты, комнаты, скиллы."""
    p = LOGS_DIR / "praxis.log"
    if not p.exists():
        return {"items": [], "note": "лога ещё нет"}
    items = []
    try:
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()[-4000:]
    except OSError:
        return {"items": [], "note": "лог не прочитался"}
    for ln in lines:
        m = _LOG_TS_RE.match(ln)
        body = m.group(3) if m else ln
        for mark, kind in _ACTION_MARKS:
            if mark in body:
                items.append({"day": m.group(1)[5:] if m else "", "time": m.group(2) if m else "",
                              "kind": kind, "text": body.strip()[:300]})
                break
    items.reverse()
    return {"items": items[:max(1, min(limit, 500))]}


# --------------------------------------------------------------------------- #
#  PASS 6.3: гостевой доступ — урезанный скоуп без записей о людях
# --------------------------------------------------------------------------- #

def guest_ids() -> set[int]:
    """Id гостей пульта — из .env (живьём, правится через Параметры без рестарта)."""
    out: set[int] = set()
    try:
        for line in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("PRAXIS_PANEL_GUEST_IDS="):
                for tok in line.split("=", 1)[1].replace(";", ",").split(","):
                    tok = tok.strip()
                    if tok.isdigit():
                        out.add(int(tok))
    except Exception:
        pass
    return out


_GUEST_MEM_SECTIONS = ("Душа", "Скиллы", "Архив души")
_GUEST_ACTION_KINDS = ("shell", "blocked", "commit", "restart", "skill")


def memory_tree_scoped(role: str) -> dict:
    t = memory_tree()
    if role == "owner":
        return t
    return {"sections": [s for s in t["sections"] if s["title"] in _GUEST_MEM_SECTIONS]}


def memory_read_scoped(rel: str, role: str) -> dict | None:
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if role != "owner" and not rel.startswith("soul/"):
        return None  # гостю — только душа (она и так в публичном репо); memory/* — записи о людях
    return memory_read(rel)


def actions_scoped(role: str) -> dict:
    a = actions()
    if role == "owner":
        return a
    return {"items": [i for i in a["items"] if i["kind"] in _GUEST_ACTION_KINDS]}


def pulse_scoped(role: str) -> dict:
    p = pulse()
    if role == "owner":
        return p
    return {"heartbeat": {"note": "жива"} if p.get("heartbeat") else None,
            "tasks": [], "consolidations": [{"chat": "…", "mtime": c["mtime"], "chars": c["chars"]}
                                            for c in p.get("consolidations", [])],
            "voice_log": p.get("voice_log")}


# --------------------------------------------------------------------------- #
#  PASS 7: предложения к её коду (контур selfdev) — плитка и карточки.
#  Логика в selfdev.py; здесь — только read-модель и тонкие действия для UI.
# --------------------------------------------------------------------------- #

def proposals_list() -> dict:
    import selfdev
    items = []
    for t in reversed(selfdev.all_items(30)):
        tests = t.get("tests") or {}
        items.append({
            "id": t.get("id"), "title": t.get("title") or "", "why": t.get("why") or "",
            "status": t.get("status"), "zone": t.get("zone"),
            "files": t.get("files") or [], "diffstat": t.get("diffstat") or "",
            "tests_ok": bool(tests.get("ok")), "tests": (tests.get("summary") or "").splitlines()[:1],
            "created": t.get("created"), "decided_by": t.get("decided_by") or "",
            "reason": t.get("reason") or "",
            "review": t.get("review") or "",     # PASS 16.4: её собственное ревью диффа
            "checked": t.get("checked") or "",   # и чем проверено
        })
    return {"items": items, "pending": sum(1 for i in items if i["status"] == "proposed")}


def proposal_view(pid: str) -> dict | None:
    import selfdev
    t = selfdev.get(str(pid))
    if not t:
        return None
    out = dict(t)
    out["diff"] = selfdev.diff_text(str(pid))
    return out


def proposal_apply(pid: str) -> dict:
    import selfdev
    return selfdev.apply(str(pid), by="egor")


def proposal_reject(pid: str, reason: str = "") -> dict:
    import selfdev
    return selfdev.reject(str(pid), reason, by="egor")


# --------------------------------------------------------------------------- #
#  PASS 8.6: плитка «Граф памяти» — узлы (люди/темы) + рёбра для 3D-рендера.
#  Owner-only (гость 403): карта «кто с кем связан» — часть памяти Егора.
# --------------------------------------------------------------------------- #

def memgraph() -> dict:
    """{nodes: [{id, label, kind: people|topic, degree}], links: [{a, b, label}]}.
    Пустой граф — честные пустые списки (плитка не падает)."""
    import graph as _graph
    import people as _people
    try:
        edges = _graph.edges()
    except Exception:
        log.warning("memgraph: рёбра не прочитались", exc_info=True)
        edges = []
    people_slugs: dict[str, str] = {}
    try:
        for p in sorted(_people.PEOPLE_DIR.glob("*.md")):
            if p.stem.startswith("_"):
                continue
            title, _ = _people.read(p.stem)
            people_slugs[p.stem] = title or p.stem
    except Exception:
        log.debug("memgraph: люди не прочитались", exc_info=True)
    nodes: dict[str, dict] = {}

    def _node(nid: str) -> dict:
        return nodes.setdefault(nid, {
            "id": nid, "label": people_slugs.get(nid, nid),
            "kind": "people" if nid in people_slugs else "topic", "degree": 0})

    links = []
    for e in edges:
        _node(e["a"])["degree"] += 1
        _node(e["b"])["degree"] += 1
        links.append({"a": e["a"], "b": e["b"], "label": e.get("label") or ""})
    for slug in people_slugs:
        _node(slug)  # люди без рёбер — тоже узлы (одинокие видны честно)
    return {"nodes": list(nodes.values()), "links": links}


# --------------------------------------------------------------------------- #
#  PASS 8.2: плитка «Мозг» — конфиг llm.json (роли/фреймворки/лимиты) с пульта.
#  Ключи наружу не отдаются (маска ••••xxxx), пустой ключ в apply = «не менять».
# --------------------------------------------------------------------------- #

_LLM_FRAMEWORKS = ("anthropic", "openai")
_LLM_ROLES = ("voice", "evaluator")

# Короткий каталог для комбобокса в пульте. Это не allowlist: поле модели остаётся
# редактируемым, а каталог лишь даёт безопасный выбор в один тап. Живой relay всё равно
# является источником правды и проверяется кнопкой «Проверить»/ротацией в llm.py.
_LLM_MODEL_OPTIONS = {
    "openai": (
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
        "gpt-5.5", "gpt-5.4", "gpt-5.4-mini",
    ),
    "anthropic": (),
}

# Do not re-offer aliases known to fail even when an old llm.json still contains
# one. The role value remains visible/editable; runtime rotation handles migration.
_LLM_RETIRED_MODEL_ALIASES = {"openai": {"gpt-5.6"}}


def _journal_panel(msg: str) -> None:
    """Строка ей в дневник о действии Егора с пульта (без ключей!)."""
    try:
        jd = BASE / "memory" / "journal"
        jd.mkdir(parents=True, exist_ok=True)
        p = jd / f"{_dt.date.today().isoformat()}.md"
        if not p.exists():
            p.write_text(f"# {_dt.date.today().isoformat()}\n\n", encoding="utf-8")
        with p.open("a", encoding="utf-8") as fh:
            fh.write(f"- {_dt.datetime.now():%H:%M} (s2) [пульт] {msg}\n")
    except Exception:
        log.debug("journal панели не удался", exc_info=True)


def _mask_key(key: str) -> str:
    key = key or ""
    return ("••••" + key[-4:]) if key else ""


def llm_get() -> dict:
    """Конфиг мозга для UI: роли (+живой снапшот on_fallback/last_error), фреймворки с
    масками ключей, лимиты. Сами ключи наружу НЕ отдаются."""
    import llm
    cfg = llm._config()
    snap = llm.snapshot()
    frameworks = {}
    for fw in _LLM_FRAMEWORKS:
        f = (cfg.get("frameworks") or {}).get(fw) or {}
        frameworks[fw] = {"base_url": f.get("base_url", ""),
                          "key_mask": _mask_key(f.get("api_key", "")),
                          "has_key": bool(f.get("api_key"))}
    roles = {}
    for role in _LLM_ROLES:
        r = dict((cfg.get("roles") or {}).get(role) or {})
        r.update({k: snap.get(role, {}).get(k) for k in ("on_fallback", "last_error", "fallback_armed")})
        roles[role] = r
    # Не теряем пользовательские/провайдерские имена: текущие модели тоже показываем
    # рядом с известными вариантами, даже если каталог пульта их ещё не знает.
    model_options = {fw: list(_LLM_MODEL_OPTIONS.get(fw, ())) for fw in _LLM_FRAMEWORKS}
    for r in roles.values():
        fw = r.get("framework")
        model = str(r.get("model") or "").strip()
        if (fw in model_options and model and model not in model_options[fw]
                and model not in _LLM_RETIRED_MODEL_ALIASES.get(fw, set())):
            model_options[fw].append(model)
        other = "openai" if fw == "anthropic" else "anthropic"
        fallback = str(r.get("fallback_model") or "").strip()
        if (other in model_options and fallback and fallback not in model_options[other]
                and fallback not in _LLM_RETIRED_MODEL_ALIASES.get(other, set())):
            model_options[other].append(fallback)
    return {"frameworks": frameworks, "roles": roles,
            "model_options": model_options,
            "limits": dict(cfg.get("limits") or {})}


# PASS 12.2: http:// допустим ТОЛЬКО для локальных адресов (localhost-фолбэк «мозга» в
# контейнере). Иначе ключ ушёл бы открытым на случайный http-хост — всё прочее требует https://.
_LOCAL_HTTP_HOSTS = ("127.0.0.1", "localhost", "host.docker.internal")


def _base_url_ok(u: str) -> bool:
    if u.startswith("https://"):
        return True
    if u.startswith("http://"):
        from urllib.parse import urlparse
        try:
            return (urlparse(u).hostname or "").lower() in _LOCAL_HTTP_HOSTS
        except Exception:
            return False
    return False


def _llm_validate(changes: dict) -> tuple[dict, list[str]]:
    """Отвалидировать и НОРМАЛИЗОВАТЬ изменения для llm.update_config. -> (clean, errors)."""
    clean: dict = {}
    errors: list[str] = []
    for fw, f in (changes.get("frameworks") or {}).items():
        if fw not in _LLM_FRAMEWORKS:
            errors.append(f"фреймворк {fw!r}: не знаю такого")
            continue
        if not isinstance(f, dict):
            continue
        out = {}
        if "base_url" in f:
            u = str(f["base_url"] or "").strip()
            if not _base_url_ok(u):
                errors.append(f"{fw}.base_url: нужен https:// "
                              f"(http:// только для localhost/127.0.0.1/host.docker.internal)")
            else:
                out["base_url"] = u
        key = str(f.get("api_key") or "").strip()
        if key:  # пустая строка = «не менять» (ключ write-only)
            out["api_key"] = key
        if out:
            clean.setdefault("frameworks", {})[fw] = out
    for role, r in (changes.get("roles") or {}).items():
        if role not in _LLM_ROLES:
            errors.append(f"роль {role!r}: не знаю такой")
            continue
        if not isinstance(r, dict):
            continue
        out = {}
        if "framework" in r:
            if r["framework"] not in _LLM_FRAMEWORKS:
                errors.append(f"{role}.framework: только anthropic|openai")
            else:
                out["framework"] = r["framework"]
        if "model" in r:
            m = str(r["model"] or "").strip()
            if not m:
                errors.append(f"{role}.model: пустой")
            else:
                out["model"] = m
        if "fallback_model" in r:
            out["fallback_model"] = str(r["fallback_model"] or "").strip()
        if "max_tokens" in r:
            try:
                out["max_tokens"] = max(64, min(32768, int(r["max_tokens"])))
            except (TypeError, ValueError):
                errors.append(f"{role}.max_tokens: не число")
        if out:
            clean.setdefault("roles", {})[role] = out
    lim = changes.get("limits") or {}
    if isinstance(lim, dict):
        out = {}
        if "max_tool_iters" in lim:
            try:
                v = int(lim["max_tool_iters"])
                if not (1 <= v <= 600):
                    raise ValueError
                out["max_tool_iters"] = v
            except (TypeError, ValueError):
                errors.append("max_tool_iters: целое 1–600")
        if "evaluator_mode" in lim:
            errors.append("evaluator_mode удалён: оценщика речи больше нет")
        if "windows_per_day" in lim:
            errors.append("windows_per_day удалён: автономный pulse не имеет поведенческого капа")
        # Эти ручки удалены, а не просто спрятаны в новом UI: старый клиент панели
        # или прямой POST не может вернуть кодовое управление голосом/участием.
        if "group_mode" in lim:
            errors.append("group_mode удалён: участие задаётся room policy и решением Praxis")
        if "presence_cooldown_sec" in lim:
            errors.append("presence_cooldown_sec удалён: batching настраивается транспортом комнаты")
        if "drift_action" in lim:
            errors.append("drift_action удалён: telemetry не управляет комнатой")
        if out:
            clean["limits"] = out
    return clean, errors


def _llm_diff_summary(old: dict, clean: dict) -> str:
    """Краткий дифф для дневника — БЕЗ значений ключей."""
    parts = []
    for fw, f in (clean.get("frameworks") or {}).items():
        if "base_url" in f and f["base_url"] != (old["frameworks"].get(fw) or {}).get("base_url"):
            parts.append(f"{fw}.base_url → {f['base_url']}")
        if "api_key" in f:
            parts.append(f"{fw}.api_key обновлён")
    for role, r in (clean.get("roles") or {}).items():
        o = old["roles"].get(role) or {}
        for k in ("framework", "model", "fallback_model", "max_tokens"):
            if k in r and r[k] != o.get(k):
                parts.append(f"{role}.{k}: {o.get(k)} → {r[k]}")
    for k, v in (clean.get("limits") or {}).items():
        if v != (old.get("limits") or {}).get(k):
            parts.append(f"{k}: {(old.get('limits') or {}).get(k)} → {v}")
    return "; ".join(parts)


def llm_set(changes: dict) -> dict:
    """Применить изменения конфига мозга (плитка «Мозг»). Валидация → атомарная запись
    (llm.save_config) → событие ей в дневник (краткий дифф без ключей)."""
    import llm
    clean, errors = _llm_validate(changes or {})
    if errors:
        return {"ok": False, "errors": errors}
    if not clean:
        return {"ok": False, "errors": ["нет изменений"]}
    old = llm._config()
    diff = _llm_diff_summary(old, clean)
    llm.update_config(clean)
    if diff:
        _journal_panel(f"Егор сменил конфиг мозга: {diff}")
        log.info("панель: конфиг мозга изменён (%s)", diff)
    return {"ok": True, "changed": diff, "state": llm_get()}


def llm_ping(role: str) -> dict:
    """Кнопка «проверить»: 1-токенный вызов основного канала роли. -> {ok, error}."""
    import llm
    role = (role or "").strip()
    if role not in _LLM_ROLES:
        return {"ok": False, "error": f"нет роли {role!r}"}
    ok, err = llm.ping(role)
    return {"ok": ok, "error": err}


def llm_usage() -> dict:
    """PASS 9.1: секция «Расход» плитки «Мозг» — 7 дней по ролям (+сегодня отдельно).

    Деньги считаются ТОЛЬКО если в llm.json руками задан блок pricing
    {"<model>": {"in_per_1m": X, "out_per_1m": Y}}; без него — честно одни токены,
    выдуманных прайсов нет."""
    import llm
    days = llm.usage_days(7)
    pricing = llm.pricing()
    cfg = llm._config()
    models = {r: ((cfg.get("roles") or {}).get(r) or {}).get("model") or "" for r in _LLM_ROLES}
    cost: dict = {}
    if pricing:
        for day, roles in days.items():
            for role, d in (roles or {}).items():
                if not isinstance(d, dict):
                    continue
                # 18.5: если есть по-модельный подразрез (день→роль→models) — ценим по
                # ФАКТИЧЕСКОЙ модели вызова; старые дни без него — по текущей модели роли.
                by_model = d.get("models") if isinstance(d.get("models"), dict) else {}
                try:
                    if by_model:
                        c = 0.0
                        for mname, m in by_model.items():
                            p = pricing.get(mname) or {}
                            if isinstance(p, dict) and isinstance(m, dict):
                                c += (int(m.get("in", 0)) * float(p.get("in_per_1m", 0)) +
                                      int(m.get("out", 0)) * float(p.get("out_per_1m", 0))) / 1e6
                    else:
                        p = pricing.get(models.get(role, "")) or {}
                        if not isinstance(p, dict):
                            continue
                        c = (int(d.get("in", 0)) * float(p.get("in_per_1m", 0)) +
                             int(d.get("out", 0)) * float(p.get("out_per_1m", 0))) / 1e6
                except (TypeError, ValueError):
                    continue
                cost.setdefault(day, {})[role] = round(c, 4)
    return {"days": days, "models": models, "priced": bool(pricing), "cost": cost}


def appetite_state() -> dict:
    """PASS 18.5: секция «Аппетит» плитки «Мозг» — договор, обещание, наблюдаемое, отказы."""
    import appetite
    import rails
    s = appetite.state()
    s["promise_check"] = appetite.promise_check()
    s["background_hold"] = appetite.background_hold()
    s["denials"] = rails.recent_denials(8)
    s["denials_today"] = rails.denials_today()
    return s


def appetite_intent(kind: str, text: str = "") -> dict:
    """Кнопки «не экономь / умерь / останови фон» (kind) и свободное поле (kind=text).
    Пишется как слово Егора; толкование — за Praxis (manage_appetite), код не решает."""
    import appetite
    kind = (kind or "").strip().lower()
    if kind not in appetite.REQUEST_KINDS:
        return {"ok": False, "error": f"kind не из {appetite.REQUEST_KINDS}"}
    if kind == "text" and not (text or "").strip():
        return {"ok": False, "error": "пустая просьба"}
    req = appetite.set_owner_request(kind, text, source="panel")
    _journal_panel(f"просьба об аппетитах: {kind}" + (f" «{(text or '')[:80]}»" if (text or "").strip() else ""))
    return {"ok": True, "request": req}


# --------------------------------------------------------------------------- #
#  PASS 20–22 → пульт (24·Ф3): органы самости получают наблюдение и управление
# --------------------------------------------------------------------------- #
def identity_state() -> dict:
    """Плитка «Я»: слои/версии души, стресс-интегралы, сдвиги, «кто я сейчас»."""
    import identity
    return identity.panel_state()


def identity_rollback(name: str, version: int) -> dict:
    """Откат Егора постфактум: текущий текст сам архивируется, история цела."""
    import identity
    res = identity.rollback(name, int(version), by="egor", reason="откат Егора с пульта")
    if res.get("ok"):
        _journal_panel(f"откат души: {name} к v{version} (стал v{res['version']})")
    return res


def perception_state() -> dict:
    """Плитка «Восприятие»: рычаги (значение/источник/границы) + журнал пропусков."""
    import perception
    return perception.panel_state()


def perception_set(knob: str, value=None, reset: bool = False) -> dict:
    """Правка Егора: источник честно «egor» — она видит и в таблице, и в журнале."""
    import perception
    if reset:
        res = perception.reset_knob(knob, by="egor")
    else:
        res = perception.set_knob(knob, value, by="egor", reason="пульт")
    if res.get("ok"):
        _journal_panel(f"рычаг восприятия {knob}: {res.get('old')} → {res.get('new')}"
                       + (" (сброс)" if reset else ""))
    return res


def brain_catalog() -> dict:
    """Плитка «Мозг»: каталог + per-model наблюдения + её последние свитчи."""
    import brain
    cat = brain.catalog()
    try:
        import memory_life as life
        evs = [e for e in life.iter_events(kinds={"brain_switch"}) if e.get("chat_id") is None]
        cat["switches"] = list(reversed(evs[-5:]))
    except Exception:
        cat["switches"] = []
    return cat
