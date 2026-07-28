"""
hands — мост к компилируемому полу её рук (`hands/`, бинарь praxis-hands).

Зачем: рельсы (разрешение путей, зоны записи, тайм-ауты, потолки вывода,
гард от случайного усечения) живут ВНЕ самоизменяемого питона. Она правит мастерскую
(пальцы) сколько хочет — пол под ней не двигается: он скомпилирован и лежит рядом.

Как это устроено:
  * бинарь ищется по PRAXIS_HANDS, затем bin/praxis-hands, затем hands/target/release/…;
  * НЕТ бинаря — питон-мастерская работает как раньше (одна семантика, две реализации);
  * каждый вызов оставляет расписку в memory/.state/hands.jsonl (что, где, чем кончилось).

Контракт: бинарь всегда печатает одну JSON-строку. Ошибка операции — это ok:false,
а не исключение. Ни один сбой моста не должен ронять её ход: не вышло — вернём None,
и мастерская пойдёт питоновым путём.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("praxis-hands")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
REPO = Path(__file__).resolve().parent

_CALL_TIMEOUT_PAD = 30      # сверх тайм-аута самой операции: бинарь обязан ответить раньше


def _base(base: str | Path | None = None) -> Path:
    """Дом, относительно которого судят рельсы. Вызывающий (мастерская) знает его точно —
    её BASE подменяется в тестах и в worktree предложений; глобал здесь только дефолт."""
    return Path(base) if base else BASE


def receipts_path(base: str | Path | None = None) -> Path:
    return _base(base) / "memory" / ".state" / "hands.jsonl"


def binary() -> Path | None:
    """Путь к бинарю или None. PRAXIS_HANDS: `off` — выключатель; путь — ТОЛЬКО он
    (указали и промахнулись — честный питоновый путь, а не тихая подмена другим
    бинарём). Пусто — ищем: bin/ рядом с домом, затем сборка разработчика."""
    env = (os.getenv("PRAXIS_HANDS") or "").strip()
    if env.lower() in ("off", "0", "false", "no"):
        return None
    if env:
        cands = [Path(env)]
    else:
        cands = []
        for root in (BASE, REPO):
            cands += [root / "bin" / "praxis-hands", root / "bin" / "praxis-hands.exe",
                      root / "hands" / "target" / "release" / "praxis-hands",
                      root / "hands" / "target" / "release" / "praxis-hands.exe"]
    for c in cands:
        try:
            if c.is_file() and os.access(c, os.X_OK):
                return c
        except OSError:
            continue
    return None


def available() -> bool:
    return binary() is not None


def call(subcmd: str, wait: int = 150, base: str | Path | None = None, **flags) -> dict | None:
    """Позвать бинарь. -> разобранный JSON, или None если бинаря нет / он не ответил.

    None значит ровно одно: «иди питоновым путём». Ни ошибок операции, ни отказов
    рельсов здесь нет — они приезжают внутри dict как ok:false.

    Имена служебных параметров нарочно НЕ пересекаются с флагами бинаря: `subcmd`
    (а не `op` — у guard свой --op) и `wait` (а не `timeout` — у exec свой --timeout;
    когда-то здесь `timeout` съедался сигнатурой и рельс тайм-аута молча не применялся,
    бинарь ждал дефолтные 120с. Тест test_run_timeout_is_reported держит это место)."""
    exe = binary()
    if exe is None:
        return None
    root = _base(base)
    argv = [str(exe), subcmd, "--base", str(root), "--receipt", str(receipts_path(root))]
    for k, v in flags.items():
        key = "--" + k.replace("_", "-")
        if v is True:
            argv.append(key)
        elif v is False or v is None:
            continue
        else:
            argv += [key, str(v)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=wait)
    except (OSError, subprocess.TimeoutExpired):
        log.warning("hands: %s не ответил — питоновый путь", subcmd, exc_info=True)
        return None
    if proc.returncode != 0:
        log.warning("hands: %s вернул %s: %s", subcmd, proc.returncode, (proc.stderr or "")[:200])
        return None
    try:
        return json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        log.warning("hands: %s отдал не-JSON: %s", subcmd, (proc.stdout or "")[:200])
        return None


def _tmp(text: str) -> str:
    """Содержимое уезжает файлом, не аргументом: длина и кавычки — не наша забота."""
    fd, path = tempfile.mkstemp(prefix="praxis_hands_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def edit(path: str, old: str, new: str, root: str = "", base=None) -> dict | None:
    """Точная правка одного вхождения. dict{ok,msg[,lines]} или None (нет бинаря)."""
    of, nf = _tmp(old), _tmp(new)
    try:
        return call("edit", base=base, file=path, old_file=of, new_file=nf, root=root or None)
    finally:
        for p in (of, nf):
            try:
                os.unlink(p)
            except OSError:
                pass


def write(path: str, content: str, root: str = "", overwrite: bool = False,
          force: bool = False, base=None) -> dict | None:
    cf = _tmp(content)
    try:
        return call("write", base=base, file=path, content_file=cf, root=root or None,
                    overwrite=overwrite or None, force=force or None)
    finally:
        try:
            os.unlink(cf)
        except OSError:
            pass


def outline(path: str, base=None) -> dict | None:
    return call("outline", base=base, file=path)


def search(literal: str, glob: str = "**/*.py", root: str = "", cap: int = 60,
           ignore_case: bool = False, base=None) -> dict | None:
    return call("search", base=base, literal=literal, glob=glob, root=root or ".", cap=cap,
                ignore_case=ignore_case or None)


def execute(shell: str, cwd: str, timeout: int = 120, cap: int = 8000, base=None) -> dict | None:
    """Запуск под рельсами бинаря. `timeout` уезжает ФЛАГОМ (его исполняет бинарь),
    а мы ждём его ответа чуть дольше — иначе убьём того, кто убивает."""
    return call("exec", base=base, wait=timeout + _CALL_TIMEOUT_PAD,
                shell=shell, cwd=cwd, cap=cap, timeout=timeout)


def guard(path: str, op: str = "write", root: str = "", base=None) -> dict | None:
    """Вердикт рельсов без действия — тесты сверяют его с питоновым."""
    return call("guard", base=base, path=path, op=op, root=root or None)


# --------------------------------------------------------------------------- #
#  Рукопожатие рельсов: бинарь, собранный под СТАРЫЕ таблицы, не должен молчать.
#  Цепочка доверия: питон-константы ≡ rails.rs (test_rails_rs_is_fresh) и
#  rails.rs ≡ бинарь (отпечаток ниже) ⇒ константы ≡ бинарь.
# --------------------------------------------------------------------------- #

_FP_RE = re.compile(r'FINGERPRINT: &str = "([0-9a-f]+)"')


def rails_fp_expected() -> str:
    """Отпечаток рельсов, под который ОБЯЗАН быть собран бинарь — из hands/src/rails.rs
    (свежесть самого rails.rs держит тест; импортов workshop/selfdev здесь нет намеренно)."""
    try:
        text = (REPO / "hands" / "src" / "rails.rs").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = _FP_RE.search(text)
    return m.group(1) if m else ""


def rails_status() -> str:
    """fresh | stale | unknown. stale = бинарь жив, но рельсы в нём отстали от кода —
    решения принимает он, поэтому это не косметика, а первым делом «пересобери»."""
    if binary() is None:
        return "unknown"
    got = (call("version") or {}).get("rails_fp") or ""
    want = rails_fp_expected()
    if not got or not want:
        return "unknown"
    return "fresh" if got == want else "stale"


def state_line() -> str:
    """Строка для my_capabilities/STATE: есть ли пол под руками и свежи ли его рельсы."""
    exe = binary()
    if exe is None:
        return "руки: питон (компилируемый пол не собран — рельсы из workshop.py)"
    line = f"руки: питон + компилируемый пол ({exe.name})"
    if rails_status() == "stale":
        line += " ⚠ рельсы бинаря отстали от кода — пересобери praxis-hands"
    return line
