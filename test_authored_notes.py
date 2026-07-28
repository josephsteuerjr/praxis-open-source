from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from authored_notes import AuthoredNoteLedger


class AuthoredNoteLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = AuthoredNoteLedger(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_write_list_read_and_close(self):
        row = self.ledger.write(
            "проверить контракт",
            kind="scratch",
            scope="run",
            run_id="run-1",
            chat_id="42",
            message_id=7,
            source_ref="run:run-1:call:call-1",
        )
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["run_id"], "run-1")
        self.assertEqual(self.ledger.get(row["id"])["text"], "проверить контракт")
        self.assertEqual([item["id"] for item in self.ledger.list(run_id="run-1")], [row["id"]])

        closed = self.ledger.close(row["id"], reason="проверено")
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["closed_reason"], "проверено")
        self.assertEqual(self.ledger.list(run_id="run-1"), [])
        self.assertEqual(self.ledger.list(status="closed", run_id="run-1")[0]["id"], row["id"])

    def test_scope_requires_real_context(self):
        with self.assertRaisesRegex(ValueError, "active run"):
            self.ledger.write("x", scope="run")
        with self.assertRaisesRegex(ValueError, "active chat"):
            self.ledger.write("x", scope="chat")

    def test_torn_line_does_not_hide_prior_events(self):
        row = self.ledger.write("сохранено")
        with self.ledger.events_path.open("a", encoding="utf-8") as handle:
            handle.write('{"schema":"praxis.authored_note.event.v1"')
        self.assertEqual(self.ledger.get(row["id"])["text"], "сохранено")

    def test_invalid_utf8_tail_does_not_hide_prior_events(self):
        row = self.ledger.write("сохранено")
        with self.ledger.events_path.open("ab") as handle:
            handle.write(b"\xff\xfe")
        self.assertEqual(self.ledger.get(row["id"])["text"], "сохранено")

    def test_run_metadata_never_contains_note_text(self):
        row = self.ledger.write("секретный внутренний текст", kind="reflection", scope="run", run_id="run-2")
        metadata = self.ledger.metadata_for_run("run-2")
        self.assertEqual(metadata[0]["id"], row["id"])
        self.assertNotIn("text", metadata[0])
        self.assertNotIn("секретный", json.dumps(metadata, ensure_ascii=False))

    def test_ledger_does_not_create_other_memory_layers(self):
        self.ledger.write("только заметка")
        memory = Path(self.temp.name) / "memory"
        self.assertTrue((memory / "notes" / "events.jsonl").is_file())
        for path in (
            memory / "journal",
            memory / "desires",
            memory / "self" / "observations.jsonl",
            memory / "tasks.json",
        ):
            self.assertFalse(path.exists(), str(path))


if __name__ == "__main__":
    unittest.main()
