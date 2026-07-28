"""
Хвосты правды доставки: случаи, где «перестала врать» едва не превратилось во
«врёт наизнанку».

Запуск:  python praxis_test.py test_delivery_truth_tail -v
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import turns
from core import narration as core_narration


class TestMediaOnlyTurnIsNotLeftUnconfirmed(unittest.TestCase):
    """Ход, у которого ушли ТОЛЬКО вложения, не доходит до текстовой расписки —
    и остался бы навсегда «написала, доставка не подтверждена», хотя всё улетело."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_dtt_"))
        self._orig = [(turns, "STATE_DIR", turns.STATE_DIR), (turns, "PATH", turns.PATH)]
        turns.STATE_DIR = self.tmp / "memory" / ".state"
        turns.PATH = turns.STATE_DIR / "turns.jsonl"
        turns._reset()

    def tearDown(self):
        for mod, k, v in self._orig:
            setattr(mod, k, v)
        turns._reset()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _turn(self, run_id="run-media"):
        t = turns.begin(kind="chat", chat_id="777", scope="owner", who="Егор")
        t["out"] = "вот файл"
        t["run_id"] = run_id
        turns.record(t)
        return t

    def test_authored_reads_as_unconfirmed(self):
        self._turn()
        row = turns.recent(1, scope="owner", chat_id="777")[-1]
        self.assertEqual(row.get("delivery"), "authored")
        self.assertIn("написала", turns.format_line(row))

    def test_completion_without_text_still_confirms_the_turn(self):
        import agent
        self._turn(run_id="run-media-2")
        agent.project_delivery_outcome("run-media-2", "accepted", text="")
        row = turns.recent(1, scope="owner", chat_id="777")[-1]
        self.assertEqual(row.get("delivery"), "accepted",
                         "медиа-ход обязан получить исход, иначе журнал отрицает отправку")
        self.assertIn("сказала", turns.format_line(row))


class TestNarrationPendingIsResolved(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_nar_"))
        self._orig = core_narration.LEDGER
        core_narration.LEDGER = self.tmp / "narration.jsonl"
        core_narration.LEDGER.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        core_narration.LEDGER = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pending_becomes_accepted(self):
        core_narration.note("-100", "собираю кадры", delivery="pending")
        self.assertEqual(core_narration.recent(1)[-1]["delivery"], "pending")
        self.assertTrue(core_narration.resolve("-100", "собираю кадры"))
        self.assertEqual(core_narration.recent(1)[-1]["delivery"], "accepted")

    def test_pending_still_dedupes_so_the_text_cannot_go_twice(self):
        """Запись обязана существовать до сети: идемпотентность прямого аутбокса
        привязана к call_id, и повторная наррация создала бы ВТОРУЮ запись очереди."""
        core_narration.note("-100", "собираю кадры", delivery="pending")
        self.assertTrue(core_narration.is_duplicate("-100", "собираю кадры"))

    def test_resolve_is_idempotent_and_survives_a_miss(self):
        core_narration.note("-100", "раз", delivery="pending")
        self.assertTrue(core_narration.resolve("-100", "раз"))
        self.assertTrue(core_narration.resolve("-100", "раз"))
        self.assertFalse(core_narration.resolve("-100", "такого не было"))


class TestFailRetainCeilingCountsBytes(unittest.TestCase):
    def test_ceiling_is_expressed_in_bytes_not_only_lines(self):
        """Потолок в строках был бы обманом самого себя: строка держит `out` до 24000
        символов, поэтому 20 колец — это сотни мегабайт, перечитываемых каждый ход."""
        self.assertTrue(hasattr(turns, "RETAIN_MAX_BYTES"))
        self.assertLess(turns.RETAIN_MAX_BYTES,
                        turns.KEEP * turns.RETAIN_MAX_RINGS * turns.OUT_CHARS,
                        "байтовый потолок обязан быть строже строчного в худшем случае")


if __name__ == "__main__":
    unittest.main()
