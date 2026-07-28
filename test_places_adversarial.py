"""
Находки адверсарки 25.07 по пассу «место» — каждая закреплена тестом.

29 агентов, 18 находок, 17 пережили опровержение. Ни одну из них я бы не увидел сам:
все они об одном и том же — утверждение в комментарии расходилось с тем, что делает код.
Поэтому здесь проверяется не «работает», а именно то УТВЕРЖДЕНИЕ, которое расходилось.

Запуск:  python praxis_test.py test_places_adversarial -v
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import group_context
import memory_life as ml
import telegram_routes as tr

ROOM = "-1001240718803"
FORUM = "-1004301095307"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_adv_"))
        self._gc = [(group_context, "BASE", group_context.BASE),
                    (group_context, "GROUPS_DIR", group_context.GROUPS_DIR),
                    (tr, "DIR", tr.DIR)]
        group_context.BASE = self.tmp
        group_context.GROUPS_DIR = self.tmp / "memory" / "groups"
        tr.DIR = self.tmp / "memory" / ".state" / "group_context"

    def tearDown(self):
        for mod, key, value in self._gc:
            setattr(mod, key, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _msg(self, peer, mid, *, topic=None, reply=None, text=""):
        group_context.observe_message(
            peer_id=peer, topic_id=topic, message_id=mid, sender_id=1,
            sender_name="Николай", reply_to_message_id=reply, timestamp=None,
            edited_at=None, text=text or f"сообщение {mid}", topic_title="",
            media="", outgoing=False)


class TestTheDigestKeepsEveryPlace(Base):
    """Обзор «где я ещё сейчас» терял настоящие темы форума и ветки неизвестных комнат.

    Схлопывание шло по ПРЕФИКСУ комнаты: оставляли один ключ на комнату и выбрасывали
    все остальные — в том числе те, которые тот же реестр честно отказался склеивать.
    Регрессия против дерева до правки; проверено адверсаркой тремя независимыми линзами.
    """

    def _digest(self, keys, *, exclude=None):
        import agent
        state = self.tmp / "state"
        state.mkdir(parents=True, exist_ok=True)
        meta = {key: {"last_ts": ts, "name": name, "is_dm": False}
                for key, (ts, name) in keys.items()}
        (state / "buf_meta.json").write_text(json.dumps(meta), encoding="utf-8")
        scratch = self.tmp / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        orig = (agent.STATE_DIR, agent.notes.SCRATCH_DIR)
        agent.STATE_DIR, agent.notes.SCRATCH_DIR = state, scratch
        try:
            for key in keys:
                agent.notes.append(key, f"строка про {key}")
            return agent.other_rooms_digest(exclude_chat_id=exclude)
        finally:
            agent.STATE_DIR, agent.notes.SCRATCH_DIR = orig

    def test_real_forum_topics_all_stay_visible(self):
        tr.observe(FORUM, kind="topic_opener_seen", since_message_id=1,
                   until_message_id=9999)
        tr.observe_branches(FORUM, {500: None})
        out = self._digest({
            FORUM: (100.0, "Грибница"),
            f"{FORUM}__topic__11": (200.0, "Грибница / Вход"),
            f"{FORUM}__topic__22": (300.0, "Грибница / Питание"),
            f"{FORUM}__topic__500": (400.0, "Грибница / General-ветка"),
        })
        self.assertIn("__topic__11", out, "настоящая тема форума — отдельное место")
        self.assertIn("__topic__22", out)

    def test_branches_of_an_unknown_room_stay_visible(self):
        out = self._digest({
            "-100888": (100.0, "комната"),
            "-100888__topic__7": (200.0, "её ветка"),
        })
        self.assertIn("-100888__topic__7", out,
                      "без свидетельства ничего не склеиваем — и ничего не прячем")
        self.assertIn("-100888", out)

    def test_a_place_is_not_lost_when_its_freshest_key_has_no_note(self):
        """Схлопывание стояло ДО отбора: пустая записка у самого свежего ключа — и всё
        место исчезало из «где я сейчас» (вторая адверсарка 25.07)."""
        import agent
        tr.observe(ROOM, kind="channel_forum_missing", since_message_id=1,
                   until_message_id=999999)
        state = self.tmp / "state2"
        state.mkdir(parents=True, exist_ok=True)
        (state / "buf_meta.json").write_text(json.dumps({
            ROOM: {"last_ts": 100.0, "name": "AbstractDL", "is_dm": False},
            f"{ROOM}__topic__9": {"last_ts": 900.0, "name": "AbstractDL", "is_dm": False},
        }), encoding="utf-8")
        scratch = self.tmp / "scratch2"
        scratch.mkdir(parents=True, exist_ok=True)
        orig = (agent.STATE_DIR, agent.notes.SCRATCH_DIR)
        agent.STATE_DIR, agent.notes.SCRATCH_DIR = state, scratch
        try:
            agent.notes.append(ROOM, "живая записка в корне")   # у свежего ключа записки нет
            out = agent.other_rooms_digest()
        finally:
            agent.STATE_DIR, agent.notes.SCRATCH_DIR = orig
        self.assertIn("живая записка в корне", out,
                      "место остаётся видимым по той ветке, у которой есть что показать")

    def test_a_proven_room_still_collapses_to_one_line(self):
        tr.observe(ROOM, kind="channel_forum_missing", since_message_id=1,
                   until_message_id=999999)
        out = self._digest({
            ROOM: (100.0, "AbstractDL"),
            f"{ROOM}__topic__1": (200.0, "AbstractDL"),
            f"{ROOM}__topic__2": (300.0, "AbstractDL"),
        })
        self.assertEqual(out.count("AbstractDL"), 1, "одно место — одна строка")


class TestWideningNeverShowsLess(Base):
    """«Строго добавление» — обещание, которое надо выполнять, а не декларировать.

    Расширение места складывает в срез сотни соседних строк, и общий потолок начинал
    выбрасывать её собственную ветку, чтобы уместить соседей.
    """

    def _room(self):
        for i in range(40):
            self._msg(ROOM, 1000 + i, topic=None, text=f"сосед {i} " + "ы" * 400)
        self._msg(ROOM, 100, topic=None, text="корень её ветки")
        for i in range(8):
            self._msg(ROOM, 200 + i, topic=100, reply=100, text=f"её строка {i}")
        tr.observe(ROOM, kind="channel_forum_missing", since_message_id=1,
                   until_message_id=999999)

    def test_her_own_branch_survives_a_tight_budget(self):
        self._room()
        scope = tr.read_scope(ROOM, 100)
        narrow = group_context.context(ROOM, topic_id=100, limit=200, max_chars=40000)
        wide = group_context.context(ROOM, topic_id=100, limit=200, max_chars=4000,
                                     whole_room=scope.whole_room, members=scope.members,
                                     thread_word=scope.thread_word)
        for i in range(8):
            self.assertIn(f"её строка {i}", wide,
                          "соседи занимают ОСТАТОК бюджета, а не место её ветки")
        self.assertIn("корень её ветки", wide)
        self.assertTrue(len(narrow) > 0)

    def test_a_tight_cap_keeps_her_branch_too(self):
        self._room()
        scope = tr.read_scope(ROOM, 100)
        wide = group_context.context(ROOM, topic_id=100, limit=10, max_chars=40000,
                                     whole_room=scope.whole_room, members=scope.members,
                                     thread_word=scope.thread_word)
        self.assertIn("её строка 7", wide)
        self.assertIn("корень её ветки", wide)

    def test_reservation_works_for_the_root_branch_too(self):
        """General форума — корневая ветка, `topic_id` пуст. Резервирование выключалось
        целиком ровно в том случае, ради которого делалось (вторая адверсарка)."""
        for i in range(30):
            self._msg(FORUM, 1000 + i, topic=777, text=f"сосед {i} " + "ы" * 300)
        for i in range(6):
            self._msg(FORUM, 100 + i, topic=None, text=f"её строка General {i}")
        tr.observe(FORUM, kind="topic_opener_seen", since_message_id=1,
                   until_message_id=9999)
        tr.observe_branches(FORUM, {777: None})
        scope = tr.read_scope(FORUM, None)
        self.assertIsNotNone(scope.members)
        out = group_context.context(FORUM, topic_id=None, limit=200, max_chars=3000,
                                    whole_room=scope.whole_room, members=scope.members,
                                    thread_word=scope.thread_word)
        for i in range(6):
            self.assertIn(f"её строка General {i}", out)

    def test_the_cap_stays_a_cap_when_her_branch_fills_it(self):
        """`[-max(0, cap - room):]` — это `[-0:]`, то есть ВЕСЬ список, а не пустой."""
        for i in range(30):
            self._msg(ROOM, 2000 + i, topic=None, text=f"сосед {i}")
        for i in range(12):
            self._msg(ROOM, 100 + i, topic=100 if i else None,
                      reply=100 if i else None, text=f"её строка {i}")
        tr.observe(ROOM, kind="channel_forum_missing", since_message_id=1,
                   until_message_id=999999)
        scope = tr.read_scope(ROOM, 100)
        out = group_context.context(ROOM, topic_id=100, limit=5, max_chars=40000,
                                    whole_room=scope.whole_room, members=scope.members,
                                    thread_word=scope.thread_word)
        rows = [l for l in out.splitlines() if l.startswith("[")]
        self.assertLessEqual(len(rows), 6, "потолок остаётся потолком")
        self.assertIn("её строка 11", out, "и занимает его её собственная ветка")

    def test_her_newest_line_keeps_the_wide_render(self):
        """Широкий рендер — тоже ресурс: соседи не занимают его вперёд неё."""
        long_text = "ц" * 3000
        for i in range(6):
            self._msg(ROOM, 3000 + i, topic=None, text=f"сосед {i} " + "ы" * 100)
        self._msg(ROOM, 100, topic=None, text="корень")
        self._msg(ROOM, 101, topic=100, reply=100, text="её длинная реплика " + long_text)
        tr.observe(ROOM, kind="channel_forum_missing", since_message_id=1,
                   until_message_id=999999)
        scope = tr.read_scope(ROOM, 100)
        out = group_context.context(ROOM, topic_id=100, limit=200, max_chars=40000,
                                    whole_room=scope.whole_room, members=scope.members,
                                    thread_word=scope.thread_word)
        line = next(l for l in out.splitlines() if "её длинная реплика" in l)
        self.assertNotIn("ОБРЕЗАНО", line,
                         "её свежая реплика не обрезается ради соседских строк")

    def test_the_cut_says_which_side_was_cut(self):
        self._room()
        scope = tr.read_scope(ROOM, 100)
        wide = group_context.context(ROOM, topic_id=100, limit=200, max_chars=4000,
                                     whole_room=scope.whole_room, members=scope.members,
                                     thread_word=scope.thread_word)
        self.assertIn("ЛЕНТА ОБРЕЗАНА", wide)
        self.assertIn("в соседних ветках", wide, "обрез назван по имени, а не молча")
        # ⚠ Счёт идёт от ВСЕГО места, а не от уже обрезанного среза: первая версия
        # маркера (моя) считала остаток внутри `selected`, урезанного потолком, и
        # обещала «ещё 49», когда там были тысячи. И совет был ложный — упирается
        # бюджет символов, а не limit (вторая адверсарка + опись 26.07).
        self.assertNotIn("бо́льшим limit", wide, "ложный совет убран")
        self.assertIn("context_summary_chars", wide, "сказано, что реально поднимать")


class TestOrientationMatchesWhatSheGets(Base):
    """Строка ориентации — самое авторитетное предложение промпта про эту комнату."""

    def test_forum_general_is_not_called_isolated(self):
        tr.observe(FORUM, kind="topic_opener_seen", since_message_id=1,
                   until_message_id=9999)
        tr.observe_branches(FORUM, {100: None, 300: None})
        line = tr.orientation_line(FORUM, 100, "sel", tr.TRUE)
        self.assertNotIn("isolated from every other topic", line,
                         "именно здесь мы её ветку и склеили с General")
        self.assertIn("General", line)
        scope = tr.read_scope(FORUM, 100)
        self.assertIn(300, scope.members, "и склеили ровно так, как сказали")

    def test_a_real_forum_topic_is_still_called_isolated(self):
        tr.observe(FORUM, kind="topic_opener_seen", since_message_id=1,
                   until_message_id=9999)
        tr.observe_branches(FORUM, {100: None})
        line = tr.orientation_line(FORUM, 5, "sel", tr.TRUE)
        self.assertIn("isolated from every other topic", line)

    def test_an_ordinary_room_says_it_is_one_room(self):
        tr.observe(ROOM, kind="channel_forum_missing", since_message_id=1,
                   until_message_id=999999)
        line = tr.orientation_line(ROOM, 100, "sel", tr.FALSE)
        self.assertIn("NOT a Telegram forum topic", line)


class TestTheMapNamesEachBranchByItsOwnNature(Base):
    def test_a_real_topic_is_never_renamed_to_a_reply_thread(self):
        group_context.record_topic(FORUM, 5, "Вход", message_id=5)
        self._msg(FORUM, 5, topic=5, text="создана тема")
        self._msg(FORUM, 6, topic=5)
        self._msg(FORUM, 100, topic=None)
        self._msg(FORUM, 101, topic=100, reply=100)
        tr.observe(FORUM, kind="topic_opener_seen", since_message_id=1,
                   until_message_id=9999)
        tr.observe_branches(FORUM, group_context.branch_containers(FORUM))
        out = group_context.map_text(FORUM, current_topic=100,
                                     artifacts=tr.artifacts_of(FORUM))
        self.assertIn("topic #5", out, "тему форума провёл Telegram")
        self.assertIn("reply thread #100", out, "а эту ветку завели мы")

    def test_a_proven_ordinary_room_calls_them_all_ours(self):
        self._msg(ROOM, 100, topic=None)
        self._msg(ROOM, 101, topic=100, reply=100)
        out = group_context.map_text(ROOM, not_a_forum=True)
        self.assertIn("reply thread #100", out)
        self.assertNotIn("topic #100", out)


class TestNothingElseInTheStateDirIsTouched(unittest.TestCase):
    """`.state/life` — общий каталог. Заявка на переплавку — не разговор и невосстановима."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_state_"))
        self._orig = (ml.STATE_DIR, ml.MEM_DIR, ml.LIFE_DIR)
        ml.MEM_DIR = self.tmp / "memory"
        ml.LIFE_DIR = ml.MEM_DIR / "life"
        ml.STATE_DIR = ml.MEM_DIR / ".state" / "life"
        ml.STATE_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        ml.STATE_DIR, ml.MEM_DIR, ml.LIFE_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_formation_request_is_not_a_conversation(self):
        request = ml.STATE_DIR / "formation.request.json"
        request.write_text(json.dumps({"depth": "full", "requested_at": "сейчас"}),
                           encoding="utf-8")
        (ml.STATE_DIR / "-100777.json").write_text(
            json.dumps({"schema": 1, "chat_id": "-100777", "hot": [], "frontier": []}),
            encoding="utf-8")
        self.assertFalse(ml.is_state_file(request))
        self.assertEqual(ml.state_keys(), ["-100777"])
        out = ml.retire_split_states()
        self.assertEqual(out["moved"], [])
        self.assertTrue(request.exists(), "её заявку на переплавку никто не отодвигает")

    def test_rollback_asks_the_same_question_as_retirement(self):
        """Откат удалял ЛЮБОЙ появившийся json — включая заявку на переплавку."""
        import migrate_places
        attic = ml.STATE_DIR / "_pre_places"
        attic.mkdir(parents=True, exist_ok=True)
        (attic / "-100777.json").write_text(
            json.dumps({"schema": 1, "chat_id": "-100777", "hot": [], "frontier": []}),
            encoding="utf-8")
        (ml.STATE_DIR / "-100777.json").write_text(
            json.dumps({"schema": 1, "chat_id": "-100777", "hot": [1], "frontier": []}),
            encoding="utf-8")
        request = ml.STATE_DIR / "formation.request.json"
        request.write_text(json.dumps({"depth": "full"}), encoding="utf-8")
        appeared = ml.STATE_DIR / "-100888.json"
        appeared.write_text(
            json.dumps({"schema": 1, "chat_id": "-100888", "hot": [], "frontier": []}),
            encoding="utf-8")

        class _Routes:
            DIR = ml.STATE_DIR / "no_such_registry"

        out = migrate_places.rollback(ml, _Routes)
        self.assertTrue(request.exists(), "её заявку откат не трогает")
        self.assertFalse(appeared.exists(), "а появившееся состояние — убирает")
        self.assertIn("-100777", out["restored"])

    def test_a_corrupt_file_is_left_alone(self):
        broken = ml.STATE_DIR / "-100888__topic__5.json"
        broken.write_text("{не json", encoding="utf-8")
        self.assertEqual(ml.retire_split_states()["moved"], [])
        self.assertTrue(broken.exists())


if __name__ == "__main__":
    unittest.main()
