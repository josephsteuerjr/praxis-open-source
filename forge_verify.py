"""Detached verification-matrix runner for Praxis Forge.

The parent process writes a request, this supervisor runs independent checks with bounded
parallelism, and every check keeps a complete log.  The matrix therefore survives a Telegram
turn or a restart and can be inspected without trusting a summary.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import process_liveness


# Общий срок матрицы: он применяется к КАЖДОЙ проверке, которая пришла без своего.
# ⚠ 27.07, ревью. Здесь стоял отдельный `PRAXIS_VERIFY_HOST_DEADLINE_SEC` «на случай
# проверки совсем без предела» — и оказался рычагом, которого у неё нет: forge.verify
# всегда кладёт в запрос число, а `request.get("timeout") or 900` ниже добивает и ноль.
# Ветка не достигалась из её рук ни разу; зато настоящий предел — вот этот литерал 900 —
# именем не назывался вообще и молча переписывал её `coding_verify(timeout=0)`.
# Поэтому лишний рычаг убран, а действующий назван и вынесен в окружение (закон 2).
try:
    MATRIX_DEADLINE_SEC = int(float(os.getenv("PRAXIS_VERIFY_TIMEOUT_SEC") or 900))
except (TypeError, ValueError):
    MATRIX_DEADLINE_SEC = 900
if MATRIX_DEADLINE_SEC <= 0:
    MATRIX_DEADLINE_SEC = 900

# Совместимость на один шаг: имя ещё читает test_serverd_timeout.py (файл соседнего
# владельца). Убрать вместе с той ссылкой — своей роли у него больше нет.
DETACHED_HOST_DEADLINE_SEC = MATRIX_DEADLINE_SEC


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _safe_id(value: str, fallback: str) -> str:
    clean = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in str(value or ""))
    return clean.strip("-.")[:80] or fallback


def _run_one(index: int, check: dict, root: Path, directory: Path, default_timeout: int,
             state: dict, lock: threading.Lock, scope: str = "self", matrix_id: str = "",
             observation: dict | None = None) -> dict:
    cid = _safe_id(check.get("id", ""), f"check-{index + 1}")
    command = str(check.get("command") or "").strip()
    rel_cwd = str(check.get("cwd") or ".")
    cwd = root if scope == "windows" else (root / rel_cwd).resolve()
    if scope not in {"host", "windows"}:
        try:
            cwd.relative_to(root)
        except ValueError:
            return {"id": cid, "status": "error", "exit": -1, "duration_s": 0,
                    "error": f"cwd leaves task root: {rel_cwd}"}
    own_timeout = max(0, int(check.get("timeout") or 0))
    timeout = own_timeout or max(0, int(default_timeout or 0))
    log_path = directory / "logs" / f"{index + 1:02d}-{cid}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    deadline_s = timeout
    with lock:
        state["checks"][cid] = {"status": "running", "command": command, "started": _now(),
                                "log": str(log_path)}
        _write(directory / "state.json", state)
    try:
        if scope == "host":
            import serverd_client
            deadline_s = timeout or MATRIX_DEADLINE_SEC
            wait_s = max(60, deadline_s + 60)
            result = serverd_client.call(
                "op.run", {"root_value": str(root), "command": command, "cwd": rel_cwd,
                           "timeout": deadline_s}, timeout=wait_s,
                request_id=f"verify-{matrix_id}-{cid}",
            )
            body = str(result.get("body") or result.get("error") or "")
            if not own_timeout:
                # Свой срок проверки не назначала — значит подставлен общий, и он обязан
                # быть виден в её же логе, а не только в коде (закон 2).
                body += (f"\n[срок] у этой проверки своего предела не было — применён общий "
                         f"срок матрицы {deadline_s}с (PRAXIS_VERIFY_TIMEOUT_SEC); демон "
                         f"остановит команду сам, клиент ждёт {wait_s}с.")
            log_path.write_text(body, encoding="utf-8", errors="replace")
            code = int(result.get("exit") if result.get("exit") is not None else -1)
            status = "passed" if result.get("ok") and code == 0 else (
                "timed_out" if result.get("status") == "timed_out" else "failed"
            )
            remote_log = str(result.get("log") or "")
        elif scope == "windows":
            import body_client
            task = dict(observation or {})
            task.setdefault("root", str(root))
            with body_client.observation_context(task):
                result = body_client.run_result(str(root), command, cwd=rel_cwd, timeout=timeout)
            body = str(result.get("body") or result.get("error") or "")
            log_path.write_text(body, encoding="utf-8", errors="replace")
            terminal = result.get("result") or {}
            code = int(terminal.get("exit_code") if terminal.get("exit_code") is not None else -1)
            status = "passed" if result.get("ok") and code == 0 else (
                "timed_out" if result.get("status") == "timed_out" else "failed"
            )
            remote_log = str(terminal.get("stdout_log") or "")
        else:
            remote_log = ""
            with log_path.open("w", encoding="utf-8", errors="replace") as log:
                kwargs = {"shell": True, "cwd": str(cwd), "stdout": log,
                          "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "text": True}
                if os.name == "nt":
                    kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                else:
                    kwargs["start_new_session"] = False  # supervisor group; one stop kills all
                proc = subprocess.Popen(command, **kwargs)
                try:
                    code = proc.wait(timeout=(timeout or None))
                    status = "passed" if code == 0 else "failed"
                except subprocess.TimeoutExpired:
                    try:
                        if os.name == "nt":
                            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                           capture_output=True, timeout=15)
                        else:
                            proc.terminate()
                            proc.wait(timeout=2)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    code, status = -1, "timed_out"
        result = {"id": cid, "status": status, "exit": code,
                  "duration_s": round(time.monotonic() - started, 3), "command": command,
                  "kind": check.get("kind", "check"), "source": check.get("source", ""),
                  "scope": check.get("scope", ""), "log": str(log_path),
                  # Срок, под которым проверка реально шла: «0» = предела не было. Без этого
                  # `timed_out` в отчёте не отличить от «упало само».
                  "deadline_s": deadline_s,
                  "remote_log": remote_log}
    except Exception as exc:  # noqa: BLE001
        result = {"id": cid, "status": "error", "exit": -1,
                  "duration_s": round(time.monotonic() - started, 3), "command": command,
                  "error": f"{type(exc).__name__}: {exc}", "log": str(log_path)}
    with lock:
        state["checks"][cid] = result
        _write(directory / "state.json", state)
    return result


def run(request_path: Path) -> int:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    directory = request_path.parent
    scope = str(request.get("scope") or "self")
    root = Path(request["root"]) if scope == "windows" else Path(request["root"]).resolve()
    checks = [row for row in (request.get("checks") or []) if str(row.get("command") or "").strip()]
    parallel = max(1, min(int(request.get("max_parallel") or 2), 8))
    timeout = max(0, int(request.get("timeout") or MATRIX_DEADLINE_SEC))
    # Номер супервизора без метки рождения — это приглашение убить постороннего:
    # forge.verify(action="stop") шлёт killpg по нему, а номера в контейнере
    # переиспользуются первым же спавном.
    state = {"status": "running", "started": _now(), "supervisor_pid": os.getpid(),
             "supervisor_started_at": process_liveness.process_started_at(os.getpid()),
             "matrix_deadline_s": timeout,
             "deadline_note": (f"проверке без своего срока даётся {timeout}с "
                               f"(PRAXIS_VERIFY_TIMEOUT_SEC)"),
             "checks": {}}
    _write(directory / "state.json", state)
    lock = threading.Lock()
    started = time.monotonic()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = [pool.submit(_run_one, i, row, root, directory, timeout, state, lock,
                                   scope, str(request.get("id") or directory.name), request)
                       for i, row in enumerate(checks)]
            results = [future.result() for future in futures]
        failed = [row for row in results if row.get("status") != "passed"]
        final = {"status": "passed" if not failed else "failed", "finished": _now(),
                 "duration_s": round(time.monotonic() - started, 3), "checks": results,
                 "passed": len(results) - len(failed), "failed": len(failed),
                 # Срок виден в самом отчёте, а не только в логах отдельных проверок:
                 # `timed_out` без названного предела читается как «упало само».
                 "matrix_deadline_s": timeout,
                 "supervisor_pid": os.getpid(),
                 "supervisor_started_at": state.get("supervisor_started_at", "")}
        _write(directory / "result.json", final)
        return 0 if not failed else 1
    except BaseException as exc:  # noqa: BLE001
        final = {"status": "error", "finished": _now(),
                 "duration_s": round(time.monotonic() - started, 3),
                 "error": f"{type(exc).__name__}: {exc}"}
        _write(directory / "result.json", final)
        return 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(143)))
    raise SystemExit(run(Path(args.request)))
