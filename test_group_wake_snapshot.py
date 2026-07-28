"""Regressions for delayed explicit group addresses and their frozen provenance.

Run with: python praxis_test.py test_group_wake_snapshot -v
"""
from __future__ import annotations

import asyncio
import datetime
import sys
import time
import types
import unittest
from collections import defaultdict, deque
from unittest.mock import ANY, AsyncMock, Mock, patch

import agent
import media


class _ImportOnlyTelegramClient:
    def __init__(self, *args, **kwargs):
        pass

    def on(self, *args, **kwargs):
        return lambda fn: fn


class _ImportOnlyEvents:
    @staticmethod
    def NewMessage(*args, **kwargs):
        return object()

    @staticmethod
    def ChatAction(*args, **kwargs):
        return object()


if "mtproto_runner" not in sys.modules:
    _prior_telethon = sys.modules.get("telethon")
    _fake_telethon = types.ModuleType("telethon")
    _fake_telethon.TelegramClient = _ImportOnlyTelegramClient
    _fake_telethon.events = _ImportOnlyEvents
    sys.modules["telethon"] = _fake_telethon
    try:
        import mtproto_runner as runner
    finally:
        if _prior_telethon is None:
            sys.modules.pop("telethon", None)
        else:
            sys.modules["telethon"] = _prior_telethon
else:
    import mtproto_runner as runner


def _wake(*, mid: int, ts: float, speaker: str = "Алиса",
          context: str = "Алиса: вопрос", owner: bool = False, sender_id: int = 101,
          reply_targets=(), media_refs=()) -> runner.GroupWake:
    return runner.GroupWake(
        message_id=mid,
        message_ts=ts,
        kind="reply",
        speaker=speaker,
        sender_id=sender_id,
        owner=owner,
        known=True,
        family=False,
        context_snapshot=context,
        reply_targets_snapshot=tuple(reply_targets),
        media_snapshot=tuple(media_refs),
    )


class _Action:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Client:
    def __init__(self):
        self.sent = []

    def action(self, *args, **kwargs):
        return _Action()

    async def send_message(self, entity, text, **kwargs):
        self.sent.append((entity, text, kwargs))
        return types.SimpleNamespace(id=1000 + len(self.sent))


async def _no_compact(_chat_id):
    return None


class TestTelegramTextSplitter(unittest.TestCase):
    def test_utf16_emoji_cap_and_lossless_roundtrip(self):
        text = "🙂" * 2001
        chunks = runner._split_telegram_text(text)
        self.assertEqual("".join(chunks), text)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(runner._utf16_units(chunks[0]), 3800)
        self.assertTrue(all(
            runner._utf16_units(chunk) <= runner.TELEGRAM_TEXT_CHUNK_UTF16
            for chunk in chunks))

    def test_prefers_paragraph_boundary_without_losing_separator(self):
        text = ("🙂" * 1400) + "\n\n" + ("x" * 2500)
        chunks = runner._split_telegram_text(text)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(chunks[0].endswith("\n\n"))
        self.assertTrue(all(runner._utf16_units(chunk) <= 3800 for chunk in chunks))


class TestFrozenGroupPass(unittest.IsolatedAsyncioTestCase):
    async def test_boundary_send_records_telegram_id_and_sender(self):
        chat_id = "-701"
        wake = _wake(mid=10, ts=820.0, speaker="Боб", sender_id=202,
                     context="Боб: давление")
        client = _Client()
        boundaries = defaultdict(lambda: deque(maxlen=8))
        with (
            patch.object(runner, "client", client),
            patch.object(runner, "_meta", {chat_id: {
                "entity": -701, "is_dm": False, "is_owner": False,
                "known": True, "family": False, "name": "Боб",
                "title": "room", "size": 4, "addressed": True,
                "addressed_mid": 10, "room_mode": "normal",
            }}),
            patch.object(runner, "_group_wakes", {chat_id: wake}),
            patch.object(runner, "_boundary_replies", boundaries),
            patch.object(runner, "_last_pass", defaultdict(float)),
            patch.object(runner, "_passing", set()),
            patch.object(runner, "_pending_media", defaultdict(lambda: deque(maxlen=16))),
            patch.object(runner, "_recent_msgs", defaultdict(lambda: deque(maxlen=12))),
            patch.object(runner, "_missed", {}),
            patch.object(runner, "_cooldown", return_value=0.0),
            patch.object(runner, "_maybe_compact", side_effect=_no_compact),
            patch.object(agent, "voice_turn_envelope",
                         return_value=media.TurnEnvelope(text="Хватит.", boundary=True)),
        ):
            await runner._run_pass(chat_id)
            await asyncio.sleep(0)

        self.assertEqual(boundaries[chat_id][0][:2], (1001, 202))

    async def test_deferred_address_uses_frozen_snapshot_provenance_and_age(self):
        chat_id = "-700"
        wake = _wake(
            mid=10, ts=820.0, speaker="Алиса", owner=True,
            context="Иван: раньше\nАлиса (в ответ Praxis): [Стикер]",
            reply_targets=((9, "Иван", "раньше"), (10, "Алиса", "[Стикер]")),
        )
        wakes = {chat_id: wake}
        # Rolling state has moved to Bob's unrelated message before cooldown expires.
        meta = {chat_id: {
            "entity": -700, "is_dm": False, "is_owner": False,
            "known": False, "family": False, "name": "Боб",
            "title": "room", "size": 4, "addressed": False,
            "addressed_mid": None, "room_mode": "normal",
        }}
        recent = defaultdict(lambda: deque(maxlen=12))
        recent[chat_id].extend(((10, "Алиса", "[Стикер]"),
                                (11, "Боб", "совсем другая тема")))
        captured = {}
        client = _Client()

        def voice(*args, **kwargs):
            captured.update(args=args, kwargs=kwargs)
            return media.TurnEnvelope(text="увидела")

        with (
            patch.object(runner, "client", client),
            patch.object(runner, "_meta", meta),
            patch.object(runner, "_group_wakes", wakes),
            patch.object(runner, "_last_pass", defaultdict(float)),
            patch.object(runner, "_passing", set()),
            patch.object(runner, "_pending_media", defaultdict(lambda: deque(maxlen=16))),
            patch.object(runner, "_recent_msgs", recent),
            patch.object(runner, "_missed", {}),
            patch.object(runner, "_cooldown", return_value=0.0),
            patch.object(runner, "_group_context_snapshot",
                         return_value=(wake.context_snapshot +
                                       "\nБоб: совсем другая тема\nВера: вопрос уже закрыт")),
            patch.object(runner, "_last_n_text",
                         side_effect=AssertionError("group pass fetched moving Telegram tail")),
            patch.object(runner, "_buf_push", return_value=None),
            patch.object(runner, "_maybe_compact", side_effect=_no_compact),
            patch.object(runner.time, "time", return_value=1000.0),
            patch.object(agent, "voice_turn_envelope", side_effect=voice),
        ):
            await runner._run_pass(chat_id)
            await asyncio.sleep(0)

        self.assertEqual(captured["args"][0], chat_id)
        self.assertTrue(captured["args"][1].startswith(wake.context_snapshot))
        self.assertIn("новой проверки актуальности", captured["args"][1])
        self.assertIn("Вера: вопрос уже закрыт", captured["args"][1])
        self.assertEqual(captured["args"][2], "Алиса")
        ctx = captured["kwargs"]["ctx"]
        self.assertTrue(ctx.owner, "права должны принадлежать автору адреса, не позднему фону")
        self.assertTrue(ctx.known)
        self.assertEqual(ctx.reply_targets, wake.reply_targets_snapshot)
        self.assertEqual(ctx.address_message_id, 10)
        self.assertEqual(ctx.address_kind, "reply")
        self.assertEqual(ctx.address_age_sec, 180.0)
        self.assertEqual(client.sent, [(-700, "увидела", {"reply_to": 10})])
        self.assertNotIn(chat_id, wakes)

    async def test_cooldown_defers_as_transport_retry_without_model_or_task_side_effects(self):
        chat_id = "-709"
        wake = _wake(mid=90, ts=990.0, context="Алиса: вопрос")
        deferred = Mock()
        notes = []
        with (
            patch.object(runner, "_meta", {chat_id: {
                "is_dm": False, "room_mode": "normal", "addressed": True,
            }}),
            patch.object(runner, "_group_wakes", {chat_id: wake}),
            patch.object(runner, "_last_pass", defaultdict(float, {chat_id: 900.0})),
            patch.object(runner, "_passing", set()),
            patch.object(runner.time, "time", return_value=1000.0),
            patch.object(runner, "_cooldown", return_value=180.0) as cooldown,
            patch.object(runner, "_defer_pass", deferred),
            patch.object(runner.perception, "note_skip",
                         side_effect=lambda *args, **kwargs: notes.append((args, kwargs))),
            patch.object(agent, "voice_turn_envelope",
                         side_effect=AssertionError("cooldown retry called model")),
            patch.object(agent, "task_window",
                         side_effect=AssertionError("cooldown retry opened task")),
            patch.object(agent, "tool_manage_loop",
                         side_effect=AssertionError("cooldown retry touched loop")),
        ):
            await runner._run_pass(chat_id)

        cooldown.assert_called_once_with(False, "normal", addressed=True)
        deferred.assert_called_once_with(chat_id, 80.05)
        self.assertIn("transport retry", notes[0][1]["detail"])
        self.assertIn("актуальность", notes[0][1]["detail"])

    async def test_ambient_and_addressed_group_cooldowns_are_distinct(self):
        with patch.object(runner.perception, "value", side_effect=lambda name: {
            "cooldown_group": 300.0,
            "cooldown_addressed": 180.0,
        }[name]):
            self.assertEqual(runner._cooldown(False, addressed=False), 300.0)
            self.assertEqual(runner._cooldown(False, addressed=True), 180.0)

    async def test_stale_deferred_timer_without_wake_is_a_noop(self):
        chat_id = "-701"
        last_pass = defaultdict(float, {chat_id: 55.0})
        with (
            patch.object(runner, "_meta", {chat_id: {"is_dm": False}}),
            patch.object(runner, "_group_wakes", {}),
            patch.object(runner, "_last_pass", last_pass),
            patch.object(runner, "_passing", set()),
            patch.object(runner, "_cooldown",
                         side_effect=AssertionError("stale timer reached cooldown")),
            patch.object(runner, "_last_n_text",
                         side_effect=AssertionError("stale timer fetched history")),
            patch.object(agent, "voice_turn_envelope",
                         side_effect=AssertionError("stale timer called model")),
        ):
            await runner._run_pass(chat_id)
        self.assertEqual(last_pass[chat_id], 55.0)

    async def test_terminal_consumption_is_generation_safe_and_rearms_new_address(self):
        chat_id = "-702"
        first = _wake(mid=20, ts=900.0, context="Алиса: первый")
        second = _wake(mid=22, ts=999.0, speaker="Вера", context="Вера: второй")
        wakes = {chat_id: first}
        arm = Mock()

        def voice(*args, **kwargs):
            wakes[chat_id] = second  # a real address arrived while the model thread ran
            return media.TurnEnvelope()

        with (
            patch.object(runner, "_meta", {chat_id: {
                "entity": -702, "is_dm": False, "title": "room", "size": 3,
                "room_mode": "normal", "addressed": False,
            }}),
            patch.object(runner, "_group_wakes", wakes),
            patch.object(runner, "_last_pass", defaultdict(float)),
            patch.object(runner, "_passing", set()),
            patch.object(runner, "_pending_media", defaultdict(lambda: deque(maxlen=16))),
            patch.object(runner, "_recent_msgs", defaultdict(lambda: deque(maxlen=12))),
            patch.object(runner, "_missed", {}),
            patch.object(runner, "_cooldown", return_value=0.0),
            patch.object(runner, "_maybe_compact", side_effect=_no_compact),
            patch.object(runner, "_arm", arm),
            patch.object(agent, "voice_turn_envelope", side_effect=voice),
        ):
            await runner._run_pass(chat_id)
            await asyncio.sleep(0)

        self.assertIs(wakes[chat_id], second)
        arm.assert_called_once_with(chat_id)

    async def test_send_failure_preserves_and_rearms_same_address(self):
        chat_id = "-703"
        inbound_ref = object()
        wake = _wake(mid=30, ts=990.0, context="Алиса: не потеряй",
                     media_refs=(inbound_ref,))
        wakes = {chat_id: wake}
        pending = defaultdict(lambda: deque(maxlen=16))
        pending[chat_id].append(inbound_ref)
        arm = Mock()

        class FailingClient(_Client):
            async def send_message(self, entity, text, **kwargs):
                raise RuntimeError("temporary Telegram failure")

        with (
            patch.object(runner, "client", FailingClient()),
            patch.object(runner, "_meta", {chat_id: {
                "entity": -703, "is_dm": False, "title": "room", "size": 3,
                "room_mode": "normal", "addressed": False,
            }}),
            patch.object(runner, "_group_wakes", wakes),
            patch.object(runner, "_last_pass", defaultdict(float)),
            patch.object(runner, "_passing", set()),
            patch.object(runner, "_pending_media", pending),
            patch.object(runner, "_recent_msgs", defaultdict(lambda: deque(maxlen=12))),
            patch.object(runner, "_missed", {}),
            patch.object(runner, "_cooldown", return_value=0.0),
            patch.object(runner, "_maybe_compact", side_effect=_no_compact),
            patch.object(runner, "_arm", arm),
            patch.object(agent, "voice_turn_envelope",
                         return_value=media.TurnEnvelope(text="ответ")),
        ):
            await runner._run_pass(chat_id)
            await asyncio.sleep(0)

        self.assertIs(wakes[chat_id], wake)
        self.assertEqual(tuple(pending[chat_id]), (inbound_ref,))
        arm.assert_called_once_with(chat_id)

    async def test_outbound_handoff_failure_after_text_does_not_repeat_model_turn(self):
        chat_id = "-704"
        inbound_ref = object()
        outbound = object()
        wake = _wake(mid=40, ts=990.0, context="Алиса: ответь",
                     media_refs=(inbound_ref,))
        wakes = {chat_id: wake}
        pending = defaultdict(lambda: deque(maxlen=16))
        pending[chat_id].append(inbound_ref)
        client = _Client()
        handoff = AsyncMock(return_value=False)

        with (
            patch.object(runner, "client", client),
            patch.object(runner, "_meta", {chat_id: {
                "entity": -704, "is_dm": False, "title": "room", "size": 3,
                "room_mode": "normal", "addressed": False,
            }}),
            patch.object(runner, "_group_wakes", wakes),
            patch.object(runner, "_last_pass", defaultdict(float)),
            patch.object(runner, "_passing", set()),
            patch.object(runner, "_pending_media", pending),
            patch.object(runner, "_recent_msgs", defaultdict(lambda: deque(maxlen=12))),
            patch.object(runner, "_missed", {}),
            patch.object(runner, "_cooldown", return_value=0.0),
            patch.object(runner, "_maybe_compact", side_effect=_no_compact),
            patch.object(runner, "_queue_and_send_media", handoff),
            patch.object(agent, "voice_turn_envelope",
                         return_value=media.TurnEnvelope(text="ответ", outbound=(outbound,))),
        ):
            await runner._run_pass(chat_id)
            await asyncio.sleep(0)

        self.assertEqual(client.sent, [(-704, "ответ", {"reply_to": 40})])
        handoff.assert_awaited_once()
        self.assertNotIn(chat_id, wakes)
        self.assertNotIn(chat_id, pending)

    async def test_long_reply_is_chunked_and_only_first_chunk_is_telegram_reply(self):
        chat_id = "-705"
        reply = "🙂" * 2001
        chunks = runner._split_telegram_text(reply)
        wake = _wake(mid=50, ts=990.0, context="Алиса: длинно")
        wakes = {chat_id: wake}
        client = _Client()
        buf_push = Mock()
        arm = Mock()

        with (
            patch.object(runner, "client", client),
            patch.object(runner, "_meta", {chat_id: {
                "entity": -705, "is_dm": False, "title": "room", "size": 3,
                "room_mode": "normal", "addressed": False,
            }}),
            patch.object(runner, "_group_wakes", wakes),
            patch.object(runner, "_last_pass", defaultdict(float)),
            patch.object(runner, "_passing", set()),
            patch.object(runner, "_pending_media", defaultdict(lambda: deque(maxlen=16))),
            patch.object(runner, "_recent_msgs", defaultdict(lambda: deque(maxlen=12))),
            patch.object(runner, "_missed", {}),
            patch.object(runner, "_cooldown", return_value=0.0),
            patch.object(runner, "_buf_push", buf_push),
            patch.object(runner, "_maybe_compact", side_effect=_no_compact),
            patch.object(runner, "_arm", arm),
            patch.object(agent, "voice_turn_envelope",
                         return_value=media.TurnEnvelope(text=reply)),
        ):
            await runner._run_pass(chat_id)
            await asyncio.sleep(0)

        self.assertEqual([text for _entity, text, _kw in client.sent], list(chunks))
        self.assertEqual(client.sent[0][2], {"reply_to": 50})
        self.assertTrue(all(not kw for _entity, _text, kw in client.sent[1:]))
        buf_push.assert_called_once_with(
            chat_id, f"Praxis: {reply}", author="Praxis", is_dm=False,
            source_id="1001,1002", ts=ANY)
        self.assertNotIn(chat_id, wakes)
        arm.assert_not_called()

    async def test_first_chunk_failure_retains_wake_media_and_rearms(self):
        chat_id = "-706"
        reply = "🙂" * 2001
        inbound_ref = object()
        wake = _wake(mid=60, ts=990.0, context="Алиса: повтори",
                     media_refs=(inbound_ref,))
        wakes = {chat_id: wake}
        pending = defaultdict(lambda: deque(maxlen=16))
        pending[chat_id].append(inbound_ref)
        buf_push = Mock()
        arm = Mock()

        class FirstFailClient(_Client):
            def __init__(self):
                super().__init__()
                self.attempts = []

            async def send_message(self, entity, text, **kwargs):
                self.attempts.append((entity, text, kwargs))
                raise RuntimeError("first chunk failed")

        client = FirstFailClient()
        with (
            patch.object(runner, "client", client),
            patch.object(runner, "_meta", {chat_id: {
                "entity": -706, "is_dm": False, "title": "room", "size": 3,
                "room_mode": "normal", "addressed": False,
            }}),
            patch.object(runner, "_group_wakes", wakes),
            patch.object(runner, "_last_pass", defaultdict(float)),
            patch.object(runner, "_passing", set()),
            patch.object(runner, "_pending_media", pending),
            patch.object(runner, "_recent_msgs", defaultdict(lambda: deque(maxlen=12))),
            patch.object(runner, "_missed", {}),
            patch.object(runner, "_cooldown", return_value=0.0),
            patch.object(runner, "_buf_push", buf_push),
            patch.object(runner, "_maybe_compact", side_effect=_no_compact),
            patch.object(runner, "_arm", arm),
            patch.object(agent, "voice_turn_envelope",
                         return_value=media.TurnEnvelope(text=reply)),
        ):
            await runner._run_pass(chat_id)
            await asyncio.sleep(0)

        self.assertEqual(len(client.attempts), 1)
        self.assertEqual(client.attempts[0][2], {"reply_to": 60})
        self.assertIs(wakes[chat_id], wake)
        self.assertEqual(tuple(pending[chat_id]), (inbound_ref,))
        buf_push.assert_not_called()
        arm.assert_called_once_with(chat_id)

    async def test_later_chunk_failure_records_delivered_prefix_and_is_terminal(self):
        chat_id = "-707"
        reply = "🙂" * 2001
        chunks = runner._split_telegram_text(reply)
        inbound_ref = object()
        wake = _wake(mid=70, ts=990.0, context="Алиса: без дубля",
                     media_refs=(inbound_ref,))
        wakes = {chat_id: wake}
        pending = defaultdict(lambda: deque(maxlen=16))
        pending[chat_id].append(inbound_ref)
        buf_push = Mock()
        arm = Mock()
        voice = Mock(return_value=media.TurnEnvelope(text=reply))

        class SecondFailClient(_Client):
            def __init__(self):
                super().__init__()
                self.attempts = []

            async def send_message(self, entity, text, **kwargs):
                self.attempts.append((entity, text, kwargs))
                if len(self.attempts) == 2:
                    raise RuntimeError("second chunk failed")
                self.sent.append((entity, text, kwargs))

        client = SecondFailClient()
        with (
            patch.object(runner, "client", client),
            patch.object(runner, "_meta", {chat_id: {
                "entity": -707, "is_dm": False, "title": "room", "size": 3,
                "room_mode": "normal", "addressed": False,
            }}),
            patch.object(runner, "_group_wakes", wakes),
            patch.object(runner, "_last_pass", defaultdict(float)),
            patch.object(runner, "_passing", set()),
            patch.object(runner, "_pending_media", pending),
            patch.object(runner, "_recent_msgs", defaultdict(lambda: deque(maxlen=12))),
            patch.object(runner, "_missed", {}),
            patch.object(runner, "_cooldown", return_value=0.0),
            patch.object(runner, "_buf_push", buf_push),
            patch.object(runner, "_maybe_compact", side_effect=_no_compact),
            patch.object(runner, "_arm", arm),
            patch.object(agent, "voice_turn_envelope", side_effect=voice),
        ):
            await runner._run_pass(chat_id)
            await runner._run_pass(chat_id)  # stale retry timer/call must be a no-op
            await asyncio.sleep(0)

        self.assertEqual(len(client.attempts), 2)
        self.assertEqual(client.attempts[0][2], {"reply_to": 70})
        self.assertEqual(client.attempts[1][2], {})
        self.assertEqual(client.sent, [(-707, chunks[0], {"reply_to": 70})])
        buf_push.assert_called_once_with(
            chat_id, f"Praxis: {chunks[0]}", author="Praxis", is_dm=False)
        self.assertNotIn(chat_id, wakes)
        self.assertNotIn(chat_id, pending)
        arm.assert_not_called()
        self.assertEqual(voice.call_count, 1)

    async def test_dm_keeps_live_context_path_without_group_wake(self):
        chat_id = "42"
        client = _Client()
        captured = {}

        def voice(*args, **kwargs):
            captured.update(args=args, kwargs=kwargs)
            return media.TurnEnvelope()

        with (
            patch.object(runner, "client", client),
            patch.object(runner, "_meta", {chat_id: {
                "entity": 42, "is_dm": True, "is_owner": True, "known": True,
                "family": False, "name": "Егор", "title": None, "size": None,
                "addressed": False, "room_mode": "normal",
            }}),
            patch.object(runner, "_group_wakes", {}),
            patch.object(runner, "_last_pass", defaultdict(float)),
            patch.object(runner, "_passing", set()),
            patch.object(runner, "_pending_media", defaultdict(lambda: deque(maxlen=16))),
            patch.object(runner, "_recent_msgs", defaultdict(lambda: deque(maxlen=12))),
            patch.object(runner, "_missed", {}),
            patch.object(runner, "_cooldown", return_value=0.0),
            patch.object(runner, "_last_n_text", AsyncMock(return_value="Егор: свежая личка")),
            patch.object(runner, "_maybe_compact", side_effect=_no_compact),
            patch.object(agent, "voice_turn_envelope", side_effect=voice),
        ):
            await runner._run_pass(chat_id)
            await asyncio.sleep(0)

        self.assertEqual(captured["args"][:3], (chat_id, "Егор: свежая личка", "Егор"))
        self.assertTrue(captured["kwargs"]["ctx"].is_dm)


class _Message:
    def __init__(self, *, mid, text, mentioned=False, reply_to=None, reply_sender_id=None):
        self.id = mid
        self.message = text
        self.mentioned = mentioned
        self.is_reply = reply_to is not None
        self.reply_to_msg_id = reply_to
        self._reply = (types.SimpleNamespace(
            sender_id=reply_sender_id, message="граница") if reply_to is not None else None)
        self.date = datetime.datetime.fromtimestamp(1000 + mid, datetime.timezone.utc)

    async def get_reply_message(self):
        return self._reply


class _Event:
    def __init__(self, *, mid, text, name, sender_id, mentioned=False,
                 reply_to=None, reply_sender_id=None, username=None, usernames=None):
        self.message = _Message(
            mid=mid, text=text, mentioned=mentioned,
            reply_to=reply_to, reply_sender_id=reply_sender_id)
        self.chat_id = -800
        self.is_private = False
        self.sender_id = sender_id
        self._sender = types.SimpleNamespace(
            id=sender_id, first_name=name, last_name=None, username=username,
            usernames=usernames or (), is_self=False)

    async def get_sender(self):
        return self._sender


class TestWakeCapture(unittest.IsolatedAsyncioTestCase):
    async def test_background_cannot_mutate_wake_and_new_address_reanchors(self):
        chat_id = "-800"
        buf = defaultdict(lambda: deque(maxlen=runner.BUF_MAXLEN))
        meta = {}
        wakes = {}
        recent = defaultdict(lambda: deque(maxlen=12))
        arm = Mock()

        async def deliver(mid, text, name, sender_id, mentioned=False,
                          reply_to=None, reply_sender_id=None, username=None, usernames=None):
            await runner.on_new(_Event(
                mid=mid, text=text, name=name, sender_id=sender_id, mentioned=mentioned,
                reply_to=reply_to, reply_sender_id=reply_sender_id,
                username=username, usernames=usernames))

        with (
            patch.object(runner, "OWNER_ID", 101),
            patch.object(runner, "_buf", buf),
            patch.object(runner, "_buf_dirty", set()),
            patch.object(runner, "_meta", meta),
            patch.object(runner, "_group_wakes", wakes),
            patch.object(runner, "_pending_media", defaultdict(lambda: deque(maxlen=16))),
            patch.object(runner, "_recent_msgs", recent),
            patch.object(runner, "_recent_senders", defaultdict(lambda: deque(maxlen=40))),
            patch.object(runner, "_seen_ids", defaultdict(lambda: deque(maxlen=100))),
            patch.object(runner, "_capture_typed_media", AsyncMock(return_value=(None, None))),
            patch.object(runner, "_chat_descriptor",
                         AsyncMock(return_value={"title": "room", "size": 4})),
            patch.object(runner, "_arm", arm),
            patch.object(runner.bufstore, "meta_update", return_value=None),
            patch.object(runner.rooms, "is_frozen", return_value=False),
            patch.object(runner.rooms, "is_allowed", return_value=True),
            patch.object(runner.rooms, "effective_mode", return_value="normal"),
            patch.object(runner.social, "category", return_value="known"),
            patch.object(runner.reflex, "triage", return_value="reply"),
        ):
            await deliver(10, "@praxis первый вопрос", "Алиса", 101, mentioned=True,
                          usernames=[types.SimpleNamespace(username="alice_live", active=True)])
            first = wakes[chat_id]
            self.assertTrue(first.owner)
            self.assertEqual(first.message_id, 10)
            self.assertIn("Алиса (@alice_live): @praxis первый вопрос", first.context_snapshot)

            await deliver(11, "совсем другая тема", "Боб", 202)
            self.assertIs(wakes[chat_id], first)
            self.assertNotIn("другая тема", first.context_snapshot)
            self.assertEqual(arm.call_count, 1, "фон не перевзводит адресный debounce")

            await deliver(12, "@praxis второй вопрос", "Вера", 303, mentioned=True)
            second = wakes[chat_id]
            self.assertIsNot(second, first)
            self.assertEqual(second.message_id, 12)
            self.assertIn("совсем другая тема", second.context_snapshot)
            self.assertIn("Вера", second.context_snapshot)
            self.assertIn("@praxis второй вопрос", second.context_snapshot)
            self.assertEqual(tuple(mid for mid, _author, _gist in second.reply_targets_snapshot),
                             (10, 11, 12))

            await deliver(13, "поздний фон", "Глеб", 404)
            self.assertIs(wakes[chat_id], second)
            self.assertNotIn("поздний фон", second.context_snapshot)
            self.assertEqual(arm.call_count, 2)

    async def test_boundary_provenance_is_consumed_without_suppressing_voice(self):
        chat_id = "-800"
        buf = defaultdict(lambda: deque(maxlen=runner.BUF_MAXLEN))
        wakes = {}
        arm = Mock()
        boundary = defaultdict(lambda: deque(maxlen=8))
        boundary[chat_id].append((700, 202, time.time()))

        async def deliver(mid, sender_id):
            await runner.on_new(_Event(
                mid=mid, text="ну и что", name="Боб", sender_id=sender_id,
                reply_to=700, reply_sender_id=runner._self_id))

        with (
            patch.object(runner, "OWNER_ID", 101),
            patch.object(runner, "_self_id", 999),
            patch.object(runner, "_buf", buf),
            patch.object(runner, "_buf_dirty", set()),
            patch.object(runner, "_meta", {}),
            patch.object(runner, "_group_wakes", wakes),
            patch.object(runner, "_boundary_replies", boundary),
            patch.object(runner, "_pending_media", defaultdict(lambda: deque(maxlen=16))),
            patch.object(runner, "_recent_msgs", defaultdict(lambda: deque(maxlen=12))),
            patch.object(runner, "_recent_senders", defaultdict(lambda: deque(maxlen=40))),
            patch.object(runner, "_seen_ids", defaultdict(lambda: deque(maxlen=100))),
            patch.object(runner, "_capture_typed_media", AsyncMock(return_value=(None, None))),
            patch.object(runner, "_chat_descriptor",
                         AsyncMock(return_value={"title": "room", "size": 4})),
            patch.object(runner, "_arm", arm),
            patch.object(runner.bufstore, "meta_update", return_value=None),
            patch.object(runner.rooms, "is_frozen", return_value=False),
            patch.object(runner.rooms, "is_allowed", return_value=True),
            patch.object(runner.rooms, "effective_mode", return_value="normal"),
            patch.object(runner.social, "category", return_value="known"),
            patch.object(runner.reflex, "triage", return_value="reply"),
        ):
            await deliver(20, 202)
            self.assertEqual(arm.call_count, 1,
                             "boundary provenance cannot suppress a normal addressed pass")
            self.assertNotIn(chat_id, boundary)

            await deliver(21, 202)
            self.assertEqual(arm.call_count, 2, "граница одноразовая, вечного мута нет")

            boundary[chat_id].append((700, 202, time.time()))
            await deliver(22, 303)
            self.assertEqual(arm.call_count, 3, "другой участник не наследует чужую provenance")


class TestAddressFrame(unittest.TestCase):
    def test_age_and_frozen_boundary_are_visible_to_voice(self):
        ctx = agent.ChannelContext(
            chat_id="-900", is_dm=False, addressed=True,
            address_message_id=77, address_kind="mention", address_age_sec=180.2)
        with patch.object(agent.notes, "read", return_value=""):
            frame = agent._presence_frame(ctx)
        self.assertIn("message #77", frame)
        self.assertIn("mention", frame)
        self.assertIn("180 seconds ago", frame)
        self.assertIn("stop at that address", frame)


if __name__ == "__main__":
    unittest.main(verbosity=2)
