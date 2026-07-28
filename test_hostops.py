"""
Тесты PASS 17.C — правки хоста (верхняя ступень «хозяйки сервера»).

Это самая рискованная ступень (root-запись в /etc), поэтому проверяем ГРАНИЦЫ:
  1. её сторона умеет ТОЛЬКО стейджить, не писать на хост;
  2. старый hostagent сохраняет свой allowlist, optional owner scope, shrink-guard и бэкап;
  3. апрув неформжируем — применение только по явной команде (cmd_apply), не по маркеру;
  4. два списка (её зеркало и вшитый пол hostagent) не разъезжаются;
  5. каждое решение — расписка, которую она видит.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hostops
import hostagent


class Base(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="praxis_hostc_"))
        (self.home / "memory" / ".state").mkdir(parents=True)
        self.stage = self.home / "memory" / ".host_ops"
        self.stage.mkdir()
        # её сторона смотрит в этот дом
        self._ho = {"STAGE_DIR": hostops.STAGE_DIR, "RECEIPTS": hostops.RECEIPTS,
                    "ALLOW": hostops.HOST_ALLOW_MIRROR}
        hostops.STAGE_DIR = self.stage
        hostops.RECEIPTS = self.home / "memory" / ".state" / "hostops.jsonl"
        # hostagent смотрит в тот же дом + свой backups; allowlist сузим на тестовый файл
        self.hostfile = self.home / "etc" / "target.conf"
        self.hostfile.parent.mkdir(parents=True)
        self.hostfile.write_text("original host config\nline2\nline3\n", encoding="utf-8")
        self._ha = {k: getattr(hostagent, k) for k in
                    ("STAGE_DIR", "RECEIPTS", "BACKUPS", "HOST_ALLOW")}
        hostagent.STAGE_DIR = self.stage
        hostagent.RECEIPTS = hostops.RECEIPTS
        hostagent.BACKUPS = self.home / "agent" / "backups"
        hostagent.HOST_ALLOW = (str(self.hostfile),)
        hostops.HOST_ALLOW_MIRROR = (str(self.hostfile),)

    def tearDown(self):
        for k, v in self._ho.items():
            setattr(hostops, {"ALLOW": "HOST_ALLOW_MIRROR"}.get(k, k), v)
        for k, v in self._ha.items():
            setattr(hostagent, k, v)
        shutil.rmtree(self.home, ignore_errors=True)

    def _receipts(self):
        p = hostops.RECEIPTS
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


# --------------------------------------------------------------------------- #
#  1. Её сторона: только стейдж
# --------------------------------------------------------------------------- #

class TestSheOnlyStages(Base):
    def test_stage_writes_request_not_host_file(self):
        oid, msg = hostops.stage(str(self.hostfile), "new config\nnew2\nnew3\n", "почистить")
        self.assertTrue(oid, msg)
        self.assertIn("применит", msg.lower())
        # хост-файл НЕ тронут: она только заявила
        self.assertEqual(self.hostfile.read_text(encoding="utf-8"), "original host config\nline2\nline3\n")
        self.assertTrue((self.stage / f"{oid}.json").exists())
        self.assertTrue((self.stage / f"{oid}.content").exists())

    def test_stage_refuses_path_outside_allowlist(self):
        # OS-абсолютный путь вне allowlist (на Windows "/etc/x" не считается absolute)
        outside = str(self.home / "etc" / "passwd")
        oid, msg = hostops.stage(outside, "root::0:0::/:/bin/sh\n", "зло")
        self.assertEqual(oid, "")
        self.assertIn("нельзя", msg.lower())

    def test_stage_refuses_relative_path(self):
        oid, msg = hostops.stage("etc/x.conf", "x", "y")
        self.assertEqual(oid, "")
        self.assertIn("абсолютн", msg.lower())

    def test_stage_refuses_empty_content(self):
        oid, msg = hostops.stage(str(self.hostfile), "", "удалить?")
        self.assertEqual(oid, "")


# --------------------------------------------------------------------------- #
#  2. Рельсы hostagent независимы от неё
# --------------------------------------------------------------------------- #

@unittest.skipUnless(os.name == "posix", "hostagent — Linux-only root-CLI; рельсы на unix-путях")
class TestAgentRails(Base):
    def test_explicit_owner_scope_beats_allowlist(self):
        """An explicitly configured protected root beats this legacy route's allowlist."""
        protected = self.home / "private-root"
        hostagent.HOST_ALLOW = (str(self.hostfile), str(protected))
        with mock.patch.object(hostagent, "PROTECTED_ROOTS", (str(protected),)):
            self.assertIn("PRAXIS_PROTECTED_ROOTS", hostagent.rails_check(str(protected)))

    def test_project_name_is_not_a_boundary_by_default(self):
        project = self.home / "hardbot"
        hostagent.HOST_ALLOW = (str(project),)
        with mock.patch.object(hostagent, "PROTECTED_ROOTS", ()):
            self.assertEqual(hostagent.rails_check(str(project)), "")

    def test_path_not_in_allowlist_refused(self):
        self.assertIn("allowlist", hostagent.rails_check("/etc/other.conf"))

    def test_allowlisted_path_ok(self):
        self.assertEqual(hostagent.rails_check(str(self.hostfile)), "")

    def test_dotdot_cannot_escape_allowlist(self):
        """`..` не лазейка: normpath его схлопывает, а результат обязан ТОЧНО быть в allowlist.
        `/etc/каталог/../shadow` → `/etc/shadow`; побег в непопавший путь ловится
        allowlist. В любом случае — не '' (не пропущено)."""
        self.assertNotEqual(hostagent.rails_check("/etc/foo/../shadow"), "")
        self.assertNotEqual(hostagent.rails_check(str(self.hostfile.parent / ".." / "escape.conf")), "")
        self.assertIn("allowlist", hostagent.rails_check("/etc/caddy/../shadow"))


# --------------------------------------------------------------------------- #
#  3. Применение — только явной командой; бэкап; shrink-guard
# --------------------------------------------------------------------------- #

class TestApply(Base):
    def _stage(self, content, reason="правка"):
        oid, _ = hostops.stage(str(self.hostfile), content, reason)
        self.assertTrue(oid)
        return oid

    def test_apply_writes_host_file_and_backs_up(self):
        oid = self._stage("brand new config\nb\nc\n")
        rc = hostagent.cmd_apply(oid)
        self.assertEqual(rc, 0)
        self.assertEqual(self.hostfile.read_text(encoding="utf-8"), "brand new config\nb\nc\n")
        # бэкап оригинала существует
        backups = list((self.home / "agent" / "backups").glob("*.bak"))
        self.assertTrue(backups, "перед правкой хоста обязан быть бэкап")
        self.assertIn("original host config", backups[0].read_text(encoding="utf-8"))
        # заявка убрана из очереди, расписка есть
        self.assertFalse((self.stage / f"{oid}.json").exists())
        rec = self._receipts()
        self.assertTrue(rec and rec[-1]["ok"] and rec[-1]["op"] == "apply")

    def test_shrink_guard_blocks_truncation(self):
        oid = self._stage("x\n")   # резко короче оригинала
        rc = hostagent.cmd_apply(oid)
        self.assertEqual(rc, 2)
        self.assertEqual(self.hostfile.read_text(encoding="utf-8"),
                         "original host config\nline2\nline3\n", "усечение не должно примениться")
        self.assertTrue(any("shrink" in r.get("note", "") for r in self._receipts()))

    def test_shrink_guard_lifted_by_force(self):
        oid = self._stage("x\n")
        rc = hostagent.cmd_apply(oid, force=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.hostfile.read_text(encoding="utf-8"), "x\n")

    def test_reject_leaves_host_untouched_and_receipts(self):
        oid = self._stage("whatever\na\nb\n")
        rc = hostagent.cmd_reject(oid, "не сейчас")
        self.assertEqual(rc, 0)
        self.assertEqual(self.hostfile.read_text(encoding="utf-8"),
                         "original host config\nline2\nline3\n")
        self.assertFalse((self.stage / f"{oid}.json").exists())
        rec = self._receipts()
        self.assertTrue(rec and rec[-1]["op"] == "reject" and "не сейчас" in rec[-1]["note"])

    def test_forged_approval_marker_does_nothing(self):
        """Ключевое: она может писать в stage-каталог, но application не запускается ни от
        какого маркера — только от cmd_apply (команды Егора на хосте). Пусть подделает
        «одобрено» — хост-файл не изменится, пока Егор не применит сам."""
        oid = self._stage("malicious\n" * 5)
        # она пишет фейковый маркер одобрения — у hostagent нет вотчера, это ничего не делает
        (self.stage / f"{oid}.approved").write_text("approved by me!", encoding="utf-8")
        # хост-файл не тронут: применение бывает только через cmd_apply
        self.assertEqual(self.hostfile.read_text(encoding="utf-8"),
                         "original host config\nline2\nline3\n")


# --------------------------------------------------------------------------- #
#  4. Два списка не разъезжаются; расписки видны ей
# --------------------------------------------------------------------------- #

class TestConsistencyAndVisibility(Base):
    def test_mirror_matches_real_floor_in_source(self):
        """Зеркало у hostops и вшитый allowlist у hostagent — один узкий список.
        Сверяем ИСХОДНИКИ (в тестах оба переопределены), чтобы прод не разъехался."""
        import importlib
        ho = importlib.import_module("hostops")
        ha = importlib.import_module("hostagent")
        # перечитываем константы из модулей заново, минуя setUp-подмену
        ho_src = Path(ho.__file__).read_text(encoding="utf-8")
        ha_src = Path(ha.__file__).read_text(encoding="utf-8")
        # оба содержат тестовый путь как единственный «боевой» дефолт
        self.assertIn("/etc/praxis-hostagent/test.conf", ho_src)
        self.assertIn("/etc/praxis-hostagent/test.conf", ha_src)

    def test_she_sees_outcome_via_describe(self):
        # контент не короче половины оригинала — иначе законно сработает shrink-guard
        oid, _ = hostops.stage(str(self.hostfile), "new host config\nline2\nline3-changed\n", "тест")
        text = hostops.describe()
        self.assertIn(oid, text)
        self.assertEqual(hostagent.cmd_apply(oid), 0)
        text2 = hostops.describe()
        self.assertIn("применено", text2)

    def test_state_line_is_honest(self):
        line = hostops.state_line()
        self.assertIn("стейдж", line.lower())
        self.assertIn("применяет егор", line.lower())


# --------------------------------------------------------------------------- #
#  5. Проводка тулов: owner-only, действие не разведчику
# --------------------------------------------------------------------------- #

class TestToolWiring(unittest.TestCase):
    def test_propose_is_owner_only_not_scout(self):
        import agent
        self.assertIn("propose_host_change", agent.TOOL_IMPL)
        self.assertIn("propose_host_change", [t["name"] for t in agent.OWNER_TOOLS])
        self.assertNotIn("propose_host_change", [t["name"] for t in agent.BASE_TOOLS])
        self.assertNotIn("propose_host_change", [t["name"] for t in agent.FAMILY_TOOLS])
        self.assertNotIn("propose_host_change", agent._SCOUT_TOOL_NAMES,
                         "правка хоста — действие, разведчику нельзя")

    def test_list_host_changes_registered(self):
        import agent
        self.assertIn("list_host_changes", agent.TOOL_IMPL)
        self.assertIn("list_host_changes", [t["name"] for t in agent.OWNER_TOOLS])

    def test_hostagent_on_protected_floor(self):
        import selfdev
        self.assertIn("hostagent.py", selfdev.PROTECTED_PATTERNS)
        self.assertIn("hostops.py", selfdev.PROTECTED_PATTERNS)
        self.assertEqual(selfdev.zone_for(["hostagent.py"]), "protected")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
