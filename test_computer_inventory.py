import json
import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import computer_inventory


class ComputerInventoryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_base = os.environ.get("PRAXIS_BASE")
        os.environ["PRAXIS_BASE"] = self.temp.name

    def tearDown(self):
        if self.old_base is None:
            os.environ.pop("PRAXIS_BASE", None)
        else:
            os.environ["PRAXIS_BASE"] = self.old_base
        self.temp.cleanup()

    def test_refresh_writes_timestamped_current_and_markdown_views(self):
        payload = {
            "schema": "praxis.computer.inventory.payload.v1",
            "captured_at": "2026-07-13T04:30:00Z",
            "hostname": "LOVE",
            "user": "yegor",
            "os": {"caption": "Windows 11 Pro", "version": "10.0", "build": "26200",
                   "architecture": "64-bit", "last_boot": "2026-07-13T02:38:16Z"},
            "machine": {"manufacturer": "HONOR", "model": "BMH-WCX9",
                        "memory_bytes": 16 * 1024**3},
            "volumes": [{"name": "C:", "filesystem": "NTFS", "size_bytes": 512 * 1024**3,
                         "free_bytes": 280 * 1024**3}],
            "tools": [{"name": "git", "path": r"C:\Program Files\Git\git.exe", "version": "2.53"}],
            "apps": [{"name": "Visual Studio Code", "version": "1.0", "publisher": "Microsoft"}],
            "known_roots": [r"C:\Users\example\Downloads"],
            "project_roots": [r"C:\Users\example\Downloads\Praxis"],
        }
        status = {"ok": True, "identity": {"kind": "interactive", "integrity": "high",
                                               "elevated": True},
                  "manifest": {"execution_contexts": {"interactive": {"available": True}}}}
        process = {"ok": True, "stdout_tail": json.dumps(payload)}
        observed = dt.datetime(2026, 7, 13, 4, 30, tzinfo=dt.timezone.utc)
        with mock.patch("computer_inventory.body_client.device_id", return_value="love"), \
             mock.patch("computer_inventory.body_client.status_probe", return_value=status), \
             mock.patch("computer_inventory.body_client.run_argv", return_value=process) as run, \
             mock.patch("computer_inventory._utc_now", return_value=observed), \
             mock.patch("computer_memory.render_map"), \
             mock.patch("memory_catalog.rebuild"):
            result = computer_inventory.refresh()

        self.assertTrue(result["ok"])
        self.assertEqual(result["apps"], 1)
        self.assertEqual(run.call_args.kwargs["tail"], 1_000_000)
        base = Path(self.temp.name) / "memory" / "computer" / "inventory" / "love"
        self.assertTrue((base / "20260713T043000Z.json").is_file())
        current = json.loads((base / "CURRENT.json").read_text(encoding="utf-8"))
        self.assertEqual(current["payload"]["hostname"], "LOVE")
        self.assertEqual(current["observed_at"], "2026-07-13T04:30:00.000000Z")
        card = (base / "CURRENT.md").read_text(encoding="utf-8")
        self.assertIn("Visual Studio Code", card)
        self.assertIn(r"C:\Users\example\Downloads\Praxis", card)

    def test_script_handles_cim_datetime_and_sorts_apps_before_projection(self):
        self.assertIn("[Console]::OutputEncoding=$utf8", computer_inventory._SCRIPT)
        self.assertIn("$os.LastBootUpTime -is [datetime]", computer_inventory._SCRIPT)
        self.assertIn("Sort-Object DisplayName,DisplayVersion -Unique", computer_inventory._SCRIPT)
        self.assertNotIn("Sort-Object name -Unique", computer_inventory._SCRIPT)

    def test_due_uses_server_observation_not_a_skewed_windows_clock(self):
        directory = Path(self.temp.name) / "memory/computer/inventory/love"
        directory.mkdir(parents=True)
        (directory / "CURRENT.json").write_text(json.dumps({
            "captured_at": "2099-01-01T00:00:00Z",
            "observed_at": "2026-07-13T04:30:00Z",
        }), encoding="utf-8")
        self.assertTrue(computer_inventory.due(
            "love", now=dt.datetime(2026, 7, 14, 4, 30, tzinfo=dt.timezone.utc),
        ))

    def test_zero_interval_disables_inventory(self):
        with mock.patch.dict(os.environ, {"PRAXIS_COMPUTER_INVENTORY_HOURS": "0"}):
            self.assertFalse(computer_inventory.due("missing"))

    def test_same_tick_snapshot_name_does_not_overwrite_history(self):
        directory = Path(self.temp.name) / "memory/computer/inventory/love"
        directory.mkdir(parents=True)
        observed = "2026-07-13T04:30:00.000000Z"
        first = computer_inventory._snapshot_name(observed, "first\n", directory)
        (directory / first).write_text("first\n", encoding="utf-8")
        second = computer_inventory._snapshot_name(observed, "second\n", directory)
        self.assertNotEqual(first, second)
        self.assertGreater(second, first)
        self.assertTrue(second.endswith(".json"))


if __name__ == "__main__":
    unittest.main()
