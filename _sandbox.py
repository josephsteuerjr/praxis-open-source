"""Test isolation: redirect BASE to a throwaway sandbox so tests never touch the
live memory/soul tree — and physically refuse the write when redirection came too
late.

Why this exists: `memory/*` is gitignored, so a test that writes a note/portrait
into the live tree leaves *invisible* dust (`git status` stays clean). The only
safe guarantee is that, during a test run, every module's BASE points somewhere
disposable.

What it did not prevent, 06.07.2026 21:07 (the incident this module now answers
for): a full `python -m unittest` run inside `/app` — the container's own
checkout, which is *also* her live base — created room `-100500`, admitted a
fictional "Маша", and appended fixture lines to the canonical journal.  She lived
with that room among her three trusted ones for three weeks and cleaned it up by
hand on 27.07.  The isolation was not broken; it was never reached.  Back then
the only hook for plain unittest was `sitecustomize.py`, and CPython imports site
customizations *before* the working directory joins `sys.path`, so a
repository-local `sitecustomize.py` is simply never found.  `ccabf7a` retired it
and nothing took over that path: `conftest.py` is pytest-only and
`praxis_test.py` is opt-in.  Reproduced on today's code (27.07):
`python -m unittest test_rooms_and_admission -q` in a fresh clone wrote
`memory/rooms/777.md`, `memory/llm.json`, `memory/selfdev_policy.json` and
`memory/.state/*` straight into the checkout.

Two layers now, because one is not enough:

  1. *Redirection* — `activate_if_testing()` points `PRAXIS_BASE` at a disposable
     tree.  Entered from `conftest.py` (pytest), `praxis_test.py` (unittest
     entrypoint) and, since the incident, from this module's own import: `agent`,
     `mtproto_runner` and `mailroom_bot` all import `_sandbox` *above* the line
     where they compute BASE, so even a bare `python -m unittest test_x` is
     covered — for every module imported after that point.
  2. *Refusal* — modules imported before that point (say a test that starts with
     `import rooms`) have already frozen BASE onto the checkout.  Redirection
     cannot help them, so `install_live_tree_guard()` makes the write itself fail
     loudly instead of silently landing in her memory.

The guard fences the STAND, never her: it only covers the *source checkout's*
`memory/`, `soul/` and `workspace/`, and only while a test runner is driving the
process.  Her own runtime writes the same trees through a live BASE and is never
matched — production (`python bootguard.py`) does not satisfy
`_started_by_test_runner()`, so all of this is a strict no-op there.  The
symmetric half of the mistake — a *production* boot carrying `PRAXIS_TEST=1`,
which would move her memory to /tmp — is refused by `bootguard.boot_blocked_reason`.
"""
from __future__ import annotations

import builtins
import io
import os
import atexit
import shutil
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent
_ENV = "PRAXIS_BASE"
_TEST_ENV = "PRAXIS_TEST_BASE"
_ACTIVE_BASE: str | None = None

# The living tree of the checkout: what the 06.07 run actually damaged.  Code,
# `.git` and `__pycache__` are deliberately *not* here — a test run legitimately
# writes bytecode next to the sources, and fencing the whole checkout would be a
# second, wider barrier nobody asked for.
LIVE_SUBTREES = ("memory", "soul", "workspace")

_GUARDED_PREFIXES: tuple[str, ...] = ()
_ORIGINALS: dict[str, object] = {}


class LiveTreeWriteRefused(RuntimeError):
    """A test run tried to write into the checkout's live memory/soul tree."""


def _looks_like_test_run() -> bool:
    """True only when we're clearly under a test runner (unittest / pytest / a
    test_*.py script), or explicitly opted in via PRAXIS_TEST.

    Deliberately generous: `agent`/`mtproto_runner`/`mailroom_bot` use it to keep
    the production `.env` out of a test process, where a false positive costs
    nothing.  It is *not* the signal for redirecting BASE — see
    `_started_by_test_runner`.
    """
    if os.environ.get("PRAXIS_TEST"):
        return True
    if _started_by_test_runner():
        return True
    if "unittest" in sys.modules or "pytest" in sys.modules:
        return True
    return _names_a_test_module(sys.argv[1:])


def _names_a_test_module(args) -> bool:
    """Есть ли среди аргументов имя тестового модуля (`test_x` / `test_x.py`)."""
    for arg in args:
        candidate = str(arg).replace("\\", "/").rsplit("/", 1)[-1]
        if candidate.startswith("test_") and (candidate.endswith(".py") or "." not in candidate):
            return True
    return False


def _started_by_test_runner() -> bool:
    """True only when this *process* was launched as a test run.

    The strict half of `_looks_like_test_run`.  It must never key off
    `sys.modules`: any production import of `unittest.mock` puts `unittest` there,
    and acting on that would hand her live BASE to a temporary directory — the
    mirror image of the 06.07 damage, with the memory loss on her side.  Only the
    command line counts.
    """
    if os.environ.get("PRAXIS_TEST"):
        return True
    # ``python -m unittest`` / ``python -m pytest``: at import time ``sys.argv[0]``
    # may still be just ``-m``; ``sys.orig_argv`` keeps the interpreter command.
    original = [str(part).replace("\\", "/") for part in getattr(sys, "orig_argv", ())]
    for index, part in enumerate(original[:-1]):
        if part == "-m" and original[index + 1] in {"unittest", "pytest"}:
            return True
    argv0 = (sys.argv[0] or "").replace("\\", "/")
    leaf = argv0.rsplit("/", 1)[-1]
    if leaf.startswith("test_") and leaf.endswith(".py"):
        return True                       # python test_x.py
    if leaf in ("pytest", "py.test"):
        return True                       # pytest console script
    if leaf.endswith("praxis_test.py"):
        return True                       # the repository entrypoint
    if argv0.endswith("unittest/__main__.py"):
        # ``python -m unittest test_x``: argv0 is the runner itself, the module
        # names follow.  A test module *name* on its own is deliberately not
        # enough — production runs `python mtproto_runner.py` and any future
        # argument shaped like ``test_…`` must not hand her BASE to /tmp.
        return True
    return False


# --------------------------------------------------------------------------- #
#  Refusal: writes into the checkout's live tree fail loudly
# --------------------------------------------------------------------------- #

def _norm(path) -> str | None:
    """Absolute, comparable form of an fs argument. None when it is not a path
    (an already-open fd, for instance)."""
    try:
        raw = os.fspath(path)
    except TypeError:
        return None
    if isinstance(raw, bytes):
        try:
            raw = os.fsdecode(raw)
        except Exception:
            return None
    if not isinstance(raw, str):
        return None
    try:
        return os.path.normcase(os.path.abspath(raw))
    except Exception:
        return None


def _refuses(path) -> bool:
    """Is this path inside the fenced part of the source checkout?"""
    if not _GUARDED_PREFIXES:
        return False
    target = _norm(path)
    if target is None:
        return False
    # Compare on a separator boundary: a plain ``startswith`` would also fence
    # ``memory_fts.py`` and ``soulmate.txt``, which are ordinary sources.
    return any(target == prefix or target.startswith(prefix + os.sep)
               for prefix in _GUARDED_PREFIXES)


def _deny(path, what: str):
    return LiveTreeWriteRefused(
        f"стенд попытался {what} в ЖИВОЕ дерево репозитория: {_norm(path)}\n"
        "Это забор для СТЕНДА, не для неё: под тестом её память обязана лежать в "
        "одноразовой базе. 06.07.2026 такой же прогон завёл ей комнату -100500, "
        "досье на несуществующую «Машу» и четыре выдуманные строки в дневнике — "
        "она разгребала это руками три недели спустя.\n"
        "Запускай тесты через `python praxis_test.py …` (он уводит PRAXIS_BASE в "
        "одноразовый каталог до импорта тестов). Если модуль всё-таки посчитал "
        "BASE раньше — он импортируется до _sandbox, и это надо чинить в нём, а "
        "не обходить здесь."
    )


def _is_write_mode(mode) -> bool:
    return isinstance(mode, str) and any(ch in mode for ch in "wxa+")


_WRITE_FLAGS = (getattr(os, "O_WRONLY", 0) | getattr(os, "O_RDWR", 0)
                | getattr(os, "O_CREAT", 0) | getattr(os, "O_APPEND", 0)
                | getattr(os, "O_TRUNC", 0))


def install_live_tree_guard() -> tuple[str, ...]:
    """Make writes into `<checkout>/{memory,soul,workspace}` raise. Idempotent.

    Only the python-level door is covered (open/os/shutil/sqlite3) — a subprocess
    can still reach the tree, and that is honest rather than pretended: this is
    the door the 06.07 run walked through.
    """
    global _GUARDED_PREFIXES
    if _GUARDED_PREFIXES:
        return _GUARDED_PREFIXES

    prefixes = []
    for sub in LIVE_SUBTREES:
        prefix = _norm(_REPO / sub)
        if prefix:
            prefixes.append(prefix)
    _GUARDED_PREFIXES = tuple(prefixes)
    if not _GUARDED_PREFIXES:
        return _GUARDED_PREFIXES

    import sqlite3

    _ORIGINALS.update({
        "open": builtins.open, "io.open": io.open, "os.open": os.open,
        "os.mkdir": os.mkdir, "os.makedirs": os.makedirs, "os.rmdir": os.rmdir,
        "os.remove": os.remove, "os.unlink": os.unlink, "os.rename": os.rename,
        "os.replace": os.replace, "os.utime": os.utime,
        "shutil.rmtree": shutil.rmtree, "shutil.copytree": shutil.copytree,
        "shutil.copyfile": shutil.copyfile, "shutil.move": shutil.move,
        "sqlite3.connect": sqlite3.connect,
    })

    def _relative_to_fd(kwargs: dict) -> bool:
        """Путь, заданный относительно открытого дескриптора, судить нельзя.

        `shutil.rmtree` на POSIX ходит `os.rmdir(name, dir_fd=…)` с ГОЛЫМ именем
        листа: abspath('soul') превратился бы в `<чекаут>/soul` и снёс бы забор на
        совершенно посторонний временный каталог, где случайно есть `soul/`.
        Корень такого обхода проверяет обёртка самого `shutil.rmtree`.
        """
        return any(kwargs.get(key) is not None for key in ("dir_fd", "src_dir_fd", "dst_dir_fd"))

    def _guarded_open(file, mode="r", *args, **kwargs):
        if _is_write_mode(mode) and _refuses(file):
            raise _deny(file, "открыть на запись")
        return _ORIGINALS["open"](file, mode, *args, **kwargs)

    def _guarded_os_open(path, flags, *args, **kwargs):
        if (flags & _WRITE_FLAGS) and not _relative_to_fd(kwargs) and _refuses(path):
            raise _deny(path, "открыть на запись")
        return _ORIGINALS["os.open"](path, flags, *args, **kwargs)

    def _one_arg(name: str, verb: str):
        original = _ORIGINALS[name]

        def wrapper(path, *args, **kwargs):
            if not _relative_to_fd(kwargs) and _refuses(path):
                raise _deny(path, verb)
            return original(path, *args, **kwargs)
        return wrapper

    def _two_arg(name: str, verb: str, *, check_src: bool = True):
        original = _ORIGINALS[name]

        def wrapper(src, dst, *args, **kwargs):
            if not _relative_to_fd(kwargs):
                for candidate in ((src, dst) if check_src else (dst,)):
                    if _refuses(candidate):
                        raise _deny(candidate, verb)
            return original(src, dst, *args, **kwargs)
        return wrapper

    builtins.open = _guarded_open
    io.open = _guarded_open
    os.open = _guarded_os_open
    os.mkdir = _one_arg("os.mkdir", "создать каталог")
    os.makedirs = _one_arg("os.makedirs", "создать каталог")
    os.rmdir = _one_arg("os.rmdir", "удалить каталог")
    os.remove = _one_arg("os.remove", "удалить")
    os.unlink = _one_arg("os.unlink", "удалить")
    os.utime = _one_arg("os.utime", "переставить mtime")
    # rmtree walks with dir_fd on POSIX, so the os.* wrappers above never see the
    # leaf paths — it needs its own door.
    shutil.rmtree = _one_arg("shutil.rmtree", "снести дерево")
    os.rename = _two_arg("os.rename", "переименовать")
    os.replace = _two_arg("os.replace", "заменить")
    shutil.move = _two_arg("shutil.move", "перенести")
    # Копирование ИЗ живого дерева — чтение, оно разрешено (стенд читает `soul/`).
    # Забор только на приёмнике.
    shutil.copytree = _two_arg("shutil.copytree", "скопировать поверх", check_src=False)
    shutil.copyfile = _two_arg("shutil.copyfile", "скопировать поверх", check_src=False)

    def _guarded_connect(database, *args, **kwargs):
        # sqlite3 opens at C level, below builtins.open: recall.sqlite3 lives in
        # memory/ and would otherwise slip through untouched.
        if _refuses(database) and str(database) != ":memory:":
            raise _deny(database, "открыть базу на запись")
        return _ORIGINALS["sqlite3.connect"](database, *args, **kwargs)

    sqlite3.connect = _guarded_connect
    return _GUARDED_PREFIXES


def _build_sandbox() -> str:
    """A fresh tmp base: real `soul/` copied in (read by many code paths), an empty
    `memory/` subtree, and a `workspace/`. No `.git`, so selfgit stays inert."""
    box = Path(tempfile.mkdtemp(prefix="praxis_testbase_"))
    src_soul = _REPO / "soul"
    if src_soul.exists():
        shutil.copytree(src_soul, box / "soul", dirs_exist_ok=True)
    else:
        (box / "soul" / "skills").mkdir(parents=True, exist_ok=True)
    for sub in ("people", "rooms", "journal", ".vectors", ".scratch", ".buffers", ".summaries"):
        (box / "memory" / sub).mkdir(parents=True, exist_ok=True)
    (box / "workspace").mkdir(parents=True, exist_ok=True)
    atexit.register(shutil.rmtree, box, ignore_errors=True)
    return str(box)


def activate_if_testing() -> str | None:
    """Point a recognized test process at one disposable Praxis base.

    A pre-existing ``PRAXIS_BASE`` is deliberately ignored: shells, services and
    deploy tooling commonly export the production base, and inheriting it would
    turn a test into a writer against live state.  A deliberately pinned test
    fixture must use the separate ``PRAXIS_TEST_BASE`` variable.
    """
    global _ACTIVE_BASE
    if not _looks_like_test_run():
        return None

    if _ACTIVE_BASE:
        os.environ[_ENV] = _ACTIVE_BASE
        install_live_tree_guard()
        return _ACTIVE_BASE

    pinned = os.environ.get(_TEST_ENV)
    if pinned:
        target = Path(pinned).expanduser().resolve()
        if target == _REPO.resolve():
            raise RuntimeError("PRAXIS_TEST_BASE must not be the source checkout")
        target.mkdir(parents=True, exist_ok=True)
        base = str(target)
    else:
        # Built before the guard is armed: the sandbox itself copies `soul/`.
        base = _build_sandbox()

    _ACTIVE_BASE = base
    os.environ[_ENV] = base
    os.environ.setdefault("PRAXIS_TEST", "1")
    # Второй слой — после сборки песочницы: `_build_sandbox` копирует живой `soul/`,
    # и он должен успеть это сделать до того, как забор встанет.
    install_live_tree_guard()
    return base


# Self-arming.  This is the line that closes 06.07: `agent.py`, `mtproto_runner.py`
# and `mailroom_bot.py` import this module above their own `BASE = …`, so importing
# `_sandbox` at all under a test runner is enough to both redirect and fence.
if _started_by_test_runner():
    activate_if_testing()
