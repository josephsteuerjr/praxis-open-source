"""
Praxis — git-рельсы для её само-изменений.

Любая правка её файлов (soul/skills, код) авто-коммитится плоским сообщением. Это
даёт обратимость и аудит — фундамент безопасности «полного» шелла: не «уровни
доверия» и песочницы, а git + лог + owner-gate.

Робастно: если репо/идентичности нет или git недоступен — тихо ничего не делаем.
"""

from __future__ import annotations

import os

import logging
import subprocess
import time
from pathlib import Path

log = logging.getLogger("praxis-git")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
TIMEOUT = 30
SNAPSHOT_REF_NS = "refs/praxis-snapshots"
SNAPSHOT_KEEP = 40


def _git(*args: str):
    return subprocess.run(["git", "-C", str(BASE), *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=TIMEOUT)


def repo_ready() -> bool:
    return (BASE / ".git").exists()


def changed_paths() -> list[str]:
    """Пути изменённых/новых файлов (git status --porcelain), без gitignored."""
    if not repo_ready():
        return []
    try:
        r = _git("status", "--porcelain")
    except Exception:
        return []
    out = []
    for line in r.stdout.splitlines():
        p = line[3:].strip().strip('"')
        if p:
            out.append(p)
    return out


def recent(n: int = 3) -> list[str]:
    """PASS 5: последние коммиты одной строкой (для STATE) — «sha дата: сообщение»."""
    if not repo_ready():
        return []
    try:
        r = _git("log", f"-{max(1, int(n))}", "--pretty=format:%h %ad: %s",
                 "--date=format:%d.%m %H:%M")
        return [l.strip() for l in r.stdout.splitlines() if l.strip()]
    except Exception:
        return []


def _prune_snapshot_refs(keep: int = SNAPSHOT_KEEP) -> None:
    try:
        r = _git("for-each-ref", "--format=%(refname)", "--sort=refname", SNAPSHOT_REF_NS + "/")
        refs = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        for ref in (refs[:-keep] if keep > 0 else refs):
            _git("update-ref", "-d", ref)
    except Exception:
        log.debug("prune снапшот-рефов не удался", exc_info=True)


def list_safety_points(n: int = 10) -> list[str]:
    """Последние страховочные снапшоты: «ref sha» (для глаз/восстановления)."""
    if not repo_ready():
        return []
    try:
        r = _git("for-each-ref", "--format=%(refname:short) %(objectname:short)",
                 "--sort=-refname", SNAPSHOT_REF_NS + "/")
        return [l.strip() for l in r.stdout.splitlines() if l.strip()][:max(1, int(n))]
    except Exception:
        return []


def safety_point(message: str) -> str | None:
    """PASS 30 Этап 1: страховочный снапшот ВНЕ ветки — master не получает коммит.

    Состояние рабочего дерева (включая untracked, минус gitignored) сохраняется
    commit-объектом под refs/praxis-snapshots/<ts>-<sha> через ВРЕМЕННЫЙ индекс:
    настоящий индекс и рабочее дерево не меняются (её незакоммиченная работа и её
    staged-выбор остаются ровно как были — она коммитит в прод сама, осмысленно).
    Восстановление: `git checkout <sha> -- <путь>`; список — list_safety_points().
    Хранится последних SNAPSHOT_KEEP, старые рефы прунятся. -> короткий sha | None."""
    if not repo_ready():
        return None
    git_dir = _git("rev-parse", "--absolute-git-dir").stdout.strip()
    if not git_dir:
        return None  # и linked worktree (.git-файл) резолвится честно, не падает
    tmp_index = Path(git_dir) / f"praxis-snapshot-index-{os.getpid()}"
    try:
        head = _git("rev-parse", "HEAD").stdout.strip()
        if not head:
            return None  # репо без коммитов — не к чему цеплять родителя
        env = dict(os.environ, GIT_INDEX_FILE=str(tmp_index))

        def _giti(*args: str):
            return subprocess.run(["git", "-C", str(BASE), *args],
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=TIMEOUT, env=env)

        # Коды возврата проверяются: усечённый снапшот, молча притворяющийся полной
        # страховкой, хуже честного отсутствия снапшота.
        if _giti("read-tree", "HEAD").returncode != 0:
            log.warning("safety_point: read-tree не удался")
            return None
        add = _giti("add", "-A")
        if add.returncode != 0:
            log.warning("safety_point: add -A не удался: %s", (add.stderr or "")[:200])
            return None
        tree = _giti("write-tree").stdout.strip()
        head_tree = _git("rev-parse", "HEAD^{tree}").stdout.strip()
        if not tree or tree == head_tree:
            return None  # дерево чистое — страховать нечего
        c = subprocess.run(
            ["git", "-C", str(BASE),
             "-c", "user.name=Praxis", "-c", "user.email=praxis@local",
             "commit-tree", tree, "-p", head, "-m", message],
            capture_output=True, text=True, timeout=TIMEOUT, env=env)
        sha = (c.stdout or "").strip()
        if c.returncode != 0 or not sha:
            log.warning("safety_point commit-tree не удался: %s", (c.stderr or "")[:200])
            return None
        ref = f"{SNAPSHOT_REF_NS}/{time.strftime('%Y%m%dT%H%M%S')}-{sha[:8]}"
        _git("update-ref", ref, sha)
        _prune_snapshot_refs()
        log.info("safety-point %s: %s", sha[:8], message)
        return sha[:8]
    except Exception:
        log.warning("safety_point упал", exc_info=True)
        return None
    finally:
        try:
            tmp_index.unlink(missing_ok=True)
        except OSError:
            pass


def snapshot_paths(paths: list[str], message: str) -> str | None:
    """Закоммитить ТОЛЬКО названные пути (pathspec-коммит): чужая пред-существующая
    грязь и её staged-выбор по другим путям остаются нетронутыми. -> sha | None."""
    paths = [p for p in (paths or []) if p]
    if not repo_ready() or not paths:
        return None
    try:
        _git("add", "--", *paths[:200])  # untracked становятся известными
        c = subprocess.run(
            ["git", "-C", str(BASE),
             "-c", "user.name=Praxis", "-c", "user.email=praxis@local",
             "commit", "-q", "-m", message, "--", *paths[:200]],
            capture_output=True, text=True, timeout=TIMEOUT,
        )
        if c.returncode != 0:
            log.warning("pathspec-коммит не удался: %s", (c.stderr or "")[:200])
            return None
        sha = _git("rev-parse", "--short", "HEAD").stdout.strip()
        log.info("self-commit %s: %s", sha, message)
        return sha
    except Exception:
        log.warning("snapshot_paths упал", exc_info=True)
        return None


def snapshot(message: str) -> str | None:
    """Закоммитить текущие изменения плоским сообщением. -> sha или None, если коммитить нечего."""
    if not repo_ready():
        return None
    try:
        _git("add", "-A")
        if _git("diff", "--cached", "--quiet").returncode == 0:
            return None  # нечего коммитить
        c = subprocess.run(
            ["git", "-C", str(BASE),
             "-c", "user.name=Praxis", "-c", "user.email=praxis@local",
             "commit", "-q", "-m", message],
            capture_output=True, text=True, timeout=TIMEOUT,
        )
        if c.returncode != 0:
            log.warning("self-commit не удался: %s", (c.stderr or "")[:200])
            return None
        sha = _git("rev-parse", "--short", "HEAD").stdout.strip()
        log.info("self-commit %s: %s", sha, message)
        return sha
    except Exception:
        log.warning("snapshot упал", exc_info=True)
        return None
