"""
Тесты boot-супервизора (краш-безопасное само-изменение). Герметичны; git есть в окружении.

Покрыто: политики решений (preflight/after), сам preflight (импорт ядра ловит битый код),
откат на здоровый коммит и внятная запись в дневник.

Запуск:  python praxis_test.py test_bootguard -v
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import bootguard as bg


def _git(tmp: Path, *args: str):
    return subprocess.run(["git", "-C", str(tmp), *args], capture_output=True, text=True)


class TestDecidePreflight(unittest.TestCase):
    def test_ok_launches(self):
        self.assertEqual(bg.decide_preflight(True, "abc", "good"), ("launch", None))

    def test_bad_rolls_back_when_good_exists(self):
        self.assertEqual(bg.decide_preflight(False, "bad", "good"), ("rollback", "good"))

    def test_bad_stuck_without_good(self):
        self.assertEqual(bg.decide_preflight(False, "bad", None), ("stuck", None))
        self.assertEqual(bg.decide_preflight(False, "x", "x"), ("stuck", None))


class TestDecideAfter(unittest.TestCase):
    def test_clean_exit_stops(self):
        d = bg.decide_after(0, 5, "s", "g", 0, 60, 3)
        self.assertEqual(d["action"], "stop")

    def test_survived_grace_marks_good(self):
        d = bg.decide_after(1, 120, "s1", "g", 2, 60, 3)
        self.assertEqual(d["action"], "relaunch")
        self.assertEqual(d["mark_good"], "s1")
        self.assertEqual(d["early_fails"], 0)

    def test_intentional_restart_not_a_crash(self):
        d = bg.decide_after(bg.RESTART_CODE, 3, "s1", "g", 0, 60, 3)
        self.assertEqual(d["action"], "relaunch")
        self.assertNotIn("mark_good", d)
        self.assertEqual(d["early_fails"], 0)

    def test_early_crash_counts_then_rolls_back(self):
        # ниже порога — копим, не откатываем
        d1 = bg.decide_after(1, 2, "bad", "good", 0, 60, 3)
        self.assertEqual(d1["action"], "relaunch")
        self.assertEqual(d1["early_fails"], 1)
        # достигли лимита и есть здоровый коммит — откат
        d3 = bg.decide_after(1, 2, "bad", "good", 2, 60, 3)
        self.assertEqual(d3["action"], "rollback")
        self.assertEqual(d3["to"], "good")
        self.assertEqual(d3["bad"], "bad")

    def test_no_rollback_without_good_ref(self):
        d = bg.decide_after(1, 2, "bad", None, 5, 60, 3)
        self.assertEqual(d["action"], "relaunch")


class TestPreflight(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_pf_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_good_module_passes(self):
        (self.tmp / "good_mod.py").write_text("VALUE = 1\n", encoding="utf-8")
        ok, err = bg.preflight(cwd=self.tmp, modules=["good_mod"])
        self.assertTrue(ok, err)

    def test_broken_module_fails(self):
        (self.tmp / "broken_mod.py").write_text("def (:\n", encoding="utf-8")  # синтаксис битый
        ok, err = bg.preflight(cwd=self.tmp, modules=["broken_mod"])
        self.assertFalse(ok)
        self.assertTrue(err, "ошибка должна быть непустой")


class TestRollbackJournal(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_bg_"))
        (self.tmp / "memory" / "journal").mkdir(parents=True, exist_ok=True)
        _git(self.tmp, "init", "-q")
        _git(self.tmp, "config", "user.email", "t@t")
        _git(self.tmp, "config", "user.name", "t")
        (self.tmp / "code.py").write_text("V = 1\n", encoding="utf-8")
        _git(self.tmp, "add", "-A")
        _git(self.tmp, "commit", "-q", "-m", "v1")
        self.good = _git(self.tmp, "rev-parse", "--short", "HEAD").stdout.strip()
        (self.tmp / "code.py").write_text("V = 2  # bad\n", encoding="utf-8")
        _git(self.tmp, "add", "-A")
        _git(self.tmp, "commit", "-q", "-m", "v2")
        self._orig = (bg.BASE, bg.STATE, bg.JOURNAL_DIR)
        bg.BASE = self.tmp
        bg.STATE = self.tmp / "memory" / ".boot.json"
        bg.JOURNAL_DIR = self.tmp / "memory" / "journal"

    def tearDown(self):
        bg.BASE, bg.STATE, bg.JOURNAL_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rollback_reverts_working_tree(self):
        self.assertIn("bad", (self.tmp / "code.py").read_text(encoding="utf-8"))
        self.assertTrue(bg.rollback(self.good))
        self.assertNotIn("bad", (self.tmp / "code.py").read_text(encoding="utf-8"), "откат не вернул код")

    def test_journal_writes_readable_line(self):
        bg.journal("откатила правку abc123: preflight упал. git show abc123")
        jtext = "".join(p.read_text(encoding="utf-8") for p in bg.JOURNAL_DIR.glob("*.md"))
        self.assertIn("[boot]", jtext)
        self.assertIn("git show abc123", jtext)


if __name__ == "__main__":
    unittest.main(verbosity=2)
