"""
Место — это то, что разделил Telegram. Всё остальное разделили мы сами.

Вердикта «комната — форум» мало: в General настоящего форума живёт ровно тот же
артефакт первого ответа. Замер 25.07 на живом архиве Грибницы: 16 настоящих тем (893
сообщения) и 37 наших псевдоветок (437 сообщений) вперемешку с 631 сообщением General.
Значит границу надо знать по ветке, а не по комнате — и знать по свидетельству.

Запуск:  python praxis_test.py test_places -v
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import group_context
import telegram_routes as tr

ROOM = "-1001240718803"      # обычная супергруппа
FORUM = "-1004301095307"     # настоящий форум


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_place_"))
        self._orig = [(group_context, "BASE", group_context.BASE),
                      (group_context, "GROUPS_DIR", group_context.GROUPS_DIR),
                      (tr, "DIR", tr.DIR)]
        group_context.BASE = self.tmp
        group_context.GROUPS_DIR = self.tmp / "memory" / "groups"
        tr.DIR = self.tmp / "memory" / ".state" / "group_context"

    def tearDown(self):
        for mod, key, value in self._orig:
            setattr(mod, key, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _msg(self, peer, mid, *, topic=None, reply=None, text=""):
        group_context.observe_message(
            peer_id=peer, topic_id=topic, message_id=mid, sender_id=1,
            sender_name="Николай", reply_to_message_id=reply,
            timestamp=None, edited_at=None, text=text or f"сообщение {mid}",
            topic_title="", media="", outgoing=False)

    def _forum_room(self):
        """Форум как в жизни: настоящая тема, General и цепочка ответов в General."""
        # настоящая тема: её открывает служебное сообщение, и оно лежит в ней самой
        group_context.record_topic(FORUM, 5, "Вход и подключение", message_id=5)
        self._msg(FORUM, 5, topic=5, text="создана тема")
        self._msg(FORUM, 6, topic=5, text="в настоящей теме")
        self._msg(FORUM, 7, topic=5, text="и ещё в ней же")
        # General: корень без темы, ответ на него уезжает в псевдоветку
        self._msg(FORUM, 100, topic=None, text="разговор в General")
        self._msg(FORUM, 101, topic=None, reply=100, text="первый ответ в General")
        self._msg(FORUM, 102, topic=100, reply=101, text="продолжение в General")
        self._msg(FORUM, 103, topic=None, text="другой разговор в General")
        return FORUM


class TestTheRule(Base):
    """Ветка — настоящее место тогда и только тогда, когда её корень лежит в ней самой."""

    def test_artifacts_are_named_and_real_topics_are_not(self):
        peer = self._forum_room()
        mapping = group_context.branch_containers(peer)
        self.assertEqual(mapping, {100: None}, "псевдоветка General — и только она")

    def test_a_branch_without_its_root_is_not_guessed(self):
        peer = self._forum_room()
        self._msg(peer, 500, topic=404, text="корня #404 в архиве нет")
        self.assertNotIn(404, group_context.branch_containers(peer),
                         "нет корня — нет вывода, а не удобная догадка")

    def test_a_chain_inside_a_real_topic_belongs_to_that_topic(self):
        peer = self._forum_room()
        self._msg(peer, 200, topic=5, text="корень цепочки внутри темы 5")
        self._msg(peer, 201, topic=200, reply=200, text="ответ уехал в псевдоветку")
        self.assertEqual(group_context.branch_containers(peer)[200], 5)


class TestPlaceOf(Base):
    def test_unknown_room_keeps_every_key_apart(self):
        self.assertEqual(tr.place_of(f"{ROOM}__topic__7"), f"{ROOM}__topic__7")

    def test_a_plain_room_is_one_place(self):
        tr.observe(ROOM, kind="channel_forum_missing", message_id=1)
        self.assertEqual(tr.place_of(f"{ROOM}__topic__7"), ROOM)
        self.assertEqual(tr.place_of(ROOM), ROOM)

    def test_a_forum_topic_is_its_own_place(self):
        peer = self._forum_room()
        tr.observe(peer, kind="topic_opener_seen", since_message_id=1,
                   until_message_id=9999)
        tr.observe_branches(peer, group_context.branch_containers(peer))
        self.assertEqual(tr.place_of(f"{peer}__topic__5"), f"{peer}__topic__5")

    def test_a_general_artifact_returns_to_general(self):
        peer = self._forum_room()
        tr.observe(peer, kind="topic_opener_seen", since_message_id=1,
                   until_message_id=9999)
        tr.observe_branches(peer, group_context.branch_containers(peer))
        self.assertEqual(tr.place_of(f"{peer}__topic__100"), peer)
        self.assertTrue(tr.same_room(f"{peer}__topic__100", peer))
        self.assertFalse(tr.same_room(f"{peer}__topic__100", f"{peer}__topic__5"),
                         "General и настоящая тема — разные места")

    def test_an_unproven_branch_of_a_forum_stays_apart(self):
        peer = self._forum_room()
        tr.observe(peer, kind="topic_opener_seen", since_message_id=1,
                   until_message_id=9999)
        self.assertEqual(tr.place_of(f"{peer}__topic__100"), f"{peer}__topic__100",
                         "без доказательства форум делится как делился")


class TestReadingOnePlace(Base):
    def test_general_reads_as_one_conversation(self):
        peer = self._forum_room()
        tr.observe(peer, kind="topic_opener_seen", since_message_id=1,
                   until_message_id=9999)
        tr.observe_branches(peer, group_context.branch_containers(peer))
        scope = tr.read_scope(peer, 100)
        self.assertFalse(scope.whole_room, "форум целиком не читаем никогда")
        self.assertEqual(scope.thread_word, "thread")
        out = group_context.describe(peer, action="context", topic_id=100, limit=200,
                                     whole_room=scope.whole_room, members=scope.members,
                                     thread_word=scope.thread_word)
        for marker in ("разговор в General", "первый ответ в General",
                       "продолжение в General", "другой разговор в General"):
            self.assertIn(marker, out, marker)
        self.assertNotIn("в настоящей теме", out,
                         "тему форума в General не втягиваем — её разделил Telegram")

    def test_a_real_topic_reads_as_itself(self):
        peer = self._forum_room()
        tr.observe(peer, kind="topic_opener_seen", since_message_id=1,
                   until_message_id=9999)
        tr.observe_branches(peer, group_context.branch_containers(peer))
        scope = tr.read_scope(peer, 5)
        self.assertEqual(scope.thread_word, "topic", "тему форума темой и зовём")
        out = group_context.describe(peer, action="context", topic_id=5, limit=200,
                                     whole_room=scope.whole_room, members=scope.members,
                                     thread_word=scope.thread_word)
        self.assertIn("в настоящей теме", out)
        self.assertNotIn("разговор в General", out)

    def test_a_plain_room_still_reads_whole(self):
        for mid, topic in ((10, None), (11, 10), (20, None), (21, 20)):
            self._msg(ROOM, mid, topic=topic, reply=(topic if topic else None),
                      text=f"строка {mid}")
        tr.observe(ROOM, kind="channel_forum_missing", since_message_id=1,
                   until_message_id=9999)
        scope = tr.read_scope(ROOM, 10)
        self.assertTrue(scope.whole_room)
        self.assertEqual(scope.thread_word, "thread")
        out = group_context.describe(ROOM, action="context", topic_id=10, limit=200,
                                     whole_room=scope.whole_room, members=scope.members,
                                     thread_word=scope.thread_word)
        for mid in (10, 11, 20, 21):
            self.assertIn(f"строка {mid}", out)

    def test_scope_never_shows_less_than_the_old_branch_read(self):
        """Расширение обязано быть строгим добавлением — иначе это не расширение."""
        peer = self._forum_room()
        tr.observe(peer, kind="topic_opener_seen", since_message_id=1,
                   until_message_id=9999)
        tr.observe_branches(peer, group_context.branch_containers(peer))
        old = group_context.context(peer, topic_id=100, limit=200, max_chars=40000)
        scope = tr.read_scope(peer, 100)
        new = group_context.context(peer, topic_id=100, limit=200, max_chars=40000,
                                    whole_room=scope.whole_room, members=scope.members,
                                    thread_word=scope.thread_word)
        for line in old.splitlines():
            body = line.split("] ", 1)[-1].strip()
            if body and not body.startswith("["):
                self.assertIn(body, new, body[:40])

    def test_an_empty_registry_reads_the_whole_room_not_a_branch(self):
        """Молчание реестра — «мы не спрашивали», а не «здесь ветки». Решение Егора 06.08.

        ⚠ ЭТОТ ТЕСТ НАЗЫВАЛСЯ `..._reads_exactly_as_before` и держал ОБРАТНУЮ политику:
        пока про комнату ничего не доказано, лента сжималась до ветки. Замер 06.08 показал
        цену осторожности: 446 буферов, из них 424 — ветки, при двух настоящих форумах на
        все её места. Она просыпалась в куске разговора и не видела комнаты, из которой
        кусок вырезан.

        Цена ошибки несимметрична, и тест держит именно это: ошибиться в сторону комнаты —
        показать ей соседние сообщения того же чата, то есть ровно то, что видит человек;
        ошибиться в сторону ветки — показать обрывок и назвать его разговором.
        """
        peer = self._forum_room()
        scope = tr.read_scope(peer, 100)
        self.assertTrue(scope.whole_room, "пустой реестр снова режет ленту до ветки")
        branch = group_context.context(peer, topic_id=100, limit=200, max_chars=40000)
        room = group_context.context(peer, topic_id=100, limit=200, max_chars=40000,
                                     whole_room=scope.whole_room, members=scope.members,
                                     thread_word=scope.thread_word)
        # Строгое добавление: ни одна строка ветки не пропала, и комната шире неё.
        for line in branch.splitlines():
            body = line.split("] ", 1)[-1].strip()
            if body and not body.startswith("["):
                self.assertIn(body, room, body[:40])
        self.assertGreater(len(room), len(branch),
                           "комната не шире ветки — расширение не состоялось")

    def test_a_proven_forum_still_keeps_its_topics_apart(self):
        """Перевёрнутое умолчание НЕ трогает настоящий форум: его границы провёл Telegram."""
        peer = self._forum_room()
        tr.observe(peer, kind="entity_forum_flag", forum=True, message_id=1)
        scope = tr.read_scope(peer, 100)
        self.assertFalse(scope.whole_room,
                         "прямое forum=true перестало разделять темы — это уже не место")


if __name__ == "__main__":
    unittest.main()
