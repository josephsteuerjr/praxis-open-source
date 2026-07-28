"""Versioned read/control model for the PASS 24 Praxis mini-app.

The browser is a view and an owner control surface.  It does not host a model,
memory, scheduler or decision loop.  All facts below are projections of the
canonical server runtime and all mutations go through the same durable ledgers
used by Telegram, Forge and the Windows body.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import mimetypes
import os
import platform
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import body_client
import computer_access
import owner_delivery
import praxis_device_auth
import run_manager
import run_context
import telegram_followups
import telegram_membership


SCHEMA = "praxis.app.snapshot.v1"
API_VERSION = "v1"
MAP_NAMES = ("PEOPLE", "ROOMS", "PROJECTS", "THREADS", "RUNS", "COMPUTERS")
RUN_CONTROL_ACTIONS = frozenset({"pause", "resume", "cancel"})


@dataclass(frozen=True, slots=True)
class BodyCapabilityPolicy:
    """Server authority/presentation metadata for one body capability.

    The Rust manifest says what a connected body can execute.  This table says
    what a mini-app principal may request and how a returned artifact is shown.
    It is intentionally data, not another command dispatch tree.
    """

    scope: str
    read_only: bool = False
    execution: str = "any"
    timeout: float = 60.0
    presentation: str = ""


BODY_CAPABILITIES: dict[str, BodyCapabilityPolicy] = {
    "body.status": BodyCapabilityPolicy("computer.read", read_only=True),
    "fs.list": BodyCapabilityPolicy("computer.read", read_only=True),
    "fs.stat": BodyCapabilityPolicy("computer.read", read_only=True),
    "fs.read": BodyCapabilityPolicy("computer.files", read_only=True),
    "fs.search": BodyCapabilityPolicy("computer.files", read_only=True),
    "fs.diff": BodyCapabilityPolicy("computer.files", read_only=True),
    "fs.history": BodyCapabilityPolicy("computer.files", read_only=True),
    "fs.export": BodyCapabilityPolicy("computer.files", timeout=600, presentation="download"),
    "fs.import": BodyCapabilityPolicy("computer.files", timeout=600),
    "process.list": BodyCapabilityPolicy("computer.process", read_only=True),
    "process.status": BodyCapabilityPolicy("computer.process", read_only=True),
    "process.start": BodyCapabilityPolicy("computer.process"),
    "process.cancel": BodyCapabilityPolicy("computer.process"),
    "os.process.list": BodyCapabilityPolicy("computer.apps", read_only=True),
    "desktop.status": BodyCapabilityPolicy(
        "computer.apps", read_only=True, execution="interactive",
    ),
    "desktop.window.list": BodyCapabilityPolicy(
        "computer.apps", read_only=True, execution="interactive",
    ),
    "desktop.window.activate": BodyCapabilityPolicy("computer.apps", execution="interactive"),
    "desktop.input.perform": BodyCapabilityPolicy("computer.apps", execution="interactive"),
    "desktop.screen.capture": BodyCapabilityPolicy(
        "computer.apps", execution="interactive", presentation="image",
    ),
    "desktop.clipboard.read": BodyCapabilityPolicy(
        "computer.apps", read_only=True, execution="interactive",
    ),
    "desktop.clipboard.write": BodyCapabilityPolicy("computer.apps", execution="interactive"),
}

# Compatibility exports for older tests/operators; both are derived from the one registry.
BODY_ACTION_SCOPES = {name: policy.scope for name, policy in BODY_CAPABILITIES.items()}
BODY_READ_ONLY = frozenset(
    name for name, policy in BODY_CAPABILITIES.items() if policy.read_only
)


@dataclass(frozen=True, slots=True)
class BodyCommandAlias:
    capability: str
    arg_fields: tuple[str, ...]
    defaults: tuple[tuple[str, Any], ...] = ()


BODY_COMMAND_ALIASES: dict[tuple[str, str], BodyCommandAlias] = {
    ("process", "start"): BodyCommandAlias(
        "process.start", ("operation_id", "command", "cwd", "name"),
        (("shell", "power_shell"),),
    ),
    ("process", "cancel"): BodyCommandAlias(
        "process.cancel", ("operation_id", "command", "cwd", "name"),
    ),
    ("process", "status"): BodyCommandAlias(
        "process.status", ("operation_id", "command", "cwd", "name"),
    ),
    ("process", "list"): BodyCommandAlias(
        "process.list", ("operation_id", "command", "cwd", "name"),
    ),
    ("files", "list"): BodyCommandAlias("fs.list", ("path", "query", "offset", "limit")),
    ("files", "stat"): BodyCommandAlias("fs.stat", ("path", "query", "offset", "limit")),
    ("files", "read"): BodyCommandAlias("fs.read", ("path", "query", "offset", "limit")),
    ("files", "search"): BodyCommandAlias("fs.search", ("path", "query", "offset", "limit")),
    ("files", "export"): BodyCommandAlias("fs.export", ("path", "query", "offset", "limit")),
    ("desktop", "status"): BodyCommandAlias(
        "desktop.status", ("target", "hwnd", "x", "y", "width", "height", "name"),
    ),
    ("desktop", "windows"): BodyCommandAlias(
        "desktop.window.list", ("target", "hwnd", "x", "y", "width", "height", "name"),
    ),
    ("desktop", "capture"): BodyCommandAlias(
        "desktop.screen.capture", ("target", "hwnd", "x", "y", "width", "height", "name"),
        (("target", "desktop"),),
    ),
}
PWA_FILE_MAX_BYTES = max(
    1024 * 1024,
    min(int(os.environ.get("PRAXIS_PWA_FILE_MAX_BYTES", str(64 * 1024 * 1024))),
        256 * 1024 * 1024),
)
BODY_DEFINITIVE_FAILURES = frozenset({
    # These receipts are produced after the body has durably rejected or
    # terminally failed the exact request.  Transport/router/journal failures
    # are deliberately absent: retrying those with the same request id is the
    # reconciliation mechanism, not a new side effect.
    "bad_args", "id_conflict", "capability", "process_start",
})
BODY_DEFINITIVE_STATUSES = frozenset({"failed", "cancelled", "timed_out"})
BODY_IN_DOUBT_STATUSES = frozenset({"in_doubt"})
SERVER_OPERATION_RECEIPT_SCHEMA = "praxis.app.operation-receipt.v1"
SERVER_OPERATION_WAIT_SECONDS = 15.0
SERVER_OPERATION_LEASE_SECONDS = 30.0
SERVER_OPERATION_HEARTBEAT_SECONDS = 5.0
SERVER_OPERATION_MAX_RECEIPT_BYTES = 4 * 1024 * 1024


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _server_machine_identity() -> str:
    """Return a stable, non-secret host fingerprint for executor binding."""
    raw = ""
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
            ) as key:
                raw = str(winreg.QueryValueEx(key, "MachineGuid")[0]).strip()
        except (OSError, ValueError):
            raw = ""
    else:
        for candidate in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
            try:
                raw = candidate.read_text(encoding="ascii").strip()
            except OSError:
                continue
            if raw:
                break
    raw = raw or platform.node().strip()
    if not raw:
        return ""
    return hashlib.sha256(
        f"praxis-server-host-v1\0{raw}".encode("utf-8")
    ).hexdigest()


def _server_boot_identity() -> str:
    """Return an exact boot identity when the host exposes one."""
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                (r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management"
                 r"\PrefetchParameters"),
            ) as key:
                return f"windows:{int(winreg.QueryValueEx(key, 'BootId')[0])}"
        except (OSError, TypeError, ValueError):
            return ""
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii"
        ).strip()
    except OSError:
        return ""
    return f"linux:{value}" if value else ""


def _server_process_start_identity(pid: int) -> str:
    """Identify a process generation, so a reused PID is never accepted."""
    try:
        process_id = int(pid)
    except (TypeError, ValueError):
        return ""
    if process_id <= 0:
        return ""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            query_limited_information = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (
                wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
            )
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            )
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(
                query_limited_information, False, process_id,
            )
            if not handle:
                return ""
            try:
                created = wintypes.FILETIME()
                exited = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                if not kernel32.GetProcessTimes(
                    handle, ctypes.byref(created), ctypes.byref(exited),
                    ctypes.byref(kernel), ctypes.byref(user),
                ):
                    return ""
                ticks = (int(created.dwHighDateTime) << 32) | int(
                    created.dwLowDateTime
                )
                return f"windows-filetime:{ticks}" if ticks else ""
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, TypeError, ValueError):
            return ""
    try:
        data = Path(f"/proc/{process_id}/stat").read_text(encoding="ascii")
        tail = data[data.rfind(")") + 1:].split()
        start_ticks = tail[19] if len(tail) > 19 else ""
    except (OSError, IndexError):
        return ""
    return f"linux-ticks:{start_ticks}" if start_ticks else ""


def _server_process_namespace_identity(pid: int) -> str:
    if os.name == "nt":
        return "windows-global"
    try:
        return os.readlink(f"/proc/{int(pid)}/ns/pid")
    except (AttributeError, OSError, TypeError, ValueError):
        return ""


def _server_utc_after(seconds: float) -> str:
    return (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=max(0.1, seconds))
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _server_utc_epoch(value: object) -> float | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False, default=str))


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _mtime(path: Path) -> str:
    try:
        return dt.datetime.fromtimestamp(
            path.stat().st_mtime, dt.timezone.utc
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
    except OSError:
        return ""


def _small_error(exc: BaseException) -> dict:
    return {
        "type": type(exc).__name__,
        "message": str(exc)[:500],
    }


@dataclass(frozen=True, slots=True)
class Viewer:
    actor_id: str
    role: str
    scopes: tuple[str, ...]

    @property
    def owner(self) -> bool:
        return self.role == "owner"

    def may(self, scope: str) -> bool:
        return self.owner or scope in self.scopes

    @property
    def principal_id(self) -> str:
        prefix = "telegram" if self.role in {"owner", "trusted"} else self.role
        return f"{prefix}:{self.actor_id}"

    def public(self) -> dict:
        sections = ["now", "computer"]
        if self.owner or self.may("praxis.snapshot"):
            sections = [
                "now", "inbox", "runs", "computer", "memory", "telegram", "system"
            ]
        if self.owner:
            sections.append("trust")
        return {
            "actor_id": self.actor_id,
            "role": self.role,
            "scopes": list(self.scopes),
            "sections": sections,
            "can_delegate": self.owner,
        }


TicketValidator = Callable[[Viewer], Viewer | None]


@dataclass(frozen=True, slots=True)
class ArtifactTicketGrant:
    """A one-use, path-bound download capability; it grants no viewer scope."""

    viewer: Viewer
    run_id: str
    artifact_id: str
    presentation: str = "download"


class EventTickets:
    """Short-lived, one-use SSE tickets; Telegram initData never enters a URL."""

    def __init__(self, ttl_seconds: float = 45.0, per_principal: int = 4) -> None:
        self.ttl_seconds = max(5.0, float(ttl_seconds))
        self.per_principal = max(1, int(per_principal))
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, Viewer, TicketValidator | None]] = {}

    def issue(self, viewer: Viewer, *, validator: TicketValidator | None = None) -> dict:
        if viewer.role == "device" and validator is None:
            raise ValueError("device event tickets require current-state validation")
        token = secrets.token_urlsafe(32)
        expires = time.monotonic() + self.ttl_seconds
        with self._lock:
            self._prune_locked()
            same = [
                (key, value) for key, value in self._items.items()
                if value[1].principal_id == viewer.principal_id
            ]
            for key, _value in sorted(same, key=lambda item: item[1][0])[
                    :max(0, len(same) - self.per_principal + 1)]:
                self._items.pop(key, None)
            self._items[token] = (expires, viewer, validator)
        return {"ticket": token, "expires_in": int(self.ttl_seconds)}

    def consume(self, token: str) -> Viewer | None:
        with self._lock:
            self._prune_locked()
            row = self._items.pop(str(token or ""), None)
        if not row or row[0] < time.monotonic():
            return None
        return row[2](row[1]) if row[2] is not None else row[1]

    def _prune_locked(self) -> None:
        now = time.monotonic()
        for token, (expires, _viewer, _validator) in list(self._items.items()):
            if expires < now:
                self._items.pop(token, None)


class ArtifactTickets:
    """Short-lived capability URLs for native browser downloads."""

    def __init__(self, ttl_seconds: float = 90.0, per_principal: int = 1024) -> None:
        self.ttl_seconds = max(10.0, float(ttl_seconds))
        self.per_principal = max(1, int(per_principal))
        self._lock = threading.Lock()
        self._items: dict[
            str, tuple[float, Viewer, str, str, TicketValidator | None, str]
        ] = {}

    def issue(self, viewer: Viewer, run_id: str, artifact_id: str, *,
              validator: TicketValidator | None = None,
              presentation: str = "download") -> str:
        if viewer.role == "device" and validator is None:
            raise ValueError("device artifact tickets require current-state validation")
        if presentation not in {"download", "image"}:
            raise ValueError("unsupported artifact ticket presentation")
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = time.monotonic()
            self._items = {
                key: value for key, value in self._items.items() if value[0] >= now
            }
            same = [
                (key, value) for key, value in self._items.items()
                if value[1].principal_id == viewer.principal_id
            ]
            for key, _value in sorted(same, key=lambda item: item[1][0])[
                    :max(0, len(same) - self.per_principal + 1)]:
                self._items.pop(key, None)
            self._items[token] = (
                now + self.ttl_seconds, viewer, str(run_id), str(artifact_id), validator,
                presentation,
            )
        return token

    def authorize(self, token: str, run_id: str, artifact_id: str, *,
                  consume: bool = True) -> ArtifactTicketGrant | None:
        with self._lock:
            key = str(token or "")
            row = self._items.get(key)
            if consume and row is not None:
                self._items.pop(key, None)
        if not row or row[0] < time.monotonic():
            return None
        if row[2:4] != (str(run_id), str(artifact_id)):
            return None
        viewer = row[4](row[1]) if row[4] is not None else row[1]
        if viewer is None:
            return None
        return ArtifactTicketGrant(viewer, row[2], row[3], row[5])

    def consume(self, token: str, run_id: str,
                artifact_id: str) -> ArtifactTicketGrant | None:
        return self.authorize(token, run_id, artifact_id, consume=True)


class PraxisAppService:
    """One compact projection over the existing Praxis runtime."""

    def __init__(
        self,
        base: str | Path | None = None,
        *,
        owner_id: str | int | None = None,
        body_probe: Callable[..., dict] | None = None,
        resume_run: Callable[[str], dict] | None = None,
        device_store: praxis_device_auth.DeviceAuthStore | None = None,
    ) -> None:
        self.base = Path(
            base or os.environ.get("PRAXIS_BASE") or Path(__file__).resolve().parent
        ).resolve()
        self.owner_id = str(
            owner_id if owner_id is not None else os.environ.get("PRAXIS_OWNER_ID") or "0"
        )
        self.runs = run_manager.RunManager(self.base)
        self.membership = telegram_membership.MembershipLedger(
            self.base / "memory" / ".state" / "telegram_membership.jsonl"
        )
        self.followups = telegram_followups.FollowUpLedger(
            self.base / "memory" / ".state" / "telegram_followups.json"
        )
        self.deliveries = owner_delivery.OwnerDeliveryLedger(
            self.base / "memory" / ".state" / "owner_delivery" / "events.jsonl"
        )
        self.devices = device_store or praxis_device_auth.DeviceAuthStore(
            base=self.base, owner_id=self.owner_id,
        )
        self._body_probe = body_probe or body_client.status_probe
        self._resume_run = resume_run
        self._body_lock = threading.Lock()
        self._body_cache: tuple[float, dict] = (0.0, {})
        # Bound memory while serialising retries of the same deterministic
        # command intent.  A small striped set avoids holding the status-probe
        # lock across a network call and prevents two HTTP requests for the
        # same key from racing the run reducer inside this service process.
        self._body_command_locks = tuple(threading.Lock() for _ in range(64))
        # Body execution and browser materialisation are separate phases.  Keep
        # same-export callers in one lane after the durable body receipt so they
        # cannot race a CAS download or issue competing browser artifacts.
        self._browser_artifact_locks = tuple(threading.Lock() for _ in range(32))
        self._browser_artifact_downloads: dict[
            tuple[str, str, str, str, str], tuple[float, str, dict]
        ] = {}
        self._server_identity_lock = threading.Lock()
        self._server_executor_identity: dict[str, object] = {}
        self._refresh_server_executor_identity()

    # ------------------------------------------------------------------ auth

    def viewer(self, actor_id: str | int | None) -> Viewer | None:
        raw = str(actor_id or "").strip()
        if not raw or not raw.isdigit() or raw == "0":
            return None
        if self.owner_id != "0" and raw == self.owner_id:
            return Viewer(raw, "owner", tuple(sorted(computer_access.SCOPES)))
        scopes = tuple(
            sorted(scope for scope in computer_access.SCOPES
                   if computer_access.allowed(raw, scope))
        )
        return Viewer(raw, "trusted", scopes) if scopes else None

    def device_viewer(self, bearer_token: str) -> Viewer | None:
        principal = self.devices.validate_bearer(bearer_token)
        if principal is None:
            return None
        return Viewer(principal.device_id, "device", tuple(principal.scopes))

    def revalidate_ticket_viewer(self, viewer: Viewer, *,
                                 required_scope: str | None = None) -> Viewer | None:
        """Fail closed when a device capability outlives owner revocation.

        Telegram tickets keep their existing short-lived semantics.  Device
        tickets are checked against the canonical device ledger on every use;
        their scopes can only stay the same or shrink from the issued viewer.
        """
        if viewer.role != "device":
            return viewer if required_scope is None or viewer.may(required_scope) else None
        try:
            principal = self.devices.active_device_principal(viewer.actor_id)
        except praxis_device_auth.DeviceAuthError:
            return None
        if principal is None:
            return None
        current_scopes = frozenset(principal.scopes)
        current = Viewer(
            viewer.actor_id, "device",
            tuple(scope for scope in viewer.scopes if scope in current_scopes),
        )
        if required_scope is not None and not current.may(required_scope):
            return None
        return current

    def revalidate_event_ticket(self, viewer: Viewer) -> Viewer | None:
        return self.revalidate_ticket_viewer(viewer, required_scope="praxis.events")

    def revalidate_artifact_ticket(self, viewer: Viewer) -> Viewer | None:
        # A body export is authorized by computer.files; a run-detail download
        # by praxis.snapshot.  The ticket itself remains bound to one artifact,
        # so current activity is revalidated without manufacturing either scope.
        current = self.revalidate_ticket_viewer(viewer)
        if current is None or not (
                current.may("computer.files") or current.may("praxis.snapshot")):
            return None
        return current

    def _owner_principal(self, viewer: Viewer) -> praxis_device_auth.OwnerPrincipal:
        if not viewer.owner:
            raise PermissionError("device authority is owner-only")
        return praxis_device_auth.OwnerPrincipal(viewer.actor_id)

    def issue_device_enrollment(self, viewer: Viewer, *, label: str,
                                scopes: list[str] | tuple[str, ...] | None = None,
                                ttl_seconds: int = 900) -> dict:
        credential = self.devices.create_enrollment(
            self._owner_principal(viewer), label=label, scopes=scopes,
            ttl_seconds=ttl_seconds,
        )
        return {
            "enrollment_id": credential.enrollment_id,
            "label": credential.label,
            "scopes": list(credential.scopes),
            "expires_at": credential.expires_at,
            "enrollment_token": credential.enrollment_token,
        }

    def redeem_device(self, enrollment_token: str, *, platform: str) -> dict:
        credential = self.devices.redeem(enrollment_token, platform=platform)
        principal = credential.principal
        return {
            "device_token": credential.bearer_token,
            "device": {
                "device_id": principal.device_id,
                "label": principal.label,
                "platform": principal.platform,
                "scopes": list(principal.scopes),
                "issued_at": principal.issued_at,
                "status": "active",
            },
        }

    def list_devices(self, viewer: Viewer) -> list[dict]:
        return self.devices.list_devices(self._owner_principal(viewer))

    def revoke_device(self, viewer: Viewer, device_id: str, *, reason: str = "") -> dict:
        revoked = self.devices.revoke_device(
            self._owner_principal(viewer), device_id,
            reason=reason or "owner revoke",
        )
        return {"ok": revoked, "device_id": str(device_id or "")}

    # --------------------------------------------------------------- snapshot

    def _body_status(self) -> dict:
        if not body_client.available():
            return {
                "configured": False,
                "online": False,
                "device_id": body_client.device_id(),
                "state": "not_configured",
            }
        with self._body_lock:
            observed_at, cached = self._body_cache
            if cached and time.monotonic() - observed_at < 8.0:
                return _json_clone(cached)
            try:
                probe = self._body_probe(timeout=4.0)
            except Exception as exc:
                probe = {"ok": False, "error": type(exc).__name__}
            identity = probe.get("identity") if isinstance(probe.get("identity"), dict) else {}
            manifest = probe.get("manifest") if isinstance(probe.get("manifest"), dict) else {}
            contexts = []
            for row in manifest.get("execution_contexts") or ():
                if not isinstance(row, dict):
                    continue
                contexts.append({
                    "kind": str(row.get("kind") or ""),
                    "available": bool(row.get("available")),
                    "integrity": str(row.get("integrity") or ""),
                    "session_id": row.get("session_id"),
                })
            result = {
                "configured": True,
                "online": bool(probe.get("ok")),
                "device_id": body_client.device_id(),
                "state": "online" if probe.get("ok") else "offline",
                "probe_execution": str(probe.get("probe_execution") or ""),
                "identity": {
                    key: identity.get(key)
                    for key in ("kind", "integrity", "elevated", "session_id", "user")
                    if identity.get(key) is not None
                },
                "contexts": contexts,
            }
            if not probe.get("ok"):
                result["error"] = str(
                    probe.get("system_error") or probe.get("error") or probe.get("code") or
                    "unavailable"
                )[:300]
            self._body_cache = (time.monotonic(), result)
            return _json_clone(result)

    def _inventory(self, viewer: Viewer) -> dict:
        if not viewer.may("computer.read"):
            return {"available": False, "observed_at": "", "state": "scope_required"}
        device = body_client.device_id()
        safe_device = "".join(
            ch if ch.isalnum() or ch in "_.-" else "-" for ch in device
        ).strip("-.") or "unknown"
        path = self.base / "memory" / "computer" / "inventory" / safe_device / "CURRENT.json"
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            os_row = payload.get("os") if isinstance(payload.get("os"), dict) else {}
            machine = payload.get("machine") if isinstance(payload.get("machine"), dict) else {}
            result = {
                "available": True,
                "observed_at": str(row.get("observed_at") or ""),
                "captured_at": str(row.get("captured_at") or ""),
                "hostname": str(payload.get("hostname") or ""),
                "os": {
                    key: os_row.get(key)
                    for key in ("caption", "version", "build", "architecture", "last_boot")
                    if os_row.get(key) is not None
                },
                "machine": {
                    key: machine.get(key)
                    for key in ("manufacturer", "model", "memory_bytes")
                    if machine.get(key) is not None
                },
                "volumes": list(payload.get("volumes") or ())[:32],
                "tools": list(payload.get("tools") or ())[:64],
                "apps_count": len(payload.get("apps") or ()),
                "projects_count": len(payload.get("project_roots") or ()),
            }
            if viewer.may("computer.files"):
                result["known_roots"] = list(payload.get("known_roots") or ())[:64]
                result["project_roots"] = list(payload.get("project_roots") or ())[:200]
            return result
        except (OSError, TypeError, ValueError):
            return {"available": False, "observed_at": ""}

    def _computer_evidence(self, viewer: Viewer) -> list[dict]:
        if not viewer.may("computer.read"):
            return []
        try:
            import computer_memory

            rows = computer_memory.search(device_id=body_client.device_id(), limit=12)
        except Exception:
            return []
        return [{
            "id": str(row.get("id") or ""),
            "at": str(row.get("at") or ""),
            "task_id": str(row.get("task_id") or ""),
            "capability": str(row.get("capability") or ""),
            "status": str(row.get("status") or ""),
            "subject": str(row.get("subject") or "")[:500],
            "summary": str(row.get("summary") or "")[:1000],
        } for row in rows]

    def _runs_snapshot(self) -> dict:
        try:
            # Counts come from every durable manifest; only the recent
            # terminal history is limited before expensive per-run WAL
            # reduction. Active and attention runs are retained by the run
            # manager even when that makes the visible set larger than 40.
            return self.runs.run_listing(limit=40)
        except Exception as exc:
            return {"items": [], "counts": {}, "error": _small_error(exc)}

    def _memory_health(self) -> dict:
        maps = []
        for name in MAP_NAMES:
            path = self.base / "memory" / "maps" / f"{name}.md"
            maps.append({
                "id": name.lower(),
                "name": name,
                "available": path.is_file(),
                "updated_at": _mtime(path),
                "bytes": path.stat().st_size if path.is_file() else 0,
            })
        db_path = self.base / "memory" / ".state" / "recall.sqlite3"
        index = {
            "available": db_path.is_file(),
            "updated_at": _mtime(db_path),
            "chunks": 0,
            "sources": 0,
            "role": "rebuildable_query_index",
        }
        if db_path.is_file():
            try:
                uri = db_path.resolve().as_uri() + "?mode=ro"
                with sqlite3.connect(uri, uri=True, timeout=1.0) as db:
                    chunks, sources = db.execute(
                        "SELECT COUNT(*), COUNT(DISTINCT path) FROM chunks"
                    ).fetchone()
                index.update({"chunks": int(chunks), "sources": int(sources)})
            except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
                index["error"] = type(exc).__name__
        return {
            "canonical": "markdown_and_append_only_jsonl",
            "maps": maps,
            "index": index,
            "raw_journal_is_normative": False,
        }

    @staticmethod
    def _followup_card(row: dict) -> dict:
        response = row.get("response") if isinstance(row.get("response"), dict) else {}
        return {
            "id": str(row.get("id") or ""),
            "status": str(row.get("status") or ""),
            "target_label": str(row.get("target_label") or ""),
            "target_ref": str(row.get("target_ref") or ""),
            "sent_at": row.get("sent_at"),
            "sent_message_id": row.get("sent_message_id"),
            "answered_at": response.get("received_at"),
            "answer_preview": str(response.get("text") or "")[:280],
        }

    def _telegram(self) -> dict:
        try:
            import panel
            rooms = list((panel.rooms_list() or {}).get("items") or ())[:120]
        except Exception:
            rooms = []
        try:
            followups = [self._followup_card(row) for row in self.followups.list()][-80:]
        except Exception:
            followups = []
        try:
            membership = self.membership.pending()[:80]
        except Exception:
            membership = []
        return {
            "rooms": rooms,
            "followups": followups,
            # ⚠ Считаем только ЗАКАЗАННЫЕ отчёты. Остальные нити — её собственный след
            # того, что уже сказано (единственная память об этом внутри пульса, где связь
            # разорвана); тащить его бейджем на панель Егора значит зеркалить ему её
            # внутреннюю работу, а он просил обратного.
            "pending_followups": sum(
                1 for row in followups
                if row.get("notify_owner") and row.get("status") in {"pending", "answered"}
            ),
            "membership": membership,
        }

    def _trust(self, viewer: Viewer) -> dict:
        grants: list[dict] = []
        if viewer.owner:
            try:
                grants = list(computer_access.effective(actor=viewer.actor_id).values())
            except Exception:
                grants = []
        devices: list[dict] = []
        if viewer.owner:
            try:
                devices = self.list_devices(viewer)
            except praxis_device_auth.DeviceAuthError:
                raise
            except Exception:
                devices = []
        return {
            "owner_only": True,
            "delegation": "non_delegable",
            "available_scopes": sorted(computer_access.SCOPES),
            "available_device_scopes": sorted(praxis_device_auth.ALLOWED_DEVICE_SCOPES),
            "grants": grants,
            "devices": devices,
        }

    def _inbox(self) -> dict:
        """Project the private canonical owner-delivery ledger for owner surfaces."""
        # Never turn ambiguous provenance into a plausible empty inbox.  A final
        # torn tail is repaired by the ledger; any committed corruption aborts
        # the snapshot so the owner surface cannot silently erase attention.
        return self.deliveries.snapshot(limit=80)

    def _system(self) -> dict:
        try:
            import panel
            state = panel.server_state()
        except Exception as exc:
            state = {"error": _small_error(exc)}
        state["api"] = API_VERSION
        return state

    def snapshot(self, viewer: Viewer) -> dict:
        if viewer.role == "device" and not viewer.may("praxis.snapshot"):
            raise PermissionError("praxis.snapshot is required")
        body = self._body_status() if viewer.may("computer.read") else {
            "configured": False, "online": False, "state": "scope_required",
        }
        computer = {
            **body,
            "inventory": self._inventory(viewer),
            "evidence": self._computer_evidence(viewer),
            "capabilities": {
                scope: viewer.may(scope) for scope in sorted(computer_access.SCOPES)
            },
        }
        payload: dict[str, Any] = {
            "schema": SCHEMA,
            "api_version": API_VERSION,
            "viewer": viewer.public(),
            "computer": computer,
        }
        full_read = viewer.owner or viewer.may("praxis.snapshot")
        if full_read:
            runs = self._runs_snapshot()
            telegram = self._telegram()
            inbox = self._inbox()
            payload.update({
                "inbox": inbox,
                "runs": runs,
                "memory": self._memory_health(),
                "telegram": telegram,
                "system": self._system(),
            })
            if viewer.owner:
                payload["trust"] = self._trust(viewer)
            active = sum(
                int(runs.get("counts", {}).get(name, 0))
                for name in ("pending", "running", "paused", "blocked", "in_doubt")
            )
            payload["now"] = {
                "state": "active" if active else "ready",
                "active_runs": active,
                "body_online": bool(body.get("online")),
                "pending_followups": int(telegram.get("pending_followups") or 0),
                "inbox_unread": int(inbox.get("unread") or 0),
            }
        else:
            payload["now"] = {
                "state": "online" if body.get("online") else "offline",
                "body_online": bool(body.get("online")),
            }
        payload["revision"] = self.revision_hint(viewer)
        payload["generated_at"] = _now()
        return payload

    def revision_hint(self, viewer: Viewer) -> str:
        roots = [
            self.base / "memory" / ".state" / "telegram_followups.json",
            self.base / "memory" / ".state" / "telegram_membership.jsonl",
            self.base / "memory" / ".state" / "owner_delivery" / "events.jsonl",
            self.base / "memory" / ".state" / "recall.sqlite3",
            self.base / "memory" / "tasks.json",
        ]
        roots.extend(self.base.glob("memory/maps/*.md"))
        roots.extend(self.base.glob("memory/runs/*/*/manifest.json"))
        roots.extend(self.base.glob("memory/access/events/*.jsonl"))
        roots.extend(self.base.glob("memory/computer/inventory/*/CURRENT.json"))
        if viewer.owner:
            roots.extend(self.base.glob("logs/*.log"))
        rows = []
        for path in roots:
            try:
                stat = path.stat()
            except OSError:
                continue
            try:
                rel = path.resolve().relative_to(self.base).as_posix()
            except ValueError:
                rel = path.name
            rows.append((rel, stat.st_mtime_ns, stat.st_size))
        with self._body_lock:
            body_state = self._body_cache[1]
            if body_state:
                rows.append((
                    "__body__",
                    json.dumps(body_state, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), default=str),
                    0,
                ))
        raw = json.dumps(sorted(rows), separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:24]

    # -------------------------------------------------------------- run views

    def _artifact_cards(self, viewer: Viewer | None, run_id: str, *,
                        issue_tickets: bool) -> list[dict]:
        artifacts: dict[str, dict] = {}
        # The event window shown in the UI is deliberately compact, but artifact
        # discovery is not: an old file must not disappear after 160 later events.
        for row in self.runs.iter_events(run_id, reverse=True, strict=True):
            artifact = row.get("artifact") if isinstance(row.get("artifact"), dict) else None
            if not artifact or not artifact.get("artifact_id"):
                continue
            artifact_id = str(artifact["artifact_id"])
            artifacts.setdefault(artifact_id, {
                key: artifact.get(key)
                for key in ("artifact_id", "name", "media_type", "size", "sha256")
            })

        def sequence(card: dict) -> int:
            try:
                return int(str(card.get("artifact_id") or "").split("-", 1)[1])
            except (IndexError, TypeError, ValueError):
                return -1

        cards = sorted(artifacts.values(), key=sequence)
        if issue_tickets:
            if viewer is None:
                raise ValueError("viewer is required when issuing artifact tickets")
            # Keep the ticket table bounded.  Every artifact remains discoverable;
            # the newest bounded window also receives a native-browser URL.
            for card in cards[-ARTIFACT_TICKETS.per_principal:]:
                artifact_id = str(card["artifact_id"])
                ticket = ARTIFACT_TICKETS.issue(
                    viewer, run_id, artifact_id,
                    validator=self.revalidate_artifact_ticket,
                )
                card["download_url"] = (
                    f"/api/praxis/v1/runs/{run_id}/artifacts/{artifact_id}?ticket={ticket}"
                )
        return cards

    def run_detail(self, viewer: Viewer, run_id: str) -> dict:
        if not (viewer.owner or viewer.may("praxis.snapshot")):
            raise PermissionError("run evidence requires praxis.snapshot")
        status = self.runs.status(run_id)
        events = list(self.runs.iter_events(run_id, reverse=True, strict=True))[:160]
        events.reverse()
        artifacts = self._artifact_cards(viewer, run_id, issue_tickets=True)
        run_dir = self.runs.path(run_id)
        recap_path = run_dir / "RECAP.md"
        recap = ""
        if recap_path.is_file():
            recap = recap_path.read_text(encoding="utf-8", errors="replace")[:64_000]
        return {
            "run": status,
            "events": events,
            "artifacts": artifacts,
            "recap": recap,
        }

    def control_run(self, viewer: Viewer, run_id: str, action: str, *,
                    reason: str = "", expected_revision: int | None = None) -> dict:
        if not viewer.may("praxis.runs.control"):
            raise PermissionError("praxis.runs.control is required")
        action = str(action or "").strip().lower()
        if action not in RUN_CONTROL_ACTIONS:
            raise ValueError("action must be pause, resume or cancel")
        actor = viewer.principal_id
        if expected_revision is None:
            raise ValueError("expected_revision is required")
        if action == "pause":
            self.runs.request_pause(
                run_id, actor=actor, reason=reason,
                expected_revision=expected_revision,
            )
        elif action == "cancel":
            self.runs.request_cancel(
                run_id, actor=actor, reason=reason,
                expected_revision=expected_revision,
            )
        else:
            self.runs.authorize_resume(
                run_id, actor=actor, reason=reason,
                expected_revision=expected_revision,
            )
            self._wake_resume(run_id)
        return {"ok": True, "run": self.runs.status(run_id)}

    def _wake_resume(self, run_id: str) -> None:
        # A web control surface does not own a cognitive executor.  Running the
        # model loop here would execute the brain in THIS process, outside the
        # runner's single-flight _ONE_MIND (F1 vector B) and where the reaper
        # cannot see it, so a healthy resume looks orphaned (F2).  control_run has
        # already called authorize_resume, leaving the run durably paused with a
        # resume_authorized event; the runner's durable_resume clock claims and
        # continues it under _ONE_MIND.  A resume_run callback may be injected to
        # shorten pickup latency, but it must only SIGNAL — never execute the brain.
        callback = self._resume_run
        if callback is None:
            return

        def wake() -> None:
            try:
                callback(run_id)
            except Exception:
                # The authorization is durable; the runner clock retries the plan.
                pass

        threading.Thread(
            target=wake, name=f"praxis-app-resume-{run_id[-12:]}", daemon=True
        ).start()

    def artifact_path(self, viewer: Viewer, run_id: str,
                      artifact_id: str) -> tuple[Path, dict]:
        if not (viewer.owner or viewer.may("praxis.snapshot")):
            raise PermissionError("run artifacts require praxis.snapshot")
        return self._artifact_path_for_exact_capability(run_id, artifact_id)

    def artifact_path_from_ticket(self, grant: ArtifactTicketGrant) -> tuple[Path, dict]:
        """Resolve only the exact run/artifact pair carried by a valid ticket."""
        if not isinstance(grant, ArtifactTicketGrant):
            raise PermissionError("an artifact ticket capability is required")
        return self._artifact_path_for_exact_capability(grant.run_id, grant.artifact_id)

    def _artifact_path_for_exact_capability(self, run_id: str,
                                            artifact_id: str) -> tuple[Path, dict]:
        wanted = str(artifact_id or "").strip()
        card = next(
            (row for row in self._artifact_cards(None, run_id, issue_tickets=False)
             if row.get("artifact_id") == wanted),
            None,
        )
        if card is None:
            raise FileNotFoundError(wanted)
        seq = int(wanted.split("-", 1)[1])
        artifacts = (self.runs.path(run_id) / "artifacts").resolve()
        hits = list(artifacts.glob(f"{seq:04d}-*"))
        if len(hits) != 1 or not hits[0].is_file():
            raise FileNotFoundError(wanted)
        path = hits[0].resolve()
        path.relative_to(artifacts)
        if path.stat().st_size != int(card.get("size") or -1) or _sha_file(path) != card.get("sha256"):
            raise run_manager.RunConflict("artifact differs from its durable receipt")
        return path, {
            **card,
            "media_type": str(card.get("media_type") or mimetypes.guess_type(str(path))[0]
                              or "application/octet-stream"),
        }

    # ------------------------------------------------------------- mutations

    def change_access(self, viewer: Viewer, *, action: str,
                      telegram_id: str | int, name: str = "",
                      scopes: list[str] | tuple[str, ...] = ()) -> dict:
        if not viewer.owner:
            raise PermissionError("only owner can change trust")
        result = computer_access.change(
            action, telegram_id, name=name, scopes=scopes, actor=viewer.actor_id,
        )
        if not result.get("ok"):
            raise ValueError(str(result.get("error") or result.get("code") or "access rejected"))
        return result

    @staticmethod
    def _client_key(body: dict, *, required: bool) -> str:
        value = str(body.get("idempotency_key") or "").strip()
        if not value:
            if required:
                raise ValueError("idempotency_key is required for mutating commands")
            return secrets.token_urlsafe(24)
        if (len(value) < 8 or len(value) > 200
                or any(not char.isprintable() or char.isspace() for char in value)):
            raise ValueError("idempotency_key must be 8..200 visible characters")
        return value

    @staticmethod
    def _command_digest(viewer: Viewer, client_key: str) -> str:
        raw = f"praxis-app-command-v1\0{viewer.principal_id}\0{client_key}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _command_run(self, viewer: Viewer, digest: str, *, capability: str,
                     execution: str, side_effect: bool, client_key: str,
                     args: dict) -> tuple[run_context.RunContext, str, str, str]:
        run_id = f"run-app-{digest[:40]}"
        call_id = f"call-body-{digest[:32]}"
        request_id = f"app-req-{digest[:40]}"
        operation_id = f"app-op-{digest[:40]}"
        expected_args = {
            "capability": capability, "execution": execution, "args": args,
        }
        try:
            context = self.runs.context(run_id)
        except run_manager.RunNotFound:
            proposed = run_context.RunContext.create(
                run_id=run_id,
                kind="praxis_app_command",
                goal=f"Mini-app computer command: {capability}",
                principal_id=viewer.principal_id,
                scope=viewer.role,
            )
            markdown = (
                "# Praxis app command\n\n"
                f"- Principal: `{viewer.principal_id}`\n"
                f"- Capability: `{capability}`\n"
                f"- Execution: `{execution}`\n"
                f"- Client operation: `{operation_id}`\n"
            )
            try:
                context = self.runs.create(proposed, markdown)
            except run_manager.RunConflict:
                context = self.runs.context(run_id)

        started = next((
            row for row in self.runs.iter_events(run_id, reverse=True, strict=True)
            if row.get("kind") == "tool_started" and row.get("call_id") == call_id
        ), None)
        if started is not None:
            observed = {
                "tool": started.get("tool"),
                "args": started.get("args"),
                "side_effect": started.get("side_effect"),
                "idempotency_key": started.get("idempotency_key"),
            }
            expected = {
                "tool": "windows_body",
                "args": expected_args,
                "side_effect": side_effect,
                "idempotency_key": client_key,
            }
            if observed != expected:
                raise run_manager.RunConflict(
                    "idempotency_key is already bound to a different computer command"
                )
            status = self.runs.status(run_id)["status"]
            if status == "blocked":
                self.runs.transition(
                    run_id, "running", expected="blocked",
                    reason="retrying the same idempotent body request",
                    details={"capability": capability, "operation_id": operation_id},
                )
            elif status in {"paused", "in_doubt", "cancelled"}:
                raise run_manager.RunConflict(
                    f"command run is {status}; explicit run control is required"
                )
            elif status not in {"pending", "running", "done", "failed"}:
                raise run_manager.RunConflict(f"command run has unsupported status {status}")
            elif status == "pending":
                self.runs.transition(run_id, "running", expected="pending")
        else:
            status = self.runs.status(run_id)["status"]
            if status == "pending":
                self.runs.transition(run_id, "running", expected="pending")
            elif status != "running":
                raise run_manager.RunConflict("command receipt exists without its durable intent")
            self.runs.start_tool(
                run_id, call_id, "windows_body", expected_args,
                side_effect=side_effect, idempotency_key=client_key,
            )
        return self.runs.context(run_id), call_id, request_id, operation_id

    @staticmethod
    def _body_receipt_status(result: dict) -> str:
        code = str(result.get("code") or "").strip().lower()
        status = str(result.get("status") or "").strip().lower()
        if code == "operation_in_doubt" or status in BODY_IN_DOUBT_STATUSES:
            return "in_doubt"
        if code in BODY_DEFINITIVE_FAILURES or status in BODY_DEFINITIVE_STATUSES:
            return "failed"
        # No definitive body receipt exists yet.  The stable request and
        # operation ids make a later retry safe even if the first response was
        # lost after application.
        return "blocked"

    def _body_command(self, viewer: Viewer, body: dict, *, capability: str,
                      args: dict, execution: str, timeout: float) -> dict:
        policy = BODY_CAPABILITIES.get(capability)
        if policy is None:
            raise ValueError("unsupported computer capability")
        side_effect = not policy.read_only
        client_key = self._client_key(body, required=side_effect)
        digest = self._command_digest(viewer, client_key)
        command_lock = self._body_command_locks[
            int(digest[:8], 16) % len(self._body_command_locks)
        ]
        with command_lock:
            return self._body_command_locked(
                viewer, capability=capability, args=args, execution=execution,
                timeout=timeout, side_effect=side_effect, client_key=client_key,
                digest=digest,
            )

    def _body_command_locked(self, viewer: Viewer, *, capability: str,
                             args: dict, execution: str, timeout: float,
                             side_effect: bool, client_key: str,
                             digest: str) -> dict:
        context, call_id, request_id, operation_id = self._command_run(
            viewer, digest, capability=capability, execution=execution,
            side_effect=side_effect, client_key=client_key, args=args,
        )
        try:
            with run_context.bind_run(context):
                result = body_client.call(
                    capability, args, execution=execution, timeout=timeout,
                    request_id=request_id, operation_id=operation_id,
                )
            if not isinstance(result, dict):
                result = {"ok": False, "code": "bad_body_receipt",
                          "error": "Windows body returned a non-object receipt"}
        except Exception as exc:
            result = {"ok": False, "code": "body_client_exception",
                      "error": _small_error(exc)}
        result.setdefault("request_id", request_id)
        result.setdefault("operation_id", operation_id)
        serialized = json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False, default=str,
        )
        self.runs.store_result(
            context.run_id, serialized, call_id=call_id, name="body-receipt.json",
            media_type="application/json",
            metadata={
                "principal_id": viewer.principal_id,
                "capability": capability,
                "request_id": request_id,
                "operation_id": operation_id,
            },
        )
        terminal = "done" if result.get("ok") else self._body_receipt_status(result)
        self.runs.transition(
            context.run_id, terminal,
            expected={"running", terminal},
            reason=f"direct body command {terminal}",
            details={
                "capability": capability, "operation_id": operation_id,
                "retryable": terminal == "blocked",
            },
        )
        return {
            **result,
            "ok": bool(result.get("ok")),
            "praxis_audit": {
                "run_id": context.run_id,
                "principal_id": viewer.principal_id,
                "client_operation_id": operation_id,
                "request_id": request_id,
            },
        }

    @staticmethod
    def _server_operation_digest(viewer: Viewer, client_key: str) -> str:
        raw = (
            f"praxis-app-server-operation-v1\0{viewer.principal_id}\0{client_key}"
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _refresh_server_executor_identity(self) -> dict[str, object]:
        """Bind this service instance to one exact operating-system process."""
        pid = os.getpid()
        with self._server_identity_lock:
            current = self._server_executor_identity
            if int(current.get("executor_pid") or 0) == pid:
                return dict(current)
            current = {
                "executor_pid": pid,
                "executor_id": secrets.token_hex(16),
                "executor_host_id": _server_machine_identity(),
                "executor_boot_id": _server_boot_identity(),
                "executor_process_start": _server_process_start_identity(pid),
                "executor_pid_namespace": _server_process_namespace_identity(pid),
            }
            self._server_executor_identity = current
            return dict(current)

    @staticmethod
    def _server_claim_identity_fields(claim: dict) -> dict[str, object]:
        return {
            field: claim.get(field)
            for field in (
                "executor_pid", "executor_id", "executor_host_id",
                "executor_boot_id", "executor_process_start",
                "executor_pid_namespace",
            )
        }

    @staticmethod
    def _server_claim_executor_state(claim: dict) -> str:
        """Classify the claimed process as live, dead, or unverifiable.

        A PID alone is never sufficient.  Process start identity makes reuse
        fail closed, while host/boot/namespace bindings prevent a process in a
        different execution universe from inheriting the claim accidentally.
        """
        try:
            pid = int(claim.get("executor_pid") or 0)
        except (TypeError, ValueError):
            return "ambiguous"
        executor_id = str(claim.get("executor_id") or "")
        claimed_host = str(claim.get("executor_host_id") or "")
        claimed_start = str(claim.get("executor_process_start") or "")
        if pid <= 0 or not executor_id or not claimed_host or not claimed_start:
            return "ambiguous"

        current_host = _server_machine_identity()
        if not current_host or current_host != claimed_host:
            return "ambiguous"
        claimed_boot = str(claim.get("executor_boot_id") or "")
        current_boot = _server_boot_identity()
        if claimed_boot and current_boot and claimed_boot != current_boot:
            return "dead"
        try:
            if not run_manager._owner_alive(pid):
                return "dead"
        except (OSError, TypeError, ValueError):
            return "ambiguous"

        observed_start = _server_process_start_identity(pid)
        if not observed_start:
            return "ambiguous"
        if observed_start != claimed_start:
            return "dead"
        claimed_namespace = str(claim.get("executor_pid_namespace") or "")
        observed_namespace = _server_process_namespace_identity(pid)
        if (claimed_namespace and observed_namespace
                and claimed_namespace != observed_namespace):
            return "ambiguous"
        return "live"

    def _server_claim_state(
        self, context: run_context.RunContext, call_id: str, claim: dict,
    ) -> tuple[str, dict]:
        """Return live/stale/dead/ambiguous from identity plus durable lease."""
        identity_state = self._server_claim_executor_state(claim)
        if identity_state != "live":
            return identity_state, claim

        latest = claim
        expected = {
            "call_id": call_id,
            "operation": claim.get("operation"),
            "operation_id": claim.get("operation_id"),
            "attempt_id": claim.get("attempt_id"),
            **self._server_claim_identity_fields(claim),
        }
        for event in self.runs.iter_events(
                context.run_id, reverse=True, strict=True):
            if (event.get("kind") != "server_operation_heartbeat"
                    or event.get("call_id") != call_id
                    or event.get("attempt_id") != claim.get("attempt_id")):
                continue
            if any(event.get(field) != value for field, value in expected.items()):
                return "ambiguous", event
            latest = event
            break

        expires = _server_utc_epoch(latest.get("lease_expires_at"))
        observed_at = _server_utc_epoch(latest.get("at"))
        if (expires is None or observed_at is None or expires <= observed_at
                or expires - observed_at > max(120.0, SERVER_OPERATION_LEASE_SECONDS * 2)):
            return "ambiguous", latest
        if expires <= time.time():
            return "stale", latest
        return "live", latest

    def _server_heartbeat(self, context: run_context.RunContext,
                          call_id: str, claim: dict) -> dict:
        return self.runs.append_event(
            context.run_id, "server_operation_heartbeat",
            call_id=call_id,
            operation=claim.get("operation"),
            operation_id=claim.get("operation_id"),
            attempt_id=claim.get("attempt_id"),
            **self._server_claim_identity_fields(claim),
            lease_expires_at=_server_utc_after(SERVER_OPERATION_LEASE_SECONDS),
        )

    def _server_heartbeat_worker(
        self, context: run_context.RunContext, call_id: str, claim: dict,
        stop: threading.Event, failed: threading.Event,
    ) -> None:
        interval = max(
            0.025,
            min(SERVER_OPERATION_HEARTBEAT_SECONDS,
                SERVER_OPERATION_LEASE_SECONDS / 3.0),
        )
        while not stop.wait(interval):
            try:
                self._server_heartbeat(context, call_id, claim)
            except Exception:
                # The side effect may already be under way.  Never manufacture
                # a replacement claim: a missing lease later becomes in_doubt.
                failed.set()
                return

    @staticmethod
    def _server_receipt_name(name: str) -> str:
        return f"{name.replace('.', '-')}-receipt.json"

    def _server_operation_run(
        self, viewer: Viewer, *, digest: str, name: str, args: dict,
        client_key: str,
    ) -> tuple[run_context.RunContext, str, str]:
        """Create or validate the deterministic durable intent for one client key."""
        run_id = f"run-app-server-{digest[:40]}"
        call_id = f"call-server-{digest[:32]}"
        operation_id = f"app-server-op-{digest[:40]}"
        normalized_args = _json_clone(args)
        try:
            context = self.runs.context(run_id)
        except run_manager.RunNotFound:
            proposed = run_context.RunContext.create(
                run_id=run_id,
                kind="praxis_app_operation",
                goal=f"Mini-app server operation: {name}",
                principal_id=viewer.principal_id,
                scope=viewer.role,
            )
            markdown = (
                "# Praxis app server operation\n\n"
                f"- Principal: `{viewer.principal_id}`\n"
                f"- Operation: `{name}`\n"
                f"- Client operation: `{operation_id}`\n"
            )
            try:
                context = self.runs.create(proposed, markdown)
            except run_manager.RunConflict:
                # Another process may have won ``mkdir`` but still be between
                # its context/event writes and the atomic manifest publish.
                # Wait briefly for that same deterministic run instead of
                # turning an ordinary concurrent submit into a false failure.
                deadline = time.monotonic() + 2.0
                while True:
                    try:
                        context = self.runs.context(run_id)
                        break
                    except run_manager.RunNotFound as exc:
                        if time.monotonic() >= deadline:
                            raise run_manager.RunConflict(
                                "concurrent server operation run creation did not finish"
                            ) from exc
                        time.sleep(0.01)

        expected = {
            "tool": f"praxis_app.{name}",
            "args": normalized_args,
            "side_effect": True,
            "idempotency_key": client_key,
        }
        started = next((
            row for row in self.runs.iter_events(run_id, reverse=True, strict=True)
            if row.get("kind") == "tool_started" and row.get("call_id") == call_id
        ), None)
        if started is not None:
            observed = {field: started.get(field) for field in expected}
            if observed != expected:
                raise run_manager.RunConflict(
                    "idempotency_key is already bound to a different server command"
                )
            status = self.runs.status(run_id)["status"]
            if status == "pending":
                self.runs.transition(run_id, "running", expected="pending")
        else:
            status = self.runs.status(run_id)["status"]
            if status == "pending":
                self.runs.transition(run_id, "running", expected="pending")
            elif status != "running":
                raise run_manager.RunConflict(
                    "server operation run exists without its durable intent"
                )
            self.runs.start_tool(
                run_id, call_id, expected["tool"], normalized_args,
                side_effect=True, idempotency_key=client_key,
            )
        return self.runs.context(run_id), call_id, operation_id

    def _server_receipt(self, context: run_context.RunContext, call_id: str,
                        name: str, viewer: Viewer, operation_id: str) -> dict | None:
        receipt_name = self._server_receipt_name(name)
        event = next((
            row for row in self.runs.iter_events(
                context.run_id, reverse=True, strict=True,
            )
            if (row.get("kind") == "tool_result"
                and row.get("call_id") == call_id
                and row.get("name") == receipt_name)
        ), None)
        if event is None:
            return None
        ref = dict(event.get("result") or {})
        size = int(ref.get("size") or 0)
        if size < 1 or size > SERVER_OPERATION_MAX_RECEIPT_BYTES:
            raise run_manager.RunConflict("server operation receipt has an invalid size")
        loaded = self.runs.read_result(
            context.run_id, str(ref.get("path") or ""), byte_limit=size,
        )
        if (not loaded.get("eof")
                or int(loaded.get("size") or -1) != size
                or str(loaded.get("sha256") or "") != str(ref.get("sha256") or "")):
            raise run_manager.RunConflict("server operation receipt differs from its hash")
        if int(loaded.get("bytes") or 0) != size:
            raise run_manager.RunConflict("server operation receipt exceeds replay budget")
        try:
            receipt = json.loads(str(loaded.get("text") or ""))
        except (TypeError, ValueError) as exc:
            raise run_manager.RunConflict("server operation receipt is not valid JSON") from exc
        expected = {
            "schema": SERVER_OPERATION_RECEIPT_SCHEMA,
            "operation": name,
            "principal_id": viewer.principal_id,
            "client_operation_id": operation_id,
        }
        if not isinstance(receipt, dict) or any(
                receipt.get(field) != value for field, value in expected.items()):
            raise run_manager.RunConflict("server operation receipt binding differs")
        result = receipt.get("result")
        if not isinstance(result, dict):
            raise run_manager.RunConflict("server operation receipt result is not an object")
        return result

    @staticmethod
    def _server_response(viewer: Viewer, context: run_context.RunContext,
                         name: str, operation_id: str, result: dict) -> dict:
        return {
            **_json_clone(result),
            "ok": bool(result.get("ok")),
            "praxis_audit": {
                "run_id": context.run_id,
                "principal_id": viewer.principal_id,
                "operation": name,
                "client_operation_id": operation_id,
            },
        }

    def _finish_server_operation(self, context: run_context.RunContext,
                                 name: str, result: dict) -> None:
        terminal = "done" if result.get("ok") else "failed"
        status = self.runs.status(context.run_id)["status"]
        if status == terminal:
            return
        if status == "in_doubt":
            # A late, hash-verified receipt is still the canonical replay
            # value.  Keep the attention state for explicit owner resolution
            # instead of silently rewriting an already raised uncertainty.
            return
        if status != "running":
            raise run_manager.RunConflict(
                f"server operation receipt cannot close run from {status}"
            )
        self.runs.transition(
            context.run_id, terminal, expected="running",
            reason=f"server operation {terminal}", details={"operation": name},
        )

    def _server_claim(self, context: run_context.RunContext, call_id: str,
                      name: str, operation_id: str) -> tuple[dict, bool]:
        attempt_id = secrets.token_hex(16)
        identity = self._refresh_server_executor_identity()
        try:
            row = self.runs.append_event_once(
                context.run_id, "server_operation_claimed", f"{call_id}:execute",
                call_id=call_id, operation=name, operation_id=operation_id,
                attempt_id=attempt_id,
                **identity,
                lease_expires_at=_server_utc_after(SERVER_OPERATION_LEASE_SECONDS),
            )
            return row, True
        except run_manager.RunConflict:
            row = next((
                event for event in self.runs.iter_events(
                    context.run_id, reverse=True, strict=True,
                )
                if (event.get("kind") == "server_operation_claimed"
                    and event.get("receipt_key") == f"{call_id}:execute")
            ), None)
            if row is None:
                raise
            expected = {
                "call_id": call_id, "operation": name,
                "operation_id": operation_id,
            }
            if any(row.get(field) != value for field, value in expected.items()):
                raise run_manager.RunConflict("server operation claim binding differs")
            return row, False

    def _unreceipted_server_operation(
        self, viewer: Viewer, context: run_context.RunContext, call_id: str,
        name: str, operation_id: str, claim: dict,
    ) -> dict:
        deadline = time.monotonic() + SERVER_OPERATION_WAIT_SECONDS
        claim_state = "ambiguous"
        while time.monotonic() < deadline:
            receipt = self._server_receipt(
                context, call_id, name, viewer, operation_id,
            )
            if receipt is not None:
                self._finish_server_operation(context, name, receipt)
                return self._server_response(
                    viewer, context, name, operation_id, receipt,
                )
            status = self.runs.status(context.run_id)["status"]
            if status == "in_doubt":
                claim_state = "in_doubt"
                break
            if status in {"done", "failed"}:
                # The receipt event is committed before the terminal status.
                # Re-read it after observing that status: another executor can
                # finish in the gap between the receipt probe above and this
                # manifest read.
                receipt = self._server_receipt(
                    context, call_id, name, viewer, operation_id,
                )
                if receipt is not None:
                    return self._server_response(
                        viewer, context, name, operation_id, receipt,
                    )
                raise run_manager.RunConflict(
                    f"terminal server operation {status} has no durable receipt"
                )
            if status != "running":
                raise run_manager.RunConflict(
                    f"unreceipted server operation has unsupported status {status}"
                )
            claim_state, _lease = self._server_claim_state(
                context, call_id, claim,
            )
            if claim_state != "live":
                break
            time.sleep(0.025)

        receipt = self._server_receipt(
            context, call_id, name, viewer, operation_id,
        )
        if receipt is not None:
            self._finish_server_operation(context, name, receipt)
            return self._server_response(
                viewer, context, name, operation_id, receipt,
            )

        status = self.runs.status(context.run_id)["status"]
        if status == "running":
            claim_state, _lease = self._server_claim_state(
                context, call_id, claim,
            )
        elif status == "in_doubt":
            claim_state = "in_doubt"
        elif status in {"done", "failed"}:
            # Same terminal-status/receipt TOCTOU as in the polling loop.
            receipt = self._server_receipt(
                context, call_id, name, viewer, operation_id,
            )
            if receipt is not None:
                return self._server_response(
                    viewer, context, name, operation_id, receipt,
                )
            raise run_manager.RunConflict(
                f"terminal server operation {status} has no durable receipt"
            )
        else:
            raise run_manager.RunConflict(
                f"unreceipted server operation has unsupported status {status}"
            )

        if status == "running" and claim_state == "live":
            result = {
                "ok": False,
                "code": "operation_in_progress",
                "status": "running",
                "error": "the original executor still owns this durable operation",
            }
        else:
            if status == "running":
                try:
                    self.runs.transition(
                        context.run_id, "in_doubt", expected="running",
                        reason="server executor claim is no longer trustworthy",
                        details={
                            "operation": name,
                            "operation_id": operation_id,
                            "claim_state": claim_state,
                            "attempt_id": claim.get("attempt_id"),
                        },
                    )
                except (run_manager.InvalidTransition, run_manager.RunConflict):
                    if self.runs.status(context.run_id)["status"] != "in_doubt":
                        raise
            result = {
                "ok": False,
                "code": "operation_in_doubt",
                "status": "in_doubt",
                "error": (
                    "the durable claim has no receipt and its executor lease or "
                    "identity is not trustworthy; the side effect was not repeated"
                ),
            }
        return self._server_response(viewer, context, name, operation_id, result)

    def _server_operation(self, viewer: Viewer, body: dict, name: str, args: dict,
                          callback: Callable[[], dict]) -> dict:
        client_key = self._client_key(body, required=True)
        digest = self._server_operation_digest(viewer, client_key)
        command_lock = self._body_command_locks[
            int(digest[:8], 16) % len(self._body_command_locks)
        ]
        with command_lock:
            context, call_id, operation_id = self._server_operation_run(
                viewer, digest=digest, name=name, args=args, client_key=client_key,
            )
            receipt = self._server_receipt(
                context, call_id, name, viewer, operation_id,
            )
            if receipt is not None:
                self._finish_server_operation(context, name, receipt)
                return self._server_response(
                    viewer, context, name, operation_id, receipt,
                )

            claim, acquired = self._server_claim(
                context, call_id, name, operation_id,
            )
            if not acquired:
                return self._unreceipted_server_operation(
                    viewer, context, call_id, name, operation_id, claim,
                )

            heartbeat_stop = threading.Event()
            heartbeat_failed = threading.Event()
            heartbeat = threading.Thread(
                target=self._server_heartbeat_worker,
                args=(context, call_id, claim, heartbeat_stop, heartbeat_failed),
                name=f"praxis-app-operation-{operation_id[-10:]}",
                daemon=True,
            )
            heartbeat.start()
            try:
                try:
                    with run_context.bind_run(self.runs.context(context.run_id)):
                        result = callback()
                    if not isinstance(result, dict):
                        raise TypeError("server operation returned a non-object receipt")
                except Exception as exc:
                    result = {
                        "ok": False, "code": "server_operation_failed",
                        "error": _small_error(exc),
                    }
                result = _json_clone(result)
                receipt = {
                    "schema": SERVER_OPERATION_RECEIPT_SCHEMA,
                    "operation": name,
                    "principal_id": viewer.principal_id,
                    "client_operation_id": operation_id,
                    "result": result,
                }
                serialized = json.dumps(
                    receipt, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False,
                )
                if len(serialized.encode("utf-8")) > SERVER_OPERATION_MAX_RECEIPT_BYTES:
                    result = {
                        "ok": False, "code": "server_operation_receipt_too_large",
                        "error": "server operation receipt exceeds 4 MiB",
                    }
                    receipt["result"] = result
                    serialized = json.dumps(
                        receipt, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"), allow_nan=False,
                    )
                self.runs.store_result(
                    context.run_id, serialized, call_id=call_id,
                    name=self._server_receipt_name(name), media_type="application/json",
                    idempotent=True, metadata={
                        "principal_id": viewer.principal_id,
                        "operation": name,
                        "client_operation_id": operation_id,
                    },
                )
            finally:
                heartbeat_stop.set()
                heartbeat.join(timeout=max(1.0, SERVER_OPERATION_HEARTBEAT_SECONDS * 2))
            self._finish_server_operation(context, name, result)
            return self._server_response(
                viewer, context, name, operation_id, result,
            )

    def command(self, viewer: Viewer, body: dict) -> dict:
        domain = str(body.get("domain") or "praxis").strip().lower()
        action = str(body.get("action") or "run").strip().lower()
        if domain == "praxis" and action == "run":
            if not viewer.may("praxis.work"):
                raise PermissionError("praxis.work is required")
            goal = str(body.get("goal") or "").strip()
            if not goal:
                raise ValueError("goal must not be empty")
            if len(goal) > 12_000:
                raise ValueError("goal is too long")
            kind = str(body.get("kind") or "window").strip().lower()
            if kind not in {"window", "coding"}:
                raise ValueError("kind must be window or coding")
            if kind == "coding" and not goal.lower().startswith("код:"):
                goal = "код: " + goal
            import tasks

            def enqueue_praxis_run() -> dict:
                task = tasks.add("window", goal, when="in 0m", target=None, author="app")
                return {
                    "ok": True,
                    "accepted": {
                        "kind": "praxis_run",
                        "queue_id": task["id"],
                        "status": "queued",
                        "goal": goal,
                    },
                }

            return self._server_operation(
                viewer, body, "praxis.run", {"kind": kind, "goal": goal},
                enqueue_praxis_run,
            )
        if domain == "telegram" and action in {"join", "leave"}:
            if not viewer.may("praxis.telegram"):
                raise PermissionError("praxis.telegram is required")
            target = str(body.get("target") or "").strip()
            return self._server_operation(
                viewer, body, f"telegram.{action}",
                {"action": action, "target": target},
                lambda: {"ok": True, "membership": self.membership.begin(
                    action, target, viewer.actor_id,
                )},
            )
        if domain == "telegram" and action in {"cancel_followup", "followup.cancel"}:
            if not viewer.may("praxis.telegram"):
                raise PermissionError("praxis.telegram is required")
            followup_id = str(body.get("followup_id") or "").strip()
            return self._server_operation(
                viewer, body, "telegram.followup.cancel",
                {"followup_id": followup_id},
                lambda: {
                    "ok": self.followups.cancel(followup_id),
                    "followup_id": followup_id,
                },
            )
        if domain == "inbox" and action in {"read", "acted"}:
            if not viewer.may("praxis.snapshot"):
                raise PermissionError("praxis.snapshot is required for owner inbox state")
            delivery_id = str(body.get("delivery_id") or "").strip()
            if not delivery_id:
                raise ValueError("delivery_id is required")
            expected_revision = body.get("expected_revision")
            if (not isinstance(expected_revision, int)
                    or isinstance(expected_revision, bool) or expected_revision < 1):
                raise ValueError("expected_revision must be a positive integer")
            item = self.deliveries.transition(
                delivery_id, action,
                expected_revision=expected_revision,
                detail={
                    "surface": "praxis_app",
                    "actor_id": viewer.actor_id,
                    "note": str(body.get("note") or "")[:500],
                },
            )
            return {"ok": True, "delivery": item}
        if domain == "memory":
            if action == "search":
                if not viewer.may("praxis.snapshot"):
                    raise PermissionError("praxis.snapshot is required")
                query = str(body.get("query") or "").strip()
                if not query:
                    raise ValueError("query is required")
                if len(query) > 500:
                    raise ValueError("query is too long")
                limit = max(1, min(int(body.get("limit") or 12), 30))
                import memory_index
                rows = memory_index.search(
                    query, k=limit, scope="owner", semantic=False,
                )
                results = []
                for row in rows[:limit]:
                    if not isinstance(row, dict):
                        continue
                    score = row.get("score")
                    if score is None:
                        score = row.get("lexical")
                    try:
                        score = round(float(score), 6) if score is not None else None
                        if score is not None and not math.isfinite(score):
                            score = None
                    except (TypeError, ValueError):
                        score = None
                    results.append({
                        "title": str(row.get("source") or row.get("path") or "Memory")[:300],
                        "path": str(row.get("path") or "")[:600],
                        "source": str(row.get("source") or "")[:300],
                        "snippet": str(row.get("text") or "")[:2400],
                        "score": score,
                        "source_type": str(row.get("source_type") or "")[:80],
                        "visibility": str(row.get("visibility") or "")[:40],
                        "provenance": [
                            str(value)[:600]
                            for value in list(row.get("provenance") or ())[:24]
                        ],
                    })
                return {"ok": True, "query": query, "results": results}
            if action == "map.read":
                if not viewer.may("praxis.snapshot"):
                    raise PermissionError("praxis.snapshot is required")
                name = str(body.get("map") or "").strip().upper()
                if name not in MAP_NAMES:
                    raise ValueError("unknown memory map")
                path = self.base / "memory" / "maps" / f"{name}.md"
                raw = path.read_bytes()
                limit = 128 * 1024
                content = raw[:limit].decode("utf-8", "replace")
                return {
                    "ok": True, "map": name, "path": f"memory/maps/{name}.md",
                    "content": content, "text": content,
                    "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                    "truncated": len(raw) > limit,
                }
            if action == "rebuild":
                if not viewer.may("praxis.work"):
                    raise PermissionError("praxis.work is required")
                import memory_catalog
                import memory_fts
                memory = self.base / "memory"
                def rebuild_memory() -> dict:
                    index = memory_catalog.rebuild(
                        memory_dir=memory, people_dir=memory / "people",
                        index_path=memory / "INDEX.md",
                    )
                    fts = memory_fts.rebuild(
                        base=self.base, memory_dir=memory,
                        skills_dir=self.base / "soul" / "skills",
                    )
                    return {
                        "ok": True,
                        "message": "memory maps and query index rebuilt",
                        "receipt": {
                            "at": _now(),
                            "index": str(index.resolve().relative_to(self.base).as_posix()),
                            "maps": list(MAP_NAMES),
                            "fts": {
                                key: fts.get(key)
                                for key in ("schema", "database", "sources", "chunks",
                                            "corrupt_lines", "fingerprint")
                            },
                        },
                    }

                return self._server_operation(
                    viewer, body, "memory.rebuild", {"maps": list(MAP_NAMES)},
                    rebuild_memory,
                )
            raise ValueError("unsupported memory action")
        if domain == "inventory":
            if action != "refresh":
                raise ValueError("unsupported inventory action")
            if not viewer.may("computer.read"):
                raise PermissionError("computer.read is required")
            import computer_inventory
            device = body_client.device_id()
            return self._server_operation(
                viewer, body, "inventory.refresh", {"device_id": device},
                lambda: computer_inventory.refresh(device),
            )
        if domain == "system":
            if action == "logs":
                if not viewer.may("praxis.system.read"):
                    raise PermissionError("praxis.system.read is required")
                try:
                    import panel
                    logs = (panel.server_state() or {}).get("logs") or {}
                except Exception:
                    logs = {}
                return {
                    "ok": True,
                    "items": [{"name": name, **row} for name, row in logs.items()],
                }
            if action == "restart":
                if not viewer.may("praxis.system.control"):
                    raise PermissionError("praxis.system.control is required")
                import selfdev
                reason = "Praxis mini-app: owner requested restart"

                def request_restart() -> dict:
                    selfdev.request_restart(reason)
                    return {"ok": True, "message": "restart request recorded"}

                return self._server_operation(
                    viewer, body, "system.restart", {"reason": reason},
                    request_restart,
                )
            raise ValueError("unsupported system action")
        if domain == "device":
            if action != "revoke":
                raise ValueError("unsupported device action")
            if not viewer.owner:
                raise PermissionError("device authority is owner-only")
            device_id = str(body.get("device_id") or "").strip()
            reason = str(body.get("reason") or "")[:200]
            return self._server_operation(
                viewer, body, "device.revoke",
                {"device_id": device_id, "reason": reason},
                lambda: self.revoke_device(viewer, device_id, reason=reason),
            )

        # Friendly UI domains compile through one declarative alias registry into
        # the same typed body capabilities used by generic computer callers.
        mapped = dict(body)
        alias = BODY_COMMAND_ALIASES.get((domain, action))
        if alias is not None:
            args = {
                key: mapped[key] for key in alias.arg_fields
                if mapped.get(key) not in (None, "")
            }
            for key, value in alias.defaults:
                args.setdefault(key, value)
            mapped.update({
                "domain": "computer", "capability": alias.capability, "args": args,
            })
            domain = "computer"
        elif domain in {"process", "files", "desktop"}:
            raise ValueError(f"unsupported {domain} action")

        if domain == "computer":
            capability = str(mapped.get("capability") or action).strip()
            policy = BODY_CAPABILITIES.get(capability)
            if policy is None:
                raise ValueError("unsupported computer capability")
            scope = policy.scope
            if not viewer.may(scope):
                raise PermissionError(f"{scope} is required")
            args = mapped.get("args") if isinstance(mapped.get("args"), dict) else {}
            execution = str(mapped.get("execution") or "interactive").strip().lower()
            if execution not in {"interactive", "system"}:
                raise ValueError("execution must be interactive or system")
            if execution == "system" and not viewer.owner:
                raise PermissionError("SYSTEM execution is sovereign-only")
            if policy.execution != "any" and execution != policy.execution:
                raise ValueError(f"{capability} requires {policy.execution} execution")
            timeout = max(1.0, min(float(mapped.get("timeout") or policy.timeout), 600.0))
            result = self._body_command(
                viewer, mapped, capability=capability, args=args,
                execution=execution, timeout=timeout,
            )
            if (policy.presentation and result.get("ok")
                    and isinstance(result.get("artifact"), dict)):
                return self._materialize_browser_artifact(
                    viewer, result, required_scope=scope,
                    presentation=policy.presentation,
                )
            return result
        raise ValueError("unsupported command")

    def _materialize_browser_artifact(
        self, viewer: Viewer, result: dict, *, required_scope: str,
        presentation: str = "download",
    ) -> dict:
        """Turn any provider-declared body artifact into one ticketed run artifact."""
        if presentation not in {"download", "image"}:
            raise ValueError("unsupported artifact presentation")
        artifact = result.get("artifact") if isinstance(result.get("artifact"), dict) else {}
        sha256 = str(artifact.get("sha256") or "").lower()
        size = artifact.get("size")
        name = str(artifact.get("name") or sha256)
        audit = result.get("praxis_audit") if isinstance(result.get("praxis_audit"), dict) else {}
        run_id = str(audit.get("run_id") or "")
        operation_id = str(audit.get("client_operation_id") or "")
        if (not re.fullmatch(r"[0-9a-f]{64}", sha256)
                or not isinstance(size, int) or isinstance(size, bool) or size < 0
                or not run_id or not operation_id):
            return {
                **result, "ok": False, "code": "bad_artifact_receipt",
                "error": "Windows capability returned no verifiable artifact receipt",
            }
        if size > PWA_FILE_MAX_BYTES:
            return {
                **result, "ok": False, "code": "pwa_file_too_large",
                "error": f"PWA download is limited to {PWA_FILE_MAX_BYTES} bytes",
                "max_bytes": PWA_FILE_MAX_BYTES,
            }
        lock = self._browser_artifact_locks[
            int(hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:8], 16)
            % len(self._browser_artifact_locks)
        ]
        with lock:
            existing = next((
                card for card in self._artifact_cards(viewer, run_id, issue_tickets=False)
                if card.get("sha256") == sha256 and card.get("name") == name
            ), None)
            if existing is None:
                stage_dir = self.base / "memory" / ".state" / "praxis_app" / "exports"
                stage_dir.mkdir(parents=True, exist_ok=True)
                if os.name != "nt":
                    os.chmod(stage_dir, 0o700)
                # Unique destinations keep separate service processes from
                # sharing an unaudited partial.  The run ledger performs the
                # final cross-process exact-once arbitration.
                stage = stage_dir / (
                    f".{run_id}-{sha256}-{secrets.token_hex(8)}.download"
                )
                try:
                    fetched = body_client.fetch_artifact(artifact, stage)
                    if not fetched.get("ok"):
                        return {
                            **result, "ok": False,
                            "code": str(fetched.get("code") or "pwa_export_fetch"),
                            "error": str(
                                fetched.get("error") or "artifact fetch failed"
                            )[:1000],
                        }
                    existing = self.runs.store_artifact(
                        run_id, stage, name=name,
                        media_type=str(
                            artifact.get("mime") or mimetypes.guess_type(name)[0]
                            or "application/octet-stream"
                        ),
                        idempotency_key=f"browser-artifact:{operation_id}",
                        expected_sha256=sha256,
                        expected_size=size,
                    )
                finally:
                    try:
                        stage.unlink(missing_ok=True)
                    except OSError:
                        pass

            artifact_id = str(existing.get("artifact_id") or "")
            if not artifact_id:
                return {
                    **result, "ok": False, "code": "pwa_export_ticket",
                    "error": "browser artifact has no durable identity",
                }
            download_key = (
                viewer.principal_id, run_id, artifact_id, required_scope, presentation,
            )
            cached = self._browser_artifact_downloads.get(download_key)
            if cached is not None:
                _issued_at, token, card = cached
                if ARTIFACT_TICKETS.authorize(
                    token, run_id, artifact_id, consume=False,
                ) is not None:
                    return {**result, "download": _json_clone(card)}
                self._browser_artifact_downloads.pop(download_key, None)

            def current_scope(ticket_viewer: Viewer) -> Viewer | None:
                return self.revalidate_ticket_viewer(
                    ticket_viewer, required_scope=required_scope,
                )

            ticket = ARTIFACT_TICKETS.issue(
                viewer, run_id, artifact_id,
                validator=current_scope,
                presentation=presentation,
            )
            download = {
                key: existing.get(key)
                for key in ("artifact_id", "name", "media_type", "size", "sha256")
            }
            download["download_url"] = (
                f"/api/praxis/v1/runs/{run_id}/artifacts/{artifact_id}?ticket={ticket}"
            )
            download["presentation"] = presentation
            if len(self._browser_artifact_downloads) >= ARTIFACT_TICKETS.per_principal:
                oldest = min(
                    self._browser_artifact_downloads,
                    key=lambda key: self._browser_artifact_downloads[key][0],
                )
                self._browser_artifact_downloads.pop(oldest, None)
            self._browser_artifact_downloads[download_key] = (
                time.monotonic(), ticket, _json_clone(download),
            )
            return {**result, "download": download}

    def _materialize_browser_export(self, viewer: Viewer, result: dict) -> dict:
        """Compatibility wrapper for older callers."""
        return self._materialize_browser_artifact(
            viewer, result, required_scope="computer.files", presentation="download",
        )

    def import_uploaded_file(
        self,
        viewer: Viewer,
        source: Path,
        *,
        name: str,
        media_type: str = "application/octet-stream",
        destination: str,
        execution: str,
        idempotency_key: str,
    ) -> dict:
        """Upload a bounded browser file to CAS and import it through exact body ids."""
        if not viewer.may("computer.files"):
            raise PermissionError("computer.files is required")
        execution = str(execution or "interactive").strip().lower()
        if execution not in {"interactive", "system"}:
            raise ValueError("execution must be interactive or system")
        if execution == "system" and not viewer.owner:
            raise PermissionError("SYSTEM execution is sovereign-only")
        path = Path(source)
        if not path.is_file() or path.stat().st_size > PWA_FILE_MAX_BYTES:
            raise ValueError("staged browser upload is missing or too large")
        target = str(destination or "").strip()
        if not target or len(target) > 32_767 or "\0" in target:
            raise ValueError("destination must be a valid non-empty Windows path")
        client_key = self._client_key(
            {"idempotency_key": idempotency_key}, required=True,
        )
        uploaded = body_client.upload_artifact(path, name=name, timeout=600)
        if not uploaded.get("ok"):
            return {
                **uploaded,
                "ok": False,
                "media_type": str(media_type or "application/octet-stream")[:200],
            }
        artifact = dict(uploaded.get("artifact") or {})
        # fs.import validates the downloaded bytes against artifact.sha256.
        # Its optional expected_sha256 is instead a compare-and-swap guard for
        # the file already present at the destination, so the artifact digest
        # must not be sent for a new browser-chosen path.
        args = {
            "artifact": artifact,
            "path": target,
        }
        receipt = self._body_command(
            viewer,
            {"idempotency_key": client_key},
            capability="fs.import",
            args=args,
            execution=execution,
            timeout=600,
        )
        return {
            **receipt,
            "upload": {
                key: uploaded[key] for key in (
                    "complete", "reused", "chunk_size", "resumed_offsets", "uploaded_offsets",
                ) if key in uploaded
            },
            "artifact": artifact,
            "destination": target,
        }


TICKETS = EventTickets()
ARTIFACT_TICKETS = ArtifactTickets()
