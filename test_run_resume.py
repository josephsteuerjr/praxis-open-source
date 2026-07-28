from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import run_resume
from run_context import RunContext
from run_manager import RunConflict, RunManager
from run_resume import (
    CHECKPOINT_SCHEMA,
    ResumeEvidenceError,
    plan_resume,
    read_full_json_result,
    read_full_result_bytes,
    restore_outbound_descriptors,
)


RECOVERY_REASON = "process restarted; no uncertain side effect observed"


class _EventsProxy:
    def __init__(self, manager: RunManager, mutate):
        self._manager = manager
        self._mutate = mutate

    def __getattr__(self, name):
        return getattr(self._manager, name)

    def events(self, run_id: str, *, strict: bool = False, **kwargs):
        rows = copy.deepcopy(
            self._manager.events(run_id, strict=strict, **kwargs)
        )
        return self._mutate(rows)


class RunResumeBase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.base = Path(self._temp.name)
        self.manager = RunManager(self.base)
        self.spool = self.base / "media-spool"
        self.spool.mkdir()

    def create(self, suffix: str) -> RunContext:
        context = RunContext.create(
            run_id=f"run-resume-{suffix}",
            kind="chat",
            goal=f"resume {suffix}",
            principal_id="telegram:100",
            scope="owner",
            origin_chat_id="100",
            origin_message_ids=[7],
            delivery_chat_id="100",
        )
        persisted = self.manager.create(context, f"# Context {suffix}\n")
        self.manager.transition(persisted.run_id, "running", expected="pending")
        return persisted.with_status("running")

    def model(self, context: RunContext, *, blocks: list[dict], stop_reason: str,
              text: str = "answer", call_id: str = "model-one") -> tuple[dict, dict]:
        offered_names: list[str] = []
        for block in blocks:
            if (block.get("type") == "tool_use"
                    and block.get("name") not in offered_names):
                offered_names.append(block["name"])
        model_input = {
            "system": "exact system",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [{"name": name} for name in offered_names] or [{"name": "fs_read"}],
        }
        input_ref = self.manager.store_result(
            context.run_id, json.dumps(model_input, ensure_ascii=False, indent=2),
            call_id=call_id, name="model-input",
            media_type="application/json; charset=utf-8", event_kind="model_input",
            idempotent=True,
        )
        self.manager.append_event(
            context.run_id, "model_started", call_id=call_id, role="voice",
            message_count=1, tool_count=1,
        )
        model_output = {
            "text": text,
            "blocks": blocks,
            "stop_reason": stop_reason,
            "framework": "test",
            "model": "test-model",
            "usage": {"input_tokens": 3},
        }
        output_ref = self.manager.store_result(
            context.run_id, json.dumps(model_output, ensure_ascii=False, indent=2),
            call_id=call_id, name="model-output",
            media_type="application/json; charset=utf-8", event_kind="model_output",
            idempotent=True,
        )
        self.manager.append_event(
            context.run_id, "model_completed", call_id=call_id, role="voice",
            stop_reason=stop_reason,
        )
        return input_ref, output_ref

    def checkpoint(self, context: RunContext, *, iteration: int = 1,
                   outbound: list[dict] | None = None,
                   tools: list[dict] | None = None) -> dict:
        value = {
            "schema": CHECKPOINT_SCHEMA,
            "iteration": iteration,
            "system": "exact system",
            "messages": [{"role": "user", "content": "after tools"}],
            "tools": list(tools) if tools is not None else [{"name": "fs_read"}],
            "outbound": list(outbound or ()),
        }
        return self.manager.store_result(
            context.run_id, json.dumps(value, ensure_ascii=False, indent=2),
            call_id=f"checkpoint-{iteration}", name="tool-loop-checkpoint",
            media_type="application/json; charset=utf-8", event_kind="run_checkpoint",
        )

    def recovery_pause(self, context: RunContext) -> None:
        self.manager.transition(
            context.run_id, "paused", expected="running", reason=RECOVERY_REASON,
        )

    def outbound(self, context: RunContext, name: str = "report.txt") -> tuple[dict, Path]:
        path = self.spool / name
        payload = b"durable outbound bytes"
        path.write_bytes(payload)
        return ({
            "kind": "document",
            "path": str(path.resolve()),
            "mime": "text/plain",
            "size": len(payload),
            "target_chat_id": "100",
            "scope": "owner",
            "caption": "report",
            "reply_to_message_id": 7,
            "voice_note": False,
            "queue_id": "queue-one",
            "run_id": context.run_id,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }, path)

    def current_evidence_bytes(self, context: RunContext) -> int:
        events = self.manager.events(context.run_id, strict=True)
        event_bytes = sum(
            len(json.dumps(
                row, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")) + 1
            for row in events
        )
        result_bytes = sum({
            (row["result"]["result_id"], row["result"]["sha256"]):
                row["result"]["size"]
            for row in events if isinstance(row.get("result"), dict)
        }.values())
        return event_bytes + result_bytes


class TestResultAndOutboundHelpers(RunResumeBase):
    def test_full_json_result_uses_public_cursor_and_checks_hash(self):
        context = self.create("json")
        ref = self.manager.store_result(
            context.run_id, json.dumps({"large": "x" * 9000}, indent=2),
            name="payload", media_type="application/json; charset=utf-8",
            event_kind="evidence",
        )
        self.assertEqual(
            read_full_json_result(self.manager, context.run_id, ref)["large"],
            "x" * 9000,
        )

        result_path = self.manager.path(context.run_id) / ref["path"]
        result_path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ResumeEvidenceError, "checksum differs"):
            read_full_json_result(self.manager, context.run_id, ref)

    def test_multi_page_result_hashes_only_the_final_assembled_payload(self):
        context = self.create("multi-page")
        payload = b"x" * (8 * 1024 * 1024 + 17)
        ref = self.manager.store_result(
            context.run_id, payload, name="large", event_kind="evidence",
        )

        with mock.patch(
                "run_manager._file_sha256",
                side_effect=AssertionError("per-page full-file hash")):
            with mock.patch.object(
                    self.manager, "read_result",
                    wraps=self.manager.read_result) as read_result:
                restored = read_full_result_bytes(
                    self.manager, context.run_id, ref,
                    max_bytes=len(payload),
                )

        self.assertEqual(restored, payload)
        self.assertEqual(read_result.call_count, 3)
        self.assertTrue(all(
            call.kwargs.get("verify_sha256") is False
            for call in read_result.call_args_list
        ))

        result_path = self.manager.path(context.run_id) / ref["path"]
        with result_path.open("r+b") as stream:
            stream.seek(4 * 1024 * 1024 + 3)
            stream.write(b"y")
        with mock.patch(
                "run_manager._file_sha256",
                side_effect=AssertionError("per-page full-file hash")):
            with self.assertRaisesRegex(ResumeEvidenceError, "checksum differs"):
                read_full_result_bytes(
                    self.manager, context.run_id, ref,
                    max_bytes=len(payload),
                )

    def test_outbound_restore_is_inert_and_strictly_hash_bound(self):
        context = self.create("outbound-helper")
        item, path = self.outbound(context)
        checkpoint = {"outbound": [item]}
        restored = restore_outbound_descriptors(
            checkpoint, run_id=context.run_id, allowed_roots=[self.spool],
        )
        self.assertEqual(restored[0].path, str(path.resolve()))
        self.assertTrue(path.is_file())

        path.write_bytes(b"tampered")
        with self.assertRaisesRegex(ResumeEvidenceError, "metadata changed"):
            restore_outbound_descriptors(
                checkpoint, run_id=context.run_id, allowed_roots=[self.spool],
            )

    def test_outbound_requires_explicit_containment_root(self):
        context = self.create("outbound-root")
        item, _path = self.outbound(context)
        with self.assertRaisesRegex(ResumeEvidenceError, "explicit allowed roots"):
            restore_outbound_descriptors(
                {"outbound": [item]}, run_id=context.run_id, allowed_roots=[],
            )
        outside = self.base / "elsewhere"
        outside.mkdir()
        with self.assertRaisesRegex(ResumeEvidenceError, "outside allowed roots"):
            restore_outbound_descriptors(
                {"outbound": [item]}, run_id=context.run_id, allowed_roots=[outside],
            )


class TestStrictResumeKinds(RunResumeBase):
    def test_owner_authorize_plan_and_claim_resume_end_to_end(self):
        context = self.create("owner-authorized")
        self.model(
            context, blocks=[{"type": "text", "text": "ready"}],
            stop_reason="end_turn", text="ready",
        )
        paused = self.manager.request_pause(
            context.run_id, actor="telegram:100", reason="inspect first",
        )
        authorized = self.manager.authorize_resume(
            context.run_id, actor="telegram:100", reason="continue",
            expected_revision=paused["revision"],
        )

        plan = plan_resume(self.manager, context.run_id)

        self.assertEqual(plan.kind, "authored_output")
        self.assertTrue(plan.auto_resume)
        self.assertEqual(plan.revision, authorized["revision"])
        self.assertEqual(plan.event_seq, authorized["event_seq"])
        claimed = self.manager.claim_resume(
            context.run_id, expected_revision=plan.revision,
            expected_event_seq=plan.event_seq, actor="runtime:test",
        )
        self.assertEqual(claimed["status"], "running")
        with self.assertRaisesRegex(RunConflict, "stale resume plan"):
            self.manager.claim_resume(
                context.run_id, expected_revision=plan.revision,
                expected_event_seq=plan.event_seq, actor="runtime:test",
            )

    def test_event_count_budget_fails_closed_with_explicit_reason(self):
        context = self.create("event-budget")
        self.recovery_pause(context)

        plan = plan_resume(self.manager, context.run_id, max_events=1)

        self.assertEqual(plan.kind, "blocked")
        self.assertIn("event evidence count budget exceeded", plan.reason)
        self.assertFalse(plan.auto_resume)

    def test_cumulative_event_and_result_byte_budget_fails_closed(self):
        context = self.create("byte-budget")
        self.model(
            context, blocks=[{"type": "text", "text": "x" * 20_000}],
            stop_reason="end_turn", text="x" * 20_000,
        )
        self.recovery_pause(context)
        events = self.manager.events(context.run_id, strict=True)
        event_bytes = sum(
            len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
            for row in events
        )
        result_bytes = sum({
            (row["result"]["result_id"], row["result"]["sha256"]): row["result"]["size"]
            for row in events if isinstance(row.get("result"), dict)
        }.values())

        plan = plan_resume(
            self.manager, context.run_id,
            max_evidence_bytes=event_bytes + result_bytes - 1,
        )

        self.assertEqual(plan.kind, "blocked")
        self.assertIn("cumulative evidence byte budget exceeded", plan.reason)
        self.assertFalse(plan.auto_resume)

    def test_planning_time_budget_fails_closed_with_explicit_reason(self):
        context = self.create("time-budget")
        self.recovery_pause(context)
        with mock.patch("run_resume.time.monotonic", side_effect=[100.0, 102.0]):
            plan = plan_resume(
                self.manager, context.run_id, max_planning_seconds=1.0,
            )

        self.assertEqual(plan.kind, "blocked")
        self.assertIn("planning-time budget exceeded", plan.reason)
        self.assertFalse(plan.auto_resume)

    def test_outbound_bytes_are_reserved_before_hashing_and_fail_closed(self):
        context = self.create("outbound-byte-budget")
        item, _path = self.outbound(context)
        self.checkpoint(context, outbound=[item])
        self.recovery_pause(context)

        with mock.patch(
                "run_resume._file_sha256",
                side_effect=AssertionError("over-budget outbound was hashed")) as digest:
            plan = plan_resume(
                self.manager, context.run_id,
                outbound_roots=[self.spool],
                max_evidence_bytes=(
                    self.current_evidence_bytes(context) + item["size"] - 1
                ),
            )

        self.assertEqual(plan.kind, "blocked")
        self.assertIn("cumulative evidence byte budget exceeded", plan.reason)
        self.assertIn("outbound evidence", plan.reason)
        digest.assert_not_called()

    def test_duplicate_outbound_descriptors_share_budget_and_verification(self):
        context = self.create("outbound-deduplicate")
        first, _path = self.outbound(context)
        second = copy.deepcopy(first)
        second["queue_id"] = "queue-two"
        self.checkpoint(context, outbound=[first, second])
        self.recovery_pause(context)

        with mock.patch(
                "run_resume._file_sha256",
                wraps=run_resume._file_sha256) as digest:
            plan = plan_resume(
                self.manager, context.run_id,
                outbound_roots=[self.spool],
                max_evidence_bytes=(
                    self.current_evidence_bytes(context) + first["size"]
                ),
            )

        self.assertEqual(plan.kind, "continue_checkpoint")
        self.assertEqual(
            [item.queue_id for item in plan.outbound],
            ["queue-one", "queue-two"],
        )
        self.assertEqual(digest.call_count, 1)

    def test_outbound_hash_deadline_failure_blocks_the_plan(self):
        context = self.create("outbound-time-budget")
        item, _path = self.outbound(context)
        self.checkpoint(context, outbound=[item])
        self.recovery_pause(context)
        original = run_resume._file_sha256

        def expire_during_hash(path, *, budget=None,
                               phase="hashing outbound evidence"):
            self.assertIsNotNone(budget)
            budget.deadline_monotonic = run_resume.time.monotonic() - 1.0
            return original(path, budget=budget, phase=phase)

        with mock.patch("run_resume._file_sha256", side_effect=expire_during_hash):
            plan = plan_resume(
                self.manager, context.run_id,
                outbound_roots=[self.spool], max_planning_seconds=30.0,
            )

        self.assertEqual(plan.kind, "blocked")
        self.assertIn("planning-time budget exceeded", plan.reason)
        self.assertIn("hashing outbound evidence", plan.reason)

    def test_terminal_model_output_is_authored_not_reauthored(self):
        context = self.create("authored")
        model_input, _ = self.model(
            context, blocks=[{"type": "text", "text": "exact answer"}],
            stop_reason="end_turn", text="exact answer",
        )
        self.recovery_pause(context)

        plan = plan_resume(self.manager, context.run_id)
        self.assertEqual(plan.kind, "authored_output")
        self.assertTrue(plan.auto_resume)
        self.assertEqual(plan.model_input["messages"], [{"role": "user", "content": "hello"}])
        self.assertEqual(plan.model_output["text"], "exact answer")
        self.assertGreater(plan.revision, 0)
        self.assertEqual(plan.event_seq, self.manager.manifest(context.run_id)["event_seq"])

    def test_checkpoint_restores_exact_input_and_verified_outbound(self):
        context = self.create("checkpoint")
        item, path = self.outbound(context)
        self.checkpoint(context, iteration=3, outbound=[item])
        self.recovery_pause(context)

        plan = plan_resume(
            self.manager, context.run_id, outbound_roots=[self.spool],
        )
        self.assertEqual(plan.kind, "continue_checkpoint")
        self.assertTrue(plan.auto_resume)
        self.assertEqual(plan.checkpoint["iteration"], 3)
        self.assertEqual(plan.checkpoint["messages"][0]["content"], "after tools")
        self.assertEqual(plan.outbound[0].path, str(path.resolve()))
        self.assertTrue(path.exists(), "planning must not consume or queue the file")

    def test_checkpoint_with_hosted_search_remains_resumable(self):
        context = self.create("checkpoint-hosted-search")
        tools = [
            {"name": "fs_read"},
            {"type": "web_search", "search_context_size": "medium"},
        ]
        self.checkpoint(context, tools=tools)
        self.recovery_pause(context)

        plan = plan_resume(self.manager, context.run_id)

        self.assertEqual(plan.kind, "continue_checkpoint")
        self.assertEqual(plan.checkpoint["tools"], tools)

    def test_transport_intent_wins_and_contains_exact_route(self):
        context = self.create("transport")
        self.model(context, blocks=[{"type": "text", "text": "ready"}],
                   stop_reason="end_turn", text="ready")
        self.manager.start_tool(
            context.run_id, f"delivery:{context.run_id}", "telegram.deliver",
            {"chat_id": "100", "text_chars": 5, "media_count": 0},
            side_effect=True, idempotency_key=f"telegram-delivery:{context.run_id}",
        )
        self.recovery_pause(context)

        plan = plan_resume(self.manager, context.run_id)
        self.assertEqual(plan.kind, "transport_owned")
        self.assertFalse(plan.auto_resume)
        self.assertEqual(plan.transport_intent["args"]["chat_id"], "100")
        self.assertIsNone(plan.model_output)

    def test_transport_restores_only_media_named_by_the_parent_intent(self):
        context = self.create("transport-media-filter")
        first, _ = self.outbound(context, "first.txt")
        second, second_path = self.outbound(context, "second.txt")
        second["queue_id"] = "queue-two"
        self.checkpoint(context, outbound=[first, second])
        self.manager.start_tool(
            context.run_id, f"delivery:{context.run_id}", "telegram.deliver",
            {
                "chat_id": "100", "text_chars": 0, "media_count": 1,
                "media_queue_ids": ["queue-two"],
            },
            side_effect=True, idempotency_key=f"telegram-delivery:{context.run_id}",
        )
        self.recovery_pause(context)

        plan = plan_resume(
            self.manager, context.run_id, outbound_roots=[self.spool],
        )

        self.assertEqual(plan.kind, "transport_owned")
        self.assertEqual([item.queue_id for item in plan.outbound], ["queue-two"])
        self.assertEqual(plan.outbound[0].path, str(second_path.resolve()))

    def test_completed_media_does_not_require_its_deleted_staged_file(self):
        context = self.create("transport-partial-receipt")
        first, first_path = self.outbound(context, "first-delivered.txt")
        second, second_path = self.outbound(context, "second-pending.txt")
        second["queue_id"] = "queue-two"
        self.checkpoint(context, outbound=[first, second])
        self.manager.start_tool(
            context.run_id, f"delivery:{context.run_id}", "telegram.deliver",
            {
                "chat_id": "100", "text_chars": 0, "media_count": 2,
                "media_queue_ids": ["queue-one", "queue-two"],
            },
            side_effect=True, idempotency_key=f"telegram-delivery:{context.run_id}",
        )
        self.manager.start_tool(
            context.run_id, "delivery-media:queue-one", "telegram.send_media",
            {"queue_id": "queue-one"}, side_effect=True,
            idempotency_key="queue-one",
        )
        self.manager.store_result(
            context.run_id,
            json.dumps({"queue_id": "queue-one", "message_id": "501", "ok": True}),
            call_id="delivery-media:queue-one", name="telegram-media",
            media_type="application/json; charset=utf-8", idempotent=True,
        )
        first_path.unlink()
        self.recovery_pause(context)

        plan = plan_resume(
            self.manager, context.run_id, outbound_roots=[self.spool],
        )

        self.assertEqual(plan.kind, "transport_owned")
        self.assertEqual([item.queue_id for item in plan.outbound], ["queue-two"])
        self.assertEqual(plan.outbound[0].path, str(second_path.resolve()))

    def test_media_transport_without_exact_checkpoint_fails_closed(self):
        context = self.create("transport-media-no-checkpoint")
        self.manager.start_tool(
            context.run_id, f"delivery:{context.run_id}", "telegram.deliver",
            {
                "chat_id": "100", "text_chars": 0, "media_count": 1,
                "media_queue_ids": ["queue-one"],
            },
            side_effect=True, idempotency_key=f"telegram-delivery:{context.run_id}",
        )
        self.recovery_pause(context)

        plan = plan_resume(
            self.manager, context.run_id, outbound_roots=[self.spool],
        )

        self.assertEqual(plan.kind, "blocked")
        self.assertIn("no durable outbound checkpoint", plan.reason)

    def test_owner_pause_is_never_auto_resumed(self):
        context = self.create("owner-pause")
        self.model(context, blocks=[{"type": "text", "text": "ready"}],
                   stop_reason="end_turn", text="ready")
        self.manager.request_pause(
            context.run_id, actor="telegram:100", reason="owner asked",
        )

        plan = plan_resume(self.manager, context.run_id)
        self.assertEqual(plan.kind, "blocked")
        self.assertFalse(plan.auto_resume)
        self.assertIn("pause control", plan.reason)

    def test_pending_cancel_is_never_transport_replayed(self):
        context = self.create("cancel")
        self.model(context, blocks=[{"type": "text", "text": "ready"}],
                   stop_reason="end_turn", text="ready")
        self.manager.start_tool(
            context.run_id, f"delivery:{context.run_id}", "telegram.deliver",
            {"chat_id": "100", "text_chars": 5, "media_count": 0},
            side_effect=True, idempotency_key=f"telegram-delivery:{context.run_id}",
        )
        self.manager.request_cancel(
            context.run_id, actor="telegram:100", reason="stop",
        )

        plan = plan_resume(self.manager, context.run_id)
        self.assertEqual(plan.kind, "blocked")
        self.assertIn("cancel control", plan.reason)

    def test_non_transport_uncertain_effect_is_in_doubt(self):
        context = self.create("uncertain")
        self.manager.start_tool(
            context.run_id, "call-write", "fs_edit", {"path": "x"},
            side_effect=True,
        )
        self.manager.transition(
            context.run_id, "in_doubt", expected="running", reason="restart",
        )

        plan = plan_resume(self.manager, context.run_id)
        self.assertEqual(plan.kind, "in_doubt")
        self.assertEqual(plan.diagnostics, ("call-write",))

    def test_running_pending_and_terminal_runs_are_not_resumable(self):
        running = self.create("running")
        self.assertEqual(plan_resume(self.manager, running.run_id).kind, "not_resumable")

        pending_context = RunContext.create(
            run_id="run-resume-pending", kind="chat", goal="pending",
            principal_id="telegram:100", scope="owner",
        )
        pending = self.manager.create(pending_context, "# pending\n")
        self.assertEqual(plan_resume(self.manager, pending.run_id).kind, "not_resumable")

        terminal = self.create("terminal")
        self.manager.transition(terminal.run_id, "done", expected="running")
        self.assertEqual(plan_resume(self.manager, terminal.run_id).kind, "not_resumable")

    def test_initial_interrupted_model_request_does_not_reuse_an_older_answer(self):
        context = self.create("initial-model-crash")
        self.manager.store_result(
            context.run_id,
            json.dumps({"system": "s", "messages": [], "tools": []}, indent=2),
            call_id="model-unfinished", name="model-input",
            media_type="application/json; charset=utf-8", event_kind="model_input",
            idempotent=True,
        )
        self.manager.append_event(
            context.run_id, "model_started", call_id="model-unfinished", role="voice",
        )
        self.recovery_pause(context)

        plan = plan_resume(self.manager, context.run_id)
        self.assertEqual(plan.kind, "not_resumable")
        self.assertIn("no preceding tool-loop checkpoint", plan.reason)


class TestToolResponseReplay(RunResumeBase):
    def test_only_read_only_and_keyed_outstanding_calls_are_replayable(self):
        context = self.create("tool-replay")
        blocks = [
            {"type": "text", "text": "checking"},
            {"type": "tool_use", "id": "read-one", "name": "fs_read",
             "input": {"path": "README.md", "optional": None}},
            {"type": "tool_use", "id": "send-one", "name": "send",
             "input": {"to": "100", "operation_id": "op-1"}},
            {"type": "tool_use", "id": "later", "name": "fs_read",
             "input": {"path": "CODEMAP.md"}},
        ]
        self.model(context, blocks=blocks, stop_reason="tool_use", text="checking")
        self.manager.start_tool(
            context.run_id, "read-one", "fs_read", {"path": "README.md"},
            side_effect=False,
        )
        self.manager.start_tool(
            context.run_id, "send-one", "send",
            {"to": "100", "operation_id": "op-1"},
            side_effect=True, idempotency_key="op-1",
        )
        self.recovery_pause(context)

        plan = plan_resume(self.manager, context.run_id)
        self.assertEqual(plan.kind, "replay_model_tool_response")
        self.assertEqual(plan.model_output["blocks"], blocks)
        calls = {item.call_id: item for item in plan.tool_calls}
        self.assertEqual(calls["read-one"].replay_basis, "read_only")
        self.assertTrue(calls["read-one"].replayable)
        self.assertEqual(calls["send-one"].replay_basis, "idempotency_key")
        self.assertTrue(calls["send-one"].replayable)
        self.assertEqual(calls["later"].state, "pending_start")
        self.assertFalse(calls["later"].replayable)
        self.assertNotIn("optional", calls["read-one"].input)

    def test_completed_tool_result_is_hash_checked_and_not_replayed(self):
        context = self.create("tool-complete")
        block = {"type": "tool_use", "id": "read-one", "name": "fs_read",
                 "input": {"path": "README.md"}}
        self.model(context, blocks=[block], stop_reason="tool_use", text="")
        self.manager.start_tool(
            context.run_id, "read-one", "fs_read", {"path": "README.md"},
            side_effect=False,
        )
        ref = self.manager.store_result(
            context.run_id, "full read result", call_id="read-one", name="fs_read",
            idempotent=True,
        )
        self.recovery_pause(context)

        plan = plan_resume(self.manager, context.run_id)
        self.assertEqual(plan.kind, "replay_model_tool_response")
        self.assertEqual(plan.tool_calls[0].state, "completed")
        self.assertFalse(plan.tool_calls[0].replayable)
        self.assertEqual(plan.tool_calls[0].result_ref["sha256"], ref["sha256"])

        (self.manager.path(context.run_id) / ref["path"]).write_text(
            "changed", encoding="utf-8",
        )
        rejected = plan_resume(self.manager, context.run_id)
        self.assertEqual(rejected.kind, "blocked")
        self.assertIn("checksum differs", rejected.reason)

    def test_checkpoint_after_tool_batch_supersedes_response_replay(self):
        context = self.create("tool-checkpoint")
        block = {"type": "tool_use", "id": "read-one", "name": "fs_read",
                 "input": {"path": "README.md"}}
        self.model(context, blocks=[block], stop_reason="tool_use", text="")
        self.manager.start_tool(
            context.run_id, "read-one", "fs_read", {"path": "README.md"},
            side_effect=False,
        )
        self.manager.store_result(
            context.run_id, "read", call_id="read-one", name="fs_read",
            idempotent=True,
        )
        self.checkpoint(context)
        self.recovery_pause(context)

        plan = plan_resume(self.manager, context.run_id)
        self.assertEqual(plan.kind, "continue_checkpoint")
        self.assertEqual(plan.tool_calls[0].state, "completed")

    def test_checkpoint_retries_a_newer_interrupted_model_request(self):
        context = self.create("checkpoint-model-crash")
        block = {"type": "tool_use", "id": "read-one", "name": "fs_read",
                 "input": {"path": "README.md"}}
        self.model(context, blocks=[block], stop_reason="tool_use", text="")
        self.manager.start_tool(
            context.run_id, "read-one", "fs_read", {"path": "README.md"},
            side_effect=False,
        )
        self.manager.store_result(
            context.run_id, "read", call_id="read-one", name="fs_read",
            idempotent=True,
        )
        self.checkpoint(context)
        self.manager.store_result(
            context.run_id,
            json.dumps({"system": "s", "messages": [], "tools": []}, indent=2),
            call_id="model-unfinished", name="model-input",
            media_type="application/json; charset=utf-8", event_kind="model_input",
            idempotent=True,
        )
        self.manager.append_event(
            context.run_id, "model_started", call_id="model-unfinished", role="voice",
        )
        self.recovery_pause(context)

        plan = plan_resume(self.manager, context.run_id)
        self.assertEqual(plan.kind, "continue_checkpoint")
        self.assertIn("interrupted model request", plan.reason)
        self.assertEqual(plan.checkpoint["iteration"], 1)

    def test_unsafe_outstanding_is_not_labeled_replayable(self):
        context = self.create("tool-unsafe")
        block = {"type": "tool_use", "id": "write-one", "name": "fs_edit",
                 "input": {"path": "x", "text": "y"}}
        self.model(context, blocks=[block], stop_reason="tool_use", text="")
        self.manager.start_tool(
            context.run_id, "write-one", "fs_edit", {"path": "x", "text": "y"},
            side_effect=True,
        )
        # A real RunManager.recover() would choose in_doubt.  This deliberately
        # malformed recovery pause proves the planner does not inherit its label.
        self.recovery_pause(context)

        plan = plan_resume(self.manager, context.run_id)
        self.assertEqual(plan.kind, "in_doubt")
        self.assertFalse(plan.tool_calls[0].replayable)
        self.assertEqual(plan.diagnostics, ("write-one",))


class TestEvidenceRejection(RunResumeBase):
    def test_event_sequence_tamper_blocks_resume(self):
        context = self.create("event-order")
        self.model(context, blocks=[{"type": "text", "text": "ok"}],
                   stop_reason="end_turn", text="ok")
        self.recovery_pause(context)

        def mutate(rows):
            rows[1]["seq"] = 99
            return rows

        plan = plan_resume(_EventsProxy(self.manager, mutate), context.run_id)
        self.assertEqual(plan.kind, "blocked")
        self.assertIn("sequence/id", plan.reason)

    def test_resultref_inline_tamper_blocks_resume(self):
        context = self.create("inline-tamper")
        self.model(context, blocks=[{"type": "text", "text": "ok"}],
                   stop_reason="end_turn", text="ok")
        self.recovery_pause(context)

        def mutate(rows):
            for row in rows:
                if row.get("kind") == "model_output":
                    row["result"]["inline"]["head"] = "forged"
            return rows

        plan = plan_resume(_EventsProxy(self.manager, mutate), context.run_id)
        self.assertEqual(plan.kind, "blocked")
        self.assertIn("inline text", plan.reason)

    def test_guard_receipt_file_tamper_blocks_resume(self):
        context = self.create("guard-result-tamper")
        self.model(context, blocks=[{"type": "text", "text": "ok"}],
                   stop_reason="end_turn", text="ok")
        draft_sha = hashlib.sha256(b"ok").hexdigest()
        self.manager.store_result(
            context.run_id,
            json.dumps({
                "schema": "praxis.outbound-guard-input.v1",
                "draft_sha256": draft_sha,
                "media_queue_ids": [],
            }),
            call_id=f"outbound-guard-input:{context.run_id}",
            name="outbound-guard-input",
            media_type="application/json; charset=utf-8",
            event_kind="outbound_guard_input", idempotent=True,
        )
        receipt_ref = self.manager.store_result(
            context.run_id,
            json.dumps({
                "schema": "praxis.outbound-guard-result.v3",
                "draft_sha256": draft_sha, "text": "ok",
                "media_queue_ids": [],
                "advisor": "privacy", "advisor_verdict": "ok",
                "advisor_reason": "", "praxis_decision": "send_authored",
            }),
            call_id=f"outbound-guard-v2:{context.run_id}", name="outbound-guard",
            media_type="application/json; charset=utf-8",
            event_kind="outbound_guard_result", idempotent=True,
        )
        self.recovery_pause(context)
        (self.manager.path(context.run_id) / receipt_ref["path"]).write_text(
            "{}", encoding="utf-8",
        )

        plan = plan_resume(self.manager, context.run_id)

        self.assertEqual(plan.kind, "blocked")
        self.assertIn("checksum differs", plan.reason)

    def test_model_output_before_model_started_blocks_resume(self):
        context = self.create("model-order")
        self.model(context, blocks=[{"type": "text", "text": "ok"}],
                   stop_reason="end_turn", text="ok")
        self.recovery_pause(context)

        def mutate(rows):
            rows[:] = [row for row in rows if row.get("kind") != "model_started"]
            for index, row in enumerate(rows, 1):
                row["seq"] = index
                row["id"] = f"{context.run_id}:evt:{index:08d}"
            # Keep the manifest cursor aligned so the causal validation, not a
            # trivial length mismatch, is what rejects the evidence.
            return rows

        proxy = _EventsProxy(self.manager, mutate)
        plan = plan_resume(proxy, context.run_id)
        self.assertEqual(plan.kind, "blocked")
        self.assertIn("model_output precedes model_started", plan.reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
