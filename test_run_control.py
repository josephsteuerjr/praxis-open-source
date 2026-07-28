from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from run_context import RunContext
from run_manager import InvalidTransition, RunConflict, RunManager


class RunControlBase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.base = Path(self._temp.name)
        self.manager = RunManager(self.base)

    def create(self, suffix: str, *, kind: str = "computer") -> RunContext:
        context = RunContext.create(
            run_id=f"run-control-{suffix}",
            kind=kind,
            goal=f"control {suffix}",
            principal_id="telegram:100",
            scope="owner",
            origin_chat_id="100",
            origin_message_ids=[1],
            delivery_chat_id="100",
        )
        return self.manager.create(context, f"# Context {suffix}\n")


class TestPublicRunControlViews(RunControlBase):
    def test_manifest_status_and_list_views_are_public_and_filterable(self):
        first = self.create("first")
        second = self.create("second", kind="chat")
        self.manager.transition(first.run_id, "running", expected="pending")

        manifest = self.manager.manifest(first.run_id)
        status = self.manager.status(first.run_id)
        self.assertEqual(manifest["status"], "running")
        self.assertEqual(status["schema"], "praxis.run.status.v1")
        self.assertEqual(status["run_id"], first.run_id)
        self.assertEqual(status["status"], "running")
        self.assertTrue(status["terminalizable"])

        rows = self.manager.list_runs(statuses={"running"})
        self.assertEqual([row["run_id"] for row in rows], [first.run_id])
        self.assertEqual(
            [row["run_id"] for row in self.manager.list(kind="chat")],
            [second.run_id],
        )
        self.assertEqual(self.manager.list_runs(limit=0), [])
        with self.assertRaises(ValueError):
            self.manager.list_runs(statuses={"imaginary"})

    def test_outstanding_tools_returns_an_isolated_projection(self):
        context = self.create("projection")
        self.manager.start_tool(
            context.run_id, "call-1", "fs.read", {"path": "README.md"},
            side_effect=False,
        )
        first = self.manager.outstanding_tools(context.run_id)
        self.assertEqual(list(first), ["call-1"])
        first["call-1"]["tool"] = "mutated"
        self.assertEqual(
            self.manager.outstanding_tools(context.run_id)["call-1"]["tool"],
            "fs.read",
        )


class TestFailClosedTerminalization(RunControlBase):
    def test_terminalization_rejects_outstanding_call_until_result_is_durable(self):
        context = self.create("outstanding")
        self.manager.transition(context.run_id, "running", expected="pending")
        self.manager.start_tool(
            context.run_id, "call-build", "build", {"target": "all"},
            side_effect=True,
        )

        with self.assertRaisesRegex(RunConflict, "outstanding_call_ids"):
            self.manager.transition(context.run_id, "done", reason="not actually durable")
        status = self.manager.status(context.run_id)
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["outstanding_call_ids"], ["call-build"])
        self.assertFalse(status["terminalizable"])

        self.manager.store_result(
            context.run_id, "build passed", call_id="call-build", name="build",
        )
        terminal = self.manager.transition(context.run_id, "done", reason="verified")
        self.assertEqual(terminal["status"], "done")

    def test_terminalization_rejects_result_for_unknown_call(self):
        context = self.create("unknown-result")
        self.manager.transition(context.run_id, "running", expected="pending")
        self.manager.append_event(
            context.run_id, "tool_result", call_id="ghost-call", name="ghost",
            result={"result_id": "result-9999"},
        )

        with self.assertRaisesRegex(RunConflict, "unknown_result_call_ids"):
            self.manager.transition(context.run_id, "failed", reason="ledger corrupt")
        status = self.manager.status(context.run_id)
        self.assertEqual(status["unknown_result_call_ids"], ["ghost-call"])
        self.assertFalse(status["terminalizable"])

    def test_observed_tool_failure_closes_the_call(self):
        context = self.create("tool-failed")
        self.manager.transition(context.run_id, "running", expected="pending")
        self.manager.start_tool(
            context.run_id, "call-fail", "compiler", {}, side_effect=False,
        )
        self.manager.append_event(
            context.run_id, "tool_failed", call_id="call-fail",
            tool="compiler", error="syntax error",
        )

        self.assertEqual(self.manager.outstanding_tools(context.run_id), {})
        terminal = self.manager.transition(context.run_id, "failed", reason="compiler failed")
        self.assertEqual(terminal["status"], "failed")


class TestAuditedOwnerControls(RunControlBase):
    def test_pause_and_resume_are_separate_audited_controls(self):
        context = self.create("pause-resume")
        self.manager.transition(context.run_id, "running", expected="pending")

        paused = self.manager.request_pause(
            context.run_id, actor="telegram:100", reason="owner asked",
        )
        self.assertEqual(paused["status"], "paused")
        pause_event = self.manager.events(context.run_id)[-1]
        self.assertEqual(pause_event["kind"], "status_changed")
        self.assertEqual(pause_event["control_action"], "pause")
        self.assertEqual(pause_event["requested_by"], "telegram:100")

        event_count = len(self.manager.events(context.run_id))
        self.manager.request_pause(context.run_id, actor="telegram:100")
        self.assertEqual(len(self.manager.events(context.run_id)), event_count)
        with self.assertRaisesRegex(InvalidTransition, "use resume"):
            self.manager.transition(context.run_id, "running")

        resumed = self.manager.resume(
            context.run_id, actor="telegram:100", reason="continue now",
        )
        self.assertEqual(resumed["status"], "running")
        resume_event = self.manager.events(context.run_id)[-1]
        self.assertEqual(resume_event["control_action"], "resume")
        self.assertEqual(resume_event["from_status"], "paused")

    def test_cancel_is_durable_and_waits_for_running_tool(self):
        blocked = self.create("cancel-blocked")
        self.manager.transition(blocked.run_id, "running", expected="pending")
        self.manager.start_tool(
            blocked.run_id, "call-active", "send", {}, side_effect=True,
        )
        waiting = self.manager.request_cancel(
            blocked.run_id, actor="telegram:100", reason="stop",
        )
        self.assertEqual(waiting["status"], "paused")
        self.assertEqual(waiting["control"]["action"], "cancel")
        status = self.manager.status(blocked.run_id)
        self.assertEqual(status["requested_control"]["requested_by"], "telegram:100")
        self.assertEqual(status["outstanding_call_ids"], ["call-active"])
        with self.assertRaises(InvalidTransition):
            self.manager.start_tool(
                blocked.run_id, "call-late", "send", {}, side_effect=True,
            )
        with self.assertRaises(RunConflict):
            self.manager.resume(blocked.run_id, actor="telegram:100")

        self.manager.append_event(
            blocked.run_id, "tool_failed", call_id="call-active",
            tool="send", error="interrupted after cancellation request",
        )
        cancelled_after_receipt = self.manager.request_cancel(
            blocked.run_id, actor="telegram:100", reason="stop",
        )
        self.assertEqual(cancelled_after_receipt["status"], "cancelled")
        self.assertEqual(cancelled_after_receipt["control"], {})
        actions = [
            row.get("control_action") for row in self.manager.events(blocked.run_id)
            if row.get("control_action")
        ]
        self.assertEqual(actions, ["cancel_pending", "cancel"])

        clean = self.create("cancel-clean")
        cancelled = self.manager.request_cancel(
            clean.run_id, actor="telegram:100", reason="no longer wanted",
        )
        self.assertEqual(cancelled["status"], "cancelled")
        event = self.manager.events(clean.run_id)[-1]
        self.assertEqual(event["control_action"], "cancel")
        self.assertEqual(event["requested_by"], "telegram:100")

    def test_controls_require_an_explicit_actor(self):
        context = self.create("actor")
        self.manager.transition(context.run_id, "running", expected="pending")
        with self.assertRaises(ValueError):
            self.manager.request_pause(context.run_id, actor="")

    def test_recovery_resume_claim_is_exact_and_cannot_clear_owner_control(self):
        context = self.create("resume-claim")
        self.manager.transition(context.run_id, "running", expected="pending")
        self.manager.transition(
            context.run_id, "paused", expected="running",
            reason="process restarted; no uncertain side effect observed",
        )
        planned = self.manager.manifest(context.run_id)

        with self.assertRaisesRegex(RunConflict, "stale resume plan"):
            self.manager.claim_resume(
                context.run_id,
                expected_revision=planned["revision"] - 1,
                expected_event_seq=planned["event_seq"],
                actor="recovery:test",
            )

        self.manager.append_event(context.run_id, "recovery_observed", probe="alive")
        with self.assertRaisesRegex(RunConflict, "stale resume plan"):
            self.manager.claim_resume(
                context.run_id,
                expected_revision=planned["revision"],
                expected_event_seq=planned["event_seq"],
                actor="recovery:test",
            )

        fresh = self.manager.manifest(context.run_id)
        claimed = self.manager.claim_resume(
            context.run_id,
            expected_revision=fresh["revision"],
            expected_event_seq=fresh["event_seq"],
            actor="recovery:test",
        )
        self.assertEqual(claimed["status"], "running")
        event = self.manager.events(context.run_id)[-1]
        self.assertEqual(event["control_action"], "resume_claim")
        self.assertEqual(event["planned_revision"], fresh["revision"])
        self.assertEqual(event["planned_event_seq"], fresh["event_seq"])

        self.manager.request_pause(
            context.run_id, actor="telegram:100", reason="leave it paused",
        )
        owner_paused = self.manager.manifest(context.run_id)
        with self.assertRaisesRegex(RunConflict, "control is pending"):
            self.manager.claim_resume(
                context.run_id,
                expected_revision=owner_paused["revision"],
                expected_event_seq=owner_paused["event_seq"],
                actor="recovery:test",
            )


class TestExplicitInDoubtResolution(RunControlBase):
    def test_each_uncertain_call_needs_evidence_then_run_stays_paused(self):
        context = self.create("reconcile")
        self.manager.transition(context.run_id, "running", expected="pending")
        self.manager.start_tool(
            context.run_id, "call-one", "send", {"to": "1"}, side_effect=True,
        )
        self.manager.start_tool(
            context.run_id, "call-two", "write", {"path": "x"}, side_effect=True,
        )
        self.manager.transition(context.run_id, "in_doubt", expected="running")

        with self.assertRaisesRegex(InvalidTransition, "resolve_in_doubt"):
            self.manager.transition(context.run_id, "paused")
        first = self.manager.resolve_in_doubt(
            context.run_id, "call-one", "completed",
            evidence={"remote_receipt": "message-7"},
            reason="transport receipt observed", actor="telegram:100",
        )
        self.assertEqual(first["status"], "in_doubt")
        self.assertEqual(
            list(self.manager.outstanding_tools(context.run_id)), ["call-two"],
        )

        before = len(self.manager.events(context.run_id))
        revision = self.manager.manifest(context.run_id)["revision"]
        repeated = self.manager.resolve_in_doubt(
            context.run_id, "call-one", "completed",
            evidence={"remote_receipt": "message-7"},
            reason="transport receipt observed", actor="telegram:100",
        )
        self.assertEqual(repeated["status"], "in_doubt")
        self.assertEqual(len(self.manager.events(context.run_id)), before)
        self.assertEqual(repeated["revision"], revision)
        with self.assertRaises(RunConflict):
            self.manager.resolve_in_doubt(
                context.run_id, "call-one", "failed",
                evidence={"remote_receipt": "message-7"},
                reason="changed story", actor="telegram:100",
            )

        reconciled = self.manager.resolve_in_doubt(
            context.run_id, "call-two", "not_applied",
            evidence={"filesystem_hash": "unchanged"},
            reason="target hash proves no write", actor="telegram:100",
        )
        self.assertEqual(reconciled["status"], "paused")
        self.assertEqual(self.manager.outstanding_tools(context.run_id), {})
        events = self.manager.events(context.run_id)
        self.assertEqual(
            [row["call_id"] for row in events if row["kind"] == "tool_reconciled"],
            ["call-one", "call-two"],
        )
        final_transition = events[-1]
        self.assertEqual(final_transition["to_status"], "paused")
        self.assertEqual(final_transition["control_action"], "resolve_in_doubt")

        resumed = self.manager.resume(
            context.run_id, actor="telegram:100", reason="evidence reviewed",
        )
        self.assertEqual(resumed["status"], "running")

    def test_resolution_requires_known_call_proof_reason_and_in_doubt(self):
        context = self.create("resolution-input")
        self.manager.transition(context.run_id, "running", expected="pending")
        self.manager.start_tool(
            context.run_id, "call-one", "send", {}, side_effect=True,
        )
        with self.assertRaises(InvalidTransition):
            self.manager.resolve_in_doubt(
                context.run_id, "call-one", "completed",
                evidence={"receipt": "1"}, reason="seen", actor="telegram:100",
            )
        self.manager.transition(context.run_id, "in_doubt", expected="running")
        with self.assertRaises(ValueError):
            self.manager.resolve_in_doubt(
                context.run_id, "call-one", "unknown",
                evidence={"receipt": "1"}, reason="seen", actor="telegram:100",
            )
        with self.assertRaises(ValueError):
            self.manager.resolve_in_doubt(
                context.run_id, "call-one", "completed",
                evidence={}, reason="seen", actor="telegram:100",
            )
        with self.assertRaises(ValueError):
            self.manager.resolve_in_doubt(
                context.run_id, "call-one", "completed",
                evidence={"receipt": "1"}, reason="", actor="telegram:100",
            )
        with self.assertRaises(RunConflict):
            self.manager.resolve_in_doubt(
                context.run_id, "not-started", "not_applied",
                evidence={"receipt": "none"}, reason="checked", actor="telegram:100",
            )

    def test_cancel_requested_in_doubt_waits_for_reconciliation(self):
        context = self.create("cancel-in-doubt")
        self.manager.transition(context.run_id, "running", expected="pending")
        self.manager.start_tool(
            context.run_id, "call-send", "send", {}, side_effect=True,
        )
        self.manager.transition(context.run_id, "in_doubt", expected="running")

        waiting = self.manager.request_cancel(
            context.run_id, actor="telegram:100", reason="stop after checking",
        )
        self.assertEqual(waiting["status"], "in_doubt")
        self.assertEqual(waiting["control"]["action"], "cancel")
        reconciled = self.manager.resolve_in_doubt(
            context.run_id, "call-send", "not_applied",
            evidence={"remote_state": "absent"}, reason="checked remote state",
            actor="telegram:100",
        )
        self.assertEqual(reconciled["status"], "paused")
        with self.assertRaises(RunConflict):
            self.manager.resume(context.run_id, actor="telegram:100")
        cancelled = self.manager.request_cancel(
            context.run_id, actor="telegram:100", reason="stop after checking",
        )
        self.assertEqual(cancelled["status"], "cancelled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
