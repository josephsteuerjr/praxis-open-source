from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from telegram_membership import MembershipLedger


class MembershipLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "membership.jsonl"
        self.ledger = MembershipLedger(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_intent_is_durable_and_duplicate_active_request_reuses_it(self):
        first = self.ledger.begin("join", "https://t.me/+AbCdEfGh_123", 101)
        second = MembershipLedger(self.path).begin(
            "join", "https://t.me/+AbCdEfGh_123", "telegram:101",
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["status"], "intent")
        self.assertEqual(second["principal_id"], "101")

    def test_acceptance_survives_restart_until_local_projection_is_applied(self):
        tx = self.ledger.begin("leave", "@kraken_lab", 101)
        self.ledger.prepared(tx["id"], {"chat_id": -1000000000077, "title": "Kraken"})
        self.ledger.accepted(tx["id"], {
            "status": "left", "chat_id": -1000000000077,
            "entity_id": 77, "title": "Kraken",
        })
        pending = MembershipLedger(self.path).pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "accepted")
        self.assertEqual(pending[0]["result"]["chat_id"], -1000000000077)
        MembershipLedger(self.path).applied(tx["id"])
        self.assertEqual(MembershipLedger(self.path).pending(), [])

    def test_torn_and_forged_rows_never_override_valid_state(self):
        tx = self.ledger.begin("join", "@kraken_lab", 101)
        with self.path.open("a", encoding="utf-8", newline="") as handle:
            handle.write('{"schema":"praxis.telegram.membership.v1"')
        # A valid append isolates the torn evidence instead of joining onto it.
        self.ledger.in_doubt(tx["id"], "transport reset")
        forged = {
            "schema": "praxis.telegram.membership.v1",
            "event_id": "membership-event-" + "a" * 32,
            "tx_id": tx["id"],
            "at": "2026-07-14T00:00:00.000Z",
            "kind": "applied",
            "action": "join",
            "target": "@kraken_lab",
            "principal_id": "not-a-user",
            "data": {},
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(forged) + "\n")
        state = MembershipLedger(self.path).get(tx["id"])
        self.assertEqual(state["status"], "in_doubt")
        self.assertEqual(state["error"], "transport reset")

    def test_praxis_self_is_preserved_as_a_real_actor(self):
        tx = self.ledger.begin("join", "@kraken_lab", "praxis:self")
        self.assertEqual(tx["principal_id"], "praxis:self")
        self.assertEqual(MembershipLedger(self.path).get(tx["id"])["principal_id"], "praxis:self")

    def test_principal_must_be_praxis_or_positive_numeric_user_id(self):
        for bad in ("", "owner", "-100123", "@name"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.ledger.begin("join", "@kraken_lab", bad)

    def test_acceptance_is_required_before_local_projection_is_terminal(self):
        tx = self.ledger.begin("join", "@kraken_lab", 101)
        with self.assertRaisesRegex(ValueError, "intent -> applied"):
            self.ledger.applied(tx["id"])
        self.assertEqual(self.ledger.get(tx["id"])["status"], "intent")

        self.ledger.accepted(tx["id"], {
            "status": "joined", "chat_id": -10077, "title": "Kraken",
        })
        with self.assertRaisesRegex(ValueError, "accepted -> prepared"):
            self.ledger.prepared(tx["id"], {"chat_id": -10088})
        self.ledger.applied(tx["id"])
        self.assertEqual(self.ledger.get(tx["id"])["status"], "applied")


if __name__ == "__main__":
    unittest.main()
