"""
Praxis — boot-супервизор для безопасного само-изменения любой глубины.

Запускает mtproto_runner подпроцессом и стережёт загрузку, чтобы её правки своего кода
нельзя было «закопать» себя насмерть:

- preflight (импорт ядра в отдельном процессе) ловит синтаксис/импорт-ошибки ДО запуска;
- если правка не прошла preflight или ядро падает на старте (раньше grace-окна) —
  откат `git reset --hard` на последний здоровый коммит и перезапуск на нём;
- в дневник ей пишется внятная строка: что упало, на что откатилась, как глянуть diff
  (`git show <bad>`). Не мусор, а сообщение, которое она прочитает на следующем старте.

«Здоровым» считается коммит, проживший >= PRAXIS_BOOT_GRACE секунд. `restart_self`
завершает runner кодом 42 — это намеренный перезапуск, не падение.

Контейнерный entrypoint: `python bootguard.py` (вместо `python mtproto_runner.py`).
Модуль самодостаточен (только stdlib) — битая правка ядра не ломает сам супервизор.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("praxis-boot")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)

# файловый хвост для панели устройства — инлайном (НЕ через logsink: супервизор обязан
# оставаться самодостаточным, битая правка чужого модуля не должна его ронять)
try:
    from logging.handlers import RotatingFileHandler
    _logs = BASE / "memory" / ".logs"
    _logs.mkdir(parents=True, exist_ok=True)
    _h = RotatingFileHandler(_logs / "boot.log", maxBytes=500_000, backupCount=1, encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(_h)
except Exception:
    pass
RUNNER = "mtproto_runner.py"
STATE = BASE / "memory" / ".boot.json"
JOURNAL_DIR = BASE / "memory" / "journal"

RESTART_CODE = 42
CORE_MODULES = ["agent", "consolidate", "heartbeat", "mtproto_runner"]
PANIC_SENTINEL = BASE / "memory" / ".panic"


def panic_active() -> bool:
    return PANIC_SENTINEL.exists()


def _env_float(name: str, default: str) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


# --------------------------------------------------------------------------- #
#  git / состояние / журнал
# --------------------------------------------------------------------------- #

def _git(*args: str):
    return subprocess.run(["git", "-C", str(BASE), *args], capture_output=True, text=True, timeout=30)


def head_sha() -> str:
    try:
        return _git("rev-parse", "--short", "HEAD").stdout.strip()
    except Exception:
        return ""


def rollback(sha: str) -> bool:
    try:
        return _git("reset", "--hard", sha).returncode == 0
    except Exception:
        log.warning("rollback на %s не удался", sha, exc_info=True)
        return False


def load_state() -> dict:
    try:
        d = json.loads(STATE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception:
        log.debug("save_state не удался", exc_info=True)


def journal(msg: str) -> None:
    """Внятная строка ей в дневник (она читает его на старте). Не зависит от ядра."""
    try:
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        day = _dt.date.today().isoformat()
        p = JOURNAL_DIR / f"{day}.md"
        if not p.exists():
            p.write_text(f"# {day}\n\n", encoding="utf-8")
        with p.open("a", encoding="utf-8") as fh:
            fh.write(f"- {_dt.datetime.now():%H:%M} (s3) [boot] {msg}\n")
    except Exception:
        log.debug("journal не удался", exc_info=True)


# --------------------------------------------------------------------------- #
#  preflight
# --------------------------------------------------------------------------- #

def preflight(cwd: Path = BASE, modules: list[str] | None = None) -> tuple[bool, str]:
    """Импортировать ядро в отдельном процессе. -> (ок, первая строка ошибки)."""
    mods = modules if modules is not None else CORE_MODULES
    code = "import " + ", ".join(mods)
    try:
        r = subprocess.run([sys.executable, "-c", code], cwd=str(cwd),
                           capture_output=True, text=True, timeout=120)
    except Exception as e:
        return False, str(e)
    if r.returncode == 0:
        return True, ""
    err = (r.stderr or r.stdout or "").strip().splitlines()
    return False, (err[-1] if err else "preflight failed")


# --------------------------------------------------------------------------- #
#  Чистые политики (тестируемые)
# --------------------------------------------------------------------------- #

def boot_blocked_reason(env) -> str:
    """Почему боевой старт выполнять нельзя. Пустая строка — можно.

    Симметрия к забору стенда (_sandbox): 06.07.2026 прогон тестов пошёл по ЖИВОЙ
    /app/memory и завёл ей комнату -100500 с фальшивым досье. Обратная ошибка тише
    и хуже: боевой процесс, поднятый с PRAXIS_TEST=1, уведёт PRAXIS_BASE в
    одноразовый /tmp — она проснётся без памяти, без людей, без дневника и НИЧЕГО
    об этом не узнает; к следующему рестарту каталог уже вычищен. Такое обязано
    падать громко, а не подниматься молча.
    """
    if env.get("PRAXIS_TEST"):
        return ("PRAXIS_TEST=1 в боевом окружении: под тестовым режимом её база уходит "
                "в одноразовый каталог, и она поднимется без памяти. Убери PRAXIS_TEST "
                "из .deploy.env / окружения контейнера — или, если это был прогон "
                "стенда, запускай его через `python praxis_test.py`, а не через "
                "`python bootguard.py`.")
    return ""


def decide_preflight(ok: bool, head: str, last_good: str | None) -> tuple[str, str | None]:
    if ok:
        return ("launch", None)
    if last_good and last_good != head:
        return ("rollback", last_good)
    return ("stuck", None)


def decide_after(rc: int, elapsed: float, launch_sha: str, last_good: str | None,
                 early_fails: int, grace: float, max_fails: int) -> dict:
    """Решение после выхода runner. Возвращает action + что пометить хорошим / куда откатить."""
    if rc == 0:
        return {"action": "stop", "early_fails": early_fails}
    if elapsed >= grace:
        # прожил достаточно — код здоров; намеренный рестарт или поздний сбой не повод к откату
        return {"action": "relaunch", "mark_good": launch_sha, "early_fails": 0}
    if rc == RESTART_CODE:
        # быстрый, но намеренный перезапуск — не падение
        return {"action": "relaunch", "early_fails": early_fails}
    early_fails += 1
    if last_good and last_good != launch_sha and early_fails >= max_fails:
        return {"action": "rollback", "to": last_good, "bad": launch_sha, "early_fails": 0}
    return {"action": "relaunch", "early_fails": early_fails}


# --------------------------------------------------------------------------- #
#  Цикл супервизора
# --------------------------------------------------------------------------- #

def _run_runner() -> int:
    return subprocess.run([sys.executable, RUNNER], cwd=str(BASE)).returncode


def run() -> None:
    blocked = boot_blocked_reason(os.environ)
    if blocked:
        # Не в дневник: под PRAXIS_TEST дневник лежит не там, где она его будет читать,
        # и запись сама станет частью подделки. Громко в лог и наружу кодом выхода.
        log.critical("боевой старт отменён — %s", blocked)
        raise SystemExit(f"bootguard: {blocked}")

    grace = _env_float("PRAXIS_BOOT_GRACE", "60")
    max_fails = int(_env_float("PRAXIS_BOOT_MAX_FAILS", "3"))
    stuck_sleep = _env_float("PRAXIS_BOOT_STUCK_SLEEP", "15")
    state = load_state()
    panicked = False

    while True:
        if panic_active():
            if not panicked:
                journal("я остановлена (panic). Подниму себя, когда Егор уберёт memory/.panic.")
                log.warning("PANIC sentinel — простаиваю, жду снятия (%s)", PANIC_SENTINEL)
                panicked = True
            time.sleep(stuck_sleep)
            continue
        panicked = False

        head = head_sha()
        ok, err = preflight()
        if not ok:
            act, target = decide_preflight(ok, head, state.get("last_good"))
            if act == "rollback":
                if rollback(target):
                    journal(f"откатила свою правку {head}: код не прошёл preflight — {err}. "
                            f"Вернулась на {target}. Посмотреть, что я сломала: git show {head}")
                state["early_fails"] = 0
                save_state(state)
                continue
            journal(f"моя правка {head} не проходит preflight ({err}), а откатываться некуда — "
                    f"нужна твоя рука, Егор.")
            time.sleep(stuck_sleep)
            continue

        launch_sha = head
        start = time.time()
        log.info("launch runner @ %s", launch_sha)
        rc = _run_runner()
        elapsed = time.time() - start

        d = decide_after(rc, elapsed, launch_sha, state.get("last_good"),
                         int(state.get("early_fails", 0)), grace, max_fails)
        state["early_fails"] = d["early_fails"]
        if d.get("mark_good"):
            state["last_good"] = d["mark_good"]
        if d["action"] == "stop":
            save_state(state)
            log.info("runner вышел чисто (rc=0) — останавливаюсь")
            return
        if d["action"] == "rollback":
            if rollback(d["to"]):
                journal(f"моя правка {d['bad']} падает на старте (rc={rc}, прожила {elapsed:.0f}s) — "
                        f"откатила на {d['to']}. Посмотреть diff: git show {d['bad']}")
        save_state(state)


if __name__ == "__main__":
    run()
