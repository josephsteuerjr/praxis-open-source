"""Completion gate for PASS 23.1 semantic/test/swarm/learning runtime."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import forge
import forge_intelligence
import forge_learning
import forge_swarm
import forge_verify


class CompleteForgeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        (self.project / "app.py").write_text(
            "def answer():\n    return 41\n\nclass Engine:\n    def run(self):\n        return answer()\n",
            encoding="utf-8",
        )
        (self.project / "consumer.py").write_text(
            "from app import answer\n\ndef render():\n    return str(answer())\n", encoding="utf-8"
        )
        (self.project / "test_app.py").write_text(
            "import unittest\nimport app\n\nclass AppCase(unittest.TestCase):\n"
            "    def test_answer(self): self.assertEqual(app.answer(), 41)\n",
            encoding="utf-8",
        )
        self.old = forge.BASE, forge.STATE_DIR, forge.TASKS_DIR
        forge.BASE = self.base
        forge.STATE_DIR = self.base / "state"
        forge.TASKS_DIR = forge.STATE_DIR / "tasks"

    def tearDown(self):
        forge.BASE, forge.STATE_DIR, forge.TASKS_DIR = self.old
        self.tmp.cleanup()

    def _start(self) -> str:
        out = forge.start("change answer without breaking render", str(self.project), isolation="direct")
        return out.split(":", 1)[0].replace("coding-задача ", "").strip()

    def test_semantic_symbols_references_diagnostics_and_impact(self):
        model = forge_intelligence.project_model(self.project)
        self.assertEqual(model["languages"].get("python"), 3)
        syms = forge_intelligence.symbols(self.project, query="answer")
        self.assertTrue(any(row["name"] == "answer" for row in syms["symbols"]))
        refs = forge_intelligence.references(self.project, "answer")
        self.assertTrue(any(row["path"] == "consumer.py" for row in refs["references"]))
        self.assertTrue(forge_intelligence.diagnostics(self.project)["ok"])
        (self.project / "compiled-tool").write_bytes(b"\x7fELF\x00" + b"x" * 400)
        (self.project / "runtime.log").write_text("noise\n" * 5000, encoding="utf-8")
        overview = forge_intelligence.project_overview(self.project)
        self.assertIn("app.py", [row["path"] for row in overview["hotspots"]])
        self.assertNotIn("compiled-tool", [row["path"] for row in overview["hotspots"]])
        self.assertNotIn("runtime.log", [row["path"] for row in overview["hotspots"]])
        affected = forge_intelligence.impact(self.project, ["app.py"])
        self.assertIn("consumer.py", affected["impacted"])
        self.assertIn("test_app.py", affected["tests"])

    def test_task_inspect_exposes_normalized_intelligence(self):
        task_id = self._start()
        self.assertIn('"languages"', forge.inspect(task_id, "model"))
        self.assertIn('"answer"', forge.inspect(task_id, "symbols", query="answer"))
        self.assertIn("consumer.py", forge.inspect(task_id, "references", query="answer"))
        self.assertIn('"ok": true', forge.inspect(task_id, "diagnostics").lower())
        self.assertIn('"checks"', forge.inspect(task_id, "checks"))
        self.assertIn('"central_modules"', forge.inspect(task_id, "overview"))
        self.assertIn('"ready_for_human_review"', forge.inspect(task_id, "review"))

    def test_discovery_uses_the_interpreter_that_exists_on_the_execution_host(self):
        def available(name: str):
            return "/usr/bin/python3" if name == "python3" else None

        with mock.patch.object(forge_intelligence.shutil, "which", side_effect=available):
            checks = forge_intelligence.discovered_checks(self.project)
            plan = forge_intelligence.verification_plan(self.project)
        python_checks = [row["command"] for row in checks + plan["checks"]
                         if "unittest" in row["command"] or "compile" in row["command"]]
        self.assertTrue(python_checks)
        self.assertTrue(all(command.startswith("python3 -m ") for command in python_checks),
                        python_checks)

    def test_verification_plan_and_detached_request(self):
        task_id = self._start()
        plan = json.loads(forge.verify(task_id, "plan"))
        self.assertTrue(plan["checks"])
        with mock.patch.object(forge, "_spawn_runner", return_value=4242):
            out = forge.verify(task_id, "start", commands="python -m py_compile app.py\npython -m unittest -q test_app")
        self.assertIn("Стартовала матрица", out)
        units = list((forge._task_dir(task_id) / "verifications").glob("*/request.json"))
        request = json.loads(units[0].read_text(encoding="utf-8"))
        self.assertEqual(len(request["checks"]), 2)
        self.assertEqual(request["supervisor_pid"], 4242)

    def test_verification_runner_keeps_per_check_exit_and_logs(self):
        directory = self.base / "matrix"
        request = {
            "root": str(self.project), "max_parallel": 2, "timeout": 20,
            "checks": [
                {"id": "green", "command": "python -c \"print(6*7)\"", "kind": "test"},
                {"id": "red", "command": "python -c \"raise SystemExit(3)\"", "kind": "test"},
            ],
        }
        request_path = directory / "request.json"
        directory.mkdir()
        request_path.write_text(json.dumps(request), encoding="utf-8")
        self.assertEqual(forge_verify.run(request_path), 1)
        result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual({row["id"]: row["exit"] for row in result["checks"]},
                         {"green": 0, "red": 3})
        self.assertIn("42", (directory / "logs" / "01-green.log").read_text(encoding="utf-8"))

    def test_swarm_dag_mailbox_and_advisory_claim_conflict(self):
        task_id = self._start()
        plan = json.dumps({"nodes": [
            {"id": "map", "role": "scout", "brief": "map seams", "owns": ["app.py"]},
            {"id": "build", "role": "worker", "brief": "implement", "deps": ["map"]},
        ]})
        self.assertIn("map [pending]", forge.swarm(task_id, "plan", plan=plan, max_parallel=2))
        with mock.patch.object(forge, "agent", return_value="Порождён agent-a1b2c3d4 (scout, pid 1)."):
            started = forge.swarm(task_id, "start")
        self.assertIn("map [running]", started)
        first = forge.swarm(task_id, "signal", node_id="map", kind="claim", files=["app.py"])
        second = forge.swarm(task_id, "signal", node_id="build", kind="claim", files=["app.py"])
        self.assertIn('"conflicts": []', first)
        self.assertIn('"owner": "map"', second)
        self.assertIn("claim", forge.swarm(task_id, "mailbox"))

    def test_evidence_lesson_is_recalled_by_similar_task(self):
        task_id = self._start()
        recorded = forge.learn(task_id, "record", lesson="Run impacted unittest before the full gate",
                               regression="python -m unittest -q test_app")
        lesson_id = json.loads(recorded)["id"]
        recalled = forge.learn(task_id, "recall", query="answer unittest")
        self.assertIn(lesson_id, recalled)
        rows = forge_learning.recall(forge.STATE_DIR, "answer unittest", self.project)
        self.assertTrue(rows)

    def test_owner_surface_contains_completed_engineering_cycle(self):
        import agent
        names = {tool["name"] for tool in agent.OWNER_TOOLS}
        self.assertTrue({"coding_verify", "coding_swarm", "coding_learn"} <= names)
        import forge_worker
        self.assertEqual(forge_worker.VERIFY_TOOL["name"], "verify")
        self.assertEqual(forge_worker.SIGNAL_TOOL["name"], "signal")


if __name__ == "__main__":
    unittest.main()
