"""hostproc — супервизор долгоживущих root-команд praxis-serverd.

Модель как в forge_process.py (контейнер), с усилением под критику панели:
  * ОТДЕЛЬНЫЙ супервизор-процесс (`python3 hostproc.py --supervise <dir>`) делает wait() и
    пишет result.json с реальным exit — детач-ребёнок сам по себе exit-кода не отдаёт, и без
    супервизора «завершился успешно» неотличимо от «убит» (обе → lost). Супервизор detached
    (start_new_session), поэтому переживает рестарт демона;
  * pid-identity-safe: state.json несёт pid+pgid+boot_id+/proc starttime — после рестарта/ребута
    не переусыновляем и не killpg переиспользованный pid;
  * ремни ресурсов на ребёнке: RLIMIT_NPROC/CORE через preexec; кап конкуррентности — в демоне.

Linux-only (setsid, killpg, /proc). Каждый юнит — директория с request/state/result/command.log.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import resource
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

_SUPERVISORS: dict[int, subprocess.Popen] = {}
_SUPERVISOR_LOCK = threading.Lock()

# ── пределы этого файла ───────────────────────────────────────────────────────
# Закон 2: молчаливых пределов нет. Оба числа названы вслух в тексте, который она
# получает от op.stop (см. f-строки в `_kill_tree` и `stop`) — и оба обязаны стоять
# в манифесте рельсов (`rails.registry()`; это другой файл, вынесено в отчёт).
TERM_GRACE_SEC = 2.0    # сколько ждём добровольного выхода после SIGTERM, прежде чем SIGKILL
START_GRACE_SEC = 2.0   # сколько ждём, пока супервизор опубликует номер группы, прежде чем сознаться

# ⚠ 27.07: `"error"` пишет сам же `_supervise` (упавший супервизор: «no space left on
# device» и подобное), но в терминальные его не включали — и первый же op.stop затирал
# улику надгробием «остановлено». Улика об упавшем супервизоре — это ИСХОД, а не «ещё бежит».
TERMINAL_STATUSES = frozenset({"done", "failed", "timed_out", "stopped", "error"})


def _reap_supervisor(proc: subprocess.Popen) -> None:
    try:
        proc.wait()
    finally:
        with _SUPERVISOR_LOCK:
            _SUPERVISORS.pop(int(proc.pid), None)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _starttime(pid: int) -> str:
    try:
        data = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        tail = data[data.rfind(")") + 1:].split()
        return tail[19] if len(tail) > 19 else ""   # поле 22 = индекс 19 после comm
    except (OSError, IndexError):
        return ""


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def same_process(pid: int, starttime: str, rec_boot: str) -> bool:
    if rec_boot and boot_id() and rec_boot != boot_id():
        return False
    if not pid_alive(pid):
        return False
    return bool(starttime) and _starttime(pid) == starttime


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def spawn(unit_dir: Path, command: str, cwd: str, timeout: int = 0,
          env: dict | None = None, limits: dict | None = None) -> dict:
    """Запустить detached команду под супервизором. Возвращает начальный state. Не блокирует."""
    unit_dir.mkdir(parents=True, exist_ok=True)
    _write(unit_dir / "request.json", {"command": command, "cwd": cwd,
                                       "timeout": int(timeout or 0),
                                       "log": str(unit_dir / "command.log"),
                                       "env": env or {}, "limits": limits or {}, "created": _now()})
    sup_log = open(unit_dir / "supervisor.log", "ab")
    try:
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--supervise", str(unit_dir)],
            cwd=str(Path(__file__).resolve().parent), stdout=sup_log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent)},
        )
    finally:
        sup_log.close()
    with _SUPERVISOR_LOCK:
        _SUPERVISORS[int(proc.pid)] = proc
    threading.Thread(target=_reap_supervisor, args=(proc,), daemon=True).start()
    # супервизор сам пропишет state.json с child_pid; тут фиксируем факт запуска
    _write(unit_dir / "state.json", {"status": "starting", "supervisor_pid": proc.pid,
                                     "supervisor_starttime": _starttime(proc.pid),
                                     "boot_id": boot_id(), "started": time.time()})
    return {"status": "starting", "supervisor_pid": proc.pid}


def poll(unit_dir: Path) -> dict:
    """Актуальный статус: result.json (терминал+exit) или живость супервизора/ребёнка."""
    result = _read(unit_dir / "result.json")
    if result:
        return result
    state = _read(unit_dir / "state.json")
    if not state:
        return {"status": "unknown"}
    child = int(state.get("child_pid") or 0)
    sup = int(state.get("supervisor_pid") or 0)
    rec_boot = str(state.get("boot_id") or "")
    if child and same_process(child, str(state.get("child_starttime") or ""), rec_boot):
        return {**state, "status": "running"}
    if sup and same_process(sup, str(state.get("supervisor_starttime") or ""), rec_boot):
        return {**state, "status": "finishing" if child else "starting"}
    # ни ребёнка, ни result — процесс исчез без отметки (упал/убит/ребут)
    return {**state, "status": "lost"}


def _secs(value: float) -> str:
    """Срок словами так, как он есть: 2 → «2», 0.2 → «0.2». Закон 2 — предел назван честно."""
    return f"{float(value):g}"


def _outcome_text(result: dict, *, after_signal: bool = False) -> str:
    """Честный пересказ уже записанного исхода. Никаких «остановлено» поверх правды.

    `after_signal` — сигнал мы всё-таки послали, и приписка «останавливать нечего»
    была бы вторым враньём подряд: останавливать было чего, просто исход записали не мы.
    """
    status = str(result.get("status") or "")
    if status == "error":
        return ("останавливать нечего: супервизор этой операции сам упал — "
                f"{result.get('error') or 'без текста ошибки'}. Что успела сделать команда, "
                "я не знаю; улику не затираю, назвать это «остановлено» было бы враньём")
    code = result.get("exit")
    if code is not None:
        if after_signal:
            return f"исход записал сам супервизор: {status}, код выхода {code}"
        return (f"уже завершён сам: {status}, код выхода {code} — останавливать нечего, "
                "надгробие не пишу")
    note = str(result.get("note") or "")
    head = "исход: " if after_signal else "уже завершён: "
    return head + status + (f" ({note})" if note else "")


def _proc_cmdline(pid: int) -> str:
    """Командная строка процесса одной строкой (/proc/<pid>/cmdline, NUL → пробел)."""
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as fh:
            return fh.read().decode("utf-8", "replace").replace("\0", " ").strip()
    except (OSError, ValueError):
        return ""


def _supervisor_holds(state: dict, unit_dir: Path) -> bool:
    """Жив ли НАШ супервизор этого юнита — самое сильное доказательство тождества группы.

    Пока супервизор не сделал wait(), номер его ребёнка держит ядро: даже умерший
    ребёнок остаётся зомби, номер не освобождается и достаться постороннему не может.
    Аналог живого Popen в `forge._kill_identity`.
    """
    sup = int(state.get("supervisor_pid") or 0)
    if not sup or not pid_alive(sup):
        return False
    born = str(state.get("supervisor_starttime") or "")
    if born:
        return same_process(sup, born, str(state.get("boot_id") or ""))
    # Легаси-юниты без метки на проде есть (7 из 450). Доказываем командной строкой:
    # супервизор запускается как `python hostproc.py --supervise <путь юнита>`, путь уникален.
    # ⚠ 27.07: в откаченной версии этот путь был НЕОБЯЗАТЕЛЬНЫМ и вырождался в Path("."),
    # у которого `.name == ""`, а `"" in cmdline` истинно всегда — доказательство
    # превращалось в «доказано всегда». Пустой и короткий путь не доказывает ничего.
    needle = str(unit_dir)
    return len(needle) >= 8 and needle in _proc_cmdline(sup)


def _kill_doubt(pgid: int, state: dict, unit_dir: Path) -> str:
    """Пусто = группа доказанно наша. Иначе — текст честного отказа.

    ⚠ 27.07, стенд ревью: рут сносил ЧУЖУЮ безлидерную группу в 3 сценариях из 3
    (двойная вилка `sh -c "sleep 300 & exit 0"` под start_new_session: лидер вышел,
    /proc/<pgid> пуст, группа жива за счёт потомка). Корень был в ПОРЯДКЕ: ранний
    выход «метки рождения нет → считаем своим» стоял выше отвода по boot_id и сверки
    метки, то есть «не смогла доказать» читалось как «доказано, что наш». Здесь
    наоборот: сначала доказательства, недоказанное — отказ.

    Это не забор: отказ называет, чем посмотреть и как снять руками. Демон живёт
    на хосте от рута, рядом с praxis/mailbot/serverapp/relay/ollama и прочими
    сервисами Егора — цена ошибки здесь не «операция не остановилась», а «погашен
    чужой сервис».
    """
    rec_boot = str(state.get("boot_id") or "")
    here = boot_id()
    if rec_boot and here and rec_boot != here:
        return (f"не трогаю группу {pgid}: юнит заводился в ПРОШЛУЮ загрузку хоста "
                f"(boot_id {rec_boot[:8]}… ≠ {here[:8]}…), после ребута номера розданы заново — "
                f"под этим номером сейчас почти наверняка чужой процесс. Посмотреть, кто там: "
                f"cat /proc/{pgid}/cmdline; если это всё-таки твоя команда — сними отдельной "
                f"op.start `kill -TERM -{pgid}`")
    born = str(state.get("child_starttime") or "")
    now_born = _starttime(pgid)
    # Несовпавшая метка — это не «не доказали», это ПРЯМАЯ улика чужого, поэтому она стоит
    # выше любых доказательств: даже живой супервизор её не перебивает.
    if born and now_born and born != now_born:
        return (f"не трогаю группу {pgid}: этот номер занят ДРУГИМ процессом (метка рождения "
                f"{now_born} ≠ {born}) — мой умер, а по его номеру живёт посторонний. "
                f"Посмотреть, кто там: cat /proc/{pgid}/cmdline; если нужно снять именно его — "
                f"отдельной op.start `kill -TERM -{pgid}`")
    if born and now_born and born == now_born:
        return ""
    if _supervisor_holds(state, unit_dir):
        return ""
    why = (f"лидер группы уже вышел (/proc/{pgid} пуст), живого супервизора у юнита нет"
           if born else
           "метки рождения у этого юнита нет (заводился до 27.07), живого супервизора тоже нет")
    return (f"не убила группу {pgid}: {why} — доказать, что оставшиеся в ней процессы мои, "
            f"нечем. Ни жив, ни мёртв не утверждаю и надгробие не пишу. Посмотреть, кто там: "
            f"cat /proc/{pgid}/cmdline и `ps -o pgid,pid,args -g {pgid}`; если это точно твоя "
            f"команда — сними отдельной op.start `kill -TERM -{pgid}`")


def stop(unit_dir: Path) -> str:
    result = _read(unit_dir / "result.json")
    if result.get("status") in TERMINAL_STATUSES:
        return _outcome_text(result)

    def settled() -> bool:
        return _read(unit_dir / "result.json").get("status") in TERMINAL_STATUSES

    state = _read(unit_dir / "state.json")
    pgid = int(state.get("pgid") or state.get("child_pid") or 0)
    # Супервизор публикует номер группы не мгновенно. Раньше op.stop сразу после op.start
    # молча ничего не останавливал (закон 4: потерять её намерение молча — худшее), поэтому
    # ждём — но каждый шаг сверяемся с result.json: быстрая команда успевает отработать
    # сама, и тогда останавливать уже нечего.
    deadline = time.monotonic() + START_GRACE_SEC
    while not pgid and time.monotonic() < deadline:
        time.sleep(.05)
        if settled():
            return _outcome_text(_read(unit_dir / "result.json"))
        state = _read(unit_dir / "state.json")
        pgid = int(state.get("pgid") or state.get("child_pid") or 0)
    if not pgid:
        return (f"не остановлен: за {_secs(START_GRACE_SEC)}с супервизор не опубликовал номер "
                "группы — я не знаю, что останавливать, и не знаю, запустилась ли команда "
                "вообще. Посмотри op.poll: если статус lost — супервизор умер, не начав; "
                "если running — позови op.stop ещё раз")
    msg, acted = _kill_tree(pgid, state, unit_dir, settled=settled)
    fresh = _read(unit_dir / "result.json")
    if fresh.get("status") in TERMINAL_STATUSES:
        # ⚠ 27.07: ровно здесь терялся код выхода её команды (10 раз из 10 на стенде):
        # супервизор писал правду `{"status":"failed","exit":7}`, а stop() накрывал её
        # надгробием `{"status":"stopped","note":"уже завершён"}`. Правду не затираем.
        # Если сигнала не было — недоказанное тождество уже не при чём: операция кончилась сама.
        return f"{msg}; {_outcome_text(fresh, after_signal=True)}" if acted else _outcome_text(fresh)
    if not acted:
        # Отказ доказать тождество — не исход операции. Написать сюда «stopped» значило бы
        # соврать дважды: и про смерть команды, и про то, что это сделали мы.
        return msg
    _write(unit_dir / "result.json", {"status": "stopped", "note": msg, "finished": _now()})
    return msg


def _kill_tree(pgid: int, state: dict, unit_dir: Path, *, proven: str = "",
               settled: Callable[[], bool] | None = None) -> tuple[str, bool]:
    """Снять группу. Возвращает (текст для неё, можно ли честно записать исход «остановлено»).

    `state` и `unit_dir` ОБЯЗАТЕЛЬНЫЕ: доказательства тождества нельзя делать
    необязательными — необязательное доказательство вырождается в «доказано всегда».
    `proven` — для вызывающего, у которого тождество уже на руках (живой Popen в супервизоре).
    """
    if not pgid:
        return "pid ещё не появился", False
    if not proven:
        doubt = _kill_doubt(pgid, state, unit_dir)
        if doubt:
            return doubt, False
    # Последняя сверка перед сигналом: команда могла закончиться, пока мы доказывали
    # тождество. Тогда сигнала не будет вовсе — и надгробия тоже (см. `stop`).
    if settled is not None and settled():
        return "уже завершён сам, пока я проверяла — сигнал не посылала", False
    born = str(state.get("child_starttime") or "") or _starttime(pgid)
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return "уже завершён: группы с этим номером нет", False
    except Exception as exc:  # noqa: BLE001
        return f"не остановлен: {type(exc).__name__}: {exc}", False
    deadline = time.monotonic() + TERM_GRACE_SEC
    while time.monotonic() < deadline:
        if settled is not None and settled():
            return "остановлен (SIGTERM послан)", True
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return "остановлен (вышла по SIGTERM)", True
        time.sleep(.05)
    # Окно TERM→KILL — второе место, где номер может уйти постороннему: наш умер от TERM,
    # ядро освободило номер, и голое «жива ли группа?» отправило бы SIGKILL уже чужому.
    # Здесь опора двойная: (1) всё окно мы опрашивали группу каждые 50мс и она ни разу не
    # была пуста, а пока в группе есть хоть один процесс, ядро держит её номер занятым;
    # (2) если лидер жив, сверяем его метку рождения ещё раз.
    now_born = _starttime(pgid)
    if born and now_born and now_born != born:
        # Второй сигнал ушёл бы уже постороннему. Умерла ли от первого моя группа — не знаю,
        # поэтому и «остановлено» не пишу: исход тут неизвестен, а не отрицателен.
        return (f"SIGTERM послан, SIGKILL — нет: за эти {_secs(TERM_GRACE_SEC)}с номер {pgid} "
                f"успел достаться другому процессу (метка {now_born} ≠ {born}). Жива ли ещё моя "
                f"команда — не знаю; посмотреть: `ps -o pgid,pid,args -g {pgid}`", False)
    try:
        os.killpg(pgid, signal.SIGKILL)
        return (f"остановлен (SIGKILL: за {_secs(TERM_GRACE_SEC)}с после SIGTERM группа "
                "не вышла сама)", True)
    except ProcessLookupError:
        return "остановлен (вышла по SIGTERM)", True
    except Exception as exc:  # noqa: BLE001
        # ⚠ Здесь стояло `True` — и надгробие «status: stopped» ложилось поверх текста
        # «не остановлен: PermissionError». Провалившийся SIGKILL исходом не является:
        # группа, скорее всего, ЖИВА, и сказать про неё «остановлена» — то самое враньё.
        return f"не остановлен: {type(exc).__name__}: {exc}", False


def _preexec(limits: dict | None = None):
    limits = limits or {}
    os.setsid()
    for res, lim in ((resource.RLIMIT_CORE, (0, 0)),):
        try:
            resource.setrlimit(res, lim)
        except (ValueError, OSError):
            pass
    try:
        _, hard = resource.getrlimit(resource.RLIMIT_NPROC)
        cap = max(64, int(limits.get("nproc") or 2048))
        resource.setrlimit(resource.RLIMIT_NPROC,
                           (cap, hard if hard != resource.RLIM_INFINITY else cap * 4))
    except (ValueError, OSError):
        pass
    requested = (
        (resource.RLIMIT_NOFILE, int(limits.get("nofile") or 4096)),
        (resource.RLIMIT_AS, int(limits.get("as_mb") or 0) * 1024 * 1024),
        (resource.RLIMIT_FSIZE, int(limits.get("fsize_mb") or 0) * 1024 * 1024),
        (resource.RLIMIT_CPU, int(limits.get("cpu_seconds") or 0)),
    )
    for res, soft in requested:
        if soft <= 0:
            continue
        try:
            _, hard = resource.getrlimit(res)
            chosen = min(soft, hard) if hard != resource.RLIM_INFINITY else soft
            resource.setrlimit(res, (chosen, hard))
        except (ValueError, OSError):
            pass


def _supervise(unit_dir: Path) -> int:
    """Тело супервизора: запустить команду, дождаться, записать result с реальным exit."""
    req = _read(unit_dir / "request.json")
    log_path = Path(req.get("log") or (unit_dir / "command.log"))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    try:
        with open(log_path, "ab") as log:
            proc = subprocess.Popen(
                ["/bin/sh", "-c", str(req.get("command") or "true")],
                cwd=str(req.get("cwd") or "/"), stdout=log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, preexec_fn=lambda: _preexec(req.get("limits") or {}), close_fds=True,
                env={**os.environ, **(req.get("env") or {})},
            )
            _write(unit_dir / "state.json", {
                "status": "running", "child_pid": proc.pid, "pgid": proc.pid,
                "supervisor_pid": os.getpid(), "boot_id": boot_id(),
                "supervisor_starttime": _starttime(os.getpid()),
                "child_starttime": _starttime(proc.pid), "started": time.time()})
            timeout = int(req.get("timeout") or 0)
            try:
                code = proc.wait(timeout=(timeout or None))
                status = "done" if code == 0 else "failed"
            except subprocess.TimeoutExpired:
                # Здесь тождество доказывать не нужно и нечем: живой Popen — само доказательство,
                # ядро держит номер ребёнка за нами, пока мы его не пожали.
                _kill_tree(proc.pid, {}, unit_dir, proven="живой Popen в супервизоре")
                code, status = -1, "timed_out"
        _write(unit_dir / "result.json", {"status": status, "exit": code, "finished": _now(),
                                          "duration_s": round(time.monotonic() - t0, 3),
                                          "log": str(log_path)})
        return 0 if status == "done" else 1
    except Exception as exc:  # noqa: BLE001
        _write(unit_dir / "result.json", {"status": "error", "error": f"{type(exc).__name__}: {exc}",
                                          "finished": _now(),
                                          "duration_s": round(time.monotonic() - t0, 3)})
        return 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--supervise", required=True)
    args = parser.parse_args()
    raise SystemExit(_supervise(Path(args.supervise)))
