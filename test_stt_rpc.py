"""Hermetic tests for the authenticated shared Whisper Unix-socket API."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from aiohttp import ClientSession, FormData, UnixConnector
from aiohttp.test_utils import TestClient, TestServer

import media_audio
import stt_rpc


TOKEN = "a" * 48


class FakeExternalStt:
    def __init__(self) -> None:
        self.loaded = True
        self.busy = False
        self.calls: list[tuple[bytes, str | None]] = []
        self.paths: list[Path] = []
        self.transcript = "секретный транскрипт"
        self.failure: Exception | None = None
        self.started: threading.Event | None = None
        self.release: threading.Event | None = None

    @property
    def is_loaded(self) -> bool:
        return self.loaded

    @property
    def is_busy(self) -> bool:
        return self.busy

    def transcribe_external(
        self,
        path: str | os.PathLike[str],
        *,
        language: str | None,
    ) -> str:
        source = Path(path)
        self.paths.append(source)
        self.calls.append((source.read_bytes(), language))
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            self.release.wait(timeout=2)
        if self.failure is not None:
            raise self.failure
        return self.transcript


class FakeMultipartPart:
    def __init__(self, name: str, payload: bytes) -> None:
        self.name = name
        self._payload = payload

    async def read_chunk(self, *, size: int) -> bytes:
        del size
        payload, self._payload = self._payload, b""
        return payload


class FakeMultipartRequest:
    def __init__(self) -> None:
        self.headers = {"Authorization": f"Bearer {TOKEN}"}
        self.content_type = "multipart/form-data"
        self.content_length = 128
        self._parts = [
            FakeMultipartPart("audio", b"voice"),
            FakeMultipartPart("language", b"ru"),
        ]

    async def multipart(self) -> "FakeMultipartRequest":
        return self

    async def next(self) -> FakeMultipartPart | None:
        if not self._parts:
            return None
        return self._parts.pop(0)


class SttRpcTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        self.backend = FakeExternalStt()
        self.config = stt_rpc.SttRpcConfig(
            enabled=True,
            socket_path=self.root / "stt.sock",
            token_file=self.root / "client.token",
            max_bytes=64,
            rate_per_minute=120,
            burst=10,
            upload_timeout_seconds=5,
            temp_dir=self.uploads,
        )
        self.rpc = stt_rpc.SttRpcServer(
            self.config,
            self.backend,
            token=TOKEN,
        )
        self.client = TestClient(TestServer(self.rpc.app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.temp.cleanup()

    @staticmethod
    def _headers(token: str = TOKEN) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _form(
        audio: bytes = b"voice",
        *,
        language: str = "ru",
        filename: str = "voice.ogg",
    ) -> FormData:
        form = FormData()
        form.add_field(
            "audio",
            audio,
            filename=filename,
            content_type="application/octet-stream",
        )
        form.add_field("language", language)
        return form

    async def test_health_is_authenticated_and_reports_warmup_and_busy(self) -> None:
        response = await self.client.get("/healthz")
        self.assertEqual(response.status, 401)
        self.assertEqual((await response.json())["error"]["code"], "unauthorized")

        self.backend.loaded = False
        response = await self.client.get("/healthz", headers=self._headers())
        payload = await response.json()
        self.assertEqual(response.status, 503)
        self.assertEqual(payload["schema"], stt_rpc.HEALTH_SCHEMA)
        self.assertEqual(payload["status"], "warming")
        self.assertFalse(payload["ready"])

        self.backend.loaded = True
        self.backend.busy = True
        response = await self.client.get("/healthz", headers=self._headers())
        payload = await response.json()
        self.assertEqual(response.status, 200)
        self.assertTrue(payload["ready"])
        self.assertTrue(payload["busy"])

    async def test_transcription_supports_ru_uz_auto_and_cleans_temp_files(self) -> None:
        expected = {"ru": "ru", "uz": "uz", "auto": None}
        for language, backend_language in expected.items():
            response = await self.client.post(
                "/v1/transcriptions",
                data=self._form(language=language),
                headers=self._headers(),
            )
            payload = await response.json()
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["schema"], stt_rpc.SCHEMA)
            self.assertEqual(payload["text"], self.backend.transcript)
            self.assertEqual(payload["language"], language)
            self.assertEqual(self.backend.calls[-1][1], backend_language)

        self.assertEqual(list(self.uploads.iterdir()), [])
        self.assertTrue(self.backend.paths)
        self.assertTrue(all(not path.exists() for path in self.backend.paths))

    async def test_logs_never_contain_filename_audio_or_transcript(self) -> None:
        private_filename = "client-passport-123.ogg"
        private_audio = b"private-audio-marker"
        self.backend.transcript = "private transcript marker"
        with self.assertLogs("praxis-stt-rpc", level=logging.INFO) as captured:
            response = await self.client.post(
                "/v1/transcriptions",
                data=self._form(
                    private_audio,
                    language="auto",
                    filename=private_filename,
                ),
                headers=self._headers(),
            )
        self.assertEqual(response.status, 200)
        logs = "\n".join(captured.output)
        self.assertNotIn(private_filename, logs)
        self.assertNotIn(private_audio.decode("ascii"), logs)
        self.assertNotIn(self.backend.transcript, logs)

        private_error = "decoder exposed client-passport-123.ogg"
        self.backend.failure = media_audio.AudioProcessingError(private_error)
        with self.assertLogs("praxis-stt-rpc", level=logging.INFO) as failed:
            response = await self.client.post(
                "/v1/transcriptions",
                data=self._form(),
                headers=self._headers(),
            )
        payload = await response.json()
        self.assertEqual(response.status, 422)
        self.assertEqual(payload["error"]["code"], "transcription_failed")
        self.assertNotIn(private_error, str(payload))
        self.assertNotIn(private_error, "\n".join(failed.output))

    async def test_rejects_wrong_shape_language_and_size_without_temp_leaks(self) -> None:
        response = await self.client.post(
            "/v1/transcriptions",
            data=b"voice",
            headers={
                **self._headers(),
                "Content-Type": "application/octet-stream",
            },
        )
        self.assertEqual(response.status, 415)

        response = await self.client.post(
            "/v1/transcriptions",
            data=b"--missing-boundary",
            headers={
                **self._headers(),
                "Content-Type": "multipart/form-data",
            },
        )
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "invalid_multipart")

        response = await self.client.post(
            "/v1/transcriptions",
            data=b"--x\r\nbad-header\r\n\r\nabc\r\n--x--\r\n",
            headers={
                **self._headers(),
                "Content-Type": "multipart/form-data; boundary=x",
            },
        )
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "invalid_multipart")

        response = await self.client.post(
            "/v1/transcriptions",
            data=b"--different-boundary--\r\n",
            headers={
                **self._headers(),
                "Content-Type": "multipart/form-data; boundary=declared-boundary",
            },
        )
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "invalid_multipart")

        response = await self.client.post(
            "/v1/transcriptions",
            data=self._form(language="en"),
            headers=self._headers(),
        )
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "invalid_language")

        response = await self.client.post(
            "/v1/transcriptions",
            data=self._form(audio=b"x" * 65),
            headers=self._headers(),
        )
        self.assertEqual(response.status, 413)
        self.assertEqual((await response.json())["error"]["code"], "audio_too_large")

        unexpected = self._form()
        unexpected.add_field("actor", "private-person")
        response = await self.client.post(
            "/v1/transcriptions",
            data=unexpected,
            headers=self._headers(),
        )
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"]["code"], "unexpected_field")
        self.assertEqual(list(self.uploads.iterdir()), [])

    async def test_warming_busy_and_rate_guards_fail_without_inference(self) -> None:
        self.backend.loaded = False
        response = await self.client.post(
            "/v1/transcriptions",
            data=self._form(),
            headers=self._headers(),
        )
        self.assertEqual(response.status, 503)
        self.assertEqual(response.headers["Retry-After"], "2")

        self.backend.loaded = True
        self.backend.busy = True
        response = await self.client.post(
            "/v1/transcriptions",
            data=self._form(),
            headers=self._headers(),
        )
        self.assertEqual(response.status, 429)
        self.assertEqual((await response.json())["error"]["code"], "busy")
        self.assertEqual(self.backend.calls, [])

        await self.client.close()
        limited_config = stt_rpc.SttRpcConfig(
            enabled=True,
            socket_path=self.root / "limited.sock",
            token_file=self.root / "client.token",
            max_bytes=64,
            rate_per_minute=1,
            burst=1,
            upload_timeout_seconds=5,
            temp_dir=self.uploads,
        )
        self.backend.busy = False
        self.rpc = stt_rpc.SttRpcServer(
            limited_config,
            self.backend,
            token=TOKEN,
        )
        self.client = TestClient(TestServer(self.rpc.app))
        await self.client.start_server()

        first = await self.client.post(
            "/v1/transcriptions",
            data=self._form(),
            headers=self._headers(),
        )
        self.assertEqual(first.status, 200)
        second = await self.client.post(
            "/v1/transcriptions",
            data=self._form(),
            headers=self._headers(),
        )
        self.assertEqual(second.status, 429)
        self.assertEqual((await second.json())["error"]["code"], "rate_limited")
        self.assertIn("Retry-After", second.headers)
        self.assertEqual(len(self.backend.calls), 1)

    async def test_second_external_request_never_queues(self) -> None:
        self.backend.started = threading.Event()
        self.backend.release = threading.Event()
        first = asyncio.create_task(
            self.client.post(
                "/v1/transcriptions",
                data=self._form(audio=b"first"),
                headers=self._headers(),
            )
        )
        for _ in range(100):
            if self.backend.started.is_set():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.backend.started.is_set())
        try:
            second = await self.client.post(
                "/v1/transcriptions",
                data=self._form(audio=b"second"),
                headers=self._headers(),
            )
            self.assertEqual(second.status, 429)
            self.assertEqual((await second.json())["error"]["code"], "busy")
        finally:
            self.backend.release.set()
        first_response = await first
        self.assertEqual(first_response.status, 200)
        self.assertEqual(len(self.backend.calls), 1)

    async def test_cancellation_keeps_temp_until_inference_stops_then_cleans(self) -> None:
        self.backend.started = threading.Event()
        self.backend.release = threading.Event()
        task = asyncio.create_task(
            self.rpc._transcribe(FakeMultipartRequest())  # type: ignore[arg-type]
        )
        for _ in range(100):
            if self.backend.started.is_set():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.backend.started.is_set())
        self.assertEqual(len(self.backend.paths), 1)
        temp_path = self.backend.paths[0]
        self.assertTrue(temp_path.exists())

        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertTrue(self.rpc._external_busy)
        self.assertTrue(temp_path.exists())

        self.backend.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.rpc._external_busy)
        self.assertFalse(temp_path.exists())

    async def test_optional_start_is_disabled_by_default_and_fails_closed(self) -> None:
        disabled = await stt_rpc.start_from_env(self.backend, {})
        self.assertIsNone(disabled)

        missing_token = self.root / "missing.token"
        socket_path = self.root / "not-created.sock"
        crash_dir = self.root / "crash-tmp"
        crash_dir.mkdir()
        crash_audio = crash_dir / "praxis-stt-crash.audio"
        crash_audio.write_bytes(b"private residue")
        with self.assertLogs("praxis-stt-rpc", level=logging.ERROR) as captured:
            failed = await stt_rpc.start_from_env(
                self.backend,
                {
                    "PRAXIS_STT_RPC_ENABLED": "1",
                    "PRAXIS_STT_RPC_SOCKET": str(socket_path),
                    "PRAXIS_STT_RPC_TOKEN_FILE": str(missing_token),
                    "PRAXIS_STT_RPC_TEMP_DIR": str(crash_dir),
                },
            )
        self.assertIsNone(failed)
        self.assertFalse(socket_path.exists())
        self.assertFalse(crash_audio.exists())
        logs = "\n".join(captured.output)
        self.assertIn("SttRpcConfigurationError", logs)
        self.assertNotIn(str(missing_token), logs)

    def test_config_caps_uploads_and_token_comes_from_a_private_file(self) -> None:
        disabled = stt_rpc.SttRpcConfig.from_env({})
        self.assertFalse(disabled.enabled)
        self.assertEqual(disabled.max_bytes, stt_rpc.HARD_MAX_BYTES)

        token_file = self.root / "token"
        token_file.write_text(TOKEN + "\n", encoding="ascii")
        if os.name == "posix":
            token_file.chmod(0o600)
        self.assertEqual(stt_rpc._load_token(token_file), TOKEN)
        if os.name == "posix":
            token_file.chmod(0o604)
            with self.assertRaises(stt_rpc.SttRpcConfigurationError):
                stt_rpc._load_token(token_file)
            token_file.chmod(0o600)
            token_link = self.root / "token.link"
            token_link.symlink_to(token_file)
            with self.assertRaises(stt_rpc.SttRpcConfigurationError):
                stt_rpc._load_token(token_link)

        with self.assertRaises(stt_rpc.SttRpcConfigurationError):
            stt_rpc.SttRpcConfig.from_env(
                {"PRAXIS_STT_RPC_MAX_BYTES": str(stt_rpc.HARD_MAX_BYTES + 1)}
            )

    @unittest.skipUnless(
        os.name == "posix" and hasattr(os, "O_NOFOLLOW"),
        "Unix-domain socket lifecycle is verified on Linux",
    )
    async def test_linux_uds_lifecycle_uses_secret_file_and_removes_socket(self) -> None:
        token_file = self.root / "uds.token"
        token_file.write_text(TOKEN + "\n", encoding="ascii")
        token_file.chmod(0o600)
        socket_path = self.root / "run" / "stt.sock"
        rpc = stt_rpc.SttRpcServer(
            stt_rpc.SttRpcConfig(
                enabled=True,
                socket_path=socket_path,
                token_file=token_file,
                max_bytes=64,
                rate_per_minute=6,
                burst=2,
                upload_timeout_seconds=5,
                temp_dir=self.uploads,
            ),
            self.backend,
        )

        stale_audio = self.uploads / "praxis-stt-dead.audio"
        stale_audio.write_bytes(b"private crash residue")
        unrelated = self.uploads / "keep.txt"
        unrelated.write_text("keep", encoding="utf-8")
        link_target = self.uploads / "keep-target"
        link_target.write_text("keep target", encoding="utf-8")
        stale_link = self.uploads / "praxis-stt-link.audio"
        stale_link.symlink_to(link_target)

        await rpc.start()
        try:
            self.assertFalse(stale_audio.exists())
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")
            self.assertTrue(stale_link.is_symlink())
            self.assertEqual(link_target.read_text(encoding="utf-8"), "keep target")
            self.assertTrue(socket_path.exists())
            self.assertEqual(stat.S_IMODE(socket_path.stat().st_mode), 0o660)
            async with ClientSession(
                connector=UnixConnector(path=str(socket_path))
            ) as session:
                async with session.get(
                    "http://localhost/healthz",
                    headers=self._headers(),
                ) as response:
                    self.assertEqual(response.status, 200)
                    self.assertTrue((await response.json())["ready"])
        finally:
            await rpc.stop()
        self.assertFalse(socket_path.exists())

    @unittest.skipUnless(
        os.name == "posix" and hasattr(os, "O_NOFOLLOW"),
        "Unix-domain socket collision handling is verified on Linux",
    )
    async def test_linux_uds_refuses_active_or_non_socket_targets(self) -> None:
        token_file = self.root / "collision.token"
        token_file.write_text(TOKEN, encoding="ascii")
        token_file.chmod(0o600)
        socket_path = self.root / "collision" / "stt.sock"
        config = stt_rpc.SttRpcConfig(
            enabled=True,
            socket_path=socket_path,
            token_file=token_file,
            max_bytes=64,
            rate_per_minute=6,
            burst=2,
            upload_timeout_seconds=5,
            temp_dir=self.uploads,
        )
        first = stt_rpc.SttRpcServer(config, self.backend)
        second = stt_rpc.SttRpcServer(config, self.backend)
        await first.start()
        try:
            with self.assertRaises(stt_rpc.SttRpcConfigurationError):
                await second.start()
            self.assertTrue(socket_path.exists())
        finally:
            await second.stop()
            await first.stop()

        socket_path.write_text("do not delete", encoding="utf-8")
        third = stt_rpc.SttRpcServer(config, self.backend)
        with self.assertRaises(stt_rpc.SttRpcConfigurationError):
            await third.start()
        self.assertEqual(socket_path.read_text(encoding="utf-8"), "do not delete")


if __name__ == "__main__":
    unittest.main()
