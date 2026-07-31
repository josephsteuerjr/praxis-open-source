import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import computer_memory


class ComputerMemoryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_base = os.environ.get("PRAXIS_BASE")
        os.environ["PRAXIS_BASE"] = self.temp.name
        self.task = {
            "id": "wcode-evidence1", "device_id": "windows-pc",
            "root": r"C:\work\demo", "goal": "inspect and test the project",
        }

    def tearDown(self):
        if self.old_base is None:
            os.environ.pop("PRAXIS_BASE", None)
        else:
            os.environ["PRAXIS_BASE"] = self.old_base
        self.temp.cleanup()

    def test_receipts_are_structured_maps_are_grepable_and_sql_is_rebuildable(self):
        row = computer_memory.record(
            self.task, "fs.read",
            {"path": r"C:\work\demo\secret.txt", "token": "must-not-survive",
             "content": "request-payload"},
            "interactive",
            {"ok": True, "request_id": "req-1", "operation_id": "op-1",
             "text": "private-file-content", "size": 20, "sha256": "a" * 64},
        )
        self.assertIsNotNone(row)
        base = Path(self.temp.name)
        event_text = next((base / "memory" / "computer" / "events").glob("*.jsonl")).read_text(
            encoding="utf-8")
        receipt_text = (base / row["receipt"]).read_text(encoding="utf-8")
        self.assertNotIn("must-not-survive", receipt_text)
        self.assertNotIn("private-file-content", receipt_text)
        self.assertNotIn("request-payload", receipt_text)
        self.assertNotIn("private-file-content", event_text)
        self.assertIn('"sha256"', receipt_text)

        task_map = base / "memory" / "computer" / "tasks" / "wcode-evidence1.md"
        device_map = base / "memory" / "computer" / "devices" / "windows-pc.md"
        global_map = base / "memory" / "computer" / "MAP.md"
        self.assertTrue(task_map.is_file())
        self.assertTrue(device_map.is_file())
        self.assertTrue(global_map.is_file())
        self.assertIn("fs.read", task_map.read_text(encoding="utf-8"))
        self.assertEqual(computer_memory.search(task_id="wcode-evidence1")[0]["id"], row["id"])

        running = computer_memory.record(
            self.task, "process.status", {"operation_id": "op-run"}, "interactive",
            {"ok": True, "status": "running", "operation_id": "op-run"},
        )
        self.assertIsNone(running)
        terminal = computer_memory.record(
            self.task, "process.status", {"operation_id": "op-run"}, "interactive",
            {"ok": True, "status": "succeeded", "operation_id": "op-run",
             "stdout_tail": "process-output", "result": {"exit_code": 0}},
        )
        terminal_receipt = json.loads((base / terminal["receipt"]).read_text(encoding="utf-8"))
        stdout_meta = terminal_receipt["result"]["stdout_tail"]
        self.assertNotIn("process-output", json.dumps(terminal_receipt))
        self.assertEqual((base / stdout_meta["log"]).read_text(encoding="utf-8"), "process-output")

        projections = {
            path: path.read_bytes() for path in (task_map, device_map, global_map)
        }
        rebuilt = computer_memory.rebuild_index()
        self.assertEqual(rebuilt["observations"], 2)
        self.assertEqual(len(computer_memory.search(task_id="wcode-evidence1")), 2)
        self.assertEqual(projections, {path: path.read_bytes() for path in projections})

        database = base / rebuilt["database"]
        for suffix in ("", "-wal", "-shm"):
            Path(str(database) + suffix).unlink(missing_ok=True)
        self.assertEqual(
            len(computer_memory.search(task_id="wcode-evidence1")), 2,
            "a deleted disposable SQL index must rebuild transparently from JSONL",
        )
        self.assertEqual(projections, {path: path.read_bytes() for path in projections})

    def test_recent_artifact_evidence_is_bounded_and_ignores_non_artifacts(self):
        self.assertEqual(computer_memory.recent_artifact_evidence(), [])
        computer_memory.record(
            self.task, "fs.stat", {"path": self.task["root"]}, "interactive",
            {"ok": True, "kind": "directory", "sha256": "b" * 64},
        )
        self.assertEqual(computer_memory.recent_artifact_evidence(), [])

        row = computer_memory.record(
            {**self.task, "id": "run-direct-artifact"}, "artifact.fetch", {}, "interactive",
            {"ok": True, "artifact": {"name": "report.txt", "size": 7,
                                      "sha256": "a" * 64}},
        )
        evidence = computer_memory.recent_artifact_evidence(limit=1)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["artifacts"][0]["name"], "report.txt")
        self.assertEqual(evidence[0]["artifacts"][0]["sha256"], "a" * 64)
        self.assertEqual(evidence[0]["receipt"], row["receipt"])
        self.assertNotIn("delivery", evidence[0])

    def test_finish_promotes_one_idempotent_life_episode(self):
        computer_memory.record(
            self.task, "fs.stat", {"path": self.task["root"]}, "interactive",
            {"ok": True, "kind": "directory", "request_id": "r", "operation_id": "o"},
        )
        with mock.patch("memory_life.append_event", return_value={"id": "evt-computer-1"}) as append:
            first = computer_memory.finish_task(self.task, outcome="done", checked="tests green")
            second = computer_memory.finish_task(self.task, outcome="done", checked="tests green")
        self.assertEqual(first["life_event_id"], "evt-computer-1")
        self.assertEqual(first, second)
        append.assert_called_once()
        self.assertIn("memory/computer/tasks/wcode-evidence1.md", first["map"])

    def test_non_forge_run_receipts_stay_inside_the_run(self):
        base = Path(self.temp.name)
        run_id = "run-20260713T120000000000Z-test0001"
        run_dir = base / "memory" / "runs" / "2026-07" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text("{}\n", encoding="utf-8")

        row = computer_memory.record(
            {"task_id": run_id, "run_id": run_id, "device_id": "windows-pc",
             "goal": "look around"},
            "desktop.window.list", {"visible_only": True}, "interactive",
            {"ok": True, "windows": []},
        )

        self.assertIsNotNone(row)
        self.assertTrue(str(row["receipt"]).startswith(
            f"memory/runs/2026-07/{run_id}/computer/receipts/"))
        self.assertFalse((base / "memory" / ".forge" / "tasks" / run_id).exists())

    def test_disposable_projection_failure_does_not_fail_canonical_record(self):
        with self.assertLogs("praxis.computer-memory", level="WARNING") as logs:
            with mock.patch("computer_memory._index", side_effect=RuntimeError("projection bug")):
                row = computer_memory.record(
                    self.task, "fs.stat", {"path": self.task["root"]}, "interactive",
                    {"ok": True, "kind": "directory", "request_id": "r", "operation_id": "o"},
                )

        self.assertIsNotNone(row)
        self.assertIn("canonical event is durable", "\n".join(logs.output))
        base = Path(self.temp.name)
        self.assertTrue((base / row["receipt"]).is_file())
        event_file = next((base / "memory/computer/events").glob("*.jsonl"))
        self.assertIn(row["id"], event_file.read_text(encoding="utf-8"))
        rebuilt = computer_memory.rebuild_index()
        self.assertEqual(rebuilt["observations"], 1)
        self.assertEqual(computer_memory.search(task_id=self.task["id"])[0]["id"], row["id"])


if __name__ == "__main__":
    unittest.main()
