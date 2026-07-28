from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import social_pulse


class SocialPulseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="praxis_pulse_")
        self.path = Path(self.tmp.name) / "pulse.json"
        self.env = {k: os.environ.get(k) for k in (
            "PRAXIS_SOCIAL_PULSE_HOURS", "PRAXIS_SOCIAL_PULSE_TARGET_COOLDOWN_H",
            "PRAXIS_SOCIAL_PULSE_DAILY_CAP", "PRAXIS_TZ", "PRAXIS_TZ_OFFSET_H",
        )}
        os.environ["PRAXIS_SOCIAL_PULSE_HOURS"] = "1"
        os.environ["PRAXIS_SOCIAL_PULSE_TARGET_COOLDOWN_H"] = "12"
        os.environ["PRAXIS_SOCIAL_PULSE_DAILY_CAP"] = "4"

    def tearDown(self):
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_claim_is_persisted_and_hourly(self):
        pulse = social_pulse.begin(now=100, path=self.path)
        self.assertTrue(pulse)
        self.assertIsNone(social_pulse.begin(now=200, path=self.path))
        self.assertFalse(social_pulse.due(now=3699, path=self.path))
        self.assertTrue(social_pulse.due(now=3700, path=self.path))
        social_pulse.finish(pulse, ok=True, now=150, path=self.path)

    def test_next_due_at_keeps_persisted_boundary(self):
        """Пункт C промежуточного пасса: граница = last_started + interval, не now+период."""
        # нет состояния — можно сейчас
        self.assertEqual(social_pulse.next_due_at(now=500.0, path=self.path), 500.0)
        pulse = social_pulse.begin(now=100, path=self.path)
        social_pulse.finish(pulse, ok=True, now=150, path=self.path)
        # рестарт «за 5 минут до окна»: граница остаётся 100+3600, не 3400+3600
        self.assertEqual(social_pulse.next_due_at(now=3400.0, path=self.path), 3700.0)
        # интервал уже прошёл — можно сейчас
        self.assertEqual(social_pulse.next_due_at(now=4000.0, path=self.path), 4000.0)

    def test_next_due_at_disabled_interval_returns_now(self):
        os.environ["PRAXIS_SOCIAL_PULSE_HOURS"] = "0"
        self.assertEqual(social_pulse.next_due_at(now=42.0, path=self.path), 42.0)

    def test_new_claim_marks_unfinished_prior_run_interrupted(self):
        prior = social_pulse.begin(now=100, path=self.path)
        current = social_pulse.begin(now=3700, path=self.path)
        self.assertTrue(current)
        state = json.loads(self.path.read_text(encoding="utf-8"))
        prior_run = next(run for run in state["runs"] if run["id"] == prior)
        self.assertEqual(prior_run["status"], "interrupted")
        self.assertIn("before pulse receipt", prior_run["detail"])

    def test_observability_uses_pulse_receipts(self):
        prior = social_pulse.begin(now=100, path=self.path)
        social_pulse.finish(prior, ok=True, now=120, path=self.path)
        current = social_pulse.begin(now=3700, path=self.path)
        text = social_pulse.observability(current, now=3705, path=self.path)
        self.assertTrue(text.startswith("SOCIAL PULSE OBSERVABILITY: "))
        payload = json.loads(text.split(": ", 1)[1])
        self.assertEqual(payload["pulse_id"], current)
        self.assertEqual(payload["current_age_seconds"], 5.0)
        self.assertEqual(payload["hours_since_previous_started"], 1.0)
        self.assertEqual(payload["previous_status"], "done")

    def test_observability_counts_today_in_praxis_timezone(self):
        os.environ["PRAXIS_TZ"] = "Europe/Moscow"
        prior = social_pulse.begin(now=1784145600, path=self.path)
        social_pulse.finish(prior, ok=True, now=1784145660, path=self.path)
        current = social_pulse.begin(now=1784149200, path=self.path)
        payload = json.loads(
            social_pulse.observability(current, now=1784149205, path=self.path)
            .split(": ", 1)[1]
        )
        self.assertEqual(payload["started_today"], 1)
        self.assertTrue(payload["local_time"].startswith("2026-07-16T"))

    def test_non_pulse_owner_turn_is_never_throttled(self):
        self.assertEqual(social_pulse.allow_outbound(42, now=100, path=self.path),
                         (True, "explicit/non-pulse turn"))

    def test_delivery_receipts_are_context_not_message_or_target_gates(self):
        p1 = social_pulse.begin(now=100, path=self.path)
        with social_pulse.active(p1):
            self.assertTrue(social_pulse.allow_outbound(42, now=100, path=self.path)[0])
            social_pulse.note_outbound(42, message_id=9, now=100, path=self.path)
            ok, reason = social_pulse.allow_outbound(99, now=101, path=self.path)
            self.assertTrue(ok)
            self.assertIn("sent_this_pulse=1", reason)
        p2 = social_pulse.begin(now=3700, path=self.path)
        with social_pulse.active(p2):
            ok, reason = social_pulse.allow_outbound(42, now=3700, path=self.path)
            self.assertTrue(ok)
            self.assertIn("same_peer_last_hours=", reason)
            self.assertTrue(social_pulse.allow_outbound(99, now=3700, path=self.path)[0])

    def test_goal_is_active_but_silence_is_not_failure(self):
        text = social_pulse.goal("- tgfu_1 [pending]")
        self.assertIn("несколько разговоров", text)
        self.assertIn("ничего не отправлять", text)
        self.assertIn("tgfu_1", text)

    def test_recovery_projection_is_idempotent_without_active_context(self):
        pulse = social_pulse.begin(now=100, path=self.path)
        social_pulse.note_outbound(
            42, message_id=9, now=101, path=self.path,
            pulse_id_override=pulse, idempotency_key="outbox:first",
        )
        social_pulse.note_outbound(
            42, message_id=9, now=102, path=self.path,
            pulse_id_override=pulse, idempotency_key="outbox:recovery",
        )
        state = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(state["outbound"]), 1)

    def test_malformed_shape_and_future_receipt_do_not_pin_clock(self):
        self.path.write_text(json.dumps({
            "version": 1, "last_started_at": "broken", "runs": {},
            "outbound": [{"pulse_id": "old", "peer_id": "42", "sent_at": 999999}],
        }), encoding="utf-8")
        pulse = social_pulse.begin(now=100, path=self.path)
        self.assertTrue(pulse)
        with social_pulse.active(pulse):
            self.assertTrue(social_pulse.allow_outbound(42, now=100, path=self.path)[0])

    def test_deferred_pulse_releases_due_clock_without_recording_failure(self):
        prior = social_pulse.begin(now=100, path=self.path)
        social_pulse.finish(prior, ok=True, now=120, path=self.path)
        current = social_pulse.begin(now=3700, path=self.path)

        social_pulse.defer(current, detail="another turn active", now=3701, path=self.path)

        state = json.loads(self.path.read_text(encoding="utf-8"))
        current_run = next(run for run in state["runs"] if run["id"] == current)
        self.assertEqual(current_run["status"], "deferred")
        self.assertTrue(social_pulse.due(now=3701, path=self.path))

    def test_mailbox_items_are_seen_only_after_successful_pulse(self):
        pulse = social_pulse.begin(now=100, path=self.path)
        social_pulse.finish(pulse, ok=False, mailbox_hashes=["abcd"], now=101, path=self.path)
        self.assertEqual(social_pulse.mailbox_seen(path=self.path), set())

        pulse = social_pulse.begin(now=3700, path=self.path)
        social_pulse.finish(pulse, ok=True, mailbox_hashes=["abcd", "efgh"],
                            now=3701, path=self.path)
        self.assertEqual(social_pulse.mailbox_seen(path=self.path), {"abcd", "efgh"})


if __name__ == "__main__":
    unittest.main()
