"""Матрица проверок узнаёт Lean-проект и берёт ПРОЕКТНЫЙ тулчейн.

03.08.2026 Praxis завела Lean-проект и поставила тулчейн локально в него
(`.tools/elan-home`), а не в образ. `discovered_checks` знала python/npm/cargo/go/make —
и отдавала пустой план. Отдельно: `forge_verify` исполняет команду через shell с
унаследованным окружением, поэтому голое `lake` проектный тулчейн не нашло бы.

Проверяются три обещания, а не формулировка:
  • lakefile/lean-toolchain делают проект видимым для матрицы;
  • проектный `lake` побеждает системный, и путь абсолютный;
  • если lake нет вовсе — проверка НЕ выдумывается (пустой план честнее красного).
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import forge_intelligence as fi


class LeanProjectIsVisibleToTheMatrix(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="forge_lean_")
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _local_lake(self) -> Path:
        binary = self.root / ".tools" / "elan-home" / "bin" / "lake"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o755)
        return binary

    def _ids(self):
        return {row["id"]: row for row in fi.discovered_checks(self.root)}

    def test_lakefile_alone_is_not_enough_without_a_toolchain(self):
        (self.root / "lakefile.lean").write_text("import Lake\n", encoding="utf-8")
        with mock.patch.object(fi.shutil, "which", lambda name: None):
            self.assertNotIn("lean-lake-build", self._ids(),
                             "проверка, которую нечем запустить, — заведомо красная дверь")

    def _wrapper(self):
        scripts = self.root / "scripts"
        scripts.mkdir(exist_ok=True)
        build = scripts / "build.sh"
        build.write_text("#!/bin/sh" + chr(10), encoding="utf-8")
        build.chmod(0o755)
        return build

    def test_the_projects_own_wrapper_wins(self):
        """03.08.2026 Praxis завела `scripts/build.sh` поверх проверяемой по SHA-256
        установки Lean. Это заявленный проектом способ собраться — матрица обязана
        брать его, а не изобретать свой."""
        (self.root / "lakefile.toml").write_text("name = \"x\"", encoding="utf-8")
        binary = self.root / ".tools" / "lean-4.32.2" / "bin" / "lake"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh" + chr(10), encoding="utf-8")
        binary.chmod(0o755)
        wrapper = self._wrapper()
        row = self._ids()["lean-lake-build"]
        self.assertEqual(row["command"], "sh " + str(wrapper))
        self.assertNotIn("build\" build", row["command"])

    def test_wrapper_without_a_toolchain_invents_nothing(self):
        """`build.sh` без установленного Lean вышел бы с «Lean is not installed» —
        заведомо красная дверь. Пустой план честнее."""
        (self.root / "lakefile.toml").write_text("name = \"x\"", encoding="utf-8")
        self._wrapper()
        with mock.patch.object(fi.shutil, "which", lambda name: None):
            self.assertNotIn("lean-lake-build", self._ids())

    def test_project_local_toolchain_wins_and_is_absolute(self):
        (self.root / "lakefile.lean").write_text("import Lake\n", encoding="utf-8")
        binary = self._local_lake()
        with mock.patch.object(fi.shutil, "which", lambda name: "/usr/bin/lake"):
            row = self._ids()["lean-lake-build"]
        self.assertIn(str(binary), row["command"], "проектный тулчейн должен побеждать PATH")
        self.assertIn("ELAN_HOME=", row["command"],
                      "без явного дома шим поставит тулчейн руту, а не в проект")
        self.assertIn(str(binary.parent.parent), row["command"])
        self.assertTrue(row["command"].endswith(" build"))
        self.assertEqual(row["kind"], "test")

    def test_system_lake_is_used_when_the_project_has_none(self):
        (self.root / "lean-toolchain").write_text("leanprover/lean4:v4.32.2\n", encoding="utf-8")
        with mock.patch.object(fi.shutil, "which", lambda name: "/usr/bin/lake"):
            row = self._ids()["lean-lake-build"]
        self.assertIn("/usr/bin/lake", row["command"])

    def test_lakefile_toml_counts_too(self):
        (self.root / "lakefile.toml").write_text("name = \"lamp\"\n", encoding="utf-8")
        self._local_lake()
        self.assertIn("lean-lake-build", self._ids())

    def test_a_plain_python_project_gains_no_lean_check(self):
        (self.root / "test_x.py").write_text("import unittest\n", encoding="utf-8")
        self.assertNotIn("lean-lake-build", self._ids())


if __name__ == "__main__":
    unittest.main()
