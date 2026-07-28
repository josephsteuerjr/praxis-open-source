from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import desires


class DesireLedgerTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="praxis_desires_"))
        self.ledger = desires.DesireLedger(self.base)

    def _notice(self, *, dedupe_key=""):
        return self.ledger.notice(
            "Хочу научиться надёжно управлять длинным компьютерным действием",
            source="run-kraken: wheel was never called",
            why_it_matters="Это расширяет мою реальную дееспособность, а не изображает её",
            next_move="воспроизвести и проверить scroll",
            evidence_refs=["run-kraken/events.jsonl"],
            run_id="run-kraken",
            desire_id="desire-scroll",
            dedupe_key=dedupe_key,
        )

    def test_full_causal_chain_is_durable_and_grep_friendly(self):
        self._notice()
        self.ledger.want("desire-scroll", note="основание устойчиво", next_move="выбрать проверку")
        self.ledger.choose("desire-scroll", note="выбрала live-safe canary", next_move="запустить canary")
        self.ledger.act(
            "desire-scroll",
            note="запустила canary",
            run_id="run-canary",
            evidence_refs=["memory/runs/run-canary/manifest.json"],
        )
        self.ledger.observe(
            "desire-scroll",
            note="scroll подтверждён снимком до/после",
            run_id="run-canary",
            evidence_refs=["artifact:before", "artifact:after"],
        )
        self.ledger.change(
            "desire-scroll",
            note="намерение исполнено",
            status="satisfied",
            next_move="",
            run_id="run-canary",
            evidence_refs=["memory/runs/run-canary/RECAP.md"],
        )

        state = self.ledger.get("desire-scroll")
        self.assertEqual(state["status"], "satisfied")
        self.assertEqual(state["stage"], "changed")
        self.assertEqual(
            [item["stage"] for item in state["timeline"] if item["event_type"] == "transition"],
            list(desires.STAGES),
        )
        self.assertEqual(state["run_ids"], ["run-kraken", "run-canary"])
        projection = self.ledger.projection_path.read_text(encoding="utf-8")
        self.assertIn("statement:", projection)
        self.assertIn("status: satisfied", projection)
        self.assertIn("`noticed`", projection)
        self.assertIn("`changed`", projection)
        self.assertIn("run-canary", projection)
        self.assertTrue(projection.startswith("<!-- praxis-generated:"))

    def test_skipped_or_reordered_stages_are_rejected(self):
        self._notice()
        with self.assertRaisesRegex(ValueError, "noticed -> wanted"):
            self.ledger.choose("desire-scroll", note="skip")
        with self.assertRaisesRegex(ValueError, "observed -> changed evidence"):
            self.ledger.want("desire-scroll", note="premature success", status="satisfied")
        self.ledger.want("desire-scroll", note="yes")
        with self.assertRaisesRegex(ValueError, "wanted -> chosen"):
            self.ledger.act("desire-scroll", note="skip")
        self.assertEqual(len(self.ledger.events("desire-scroll")), 2)

    def test_run_recap_updates_observed_then_changed(self):
        self._notice()
        self.ledger.want("desire-scroll", note="хочу")
        self.ledger.choose("desire-scroll", note="выбрала")
        self.ledger.act("desire-scroll", note="действовала", run_id="run-1")
        result = self.ledger.update_from_run(
            "desire-scroll",
            "run-1",
            observation="тест упал на горизонтальном scroll",
            change_note="оставляю желание активным с более точным ходом",
            status="active",
            next_move="исправить horizontal routing",
            evidence_refs=["memory/runs/run-1/RECAP.md"],
            dedupe_key="recap:run-1",
        )
        self.assertTrue(result["ok"])
        state = self.ledger.get("desire-scroll")
        self.assertEqual(state["stage"], "changed")
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["next_move"], "исправить horizontal routing")
        self.assertIn("memory/runs/run-1/RECAP.md", state["evidence_refs"])
        count = len(self.ledger.events())
        retry = self.ledger.update_from_run(
            "desire-scroll",
            "run-1",
            observation="same",
            change_note="same",
            status="active",
            dedupe_key="recap:run-1",
        )
        self.assertTrue(retry["deduplicated"])
        self.assertEqual(len(self.ledger.events()), count)

    def test_run_recap_resumes_after_crash_between_observed_and_changed(self):
        self._notice()
        self.ledger.want("desire-scroll", note="хочу")
        self.ledger.choose("desire-scroll", note="выбрала")
        self.ledger.act("desire-scroll", note="действовала", run_id="run-2")
        observed = self.ledger.observe(
            "desire-scroll",
            note="фактический результат уже записан",
            run_id="run-2",
            evidence_refs=["memory/runs/run-2/RECAP.md"],
            dedupe_key="recap:run-2:observed",
        )

        resumed = self.ledger.update_from_run(
            "desire-scroll",
            "run-2",
            observation="retry must not duplicate this",
            change_note="продолжаю с уточнённым следующим шагом",
            status="active",
            next_move="следующий шаг",
            evidence_refs=["memory/runs/run-2/RECAP.md"],
            dedupe_key="recap:run-2",
        )

        self.assertTrue(resumed["observed"]["deduplicated"])
        self.assertEqual(resumed["observed"]["event_id"], observed["event_id"])
        self.assertEqual(self.ledger.get("desire-scroll")["stage"], "changed")
        self.assertEqual(
            len([event for event in self.ledger.events("desire-scroll")
                 if event.get("stage") == "observed"]),
            1,
        )

    def test_link_run_does_not_fake_a_causal_transition(self):
        self._notice()
        event = self.ledger.link_run(
            "desire-scroll",
            "run-research",
            note="research run supports the next choice",
            evidence_refs=["memory/runs/run-research/manifest.json"],
        )
        self.assertEqual(event["event_type"], "run_link")
        state = self.ledger.get("desire-scroll")
        self.assertEqual(state["stage"], "noticed")
        self.assertIn("run-research", state["run_ids"])
        self.assertEqual([x["event_type"] for x in state["timeline"]], ["transition", "run_link"])

    def test_terminal_desire_requires_explicit_reopen(self):
        self._notice()
        self.ledger.want("desire-scroll", note="хочу")
        self.ledger.choose("desire-scroll", note="выбрала")
        self.ledger.act("desire-scroll", note="сделала")
        self.ledger.observe("desire-scroll", note="увидела")
        self.ledger.change("desire-scroll", note="готово", status="satisfied")
        with self.assertRaisesRegex(ValueError, "explicit reopen"):
            self.ledger.want("desire-scroll", note="снова")
        self.ledger.reopen(
            "desire-scroll",
            note="новые данные вернули интерес",
            next_move="проверить новый путь",
            evidence_refs=["evt-new"],
        )
        state = self.ledger.get("desire-scroll")
        self.assertEqual(state["status"], "active")
        self.assertEqual(state["stage"], "wanted")
        self.assertEqual(state["cycle"], 2)

    def test_dedupe_and_rebuild_from_canonical_jsonl(self):
        first = self._notice(dedupe_key="candidate:scroll")
        second = self.ledger.notice(
            "different retry text",
            source="retry",
            why_it_matters="retry",
            desire_id="would-be-different",
            dedupe_key="candidate:scroll",
        )
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["event_id"], first["event_id"])
        self.assertEqual(len(self.ledger.events()), 1)
        expected_state = self.ledger.states()
        expected_projection = self.ledger.projection_path.read_bytes()
        self.ledger.projection_path.unlink()
        rebuilt = self.ledger.rebuild_projection()
        self.assertTrue(rebuilt["ok"])
        self.assertEqual(self.ledger.states(), expected_state)
        self.assertEqual(self.ledger.projection_path.read_bytes(), expected_projection)
        self.assertIn("desire-scroll", self.ledger.projection_path.read_text(encoding="utf-8"))

    def test_torn_tail_does_not_destroy_prior_events(self):
        self._notice()
        with self.ledger.events_path.open("ab") as fh:
            fh.write(b'{"schema":"praxis.desire.event.v1"')
        self.assertIsNotNone(self.ledger.get("desire-scroll"))
        self.assertEqual(len(self.ledger.events()), 1)
        self.ledger.want("desire-scroll", note="continues after crash tail")
        self.assertEqual([event["stage"] for event in self.ledger.events()], ["noticed", "wanted"])

    def test_active_filter_separates_conation_from_legacy_goals(self):
        self._notice()
        active = self.ledger.list(statuses={"latent", "active", "blocked"})
        self.assertEqual([item["id"] for item in active], ["desire-scroll"])
        with self.assertRaises(ValueError):
            self.ledger.list(statuses={"speech-antipattern"})

    def test_desire_ids_cannot_inject_projection_structure(self):
        with self.assertRaisesRegex(ValueError, "must match"):
            self.ledger.notice(
                "x", source="x", why_it_matters="x",
                desire_id="desire-ok\n## forged",
            )


if __name__ == "__main__":
    unittest.main()
