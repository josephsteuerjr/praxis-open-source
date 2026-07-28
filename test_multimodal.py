"""Герметичная сквозная проверка media -> agent guard -> Telegram sender boundary."""
from __future__ import annotations

import asyncio
import tempfile
import types
import unittest
import wave
from pathlib import Path

import agent
import media
import media_audio
import mtproto_runner as runner


JPEG = b"\xff\xd8\xff\xe0" + b"fixture-jpeg"
OGG = b"OggS" + b"fixture-audio"


class FakeAudio:
    def __init__(self, root: Path):
        self.root = root

    def transcribe(self, path):
        return "Проверочная расшифровка"

    def synthesize(self, text):
        p = self.root / "spoken.wav"
        with wave.open(str(p), "wb") as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000)
            out.writeframes(b"\x00\x00" * 100)
        return p


class TestAgentMultimodal(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="praxis_multi_")
        self.root = Path(self.td.name)
        self.spool = media.MediaSpool(self.root / "spool")
        self.old_spool = agent._MEDIA_SPOOL
        agent._MEDIA_SPOOL = self.spool
        media_audio.set_default_backend(FakeAudio(self.root))
        self.ctx = agent.ChannelContext(chat_id="777", is_dm=True, owner=True)

    def tearDown(self):
        agent._MEDIA_SPOOL = self.old_spool
        media_audio.set_default_backend(None)
        self.td.cleanup()

    def _refs(self):
        photo = self.spool.ingest_bytes(
            JPEG, kind="photo", filename="x.jpg", chat_id="777", message_id=1, scope="owner")
        audio = self.spool.ingest_bytes(
            OGG, kind="audio", filename="x.ogg", chat_id="777", message_id=2, scope="owner")
        return photo, audio

    def test_photo_and_audio_reach_model_content(self):
        text, content = agent._media_prompt("Егор: посмотри и послушай", self._refs(), self.ctx)
        self.assertIn("Проверочная расшифровка", text)
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image")
        self.assertEqual(content[1]["mime"], "image/jpeg")

    def test_synthesized_audio_is_staged_until_guard_passes(self):
        old_configured, old_voice, old_guard = agent.llm.configured, agent._voice, agent.guard_outbound_reply
        calls = []
        agent.llm.configured = lambda *a, **k: True

        def fake_voice(content, history, speaker, **kw):
            calls.append(agent.tool_speak("Привет голосом", "проверка"))
            return "Готово."

        agent._voice = fake_voice
        agent.guard_outbound_reply = lambda reply, *a, **k: reply
        try:
            turn = agent.voice_turn_envelope("777", "Егор: ответь аудио", "Егор", ctx=self.ctx)
        finally:
            agent.llm.configured, agent._voice, agent.guard_outbound_reply = old_configured, old_voice, old_guard
        self.assertEqual(turn.text, "Готово.")
        self.assertEqual(len(turn.outbound), 1)
        self.assertEqual(turn.outbound[0].kind, "audio")
        self.assertTrue(turn.outbound[0].path.is_file())
        self.assertIn("после проверки", calls[0])

    def test_guard_silence_discards_staged_audio(self):
        old_configured, old_voice, old_guard = agent.llm.configured, agent._voice, agent.guard_outbound_reply
        staged = []
        agent.llm.configured = lambda *a, **k: True

        def fake_voice(content, history, speaker, **kw):
            agent.tool_speak("Не должно уйти")
            staged.extend(agent._TURN_OUTBOUND.get() or [])
            return "черновик"

        agent._voice = fake_voice
        agent.guard_outbound_reply = lambda *a, **k: ""
        try:
            turn = agent.voice_turn_envelope("777", "Егор: тест", "Егор", ctx=self.ctx)
        finally:
            agent.llm.configured, agent._voice, agent.guard_outbound_reply = old_configured, old_voice, old_guard
        self.assertFalse(turn.text)
        self.assertEqual(len(staged), 1)
        self.assertFalse(staged[0].path.exists(), "не прошедшее guard медиа нельзя оставить к отправке")


class TestRunnerMultimodal(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="praxis_runner_media_")
        self.root = Path(self.td.name)
        self.spool = media.MediaSpool(self.root / "spool")
        self.old_spool = runner._MEDIA_SPOOL
        self.old_accepted = runner._MEDIA_ACCEPTED
        self.old_sending = runner._MEDIA_SENDING
        runner._MEDIA_SPOOL = self.spool
        runner._MEDIA_ACCEPTED = {}
        runner._MEDIA_SENDING = set()

    def tearDown(self):
        runner._MEDIA_SPOOL = self.old_spool
        runner._MEDIA_ACCEPTED = self.old_accepted
        runner._MEDIA_SENDING = self.old_sending
        self.td.cleanup()

    def test_capture_streams_to_capped_sink_and_binds_scope(self):
        class Msg:
            id = 11
            photo = object()
            voice = audio = None
            file = types.SimpleNamespace(size=len(JPEG), name="lied.png", ext=".png")

            async def download_media(self, file=None):
                self.requested = file
                file.write(JPEG)
                return file

        msg = Msg()
        ref, error = asyncio.run(runner._capture_typed_media(
            msg, chat_id="777", scope="owner", caption="подпись"))
        self.assertEqual(error, "")
        self.assertEqual(ref.mime, "image/jpeg", "magic, а не лживое расширение")
        self.assertEqual(ref.scope, "owner")
        self.assertEqual(str(ref.chat_id), "777")
        self.assertIsInstance(msg.requested, runner._CappedMediaSink)
        self.assertEqual(msg.requested.total, len(JPEG))

    def test_capture_enforces_cap_when_telegram_size_is_missing(self):
        tiny = media.MediaSpool(self.root / "tiny", photo_max_bytes=8)
        runner._MEDIA_SPOOL = tiny

        class Msg:
            id = 12
            photo = object()
            voice = audio = None
            file = types.SimpleNamespace(size=0, name="unknown.jpg", ext=".jpg")

            async def download_media(self, file=None):
                file.write(JPEG)
                return file

        ref, error = asyncio.run(runner._capture_typed_media(
            Msg(), chat_id="777", scope="owner"))
        self.assertIsNone(ref)
        self.assertIn("слишком большое", error)
        self.assertEqual(tiny.used_bytes(), 0)

    def test_sender_revalidates_and_deletes_uploaded_copy(self):
        source = self.root / "photo.jpg"; source.write_bytes(JPEG)
        item = self.spool.queue_outbound(
            source, kind="photo", target_chat_id="777", scope="owner", caption="готово")
        sent = []

        class Client:
            async def send_file(self, *args, **kwargs):
                sent.append((args, kwargs))
                return types.SimpleNamespace(id=321)

        old_client = runner.client
        runner.client = Client()
        ctx = agent.ChannelContext(chat_id="777", is_dm=True, owner=True)
        try:
            ok = asyncio.run(runner._attempt_queued_media(
                "entity", item, ctx=ctx, reply_to=9))
        finally:
            runner.client = old_client
            runner._buf.pop("777", None)
        self.assertTrue(ok)
        self.assertEqual(sent[0][0][0], "entity")
        self.assertEqual(sent[0][1]["reply_to"], 9)
        delivered = self.spool.outbox_results("delivered")
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]["result"]["message_id"], 321)
        self.assertFalse(item.path.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
