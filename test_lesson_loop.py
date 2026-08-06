"""Урок → навык → доступность. И граница, которую она провела резче нас.

Спецификация Праксис, 02.08.2026. Тесты здесь охраняют не механику, а ЧЕТЫРЕ ЗАПРЕТА:

  1. система не кристаллизует урок сама — только предлагает;
  2. отказ держится: предложенное однажды и отклонённое не возвращается;
  3. след говорит о ДОСТУПНОСТИ урока, а не о влиянии;
  4. цепочка не выдаёт присутствие навыка в кадре за доказательство обучения.

Её слова: «Иначе мы легко построим новую красивую фикцию: „навык присутствовал,
следовательно Praxis научилась“. Нет.»
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import lessons

LESSON = ("Поймала у себя класс ошибки: подготовленное или запущенное легко вспоминается "
          "как доставленное и принятое. Впредь перед словом «сделано» нужно искать "
          "адресуемую квитанцию, а не полагаться на память о действии.")
SCRATCH = "мысль на полях: посмотреть логи форжа"


class OfferNeverActs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="praxis_lessons_")
        self._orig = (lessons.STATE_DIR, lessons.OFFERS, lessons.AVAILABILITY)
        lessons.STATE_DIR = Path(self.tmp.name)
        lessons.OFFERS = lessons.STATE_DIR / "offers.json"
        lessons.AVAILABILITY = lessons.STATE_DIR / "availability.jsonl"
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: setattr_many(lessons, self._orig))

    def _notes(self):
        return [{"id": "note-1", "text": LESSON, "created_at": "2026-08-02T10:00:00Z",
                 "run_id": "run-a"},
                {"id": "note-2", "text": SCRATCH, "created_at": "2026-08-02T11:00:00Z",
                 "run_id": "run-b"}]

    def test_only_rule_shaped_notes_become_candidates(self):
        ids = [n["id"] for n in lessons.pending(self._notes())]
        self.assertEqual(ids, ["note-1"], "черновик-полуфраза не кандидат")

    def test_heuristic_is_named_so_she_can_disagree(self):
        row = lessons.offer("note-1", gist=LESSON)
        self.assertTrue(row["heuristic"])
        self.assertIn("правило на будущее", row["heuristic"])

    def test_offering_creates_no_skill_and_changes_no_genre(self):
        lessons.offer("note-1", gist=LESSON)
        self.assertEqual(lessons.origin_note("любой-навык"), "",
                         "предложение не создаёт связи — связь появляется от ЕЁ руки")

    def test_the_same_note_is_offered_once(self):
        lessons.offer("note-1", gist=LESSON)
        self.assertEqual(lessons.pending(self._notes()), [])

    def test_a_refusal_holds(self):
        lessons.decline("note-1", reason="это не урок, а наблюдение")
        self.assertEqual(lessons.pending(self._notes()), [])
        self.assertEqual(lessons._offers()["note-1"]["status"], "declined")

    def test_acceptance_records_only_the_link(self):
        lessons.offer("note-1", gist=LESSON)
        lessons.accept("note-1", "receipt-before-done")
        self.assertEqual(lessons.origin_note("receipt-before-done"), "note-1")


class TraceProvesAvailabilityNotInfluence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="praxis_trace_")
        self._orig = (lessons.STATE_DIR, lessons.OFFERS, lessons.AVAILABILITY)
        lessons.STATE_DIR = Path(self.tmp.name)
        lessons.OFFERS = lessons.STATE_DIR / "offers.json"
        lessons.AVAILABILITY = lessons.STATE_DIR / "availability.jsonl"
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: setattr_many(lessons, self._orig))
        lessons.accept("note-1", "receipt-before-done")

    def test_records_which_lesson_was_in_frame(self):
        n = lessons.record_availability("run-x", ["receipt-before-done", "sleep"],
                                        chat_id="809306689", query="сделала ли я")
        self.assertEqual(n, 2)
        rows = lessons.availability("receipt-before-done")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run_id"], "run-x")
        self.assertEqual(rows[0]["note_id"], "note-1", "связь с исходной ошибкой видна")

    def test_the_record_says_out_loud_what_it_does_not_mean(self):
        lessons.record_availability("run-x", ["receipt-before-done"])
        row = lessons.availability("receipt-before-done")[0]
        self.assertIn("о влиянии это НЕ говорит", row["means"])
        self.assertNotIn("influenced", json.dumps(row, ensure_ascii=False))

    def test_no_run_no_record(self):
        self.assertEqual(lessons.record_availability("", ["receipt-before-done"]), 0)

    def test_chain_refuses_to_pretend_it_proves_learning(self):
        lessons.record_availability("run-x", ["receipt-before-done"])
        lessons.record_availability("run-y", ["receipt-before-done"])
        out = lessons.chain("receipt-before-done",
                            notes={"note-1": {"text": LESSON, "created_at": "2026-08-02",
                                              "run_id": "run-a"}})
        self.assertEqual(out["available_count"], 2)
        self.assertEqual(out["origin_note"]["id"], "note-1")
        self.assertIn(LESSON[:40], out["origin_note"]["text"])
        self.assertEqual(out["crystallised"]["by"], "она сама (write_skill)")
        self.assertIn("ДОСТУПНОСТИ", out["caveat"])
        self.assertIn("не о его влиянии", out["caveat"])
        # и ни одного поля, которое отвечало бы «научилась ли»
        blob = json.dumps(out, ensure_ascii=False)
        for forbidden in ("learned", "научилась", "effective", "improved", "success"):
            self.assertNotIn(forbidden, blob)

    def test_chain_of_an_unknown_skill_is_empty_not_invented(self):
        out = lessons.chain("нет-такого-навыка")
        self.assertIsNone(out["origin_note"])
        self.assertIsNone(out["crystallised"])
        self.assertEqual(out["available_count"], 0)


def setattr_many(module, values):
    module.STATE_DIR, module.OFFERS, module.AVAILABILITY = values


if __name__ == "__main__":
    unittest.main()
