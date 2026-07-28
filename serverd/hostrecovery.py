"""Visible recovery receipts for load-bearing host operations.

Receipts are not permission gates.  Typed operations arm a systemd timer that restores the last
known state unless Praxis confirms the observed after-state.  Raw root exec remains available, so
the mechanism amplifies recoverability without secretly deciding what she may do.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import hostproc


STATE = Path(os.environ.get("PRAXIS_SERVERD_STATE") or "/opt/praxis-serverd/state")
RECEIPTS = STATE / "recovery"
REBOOT = STATE / "reboot.json"
REBOOT_HISTORY = STATE / "reboot-history.json"


def configure(state: Path) -> None:
    global STATE, RECEIPTS, REBOOT, REBOOT_HISTORY
    STATE = Path(state)
    RECEIPTS = STATE / "recovery"
    REBOOT = STATE / "reboot.json"
    REBOOT_HISTORY = STATE / "reboot-history.json"
    RECEIPTS.mkdir(parents=True, exist_ok=True)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _write(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def notice(receipt: dict | None) -> str:
    """Громкая строка про взведённый откат.

    Механизм не убран — он полезен. Убрано молчание: она правила `daemon.json`, получала
    `ok:true`, уходила, а через 120с таймер откатывал правку, и в её памяти оставалось
    «применено». Теперь срок и способ подтверждения стоят в самом ответе.
    """
    if not isinstance(receipt, dict) or not receipt:
        return ""
    receipt_id = str(receipt.get("id") or "?")
    if receipt.get("status") == "arm_failed":
        return (f"ВНИМАНИЕ: таймер отката НЕ взведён ({receipt.get('error') or 'причина неизвестна'}) — "
                f"автоматического возврата не будет, состояние держится только твоей правкой.")
    if receipt.get("status") != "armed":
        return ""
    delay = int(receipt.get("delay_s") or 0)
    deadline = int(receipt.get("deadline_epoch") or 0)
    stamp = (dt.datetime.fromtimestamp(deadline, dt.timezone.utc).isoformat(timespec="seconds")
             if deadline else "?")
    return (f"⏱ ОТКАЧУ ЭТО ЧЕРЕЗ {delay}с (в {stamp}), если не подтвердишь: "
            f"host_ctl verb=confirm receipt_id={receipt_id}. "
            f"Откат выполнит: `{str(receipt.get('rollback_command') or '')[:400]}`. "
            f"Подтверждение — не формальность: без него правка исчезнет сама.")


def arm(kind: str, rollback_command: str, *, delay: int = 120,
        description: str = "", verify_command: str = "") -> dict:
    delay = max(15, min(int(delay or 120), 3600))
    receipt_id = "recover-" + uuid.uuid4().hex[:10]
    unit = f"praxis-{receipt_id}"
    receipt = {"id": receipt_id, "kind": str(kind), "status": "armed", "created": _now(),
               "deadline_epoch": int(time.time()) + delay, "delay_s": delay,
               "delay_source": "recover_after (по умолчанию 120с)",
               "rollback_command": str(rollback_command), "verify_command": str(verify_command),
               "description": str(description), "unit": unit}
    receipt["notice"] = notice(receipt)
    _write(RECEIPTS / f"{receipt_id}.json", receipt)
    command = rollback_command
    command += (f"; rc=$?; printf '%s' \"$rc\" > {str(RECEIPTS / (receipt_id + '.exit'))}; exit $rc")
    try:
        result = subprocess.run([
            "systemd-run", "--quiet", "--unit", unit, "--on-active", f"{delay}s",
            "--property", "Type=oneshot", "/bin/sh", "-c", command,
        ], capture_output=True, text=True, errors="replace", timeout=30)
        if result.returncode != 0:
            receipt["status"] = "arm_failed"
            receipt["error"] = ((result.stdout or "") + (result.stderr or ""))[-4000:]
            receipt["notice"] = notice(receipt)
            _write(RECEIPTS / f"{receipt_id}.json", receipt)
    except Exception as exc:  # noqa: BLE001
        receipt["status"] = "arm_failed"
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        receipt["notice"] = notice(receipt)
        _write(RECEIPTS / f"{receipt_id}.json", receipt)
    return receipt


def armed() -> list[dict]:
    """Взведённые прямо сейчас откаты — чтобы их было видно и БЕЗ обращения к конкретной расписке."""
    rows = []
    now = time.time()
    for row in status().get("receipts") or []:
        if row.get("status") != "armed":
            continue
        rows.append({"id": row.get("id"), "kind": row.get("kind"),
                     "description": row.get("description"),
                     "seconds_left": max(0, int(float(row.get("deadline_epoch") or 0) - now)),
                     "rollback_command": row.get("rollback_command"),
                     "notice": row.get("notice") or notice(row)})
    return rows


def confirm(receipt_id: str) -> dict:
    path = RECEIPTS / f"{str(receipt_id or '')}.json"
    receipt = _read(path, {})
    if not isinstance(receipt, dict) or not receipt:
        return {"ok": False, "error": f"unknown recovery receipt {receipt_id}"}
    exit_path = RECEIPTS / f"{receipt_id}.exit"
    if exit_path.exists() and receipt.get("status") == "armed":
        receipt.update(status="rolled_back", rollback_exit=exit_path.read_text(
            encoding="utf-8", errors="replace"))
        _write(path, receipt)
    if receipt.get("status") == "confirmed":
        return {"ok": True, "receipt": receipt, "reused": True}
    if receipt.get("status") != "armed":
        return {"ok": False, "error": f"recovery is {receipt.get('status')}",
                "receipt": receipt}
    # Verify while the timer is still armed.  A failed check must never silently disarm the
    # rollback that the check was meant to protect.
    unit = str(receipt.get("unit") or "")
    verify = ""
    verify_code = None
    if receipt.get("verify_command"):
        try:
            result = subprocess.run(["/bin/sh", "-c", str(receipt["verify_command"])],
                                    capture_output=True, text=True, errors="replace", timeout=60)
            verify_code = result.returncode
            verify = ((result.stdout or "") + (result.stderr or ""))[-8000:]
        except Exception as exc:  # noqa: BLE001
            verify_code = -1
            verify = f"{type(exc).__name__}: {exc}"
        if verify_code != 0:
            receipt.update(last_verify=_now(), verify_exit=verify_code, verify_output=verify)
            _write(path, receipt)
            return {"ok": False, "error": "verification failed; recovery remains armed",
                    "receipt": receipt}

    output = ""
    stop_code = 0
    if unit:
        try:
            # --on-active creates the .service only when the timer fires.  Stopping that not-yet-
            # loaded service makes systemctl return 5 even though the timer was cancelled, so the
            # cancellation target is deliberately the timer alone.
            result = subprocess.run(["systemctl", "stop", f"{unit}.timer"],
                                    capture_output=True, text=True, errors="replace", timeout=30)
            stop_code = result.returncode
            output = ((result.stdout or "") + (result.stderr or "")).strip()
        except Exception as exc:  # noqa: BLE001
            stop_code = -1
            output = f"{type(exc).__name__}: {exc}"
    missing = any(token in output.casefold() for token in
                  ("not loaded", "not found", "could not be found"))
    if stop_code != 0 and missing and not exit_path.exists():
        # Idempotent retry after the timer was already stopped and garbage-collected.
        stop_code = 0
        output = (output + "\nrecovery timer was already absent").strip()
    if unit and stop_code == 0:
        try:
            state = subprocess.run(["systemctl", "is-active", f"{unit}.service"],
                                   capture_output=True, text=True, errors="replace", timeout=10)
            service_state = (state.stdout or "").strip()
        except Exception:  # noqa: BLE001
            service_state = ""
        if service_state in {"active", "activating"}:
            for _ in range(50):
                if exit_path.exists():
                    break
                time.sleep(.1)
            if not exit_path.exists():
                receipt.update(last_confirm=_now(), confirm_exit=-1,
                               confirm_output="rollback service is already running")
                _write(path, receipt)
                return {"ok": False, "error": "rollback is already running", "receipt": receipt}
    if exit_path.exists():
        receipt.update(status="rolled_back", rollback_exit=exit_path.read_text(
            encoding="utf-8", errors="replace"))
        _write(path, receipt)
        return {"ok": False, "error": "recovery already ran", "receipt": receipt}
    if stop_code != 0:
        receipt.update(last_confirm=_now(), confirm_exit=stop_code, confirm_output=output,
                       verify_exit=verify_code, verify_output=verify)
        _write(path, receipt)
        return {"ok": False, "error": "could not disarm recovery timer", "receipt": receipt}
    receipt.update(status="confirmed", confirmed=_now(), confirm_exit=stop_code,
                   confirm_output=output, verify_exit=verify_code, verify_output=verify)
    _write(path, receipt)
    return {"ok": True, "receipt": receipt}


def status(receipt_id: str = "") -> dict:
    if receipt_id:
        row = _read(RECEIPTS / f"{receipt_id}.json", {})
        return {"ok": bool(row), "receipt": row, "error": "" if row else "not found"}
    rows = []
    if RECEIPTS.is_dir():
        for path in sorted(RECEIPTS.glob("recover-*.json"), key=lambda item: item.stat().st_mtime,
                           reverse=True)[:100]:
            row = _read(path, {})
            if row:
                exit_path = path.with_suffix(".exit")
                if exit_path.exists() and row.get("status") == "armed":
                    row["status"] = "rolled_back"
                    row["rollback_exit"] = exit_path.read_text(encoding="utf-8", errors="replace")
                    _write(path, row)
                rows.append(row)
    return {"ok": True, "receipts": rows}


def _reboot_history() -> list[dict]:
    rows = _read(REBOOT_HISTORY, [])
    return rows if isinstance(rows, list) else []


def reboot_allowed() -> dict:
    cutoff = time.time() - 15 * 60
    recent = [row for row in _reboot_history() if float(row.get("epoch") or 0) >= cutoff]
    return {"ok": len(recent) < 3, "recent_15m": len(recent),
            "reason": "reboot circuit breaker: 3 requests in 15m" if len(recent) >= 3 else ""}


def record_reboot(delay_minutes: int = 1, verify_command: str = "") -> dict:
    allowed = reboot_allowed()
    if not allowed["ok"]:
        return allowed
    row = {"id": "reboot-" + uuid.uuid4().hex[:10], "requested": _now(),
           "epoch": time.time(), "pre_boot_id": hostproc.boot_id(), "delay_minutes": delay_minutes,
           "verify_command": str(verify_command), "status": "scheduled"}
    history = _reboot_history()
    history.append(row)
    _write(REBOOT_HISTORY, history[-100:])
    _write(REBOOT, row)
    return {"ok": True, "reboot": row}


def cancel_reboot() -> dict:
    result = subprocess.run(["shutdown", "-c"], capture_output=True, text=True,
                            errors="replace", timeout=20)
    row = _read(REBOOT, {})
    if isinstance(row, dict) and row:
        row.update(status="cancelled", cancelled=_now())
        _write(REBOOT, row)
    return {"ok": result.returncode == 0, "exit": result.returncode,
            "text": ((result.stdout or "") + (result.stderr or "")).strip(), "reboot": row}


def reconcile_boot() -> dict:
    row = _read(REBOOT, {})
    if not isinstance(row, dict) or not row:
        return {"pending": False, "boot_id": hostproc.boot_id()}
    current = hostproc.boot_id()
    if row.get("status") == "scheduled" and current and current != row.get("pre_boot_id"):
        row.update(status="booted", booted=_now(), post_boot_id=current)
        if row.get("verify_command"):
            result = subprocess.run(["/bin/sh", "-c", str(row["verify_command"])],
                                    capture_output=True, text=True, errors="replace", timeout=120)
            row["verify_exit"] = result.returncode
            row["verify_output"] = ((result.stdout or "") + (result.stderr or ""))[-10000:]
            row["status"] = "verified" if result.returncode == 0 else "verify_failed"
        _write(REBOOT, row)
    return {"pending": row.get("status") == "scheduled", "reboot": row,
            "boot_id": current, "circuit": reboot_allowed()}
