"""
Слой B: там, где Telegram прямо сказал «не форум», она читает КОМНАТУ, а не ветку.

Хранение не трогается. Меняется ровно одно: сколько своей комнаты она видит,
просыпаясь в ветке. Настоящий форум остаётся разделённым — иначе мы чинили бы одно,
ломая другое.

Запуск:  python praxis_test.py test_whole_room_read -v
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import group_context
import telegram_routes as tr


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_room_"))
        self._orig = [(group_context, "BASE", group_context.BASE),
                      (group_context, "GROUPS_DIR", group_context.GROUPS_DIR),
                      (tr, "DIR", tr.DIR)]
        group_context.BASE = self.tmp
        group_context.GROUPS_DIR = self.tmp / "memory" / "groups"
        tr.DIR = self.tmp / "memory" / ".state" / "group_context"

    def tearDown(self):
        for mod, k, v in self._orig:
            setattr(mod, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _room(self, peer="-1001240718803"):
        """Комната как в жизни: три цепочки, у каждой корень + первый ответ без
        topic_id (Telegram его на первый ответ не ставит) и продолжения под корнем."""
        n = 0
        for root in (100, 200, 300):
            group_context.observe_message(
                peer_id=peer, topic_id=None, message_id=root, sender_id=1,
                sender_name="Николай", reply_to_message_id=None,
                timestamp=None, edited_at=None, text=f"зачин {root}",
                topic_title="", media="", outgoing=False)
            group_context.observe_message(
                peer_id=peer, topic_id=None, message_id=root + 1, sender_id=2,
                sender_name="Аret", reply_to_message_id=root,
                timestamp=None, edited_at=None, text=f"первый ответ {root}",
                topic_title="", media="", outgoing=False)
            for i in range(2, 5):
                group_context.observe_message(
                    peer_id=peer, topic_id=root, message_id=root + i, sender_id=1,
                    sender_name="Николай", reply_to_message_id=root + i - 1,
                    timestamp=None, edited_at=None, text=f"продолжение {root}.{i}",
                    topic_title="", media="", outgoing=False)
                n += 1
        return peer


class TestBranchVersusRoom(Base):
    def test_branch_read_shows_only_part_of_the_room(self):
        peer = self._room()
        branch = group_context.context(peer, topic_id=100, limit=200, max_chars=40000)
        self.assertIn("зачин 100", branch)
        self.assertIn("продолжение 100.2", branch)
        self.assertNotIn("зачин 200", branch, "чужая цепочка в ветку не входит")
        self.assertNotIn("продолжение 300.4", branch)

    def test_whole_room_read_shows_the_room(self):
        peer = self._room()
        room = group_context.context(peer, topic_id=100, limit=200, max_chars=40000,
                                     whole_room=True)
        for marker in ("зачин 100", "зачин 200", "зачин 300",
                       "первый ответ 200", "продолжение 300.4"):
            self.assertIn(marker, room, marker)

    def test_whole_room_is_strictly_more_not_different(self):
        """Слой B ничего не прячет — он только перестаёт прятать."""
        peer = self._room()
        branch = group_context.context(peer, topic_id=100, limit=200, max_chars=40000)
        room = group_context.context(peer, topic_id=100, limit=200, max_chars=40000,
                                     whole_room=True)
        for line in branch.splitlines():
            if line.strip().startswith("["):
                continue      # служебные маркеры бюджета сравнивать бессмысленно
            if "зачин 100" in line or "продолжение 100" in line:
                self.assertIn(line.strip()[:40], room,
                              "то, что было видно в ветке, обязано остаться видно")


class TestItFiresOnlyOnDirectEvidence(Base):
    def test_unknown_room_behaves_exactly_as_before(self):
        peer = self._room()
        self.assertEqual(tr.status_at(peer, 100)[0], tr.UNKNOWN)
        before = group_context.context(peer, topic_id=100, limit=200, max_chars=40000)
        after = group_context.context(peer, topic_id=100, limit=200, max_chars=40000,
                                      whole_room=(tr.status_at(peer, 100)[0] == tr.FALSE))
        self.assertEqual(before, after, "без свидетельства поведение не меняется")

    def test_real_forum_stays_split(self):
        """Комната Грибницы — настоящий форум. Схлопнуть её значило бы чинить одно,
        ломая другое."""
        peer = self._room(peer="-1004301095307")
        tr.observe(peer, kind="topic_opener_seen", message_id=5)
        self.assertEqual(tr.status_at(peer, 100)[0], tr.TRUE)
        whole = tr.status_at(peer, 100)[0] == tr.FALSE
        self.assertFalse(whole, "у форума вердикт не false — ветки остаются ветками")

    def test_direct_refusal_switches_it_on(self):
        peer = self._room()
        tr.observe(peer, kind="channel_forum_missing", message_id=1)
        self.assertEqual(tr.status_at(peer, 100)[0], tr.FALSE)
        room = group_context.context(peer, topic_id=100, limit=200, max_chars=40000,
                                     whole_room=True)
        self.assertIn("зачин 300", room)


class TestTheHandDoesWhatThePromptPromises(Base):
    """Строка ориентации говорит: «ты видишь ЧАСТЬ комнаты — прочитай остальное через
    group_context». Значит рука обязана отдавать остальное, а не ту же ветку.

    Тест идёт через настоящий тул с настоящим каналом хода: проверять `describe`
    напрямую значило бы поверить тулу на слово ровно там, где он и врал.
    """

    def _tool(self, peer, current, **kw):
        import agent
        ctx = agent.ChannelContext(
            chat_id=f"{peer}__topic__{current}" if current else str(peer),
            room_id=str(peer), is_dm=False, principal_id=1)
        token = agent._TURN_CHANNEL.set(ctx)
        try:
            return agent.tool_group_context(**kw)
        finally:
            agent._TURN_CHANNEL.reset(token)

    def test_without_evidence_the_hand_returns_the_whole_room(self):
        """Недоказанное — не форум. Решение Егора 06.08, принято Праксис.

        ⚠ Тест назывался `..._stays_in_the_branch` и держал ОБРАТНУЮ политику: пока про
        комнату ничего не доказано, её рука отдавала ветку. Но форум — это то, о чём
        Telegram отвечает ПРЯМО; молчание реестра значит «мы не спрашивали». Замер 06.08:
        446 буферов, из них 424 ветки, при двух настоящих форумах на все её места.

        Сузиться до одной ветки она по-прежнему может сама — это `topic_id` и это её
        право, а не наша забывчивость (соседний тест).
        """
        peer = self._room()
        out = self._tool(peer, 100, action="context", limit=200)
        for marker in ("зачин 100", "зачин 200", "зачин 300"):
            self.assertIn(marker, out, marker)

    def test_proven_room_hand_returns_the_room(self):
        peer = self._room()
        tr.observe(peer, kind="channel_forum_missing", message_id=1)
        out = self._tool(peer, 100, action="context", limit=200)
        for marker in ("зачин 100", "зачин 200", "зачин 300"):
            self.assertIn(marker, out, marker)

    def test_she_can_still_narrow_to_one_thread(self):
        """Сузила она сама — это её право, а не наша забывчивость."""
        peer = self._room()
        tr.observe(peer, kind="channel_forum_missing", message_id=1)
        out = self._tool(peer, 100, action="context", topic_id=200, limit=200)
        self.assertIn("зачин 200", out)
        self.assertNotIn("зачин 300", out)

    def test_a_real_forum_hand_stays_in_the_topic(self):
        peer = self._room(peer="-1004301095307")
        tr.observe(peer, kind="topic_opener_seen", message_id=5)
        out = self._tool(peer, 100, action="context", limit=200)
        self.assertIn("зачин 100", out)
        self.assertNotIn("зачин 200", out, "в форуме тема — отдельное место")

    def test_the_map_calls_threads_by_their_real_name(self):
        """Имя ветки следует за тем же решением, что и чтение. Один читатель — одно слово.

        ⚠ Прежде карта до свидетельства говорила `topic #`, то есть утверждала границу,
        проведённую Telegram, не имея на то ответа Telegram. Это была та же неверная
        осторожность, что и в чтении, только в словах: `topic` — сильное утверждение, а не
        нейтральное. Класс этого файла ниже говорит прямо: читателей решения трое, и
        разойтись они могут только молча. Значит карта обязана назвать ветки цепочками
        ответов там же, где чтение отдаёт комнату.
        """
        peer = self._room()
        plain = self._tool(peer, 100, action="topics")
        self.assertIn("reply thread #", plain,
                      "карта зовёт ветку темой, пока чтение уже отдаёт комнату")
        self.assertNotIn("topic #", plain)
        tr.observe(peer, kind="channel_forum_missing", message_id=1)
        proven = self._tool(peer, 100, action="topics")
        self.assertIn("reply thread #", proven)
        self.assertNotIn("topic #", proven,
                         "карта не должна возвращать ложную модель сразу после ориентации")


class TestOneDecisionForEveryReader(Base):
    """Читателей решения «читать комнату целиком» трое: снимок пробуждения, рука и
    карта. Разойтись они могут только молча — поэтому решение одно на всех."""

    def _tool(self, peer, current, **kw):
        import agent
        ctx = agent.ChannelContext(
            chat_id=f"{peer}__topic__{current}", room_id=str(peer),
            is_dm=False, principal_id=1)
        token = agent._TURN_CHANNEL.set(ctx)
        try:
            return agent.tool_group_context(**kw)
        finally:
            agent._TURN_CHANNEL.reset(token)

    def test_all_three_agree_with_the_registry(self):
        peer = self._room()
        for kind, expected in (("channel_forum_missing", True),
                               ("topic_opener_seen", False)):
            tr.DIR = self.tmp / "memory" / ".state" / f"gc_{kind}"
            tr.observe(peer, kind=kind, since_message_id=1, until_message_id=9999)
            decision = tr.reads_whole_room(peer, 100)
            self.assertIs(decision, expected, kind)
            line = tr.orientation_line(peer, 100, "sel", tr.status_at(peer, 100)[0])
            self.assertEqual("NOT a Telegram forum topic" in line, expected,
                             "слова ориентации и решение о чтении — про один факт")
            room = self._tool(peer, 100, action="context", limit=200)
            self.assertEqual("зачин 300" in room, expected,
                             "рука обязана следовать тому же решению")
            self.assertEqual("reply thread #" in self._tool(peer, 100, action="topics"),
                             expected, "и карта — тоже")

    def test_an_unreadable_registry_degrades_to_the_old_behaviour(self):
        peer = self._room()
        tr.observe(peer, kind="channel_forum_missing", message_id=1)
        self.assertTrue(tr.reads_whole_room(peer, 100))
        tr.DIR = self.tmp / "no" / "such" / "place"
        self.assertFalse(tr.reads_whole_room(peer, 100),
                         "не знаем — ведём себя как раньше, а не как удобнее")


if __name__ == "__main__":
    unittest.main()
