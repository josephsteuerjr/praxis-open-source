"""PASS 24 server-side contract: one Forge, Windows execution backend."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class WindowsBodyRoutingCase(unittest.TestCase):
    def test_windows_task_prefix_and_owner_schema(self):
        import agent
        self.assertTrue(agent._is_windows_task("wcode-abc123"))
        self.assertFalse(agent._is_windows_task("hcode-abc123"))
        coding = next(tool for tool in agent.OWNER_TOOLS if tool["name"] == "coding_session")
        self.assertIn("windows", coding["input_schema"]["properties"]["scope"]["enum"])

    def test_windows_task_lives_in_canonical_forge(self):
        import forge
        old = forge.BASE, forge.STATE_DIR, forge.TASKS_DIR
        with tempfile.TemporaryDirectory() as tmp:
            forge.BASE = Path(tmp)
            forge.STATE_DIR = Path(tmp) / "state"
            forge.TASKS_DIR = forge.STATE_DIR / "tasks"
            with mock.patch.object(forge.body_client, "available", return_value=True), \
                 mock.patch.object(forge.body_client, "device_id", return_value="windows-pc"), \
                 mock.patch.object(forge.body_client, "workspace_inspect_result", return_value={
                     "ok": True, "text": "Windows device: windows-pc\nroot: C:\\Work",
                     "head": "abc123", "git_root": "C:\\Work",
                 }):
                out = forge.start_windows("build locally", "C:\\Work")
            task_id = out.split(":", 1)[0].replace("coding-задача ", "")
            task = forge.get(task_id)
            self.assertEqual(task["scope"], "windows")
            self.assertEqual(task["backend"], "praxis.body.v1")
            self.assertEqual(task["device_id"], "windows-pc")
            self.assertTrue((forge.TASKS_DIR / task_id / "task.json").is_file())
        forge.BASE, forge.STATE_DIR, forge.TASKS_DIR = old

    def test_windows_primitives_route_without_resolving_path_as_linux(self):
        import forge
        old = forge.BASE, forge.STATE_DIR, forge.TASKS_DIR
        with tempfile.TemporaryDirectory() as tmp:
            forge.BASE = Path(tmp)
            forge.STATE_DIR = Path(tmp) / "state"
            forge.TASKS_DIR = forge.STATE_DIR / "tasks"
            with mock.patch.object(forge.body_client, "available", return_value=True), \
                 mock.patch.object(forge.body_client, "device_id", return_value="windows-pc"), \
                 mock.patch.object(forge.body_client, "workspace_inspect_result",
                                   return_value={"ok": True, "text": "orientation"}):
                out = forge.start_windows("work", "D:\\Project")
            task_id = out.split(":", 1)[0].replace("coding-задача ", "")
            with mock.patch.object(forge.body_client, "workspace_inspect", return_value="read") as inspect, \
                 mock.patch.object(forge.body_client, "workspace_edit", return_value="edited") as edit, \
                 mock.patch.object(forge.body_client, "run", return_value="ran") as run, \
                 mock.patch.object(forge.body_client, "process", return_value="op") as process, \
                 mock.patch.object(forge.body_client, "checkpoint", return_value="checkpoint") as checkpoint:
                self.assertEqual(forge.inspect(task_id, "read", path="x"), "read")
                self.assertEqual(forge.edit(task_id, "write", path="x", content="y"), "edited")
                self.assertEqual(forge.run(task_id, "git status"), "ran")
                self.assertEqual(forge.process(task_id, "start", command="cargo test"), "op")
                self.assertEqual(forge.checkpoint(task_id, "cp"), "checkpoint")
            self.assertEqual(inspect.call_args.args[0], "D:\\Project")
            self.assertEqual(edit.call_args.args[0], "D:\\Project")
            self.assertEqual(run.call_args.args[0], "D:\\Project")
            self.assertEqual(process.call_args.args[0], "D:\\Project")
            self.assertEqual(checkpoint.call_args.args[0], "D:\\Project")
        forge.BASE, forge.STATE_DIR, forge.TASKS_DIR = old

    def test_windows_verification_runner_uses_body_not_local_shell(self):
        import forge_verify
        state = {"checks": {}}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("body_client.run_result", return_value={
                 "ok": True, "status": "succeeded", "body": "green",
                 "result": {"exit_code": 0, "stdout_log": "remote.log"},
             }) as run:
            result = forge_verify._run_one(
                0, {"id": "test", "command": "cargo test", "cwd": "."},
                Path("D:\\Project"), Path(tmp), 60, state,
                __import__("threading").Lock(), "windows", "verify-one",
            )
        self.assertEqual(result["status"], "passed")
        run.assert_called_once_with("D:\\Project", "cargo test", cwd=".", timeout=60)


if __name__ == "__main__":
    unittest.main()
