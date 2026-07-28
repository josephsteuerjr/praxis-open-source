"""Typed root host capabilities with before/after evidence and visible recovery receipts.

Raw ``op.run`` remains the sovereign escape hatch.  These verbs exist because structured
observation and automatic rollback make power more precise, not because they decide what Praxis
is allowed to do.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import grp
import shlex
import shutil
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path

import advisor
import hostrecovery


_SYSTEMCTL_ACTIONS = {"status", "start", "stop", "restart", "reload", "enable", "disable",
                      "is-active", "is-enabled", "daemon-reload"}
_SYSTEMCTL_MUTATING = {"start", "stop", "restart", "reload", "enable", "disable"}
_DOCKER_ACTIONS = {"ps", "inspect", "logs", "restart", "stop", "start", "up", "down",
                   "pull", "compose"}
_DOCKER_MUTATING = {"restart", "stop", "start", "up", "down", "pull", "compose"}
_CRITICAL_UNITS = {"ssh", "sshd", "docker", "praxis-serverd"}
_CRITICAL_CONTAINERS = {"praxis", "praxis-mailbot", "praxis-serverapp"}
_CRITICAL_PATH_PREFIXES = ("/etc/ssh/", "/etc/nftables", "/etc/docker/", "/etc/systemd/system/praxis-")


def _protected_error(subject: str) -> dict:
    return {"ok": False, "error": f"owner-configured protected root: {subject}"}


def _protected_name(value: str) -> bool:
    """Apply an explicit owner root policy to related unit/container names."""
    return advisor.command_touches_protected(str(value or ""))


def _protected_path(path: Path) -> bool:
    """Follow existing symlinks before checking an explicit protected-root policy."""
    try:
        value = str(path.resolve(strict=False))
    except OSError:
        value = str(path.absolute())
    return advisor.is_protected(value)


def configure(state: Path) -> None:
    hostrecovery.configure(state)


def armed_recoveries() -> list[dict]:
    return hostrecovery.armed()


def _loud(result: dict, receipt: dict | None = None, note: str = "") -> dict:
    """Совет и взведённый откат — В ТЕКСТ ответа, а не только в структурные поля.

    Клиент печатает ей `text` (и не печатает `advice`), а из `recovery` берёт только
    `deadline_epoch` голым числом. Раз единственный надёжный канал — `text`, важное встаёт
    в его начало, где его не срежет кап.
    """
    head: list[str] = []
    if receipt:
        result["recovery"] = receipt
        line = hostrecovery.notice(receipt)
        if line:
            result["recovery_notice"] = line
            head.append(line)
    if note:
        result["advice"] = note
        head.append("советник: " + note)
    if head:
        result["text"] = "\n".join([*head, str(result.get("text") or "")]).strip()
    return result


def _sh(argv: list[str], timeout: int = 120, input_text: str | None = None,
        cwd: Path | None = None) -> tuple[int, str]:
    try:
        result = subprocess.run(argv, input=input_text, capture_output=True, text=True,
                                errors="replace", timeout=timeout,
                                cwd=str(cwd) if cwd else None)
        return result.returncode, ((result.stdout or "") + (result.stderr or "")).strip()
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


def _unit_snapshot(unit: str) -> dict:
    code, output = _sh(["systemctl", "show", unit, "-p",
                        "ActiveState,SubState,UnitFileState,LoadState,MainPID,ExecMainStatus"], 20)
    row = {"exit": code}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            row[key] = value
    return row


def _arm_unit(unit: str, action: str, delay: int, before: dict) -> dict | None:
    short = unit.removesuffix(".service")
    if short not in _CRITICAL_UNITS or action not in {"stop", "restart", "reload", "disable"}:
        return None
    quoted = shlex.quote(unit)
    rollback = []
    unit_state = str(before.get("UnitFileState") or "")
    if unit_state == "enabled":
        rollback.append(f"systemctl enable {quoted}")
    elif unit_state == "disabled":
        rollback.append(f"systemctl disable {quoted}")
    was_active = before.get("ActiveState") == "active"
    rollback.append(f"systemctl {'start' if was_active else 'stop'} {quoted}")
    verify = (f"systemctl is-active --quiet {quoted}" if was_active else
              f"! systemctl is-active --quiet {quoted}")
    return hostrecovery.arm("systemctl", "; ".join(rollback), delay=delay,
                            description=f"restore critical unit {unit} after {action}",
                            verify_command=verify)


def systemctl(action: str, unit: str = "", timeout: int = 120,
              recover_after: int = 120) -> dict:
    action = str(action or "").strip().lower()
    if action not in _SYSTEMCTL_ACTIONS:
        return {"ok": False, "error": "action: " + " | ".join(sorted(_SYSTEMCTL_ACTIONS))}
    if action == "daemon-reload":
        code, output = _sh(["systemctl", "daemon-reload"], timeout)
        return {"ok": code == 0, "verb": "systemctl", "action": action,
                "exit": code, "text": output}
    unit = str(unit or "").strip()
    if not unit:
        return {"ok": False, "error": "unit is required"}
    if _protected_name(unit):
        return _protected_error(unit)
    before = _unit_snapshot(unit)
    receipt = (_arm_unit(unit, action, int(recover_after or 120), before)
               if action in _SYSTEMCTL_MUTATING else None)
    code, output = _sh(["systemctl", action, unit], timeout)
    after = _unit_snapshot(unit)
    result = {"ok": code == 0, "verb": "systemctl", "action": action, "unit": unit,
              "exit": code, "text": output[:12000], "before": before, "after": after}
    note = advisor.advise(unit=unit) if action in _SYSTEMCTL_MUTATING else ""
    return _loud(result, receipt, note)


def _docker_snapshot(name: str) -> dict:
    if not name:
        code, output = _sh(["docker", "ps", "-a", "--format", "{{.Names}}|{{.Status}}|{{.Image}}"], 30)
        return {"exit": code, "rows": output.splitlines()[:200]}
    code, output = _sh(["docker", "inspect", "-f",
                        "{{.State.Status}}|{{.State.Running}}|{{.RestartCount}}|{{.Image}}", name], 20)
    if code != 0:
        return {"exists": False, "exit": code}
    parts = output.split("|")
    return {"exists": True, "status": parts[0] if parts else "?",
            "running": parts[1] if len(parts) > 1 else "?",
            "restart_count": parts[2] if len(parts) > 2 else "?",
            "image": parts[3] if len(parts) > 3 else "?"}


def _compose_snapshot(cwd: Path) -> dict:
    code, output = _sh(["docker", "compose", "ps", "--format", "json"], 30, cwd=cwd)
    return {"exit": code, "cwd": str(cwd), "text": output[:20000]}


def docker(action: str, name: str = "", args: str = "", timeout: int = 300,
           recover_after: int = 120, cwd: str = "") -> dict:
    action = str(action or "").strip().lower()
    if action not in _DOCKER_ACTIONS:
        return {"ok": False, "error": "action: " + " | ".join(sorted(_DOCKER_ACTIONS))}
    name = str(name or "").strip()
    if action == "ps":
        snap = _docker_snapshot("")
        rows = [row for row in (snap.get("rows") or []) if not _protected_name(row)]
        return {"ok": snap.get("exit") == 0, "verb": "docker", "action": action,
                "text": "\n".join(rows)}
    if _protected_name(name) or _protected_name(args):
        return _protected_error(name or args)
    if action == "inspect":
        code, output = _sh(["docker", "inspect", name], timeout)
        return {"ok": code == 0, "verb": "docker", "action": action,
                "exit": code, "text": output[:24000]}
    if action == "logs":
        code, output = _sh(["docker", "logs", "--tail", "300", name], timeout)
        return {"ok": code == 0, "verb": "docker", "action": action,
                "exit": code, "text": output[-20000:]}

    # Compose is explicitly rooted.  Running it from the daemon's own lib directory is both
    # surprising and dangerous; the caller must name the project directory it intends to mutate.
    if action in {"compose", "up", "down"}:
        place = Path(str(cwd or "")).expanduser()
        if not place.is_absolute() or not place.is_dir():
            return {"ok": False, "error": "docker compose requires an existing absolute cwd"}
        if _protected_path(place):
            return _protected_error(str(place))
        tokens = shlex.split(args)
        if action == "compose":
            if not tokens:
                return {"ok": False, "error": "compose args must begin with a subcommand"}
            subaction = tokens[0]
            argv = ["docker", "compose", *tokens]
        else:
            subaction = action
            argv = ["docker", "compose", action, *([name] if name else []), *tokens]
        before = _compose_snapshot(place)
        receipt = None
        if subaction in {"down", "stop", "restart"}:
            service = f" {shlex.quote(name)}" if name and subaction != "down" else ""
            receipt = hostrecovery.arm(
                "docker-compose",
                f"cd {shlex.quote(str(place))} && docker compose up -d{service}",
                delay=int(recover_after or 120),
                description=f"restore compose project {place} after {subaction}",
                verify_command=(f"cd {shlex.quote(str(place))} && "
                                "test -n \"$(docker compose ps --status running -q)\""),
            )
        code, output = _sh(argv, timeout, cwd=place)
        after = _compose_snapshot(place)
        result = {"ok": code == 0, "verb": "docker", "action": action,
                  "subaction": subaction, "name": name, "cwd": str(place),
                  "exit": code, "text": output[:16000], "before": before, "after": after}
        note = advisor.advise(container=name) or advisor.advise(command=" ".join(argv))
        return _loud(result, receipt, note)

    before = _docker_snapshot(name)
    receipt = None
    if name in _CRITICAL_CONTAINERS and action in {"stop", "restart", "down"}:
        receipt = hostrecovery.arm(
            "docker", f"docker start {shlex.quote(name)}", delay=int(recover_after or 120),
            description=f"restart critical container {name} after {action}",
            verify_command=f"test \"$(docker inspect -f '{{{{.State.Running}}}}' {shlex.quote(name)})\" = true",
        )
    argv = ["docker", action, *([name] if name else []), *shlex.split(args)]
    code, output = _sh(argv, timeout)
    after = _docker_snapshot(name)
    result = {"ok": code == 0, "verb": "docker", "action": action, "name": name,
              "exit": code, "text": output[:16000], "before": before, "after": after}
    note = advisor.advise(container=name) if action in _DOCKER_MUTATING else ""
    return _loud(result, receipt, note)


def _pkg_manager() -> str:
    return next((name for name in ("apt-get", "dnf", "yum", "pacman") if shutil.which(name)), "")


def _pkg_snapshot(names: list[str]) -> dict:
    if shutil.which("dpkg-query"):
        rows = {}
        for name in names:
            code, output = _sh(["dpkg-query", "-W", "-f=${Status}|${Version}", name], 20)
            rows[name] = {"installed": code == 0 and "install ok installed" in output,
                          "version": output.rsplit("|", 1)[-1] if code == 0 else ""}
        return rows
    return {name: {"installed": None, "version": ""} for name in names}


def pkg(action: str, names: str = "", args: str = "", timeout: int = 900) -> dict:
    action = str(action or "query").strip().lower()
    packages = shlex.split(str(names or ""))
    manager = _pkg_manager()
    if action == "query":
        return {"ok": True, "verb": "pkg", "action": action, "manager": manager,
                "after": _pkg_snapshot(packages)}
    if action not in {"update", "install", "remove", "upgrade"}:
        return {"ok": False, "error": "action: query | update | install | remove | upgrade"}
    if not manager:
        return {"ok": False, "error": "no supported package manager"}
    before = _pkg_snapshot(packages)
    if manager == "apt-get":
        mapping = {"update": ["update"], "install": ["install", "-y", *packages],
                   "remove": ["remove", "-y", *packages], "upgrade": ["upgrade", "-y", *packages]}
    elif manager in {"dnf", "yum"}:
        mapping = {"update": ["makecache"], "install": ["install", "-y", *packages],
                   "remove": ["remove", "-y", *packages], "upgrade": ["upgrade", "-y", *packages]}
    else:
        mapping = {"update": ["-Sy"], "install": ["-S", "--noconfirm", *packages],
                   "remove": ["-R", "--noconfirm", *packages], "upgrade": ["-Syu", "--noconfirm"]}
    code, output = _sh([manager, *mapping[action], *shlex.split(args)], timeout)
    result = {"ok": code == 0, "verb": "pkg", "action": action, "manager": manager,
              "packages": packages, "exit": code, "text": output[-20000:],
              "before": before, "after": _pkg_snapshot(packages),
              # Собственный срок глагола: он один из немногих, кто честно длиннее потолка руки,
              # и до сих пор был известен только из кода.
              "limits": {"timeout_s": int(timeout), "output_tail_chars": 20000}}
    if code == -1 and "Timeout" in output:
        result["text"] = (f"[{manager} не уложился в {int(timeout)}с и был убит — состояние пакетов "
                          f"НЕИЗВЕСТНО, сверься с before/after]\n" + result["text"])
    return _loud(result, None, advisor.advise(command=f"{manager} {action} {' '.join(packages)}"))


def _file_snapshot(path: Path) -> dict:
    try:
        info = path.lstat()
    except OSError:
        return {"exists": False}
    return {"exists": True, "type": "dir" if path.is_dir() else "symlink" if path.is_symlink() else "file",
            "mode": oct(stat.S_IMODE(info.st_mode)), "uid": info.st_uid, "gid": info.st_gid,
            "size": info.st_size, "mtime_ns": info.st_mtime_ns,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else ""}


def _critical_path(path: Path) -> bool:
    value = str(path.resolve())
    return any(value == prefix.rstrip("/") or value.startswith(prefix) for prefix in _CRITICAL_PATH_PREFIXES)


def file(action: str, path: str, target: str = "", content: str = "", mode: str = "",
         owner: str = "", recover_after: int = 120) -> dict:
    action = str(action or "stat").strip().lower()
    source = Path(path).expanduser()
    if not source.is_absolute():
        return {"ok": False, "error": "path must be absolute"}
    if _protected_path(source):
        return _protected_error(str(source))
    destination = Path(target).expanduser() if target else None
    if action in {"copy", "move"} and destination is None:
        return {"ok": False, "error": "target must be absolute"}
    if destination is not None:
        if not destination.is_absolute():
            return {"ok": False, "error": "target must be absolute"}
        if _protected_path(destination):
            return _protected_error(str(destination))
    before = _file_snapshot(source)
    if action == "stat":
        return {"ok": True, "verb": "file", "action": action, "path": str(source), "after": before}
    allowed = {"write", "copy", "move", "remove", "mkdir", "chmod", "chown"}
    if action not in allowed:
        return {"ok": False, "error": "action: stat|write|copy|move|remove|mkdir|chmod|chown"}
    backup = ""
    if action != "copy" and source.exists() and source.is_file():
        directory = hostrecovery.RECEIPTS / "file-backups"
        directory.mkdir(parents=True, exist_ok=True)
        backup_path = directory / (hashlib.sha256(str(source).encode()).hexdigest()[:12] + "-" +
                                   uuid.uuid4().hex[:10] + "-" + source.name)
        shutil.copy2(source, backup_path)
        backup = str(backup_path)
    receipt = None
    if _critical_path(source) and action != "copy":
        rollback = ""
        if backup:
            rollback = f"cp --preserve=all {shlex.quote(backup)} {shlex.quote(str(source))}"
        elif not before.get("exists") and action == "write":
            rollback = f"rm -f {shlex.quote(str(source))}"
        elif not before.get("exists") and action == "mkdir":
            rollback = f"rmdir {shlex.quote(str(source))}"
        elif before.get("exists") and action in {"chmod", "chown"}:
            previous_mode = str(before.get("mode") or "0o755").removeprefix("0o")
            rollback = (f"chmod {shlex.quote(previous_mode)} "
                        f"{shlex.quote(str(source))}; chown {int(before.get('uid') or 0)}:"
                        f"{int(before.get('gid') or 0)} {shlex.quote(str(source))}")
        if rollback:
            receipt = hostrecovery.arm(
                "file", rollback, delay=int(recover_after or 120),
                description=f"restore critical file {source}")
    try:
        if action == "write":
            source.parent.mkdir(parents=True, exist_ok=True)
            tmp = source.with_name(source.name + f".{os.getpid()}.tmp")
            tmp.write_text(content, encoding="utf-8")
            if before.get("exists"):
                os.chmod(tmp, int(str(before["mode"]), 8))
            os.replace(tmp, source)
        elif action == "copy":
            assert destination is not None  # validated before any filesystem work
            shutil.copy2(source, destination)
        elif action == "move":
            assert destination is not None  # validated before any filesystem work
            os.replace(source, destination)
        elif action == "remove":
            if source.is_dir() and not source.is_symlink():
                return {"ok": False, "error": "typed remove does not recurse; use a host operation explicitly"}
            source.unlink()
        elif action == "mkdir":
            source.mkdir(parents=True, exist_ok=True)
        elif action == "chmod":
            os.chmod(source, int(mode, 8))
        elif action == "chown":
            user, _, group = owner.partition(":")
            uid = int(user) if user.isdigit() else pwd.getpwnam(user).pw_uid
            gid = (int(group) if group.isdigit() else grp.getgrnam(group).gr_gid) if group else -1
            os.chown(source, uid, gid)
    except Exception as exc:  # noqa: BLE001
        # Таймер взводится ДО правки: если правка упала, откат всё равно сработает — молчать
        # об этом нельзя, иначе «ошибка» и «через 120с ещё и откат» сольются в одно.
        return _loud({"ok": False, "error": f"{type(exc).__name__}: {exc}", "before": before,
                      "backup": backup, "text": ""}, receipt)
    result = {"ok": True, "verb": "file", "action": action, "path": str(source),
              "target": target, "before": before, "after": _file_snapshot(source),
              "backup": backup, "text": ""}
    return _loud(result, receipt, advisor.advise(path=str(source)))


def net(action: str, args: str = "", content: str = "", recover_after: int = 120) -> dict:
    action = str(action or "status").strip().lower()
    if action == "status":
        outputs = {}
        for name, argv in (("addresses", ["ip", "-brief", "address"]),
                           ("routes", ["ip", "route"]),
                           ("listeners", ["ss", "-lntup"]),
                           ("dns", ["resolvectl", "status"])):
            code, output = _sh(argv, 30)
            outputs[name] = {"exit": code, "text": output[:12000]}
        return {"ok": True, "verb": "net", "action": action, "after": outputs}
    if action == "nft-list":
        code, output = _sh(["nft", "list", "ruleset"], 60)
        return {"ok": code == 0, "verb": "net", "action": action, "exit": code,
                "text": output[:30000]}
    if action == "sshd-test":
        code, output = _sh(["sshd", "-t"], 60)
        return {"ok": code == 0, "verb": "net", "action": action, "exit": code, "text": output}
    if action == "sshd-reload":
        before = _unit_snapshot("ssh")
        code, output = _sh(["sshd", "-t"], 60)
        if code == 0:
            code, output2 = _sh(["systemctl", "reload", "ssh"], 60)
            output = (output + "\n" + output2).strip()
        return {"ok": code == 0, "verb": "net", "action": action, "exit": code,
                "text": output, "before": before, "after": _unit_snapshot("ssh"),
                "advice": advisor.advise(unit="ssh")}
    if action == "nft-apply":
        backup_dir = hostrecovery.RECEIPTS / "nft-backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"rules-{os.getpid()}.nft"
        code, current = _sh(["nft", "list", "ruleset"], 60)
        if code != 0:
            return {"ok": False, "error": current}
        backup.write_text(current + "\n", encoding="utf-8")
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".nft", delete=False) as fh:
            fh.write(content)
            candidate = fh.name
        try:
            check, check_out = _sh(["nft", "--check", "-f", candidate], 60)
            if check != 0:
                return {"ok": False, "error": check_out}
            receipt = hostrecovery.arm(
                "nft", f"nft -f {shlex.quote(str(backup))}", delay=int(recover_after or 120),
                description="restore nft ruleset", verify_command="nft list ruleset >/dev/null")
            code, output = _sh(["nft", "-f", candidate], 60)
        finally:
            try:
                os.unlink(candidate)
            except OSError:
                pass
        return _loud({"ok": code == 0, "verb": "net", "action": action, "exit": code,
                      "text": output,
                      "before": {"sha256": hashlib.sha256(current.encode()).hexdigest()},
                      "after": {"applied_bytes": len(content.encode())}},
                     receipt, advisor.advise(command="nft apply"))
    return {"ok": False, "error": "action: status | nft-list | nft-apply | sshd-test | sshd-reload"}


def reboot(action: str = "status", delay_minutes: int = 1, verify_command: str = "") -> dict:
    action = str(action or "status").strip().lower()
    if action == "status":
        return {"ok": True, **hostrecovery.reconcile_boot()}
    if action == "cancel":
        return hostrecovery.cancel_reboot()
    if action in {"schedule", "now"}:
        minutes = 0 if action == "now" else max(1, int(delay_minutes or 1))
        recorded = hostrecovery.record_reboot(minutes, verify_command)
        if not recorded.get("ok"):
            return recorded
        argv = ["systemctl", "reboot"] if action == "now" else ["shutdown", "-r", f"+{minutes}"]
        code, output = _sh(argv, 30)
        return {"ok": code == 0, "verb": "reboot", "action": action, "exit": code,
                "text": output, **recorded,
                "advice": "reboot записан с boot_id и будет верифицирован broker'ом после старта"}
    return {"ok": False, "error": "action: status | schedule | now | cancel"}


def confirm(receipt_id: str) -> dict:
    return hostrecovery.confirm(receipt_id)


def dispatch(verb: str, args: dict) -> dict:
    values = {key: value for key, value in (args or {}).items() if value is not None}
    if verb == "host.systemctl":
        return systemctl(**values)
    if verb == "host.docker":
        return docker(**values)
    if verb == "host.pkg":
        return pkg(**values)
    if verb == "host.file":
        return file(**values)
    if verb == "host.net":
        return net(**values)
    if verb == "host.reboot":
        return reboot(**values)
    if verb == "host.confirm":
        return confirm(str(values.get("receipt_id") or ""))
    return {"ok": False, "error": f"unknown host verb {verb}"}
