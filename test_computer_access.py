import concurrent.futures
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import computer_access


class ComputerAccessLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {
            "PRAXIS_BASE": self.tmp.name,
            "PRAXIS_OWNER_ID": "101",
            "PRAXIS_TEST": "1",
        })
        self.env.start()
        self.catalog = mock.patch.object(computer_access, "_refresh_catalog")
        self.catalog.start()

    def tearDown(self):
        self.catalog.stop()
        self.env.stop()
        self.tmp.cleanup()

    @property
    def access_dir(self) -> Path:
        return Path(self.tmp.name) / "memory" / "access"

    def grant(self, target: str = "202", scopes=("computer.files",), **kwargs):
        return computer_access.change(
            "grant", target, name=kwargs.get("name", "A"), scopes=scopes, actor="101",
        )

    def test_only_owner_can_grant_revoke_and_list(self):
        denied = computer_access.change(
            "grant", "303", name="B", scopes=["computer.read"], actor="202",
        )
        self.assertEqual(denied["code"], "owner_only")
        self.assertIn("Только владелец", computer_access.describe(actor="202"))
        with self.assertRaises(PermissionError):
            computer_access.effective(actor="202")

        granted = self.grant()
        self.assertTrue(granted["ok"])
        self.assertIn("telegram:202", computer_access.describe(actor="telegram:101"))
        self.assertIn("telegram:202", computer_access.effective(actor="101"))

        denied_revoke = computer_access.change("revoke", "202", actor="202")
        self.assertEqual(denied_revoke["code"], "owner_only")
        self.assertTrue(computer_access.allowed("202", "computer.files"))
        self.assertTrue(computer_access.change("revoke", "202", actor="101")["ok"])
        self.assertFalse(computer_access.allowed("202", "computer.files"))

    def test_scopes_are_exact_and_unknown_scope_is_denied_even_to_owner(self):
        self.assertEqual(self.grant(scopes=[])["code"], "bad_scopes")
        self.assertEqual(
            self.grant(scopes=["computer.files", "computer.*"])["code"], "bad_scopes",
        )
        self.assertEqual(
            computer_access.change(
                "revoke", "202", scopes=["computer.files"], actor="101",
            )["code"],
            "bad_scopes",
        )
        self.assertFalse(computer_access.allowed("101", "computer.*"))
        self.assertFalse(computer_access.allowed("101", ""))

        self.assertTrue(self.grant(scopes=["computer.files"])["ok"])
        self.assertTrue(computer_access.allowed("202", "computer.files"))
        self.assertFalse(computer_access.allowed("202", "computer.read"))
        self.assertFalse(computer_access.allowed("202", "computer.process"))

    def test_non_user_ids_and_invalid_owner_configuration_fail_closed(self):
        self.assertEqual(self.grant(target="-100123")["code"], "bad_principal")
        with mock.patch.dict(os.environ, {"PRAXIS_OWNER_ID": "not-a-user"}):
            self.assertFalse(computer_access.allowed("not-a-user", "computer.read"))
            self.assertEqual(self.grant()["code"], "owner_only")
            self.assertIn("Только владелец", computer_access.describe(actor="not-a-user"))

    def test_torn_tail_is_preserved_but_cannot_consume_the_next_event(self):
        first = self.grant("202", name="First")
        path = self.access_dir / "events" / f"{first['at'][:10]}.jsonl"
        with path.open("ab") as handle:
            handle.write(b'{"schema":"praxis.access.v1","id":"torn')
        second = self.grant("303", name="Second")

        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIsInstance(json.loads(lines[0]), dict)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(lines[1])
        self.assertEqual(json.loads(lines[2])["id"], second["id"])
        state = computer_access.effective(actor="101")
        self.assertEqual(set(state), {"telegram:202", "telegram:303"})

    def test_malformed_or_non_owner_rows_cannot_broaden_authority(self):
        first = self.grant("202", scopes=["computer.read"])
        path = self.access_dir / "events" / f"{first['at'][:10]}.jsonl"
        forged = {
            "schema": "praxis.access.v1",
            "id": "access-" + "f" * 32,
            "at": first["at"],
            "action": "grant",
            "principal": "telegram:202",
            "name": "forged",
            "scopes": ["computer.process"],
            "by": "telegram:202",
        }
        unknown_scope = dict(forged, id="access-" + "e" * 32,
                             scopes=["computer.admin"], by="telegram:101")
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(forged, separators=(",", ":")) + "\n")
            handle.write(json.dumps(unknown_scope, separators=(",", ":")) + "\n")

        self.assertTrue(computer_access.allowed("202", "computer.read"))
        self.assertFalse(computer_access.allowed("202", "computer.process"))
        self.assertFalse(computer_access.allowed("202", "computer.admin"))

    def test_concurrent_threads_leave_complete_events_and_projection(self):
        targets = [str(2000 + index) for index in range(24)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(
                lambda target: self.grant(target, name=f"Person {target}"), targets,
            ))
        self.assertTrue(all(row.get("ok") for row in results))
        self.assertEqual(
            set(computer_access.effective(actor="101")),
            {f"telegram:{target}" for target in targets},
        )
        event_lines = []
        for path in (self.access_dir / "events").glob("*.jsonl"):
            event_lines.extend(path.read_text(encoding="utf-8").splitlines())
        self.assertEqual(len(event_lines), len(targets))
        self.assertTrue(all(isinstance(json.loads(line), dict) for line in event_lines))
        trust = (self.access_dir / "TRUST.md").read_text(encoding="utf-8")
        self.assertTrue(all(f"telegram:{target}" in trust for target in targets))

    def test_trust_markdown_is_deterministic_rebuildable_projection(self):
        self.assertTrue(self.grant("202", name="A`\nInjected\x00Name")["ok"])
        path = self.access_dir / "TRUST.md"
        rendered = path.read_bytes()
        path.unlink()
        rebuilt = computer_access.rebuild_projection()
        self.assertEqual(rebuilt.read_bytes(), rendered)
        text = rebuilt.read_text(encoding="utf-8")
        self.assertIn("A' Injected Name", text)
        self.assertNotIn("\x00", text)
        self.assertIn("Канонический источник: `events/*.jsonl`", text)


if __name__ == "__main__":
    unittest.main()
