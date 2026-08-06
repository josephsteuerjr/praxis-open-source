"""Форж не проверяет её код заведомо красной дверью.

`-m unittest discover` на её репо даёт ~130 падений на ЧИСТОМ дереве (96 из них — один
`LiveTreeWriteRefused`): `_sandbox` взводится только при импорте, а BASE заморожен
модульной константой раньше, поэтому песочница встаёт посередине прогона. Форж держит
worktree её собственного репо, значит проверка её кода была красной по построению — и
она читала это как «мой код сломан», а не «дверь не та».
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import forge_intelligence


class CheckDoor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="praxis_forge_door_")
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.root / "test_thing.py").write_text("", encoding="utf-8")

    def _test_commands(self):
        return [row["command"] for row in forge_intelligence.discovered_checks(self.root)
                if row["kind"] == "test"]

    def test_plain_project_still_uses_unittest(self):
        commands = self._test_commands()
        self.assertTrue(any("-m unittest discover" in c for c in commands), commands)

    def test_repo_with_its_own_door_uses_it(self):
        (self.root / "praxis_test.py").write_text("", encoding="utf-8")
        commands = self._test_commands()
        self.assertTrue(any("praxis_test.py discover" in c for c in commands), commands)
        self.assertFalse(any("-m unittest" in c for c in commands),
                         "неправильная дверь не должна оставаться запасной")

    def test_source_names_the_door(self):
        (self.root / "praxis_test.py").write_text("", encoding="utf-8")
        row = next(r for r in forge_intelligence.discovered_checks(self.root)
                   if r["kind"] == "test")
        self.assertIn("praxis_test.py", row["source"])

    def test_targeted_impacted_tests_use_the_same_door(self):
        (self.root / "praxis_test.py").write_text("", encoding="utf-8")
        prefix, _why = forge_intelligence._unittest_prefix(self.root, "python")
        self.assertEqual(prefix, "python praxis_test.py")


if __name__ == "__main__":
    unittest.main()
