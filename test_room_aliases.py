"""
Псевдонимы на чтении: её собственная непрерывность перестаёт дробиться вместе с ключом.

Ничего не пишется под новым именем. Каждое расширение включается ТОЛЬКО там, где
Telegram прямо ответил, что комната не форум, и вырождается в точное равенство везде,
где такого ответа нет.

Запуск:  python praxis_test.py test_room_aliases -v
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import notes
import telegram_routes as tr
import turns

PEER = "-1001240718803"
A = f"{PEER}__topic__93707"
B = f"{PEER}__topic__92488"
FORUM = "-1004301095307"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_alias_"))
        self._orig = [(tr, "DIR", tr.DIR),
                      (notes, "SCRATCH_DIR", notes.SCRATCH_DIR),
                      (turns, "STATE_DIR", turns.STATE_DIR),
                      (turns, "PATH", turns.PATH)]
        tr.DIR = self.tmp / "memory" / ".state" / "group_context"
        notes.SCRATCH_DIR = self.tmp / "memory" / ".scratch"
        turns.STATE_DIR = self.tmp / "memory" / ".state"
        turns.PATH = turns.STATE_DIR / "turns.jsonl"
        turns._reset()

    def tearDown(self):
        for mod, k, v in self._orig:
            setattr(mod, k, v)
        turns._reset()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _not_a_forum(self, peer=PEER):
        tr.observe(peer, kind="no_topic_openers_in_range",
                   since_message_id=1, until_message_id=999999)

    def _a_forum(self, peer=FORUM):
        tr.observe(peer, kind="topic_opener_seen", since_message_id=1,
                   until_message_id=999999)


class TestPredicate(Base):
    def test_off_without_evidence(self):
        self.assertFalse(tr.same_room(A, B), "без свидетельства — прежнее поведение")
        self.assertTrue(tr.same_room(A, A))

    def test_on_for_a_proven_non_forum(self):
        self._not_a_forum()
        self.assertTrue(tr.same_room(A, B))
        self.assertTrue(tr.same_room(A, PEER))

    def test_never_for_a_real_forum(self):
        self._a_forum()
        self.assertFalse(tr.same_room(f"{FORUM}__topic__5", f"{FORUM}__topic__6"),
                         "темы форума — разные места, склеивать нельзя")

    def test_different_rooms_never_merge(self):
        self._not_a_forum()
        self._not_a_forum("-100999")
        self.assertFalse(tr.same_room(A, "-100999__topic__5"))


class TestLivedTurns(Base):
    def _turn(self, chat_id, out):
        t = turns.begin(kind="chat", chat_id=chat_id, scope="group", who="Николай")
        t["out"] = out
        turns.record(t)

    def test_her_turns_in_one_room_see_each_other(self):
        self._not_a_forum()
        self._turn(A, "ответила в ветке A")
        self._turn(B, "ответила в ветке B")
        seen = [t["out"] for t in turns.recent(10, scope="group", chat_id=A)]
        self.assertIn("ответила в ветке A", seen)
        self.assertIn("ответила в ветке B", seen,
                      "её собственный ход в той же комнате обязан быть виден")

    def test_other_rooms_still_hidden(self):
        self._not_a_forum()
        self._not_a_forum("-100999")
        self._turn(A, "здесь")
        self._turn("-100999__topic__1", "в другой комнате")
        seen = [t["out"] for t in turns.recent(10, scope="group", chat_id=A)]
        self.assertIn("здесь", seen)
        self.assertNotIn("в другой комнате", seen, "граница комнат остаётся")

    def test_without_evidence_behaviour_is_unchanged(self):
        self._turn(A, "ветка A")
        self._turn(B, "ветка B")
        seen = [t["out"] for t in turns.recent(10, scope="group", chat_id=A)]
        self.assertEqual(seen, ["ветка A"])


class TestScratchNotes(Base):
    """Её записка — её нить; анти-повтор смотрит шире, но НЕ вместо неё.

    ⚠ Здесь была склейка соседних веток с сортировкой по `ЧЧ:ММ`. Даты в строке нет:
    позавчерашняя запись в 23:50 обгоняла сегодняшнюю в 09:10, хвостовой срез выбрасывал
    именно сегодняшнюю, и `said_recently` начинал отвечать «нет» на сказанное минуту
    назад. Адверсарка 25.07 воспроизвела это трижды независимо.
    """

    def _write(self, key, text):
        notes.SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
        notes.path_for(key).write_text(text, encoding="utf-8")

    def test_her_own_note_is_never_evicted_by_a_neighbour(self):
        self._not_a_forum()
        self._write(A, "09:10 · сегодня, самое свежее\n")
        self._write(B, "23:50 · позавчера поздно вечером\n23:55 · и ещё позавчера\n")
        out = notes.read(A, 2)
        self.assertIn("сегодня, самое свежее", out)
        self.assertNotIn("позавчера", out, "чужая ветка не вытесняет её собственную нить")

    def test_anti_repeat_still_sees_the_neighbouring_branch(self):
        self._not_a_forum()
        self._write(A, "09:10 · пишу\n")
        self._write(B, "08:00 · сказала: «мы уже это обсуждали вчера»\n")
        self.assertTrue(notes.said_recently(A, "мы уже это обсуждали вчера"),
                        "сказанное в соседней ветке того же места — сказано")
        self.assertFalse(notes.said_recently(A, "совершенно другая мысль"))

    def test_a_stale_neighbour_does_not_hide_a_fresh_line(self):
        self._not_a_forum()
        self._write(A, "09:10 · сказала: «свежая мысль»\n")
        self._write(B, "23:59 · сказала: «мысль трёхнедельной давности»\n")
        self.assertTrue(notes.said_recently(A, "свежая мысль"),
                        "её собственная свежая строка обязана оставаться видимой")

    def test_a_real_forum_topic_is_not_a_neighbour(self):
        self._a_forum()
        self._write(f"{FORUM}__topic__5", "09:00 · сказала: «в теме форума»\n")
        self._write(f"{FORUM}__topic__6", "10:00 · пишу\n")
        self.assertFalse(notes.said_recently(f"{FORUM}__topic__6", "в теме форума"),
                         "темы форума — разные места и для анти-повтора тоже")

    def test_without_evidence_only_its_own_file(self):
        self._write(A, "09:00 · только A\n")
        self._write(B, "10:00 · только B\n")
        self.assertEqual(notes.read(A).strip(), "09:00 · только A")
        self.assertFalse(notes.said_recently(A, "только B"))


class TestPromiseDedup(Base):
    def test_one_promise_per_room(self):
        import tasks
        self._not_a_forum()
        orig = tasks.list_open
        tasks.list_open = lambda: [{"kind": "window", "target": f"promise:{B}", "id": "x"}]
        try:
            import promises
            self.assertIsNotNone(promises.open_for(A),
                                 "обещание в соседней ветке той же комнаты — то же обещание")
        finally:
            tasks.list_open = orig

    def test_without_evidence_dedup_stays_per_key(self):
        import tasks
        orig = tasks.list_open
        tasks.list_open = lambda: [{"kind": "window", "target": f"promise:{B}", "id": "x"}]
        try:
            import promises
            self.assertIsNone(promises.open_for(A))
        finally:
            tasks.list_open = orig


if __name__ == "__main__":
    unittest.main()
