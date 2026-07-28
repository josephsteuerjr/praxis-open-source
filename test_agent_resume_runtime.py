from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent
import media
import run_context
import run_manager
import telegram_outbox
import workshop


RECOVERY_REASON = "process restarted; no uncertain side effect observed"


class AgentResumeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="praxis-agent-resume-")
        self.addCleanup(self._temp.cleanup)
        self.base = Path(self._temp.name)
        self.manager = run_manager.RunManager(self.base)
        self.spool = media.MediaSpool(self.base / "workspace" / "media")
        self.previous_manager = agent._RUN_MANAGER
        self.previous_spool = agent._MEDIA_SPOOL
        agent._RUN_MANAGER = self.manager
        agent._MEDIA_SPOOL = self.spool
        self.addCleanup(self._restore_agent_state)

    def _restore_agent_state(self):
        agent._RUN_MANAGER = self.previous_manager
        agent._MEDIA_SPOOL = self.previous_spool

    def _create(self, suffix: str, *, channel: agent.ChannelContext | None = None,
                history: list[dict] | None = None) -> run_context.RunContext:
        channel = channel or agent.ChannelContext(
            chat_id="100", room_id="100", principal_id="100",
            is_dm=True, owner=True, known=True, addressed=True,
            address_message_id=7, address_kind="direct",
            reply_targets=((7, "Yegor", "continue"),),
        )
        context = run_context.RunContext.create(
            run_id=f"run-agent-resume-{suffix}",
            kind="chat_turn", goal=f"resume {suffix}",
            principal_id=(agent.PRAXIS_SELF_PRINCIPAL if channel.praxis_self
                          else str(channel.principal_id)),
            scope=channel.scope,
            origin_chat_id=channel.chat_id,
            origin_message_ids=agent._run_origin_message_ids(channel),
            delivery_chat_id=channel.chat_id,
            model_profile="voice",
        )
        persisted = self.manager.create(
            context,
            agent._run_context_markdown(
                ctx=channel, kind=context.kind, goal=context.goal,
                conversation="immutable conversation", history=history,
                extra="immutable runtime frame",
            ),
        )
        self.manager.transition(persisted.run_id, "running", expected="pending")
        return self.manager.context(persisted.run_id)

    def _model(self, context: run_context.RunContext, *, blocks: list[dict],
               stop_reason: str, text: str,
               system="exact system", messages: list[dict] | None = None,
               tools: list[dict] | None = None, call_id: str = "model-one",
               guard_outbound: list[media.OutboundMedia] | None = None) -> None:
        if tools is None:
            names = []
            for block in blocks:
                if block.get("type") == "tool_use" and block.get("name") not in names:
                    names.append(block["name"])
            tools = [{"name": name} for name in names] or [{"name": "fs_read"}]
        model_input = {
            "system": system,
            "messages": list(messages or [{"role": "user", "content": "hello"}]),
            "tools": list(tools),
        }
        self.manager.store_result(
            context.run_id, json.dumps(model_input, ensure_ascii=False, indent=2),
            call_id=call_id, name="model-input", inline_chars=128,
            media_type="application/json; charset=utf-8",
            event_kind="model_input", idempotent=True,
        )
        self.manager.append_event(
            context.run_id, "model_started", call_id=call_id,
            role="voice", message_count=len(model_input["messages"]),
            tool_count=len(model_input["tools"]),
        )
        output = {
            "text": text, "blocks": blocks, "stop_reason": stop_reason,
            "framework": "test", "model": "test-model", "usage": {},
        }
        self.manager.store_result(
            context.run_id, json.dumps(output, ensure_ascii=False, indent=2),
            call_id=call_id, name="model-output", inline_chars=128,
            media_type="application/json; charset=utf-8",
            event_kind="model_output", idempotent=True,
        )
        self.manager.append_event(
            context.run_id, "model_completed", call_id=call_id,
            role="voice", stop_reason=stop_reason,
        )
        if stop_reason != "tool_use":
            agent._store_outbound_guard_input(
                context.run_id, draft=text,
                conversation="immutable conversation",
                orient="immutable runtime frame", tool_trace="", turn={},
                grounding_images=(), outbound_context="", outbound_images=(),
                repeat_discriminator="|".join(
                    item.queue_id for item in (guard_outbound or ())),
                outbound=list(guard_outbound or ()),
            )

    def _checkpoint(self, context: run_context.RunContext, *, iteration: int,
                    system, messages: list[dict], tools: list[dict],
                    outbound: list[media.OutboundMedia] | None = None) -> None:
        value = {
            "schema": "praxis.tool-loop-checkpoint.v1",
            "iteration": iteration, "system": system,
            "messages": messages, "tools": tools,
            "outbound": [{
                "kind": item.kind, "path": str(item.path), "mime": item.mime,
                "size": item.size, "target_chat_id": item.target_chat_id,
                "scope": item.scope, "caption": item.caption,
                "reply_to_message_id": item.reply_to_message_id,
                "voice_note": item.voice_note, "queue_id": item.queue_id,
                "run_id": item.run_id, "sha256": item.sha256,
                "guard_note": "",
            } for item in (outbound or ())],
        }
        self.manager.store_result(
            context.run_id, json.dumps(value, ensure_ascii=False, indent=2),
            call_id=f"checkpoint-{iteration}", name="tool-loop-checkpoint",
            inline_chars=128, media_type="application/json; charset=utf-8",
            event_kind="run_checkpoint", idempotent=True,
        )

    def _pause(self, context: run_context.RunContext,
               reason: str = RECOVERY_REASON) -> None:
        self.manager.transition(
            context.run_id, "paused", expected="running", reason=reason,
        )

    @staticmethod
    def _guard_passthrough(draft, *_args, **_kwargs):
        return draft

    def test_authored_output_reuses_exact_topic_route_without_voice_model(self):
        channel = agent.ChannelContext(
            chat_id="-10042__topic__101", room_id="-10042",
            principal_id="100", is_dm=False, owner=True, known=True,
            addressed=True, address_message_id=77, address_kind="mention",
            title="Mycelium", size=250,
            reply_targets=((77, "Yegor", "answer here"),),
        )
        context = self._create("authored-topic", channel=channel)
        self._model(
            context, blocks=[{"type": "text", "text": "exact answer"}],
            stop_reason="end_turn", text="exact answer",
        )
        self._pause(context)

        with mock.patch.object(agent.social, "owner_id", return_value="100"), \
                mock.patch.object(
                    agent, "guard_outbound_reply", side_effect=self._guard_passthrough,
                ) as guard, \
                mock.patch.object(
                    agent, "_model_call", side_effect=AssertionError("voice model called"),
                ):
            report = agent.resume_durable_run(context.run_id)

        self.assertEqual(report["plan_kind"], "authored_output")
        self.assertTrue(report["lease_acquired"])
        self.assertEqual(guard.call_count, 1)
        intent = next(
            row for row in self.manager.events(context.run_id)
            if row.get("tool") == "telegram.deliver"
        )
        args = intent["args"]
        self.assertEqual(args["chat_id"], "-10042__topic__101")
        self.assertEqual(args["text_plan"]["peer_id"], "-10042")
        self.assertEqual(args["text_plan"]["topic_id"], 101)
        self.assertEqual(args["text_plan"]["chunks"][0]["reply_to"], 77)
        self.assertEqual(args["text_plan"]["chunks"][0]["text"], "exact answer")

    def test_authored_output_without_delivery_chat_fails_terminally(self):
        channel = agent.ChannelContext(
            chat_id=None, room_id=None, principal_id=agent.PRAXIS_SELF_PRINCIPAL,
            is_dm=True, owner=False, known=True, addressed=True,
            _scope_override="owner",
        )
        context = self._create("undeliverable-authored-output", channel=channel)
        self._model(
            context, blocks=[{"type": "text", "text": "private reflection"}],
            stop_reason="end_turn", text="private reflection",
        )
        self._pause(context)

        with mock.patch.object(agent.social, "owner_id", return_value="100"), \
                mock.patch.object(
                    agent, "guard_outbound_reply", side_effect=self._guard_passthrough,
                ), \
                mock.patch.object(
                    agent, "_model_call", side_effect=AssertionError("voice model called"),
                ):
            first = agent.resume_durable_run(context.run_id)
            second = agent.resume_durable_run(context.run_id)

        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["run_status"], "failed")
        self.assertEqual(self.manager.manifest(context.run_id)["status"], "failed")
        self.assertEqual(second["plan_kind"], "not_resumable")
        self.assertEqual(second["status"], "noop")
        self.assertFalse(second["lease_acquired"])
        self.assertEqual(
            sum(row.get("control_action") == "resume_claim"
                for row in self.manager.events(context.run_id)),
            1,
        )

    def test_guard_receipt_survives_failure_and_is_not_authored_again(self):
        context = self._create("guard-receipt")
        self._model(
            context, blocks=[{"type": "text", "text": "persist me"}],
            stop_reason="end_turn", text="persist me",
        )
        self._pause(context)

        with mock.patch.object(agent.social, "owner_id", return_value="100"), \
                mock.patch.object(
                    agent, "guard_outbound_reply", side_effect=self._guard_passthrough,
                ) as guard, \
                mock.patch.object(
                    agent, "run_delivery_started", side_effect=RuntimeError("crash gap"),
                ):
            first = agent.resume_durable_run(context.run_id)
        self.assertEqual(first["status"], "failed")
        self.assertEqual(self.manager.manifest(context.run_id)["status"], "paused")
        self.assertEqual(guard.call_count, 1)

        with mock.patch.object(agent.social, "owner_id", return_value="100"), \
                mock.patch.object(
                    agent, "guard_outbound_reply",
                    side_effect=AssertionError("guard receipt was not reused"),
                ), \
                mock.patch.object(
                    agent, "_model_call", side_effect=AssertionError("voice model called"),
                ):
            second = agent.resume_durable_run(context.run_id)

        self.assertEqual(second["status"], "completed")
        receipts = [row for row in self.manager.events(context.run_id)
                    if row.get("kind") == "outbound_guard_result"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["call_id"], f"outbound-guard-v2:{context.run_id}")

    def test_dm_legacy_v1_guard_receipt_is_ignored_and_guard_reruns_from_input(self):
        context = self._create("legacy-guard-policy")
        draft = "authored under durable input"
        self._model(
            context, blocks=[{"type": "text", "text": draft}],
            stop_reason="end_turn", text=draft,
        )
        self.manager.store_result(
            context.run_id,
            json.dumps({
                "schema": "praxis.outbound-guard-result.v1",
                "draft_sha256": hashlib.sha256(draft.encode("utf-8")).hexdigest(),
                "text": "OLD COERCIVE SANITIZED TEXT",
                "media_queue_ids": [],
            }, ensure_ascii=False),
            call_id=f"outbound-guard:{context.run_id}", name="outbound-guard",
            media_type="application/json; charset=utf-8",
            event_kind="outbound_guard_result", idempotent=True,
        )
        self._pause(context)

        with mock.patch.object(agent.social, "owner_id", return_value="100"), \
                mock.patch.object(
                    agent, "guard_outbound_reply", return_value="fresh policy text",
                ) as guard, \
                mock.patch.object(
                    agent, "_model_call", side_effect=AssertionError("voice model called"),
                ):
            report = agent.resume_durable_run(context.run_id)

        self.assertEqual(report["status"], "completed")
        guard.assert_called_once()
        receipts = [row for row in self.manager.events(context.run_id)
                    if row.get("kind") == "outbound_guard_result"]
        self.assertEqual(
            [row["call_id"] for row in receipts],
            [f"outbound-guard:{context.run_id}",
             f"outbound-guard-v2:{context.run_id}"],
        )
        intent = next(row for row in self.manager.events(context.run_id)
                      if row.get("tool") == "telegram.deliver")
        self.assertEqual(intent["args"]["text_plan"]["chunks"][0]["text"],
                         "fresh policy text")

    def test_group_legacy_v1_guard_receipt_is_reused_exactly(self):
        channel = agent.ChannelContext(
            chat_id="-10042", room_id="-10042", principal_id="100",
            is_dm=False, owner=True, known=True, addressed=True,
            address_message_id=77, address_kind="mention",
            reply_targets=((77, "Yegor", "continue"),),
        )
        context = self._create("legacy-group-policy", channel=channel)
        draft = "group authored draft"
        legacy_guarded = "EXACT LEGACY GROUP DECISION"
        self._model(
            context, blocks=[{"type": "text", "text": draft}],
            stop_reason="end_turn", text=draft,
        )
        self.manager.store_result(
            context.run_id,
            json.dumps({
                "schema": "praxis.outbound-guard-result.v1",
                "draft_sha256": hashlib.sha256(draft.encode("utf-8")).hexdigest(),
                "text": legacy_guarded,
                "media_queue_ids": [],
            }, ensure_ascii=False),
            call_id=f"outbound-guard:{context.run_id}", name="outbound-guard",
            media_type="application/json; charset=utf-8",
            event_kind="outbound_guard_result", idempotent=True,
        )
        self._pause(context)

        with mock.patch.object(agent.social, "owner_id", return_value="100"), \
                mock.patch.object(
                    agent, "guard_outbound_reply",
                    side_effect=AssertionError("group v1 receipt must be reused"),
                ), \
                mock.patch.object(
                    agent, "_model_call", side_effect=AssertionError("voice model called"),
                ):
            report = agent.resume_durable_run(context.run_id)

        self.assertEqual(report["status"], "completed")
        receipts = [row for row in self.manager.events(context.run_id)
                    if row.get("kind") == "outbound_guard_result"]
        self.assertEqual([row["call_id"] for row in receipts],
                         [f"outbound-guard:{context.run_id}"])
        intent = next(row for row in self.manager.events(context.run_id)
                      if row.get("tool") == "telegram.deliver")
        self.assertEqual(intent["args"]["text_plan"]["chunks"][0]["text"],
                         legacy_guarded)

    def test_checkpoint_continuation_receives_exact_state_and_iteration(self):
        context = self._create("checkpoint")
        system = [{"type": "text", "text": "cached exact system"}]
        messages = [{"role": "user", "content": "after prior tools"}]
        tools = [{"name": "fs_read", "description": "exact tool schema"}]
        self._checkpoint(
            context, iteration=9, system=system, messages=messages, tools=tools,
        )
        self._pause(context)

        captured = {}

        def terminal(**kwargs):
            captured.update(kwargs)
            return "continued exactly"

        with mock.patch.object(agent.social, "owner_id", return_value="100"), \
                mock.patch.object(
                    agent, "guard_outbound_reply", side_effect=self._guard_passthrough,
                ), \
                mock.patch.object(agent, "_terminal_tool_loop", side_effect=terminal):
            report = agent.resume_durable_run(context.run_id)

        self.assertEqual(report["plan_kind"], "continue_checkpoint")
        self.assertEqual(captured["start_iteration"], 9)
        self.assertEqual(captured["system"], system)
        self.assertEqual(captured["messages"], messages)
        self.assertEqual(captured["tools"], tools)
        self.assertIsNone(captured["max_iters"])

    def test_tool_replay_reuses_completed_and_runs_only_safe_calls(self):
        context = self._create("tool-replay")
        blocks = [
            {"type": "tool_use", "id": "done", "name": "fs_read",
             "input": {"path": "done.txt", "optional": None}},
            {"type": "tool_use", "id": "outstanding", "name": "fs_read",
             "input": {"path": "outstanding.txt"}},
            {"type": "tool_use", "id": "pending", "name": "fs_read",
             "input": {"path": "pending.txt"}},
        ]
        self._model(context, blocks=blocks, stop_reason="tool_use", text="")
        self.manager.start_tool(
            context.run_id, "done", "fs_read", {"path": "done.txt"},
            side_effect=False,
        )
        done_ref = self.manager.store_result(
            context.run_id, "durable completed result", call_id="done",
            name="fs_read", idempotent=True,
        )
        self.manager.start_tool(
            context.run_id, "outstanding", "fs_read",
            {"path": "outstanding.txt"}, side_effect=False,
        )
        self._pause(context)
        invoked: list[str] = []
        continued = {}

        def fs_read(path):
            invoked.append(path)
            return f"fresh {path}"

        def terminal(**kwargs):
            continued.update(kwargs)
            return "tool batch continued"

        with mock.patch.object(agent.social, "owner_id", return_value="100"), \
                mock.patch.object(
                    agent, "guard_outbound_reply", side_effect=self._guard_passthrough,
                ), \
                mock.patch.object(agent, "_terminal_tool_loop", side_effect=terminal), \
                mock.patch.dict(agent.TOOL_IMPL, {"fs_read": fs_read}):
            report = agent.resume_durable_run(context.run_id)

        self.assertEqual(report["plan_kind"], "replay_model_tool_response")
        self.assertEqual(invoked, ["outstanding.txt", "pending.txt"])
        self.assertEqual(
            len([row for row in self.manager.events(context.run_id)
                 if row.get("call_id") == "done" and row.get("kind") == "tool_result"]),
            1,
        )
        user_blocks = continued["messages"][-1]["content"]
        done_block = next(row for row in user_blocks
                          if row.get("tool_use_id") == "done")
        self.assertIn(done_ref["result_id"], done_block["content"])
        self.assertEqual(continued["start_iteration"], 1)

    def test_desktop_operation_id_never_authorizes_effect_replay(self):
        context = self._create("desktop-no-replay")
        call_input = {
            "action": "click", "x": 20, "y": 30,
            "operation_id": "model-supplied-not-a-ledger",
        }
        block = {
            "type": "tool_use", "id": "desktop-one", "name": "computer",
            "input": call_input,
        }
        self._model(context, blocks=[block], stop_reason="tool_use", text="")
        self.assertTrue(agent._tool_has_side_effect("computer", call_input))
        self.assertEqual(
            agent._tool_idempotency_key(
                context, "desktop-one", "computer", call_input,
            ),
            "",
        )
        self.manager.start_tool(
            context.run_id, "desktop-one", "computer", call_input,
            side_effect=True,
        )
        self._pause(context)

        with mock.patch.dict(agent.TOOL_IMPL, {
            "computer": mock.Mock(
                side_effect=AssertionError("desktop effect was replayed"),
            ),
        }), mock.patch.object(
            agent, "_model_call", side_effect=AssertionError("model was called"),
        ):
            report = agent.resume_durable_run(context.run_id)

        self.assertEqual(report["plan_kind"], "in_doubt")
        self.assertEqual(report["status"], "noop")
        self.assertFalse(report["lease_acquired"])

    def test_direct_outbox_acceptance_wakes_exact_continuation_without_resend(self):
        context = self._create("direct-outbox")
        block = {
            "type": "tool_use", "id": "send-one", "name": "send_message",
            "input": {"to": "42", "text": "ping"},
        }
        self._model(context, blocks=[block], stop_reason="tool_use", text="")
        key = f"telegram-outbox:{context.run_id}:tool:send-one"
        self.manager.start_tool(
            context.run_id, "send-one", "send_message",
            {"to": "42", "text": "ping"}, side_effect=True,
            idempotency_key=key,
        )
        self._pause(
            context,
            reason="durable send_message intent awaits Telegram acceptance",
        )
        random_id = telegram_outbox.stable_random_id(key)
        entry = {
            "state": "accepted", "key": key, "kind": "text",
            "run_id": context.run_id, "call_id": "send-one",
            "purpose": "tool:send_message", "peer_id": 42,
            "topic_id": None, "random_id": random_id,
            "payload": {"text": "ping"},
            "receipt": {"message_id": 501, "random_id": random_id},
        }
        agent.run_direct_outbox_prepared(entry, target_label="peer 42")
        with mock.patch.dict(agent._TELETHON, {
            "project_direct_outbox_acceptance": lambda _proof, _entry: "ok",
        }):
            self.assertTrue(agent.run_direct_outbox_accepted(entry))
        continued = {}

        def terminal(**kwargs):
            continued.update(kwargs)
            return "accepted and continued"

        with mock.patch.object(agent.social, "owner_id", return_value="100"), \
                mock.patch.object(
                    agent, "guard_outbound_reply", side_effect=self._guard_passthrough,
                ), \
                mock.patch.object(agent, "_terminal_tool_loop", side_effect=terminal), \
                mock.patch.dict(agent.TOOL_IMPL, {
                    "send_message": mock.Mock(
                        side_effect=AssertionError("accepted send was replayed"),
                    ),
                }):
            report = agent.resume_durable_run(context.run_id)

        self.assertEqual(report["plan_kind"], "replay_model_tool_response")
        self.assertTrue(report["lease_acquired"])
        send_result = continued["messages"][-1]["content"][0]["content"]
        self.assertIn("ResultRef", send_result)
        self.assertEqual(self.manager.manifest(context.run_id)["status"], "running")
        self.assertTrue(any(
            row.get("tool") == "telegram.deliver"
            for row in self.manager.events(context.run_id)
        ))

    def test_direct_projection_retries_after_crash_without_duplicate_effect(self):
        context = self._create("projection-crash")
        block = {
            "type": "tool_use", "id": "send-one", "name": "send_message",
            "input": {"to": "42", "text": "ping"},
        }
        self._model(context, blocks=[block], stop_reason="tool_use", text="")
        key = f"telegram-outbox:{context.run_id}:tool:send-one"
        self.manager.start_tool(
            context.run_id, "send-one", "send_message",
            {"to": "42", "text": "ping"}, side_effect=True,
            idempotency_key=key,
        )
        self._pause(
            context,
            reason="durable send_message intent awaits Telegram acceptance",
        )
        random_id = telegram_outbox.stable_random_id(key)
        entry = {
            "state": "accepted", "key": key, "kind": "text",
            "run_id": context.run_id, "call_id": "send-one",
            "purpose": "tool:send_message", "peer_id": 42,
            "topic_id": None, "reply_to": None, "random_id": random_id,
            "payload": {"text": "ping"},
            "receipt": {"message_id": 503, "random_id": random_id},
        }
        agent.run_direct_outbox_prepared(entry, target_label="peer 42")
        callback_calls = []
        projected_keys = set()
        visible_effects = []

        def projector(proof, _entry):
            callback_calls.append(proof["entry"]["key"])
            if proof["entry"]["key"] not in projected_keys:
                projected_keys.add(proof["entry"]["key"])
                visible_effects.append(proof["entry"]["key"])
            return "projected"

        original_store = self.manager.store_result
        fail_once = {"value": True}

        def flaky_store(*args, **kwargs):
            if (kwargs.get("name") == "telegram-outbox-projection"
                    and fail_once["value"]):
                fail_once["value"] = False
                raise RuntimeError("crash after social projection")
            return original_store(*args, **kwargs)

        with mock.patch.dict(agent._TELETHON, {
            "project_direct_outbox_acceptance": projector,
        }), mock.patch.object(self.manager, "store_result", side_effect=flaky_store):
            with self.assertRaisesRegex(RuntimeError, "crash after social projection"):
                agent.run_direct_outbox_accepted(entry)
            self.assertTrue(agent.run_direct_outbox_accepted(entry))

        self.assertEqual(callback_calls, [key, key])
        self.assertEqual(visible_effects, [key])
        self.assertEqual(len([
            row for row in self.manager.events(context.run_id)
            if row.get("kind") == "direct_outbox_projection"
        ]), 1)
        self.assertEqual(len([
            row for row in self.manager.events(context.run_id)
            if row.get("kind") == "tool_result"
            and row.get("call_id") == "send-one"
            and row.get("name") == "send_message"
        ]), 1)

    def test_completed_implicit_file_restores_atomic_stage_before_checkpoint(self):
        context = self._create("staged-file-gap")
        source = self.base / "report.txt"
        source.write_text("durable report", encoding="utf-8")
        call_input = {"path": str(source), "caption": "ready"}
        block = {
            "type": "tool_use", "id": "file-one", "name": "send_file",
            "input": {**call_input, "to": None},
        }
        self._model(context, blocks=[block], stop_reason="tool_use", text="")
        channel, _snapshot = agent._load_exact_run_channel(self.manager, context)
        outbound: list[media.OutboundMedia] = []
        direct_calls = []
        channel_token = agent._TURN_CHANNEL.set(channel)
        outbound_token = agent._TURN_OUTBOUND.set(outbound)
        guard_token = agent._TURN_MEDIA_GUARD.set({})
        run_token = run_context.set_run(context)
        key = agent._tool_idempotency_key(context, "file-one", "send_file", call_input)
        self.assertEqual(key, f"turn-media-stage:{context.run_id}:tool:file-one")
        self.manager.start_tool(
            context.run_id, "file-one", "send_file", call_input,
            side_effect=True, idempotency_key=key,
        )
        execution_token = agent._TOOL_EXECUTION.set({
            "run_id": context.run_id, "call_id": "file-one", "tool": "send_file",
            "args": call_input, "side_effect": True, "idempotency_key": key,
        })
        before = agent._tool_outbound_snapshot()
        try:
            with mock.patch.object(workshop, "_resolve_read", return_value=source), \
                    mock.patch.dict(agent._TELETHON, {
                        "send_file": lambda *args: direct_calls.append(args) or "direct",
                    }):
                result = workshop.send_file(**call_input)
            self.assertIn("document", result)
            metadata = agent._tool_result_metadata(before)
            self.manager.store_result(
                context.run_id, result, call_id="file-one", name="send_file",
                idempotent=True, metadata=metadata,
            )
        finally:
            agent._TOOL_EXECUTION.reset(execution_token)
            run_context.reset_run(run_token)
            agent._TURN_MEDIA_GUARD.reset(guard_token)
            agent._TURN_OUTBOUND.reset(outbound_token)
            agent._TURN_CHANNEL.reset(channel_token)
        self.assertEqual(direct_calls, [])
        self.assertEqual(len(outbound), 1)
        staged = outbound[0]
        self._pause(context)

        with mock.patch.object(agent.social, "owner_id", return_value="100"), \
                mock.patch.object(
                    agent, "guard_outbound_reply", side_effect=self._guard_passthrough,
                ), \
                mock.patch.object(
                    agent, "_terminal_tool_loop", return_value="file ready",
                ), \
                mock.patch.object(
                    self.spool, "enqueue", side_effect=RuntimeError("handoff crash"),
                ), \
                mock.patch.dict(agent.TOOL_IMPL, {
                    "send_file": mock.Mock(
                        side_effect=AssertionError("completed stage was replayed"),
                    ),
                }):
            first = agent.resume_durable_run(context.run_id)

        self.assertEqual(first["plan_kind"], "replay_model_tool_response")
        self.assertTrue(first["lease_acquired"])
        self.assertEqual(first["status"], "failed")
        self.assertEqual(
            len([row for row in self.manager.events(context.run_id)
                 if row.get("call_id") == "file-one"
                 and row.get("kind") == "tool_result"]),
            1,
        )
        with mock.patch.object(
                agent, "_model_call", side_effect=AssertionError("model reauthored")):
            second = agent.resume_durable_run(context.run_id)
        self.assertEqual(second["plan_kind"], "transport_owned")
        self.assertTrue(second["transport_claimed"])
        pending = self.spool.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].queue_id, staged.queue_id)
        self.assertEqual(pending[0].path, staged.path)
        self.assertEqual(pending[0].sha256, staged.sha256)

    def test_owner_pause_after_direct_receipt_never_acquires_resume_lease(self):
        context = self._create("direct-owner-pause")
        block = {
            "type": "tool_use", "id": "send-one", "name": "send_message",
            "input": {"to": "42", "text": "ping"},
        }
        self._model(context, blocks=[block], stop_reason="tool_use", text="")
        key = f"telegram-outbox:{context.run_id}:tool:send-one"
        self.manager.start_tool(
            context.run_id, "send-one", "send_message",
            {"to": "42", "text": "ping"}, side_effect=True,
            idempotency_key=key,
        )
        self._pause(
            context,
            reason="durable send_message intent awaits Telegram acceptance",
        )
        self.manager.request_pause(
            context.run_id, actor="telegram:100", reason="leave it paused",
        )
        random_id = telegram_outbox.stable_random_id(key)
        entry = {
            "state": "accepted", "key": key, "kind": "text",
            "run_id": context.run_id, "call_id": "send-one",
            "purpose": "tool:send_message", "peer_id": 42,
            "topic_id": None, "random_id": random_id,
            "payload": {"text": "ping"},
            "receipt": {"message_id": 502, "random_id": random_id},
        }
        agent.run_direct_outbox_prepared(entry, target_label="peer 42")
        with mock.patch.dict(agent._TELETHON, {
            "project_direct_outbox_acceptance": lambda _proof, _entry: "ok",
        }):
            self.assertTrue(agent.run_direct_outbox_accepted(entry))

        with mock.patch.object(
                agent, "_model_call", side_effect=AssertionError("model called")):
            report = agent.resume_durable_run(context.run_id)

        self.assertEqual(report["plan_kind"], "blocked")
        self.assertFalse(report["lease_acquired"])
        manifest = self.manager.manifest(context.run_id)
        self.assertEqual(manifest["status"], "paused")
        self.assertEqual(manifest["control"]["action"], "pause")
        self.assertFalse(any(
            row.get("control_action") == "resume_claim"
            for row in self.manager.events(context.run_id)
        ))

    def test_stale_human_owner_fails_before_lease_but_praxis_self_continues(self):
        human = self._create("stale-owner")
        self._model(
            human, blocks=[{"type": "text", "text": "human answer"}],
            stop_reason="end_turn", text="human answer",
        )
        self._pause(human)
        with mock.patch.object(agent.social, "owner_id", return_value="999"), \
                mock.patch.object(
                    agent, "guard_outbound_reply",
                    side_effect=AssertionError("stale owner reached guard"),
                ):
            rejected = agent.resume_durable_run(human.run_id)
        self.assertEqual(rejected["status"], "invalid_context")
        self.assertFalse(rejected["lease_acquired"])
        self.assertEqual(self.manager.manifest(human.run_id)["status"], "paused")
        self.assertFalse(any(
            row.get("control_action") == "resume_claim"
            for row in self.manager.events(human.run_id)
        ))

        self_channel = agent.ChannelContext(
            chat_id="100", room_id="100",
            principal_id=agent.PRAXIS_SELF_PRINCIPAL,
            is_dm=True, owner=False, known=True, _scope_override="owner",
        )
        praxis = self._create("praxis-self", channel=self_channel)
        self._model(
            praxis, blocks=[{"type": "text", "text": "self answer"}],
            stop_reason="end_turn", text="self answer",
        )
        self._pause(praxis)
        with mock.patch.object(agent.social, "owner_id", return_value="999"), \
                mock.patch.object(
                    agent, "guard_outbound_reply", side_effect=self._guard_passthrough,
                ), \
                mock.patch.object(
                    agent, "_model_call", side_effect=AssertionError("voice model called"),
                ):
            accepted = agent.resume_durable_run(praxis.run_id)
        self.assertEqual(accepted["status"], "completed")
        self.assertTrue(accepted["lease_acquired"])

    def test_stale_family_and_known_membership_fail_before_lease(self):
        cases = (
            (
                "family", agent.ChannelContext(
                    chat_id="200", room_id="200", principal_id="200",
                    is_dm=True, owner=False, known=True, family=True,
                ),
            ),
            (
                "known", agent.ChannelContext(
                    chat_id="300", room_id="300", principal_id="300",
                    is_dm=True, owner=False, known=True, family=False,
                ),
            ),
        )
        for suffix, channel in cases:
            with self.subTest(scope=suffix):
                context = self._create(f"stale-{suffix}", channel=channel)
                self._model(
                    context, blocks=[{"type": "text", "text": "never"}],
                    stop_reason="end_turn", text="never",
                )
                self._pause(context)
                with mock.patch.object(agent.social, "owner_id", return_value="100"), \
                        mock.patch.object(agent.social, "is_family", return_value=False), \
                        mock.patch.object(agent.social, "category", return_value="unknown"), \
                        mock.patch.object(
                            agent, "_model_call",
                            side_effect=AssertionError("stale contact called model"),
                        ):
                    report = agent.resume_durable_run(context.run_id)

                self.assertEqual(report["status"], "invalid_context")
                self.assertFalse(report["lease_acquired"])
                self.assertEqual(
                    self.manager.manifest(context.run_id)["status"], "paused",
                )
                self.assertFalse(any(
                    row.get("control_action") == "resume_claim"
                    for row in self.manager.events(context.run_id)
                ))

    def test_missing_authority_snapshot_cannot_borrow_process_globals(self):
        context = self._create("missing-authority")
        self._model(
            context, blocks=[{"type": "text", "text": "never"}],
            stop_reason="end_turn", text="never",
        )
        self._pause(context)
        (self.manager.path(context.run_id) / "context.md").write_text(
            "# legacy context without immutable authority\n", encoding="utf-8",
        )
        with mock.patch.object(agent.social, "owner_id", return_value="100"), \
                mock.patch.object(agent, "_CURRENT_SCOPE", "owner"), \
                mock.patch.object(agent, "_CURRENT_CHAT", "100"), \
                mock.patch.object(
                    agent, "_model_call", side_effect=AssertionError("model called"),
                ):
            report = agent.resume_durable_run(context.run_id)

        self.assertEqual(report["status"], "invalid_context")
        self.assertFalse(report["lease_acquired"])
        self.assertIn("context", report["reason"])
        self.assertIn("bytes changed", report["reason"])

    def test_valid_authority_rewrite_is_rejected_by_context_digest(self):
        context = self._create("valid-authority-tamper")
        self._model(
            context, blocks=[{"type": "text", "text": "never"}],
            stop_reason="end_turn", text="never",
        )
        self._pause(context)
        path = self.manager.path(context.run_id) / "context.md"
        original = path.read_text(encoding="utf-8")
        self.assertIn('"title": null', original)
        path.write_text(
            original.replace('"title": null', '"title": "forged"', 1),
            encoding="utf-8",
        )

        with mock.patch.object(agent.social, "owner_id", return_value="100"), \
                mock.patch.object(
                    agent, "_model_call", side_effect=AssertionError("model called"),
                ):
            report = agent.resume_durable_run(context.run_id)

        self.assertEqual(report["status"], "invalid_context")
        self.assertFalse(report["lease_acquired"])
        self.assertIn("context bytes changed", report["reason"])
        self.assertFalse(any(
            row.get("control_action") == "resume_claim"
            for row in self.manager.events(context.run_id)
        ))

    def test_transport_owned_reconciliation_never_acquires_lease_or_model(self):
        context = self._create("transport-owned")
        self._model(
            context, blocks=[{"type": "text", "text": "send once"}],
            stop_reason="end_turn", text="send once",
        )
        self._pause(context)
        with mock.patch.object(agent.social, "owner_id", return_value="100"), \
                mock.patch.object(
                    agent, "guard_outbound_reply", side_effect=self._guard_passthrough,
                ), \
                mock.patch.object(
                    agent, "_model_call", side_effect=AssertionError("voice model called"),
                ):
            authored = agent.resume_durable_run(context.run_id)
        self.assertEqual(authored["status"], "completed")
        self.manager.recover()
        resume_claims_before = sum(
            row.get("control_action") == "resume_claim"
            for row in self.manager.events(context.run_id)
        )

        with mock.patch.object(
                agent, "_model_call", side_effect=AssertionError("transport called model")):
            transport = agent.resume_durable_run(context.run_id)

        self.assertEqual(transport["plan_kind"], "transport_owned")
        self.assertEqual(transport["status"], "noop")
        self.assertFalse(transport["lease_acquired"])
        self.assertEqual(
            sum(row.get("control_action") == "resume_claim"
                for row in self.manager.events(context.run_id)),
            resume_claims_before,
        )

    def test_resume_scan_reaches_effectful_run_beyond_permanent_noops(self):
        blocked_ids = []
        for index in range(25):
            context = self._create(f"starved-blocked-{index}")
            self._pause(context)
            blocked_ids.append(context.run_id)
        tail = self._create("starved-effectful")
        self._pause(tail)
        seen = []

        def resume_one(run_id):
            seen.append(run_id)
            if run_id == tail.run_id:
                return {
                    "run_id": run_id, "plan_kind": "continue_checkpoint",
                    "status": "completed", "lease_acquired": True,
                    "effects_started": True,
                }
            return {
                "run_id": run_id, "plan_kind": "blocked", "status": "noop",
                "lease_acquired": False, "effects_started": False,
            }

        with mock.patch.object(agent, "resume_durable_run", side_effect=resume_one):
            reports = agent.resume_durable_runs(limit=1)

        self.assertEqual(set(seen), set(blocked_ids + [tail.run_id]))
        self.assertEqual(len(seen), len(blocked_ids) + 1)
        self.assertEqual(seen[-1], tail.run_id)
        self.assertTrue(any(row.get("run_id") == tail.run_id for row in reports))


if __name__ == "__main__":
    unittest.main(verbosity=2)
