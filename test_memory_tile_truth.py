# -*- coding: utf-8 -*-
"""Числа про память на экране берутся из того, что сервер посчитал."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import praxis_app


class MemoryHealthShapeIsWhatTheTileReads(unittest.TestCase):
    """Дефект: плитка читала memory.records и memory.status — таких ключей у
    _memory_health нет. count(undefined) даёт уверенный 0, а first(...) добирался до
    литерала "healthy". Экран сообщал «0 записей» и «здоров» вообще без источника.
    Тест закрепляет форму, на которую теперь опирается плитка."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "memory" / "maps").mkdir(parents=True)
        for name in praxis_app.MAP_NAMES:
            (self.root / "memory" / "maps" / f"{name}.md").write_text("#\n", encoding="utf-8")
        self.service = praxis_app.PraxisAppService(self.root, owner_id=1)

    def test_health_has_no_records_or_status_key(self):
        health = self.service._memory_health()
        self.assertNotIn("records", health, "плитка снова получит уверенный ноль")
        self.assertNotIn("status", health, "пилюля снова упадёт на литерал healthy")

    def test_missing_index_is_reported_as_unavailable_not_as_zero(self):
        health = self.service._memory_health()
        self.assertFalse(health["index"]["available"],
                         "отсутствующий индекс обязан называться отсутствующим")
        self.assertEqual(health["index"]["chunks"], 0)

    def test_present_index_reports_its_own_size(self):
        state = self.root / "memory" / ".state"
        state.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(state / "recall.sqlite3") as db:
            db.execute("CREATE TABLE chunks (path TEXT, text TEXT)")
            db.executemany("INSERT INTO chunks VALUES (?, ?)",
                           [("a.md", "раз"), ("a.md", "два"), ("b.md", "три")])
        health = self.service._memory_health()
        self.assertTrue(health["index"]["available"])
        self.assertEqual(health["index"]["chunks"], 3)
        self.assertEqual(health["index"]["sources"], 2)


class TileReadsTheIndexNotAPhantomKey(unittest.TestCase):
    def setUp(self):
        self.js = (Path(praxis_app.__file__).resolve().parent
                   / "praxis_static" / "app.js").read_text(encoding="utf-8")

    def test_tile_counts_index_chunks(self):
        block = self.js.split('setText("#tileMemorySub"', 1)[0][-900:]
        self.assertIn("memoryIndex.chunks", block)
        self.assertNotIn("count(memory.records)", self.js,
                         "плитка снова считает несуществующий ключ")

    def test_health_pill_reads_availability(self):
        block = self.js.split("function renderMemory(snapshot) {", 1)[1].split(
            "function mountMemoryViews", 1)[0]
        self.assertIn("index.available === false", block,
                      "снятый или отсутствующий индекс снова рисуется зелёным")
        self.assertIn("index.error", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
