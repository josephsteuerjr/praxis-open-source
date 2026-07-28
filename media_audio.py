"""Speech-to-text and text-to-speech backends for Praxis.

The module deliberately keeps heavyweight dependencies optional.  Importing
the module does not import or load faster-whisper/Piper, and model objects are
created only on the first request.  Inference is serialized because the target
server is a small two-vCPU host where parallel model execution would mainly add
memory pressure.
"""

from __future__ import annotations

import asyncio
import gc
import os
import tempfile
import threading
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


# This is the canonical mapping used by faster-whisper 1.2.1 for `large-v3-turbo`.
DEFAULT_STT_MODEL = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
DEFAULT_STT_REVISION = None
DEFAULT_PIPER_VOICE = "ru_RU-irina-medium.onnx"
DEFAULT_EDGE_VOICE = "ru-RU-SvetlanaNeural"


class AudioBackendError(RuntimeError):
    """Base class for an expected audio-backend failure."""


class AudioConfigurationError(AudioBackendError):
    """The backend is not provisioned or its configuration is invalid."""


class AudioDependencyError(AudioBackendError):
    """An optional runtime dependency is missing."""


class AudioProcessingError(AudioBackendError):
    """A configured backend failed while processing media."""


def _env_bool(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise AudioConfigurationError(f"invalid boolean value: {value!r}")


def _env_int(
    value: str | None,
    default: int,
    *,
    name: str,
    minimum: int = 1,
) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise AudioConfigurationError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise AudioConfigurationError(f"{name} must be >= {minimum}")
    return parsed


@dataclass(frozen=True)
class AudioConfig:
    """Configuration for the audio stack."""

    model_dir: Path
    output_dir: Path
    stt_model: str = DEFAULT_STT_MODEL
    stt_revision: str | None = DEFAULT_STT_REVISION
    stt_device: str = "cpu"
    stt_compute_type: str = "int8"
    stt_cpu_threads: int = 2
    stt_num_workers: int = 1
    stt_language: str | None = "ru"
    stt_beam_size: int = 5
    stt_vad_filter: bool = True
    stt_local_files_only: bool = True
    stt_keep_loaded: bool = True
    stt_max_bytes: int = 200 * 1024 * 1024
    tts_backend: str = "edge"
    edge_voice: str = DEFAULT_EDGE_VOICE
    edge_rate: str = "+0%"
    edge_volume: str = "+0%"
    edge_pitch: str = "+0Hz"
    edge_timeout: int = 45
    piper_model: Path = Path("/app/models/audio/piper/ru_RU-irina-medium.onnx")
    piper_use_cuda: bool = False
    tts_max_chars: int = 4000

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "AudioConfig":
        env = os.environ if environ is None else environ
        model_dir = Path(
            env.get("PRAXIS_AUDIO_MODEL_DIR", "/app/models/audio")
        ).expanduser()
        output_dir = Path(
            env.get(
                "PRAXIS_TTS_OUTPUT_DIR",
                str(Path(tempfile.gettempdir()) / "praxis-audio"),
            )
        ).expanduser()
        default_piper = model_dir / "piper" / DEFAULT_PIPER_VOICE
        language = env.get("PRAXIS_STT_LANGUAGE", "ru").strip() or None
        revision_raw = env.get("PRAXIS_STT_REVISION", DEFAULT_STT_REVISION or "")
        revision = str(revision_raw or "").strip() or None

        return cls(
            model_dir=model_dir,
            output_dir=output_dir,
            stt_model=env.get("PRAXIS_STT_MODEL", DEFAULT_STT_MODEL).strip()
            or DEFAULT_STT_MODEL,
            stt_revision=revision,
            stt_device=env.get("PRAXIS_STT_DEVICE", "cpu").strip() or "cpu",
            stt_compute_type=env.get(
                "PRAXIS_STT_COMPUTE_TYPE", "int8"
            ).strip()
            or "int8",
            stt_cpu_threads=_env_int(
                env.get("PRAXIS_STT_CPU_THREADS"),
                2,
                name="PRAXIS_STT_CPU_THREADS",
            ),
            stt_num_workers=_env_int(
                env.get("PRAXIS_STT_NUM_WORKERS"),
                1,
                name="PRAXIS_STT_NUM_WORKERS",
            ),
            stt_language=language,
            stt_beam_size=_env_int(
                env.get("PRAXIS_STT_BEAM_SIZE"),
                5,
                name="PRAXIS_STT_BEAM_SIZE",
            ),
            stt_vad_filter=_env_bool(env.get("PRAXIS_STT_VAD_FILTER"), True),
            stt_local_files_only=_env_bool(
                env.get("PRAXIS_STT_LOCAL_FILES_ONLY"), True
            ),
            stt_keep_loaded=_env_bool(env.get("PRAXIS_STT_KEEP_LOADED"), True),
            stt_max_bytes=_env_int(
                env.get("PRAXIS_STT_MAX_BYTES"),
                200 * 1024 * 1024,
                name="PRAXIS_STT_MAX_BYTES",
            ),
            tts_backend=env.get("PRAXIS_TTS_BACKEND", "edge").strip().lower() or "edge",
            edge_voice=env.get("PRAXIS_EDGE_VOICE", DEFAULT_EDGE_VOICE).strip()
            or DEFAULT_EDGE_VOICE,
            edge_rate=env.get("PRAXIS_EDGE_RATE", "+0%").strip() or "+0%",
            edge_volume=env.get("PRAXIS_EDGE_VOLUME", "+0%").strip() or "+0%",
            edge_pitch=env.get("PRAXIS_EDGE_PITCH", "+0Hz").strip() or "+0Hz",
            edge_timeout=_env_int(
                env.get("PRAXIS_EDGE_TIMEOUT"), 45, name="PRAXIS_EDGE_TIMEOUT"
            ),
            piper_model=Path(
                env.get("PRAXIS_PIPER_MODEL", str(default_piper))
            ).expanduser(),
            piper_use_cuda=_env_bool(env.get("PRAXIS_PIPER_USE_CUDA"), False),
            tts_max_chars=_env_int(
                env.get("PRAXIS_TTS_MAX_CHARS"),
                4000,
                name="PRAXIS_TTS_MAX_CHARS",
            ),
        )


class SpeechToText(Protocol):
    def transcribe(self, path: str | os.PathLike[str]) -> str: ...


class TextToSpeech(Protocol):
    def synthesize(self, text: str) -> Path: ...


class FasterWhisperSTT:
    """Lazy, single-worker faster-whisper adapter."""

    def __init__(
        self,
        config: AudioConfig,
        *,
        model_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self._model_factory = model_factory
        self._model: Any | None = None
        self._load_lock = threading.Lock()
        self._run_lock = threading.Lock()

    def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            factory = self._model_factory
            if factory is None:
                try:
                    from faster_whisper import WhisperModel
                except ImportError as exc:
                    raise AudioDependencyError(
                        "faster-whisper is not installed"
                    ) from exc
                factory = WhisperModel

            try:
                self._model = factory(
                    self.config.stt_model,
                    device=self.config.stt_device,
                    compute_type=self.config.stt_compute_type,
                    cpu_threads=self.config.stt_cpu_threads,
                    num_workers=self.config.stt_num_workers,
                    download_root=str(self.config.model_dir / "whisper"),
                    local_files_only=self.config.stt_local_files_only,
                    revision=self.config.stt_revision,
                )
            except AudioBackendError:
                raise
            except Exception as exc:
                raise AudioConfigurationError(
                    "failed to load the configured faster-whisper model"
                ) from exc
            return self._model

    def transcribe(self, path: str | os.PathLike[str]) -> str:
        source = Path(path).expanduser()
        if not source.is_file():
            raise AudioConfigurationError(f"audio input does not exist: {source}")
        size = source.stat().st_size
        if size == 0:
            raise AudioConfigurationError(f"audio input is empty: {source}")
        if size > self.config.stt_max_bytes:
            raise AudioConfigurationError(
                f"audio input is too large ({size} > {self.config.stt_max_bytes} bytes)"
            )

        try:
            model = self._get_model()
            with self._run_lock:
                segments, _info = model.transcribe(
                    str(source),
                    language=self.config.stt_language,
                    beam_size=self.config.stt_beam_size,
                    vad_filter=self.config.stt_vad_filter,
                    condition_on_previous_text=False,
                )
                parts = [
                    str(segment.text).strip()
                    for segment in segments
                    if getattr(segment, "text", "").strip()
                ]
        except AudioBackendError:
            raise
        except Exception as exc:
            raise AudioProcessingError(f"failed to transcribe {source.name}") from exc
        finally:
            if not self.config.stt_keep_loaded:
                self.clear_cache()
        return " ".join(parts)

    def clear_cache(self) -> None:
        with self._load_lock:
            with self._run_lock:
                self._model = None
        gc.collect()


class PiperTTS:
    """Lazy Piper adapter that writes atomic PCM WAV artifacts."""

    def __init__(
        self,
        config: AudioConfig,
        *,
        voice_loader: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self._voice_loader = voice_loader
        self._voice: Any | None = None
        self._load_lock = threading.Lock()
        self._run_lock = threading.Lock()

    def _get_voice(self) -> Any:
        if self._voice is not None:
            return self._voice
        with self._load_lock:
            if self._voice is not None:
                return self._voice

            model_path = self.config.piper_model
            config_path = Path(f"{model_path}.json")
            if not model_path.is_file():
                raise AudioConfigurationError(
                    f"Piper model does not exist: {model_path}"
                )
            if not config_path.is_file():
                raise AudioConfigurationError(
                    f"Piper model config does not exist: {config_path}"
                )

            loader = self._voice_loader
            if loader is None:
                try:
                    from piper import PiperVoice
                except ImportError as exc:
                    raise AudioDependencyError("piper-tts is not installed") from exc
                loader = PiperVoice.load

            try:
                self._voice = loader(
                    str(model_path),
                    config_path=str(config_path),
                    use_cuda=self.config.piper_use_cuda,
                )
            except AudioBackendError:
                raise
            except Exception as exc:
                raise AudioConfigurationError(
                    "failed to load the configured Piper voice"
                ) from exc
            return self._voice

    def synthesize(self, text: str) -> Path:
        if not isinstance(text, str):
            raise AudioConfigurationError("TTS input must be text")
        clean_text = text.strip()
        if not clean_text:
            raise AudioConfigurationError("TTS input is empty")
        if len(clean_text) > self.config.tts_max_chars:
            raise AudioConfigurationError(
                f"TTS input is too long ({len(clean_text)} > "
                f"{self.config.tts_max_chars} characters)"
            )

        voice = self._get_voice()
        output_dir = self.config.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            output_dir.chmod(0o700)
        except OSError:
            pass

        artifact_id = uuid.uuid4().hex
        final_path = output_dir / f"tts-{artifact_id}.wav"
        partial_path = output_dir / f".tts-{artifact_id}.part.wav"
        try:
            with self._run_lock:
                with wave.open(str(partial_path), "wb") as wav_file:
                    voice.synthesize_wav(clean_text, wav_file)
            if partial_path.stat().st_size <= 44:
                raise AudioProcessingError("Piper produced an empty WAV file")
            os.replace(partial_path, final_path)
            try:
                final_path.chmod(0o600)
            except OSError:
                pass
        except AudioBackendError:
            partial_path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            partial_path.unlink(missing_ok=True)
            raise AudioProcessingError("failed to synthesize speech") from exc
        return final_path

    def clear_cache(self) -> None:
        with self._load_lock:
            with self._run_lock:
                self._voice = None
        gc.collect()


class EdgeTTS:
    """High-quality Microsoft neural voice adapter producing an atomic MP3."""

    def __init__(self, config: AudioConfig, *,
                 communicator_factory: Callable[..., Any] | None = None) -> None:
        self.config = config
        self._communicator_factory = communicator_factory
        self._run_lock = threading.Lock()

    def synthesize(self, text: str) -> Path:
        if not isinstance(text, str):
            raise AudioConfigurationError("TTS input must be text")
        clean_text = text.strip()
        if not clean_text:
            raise AudioConfigurationError("TTS input is empty")
        if len(clean_text) > self.config.tts_max_chars:
            raise AudioConfigurationError(
                f"TTS input is too long ({len(clean_text)} > "
                f"{self.config.tts_max_chars} characters)"
            )
        factory = self._communicator_factory
        if factory is None:
            try:
                from edge_tts import Communicate
            except ImportError as exc:
                raise AudioDependencyError("edge-tts is not installed") from exc
            factory = Communicate

        output_dir = self.config.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        artifact_id = uuid.uuid4().hex
        final_path = output_dir / f"tts-{artifact_id}.mp3"
        partial_path = output_dir / f".tts-{artifact_id}.part.mp3"

        async def _save() -> None:
            communicator = factory(
                clean_text,
                self.config.edge_voice,
                rate=self.config.edge_rate,
                volume=self.config.edge_volume,
                pitch=self.config.edge_pitch,
            )
            await communicator.save(str(partial_path))

        try:
            with self._run_lock:
                asyncio.run(asyncio.wait_for(_save(), timeout=self.config.edge_timeout))
            if partial_path.stat().st_size < 128:
                raise AudioProcessingError("neural TTS produced an empty MP3 file")
            os.replace(partial_path, final_path)
            try:
                final_path.chmod(0o600)
            except OSError:
                pass
            return final_path
        except AudioBackendError:
            partial_path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            partial_path.unlink(missing_ok=True)
            raise AudioProcessingError("online neural TTS failed") from exc


class FallbackTTS:
    """Prefer the quality backend; keep a local female Piper voice as continuity."""

    def __init__(self, primary: TextToSpeech, fallback: TextToSpeech) -> None:
        self.primary = primary
        self.fallback = fallback

    def synthesize(self, text: str) -> Path:
        try:
            return self.primary.synthesize(text)
        except AudioBackendError:
            return self.fallback.synthesize(text)

    def clear_cache(self) -> None:
        for backend in (self.primary, self.fallback):
            clear = getattr(backend, "clear_cache", None)
            if callable(clear):
                clear()


class MediaAudio:
    """Small pluggable facade used by the Telegram/application layer."""

    def __init__(
        self,
        config: AudioConfig | None = None,
        *,
        stt: SpeechToText | None = None,
        tts: TextToSpeech | None = None,
    ) -> None:
        self.config = config or AudioConfig.from_env()
        self.stt = stt or FasterWhisperSTT(self.config)
        if tts is not None:
            self.tts = tts
        elif self.config.tts_backend == "piper":
            self.tts = PiperTTS(self.config)
        elif self.config.tts_backend == "edge":
            self.tts = FallbackTTS(EdgeTTS(self.config), PiperTTS(self.config))
        else:
            raise AudioConfigurationError(
                f"PRAXIS_TTS_BACKEND must be edge or piper, got {self.config.tts_backend!r}"
            )

    def transcribe(self, path: str | os.PathLike[str]) -> str:
        return self.stt.transcribe(path)

    def synthesize(self, text: str) -> Path:
        return self.tts.synthesize(text)

    def clear_caches(self) -> None:
        for backend in (self.stt, self.tts):
            clear = getattr(backend, "clear_cache", None)
            if callable(clear):
                clear()


_default_backend: MediaAudio | None = None
_default_backend_lock = threading.Lock()


def set_default_backend(backend: MediaAudio | None) -> None:
    """Replace/reset the process-wide facade (primarily for wiring and tests)."""

    global _default_backend
    with _default_backend_lock:
        _default_backend = backend


def get_default_backend() -> MediaAudio:
    global _default_backend
    if _default_backend is not None:
        return _default_backend
    with _default_backend_lock:
        if _default_backend is None:
            _default_backend = MediaAudio()
        return _default_backend


def transcribe(path: str | os.PathLike[str]) -> str:
    """Transcribe a local audio file using the configured local backend."""

    return get_default_backend().transcribe(path)


def synthesize(text: str) -> Path:
    """Synthesize text to a unique local audio artifact."""

    return get_default_backend().synthesize(text)


def warm(*, stt: bool = True, tts: bool = True) -> dict[str, str]:
    """Eagerly load the STT (and local TTS) models into the resident cache so they are
    in memory FROM BOOT, not lazily on the first voice.  ``keep_loaded`` then holds them.
    Best-effort by contract: this must NEVER raise — a warmup failure must not stop the
    process booting; the models simply fall back to their lazy first-use load path.
    """
    result: dict[str, str] = {}
    backend = get_default_backend()
    if stt:
        try:
            loader = getattr(backend.stt, "_get_model", None)
            if callable(loader):
                loader()
                result["stt"] = "loaded"
            else:
                result["stt"] = "no-loader"
        except Exception as exc:  # noqa: BLE001 — warmup is strictly best-effort
            result["stt"] = f"{type(exc).__name__}: {exc}"
    if tts:
        try:
            loader = getattr(backend.tts, "_get_model", None)
            if callable(loader):
                loader()
            else:
                synthesize("прогрев")  # local piper loads its model into cache
            result["tts"] = "loaded"
        except Exception as exc:  # noqa: BLE001 — warmup is strictly best-effort
            result["tts"] = f"{type(exc).__name__}: {exc}"
    return result


__all__ = [
    "AudioBackendError",
    "AudioConfig",
    "AudioConfigurationError",
    "AudioDependencyError",
    "AudioProcessingError",
    "EdgeTTS",
    "FallbackTTS",
    "FasterWhisperSTT",
    "MediaAudio",
    "PiperTTS",
    "get_default_backend",
    "set_default_backend",
    "synthesize",
    "transcribe",
]
