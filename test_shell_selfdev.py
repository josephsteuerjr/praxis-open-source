"""
Тесты Фазы 3 — шелл-рельсы и контролируемое само-развитие (SPEC §3).

Герметичны: без сети/ключей/Ollama и без bash (секрет-гард, автокоммит, write_skill,
restart_self проверяются напрямую). git нужен — он есть в окружении.

Запуск:  python praxis_test.py test_shell_selfdev -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

_fa = types.ModuleType("anthropic")
_fa.Anthropic = lambda **kw: None
sys.modules.setdefault("anthropic", _fa)
_fd = types.ModuleType("dotenv")
_fd.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _fd)

import memory_index as mi  # noqa: E402
import people as pe  # noqa: E402
import selfgit  # noqa: E402
import agent  # noqa: E402


def _git(tmp: Path, *args: str):
    return subprocess.run(["git", "-C", str(tmp), *args], capture_output=True, text=True)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_sd_"))
        mem = self.tmp / "memory"
        soul = self.tmp / "soul"
        for d in (mem / "people", mem / "journal", mem / "rooms", mem / ".vectors",
                  soul / "skills", self.tmp / "workspace"):
            d.mkdir(parents=True, exist_ok=True)
        for n in ("SOUL", "self", "emotions", "being_with"):
            (soul / f"{n}.md").write_text(f"# {n}\nперсона.\n", encoding="utf-8")
        (soul / "skills" / "INDEX.md").write_text("# Praxis Skills Index\n\n| Skill | Trigger |\n|---|---|\n", encoding="utf-8")

        # настоящий git-репо во временной папке
        _git(self.tmp, "init", "-q")
        _git(self.tmp, "config", "user.email", "t@t")
        _git(self.tmp, "config", "user.name", "t")
        _git(self.tmp, "add", "-A")
        _git(self.tmp, "commit", "-q", "-m", "init")

        self._orig: list[tuple] = []
        patch = {
            agent: dict(BASE=self.tmp, SOUL_DIR=soul, SKILLS_DIR=soul / "skills", MEM_DIR=mem,
                        PEOPLE_DIR=mem / "people", ROOMS_DIR=mem / "rooms",
                        JOURNAL_DIR=mem / "journal", REFLECTIONS=mem / "reflections.md",
                        INDEX_MD=mem / "INDEX.md", WORKDIR=str(self.tmp / "workspace")),
            selfgit: dict(BASE=self.tmp),
            pe: dict(BASE=self.tmp, PEOPLE_DIR=mem / "people"),
            mi: dict(BASE=self.tmp, MEM_DIR=mem, SOUL_DIR=soul, SKILLS_DIR=soul / "skills",
                     PEOPLE_DIR=mem / "people", VECTORS_DIR=mem / ".vectors",
                     INDEX_JSON=mem / ".vectors" / "index.json", INDEX_MD=mem / "INDEX.md"),
        }
        for module, attrs in patch.items():
            for k, val in attrs.items():
                self._orig.append((module, k, getattr(module, k)))
                setattr(module, k, val)
        mi._CACHE = {"mtime": None, "index": None}
        self._orig.append((mi, "embed", mi.embed))
        mi.embed = lambda t: (_ for _ in ()).throw(ConnectionError("no ollama"))

    def tearDown(self) -> None:
        for module, k, val in reversed(self._orig):
            setattr(module, k, val)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _head(self) -> str:
        return _git(self.tmp, "rev-parse", "HEAD").stdout.strip()


class TestSelfGit(Base):
    def test_snapshot_commits_change(self):
        before = self._head()
        (self.tmp / "foo.py").write_text("x = 1\n", encoding="utf-8")
        sha = selfgit.snapshot("test change")
        self.assertIsNotNone(sha)
        self.assertNotEqual(before, self._head(), "коммит не создан")

    def test_snapshot_noop_when_clean(self):
        self.assertIsNone(selfgit.snapshot("nothing"), "коммит на чистом дереве")

    def test_changed_paths(self):
        (self.tmp / "bar.py").write_text("y = 2\n", encoding="utf-8")
        self.assertIn("bar.py", selfgit.changed_paths())


class TestFormerSecretGuard(Base):
    def test_names_do_not_block_shell(self):
        for cmd in ("echo x > .env", "rm praxis.session", "cat memory/llm.json",
                    "grep KEY .env", "echo hi > note.txt"):
            self.assertFalse(agent._writes_secret(cmd), cmd)
            self.assertFalse(agent._touches_llm_config(cmd), cmd)


class TestAutocommit(Base):
    def test_core_edit_committed_and_journaled(self):
        before = self._head()
        (self.tmp / "agent_patch.py").write_text("# her edit\n", encoding="utf-8")
        agent._autocommit_self_edit()
        self.assertNotEqual(before, self._head(), "правка не закоммичена")
        jtext = "".join(p.read_text(encoding="utf-8") for p in (agent.JOURNAL_DIR).glob("*.md"))
        self.assertIn("изменила свой код", jtext, "правка core не отмечена в дневнике")

    def test_noop_when_clean(self):
        before = self._head()
        agent._autocommit_self_edit()
        self.assertEqual(before, self._head())


class TestWriteSkill(Base):
    def test_writes_skill_and_index(self):
        before = self._head()
        out = agent.tool_write_skill("Зеркало паузы", "Когда тяжело — сначала пауза, потом слово.")
        path = agent.SKILLS_DIR / "зеркало-паузы.md"
        self.assertTrue(path.exists(), "файл навыка не создан")
        self.assertIn("пауза", path.read_text(encoding="utf-8"))
        self.assertIn("зеркало-паузы.md", agent._read(agent.SKILLS_DIR / "INDEX.md"), "индекс не обновлён")
        self.assertNotEqual(before, self._head(), "навык не закоммичен")
        self.assertIn("навык", out.lower())


class TestRestartSelf(Base):
    def test_journals_and_schedules_exit(self):
        calls = []
        orig = agent._schedule_exit
        agent._schedule_exit = lambda: calls.append(1)
        try:
            out = agent.tool_restart_self("после правки")
        finally:
            agent._schedule_exit = orig
        self.assertEqual(calls, [1], "выход не запланирован")
        self.assertIn("Перезапускаюсь", out)
        jtext = "".join(p.read_text(encoding="utf-8") for p in agent.JOURNAL_DIR.glob("*.md"))
        self.assertIn("restart", jtext.lower())


class TestShellWorkdir(Base):
    def test_missing_workdir_falls_back_to_base(self):
        missing = self.tmp / "vanished-workdir"
        agent.WORKDIR = str(missing)

        out = agent.tool_shell("pwd")

        self.assertEqual(out.strip(), str(self.tmp))
        self.assertFalse(missing.exists())


class TestCoreEditCheck(Base):
    def _journal(self) -> str:
        return "".join(p.read_text(encoding="utf-8") for p in agent.JOURNAL_DIR.glob("*.md"))

    def test_edits_core_detection(self):
        for cmd in ("echo x > agent.py", "sed -i s/a/b/ mtproto_runner.py", "cat a > b.py"):
            self.assertTrue(agent._edits_core(cmd), cmd)
        for cmd in ("cat agent.py", "echo x > note.md", "python agent.py", "ls *.py"):
            self.assertFalse(agent._edits_core(cmd), cmd)

    def test_danger_warns_and_proceeds(self):
        orig = agent._core_edit_verdict
        agent._core_edit_verdict = lambda c: ("danger", "массовое удаление")
        try:
            out = agent.tool_shell("printf danger-check > danger.py")
        finally:
            agent._core_edit_verdict = orig
        self.assertNotIn("остановила", out.lower())
        self.assertEqual((Path(agent.WORKDIR) / "danger.py").read_text(), "danger-check")
        self.assertIn("вопреки сильному предупреждению", self._journal())
        self.assertIn("массовое удаление", self._journal())

    def test_warn_proceeds_but_journals(self):
        orig = agent._core_edit_verdict
        agent._core_edit_verdict = lambda c: ("warn", "рискованно")
        try:
            agent.tool_shell("echo x > agent.py")  # bash может не сработать на Windows — журнал пишется до него
        finally:
            agent._core_edit_verdict = orig
        self.assertIn("проверка предупредила", self._journal())

    def test_disabled_skips_check(self):
        import os
        orig = agent._core_edit_verdict
        agent._core_edit_verdict = lambda c: (_ for _ in ()).throw(AssertionError("проверка не должна вызываться"))
        os.environ["PRAXIS_CORE_EDIT_CHECK"] = "0"
        try:
            out = agent.tool_shell("echo x > agent.py")  # не должно бросить и не блок
            self.assertNotIn("остановила", out.lower())
        finally:
            os.environ.pop("PRAXIS_CORE_EDIT_CHECK", None)
            agent._core_edit_verdict = orig

    def test_fail_open_on_error(self):
        orig = agent._core_edit_verdict
        agent._core_edit_verdict = lambda c: (_ for _ in ()).throw(RuntimeError("модель легла"))
        try:
            out = agent.tool_shell("echo x > agent.py")  # ошибка проверки -> не блокируем
            self.assertNotIn("остановила", out.lower())
        finally:
            agent._core_edit_verdict = orig


class TestOwnerToolset(Base):
    def test_owner_tools_present(self):
        names = [t["name"] for t in agent.OWNER_TOOLS]
        for n in ("shell", "manage_room", "admit", "write_skill", "restart_self"):
            self.assertIn(n, names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
