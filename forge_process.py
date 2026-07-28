"""Out-of-line supervisor for a long-running Praxis Forge command."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import process_liveness


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _write(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _terminate_tree(proc: subprocess.Popen) -> None:
    """Terminate the command and every descendant, not merely its shell wrapper."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, text=True, timeout=15)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run(request_path: Path) -> int:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    directory = request_path.parent
    result_path = directory / "result.json"
    state_path = directory / "state.json"
    log_path = directory / "command.log"
    started = time.monotonic()
    try:
        with log_path.open("a", encoding="utf-8") as log:
            kwargs = {"shell": True, "cwd": str(request["cwd"]), "stdout": log,
                      "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "text": True}
            if os.name == "nt":
                kwargs["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                           | getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                kwargs["start_new_session"] = True
            proc = subprocess.Popen(str(request["command"]), **kwargs)
            # Метка рождения снимается ЗДЕСЬ, пока ребёнок заведомо наш и ещё не пожат:
            # позже номер уже ничего не доказывает. Без неё forge.process(action="stop")
            # шлёт killpg по голому child_pid, а в контейнере номера переиспользуются
            # мгновенно (26.07 /proc/10 после рестарта занял новый python) — то есть
            # «останови мою команду» может убить постороннего.
            state = {"status": "running", "child_pid": proc.pid,
                     "child_started_at": process_liveness.process_started_at(proc.pid),
                     "supervisor_pid": os.getpid(),
                     "supervisor_started_at": process_liveness.process_started_at(os.getpid()),
                     "started": _now()}
            _write(state_path, state)
            timeout = int(request.get("timeout") or 0)
            try:
                code = proc.wait(timeout=(timeout or None))
                status = "done" if code == 0 else "failed"
            except subprocess.TimeoutExpired:
                _terminate_tree(proc)
                code, status = -1, "timed_out"
            # Раньше state.json навсегда оставался «running» с живым child_pid: команда
            # давно кончилась, номер отдали другому, а «останови» всё ещё целилась в него.
            # Номер и метку не стираем — они нужны, чтобы сказать правду о том, кого
            # НЕ трогаем; меняем только статус.
            _write(state_path, {**state, "status": status, "child_running": False,
                                "finished": _now()})
        _write(result_path, {"status": status, "code": code, "finished": _now(),
                            "duration_s": round(time.monotonic() - started, 3),
                            "log": str(log_path)})
        return 0 if status == "done" else 1
    except Exception as exc:
        _write(result_path, {"status": "error", "error": f"{type(exc).__name__}: {exc}",
                            "finished": _now(), "duration_s": round(time.monotonic() - started, 3)})
        return 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    raise SystemExit(run(Path(args.request)))
