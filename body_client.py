"""Synchronous server-side client for the brainless Windows body.

The canonical Forge owns goals, tasks, workers and learning.  This module only
submits versioned operations to ``praxis-bridge`` and formats their receipts for
Forge's existing backend interface.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import hashlib
import json
import mimetypes
import ntpath
import os
import re
from pathlib import Path
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any


PROTOCOL = "praxis.body.v1"
DEFAULT_URL = "http://127.0.0.1:9473"
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "in_doubt"}
MIN_ARTIFACT_CHUNK_SIZE = 64 * 1024
# Сроки, которые сервер держит поверх собственных сроков тела.  Числа ниже — не выдумка
# серверной стороны, а копия того, что тело объявляет в своём `limits` (uia.rs, compose.rs,
# fsops.rs): тело клампит просьбу к этим границам само.  Сервер обязан ждать ДОЛЬШЕ тела —
# иначе честная частичная расписка, которую тело готовило все свои 45 секунд, приезжает к
# ней как «body response timeout», то есть как «тело умерло».  Живой случай: 13–14.07 две
# терминальные операции числились живыми ровно потому, что расписку никто не дождался.
WINDOW_READ_DEFAULT_TIMEOUT_MS = 1_800
WINDOW_READ_TIMEOUT_CEILING_MS = 20_000
WINDOW_READ_WORKER_GRACE_MS = 750
WINDOW_WAIT_DEFAULT_TIMEOUT_MS = 5_000
WINDOW_WAIT_TIMEOUT_CEILING_MS = 45_000
WINDOW_WAIT_PROBE_DEFAULT_TIMEOUT_MS = 1_200
WINDOW_WAIT_PROBE_TIMEOUT_CEILING_MS = 20_000
INPUT_SETTLE_CEILING_MS = 5_000
# `desktop.input.perform` уже жил с этим сроком; склейка «нажми и посмотри» кладёт поверх
# него паузу отстоя и чтение окна, а не заменяет его.
INPUT_WAIT_SECONDS = 60.0
# Обход дерева у fs.delete/fs.move/fs.copy тело обрывает само на 45 с и возвращает
# `complete:false` — это полезный ответ, и он обязан успеть доехать.
FS_TREE_BODY_SECONDS = 45
FS_TREE_WAIT_SECONDS = 60.0
# Дорога до тела и обратно поверх любого его собственного срока: WSS-пуш, очередь моста,
# HTTP-опрос расписки шагом 0.1 с.
BODY_TRANSPORT_GRACE_SECONDS = 12.0
MAX_ARTIFACT_CHUNK_SIZE = 16 * 1024 * 1024
MAX_ARTIFACT_SIZE = 16 * 1024 * 1024 * 1024 * 1024
_OBSERVATION: contextvars.ContextVar[tuple[dict, Any] | None] = contextvars.ContextVar(
    "praxis_body_observation", default=None,
)


@contextlib.contextmanager
def observation_context(task: dict, sink=None):
    """Bind body receipts to one canonical Forge task in this execution context."""
    if sink is None:
        import computer_memory
        sink = computer_memory.record
    context = {
        "task_id": str(task.get("task_id") or task.get("id") or ""),
        "device_id": str(task.get("device_id") or device_id()),
        "root": str(task.get("root") or task.get("target") or ""),
        "goal": str(task.get("goal") or ""),
    }
    token = _OBSERVATION.set((context, sink))
    try:
        yield
    finally:
        _OBSERVATION.reset(token)


def _observed(capability: str, args: dict, execution: str, result: dict) -> dict:
    bound = _OBSERVATION.get()
    if bound is None:
        # Direct computer calls outside Forge still belong to a durable server run.  Bind their
        # receipts to that run so the computer map records what Praxis actually explored instead
        # of silently dropping every non-coding desktop operation.
        try:
            import run_context
            current = run_context.current_run()
            if current is not None:
                import computer_memory
                bound = ({
                    "task_id": current.run_id,
                    "run_id": current.run_id,
                    "device_id": device_id(),
                    "root": "",
                    "goal": current.goal,
                }, computer_memory.record)
        except Exception:
            bound = None
    if bound is not None:
        context, sink = bound
        try:
            sink(context, capability, dict(args or {}), execution, dict(result or {}))
        except Exception:
            # Evidence recording must never turn a successful machine operation into a failure.
            pass
    return result


def _settings() -> tuple[str, str, str]:
    return (
        (os.getenv("PRAXIS_BODY_BRIDGE_URL") or DEFAULT_URL).rstrip("/"),
        os.getenv("PRAXIS_BODY_CONTROLLER_TOKEN") or "",
        os.getenv("PRAXIS_BODY_DEVICE") or "windows-pc",
    )


def device_id() -> str:
    return _settings()[2]


def available() -> bool:
    _url, token, device = _settings()
    return bool(token and device)


def _request(method: str, path: str, payload: dict | None = None,
             timeout: float = 30.0) -> dict:
    base, token, _device = _settings()
    if not token:
        return {"ok": False, "code": "unavailable",
                "error": "PRAXIS_BODY_CONTROLLER_TOKEN is not configured"}
    raw = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        base + path, data=raw, method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            try:
                body = json.loads(exc.read().decode("utf-8", "replace"))
            except (ValueError, OSError):
                body = {"error": str(exc)}
        finally:
            exc.close()
        return {"ok": False, "code": f"http_{exc.code}", **body}
    except (OSError, ValueError) as exc:
        return {"ok": False, "code": "transport",
                "error": f"body bridge: {type(exc).__name__}: {exc}"}


def submit(capability: str, args: dict | None = None, *, execution: str = "interactive",
           request_id: str = "", operation_id: str = "", deadline: str = "",
           timeout: float = 30.0) -> dict:
    _url, _token, device = _settings()
    request_id = request_id or uuid.uuid4().hex
    operation_id = operation_id or f"op-{uuid.uuid4().hex}"
    payload = {
        "request_id": request_id,
        "operation_id": operation_id,
        "execution": execution,
        "capability": capability,
        "args": args or {},
    }
    if deadline:
        payload["deadline"] = deadline
    result = _request(
        "POST", f"/v1/controller/{urllib.parse.quote(device, safe='')}/invoke",
        payload, timeout=timeout,
    )
    result.setdefault("request_id", request_id)
    result.setdefault("operation_id", operation_id)
    return result


def response(request_id: str, *, timeout: float = 30.0) -> dict:
    _url, _token, device = _settings()
    return _request(
        "GET",
        f"/v1/controller/{urllib.parse.quote(device, safe='')}/requests/"
        f"{urllib.parse.quote(request_id, safe='')}",
        timeout=timeout,
    )


def call(capability: str, args: dict | None = None, *, execution: str = "interactive",
         request_id: str = "", operation_id: str = "", timeout: float = 60.0) -> dict:
    call_args = dict(args or {})
    wire_deadline = (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=max(0.1, float(timeout)))
    ).isoformat().replace("+00:00", "Z")
    started = submit(capability, args, execution=execution, request_id=request_id,
                     operation_id=operation_id, deadline=wire_deadline,
                     timeout=min(timeout, 30.0))
    if not started.get("ok"):
        return _observed(capability, call_args, execution, started)
    rid = str(started["request_id"])
    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        observed = response(rid, timeout=min(30.0, max(1.0, deadline - time.monotonic())))
        frame = observed.get("response") if observed.get("ok") else None
        if isinstance(frame, dict):
            kind = str(frame.get("type") or "")
            if kind == "error":
                return _observed(capability, call_args, execution, {
                    "ok": False, "code": frame.get("code"),
                    "error": frame.get("message"), **frame,
                })
            if kind == "result":
                result = frame.get("result") if isinstance(frame.get("result"), dict) else {}
                return _observed(capability, call_args, execution, {
                    **frame, **result, "ok": bool(frame.get("ok")),
                })
            if kind in {"accepted", "progress", "cancelled"}:
                return _observed(capability, call_args, execution, {"ok": True, **frame})
        if not observed.get("ok"):
            return _observed(capability, call_args, execution, observed)
        time.sleep(.1)
    return _observed(capability, call_args, execution, {
        "ok": False, "code": "timeout",
        "error": f"body response timeout after {timeout}s",
        "request_id": rid, "operation_id": started.get("operation_id"),
    })


def fetch_artifact(artifact: dict, destination: str | Path, *, chunk_size: int = 4 * 1024 * 1024) -> dict:
    """Download a body-exported artifact to the server and verify its exact hash/size."""
    sha256 = str((artifact or {}).get("sha256") or "").lower()
    raw_size = (artifact or {}).get("size")
    try:
        size = int(raw_size)
    except (TypeError, ValueError):
        size = -1
    if (not re.fullmatch(r"[0-9a-f]{64}", sha256)
            or isinstance(raw_size, bool) or size < 0):
        return {"ok": False, "code": "bad_artifact", "error": "artifact needs sha256 and size"}
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    stage = path.with_name(f".{path.name}.praxis-{sha256[:12]}.part")
    committed = False
    try:
        try:
            staged_size = stage.stat().st_size
        except FileNotFoundError:
            staged_size = 0
        offset = staged_size if 0 < staged_size <= size else 0
        if stage.exists() and offset == 0:
            stage.unlink()
        base, token, _device = _settings()
        with stage.open("ab") as output:
            while offset < size:
                length = min(max(64 * 1024, int(chunk_size)), size - offset)
                url = (f"{base}/v1/artifacts/{sha256}/content?" +
                       urllib.parse.urlencode({"offset": offset, "length": length}))
                request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
                with urllib.request.urlopen(request, timeout=60) as response:
                    chunk = response.read(length + 1)
                if not chunk:
                    raise OSError(f"empty artifact chunk at {offset}")
                if len(chunk) > length:
                    raise OSError(
                        f"artifact chunk at {offset} exceeds requested length {length}"
                    )
                output.write(chunk)
                output.flush()
                offset += len(chunk)
        actual_size = stage.stat().st_size
        digest = hashlib.sha256()
        with stage.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        actual_sha = digest.hexdigest()
        if actual_size != size or actual_sha != sha256:
            stage.unlink(missing_ok=True)
            return {"ok": False, "code": "artifact_mismatch",
                    "error": f"expected {sha256}/{size}, got {actual_sha}/{actual_size}"}
        os.replace(stage, path)
        committed = True
        return {"ok": True, "path": str(path), "sha256": sha256, "size": size}
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        return {"ok": False, "code": "artifact_download", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        # A retry starts from the body CAS again.  Keeping a failed partial in a
        # private server directory indefinitely both leaks bytes and makes a
        # later call trust an unaudited prefix from an earlier transport.
        if not committed:
            try:
                stage.unlink(missing_ok=True)
            except OSError:
                pass


def _artifact_failure(code: str, error: object, *, stage: str,
                      artifact: dict | None = None, **evidence) -> dict:
    result = {
        "ok": False,
        "code": str(code or "artifact_upload"),
        "stage": str(stage),
        "error": str(error or "artifact transfer failed"),
    }
    if artifact is not None:
        result["artifact"] = dict(artifact)
    result.update(evidence)
    return result


def _portable_artifact_name(value: object) -> tuple[str | None, str | None]:
    try:
        name = str(value)
        encoded = name.encode("utf-8")
    except (TypeError, UnicodeError) as exc:
        return None, f"artifact name is not UTF-8: {type(exc).__name__}"
    if not name.strip() or name in {".", ".."}:
        return None, "artifact name must not be empty or a dot path"
    if len(name) > 255 or len(encoded) > 1024:
        return None, "artifact name exceeds the portable Windows limit"
    forbidden = '<>:"/\\|?*'
    if (name.endswith((" ", "."))
            or any(char in forbidden or unicodedata.category(char).startswith("C")
                   for char in name)):
        return None, "artifact name must be one portable file name"
    stem = name.split(".", 1)[0].upper()
    numbered = stem[3:] if stem.startswith(("COM", "LPT")) else ""
    if (stem in {"CON", "PRN", "AUX", "NUL"}
            or (stem.startswith(("COM", "LPT")) and numbered in set("123456789"))):
        return None, "artifact name is reserved by Windows"
    return name, None


def _file_snapshot(source, path: Path) -> tuple[int, int, int]:
    """Return stable-enough identity for detecting mutation while one descriptor is open."""
    try:
        value = os.fstat(source.fileno())
    except (AttributeError, OSError):
        value = path.stat()
    return (
        int(value.st_size),
        int(getattr(value, "st_mtime_ns", int(value.st_mtime * 1_000_000_000))),
        int(getattr(value, "st_ctime_ns", int(value.st_ctime * 1_000_000_000))),
    )


def _read_exact(source, size: int) -> bytes:
    """Accumulate ordinary short reads without ever buffering more than one CAS chunk."""
    output = bytearray()
    while len(output) < size:
        block = source.read(size - len(output))
        if not block:
            break
        if not isinstance(block, (bytes, bytearray, memoryview)):
            raise TypeError("artifact source returned non-bytes data")
        output.extend(block)
    return bytes(output)


def _artifact_json_request(method: str, path: str, payload: dict | None = None,
                           *, timeout: float = 120.0) -> dict:
    try:
        result = _request(method, path, payload, timeout=timeout)
    except (OSError, TypeError, ValueError, UnicodeError) as exc:
        return {"ok": False, "code": "transport",
                "error": f"body bridge: {type(exc).__name__}: {exc}"}
    if not isinstance(result, dict):
        return {"ok": False, "code": "artifact_protocol",
                "error": "body bridge returned a non-object JSON response"}
    return result


def _artifact_chunk_request(sha256: str, offset: int, chunk: bytes, artifact: dict,
                            *, timeout: float) -> dict:
    base, token, _device = _settings()
    if not token:
        return {"ok": False, "code": "unavailable",
                "error": "PRAXIS_BODY_CONTROLLER_TOKEN is not configured"}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
        "x-praxis-total-size": str(artifact["size"]),
        "x-praxis-name-hex": str(artifact["name"]).encode("utf-8").hex(),
        "x-praxis-chunk-sha256": hashlib.sha256(chunk).hexdigest(),
        "x-praxis-device-id": str(artifact.get("source_device") or "praxis-server"),
        "x-praxis-mime": str(artifact.get("mime") or "application/octet-stream"),
    }
    try:
        request = urllib.request.Request(
            f"{base}/v1/artifacts/{sha256}/chunks/{offset}",
            data=chunk, method="PUT", headers=headers,
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("body bridge returned a non-object chunk response")
        return result
    except urllib.error.HTTPError as exc:
        try:
            try:
                body = json.loads(exc.read().decode("utf-8", "replace"))
            except (OSError, UnicodeError, ValueError):
                body = {}
        finally:
            exc.close()
        detail = body.get("error") if isinstance(body, dict) else None
        return {"ok": False, "code": f"http_{exc.code}",
                "error": str(detail or f"HTTP {exc.code} while uploading artifact chunk")}
    except (OSError, TypeError, ValueError, UnicodeError) as exc:
        return {"ok": False, "code": "transport",
                "error": f"body bridge: {type(exc).__name__}: {exc}"}


def _matching_artifact(value: object, sha256: str, size: int) -> str | None:
    if not isinstance(value, dict):
        return "body bridge returned no artifact metadata"
    actual_sha = str(value.get("sha256") or "").lower()
    actual_size = value.get("size")
    if (not re.fullmatch(r"[0-9a-f]{64}", actual_sha)
            or not isinstance(actual_size, int) or isinstance(actual_size, bool)):
        return "body bridge returned malformed artifact metadata"
    if actual_sha != sha256 or actual_size != size:
        return f"expected artifact {sha256}/{size}, got {actual_sha}/{actual_size}"
    return None


def _artifact_status(sha256: str, size: int, *, timeout: float) -> dict:
    result = _artifact_json_request(
        "GET", f"/v1/artifacts/{sha256}", timeout=timeout,
    )
    if result.get("ok") is False:
        return result
    mismatch = _matching_artifact(result.get("artifact"), sha256, size)
    if mismatch:
        return {"ok": False, "code": "artifact_mismatch", "error": mismatch}
    complete = result.get("complete")
    chunk_size = result.get("chunk_size")
    offsets = result.get("received_offsets")
    if not isinstance(complete, bool):
        return {"ok": False, "code": "artifact_protocol",
                "error": "body bridge returned no complete flag"}
    if (not isinstance(chunk_size, int) or isinstance(chunk_size, bool)
            or not MIN_ARTIFACT_CHUNK_SIZE <= chunk_size <= MAX_ARTIFACT_CHUNK_SIZE):
        return {"ok": False, "code": "artifact_protocol",
                "error": "body bridge returned an invalid artifact chunk_size"}
    if not isinstance(offsets, list):
        return {"ok": False, "code": "artifact_protocol",
                "error": "body bridge returned invalid received_offsets"}
    expected_chunks = 0 if size == 0 else (size + chunk_size - 1) // chunk_size
    received: set[int] = set()
    for offset in offsets:
        if (not isinstance(offset, int) or isinstance(offset, bool)
                or offset < 0 or offset >= size or offset % chunk_size
                or offset in received):
            return {"ok": False, "code": "artifact_protocol",
                    "error": f"body bridge returned an invalid resume offset: {offset!r}"}
        received.add(offset)
    if len(received) > expected_chunks:
        return {"ok": False, "code": "artifact_protocol",
                "error": "body bridge returned too many resume offsets"}
    return {
        "ok": True, "complete": complete, "chunk_size": chunk_size,
        "received_offsets": received, "server_artifact": dict(result["artifact"]),
    }


def upload_artifact(source: str | Path, name: str | None = None, *,
                    timeout: float = 120.0) -> dict:
    """Stream one server file into bridge CAS, resuming on its authoritative chunk grid."""
    _base, token, _device = _settings()
    if not token:
        return _artifact_failure(
            "unavailable", "PRAXIS_BODY_CONTROLLER_TOKEN is not configured", stage="config",
        )
    try:
        path = Path(source)
    except TypeError as exc:
        return _artifact_failure("bad_source", type(exc).__name__, stage="source")
    if not path.is_file():
        return _artifact_failure(
            "bad_source", "artifact source is not a regular file", stage="source",
        )
    selected_name, name_error = _portable_artifact_name(path.name if name is None else name)
    if name_error:
        return _artifact_failure("bad_artifact_name", name_error, stage="source")

    try:
        with path.open("rb") as input_file:
            before = _file_snapshot(input_file, path)
            size = before[0]
            if size < 0 or size > MAX_ARTIFACT_SIZE:
                return _artifact_failure(
                    "bad_artifact", f"artifact size {size} exceeds the client limit", stage="source",
                )
            digest = hashlib.sha256()
            counted = 0
            while True:
                block = input_file.read(1024 * 1024)
                if not block:
                    break
                if not isinstance(block, (bytes, bytearray, memoryview)):
                    return _artifact_failure(
                        "bad_source", "artifact source returned non-bytes data", stage="hash",
                    )
                digest.update(block)
                counted += len(block)
            after_hash = _file_snapshot(input_file, path)
            if counted != size or after_hash != before:
                return _artifact_failure(
                    "artifact_source_changed", "artifact source changed while it was hashed",
                    stage="hash",
                )
            sha256 = digest.hexdigest()
            artifact = {
                "sha256": sha256,
                "size": size,
                "name": selected_name,
                "source_device": "praxis-server",
            }
            mime = mimetypes.guess_type(selected_name)[0]
            if mime:
                artifact["mime"] = mime

            offer = _artifact_json_request(
                "POST", f"/v1/artifacts/{sha256}/offer", artifact, timeout=timeout,
            )
            if offer.get("ok") is not True:
                return _artifact_failure(
                    str(offer.get("code") or "artifact_offer"),
                    offer.get("error") or "artifact offer was rejected",
                    stage="offer", artifact=artifact,
                )
            status = _artifact_status(sha256, size, timeout=timeout)
            if not status.get("ok"):
                return _artifact_failure(
                    str(status.get("code") or "artifact_status"), status.get("error"),
                    stage="status", artifact=artifact,
                )
            if status["complete"]:
                return {
                    "ok": True, "artifact": artifact, "complete": True, "reused": True,
                    "chunk_size": status["chunk_size"], "resumed_offsets": [],
                    "uploaded_offsets": [],
                }

            chunk_size = int(status["chunk_size"])
            received = set(status["received_offsets"])
            uploaded: list[int] = []
            input_file.seek(0)
            verifier = hashlib.sha256()
            offset = 0
            while offset < size:
                take = min(chunk_size, size - offset)
                chunk = _read_exact(input_file, take)
                if len(chunk) != take:
                    return _artifact_failure(
                        "artifact_source_changed",
                        f"short artifact read at {offset}: expected {take}, got {len(chunk)}",
                        stage="read", artifact=artifact,
                        resumed_offsets=sorted(received), uploaded_offsets=uploaded,
                    )
                verifier.update(chunk)
                if offset not in received:
                    response = _artifact_chunk_request(
                        sha256, offset, chunk, artifact, timeout=timeout,
                    )
                    if response.get("ok") is not True:
                        return _artifact_failure(
                            str(response.get("code") or "artifact_chunk"),
                            response.get("error") or f"artifact chunk {offset} was rejected",
                            stage="chunk", artifact=artifact,
                            resumed_offsets=sorted(received), uploaded_offsets=uploaded,
                        )
                    if (str(response.get("sha256") or "").lower() != sha256
                            or response.get("offset") != offset
                            or response.get("size") != len(chunk)):
                        return _artifact_failure(
                            "artifact_mismatch", "body bridge acknowledged the wrong artifact chunk",
                            stage="chunk", artifact=artifact,
                            resumed_offsets=sorted(received), uploaded_offsets=uploaded,
                        )
                    uploaded.append(offset)
                offset += take
            extra = input_file.read(1)
            after_upload = _file_snapshot(input_file, path)
            if extra or verifier.hexdigest() != sha256 or after_upload != before:
                return _artifact_failure(
                    "artifact_source_changed", "artifact source changed while it was uploaded",
                    stage="verify", artifact=artifact,
                    resumed_offsets=sorted(received), uploaded_offsets=uploaded,
                )

            completed = _artifact_json_request(
                "POST", f"/v1/artifacts/{sha256}/complete", timeout=timeout,
            )
            if completed.get("ok") is not True:
                return _artifact_failure(
                    str(completed.get("code") or "artifact_complete"),
                    completed.get("error") or "artifact completion was rejected",
                    stage="complete", artifact=artifact,
                    resumed_offsets=sorted(received), uploaded_offsets=uploaded,
                )
            if completed.get("artifact") is not None:
                mismatch = _matching_artifact(completed.get("artifact"), sha256, size)
            else:
                returned_sha = str(completed.get("sha256") or "").lower()
                mismatch = None if returned_sha == sha256 else "completion returned the wrong hash"
            if mismatch:
                return _artifact_failure(
                    "artifact_mismatch", mismatch, stage="complete", artifact=artifact,
                    resumed_offsets=sorted(received), uploaded_offsets=uploaded,
                )
            final = _artifact_status(sha256, size, timeout=timeout)
            if not final.get("ok") or not final.get("complete"):
                return _artifact_failure(
                    str(final.get("code") or "artifact_incomplete"),
                    final.get("error") or "body bridge did not confirm a complete artifact",
                    stage="verify", artifact=artifact,
                    resumed_offsets=sorted(received), uploaded_offsets=uploaded,
                )
            return {
                "ok": True, "artifact": artifact, "complete": True, "reused": False,
                "chunk_size": final["chunk_size"],
                "resumed_offsets": sorted(received), "uploaded_offsets": uploaded,
            }
    except FileNotFoundError:
        return _artifact_failure("bad_source", "artifact source does not exist", stage="source")
    except (OSError, TypeError, ValueError, UnicodeError) as exc:
        return _artifact_failure(
            "artifact_source", f"{type(exc).__name__}: {exc}", stage="source",
        )


def import_artifact_to_windows(source: str | Path, destination: str | Path,
                               execution: str = "interactive", *, name: str | None = None,
                               timeout: float = 600.0) -> dict:
    """Upload a server file to bridge CAS, then import that exact ref on Windows."""
    destination_text = str(destination or "")
    if not destination_text.strip():
        return _artifact_failure("bad_destination", "Windows destination is empty", stage="import")
    uploaded = upload_artifact(source, name=name)
    if not uploaded.get("ok"):
        return uploaded
    artifact = dict(uploaded["artifact"])
    result = call(
        "fs.import", {"artifact": artifact, "path": destination_text},
        execution=execution, timeout=timeout,
    )
    if not isinstance(result, dict):
        return _artifact_failure(
            "artifact_import", "Windows body returned a non-object import receipt",
            stage="import", artifact=artifact,
        )
    combined = dict(result)
    combined["upload"] = {
        key: uploaded[key] for key in (
            "complete", "reused", "chunk_size", "resumed_offsets", "uploaded_offsets",
        ) if key in uploaded
    }
    combined.setdefault("artifact", artifact)
    combined.setdefault("path", destination_text)
    if combined.get("ok"):
        mismatch = _matching_artifact(combined.get("artifact"), artifact["sha256"], artifact["size"])
        if mismatch or combined.get("artifact", {}).get("name") != artifact["name"]:
            return {
                "ok": False, "code": "artifact_import_mismatch", "stage": "import",
                "error": mismatch or "Windows body returned a different artifact name",
                "artifact": artifact, "upload": combined["upload"],
                "import_receipt": result,
            }
    return combined


def process_start(*, program: str = "", args: list[str] | None = None,
                  command: str = "", shell: str = "direct", cwd: str = "",
                  env: dict[str, str] | None = None, timeout: int = 0,
                  name: str = "", execution: str = "interactive",
                  request_id: str = "") -> dict:
    payload: dict[str, Any] = {
        "shell": shell, "args": args or [], "env": env or {}, "timeout_s": int(timeout or 0),
    }
    if program:
        payload["program"] = program
    if command:
        payload["command"] = command
    if cwd:
        payload["cwd"] = cwd
    if name:
        payload["name"] = name
    return call("process.start", payload, execution=execution,
                request_id=request_id, timeout=60)


def process_status(operation_id: str, tail: int = 16000,
                   execution: str = "interactive") -> dict:
    return call("process.status", {"operation_id": operation_id, "tail": int(tail)},
                execution=execution, timeout=60)


def desktop_status(*, execution: str = "interactive") -> dict:
    """Return the interactive desktop/session state exposed by the Windows body."""
    return call("desktop.status", {}, execution=execution, timeout=30)


def desktop_window_list(*, offset: int = 0, limit: int = 2048,
                        visible_only: bool = True, pid: int | None = None,
                        title_contains: str = "",
                        execution: str = "interactive") -> dict:
    """List visible top-level windows without relying on UI Automation or COM."""
    payload: dict[str, Any] = {
        "offset": max(0, int(offset)), "limit": max(1, int(limit)),
        "visible_only": bool(visible_only), "title_contains": str(title_contains or ""),
    }
    if pid is not None:
        payload["pid"] = int(pid)
    return call("desktop.window.list", payload, execution=execution, timeout=30)


def desktop_window_activate(hwnd: str | int, *, expected_pid: int | None = None,
                            restore: bool = True, timeout_ms: int = 1500,
                            execution: str = "interactive") -> dict:
    """Bring one top-level window, identified by its native handle, to the foreground."""
    payload: dict[str, Any] = {
        "hwnd": hwnd, "restore": bool(restore), "timeout_ms": int(timeout_ms),
    }
    if expected_pid is not None:
        payload["expected_pid"] = int(expected_pid)
    return call("desktop.window.activate", payload, execution=execution, timeout=30)


def _clamped_ms(value: object, default: int, ceiling: int) -> int:
    """Повторить кламп тела, чтобы посчитать СВОЙ срок ожидания, а не чужой.

    Тело подтягивает просьбу к своей границе и объявляет правку в `notes`.  Сервер обязан
    посчитать ожидание по тому же числу: иначе она просит `timeout_ms=600000`, тело честно
    урезает до 20 с, а сервер уходит ждать десять минут.
    """
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(number, ceiling))


def _with_server_frame(result: dict, *, wait_s: float, truth_field: str = "",
                       truth_note: str = "") -> dict:
    """Приписать к расписке тела серверный срок ожидания и назвать поле правды.

    Два закона сразу.  Второй: срок, который держит сервер, — такой же предел, как кап узлов
    внутри тела, и он обязан приезжать в ответе, а не жить молча в сигнатуре функции.
    Третий: `call()` (строка «**frame, **result, ok=frame.ok») затирает собственный `ok`
    тела рамкой транспорта, поэтому у глаголов с частичным успехом — fs.delete/move/copy
    (`complete`), desktop.window.wait (`met`) — «ok: true» значит только «ответ доехал».
    Молча оставить это знание в чужом отчёте нельзя: первый же `if result["ok"]` объявит
    недоделанное сделанным, а это ровно тот сорт вранья, из-за которого затеяна волна.
    """
    if not isinstance(result, dict):
        return result
    frame: dict[str, Any] = {"wait_s": round(float(wait_s), 3)}
    if truth_field:
        frame["truth_field"] = truth_field
        if truth_field in result:
            frame["truth"] = result.get(truth_field)
            frame["note"] = truth_note or (
                f"ok здесь значит «ответ доехал»; сделано ли дело — в поле {truth_field}"
            )
        else:
            frame["note"] = (
                f"поле {truth_field} в расписке отсутствует — это ответ не того вида, "
                "который обещал глагол (ошибка транспорта или отказ до начала работы)"
            )
    result["server_side"] = frame
    return result


def _input_payload(events: list[dict], *, expected_foreground: str | int | None,
                   expected_pid: int | None, inter_event_delay_ms: int) -> dict:
    """Собрать аргументы `desktop.input.perform` ровно один раз на две точки входа.

    Склейка `desktop.input.perform_and_read` внутри тела отдаёт поле `input` тому же
    разборщику ввода, что и одиночный глагол.  Значит и сервер обязан собирать его тем же
    кодом: разойдись эти две сборки хоть на одно имя поля — и составной глагол начнёт
    падать на аргументе, который в одиночном работает.
    """
    payload: dict[str, Any] = {
        "events": list(events or []),
        "inter_event_delay_ms": max(0, int(inter_event_delay_ms)),
    }
    if expected_foreground is not None:
        payload["expected_foreground"] = expected_foreground
    if expected_pid is not None:
        payload["expected_pid"] = int(expected_pid)
    return payload


def desktop_input_perform(events: list[dict], *, expected_foreground: str | int | None = None,
                          expected_pid: int | None = None,
                          inter_event_delay_ms: int = 0,
                          execution: str = "interactive") -> dict:
    """Perform one ordered native keyboard/mouse batch on the interactive desktop.

    Пачка непрозрачна для сервера: `mouse_down`/`mouse_up`/`drag` появились в теле v2 и
    проезжают здесь без единой правки — но её описание тула обязано знать, что зажатая
    кнопка НЕ переживает вызов (тело отпускает её на выходе и называет в
    `buttons_auto_released`), а координаты вне виртуального экрана подтягиваются к краю
    и считаются в `clamped_moves`.
    """
    payload = _input_payload(events, expected_foreground=expected_foreground,
                             expected_pid=expected_pid,
                             inter_event_delay_ms=inter_event_delay_ms)
    return call("desktop.input.perform", payload, execution=execution, timeout=60)


def _window_read_payload(*, hwnd: str | int | None, backend: str, shape: str,
                         text_contains: str, visible_only: bool | None,
                         max_nodes: int | None, max_depth: int | None,
                         max_children_per_node: int | None, max_text_chars: int | None,
                         timeout_ms: int | None) -> dict:
    """Собрать аргументы читалки окна, не подставляя ни одного умолчания за неё.

    У читалки `deny_unknown_fields`, и все поля необязательны.  Посылать сюда «дефолты»
    сервера было бы враньём дважды: тело перестало бы отличать «она не просила» от «она
    попросила ровно столько», и её просьба вне границ уже не попала бы в `notes` как правка.
    """
    payload: dict[str, Any] = {}
    if hwnd is not None and str(hwnd).strip() != "":
        payload["hwnd"] = hwnd
    if backend:
        payload["backend"] = str(backend)
    if shape:
        payload["shape"] = str(shape)
    if text_contains:
        payload["text_contains"] = str(text_contains)
    if visible_only is not None:
        payload["visible_only"] = bool(visible_only)
    for key, value in (("max_nodes", max_nodes), ("max_depth", max_depth),
                       ("max_children_per_node", max_children_per_node),
                       ("max_text_chars", max_text_chars), ("timeout_ms", timeout_ms)):
        if value is not None:
            payload[key] = int(value)
    return payload


def _window_read_wait_seconds(timeout_ms: object = None) -> float:
    budget = _clamped_ms(timeout_ms, WINDOW_READ_DEFAULT_TIMEOUT_MS,
                         WINDOW_READ_TIMEOUT_CEILING_MS)
    return round((budget + WINDOW_READ_WORKER_GRACE_MS) / 1000.0
                 + BODY_TRANSPORT_GRACE_SECONDS, 3)


def desktop_window_read(*, hwnd: str | int | None = None, backend: str = "",
                        shape: str = "", text_contains: str = "",
                        visible_only: bool | None = None, max_nodes: int | None = None,
                        max_depth: int | None = None,
                        max_children_per_node: int | None = None,
                        max_text_chars: int | None = None, timeout_ms: int | None = None,
                        execution: str = "interactive") -> dict:
    """Прочитать окно текстом через UI Automation, с названным откатом на обычный Win32.

    Ради этого глагол и заведён: раньше, чтобы узнать надпись на кнопке, требовался снимок
    экрана (2.4 с на пяти раундтрипах) плюс вызов зрения.  Расписка возвращается КАК ЕСТЬ,
    структурой: `limits`, `notes`, `truncated_by`, `total_known`, `fallback_reason` — это её
    единственный источник знания о том, что именно она сейчас видит и чего не видит.
    Заворачивать это в строку «[windows-body] …», как сделано с `impact`, нельзя.
    """
    payload = _window_read_payload(
        hwnd=hwnd, backend=backend, shape=shape, text_contains=text_contains,
        visible_only=visible_only, max_nodes=max_nodes, max_depth=max_depth,
        max_children_per_node=max_children_per_node, max_text_chars=max_text_chars,
        timeout_ms=timeout_ms,
    )
    wait = _window_read_wait_seconds(timeout_ms)
    return _with_server_frame(
        call("desktop.window.read", payload, execution=execution, timeout=wait),
        wait_s=wait, truth_field="total_known",
        truth_note=("ok значит «окно нашлось и ответ доехал»; всё ли окно прочитано — "
                    "в total_known, truncated_by и nodes_read/discovered_unread"),
    )


# Ключи расписки читалки, у которых есть своя строка в тексте ниже.  Всё, чего в этом
# наборе нет, печатается отдельным блоком «прочее» — чтобы поле, добавленное в теле завтра,
# не исчезло молча в свёртке сегодня.  Это и есть цена закона 2 в текстовом виде.
_WINDOW_READ_RENDERED = frozenset({
    "capability", "backend", "backend_detail", "fallback_reason", "window", "shape",
    "root", "items", "nodes_read", "nodes_returned", "discovered_unread",
    "subtrees_unread", "skipped_offscreen", "depth_reached", "truncated", "truncated_by",
    "total_known", "limits", "elapsed_ms", "walk_ms", "coordinates", "state_semantics",
    "ids", "filter", "notes",
    # `result` — байт-в-байт тот же объект, который `call()` уже расплющил в верхний
    # уровень: печатать его вторым экземпляром значило бы удвоить весь ответ.
    "result",
})
_WINDOW_READ_NODE_RENDERED = frozenset({
    "id", "parent", "depth", "role", "name", "value", "automation_id", "class",
    "localized_role", "hwnd", "rect", "state", "text_truncated", "children_unread",
    "children",
})


def _window_read_node_lines(node: object, out: list[str], indent: str, level: int) -> None:
    if not isinstance(node, dict):
        out.append(f"{indent * level}? {json.dumps(node, ensure_ascii=False)}")
        return
    parts = [f"#{node.get('id')}"]
    if "depth" in node:
        parts.append(f"d{node['depth']}")
    if "parent" in node:
        parts.append(f"p{node['parent']}")
    parts.append(str(node.get("role") or "?"))
    if node.get("name"):
        parts.append(f"«{node['name']}»")
    if node.get("value"):
        parts.append(f"value=«{node['value']}»")
    for key, label in (("automation_id", "aid"), ("class", "class"),
                       ("localized_role", "role_ru"), ("hwnd", "hwnd")):
        if node.get(key):
            parts.append(f"{label}={node[key]}")
    rect = node.get("rect") if isinstance(node.get("rect"), dict) else None
    if rect:
        centre = rect.get("center") if isinstance(rect.get("center"), dict) else {}
        if centre:
            # Именно этой точкой она и тыкает: центр — единственное, что можно отдать
            # в desktop.input.perform без пересчёта.
            parts.append(f"@{centre.get('x')},{centre.get('y')}")
        if all(key in rect for key in ("left", "top", "width", "height")):
            parts.append(f"rect={rect['left']},{rect['top']}+{rect['width']}x{rect['height']}")
        for key in sorted(set(rect) - {"left", "top", "width", "height", "center"}):
            parts.append(f"rect.{key}={json.dumps(rect[key], ensure_ascii=False)}")
    state = node.get("state") if isinstance(node.get("state"), dict) else None
    if state:
        # Отсутствующий ключ состояния значит «элемент этого не отдаёт», а не «false»,
        # поэтому печатаем ровно то, что пришло, и ничего не достраиваем.
        parts.append("state:" + ",".join(
            f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in state.items()
        ))
    if node.get("text_truncated"):
        parts.append("text_truncated")
    if node.get("children_unread"):
        parts.append(f"children_unread={node['children_unread']}")
    for key in sorted(set(node) - _WINDOW_READ_NODE_RENDERED):
        parts.append(f"{key}={json.dumps(node[key], ensure_ascii=False)}")
    out.append(indent * level + " ".join(parts))
    for child in node.get("children") or []:
        _window_read_node_lines(child, out, indent, level + 1)


def format_window_read(result: dict, *, indent: str = "  ",
                       leftover_chars: int = 600) -> str:
    """Свернуть расписку `desktop.window.read` в текст, ничего не потеряв молча.

    Смысл глагола — быть дешевле снимка экрана; расписка на 400 узлов в JSON-отступах стоит
    сотню килобайт и съедает выигрыш.  Поэтому здесь есть текстовая свёртка — но она обязана
    выдерживать тот же закон, что и сама расписка: любой предел, срез и оговорка печатаются,
    а любое поле, которого эта функция не знает, попадает в блок «прочее».  Иначе следующее
    поле, добавленное в теле, умрёт тихо — ровно так, как умер `reflexes['mine']`.
    """
    if not isinstance(result, dict):
        return f"[windows-body] нечитаемая расписка: {result!r}"
    if not result.get("ok"):
        detail = result.get("error") or result.get("code") or result
        return f"[windows-body] desktop.window.read не ответил: {detail}"
    lines: list[str] = []
    head = f"desktop.window.read: backend={result.get('backend')}"
    if result.get("backend_detail"):
        head += f" ({result['backend_detail']})"
    if result.get("fallback_reason"):
        head += f"; откат потому что: {result['fallback_reason']}"
    lines.append(head)
    window = result.get("window") if isinstance(result.get("window"), dict) else {}
    if window:
        rect = window.get("rect") if isinstance(window.get("rect"), dict) else {}
        lines.append(
            f"окно {window.get('hwnd')} «{window.get('title')}» class={window.get('class')} "
            f"pid={window.get('pid')} rect={rect.get('left')},{rect.get('top')}"
            f"+{rect.get('width')}x{rect.get('height')} "
            f"foreground={window.get('foreground')} "
            f"resolved_from={window.get('resolved_from')}"
        )
    lines.append(
        f"узлов прочитано {result.get('nodes_read')}, отдано {result.get('nodes_returned')}; "
        f"глубина {result.get('depth_reached')}; не раскрыто узлов "
        f"{result.get('discovered_unread')}, поддеревьев {result.get('subtrees_unread')}; "
        f"пропущено offscreen {result.get('skipped_offscreen')}; "
        f"truncated={result.get('truncated')} truncated_by={result.get('truncated_by')}; "
        f"total_known={result.get('total_known')}"
    )
    if not result.get("total_known", True):
        lines.append("total_known=false — это НЕ всё окно; прочитана только названная часть")
    lines.append(f"время: elapsed_ms={result.get('elapsed_ms')} walk_ms={result.get('walk_ms')}")
    filtered = result.get("filter") if isinstance(result.get("filter"), dict) else None
    if filtered:
        lines.append("фильтр: " + json.dumps(filtered, ensure_ascii=False))
    limits = result.get("limits") if isinstance(result.get("limits"), dict) else None
    if limits:
        lines.append("пределы: " + ", ".join(
            f"{key}={value}" for key, value in limits.items()
        ))
    for key in ("coordinates", "state_semantics", "ids"):
        if result.get(key):
            lines.append(f"{key}: {result[key]}")
    for note in result.get("notes") or []:
        lines.append(f"заметка: {note}")
    leftovers = sorted(set(result) - _WINDOW_READ_RENDERED - {"ok"})
    if leftovers:
        rendered = []
        for key in leftovers:
            raw = json.dumps(result[key], ensure_ascii=False, default=str)
            if len(raw) > leftover_chars:
                raw = (raw[:leftover_chars]
                       + f"…(+{len(raw) - leftover_chars} символов, целиком — в JSON расписки)")
            rendered.append(f"{key}={raw}")
        lines.append("прочее: " + "; ".join(rendered))
    shape = str(result.get("shape") or "tree")
    lines.append(f"дерево ({shape}):")
    if shape == "flat":
        items = result.get("items") or []
        if not items:
            lines.append(f"{indent}(ни одного узла — при заданном фильтре это «не нашлось», "
                         "а не «окно пустое»)")
        for item in items:
            _window_read_node_lines(item, lines, indent, 1)
    elif result.get("root") is None:
        lines.append(f"{indent}root=null — фильтр не нашёл ничего в прочитанной части")
    else:
        _window_read_node_lines(result.get("root"), lines, indent, 1)
    return "\n".join(lines)


def desktop_input_perform_and_read(events: list[dict], *,
                                   expected_foreground: str | int | None = None,
                                   expected_pid: int | None = None,
                                   inter_event_delay_ms: int = 0,
                                   settle_ms: int | None = None,
                                   read: dict | None = None,
                                   execution: str = "interactive") -> dict:
    """Нажать и тут же посмотреть, что стало, — одной дорогой до тела вместо двух.

    Экономится не такт, а раундтрип: любой глагол стоит ≈0.37 с только на дорогу, и
    «нажала → прочитала» раньше стоило две.  Это НЕ атомарный акт: рабочий стол волен
    измениться между вводом и чтением, и глагол сам это говорит в `semantics`.
    Ошибкой считается только сорванный ВВОД; несостоявшееся чтение приезжает как
    `observed:false` с причиной — «не смогла прочитать» не равно «окно пустое».
    """
    payload: dict[str, Any] = {
        "input": _input_payload(events, expected_foreground=expected_foreground,
                                expected_pid=expected_pid,
                                inter_event_delay_ms=inter_event_delay_ms),
    }
    if settle_ms is not None:
        payload["settle_ms"] = int(settle_ms)
    if read is not None:
        payload["read"] = dict(read)
    wait = round(
        INPUT_WAIT_SECONDS
        + _clamped_ms(settle_ms, 0, INPUT_SETTLE_CEILING_MS) / 1000.0
        + _window_read_wait_seconds((read or {}).get("timeout_ms")),
        3,
    )
    return _with_server_frame(
        call("desktop.input.perform_and_read", payload, execution=execution, timeout=wait),
        wait_s=wait, truth_field="observed",
        truth_note=("ok значит «ввод прошёл»; удалось ли посмотреть на результат — "
                    "в observed, а причина неудачи — в observation.error"),
    )


def desktop_window_wait(condition: str, *, title_contains: str = "",
                        class_contains: str = "", pid: int | None = None,
                        foreground: bool | None = None, hwnd: str | int | None = None,
                        text_contains: str = "", visible_only: bool | None = None,
                        timeout_ms: int | None = None, poll_interval_ms: int | None = None,
                        probe_timeout_ms: int | None = None,
                        probe_max_nodes: int | None = None,
                        probe_max_depth: int | None = None,
                        execution: str = "interactive") -> dict:
    """Дождаться условия на рабочем столе, не тратя по вызову на каждую пробу.

    Истечение срока — это `ok:true, met:false`, а НЕ ошибка: «не увидела за 5 секунд» и
    «этого нет» — разные утверждения, и тело их различает (`reason` договаривает, была ли
    последняя проба частичной и не сорвались ли вообще все пробы).  Поэтому правду здесь
    читать по `met`; `ok` перетирается рамкой транспорта.
    """
    payload: dict[str, Any] = {"for": str(condition or "")}
    for key, value in (("title_contains", title_contains),
                       ("class_contains", class_contains),
                       ("text_contains", text_contains)):
        if value:
            payload[key] = str(value)
    if pid is not None:
        payload["pid"] = int(pid)
    if foreground is not None:
        payload["foreground"] = bool(foreground)
    if hwnd is not None and str(hwnd).strip() != "":
        payload["hwnd"] = hwnd
    if visible_only is not None:
        payload["visible_only"] = bool(visible_only)
    for key, value in (("timeout_ms", timeout_ms), ("poll_interval_ms", poll_interval_ms),
                       ("probe_timeout_ms", probe_timeout_ms),
                       ("probe_max_nodes", probe_max_nodes),
                       ("probe_max_depth", probe_max_depth)):
        if value is not None:
            payload[key] = int(value)
    # Последняя проба вправе перебрать общий бюджет ровно на свой срок — так объявлено в
    # `limits.budget` самого глагола, и сервер обязан пережить именно эту сумму.
    wait = round(
        (_clamped_ms(timeout_ms, WINDOW_WAIT_DEFAULT_TIMEOUT_MS,
                     WINDOW_WAIT_TIMEOUT_CEILING_MS)
         + _clamped_ms(probe_timeout_ms, WINDOW_WAIT_PROBE_DEFAULT_TIMEOUT_MS,
                       WINDOW_WAIT_PROBE_TIMEOUT_CEILING_MS)) / 1000.0
        + BODY_TRANSPORT_GRACE_SECONDS,
        3,
    )
    return _with_server_frame(
        call("desktop.window.wait", payload, execution=execution, timeout=wait),
        wait_s=wait, truth_field="met",
        truth_note=("ok значит «ожидание закончилось и ответ доехал»; сбылось ли условие — "
                    "в met; met:false вместе с timed_out:true — это «не увидела в срок», "
                    "а не «условия нет»"),
    )


def desktop_screen_capture(*, target: str = "desktop", hwnd: str | int | None = None,
                           x: int | None = None, y: int | None = None,
                           width: int | None = None, height: int | None = None,
                           name: str = "", execution: str = "interactive") -> dict:
    """Capture the interactive desktop; the body returns a local exportable path."""
    payload: dict[str, Any] = {"target": str(target or "desktop")}
    for key, value in (("hwnd", hwnd), ("x", x), ("y", y),
                       ("width", width), ("height", height)):
        if value is not None:
            payload[key] = value
    if name:
        payload["name"] = str(name)
    return call("desktop.screen.capture", payload, execution=execution, timeout=60)


def desktop_clipboard_read(*, limit_chars: int = 1_000_000,
                           execution: str = "interactive") -> dict:
    return call("desktop.clipboard.read", {"limit_chars": max(1, int(limit_chars))},
                execution=execution, timeout=30)


def desktop_clipboard_write(text: str, *, execution: str = "interactive") -> dict:
    return call("desktop.clipboard.write", {"text": str(text)},
                execution=execution, timeout=30)


def os_process_list(*, offset: int = 0, limit: int = 2048, name_contains: str = "",
                    session_id: int | None = None,
                    execution: str = "interactive") -> dict:
    payload: dict[str, Any] = {
        "offset": max(0, int(offset)), "limit": max(1, int(limit)),
        "name_contains": str(name_contains or ""),
    }
    if session_id is not None:
        payload["session_id"] = int(session_id)
    return call("os.process.list", payload, execution=execution, timeout=30)


def _fs_tree_call(capability: str, payload: dict, execution: str) -> dict:
    """Один вызов файлового глагола, который может вернуться частично сделанным.

    Тело обрывает обход само — на 200 000 узлах или на 45 секундах — и отдаёт `Ok` с
    `complete:false` и `stopped_because`.  Это не ошибка и не успех: это половина работы,
    названная половиной.  Повторный вызов продолжает с оставшегося.
    """
    return _with_server_frame(
        call(capability, dict(payload), execution=execution, timeout=FS_TREE_WAIT_SECONDS),
        wait_s=FS_TREE_WAIT_SECONDS, truth_field="complete",
        truth_note=("ok значит «расписка доехала»; доведено ли дело — в complete, "
                    "stopped_because и failed_count (failed перечислен поимённо не весь, "
                    "смотри limits.max_failures_reported и failed_truncated)"),
    )


def fs_delete(path: str, *, recursive: bool = False,
              execution: str = "interactive") -> dict:
    """Удалить файл, пустой каталог или — при recursive — целое дерево.

    Несуществующий путь тут не ошибка: `ok:true, existed:false`.  Это разные утверждения —
    «удалила» и «удалять было нечего», и оба обязаны звучать по-разному.
    Junction/symlink не разворачивается: снимается сама связь (`removed_links`).
    """
    return _fs_tree_call("fs.delete", {"path": str(path), "recursive": bool(recursive)},
                         execution)


def fs_move(source: str, destination: str, *, overwrite: bool = False,
            execution: str = "interactive") -> dict:
    """Перенести файл или дерево.  Через границу тома это копия + снос источника.

    Тело называет это прямо (`strategy:"copy_and_delete"`, `cross_volume:true`) — и там же
    начинают действовать оба бюджета обхода: минуты вместо миллисекунд.  Если `destination`
    оказался существующим каталогом, имя источника сохраняется, а ИТОГОВЫЙ путь приезжает
    в `to` (просьба — в `requested_to`).
    """
    return _fs_tree_call("fs.move", {"from": str(source), "to": str(destination),
                                     "overwrite": bool(overwrite)}, execution)


def fs_copy(source: str, destination: str, *, recursive: bool = False,
            overwrite: bool = False, execution: str = "interactive") -> dict:
    """Скопировать файл или — при recursive — дерево.

    При `overwrite:false` существующие файлы дерева не роняют вызов: они пропускаются и
    считаются в `skipped_existing`.  Reparse-точки не копируются вовсе (`skipped_reparse`).
    """
    return _fs_tree_call("fs.copy", {"from": str(source), "to": str(destination),
                                     "recursive": bool(recursive),
                                     "overwrite": bool(overwrite)}, execution)


def fs_mkdir(path: str, *, parents: bool = True,
             execution: str = "interactive") -> dict:
    """Создать каталог; при parents — всю недостающую цепочку.

    Поимённо перечисляется не больше 64 созданных уровней (`limits.max_created_reported`),
    но `created_count` полный, а срез назван флагом `created_truncated`.
    """
    return _fs_tree_call("fs.mkdir", {"path": str(path), "parents": bool(parents)},
                         execution)


def run_argv(program: str, args: list[str], *, cwd: str = "", timeout: int = 600,
             execution: str = "interactive", tail: int = 16_000) -> dict:
    started = process_start(program=program, args=args, cwd=cwd, timeout=timeout,
                            execution=execution)
    if not started.get("ok"):
        return started
    operation_id = str(started.get("operation_id") or "")
    deadline = time.monotonic() + max(30, timeout + 30 if timeout else 3600)
    while time.monotonic() < deadline:
        current = process_status(operation_id, tail=tail, execution=execution)
        status = str(current.get("status") or "")
        if status in TERMINAL:
            current["ok"] = status == "succeeded"
            return current
        if not current.get("ok"):
            return current
        time.sleep(.2)
    return {"ok": False, "code": "timeout", "operation_id": operation_id,
            "error": "process did not reach a terminal state"}


def run(root: str, command: str, cwd: str = ".", timeout: int = 600,
        on_chunk=None, execution: str = "interactive") -> str:
    result = run_result(root, command, cwd=cwd, timeout=timeout,
                        on_chunk=on_chunk, execution=execution)
    if not result.get("ok"):
        return f"[windows-body] {result.get('error') or result}"
    body = str(result.get("body") or "").strip() or "(empty output)"
    terminal = result.get("result") or {}
    return (body + f"\n[windows-body: {result.get('status')}; "
            f"exit {terminal.get('exit_code')}; op {result.get('operation_id')}]")


def run_result(root: str, command: str, cwd: str = ".", timeout: int = 600,
               on_chunk=None, execution: str = "interactive") -> dict:
    place = root if cwd in ("", ".") else ntpath.join(root, cwd)
    started = process_start(command=command, shell="power_shell", cwd=place,
                            timeout=timeout, execution=execution)
    if not started.get("ok"):
        return started
    operation_id = str(started.get("operation_id") or "")
    deadline = time.monotonic() + max(30, timeout + 30 if timeout else 3600)
    seen = ""
    while time.monotonic() < deadline:
        current = process_status(operation_id, tail=500_000, execution=execution)
        body = str(current.get("stdout_tail") or "") + str(current.get("stderr_tail") or "")
        if on_chunk and len(body) > len(seen):
            on_chunk(body[len(seen):])
        seen = body
        status = str(current.get("status") or "")
        if status in TERMINAL:
            current["body"] = body
            current["ok"] = status == "succeeded"
            return current
        if not current.get("ok"):
            return current
        time.sleep(.2)
    return {"ok": False, "code": "timeout", "operation_id": operation_id,
            "error": f"timeout waiting for {operation_id}"}


def workspace_inspect_result(root: str, action: str = "status", **kwargs) -> dict:
    action = str(action or "status").lower()
    if action == "read":
        path = ntpath.join(root, str(kwargs.get("path") or ""))
        raw = call("fs.read", {"path": path, "offset": 0, "limit": 8_000_000})
        if not raw.get("ok"):
            return raw
        start = max(1, int(kwargs.get("start") or 1))
        lines = str(raw.get("text") or "").splitlines()
        end = int(kwargs.get("end") or len(lines))
        selected = lines[start - 1:max(start - 1, end)]
        digest = call("fs.hash", {"path": path})
        numbered = "\n".join(f"{no}: {line}" for no, line in enumerate(selected, start))
        return {"ok": True, "text": numbered,
                "sha256": digest.get("sha256"), "size": raw.get("size")}
    if action == "list":
        path = ntpath.join(root, str(kwargs.get("path") or ""))
        listed = call("fs.list", {"path": path})
        listed["text"] = "\n".join(
            f"{row.get('kind')}\t{row.get('size')}\t{row.get('name')}"
            for row in listed.get("entries") or []
        )
        return listed
    if action == "orientation":
        stat = call("fs.stat", {"path": root})
        listed = call("fs.list", {"path": root}) if stat.get("ok") else {}
        git = run_argv("git.exe", ["-C", root, "status", "--short", "--branch"], timeout=60)
        text = "\n".join([
            f"Windows device: {_settings()[2]}", f"root: {root}",
            f"kind: {stat.get('kind', 'unknown')}",
            "entries: " + ", ".join(str(x.get("name")) for x in listed.get("entries", [])[:80]),
            "git:\n" + str(git.get("stdout_tail") or git.get("error") or "not a repository"),
        ])
        return {"ok": bool(stat.get("ok")), "text": text, "head": "", "git_root": root}
    if action == "status":
        result = run_argv("git.exe", ["-C", root, "status", "--short", "--branch"], timeout=60)
    elif action == "diff":
        result = run_argv("git.exe", ["-C", root, "diff", "--", "."], timeout=120)
    elif action in {"search", "references", "symbols"}:
        query = str(kwargs.get("query") or "")
        result = run_argv("rg.exe", ["--line-number", "--hidden", "--", query, root], timeout=120)
    elif action == "checks":
        listed = call("fs.list", {"path": root})
        names = {str(row.get("name") or "").lower() for row in listed.get("entries") or []}
        checks = []
        if "cargo.toml" in names:
            checks.append({"id": "cargo-test", "command": "cargo test", "cwd": ".",
                           "kind": "test", "source": "Cargo.toml", "scope": "project"})
        if "package.json" in names:
            checks.append({"id": "npm-test", "command": "npm test", "cwd": ".",
                           "kind": "test", "source": "package.json", "scope": "project"})
        if "pyproject.toml" in names or "pytest.ini" in names:
            checks.append({"id": "pytest", "command": "python -m pytest", "cwd": ".",
                           "kind": "test", "source": "python", "scope": "project"})
        return {"ok": True, "text": json.dumps({"checks": checks}, ensure_ascii=False)}
    elif action == "impact":
        changed = run_argv("git.exe", ["-C", root, "diff", "--name-only"], timeout=60)
        if not changed.get("ok"):
            # Раньше отсюда уезжало `ok:false` вместе с валидным JSON `{"changed": []}` —
            # и `workspace_inspect` заворачивал этот JSON в «[windows-body] …», ломая разбор
            # у читателя.  Хуже разбора было содержание: «git не ответил» превращалось в
            # «ничего не менялось».  Не смогла прочитать ≠ пусто.
            detail = (str(changed.get("stderr_tail") or "").strip()
                      or str(changed.get("error") or "").strip()
                      or f"git diff --name-only завершился со статусом {changed.get('status')}")
            return {"ok": False, "code": "impact_unknown",
                    "error": f"состав правок неизвестен: {detail}"}
        names = [line for line in str(changed.get("stdout_tail") or "").splitlines() if line]
        return {"ok": True, "text": json.dumps({"changed": names}, ensure_ascii=False)}
    elif action == "diagnostics":
        result = run_argv("git.exe", ["-C", root, "diff", "--check"], timeout=60)
    else:
        return {"ok": False, "error": f"unsupported Windows inspect action {action}"}
    return {"ok": bool(result.get("ok")),
            "text": str(result.get("stdout_tail") or result.get("stderr_tail") or result.get("error") or "")}


def workspace_inspect(root: str, action: str = "status", **kwargs) -> str:
    result = workspace_inspect_result(root, action, **kwargs)
    if result.get("ok"):
        return str(result.get("text") or "")
    detail = result.get("error") or result.get("text") or result
    return f"[windows-body] {detail}"


def workspace_edit(root: str, action: str, **kwargs) -> str:
    path = ntpath.join(root, str(kwargs.get("path") or ""))
    expected = str(kwargs.get("expected_sha256") or "").strip()
    if action == "write":
        payload = {"path": path, "content": kwargs.get("content") or ""}
        if expected:
            payload["expected_sha256"] = expected
        result = call("fs.write_atomic", payload)
    elif action == "replace":
        payload = {"path": path, "old": kwargs.get("old") or "",
                   "new": kwargs.get("new") or ""}
        if expected:
            payload["expected_sha256"] = expected
        result = call("fs.replace", payload)
    elif action == "patch":
        result = call("fs.apply_patch", {"root": root,
                                          "patch": kwargs.get("patch") or kwargs.get("content") or ""})
    else:
        return f"[windows-body] unsupported edit action {action}"
    return json.dumps(result, ensure_ascii=False, indent=2) if result.get("ok") else f"[windows-body] {result.get('error')}"


def process(root: str, action: str, operation_id: str = "", command: str = "",
            cwd: str = ".", name: str = "", timeout: int = 0, tail: int = 12000,
            limits: dict | None = None) -> str:
    if action == "start":
        place = root if cwd in ("", ".") else ntpath.join(root, cwd)
        result = process_start(command=command, shell="power_shell", cwd=place,
                               name=name, timeout=timeout)
    elif action == "poll":
        result = process_status(operation_id, tail)
    elif action == "stop":
        result = call("process.cancel", {"operation_id": operation_id, "tail": tail})
    elif action == "list":
        result = call("process.list", {"root": root})
    else:
        return "action: start | poll | stop | list"
    return json.dumps(result, ensure_ascii=False, indent=2)


def checkpoint(root: str, message: str = "Praxis Windows checkpoint") -> str:
    staged = run_argv("git.exe", ["-C", root, "add", "-A"], timeout=120)
    if not staged.get("ok"):
        return f"[windows-body] {staged.get('stderr_tail') or staged.get('error') or staged}"
    committed = run_argv(
        "git.exe",
        ["-C", root, "-c", "user.name=Praxis", "-c", "user.email=praxis@local",
         "commit", "-m", str(message or "Praxis Windows checkpoint")],
        timeout=180,
    )
    return json.dumps(committed, ensure_ascii=False, indent=2)


def status_probe(*, timeout: float = 5.0) -> dict:
    """Probe the user-session route first, then the boot-time SYSTEM service.

    The service owns WSS and is reachable before logon.  A missing interactive
    session must therefore not make the whole Windows body look offline.
    """
    interactive = call("body.status", {}, execution="interactive", timeout=timeout)
    if interactive.get("ok"):
        interactive.setdefault("probe_execution", "interactive")
        return interactive
    system = call("body.status", {}, execution="system", timeout=timeout)
    if system.get("ok"):
        system.setdefault("probe_execution", "system")
        system.setdefault("interactive_error", interactive.get("error") or interactive.get("code"))
        return system
    return {
        **system,
        "ok": False,
        "interactive_error": interactive.get("error") or interactive.get("code"),
        "system_error": system.get("error") or system.get("code"),
    }


def _context_available(probe: dict, kind: str) -> bool | None:
    contexts = ((probe.get("manifest") or {}).get("execution_contexts")
                if isinstance(probe.get("manifest"), dict) else None)
    for row in contexts or []:
        if isinstance(row, dict) and str(row.get("kind") or "").lower() == kind:
            return bool(row.get("available"))
    return None


def state_line() -> str:
    if not available():
        return "Windows body: controller token не настроен"
    probe = status_probe(timeout=5)
    if not probe.get("ok"):
        detail = probe.get("system_error") or probe.get("error") or probe.get("code")
        return f"Windows body: offline ({detail})"
    identity = probe.get("identity") or {}
    interactive = _context_available(probe, "interactive")
    system = _context_available(probe, "system")
    routes = []
    if interactive is not None:
        routes.append(f"interactive={'ready' if interactive else 'not connected'}")
    if system is not None:
        routes.append(f"system={'ready' if system else 'unavailable'}")
    suffix = ("; " + ", ".join(routes)) if routes else ""
    return (f"Windows body {_settings()[2]}: {identity.get('kind')} / "
            f"{identity.get('integrity')}; {PROTOCOL}{suffix}")
