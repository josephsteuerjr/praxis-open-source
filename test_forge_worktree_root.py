"""Задача с недостижимым корнем: не рождаться мёртвой, не врать о причине, закрываться.

Все три теста воспроизводят ровно то, что произошло 28.07.2026 с `code-c280f6e7` и
`code-f758ea57`: источник лежал в `workspace/` — каталоге, который стоит в `.gitignore`
репозитория. `git worktree add` делает чекаут HEAD, и такого пути в нём нет никогда.
Задача открывалась `active` с корнем `<worktree>/workspace/…`, умирала первым же шагом,
и получала «корень задачи пропал» — про то, чего не существовало ни секунды.
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import forge


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, timeout=60)


class WorktreeRootTest(unittest.TestCase):
    """Живой git: гипотеза про чекаут HEAD проверяется настоящим git, не заглушкой."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.repo = self.tmp / "repo"
        (self.repo / "src").mkdir(parents=True)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@t")
        _git(self.repo, "config", "user.name", "t")
        _git(self.repo, "config", "commit.gpgsign", "false")
        (self.repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
        # ровно как в живом репозитории: рабочий каталог не отслеживается
        (self.repo / ".gitignore").write_text("workspace/\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "base")
        self.ignored = self.repo / "workspace" / "project"
        self.ignored.mkdir(parents=True)
        (self.ignored / "main.py").write_text("print(1)\n", encoding="utf-8")

        self._base, self._tasks = forge.BASE, forge.TASKS_DIR
        forge.BASE = self.tmp / "base"
        forge.TASKS_DIR = self.tmp / "base" / "tasks"
        forge.TASKS_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        forge.BASE, forge.TASKS_DIR = self._base, self._tasks

    # ── дефект 1: задача не должна рождаться с адресом, которого нет ──────────
    def test_a_source_git_does_not_track_never_becomes_a_worktree_task(self):
        out = forge.start("привести в порядок", target=str(self.ignored), isolation="worktree")
        self.assertTrue(out.startswith("coding-задача "), out)
        task_id = out.split()[1].rstrip(":")
        task = forge.get(task_id)
        self.assertEqual(task["isolation"], "direct",
                         "путь вне git не может получить изоляцию через чекаут HEAD")
        self.assertEqual(Path(task["root"]).resolve(), self.ignored,
                         "корень direct-задачи — сам источник")
        self.assertTrue(Path(task["root"]).is_dir(), "корень обязан существовать при рождении")
        self.assertFalse(task["worktree_root"], "worktree не создавался — и не должен числиться")

    def test_b_the_substitution_is_never_silent(self):
        out = forge.start("цель", target=str(self.ignored), isolation="worktree")
        task_id = out.split()[1].rstrip(":")
        note = forge.get(task_id)["isolation_note"]
        self.assertIn(".gitignore", note, "причина обязана называть настоящий механизм")
        self.assertIn("прямо в источнике", note, "последствие обязано быть названо")
        self.assertIn("изоляция", out.lower(),
                      "ориентировка — первое, что она читает; подмена режима стоит там")

    def test_c_a_tracked_subdirectory_still_gets_its_worktree(self):
        """Починка не имеет права отобрать изоляцию у тех, кому она работала."""
        out = forge.start("цель", target=str(self.repo / "src"), isolation="worktree")
        task_id = out.split()[1].rstrip(":")
        task = forge.get(task_id)
        self.assertEqual(task["isolation"], "git-worktree", task.get("isolation_note"))
        self.assertTrue(Path(task["root"]).is_dir())
        self.assertTrue((Path(task["root"]) / "a.py").is_file(),
                        "в чекауте должен лежать отслеживаемый файл")
        self.assertFalse(task["isolation_note"], "подмены не было — и оговорки быть не должно")

    # ── дефект 2: сообщение обязано знать причину, о которой сообщает ─────────
    def test_d_a_root_that_never_existed_is_not_called_a_disappearance(self):
        task_id = "code-fixture1"
        wt = self.tmp / "wt"
        (wt / "src").mkdir(parents=True)          # worktree цел, подкаталога в нём нет
        (forge.TASKS_DIR / task_id).mkdir(parents=True, exist_ok=True)
        (forge.TASKS_DIR / task_id / "task.json").write_text(json.dumps({
            "id": task_id, "goal": "g", "status": "active",
            "root": str(wt / "workspace" / "project"), "worktree_root": str(wt),
        }), encoding="utf-8")
        _task, root, err = forge._task_root(task_id)
        self.assertIsNone(root)
        self.assertNotIn("пропал", err, f"пропажи не было — worktree на месте: {err}")
        self.assertIn("не существует", err)
        self.assertIn("abandon", err, "тупик обязан называть выход из себя")

    def test_e_a_real_disappearance_is_still_called_one(self):
        task_id = "code-fixture2"
        (forge.TASKS_DIR / task_id).mkdir(parents=True, exist_ok=True)
        (forge.TASKS_DIR / task_id / "task.json").write_text(json.dumps({
            "id": task_id, "goal": "g", "status": "active",
            "root": str(self.tmp / "ушло"), "worktree_root": "",
        }), encoding="utf-8")
        _task, root, err = forge._task_root(task_id)
        self.assertIsNone(root)
        self.assertIn("пропал", err)

    # ── дефект 3: из тупика обязан быть выход ────────────────────────────────
    def test_f_an_unreachable_task_can_finally_be_closed(self):
        task_id = "code-fixture3"
        wt = self.tmp / "wt2"
        wt.mkdir(parents=True)
        (forge.TASKS_DIR / task_id).mkdir(parents=True, exist_ok=True)
        (forge.TASKS_DIR / task_id / "task.json").write_text(json.dumps({
            "id": task_id, "goal": "g", "status": "lost",
            "root": str(wt / "workspace"), "worktree_root": str(wt),
        }), encoding="utf-8")

        self.assertIn("Нужна причина", forge.abandon(task_id, reason=""),
                      "закрытие навсегда не делается молча")

        out = forge.abandon(task_id, reason="корня не существовало; восстанавливать нечего",
                            checked="coding_session(status)")
        self.assertIn("оставлена", out)
        task = forge.get(task_id)
        self.assertEqual(task["status"], "abandoned")
        self.assertEqual(task["abandoned_from"], "lost",
                         "чем задача была до закрытия — часть правды о ней")
        self.assertTrue(task["finished"])
        self.assertIn("не существует", task["abandon_root_state"])
        self.assertIn("уже закрыта", forge.abandon(task_id, reason="ещё раз"),
                      "повторное закрытие не переписывает историю")

    def test_g_abandon_never_touches_the_branch_or_the_tree(self):
        out = forge.start("цель", target=str(self.repo / "src"), isolation="worktree")
        task_id = out.split()[1].rstrip(":")
        task = forge.get(task_id)
        wt = Path(task["worktree_root"])
        self.assertTrue(wt.is_dir())
        forge.abandon(task_id, reason="передумала")
        self.assertTrue(wt.is_dir(), "abandon не убирает worktree — это не finish")
        branches = _git(self.repo, "branch", "--list", task["branch"]).stdout
        self.assertIn(task["branch"], branches, "ветка тоже остаётся: работа не выброшена")


if __name__ == "__main__":
    unittest.main()
