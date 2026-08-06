"""То, что она записала СЕБЕ, можно найти. И прибор расхода показывает то, что расходуется.

02.08.2026. Её четвёртый шаг доказательства обучения — «извлечение в точке выбора» — стоял,
и мы думали, что дело в аллоулисте автоматического recall. Оказалось хуже: пути
`memory/notes/events.jsonl` не было в списке индексируемых ВООБЩЕ. Её собственные уроки
(`praxis.authored_note.event.v1`) не находил ни автоматический recall, ни явное «вспомни».
Урок написан — и невидим.

Граница здесь важнее самой правки: заметки становятся находимыми ЯВНО, но в автоматический
канон НЕ входят. У jsonl-видов `automatic_eligible` по умолчанию True, поэтому без явного
исключения новый вид молча попал бы к ней в кадр на каждом ходе — а что стоит в кадре,
решают Егор и она.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import llm
import memory_fts

NOTE = {
    "schema": "praxis.authored_note.event.v1",
    "event_type": "written", "kind": "note",
    "note_id": "note-20260728T210706-8bf9c75b2e",
    "run_id": "run-20260728T210650Z-aaaaaaaa",
    "scope": "chat", "ts": "2026-07-28T21:07:06Z",
    "text": "Урокмаркер: подготовленное вспоминается как доставленное — перед словом "
            "«сделано» искать квитанцию, а не память.",
}


class HerNotesAreFindable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="praxis_notes_")
        self.base = Path(self.tmp.name)
        self.mem = self.base / "memory"
        self.skills = self.base / "soul" / "skills"
        self.skills.mkdir(parents=True)
        (self.mem / "notes").mkdir(parents=True)
        (self.mem / "notes" / "events.jsonl").write_text(
            json.dumps(NOTE, ensure_ascii=False) + "\n", encoding="utf-8")
        memory_fts.rebuild(base=self.base, memory_dir=self.mem, skills_dir=self.skills)
        self.addCleanup(self.tmp.cleanup)

    def _rows(self, query, purpose="explicit"):
        return memory_fts.search(query, base=self.base, memory_dir=self.mem,
                                 skills_dir=self.skills, scope="owner",
                                 purpose=purpose, limit=20)

    def test_the_path_is_indexed_at_all(self):
        rels = {s.rel for s in memory_fts.iter_sources(base=self.base, memory_dir=self.mem)}
        self.assertIn("memory/notes/events.jsonl", rels)

    def test_explicit_recall_finds_her_lesson(self):
        rows = self._rows("Урокмаркер")
        self.assertTrue(rows, "её собственный урок должен находиться явным recall")
        self.assertEqual(rows[0]["source_type"], "authored_note")

    def test_it_does_not_enter_the_automatic_canon(self):
        """Граница 👤: что стоит в кадре на каждом ходе — не наше решение."""
        self.assertEqual(self._rows("Урокмаркер", purpose="automatic"), [])

    def test_kind_is_typed_not_lumped_into_self_event(self):
        kind, visibility = memory_fts._selected_jsonl(
            self.mem / "notes" / "events.jsonl", self.mem)
        self.assertEqual((kind, visibility), ("authored_note", "owner"))


class SpendMeterShowsWhatIsSpent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="praxis_meter_")
        self.orig = llm.USAGE_PATH
        llm.USAGE_PATH = Path(self.tmp.name) / "usage.json"
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: setattr(llm, "USAGE_PATH", self.orig))

    def test_line_names_the_cache_share_and_the_fresh_input(self):
        llm._usage_add("voice", {"in": 310139, "out": 2000, "cache_read": 264192},
                       model="gpt-5.6-sol")
        line = llm.usage_line()
        self.assertIn("кэш 85%", line)
        self.assertIn("свежего входа", line)

    def test_silence_when_the_provider_says_nothing(self):
        """Отсутствие поля — не ноль-как-факт: строка про кэш просто не появляется."""
        llm._usage_add("voice", {"in": 1000, "out": 10}, model="gpt-5.6-sol")
        self.assertNotIn("кэш", llm.usage_line())

    def test_no_division_by_zero_on_an_empty_day(self):
        llm._usage_add("voice", {"in": 0, "out": 0, "cache_read": 0}, model="x")
        llm.usage_line()


if __name__ == "__main__":
    unittest.main()
