from __future__ import annotations

import datetime
import contextlib
import json
import os
import sys
import tempfile
import types
import unittest
from collections import defaultdict, deque
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("PRAXIS_TEST", "1")
os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test")
os.environ.setdefault("TELEGRAM_SESSION", ":memory:")

import agent
import group_context
import memory_fts
import rooms
import telegram_topics


class _ImportClient:
    def __init__(self, *args, **kwargs):
        pass

    def on(self, *args, **kwargs):
        return lambda fn: fn


class _ImportEvents:
    @staticmethod
    def NewMessage(*args, **kwargs):
        return object()

    @staticmethod
    def MessageEdited(*args, **kwargs):
        return object()

    @staticmethod
    def ChatAction(*args, **kwargs):
        return object()


if "mtproto_runner" not in sys.modules:
    prior = sys.modules.get("telethon")
    fake = types.ModuleType("telethon")
    fake.TelegramClient = _ImportClient
    fake.events = _ImportEvents
    sys.modules["telethon"] = fake
    try:
        import mtproto_runner as runner
    finally:
        if prior is None:
            sys.modules.pop("telethon", None)
        else:
            sys.modules["telethon"] = prior
else:
    import mtproto_runner as runner


class TempGroupMemory(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.memory = self.base / "memory"
        self.groups = self.memory / "groups"
        self.state = self.memory / ".state" / "group_context"
        self.patchers = [
            patch.object(group_context, "BASE", self.base),
            patch.object(group_context, "MEM_DIR", self.memory),
            patch.object(group_context, "GROUPS_DIR", self.groups),
            patch.object(group_context, "STATE_DIR", self.state),
        ]
        for item in self.patchers:
            item.start()
        group_context._KEY_CACHE.clear()

    def tearDown(self):
        group_context._KEY_CACHE.clear()
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    def add(self, peer, topic, mid, text, sender=10, name="Alice", reply=None,
            title=""):
        return group_context.observe_message(
            peer_id=peer, topic_id=topic, message_id=mid,
            sender_id=sender, sender_name=name, reply_to_message_id=reply,
            timestamp=f"2026-07-14T12:{mid:02d}:00Z", text=text,
            topic_title=title,
        )


class TestBranchIsTheReplyChain(TempGroupMemory):
    """Ветка — это реплай-цепочка, а не колонка topic_id.

    Регрессия 24.07.2026: вне форума Telegram не ставит reply_to_top_id на первый
    ответ, поэтому корень цепочки и первый ответ на него архивировались с
    topic_id=None, а всё остальное — под topic_id=<корень>. Фильтр по колонке
    выбрасывал ровно те два сообщения, о которых ветка и была, причём срез выглядел
    цельным. Живая улика: «у меня в топик попал только твой ответ, без сообщения
    #93708» — Арету пришлось дублировать выпавшее руками.
    """

    def test_chain_root_and_its_first_reply_are_part_of_the_branch(self):
        # Как это лежит в живом архиве.
        self.add("-1001", None, 7, "открывай своё репо")                      # корень
        self.add("-1001", None, 8, "имя репы было другое", sender=20,
                 name="Arete", reply=7)                                       # 1-й ответ
        self.add("-1001", 7, 10, "жду твой ход", reply=7)                     # 2-й уровень
        self.add("-1001", 99, 11, "совсем другая ветка", sender=30, name="Vadim")

        seen = group_context.context("-1001", topic_id=7, limit=20)
        self.assertIn("жду твой ход", seen)
        self.assertIn("открывай своё репо", seen)      # корень — предмет разговора
        self.assertIn("имя репы было другое", seen)    # выпадавший первый ответ
        self.assertNotIn("совсем другая ветка", seen)  # чужая ветка не втянута

    def test_bare_peer_branch_is_not_polluted_by_numbered_branches(self):
        """topic_id=None — такая же ветка, и она обязана остаться собой."""

        self.add("-1001", None, 1, "разговор на корневом уровне")
        for mid in range(20, 40):
            self.add("-1001", 99, mid, f"шум чужой ветки {mid}", sender=30, name="Noise")

        seen = group_context.context("-1001", topic_id=None, limit=50)
        self.assertIn("разговор на корневом уровне", seen)
        self.assertNotIn("шум чужой ветки", seen)

    def test_forum_topic_isolation_is_unchanged(self):
        """В настоящем форуме корень — служебный опенер, его прямые ответы и так в
        топике: множество то же самое, контракт изоляции держится."""

        self.add("-1001", 11, 1, "alpha", title="Ideas")
        self.add("-1001", 22, 2, "beta", sender=20, name="Bob", title="Rituals")
        self.add("-1001", 22, 3, "beta-2", sender=20, name="Bob", reply=2, title="Rituals")
        narrow = group_context.context("-1001", topic_id=11, limit=20)
        self.assertIn("alpha", narrow)
        self.assertNotIn("beta", narrow)

    def test_the_root_is_the_subject_and_survives_the_budget(self):
        """Корень — самое старое сообщение цепочки, значит обрез «от новых к старым»
        выбрасывает его первым. Живой случай: ветка #93759 на 33 реплики упиралась
        в бюджет и теряла собственную тему."""

        self.add("-1001", None, 7, "ТЕМА ВЕТКИ: почему у тебя закрытое репо")
        for mid in range(20, 70):
            self.add("-1001", 7, mid, "обсуждение " + "я" * 400, reply=7,
                     sender=30, name="Talker")

        seen = group_context.context("-1001", topic_id=7, limit=200, max_chars=3000)
        self.assertIn("ТЕМА ВЕТКИ", seen)
        self.assertIn("обсуждение", seen)                 # хвост тоже на месте
        self.assertLessEqual(len(seen), 3200)             # бюджет соблюдён
        self.assertEqual(seen.count("ТЕМА ВЕТКИ"), 1)     # и не задвоена

    def test_an_old_edit_cannot_evict_todays_conversation(self):
        """Порядок — по времени, когда сказано. Иначе правка недельной давности
        переезжает в конец ленты и вытесняет сегодняшний разговор."""

        for mid in range(1, 6):
            group_context.observe_message(
                peer_id="-1001", topic_id=7, message_id=mid, sender_id=10,
                sender_name="Alice", reply_to_message_id=7,
                timestamp="2026-07-24T12:00:00Z", text=f"сегодняшний разговор {mid}")
        for mid in range(50, 55):
            group_context.observe_message(
                peer_id="-1001", topic_id=7, message_id=mid, sender_id=20,
                sender_name="Bob", reply_to_message_id=7,
                timestamp="2026-07-17T08:00:00Z", edited_at="2026-07-24T13:00:00Z",
                text=f"прошлонедельное, поправлено сейчас {mid}")

        seen = group_context.context("-1001", topic_id=7, limit=5)
        self.assertIn("сегодняшний разговор", seen)



class TestCanonicalArchive(TempGroupMemory):
    def test_exact_topic_dedup_map_and_search_are_root_bounded(self):
        self.assertTrue(self.add("-1001", 11, 1, "alpha mushrooms", title="Ideas"))
        self.assertTrue(self.add("-1001", 22, 1, "beta ritual", sender=20, name="Bob",
                                 title="Rituals"))
        self.assertFalse(self.add("-1001", 11, 1, "changed"))
        self.assertTrue(self.add("-1002", 11, 1, "private neighbouring alpha"))

        first = group_context.context("-1001", topic_id=11, limit=20)
        second = group_context.context("-1001", topic_id=22, limit=20)
        self.assertIn("alpha mushrooms", first)
        self.assertNotIn("beta ritual", first)
        self.assertIn("beta ritual", second)
        self.assertEqual(group_context.search("-1001", "neighbouring"), [])

        mapped = group_context.projection("-1001")
        self.assertEqual(mapped["message_count"], 2)
        self.assertEqual(set(mapped["topics"]), {"11", "22"})
        self.assertEqual(len(mapped["participants"]), 2)
        self.assertTrue(
            group_context.projection_markdown_path("-1001").read_text(encoding="utf-8")
            .startswith("<!-- praxis-generated:"))
        sources = memory_fts.iter_sources(
            base=self.base, memory_dir=self.memory, skills_dir=None)
        paths = {item.path: item.kind for item in sources}
        self.assertEqual(paths[group_context.archive_path("-1001")], "group_message")
        self.assertNotIn(group_context.projection_markdown_path("-1001"), paths)

    def test_message_edit_is_an_idempotent_append_only_revision(self):
        self.assertTrue(self.add("-1001", 11, 7, "original"))
        self.assertFalse(self.add("-1001", 11, 7, "original"))
        arguments = {
            "peer_id": "-1001", "topic_id": 11, "message_id": 7,
            "sender_id": 10, "sender_name": "Alice",
            "reply_to_message_id": None,
            "timestamp": "2026-07-14T12:07:00Z",
            "edited_at": "2026-07-14T12:09:00Z",
            "text": "corrected",
        }
        self.assertTrue(group_context.observe_message(**arguments))
        self.assertFalse(group_context.observe_message(**arguments))
        same_second = {**arguments, "text": "final same-second correction"}
        self.assertTrue(group_context.observe_message(**same_second))
        self.assertFalse(group_context.observe_message(**same_second))

        rows = [row for row in group_context.iter_records("-1001", max_records=None)
                if row.get("kind") == "message"]
        self.assertEqual(len(rows), 3)
        self.assertIsNone(rows[0].get("edited_at"))
        self.assertEqual(rows[1]["edited_at"], "2026-07-14T12:09:00Z")
        rendered = group_context.context("-1001", topic_id=11, limit=10)
        self.assertNotIn("original", rendered)
        self.assertNotIn("corrected", rendered)
        self.assertIn("final same-second correction", rendered)
        self.assertIn("edited=2026-07-14T12:09:00Z", rendered)
        mapped = group_context.projection("-1001")
        self.assertEqual(mapped["message_count"], 1)
        self.assertEqual(mapped["revision_count"], 2)
        self.assertEqual(group_context.search("-1001", "original"), [])
        self.assertEqual(len(group_context.search("-1001", "final")), 1)

    def test_torn_tail_is_isolated_before_next_append(self):
        self.add("-1001", 11, 1, "first")
        path = group_context.archive_path("-1001")
        with path.open("ab") as stream:
            stream.write(b'{"broken":')
        group_context._KEY_CACHE.clear()
        self.assertTrue(self.add("-1001", 11, 2, "second"))
        text = group_context.context("-1001", topic_id=11, limit=20)
        self.assertIn("first", text)
        self.assertIn("second", text)

    def test_structurally_invalid_json_row_cannot_break_projection(self):
        self.add("-1001", 11, 1, "valid")
        invalid = {
            "schema": group_context.SCHEMA_MESSAGE,
            "kind": "message", "peer_id": "-1001", "topic_id": "not-an-id",
            "message_id": "also-bad", "sender_id": None, "sender_name": "Mallory",
            "reply_to_message_id": None, "timestamp": "bad", "edited_at": None,
            "text": "must be ignored", "media": "", "outgoing": False,
        }
        with group_context.archive_path("-1001").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(invalid) + "\n")
        group_context._KEY_CACHE.clear()
        mapped = group_context.rebuild_projection("-1001")
        self.assertEqual(mapped["message_count"], 1)
        self.assertNotIn("must be ignored", group_context.context("-1001", topic_id=11))

    def test_cross_topic_bundle_is_map_plus_marked_matches(self):
        self.add("-1001", 11, 1, "current seed", title="Current")
        self.add("-1001", 22, 2, "shared mycelium idea", title="Elsewhere")
        value = group_context.orientation_bundle(
            "-1001", current_topic=11, query="mycelium", cross_topics="map")
        self.assertIn("aggregate only", value)
        self.assertIn("[CROSS-TOPIC EXCERPT]", value)
        self.assertIn("topic #22", value)

    def test_generated_group_map_is_bounded_and_rebuild_stable(self):
        with patch.object(group_context, "MAX_MAP_TOPICS", 3), \
             patch.object(group_context, "MAX_MAP_PARTICIPANTS", 2), \
             patch.object(group_context, "MAX_TOPIC_PARTICIPANTS", 2), \
             patch.object(group_context, "MAX_PARTICIPANT_TOPICS", 2):
            for index in range(5):
                group_context.observe_message(
                    peer_id="-1001", topic_id=100 + index, message_id=index + 1,
                    sender_id=1000 + index, sender_name=f"Person {index}",
                    reply_to_message_id=None,
                    timestamp=datetime.datetime(2026, 7, 14, 12, index,
                                                tzinfo=datetime.timezone.utc),
                    text=f"bounded row {index}", topic_title=f"Topic {index}",
                )
            first = group_context.rebuild_projection("-1001")
            first_md = group_context.projection_markdown_path("-1001").read_bytes()
            second = group_context.rebuild_projection("-1001")

        self.assertEqual(first, second)
        self.assertEqual(first_md, group_context.projection_markdown_path("-1001").read_bytes())
        self.assertEqual(first["topic_count"], 5)
        self.assertEqual(first["participant_count"], 5)
        self.assertEqual(len(first["topics"]), 3)
        self.assertEqual(len(first["participants"]), 2)
        self.assertEqual(len(list(group_context.iter_records("-1001", max_records=None))), 5)


class TestRoomPolicy(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.memory = self.base / "memory"
        self.room_dir = self.memory / "rooms"
        self.patchers = [
            patch.object(rooms, "BASE", self.base),
            patch.object(rooms, "MEM_DIR", self.memory),
            patch.object(rooms, "ROOMS_DIR", self.room_dir),
            patch.object(rooms, "ALLOWLIST", self.memory / "rooms_allowlist.json"),
            patch.object(rooms, "FROZEN", self.memory / "frozen_chats.json"),
        ]
        for item in self.patchers:
            item.start()

    def tearDown(self):
        for item in reversed(self.patchers):
            item.stop()
        self.temp.cleanup()

    def test_default_is_deep_reflective_and_values_are_clamped(self):
        self.assertEqual(rooms.room_policy("-1001"), {
            "engagement": "reflective", "context_hot": 200,
            "context_summary_chars": 24000, "cross_topics": "map",
            "backfill_limit": 1500,
        })
        rooms.profile_update(
            "-1001", engagement="reflective", context_hot=999,
            context_summary_chars=999999, cross_topics="map", backfill_limit=99999,
        )
        self.assertEqual(rooms.room_policy("-1001"), {
            "engagement": "reflective", "context_hot": 500,
            "context_summary_chars": 40000, "cross_topics": "map",
            "backfill_limit": 5000,
        })
        with self.assertRaises(ValueError):
            rooms.profile_update("-1001", engagement="always")

    def test_owner_configure_uses_root_room(self):
        rooms.add_room("-1001")
        with (
            patch.object(agent, "_is_human_owner", return_value=True),
            patch.object(agent, "ROOMS_DIR", self.room_dir),
        ):
            out = agent.tool_manage_room(
                "configure", "-1001", engagement="reflective",
                context_hot=500, context_summary_chars=18000,
                cross_topics="map", backfill_limit=2000,
            )
        self.assertIn('"engagement": "reflective"', out)
        self.assertEqual(rooms.room_policy("-1001")["backfill_limit"], 2000)


class TestTopicRouting(unittest.TestCase):
    def test_topic_create_uses_own_message_id_and_title(self):
        action_type = type("MessageActionTopicCreate", (), {})
        action = action_type()
        action.title = "Introductions"
        message = types.SimpleNamespace(id=77, action=action, reply_to=None)
        route = telegram_topics.route_for_message("-1001", message)
        self.assertEqual(route.topic_id, 77)
        self.assertEqual(telegram_topics.topic_opener_title(message), "Introductions")

    def test_reflective_wake_never_replaces_an_address(self):
        addressed = runner.GroupWake(
            message_id=1, message_ts=1.0, kind="mention", speaker="A",
            sender_id=1, owner=False, known=True, family=False,
            context_snapshot="A: hi", reply_targets_snapshot=(), media_snapshot=(),
            addressed=True,
        )
        ambient = runner.GroupWake(
            message_id=2, message_ts=2.0, kind="ambient", speaker="B",
            sender_id=2, owner=False, known=True, family=False,
            context_snapshot="B: thought", reply_targets_snapshot=(), media_snapshot=(),
            addressed=False,
        )
        with patch.object(runner, "_group_wakes", {}):
            self.assertTrue(runner._install_group_wake("room", addressed))
            self.assertFalse(runner._install_group_wake("room", ambient))
            self.assertIs(runner._group_wakes["room"], addressed)
        with patch.object(runner, "_group_wakes", {}):
            self.assertTrue(runner._install_group_wake("room", ambient))
            self.assertTrue(runner._install_group_wake("room", addressed))
            self.assertIs(runner._group_wakes["room"], addressed)
        self.assertFalse(runner._should_wake(False, False, "maybe", "addressed"))
        self.assertTrue(runner._should_wake(False, False, "maybe", "reflective"))


class _LiveMessage:
    def __init__(self, mid, text, *, mentioned=False):
        self.id = mid
        self.message = text
        self.mentioned = mentioned
        self.is_reply = False
        self.reply_to = types.SimpleNamespace(
            reply_to_top_id=77, reply_to_msg_id=77, forum_topic=True)
        self.reply_to_msg_id = 77
        self.date = datetime.datetime.now(datetime.timezone.utc)
        self.action = None


class _LiveEvent:
    is_private = False

    def __init__(self, message):
        self.message = message
        self.chat_id = -1001
        self.sender_id = 10
        self.sender = types.SimpleNamespace(
            id=10, first_name="Alice", last_name="", username=None,
            usernames=(), is_self=False,
        )

    async def get_sender(self):
        return self.sender


class _EditedMessage(_LiveMessage):
    def __init__(self, mid, text):
        super().__init__(mid, text)
        self.date = datetime.datetime(
            2026, 7, 14, 12, 0, tzinfo=datetime.timezone.utc)
        self.edit_date = datetime.datetime(
            2026, 7, 14, 12, 5, tzinfo=datetime.timezone.utc)
        self.photo = object()


class TestEditedIncoming(TempGroupMemory, unittest.IsolatedAsyncioTestCase):
    async def test_admitted_edit_is_one_archive_revision_and_one_topic_life_event(self):
        event = _LiveEvent(_EditedMessage(41, "corrected caption"))
        buffers = defaultdict(lambda: deque(maxlen=600))
        life = Mock(return_value={"id": "life-edit-41"})
        meta_update = Mock()
        capture = AsyncMock()
        inbox = AsyncMock()
        followups = Mock()
        panic = Mock()
        update_self = Mock()
        manage_desire = Mock()
        arms = Mock()
        wakes = {}

        with (
            patch.object(runner.rooms, "is_frozen", return_value=False),
            patch.object(runner.rooms, "is_allowed", return_value=True),
            patch.object(runner.rooms, "effective_mode", return_value="normal"),
            patch.object(runner, "_group_archive_enabled", return_value=True),
            patch.object(runner.group_context, "topic_title", return_value="Ideas"),
            patch.object(runner, "_topic_titles", {}),
            patch.object(runner, "_buf", buffers),
            patch.object(runner, "_buf_dirty", set()),
            patch.object(runner, "_under_tests", return_value=False),
            patch.object(runner.memory_life, "record_message", life),
            patch.object(runner.bufstore, "meta_update", meta_update),
            patch.object(runner, "_capture_typed_media", capture),
            patch.object(runner, "_inbox_download", inbox),
            patch.object(runner.telegram_followups.LEDGER, "observe_incoming", followups),
            patch.object(runner.agent, "panic", panic),
            patch.object(runner.agent, "tool_update_self", update_self),
            patch.object(runner.agent, "tool_manage_desire", manage_desire),
            patch.object(runner, "_arm", arms),
            patch.object(runner, "_group_wakes", wakes),
        ):
            await runner.on_edited(event)
            await runner.on_edited(event)

        rows = [
            row for row in group_context.iter_records("-1001", max_records=None)
            if row.get("kind") == "message"
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["topic_id"], 77)
        self.assertEqual(rows[0]["message_id"], 41)
        self.assertEqual(rows[0]["sender_id"], 10)
        self.assertEqual(rows[0]["sender_name"], "Alice")
        self.assertEqual(rows[0]["reply_to_message_id"], 77)
        self.assertEqual(rows[0]["timestamp"], "2026-07-14T12:00:00Z")
        self.assertEqual(rows[0]["edited_at"], "2026-07-14T12:05:00Z")
        self.assertEqual(rows[0]["text"], "corrected caption")
        self.assertTrue(rows[0]["media"])

        topic_lines = list(buffers["-1001__topic__77"])
        self.assertEqual(len(topic_lines), 1)
        self.assertIn("edited #41", topic_lines[0])
        self.assertIn("2026-07-14T12:05:00Z", topic_lines[0])
        life.assert_called_once()
        source_id = runner._edit_revision_source_id(
            41,
            datetime.datetime(2026, 7, 14, 12, 5, tzinfo=datetime.timezone.utc),
            text="corrected caption", media="[Изображение]",
        )
        self.assertEqual(
            life.call_args.kwargs["source_id"],
            source_id,
        )
        self.assertEqual(
            life.call_args.kwargs["dedupe_key"],
            f"telegram:-1001__topic__77:{source_id}:in",
        )
        meta_update.assert_called_once()
        capture.assert_not_awaited()
        inbox.assert_not_awaited()
        followups.assert_not_called()
        panic.assert_not_called()
        update_self.assert_not_called()
        manage_desire.assert_not_called()
        arms.assert_not_called()
        self.assertEqual(wakes, {})

    async def test_two_payloads_with_same_edit_second_are_distinct_revisions(self):
        first = _LiveEvent(_EditedMessage(51, "first correction"))
        second = _LiveEvent(_EditedMessage(51, "second correction"))
        buffers = defaultdict(lambda: deque(maxlen=600))
        life = Mock(return_value={"id": "life-edit-51"})

        with (
            patch.object(runner.rooms, "is_frozen", return_value=False),
            patch.object(runner.rooms, "is_allowed", return_value=True),
            patch.object(runner.rooms, "effective_mode", return_value="normal"),
            patch.object(runner, "_group_archive_enabled", return_value=True),
            patch.object(runner.group_context, "topic_title", return_value="Ideas"),
            patch.object(runner, "_topic_titles", {}),
            patch.object(runner, "_buf", buffers),
            patch.object(runner, "_buf_dirty", set()),
            patch.object(runner, "_under_tests", return_value=False),
            patch.object(runner.memory_life, "record_message", life),
            patch.object(runner.bufstore, "meta_update"),
            patch.object(runner, "_group_wakes", {}),
        ):
            await runner.on_edited(first)
            await runner.on_edited(first)  # replay of one payload stays idempotent
            await runner.on_edited(second)
            await runner.on_edited(second)

        rows = [
            row for row in group_context.iter_records("-1001", max_records=None)
            if row.get("kind") == "message" and row.get("message_id") == 51
        ]
        self.assertEqual([row["text"] for row in rows], [
            "first correction", "second correction",
        ])
        self.assertEqual(len(buffers["-1001__topic__77"]), 2)
        self.assertEqual(life.call_count, 2)
        source_ids = [call.kwargs["source_id"] for call in life.call_args_list]
        self.assertEqual(len(set(source_ids)), 2)
        self.assertTrue(all(":edit:2026-07-14T12:05:00Z:" in value
                            for value in source_ids))

    async def test_edit_obeys_frozen_allowlist_and_dead_room_gates(self):
        event = _LiveEvent(_EditedMessage(42, "must stay outside"))
        cases = (
            (True, True, "normal"),
            (False, False, "normal"),
            (False, True, "dead"),
        )
        for frozen, allowed, mode in cases:
            archive = Mock(return_value=True)
            push = Mock()
            with self.subTest(frozen=frozen, allowed=allowed, mode=mode):
                with (
                    patch.object(runner.rooms, "is_frozen", return_value=frozen),
                    patch.object(runner.rooms, "is_allowed", return_value=allowed),
                    patch.object(runner.rooms, "effective_mode", return_value=mode),
                    patch.object(runner, "_group_archive_enabled", return_value=True),
                    patch.object(runner.group_context, "observe_message", archive),
                    patch.object(runner, "_buf_push", push),
                ):
                    await runner.on_edited(event)
            archive.assert_not_called()
            push.assert_not_called()


class TestReflectiveIncoming(unittest.IsolatedAsyncioTestCase):
    async def test_ambient_batches_but_address_wins_and_archive_sees_all(self):
        buffers = defaultdict(lambda: deque(maxlen=600))
        wakes = {}
        arms = Mock()
        archive = Mock(return_value=True)

        def push(chat_id, line, **_kwargs):
            buffers[chat_id].append(line)

        common = (
            patch.object(runner.rooms, "is_frozen", return_value=False),
            patch.object(runner.rooms, "is_allowed", return_value=True),
            patch.object(runner.rooms, "effective_mode", return_value="normal"),
            patch.object(runner.rooms, "room_policy", return_value={
                "engagement": "reflective", "context_hot": 0,
                "context_summary_chars": 7000, "cross_topics": "off",
                "backfill_limit": 0,
            }),
            patch.object(runner.social, "category", return_value="known"),
            patch.object(runner.telegram_contacts, "observe"),
            patch.object(runner.telegram_followups.LEDGER, "observe_incoming", return_value=None),
            patch.object(runner, "_chat_descriptor", AsyncMock(return_value={
                "title": "Mycelium", "size": 100,
            })),
            patch.object(runner, "_capture_typed_media", AsyncMock(return_value=(None, ""))),
            patch.object(runner, "_group_archive_enabled", return_value=True),
            patch.object(runner.group_context, "topic_title", return_value="Ideas"),
            patch.object(runner.group_context, "observe_message", archive),
            patch.object(runner, "_buf_push", side_effect=push),
            patch.object(runner, "_buf", buffers),
            patch.object(runner, "_pending_media", defaultdict(lambda: deque(maxlen=16))),
            patch.object(runner, "_seen_ids", defaultdict(lambda: deque(maxlen=50))),
            patch.object(runner, "_recent_msgs", defaultdict(lambda: deque(maxlen=12))),
            patch.object(runner, "_recent_senders", defaultdict(lambda: deque(maxlen=40))),
            patch.object(runner, "_meta", {}),
            patch.object(runner, "_group_wakes", wakes),
            patch.object(runner, "_entity_cache", {}),
            patch.object(runner, "_arm", arms),
        )
        with contextlib.ExitStack() as stack:
            for item in common:
                stack.enter_context(item)
            await runner.on_new(_LiveEvent(_LiveMessage(1, "a meaningful ambient thought")))
            key = "-1001__topic__77"
            self.assertFalse(wakes[key].addressed)
            await runner.on_new(_LiveEvent(_LiveMessage(
                2, "@praxis please consider this", mentioned=True)))
            self.assertTrue(wakes[key].addressed)
            addressed = wakes[key]
            await runner.on_new(_LiveEvent(_LiveMessage(3, "another ambient thought")))

        self.assertIs(wakes[key], addressed)
        self.assertEqual(archive.call_count, 3)
        self.assertEqual(arms.call_count, 2)


class TestRootProfilePrompt(unittest.TestCase):
    def test_topic_summary_stays_local_but_room_profile_comes_from_root(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            room_dir = base / "rooms"
            soul_dir = base / "soul"
            room_dir.mkdir()
            soul_dir.mkdir()
            (room_dir / "-1001.md").write_text(
                "# Root\n\nmode: normal\ndisclosure: standard\n\n## Norms\nROOT PROFILE\n",
                encoding="utf-8",
            )
            (room_dir / "-1001__topic__77.md").write_text(
                "# Wrong\n\nTOPIC PROFILE MUST NOT LOAD\n", encoding="utf-8")
            ctx = agent.ChannelContext(
                chat_id="-1001__topic__77", room_id="-1001", principal_id=10,
                is_dm=False, known=True, addressed=True,
            )
            with (
                patch.object(agent, "ROOMS_DIR", room_dir),
                patch.object(agent, "SOUL_DIR", soul_dir),
                patch.object(agent, "INDEX_MD", base / "none.md"),
                patch.object(agent, "_persona_text", return_value=""),
                patch.object(agent, "_active_desires_block", return_value=""),
                patch.object(agent, "_participant_memory_block", return_value=""),
                patch.object(agent, "_recall_block", return_value=""),
                patch.object(agent, "recent_journal", return_value=""),
                patch.object(agent, "read_summary", return_value="TOPIC SUMMARY"),
            ):
                _static, dynamic, evidence = agent._build_prompt_parts(
                    speaker="Alice", query="hello", ctx=ctx)
            self.assertNotIn("ROOT PROFILE", dynamic)
            self.assertIn("ROOT PROFILE", evidence)
            self.assertIn("TOPIC SUMMARY", evidence)
            self.assertNotIn("TOPIC PROFILE MUST NOT LOAD", evidence)


class _HistoryClient:
    def __init__(self, messages):
        self.messages = messages

    async def iter_messages(self, entity, limit):
        for item in self.messages[:limit]:
            yield item


class TestBoundedBackfill(TempGroupMemory, unittest.IsolatedAsyncioTestCase):
    async def test_resume_deduplicates_without_a_voice_turn(self):
        header = types.SimpleNamespace(
            reply_to_top_id=77, reply_to_msg_id=77, forum_topic=True)
        now = datetime.datetime.now(datetime.timezone.utc)
        messages = [
            types.SimpleNamespace(
                id=2, message="newer", reply_to=header,
                reply_to_msg_id=77, sender_id=10, sender=types.SimpleNamespace(
                    first_name="Alice", last_name="", username=None, usernames=()),
                date=now, out=False, photo=None, voice=None, video_note=None,
                sticker=None, gif=None, video=None, audio=None, document=None, media=None,
            ),
            types.SimpleNamespace(
                id=1, message="older", reply_to=header,
                reply_to_msg_id=77, sender_id=20, sender=types.SimpleNamespace(
                    first_name="Bob", last_name="", username=None, usernames=()),
                date=now, out=False, photo=None, voice=None, video_note=None,
                sticker=None, gif=None, video=None, audio=None, document=None, media=None,
            ),
        ]
        fake = _HistoryClient(messages)
        with (
            patch.object(runner, "client", fake),
            patch.object(runner, "_forum_topic_catalog", AsyncMock(return_value=[])),
            patch.object(agent, "voice_turn_envelope", Mock()) as voice,
        ):
            first = await runner._backfill_group_context("-1001", object(), limit=10)
            second = await runner._backfill_group_context("-1001", object(), limit=10)
        self.assertEqual(first["added"], 2)
        self.assertEqual(second["added"], 0)
        self.assertEqual(group_context.archived_message_count("-1001"), 2)
        voice.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class TestTruncationPromisesAreReal(TempGroupMemory):
    """Пометка обреза обещает достать сообщение целиком — обещание обязано работать.

    Иначе молчаливая ложь просто меняется на громкую: она следует совету и получает
    ровно тот же обрезок.
    """

    def test_marker_names_a_retrieval_that_actually_returns_the_full_body(self):
        long_body = "ПЕРВЫЙ вердикт " + "г" * 2600 + " ВТОРОЙ вердикт"
        self.add("-1001", 7, 10, long_body, reply=7)

        clipped = group_context.context("-1001", topic_id=7, limit=50, max_chars=1500)
        self.assertIn("ОБРЕЗАНО", clipped)
        self.assertIn('action="message"', clipped)
        self.assertIn("limit=10", clipped)          # маркер называет message_id

        full = group_context.describe("-1001", action="message", limit=10)
        self.assertIn("ПЕРВЫЙ вердикт", full)
        self.assertIn("ВТОРОЙ вердикт", full)       # то, ради чего совет и даётся
        self.assertNotIn("ОБРЕЗАНО", full)

    def test_message_action_is_honest_about_a_missing_id(self):
        self.assertIn("not in this group", group_context.describe(
            "-1001", action="message", limit=999))

    def test_feed_level_cut_is_marked_not_silent(self):
        """Обрез ленты — та же болезнь этажом выше: срез без пометки читается как
        «вот вся ветка», и она отвечает, не зная, что выше есть ещё."""

        self.add("-1001", None, 7, "тема ветки")
        for mid in range(20, 60):
            self.add("-1001", 7, mid, "реплика " + "д" * 400, reply=7)

        seen = group_context.context("-1001", topic_id=7, limit=200, max_chars=4000)
        self.assertIn("ЛЕНТА ОБРЕЗАНА", seen)
        self.assertIn("показано", seen, "маркер называет, сколько показано")
        self.assertIn("тема ветки", seen)           # тема всё равно закреплена
        self.assertLessEqual(len(seen), 4600)

    def test_wide_cap_cannot_eat_the_whole_budget(self):
        """Четыре длинных свежих реплики не имеют права выбить остальную ветку —
        включая прямой вопрос владельца."""

        self.add("-1001", None, 7, "тема ветки")
        self.add("-1001", 7, 8, "ВОПРОС ВЛАДЕЛЬЦА про gh", reply=7)
        for mid in range(20, 25):
            self.add("-1001", 7, mid, "простыня " + "е" * 3500, reply=7, sender=30,
                     name="Talker")

        seen = group_context.context("-1001", topic_id=7, limit=200, max_chars=14000)
        self.assertIn("тема ветки", seen)
        self.assertIn("ВОПРОС ВЛАДЕЛЬЦА", seen)
