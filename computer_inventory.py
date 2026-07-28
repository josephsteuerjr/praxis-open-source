"""Server-owned inventory snapshots gathered through the brainless Windows body."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path

import body_client


SCHEMA = "praxis.computer.inventory.v1"


def _base() -> Path:
    return Path(os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent)


def _dir(device: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "_.-" else "-" for ch in device).strip("-.") or "unknown"
    return _base() / "memory" / "computer" / "inventory" / safe


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_time(value: object) -> dt.datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def interval_hours() -> float:
    try:
        return max(0.0, float(os.getenv("PRAXIS_COMPUTER_INVENTORY_HOURS", "24")))
    except ValueError:
        return 24.0


def due(device_id: str = "", *, now: dt.datetime | None = None) -> bool:
    """Whether this device needs a fresh inventory snapshot.

    The hourly server clock calls this lightweight check.  A missing/offline
    body is not marked fresh, so the first tick after the PC returns catches up
    instead of waiting for a special night window.
    """
    hours = interval_hours()
    if hours <= 0:
        return False
    device = device_id or body_client.device_id()
    try:
        snapshot = json.loads((_dir(device) / "CURRENT.json").read_text(encoding="utf-8"))
        # Scheduling is server-owned.  The Windows clock is useful evidence but
        # must not postpone refresh forever when it is skewed into the future.
        previous = _parse_time(snapshot.get("observed_at") or snapshot.get("captured_at"))
        if previous is None:
            return True
    except (OSError, TypeError, ValueError):
        return True
    current = now or _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    return current.astimezone(dt.timezone.utc) - previous.astimezone(dt.timezone.utc) \
        >= dt.timedelta(hours=hours)


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def _snapshot_name(observed_at: str, text: str, directory: Path) -> str:
    observed = _parse_time(observed_at) or _utc_now()
    stem = observed.strftime("%Y%m%dT%H%M%S")
    if observed.microsecond:
        stem += f"{observed.microsecond:06d}"
    candidate = f"{stem}Z.json"
    path = directory / candidate
    if not path.exists() or path.read_text(encoding="utf-8", errors="replace") == text:
        return candidate
    # A second refresh inside the same clock tick remains a distinct canonical
    # snapshot instead of silently overwriting history.
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
    # '~' sorts after the plain ``Z.json`` form, so a same-tick collision is
    # also the newest snapshot for deterministic filename-based consumers.
    return f"{stem}Z~{digest}.json"


_SCRIPT = r"""
$ErrorActionPreference='Stop'
$utf8=New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding=$utf8
$OutputEncoding=$utf8
$os=Get-CimInstance Win32_OperatingSystem
$cs=Get-CimInstance Win32_ComputerSystem
$lastBoot=$null
if($os.LastBootUpTime){
  if($os.LastBootUpTime -is [datetime]){
    $lastBoot=$os.LastBootUpTime.ToUniversalTime().ToString('o')
  } else {
    $lastBoot=[Management.ManagementDateTimeConverter]::ToDateTime([string]$os.LastBootUpTime).ToUniversalTime().ToString('o')
  }
}
$tools=@('git','python','py','node','npm','cargo','rustc','dotnet','docker','wsl','code','pwsh','powershell') | ForEach-Object {
  $c=Get-Command $_ -ErrorAction SilentlyContinue
  if($c){[ordered]@{name=$_;path=$c.Source;version=([string]$c.Version)}}
}
$apps=@(
 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
 'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*'
) | ForEach-Object { Get-ItemProperty $_ -ErrorAction SilentlyContinue } | Where-Object DisplayName | Sort-Object DisplayName,DisplayVersion -Unique | Select-Object -First 600 | ForEach-Object {
 [ordered]@{name=$_.DisplayName;version=$_.DisplayVersion;publisher=$_.Publisher}
}
$roots=@($env:USERPROFILE,(Join-Path $env:USERPROFILE 'Desktop'),(Join-Path $env:USERPROFILE 'Documents'),(Join-Path $env:USERPROFILE 'Downloads')) | Where-Object {Test-Path -LiteralPath $_}
$projects=@()
foreach($root in $roots){
  Get-ChildItem -LiteralPath $root -Directory -Force -ErrorAction SilentlyContinue | Select-Object -First 120 | ForEach-Object {
    if((Test-Path (Join-Path $_.FullName '.git')) -or (Test-Path (Join-Path $_.FullName 'Cargo.toml')) -or (Test-Path (Join-Path $_.FullName 'pyproject.toml')) -or (Test-Path (Join-Path $_.FullName 'package.json'))){$projects+=$_.FullName}
  }
}
[ordered]@{
 schema='praxis.computer.inventory.payload.v1';captured_at=(Get-Date).ToUniversalTime().ToString('o');
 hostname=$cs.Name;user=$env:USERNAME;
 os=[ordered]@{caption=$os.Caption;version=$os.Version;build=$os.BuildNumber;architecture=$os.OSArchitecture;last_boot=$lastBoot};
 machine=[ordered]@{manufacturer=$cs.Manufacturer;model=$cs.Model;memory_bytes=[int64]$cs.TotalPhysicalMemory};
 volumes=@(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object {[ordered]@{name=$_.DeviceID;filesystem=$_.FileSystem;size_bytes=[int64]$_.Size;free_bytes=[int64]$_.FreeSpace}});
 tools=@($tools);apps=@($apps);known_roots=@($roots);project_roots=@($projects | Sort-Object -Unique)
} | ConvertTo-Json -Depth 6 -Compress
"""


def _render(snapshot: dict) -> Path:
    device = str(snapshot.get("device_id") or "unknown")
    payload = snapshot.get("payload") or {}
    os_row = payload.get("os") or {}
    machine = payload.get("machine") or {}
    identity = snapshot.get("identity") or {}
    def gib(value: object) -> str:
        try:
            return f"{int(value or 0) / (1024 ** 3):.1f} GiB"
        except (TypeError, ValueError):
            return "?"
    lines = [f"# Computer — {device}", "",
             "> Current inventory card. Canonical timestamped snapshots are in the adjacent inventory directory.", "",
             f"- Captured: {snapshot.get('captured_at')}",
             f"- Observed by server: {snapshot.get('observed_at')}",
             f"- Host: `{payload.get('hostname') or 'unknown'}`; user: `{payload.get('user') or 'unknown'}`",
             f"- Identity: `{identity.get('kind') or 'unknown'}` / `{identity.get('integrity') or 'unknown'}`; elevated={identity.get('elevated')}",
             f"- OS: {os_row.get('caption') or '?'} {os_row.get('version') or ''} build {os_row.get('build') or '?'} ({os_row.get('architecture') or '?'})",
             f"- Hardware: {machine.get('manufacturer') or '?'} {machine.get('model') or '?'}; RAM {gib(machine.get('memory_bytes'))}", "",
             "## Volumes", ""]
    for row in payload.get("volumes") or []:
        lines.append(f"- `{row.get('name')}` {row.get('filesystem') or '?'} — {gib(row.get('free_bytes'))} free / {gib(row.get('size_bytes'))}")
    lines += ["", "## Development and system tools", ""]
    for row in payload.get("tools") or []:
        lines.append(f"- `{row.get('name')}` {row.get('version') or ''} — `{row.get('path') or ''}`")
    lines += ["", "## Known roots", ""]
    for value in payload.get("known_roots") or []:
        lines.append(f"- `{value}`")
    lines += ["", "## Detected project roots", ""]
    roots = payload.get("project_roots") or []
    lines += [f"- `{value}`" for value in roots[:200]] or ["- None detected in the bounded scan."]
    lines += ["", "## Installed applications", ""]
    for row in (payload.get("apps") or [])[:400]:
        lines.append(f"- {row.get('name')} {row.get('version') or ''} ({row.get('publisher') or 'publisher unknown'})")
    lines.append("")
    path = _dir(device) / "CURRENT.md"
    _atomic(path, "\n".join(lines))
    return path


def refresh(device_id: str = "") -> dict:
    device = device_id or body_client.device_id()
    status = body_client.status_probe(timeout=20)
    if not status.get("ok"):
        return {"ok": False, "code": "body_offline", "error": status.get("error") or status.get("code")}
    result = body_client.run_argv(
        "powershell.exe", ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _SCRIPT],
        timeout=180,
        tail=1_000_000,
    )
    if not result.get("ok"):
        return {"ok": False, "code": "inventory_failed", "error": result.get("stderr_tail") or result.get("error") or result}
    raw = str(result.get("stdout_tail") or "").strip()
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return {"ok": False, "code": "bad_inventory", "error": f"{exc}: {raw[:500]}"}
    observed = _utc_now().isoformat(timespec="microseconds").replace("+00:00", "Z")
    captured = str(payload.get("captured_at") or observed)
    snapshot = {"schema": SCHEMA, "captured_at": captured, "observed_at": observed,
                "device_id": device,
                "identity": status.get("identity") or {}, "manifest": status.get("manifest") or {},
                "payload": payload}
    directory = _dir(device)
    text = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    path = directory / _snapshot_name(observed, text, directory)
    _atomic(path, text)
    _atomic(directory / "CURRENT.json", text)
    card = _render(snapshot)
    try:
        import computer_memory
        computer_memory.render_map()
    except Exception:
        pass
    try:
        import memory_catalog
        memory_catalog.rebuild()
    except Exception:
        pass
    return {"ok": True, "device_id": device, "snapshot": str(path.relative_to(_base())),
            "card": str(card.relative_to(_base())), "apps": len(payload.get("apps") or []),
            "projects": len(payload.get("project_roots") or [])}


if __name__ == "__main__":
    print(json.dumps(refresh(), ensure_ascii=False, indent=2))
