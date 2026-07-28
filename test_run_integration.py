from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import agent
import run_context
import run_manager


class RunIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = run_manager.RunManager(self.temp.name)
        self.previous = agent._RUN_MANAGER
        agent._RUN_MANAGER = self.manager

    def tearDown(self):
        agent._RUN_MANAGER = self.previous
        self.temp.cleanup()

    def _context(self, goal="test run"):
        ctx = run_context.RunContext.create(
            kind="test", goal=goal, principal_id="owner", scope="owner",
            origin_chat_id="1", delivery_chat_id="1",
        )
        ctx = self.manager.create(ctx, "FULL CONTEXT\nline two\n")
        self.manager.transition(ctx.run_id, "running")
        return ctx.with_status("running")

    def test_terminal_recap_reveals_prompt_blind_identity_load(self):
        ctx = self._context("blind experiment")
        snapshot = [{"theme": "data boundary", "score": 8.36, "count": 10, "last": 1}]

        self.assertTrue(agent._finish_durable_run(
            ctx.run_id, "done", final_text="stopped after one pass",
            reason="long work run completed",
            details={"blind_identity_load": snapshot}, strict=True,
        ))

        recap = (self.manager.path(ctx.run_id) / "RECAP.md").read_text("utf-8")
        context = (self.manager.path(ctx.run_id) / "context.md").read_text("utf-8")
        self.assertIn("## Blinded experiment reveal", recap)
        self.assertIn('"score":8.36', recap)
        self.assertNotIn("8.36", context)

    def test_voice_creates_terminal_run_with_context_and_recap(self):
        response = types.SimpleNamespace(stop_reason="end_turn", text="finished", blocks=[])
        ctx = agent.ChannelContext(chat_id="1", principal_id="1", owner=True)
        with mock.patch.object(agent, "build_system_parts", return_value=("persona", "dynamic")), \
                mock.patch.object(agent, "_system", return_value="system"), \
                mock.patch.object(agent.llm, "chat", return_value=response):
            answer = agent._voice("the complete conversation", [], "Yegor", ctx=ctx,
                                  no_tools=True)

        self.assertEqual(answer, "finished")
        manifests = list(Path(self.temp.name).glob("memory/runs/*/*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        run_dir = manifests[0].parent
        self.assertEqual(self.manager.manifest(run_dir.name)["status"], "done")
        self.assertIn("the complete conversation", (run_dir / "context.md").read_text("utf-8"))
        recap = (run_dir / "RECAP.md").read_text("utf-8")
        self.assertIn("## Authored output", recap)
        self.assertIn("finished", recap)
        self.assertNotIn("## Final state", recap)

    def test_internal_task_window_recap_is_automatic_and_evidence_based(self):
        ctx = run_context.RunContext.create(
            kind="task_window", goal="inspect receipts", principal_id="praxis:self",
            scope="owner", origin_chat_id=None, delivery_chat_id=None,
        )
        ctx = self.manager.create(ctx, "immutable task context")
        self.manager.transition(ctx.run_id, "running")
        self.manager.start_tool(ctx.run_id, "tool:1", "probe", {})
        self.manager.store_result(
            ctx.run_id, "observed tool result", call_id="tool:1", name="probe",
            event_kind="tool_result",
        )

        self.assertTrue(agent._finish_durable_run(
            ctx.run_id, "done", final_text="I inspected the evidence.",
            reason="long work run completed", strict=True,
        ))
        recap = (self.manager.path(ctx.run_id) / "RECAP.md").read_text("utf-8")
        self.assertIn("## Authored output", recap)
        self.assertIn("I inspected the evidence.", recap)
        # 26.07: рекап больше не утверждает НЕВОЗМОЖНОСТЬ отправки по виду рана. Вид её не
        # знает: окно копит отправку в outbox, а часовой пульс с этого дня идёт с живой
        # связью и шлёт прямо в нём. Отсутствие расписки — факт, невозможность — домысел;
        # это тот же класс лжи, что «Telegram жив» в рамке, за который уже платили.
        self.assertIn("No advisor receipt was recorded for this run.", recap)
        self.assertIn("No delivery receipt was recorded for this run.", recap)
        self.assertNotIn("have no Telegram delivery target", recap)
        self.assertIn("`probe` → `", recap)
        self.assertNotIn("observed tool result", recap)

    def test_chat_recap_projects_advisor_decision_without_private_payload(self):
        ctx = run_context.RunContext.create(
            kind="chat_turn", goal="reply safely", principal_id="praxis:self",
            scope="group", origin_chat_id="9", delivery_chat_id="9",
        )
        ctx = self.manager.create(ctx, "private raw conversation must stay out of recap")
        self.manager.transition(ctx.run_id, "running")
        receipt = {
            "schema": agent._OUTBOUND_GUARD_RECEIPT_SCHEMA,
            "draft_sha256": hashlib.sha256(b"authored secret draft").hexdigest(),
            "text": "authored secret draft",
            "media_queue_ids": ["private-queue-id"],
            "advisor": "privacy",
            "advisor_verdict": "молчи",
            "advisor_reason": "destination lacks authority; quote: authored secret draft; queue private-queue-id; conversation private raw conversation",
            "praxis_decision": "hold_for_data_authority",
        }
        self.manager.store_result(
            ctx.run_id, json.dumps(receipt, ensure_ascii=False),
            call_id=f"{agent._OUTBOUND_GUARD_RECEIPT_CALL_PREFIX}:{ctx.run_id}",
            name="outbound-guard", event_kind="outbound_guard_result", idempotent=True,
            media_type="application/json; charset=utf-8",
        )

        self.assertTrue(agent._finish_durable_run(
            ctx.run_id, "done", final_text="", reason="outbound held", strict=True,
        ))
        recap = (self.manager.path(ctx.run_id) / "RECAP.md").read_text("utf-8")
        self.assertIn("## Advisor decision", recap)
        self.assertIn("- Advisor: `privacy`", recap)
        self.assertIn("- Verdict: `молчи`", recap)
        self.assertIn("- Reason recorded: `yes`", recap)
        self.assertIn("- Praxis decision: `hold_for_data_authority`", recap)
        self.assertIn("Full reason remains inside the integrity-checked advisor receipt", recap)
        self.assertNotIn("authored secret draft", recap)
        self.assertNotIn("private-queue-id", recap)
        self.assertNotIn("private raw conversation", recap)

    def test_chat_recap_projects_advisor_pass_and_owner_skip(self):
        cases = [
            ("privacy", "ok", "", "send_authored"),
            ("not_run_owner_audience", "", "", "send_authored"),
        ]
        for advisor, verdict, reason, decision in cases:
            with self.subTest(advisor=advisor):
                ctx = run_context.RunContext.create(
                    kind="chat_turn", goal="reply", principal_id="praxis:self",
                    scope="owner", origin_chat_id="1", delivery_chat_id="1",
                )
                ctx = self.manager.create(ctx, "immutable context")
                self.manager.transition(ctx.run_id, "running")
                receipt = {
                    "schema": agent._OUTBOUND_GUARD_RECEIPT_SCHEMA,
                    "draft_sha256": hashlib.sha256(b"draft").hexdigest(),
                    "text": "draft", "media_queue_ids": [], "advisor": advisor,
                    "advisor_verdict": verdict, "advisor_reason": reason,
                    "praxis_decision": decision,
                }
                self.manager.store_result(
                    ctx.run_id, json.dumps(receipt, ensure_ascii=False),
                    call_id=f"{agent._OUTBOUND_GUARD_RECEIPT_CALL_PREFIX}:{ctx.run_id}",
                    name="outbound-guard", event_kind="outbound_guard_result",
                    idempotent=True, media_type="application/json; charset=utf-8",
                )
                self.assertTrue(agent._finish_durable_run(
                    ctx.run_id, "done", final_text="draft", reason="completed", strict=True,
                ))
                recap = (self.manager.path(ctx.run_id) / "RECAP.md").read_text("utf-8")
                self.assertIn(f"- Advisor: `{advisor}`", recap)
                self.assertIn(f"- Praxis decision: `{decision}`", recap)
                self.assertNotIn("draft_sha256", recap)

    def test_delivery_recap_hides_receipt_locators_and_skip_reason(self):
        ctx = run_context.RunContext.create(
            kind="chat_turn", goal="deliver", principal_id="praxis:self",
            scope="group", origin_chat_id="9", delivery_chat_id="9",
        )
        ctx = self.manager.create(ctx, "immutable context")
        self.manager.transition(ctx.run_id, "running")
        for index, name in enumerate(("telegram-text", "telegram-media", "telegram-delivery")):
            call_id = f"delivery-{index}"
            self.manager.start_tool(ctx.run_id, call_id, name, {})
            ref = self.manager.store_result(
                ctx.run_id, json.dumps({"ok": True, "text": "secret sent text"}),
                call_id=call_id, name=name, event_kind="tool_result",
                media_type="application/json; charset=utf-8",
            )
            self.assertTrue(ref.get("result_id"))
        self.manager.append_event_once(
            ctx.run_id, "delivery_skipped", f"delivery-skipped:{ctx.run_id}",
            reason="secret skip reason with message-id 123",
        )

        self.assertTrue(agent._finish_durable_run(
            ctx.run_id, "done", final_text="", reason="completed", strict=True,
        ))
        recap = (self.manager.path(ctx.run_id) / "RECAP.md").read_text("utf-8")
        self.assertIn("## Delivery outcome", recap)
        self.assertNotIn("secret sent text", recap)
        self.assertNotIn("secret skip reason", recap)
        for name in ("telegram-text", "telegram-media", "telegram-delivery"):
            self.assertNotIn(f"`{name}` →", recap)

    def test_corrupt_advisor_receipt_does_not_terminalize_without_recap(self):
        ctx = run_context.RunContext.create(
            kind="chat_turn", goal="fail closed", principal_id="praxis:self",
            scope="group", origin_chat_id="9", delivery_chat_id="9",
        )
        ctx = self.manager.create(ctx, "immutable context")
        self.manager.transition(ctx.run_id, "running")
        self.manager.store_result(
            ctx.run_id, "not-json", call_id=f"{agent._OUTBOUND_GUARD_RECEIPT_CALL_PREFIX}:{ctx.run_id}",
            name="outbound-guard", event_kind="outbound_guard_result", idempotent=True,
            media_type="application/json; charset=utf-8",
        )

        with self.assertRaises(agent.DurableExecutionError):
            agent._finish_durable_run(ctx.run_id, "done", reason="held", strict=True)
        self.assertEqual(self.manager.manifest(ctx.run_id)["status"], "running")
        self.assertFalse((self.manager.path(ctx.run_id) / "RECAP.md").exists())

    def _store_v2_advisor_receipt(self, run_id):
        # Previous-schema (v2) receipt: predates the advisor provenance fields,
        # written under the same current call id as v3.
        receipt = {
            "schema": agent._OUTBOUND_GUARD_PREVIOUS_SCHEMA,
            "draft_sha256": hashlib.sha256(b"legacy draft").hexdigest(),
            "text": "legacy secret draft",
            "media_queue_ids": [],
        }
        self.manager.store_result(
            run_id, json.dumps(receipt, ensure_ascii=False),
            call_id=f"{agent._OUTBOUND_GUARD_RECEIPT_CALL_PREFIX}:{run_id}",
            name="outbound-guard", event_kind="outbound_guard_result",
            idempotent=True, media_type="application/json; charset=utf-8",
        )

    def test_recap_tolerates_previous_v2_advisor_receipt_schema(self):
        # A terminal run whose advisor receipt uses the PREVIOUS (v2) schema must
        # still produce a RECAP.  The authoritative receipt reader already tolerates
        # v2 for this call id; the recap projector must mirror it rather than raise
        # "advisor receipt uses an unsupported schema".  v2 lacks provenance, so the
        # projection falls back to graceful defaults without leaking the payload.
        ctx = run_context.RunContext.create(
            kind="chat_turn", goal="reply", principal_id="praxis:self",
            scope="owner", origin_chat_id="1", delivery_chat_id="1",
        )
        ctx = self.manager.create(ctx, "immutable context")
        self.manager.transition(ctx.run_id, "running")
        self._store_v2_advisor_receipt(ctx.run_id)

        self.assertTrue(agent._finish_durable_run(
            ctx.run_id, "done", final_text="", reason="completed", strict=True,
        ))
        recap = (self.manager.path(ctx.run_id) / "RECAP.md").read_text("utf-8")
        self.assertIn("## Advisor decision", recap)
        self.assertIn("- Advisor: `unknown`", recap)
        self.assertIn("- Verdict: `not_recorded`", recap)
        self.assertIn("- Reason recorded: `no`", recap)
        self.assertIn("- Praxis decision: `not_recorded`", recap)
        self.assertNotIn("legacy secret draft", recap)

    def test_recovery_rebuilds_recap_for_terminal_run_with_v2_receipt(self):
        # Production regression (the ~10x-per-boot noise): a run terminalized
        # (failed/cancelled) whose stored advisor receipt uses the v2 schema and
        # whose RECAP was never written.  Recovery must rebuild the recap once and
        # then go quiet, not re-raise DurableExecutionError on every boot.
        ctx = run_context.RunContext.create(
            kind="chat_turn", goal="reply", principal_id="praxis:self",
            scope="owner", origin_chat_id="1", delivery_chat_id="1",
        )
        ctx = self.manager.create(ctx, "immutable context")
        self.manager.transition(ctx.run_id, "running")
        self._store_v2_advisor_receipt(ctx.run_id)
        self.manager.transition(
            ctx.run_id, "failed",
            reason="authored output is permanently undeliverable",
        )
        self.assertFalse((self.manager.path(ctx.run_id) / "RECAP.md").exists())

        reports = agent.recover_durable_runs()

        self.assertIn({"run_id": ctx.run_id, "recap": "recovered"}, reports)
        self.assertTrue((self.manager.path(ctx.run_id) / "RECAP.md").is_file())
        recap = (self.manager.path(ctx.run_id) / "RECAP.md").read_text("utf-8")
        self.assertIn("## Advisor decision", recap)
        self.assertIn("- Advisor: `unknown`", recap)
        self.assertNotIn("legacy secret draft", recap)

        # Idempotent and quiet: a second recovery pass neither raises nor
        # re-reports this run (the boot noise is gone for good).
        self.assertNotIn(
            {"run_id": ctx.run_id, "recap": "recovered"},
            agent.recover_durable_runs(),
        )
        self.assertEqual(len([
            row for row in self.manager.events(ctx.run_id)
            if row.get("kind") in {"recap_written", "recap_recovered"}
        ]), 1)

    def test_full_tool_result_is_externalized_and_cursor_readable(self):
        current = self._context()
        payload = "HEAD\n" + ("middle\n" * 2000) + "TAIL"
        seen = []

        def fake_chat(*_args, **kwargs):
            seen.append(copy.deepcopy(kwargs["messages"]))
            if len(seen) == 1:
                return types.SimpleNamespace(stop_reason="tool_use", text="", blocks=[{
                    "type": "tool_use", "id": "call-1", "name": "huge", "input": {},
                }])
            return types.SimpleNamespace(stop_reason="end_turn", text="done", blocks=[])

        with run_context.bind_run(current), \
                mock.patch.object(agent.llm, "chat", side_effect=fake_chat), \
                mock.patch.dict(agent.TOOL_IMPL, {"huge": lambda: payload}):
            self.assertEqual(agent._terminal_tool_loop(
                system="s", messages=[], tools=[{"name": "huge"}],
            ), "done")

        result_event = next(row for row in self.manager.events(current.run_id)
                            if row.get("kind") == "tool_result")
        ref = result_event["result"]
        self.assertEqual(self.manager.read_result(current.run_id, ref["result_id"],
                                                  byte_limit=1000000)["text"], payload)
        inline = seen[-1][-1]["content"][0]["content"]
        self.assertIn("ResultRef", inline)
        self.assertIn("HEAD", inline)
        self.assertIn("TAIL", inline)

    def test_account_critical_tool_parameters_never_enter_durable_run_files(self):
        current = self._context("critical telegram redaction")
        secret = "auth-secret-731946"
        seen = []

        def fake_chat(*_args, **kwargs):
            seen.append(copy.deepcopy(kwargs["messages"]))
            if len(seen) == 1:
                return types.SimpleNamespace(stop_reason="tool_use", text="", blocks=[{
                    "type": "tool_use", "id": "critical-call-1",
                    "name": "telegram_account", "input": {
                        "action": "call",
                        "request": "functions.auth.SignInRequest",
                        "params": {"phone_number": "+100000000", "phone_code": secret},
                    },
                }])
            return types.SimpleNamespace(stop_reason="end_turn", text="done", blocks=[])

        with run_context.bind_run(current), \
                mock.patch.object(agent.llm, "chat", side_effect=fake_chat), \
                mock.patch.dict(agent.TOOL_IMPL, {
                    "telegram_account": lambda **_kwargs: (
                        '{"action":"challenge","debug":"' + secret
                        + '","challenge":{"status":"pending"}}'
                    ),
                }):
            self.assertEqual(agent._terminal_tool_loop(
                system="s", messages=[], tools=[{"name": "telegram_account"}],
            ), "done")

        run_dir = self.manager.path(current.run_id)
        for path in run_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret.encode("utf-8"), path.read_bytes(), str(path))
        started = next(
            row for row in self.manager.events(current.run_id)
            if row.get("kind") == "tool_started"
        )
        marker = started["args"]["params"]["_praxis_redacted"]
        self.assertEqual(marker["schema"], agent._CRITICAL_PARAM_MARKER)
        self.assertEqual(set(marker), {"schema", "commitment"})
        self.assertRegex(marker["commitment"], r"^[0-9a-f]{64}$")
        # Live model continuity may see its own exact call; only disk persistence is redacted.
        self.assertEqual(seen[0], [])
        self.assertIn(secret, json.dumps(seen[1], ensure_ascii=False))
        key_hex = agent._CRITICAL_PARAM_COMMITMENT_KEY.hex().encode("ascii")
        for path in run_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(key_hex, path.read_bytes(), str(path))

    def test_critical_alias_redaction_is_fail_closed_and_idempotent(self):
        secret = "otp-731946"
        alias_call = {
            "action": "call",
            "request": "auth.SignInRequest",
            "params": {"phone_number": "+100000000", "phone_code": secret},
            "params_json": json.dumps({"phone_code": secret}),
            "confirm": "legacy-confirm-secret",
        }

        first = agent._durable_tool_input("telegram_account", alias_call)
        second = agent._durable_tool_input("telegram_account", first)
        self.assertEqual(first, second)
        self.assertEqual(
            first, agent._durable_tool_input("telegram_account", alias_call),
        )
        with mock.patch.object(
            agent, "_CRITICAL_PARAM_COMMITMENT_KEY", b"new-process-key" * 2,
        ):
            # Recovery sees the tombstone, not raw parameters, and therefore
            # preserves it even though a restarted process owns another key.
            self.assertEqual(
                first, agent._durable_tool_input("telegram_account", first),
            )
        self.assertNotIn(secret, json.dumps(first, ensure_ascii=False))
        marker = first["params"]["_praxis_redacted"]
        self.assertEqual(marker["schema"], agent._CRITICAL_PARAM_MARKER)
        self.assertEqual(set(marker), {"schema", "commitment"})
        plain_payload = json.dumps({
            "params": alias_call["params"],
            "params_json": alias_call["params_json"],
            "confirm": alias_call["confirm"],
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
           allow_nan=False, default=str).encode("utf-8")
        self.assertNotEqual(
            marker["commitment"], hashlib.sha256(plain_payload).hexdigest(),
        )

        legacy_digest = "a" * 64
        legacy = agent._durable_tool_input("telegram_account", {
            "action": "call", "request": "auth.SignInRequest",
            "params": {"_praxis_redacted": {
                "schema": agent._CRITICAL_PARAM_MARKER_LEGACY,
                "sha256": legacy_digest, "bytes": 42,
            }},
        })
        self.assertEqual(
            legacy["params"]["_praxis_redacted"]["schema"],
            agent._CRITICAL_PARAM_MARKER,
        )
        self.assertNotIn(legacy_digest, json.dumps(legacy, ensure_ascii=False))
        self.assertEqual(
            legacy, agent._durable_tool_input("telegram_account", legacy),
        )

        unknown = agent._durable_tool_input("telegram_account", {
            "action": "call", "request": "auth.SignInnRequest",
            "params": {"phone_code": secret},
        })
        self.assertIn("_praxis_redacted", unknown["params"])
        self.assertNotIn(secret, json.dumps(unknown, ensure_ascii=False))

        standard = {
            "action": "call", "request": "messages.SendMessageRequest",
            "params": {"peer": "me", "message": "hello"},
        }
        self.assertEqual(
            agent._durable_tool_input("telegram_account", standard), standard,
        )

    def test_run_scoped_scrub_covers_sibling_blocks_later_output_and_recap(self):
        current = self._context("critical telegram run-scoped scrub")
        secret = 'auth-secret-"731946"'
        seen = []

        def fake_chat(*_args, **kwargs):
            seen.append(copy.deepcopy(kwargs["messages"]))
            if len(seen) == 1:
                return types.SimpleNamespace(stop_reason="tool_use", text=secret, blocks=[
                    {"type": "text", "text": "using " + secret},
                    {
                        "type": "tool_use", "id": "critical-alias-call",
                        "name": "telegram_account", "input": {
                            "action": "call", "request": "auth.SignInRequest",
                            "params": {
                                "phone_number": "+100000000", "phone_code": secret,
                            },
                        },
                    },
                ])
            return types.SimpleNamespace(
                stop_reason="end_turn", text="terminal echo " + secret,
                blocks=[{"type": "text", "text": "block echo " + secret}],
                framework=secret, model=secret, usage={"debug": secret},
            )

        with run_context.bind_run(current), \
                mock.patch.object(agent.llm, "chat", side_effect=fake_chat), \
                mock.patch.dict(agent.TOOL_IMPL, {
                    "telegram_account": lambda **_kwargs: json.dumps(
                        {"debug": secret, "status": "pending"}, ensure_ascii=True,
                    ),
                }):
            answer = agent._terminal_tool_loop(
                system="system",
                messages=[], tools=[{"name": "telegram_account"}],
            )

        self.assertNotIn(secret, answer)
        self.assertIn(agent._CRITICAL_PARAM_REPLACEMENT, answer)
        # The second live model call still sees its own exact first call.
        self.assertIn(secret, seen[1][0]["content"][0]["text"])

        self.assertTrue(agent._finish_durable_run(
            current.run_id, "done", final_text=answer, strict=True,
        ))
        run_dir = self.manager.path(current.run_id)
        for path in run_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret.encode("utf-8"), path.read_bytes(), str(path))
        recap = (run_dir / "RECAP.md").read_text("utf-8")
        self.assertIn(agent._CRITICAL_PARAM_REPLACEMENT, recap)

    def test_delivery_receipt_owns_terminal_transition(self):
        current = self._context("deliver")
        agent.run_delivery_started(current.run_id, chat_id="1", text_chars=5, media_count=0)
        agent.run_delivery_completed(current.run_id, text="hello", message_ids=["77"])

        manifest = self.manager.manifest(current.run_id)
        self.assertEqual(manifest["status"], "done")
        self.assertTrue((self.manager.path(current.run_id) / "RECAP.md").is_file())
        events = self.manager.events(current.run_id)
        self.assertTrue(any(row.get("kind") == "tool_started" for row in events))
        self.assertTrue(any(row.get("kind") == "tool_result" for row in events))

    def _started_replayable_delivery(self, run_id, text="hi"):
        """Open an outstanding delivery call whose text plan is replayable."""
        plan = {
            "schema": agent._TELEGRAM_TEXT_PLAN_SCHEMA,
            "conversation_id": "1", "peer_id": "1", "topic_id": None,
            "chunks": [{
                "index": 0, "text": text,
                "delivery_key": f"run:{run_id}:chunk:0", "reply_to": None,
            }],
        }
        agent.run_delivery_started(
            run_id, chat_id="1", text_chars=len(text), media_count=0, text_plan=plan,
        )

    def test_permanent_refusal_terminalizes_despite_outstanding_call(self):
        # A chat she can no longer post to (ChatAdminRequired) refuses the exact
        # route forever.  Replaying it every tick hammers the account, so the run
        # must go terminal — and it must do so even though the delivery call is
        # still OUTSTANDING (this is what previously RunConflicted and left the
        # delivery looping blocked).
        ctx = self._context("blocked delivery")
        run_id = ctx.run_id
        self._started_replayable_delivery(run_id)
        self.assertTrue(agent._delivery_evidence(run_id)["replayable_text"])

        class ChatAdminRequiredError(Exception):
            pass

        agent.run_delivery_failed(run_id, ChatAdminRequiredError("admin required"))

        self.assertEqual(self.manager.manifest(run_id)["status"], "failed")
        events = self.manager.events(run_id)
        # The outstanding delivery call is CLOSED so nothing re-serves it.
        self.assertTrue(any(
            row.get("kind") == "tool_failed"
            and row.get("call_id") == f"delivery:{run_id}"
            for row in events
        ))

    def test_retryable_delivery_error_only_blocks_never_terminalizes(self):
        # A transient error (not a permanent refusal) must NOT be terminalized:
        # the durable plan is preserved so a later tick can resolve it.
        ctx = self._context("retryable delivery")
        run_id = ctx.run_id
        self._started_replayable_delivery(run_id)

        agent.run_delivery_failed(run_id, TimeoutError("network hiccup"))

        self.assertEqual(self.manager.manifest(run_id)["status"], "blocked")
        events = self.manager.events(run_id)
        self.assertFalse(any(row.get("kind") == "tool_failed" for row in events))

    def test_permanent_refusal_terminalizes_even_when_recap_build_fails(self):
        # The legacy case the fallback exists for: a run whose RECAP cannot be
        # built (e.g. an old advisor-receipt schema) must STILL terminalize on a
        # permanent refusal. _finish_durable_run swallows its own errors and
        # returns False by default, so without strict=True the force-terminal
        # fallback would be dead code and the run would loop forever.
        ctx = self._context("legacy recap failure")
        run_id = ctx.run_id
        self._started_replayable_delivery(run_id)

        class ChatAdminRequiredError(Exception):
            pass

        with mock.patch.object(
            agent, "_run_recap_markdown",
            side_effect=agent.DurableExecutionError("advisor receipt uses an unsupported schema"),
        ):
            agent.run_delivery_failed(run_id, ChatAdminRequiredError("admin required"))

        # Terminalized via the direct-transition fallback despite the recap failure.
        self.assertEqual(self.manager.manifest(run_id)["status"], "failed")
        events = self.manager.events(run_id)
        self.assertTrue(any(
            row.get("kind") == "tool_failed"
            and row.get("call_id") == f"delivery:{run_id}"
            for row in events
        ))

    def test_silent_completion_creates_zero_effect_intent_itself(self):
        current = self._context("choose silence")

        self.assertTrue(agent.run_delivery_completed(current.run_id, silent=True))

        self.assertEqual(self.manager.manifest(current.run_id)["status"], "done")
        events = self.manager.events(current.run_id)
        intent = next(row for row in events if row.get("kind") == "tool_started")
        self.assertEqual(intent["args"]["text_chars"], 0)
        self.assertEqual(intent["args"]["media_count"], 0)
        self.assertEqual(sum(row.get("kind") == "delivery_skipped" for row in events), 1)
        self.assertTrue((self.manager.path(current.run_id) / "RECAP.md").is_file())

    def test_reducer_waits_for_every_expected_media_receipt(self):
        current = self._context("queued delivery")
        agent.run_delivery_started(
            current.run_id, chat_id="1", text_chars=5, media_count=1,
            media_queue_ids=["queue-one"],
        )
        agent.run_delivery_text_accepted(current.run_id, text="hello", message_ids=["77"])
        agent.run_delivery_media_started(current.run_id, "queue-one")
        agent.run_delivery_media_started(current.run_id, "different-queue")
        agent.run_delivery_media_result(
            current.run_id, "different-queue", ok=True, message_id="87",
        )
        self.assertTrue(agent.run_delivery_blocked(current.run_id, reason="upload queued"))

        self.assertFalse(agent.run_delivery_finalize_recovered(current.run_id, media_count=999))
        self.assertEqual(self.manager.manifest(current.run_id)["status"], "blocked")
        self.assertEqual(agent._delivery_evidence(current.run_id)["observed_media_count"], 0)

        agent.run_delivery_media_result(
            current.run_id, "queue-one", ok=True, message_id="88",
        )
        self.assertTrue(agent.run_delivery_finalize_recovered(current.run_id, media_count=0))
        self.assertEqual(self.manager.manifest(current.run_id)["status"], "done")
        self.assertTrue((self.manager.path(current.run_id) / "RECAP.md").is_file())

    def test_versioned_text_plan_replays_only_missing_chunks_and_then_reconciles(self):
        current = self._context("durable topic text")
        chunks = ["first ", "second"]
        plan = {
            "schema": agent._TELEGRAM_TEXT_PLAN_SCHEMA,
            "conversation_id": "-10042__topic__101",
            "peer_id": "-10042",
            "topic_id": 101,
            "chunks": [{
                "index": index,
                "text": text,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "delivery_key": f"run:{current.run_id}:chunk:{index}",
                "reply_to": 77 if index == 0 else 101,
            } for index, text in enumerate(chunks)],
        }
        agent.run_delivery_started(
            current.run_id, chat_id=plan["conversation_id"],
            text_chars=len("".join(chunks)), media_count=0,
            text_plan=plan, media_queue_ids=[],
        )
        agent.run_delivery_text_chunk_accepted(
            current.run_id, index=0,
            delivery_key=f"run:{current.run_id}:chunk:0", message_id="501",
        )

        self.manager.recover()
        pending = agent.run_pending_text_deliveries()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["peer_id"], "-10042")
        self.assertEqual(pending[0]["topic_id"], 101)
        self.assertEqual([row["index"] for row in pending[0]["pending_chunks"]], [1])
        self.assertEqual(pending[0]["pending_chunks"][0]["reply_to"], 101)

        agent.run_delivery_text_chunk_accepted(
            current.run_id, index=1,
            delivery_key=f"run:{current.run_id}:chunk:1", message_id="502",
        )
        self.assertTrue(agent.run_delivery_text_reconcile(current.run_id))
        self.assertEqual(self.manager.manifest(current.run_id)["status"], "done")
        evidence = agent._delivery_evidence(current.run_id)
        self.assertEqual(evidence["final_text"], "first second")
        self.assertEqual(evidence["message_ids"], ["501", "502"])

    def test_text_plan_refuses_route_that_disagrees_with_conversation_id(self):
        current = self._context("mismatched topic route")
        text = "never reroute"
        plan = {
            "schema": agent._TELEGRAM_TEXT_PLAN_SCHEMA,
            "conversation_id": "-10042__topic__202",
            "peer_id": "-10042",
            "topic_id": 101,
            "chunks": [{
                "index": 0,
                "text": text,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "delivery_key": f"run:{current.run_id}:chunk:0",
                "reply_to": 101,
            }],
        }
        with self.assertRaisesRegex(ValueError, "conversation does not match"):
            agent.run_delivery_started(
                current.run_id, chat_id=plan["conversation_id"],
                text_chars=len(text), media_count=0,
                text_plan=plan, media_queue_ids=[],
            )
        self.assertFalse(any(
            row.get("tool") == "telegram.deliver"
            for row in self.manager.events(current.run_id)
        ))

    def test_legacy_media_count_cannot_be_satisfied_by_unbound_receipt(self):
        current = self._context("legacy media count")
        agent.run_delivery_started(
            current.run_id, chat_id="1", text_chars=0, media_count=1,
        )
        agent.run_delivery_media_result(
            current.run_id, "unrelated-queue", ok=True, message_id="88",
        )

        self.assertFalse(agent.run_delivery_finalize_recovered(current.run_id))
        evidence = agent._delivery_evidence(current.run_id)
        self.assertTrue(evidence["legacy_media_ambiguous"])
        self.assertFalse(evidence["ready"])
        self.assertEqual(self.manager.manifest(current.run_id)["status"], "in_doubt")

    def test_composite_receipt_can_finish_after_crash_before_terminal_transition(self):
        current = self._context("text receipt")
        agent.run_delivery_started(current.run_id, chat_id="1", text_chars=5, media_count=0)
        self.manager.store_result(
            current.run_id,
            json.dumps({"text": "hello", "message_ids": ["77"], "media_count": 0}),
            call_id=f"delivery:{current.run_id}", name="telegram-delivery",
            media_type="application/json; charset=utf-8",
        )
        self.manager.transition(current.run_id, "paused", expected="running",
                                reason="process stopped before terminal commit")

        self.assertTrue(agent.run_delivery_finalize_recovered(current.run_id))
        self.assertEqual(self.manager.manifest(current.run_id)["status"], "done")

    def test_partial_text_receipt_never_becomes_success_after_crash(self):
        current = self._context("multi chunk delivery")
        expected = "first chunk and second chunk"
        agent.run_delivery_started(
            current.run_id, chat_id="1", text_chars=len(expected), media_count=0,
        )
        agent.run_delivery_text_accepted(
            current.run_id, text="first chunk", message_ids=["77"],
        )

        reports = self.manager.recover()
        self.assertTrue(reports)
        self.assertFalse(agent.run_delivery_finalize_recovered(current.run_id))
        manifest = self.manager.manifest(current.run_id)
        self.assertEqual(manifest["status"], "blocked")
        self.assertIn("partial Telegram text receipt",
                      self.manager.events(current.run_id)[-1].get("reason", ""))
        self.assertFalse((self.manager.path(current.run_id) / "RECAP.md").exists())

    def test_recovery_rebuilds_missing_terminal_recap(self):
        current = self._context("missing recap")
        self.manager.transition(current.run_id, "done", reason="terminal event survived")
        self.assertFalse((self.manager.path(current.run_id) / "RECAP.md").exists())

        reports = agent.recover_durable_runs()

        self.assertTrue((self.manager.path(current.run_id) / "RECAP.md").is_file())
        self.assertIn({"run_id": current.run_id, "recap": "recovered"}, reports)
        recap = (self.manager.path(current.run_id) / "RECAP.md").read_bytes()
        recap_events = [row for row in self.manager.events(current.run_id)
                        if row.get("kind") in {"recap_written", "recap_recovered"}]
        self.assertEqual(len(recap_events), 1)

        self.assertEqual(agent.recover_durable_runs(), [])
        self.assertEqual((self.manager.path(current.run_id) / "RECAP.md").read_bytes(), recap)
        self.assertEqual(len([
            row for row in self.manager.events(current.run_id)
            if row.get("kind") in {"recap_written", "recap_recovered"}
        ]), 1)

    def test_bound_run_is_authoritative_without_channel_globals(self):
        current = self._context("bound authority")
        old_scope, old_chat = agent._CURRENT_SCOPE, agent._CURRENT_CHAT
        agent._CURRENT_SCOPE, agent._CURRENT_CHAT = "unknown", "wrong"
        try:
            with run_context.bind_run(current):
                self.assertEqual(agent._active_scope(), "owner")
                self.assertEqual(agent._active_chat(), "1")
        finally:
            agent._CURRENT_SCOPE, agent._CURRENT_CHAT = old_scope, old_chat


if __name__ == "__main__":
    unittest.main()
