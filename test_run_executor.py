from __future__ import annotations

import unittest

from run_context import RunContext
from run_executor import (
    LeaseGrant,
    ResumeExecutorCallbacks,
    ToolResponseContinuationRequest,
    execute_resume,
)
from run_resume import (
    CHECKPOINT_SCHEMA,
    OutboundDescriptor,
    ResumePlan,
    ToolCallResume,
)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.reject_lease = False

    def acquire(self, lease):
        self.calls.append(("acquire", lease))
        if self.reject_lease:
            return LeaseGrant.reject(
                lease,
                observed_revision=lease.revision + 1,
                observed_event_seq=lease.event_seq + 1,
                reason="stale plan",
            )
        return LeaseGrant.accept(lease, owner_token="owner-lease")

    def authored(self, request):
        self.calls.append(("authored", request))
        return {"delivery": "prepared"}

    def checkpoint(self, request):
        self.calls.append(("checkpoint", request))
        return "continued"

    def pending(self, request):
        self.calls.append(("pending", request))
        return {"fresh": request.call_id}

    def replay(self, request):
        self.calls.append(("replay", request))
        return {"replayed": request.call_id}

    def tool_response(self, request):
        self.calls.append(("tool_response", request))
        return "next-turn"

    def callbacks(self) -> ResumeExecutorCallbacks:
        return ResumeExecutorCallbacks(
            acquire_lease=self.acquire,
            postprocess_authored_output=self.authored,
            continue_checkpoint=self.checkpoint,
            execute_pending_tool=self.pending,
            replay_outstanding_tool=self.replay,
            continue_tool_response=self.tool_response,
        )


class RunExecutorBase(unittest.TestCase):
    def context(self, run_id: str = "run-executor") -> RunContext:
        return RunContext.create(
            run_id=run_id,
            kind="chat",
            goal="resume exactly",
            principal_id="telegram:100",
            scope="owner",
            delivery_chat_id="100",
        ).with_status("paused")

    def plan(self, kind: str, **overrides) -> ResumePlan:
        values = {
            "run_id": "run-executor",
            "kind": kind,
            "status": "paused",
            "reason": "recovery",
            "revision": 7,
            "event_seq": 19,
            "auto_resume": kind in {
                "authored_output", "continue_checkpoint", "replay_model_tool_response",
            },
            "context": self.context(),
        }
        values.update(overrides)
        return ResumePlan(**values)

    @staticmethod
    def result_ref(call_id: str) -> dict:
        return {
            "schema": "praxis.result-ref.v1",
            "run_id": "run-executor",
            "result_id": "result-0001",
            "path": "results/0001-tool.log",
            "sha256": "a" * 64,
            "size": 4,
            "media_type": "text/plain; charset=utf-8",
            "encoding": "utf-8",
            "call_id": call_id,
        }


class TestNoEffectKinds(RunExecutorBase):
    def test_control_transport_and_uncertainty_kinds_invoke_nothing(self):
        for kind in ("transport_owned", "blocked", "in_doubt", "not_resumable"):
            with self.subTest(kind=kind):
                recorder = Recorder()
                outcome = execute_resume(self.plan(kind), recorder.callbacks())
                self.assertEqual(outcome.status, "noop")
                self.assertFalse(outcome.effects_started)
                self.assertFalse(outcome.lease_acquired)
                self.assertEqual(recorder.calls, [])

    def test_stale_lease_never_reaches_effect_callback(self):
        recorder = Recorder()
        recorder.reject_lease = True
        plan = self.plan(
            "authored_output",
            model_input={"system": "s", "messages": [], "tools": []},
            model_output={"text": "done", "blocks": [], "stop_reason": "end_turn"},
        )
        outcome = execute_resume(plan, recorder.callbacks())
        self.assertEqual(outcome.status, "lease_rejected")
        self.assertEqual([name for name, _ in recorder.calls], ["acquire"])

    def test_malformed_accepted_lease_never_reaches_effect_callback(self):
        recorder = Recorder()

        def malformed(lease):
            recorder.calls.append(("acquire", lease))
            return LeaseGrant(
                lease=lease, accepted=True,
                observed_revision=lease.revision + 1,
                observed_event_seq=lease.event_seq,
                owner_token="bad",
            )

        callbacks = recorder.callbacks()
        callbacks = ResumeExecutorCallbacks(
            acquire_lease=malformed,
            postprocess_authored_output=callbacks.postprocess_authored_output,
        )
        plan = self.plan(
            "authored_output",
            model_input={"system": "s", "messages": [], "tools": []},
            model_output={"text": "done", "blocks": [], "stop_reason": "end_turn"},
        )
        outcome = execute_resume(plan, callbacks)
        self.assertEqual(outcome.status, "invalid_plan")
        self.assertEqual(outcome.phase, "acquire_lease")
        self.assertEqual([name for name, _ in recorder.calls], ["acquire"])


class TestAuthoredAndCheckpoint(RunExecutorBase):
    def test_authored_output_uses_only_postprocess_callback(self):
        recorder = Recorder()
        original_output = {
            "text": "already authored",
            "blocks": [{"type": "text", "text": "already authored"}],
            "stop_reason": "end_turn",
        }
        plan = self.plan(
            "authored_output",
            model_input={"system": "s", "messages": [{"role": "user"}], "tools": []},
            model_output=original_output,
        )
        outcome = execute_resume(plan, recorder.callbacks())
        self.assertTrue(outcome.completed)
        self.assertEqual([name for name, _ in recorder.calls], ["acquire", "authored"])
        request = recorder.calls[1][1]
        self.assertEqual(request.model_output, original_output)
        self.assertEqual(outcome.callback_value, {"delivery": "prepared"})

        # Callback-facing state is a copy, not a way to mutate the durable plan.
        request.model_output["text"] = "changed by postprocessor"
        self.assertEqual(plan.model_output["text"], "already authored")

    def test_checkpoint_callback_gets_exact_persisted_frame_and_outbound(self):
        recorder = Recorder()
        outbound = OutboundDescriptor(
            kind="document", path="C:/spool/file.txt", mime="text/plain", size=4,
            target_chat_id="100", scope="owner", caption="cap",
            reply_to_message_id=22, voice_note=False, queue_id="q-one",
            run_id="run-executor", sha256="b" * 64,
        )
        checkpoint = {
            "schema": CHECKPOINT_SCHEMA,
            "iteration": 4,
            "system": [{"type": "text", "text": "exact"}],
            "messages": [{"role": "assistant", "content": "after tools"}],
            "tools": [{"name": "fs_read", "input_schema": {}}],
            "outbound": [outbound.as_dict()],
            "extra": {"cursor": 9},
        }
        outcome = execute_resume(
            self.plan("continue_checkpoint", checkpoint=checkpoint, outbound=(outbound,)),
            recorder.callbacks(),
        )
        self.assertTrue(outcome.completed)
        self.assertEqual([name for name, _ in recorder.calls], ["acquire", "checkpoint"])
        request = recorder.calls[1][1]
        self.assertEqual(request.iteration, 4)
        self.assertEqual(request.system, checkpoint["system"])
        self.assertEqual(request.messages, checkpoint["messages"])
        self.assertEqual(request.tools, checkpoint["tools"])
        self.assertEqual(request.outbound, (outbound,))
        self.assertEqual(request.checkpoint["extra"], {"cursor": 9})


class TestToolReplay(RunExecutorBase):
    def replay_plan(self, calls: tuple[ToolCallResume, ...]) -> ResumePlan:
        blocks = [
            {"type": "tool_use", "id": call.call_id,
             "name": call.name, "input": dict(call.input)}
            for call in calls
        ]
        offered_names = list(dict.fromkeys(call.name for call in calls))
        return self.plan(
            "replay_model_tool_response",
            model_input={
                "system": "exact-system",
                "messages": [{"role": "user", "content": "go"}],
                "tools": [{"name": name} for name in offered_names],
            },
            model_output={"text": "", "blocks": blocks, "stop_reason": "tool_use"},
            tool_calls=calls,
        )

    def test_completed_resultrefs_are_reused_and_never_executed(self):
        recorder = Recorder()
        completed = ToolCallResume(
            call_id="done-one", name="fs_read", input={"path": "README.md"},
            state="completed", result_ref=self.result_ref("done-one"),
        )
        outcome = execute_resume(self.replay_plan((completed,)), recorder.callbacks())
        self.assertTrue(outcome.completed)
        self.assertEqual([name for name, _ in recorder.calls], ["acquire", "tool_response"])
        resolution = outcome.resolutions[0]
        self.assertEqual(resolution.source, "durable_result_ref")
        self.assertEqual(resolution.result_ref, completed.result_ref)
        request = recorder.calls[1][1]
        self.assertIsInstance(request, ToolResponseContinuationRequest)
        self.assertEqual(request.resolutions[0].result_ref, completed.result_ref)

    def test_pending_and_safe_outstanding_are_dispatched_in_model_order(self):
        recorder = Recorder()
        calls = (
            ToolCallResume(
                call_id="new-one", name="fs_read", input={"path": "one"},
                state="pending_start",
            ),
            ToolCallResume(
                call_id="read-replay", name="fs_read", input={"path": "two"},
                state="outstanding", replayable=True, replay_basis="read_only",
            ),
            ToolCallResume(
                call_id="send-replay", name="send", input={"operation_id": "op"},
                state="outstanding", replayable=True, replay_basis="idempotency_key",
            ),
        )
        outcome = execute_resume(self.replay_plan(calls), recorder.callbacks())
        self.assertTrue(outcome.completed)
        self.assertEqual(
            [name for name, _ in recorder.calls],
            ["acquire", "pending", "replay", "replay", "tool_response"],
        )
        self.assertEqual(
            [item.source for item in outcome.resolutions],
            ["pending_start", "replayed_outstanding", "replayed_outstanding"],
        )
        self.assertEqual(outcome.resolutions[2].replay_basis, "idempotency_key")

    def test_unmarked_non_idempotent_outstanding_is_rejected_before_lease(self):
        recorder = Recorder()
        unsafe = ToolCallResume(
            call_id="write-one", name="fs_edit", input={"path": "x"},
            state="outstanding", replayable=False,
        )
        outcome = execute_resume(self.replay_plan((unsafe,)), recorder.callbacks())
        self.assertEqual(outcome.status, "invalid_plan")
        self.assertIn("not safely replayable", outcome.reason)
        self.assertEqual(recorder.calls, [])

    def test_invalid_replay_basis_is_rejected_before_lease(self):
        recorder = Recorder()
        unsafe = ToolCallResume(
            call_id="write-one", name="fs_edit", input={"path": "x"},
            state="outstanding", replayable=True, replay_basis="probably_safe",
        )
        outcome = execute_resume(self.replay_plan((unsafe,)), recorder.callbacks())
        self.assertEqual(outcome.status, "invalid_plan")
        self.assertEqual(recorder.calls, [])

    def test_model_block_mismatch_is_rejected_before_lease(self):
        recorder = Recorder()
        call = ToolCallResume(
            call_id="read-one", name="fs_read", input={"path": "correct"},
            state="pending_start",
        )
        plan = self.replay_plan((call,))
        plan.model_output["blocks"][0]["input"] = {"path": "different"}
        outcome = execute_resume(plan, recorder.callbacks())
        self.assertEqual(outcome.status, "invalid_plan")
        self.assertEqual(recorder.calls, [])

    def test_unoffered_model_tool_is_rejected_before_lease(self):
        recorder = Recorder()
        call = ToolCallResume(
            call_id="write-one", name="fs_edit", input={"path": "x"},
            state="pending_start",
        )
        plan = self.replay_plan((call,))
        plan.model_input["tools"] = [{"name": "fs_read"}]

        outcome = execute_resume(plan, recorder.callbacks())

        self.assertEqual(outcome.status, "invalid_plan")
        self.assertIn("not offered", outcome.reason)
        self.assertEqual(recorder.calls, [])

    def test_hosted_search_descriptor_is_not_a_replayed_local_tool(self):
        recorder = Recorder()
        call = ToolCallResume(
            call_id="read-one", name="fs_read", input={"path": "README.md"},
            state="pending_start",
        )
        plan = self.replay_plan((call,))
        plan.model_input["tools"].append({
            "type": "web_search", "search_context_size": "medium",
        })

        outcome = execute_resume(plan, recorder.callbacks())

        self.assertTrue(outcome.completed)
        self.assertEqual([name for name, _ in recorder.calls], ["acquire", "pending", "tool_response"])

    def test_explicit_null_option_matches_the_live_normalized_invocation(self):
        recorder = Recorder()
        call = ToolCallResume(
            call_id="read-one", name="fs_read", input={"path": "README.md"},
            state="pending_start",
        )
        plan = self.replay_plan((call,))
        plan.model_output["blocks"][0]["input"]["optional"] = None

        outcome = execute_resume(plan, recorder.callbacks())

        self.assertTrue(outcome.completed)
        request = next(value for name, value in recorder.calls if name == "pending")
        self.assertEqual(request.input, {"path": "README.md"})

    def test_callback_failure_is_a_typed_partial_outcome(self):
        recorder = Recorder()

        def fail(request):
            recorder.calls.append(("pending", request))
            raise RuntimeError("implementation stopped")

        callbacks = recorder.callbacks()
        callbacks = ResumeExecutorCallbacks(
            acquire_lease=callbacks.acquire_lease,
            execute_pending_tool=fail,
            continue_tool_response=callbacks.continue_tool_response,
        )
        call = ToolCallResume(
            call_id="new-one", name="fs_read", input={"path": "one"},
            state="pending_start",
        )
        outcome = execute_resume(self.replay_plan((call,)), callbacks)
        self.assertEqual(outcome.status, "failed")
        self.assertTrue(outcome.lease_acquired)
        self.assertTrue(outcome.effects_started)
        self.assertEqual(outcome.error_type, "RuntimeError")
        self.assertEqual(outcome.phase, "execute_pending_tool:new-one")
        self.assertEqual([name for name, _ in recorder.calls], ["acquire", "pending"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
