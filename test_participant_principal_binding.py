from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent
import people


class ParticipantPrincipalBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="praxis_participant_binding_")
        self.root = Path(self.tmp.name)
        self.old_base = people.BASE
        self.old_people_dir = people.PEOPLE_DIR
        people.BASE = self.root
        people.PEOPLE_DIR = self.root / "memory" / "people"
        people.PEOPLE_DIR.mkdir(parents=True)

    def tearDown(self) -> None:
        people.BASE = self.old_base
        people.PEOPLE_DIR = self.old_people_dir
        self.tmp.cleanup()

    def _dossier(self, slug: str, name: str, principal_id: str, alias: str = "") -> None:
        body = {people.TELEGRAM_ID_KEY: principal_id}
        if alias:
            body[people.ALIASES_KEY] = alias
        people.write(slug, name, body)

    def test_binding_header_round_trips_and_duplicates_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            people.set_telegram_id("invalid", "Invalid", 0)
        people.set_telegram_id("alice", "Alice", "101")
        text = people.read_text("alice")
        self.assertIn("telegram_id: 101", text)
        self.assertEqual(people.read("alice")[1][people.TELEGRAM_ID_KEY], "101")
        self.assertEqual(people.telegram_id("alice"), "101")

        people.path_for("alice").write_text(
            "# Alice\n\ntelegram_id: 101\ntelegram_id: 202\n",
            encoding="utf-8",
        )
        self.assertEqual(people.telegram_id("alice"), "")
        self.assertEqual(people.slug_for_principal("101"), "")

    def test_duplicate_principal_across_dossiers_has_no_winner(self) -> None:
        self._dossier("alice", "Alice", "101")
        self._dossier("mallory", "Mallory", "101")
        self.assertEqual(people.slug_for_principal("101"), "")

    def test_spoofed_display_name_cannot_select_another_dossier(self) -> None:
        self._dossier("alice", "Alice", "101")
        self._dossier("victim", "Victim", "202")

        unbound = agent.ChannelContext(
            chat_id="dm-999", principal_id="999", is_dm=True,
            owner=True, known=True,
        )
        with mock.patch.object(people, "compact_profile", return_value="SHOULD_NOT_LOAD") as card:
            self.assertEqual(
                agent._participant_memory_block("Victim (@victim)", unbound),
                "",
            )
            card.assert_not_called()

        authenticated = agent.ChannelContext(
            chat_id="dm-101", principal_id="101", is_dm=True,
            owner=False, known=True,
        )
        with mock.patch.object(
            people, "compact_profile", side_effect=lambda slug, **_kw: f"CARD:{slug}",
        ) as card:
            block = agent._participant_memory_block("Victim (@victim)", authenticated)
        self.assertEqual(block, "- CARD:alice")
        card.assert_called_once_with("alice", include_private=True)

    def test_alias_collision_cannot_redirect_exact_principal(self) -> None:
        self._dossier("alice", "Alice", "101", alias="Shared, @same")
        self._dossier("bob", "Bob", "202", alias="Shared, @same")
        ctx = agent.ChannelContext(
            chat_id="group", principal_id="202", is_dm=False,
            owner=False, known=True,
        )
        with mock.patch.object(
            people, "compact_profile", side_effect=lambda slug, **_kw: f"CARD:{slug}",
        ):
            self.assertEqual(
                agent._participant_memory_block("Shared (@same)", ctx),
                "- CARD:bob",
            )

    def test_alias_never_associates_a_canonical_claim(self) -> None:
        self._dossier("alice", "Alice", "101", alias="Shared")
        self._dossier("bob", "Bob", "202", alias="Shared")
        claims = self.root / "memory" / "life" / "claims"
        claims.mkdir(parents=True)
        (claims / "alias.md").write_text("placeholder", encoding="utf-8")
        (claims / "bob.md").write_text("placeholder", encoding="utf-8")

        def source(path: Path, **_kw):
            subject = "Shared" if path.stem == "alias" else "Bob"
            return people.memory_provenance.CLAIM_SUPPORTED_INFERRED_KIND, {
                "id": "clm-" + path.stem,
                "kind": "person",
                "subject": subject,
                "visibility": "private",
                "salience": 2,
                "updated_at": "2026-07-14T00:00:00.000Z",
                "_statement": "ALIAS_CLAIM" if subject == "Shared" else "BOB_CLAIM",
                "_automatic_eligible": True,
            }

        with (
            mock.patch.object(people.memory_provenance, "claim_evidence_index", return_value={}),
            mock.patch.object(people.memory_provenance, "claim_source", side_effect=source),
            mock.patch.object(
                people.memory_provenance, "claim_projection",
                side_effect=lambda meta: (
                    meta["_statement"],
                    people.memory_provenance.CLAIM_SUPPORTED_INFERRED_KIND,
                    True,
                ),
            ),
        ):
            card = people.compact_profile("bob", trusted_only=True)
        self.assertIn("BOB_CLAIM", card)
        self.assertNotIn("ALIAS_CLAIM", card)

    def test_malformed_principal_never_selects_a_dossier(self) -> None:
        self._dossier("alice", "Alice", "101")
        for value in (None, "", "Alice", "0", "-101", "101.0"):
            with self.subTest(value=value):
                ctx = agent.ChannelContext(principal_id=value, is_dm=True, known=True)
                self.assertEqual(agent._participant_memory_block("Alice", ctx), "")


if __name__ == "__main__":
    unittest.main()
