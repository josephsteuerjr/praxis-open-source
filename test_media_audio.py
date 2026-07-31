"""Hermetic tests for the optional offline audio backends."""

from __future__ import annotations

import tempfile
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import media_audio


class FakeWhisperModel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def transcribe(self, path: str, **kwargs: object):
        self.calls.append((path, kwargs))
        segments = iter(
            [
                SimpleNamespace(text="  Привет, "),
                SimpleNamespace(text=""),
                SimpleNamespace(text="это Пракс.  "),
            ]
        )
        return segments, SimpleNamespace(language="ru")


class FakePiperVoice:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def synthesize_wav(self, text: str, wav_file: wave.Wave_write) -> None:
        self.texts.append(text)
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(22050)
        wav_file.writeframes(b"\x00\x00" * 100)


class MediaAudioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.model_dir = self.root / "models"
        self.output_dir = self.root / "output"
        self.piper_model = self.model_dir / "piper" / "voice.onnx"
        self.piper_model.parent.mkdir(parents=True)
        self.piper_model.write_bytes(b"fake onnx")
        Path(f"{self.piper_model}.json").write_text("{}", encoding="utf-8")
        self.config = media_audio.AudioConfig(
            model_dir=self.model_dir,
            output_dir=self.output_dir,
            piper_model=self.piper_model,
            stt_max_bytes=1024,
            tts_max_chars=100,
        )

    def tearDown(self) -> None:
        media_audio.set_default_backend(None)
        self.temp_dir.cleanup()

    def test_config_from_env_is_offline_and_cpu_safe_by_default(self) -> None:
        config = media_audio.AudioConfig.from_env(
            {
                "PRAXIS_AUDIO_MODEL_DIR": str(self.model_dir),
                "PRAXIS_TTS_OUTPUT_DIR": str(self.output_dir),
            }
        )
        self.assertEqual(config.stt_model, media_audio.DEFAULT_STT_MODEL)
        self.assertEqual(config.stt_revision, media_audio.DEFAULT_STT_REVISION)
        self.assertEqual(config.stt_device, "cpu")
        self.assertEqual(config.stt_compute_type, "int8")
        self.assertEqual(config.stt_cpu_threads, 2)
        self.assertEqual(config.stt_num_workers, 1)
        self.assertEqual(
            config.stt_model,
            "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        )
        self.assertTrue(config.stt_local_files_only)
        self.assertEqual(config.stt_beam_size, 5)
        self.assertEqual(config.tts_backend, "edge")
        self.assertEqual(config.edge_voice, "ru-RU-SvetlanaNeural")
        self.assertEqual(
            config.piper_model,
            self.model_dir / "piper" / media_audio.DEFAULT_PIPER_VOICE,
        )

    def test_warm_loads_stt_and_tts_into_resident_cache(self) -> None:
        # Boot warmup: both models are pulled into the resident cache up front so they
        # are in memory from boot, not lazily on the first voice.
        calls: list[str] = []

        class _FakeSTT:
            def _get_model(self) -> None:
                calls.append("stt")

        class _FakeTTS:
            def _get_model(self) -> None:
                calls.append("tts")

        media_audio.set_default_backend(SimpleNamespace(stt=_FakeSTT(), tts=_FakeTTS()))
        result = media_audio.warm()
        self.assertEqual(result, {"stt": "loaded", "tts": "loaded"})
        self.assertEqual(sorted(calls), ["stt", "tts"])

    def test_warm_is_best_effort_and_never_raises(self) -> None:
        # A warmup failure (e.g. a missing model file) must NEVER stop the process
        # booting — it is captured in the report and the model falls back to lazy load.
        class _BoomSTT:
            def _get_model(self) -> None:
                raise RuntimeError("model file missing")

        media_audio.set_default_backend(
            SimpleNamespace(stt=_BoomSTT(), tts=SimpleNamespace(_get_model=lambda: None))
        )
        result = media_audio.warm()  # must not raise
        self.assertIn("RuntimeError", result["stt"])
        self.assertEqual(result["tts"], "loaded")

    def test_stt_is_lazy_cached_and_consumes_segment_generator(self) -> None:
        input_path = self.root / "voice.ogg"
        input_path.write_bytes(b"not real audio")
        created: list[tuple[tuple[object, ...], dict[str, object]]] = []
        fake_model = FakeWhisperModel()

        def factory(*args: object, **kwargs: object) -> FakeWhisperModel:
            created.append((args, kwargs))
            return fake_model

        backend = media_audio.FasterWhisperSTT(
            self.config, model_factory=factory
        )
        self.assertEqual(created, [])

        self.assertEqual(backend.transcribe(input_path), "Привет, это Пракс.")
        self.assertEqual(backend.transcribe(input_path), "Привет, это Пракс.")
        self.assertEqual(len(created), 1)
        args, load_kwargs = created[0]
        self.assertEqual(args, (media_audio.DEFAULT_STT_MODEL,))
        self.assertEqual(load_kwargs["device"], "cpu")
        self.assertEqual(load_kwargs["compute_type"], "int8")
        self.assertEqual(load_kwargs["cpu_threads"], 2)
        self.assertEqual(load_kwargs["num_workers"], 1)
        self.assertTrue(load_kwargs["local_files_only"])
        self.assertEqual(len(fake_model.calls), 2)
        _, call_kwargs = fake_model.calls[0]
        self.assertEqual(call_kwargs["language"], "ru")
        self.assertEqual(call_kwargs["beam_size"], 5)
        self.assertTrue(call_kwargs["vad_filter"])
        self.assertFalse(call_kwargs["condition_on_previous_text"])

    def test_stt_rejects_missing_or_oversized_input_before_model_load(self) -> None:
        loaded = False

        def factory(*_args: object, **_kwargs: object) -> FakeWhisperModel:
            nonlocal loaded
            loaded = True
            return FakeWhisperModel()

        backend = media_audio.FasterWhisperSTT(
            self.config, model_factory=factory
        )
        with self.assertRaises(media_audio.AudioConfigurationError):
            backend.transcribe(self.root / "missing.ogg")

        empty = self.root / "empty.ogg"
        empty.touch()
        with self.assertRaises(media_audio.AudioConfigurationError):
            backend.transcribe(empty)

        oversized = self.root / "oversized.ogg"
        oversized.write_bytes(b"x" * 1025)
        with self.assertRaises(media_audio.AudioConfigurationError):
            backend.transcribe(oversized)
        self.assertFalse(loaded)

    def test_external_stt_requires_resident_model_and_uses_praxis_profile(self) -> None:
        input_path = self.root / "shared.ogg"
        input_path.write_bytes(b"voice")
        fake_model = FakeWhisperModel()
        backend = media_audio.FasterWhisperSTT(
            self.config,
            model_factory=lambda *_args, **_kwargs: fake_model,
        )

        self.assertFalse(backend.is_loaded)
        with self.assertRaises(media_audio.AudioNotReadyError):
            backend.transcribe_external(input_path, language=None)
        self.assertFalse(backend.is_loaded, "RPC must never trigger a model load")

        backend._get_model()
        self.assertTrue(backend.is_loaded)
        result = backend.transcribe_external(
            input_path,
            language="uz",
        )
        self.assertEqual(result, "Привет, это Пракс.")
        _, kwargs = fake_model.calls[-1]
        self.assertEqual(kwargs["language"], "uz")
        self.assertEqual(kwargs["beam_size"], self.config.stt_beam_size)

    def test_external_stt_never_queues_behind_the_inference_worker(self) -> None:
        input_path = self.root / "shared.ogg"
        input_path.write_bytes(b"voice")
        backend = media_audio.FasterWhisperSTT(
            self.config,
            model_factory=lambda *_args, **_kwargs: FakeWhisperModel(),
        )
        backend._get_model()
        self.assertTrue(backend._run_lock.acquire(blocking=False))
        try:
            self.assertTrue(backend.is_busy)
            with self.assertRaises(media_audio.AudioBusyError):
                backend.transcribe_external(input_path, language="ru")
        finally:
            backend._run_lock.release()
        self.assertFalse(backend.is_busy)

    def test_constrained_deploy_releases_turbo_model_after_each_file(self) -> None:
        input_path = self.root / "voice.ogg"
        input_path.write_bytes(b"voice")
        created: list[FakeWhisperModel] = []

        def factory(*_args: object, **_kwargs: object) -> FakeWhisperModel:
            model = FakeWhisperModel()
            created.append(model)
            return model

        backend = media_audio.FasterWhisperSTT(
            replace(self.config, stt_keep_loaded=False), model_factory=factory
        )
        backend.transcribe(input_path)
        self.assertIsNone(backend._model)
        backend.transcribe(input_path)
        self.assertEqual(len(created), 2)

    def test_piper_is_lazy_cached_and_writes_unique_valid_wav_files(self) -> None:
        loaded: list[tuple[str, dict[str, object]]] = []
        fake_voice = FakePiperVoice()

        def loader(model_path: str, **kwargs: object) -> FakePiperVoice:
            loaded.append((model_path, kwargs))
            return fake_voice

        backend = media_audio.PiperTTS(self.config, voice_loader=loader)
        self.assertEqual(loaded, [])

        first = backend.synthesize("  Привет!  ")
        second = backend.synthesize("Снова привет!")
        self.assertNotEqual(first, second)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0][0], str(self.piper_model))
        self.assertEqual(
            loaded[0][1]["config_path"], str(Path(f"{self.piper_model}.json"))
        )
        self.assertFalse(loaded[0][1]["use_cuda"])
        self.assertEqual(fake_voice.texts, ["Привет!", "Снова привет!"])

        for artifact in (first, second):
            self.assertTrue(artifact.is_file())
            with wave.open(str(artifact), "rb") as wav_file:
                self.assertEqual(wav_file.getnchannels(), 1)
                self.assertEqual(wav_file.getsampwidth(), 2)
                self.assertEqual(wav_file.getframerate(), 22050)
                self.assertEqual(wav_file.getnframes(), 100)

    def test_edge_tts_uses_female_neural_voice_and_writes_atomic_mp3(self) -> None:
        calls: list[tuple] = []

        class Communicate:
            def __init__(self, text, voice, **kwargs):
                calls.append((text, voice, kwargs))

            async def save(self, path):
                Path(path).write_bytes(b"ID3" + b"x" * 256)

        backend = media_audio.EdgeTTS(self.config, communicator_factory=Communicate)
        artifact = backend.synthesize("  Привет!  ")
        self.assertEqual(artifact.suffix, ".mp3")
        self.assertGreater(artifact.stat().st_size, 128)
        self.assertEqual(calls[0][0], "Привет!")
        self.assertEqual(calls[0][1], "ru-RU-SvetlanaNeural")

    def test_quality_tts_falls_back_to_local_voice(self) -> None:
        expected = self.root / "fallback.wav"

        class Broken:
            def synthesize(self, _text):
                raise media_audio.AudioProcessingError("network")

        class Local:
            def synthesize(self, _text):
                return expected

        backend = media_audio.FallbackTTS(Broken(), Local())
        self.assertEqual(backend.synthesize("привет"), expected)

    def test_facade_is_pluggable(self) -> None:
        input_path = self.root / "voice.ogg"
        input_path.write_bytes(b"voice")
        expected_output = self.root / "answer.wav"

        class StubSTT:
            def transcribe(self, path: str | Path) -> str:
                self.path = Path(path)
                return "текст"

        class StubTTS:
            def synthesize(self, text: str) -> Path:
                self.text = text
                return expected_output

        backend = media_audio.MediaAudio(
            self.config,
            stt=StubSTT(),
            tts=StubTTS(),
        )
        media_audio.set_default_backend(backend)
        self.assertEqual(media_audio.transcribe(input_path), "текст")
        self.assertEqual(media_audio.synthesize("ответ"), expected_output)

    def test_tts_rejects_empty_and_oversized_text(self) -> None:
        loaded = False

        def loader(*_args: object, **_kwargs: object) -> FakePiperVoice:
            nonlocal loaded
            loaded = True
            return FakePiperVoice()

        backend = media_audio.PiperTTS(self.config, voice_loader=loader)
        with self.assertRaises(media_audio.AudioConfigurationError):
            backend.synthesize("  ")
        with self.assertRaises(media_audio.AudioConfigurationError):
            backend.synthesize("x" * 101)
        self.assertFalse(loaded)


if __name__ == "__main__":
    unittest.main()
