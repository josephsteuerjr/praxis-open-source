"""Semantic and test intelligence for Praxis Forge (PASS 23.1 completion).

This module deliberately stays useful without an installed language server.  It builds a
factual project model from manifests and source syntax, then exposes the same normalized
facts for the owner process and fresh-context workers.  Optional LSP binaries are reported
as accelerators; correctness never depends on them being installed.
"""

from __future__ import annotations

import ast
import bisect
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Iterable


SKIP_DIRS = {
    ".git", ".hg", ".svn", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".venv", "venv", "node_modules", "target", "dist", "build", "__pycache__",
    ".forge-worktrees",
}
LANG_BY_EXT = {
    ".py": "python", ".pyi": "python", ".rs": "rust", ".ts": "typescript",
    ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript", ".go": "go",
    ".cs": "csharp", ".java": "java", ".rb": "ruby", ".php": "php",
    ".sh": "shell", ".ps1": "powershell",
}
OVERVIEW_TEXT_SUFFIXES = set(LANG_BY_EXT) | {
    ".md", ".rst", ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg",
    ".html", ".css", ".scss", ".sql", ".xml",
}
OVERVIEW_TEXT_NAMES = {
    "Dockerfile", "Makefile", "justfile", "Procfile", "go.mod", "Cargo.toml",
    "package.json", "requirements.txt", "pyproject.toml",
}
LSP_BINARIES = {
    "python": ("pyright-langserver", "basedpyright-langserver", "pylsp"),
    "rust": ("rust-analyzer",),
    "typescript": ("typescript-language-server",),
    "go": ("gopls",),
    "csharp": ("omnisharp", "csharp-ls"),
}
_GENERIC_SYMBOLS = {
    "rust": re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(struct|enum|trait|fn|mod|type|const|static)\s+([A-Za-z_][A-Za-z0-9_]*)"),
    "typescript": re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(class|interface|type|enum|function|const|let|var)\s+([A-Za-z_$][\w$]*)"),
    "javascript": re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(class|function|const|let|var)\s+([A-Za-z_$][\w$]*)"),
    "go": re.compile(r"^\s*(type|func|const|var)\s+(?:\([^)]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)"),
    "csharp": re.compile(r"^\s*(?:public|private|protected|internal|static|sealed|abstract|partial|async|\s)+\s*(class|interface|record|struct|enum|[A-Za-z_][\w<>?,\[\]]*)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(|[:{])"),
    "java": re.compile(r"^\s*(?:public|private|protected|static|final|abstract|synchronized|native|\s)+\s*(class|interface|record|enum|[A-Za-z_][\w<>?,\[\]]*)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(|extends|implements|\{)"),
}


def _env_number(name: str, default: float) -> float:
    """Порог из среды не имеет права уронить импорт: одна строка `20m` в .env — и модуля нет."""
    try:
        raw = (os.environ.get(name) or "").strip()
        return float(raw) if raw else float(default)
    except (TypeError, ValueError):
        return float(default)


# Бюджет обхода. 26.07 один `impact('/tmp')` держал ядро 793с и вернул картину по 11% дерева
# БЕЗ признака усечения — она читала частичный ответ как полный. Числа названы здесь, ездят
# в ответе (Budget.report) и крутятся из среды.
SCAN_SECONDS = _env_number("PRAXIS_FORGE_SCAN_SECONDS", 90.0)
SCAN_FILES = max(1, int(_env_number("PRAXIS_FORGE_SCAN_FILES", 12000)))
# Досчёт «сколько ещё в дереве» после упора в кап — тоже стоит времени; ограничен своей долей.
_TAIL_COUNT_SECONDS = 5.0


class Budget:
    """Сколько файлов и сколько секунд можно потратить — и что из этого связало ответ.

    Существует не чтобы ЗАПРЕТИТЬ обход, а чтобы ответ приходил вовремя и сам говорил,
    чего в нём нет. Пустой `stages` означает «обход не запускался», а не «всё посмотрела».
    """

    def __init__(self, *, seconds: float | None = None, files: int | None = None,
                 deadline: float | None = None, label: str = "") -> None:
        self.label = str(label or "")
        self.files = max(1, int(files)) if files else SCAN_FILES
        now = time.monotonic()
        if deadline is not None:
            self.seconds = max(1.0, float(deadline) - now)
        else:
            self.seconds = float(seconds) if seconds and seconds > 0 else SCAN_SECONDS
        self.started = now
        self.deadline = now + self.seconds
        self.stages: list[dict] = []

    def left(self) -> float:
        return self.deadline - time.monotonic()

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline

    def spent(self) -> float:
        return round(time.monotonic() - self.started, 3)

    def stage(self, name: str, *, taken: int, seen: int | None = None, exhausted: bool = True,
              stopped: str = "", exact: bool = True) -> None:
        self.stages.append({"stage": str(name), "taken": int(taken),
                            "seen": None if seen is None else int(seen),
                            "seen_exact": bool(exact), "exhausted": bool(exhausted),
                            "stopped_by": str(stopped)})

    def stopped_by(self) -> str:
        return next((row["stopped_by"] for row in self.stages if row.get("stopped_by")), "")

    def mark(self) -> int:
        """Сколько этапов уже записано.

        Кошелёк один на весь снимок, поэтому `complete()` говорит про ВЕСЬ снимок. Шагу
        внутри снимка (diagnostics в review) этого мало: ему надо знать, довёл ли он до конца
        СВОЙ обход, чтобы не выдавать чужое усечение за своё и наоборот.
        """
        return len(self.stages)

    def complete_since(self, mark: int) -> bool:
        return all(row.get("exhausted") for row in self.stages[max(0, int(mark)):])

    def complete(self) -> bool:
        # Пустой список этапов = обход не запускался (ранний возврат, ошибка аргумента). Такой
        # ответ не «частичный» — ему нечего было усекать; `note` про это скажет прямо.
        return all(row.get("exhausted") for row in self.stages)

    def note(self) -> str:
        limits = f"бюджет {self.files} файлов / {round(self.seconds, 1)}с"
        if not self.stages:
            return f"{limits} не понадобился: обход дерева не запускался"
        pieces = []
        for row in self.stages:
            seen = row.get("seen")
            of = "?" if seen is None else (str(seen) if row.get("seen_exact", True) else f"≥{seen}")
            pieces.append(f"{row['stage']}: {row['taken']} из {of}")
        head = "; ".join(pieces)
        if self.complete():
            return f"ответ полный — {head}; {limits} не связывал, потрачено {self.spent()}с"
        cause = {"file-cap": "кап файлов", "deadline": "срок", "hit-cap": "кап находок",
                 "symbol-cap": "кап символов", "reference-cap": "кап ссылок",
                 "diagnostic-cap": "кап диагностик",
                 "io-error": "ошибку файловой системы"}.get(self.stopped_by(), "предел")
        return (f"ответ ЧАСТИЧНЫЙ — {head}; упёрлась в {cause} ({limits}, потрачено {self.spent()}с). "
                "Чего нет в ответе — не значит, что этого нет в дереве. Сузь корень или путь, "
                "либо подними PRAXIS_FORGE_SCAN_FILES / PRAXIS_FORGE_SCAN_SECONDS.")

    def report(self) -> dict:
        return {"complete": self.complete(), "stopped_by": self.stopped_by(),
                "file_cap": self.files, "time_budget_s": round(self.seconds, 1),
                "spent_s": self.spent(), "stages": list(self.stages), "note": self.note()}


class _ModuleIndex:
    """Разрешение импорта в модули по префиксам вместо перебора всех имён на КАЖДЫЙ импорт.

    Было `[name for name in modules if …]` внутри цикла «модули × их импорты»: на 12000 файлов
    это ~13 минут чистого CPU (замер на проде 26.07). Отношения те же три — точное имя, предки
    импорта и его потомки; ни одно не потеряно, изменилась только сложность.
    """

    def __init__(self, names: Iterable[str]) -> None:
        self._exact = set(names)
        self._sorted = sorted(self._exact)

    def resolve(self, dep: str) -> list[str]:
        found: list[str] = []
        if dep in self._exact:
            found.append(dep)
        head = dep
        while "." in head:                      # dep.startswith(name + ".") — предки импорта
            head = head.rsplit(".", 1)[0]
            if head in self._exact:
                found.append(head)
        prefix = dep + "."                      # name.startswith(dep + ".") — его потомки
        position = bisect.bisect_left(self._sorted, prefix)
        while position < len(self._sorted) and self._sorted[position].startswith(prefix):
            found.append(self._sorted[position])
            position += 1
        return found


def _ignored(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True
    return any(part in SKIP_DIRS for part in rel.parts)


def source_files(root: Path, *, limit: int = 20000, suffixes: set[str] | None = None,
                 budget: Budget | None = None, stage: str = "файлы") -> list[Path]:
    """Файлы дерева под капом. С `budget` ещё и честно записывает, сколько НЕ посмотрено."""
    root = root.resolve()
    if budget is not None:
        limit = min(int(limit), budget.files)
    out: list[Path] = []
    seen = 0
    exact = True
    stopped = ""
    git = _git_root(root)
    if git is not None:
        try:
            listed = subprocess.run(
                ["git", "-C", str(git), "ls-files", "--cached", "--others",
                 "--exclude-standard", "-z"],
                capture_output=True, text=True, errors="replace", timeout=30,
            )
            if listed.returncode == 0:
                rows = [raw for raw in listed.stdout.split("\0") if raw]
                seen = len(rows)               # git знает дерево целиком — числитель точный
                for raw in rows:
                    if len(out) >= limit:
                        stopped = "file-cap"
                        break
                    if budget is not None and budget.expired():
                        stopped = "deadline"
                        break
                    path = (git / raw).resolve()
                    try:
                        path.relative_to(root)
                    except ValueError:
                        continue
                    if not path.is_file() or _ignored(path, root):
                        continue
                    if suffixes is not None and path.suffix.lower() not in suffixes:
                        continue
                    out.append(path)
                if budget is not None:
                    budget.stage(f"{stage} (git)", taken=len(out), seen=seen, exact=True,
                                 exhausted=not stopped, stopped=stopped)
                return out
        except Exception:
            pass
    walker = None
    try:
        walker = root.rglob("*")
        for path in walker:
            seen += 1
            if len(out) >= limit:
                stopped = "file-cap"
                break
            if budget is not None and budget.expired():
                stopped = "deadline"
                break
            if not path.is_file() or _ignored(path, root):
                continue
            if suffixes is not None and path.suffix.lower() not in suffixes:
                continue
            out.append(path)
        else:
            walker = None                      # дерево кончилось само — знаменатель точный
    except OSError:
        # Обход оборвался на ошибке ФС: раньше это молча выглядело как «дерево кончилось».
        exact = False
        stopped = stopped or "io-error"
        walker = None
    if stopped and walker is not None:
        # Знаменатель «из скольких» дороже нуля: досчитываем хвост дерева, но недолго, и если
        # не успели — говорим «≥», а не выдаём нижнюю границу за точное число.
        share = _TAIL_COUNT_SECONDS if budget is None else min(_TAIL_COUNT_SECONDS,
                                                               max(.5, budget.left() * .1))
        tail_deadline = time.monotonic() + share
        try:
            for _ in walker:
                seen += 1
                if time.monotonic() > tail_deadline:
                    exact = False
                    break
        except OSError:
            exact = False
    if budget is not None:
        budget.stage(stage, taken=len(out), seen=seen, exact=exact,
                     exhausted=not stopped, stopped=stopped)
    return out


def _small_text(path: Path, size: int) -> str:
    """Read a bounded text candidate; never turn an executable/model/archive into fake lines."""
    if size > 2 * 1024 * 1024:
        return ""
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if b"\0" in raw[:8192]:
        return ""
    return raw.decode("utf-8", errors="replace")


def _git_root(root: Path) -> Path | None:
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, errors="replace", timeout=15)
        return Path(r.stdout.strip()).resolve() if r.returncode == 0 and r.stdout.strip() else None
    except Exception:
        return None


def changed_files(root: Path, base: str = "") -> list[str]:
    """Return changed paths relative to the task root, including untracked files."""
    root = root.resolve()
    git = _git_root(root)
    if git is None:
        return []
    names: list[str] = []
    commands = [["git", "-C", str(git), "diff", "--name-only", "--diff-filter=ACMR"]]
    if base:
        commands.insert(0, ["git", "-C", str(git), "diff", "--name-only", "--diff-filter=ACMR", base])
    commands.append(["git", "-C", str(git), "ls-files", "--others", "--exclude-standard"])
    for argv in commands:
        try:
            r = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=20)
        except Exception:
            continue
        if r.returncode not in (0, 1):
            continue
        for raw in r.stdout.splitlines():
            candidate = (git / raw.strip()).resolve()
            try:
                rel = candidate.relative_to(root)
            except ValueError:
                continue
            if str(rel) not in names:
                names.append(str(rel).replace(os.sep, "/"))
    return names


def project_model(root: Path, *, budget: Budget | None = None) -> dict:
    root = root.resolve()
    own = budget or Budget(label="model")
    files = source_files(root, budget=own, stage="файлы (модель проекта)")
    languages: dict[str, int] = {}
    for path in files:
        lang = LANG_BY_EXT.get(path.suffix.lower())
        if lang:
            languages[lang] = languages.get(lang, 0) + 1
    manifests = [name for name in (
        "pyproject.toml", "requirements.txt", "setup.cfg", "pytest.ini", "package.json",
        "Cargo.toml", "go.mod", "Makefile", "justfile", "pom.xml", "build.gradle",
    ) if (root / name).is_file()]
    adapters = []
    for language in sorted(languages):
        bins = LSP_BINARIES.get(language, ())
        found = next((binary for binary in bins if shutil.which(binary)), "")
        adapters.append({"language": language, "mode": "lsp+syntax" if found else "syntax",
                         "server": found})
    return {
        "root": str(root), "files_scanned": len(files), "languages": languages,
        "manifests": manifests, "adapters": adapters, "limits": own.report(),
    }


def project_overview(root: Path, *, limit: int = 20000, budget: Budget | None = None) -> dict:
    """One factual architecture snapshot instead of a pile of broad file reads."""
    root = root.resolve()
    own = budget or Budget(label="overview")
    files = source_files(root, limit=limit, budget=own, stage="файлы (обзор)")
    tree: dict[str, dict[str, int]] = {}
    hotspots: list[dict] = []
    entrypoints: list[str] = []
    tests: list[str] = []
    docs: list[str] = []
    py_modules: dict[str, Path] = {}
    imports: dict[str, set[str]] = {}

    read = 0
    stopped = ""
    for path in files:
        if own.expired():
            stopped = "deadline"
            break
        read += 1
        rel = str(path.relative_to(root)).replace(os.sep, "/")
        top = rel.split("/", 1)[0] if "/" in rel else "."
        node = tree.setdefault(top, {"files": 0, "bytes": 0, "lines": 0})
        try:
            size = path.stat().st_size
        except OSError:
            continue
        node["files"] += 1
        node["bytes"] += size
        lines = 0
        text = (_small_text(path, size)
                if path.suffix.casefold() in OVERVIEW_TEXT_SUFFIXES
                or path.name in OVERVIEW_TEXT_NAMES else "")
        if text:
            lines = text.count("\n") + (1 if not text.endswith("\n") else 0)
        node["lines"] += lines
        if lines:
            hotspots.append({"path": rel, "lines": lines, "bytes": size})
        low = path.name.casefold()
        if low.startswith("test_") or "/test" in f"/{rel.casefold()}":
            tests.append(rel)
        if path.suffix.casefold() in {".md", ".rst"}:
            docs.append(rel)
        if path.suffix.casefold() == ".py":
            module = _python_module(path, root)
            py_modules[module] = path
            if ("if __name__" in text and "__main__" in text) or re.search(
                    r"(?m)^\s*(?:async\s+)?def\s+main\s*\(", text):
                entrypoints.append(rel)
            try:
                parsed = ast.parse(text, filename=str(path))
                deps: set[str] = set()
                for node_ast in ast.walk(parsed):
                    if isinstance(node_ast, ast.Import):
                        deps.update(alias.name for alias in node_ast.names)
                    elif isinstance(node_ast, ast.ImportFrom) and node_ast.module:
                        deps.add(node_ast.module)
                imports[module] = deps
            except (SyntaxError, ValueError):
                pass
        elif path.name in {"package.json", "Cargo.toml", "go.mod", "pom.xml"}:
            entrypoints.append(rel)

    own.stage("разбор файлов", taken=read, seen=len(files), exhausted=not stopped, stopped=stopped)
    reverse: dict[str, set[str]] = {name: set() for name in py_modules}
    index = _ModuleIndex(py_modules)
    for consumer, deps in imports.items():
        for dep in deps:
            for candidate in index.resolve(dep):
                reverse[candidate].add(consumer)
    central = sorted(
        ({"module": module, "path": str(path.relative_to(root)).replace(os.sep, "/"),
          "imported_by": len(reverse.get(module, ())), "imports": len(imports.get(module, ())) }
         for module, path in py_modules.items()),
        key=lambda row: (-row["imported_by"], -row["imports"], row["path"]),
    )
    hotspots.sort(key=lambda row: (-row["lines"], row["path"]))
    changed = changed_files(root)
    return {
        "model": project_model(root, budget=own),
        "tree": dict(sorted(tree.items(), key=lambda item: (-item[1]["lines"], item[0]))),
        "entrypoints": sorted(set(entrypoints))[:100],
        "central_modules": central[:40],
        "hotspots": hotspots[:40],
        "tests": {"count": len(set(tests)), "sample": sorted(set(tests))[:80]},
        "docs": sorted(set(docs))[:80],
        "working_tree": changed,
        "truncated": len(files) >= limit or not own.complete(),
        "limits": own.report(),
    }


def review_snapshot(root: Path, base: str = "", *, budget: Budget | None = None) -> dict:
    """Deterministic review surface: churn, impact, diagnostics, tests and explicit risks."""
    root = root.resolve()
    # Один бюджет на весь снимок: три собственных бюджета у impact/diagnostics/checks дали бы
    # тройной срок и три разных «сколько посмотрено» в одном ответе.
    own = budget or Budget(label="review")
    changed = changed_files(root, base)
    impact_map = impact(root, changed, base, budget=own)
    diagnostic = diagnostics(root, changed=changed, budget=own)
    churn: list[dict] = []
    git = _git_root(root)
    if git is not None:
        argv = ["git", "-C", str(git), "diff", "--numstat"]
        if base:
            argv.append(base)
        try:
            result = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=20)
            for line in result.stdout.splitlines():
                add, delete, path = (line.split("\t", 2) + ["", ""])[:3]
                churn.append({"path": path, "added": int(add) if add.isdigit() else None,
                              "deleted": int(delete) if delete.isdigit() else None})
        except Exception:
            pass
    test_changes = [row for row in changed if Path(row).name.startswith("test_") or "/test" in row]
    total_churn = sum((row["added"] or 0) + (row["deleted"] or 0) for row in churn)
    risks: list[str] = []
    if changed and not test_changes and any(Path(row).suffix.lower() in LANG_BY_EXT for row in changed):
        risks.append("code changed without a changed test file; run impacted/full tests and justify coverage")
    if total_churn > 800:
        risks.append(f"large diff ({total_churn} changed lines) needs staged review")
    critical = [row for row in changed if Path(row).name in {
        "Dockerfile", "docker-compose.yml", "docker-compose.deploy.yml", "pyproject.toml",
        "requirements.txt", "package.json", "Cargo.toml", "go.mod",
    }]
    if critical:
        risks.append("runtime/build contracts changed: " + ", ".join(critical))
    if diagnostic.get("ok") is False:
        risks.append("syntax/config diagnostics are not clean")
    elif diagnostic.get("ok") is None:
        # Раньше сюда попадало и «не проверено»: `ok` было `true` при checked=0, риск не
        # добавлялся вовсе, и молчание диагностики выглядело как её чистый результат.
        risks.append("диагностика синтаксиса не отработала — " + str(diagnostic.get("ok_means")))
    plan = verification_plan(root, base, full=False, budget=own)
    ready = (bool(changed) and not diagnostic.get("diagnostics")
             and diagnostic.get("ok") is True)
    ready_means = ("есть изменения, синтаксис проверен целиком и чист" if ready else
                   "изменений нет" if not changed else
                   "диагностика нашла проблемы" if diagnostic.get("diagnostics") else
                   "диагностика не довела обход до конца")
    if not own.complete():
        # Риск обязан быть назван ЗДЕСЬ же: «готово к ревью» по 11% дерева — это ложь ей.
        risks.append("частичный обзор: " + own.note())
        # ⚠ Одного риска в списке мало. 27.07 при упоре в срок impact выедал весь общий
        # кошелёк, diagnostics не смотрел ни одного файла — и снимок всё равно отдавал
        # `ready_for_human_review: true`, то есть «ничто не задето, тестов не нужно, готово».
        # Признание длиной в 500 символов пряталось в risks[], а решало булево. Теперь при
        # неполном обходе булево молчит: null — «не знаю», а не «не готово» и не «готово».
        ready = None
        ready_means = ("НЕИЗВЕСТНО: обзор неполный (упёрлась в "
                       + (own.stopped_by() or "предел")
                       + "), судить о готовности не по чему — это не «не готово», а «не знаю»")
    return {
        "root": str(root), "base": base, "changed": changed,
        "churn": churn, "total_churn": total_churn,
        "diagnostics": diagnostic, "impact": impact_map,
        "tests_changed": test_changes,
        "verification": plan,
        "risks": risks,
        "ready_for_human_review": ready,
        "ready_for_human_review_means": ready_means,
        "limits": own.report(),
    }


def _python_symbols(path: Path, root: Path) -> tuple[list[dict], list[dict]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        return [], [{"path": str(path.relative_to(root)), "line": exc.lineno or 0,
                     "column": exc.offset or 0, "kind": "syntax", "message": exc.msg}]
    except OSError as exc:
        return [], [{"path": str(path), "line": 0, "kind": "io", "message": str(exc)}]
    rows: list[dict] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack: list[str] = []

        def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            args = [a.arg for a in (*node.args.posonlyargs, *node.args.args)]
            if node.args.vararg:
                args.append("*" + node.args.vararg.arg)
            args += [a.arg for a in node.args.kwonlyargs]
            if node.args.kwarg:
                args.append("**" + node.args.kwarg.arg)
            name = ".".join([*self.stack, node.name])
            rows.append({"path": str(path.relative_to(root)).replace(os.sep, "/"),
                         "line": node.lineno, "end": getattr(node, "end_lineno", node.lineno),
                         "kind": "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function",
                         "name": name, "signature": f"({', '.join(args)})"})
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            self._function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
            self._function(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
            name = ".".join([*self.stack, node.name])
            rows.append({"path": str(path.relative_to(root)).replace(os.sep, "/"),
                         "line": node.lineno, "end": getattr(node, "end_lineno", node.lineno),
                         "kind": "class", "name": name, "signature": ""})
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

    Visitor().visit(tree)
    return rows, []


def symbols(root: Path, path: str = "", query: str = "", limit: int = 500,
            *, budget: Budget | None = None) -> dict:
    root = root.resolve()
    own = budget or Budget(label="symbols")
    base = (root / path).resolve() if path else root
    files = ([base] if base.is_file()
             else source_files(base, limit=10000, suffixes=set(LANG_BY_EXT), budget=own,
                               stage="файлы (символы)"))
    rows: list[dict] = []
    errors: list[dict] = []
    needle = query.casefold().strip()
    read = 0
    stopped = ""
    for file in files:
        if len(rows) >= limit:
            stopped = stopped or "symbol-cap"
            break
        if own.expired():
            stopped = "deadline"
            break
        read += 1
        language = LANG_BY_EXT.get(file.suffix.lower(), "")
        if language == "python":
            found, errs = _python_symbols(file, root)
            rows.extend(found)
            errors.extend(errs)
            continue
        rx = _GENERIC_SYMBOLS.get(language)
        if rx is None:
            continue
        try:
            for no, line in enumerate(file.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                match = rx.match(line)
                if match:
                    rows.append({"path": str(file.relative_to(root)).replace(os.sep, "/"),
                                 "line": no, "end": no, "kind": match.group(1),
                                 "name": match.group(2), "signature": line.strip()[:240]})
        except OSError:
            continue
    own.stage("разбор символов", taken=read, seen=len(files), exhausted=not stopped, stopped=stopped)
    if needle:
        rows = [row for row in rows if needle in str(row.get("name", "")).casefold()]
    rows.sort(key=lambda row: (row["path"], int(row["line"])))
    return {"symbols": rows[:limit], "diagnostics": errors[:100],
            "truncated": len(rows) > limit or not own.complete(), "limits": own.report()}


def references(root: Path, symbol: str, path: str = "", limit: int = 300,
               *, budget: Budget | None = None) -> dict:
    root = root.resolve()
    own = budget or Budget(label="references")
    base = (root / path).resolve() if path else root
    if not symbol or not re.match(r"^[A-Za-z_$][\w$.:/-]*$", symbol):
        return {"error": "symbol must be an identifier-like name", "references": []}
    short = symbol.rsplit(".", 1)[-1]
    rx = re.compile(rf"(?<![\w$]){re.escape(short)}(?![\w$])")
    rows: list[dict] = []
    files = ([base] if base.is_file()
             else source_files(base, limit=20000, budget=own, stage="файлы (ссылки)"))
    read = 0
    stopped = ""
    for file in files:
        if len(rows) >= limit:
            stopped = stopped or "reference-cap"
            break
        if own.expired():
            stopped = "deadline"
            break
        read += 1
        try:
            raw = file.read_bytes()
            if b"\0" in raw[:4096] or len(raw) > 4 * 1024 * 1024:
                continue
            for no, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
                if rx.search(line):
                    rows.append({"path": str(file.relative_to(root)).replace(os.sep, "/"),
                                 "line": no, "text": line.strip()[:260]})
                    if len(rows) >= limit:
                        break
        except (OSError, ValueError):
            continue
    own.stage("просмотр файлов", taken=read, seen=len(files), exhausted=not stopped, stopped=stopped)
    return {"symbol": symbol, "references": rows,
            "truncated": len(rows) >= limit or not own.complete(), "limits": own.report()}


def diagnostics(root: Path, path: str = "", changed: Iterable[str] | None = None,
                limit: int = 300, *, budget: Budget | None = None) -> dict:
    root = root.resolve()
    own = budget or Budget(label="diagnostics")
    mark = own.mark()
    if path:
        base = (root / path).resolve()
        files = ([base] if base.is_file()
                 else source_files(base, limit=10000, budget=own, stage="файлы (диагностика)"))
    elif changed:
        files = [(root / rel).resolve() for rel in changed if (root / rel).is_file()]
    else:
        files = source_files(root, limit=10000, suffixes={".py", ".pyi", ".json", ".toml"},
                             budget=own, stage="файлы (диагностика)")
    rows: list[dict] = []
    checked = 0
    stopped = ""
    for file in files:
        if len(rows) >= limit:
            stopped = stopped or "diagnostic-cap"
            break
        if own.expired():
            stopped = "deadline"
            break
        suffix = file.suffix.lower()
        if suffix not in {".py", ".pyi", ".json", ".toml"}:
            continue
        checked += 1
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
            if suffix in {".py", ".pyi"}:
                ast.parse(text, filename=str(file))
            elif suffix == ".json":
                json.loads(text)
            elif suffix == ".toml":
                tomllib.loads(text)
        except (SyntaxError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
            rows.append({"path": str(file.relative_to(root)).replace(os.sep, "/"),
                         "line": int(getattr(exc, "lineno", 0) or 0),
                         "column": int(getattr(exc, "colno", getattr(exc, "offset", 0)) or 0),
                         "kind": "syntax", "message": str(exc)})
        except OSError as exc:
            rows.append({"path": str(file), "line": 0, "kind": "io", "message": str(exc)})
    own.stage("проверка синтаксиса", taken=checked, seen=len(files),
              exhausted=not stopped, stopped=stopped)
    # `ok` без признака усечения читается как «чисто во всём дереве». Пусть говорит правду.
    # ⚠ Мало приписать рядом `complete: false`: решают именно булевы поля, их читает и она, и
    # код. 27.07 в review общий кошелёк съедал impact, diagnostics не открывал НИ ОДНОГО файла
    # (`checked: 0`) — и `ok: true` заявляло «синтаксис чист». Поэтому третье состояние: `null`
    # («не знаю»), а не `true`. Именно null, не строка «unknown»: непустая строка истинна в
    # любом наивном `if`, то есть «не знаю» читалось бы как «да».
    scanned_all = own.complete_since(mark)
    verdict = False if rows else (True if scanned_all else None)
    if verdict is False:
        means = f"не чисто: {len(rows)} находок в {checked} проверенных файлах"
    elif verdict:
        means = f"чисто: проверено {checked} файлов, обход диагностики полный"
    else:
        means = (f"НЕИЗВЕСТНО: проверено {checked} файлов из {len(files)}, обход диагностики "
                 f"оборван ({own.stopped_by() or 'предел'}) — сказать «чисто» не о чем")
    return {"checked": checked, "diagnostics": rows, "ok": verdict, "ok_means": means,
            "complete": own.complete(), "scan_complete": scanned_all,
            "truncated": len(rows) >= limit or not own.complete(), "limits": own.report()}


def _python_module(path: Path, root: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def impact(root: Path, changed: Iterable[str] | None = None, base: str = "",
           *, budget: Budget | None = None) -> dict:
    root = root.resolve()
    own = budget or Budget(label="impact")
    changed_rows = list(dict.fromkeys(str(x).replace("\\", "/") for x in (changed or changed_files(root, base))))
    py_files = source_files(root, limit=12000, suffixes={".py", ".pyi"}, budget=own,
                            stage="файлы (impact)")
    modules = {_python_module(path, root): path for path in py_files}
    index = _ModuleIndex(modules)
    reverse: dict[str, set[str]] = {}
    parse_errors: list[str] = []
    parsed = 0
    stopped = ""
    for module, file in modules.items():
        if own.expired():
            stopped = "deadline"
            break
        parsed += 1
        try:
            tree = ast.parse(file.read_text(encoding="utf-8", errors="replace"), filename=str(file))
        except (OSError, SyntaxError):
            parse_errors.append(str(file.relative_to(root)))
            continue
        deps: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                deps.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                deps.add(node.module)
        for dep in deps:
            for candidate in index.resolve(dep):
                reverse.setdefault(candidate, set()).add(module)
    own.stage("разбор импортов", taken=parsed, seen=len(modules),
              exhausted=not stopped, stopped=stopped)

    impacted_modules: set[str] = set()
    for rel in changed_rows:
        path = (root / rel).resolve()
        if path.suffix.lower() in {".py", ".pyi"} and path.exists():
            try:
                impacted_modules.add(_python_module(path, root))
            except ValueError:
                pass
    queue = list(impacted_modules)
    while queue and len(impacted_modules) < 2000:
        current = queue.pop(0)
        for consumer in reverse.get(current, set()):
            if consumer not in impacted_modules:
                impacted_modules.add(consumer)
                queue.append(consumer)

    impacted_files = []
    for module in sorted(impacted_modules):
        file = modules.get(module)
        if file:
            impacted_files.append(str(file.relative_to(root)).replace(os.sep, "/"))
    tests: list[str] = []
    for file in py_files:
        rel = str(file.relative_to(root)).replace(os.sep, "/")
        if file.name.startswith("test_") or "/test" in rel:
            module = _python_module(file, root)
            if module in impacted_modules or any(
                file.stem == f"test_{Path(changed).stem}" for changed in changed_rows
            ):
                tests.append(rel)
    if not tests:
        stems = {Path(row).stem for row in changed_rows}
        scanned = 0
        grep_stopped = ""
        for file in py_files:
            if own.expired():
                grep_stopped = "deadline"
                break
            rel = str(file.relative_to(root)).replace(os.sep, "/")
            if not file.name.startswith("test_"):
                continue
            scanned += 1
            try:
                text = file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if any(re.search(rf"\b{re.escape(stem)}\b", text) for stem in stems if stem):
                tests.append(rel)
        own.stage("поиск тестов по именам", taken=scanned, seen=len(py_files),
                  exhausted=not grep_stopped, stopped=grep_stopped)
    # 26.07 impact вернул карту по 11% дерева и молчал об этом — «impacted: []» читалось как
    # «ничто не задето». Признак усечения теперь едет в том же ответе, что и сама карта.
    return {
        "changed": changed_rows, "impacted": impacted_files[:500],
        "tests": list(dict.fromkeys(tests))[:100], "parse_errors": parse_errors[:50],
        "complete": own.complete(), "truncated": not own.complete(), "limits": own.report(),
    }


def _make_targets(path: Path) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    return {m.group(1) for m in re.finditer(r"(?m)^([A-Za-z0-9_.-]+)\s*:(?![=])", text)}


def python_command() -> str:
    """Return the interpreter name that is true in the environment doing the inspection."""
    if shutil.which("python"):
        return "python"
    if shutil.which("python3"):
        return "python3"
    return "python3"


def discovered_checks(root: Path) -> list[dict]:
    root = root.resolve()
    checks: list[dict] = []
    python = python_command()

    def add(cid: str, command: str, kind: str, source: str, scope: str = "full") -> None:
        if command and all(row["command"] != command for row in checks):
            checks.append({"id": cid, "command": command, "cwd": ".", "kind": kind,
                           "source": source, "scope": scope})

    tests = sorted(path for path in root.glob("test_*.py") if path.is_file())
    pyproject = root / "pyproject.toml"
    pytest_config = (root / "pytest.ini").is_file() or (root / "conftest.py").is_file()
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            pytest_config = pytest_config or "pytest" in (data.get("tool") or {})
        except (OSError, tomllib.TOMLDecodeError):
            pass
    if tests:
        add("python-unittest", f"{python} -m unittest discover -q", "test", "test_*.py")
    if pytest_config:
        add("python-pytest", f"{python} -m pytest -q", "test", "pytest config")
    if any(root.glob("*.py")) or pyproject.is_file():
        add("python-compile", f"{python} -m compileall -q .", "diagnostic", "Python sources")

    package = root / "package.json"
    if package.is_file():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts") or {}
        except (OSError, ValueError):
            scripts = {}
        for name in ("test", "lint", "typecheck", "build"):
            if name in scripts:
                add(f"npm-{name}", f"npm run {name}", name, f"package.json scripts.{name}")
    if (root / "Cargo.toml").is_file():
        add("cargo-test", "cargo test", "test", "Cargo.toml")
        add("cargo-clippy", "cargo clippy --all-targets -- -D warnings", "lint", "Cargo.toml")
    if (root / "go.mod").is_file():
        add("go-test", "go test ./...", "test", "go.mod")
        add("go-vet", "go vet ./...", "lint", "go.mod")
    makefile = root / "Makefile"
    if makefile.is_file():
        targets = _make_targets(makefile)
        for target in ("test", "check", "lint", "build"):
            if target in targets:
                add(f"make-{target}", f"make {target}", target, f"Makefile:{target}")
    return checks


def verification_plan(root: Path, base: str = "", *, full: bool = False,
                      budget: Budget | None = None) -> dict:
    root = root.resolve()
    own = budget or Budget(label="checks")
    python = python_command()
    changed = changed_files(root, base)
    affected = impact(root, changed, base, budget=own)
    checks: list[dict] = []
    py_changed = [row for row in changed if Path(row).suffix.lower() in {".py", ".pyi"}]
    if py_changed:
        quoted = " ".join(shlex.quote(row) for row in py_changed[:100])
        checks.append({"id": "changed-python-syntax", "command": f"{python} -m py_compile {quoted}",
                       "cwd": ".", "kind": "diagnostic", "source": "changed Python files",
                       "scope": "targeted"})
    test_paths = affected.get("tests") or []
    modules = [str(Path(path).with_suffix("")).replace("/", ".").replace("\\", ".") for path in test_paths]
    if modules:
        checks.append({"id": "impacted-python-tests",
                       "command": f"{python} -m unittest -q " + " ".join(shlex.quote(m) for m in modules[:30]),
                       "cwd": ".", "kind": "test", "source": "impact map",
                       "scope": "targeted"})
    discovered = discovered_checks(root)
    if full or not checks:
        checks.extend(discovered)
    else:
        # Keep one authoritative project-wide gate after targeted checks.
        full_test = next((row for row in discovered if row["kind"] == "test"), None)
        if full_test:
            checks.append(full_test)
    unique: list[dict] = []
    for row in checks:
        if row["command"] not in {item["command"] for item in unique}:
            unique.append(row)
    return {
        "root": str(root), "base": base, "changed": changed, "impact": affected,
        "checks": unique[:16], "full": bool(full), "model": project_model(root, budget=own),
        "limits": own.report(),
    }
