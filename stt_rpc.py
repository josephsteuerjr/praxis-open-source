"""Authenticated local STT endpoint backed by Praxis' resident Whisper object.

The endpoint is deliberately Unix-socket-only.  It accepts one bounded upload,
never queues behind Praxis inference, and never logs uploaded names, audio, or
transcripts.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import math
import os
import socket
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol

from aiohttp import http_exceptions, web

import media_audio


SCHEMA = "praxis.stt.v1"
HEALTH_SCHEMA = "praxis.stt.health.v1"
ERROR_SCHEMA = "praxis.stt.error.v1"
HARD_MAX_BYTES = 15 * 1024 * 1024
MULTIPART_OVERHEAD_BYTES = 256 * 1024
ALLOWED_LANGUAGES: dict[str, str | None] = {
    "ru": "ru",
    "uz": "uz",
    "auto": None,
}

log = logging.getLogger("praxis-stt-rpc")


class SttRpcConfigurationError(RuntimeError):
    """The optional endpoint was enabled with an unsafe configuration."""


class _RequestError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class ExternalSttBackend(Protocol):
    @property
    def is_loaded(self) -> bool: ...

    @property
    def is_busy(self) -> bool: ...

    def transcribe_external(
        self,
        path: str | os.PathLike[str],
        *,
        language: str | None,
    ) -> str: ...


def _is_rooted_path(path: Path) -> bool:
    # The deploy contract is POSIX even when hermetic config tests run on
    # Windows, where pathlib represents ``/run/...`` as ``\\run\\...``.
    return path.is_absolute() or (
        os.name == "nt" and str(path).startswith(("/", "\\"))
    )


def _env_bool(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SttRpcConfigurationError("invalid STT RPC boolean setting")


def _env_int(
    value: str | None,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise SttRpcConfigurationError("invalid STT RPC integer setting") from exc
    if parsed < minimum or parsed > maximum:
        raise SttRpcConfigurationError("STT RPC integer setting is out of range")
    return parsed


@dataclass(frozen=True)
class SttRpcConfig:
    enabled: bool = False
    socket_path: Path = Path("/run/praxis-stt/stt.sock")
    token_file: Path = Path("/run/secrets/praxis-stt-client.token")
    max_bytes: int = HARD_MAX_BYTES
    rate_per_minute: int = 6
    burst: int = 2
    upload_timeout_seconds: int = 30
    temp_dir: Path | None = None

    def __post_init__(self) -> None:
        if not _is_rooted_path(self.socket_path):
            raise SttRpcConfigurationError("STT RPC socket path must be absolute")
        if not _is_rooted_path(self.token_file):
            raise SttRpcConfigurationError("STT RPC token path must be absolute")
        if not 1 <= self.max_bytes <= HARD_MAX_BYTES:
            raise SttRpcConfigurationError("STT RPC upload limit is out of range")
        if not 1 <= self.rate_per_minute <= 120:
            raise SttRpcConfigurationError("STT RPC rate is out of range")
        if not 1 <= self.burst <= 20:
            raise SttRpcConfigurationError("STT RPC burst is out of range")
        if not 1 <= self.upload_timeout_seconds <= 120:
            raise SttRpcConfigurationError("STT RPC upload timeout is out of range")
        if self.temp_dir is not None and not _is_rooted_path(self.temp_dir):
            raise SttRpcConfigurationError("STT RPC temp path must be absolute")

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None
    ) -> "SttRpcConfig":
        env = os.environ if environ is None else environ
        raw_max = _env_int(
            env.get("PRAXIS_STT_RPC_MAX_BYTES"),
            HARD_MAX_BYTES,
            minimum=1,
            maximum=HARD_MAX_BYTES,
        )
        raw_temp = env.get("PRAXIS_STT_RPC_TEMP_DIR", "").strip()
        return cls(
            enabled=_env_bool(env.get("PRAXIS_STT_RPC_ENABLED"), False),
            socket_path=Path(
                env.get("PRAXIS_STT_RPC_SOCKET", "/run/praxis-stt/stt.sock")
            ).expanduser(),
            token_file=Path(
                env.get(
                    "PRAXIS_STT_RPC_TOKEN_FILE",
                    "/run/secrets/praxis-stt-client.token",
                )
            ).expanduser(),
            max_bytes=raw_max,
            rate_per_minute=_env_int(
                env.get("PRAXIS_STT_RPC_RATE_PER_MINUTE"),
                6,
                minimum=1,
                maximum=120,
            ),
            burst=_env_int(
                env.get("PRAXIS_STT_RPC_BURST"),
                2,
                minimum=1,
                maximum=20,
            ),
            upload_timeout_seconds=_env_int(
                env.get("PRAXIS_STT_RPC_UPLOAD_TIMEOUT"),
                30,
                minimum=1,
                maximum=120,
            ),
            temp_dir=Path(raw_temp).expanduser() if raw_temp else None,
        )


class _TokenBucket:
    def __init__(
        self,
        rate_per_minute: int,
        burst: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rate_per_second = rate_per_minute / 60.0
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._updated_at = clock()

    def admit(self) -> tuple[bool, int]:
        now = self._clock()
        elapsed = max(0.0, now - self._updated_at)
        self._updated_at = now
        self._tokens = min(
            self._capacity,
            self._tokens + elapsed * self._rate_per_second,
        )
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True, 0
        retry_after = math.ceil((1.0 - self._tokens) / self._rate_per_second)
        return False, max(1, retry_after)


def _load_token(path: Path) -> str:
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SttRpcConfigurationError("STT RPC token is not a regular file")
        if os.name == "posix" and info.st_mode & 0o007:
            raise SttRpcConfigurationError("STT RPC token is world-readable")
        if info.st_size < 32 or info.st_size > 4096:
            raise SttRpcConfigurationError("STT RPC token length is unsafe")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = None
            value = source.read(4097).decode("ascii").strip()
    except SttRpcConfigurationError:
        raise
    except (OSError, UnicodeError) as exc:
        raise SttRpcConfigurationError("STT RPC token cannot be read") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(value) < 32 or any(character.isspace() for character in value):
        raise SttRpcConfigurationError("STT RPC token value is unsafe")
    return value


def _socket_identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISSOCK(info.st_mode):
        return None
    return info.st_dev, info.st_ino


def _unlink_socket(
    path: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    identity = _socket_identity(path)
    if identity is None:
        return
    if expected_identity is None or identity == expected_identity:
        path.unlink()


def _remove_stale_socket(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode):
        raise SttRpcConfigurationError("STT RPC socket target is not a socket")
    identity = (info.st_dev, info.st_ino)

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.2)
    try:
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        _unlink_socket(path, expected_identity=identity)
        return
    except OSError as exc:
        raise SttRpcConfigurationError(
            "existing STT RPC socket could not be verified"
        ) from exc
    finally:
        probe.close()
    raise SttRpcConfigurationError("STT RPC socket is already active")


def _sweep_stale_temp_files(directory: Path) -> int:
    """Remove only files created by a previous dead RPC child process."""

    removed = 0
    try:
        candidates = list(directory.iterdir())
    except OSError as exc:
        raise SttRpcConfigurationError(
            "STT RPC temp directory cannot be inspected"
        ) from exc
    if len(candidates) > 10_000:
        raise SttRpcConfigurationError("STT RPC temp directory is unsafe")

    for candidate in candidates:
        if not (
            candidate.name.startswith("praxis-stt-")
            and candidate.name.endswith(".audio")
        ):
            continue
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise SttRpcConfigurationError(
                "STT RPC stale temp file cannot be inspected"
            ) from exc
        if not stat.S_ISREG(info.st_mode):
            continue
        if os.name == "posix" and info.st_uid != os.geteuid():
            continue
        try:
            candidate.unlink()
        except OSError as exc:
            raise SttRpcConfigurationError(
                "STT RPC stale temp file cannot be removed"
            ) from exc
        removed += 1
    return removed


class SttRpcServer:
    def __init__(
        self,
        config: SttRpcConfig,
        backend: ExternalSttBackend,
        *,
        token: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.backend = backend
        self._token = token
        self._limiter = _TokenBucket(
            config.rate_per_minute,
            config.burst,
            clock=clock,
        )
        self._external_busy = False
        self._runner: web.AppRunner | None = None
        self._site: web.UnixSite | None = None
        self._socket_identity: tuple[int, int] | None = None
        self.app = web.Application(
            client_max_size=config.max_bytes + MULTIPART_OVERHEAD_BYTES
        )
        self.app.router.add_get("/healthz", self._health)
        self.app.router.add_post("/v1/transcriptions", self._transcribe)

    def _authorized(self, request: web.Request) -> bool:
        if self._token is None:
            return False
        provided = request.headers.get("Authorization", "")
        prefix = "Bearer "
        if not provided.startswith(prefix):
            return False
        candidate = provided[len(prefix) :]
        return bool(candidate) and hmac.compare_digest(candidate, self._token)

    @staticmethod
    def _request_id() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _error(
        request_id: str,
        *,
        status: int,
        code: str,
        message: str,
        retry_after: int | None = None,
    ) -> web.Response:
        headers: dict[str, str] = {}
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        if status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        return web.json_response(
            {
                "schema": ERROR_SCHEMA,
                "request_id": request_id,
                "error": {"code": code, "message": message},
            },
            status=status,
            headers=headers,
        )

    async def _health(self, request: web.Request) -> web.Response:
        request_id = self._request_id()
        if not self._authorized(request):
            return self._error(
                request_id,
                status=401,
                code="unauthorized",
                message="valid bearer token required",
            )
        ready = bool(self.backend.is_loaded)
        busy = bool(self._external_busy or self.backend.is_busy)
        return web.json_response(
            {
                "schema": HEALTH_SCHEMA,
                "status": "ready" if ready else "warming",
                "ready": ready,
                "busy": busy,
            },
            status=200 if ready else 503,
        )

    async def _transcribe(self, request: web.Request) -> web.Response:
        request_id = self._request_id()
        if not self._authorized(request):
            return self._error(
                request_id,
                status=401,
                code="unauthorized",
                message="valid bearer token required",
            )
        if not self.backend.is_loaded:
            return self._error(
                request_id,
                status=503,
                code="warming",
                message="speech model is not ready",
                retry_after=2,
            )
        if self._external_busy or self.backend.is_busy:
            return self._error(
                request_id,
                status=429,
                code="busy",
                message="speech worker is busy",
                retry_after=2,
            )
        admitted, retry_after = self._limiter.admit()
        if not admitted:
            return self._error(
                request_id,
                status=429,
                code="rate_limited",
                message="speech request rate exceeded",
                retry_after=retry_after,
            )

        self._external_busy = True
        temp_path: Path | None = None
        language_name = "unknown"
        status = 500
        started_at = time.monotonic()
        try:
            try:
                temp_path, language_name = await asyncio.wait_for(
                    self._receive_audio(request, request_id=request_id),
                    timeout=self.config.upload_timeout_seconds,
                )
            except TimeoutError:
                raise _RequestError(
                    408, "upload_timeout", "audio upload timed out"
                ) from None

            inference = asyncio.create_task(
                asyncio.to_thread(
                    self.backend.transcribe_external,
                    temp_path,
                    language=ALLOWED_LANGUAGES[language_name],
                )
            )
            try:
                text = await asyncio.shield(inference)
            except asyncio.CancelledError:
                # ``to_thread`` cannot be cancelled.  Keep the private temp file
                # until its decoder has finished with it, then propagate shutdown.
                try:
                    await inference
                except Exception:
                    pass
                raise
            status = 200
            return web.json_response(
                {
                    "schema": SCHEMA,
                    "request_id": request_id,
                    "text": text,
                    "language": language_name,
                }
            )
        except _RequestError as exc:
            status = exc.status
            return self._error(
                request_id,
                status=exc.status,
                code=exc.code,
                message=exc.message,
            )
        except media_audio.AudioBusyError:
            status = 429
            return self._error(
                request_id,
                status=429,
                code="busy",
                message="speech worker is busy",
                retry_after=2,
            )
        except media_audio.AudioNotReadyError:
            status = 503
            return self._error(
                request_id,
                status=503,
                code="warming",
                message="speech model is not ready",
                retry_after=2,
            )
        except media_audio.AudioConfigurationError:
            status = 400
            return self._error(
                request_id,
                status=400,
                code="invalid_audio",
                message="audio input is invalid",
            )
        except web.HTTPRequestEntityTooLarge:
            status = 413
            return self._error(
                request_id,
                status=413,
                code="audio_too_large",
                message="audio exceeds size limit",
            )
        except web.HTTPBadRequest:
            status = 400
            return self._error(
                request_id,
                status=400,
                code="invalid_multipart",
                message="invalid multipart body",
            )
        except media_audio.AudioBackendError:
            status = 422
            return self._error(
                request_id,
                status=422,
                code="transcription_failed",
                message="audio could not be transcribed",
            )
        except asyncio.CancelledError:
            status = 499
            raise
        except Exception as exc:
            status = 500
            log.warning(
                "shared STT request failed request_id=%s error_type=%s.%s",
                request_id,
                type(exc).__module__,
                type(exc).__name__,
            )
            return self._error(
                request_id,
                status=500,
                code="internal_error",
                message="speech service failed",
            )
        finally:
            if temp_path is not None:
                self._cleanup_temp(temp_path, request_id=request_id)
            self._external_busy = False
            duration_ms = int((time.monotonic() - started_at) * 1000)
            log.info(
                "shared STT request request_id=%s status=%d language=%s duration_ms=%d",
                request_id,
                status,
                language_name,
                duration_ms,
            )

    @staticmethod
    def _cleanup_temp(path: Path, *, request_id: str) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning(
                "shared STT temp cleanup failed request_id=%s",
                request_id,
            )

    async def _receive_audio(
        self,
        request: web.Request,
        *,
        request_id: str,
    ) -> tuple[Path, str]:
        if request.content_type != "multipart/form-data":
            raise _RequestError(
                415,
                "unsupported_media_type",
                "multipart/form-data required",
            )
        if (
            request.content_length is not None
            and request.content_length
            > self.config.max_bytes + MULTIPART_OVERHEAD_BYTES
        ):
            raise _RequestError(413, "audio_too_large", "audio exceeds size limit")

        try:
            reader = await request.multipart()
        except (
            ValueError,
            web.HTTPBadRequest,
            http_exceptions.HttpProcessingError,
        ) as exc:
            raise _RequestError(400, "invalid_multipart", "invalid multipart body") from exc

        temp_path: Path | None = None
        audio_seen = False
        language: str | None = None
        try:
            while True:
                part = await reader.next()
                if part is None:
                    break
                if part.name == "audio":
                    if audio_seen:
                        raise _RequestError(
                            400, "duplicate_audio", "exactly one audio field required"
                        )
                    audio_seen = True
                    fd, raw_path = tempfile.mkstemp(
                        prefix="praxis-stt-",
                        suffix=".audio",
                        dir=str(self.config.temp_dir) if self.config.temp_dir else None,
                    )
                    temp_path = Path(raw_path)
                    try:
                        os.chmod(temp_path, 0o600)
                        total = 0
                        with os.fdopen(fd, "wb") as target:
                            while True:
                                chunk = await part.read_chunk(size=64 * 1024)
                                if not chunk:
                                    break
                                total += len(chunk)
                                if total > self.config.max_bytes:
                                    raise _RequestError(
                                        413,
                                        "audio_too_large",
                                        "audio exceeds size limit",
                                    )
                                target.write(chunk)
                    except BaseException:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                        raise
                    if total == 0:
                        raise _RequestError(
                            400, "empty_audio", "audio field is empty"
                        )
                elif part.name == "language":
                    if language is not None:
                        raise _RequestError(
                            400,
                            "duplicate_language",
                            "exactly one language field required",
                        )
                    raw_language = await self._read_small_field(part, maximum=16)
                    language = raw_language.decode("ascii", errors="strict").strip().lower()
                else:
                    raise _RequestError(
                        400,
                        "unexpected_field",
                        "only audio and language fields are accepted",
                    )
            if not audio_seen or temp_path is None:
                raise _RequestError(
                    400, "missing_audio", "audio field is required"
                )
            if language is None:
                raise _RequestError(
                    400,
                    "missing_language",
                    "language field is required",
                )
            if language not in ALLOWED_LANGUAGES:
                raise _RequestError(
                    400,
                    "invalid_language",
                    "language must be ru, uz, or auto",
                )
            return temp_path, language
        except UnicodeError as exc:
            if temp_path is not None:
                self._cleanup_temp(temp_path, request_id=request_id)
            raise _RequestError(
                400, "invalid_language", "language must be ru, uz, or auto"
            ) from exc
        except (
            ValueError,
            web.HTTPBadRequest,
            http_exceptions.HttpProcessingError,
        ) as exc:
            if temp_path is not None:
                self._cleanup_temp(temp_path, request_id=request_id)
            raise _RequestError(
                400, "invalid_multipart", "invalid multipart body"
            ) from exc
        except BaseException:
            if temp_path is not None:
                self._cleanup_temp(temp_path, request_id=request_id)
            raise

    @staticmethod
    async def _read_small_field(part: object, *, maximum: int) -> bytes:
        value = bytearray()
        while True:
            chunk = await part.read_chunk(size=64 * 1024)
            if not chunk:
                break
            value.extend(chunk)
            if len(value) > maximum:
                raise _RequestError(
                    400, "field_too_large", "multipart field is too large"
                )
        return bytes(value)

    async def start(self) -> None:
        if not self.config.enabled:
            return
        if self._runner is not None:
            return

        parent = self.config.socket_path.parent
        parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        if not parent.is_dir() or parent.is_symlink():
            raise SttRpcConfigurationError(
                "STT RPC socket parent is not a direct directory"
            )
        # Refuse a live peer before touching its possible in-flight temp file.
        _remove_stale_socket(self.config.socket_path)
        if self.config.temp_dir is not None:
            self.config.temp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not self.config.temp_dir.is_dir() or self.config.temp_dir.is_symlink():
                raise SttRpcConfigurationError(
                    "STT RPC temp path is not a direct directory"
                )
            removed = _sweep_stale_temp_files(self.config.temp_dir)
            if removed:
                log.info("shared STT removed stale temp files count=%d", removed)
        if self._token is None:
            self._token = _load_token(self.config.token_file)
        if not callable(getattr(self.backend, "transcribe_external", None)):
            raise SttRpcConfigurationError(
                "configured STT backend cannot serve external requests"
            )

        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        identity: tuple[int, int] | None = None
        try:
            site = web.UnixSite(runner, str(self.config.socket_path))
            await site.start()
            identity = _socket_identity(self.config.socket_path)
            if identity is None:
                raise SttRpcConfigurationError(
                    "STT RPC socket was not created safely"
                )
            os.chmod(self.config.socket_path, 0o660)
        except BaseException:
            await runner.cleanup()
            if identity is not None:
                _unlink_socket(
                    self.config.socket_path,
                    expected_identity=identity,
                )
            raise
        self._runner = runner
        self._site = site
        self._socket_identity = identity
        log.info("shared STT endpoint started")

    async def stop(self) -> None:
        runner = self._runner
        identity = self._socket_identity
        self._runner = None
        self._site = None
        self._socket_identity = None
        if runner is None:
            return
        await runner.cleanup()
        _unlink_socket(
            self.config.socket_path,
            expected_identity=identity,
        )
        log.info("shared STT endpoint stopped")


async def start_from_env(
    backend: ExternalSttBackend,
    environ: Mapping[str, str] | None = None,
) -> SttRpcServer | None:
    """Start the optional endpoint without making Praxis boot depend on it."""

    try:
        config = SttRpcConfig.from_env(environ)
        if not config.enabled:
            return None
        server = SttRpcServer(config, backend)
        await server.start()
        return server
    except Exception as exc:
        # Do not include exception text: OS errors may contain secret/socket paths.
        log.error(
            "shared STT endpoint not started error_type=%s",
            type(exc).__name__,
        )
        return None


__all__ = [
    "ALLOWED_LANGUAGES",
    "ERROR_SCHEMA",
    "HARD_MAX_BYTES",
    "HEALTH_SCHEMA",
    "SCHEMA",
    "SttRpcConfig",
    "SttRpcConfigurationError",
    "SttRpcServer",
    "start_from_env",
]
