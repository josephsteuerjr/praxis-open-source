"""PASS 23 Forge: task place, correct edits, processes and coding-agent wiring."""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import forge


class ForgeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        (self.project / "README.md").write_text("# demo\n", encoding="utf-8")
        (self.project / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        subprocess.run(["git", "-C", str(self.project), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.project), "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "commit", "-q", "-m", "init"], check=True)
        self.old_base, self.old_state, self.old_tasks = forge.BASE, forge.STATE_DIR, forge.TASKS_DIR
        forge.BASE = self.base
        forge.STATE_DIR = self.base / "state"
        forge.TASKS_DIR = forge.STATE_DIR / "tasks"

    def tearDown(self):
        forge.BASE, forge.STATE_DIR, forge.TASKS_DIR = self.old_base, self.old_state, self.old_tasks
        self.tmp.cleanup()

    def start(self, isolation: str = "direct") -> str:
        out = forge.start("make answer exact", target=str(self.project), isolation=isolation)
        m = re.search(r"coding-задача (code-[0-9a-f]+)", out)
        self.assertIsNotNone(m, out)
        return m.group(1)

    def test_start_orients_on_real_place_and_persists(self):
        task_id = self.start()
        task = forge.get(task_id)
        self.assertEqual(task["root"], str(self.project.resolve()))
        self.assertEqual(task["status"], "active")
        orientation = forge.inspect(task_id, "orientation")
        self.assertIn("Python 1", orientation)
        self.assertIn("README.md", orientation)
        self.assertIn("git status", orientation)
        self.assertIn(task_id, forge.list_tasks())

    def test_hash_aware_replace_rejects_stale_worker(self):
        task_id = self.start()
        read = forge.inspect(task_id, "read", path="app.py")
        old_hash = re.search(r"sha256=([0-9a-f]+)", read).group(1)
        out = forge.edit(task_id, "replace", path="app.py", old="return 41", new="return 42",
                         expected_sha256=old_hash)
        self.assertIn("sha256", out)
        stale = forge.edit(task_id, "replace", path="app.py", old="return 42", new="return 43",
                           expected_sha256=old_hash)
        self.assertIn("Конфликт версии", stale)
        self.assertIn("return 42", (self.project / "app.py").read_text(encoding="utf-8"))

    def test_concurrent_workers_serialize_mutation_and_one_rebases(self):
        task_id = self.start()
        read = forge.inspect(task_id, "read", path="app.py")
        old_hash = re.search(r"sha256=([0-9a-f]+)", read).group(1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(forge.edit, task_id, "replace", path="app.py", old="return 41",
                            new=f"return {value}", expected_sha256=old_hash)
                for value in (42, 43)
            ]
        results = [f.result() for f in futures]
        self.assertEqual(sum("Конфликт версии" in x for x in results), 1, results)
        self.assertEqual(sum("sha256" in x and "Конфликт" not in x for x in results), 1, results)
        final = (self.project / "app.py").read_text(encoding="utf-8")
        self.assertTrue("return 42" in final or "return 43" in final)

    def test_checked_multifile_patch_and_diff(self):
        task_id = self.start()
        patch = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def answer():
-    return 41
+    return 42
diff --git a/test_app.py b/test_app.py
new file mode 100644
--- /dev/null
+++ b/test_app.py
@@ -0,0 +1,5 @@
+import unittest
+import app
+class T(unittest.TestCase):
+    def test_answer(self): self.assertEqual(app.answer(), 42)
+
"""
        out = forge.edit(task_id, "patch", patch=patch)
        self.assertIn("Patch применён", out)
        diff = forge.inspect(task_id, "diff")
        self.assertIn("return 42", diff)
        self.assertIn("test_app.py", diff)

    def test_foreground_run_keeps_evidence(self):
        task_id = self.start()
        out = forge.run(task_id, f'"{sys.executable}" -c "print(6*7)"', timeout=20)
        self.assertIn("42", out)
        self.assertIn("[forge: ok", out)
        history = forge.inspect(task_id, "history", end=10)
        self.assertIn('"kind": "command"', history)

    def test_background_process_is_nonblocking_and_pollable(self):
        task_id = self.start()
        out = forge.process(task_id, "start",
                            command=f'"{sys.executable}" -c "print(\'FORGE_PROCESS_OK\')"',
                            name="probe")
        proc_id = re.search(r"(proc-[0-9a-f]+)", out).group(1)
        self.assertIn("Стартовал", out)
        polled = ""
        for _ in range(60):
            polled = forge.process(task_id, "poll", process_id=proc_id)
            if "FORGE_PROCESS_OK" in polled and '"status": "done"' in polled:
                break
            time.sleep(.05)
        self.assertIn("FORGE_PROCESS_OK", polled)
        self.assertIn('"status": "done"', polled)

    def test_background_timeout_reports_distinct_state(self):
        task_id = self.start()
        out = forge.process(
            task_id, "start",
            command=f'"{sys.executable}" -c "import time; print(\'BEGIN\', flush=True); time.sleep(20)"',
            name="timeout-probe", timeout=1)
        proc_id = re.search(r"(proc-[0-9a-f]+)", out).group(1)
        polled = ""
        for _ in range(80):
            polled = forge.process(task_id, "poll", process_id=proc_id)
            if '"status": "timed_out"' in polled:
                break
            time.sleep(.05)
        self.assertIn("BEGIN", polled)
        self.assertIn('"status": "timed_out"', polled)

    def test_agent_spawn_writes_independent_request(self):
        task_id = self.start()
        with mock.patch.object(forge, "_spawn_runner", return_value=424242):
            out = forge.agent(task_id, "spawn", brief="map the test seams", role="scout")
        agent_id = re.search(r"(agent-[0-9a-f]+)", out).group(1)
        req = forge._read_json(forge._unit_dir(task_id, "agents", agent_id) / "request.json")
        self.assertEqual(req["role"], "scout")
        self.assertEqual(req["root"], str(self.project.resolve()))
        self.assertEqual(req["brief"], "map the test seams")

    def test_worker_executes_real_tool_loop_and_writes_result(self):
        import forge_worker
        import llm

        task_id = self.start()
        agent_id = "agent-testloop"
        request_path = forge._unit_dir(task_id, "agents", agent_id) / "request.json"
        request = {"id": agent_id, "task_id": task_id, "goal": "make answer exact",
                   "root": str(self.project), "proposal_id": "", "role": "worker",
                   "brief": "change 41 to 42", "max_iters": 4, "created": forge._now()}
        forge._atomic_json(request_path, request)
        tool_call = llm.LLMResponse(
            blocks=[{"type": "tool_use", "id": "call-1", "name": "edit",
                     "input": {"action": "replace", "path": "app.py",
                               "old": "return 41", "new": "return 42"}}],
            stop_reason="tool_use", model="fake-coder")
        final = llm.LLMResponse(text="Changed app.py and verified the resulting diff.",
                                stop_reason="end_turn", model="fake-coder")
        with mock.patch.object(forge_worker.llm, "chat", side_effect=[tool_call, final]):
            code = forge_worker.run(request_path)
        self.assertEqual(code, 0)
        self.assertIn("return 42", (self.project / "app.py").read_text(encoding="utf-8"))
        result = forge._read_json(request_path.parent / "result.json")
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["tool_calls"], 1)
        self.assertIn("Changed app.py", result["result"])

    def test_finish_direct_task_builds_evidence_bundle(self):
        task_id = self.start()
        forge.edit(task_id, "replace", path="app.py", old="41", new="42")
        out = forge.finish(task_id, checked="python smoke", submit=False)
        self.assertIn(f"{task_id}: done", out)
        self.assertEqual(forge.get(task_id)["status"], "done")

    def test_generic_worktree_finishes_by_real_integration(self):
        task_id = self.start(isolation="worktree")
        task = forge.get(task_id)
        worktree = Path(task["worktree_root"])
        self.assertNotEqual(Path(task["root"]), self.project.resolve())
        forge.edit(task_id, "replace", path="app.py", old="41", new="42")
        out = forge.finish(task_id, title="make answer 42", checked="focused test", submit=True)
        self.assertIn("Интегрировано", out)
        self.assertIn("return 42", (self.project / "app.py").read_text(encoding="utf-8"))
        self.assertFalse(worktree.exists())


class ForgeAgentSurfaceCase(unittest.TestCase):
    def test_owner_surface_contains_complete_forge_control_plane(self):
        import agent
        names = {tool["name"] for tool in agent.OWNER_TOOLS}
        expected = {"coding_session", "coding_inspect", "coding_edit", "coding_run",
                    "coding_process", "coding_agent", "coding_checkpoint"}
        self.assertTrue(expected <= names)
        self.assertTrue(expected <= set(agent.TOOL_IMPL))

    def test_worker_and_scout_get_deliberately_different_roles(self):
        import forge_worker
        self.assertEqual(forge_worker.EDIT_TOOL["name"], "edit")
        self.assertEqual(forge_worker.DELEGATE_TOOL["name"], "delegate")


if __name__ == "__main__":
    unittest.main()
