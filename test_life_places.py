"""
Лента жизни принадлежит МЕСТУ, а не строке ключа.

Замер 25.07 на живых данных: 174 отдельных состояния жизни на одну комнату AbstractDL —
174 параллельные жизни в одном разговоре, у каждой свой курсор свёртки и своё горячее
кольцо. Событие при этом пишется под тем ключом, где было сказано: это честная запись
«где», и её никто не переписывает. Меняется то, что считается одним разговором.

Главное здесь — не «стало шире», а два инварианта:
  * ни одно событие не может пропасть и не может оказаться одновременно горячим и свёрнутым;
  * компакт, канонический сегодня, не может стать неканоническим оттого, что реестр
    узнал что-то новое. Память нельзя чинить так, чтобы она при этом могла пропасть.

Запуск:  python praxis_test.py test_life_places -v
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import memory_life as ml
import memory_provenance
import telegram_routes as tr

ROOM = "-1001240718803"
A = f"{ROOM}__topic__100"
B = f"{ROOM}__topic__200"
FORUM = "-1004301095307"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_life_"))
        self._orig = {name: getattr(ml, name) for name in
                      ("BASE", "MEM_DIR", "LIFE_DIR", "EVENTS_DIR", "COMPACTS_DIR",
                       "EPISODES_DIR", "CLAIMS_DIR", "PATCHES_DIR", "REFLECTIONS_DIR",
                       "STATE_DIR", "LEGACY_SUMMARIES_DIR", "DIALOGUES_DIR")}
        ml.BASE = self.tmp
        ml.MEM_DIR = self.tmp / "memory"
        ml.LIFE_DIR = ml.MEM_DIR / "life"
        ml.EVENTS_DIR = ml.LIFE_DIR / "events"
        ml.COMPACTS_DIR = ml.LIFE_DIR / "compacts"
        ml.EPISODES_DIR = ml.LIFE_DIR / "episodes"
        ml.CLAIMS_DIR = ml.LIFE_DIR / "claims"
        ml.PATCHES_DIR = ml.LIFE_DIR / "patches"
        ml.REFLECTIONS_DIR = ml.LIFE_DIR / "reflections"
        ml.STATE_DIR = ml.MEM_DIR / ".state" / "life"
        ml.LEGACY_SUMMARIES_DIR = ml.MEM_DIR / ".summaries"
        ml.DIALOGUES_DIR = ml.MEM_DIR / "dialogues"
        self._routes = tr.DIR
        tr.DIR = self.tmp / "memory" / ".state" / "group_context"

    def tearDown(self):
        for name, value in self._orig.items():
            setattr(ml, name, value)
        tr.DIR = self._routes
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _say(self, chat, text):
        return ml.record_message(chat, text, actor="Николай", direction="in")

    def _one_room(self, peer=ROOM):
        tr.observe(peer, kind="channel_forum_missing",
                   since_message_id=1, until_message_id=999999)


class TestOneSpinePerPlace(Base):
    def test_without_evidence_each_key_lives_apart(self):
        self._say(A, "в ветке A")
        self._say(B, "в ветке B")
        self.assertEqual(len(ml.iter_events(chat_id=A, kinds={"conversation_message"})), 1)
        self.assertNotEqual(ml._state_path(A), ml._state_path(B))

    def test_a_proven_room_has_one_spine(self):
        self._one_room()
        self._say(A, "в ветке A")
        self._say(B, "в ветке B")
        self._say(ROOM, "в корне")
        seen = [e["text"] for e in ml.iter_events(chat_id=A, kinds={"conversation_message"})]
        self.assertEqual(len(seen), 3, "одна комната — одна лента")
        self.assertEqual(ml._state_path(A), ml._state_path(ROOM))
        self.assertEqual(ml._state_path(B), ml._state_path(ROOM))

    def test_the_event_still_records_where_it_was_said(self):
        """Место — про чтение. Запись остаётся честной: сказано было в ветке."""
        self._one_room()
        rec = self._say(A, "сказано в ветке")
        self.assertEqual(rec["chat_id"], A)

    def test_a_forum_keeps_its_topics_apart(self):
        tr.observe(FORUM, kind="topic_opener_seen",
                   since_message_id=1, until_message_id=999999)
        tr.observe_branches(FORUM, {900: None})       # 900 — наш артефакт General
        self._say(f"{FORUM}__topic__5", "в настоящей теме")
        self._say(f"{FORUM}__topic__900", "в General через псевдоветку")
        self._say(FORUM, "в General")
        general = [e["text"] for e in ml.iter_events(chat_id=FORUM,
                                                    kinds={"conversation_message"})]
        self.assertEqual(len(general), 2, "General и его артефакт — одно место")
        topic = ml.iter_events(chat_id=f"{FORUM}__topic__5", kinds={"conversation_message"})
        self.assertEqual(len(topic), 1, "тему форума разделил Telegram — не сливаем")

    def test_a_place_appearing_at_runtime_does_not_orphan_the_hot_ring(self):
        """Место определяется впервые — и состояние переезжает. Прежде на новом пути
        заводилось ПУСТОЕ кольцо, а всё, что копилось под ключом ветки, оставалось
        сиротой: свёртка его больше не видела никогда, и в долгую память эти разговоры
        не попадали вовсе (адверсарка 25.07)."""
        for i in range(9):
            self._say(A, f"до того, как узнали про комнату {i}")
        self.assertEqual(len(ml._load_state(A)["hot"]), 9)
        self._one_room()                       # реестр впервые ответил про комнату
        self._say(A, "первая строка после переезда")
        hot = [x["line"] for x in ml._load_state(A)["hot"]]
        self.assertEqual(len(hot), 10, "прежнее кольцо не потерялось при переезде")
        self.assertIn("до того, как узнали про комнату 0", hot)
        self.assertIn("первая строка после переезда", hot)

    def test_rebuild_keeps_every_event_exactly_once(self):
        self._one_room()
        for i in range(12):
            self._say(A if i % 2 else B, f"строка {i}")
        state = ml.rebuild_state(ROOM)
        every = {str(e["id"]) for e in
                 ml.iter_events(chat_id=ROOM, kinds={"conversation_message"})}
        hot = {str(x["id"]) for x in state["hot"]}
        canon, _ = ml._canonical_compact_graph(ROOM)
        covered = {e for meta, _ in canon.values() for e in meta.get("source_event_ids") or []}
        self.assertEqual(hot | covered, every, "ни одно событие не потерялось")
        self.assertFalse(hot & covered, "и ни одно не задвоилось")


class TestTrustOnlyGrows(Base):
    """Компакт, канонический сегодня, не может стать неканоническим завтра."""

    def _fold(self, chat):
        for i in range(ml.HOT_HI + 5):
            self._say(chat, f"событие {i} для свёртки")
        out = ml.compact_if_due(chat, force=True)
        self.assertTrue(out.get("compact_id"), out)
        return out["compact_id"]

    def test_a_compact_made_before_the_registry_survives_it(self):
        compact_id = self._fold(A)                    # свёрнуто ещё под ключом ветки
        canon_before, _ = ml._canonical_compact_graph(A)
        self.assertIn(compact_id, canon_before)
        self._one_room()                              # теперь комната доказанно одна
        canon_after, _ = ml._canonical_compact_graph(ROOM)
        self.assertIn(compact_id, canon_after,
                      "сводка не имеет права исчезнуть от того, что мы узнали правду")
        self.assertIn(compact_id, ml.context_summary(ROOM, max_chars=100000))

    def test_a_compact_survives_the_room_becoming_a_forum(self):
        """Ровно сценарий адверсарки: новая эпоха сверху — и ключ проваливается в unknown.

        Обычная супергруппа доказана на диапазоне 1..1000. Ветка #5000 новее всего
        наблюдаемого, поэтому отвечает вердиктом комнаты, и её события сворачиваются
        под комнатой. Потом комнату РЕАЛЬНО превращают в форум — и #5000 оказывается в
        дыре между эпохами. Прежде компакт в этот момент переставал быть каноническим и
        молча исчезал из её сводки.
        """
        late = f"{ROOM}__topic__5000"
        tr.observe(ROOM, kind="channel_forum_missing",
                   since_message_id=1, until_message_id=1000)
        self.assertEqual(tr.status_at(ROOM, 5000)[0], tr.FALSE)
        for i in range(ml.HOT_HI + 5):
            self._say(late, f"событие {i}")
        out = ml.compact_if_due(late, force=True)
        compact_id = out["compact_id"]
        self.assertIn(compact_id, ml.context_summary(ROOM, max_chars=200000))

        tr.observe(ROOM, kind="get_forum_topics_ok",
                   since_message_id=9000, until_message_id=9000)
        self.assertEqual(tr.status_at(ROOM, 5000)[0], tr.UNKNOWN,
                         "ключ действительно провалился в дыру между эпохами")
        self.assertEqual(tr.place_of(late), late, "и место у него теперь другое")
        canon, _ = ml._canonical_compact_graph(ROOM)
        self.assertIn(compact_id, canon, "и компакт всё равно на месте")
        self.assertIn(compact_id, ml.context_summary(ROOM, max_chars=200000))
        self.assertIn(compact_id, ml.context_summary(late, max_chars=200000))

    def test_trust_does_not_move_when_the_registry_learns(self):
        """Слой доверия НЕ спрашивает реестр: знание уточняется, история — нет."""
        self.assertTrue(ml._same_conversation(A, A))
        self.assertFalse(ml._same_conversation(A, B))
        self._one_room()
        self.assertFalse(ml._same_conversation(A, B),
                         "вердикт комнаты ничего не привязывает — привязывает свёртка")
        self.assertTrue(ml._same_place(A, B), "а группировка ленты уточниться вправе")

    def test_only_a_real_fold_binds_and_the_binding_never_moves(self):
        self._one_room()
        ml.bind_place(ROOM, [A, B])
        self.assertTrue(ml._same_conversation(A, ROOM))
        self.assertTrue(ml._same_conversation(A, B))
        self.assertTrue(memory_provenance.same_conversation(A, ROOM, ml.bindings()))
        # реестр «передумал»: комната оказалась форумом, ветки снова разные места
        tr.observe(ROOM, kind="topic_opener_seen", since_message_id=1,
                   until_message_id=999999)
        self.assertNotEqual(tr.place_of(A), tr.place_of(B))
        self.assertTrue(ml._same_conversation(A, ROOM),
                        "уже свёрнутое остаётся свёрнутым — иначе компакт пропадёт")

    def test_already_folded_events_are_never_folded_twice(self):
        """Реестр «моргнул» — ветка снова сама по себе. Уже свёрнутое обязано остаться
        свёрнутым, иначе те же события уходят во вторую свёртку и она читает один и тот
        же кусок разговора как две независимые памяти (адверсарка 25.07)."""
        self._one_room()
        for i in range(ml.HOT_HI + 5):
            self._say(A, f"событие {i}")
        out = ml.compact_if_due(A, force=True)
        covered = set(out.get("folded_lines") or [])
        self.assertTrue(covered)
        shutil.rmtree(tr.DIR, ignore_errors=True)     # реестра больше нет
        self.assertEqual(ml.place_key(A), ROOM,
                         "журнал сильнее реестра: место, на котором подействовали, "
                         "не пересматривается")
        hot = {str(x.get("line")) for x in ml.rebuild_state(A)["hot"]}
        self.assertFalse(hot & covered,
                         "свёрнутое не возвращается в горячее ни при каком реестре")

    def test_a_fold_under_a_branch_survives_the_registry_changing_twice(self):
        """Вторая адверсарка: свёртка происходит, пока ветка САМА себе место, — тогда
        привязывать нечего, и достижимость компакта из комнаты держалась на реестре.
        Первая же пересборка курсора закрепляет состав места навсегда."""
        late = f"{ROOM}__topic__5000"
        for i in range(ml.HOT_HI + 5):
            self._say(late, f"событие {i}")
        out = ml.compact_if_due(late, force=True)     # реестр молчит: место = ключ
        compact_id = out["compact_id"]
        tr.observe(ROOM, kind="channel_forum_missing",
                   since_message_id=1, until_message_id=1000)
        ml.rebuild_state(ROOM)                        # состав места закреплён
        self.assertIn(compact_id, ml.context_summary(ROOM, max_chars=200000))
        tr.observe(ROOM, kind="get_forum_topics_ok",
                   since_message_id=9000, until_message_id=9000)
        self.assertEqual(tr.status_at(ROOM, 5000)[0], tr.UNKNOWN)
        self.assertIn(compact_id, ml.context_summary(ROOM, max_chars=200000),
                      "реестр передумал — сводка комнаты не обеднела")

    def test_a_bound_key_never_re_splits_into_a_second_cursor(self):
        """Вторая адверсарка: реестр переклеивал ключ в другое место, а лента оставалась
        общей — два курсора над одним потоком, один кусок разговора свёрнут дважды."""
        self._one_room()
        for i in range(6):
            self._say(A, f"строка {i}")
        first = ml._state_path(A)
        tr.observe(ROOM, kind="get_forum_topics_ok",
                   since_message_id=1, until_message_id=999999)
        self._say(A, "после того, как комната стала форумом")
        self.assertEqual(ml._state_path(A), first, "курсор у ключа остаётся один")
        self.assertEqual(ml.place_key(A), ROOM)

    def test_binding_is_append_only(self):
        ml.bind_place(ROOM, [A])
        ml.bind_place("-100999", [A])
        self.assertEqual(ml.bindings()[A], ROOM, "перепривязка задним числом запрещена")

    def test_a_different_room_is_never_the_same_conversation(self):
        self._one_room()
        self._one_room("-100999")
        self.assertFalse(ml._same_conversation(A, "-100999__topic__1"))
        self.assertFalse(memory_provenance.same_conversation(A, "-100999", ml.bindings()))

    def test_summaries_of_the_branches_all_survive_in_the_place(self):
        first = self._fold(A)
        second = self._fold(B)
        self._one_room()
        summary = ml.context_summary(ROOM, max_chars=200000)
        self.assertIn(first, summary)
        self.assertIn(second, summary, "обе половины разговора остаются её сводкой")


class TestCompactionFollowsThePlace(Base):
    def test_new_compacts_are_written_under_the_place(self):
        self._one_room()
        for i in range(ml.HOT_HI + 5):
            self._say(A, f"строка {i}")
        out = ml.compact_if_due(A, force=True)
        self.assertTrue(out.get("compact_id"))
        self.assertTrue((ml._compact_dir(ROOM) / f"{out['compact_id']}.md").exists(),
                        "свёртка комнаты живёт под комнатой, а не под веткой")

    def test_folding_from_any_branch_moves_the_same_cursor(self):
        self._one_room()
        for i in range(ml.HOT_HI + 5):
            self._say(A if i % 2 else B, f"строка {i}")
        ml.compact_if_due(B, force=True)
        hot_after = len(ml._load_state(A, rebuild=True).get("hot") or [])
        self.assertLess(hot_after, ml.HOT_HI + 5,
                        "курсор у места один: свернули из одной ветки — сдвинулось везде")


class TestRetiringTheOldStates(Base):
    def test_branch_states_are_moved_aside_not_deleted(self):
        self._say(A, "в ветке A")
        self._say(B, "в ветке B")
        self.assertEqual(len(list(ml.STATE_DIR.glob("*.json"))), 2)
        self._one_room()
        out = ml.retire_split_states()
        self.assertEqual(sorted(out["moved"]), sorted([ml._safe(A), ml._safe(B)]))
        self.assertEqual(list(ml.STATE_DIR.glob("*.json")), [],
                         "живой каталог перестаёт утверждать лишние разговоры")
        attic = ml.STATE_DIR / "_pre_places"
        self.assertEqual(len(list(attic.glob("*.json"))), 2, "откат остаётся возможным")

    def test_nothing_is_lost_by_retiring(self):
        self._one_room()
        for i in range(6):
            self._say(A if i % 2 else B, f"строка {i}")
        before = {str(x["id"]) for x in ml._load_state(ROOM, rebuild=True)["hot"]}
        ml.retire_split_states()
        after = {str(x["id"]) for x in ml._load_state(ROOM, rebuild=True)["hot"]}
        self.assertEqual(before, after)

    def test_a_place_of_its_own_is_never_touched(self):
        self._say(ROOM, "корневой ключ")
        self._say("555000100", "личка Егора")
        out = ml.retire_split_states()
        self.assertEqual(out["moved"], [])
        self.assertEqual(len(out["kept"]), 2)

    def test_dry_run_moves_nothing(self):
        self._say(A, "в ветке A")
        self._one_room()
        out = ml.retire_split_states(dry_run=True)
        self.assertEqual(out["moved"], [ml._safe(A)])
        self.assertTrue((ml.STATE_DIR / f"{ml._safe(A)}.json").exists())


if __name__ == "__main__":
    unittest.main()
