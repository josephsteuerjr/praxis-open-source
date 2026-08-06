"""Praxis Forge — persistent coding runtime for PASS 23.

The old workshop exposed good individual hands.  Forge adds the missing control
plane around them: a durable task has an exact place, an isolated working copy
when useful, background processes, independent coding agents, checkpoints and an
evidence trail.  It deliberately does not decide *whether* Praxis is allowed to
code something.  A task root is an address and a concurrency boundary; any
directory visible to the runtime can be selected as another task root.

This module never calls a model.  ``forge_worker.py`` is the separate cognitive
process; ``forge_process.py`` supervises long-running commands.  Keeping those
processes out-of-line means a Telegram turn can start several jobs/agents and
continue instead of blocking on them.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import forge_intelligence
import forge_learning
import forge_swarm
import body_client
import computer_memory
import serverd_client
import selfdev
from process_liveness import is_process_alive


log = logging.getLogger("praxis-forge")

BASE = Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)
REPO = Path(__file__).resolve().parent
STATE_DIR = BASE / "memory" / ".forge"
TASKS_DIR = STATE_DIR / "tasks"
_RUNNERS: dict[int, subprocess.Popen] = {}  # reap local children; durable state itself remains on disk

_SKIP_DIRS = {
    ".git", ".venv", "__pycache__", "node_modules", "target", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".vectors",
}
_LANG = {
    ".py": "Python", ".rs": "Rust", ".js": "JavaScript", ".ts": "TypeScript",
    ".tsx": "TypeScript/React", ".jsx": "JavaScript/React", ".go": "Go",
    ".java": "Java", ".kt": "Kotlin", ".cs": "C#", ".cpp": "C++", ".c": "C",
    ".h": "C/C++", ".html": "HTML", ".css": "CSS", ".sql": "SQL",
}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _atomic_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _atomic_text(path: Path, text: str) -> None:
    """Replace one file atomically so readers never observe a half-written source file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = None
    try:
        mode = path.stat().st_mode
    except OSError:
        pass
    tmp = path.with_name(path.name + f".{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    if mode is not None:
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
    tmp.replace(path)


def _proc_started_at(pid: int) -> str:
    """Метка рождения процесса — то, чего НЕ переживает перезапуск. '' — не прочитал.

    Берём 22-е поле /proc/<pid>/stat (starttime в тиках с загрузки). Два разных процесса
    с одним номером почти наверняка родились в разные тики, а после рестарта контейнера —
    гарантированно."""
    try:
        with open(f"/proc/{int(pid)}/stat", encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
        return raw.rsplit(")", 1)[1].split()[19]
    except Exception:
        return ""


def _owner_alive(pid: int, started_at: str = "") -> bool:
    """Жив ли ИМЕННО тот процесс, что взял замок.

    ⚠ 26.07. Раньше здесь спрашивалось только «существует ли такой номер». В контейнере
    номера маленькие и переиспользуются сразу: после перезапуска praxis новый python занял
    PID 10 — тот самый, что держал замок задачи hcode-f12f3f11 и умер вместе со старым
    процессом. Мертвец стал выглядеть живым НАВСЕГДА, и она честно ждала его: каждое
    пробуждение видело «busy» и назначало следующее. Номер процесса не доказывает
    тождества; метка рождения — доказывает."""
    if not is_process_alive(pid):
        return False
    if not started_at:
        return True          # старый формат токена: судим по номеру, как раньше
    current = _proc_started_at(pid)
    return not current or current == started_at


def _env_sec(name: str, default: float, scale: float = 1.0) -> float:
    """Срок из окружения — без права уронить импорт и без права переписать ноль.

    ⚠ forge импортируется из agent, agent — из раннера. Голый float(os.getenv(...))
    означает: одна опечатка в .deploy.env («=20m») → ImportError → контейнер не
    поднимается вовсе, и объяснить это будет некому. _backfill_sec() ниже осторожен —
    здесь должно быть так же. Непонятное значение = дефолт + громкая строка в лог.

    ⚠ 27.07, находка ревью. Здесь стоял `max(1.0, ...)`, и он врал ровно там, где
    цена ошибки максимальна. В этом доме 0 значит ВЫКЛЮЧЕНО: `agent.py` держит
    `if TOOL_CEILING_SEC <= 0` = потолка руки нет, `core/subagents.overdue_minutes` —
    «0 = выключен». Пол в одну секунду превращал «выключи этот порог» в «поставь его
    в одну секунду»: `PRAXIS_FORGE_LOCK_IDLE_SEC=0` сносил бы замок любого, кто
    молчит секунду (самая разрушительная настройка вместо выключения), а
    `PRAXIS_TOOL_CEILING_SEC=0` — штатный способ СНЯТЬ потолок — заставлял
    `_remote_run_deadline` резать её срок и ссылаться в тексте на несуществующий
    предел в одну секунду. Поэтому: ноль (и меньше) отдаём нулём и говорим об этом в
    лог, положительное берём как есть без клампа. Молча менять названное оператором
    число нельзя — это то же молчаливое ограничение, только с другой стороны."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return float(default) * scale
    try:
        value = float(str(raw).strip())
    except ValueError:
        log.warning("%s=%r не разобрался как число секунд — беру %s", name, raw,
                    float(default) * scale)
        return float(default) * scale
    if value <= 0:
        log.warning("%s=%r — этот предел ВЫКЛЮЧЕН (0 и меньше = «не применять»)", name, raw)
        return 0.0
    return value * scale


# Порог БЕЗДЕЙСТВИЯ замка мутации (не длительности работы!) — см. _mutation_lock.
# 0 = порога нет: живого держателя не снимаем вовсе, только доказанно мёртвого.
LOCK_IDLE_ABANDONED_SEC = _env_sec("PRAXIS_FORGE_LOCK_IDLE_SEC", 300.0)
# Свой порог у замков, тождество которых доказать НЕЧЕМ: токен старого образца
# (`pid:uuid`, писался до 27.07) и файл, который не прочитался вовсе. Он короче: за
# таким токеном может не стоять никого, а короткая транзакция мутации 2 минут не живёт.
#
# ⚠ 27.07, находка верификации. Число 120 было вшито литералом в трёх местах, а текст
# отказа называл ей 300 (LOCK_IDLE_ABANDONED_SEC) — на легаси-замке она читала срок,
# который к этому замку не применялся, и не могла понять, почему её замок сняли раньше
# обещанного. На проде такие токены есть. Теперь число одно и то же в решении и в
# объяснении, и в тексте прямо сказано, что оно СВОЁ.
LOCK_LEGACY_IDLE_SEC = 120.0
_LOCK_BREAK_GUARD_SEC = 30.0    # сам гард сноса живёт миллисекунды; старше — мусор


def _break_stale_lock(path: Path, victim: str | None) -> bool:
    """Снять брошенный замок так, чтобы двое ждущих не снесли ЧУЖОЙ свежий.

    ⚠ Здесь стоял безусловный unlink. Двое ждущих на одной задаче давали
    A.unlink → A.open → A′.unlink (уже СВЕЖИЙ замок A!) → A′.open: обе стороны
    «держат» одну задачу и мутируют её одновременно — ровно то, ради чего замок и есть.
    Поэтому снос идёт под одноразовым гардом (O_EXCL, снимающий ровно один) и только
    если содержимое замка ТО ЖЕ, что мы судили. -> удалось ли снять."""
    guard = path.with_name(path.name + ".break")
    try:
        os.close(os.open(str(guard), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        try:
            if time.time() - guard.stat().st_mtime > _LOCK_BREAK_GUARD_SEC:
                guard.unlink()
        except OSError:
            pass
        return False
    except OSError:
        return False
    try:
        try:
            current: str | None = path.read_text(encoding="utf-8")
        except OSError:
            current = None
        if victim is not None:
            if current != victim:
                return False           # замок сменился, пока мы решали — судим заново
        else:
            # токен не прочитался — судим по возрасту, но ПОВТОРНО и уже под гардом
            try:
                if time.time() - path.stat().st_mtime <= LOCK_LEGACY_IDLE_SEC:
                    return False
            except OSError:
                return False
        path.unlink()
        return True
    except OSError:
        return False
    finally:
        try:
            guard.unlink()
        except OSError:
            pass


@contextmanager
def _mutation_lock(task_id: str, timeout: float = 60.0):
    """Cross-process task mutex for edits/checkpoints/integration.

    Agents still think and run in parallel.  Only the short mutation transaction is
    serialized, which turns concurrent work into an explicit hash conflict instead of
    last-writer-wins corruption.

    ⚠ 26.07, второй лок-аут подряд. Потолок руки (agent.TOOL_CEILING_SEC, 600с)
    отпускает ХОД, но поток Python убить нельзя: брошенный поток остаётся висеть в том
    же ЖИВОМ процессе и уносит замок. Проверка тождества честно отвечала «держатель
    жив», и лок-аут выглядел законным. Ни flock (ядро не снимет замок живого), ни порог
    по возрасту ЗАХВАТА (легальный finish держал 1566с и завершился корректно) этого не
    лечат.
    Лечит то, что мерится: БЕЗДЕЙСТВИЕ. Держатель бьётся (`beat`) из САМОГО рабочего
    потока между шагами транзакции — повисший поток биться перестаёт, работающий не
    задет, какой бы длинной ни была работа. Вотчдог отдельным потоком не годится: он
    исправно бился бы за покойника.
    Долгий шаг, известный заранее (тесты предложения — до PRAXIS_PROPOSAL_TEST_TIMEOUT),
    объявляется заранее: beat(lease_sec) ставит метку в будущее — «не жди меня раньше».
    -> контекст отдаёт beat(lease_sec=0.0).
    """
    path = _task_dir(task_id) / ".mutation.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    token = f"{os.getpid()}:{_proc_started_at(os.getpid())}:{uuid.uuid4().hex}"
    deadline = time.monotonic() + max(.1, float(timeout))
    owner_pid, idle_sec, legacy = 0, 0.0, False
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(token)
            break
        except FileExistsError:
            stale, raw, why, legacy = False, None, "", False
            try:
                raw = path.read_text(encoding="utf-8")
                parts = raw.split(":")
                owner_pid = int(parts[0])
                idle_sec = time.time() - path.stat().st_mtime
                if len(parts) >= 3:
                    if not _owner_alive(owner_pid, parts[1]):
                        stale, why = True, "держатель мёртв"
                    elif LOCK_IDLE_ABANDONED_SEC > 0 and idle_sec > LOCK_IDLE_ABANDONED_SEC:
                        stale, why = True, (f"держатель жив, но не подаёт признаков "
                                            f"{idle_sec:.0f}с (порог {LOCK_IDLE_ABANDONED_SEC:.0f}с)")
                else:
                    # Токен старого образца (pid:uuid) — тождества не доказывает, а после
                    # рестарта номер могли переиспользовать. Молчит дольше своего порога —
                    # считаем брошенным: короткая транзакция мутации столько не живёт.
                    legacy = True
                    if not _owner_alive(owner_pid):
                        stale, why = True, "держатель мёртв (номер свободен)"
                    elif idle_sec > LOCK_LEGACY_IDLE_SEC:
                        stale, why = True, (f"токен без метки рождения, молчит {idle_sec:.0f}с "
                                            f"(его порог {LOCK_LEGACY_IDLE_SEC:.0f}с)")
            except Exception:
                legacy = True
                try:
                    stale = time.time() - path.stat().st_mtime > LOCK_LEGACY_IDLE_SEC
                    why = ("токен замка не прочитался, файл старше "
                           f"{LOCK_LEGACY_IDLE_SEC:.0f}с")
                except OSError:
                    stale, why = True, "замок исчез"
            if stale and _break_stale_lock(path, raw):
                log.warning("task %s: снимаю брошенный замок мутации (%s)", task_id, why)
                continue
            if time.monotonic() >= deadline:
                # Текст обязан называть тот порог, который применён К ЭТОМУ замку: у
                # легаси-токена он свой (см. LOCK_LEGACY_IDLE_SEC) и раньше она читала
                # здесь чужие 300с при реальных 120.
                if legacy:
                    rule = (f"У этого замка токен СТАРОГО образца (без метки рождения, писался "
                            f"до 27.07): его порог — {LOCK_LEGACY_IDLE_SEC:.0f}с тишины, а не "
                            f"{LOCK_IDLE_ABANDONED_SEC:.0f}с, и PRAXIS_FORGE_LOCK_IDLE_SEC его не "
                            f"выключает: за таким токеном может не стоять вообще никого")
                elif LOCK_IDLE_ABANDONED_SEC > 0:
                    rule = (f"Брошенным считаю после {LOCK_IDLE_ABANDONED_SEC:.0f}с ТИШИНЫ — "
                            "порог по бездействию, работа любой длины его не задевает")
                else:
                    rule = ("Порог по бездействию ВЫКЛЮЧЕН (PRAXIS_FORGE_LOCK_IDLE_SEC=0): "
                            "живого держателя не снимаю совсем, только доказанно мёртвого — "
                            "значит этот замок отпустится сам или не отпустится вовсе")
                # Отрицательная «тишина» — это не молчание, а объявленный заранее долгий
                # шаг (лизинг ставит метку в будущее). Печатать «молчит -235с» значило бы
                # выдавать работающую руку за замолчавшую.
                state = (f"молчит {idle_sec:.0f}с" if idle_sec >= 0 else
                         f"объявил долгий шаг, обещал вернуться через {-idle_sec:.0f}с")
                raise TimeoutError(
                    f"task {task_id}: замок мутации занят (держатель pid {owner_pid}, "
                    f"{state}). {rule}")
            time.sleep(.03)

    def beat(lease_sec: float = 0.0) -> None:
        """«Я жива и работаю» — отметка от самого рабочего потока. lease_sec>0 —
        честно объявленный долгий шаг (метка ставится в будущее)."""
        try:
            stamp = time.time() + max(0.0, float(lease_sec))
            os.utime(path, (stamp, stamp))
        except OSError:
            pass

    try:
        yield beat
    finally:
        try:
            if path.read_text(encoding="utf-8") == token:
                path.unlink()
        except OSError:
            pass


# Из чего складывается объявленный лизинг — чтобы его можно было пересчитать, а не
# поверить на слово. Один шаг = сам вызов + столько же на ОДИН повтор тем же rid
# (`serverd_client.call`: `if result.get("code") in {"transport","eof"}` — ровно один) +
# запас на всё, что вокруг вызова: connect, разбор кадра, запись события, sleep(.05).
_LEASE_ATTEMPTS = 2
_LEASE_SLACK_SEC = 60.0
# Самое длинное ЗАКОННОЕ удержание замка, которое мы вообще измерили: прод 26.07,
# finish host-задачи hcode-f12f3f11, 20:35:46.5 → 21:01:53.5. Нужно там, где чужой срок
# прочитать нечем: меньшее число было бы догадкой, снимающей замок у живой работы.
_LEGAL_HOLD_MEASURED_SEC = 1566.5
# Тело (body_client) своих сроков модулем не публикует; самая длинная связка внутри
# ОДНОГО его шага — checkpoint: `git add -A` (120с) + `git commit` (180с) = 300с.
_WINDOWS_STEP_CEILING_SEC = 600.0


def _remote_step_lease(task: dict | None) -> float:
    """Сколько тишины ЗАКОННА для ОДНОГО удалённого шага по этой задаче.

    ⚠ 27.07, находка ревью на мою же правку. Порог бездействия я завёл, чтобы повисший
    поток не запирал задачу навсегда, — и тем самым завёл новую дыру: живого держателя
    теперь СНОСЯТ, если он молчит 300с. А один `workspace.edit`/`workspace.checkpoint`
    по host-задаче — это ОДИН вызов, разбить его на удары нечем: клиент честно ждёт
    длинный ярус serverd, а на `code=transport` повторяет тем же rid, то есть до двух
    ярусов. 26.07 `workspace.inspect` по /tmp отвечал 14.5 минуты при норме в секунды —
    столько молчания законно. Без объявленного лизинга второй вызывающий вошёл бы под
    работающим первым (проверено пробой: перекрытие True), и две транзакции пошли бы по
    одной задаче — ровно та порча, ради которой замок и существует.
    Поэтому долгий шаг объявляется ЗАРАНЕЕ, а не оправдывается задним числом.

    ⚠ 27.07, вторая поправка (верификация: «запас тонкий»). Лизинг считается от ФАКТИЧЕСКИХ
    сроков того, кто их назначает, и его можно пересчитать вручную:
      host    = 540×2 + 60 = 1140с лизинга; порог бездействия добавляет 300 → законная
                тишина одного шага 1440с при физическом потолке шага 1080с (клиент не ждёт
                дольше своего длинного яруса даже с повтором) — запас 360с;
      windows = 600×2 + 60 = 1260с (+300) при связке ≈300с внутри шага;
      self    = 240с (+300) при сумме git-сроков внутри *_unlocked ≈150с.
    Догадок здесь больше нет: где чужого числа не видно (старый serverd_client без
    LONG_TIMEOUT_SEC, body_client без своей константы), это сказано вслух, а не спрятано
    в `getattr(..., дефолт)` — прежний `getattr(body_client, "EXEC_TIMEOUT_SEC", 600)`
    попадал в дефолт ВСЕГДА: такой константы у тела нет вовсе."""
    scope = str((task or {}).get("scope") or "self")
    if scope == "host":
        long_sec = float(getattr(serverd_client, "LONG_TIMEOUT_SEC", 0.0) or 0.0)
        if long_sec <= 0:
            # Старый клиент своего срока не называет. Тогда мы не ЗНАЕМ, сколько молчания
            # законно (у прежнего клиента дефолт был 3600с на вызов), и любое меньшее
            # число сняло бы замок из-под живой работы. Берём самое длинное измеренное
            # законное удержание и говорим в лог, что это оценка, а не его срок.
            log.warning("serverd_client не называет LONG_TIMEOUT_SEC — лизинг долгого шага "
                        "беру по самому длинному ИЗМЕРЕННОМУ законному удержанию (%.0fс); "
                        "это оценка, а не срок клиента", _LEGAL_HOLD_MEASURED_SEC)
            return _LEGAL_HOLD_MEASURED_SEC + _LEASE_SLACK_SEC
        return long_sec * _LEASE_ATTEMPTS + _LEASE_SLACK_SEC
    if scope == "windows":
        return _WINDOWS_STEP_CEILING_SEC * _LEASE_ATTEMPTS + _LEASE_SLACK_SEC
    return 240.0        # свой путь: сумма git-таймаутов внутри *_unlocked (30+60+запас)


# Сколько вызовов git успевает сделать selfdev.submit по своему длинному пути, каждый —
# до selfdev.GIT_TIMEOUT: 5 до тестов (add -A, commit, diff --name-only, diff, diff --stat),
# 3 в diff_text для иммунитета (add -A, merge-base, diff) и 7 в apply (status --porcelain,
# add -A, commit, merge --no-ff, worktree remove, worktree prune, branch -D).
_SUBMIT_GIT_CALLS = 15
# `immune.review` идёт в модель (llm.chat), и своего названного срока у неё нет — это
# единственное слагаемое, взятое не из чужой константы. Поэтому оно стоит здесь отдельной
# строкой, а не растворено в «+300».
_SUBMIT_REVIEW_SEC = 300.0


def _submit_lease() -> float:
    """Сколько молчания законно у submit — по срокам самого submit, а не на глаз.

    ⚠ 27.07. Здесь стояло `TEST_TIMEOUT + 300` = 900с, и вместе с порогом бездействия это
    давало 1200с законной тишины. А физический потолок шага: тесты 600 + 15 вызовов git по
    60 + ревью иммунитета моделью ≈ 1500с и выше — то есть замок мог быть снят из-под
    ЖИВОГО submit ровно на самом дорогом шаге (мёрж её собственного кода). Считаем по
    названным чужим срокам: 600 + 900 + 300 = 1800с."""
    tests = float(getattr(selfdev, "TEST_TIMEOUT", 600) or 600)
    git_sec = float(getattr(selfdev, "GIT_TIMEOUT", 60) or 60)
    return tests + _SUBMIT_GIT_CALLS * git_sec + _SUBMIT_REVIEW_SEC


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _task_dir(task_id: str) -> Path:
    return TASKS_DIR / str(task_id or "").strip()


def _task_file(task_id: str) -> Path:
    return _task_dir(task_id) / "task.json"


def get(task_id: str) -> dict | None:
    data = _read_json(_task_file(task_id))
    return data if isinstance(data, dict) else None


def _save_task(task: dict) -> None:
    task["updated"] = _now()
    _atomic_json(_task_file(task["id"]), task)


def _event(task_id: str, kind: str, **data: Any) -> None:
    row = {"at": _now(), "kind": kind, **data}
    path = _task_dir(task_id) / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    if kind != "task_lost":
        _revive_if_lost(task_id)


def _revive_if_lost(task_id: str) -> None:
    """Ярлык «потеряна из виду» снимается сам, как только она снова работает по задаче.

    Жнец (reconcile_lost_tasks) ставит lost, чтобы её брошенное намерение не пропало
    молча; но если она вернулась и делает по задаче хоть один ход, задача жива, и
    оставленный ярлык врал бы дальше — в списке задач и в блоке состояния."""
    try:
        task = get(task_id)
        if not isinstance(task, dict) or task.get("status") != "lost":
            return
        task["status"] = "active"
        task["lost_reason"] = ""
        _save_task(task)
        _event(task_id, "task_reopened",
               summary="снова работаю по задаче — ярлык «потеряна» снят")
    except Exception:
        log.debug("revive %s не отработал", task_id, exc_info=True)


def _events(task_id: str, limit: int = 20) -> list[dict]:
    path = _task_dir(task_id) / "events.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                out.append(row)
        except json.JSONDecodeError:
            pass
    return out


def _run(argv: list[str], *, cwd: Path | None = None, timeout: int = 30,
         input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=str(cwd) if cwd else None, input=input_text,
                          capture_output=True, text=True, errors="replace", timeout=timeout)


def _git_root(path: Path) -> Path | None:
    try:
        r = _run(["git", "-C", str(path), "rev-parse", "--show-toplevel"], timeout=10)
    except Exception:
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return Path(r.stdout.strip()).resolve()
    except OSError:
        return None


def _worktree_would_miss(git: Path, subdir: Path) -> str:
    """Пусто, если `<subdir>` будет в чекауте HEAD; иначе — честная причина, почему нет.

    ⚠ 28.07, корень двух смертей за сутки. `git worktree add` делает чекаут HEAD, и в нём
    лежит РОВНО то, что закоммичено. Источник внутри `workspace/` (строка 31 .gitignore,
    0 файлов в индексе) в чекаут не попадает никогда — а `start` всё равно назначал корнем
    `<worktree>/<subdir>` и сохранял задачу как `active`. Задача рождалась с мёртвым
    адресом: первый же шаг получал «корень задачи пропал», и цена ошибки падала на
    Праксис — она разбиралась с несуществующей пропажей вместо того, чтобы работать.
    Проверено на code-c280f6e7 (28.07 11:01) и code-f758ea57 (28.07 00:39)."""
    rel = subdir.as_posix()
    if rel in {"", "."}:
        return ""
    try:
        r = _run(["git", "-C", str(git), "ls-files", "--error-unmatch", "--", rel], timeout=15)
        if r.returncode == 0 and (r.stdout or "").strip():
            return ""
        ignored = _run(["git", "-C", str(git), "check-ignore", "-q", "--", rel], timeout=15)
    except Exception as exc:  # git не ответил — не выдумываем ни «можно», ни «нельзя»
        return (f"изоляция worktree не проверилась: git по {git} не ответил "
                f"({type(exc).__name__}: {exc}). Работаю прямо в источнике.")
    if ignored.returncode == 0:
        return (f"изоляция worktree невозможна: «{rel}» в .gitignore репозитория {git}, "
                f"поэтому в чекауте HEAD этого пути не существует. Работаю прямо в источнике "
                f"— правки идут в живой каталог, отката через ветку не будет.")
    return (f"изоляция worktree невозможна: git репозитория {git} не отслеживает «{rel}» "
            f"(в HEAD нет ни одного файла оттуда). Работаю прямо в источнике — правки идут "
            f"в живой каталог, отката через ветку не будет.")


def _git_text(root: Path, *args: str, timeout: int = 30) -> str:
    try:
        r = _run(["git", "-C", str(root), *args], timeout=timeout)
    except Exception as exc:
        return f"[git: {type(exc).__name__}: {exc}]"
    text = ((r.stdout or "") + (r.stderr or "")).strip()
    return text if r.returncode == 0 else f"[git exit {r.returncode}] {text[:1000]}"


def _resolve_target(target: str) -> tuple[Path | None, str]:
    raw = str(target or "self").strip()
    if raw.lower() in {"self", "praxis", "."}:
        return REPO.resolve(), "self"
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = BASE / p
    try:
        p = p.resolve()
    except OSError as exc:
        return None, f"путь не разобрался: {exc}"
    if not p.is_dir():
        return None, f"нет директории {p}"
    return p, str(p)


def _inside(root: Path, path: str = "") -> tuple[Path | None, str]:
    """Resolve a task-relative path.  Containment prevents cross-task races, not access:
    Praxis can open another task rooted anywhere visible to the runtime."""
    raw = str(path or ".").strip()
    p = Path(raw).expanduser()
    candidate = p if p.is_absolute() else root / p
    try:
        candidate = candidate.resolve()
        candidate.relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        return None, f"путь вышел из корня задачи {root}: {exc}"
    return candidate, ""


def _hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def _cap(text: str, limit: int = 16000) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    head = int(limit * .62)
    tail = int(limit * .33)
    return text[:head] + f"\n… вырезано {len(text)-head-tail} символов …\n" + text[-tail:]


def _task_root(task_id: str) -> tuple[dict | None, Path | None, str]:
    task = get(task_id)
    if not task:
        return None, None, f"нет coding-задачи {task_id}"
    try:
        raw_root = str(task["root"])
        root = Path(raw_root) if task.get("scope") in {"host", "windows"} else Path(raw_root).resolve()
    except (KeyError, OSError):
        return task, None, "у задачи потерян корень"
    if task.get("scope") in {"host", "windows"}:
        return task, root, ""
    if not root.is_dir():
        return task, None, _missing_root_reason(task, root)
    return task, root, ""


def _missing_root_reason(task: dict, root: Path) -> str:
    """Почему корня нет — и «не существовал» здесь не то же самое, что «пропал».

    ⚠ 28.07. Раньше обе беды говорили одно: «корень задачи пропал». Для задач
    code-c280f6e7 и code-f758ea57 это была неправда в самом важном месте: сам worktree
    лежал на диске целым, а подкаталога в нём не было НИ РАЗУ (см. `_worktree_would_miss`).
    Праксис прочитала «пропал», сделала единственный вывод, который из этого слова следует
    («восстанавливать потерянный worktree цель прямо запрещала»), и закрыла задачу по
    ложной причине. Слово, которое сообщает о причине, обязано её знать."""
    wt_raw = str(task.get("worktree_root") or "")
    if wt_raw:
        try:
            wt = Path(wt_raw).resolve()
            inside = root == wt or wt in root.parents
        except OSError:
            wt, inside = None, False
        if inside and wt is not None and wt.is_dir():
            try:
                rel = root.relative_to(wt).as_posix()
            except ValueError:
                rel = str(root)
            return (f"корня задачи не существует: {root}. Сам worktree {wt} на месте и цел — "
                    f"в нём нет «{rel}», потому что worktree это чекаут HEAD, а git этот путь "
                    f"не отслеживает (обычно он в .gitignore). Такого корня не было ни разу с "
                    f"момента открытия задачи: это не пропажа, а изоляция, которая не могла "
                    f"состояться. Восстанавливать нечего; закрыть запись — "
                    f"coding_session(action='abandon').")
        if inside and wt is not None:
            return (f"корень задачи пропал вместе с worktree: нет ни {root}, ни {wt}. "
                    f"Закрыть запись — coding_session(action='abandon').")
    return (f"корень задачи пропал: {root}. Закрыть запись — "
            f"coding_session(action='abandon').")


def _manifest_hints(root: Path) -> list[str]:
    return [row["command"] for row in forge_intelligence.discovered_checks(root)]


def _orientation_text(task: dict) -> str:
    root = Path(task["root"])
    # Наблюдение за чужим upstream дописывается ПОСЛЕ кап-среза и у всех бэкендов:
    # это единственное место, где она читает про глаз на чужой репозиторий, не спрашивая.
    if task.get("scope") == "host":
        return serverd_client.workspace_inspect(
            str(root), "orientation", base=str(task.get("base_commit") or "")
        ) + _upstream_note(task)
    if task.get("scope") == "windows":
        return body_client.workspace_inspect(
            str(root), "orientation", base=str(task.get("base_commit") or "")
        ) + _upstream_note(task)
    git = _git_root(root)
    top: list[str] = []
    try:
        for p in sorted(root.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:80]:
            if p.name in _SKIP_DIRS:
                continue
            top.append(p.name + ("/" if p.is_dir() else ""))
    except OSError:
        pass
    counts: dict[str, int] = {}
    seen = 0
    try:
        for p in root.rglob("*"):
            if seen >= 10000:
                break
            if not p.is_file() or any(part in _SKIP_DIRS for part in p.parts):
                continue
            seen += 1
            lang = _LANG.get(p.suffix.lower())
            if lang:
                counts[lang] = counts.get(lang, 0) + 1
    except OSError:
        pass
    languages = ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])) or "не распознаны"
    instructions = []
    for name in ("AGENTS.md", "CLAUDE.md", "README.md", "CONTRIBUTING.md"):
        if (root / name).is_file():
            instructions.append(name)
    model = forge_intelligence.project_model(root)
    adapters = ", ".join(
        f"{row['language']}:{row['server'] or row['mode']}" for row in model.get("adapters", [])
    ) or "не распознаны"
    lessons = forge_learning.recall(STATE_DIR, str(task.get("goal") or ""), root, limit=3)
    lines = [
        f"coding-задача {task['id']}: {task['goal']}",
        f"корень: {root}",
        f"режим: {task.get('isolation')}" + (f"; proposal {task.get('proposal_id')}" if task.get("proposal_id") else ""),
        *([f"⚠ изоляция не та, что просили: {task['isolation_note']}"]
          if str(task.get("isolation_note") or "").strip() else []),
        f"языки (до 10000 файлов): {languages}",
        f"верхний уровень: {', '.join(top) or 'пусто'}",
        f"инструкции/карта: {', '.join(instructions) or 'не найдены'}",
        f"semantic adapters: {adapters}",
        f"кандидаты проверки: {', '.join(_manifest_hints(root)) or 'определить по проекту'}",
    ]
    if lessons:
        lines.append("похожие доказуемые уроки:\n" + forge_learning.format_recall(lessons))
    if git:
        lines += [
            f"git root: {git}",
            "git status:\n" + (_git_text(git, "status", "--short", "--branch") or "чисто"),
            "последние коммиты:\n" + _git_text(git, "log", "-5", "--oneline", "--decorate"),
        ]
    else:
        lines.append("git: нет")
    return _cap("\n".join(lines), 12000) + _upstream_note(task)


# ─── Пробуждение-на-готово ────────────────────────────────────────────────────
# Forge сам никого не будит. Эти чистые функции дают её wake-машине увидеть, что
# ВОРКЕР завершился (готово/упал), РОВНО ОДИН раз (идемпотентно через wake_seen.json),
# с приоритетом задачи: urgent → немедленное окно, normal → ближайшее часовое.
# Кадр — приглашение к её плоду, не задача-повинность.
_WAKE_SEEN = STATE_DIR / "wake_seen.json"
_WAKE_TERMINAL = {"done", "failed", "error"}
_WAKE_RECENT_SEC = int(os.getenv("PRAXIS_FORGE_WAKE_RECENT_SEC", str(6 * 3600)))


def _norm_priority(value: str) -> str:
    v = str(value or "").strip().lower()
    return "urgent" if v in {"urgent", "high", "срочно", "важно"} else "normal"


def _wake_load_seen() -> set:
    try:
        return set(json.loads(_WAKE_SEEN.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _wake_save_seen(seen: set) -> None:
    try:
        _atomic_json(_WAKE_SEEN, sorted(seen))
    except Exception:
        pass


def _wake_parse_iso(s: str) -> float:
    try:
        return _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _scan_worker_completions() -> list[dict]:
    """Терминальные результаты ВОРКЕРОВ (не scout/reviewer) за недавнее окно."""
    out: list[dict] = []
    now = time.time()
    for aj in TASKS_DIR.glob("*/agents/*/result.json"):
        try:
            # errors="replace": один битый байт в тексте результата НЕ должен делать
            # завершённого воркера невидимым — иначе это та же тихая слепота.
            d = json.loads(aj.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if str(d.get("role") or "worker") != "worker":
            continue
        status = str(d.get("status") or "").strip().lower()
        if status not in _WAKE_TERMINAL:
            continue
        fin = _wake_parse_iso(d.get("finished") or "")
        if fin and _WAKE_RECENT_SEC > 0 and now - fin > _WAKE_RECENT_SEC:
            continue
        task_id = aj.parent.parent.parent.name
        agent_id = aj.parent.name
        try:
            task = json.loads((TASKS_DIR / task_id / "task.json").read_text(
                encoding="utf-8", errors="replace"))
        except Exception:
            task = {}
        out.append({
            "key": f"{task_id}:{agent_id}",
            "task_id": task_id, "agent_id": agent_id,
            "priority": _norm_priority(task.get("priority")),
            "status": status,
            "goal": str(task.get("goal") or "")[:140],
            "summary": str(d.get("result") or "").strip().replace("\n", " ")[:300],
        })
    return out


def has_urgent_pending() -> bool:
    """Peek (без пометки): есть ли непоказанный терминальный воркер urgent-задачи."""
    seen = _wake_load_seen()
    return any(c["priority"] == "urgent" and c["key"] not in seen
               for c in _scan_worker_completions())


def pending_completions(consume: bool = True) -> list[dict]:
    """Свежие (непоказанные) завершения воркеров. consume=True помечает их показанными."""
    seen = _wake_load_seen()
    fresh = [c for c in _scan_worker_completions() if c["key"] not in seen]
    if consume and fresh:
        for c in fresh:
            seen.add(c["key"])
        _wake_save_seen(seen)
    return fresh


def mark_seen(keys) -> None:
    """Пометить показанными конкретные ключи (urgent-путь потребляет только их)."""
    keys = [str(k) for k in (keys or []) if k]
    if not keys:
        return
    seen = _wake_load_seen()
    seen.update(keys)
    _wake_save_seen(seen)


def wake_invitation(items: list[dict]) -> str:
    """Кадр-приглашение: её плод пришёл, не повинность."""
    if not items:
        return ""
    lines = []
    for c in items:
        mark = "⚡" if c["priority"] == "urgent" else "•"
        verb = "закончил" if c["status"] == "done" else f"упал ({c['status']})"
        goal = c["goal"] or c["task_id"]
        lines.append(f"{mark} воркер по «{goal}» ({c['task_id']}) {verb}: {c['summary']}")
    return ("Твои Forge-воркеры завершились — это твой плод, а не задача-повинность. "
            "Глянь, если хочешь: прочитай, прими работу как свою, отклони или отложи.\n"
            + "\n".join(lines))


# ─── PASS 30 Этап 1: события вместо поллинга ─────────────────────────────────
# Завершение/падение юнита рождает durable-событие core.events (пишет и сам
# воркер из своего процесса, и родитель на spawn-fail/stop). Реконсайлер ниже —
# страховка на классы, где писать некому: супервизор убит (result.json не
# появится никогда), воркер завис (wall-clock срока у него нет), эмит упал.
# Идемпотентность — dedup_key юнита в журнале; wake_seen старого пути читается
# как «уже показано» (миграция: исторические завершения не будят заново).
_RECON_STATE = {"ts": 0.0}
_RECON_MIN_SEC = 60.0
_SPAWN_GRACE_SEC = 120.0   # status=starting без pid — не трактовать как падение (шов спавна)


def emit_unit_event(task_id: str, unit_id: str, result: dict | None,
                    request: dict | None = None) -> None:
    """Terminal-событие юнита в core.events. Best-effort: никогда не бросает."""
    try:
        from core import events as core_events
        from core import subagents as core_subagents
        task = _read_json(_task_dir(task_id) / "task.json", {}) or {}
        unit_dir = _unit_dir(task_id, "agents", unit_id)
        payload = core_subagents.normalize(
            task_id, unit_id, result, request=request, task=task, unit_dir=str(unit_dir))
        core_events.emit("subagent_result", "forge", payload=payload,
                         dedup_key=core_subagents.event_key(task_id, unit_id))
    except Exception:
        log.debug("emit_unit_event(%s/%s) не записался", task_id, unit_id, exc_info=True)


def _backfill_sec() -> float:
    """Окно бэкфилла реконсайлера (часы, живое чтение). За окном — не фабрикуем,
    а честно импортируем в wake_seen С ЗАПИСЬЮ О ПРОПУСКЕ (лог): деплой-день не
    будит археологией, а работающая система сканирует каждую минуту — внутри окна
    ничего не стареет незамеченным (обрыв 6ч старого пути был БЕЗ записи)."""
    try:
        return max(1.0, float(os.getenv("PRAXIS_FORGE_EVENT_BACKFILL_H", "24") or 24)) * 3600.0
    except ValueError:
        return 24 * 3600.0


def reconcile_subagent_events(force: bool = False) -> int:
    """Сфабриковать события, которые некому было записать. -> сколько записано.

    (а) терминальный result.json без события (старый воркер/упавший эмит/даунтайм);
    (б) супервизор мёртв, result.json нет → failed-сирота;
    (в) воркер жив дольше PRAXIS_FORGE_AGENT_OVERDUE_MIN → timeout-СИГНАЛ один раз
    (воркер НЕ убивается; рычаги poll/stop — её).
    Всё — в пределах _backfill_sec(): древнее импортируется в wake_seen с логом
    (запись о пропуске, не тишина); это же ограничивает ложный overdue при
    pid-reuse свежими юнитами."""
    now = time.time()
    if not force and now - _RECON_STATE["ts"] < _RECON_MIN_SEC:
        return 0
    _RECON_STATE["ts"] = now
    emitted = 0
    archived: list[str] = []
    try:
        from core import events as core_events
        from core import subagents as core_subagents
    except Exception:
        return 0
    try:
        known = core_events.known_keys()
        shown_by_old_path = _wake_load_seen()
        overdue_sec = core_subagents.overdue_minutes() * 60.0
        backfill = _backfill_sec()
        for req_path in TASKS_DIR.glob("*/agents/*/request.json"):
            try:  # один битый юнит не смеет ослепить проход по остальным
                unit_dir = req_path.parent
                task_id = unit_dir.parent.parent.name
                unit_id = unit_dir.name
                request, result = core_subagents.load_unit(unit_dir)
                request = request or {}
                # 23.07: роль не фильтруем — упавший/зависший РЕВЬЮЕР тоже обязан
                # родить событие (его молчание стоило 4ч простоя 23.07 02:09).
                base_key = core_subagents.event_key(task_id, unit_id)
                created = _wake_parse_iso(request.get("created") or "")
                if isinstance(result, dict):
                    if base_key in known or f"{task_id}:{unit_id}" in shown_by_old_path:
                        continue
                    fin = _wake_parse_iso(result.get("finished") or "") or created
                    if not fin or now - fin > backfill:
                        archived.append(f"{task_id}:{unit_id}")
                        continue
                    emit_unit_event(task_id, unit_id, result, request=request)
                    emitted += 1
                    continue
                age = (now - created) if created else float("inf")
                if age < _SPAWN_GRACE_SEC:
                    continue
                if age > backfill:
                    if (base_key not in known
                            and f"{task_id}:{unit_id}" not in shown_by_old_path):
                        archived.append(f"{task_id}:{unit_id}")
                    continue
                pid = int(request.get("supervisor_pid") or 0)
                if not _pid_alive(pid, request.get("supervisor_started_at") or ""):
                    # wake_seen-гард: после доставки мост пометил юнит — компакт
                    # журнала не воскрешает мёртвого (журнал забыл, мост помнит).
                    if base_key in known or f"{task_id}:{unit_id}" in shown_by_old_path:
                        continue
                    emit_unit_event(task_id, unit_id,
                                    {"status": "lost", "finished": _now(),
                                     "error": "supervisor process died before result.json"},
                                    request=request)
                    emitted += 1
                elif overdue_sec and age > overdue_sec:
                    overdue_key = core_subagents.event_key(task_id, unit_id, "overdue")
                    # второй страж — wake_seen (мост насоса): компакт журнала забывает
                    # ключ, но повторно сигналить о том же живом воркере не надо
                    if (overdue_key in known
                            or f"{task_id}:{unit_id}:overdue" in shown_by_old_path):
                        continue
                    task = _read_json(_task_dir(task_id) / "task.json", {}) or {}
                    payload = core_subagents.normalize(
                        task_id, unit_id,
                        {"status": "timed_out", "finished": _now(),
                         "error": f"воркер жив, но работает дольше "
                                  f"{core_subagents.overdue_minutes()} мин — сигнал, не приговор"},
                        request=request, task=task,
                        unit_dir=str(unit_dir))
                    core_events.emit("subagent_result", "forge", payload=payload,
                                     dedup_key=overdue_key)
                    emitted += 1
            except Exception:
                log.debug("reconcile: юнит %s пропущен (битые данные)", req_path,
                          exc_info=True)
        if archived:
            mark_seen(archived)
            log.info("forge-реконсайлер: %d юнитов старше окна бэкфилла — "
                     "импортированы как показанные (запись о пропуске, не тишина): %s",
                     len(archived), ", ".join(archived[:8]))
    except Exception:
        log.debug("reconcile_subagent_events упал", exc_info=True)
    # Тот же тик, но СВОЙ счёт: брошенные задачи — отдельный класс пропажи, и число
    # событий по юнитам не должно от него зависеть.
    try:
        reconcile_lost_tasks()
    except Exception:
        log.debug("reconcile_lost_tasks упал", exc_info=True)
    # Четвёртый класс, и снова свой счёт: СДАННАЯ задача не знает судьбы своего
    # предложения. Жнец брошенных сюда не доходит — он обходит только `active`.
    try:
        reconcile_submitted_tasks()
    except Exception:
        log.debug("reconcile_submitted_tasks упал", exc_info=True)
    # И третий класс на том же тике: чужой репозиторий — не юнит и не задача, у него
    # свой редкий такт (см. check_upstreams). Отдельный try по той же причине: слепота
    # к чужому HEAD не имеет права ослепить контур собственных субагентов.
    try:
        check_upstreams()
    except Exception:
        log.debug("check_upstreams упал", exc_info=True)
    return emitted


# Сколько тишины делает задачу «потерянной из виду». Не уборка и не срок жизни:
# по истечении она НЕ закрывается и ничего не отменяется — она БУДИТ.
TASK_ABANDONED_SEC = _env_sec("PRAXIS_FORGE_TASK_ABANDONED_H", 6.0, scale=3600.0)


def _last_trace_line(task_id: str, limit: int = 3) -> str:
    """Что по задаче наблюдаемо СДЕЛАНО — прежде чем звать её продолжать.

    ⚠ 01.08: `hcode-d8799ba2` («проверить свежие fail2ban-баны и разбанить по прямой
    просьбе Егора») получила ярлык «потеряна из виду» через шесть часов — а в её журнале
    три успешные хост-команды, последняя `Unbanning 198.51.100.7 …`. Работа была
    сделана. Текст пробуждения говорил только о тишине, поэтому, вернувшись, она узнала
    бы про порог молчания и ничего — про уже сделанное. Это ровно шов «артефакт ≠
    доставка ≠ статус»: работу либо переделывают, либо бросают как несделанную.

    Служебные отметки самой петли (старт, потеря, возврат) отсеиваются: они говорят о
    жизни ярлыка, а не о работе.
    """
    rows = _events(task_id, limit=40)
    keep = [row for row in rows if str(row.get("kind") or "")
            not in ("task_lost", "task_started", "task_reopened")]
    if not keep:
        return ""
    bits = []
    for row in keep[-max(1, int(limit)):]:
        kind = str(row.get("kind") or "?")
        text = " ".join(str(row.get("summary") or row.get("result") or "").split())
        bits.append(f"{kind}: {text[:110]}" if text else kind)
    return "Последнее наблюдаемое в журнале задачи — " + "; ".join(bits) + "."


def _root_state_line(task: dict) -> str:
    """Цел ли корень задачи. «Продолжить тем же id» стоит разной цены, когда рабочее
    дерево на месте и когда его снесли, — и знать это надо ДО того, как продолжать."""
    root_raw = str(task.get("root") or "").strip()
    if not root_raw:
        return "Корень не записан."
    try:
        alive = Path(root_raw).is_dir()
    except OSError:
        alive = False
    if alive:
        return f"Корень {root_raw} на месте."
    return (f"⚠ Корня {root_raw} на диске БОЛЬШЕ НЕТ: рабочее дерево не сохранилось, "
            f"продолжение по этому id начнётся с пустого места.")


def reconcile_submitted_tasks() -> int:
    """Сданная задача узнаёт судьбу своего предложения. -> сколько закрыто.

    ⚠ 01.08. `code-32d32e70` восемь суток числилась `submitted`, а её предложение
    `de64316c` Егор смёржил через двадцать минут после сдачи. Никто этого не заметил:
    жнец брошенных обходит только `active`, а `state_line()` считает `submitted`
    активной работой — то есть её блок состояния восемь суток сообщал ей о кодинг-задаче,
    которой давно нет, и на её же вопрос «что у меня в работе» отвечал неправдой.

    Это тот же класс, что её «подготовлено вспоминается как доставлено», только с другой
    стороны: сделанное и ПРИНЯТОЕ помнится как висящее. Лечение то же самое — статус
    выводится из наблюдаемого реестра, а не из момента передачи из рук в руки.

    Пробуждения здесь НЕТ намеренно. Жнец брошенных будит, потому что её намерение могло
    пропасть молча; тут пропасть нечему — про мёрж и про отказ `selfdev` уже написал ей в
    дневник в тот же момент. Догоняющая бухгалтерия не повод отнимать у неё ход.

    Пока решения по предложению нет — не трогаем ничего: `submitted` тогда правда.
    """
    if not TASKS_DIR.is_dir():
        return 0
    settled = 0
    for path in TASKS_DIR.glob("*/task.json"):
        try:
            task = _read_json(path)
            if not isinstance(task, dict) or str(task.get("status") or "") != "submitted":
                continue
            task_id = str(task.get("id") or path.parent.name)
            proposal_id = str(task.get("proposal_id") or "").strip()
            if not proposal_id:
                continue
            proposal = selfdev.get(proposal_id)
            if not isinstance(proposal, dict):
                # Реестр не знает предложения. Хоронить задачу по ОТСУТСТВИЮ записи
                # нельзя — это доказательство слабее, чем вердикт. Оставляем статус и
                # один раз записываем саму слепоту, чтобы она была видима, а не тиха.
                if not task.get("submission_unknown_since"):
                    task["submission_unknown_since"] = _now()
                    _save_task(task)
                    _event(task_id, "task_submission_unknown", proposal=proposal_id,
                           summary=f"предложения {proposal_id} нет в реестре — "
                                   f"судьбу сдачи подтвердить нечем, статус не трогаю")
                continue
            verdict = str(proposal.get("status") or "")
            if verdict not in ("merged", "rejected"):
                continue
            decided_by = str(proposal.get("decided_by") or "").strip()
            who = f" ({decided_by})" if decided_by else ""
            if verdict == "merged":
                result = (f"Предложение {proposal_id} смёржено{who} — работа задачи "
                          f"лежит в дереве.")
            else:
                reason = str(proposal.get("reason") or "").strip()
                result = (f"Предложение {proposal_id} отклонено{who}"
                          + (f": {reason[:200]}" if reason else "")
                          + " — работа задачи в дерево не легла.")
            task["status"] = "done"
            task["finished"] = _now()
            task["submission_status"] = verdict
            task["submission_result"] = result
            _save_task(task)
            _event(task_id, "task_submission_settled", proposal=proposal_id,
                   proposal_status=verdict, summary=result)
            settled += 1
            log.info("forge: сданная задача %s закрыта по вердикту предложения %s (%s)",
                     task_id, proposal_id, verdict)
        except Exception:
            log.debug("сверка сдачи: задача %s пропущена", path, exc_info=True)
    return settled


def _task_last_trace(task_id: str, task: dict) -> float:
    """Самый свежий МЕСТНЫЙ след жизни задачи (события, task.json, каталоги юнитов).
    Удалённых операций отсюда не видно — это честно сказано в тексте пробуждения."""
    stamps = [_wake_parse_iso(str(task.get("created") or "")),
              _wake_parse_iso(str(task.get("updated") or ""))]
    root = _task_dir(task_id)
    for name in ("events.jsonl", "task.json"):
        try:
            stamps.append((root / name).stat().st_mtime)
        except OSError:
            pass
    for kind in ("agents", "processes", "verifications"):
        base = root / kind
        if not base.is_dir():
            continue
        try:
            for unit in base.iterdir():
                try:
                    stamps.append(unit.stat().st_mtime)
                except OSError:
                    pass
        except OSError:
            pass
    return max(stamps or [0.0])


def reconcile_lost_tasks() -> int:
    """active-задача без единого местного следа жизни дольше порога → lost + ПРОБУЖДЕНИЕ.

    ⚠ 26.07 18:56:19 она открыла hcode-c584ba6e по ПРЯМОЙ просьбе Егора (свежая ссылка
    на свадебный сайт) и бросила через 34 секунды: пять строк событий, ноль агентов, ни
    преемника, ни следа в её ходах. Нашлась она только назавтра, пятой в ряду таких же —
    19.07, 21.07, 22.07, 24.07. Потерять её намерение молча хуже, чем выполнить поздно.
    Поэтому здесь НЕ уборка: ничего не удаляется, не закрывается и не отменяется —
    задача получает честный ярлык, будит её один раз (контур subagent_result) и
    возвращается в active первым же её действием (_revive_if_lost). -> сколько разбужено.
    """
    if not TASKS_DIR.is_dir():
        return 0
    if TASK_ABANDONED_SEC <= 0:
        # Явный выключатель (PRAXIS_FORGE_TASK_ABANDONED_H=0), как у
        # PRAXIS_FORGE_AGENT_OVERDUE_MIN. Молча — нельзя: это выключенное пробуждение.
        log.info("forge-жнец брошенных задач ВЫКЛЮЧЕН (PRAXIS_FORGE_TASK_ABANDONED_H=0)")
        return 0
    try:
        from core import events as core_events
        from core import subagents as core_subagents
    except Exception:
        return 0
    now = time.time()
    woken = 0
    for path in TASKS_DIR.glob("*/task.json"):
        try:
            task = _read_json(path)
            if not isinstance(task, dict) or str(task.get("status") or "") != "active":
                continue
            task_id = str(task.get("id") or path.parent.name)
            if not _wake_parse_iso(str(task.get("created") or "")):
                continue        # возраста не знаем — молчим, а не выдумываем смерть
            quiet_for = now - _task_last_trace(task_id, task)
            if quiet_for < TASK_ABANDONED_SEC:
                continue
            live = [row for kind in ("agents", "processes", "verifications")
                    for row in _units(task_id, kind)
                    if row.get("status") in _LIVE_UNIT_STATES]
            if live:
                continue
            scope = str(task.get("scope") or "self")
            remote_blind = ("" if scope == "self" else
                            f" ⚠ scope={scope}: удалённые операции отсюда не видны — если на "
                            "той стороне что-то ещё крутится, спроси coding_process(list), "
                            "прежде чем считать задачу мёртвой.")
            trace_line = _last_trace_line(task_id)
            recap = (
                f"Задача открыта {task.get('created')} и {quiet_for / 3600:.1f}ч не подаёт "
                f"признаков: ни живых процессов, ни новых событий. Цель: "
                f"«{str(task.get('goal') or '')[:200]}». {_root_state_line(task)} "
                + (trace_line + " " if trace_line else "") +
                f"Я ничего не закрыла и не отменила — статус lost это ярлык «потеряна из "
                f"виду» по порогу тишины {TASK_ABANDONED_SEC / 3600:.0f}ч, не приговор. "
                f"Продолжить — теми же тулами по тому же id (первое действие вернёт "
                f"active), закрыть — coding_session(finish), отпустить — просто оставить "
                f"как есть." + remote_blind)
            # ⚠ 27.07, находка ревью. Ключ был `forge:<id>:task` — один на задачу
            # НАВСЕГДА. Юнит завершается ровно раз, и для него это верно; задача же
            # теряется многократно: потерялась → разбудила → она сделала ход (задача
            # снова active) → бросила опять. Второе событие ложилось в журнал с тем же
            # ключом, `core/events.undelivered` пропускало его как доставленное (ключ
            # живёт в delivered, пока журнал не сожмётся, — на проде недели), а лог
            # рапортовал «ярлык lost + пробуждение». То есть ровно тот класс, ради
            # которого жнец и написан: её намерение пропадает молча, и мы при этом
            # утверждаем обратное. Ключ поколенческий: своя потеря — своё пробуждение.
            lost_count = int(task.get("lost_count") or 0) + 1
            payload = core_subagents.normalize(
                task_id, "task",
                {"status": "lost", "role": "task", "finished": _now(), "result": recap,
                 "error": f"нет следов жизни {quiet_for / 3600:.1f}ч"},
                request={"created": str(task.get("created") or "")},
                task=task, unit_dir=str(_task_dir(task_id)))
            core_events.emit("subagent_result", "forge", payload=payload,
                             dedup_key=core_subagents.event_key(task_id, "task",
                                                                suffix=f"lost{lost_count}"))
            _event(task_id, "task_lost", quiet_hours=round(quiet_for / 3600, 2),
                   lost_count=lost_count,
                   summary=f"нет следов жизни {quiet_for / 3600:.1f}ч — потеряна из виду, "
                           f"не закрыта")
            task["status"] = "lost"
            task["lost_reason"] = recap
            task["lost_count"] = lost_count
            _save_task(task)
            woken += 1
            log.info("forge-жнец: задача %s без следов %.1fч — ярлык lost + пробуждение "
                     "(потеря №%d)", task_id, quiet_for / 3600, lost_count)
        except Exception:
            log.debug("жнец: задача %s пропущена", path, exc_info=True)
    return woken


# ─── Глаз на чужой репозиторий ────────────────────────────────────────────────
# ⚠ 28.07. 26.07 она аудитировала замороженный SHA 107fca05 репозитория Арета, а 27.07
# он выложил поверх ed288276 — дословно два из трёх её требований. Она об этом НЕ ЗНАЛА
# и ждала, пока скажут словами: окна в чужой репозиторий у неё не было вовсе, каждый
# взгляд на чужую работу — разовый клон во временный каталог по случаю.
#
# Здесь появляется сам взгляд. Наблюдение ставится за репозиторием, который она НАЗВАЛА
# САМА в цели coding-задачи (её слова — не гадание о том, что ей интересно), спрашивает
# один только HEAD (`git ls-remote`, без клона, без записи, без кредов) и живёт дольше
# задачи: в живом случае сдвиг пришёл назавтра после finish, и наблюдение, умирающее
# вместе с задачей, не показало бы ровно того, ради чего затевалось.
#
# Сдвиг HEAD — ФАКТ, а не приказ действовать: ничего не качается, не мержится, не
# решается за неё. Своего будильника здесь нет — факт едет её же `kind=wake`.
_UPSTREAMS = STATE_DIR / "upstreams.json"
# Снятые наблюдения (ссылка оказалась не репозиторием). Держим отдельно ровно затем,
# чтобы обход не поднимал их из СТАРЫХ задач по кругу — и чтобы это не превратилось в
# запрет: если она назовёт тот же адрес в НОВОЙ задаче, наблюдение встаёт снова, а
# запись о снятии стирается. Отказ помнить — не то же самое, что отказ слушать.
_UPSTREAMS_RETIRED = STATE_DIR / "upstreams_retired.json"
_UPSTREAM_STATE = {"ts": 0.0}
# Как часто вообще спрашивать remote. Такт РЕДКИЙ намеренно: у неё уже есть часы и
# пробуждения, а чужой push не становится правдивее оттого, что о нём узнали на 20
# минут раньше. 0 = наблюдение выключено целиком (и об этом громко в лог).
UPSTREAM_CHECK_SEC = _env_sec("PRAXIS_FORGE_UPSTREAM_MIN", 30.0, scale=60.0)
# Сколько ждать ответа remote. 0 = без предела (дом читает 0 как «не применять», см. _env_sec).
UPSTREAM_PROBE_SEC = _env_sec("PRAXIS_FORGE_UPSTREAM_PROBE_SEC", 20.0)
# Сколько один обход вправе занимать целиком. Двенадцать молчащих remote по 20с — это
# четыре минуты занятого потока реконсайлера, то есть задержка контура её субагентов.
# За бюджетом обход не бросает наблюдения, а откладывает их на следующий такт, и первыми
# идут самые давно не спрошенные — иначе хвост списка не проверялся бы никогда.
UPSTREAM_PASS_SEC = _env_sec("PRAXIS_FORGE_UPSTREAM_PASS_SEC", 60.0)
# Сколько наблюдение может быть СЛЕПЫМ, прежде чем сказать ей об этом вслух. «Я не могу
# спросить» и «ничего не менялось» — разные факты, и молчать вторым о первом значит врать.
UPSTREAM_BLIND_SEC = _env_sec("PRAXIS_FORGE_UPSTREAM_BLIND_H", 24.0, scale=3600.0)
# Потолок числа наблюдений (вытесняется самое давно не тронутое). Не запрет: столько
# сетевых вопросов раз в такт, а вытеснение названо вслух в ответе на старте задачи.
UPSTREAM_KEEP = int(_env_sec("PRAXIS_FORGE_UPSTREAM_KEEP", 12.0))
# Сколько раз подряд ссылке дозволено НЕ ответить как git-репозиторий, прежде чем
# наблюдение снимется. Нужно ровно затем, чтобы обычная ссылка в цели (26.07 в цели
# стоял свадебный сайт) не осела вечным слепым наблюдением; базлайна у такой ссылки
# не было ни разу, снятие ничего не теряет, и оно записывается в след задачи.
_UPSTREAM_ARM_TRIES = 3

_GIT_URL_RE = re.compile(
    # `(?:[^/@\s]+@)?` — не «поддержка кредов», а ровно наоборот: ссылку с логином и
    # токеном надо СНАЧАЛА узнать, чтобы вырезать из неё эту часть (иначе она просто
    # не разбиралась бы как git-адрес и токен уехал бы дальше в целости).
    r"(?:https?://(?:[^/@\s]+@)?|ssh://[^/@\s]+@|git@)[\w.\-]+(?::\d+)?[/:][\w.\-~]+"
    r"(?:/[\w.\-~]+)+",
    re.IGNORECASE)
_FULL_SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")
# `(?!git@)` — «git@» это не кред, а обычай ssh-адреса (ключ, не пароль): срезать его
# значило бы ломать рабочий адрес ради вида безопасности.
_USERINFO_RE = re.compile(r"(?<=://)(?!git@)[^/@\s]+@")


def _mask_userinfo(text: str) -> str:
    """Ни логин, ни токен из ссылки не имеют права осесть в реестре, логе или её кадре.
    Единственный твёрдый рельс этого дома — креды не текут; ссылка в цели их несёт легко."""
    return _USERINFO_RE.sub("", str(text or ""))


def _upstream_urls(text: str) -> list[tuple[str, str]]:
    """(url без userinfo, ключ host/owner/repo) для каждой git-ссылки в тексте.

    Хвостовая пунктуация режется намеренно: в живой цели 26.07 ссылка кончалась точкой
    («…/papertrade-lab.»), и без этого наблюдение встало бы за несуществующий репозиторий."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in _GIT_URL_RE.findall(str(text or "")):
        url = _mask_userinfo(str(raw).strip().rstrip(".,;:!?)]}>\"'»"))
        if url.lower().endswith(".git"):
            url = url[:-4]
        scp = "://" not in url          # git@host:owner/repo — двоеточие тут разделитель
        # userinfo уже срезан маской выше, поэтому здесь снимается только схема
        body = re.sub(r"^(?:[a-z][a-z0-9+.\-]*://|git@)", "", url, flags=re.IGNORECASE)
        if scp:
            body = body.replace(":", "/", 1)
        else:
            # …а в https порт остаётся портом: host:8443/owner/repo — это три части,
            # а не четыре, иначе ключ наблюдения указывал бы на несуществующий адрес.
            body = re.sub(r"^([^/]+?):\d+", r"\1", body)
        parts = [p for p in body.split("/") if p]
        if len(parts) < 3:      # host + владелец + имя: меньше — это не адрес репозитория
            continue
        key = "/".join(parts[:3]).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((url, key))
    return out


def _clip_seen(text: str, limit: int) -> str:
    """Усечение, которое ВИДНО. Молчаливый срез читается как «тут текст и кончился» —
    в кадре пробуждения её же цель обрывалась на «Дать два независимых» без единого знака."""
    value = str(text or "")
    return value if len(value) <= limit else (
        value[:limit].rstrip() + f"…[обрезано, ещё {len(value) - limit} симв.]")


# Реестр наблюдений трогают два потока: её ход (открыла задачу с репозиторием) и обход
# реконсайлера, который держит словарь в памяти ДЕСЯТКИ СЕКУНД, пока ходит в сеть. Без
# этого замка сценарий был живой: она открывает задачу, ей честно печатают «наблюдение
# поставлено», через секунду обход дописывает свой снимок и затирает запись — наблюдения
# нет, и об этом ей никто не скажет. Замок короткий (только диск); обход при записи не
# кладёт свой снимок целиком, а сливает по ключам (см. `_merge_upstreams`).
_UPSTREAMS_LOCK = threading.RLock()


def _load_upstreams() -> dict:
    with _UPSTREAMS_LOCK:
        data = _read_json(_UPSTREAMS, {})
    return data if isinstance(data, dict) else {}


def _save_upstreams(data: dict) -> None:
    with _UPSTREAMS_LOCK:
        try:
            _atomic_json(_UPSTREAMS, data)
        except Exception:
            log.debug("реестр наблюдений не записался", exc_info=True)


def _merge_upstreams(rows: dict, keys, dropped=()) -> None:
    """Записать поверх СВЕЖЕГО файла только те ключи, которые обход вправду трогал.

    Снимок обхода устарел уже к моменту записи: пока он спрашивал сеть, она могла поставить
    новое наблюдение своей рукой. Её жест старше нашего снимка — и он не имеет права
    исчезнуть из-за того, что мы дольше ходили."""
    keys = set(keys or ())
    dropped = set(dropped or ())
    with _UPSTREAMS_LOCK:
        live = _read_json(_UPSTREAMS, {})
        if not isinstance(live, dict):
            live = {}
        for key in keys:
            if key in rows:
                live[key] = rows[key]
        for key in dropped:
            live.pop(key, None)
        try:
            _atomic_json(_UPSTREAMS, live)
        except Exception:
            log.debug("реестр наблюдений не записался", exc_info=True)


def _load_retired() -> dict:
    data = _read_json(_UPSTREAMS_RETIRED, {})
    return data if isinstance(data, dict) else {}


def _arm_upstreams(task: dict, respect_retired: bool = False) -> int:
    """Поставить наблюдение за репозиториями, названными в цели задачи. -> сколько новых.

    Базлайн («что она видела») берётся из её же слов: если цель называет ровно один
    репозиторий и ровно один полный 40-символьный SHA — это он. В живом случае цель
    говорила «ровно frozen SHA 107fca05…», и только такой базлайн даёт правду «ты
    смотрела X, а сейчас там Y». Короткие SHA не берём: они неоднозначны, а выдать
    догадку за то, что она видела, — та же ложь, только вежливая.
    """
    if UPSTREAM_KEEP <= 0 or UPSTREAM_CHECK_SEC <= 0:
        return 0
    if not isinstance(task, dict):
        return 0
    text = f"{task.get('goal') or ''}\n{task.get('target') or ''}"
    found = _upstream_urls(text)
    if not found:
        return 0
    shas = _FULL_SHA_RE.findall(text.lower())
    baseline = shas[0] if (len(found) == 1 and len(shas) == 1) else ""
    task_id = str(task.get("id") or "")
    # Давность наблюдения меряем ЗАДАЧЕЙ, а не моментом обхода: иначе каждый обход
    # обновлял бы всем одинаковое «сейчас», и потолок вытеснял бы случайное.
    stamp = _wake_parse_iso(str(task.get("updated") or "")) or time.time()
    added = 0
    # Чтение-правка-запись целиком под замком: параллельный обход держит свой снимок
    # десятки секунд, и без этого её только что поставленное наблюдение затиралось.
    with _UPSTREAMS_LOCK:
        data = _load_upstreams()
        retired = _load_retired()
        for url, key in found:
            if key in retired:
                if respect_retired:
                    continue    # обход не поднимает снятое из старых задач по кругу
                # …но её новый жест сильнее прошлого снятия: она назвала этот адрес снова.
                retired.pop(key, None)
                _atomic_json(_UPSTREAMS_RETIRED, retired)
                log.info("forge-наблюдение за %s возвращено: названо в задаче %s", url,
                         task.get("id"))
            row = data.get(key)
            if isinstance(row, dict):
                ids = [x for x in (row.get("tasks") or []) if isinstance(x, str)]
                if task_id and task_id not in ids:
                    ids.append(task_id)
                row["tasks"] = ids[-8:]
                row["touched_at"] = max(float(row.get("touched_at") or 0.0), stamp)
                continue
            data[key] = {
                "url": url, "key": key,
                # known_head — последний SHA, о котором она ЗНАЕТ: увиденный ею самой либо
                # тот, о котором ей уже сказали. Пустой = базлайна ещё нет, и заявлять
                # сдвиг не от чего (первая удачная проверка просто запишет точку отсчёта).
                "known_head": baseline, "known_source": "цель задачи" if baseline else "",
                "head": "", "tasks": [task_id] if task_id else [],
                # Цель копируется в кадр пробуждения, поэтому маска нужна и здесь: адрес
                # мы почистили, а та же ссылка целиком лежала в тексте цели рядом.
                "goal": _clip_seen(_mask_userinfo(str(task.get("goal") or "")), 200),
                "armed_at": _now(), "touched_at": stamp, "checked_at": "",
                "fail_streak": 0, "error": "", "blind_since": "", "blind_told": False,
                "moves": 0, "last_move_at": "",
            }
            added += 1
        if added:
            _evict_upstreams(data, task_id=task_id)
        _save_upstreams(data)
    return added


def _evict_upstreams(data: dict, task_id: str = "") -> list[str]:
    """Вытеснить самые давно не тронутые сверх потолка. -> что снято (называется вслух).

    ⚠ Снятие писалось только в контейнерный лог, куда она не смотрит: после вытеснения
    `_upstream_note` для той задачи возвращал ПУСТО, и отличить «наблюдаю, сдвигов нет»
    от «за этим репозиторием я больше не слежу» было нечем. Закон 2 наизнанку: предел
    применён и назван оператору, а не ей. Теперь снятие идёт следом в задачу — туда, где
    она читает про эту работу."""
    if len(data) <= UPSTREAM_KEEP:
        return []
    order = sorted(data, key=lambda k: float((data.get(k) or {}).get("touched_at") or 0.0))
    dropped = order[:len(data) - UPSTREAM_KEEP]
    for key in dropped:
        row = data.pop(key, None) or {}
        for tid in {*(row.get("tasks") or []), *( [task_id] if task_id else [] )}:
            _upstream_trail(str(tid), "upstream_evicted",
                            f"наблюдение за {row.get('url') or key} снято потолком "
                            f"PRAXIS_FORGE_UPSTREAM_KEEP={UPSTREAM_KEEP} (вытесняется самое "
                            f"давно не тронутое) — назови его снова "
                            f"(coding_inspect action=watch), и оно вернётся")
    log.info("forge-наблюдения: потолок %d (PRAXIS_FORGE_UPSTREAM_KEEP) — сняты самые "
             "давние: %s", UPSTREAM_KEEP, ", ".join(dropped))
    return dropped


def _upstream_note(task: dict) -> str:
    """Строка про наблюдения ЭТОЙ задачи — в ориентировку и в статус. Чистое чтение."""
    if not isinstance(task, dict):
        return ""
    task_id = str(task.get("id") or "")
    rows = [r for r in _load_upstreams().values()
            if isinstance(r, dict) and task_id and task_id in (r.get("tasks") or [])]
    if not rows:
        return ""
    lines = []
    for row in rows:
        known = str(row.get("known_head") or "")
        head = str(row.get("head") or "")
        if not known:
            where = "точки отсчёта ещё нет — запишу первым же удачным вопросом"
        elif head and head != known:
            where = (f"ты знаешь {known[:8]}, сейчас там {head[:8]} — сдвинулся "
                     f"{row.get('last_move_at') or 'недавно'}")
        else:
            # Откуда взялась точка отсчёта — часть правды: «ты это видела» и «я тебе про
            # это сказала» разные вещи, и через неделю их уже не различить по памяти.
            src = str(row.get("known_source") or "")
            where = (f"на {known[:8]}" + (f" ({src}" if src else " (")
                     + (f"; проверено {row.get('checked_at')}" if row.get("checked_at")
                        else "") + ")").replace(" ()", "")
        blind = (f"; ⚠ не могу спросить с {row.get('blind_since')}: {row.get('error')}"
                 if row.get("blind_since") else "")
        lines.append(f"- {row.get('url')}: {where}{blind}")
    return ("\nнаблюдение за upstream (только HEAD, без клона; сдвиг — факт, не задача; "
            f"спрашиваю не чаще раза в {UPSTREAM_CHECK_SEC / 60:.0f} мин, наблюдений держу "
            f"до {UPSTREAM_KEEP}):\n" + "\n".join(lines))


def _ls_remote_head(url: str) -> tuple[str, str]:
    """HEAD чужого репозитория одним сетевым вопросом, без клона. -> (sha, ошибка).

    Кредов не подставляем и подставить не даём: приватный репозиторий обязан отказать
    сразу, а не повиснуть на приглашении ввести пароль (и не утащить чужой токен из
    окружения в чужую сеть)."""
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "GCM_INTERACTIVE": "never",
        "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oStrictHostKeyChecking=accept-new",
    })
    try:
        r = subprocess.run(["git", "ls-remote", "--quiet", str(url), "HEAD"],
                           capture_output=True, text=True, errors="replace",
                           timeout=(UPSTREAM_PROBE_SEC or None), env=env)
    except subprocess.TimeoutExpired:
        return "", f"remote не ответил за {UPSTREAM_PROBE_SEC:.0f}с"
    except Exception as exc:
        return "", f"{type(exc).__name__}: {exc}"
    if r.returncode != 0:
        tail = ((r.stderr or "") + (r.stdout or "")).strip().splitlines()
        return "", _mask_userinfo(tail[0] if tail else "git ls-remote промолчал")[:200]
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "HEAD" and re.fullmatch(r"[0-9a-f]{40}", parts[0]):
            return parts[0], ""
    return "", "в ответе remote нет HEAD"


def _arm_upstreams_from_tasks() -> int:
    """Догнать наблюдениями уже открытые задачи (свежие — первыми). -> сколько новых.

    Без этого на проде глаз открылся бы только со СЛЕДУЮЩЕЙ задачи, а живой случай —
    ровно в уже закрытой: hcode-f12f3f11, аудит репозитория Арета от 26.07."""
    if not TASKS_DIR.is_dir():
        return 0
    rows = []
    for path in TASKS_DIR.glob("*/task.json"):
        task = _read_json(path)
        if isinstance(task, dict):
            rows.append(task)
    # Свежие задачи первыми: если репозиториев больше потолка, под наблюдением остаётся
    # то, чем она занималась позже, а не то, что случайно оказалось первым в glob.
    rows.sort(key=lambda t: str(t.get("updated") or ""), reverse=True)
    added = 0
    for task in rows:
        added += _arm_upstreams(task, respect_retired=True)
        if len(_load_upstreams()) >= UPSTREAM_KEEP:
            break
    return added


def _upstream_wake_text(moved: list[dict], blind: list[dict]) -> str:
    """Кадр факта. Ни одного глагола действия: увидела ≠ пошла проверять."""
    head = ("Это пробуждение поставил форж по стоячему наблюдению, а не отдельной твоей "
            "просьбой: репозиторий ты назвала сама в цели coding-задачи.\n"
            "Что произошло — ФАКТ, не задача: я ничего не склонировала, не скачала, "
            "не смержила и ни о чём не договорилась. Что с этим делать — решаешь ты, "
            "и «ничего» тоже решение.")
    lines = []
    for row in moved:
        known = str(row.get("known_head") or "")
        known_src = str(row.get("known_source") or "")
        lines.append(
            f"• {row.get('url')}: было {known[:8] or '—'}"
            + (f" ({known_src})" if known_src else "")
            + f", сейчас HEAD {str(row.get('head') or '')[:8]} — замечено {row.get('checked_at')}.")
        goal = " ".join(str(row.get("goal") or "").split())
        if goal:
            lines.append(f"   ты называла его в задаче {', '.join(row.get('tasks') or []) or '—'}: "
                         f"«{_clip_seen(goal, 160)}»")
        lines.append("   что там за коммиты — отсюда не видно: я спрашивала только HEAD. "
                     "Посмотреть — твоими руками (clone/fetch в coding-задаче), я не ходила.")
    for row in blind:
        lines.append(
            f"• ⚠ {row.get('url')}: я ОСЛЕПЛА с {row.get('blind_since')} — "
            f"{row.get('error')}. Это не «ничего не менялось», это «не могу спросить»: "
            f"последнее, что знаю, — {str(row.get('known_head') or '—')[:8]}.")
    tail = (f"[наблюдение: спрашиваю только HEAD не чаще раза в "
            f"{UPSTREAM_CHECK_SEC / 60:.0f} мин (PRAXIS_FORGE_UPSTREAM_MIN), ответа жду "
            f"{UPSTREAM_PROBE_SEC:.0f}с, наблюдений держу до {UPSTREAM_KEEP} "
            f"(PRAXIS_FORGE_UPSTREAM_KEEP, вытесняются самые давние), на один обход трачу "
            f"не больше {UPSTREAM_PASS_SEC:.0f}с и остаток спрашиваю следующим тактом, "
            f"про слепоту говорю через {UPSTREAM_BLIND_SEC / 3600:.0f}ч]")
    return "\n".join([head, *lines, tail])


def check_upstreams(force: bool = False) -> int:
    """Один обход наблюдений: спросить HEAD, сдвиг — фактом в её пробуждение.
    -> сколько фактов отдано (сдвиги + впервые названная слепота).

    Никакого своего будильника: факт едет её же `kind=wake` (`tasks.add`), тем самым,
    которым она будит себя сама. Пока пробуждение не поставлено, точка отсчёта НЕ
    двигается — упавший ход не имеет права съесть сдвиг молча."""
    if UPSTREAM_CHECK_SEC <= 0:
        log.info("forge: наблюдение за чужими репозиториями ВЫКЛЮЧЕНО "
                 "(PRAXIS_FORGE_UPSTREAM_MIN=0) — сдвиг HEAD она не увидит")
        return 0
    if UPSTREAM_KEEP <= 0:
        log.info("forge: наблюдений держать не велено (PRAXIS_FORGE_UPSTREAM_KEEP=0) — "
                 "глаз на чужой репозиторий выключен")
        return 0
    now = time.time()
    if not force and now - _UPSTREAM_STATE["ts"] < UPSTREAM_CHECK_SEC:
        return 0
    _UPSTREAM_STATE["ts"] = now
    _arm_upstreams_from_tasks()
    data = _load_upstreams()
    if not data:
        return 0
    moved: list[dict] = []
    blind: list[dict] = []
    dropped: list[tuple[str, dict]] = []
    # Самые давно не спрошенные — первыми: бюджет обхода не должен превращаться в
    # «хвост списка не проверяется никогда».
    order = sorted(data, key=lambda k: str((data.get(k) or {}).get("checked_at") or ""))
    # Бюджет считается от начала ОПРОСА, а не всего обхода: постановка наблюдений — это
    # чтение диска, и съеденный ею бюджет означал бы «глаза нет вовсе». По той же причине
    # первый вопрос задаётся всегда: бюджет откладывает хвост, а не отменяет наблюдение.
    probe_start = time.time()
    probed = deferred = 0
    # Какие ключи обход вправду трогал: писать обратно будем ТОЛЬКО их, а не весь
    # устаревший снимок (её наблюдение, поставленное во время обхода, должно выжить).
    touched: set[str] = set()
    for key in order:
        row = data.get(key)
        if not isinstance(row, dict):
            touched.add(key)
            data.pop(key, None)
            continue
        # Бюджет учитывает ещё не заданный вопрос: он проверялся ПЕРЕД пробой, а проба
        # ждёт ответа до UPSTREAM_PROBE_SEC — обход честно назывался «не больше 60с» и
        # занимал поток реконсайлера до 80. Названный предел обязан быть правдой.
        if (probed and UPSTREAM_PASS_SEC > 0
                and time.time() - probe_start + max(0.0, UPSTREAM_PROBE_SEC)
                >= UPSTREAM_PASS_SEC):
            deferred += 1
            continue
        probed += 1
        touched.add(key)
        sha, err = _ls_remote_head(str(row.get("url") or ""))
        row["checked_at"] = _now()
        if not sha:
            row["fail_streak"] = int(row.get("fail_streak") or 0) + 1
            row["error"] = err
            if not row.get("known_head") and row["fail_streak"] >= _UPSTREAM_ARM_TRIES:
                # Точки отсчёта не было ни разу: это не «репозиторий пропал», а «ссылка
                # не была репозиторием». Снимаем и говорим об этом в след задачи.
                dropped.append((key, row))
                data.pop(key, None)
                continue
            if not row.get("blind_since"):
                row["blind_since"] = _now()
            if (row.get("known_head") and not row.get("blind_told")
                    and UPSTREAM_BLIND_SEC > 0
                    and now - _wake_parse_iso(row["blind_since"]) >= UPSTREAM_BLIND_SEC):
                blind.append(dict(row))
            continue
        row["fail_streak"], row["error"] = 0, ""
        row["blind_since"], row["blind_told"] = "", False
        row["head"] = sha
        if not row.get("known_head"):
            # Первая удачная проверка — только точка отсчёта. Заявить сдвиг «из ниоткуда»
            # значило бы выдать собственное незнание за её новость.
            row["known_head"] = sha
            row["known_source"] = "первый вопрос remote, не твой взгляд"
            continue
        if sha != row["known_head"]:
            row["moves"] = int(row.get("moves") or 0) + 1
            row["last_move_at"] = row["checked_at"]
            moved.append(dict(row))
    if deferred:
        log.info("forge-наблюдения: обход занял бюджет %.0fс (PRAXIS_FORGE_UPSTREAM_PASS_SEC) "
                 "— %d наблюдений спрошу следующим тактом, они не забыты",
                 UPSTREAM_PASS_SEC, deferred)
    if dropped:
        retired = _load_retired()
        for key, row in dropped:
            retired[key] = {"url": row.get("url"), "at": _now(),
                            "reason": f"не ответил как git-репозиторий "
                                      f"{_UPSTREAM_ARM_TRIES} раза подряд: {row.get('error')}"}
        try:
            _atomic_json(_UPSTREAMS_RETIRED, retired)
        except Exception:
            log.debug("список снятых наблюдений не записался", exc_info=True)
    for key, row in dropped:
        log.info("forge-наблюдение снято: %s не ответил как git-репозиторий %d раза подряд "
                 "(%s)", row.get("url"), _UPSTREAM_ARM_TRIES, row.get("error"))
        for task_id in (row.get("tasks") or []):
            _upstream_trail(str(task_id), "upstream_dropped",
                            f"наблюдение за {row.get('url')} снято: не ответил как "
                            f"git-репозиторий {_UPSTREAM_ARM_TRIES} раза подряд "
                            f"({row.get('error')})")
    if not moved and not blind:
        _merge_upstreams(data, touched, {k for k, _ in dropped})
        return 0
    told = _hand_upstreams_to_wake(moved, blind)
    if told:
        # Точку отсчёта двигаем ТОЛЬКО после того, как пробуждение вправду поставлено.
        for row in moved:
            live = data.get(str(row.get("key") or ""))
            if isinstance(live, dict) and live.get("head"):
                live["known_head"] = live["head"]
                live["known_source"] = "я тебе сказала"
        for row in blind:
            live = data.get(str(row.get("key") or ""))
            if isinstance(live, dict):
                live["blind_told"] = True
        for row in moved:
            for task_id in (row.get("tasks") or []):
                _upstream_trail(str(task_id), "upstream_moved",
                                f"{row.get('url')}: {str(row.get('known_head') or '')[:8]} → "
                                f"{str(row.get('head') or '')[:8]} (факт, не задача)")
    _merge_upstreams(data, touched, {k for k, _ in dropped})
    return (len(moved) + len(blind)) if told else 0


def _upstream_trail(task_id: str, kind: str, summary: str) -> None:
    """След в журнале задачи — но не воскрешение её. `_event` снимает ярлык «потеряна из
    виду» первым же событием: для ЕЁ действия это правда, для чужого push — нет."""
    try:
        task = get(task_id)
        if not isinstance(task, dict) or task.get("status") == "lost":
            return
        _event(task_id, kind, summary=summary)
    except Exception:
        log.debug("след наблюдения по задаче %s не записался", task_id, exc_info=True)


def _hand_upstreams_to_wake(moved: list[dict], blind: list[dict]) -> bool:
    """Отдать факт её собственному будильнику. -> поставлено ли пробуждение."""
    if not moved and not blind:
        return False
    try:
        import tasks as _tasks
        _tasks.add("wake", _upstream_wake_text(moved, blind), when="in 0m", author="praxis")
    except Exception:
        log.warning("сдвиг upstream не удалось отдать пробуждению — точку отсчёта не "
                    "двигаю, повторю на следующем обходе", exc_info=True)
        return False
    log.info("forge-наблюдение: сдвигов %d, впервые названной слепоты %d → её kind=wake",
             len(moved), len(blind))
    return True


def upstream_line() -> str:
    """Коротко о наблюдениях — только когда есть НЕОТДАННОЕ (сдвиг или слепота).
    Постоянной строки нет намеренно: она сделала бы forge «активным» в её блоке
    состояния навсегда."""
    rows = [r for r in _load_upstreams().values() if isinstance(r, dict)]
    # Именно НЕОТДАННОЕ: сказанное однажды уходит отсюда, иначе одно слепое наблюдение
    # держало бы forge «активным» в её блоке состояния вечно. Сказанное живёт дальше в
    # `_upstream_note` — там, где она смотрит на саму задачу.
    # ⚠ Слепота попадала сюда ПЕРВЫМ же неудачным ответом, хотя кадр пробуждения честно
    # обещает «про слепоту говорю через 24ч»: один сетевой сбой на github вешал строку
    # «не могу спросить» в её блок состояния на целые сутки. Порог здесь тот же, что в
    # check_upstreams, — иначе обещание и поведение расходятся.
    now = time.time()

    def _blind_pending(row: dict) -> bool:
        since = str(row.get("blind_since") or "")
        if not since or row.get("blind_told") or UPSTREAM_BLIND_SEC <= 0:
            return False
        return (now - _wake_parse_iso(since)) >= UPSTREAM_BLIND_SEC

    pending = [r for r in rows
               if (r.get("head") and r.get("known_head") and r["head"] != r["known_head"])
               or _blind_pending(r)]
    if not pending:
        return ""
    parts = []
    for row in pending[:3]:
        if row.get("blind_since"):
            parts.append(f"{row.get('key')} — не могу спросить с {row.get('blind_since')}")
        else:
            parts.append(f"{row.get('key')} {str(row.get('known_head') or '')[:8]}→"
                         f"{str(row.get('head') or '')[:8]}")
    return f"upstream ({len(rows)} под наблюдением): " + "; ".join(parts)


def _upstream_row_line(row: dict) -> str:
    known = str(row.get("known_head") or "")
    head = str(row.get("head") or "")
    if not known:
        state = "точки отсчёта ещё нет — запишу первым же удачным вопросом"
    elif head and head != known:
        state = (f"знаешь {known[:8]}, сейчас там {head[:8]} — сдвинулся "
                 f"{row.get('last_move_at') or 'недавно'}")
    else:
        state = f"на {known[:8]} ({row.get('known_source') or 'источник не записан'})"
    blind = (f"; ⚠ не могу спросить с {row.get('blind_since')}: {row.get('error')}"
             if row.get("blind_since") else "")
    tasks = ", ".join(row.get("tasks") or []) or "—"
    return (f"- {row.get('url')}: {state}{blind}; названо в задачах {tasks}; "
            f"спрошено {row.get('checked_at') or 'ни разу'}")


def upstream_lever(action: str, task_id: str = "", arg: str = "") -> str:
    """Её рука на наблюдении: перечислить | поставить | СНЯТЬ. -> что сказать ей.

    ⚠ 28.07, находка ревью. Наблюдение вставало САМО за любой git-ссылкой в цели любой
    задачи, ретроспективно по всему архиву, ходило в сеть каждые полчаса и САМО ставило
    ей kind=wake — а рычага у неё не было ни одного: ни перечислить, ни снять. То есть
    «дать глаз» превратилось бы в «завести за неё будильник, который она не выключает».
    Снятое кладётся в тот же `upstreams_retired.json`, что и снятое по трём неудачам:
    иначе ближайший обход поднял бы наблюдение обратно из старой цели, и её решение
    молча откатилось бы. Названный ею адрес всегда сильнее прошлого снятия.
    """
    action = str(action or "").strip().lower()
    arg = str(arg or "").strip()
    if UPSTREAM_CHECK_SEC <= 0 or UPSTREAM_KEEP <= 0:
        return ("Глаз на чужие репозитории ВЫКЛЮЧЕН (PRAXIS_FORGE_UPSTREAM_MIN=0 или "
                "PRAXIS_FORGE_UPSTREAM_KEEP=0): ни поставить, ни снять наблюдение "
                "нечего — сдвиг чужого HEAD до меня не дойдёт вообще.")
    if action == "watching":
        rows = [r for r in _load_upstreams().values() if isinstance(r, dict)]
        retired = _load_retired()
        head = (f"наблюдений {len(rows)} из {UPSTREAM_KEEP} "
                f"(PRAXIS_FORGE_UPSTREAM_KEEP; сверх потолка вытесняется самое давно не "
                f"тронутое); спрашиваю только HEAD не чаще раза в "
                f"{UPSTREAM_CHECK_SEC / 60:.0f} мин, без клона и без кредов")
        body = [_upstream_row_line(r) for r in sorted(
            rows, key=lambda r: str(r.get("url") or ""))] or ["- пока ни одного"]
        tail = ([f"снятые (вернутся, если назову адрес снова): "
                 + ", ".join(f"{v.get('url') or k} — {v.get('reason') or 'снято'}"
                             for k, v in retired.items())] if retired else [])
        return "\n".join([head, *body, *tail])
    if action in ("watch", "unwatch"):
        found = _upstream_urls(arg)
        if not found:
            return (f"В «{arg or '—'}» я не вижу адреса git-репозитория. Нужен полный "
                    f"адрес вида https://host/владелец/репозиторий или git@host:владелец/"
                    f"репозиторий.")
        if action == "watch":
            task = get(task_id) if task_id else None
            # Базлайн берётся из её слов ровно так же, как из цели задачи: полный SHA
            # рядом с одним адресом — это «что она видела», короткий — догадка, а
            # догадку выдавать за её взгляд нельзя.
            pseudo = {"id": str(task_id or ""), "goal": arg,
                      "updated": (task or {}).get("updated") or _now()}
            added = _arm_upstreams(pseudo, respect_retired=False)
            urls = ", ".join(u for u, _ in found)
            return (f"Наблюдаю за {urls}"
                    + (f" (новых {added})" if added else " (уже наблюдала)")
                    + f". Спрашиваю только HEAD не чаще раза в "
                      f"{UPSTREAM_CHECK_SEC / 60:.0f} мин; сдвиг придёт мне пробуждением "
                      f"как ФАКТ, без клона и без следствий. Снимаю тем же тулом: "
                      f"action=unwatch.")
        gone, missing = [], []
        with _UPSTREAMS_LOCK:
            data = _load_upstreams()
            retired = _load_retired()
            for url, key in found:
                row = data.pop(key, None)
                if row is None:
                    missing.append(url)
                    continue
                retired[key] = {"url": row.get("url") or url, "at": _now(),
                                "reason": "сняла сама (coding_inspect action=unwatch)"}
                gone.append(url)
            if gone:
                _save_upstreams(data)
                try:
                    _atomic_json(_UPSTREAMS_RETIRED, retired)
                except Exception:
                    log.debug("список снятых наблюдений не записался", exc_info=True)
        for url, key in found:
            if url in gone:
                log.info("forge-наблюдение снято её рукой: %s", url)
        parts = []
        if gone:
            parts.append(f"Сняла наблюдение: {', '.join(gone)} — больше не спрашиваю и "
                         f"пробуждений по нему не будет. Назову этот адрес снова "
                         f"(action=watch или в цели новой задачи) — вернётся.")
        if missing:
            parts.append(f"Не наблюдала: {', '.join(missing)} — снимать нечего.")
        return " ".join(parts)
    return "action наблюдения: watching | watch | unwatch."


def task_origin(task_id: str) -> str:
    """Тред-заказчик задачи (conversation-id/селектор) или ''. Для наррации и
    forge_event: куда рассказывать про ЭТУ работу."""
    try:
        task = _read_json(_task_dir(str(task_id)) / "task.json", {}) or {}
        return str(task.get("origin_chat") or "")
    except Exception:
        return ""


def start(goal: str, target: str = "self", isolation: str = "auto",
          priority: str = "normal", origin_chat: str = "") -> str:
    """Open a durable coding task and return its factual orientation."""
    goal = str(goal or "").strip()
    if not goal:
        return "Нужна цель coding-задачи."
    source, label = _resolve_target(target)
    if source is None:
        return f"Не открыла coding-задачу: {label}"
    isolation = str(isolation or "auto").strip().lower()
    if isolation not in {"auto", "worktree", "direct"}:
        return "isolation: auto | worktree | direct"

    task_id = _id("code")
    root = source
    source_git = _git_root(source)
    source_branch = (_git_text(source_git, "branch", "--show-current").strip()
                     if source_git else "")
    proposal_id = ""
    branch = ""
    cleanup = "none"
    worktree_root = ""
    isolation_note = ""      # почему изоляция вышла не такой, как просили — её право знать
    is_self = source == REPO.resolve() and label == "self"
    if is_self and isolation != "direct":
        proposal = selfdev.begin(goal)
        if not proposal.get("ok"):
            return f"Не открыла coding-задачу: {proposal.get('msg') or 'proposal worktree не создался'}"
        proposal_id = str(proposal["id"])
        root = Path(proposal["path"]).resolve()
        branch = f"proposal/{proposal_id}"
        cleanup = "selfdev"
        worktree_root = str(root)
        isolation = "proposal-worktree"
    elif isolation == "worktree" or (isolation == "auto" and _git_root(source) is not None):
        git = _git_root(source)
        if git is not None:
            try:
                subdir = source.relative_to(git)
            except ValueError:
                subdir = Path(".")
            # Спрашиваем ДО чекаута: он на этом репозитории стоит 350 файлов и секунды,
            # а ответ известен заранее. Пост-проверка ниже остаётся — она ловит причины,
            # которых мы не предвидели.
            isolation_note = _worktree_would_miss(git, subdir)
            if isolation_note:
                isolation = "direct"
            else:
                wt = (BASE / "workspace" / ".forge-worktrees" / task_id).resolve()
                wt.parent.mkdir(parents=True, exist_ok=True)
                branch = f"forge/{task_id}"
                try:
                    r = _run(["git", "-C", str(git), "worktree", "add", "-b", branch,
                              str(wt), "HEAD"], timeout=60)
                except Exception as exc:
                    return f"worktree не создался: {type(exc).__name__}: {exc}"
                if r.returncode != 0:
                    return f"worktree не создался: {((r.stderr or r.stdout)[:500]).strip()}"
                candidate = (wt / subdir).resolve()
                if candidate.is_dir():
                    root, cleanup, isolation = candidate, "git-worktree", "git-worktree"
                    worktree_root = str(wt)
                else:
                    # Чекаут состоялся, а подкаталога всё равно нет. Причина не та, что
                    # мы проверили — но исход тот же, и задача с мёртвым корнем не нужна
                    # никому. Убираем за собой и работаем прямо.
                    _run(["git", "-C", str(git), "worktree", "remove", "--force", str(wt)],
                         timeout=60)
                    _run(["git", "-C", str(git), "branch", "-D", branch], timeout=30)
                    branch, isolation = "", "direct"
                    isolation_note = (
                        f"изоляция worktree не состоялась: чекаут HEAD создан, но «{subdir.as_posix()}» "
                        f"в нём не появился, а причину заранее опознать не удалось. Worktree убран, "
                        f"работаю прямо в источнике — правок в живом каталоге откатывать нечем.")
        else:
            isolation = "direct"
    else:
        isolation = "direct"

    git = _git_root(root)
    base_commit = _git_text(git, "rev-parse", "HEAD").splitlines()[0] if git else ""
    task = {
        "id": task_id, "goal": goal, "target": label, "source_root": str(source),
        "root": str(root), "isolation": isolation, "cleanup": cleanup,
        "proposal_id": proposal_id, "branch": branch, "base_commit": base_commit,
        "source_git": str(source_git) if source_git else "", "source_branch": source_branch,
        "worktree_root": worktree_root, "priority": _norm_priority(priority),
        # Молчаливой подмены изоляции не бывает: если попросили worktree, а вышло direct,
        # это стоит на задаче и попадает в ориентировку — читается до первой правки.
        "isolation_note": isolation_note,
        # PASS 30 Этап 2: тред-заказчик — для наррации по ходу и forge_event
        "origin_chat": str(origin_chat or ""),
        "status": "active", "created": _now(), "updated": _now(),
    }
    _save_task(task)
    _event(task_id, "task_started", target=label, root=str(root), isolation=isolation)
    _arm_upstreams(task)
    orientation = _orientation_text(task)
    (_task_dir(task_id) / "orientation.txt").write_text(orientation + "\n", encoding="utf-8")
    return orientation


def start_host(goal: str, target: str, priority: str = "normal",
               origin_chat: str = "") -> str:
    """Open a canonical Forge task whose execution backend is the root broker."""
    goal = str(goal or "").strip()
    target = str(target or "").strip()
    if not goal:
        return "Нужна цель coding-задачи."
    if not target or not Path(target).is_absolute():
        return "Для host-задачи нужен абсолютный путь хоста."
    if not serverd_client.available():
        return "Не открыла host-задачу: serverd broker не смонтирован."
    probe = serverd_client.workspace_inspect_result(target, "orientation")
    if not probe.get("ok"):
        return f"Не открыла host-задачу: [serverd] {probe.get('error')}"
    orientation = str(probe.get("text") or "")
    task_id = _id("hcode")
    task = {
        "id": task_id, "goal": goal, "target": target, "source_root": target,
        "root": target, "scope": "host", "backend": serverd_client.PROTOCOL,
        "isolation": "host-direct", "cleanup": "none", "proposal_id": "", "branch": "",
        "base_commit": str(probe.get("head") or ""), "source_git": str(probe.get("git_root") or ""),
        "source_branch": "", "worktree_root": "", "priority": _norm_priority(priority),
        "origin_chat": str(origin_chat or ""),
        "status": "active", "created": _now(), "updated": _now(),
    }
    _save_task(task)
    _event(task_id, "task_started", target=target, root=target, isolation="host-direct",
           backend=serverd_client.PROTOCOL)
    # Живой случай 26.07 был именно host-задачей: корень /tmp, а репозиторий Арета —
    # только в её словах. Наблюдение берётся из цели, а не из корня.
    _arm_upstreams(task)
    orientation += _upstream_note(task)
    (_task_dir(task_id) / "orientation.txt").write_text(orientation + "\n", encoding="utf-8")
    return f"coding-задача {task_id}: {goal}\n{orientation}"


def start_windows(goal: str, target: str, priority: str = "normal",
                  origin_chat: str = "") -> str:
    """Open a canonical Forge task whose execution backend is a Windows body."""
    from pathlib import PureWindowsPath

    goal = str(goal or "").strip()
    target = str(target or "").strip()
    if not goal:
        return "Нужна цель coding-задачи."
    if not target or not PureWindowsPath(target).is_absolute():
        return "Для Windows-задачи нужен абсолютный Windows-путь."
    if not body_client.available():
        return "Не открыла Windows-задачу: body controller не настроен."
    probe = body_client.workspace_inspect_result(target, "orientation")
    if not probe.get("ok"):
        return f"Не открыла Windows-задачу: [windows-body] {probe.get('error')}"
    orientation = str(probe.get("text") or "")
    task_id = _id("wcode")
    task = {
        "id": task_id, "goal": goal, "target": target, "source_root": target,
        "root": target, "scope": "windows", "device_id": body_client.device_id(),
        "backend": body_client.PROTOCOL, "isolation": "windows-direct", "cleanup": "none",
        "proposal_id": "", "branch": "", "base_commit": str(probe.get("head") or ""),
        "source_git": str(probe.get("git_root") or ""), "source_branch": "",
        "worktree_root": "", "priority": _norm_priority(priority),
        "origin_chat": str(origin_chat or ""),
        "status": "active", "created": _now(), "updated": _now(),
    }
    _save_task(task)
    _event(task_id, "task_started", target=target, root=target, isolation="windows-direct",
           backend=body_client.PROTOCOL, device_id=body_client.device_id())
    _arm_upstreams(task)
    orientation += _upstream_note(task)
    (_task_dir(task_id) / "orientation.txt").write_text(orientation + "\n", encoding="utf-8")
    return f"coding-задача {task_id}: {goal}\n{orientation}"


def _remote_client(task: dict):
    if task.get("scope") == "host":
        return serverd_client
    if task.get("scope") == "windows":
        return body_client
    return None


@contextmanager
def _remote_observations(task: dict):
    """Attach body calls to this Forge task without changing host/self backends."""
    if task.get("scope") == "windows":
        with body_client.observation_context(task):
            yield
        return
    yield


def list_tasks(limit: int = 12) -> str:
    rows = []
    if TASKS_DIR.is_dir():
        for path in TASKS_DIR.glob("*/task.json"):
            task = _read_json(path)
            if isinstance(task, dict):
                rows.append(task)
    rows.sort(key=lambda x: x.get("updated", ""), reverse=True)
    if not rows:
        return "Coding-задач пока нет."
    return "\n".join(
        f"{t.get('id')} [{t.get('status')}] {t.get('goal')} · {t.get('root')}"
        for t in rows[:max(1, min(int(limit or 12), 50))]
    )


def inspect(task_id: str, action: str = "status", path: str = "", query: str = "",
            glob: str = "**/*", start: int = 1, end: int = 0) -> str:
    # Рычаг наблюдения стоит ДО разбора задачи: перечислить и снять наблюдение она
    # вправе и тогда, когда задача, из чьей цели оно выросло, давно закрыта или
    # потеряна, — иначе «снять» работало бы ровно там, где оно уже не нужно.
    if str(action or "").strip().lower() in ("watch", "unwatch", "watching"):
        return upstream_lever(action, task_id=task_id, arg=query or path)
    task, root, err = _task_root(task_id)
    if err:
        return err
    assert task is not None and root is not None
    action = str(action or "status").strip().lower()
    remote = _remote_client(task)
    if action == "orientation":
        return _orientation_text(task)
    if action == "status":
        agents = _units(task_id, "agents")
        jobs = _units(task_id, "processes")
        checks = _units(task_id, "verifications")
        swarm_plan = forge_swarm.refresh(
            _task_dir(task_id),
            lambda aid: _unit_state(_unit_dir(task_id, "agents", aid))
            if _unit_dir(task_id, "agents", aid).is_dir() else {"status": "lost"},
        )
        git = _git_root(root) if remote is None else None
        if remote is not None:
            with _remote_observations(task):
                git_block = remote.workspace_inspect(str(root), "status")
        else:
            git_block = ((_git_text(git, "status", "--short", "--branch") or "чисто")
                         if git else "нет")
        return _cap("\n".join([
            f"{task_id} [{task.get('status')}] {task.get('goal')}",
            f"root: {root}; isolation: {task.get('isolation')}",
            f"agents: {_unit_counts(agents)}; processes: {_unit_counts(jobs)}; "
            f"verification: {_unit_counts(checks)}",
            (forge_swarm.summary(swarm_plan) if swarm_plan else "swarm: нет плана"),
            ((f"{task.get('scope')} backend:\n") if remote is not None else "git:\n") + git_block,
            "recent events:\n" + "\n".join(
                f"- {e.get('at')} {e.get('kind')}: {e.get('summary') or e.get('command') or e.get('path') or ''}"
                for e in _events(task_id, 12)
            ),
        ]) + _upstream_note(task))
    if action == "history":
        return json.dumps(_events(task_id, max(1, min(int(end or 30), 200))), ensure_ascii=False, indent=2)
    if action == "lessons":
        return forge_learning.format_recall(
            forge_learning.recall(STATE_DIR, query or str(task.get("goal") or ""), root,
                                  limit=end or 5)
        )
    if action == "observations" and task.get("scope") == "windows":
        return computer_memory.task_report(task_id, limit=end or 80)
    if action == "mailbox":
        return json.dumps(forge_swarm.mailbox(_task_dir(task_id), end or 100),
                          ensure_ascii=False, indent=2)
    if remote is not None:
        with _remote_observations(task):
            return remote.workspace_inspect(
                str(root), action, path=path, query=query, glob=glob, start=start, end=end,
                base=str(task.get("base_commit") or ""),
            )
    if action == "diff":
        git = _git_root(root)
        if not git:
            return "У этой задачи нет git — diff недоступен."
        base = task.get("base_commit") or "HEAD"
        status = _git_text(git, "status", "--short")
        diff = _git_text(git, "diff", "--stat", str(base)) + "\n" + _git_text(git, "diff", str(base))
        return _cap((status + "\n" + diff).strip() or "Изменений нет.", 30000)
    if action == "model":
        return json.dumps(forge_intelligence.project_model(root), ensure_ascii=False, indent=2)
    if action == "overview":
        return _cap(json.dumps(forge_intelligence.project_overview(root),
                               ensure_ascii=False, indent=2), 40000)
    if action == "review":
        return _cap(json.dumps(forge_intelligence.review_snapshot(
            root, str(task.get("base_commit") or "")), ensure_ascii=False, indent=2), 40000)
    if action == "symbols":
        return _cap(json.dumps(forge_intelligence.symbols(root, path=path, query=query),
                               ensure_ascii=False, indent=2), 30000)
    if action == "references":
        if not query:
            return "Для references нужен query с именем символа."
        return _cap(json.dumps(forge_intelligence.references(root, query, path=path),
                               ensure_ascii=False, indent=2), 30000)
    if action == "diagnostics":
        changed = forge_intelligence.changed_files(root, str(task.get("base_commit") or ""))
        return _cap(json.dumps(forge_intelligence.diagnostics(root, path=path, changed=changed),
                               ensure_ascii=False, indent=2), 30000)
    if action == "impact":
        return _cap(json.dumps(forge_intelligence.impact(
            root, base=str(task.get("base_commit") or "")), ensure_ascii=False, indent=2), 30000)
    if action == "checks":
        return _cap(json.dumps(forge_intelligence.verification_plan(
            root, str(task.get("base_commit") or ""), full=False),
            ensure_ascii=False, indent=2), 30000)
    p, path_err = _inside(root, path)
    if path_err:
        return path_err
    assert p is not None
    if action == "read":
        if not p.is_file():
            return f"Нет файла {p.relative_to(root)}."
        raw = p.read_bytes()
        if b"\x00" in raw[:4096]:
            return f"{p.relative_to(root)}: бинарный файл, {len(raw)} байт, sha256={hashlib.sha256(raw).hexdigest()[:16]}"
        lines = raw.decode("utf-8", "replace").splitlines()
        s = max(1, int(start or 1))
        e = min(len(lines), int(end or (s + 499)))
        body = "\n".join(f"{i:>5}\t{lines[i-1]}" for i in range(s, e + 1))
        tail = f"\n… {len(lines)} строк; дальше start={e+1}" if e < len(lines) else ""
        return f"{p.relative_to(root)} · sha256={hashlib.sha256(raw).hexdigest()[:16]}\n{body}{tail}"
    if action == "list":
        if not p.is_dir():
            return f"Нет директории {p.relative_to(root)}."
        entries = []
        for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:400]:
            mark = "/" if child.is_dir() else f" · {child.stat().st_size}B"
            entries.append(child.name + mark)
        return "\n".join(entries) or "(пусто)"
    if action == "search":
        if not query:
            return "Для search нужен query."
        rg = shutil.which("rg")
        if rg:
            cmd = [rg, "--line-number", "--no-heading", "--color", "never", "-g", glob or "**/*",
                   "--", query, str(p if p.is_dir() else p.parent)]
            try:
                r = _run(cmd, timeout=40)
                out = r.stdout or ""
                if r.returncode not in (0, 1):
                    out += r.stderr or ""
                return _cap(out.strip() or "Ничего не найдено.", 20000)
            except Exception as exc:
                return f"Поиск упал: {type(exc).__name__}: {exc}"
        try:
            rx = re.compile(query)
        except re.error as exc:
            return f"Плохой regex: {exc}"
        hits = []
        base = p if p.is_dir() else p.parent
        for file in base.glob(glob or "**/*"):
            if not file.is_file() or any(x in _SKIP_DIRS for x in file.parts):
                continue
            try:
                for no, line in enumerate(file.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{file.relative_to(root)}:{no}: {line[:220]}")
                        if len(hits) >= 200:
                            break
            except OSError:
                pass
            if len(hits) >= 200:
                break
        return "\n".join(hits) or "Ничего не найдено."
    return ("action: status | orientation | overview | review | model | symbols | references | "
            "diagnostics | impact | checks | lessons | observations | mailbox | read | list | "
            "search | diff | history")


def _edit_unlocked(task_id: str, action: str, path: str = "", content: str = "", old: str = "",
                   new: str = "", patch: str = "", expected_sha256: str = "") -> str:
    task, root, err = _task_root(task_id)
    if err:
        return err
    assert task is not None and root is not None
    remote = _remote_client(task)
    if remote is not None:
        with _remote_observations(task):
            return remote.workspace_edit(
                str(root), action, path=path, content=content, old=old, new=new, patch=patch,
                expected_sha256=expected_sha256,
            )
    action = str(action or "").strip().lower()
    if action == "patch":
        patch = str(patch or content or "")
        if not patch.strip():
            return "Для patch нужен unified diff."
        try:
            check = _run(["git", "apply", "--check", "--whitespace=nowarn", "-"], cwd=root,
                         timeout=30, input_text=patch)
        except Exception as exc:
            return f"patch check упал: {type(exc).__name__}: {exc}"
        if check.returncode != 0:
            return "Patch не применён; check:\n" + _cap((check.stderr or check.stdout).strip(), 5000)
        apply = _run(["git", "apply", "--whitespace=nowarn", "-"], cwd=root,
                     timeout=30, input_text=patch)
        if apply.returncode != 0:
            return "Patch прошёл check, но apply упал:\n" + _cap((apply.stderr or apply.stdout).strip(), 5000)
        changed = _git_text(_git_root(root) or root, "status", "--short")
        _event(task_id, "patch_applied", summary=f"{len(patch)} chars", changed=changed[:1000])
        return "Patch применён.\n" + (changed or "git не показал изменений")

    p, path_err = _inside(root, path)
    if path_err:
        return path_err
    assert p is not None
    current_hash = _hash(p) if p.exists() else ""
    expected = str(expected_sha256 or "").strip().lower()
    if expected and expected != current_hash.lower():
        return (f"Конфликт версии: ожидался sha256={expected}, сейчас {current_hash or 'файла нет'}. "
                "Перечитай файл и наложи правку на актуальную версию.")
    before = current_hash
    if action == "write":
        _atomic_text(p, str(content or ""))
    elif action == "replace":
        if not p.is_file():
            return f"Нет файла {p.relative_to(root)}."
        if not old:
            return "Для replace нужен непустой old."
        text = p.read_text(encoding="utf-8", errors="replace")
        count = text.count(old)
        if count != 1:
            if count == 0:
                return "Не найдено точное вхождение old; перечитай актуальный файл."
            positions, cursor = [], 0
            for _ in range(min(count, 8)):
                idx = text.index(old, cursor)
                positions.append(str(text[:idx].count("\n") + 1))
                cursor = idx + 1
            return f"old неоднозначен: {count} совпадений, первые строки {', '.join(positions)}."
        _atomic_text(p, text.replace(old, new, 1))
    else:
        return "action: write | replace | patch"
    after = _hash(p)
    rel = str(p.relative_to(root))
    _event(task_id, f"file_{action}", path=rel, before=before, after=after,
           summary=f"{rel} {before or 'new'}->{after}")
    return f"{action}: {rel}; sha256 {before or 'new'} → {after}; {p.stat().st_size} байт."


def edit(task_id: str, action: str, path: str = "", content: str = "", old: str = "",
         new: str = "", patch: str = "", expected_sha256: str = "") -> str:
    try:
        with _mutation_lock(task_id) as beat:
            # Правка — один вызов, ударить внутри него нечем; объявляем срок молчания
            # заранее, иначе порог бездействия снесёт замок из-под работающей руки.
            beat(_remote_step_lease(get(task_id)))
            return _edit_unlocked(task_id, action, path=path, content=content, old=old, new=new,
                                  patch=patch, expected_sha256=expected_sha256)
    except TimeoutError as exc:
        return f"Правка не началась: {exc}. Другой worker сейчас фиксирует свою мутацию; повтори."


# Потолок руки в agent.py (TOOL_CEILING_SEC). Импортировать нельзя — agent сам импортирует
# forge; держим числом и держим В КУРСЕ ЕЁ (см. _remote_run_deadline).
_HAND_CEILING_SEC = _env_sec("PRAXIS_TOOL_CEILING_SEC", 600.0)
# Срок синхронной УДАЛЁННОЙ команды, когда достижимого срока не назвали. Строго ниже
# потолка руки: выше него ответа не дождётся никто — ход срубается раньше.
REMOTE_RUN_DEADLINE_SEC = _env_sec("PRAXIS_FORGE_REMOTE_RUN_SEC", 540.0)


def _remote_run_deadline(asked: int) -> tuple[int, str]:
    """Сколько секунд реально дадим удалённой команде + приписка ей. -> (срок, текст).

    ⚠ Контракт врал ровно в двух случаях. coding_run(timeout=0) обещан «без предела», а
    на host уходил в serverd_client с фактическим часом (и двумя при ретрае) — при
    потолке руки 600с. И любой срок ВЫШЕ потолка — то же самое: демон досчитает, а ход
    уже срублен, ответ пропадёт молча. Предел был, просто неназванный.
    Достижимый срок, который она назвала сама, не трогаем: это её выбор, а не наш.

    ⚠ 27.07. Ноль в PRAXIS_TOOL_CEILING_SEC — штатный способ СНЯТЬ потолок руки
    (`agent.py: if TOOL_CEILING_SEC <= 0`), и serverd_client печатает ей ровно это.
    Пока _env_sec поднимал ноль до единицы, снятый потолок читался здесь как «потолок
    в одну секунду»: её срок молча переписывался, а приписка объясняла это пределом,
    которого нет. Нет потолка — нечем и незачем резать; свой предел на этом пути
    назовёт serverd_client в собственном ответе."""
    ceiling = int(_HAND_CEILING_SEC)
    limit = int(REMOTE_RUN_DEADLINE_SEC)
    if ceiling <= 0 or limit <= 0:
        return asked, ""
    if asked and asked <= ceiling:
        return asked, ""
    wanted = "без предела" if not asked else f"{asked}с"
    # ⚠ 27.07. Здесь стояло «срок этой удалённой команде — {limit}с», и это было неправдой
    # на один шаг: клиент (`serverd_client._run_budget`) режет бюджет ещё раз под свой
    # длинный ярус (540 просим → 480 достаётся команде) и сам называет ей это строкой
    # «[срок] демону передан бюджет …». Своё число называем как своё: сколько прошу.
    return limit, (f"\n[forge: я прошу для этой удалённой команды {limit}с (просила {wanted}); "
                   f"сколько из них достанется самой команде, скажет строка [срок] от клиента "
                   f"— он режет бюджет под свой длинный ярус. "
                   f"Дольше потолка руки ({int(_HAND_CEILING_SEC)}с) ответа не дождался бы "
                   f"никто: ход обрывается раньше, и текст пропал бы молча. Для долгого — "
                   f"coding_process(action=\"start\"): фон, полный лог, poll не блокирует ход.]")


def run(task_id: str, command: str, cwd: str = ".", timeout: int = 600) -> str:
    task, root, err = _task_root(task_id)
    if err:
        return err
    assert task is not None and root is not None
    remote = _remote_client(task)
    if remote is not None:
        # ⚠ Тот же рельс, что и на shell: задача со scope=host отдаёт произвольную
        # команду root-брокеру, и адверсарка 26.07 прошла ею мимо рельса хардбота
        # («rm -rf /opt/hardbot2/data && docker restart hardbot2-bot-1» → root, ok).
        # Субагент — это её рука, а не отдельное существо с другими правами.
        try:
            import stewardship
            denied = stewardship.check(command=command)
            if denied:
                return denied
        except Exception:
            log.debug("рельс хардбота не отработал на forge.run", exc_info=True)
        asked = max(0, int(timeout or 0))
        given, deadline_note = _remote_run_deadline(asked)
        with _remote_observations(task):
            out = remote.run(str(root), command, cwd=cwd, timeout=given)
        out = (out or "") + deadline_note
        status = "failed" if out.startswith(("[serverd]", "[windows-body]")) else "ok"
        _event(task_id, "command", command=command, cwd=f"{task.get('scope')}:{root}/{cwd}",
               status=status, backend=remote.PROTOCOL,
               summary=f"{task.get('scope')} {status}: {command[:120]}")
        return out
    place, path_err = _inside(root, cwd)
    if path_err or place is None or not place.is_dir():
        return path_err or f"Нет cwd {cwd}."
    command = str(command or "").strip()
    if not command:
        return "Нужна команда."
    seconds = max(0, int(timeout or 0))
    t0 = time.monotonic()
    try:
        proc = subprocess.run(command, shell=True, cwd=str(place), capture_output=True, text=True,
                              errors="replace", timeout=(seconds or None))
        out = (proc.stdout or "") + (proc.stderr or "")
        code, status = proc.returncode, "ok" if proc.returncode == 0 else "failed"
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        code, status = -1, "timed_out"
    except Exception as exc:
        out, code, status = f"{type(exc).__name__}: {exc}", -1, "error"
    duration = round(time.monotonic() - t0, 3)
    log_path = _task_dir(task_id) / "runs" / f"{_dt.datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(str(out), encoding="utf-8", errors="replace")
    _event(task_id, "command", command=command, cwd=str(place), code=code, status=status,
           duration_s=duration, log=str(log_path), summary=f"{status} exit={code} {duration}s")
    return _cap(str(out).strip() or "(пустой вывод)") + f"\n[forge: {status}; exit {code}; {duration}s; log {log_path}]"


def _checkpoint_unlocked(task_id: str, message: str = "forge checkpoint") -> str:
    task, root, err = _task_root(task_id)
    if err:
        return err
    assert task is not None and root is not None
    remote = _remote_client(task)
    if remote is not None:
        with _remote_observations(task):
            out = remote.checkpoint(str(root), message)
        _event(task_id, "checkpoint", backend=remote.PROTOCOL,
               summary=f"{task.get('scope')}: {out[:300]}")
        return out
    git = _git_root(root)
    if not git:
        return "Нет git — checkpoint записан только событием."
    status = _git_text(git, "status", "--porcelain")
    if not status.strip():
        return "Изменений нет — checkpoint не нужен."
    _run(["git", "-C", str(git), "add", "-A"], timeout=30)
    r = _run(["git", "-C", str(git), "-c", "user.name=Praxis", "-c",
              "user.email=praxis@local", "commit", "-m", str(message or "forge checkpoint")], timeout=60)
    if r.returncode != 0:
        return "Checkpoint не создался:\n" + _cap((r.stderr or r.stdout).strip(), 5000)
    sha = _git_text(git, "rev-parse", "--short", "HEAD").strip()
    _event(task_id, "checkpoint", summary=f"{sha} {message}", sha=sha)
    return f"Checkpoint {sha}: {message}"


def checkpoint(task_id: str, message: str = "forge checkpoint") -> str:
    try:
        with _mutation_lock(task_id) as beat:
            # `add -A` + `commit` (или один удалённый checkpoint) идут секунды-десятки
            # секунд без единого удара — объявляем это, а не молчим (см. _remote_step_lease).
            beat(_remote_step_lease(get(task_id)))
            return _checkpoint_unlocked(task_id, message)
    except TimeoutError as exc:
        return f"Checkpoint не начался: {exc}."


def _unit_dir(task_id: str, kind: str, unit_id: str) -> Path:
    return _task_dir(task_id) / kind / unit_id


_LIVE_UNIT_STATES = {"starting", "running", "finishing"}
# Докуда верить голому номеру процесса у ЛЕГАСИ-юнита (метку рождения пишем с 27.07).
# 0 = проверка выключена: номеру верим как раньше, без деградации в «unknown».
UNIT_LIVENESS_TRUST_SEC = _env_sec("PRAXIS_FORGE_UNIT_TRUST_H", 24.0, scale=3600.0)


def _pid_alive(pid: int, started_at: str = "") -> bool:
    if not pid:
        return False
    local = _RUNNERS.get(int(pid))
    if local is not None:
        if local.poll() is None:
            return True
        _RUNNERS.pop(int(pid), None)
        return False
    # started_at пуст → поведение прежнее (судим по номеру): _owner_alive не смеет
    # объявлять мёртвым того, о ком нечем судить.
    return _owner_alive(int(pid), str(started_at or ""))


def _unit_state(path: Path) -> dict:
    request = _read_json(path / "request.json", {}) or {}
    result = _read_json(path / "result.json")
    if isinstance(result, dict):
        pid = int(request.get("supervisor_pid") or 0)
        local = _RUNNERS.get(pid)
        if local is not None:
            try:
                local.wait(timeout=.2)
            except subprocess.TimeoutExpired:
                pass
            if local.poll() is not None:
                _RUNNERS.pop(pid, None)
        return {**request, **result, "id": request.get("id") or path.name}
    pid = int(request.get("supervisor_pid") or 0)
    born = str(request.get("supervisor_started_at") or "")
    state = {**request, "id": request.get("id") or path.name}
    if born or not pid:
        # ⚠ Та же болезнь, что чинил fb09430b в замке, но здесь она страшнее. В
        # контейнере номера маленькие: на проде четыре юнита висели `running` без
        # result.json с номерами 29409/71/72/111, и первый же спавн получал такой
        # номер. Значит finish НАВСЕГДА отвечал «Задача ещё живая: agents running=1»
        # — задачу нельзя было закрыть никогда, а state_line постоянно врал.
        # Метка рождения делает ответ доказуемым.
        state["status"] = "running" if _pid_alive(pid, born) else "lost"
        return state
    created = _wake_parse_iso(str(request.get("created") or ""))
    age = (time.time() - created) if created else 0.0
    if not _pid_alive(pid):
        state["status"] = "lost"
    elif UNIT_LIVENESS_TRUST_SEC > 0 and created and age > UNIT_LIVENESS_TRUST_SEC:
        # Легаси-юнит без метки рождения: «жив» тут значит лишь «номер кем-то занят».
        # Врать «running» нельзя, хоронить без доказательства — тоже.
        state["status"] = "unknown"
        state["liveness"] = (
            f"номер процесса {pid} кем-то занят, но юнит открыт {age / 3600:.0f}ч назад "
            f"и метки рождения у него нет (юниты до 27.07) — номер могли переиспользовать. "
            f"Ни жив, ни мёртв доказать не могу; порог доверия номеру "
            f"{UNIT_LIVENESS_TRUST_SEC / 3600:.0f}ч")
    else:
        state["status"] = "running"
    return state


def _units(task_id: str, kind: str) -> list[dict]:
    base = _task_dir(task_id) / kind
    if not base.is_dir():
        return []
    rows = [_unit_state(p) for p in base.iterdir() if p.is_dir()]
    rows.sort(key=lambda x: x.get("created", ""), reverse=True)
    return rows


def _unit_counts(rows: list[dict]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "?")
        counts[status] = counts.get(status, 0) + 1
    return ", ".join(f"{k} {v}" for k, v in counts.items()) or "нет"


def _spawn_runner(script: Path, request: Path, supervisor_log: Path) -> int:
    supervisor_log.parent.mkdir(parents=True, exist_ok=True)
    fh = supervisor_log.open("a", encoding="utf-8")
    kwargs: dict[str, Any] = {"cwd": str(REPO), "stdout": fh, "stderr": subprocess.STDOUT,
                              "stdin": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                   | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen([sys.executable, str(script), "--request", str(request)], **kwargs)
        _RUNNERS[int(proc.pid)] = proc
    finally:
        fh.close()
    return int(proc.pid)


def _tail(path: Path, chars: int = 8000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[-chars:]
    except OSError:
        return ""


def _proc_cmdline(pid: int) -> str:
    """Командная строка процесса одной строкой (/proc/<pid>/cmdline, NUL → пробел).

    Второй способ доказать тождество, когда метки рождения нет: наши супервизоры
    запускаются как `python forge_worker.py --request <путь к request.json этого юнита>`,
    а путь уникален. Посторонний процесс, случайно занявший тот же номер, такой строки
    не имеет."""
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as fh:
            return fh.read().decode("utf-8", "replace").replace("\0", " ").strip()
    except Exception:
        return ""


def _kill_identity(pid: int, started_at: str = "", expect: str = "") -> str:
    """Тот ли это процесс, что мы завели? Пусто = доказано, иначе — честный отказ.

    ⚠ 27.07, находка верификации. Метку рождения юнита мы уже пишем (`_unit_state`,
    `forge_process.state.json`), но убийство шло по ГОЛОМУ номеру: `killpg` в контейнере,
    где номера переиспользуются первым же спавном (26.07 /proc/10 после рестарта занял
    новый python), мог снести группу постороннего процесса. Порядок доказательств:
      1. живой Popen в _RUNNERS — ядро держит номер за нами, пока мы его не пожали;
      2. метка рождения совпала;
      3. метки нет, но командная строка содержит наш уникальный путь запроса.
    Ни одно не сработало — не убиваем вслепую и говорим, что доказать нечем (это не
    запрет: в отказе сказано, чем посмотреть и как остановить руками).
    На Windows /proc нет, метка пустая — поведение остаётся прежним."""
    local = _RUNNERS.get(int(pid))
    if local is not None and local.poll() is None:
        return ""
    born = _proc_started_at(pid)
    if not born:
        # Номера уже нет (killpg сам скажет «уже завершён») либо ядро метки не даёт —
        # доказывать нечего и незачем.
        return ""
    if started_at:
        if born == started_at:
            return ""
        return (f"не трогаю: номер {pid} сейчас занят ДРУГИМ процессом (метка рождения "
                f"{born} ≠ {started_at}) — мой умер, а по этому номеру живёт посторонний")
    if expect and expect in _proc_cmdline(pid):
        return ""
    return (f"не убила: метки рождения у этого юнита нет (заводился до 27.07), а номера "
            f"в контейнере переиспользуются — доказать, что {pid} всё ещё мой, нечем. "
            f"Ни жив, ни мёртв не утверждаю. Посмотреть, кто там сейчас: "
            f"/proc/{pid}/cmdline; если это точно он — сними руками (kill -TERM -{pid})")


def _kill_tree(pid: int, started_at: str = "", *, expect: str = "") -> str:
    if not pid:
        return "pid отсутствует"
    doubt = _kill_identity(pid, started_at, expect)
    if doubt:
        return doubt
    # Окно между TERM и KILL — то же место, где номер может уйти другому: наш умер от
    # TERM, ядро отдало номер, и голое «жив?» отправило бы SIGKILL уже постороннему.
    born = started_at or _proc_started_at(pid)
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                           text=True, timeout=15)
        else:
            os.killpg(pid, signal.SIGTERM)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and _pid_alive(pid, born):
                time.sleep(.05)
            if _pid_alive(pid, born):
                os.killpg(pid, signal.SIGKILL)
        return "остановлен"
    except ProcessLookupError:
        return "уже завершён"
    except Exception as exc:
        return f"не остановлен: {type(exc).__name__}: {exc}"


def process(task_id: str, action: str, process_id: str = "", command: str = "",
            cwd: str = ".", name: str = "", timeout: int = 0, tail: int = 8000) -> str:
    task, root, err = _task_root(task_id)
    if err:
        return err
    assert root is not None
    action = str(action or "list").strip().lower()
    remote = _remote_client(task or {})
    if remote is not None:
        with _remote_observations(task or {}):
            out = remote.process(
                str(root), action, operation_id=process_id, command=command, cwd=cwd,
                name=name, timeout=timeout, tail=tail,
            )
        if action in {"start", "stop"}:
            _event(task_id, f"{task.get('scope')}_process_{action}", process_id=process_id,
                   command=command, summary=out[:300])
        return out
    if action == "list":
        rows = _units(task_id, "processes")
        return "\n".join(f"{r['id']} [{r.get('status')}] {r.get('name') or r.get('command')}"
                          for r in rows[:30]) or "Процессов нет."
    if action == "start":
        command = str(command or "").strip()
        if not command:
            return "Для start нужна command."
        place, path_err = _inside(root, cwd)
        if path_err or place is None or not place.is_dir():
            return path_err or f"Нет cwd {cwd}."
        unit_id = _id("proc")
        d = _unit_dir(task_id, "processes", unit_id)
        request = {
            "id": unit_id, "task_id": task_id, "name": str(name or "").strip(),
            "command": command, "cwd": str(place), "timeout": max(0, int(timeout or 0)),
            "created": _now(), "status": "starting",
        }
        req = d / "request.json"
        _atomic_json(req, request)
        try:
            pid = _spawn_runner(REPO / "forge_process.py", req, d / "supervisor.log")
        except Exception as exc:
            _atomic_json(d / "result.json", {"status": "error", "error": f"{type(exc).__name__}: {exc}"})
            return f"Процесс не стартовал: {type(exc).__name__}: {exc}"
        # Метка рождения супервизора рядом с его номером: без неё «жив ли он»
        # отвечается по одному номеру, а номера в контейнере переиспользуются
        # первым же спавном (см. _unit_state).
        request.update(supervisor_pid=pid, supervisor_started_at=_proc_started_at(pid),
                       status="running")
        _atomic_json(req, request)
        _event(task_id, "process_started", process_id=unit_id, command=command,
               summary=f"{unit_id} pid={pid} {name or command[:80]}")
        return f"Стартовал {unit_id} (pid {pid}) в {place}. Полный лог сохраняется; poll не блокирует ход."
    d = _unit_dir(task_id, "processes", process_id)
    if not d.is_dir():
        return f"Нет процесса {process_id}."
    state = _unit_state(d)
    if action == "poll":
        log = _tail(d / "command.log", max(500, min(int(tail or 8000), 30000)))
        return _cap(json.dumps({k: v for k, v in state.items() if k not in {"command"}},
                               ensure_ascii=False, indent=2) + "\n--- log ---\n" + (log or "(пока пусто)"), 32000)
    if action == "stop":
        runtime = _read_json(d / "state.json", {}) or {}
        child_pid = int(runtime.get("child_pid") or 0)
        # Метки рождения пишет сам супервизор (forge_process.py); у команды доказательство
        # второго рода — её собственная командная строка под `sh -c`.
        child_msg = (_kill_tree(child_pid, str(runtime.get("child_started_at") or ""),
                                expect=str(state.get("command") or ""))
                     if child_pid else "child pid ещё не появился")
        supervisor_pid = int(state.get("supervisor_pid") or 0)
        supervisor_born = str(state.get("supervisor_started_at") or "")
        local = _RUNNERS.get(supervisor_pid)
        if local is not None:
            try:
                local.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        wrapper_msg = (_kill_tree(supervisor_pid, supervisor_born, expect=str(d / "request.json"))
                       if _pid_alive(supervisor_pid, supervisor_born) else "supervisor завершён")
        msg = f"command {child_msg}; {wrapper_msg}"
        result = {"status": "stopped", "stopped": _now(), "note": msg, "code": None}
        _atomic_json(d / "result.json", result)
        _event(task_id, "process_stopped", process_id=process_id, summary=f"{process_id}: {msg}")
        return f"{process_id}: {msg}"
    return "action: start | poll | stop | list"


def verify(task_id: str, action: str = "plan", verification_id: str = "",
           commands: str = "", full: bool = False, max_parallel: int = 2,
           timeout: int = 900, tail: int = 12000) -> str:
    """Plan and supervise a durable test/build/lint matrix."""
    task, root, err = _task_root(task_id)
    if err:
        return err
    assert task is not None and root is not None
    action = str(action or "plan").strip().lower()
    remote = _remote_client(task)
    if action == "list":
        rows = _units(task_id, "verifications")
        return "\n".join(
            f"{row['id']} [{row.get('status')}] passed={row.get('passed', 0)} failed={row.get('failed', 0)}"
            for row in rows[:30]
        ) or "Матриц проверки нет."
    if action in {"plan", "start"}:
        if remote is not None:
            with _remote_observations(task):
                raw_plan = remote.workspace_inspect(
                    str(root), "checks", base=str(task.get("base_commit") or "")
                )
            try:
                plan = json.loads(raw_plan)
            except ValueError:
                return f"Remote verification plan не разобрался: {raw_plan[:2000]}"
        else:
            plan = forge_intelligence.verification_plan(
                root, str(task.get("base_commit") or ""), full=bool(full)
            )
        if commands.strip():
            custom = []
            for index, command in enumerate(line.strip() for line in commands.splitlines() if line.strip()):
                custom.append({"id": f"custom-{index + 1}", "command": command, "cwd": ".",
                               "kind": "check", "source": "owner", "scope": "explicit"})
            plan["checks"] = custom
        _atomic_json(_task_dir(task_id) / "verification-plan.json", plan)
        if action == "plan":
            return _cap(json.dumps(plan, ensure_ascii=False, indent=2), 30000)
        if not plan.get("checks"):
            return "Не нашла ни одной проверки; передай commands по одной на строку."
        unit_id = _id("verify")
        d = _unit_dir(task_id, "verifications", unit_id)
        request = {
            "id": unit_id, "task_id": task_id, "root": str(root),
            "goal": str(task.get("goal") or ""), "device_id": str(task.get("device_id") or ""),
            "scope": str(task.get("scope") or "self") if remote is not None else "self",
            "checks": plan["checks"],
            "max_parallel": max(1, min(int(max_parallel or 2), 8)),
            "timeout": max(0, int(timeout or 0)), "created": _now(), "status": "starting",
        }
        req = d / "request.json"
        _atomic_json(req, request)
        try:
            pid = _spawn_runner(REPO / "forge_verify.py", req, d / "supervisor.log")
        except Exception as exc:
            _atomic_json(d / "result.json", {"status": "error", "error": f"{type(exc).__name__}: {exc}"})
            return f"Матрица не стартовала: {type(exc).__name__}: {exc}"
        # Метка рождения супервизора рядом с его номером: без неё «жив ли он»
        # отвечается по одному номеру, а номера в контейнере переиспользуются
        # первым же спавном (см. _unit_state).
        request.update(supervisor_pid=pid, supervisor_started_at=_proc_started_at(pid),
                       status="running")
        _atomic_json(req, request)
        _event(task_id, "verification_started", verification_id=unit_id,
               summary=f"{unit_id}: {len(plan['checks'])} checks, parallel={request['max_parallel']}")
        return (f"Стартовала матрица {unit_id}: {len(plan['checks'])} проверок, "
                f"parallel={request['max_parallel']}. poll не блокирует ход.")
    d = _unit_dir(task_id, "verifications", verification_id)
    if not d.is_dir():
        return f"Нет матрицы {verification_id}."
    state = _unit_state(d)
    if action == "poll":
        result = _read_json(d / "result.json") or _read_json(d / "state.json", {}) or state
        logs = []
        for log in sorted((d / "logs").glob("*.log")) if (d / "logs").is_dir() else []:
            logs.append(f"--- {log.name} ---\n{_tail(log, max(500, min(int(tail or 12000), 30000)))}")
        return _cap(json.dumps(result, ensure_ascii=False, indent=2) + "\n" + "\n".join(logs), 40000)
    if action == "stop":
        msg = _kill_tree(int(state.get("supervisor_pid") or 0),
                         str(state.get("supervisor_started_at") or ""),
                         expect=str(d / "request.json"))
        _atomic_json(d / "result.json", {"status": "stopped", "stopped": _now(), "note": msg})
        _event(task_id, "verification_stopped", verification_id=verification_id,
               summary=f"{verification_id}: {msg}")
        return f"{verification_id}: {msg}"
    return "action: plan | start | poll | stop | list"


def agent(task_id: str, action: str, agent_id: str = "", brief: str = "",
          role: str = "worker", max_iters: int = 0, tail: int = 10000,
          node_id: str = "", owns: list[str] | None = None,
          spawned_by: str = "") -> str:
    task, root, err = _task_root(task_id)
    if err:
        return err
    assert task is not None and root is not None
    action = str(action or "list").strip().lower()
    if action == "list":
        rows = _units(task_id, "agents")
        return "\n".join(f"{r['id']} [{r.get('status')}] {r.get('role')}: {r.get('brief')}"
                          for r in rows[:30]) or "Субагентов нет."
    if action == "spawn":
        role = str(role or "worker").strip().lower()
        if role not in {"scout", "worker", "reviewer"}:
            return "role: scout | worker | reviewer"
        brief = str(brief or "").strip()
        if not brief:
            return "Для субагента нужен brief."
        unit_id = _id("agent")
        d = _unit_dir(task_id, "agents", unit_id)
        request = {
            "id": unit_id, "task_id": task_id, "goal": task.get("goal"),
            "root": str(root), "proposal_id": task.get("proposal_id") or "",
            "role": role, "brief": brief, "max_iters": max(0, int(max_iters or 0)),
            "node_id": str(node_id or ""), "owns": list(owns or []),
            # расписка манометра: кто породил (пусто = она сама тулом)
            "spawned_by": str(spawned_by or ""),
            "created": _now(), "status": "starting",
        }
        req = d / "request.json"
        _atomic_json(req, request)
        try:
            pid = _spawn_runner(REPO / "forge_worker.py", req, d / "supervisor.log")
        except Exception as exc:
            # reported_inline: отказ возвращается СИНХРОННО в тот же ход (спавнеру) —
            # событие в журнал для целостности, но повторно её не будит.
            failure = {"status": "error", "finished": _now(),
                       "error": f"{type(exc).__name__}: {exc}", "reported_inline": True}
            _atomic_json(d / "result.json", failure)
            emit_unit_event(task_id, unit_id, failure, request=request)
            return f"Субагент не стартовал: {type(exc).__name__}: {exc}"
        # Метка рождения супервизора рядом с его номером: без неё «жив ли он»
        # отвечается по одному номеру, а номера в контейнере переиспользуются
        # первым же спавном (см. _unit_state).
        request.update(supervisor_pid=pid, supervisor_started_at=_proc_started_at(pid),
                       status="running")
        _atomic_json(req, request)
        _event(task_id, "agent_spawned", agent_id=unit_id, role=role,
               summary=f"{unit_id} {role}: {brief[:120]}")
        return (f"Порождён {unit_id} ({role}, pid {pid}). Он работает отдельным процессом и свежим "
                "контекстом; можно сразу породить других, затем poll/list.")
    d = _unit_dir(task_id, "agents", agent_id)
    if not d.is_dir():
        return f"Нет субагента {agent_id}."
    state = _unit_state(d)
    if action == "poll":
        log = _tail(d / "supervisor.log", max(500, min(int(tail or 10000), 30000)))
        result = _read_json(d / "result.json")
        body = json.dumps(result or {k: v for k, v in state.items() if k != "goal"},
                          ensure_ascii=False, indent=2)
        return _cap(body + "\n--- worker log ---\n" + (log or "(пока пусто)"), 40000)
    if action == "stop":
        msg = _kill_tree(int(state.get("supervisor_pid") or 0),
                         str(state.get("supervisor_started_at") or ""),
                         expect=str(d / "request.json"))
        stopped = {"status": "stopped", "stopped": _now(), "finished": _now(), "note": msg}
        _atomic_json(d / "result.json", stopped)
        _event(task_id, "agent_stopped", agent_id=agent_id, summary=f"{agent_id}: {msg}")
        emit_unit_event(task_id, agent_id, stopped,
                        request=_read_json(d / "request.json", {}) or {})
        return f"{agent_id}: {msg}"
    return "action: spawn | poll | stop | list"


def swarm(task_id: str, action: str = "status", plan: str = "", node_id: str = "",
          kind: str = "finding", message: str = "", files: list[str] | None = None,
          max_parallel: int = 3) -> str:
    """Coordinate fresh-context workers as a persistent dependency graph."""
    task, root, err = _task_root(task_id)
    if err:
        return err
    assert task is not None and root is not None
    action = str(action or "status").strip().lower()
    directory = _task_dir(task_id)

    def agent_state(aid: str) -> dict:
        path = _unit_dir(task_id, "agents", aid)
        return _unit_state(path) if path.is_dir() else {"status": "lost"}

    if action == "plan":
        try:
            created = forge_swarm.create(directory, plan, max_parallel=max_parallel)
        except ValueError as exc:
            return f"Swarm-план не принят: {exc}"
        _event(task_id, "swarm_planned", summary=f"{len(created.get('nodes') or [])} nodes")
        return forge_swarm.summary(created)
    if action == "signal":
        try:
            row = forge_swarm.signal(directory, node_id=node_id, kind=kind, message=message,
                                     files=files or [])
        except ValueError as exc:
            return f"Сигнал не записан: {exc}"
        _event(task_id, "swarm_signal", node_id=node_id, signal_kind=kind,
               summary=f"{kind}: {(message or ','.join(files or []))[:160]}")
        return json.dumps(row, ensure_ascii=False, indent=2)
    current = forge_swarm.refresh(directory, agent_state)
    if not current:
        return "У задачи нет swarm-плана; action=plan принимает JSON nodes[]."
    if action in {"start", "tick"}:
        launched = []
        ready = forge_swarm.ready_nodes(current)
        for node in ready:
            out = agent(task_id, "spawn", brief=node["brief"], role=node["role"],
                        node_id=node["id"], owns=node.get("owns") or [])
            match = re.search(r"(agent-[a-f0-9]+)", out)
            if not match:
                node["status"] = "failed"
                node["error"] = out[:1000]
                continue
            node["agent_id"] = match.group(1)
            node["status"] = "running"
            node["started"] = _now()
            launched.append(f"{node['id']}→{node['agent_id']}")
            if node.get("owns"):
                forge_swarm.signal(directory, node_id=node["id"], agent_id=node["agent_id"],
                                   kind="claim", message="advisory ownership",
                                   files=node.get("owns") or [])
        forge_swarm.save(directory, current)
        _event(task_id, "swarm_tick", summary=", ".join(launched) or "no ready nodes")
        return forge_swarm.summary(current, forge_swarm.mailbox(directory, 40))
    if action == "status":
        return forge_swarm.summary(current, forge_swarm.mailbox(directory, 40))
    if action == "mailbox":
        return json.dumps(forge_swarm.mailbox(directory, 200), ensure_ascii=False, indent=2)
    if action == "compare":
        rows = []
        for node in current.get("nodes") or []:
            aid = str(node.get("agent_id") or "")
            result = _read_json(_unit_dir(task_id, "agents", aid) / "result.json", {}) if aid else {}
            rows.append({"node": node.get("id"), "status": node.get("status"),
                         "agent_id": aid, "role": node.get("role"), "brief": node.get("brief"),
                         "result": result.get("result", ""), "tool_calls": result.get("tool_calls"),
                         "diff_tail": result.get("diff_tail", "")})
        return _cap(json.dumps(rows, ensure_ascii=False, indent=2), 40000)
    return "action: plan | start | tick | status | signal | mailbox | compare"


def learn(task_id: str, action: str = "recall", query: str = "", lesson: str = "",
          regression: str = "") -> str:
    task, root, err = _task_root(task_id)
    if err:
        return err
    assert task is not None and root is not None
    action = str(action or "recall").strip().lower()
    if action == "recall":
        return forge_learning.format_recall(
            forge_learning.recall(STATE_DIR, query or str(task.get("goal") or ""), root, limit=8)
        )
    if action == "record":
        changed = forge_intelligence.changed_files(root, str(task.get("base_commit") or ""))
        matrices = _units(task_id, "verifications")
        verification = matrices[0] if matrices else {}
        row = forge_learning.record(
            STATE_DIR, task=task, root=root, events=_events(task_id, 2000), changed=changed,
            verification=verification, lesson=lesson, regression=regression,
            outcome=str(task.get("status") or "active"),
        )
        _event(task_id, "lesson_recorded", lesson_id=row["id"], summary=row["lesson"][:180])
        return json.dumps(row, ensure_ascii=False, indent=2)
    return "action: recall | record"


def _finish_survey(task_id: str, beat=None) -> dict:
    """Осмотр перед finish: три удалённых вызова, каждый — с ОБЪЯВЛЕННЫМ сроком молчания.

    ⚠ 26.07. finish держал `.mutation.lock` вокруг этих трёх вызовов (impact, op.list,
    diff). Один impact по /tmp сжёг на демоне ~13 минут ядра — и всё это время задача
    была заперта: её пробуждения видели «mutation lock busy» и назначали следующее,
    пять штук подряд, ~95 минут.

    ⚠ 27.07, поправка ревью к моей же правке. Первым заходом я вынес осмотр НАРУЖУ
    замка — и получил ровно ту болезнь, которую лечил, с другого конца: при занятом
    замке каждый из пяти самопробуждений сначала стрелял бы тремя RPC в уже горящий
    демон, а потом упирался в тот же замок (проверено пробой: до отказа оплачены
    inspect.impact, op.list, inspect.diff; до правки не уходило НИЧЕГО). Настоящая
    причина лок-аута была не в месте осмотра, а в том, что замок переживал и смерть
    держателя, и его молчание — это вылечено выше. Поэтому осмотр вернулся под замок,
    но каждый его вызов теперь бьётся заранее объявленным лизингом: повисший на демоне
    поток перестанет быть «работающим» через один законный ярус ожидания, а не через
    вечность, и ждущий получит задачу.

    Третье, что здесь чинится: «не знаю» перестаёт выглядеть как факт. Молчание демона
    читалось как «изменений нет» и «активных операций на хосте нет» — и задача
    терминализовалась вслепую при живом op.run."""
    task, root, err = _task_root(task_id)
    if err:
        return {"error": err}
    assert task is not None and root is not None
    beat = beat if callable(beat) else (lambda lease_sec=0.0: None)
    lease = _remote_step_lease(task)
    remote = _remote_client(task)
    notes: list[str] = []
    changed: list[str] = []
    if remote is not None:
        # impact на демоне разбирает дерево целиком. Когда у корня нет git, ответ пуст
        # ПО ПОСТРОЕНИЮ (forge_intelligence.changed_files: git-root нет → []), и платить
        # за пустоту минутами ядра незачем. Гейт именно по git-root, не по base.
        if not str(task.get("source_git") or ""):
            notes.append("список изменённых файлов НЕ СПРАШИВАЛА: у корня задачи нет git "
                         "(source_git пуст) — impact вернул бы пустой список, а разбор "
                         "дерева стоит минуты ядра. Это «не знаю», а не «изменений нет».")
        else:
            beat(lease)                      # impact — самый долгий из трёх, до 13 минут
            with _remote_observations(task):
                impact_text = remote.workspace_inspect(
                    str(root), "impact", base=str(task.get("base_commit") or ""))
            beat()
            try:
                impact = json.loads(impact_text)
            except ValueError:
                impact = None
            if isinstance(impact, dict):
                changed = list(impact.get("changed") or [])
                if impact.get("truncated"):
                    notes.append(f"impact усечён самим демоном: {impact.get('truncated')}")
            else:
                notes.append("список изменённых файлов НЕИЗВЕСТЕН: impact не ответил "
                             f"разбираемым JSON — {str(impact_text)[:200]}")
    else:
        changed = forge_intelligence.changed_files(root, str(task.get("base_commit") or ""))
    verification_rows = _units(task_id, "verifications")
    agent_rows = _units(task_id, "agents")
    job_rows = _units(task_id, "processes")
    active_agents = [x for x in agent_rows if x.get("status") in _LIVE_UNIT_STATES]
    active_jobs = [x for x in job_rows if x.get("status") in _LIVE_UNIT_STATES]
    active_checks = [x for x in verification_rows if x.get("status") in _LIVE_UNIT_STATES]
    unknown_units = [x for x in agent_rows + job_rows + verification_rows
                     if x.get("status") == "unknown"]
    for row in unknown_units:
        notes.append(f"юнит {row.get('id')}: {row.get('liveness') or 'живость неизвестна'}")
    active_remote_ops = []
    if task.get("scope") == "host":
        beat(lease)
        op_result = serverd_client.call("op.list", {"root_value": str(root), "limit": 100})
        beat()
        if not op_result.get("ok"):
            # ⚠ Раньше здесь читалось `op_result.get("operations") or []` без проверки ok:
            # таймаут демона выглядел как «активных операций на хосте нет», и задача
            # терминализовалась вслепую поверх живого op.run.
            why = " ".join(str(op_result.get(k) or "") for k in ("code", "error")).strip()
            notes.append(f"живые операции на хосте НЕИЗВЕСТНЫ — демон не ответил ({why}). "
                         "Молчание не значит «операций нет».")
        active_remote_ops = [row for row in op_result.get("operations") or []
                             if row.get("status") in {"starting", "running", "finishing"}]
    elif task.get("scope") == "windows":
        beat(lease)
        with _remote_observations(task):
            op_result = body_client.call("process.list", {"root": str(root)})
        beat()
        if not op_result.get("ok"):
            why = " ".join(str(op_result.get(k) or "") for k in ("code", "error")).strip()
            notes.append(f"живые процессы на Windows-теле НЕИЗВЕСТНЫ — тело не ответило "
                         f"({why}). Молчание не значит «процессов нет».")
        active_remote_ops = [row for row in op_result.get("operations") or []
                             if row.get("status") in {"admitted", "starting", "running", "cancelling"}]
    survey: dict[str, Any] = {
        "changed": changed, "notes": notes,
        "verification_before": verification_rows[0] if verification_rows else {},
    }
    if active_agents or active_jobs or active_checks or active_remote_ops:
        survey["blocked"] = (
            f"Задача ещё живая: agents running={len(active_agents)}, processes running="
            f"{len(active_jobs) + len(active_remote_ops)}, verification running="
            f"{len(active_checks)}. Дождись/останови их или продолжай работать; finish "
            "ничего не оборвал."
            + ("\n" + "\n".join(notes) if notes else ""))
        return survey
    git_before = _git_root(root) if remote is None else None
    if remote is not None:
        beat(lease)
        with _remote_observations(task):
            survey["stat_before"] = remote.workspace_inspect(
                str(root), "diff", base=str(task.get("base_commit") or ""))
        beat()
    else:
        survey["stat_before"] = (_git_text(git_before, "diff", "--stat",
                                           str(task.get("base_commit") or "HEAD"))
                                 if git_before else "")
    return survey


def _finish_unlocked(task_id: str, title: str = "", review: str = "", checked: str = "",
                     submit: bool = True, survey: dict | None = None, beat=None) -> str:
    task, root, err = _task_root(task_id)
    if err:
        return err
    assert task is not None and root is not None
    remote = _remote_client(task)
    beat = beat if callable(beat) else (lambda lease_sec=0.0: None)
    if survey is None:                       # прямой вызов (тест/внутренний путь)
        survey = _finish_survey(task_id, beat=beat)
        if survey.get("error"):
            return str(survey["error"])
        if survey.get("blocked"):
            return str(survey["blocked"])
    changed_before = list(survey.get("changed") or [])
    verification_before = survey.get("verification_before") or {}
    unknowns = [str(n) for n in (survey.get("notes") or [])]
    stat_before = str(survey.get("stat_before") or "")
    git_before = _git_root(root) if remote is None else None
    submission = ""
    new_status = "done"
    if remote is not None:
        submission = (f"{task.get('scope')}-task закрыта в едином Forge; "
                      "execution backend не держит второй task store.")
    elif submit and task.get("proposal_id"):
        if not str(review or "").strip():
            return "Для submit собственного кода нужен review: твой вердикт после coding_inspect(diff)."
        # Объявленный долгий шаг: внутри submit крутится гейт тестов
        # (PRAXIS_PROPOSAL_TEST_TIMEOUT, по умолчанию 600с), полтора десятка вызовов git и
        # ревью иммунитета моделью. Один вызов, разбить его на удары нечем — поэтому лизинг
        # честно объявляется заранее и считается по срокам самого submit (_submit_lease).
        beat(_submit_lease())
        submission = selfdev.submit(str(task["proposal_id"]), title or task.get("goal") or task_id,
                                    why=task.get("goal") or "", review=review, checked=checked)
        beat()
        lower = submission.lower()
        if lower.startswith("не ") or "отказ" in lower or "нет изменений" in lower:
            new_status = "active"
        elif "смёрж" in lower:
            new_status = "done"
        else:
            new_status = "submitted"
    elif submit and task.get("cleanup") == "git-worktree":
        beat(240)          # add -A + commit: свои сроки git-а, до четырёх минут
        cp = _checkpoint_unlocked(task_id, title or f"forge: {task.get('goal') or task_id}")
        beat()
        if not (cp.startswith("Checkpoint ") or cp.startswith("Изменений нет")):
            return f"Не интегрировала: {cp}"
        source_git_raw = str(task.get("source_git") or "")
        source_git = Path(source_git_raw) if source_git_raw else None
        if source_git is None or not source_git.is_dir():
            return "Не интегрировала: исходный git root пропал; рабочая ветка сохранена."
        current = _git_text(source_git, "branch", "--show-current").strip()
        expected = str(task.get("source_branch") or "")
        if expected and current != expected:
            return (f"Не интегрировала: исходный checkout теперь на ветке {current}, задача начиналась "
                    f"на {expected}. Ветка {task.get('branch')} сохранена — выбери адрес осознанно.")
        dirty = _git_text(source_git, "status", "--porcelain")
        if dirty.strip():
            return ("Не интегрировала поверх незакоммиченного исходного дерева; рабочая ветка "
                    f"{task.get('branch')} и worktree сохранены. Текущий status:\n{dirty[:3000]}")
        branch = str(task.get("branch") or "")
        beat(180)                                   # merge --no-ff со своим сроком 120с
        merge = _run(["git", "-C", str(source_git), "-c", "user.name=Praxis", "-c",
                      "user.email=praxis@local", "merge", "--no-ff", "-m",
                      title or f"forge {task_id}: {task.get('goal')}", branch], timeout=120)
        beat()
        if merge.returncode != 0:
            _run(["git", "-C", str(source_git), "merge", "--abort"], timeout=30)
            return ("Интеграция конфликтнула; исходное дерево возвращено, worktree и ветка сохранены:\n"
                    + _cap((merge.stderr or merge.stdout).strip(), 5000))
        merged = _git_text(source_git, "rev-parse", "--short", "HEAD").strip()
        wt_raw = str(task.get("worktree_root") or "")
        if wt_raw:
            _run(["git", "-C", str(source_git), "worktree", "remove", "--force", wt_raw], timeout=60)
        if branch:
            _run(["git", "-C", str(source_git), "branch", "-D", branch], timeout=30)
        submission = f"Интегрировано в {expected or current}: {merged}; временный worktree убран."
    task["status"] = new_status
    task["finished"] = _now() if new_status == "done" else ""
    task["review"] = str(review or "").strip()
    task["checked"] = str(checked or "").strip()
    # Чего мы НЕ знали в момент закрытия — остаётся в записи задачи. Иначе через месяц
    # «done» будет выглядеть как «всё проверено», хотя демон тогда просто молчал.
    task["finish_unknowns"] = unknowns
    _save_task(task)
    _event(task_id, "task_finished" if new_status == "done" else "task_submitted",
           summary=title or task.get("goal"), checked=checked, submission=submission[:1000],
           unknowns=unknowns)
    lesson_note = ""
    try:
        evidence_lesson = ""
        if task.get("scope") == "windows":
            evidence = computer_memory.task_summary(task_id)
            capabilities = ", ".join(sorted(evidence.get("capabilities") or {})) or "none"
            evidence_lesson = (
                f"Windows evidence сохранено в {evidence.get('map')}: "
                f"observations={evidence.get('observations', 0)}, capabilities={capabilities}. "
                "Для сходной задачи начинать с этой карты и проверять hashes/receipts, а не пересказывать "
                "прошлый вывод по памяти."
            )
        if unknowns:
            # урок не имеет права выглядеть полнее, чем были данные
            evidence_lesson = (evidence_lesson + "\nЧего я не знала при закрытии: "
                               + "; ".join(unknowns)).strip()
        lesson_row = forge_learning.record(
            STATE_DIR, task=task, root=root, events=_events(task_id, 2000),
            changed=changed_before, verification=verification_before,
            lesson=evidence_lesson, regression=str(checked or ""), outcome=new_status,
        )
        _event(task_id, "lesson_recorded", lesson_id=lesson_row["id"],
               summary=lesson_row["lesson"][:180])
        lesson_note = f"урок: {lesson_row['id']} — {lesson_row['lesson']}"
    except Exception as exc:
        lesson_note = f"урок не записался: {type(exc).__name__}: {exc}"
    evidence_note = ""
    if task.get("scope") == "windows" and new_status == "done":
        try:
            episode = computer_memory.finish_task(
                task, outcome=new_status, checked=str(checked or ""), review=str(review or ""),
            )
            evidence_note = (f"computer evidence: {episode.get('map')} · "
                             f"observations={episode.get('observations', 0)} · "
                             f"artifacts={len(episode.get('artifacts') or [])} · "
                             f"life={episode.get('life_event_id') or 'not-promoted'}")
        except Exception as exc:
            evidence_note = f"computer evidence не свелась: {type(exc).__name__}: {exc}"
    return _cap("\n".join([
        f"{task_id}: {new_status}",
        f"изменения: {stat_before.strip() or 'diffstat пуст'}",
        f"проверено: {checked or 'не указано'}",
        ("чего я не знала при закрытии: " + "; ".join(unknowns)) if unknowns else "",
        lesson_note,
        evidence_note,
        submission,
    ]).strip(), 16000)


def finish(task_id: str, title: str = "", review: str = "", checked: str = "",
           submit: bool = True) -> str:
    try:
        # Замок берётся ПЕРВЫМ и только потом идут удалённые вызовы осмотра. Иначе
        # (проверено пробой) занятый замок всё равно отказывает, но три RPC в демон
        # уже оплачены — а 26.07 таких заходов по одной задаче было пять подряд, и
        # демон в этот момент как раз горел. Держать осмотр под замком стало можно
        # потому, что замок больше не переживает ни смерть держателя, ни его молчание;
        # долгие шаги осмотра объявляются лизингом внутри _finish_survey.
        with _mutation_lock(task_id, timeout=120) as beat:
            survey = _finish_survey(task_id, beat=beat)
            if survey.get("error"):
                return str(survey["error"])
            if survey.get("blocked"):
                return str(survey["blocked"])
            beat()          # осмотр кончился — дальше снова мерится тишина, не работа
            return _finish_unlocked(task_id, title=title, review=review, checked=checked,
                                    submit=submit, survey=survey, beat=beat)
    except TimeoutError as exc:
        return f"Finish не начался: {exc}. Другой worker ещё фиксирует изменение."


def abandon(task_id: str, reason: str = "", checked: str = "") -> str:
    """Закрыть запись задачи, ничего не интегрируя и ничего не восстанавливая.

    ⚠ 28.07. До этой правки закрыть задачу с недостижимым корнем было НЕЧЕМ. `finish`
    упирался в `_task_root` ещё в осмотре и возвращал одну строку про корень; `reconcile_run`
    отвечал `RunNotFound`, потому что id задачи — не id прогона. Праксис 17:02 честно
    попробовала оба пути, оба отказали, и она закрыла задачу словами в дневнике — реестр
    остался с «active»/«lost» навсегда. Отсутствие выхода из тупика — это тоже ограничение,
    и оно было молчаливым.

    Это НЕ мягкий finish: ветка и worktree не трогаются (их может не быть вовсе), диффы не
    собираются, урок пишется как отказ. Оставленная задача остаётся читаемой: `abandoned`
    отличимо и от `done`, и от `lost`, потому что `lost` ставит жнец, а `abandoned` — она."""
    task = get(task_id)
    if not task:
        return f"нет coding-задачи {task_id}"
    reason = str(reason or "").strip()
    if not reason:
        return ("Нужна причина: abandon закрывает запись НАВСЕГДА и без интеграции. "
                "Одна фраза о том, что проверено и почему продолжать нечего.")
    if task.get("status") in {"done", "abandoned"}:
        return f"{task_id} уже закрыта со статусом {task.get('status')}."
    _, root, root_err = _task_root(task_id)
    previous = str(task.get("status") or "")
    task["status"] = "abandoned"
    task["finished"] = _now()
    task["updated"] = _now()
    task["review"] = reason
    task["checked"] = str(checked or "").strip()
    task["abandoned_from"] = previous
    task["abandon_root_state"] = root_err or (f"корень на месте: {root}" if root else "")
    _save_task(task)
    _event(task_id, "task_abandoned", reason=reason[:1000], checked=str(checked or "")[:1000],
           previous_status=previous, root_state=task["abandon_root_state"][:500])
    try:
        forge_learning.record(
            STATE_DIR, task=task, root=root or Path(str(task.get("root") or ".")),
            events=_events(task_id, 500), changed=[], verification="",
            lesson=(f"Задача оставлена без интеграции. Причина: {reason}"
                    + (f" Состояние корня: {task['abandon_root_state']}"
                       if task["abandon_root_state"] else "")),
            regression=str(checked or ""), outcome="abandoned")
    except Exception:
        pass          # урок — приложение к закрытию, а не условие его законности
    tail = f" Состояние корня: {task['abandon_root_state']}" if task["abandon_root_state"] else ""
    return (f"{task_id} оставлена (было: {previous or 'без статуса'}). Ничего не "
            f"интегрировано и не восстановлено.{tail}")


def state_line() -> str:
    """Short factual line for Praxis STATE; no model and no cached guesses."""
    tasks = []
    if TASKS_DIR.is_dir():
        for path in TASKS_DIR.glob("*/task.json"):
            task = _read_json(path)
            if isinstance(task, dict):
                tasks.append(task)
    active = [t for t in tasks if t.get("status") in {"active", "submitted"}]
    agents: list[dict] = []
    jobs: list[dict] = []
    for task in active:
        agents += _units(task["id"], "agents")
        jobs += _units(task["id"], "processes")
    arun = sum(1 for x in agents if x.get("status") == "running")
    adone = sum(1 for x in agents if x.get("status") == "done")
    aunknown = sum(1 for x in agents + jobs if x.get("status") == "unknown")
    prun = sum(1 for x in jobs if x.get("status") == "running")
    lost = [t for t in tasks if t.get("status") == "lost"]
    # Чужой репозиторий попадает сюда, только когда есть НЕОТДАННЫЙ факт: сдвиг, о
    # котором ей ещё не сказали, или слепота наблюдения. Постоянная строка сделала бы
    # forge «активным» в её блоке состояния навсегда — это была бы уже неправда.
    upstream = ""
    try:
        upstream = upstream_line()
    except Exception:
        log.debug("upstream_line упал", exc_info=True)
    if not active and not lost and not arun and not adone and not prun:
        return upstream
    line = (f"coding: задач {len(active)}, субагентов running {arun}/done {adone}, "
            f"процессов running {prun}")
    if upstream:
        line += "; " + upstream
    if aunknown:
        # «unknown» в блоке состояния — не шум, а разница между «работает» и
        # «номер занят, доказать нечем» (легаси-юниты без метки рождения).
        line += f", юнитов с недоказуемой живостью {aunknown}"
    if lost:
        line += (f"; потеряны из виду {len(lost)} "
                 f"({', '.join(str(t.get('id')) for t in lost[:3])}) — не закрыты, "
                 "продолжаются тем же id")
    return line
