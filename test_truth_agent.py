"""Волна A, участок AG: «перестать ей врать» в agent/run_resume/run_manager.

Каждый тест здесь привязан к живому инциденту, а не к форме кода:
  * «Не отправилось» про доставленное (23.07, 5 из 6 доставлены, заметка в память);
  * `UndeliverableAuthoredOutput` на 33 из 33 возобновлённых окон + RECAP «No visible
    authored output» при живом тексте в том же ране;
  * `in_doubt` как вечное надгробие без её руки;
  * `conation_authorship` по аудитории вместо принципала;
  * возобновлённый ход мимо потолка руки;
  * единственный 900-байтовый слот машинной правды у судьи приватности;
  * подпись «praxis:self» в леджере из хода, который вызвал не она (R5);
  * `close=true` как надгробие живому прогону (R5);
  * кап, объявленный судье и не соблюдённый (R6);
  * «Не отправилось» из `mailer.send` там, где известно не это.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import smtplib
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import agent
import forge
import mailer
import media
import run_context
import run_manager
import telegram_outbox
import turns


class _RunHarness(unittest.TestCase):
    """Одноразовый run-слой + spool, как в test_agent_resume_runtime."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="praxis-truth-agent-")
        self.addCleanup(self._temp.cleanup)
        self.base = Path(self._temp.name)
        self.manager = run_manager.RunManager(self.base)
        self.spool = media.MediaSpool(self.base / "workspace" / "media")
        self._prev_manager = agent._RUN_MANAGER
        self._prev_spool = agent._MEDIA_SPOOL
        agent._RUN_MANAGER = self.manager
        agent._MEDIA_SPOOL = self.spool
        self.addCleanup(self._restore)

    def _restore(self):
        agent._RUN_MANAGER = self._prev_manager
        agent._MEDIA_SPOOL = self._prev_spool

    def _internal_channel(self) -> agent.ChannelContext:
        """Ровно то, с чем рождается task_window/wake: адресата нет по построению."""
        return agent.ChannelContext(
            chat_id=None, principal_id=agent.PRAXIS_SELF_PRINCIPAL, is_dm=True,
            owner=False, known=True, _scope_override="owner",
        )

    def _create(self, suffix: str, *, kind: str = "chat_turn",
                channel: agent.ChannelContext | None = None) -> run_context.RunContext:
        channel = channel or agent.ChannelContext(
            chat_id="100", room_id="100", principal_id="100",
            is_dm=True, owner=True, known=True, addressed=True,
            address_message_id=7, address_kind="direct",
            reply_targets=((7, "Yegor", "continue"),),
        )
        context = run_context.RunContext.create(
            run_id=f"run-truth-{suffix}",
            kind=kind, goal=f"truth {suffix}",
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
                ctx=channel, kind=kind, goal=context.goal,
                conversation="immutable conversation", history=None,
                extra="immutable runtime frame",
            ),
        )
        self.manager.transition(persisted.run_id, "running", expected="pending")
        return self.manager.context(persisted.run_id)

    def _model_output(self, context, *, text: str, stop_reason: str = "end_turn",
                      blocks: list[dict] | None = None, call_id: str = "model-one",
                      with_guard_input: bool = True) -> None:
        blocks = blocks if blocks is not None else [{"type": "text", "text": text}]
        tools = [{"name": block["name"]} for block in blocks
                 if block.get("type") == "tool_use"] or [{"name": "fs_read"}]
        model_input = {"system": "exact system",
                       "messages": [{"role": "user", "content": "hello"}],
                       "tools": tools}
        self.manager.store_result(
            context.run_id, json.dumps(model_input, ensure_ascii=False, indent=2),
            call_id=call_id, name="model-input", inline_chars=128,
            media_type="application/json; charset=utf-8",
            event_kind="model_input", idempotent=True,
        )
        self.manager.append_event(
            context.run_id, "model_started", call_id=call_id, role="voice",
            message_count=1, tool_count=len(tools),
        )
        self.manager.store_result(
            context.run_id,
            json.dumps({"text": text, "blocks": blocks, "stop_reason": stop_reason,
                        "framework": "test", "model": "test-model", "usage": {}},
                       ensure_ascii=False, indent=2),
            call_id=call_id, name="model-output", inline_chars=128,
            media_type="application/json; charset=utf-8",
            event_kind="model_output", idempotent=True,
        )
        self.manager.append_event(
            context.run_id, "model_completed", call_id=call_id, role="voice",
            stop_reason=stop_reason,
        )
        if stop_reason != "tool_use" and with_guard_input:
            agent._store_outbound_guard_input(
                context.run_id, draft=text, conversation="immutable conversation",
                orient="immutable runtime frame", tool_trace="", turn={},
                grounding_images=(), outbound_context="", outbound_images=(),
                repeat_discriminator="", outbound=[],
            )

    def _pause(self, context) -> None:
        self.manager.transition(
            context.run_id, "paused", expected="running",
            reason="process restarted; no uncertain side effect observed",
        )

    @staticmethod
    def _passthrough(draft, *_args, **_kwargs):
        return draft


# --------------------------------------------------------------------------- #
#  1. «Не отправилось» про доставленное
# --------------------------------------------------------------------------- #

class DeliveryTruthTests(unittest.TestCase):
    KEY = "telegram-outbox:run-x:tool:call-1"

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="praxis-truth-outbox-")
        self.addCleanup(self._temp.cleanup)
        self.outbox = telegram_outbox.TelegramOutbox(Path(self._temp.name) / "ob")
        self._prev_reader = agent._DIRECT_OUTBOX_READER
        agent._DIRECT_OUTBOX_READER = self.outbox
        self.addCleanup(self._restore)

    def _restore(self):
        agent._DIRECT_OUTBOX_READER = self._prev_reader

    def _stage(self, key: str = KEY) -> None:
        self.outbox.prepare_text(
            key, peer_id=777, text="её слово", run_id="run-x", call_id="call-1",
            purpose="tool:send_message",
        )

    def _execution(self, key: str = KEY):
        return mock.patch.object(agent, "current_tool_execution",
                                 return_value={"idempotency_key": key})

    def test_ledger_conflict_on_accepted_message_reports_delivery_not_failure(self):
        # 23.07: RunConflict «receipt … already has different content» при УЖЕ принятом
        # сообщении 1193 становился строкой «Не отправилось» и уезжал ей в журнал.
        self._stage()
        self.outbox.mark_accepted(self.KEY, message_id=1193)
        with self._execution():
            out = agent._direct_send_outcome(
                "Сообщение",
                run_manager.RunConflict("run-x: receipt call-1/telegram-outbox-intent "
                                        "already has different content"),
            )
        self.assertIn("ДОСТАВЛЕНО", out)
        self.assertIn("1193", out)
        self.assertNotIn("не отправилось", out.lower())

    def test_ledger_conflict_without_any_outbox_record_says_it_does_not_know(self):
        with self._execution():
            out = agent._direct_send_outcome(
                "Сообщение", run_manager.RunConflict("run-x: ledger blew up"))
        self.assertIn("НЕ ЗНАЮ", out)
        self.assertNotIn("ДОСТАВЛЕНО", out)

    def test_dead_letter_is_reported_as_not_sent_with_its_reason(self):
        self._stage()
        self.outbox.dead_letter(self.KEY, "peer is gone")
        with self._execution():
            out = agent._direct_send_outcome(
                "Сообщение", run_manager.RunConflict("ledger"))
        self.assertIn("НЕ ушло", out)
        self.assertIn("peer is gone", out)

    def test_pending_entry_is_named_as_unconfirmed_queue_not_as_delivery(self):
        self._stage()
        with self._execution():
            out = agent._direct_send_outcome(
                "Наррация", run_manager.RunConflict("ledger"))
        self.assertIn("очереди", out)
        self.assertNotIn("ДОСТАВЛЕНО", out)
        self.assertNotIn("НЕ ЗНАЮ", out)

    def test_transport_failure_without_receipt_still_says_it_did_not_go(self):
        # Настоящий отказ транспорта обязан остаться отказом: правда в обе стороны.
        with self._execution():
            out = agent._direct_send_outcome("Сообщение", ValueError("peer flood"))
        self.assertIn("не отправилось", out.lower())
        self.assertIn("peer flood", out)

    def test_send_message_journals_the_delivered_truth_not_the_ledger_error(self):
        self._stage()
        self.outbox.mark_accepted(self.KEY, message_id=1193)
        lines: list[str] = []

        def boom(_to, _text):
            raise run_manager.RunConflict("receipt already has different content")

        with mock.patch.dict(agent._TELETHON, {"send_message": boom}, clear=False), \
                mock.patch.object(agent, "tool_journal",
                                  side_effect=lambda text, **kw: lines.append(text)), \
                mock.patch.object(agent.stewardship, "outgoing_denial", return_value=""), \
                self._execution():
            out = agent.tool_send_message("777", "её слово")
        self.assertIn("ДОСТАВЛЕНО", out)
        self.assertTrue(lines, "исход отправки обязан попасть в журнал")
        self.assertIn("ДОСТАВЛЕНО", lines[0])
        self.assertNotIn("Не отправилось", lines[0])

    def test_direct_outbox_state_ignores_a_foreign_key(self):
        self.assertIsNone(agent._direct_outbox_state("turn-media-stage:run-x:tool:c"))
        self.assertIsNone(agent._direct_outbox_state(""))

    def test_clip_reason_marks_the_cut_and_keeps_whole_words(self):
        exact = "a" * 40
        self.assertEqual(agent._clip_reason(exact, 40), exact)
        self.assertNotIn("обрезано", agent._clip_reason(exact, 40))
        long_text = " ".join(["слово"] * 20)          # 5*20 + 19 = 119 символов
        clipped = agent._clip_reason(long_text, 40)
        self.assertIn("обрезано", clipped)
        self.assertIn("кап 40", clipped)
        head = clipped.split(" […")[0]
        self.assertLessEqual(len(head), 40)
        self.assertTrue(head.endswith("слово"), "рез обязан идти по границе слова")


# --------------------------------------------------------------------------- #
#  2. Ран без адресата ПО ПОСТРОЕНИЮ + RECAP
# --------------------------------------------------------------------------- #

class AddresseeFreeClassTests(unittest.TestCase):
    @staticmethod
    def _ctx(kind: str, origin, delivery):
        return run_context.RunContext.create(
            run_id="run-truth-pred", kind=kind, goal="g",
            principal_id="praxis:self", scope="owner",
            origin_chat_id=origin, origin_message_ids=(),
            delivery_chat_id=delivery, model_profile="voice",
        )

    def test_internal_window_and_wake_have_no_addressee_by_construction(self):
        for kind in ("task_window", "coding_window", "wake"):
            self.assertTrue(
                agent._run_has_no_addressee_by_construction(
                    self._ctx(kind, None, None)), kind)

    def test_chat_turn_with_a_lost_addressee_is_still_a_real_failure(self):
        # ⚠ Ловушка брифа: у chat_turn адрес БЫЛ; его отсутствие — настоящий провал
        # доставки, и бросок UndeliverableAuthoredOutput там правильный.
        self.assertFalse(agent._run_has_no_addressee_by_construction(
            self._ctx("chat_turn", None, None)))
        self.assertFalse(agent._run_has_no_addressee_by_construction(
            self._ctx("voice", None, None)))

    def test_a_window_that_does_have_a_chat_is_not_addressee_free(self):
        self.assertFalse(agent._run_has_no_addressee_by_construction(
            self._ctx("task_window", "100", "100")))


class ResumedWindowTerminalTests(_RunHarness):
    def test_resumed_task_window_is_done_not_failed_and_recap_keeps_her_text(self):
        # Скан прода: 33 из 33 возобновлённых task_window кончались `failed`
        # «authored output is permanently undeliverable». Успешного окна не было ни одного.
        context = self._create("window-done", kind="task_window",
                               channel=self._internal_channel())
        self._model_output(context, text="то, что я поняла за это окно")
        self._pause(context)
        with mock.patch.object(agent.social, "owner_id", return_value="100"), \
                mock.patch.object(agent, "guard_outbound_reply",
                                  side_effect=self._passthrough), \
                mock.patch.object(agent, "_model_call",
                                  side_effect=AssertionError("voice model called")):
            report = agent.resume_durable_run(context.run_id)
        self.assertEqual(report["run_status"], "done")
        self.assertEqual(self.manager.manifest(context.run_id)["status"], "done")
        recap = (self.manager.path(context.run_id) / "RECAP.md").read_text(encoding="utf-8")
        self.assertIn("то, что я поняла за это окно", recap)
        self.assertNotIn("No visible authored output", recap)
        self.assertIn("Outcome: `done`", recap)

    def test_resumed_chat_turn_without_delivery_chat_still_fails(self):
        # Обратная сторона той же монеты: сузить починку до «chat_id пуст» было нельзя.
        channel = agent.ChannelContext(
            chat_id=None, room_id=None, principal_id=agent.PRAXIS_SELF_PRINCIPAL,
            is_dm=True, owner=False, known=True, addressed=True,
            _scope_override="owner",
        )
        context = self._create("turn-failed", kind="chat_turn", channel=channel)
        self._model_output(context, text="ответ без адресата")
        self._pause(context)
        with mock.patch.object(agent.social, "owner_id", return_value="100"), \
                mock.patch.object(agent, "guard_outbound_reply",
                                  side_effect=self._passthrough), \
                mock.patch.object(agent, "_model_call",
                                  side_effect=AssertionError("voice model called")):
            report = agent.resume_durable_run(context.run_id)
        self.assertEqual(report["run_status"], "failed")
        self.assertEqual(self.manager.manifest(context.run_id)["status"], "failed")


class AddresseeFreeLandingTests(_RunHarness):
    """Куда садится ран без адресата, если терминальная расписка НЕ записалась.

    ⚠ Первая версия ставила `paused`, а `paused` резюмер подбирает на каждом проходе:
    тот же ход, тот же бросок, та же неудача — круг без счётчика. Дом это уже проходил
    (таск-окно #3bca7155 крутилось 50+ раз).
    """

    @staticmethod
    def _stub_outcome():
        import types
        return types.SimpleNamespace(
            phase="authored_output", error_type="UndeliverableAuthoredOutput",
            error_message="authored output has no immutable delivery chat",
        )

    def _running_window(self, suffix: str):
        return self._create(suffix, kind="task_window",
                            channel=self._internal_channel())

    def test_an_unwritable_recap_still_lands_terminal_not_back_in_the_resumer(self):
        context = self._running_window("recap-broken")
        self._model_output(context, text="итог окна", with_guard_input=False)
        with mock.patch.object(agent, "_run_recap_markdown",
                               side_effect=RuntimeError("recap projector blew up")):
            report = agent._land_addressee_free_run(
                self.manager, context.run_id, self._stub_outcome())
        self.assertEqual(report["run_status"], "done")
        self.assertTrue(report.get("recap_deferred"))
        self.assertEqual(self.manager.manifest(context.run_id)["status"], "done")

    def test_an_unreconciled_call_lands_in_doubt_which_the_resumer_never_executes(self):
        context = self._running_window("calls-open")
        self.manager.start_tool(context.run_id, "call-open", "coding_session",
                                {"action": "finish"}, side_effect=True)
        report = agent._land_addressee_free_run(
            self.manager, context.run_id, self._stub_outcome())
        status = self.manager.manifest(context.run_id)["status"]
        self.assertEqual(report["run_status"], "in_doubt")
        self.assertEqual(status, "in_doubt")
        self.assertNotEqual(status, "paused")           # ← именно это крутило круг
        self.assertIn("terminalization_blocked", report)
        # И вот доказательство, что круга нет: план такого рана НЕ исполняемый.
        import run_executor
        import run_resume
        plan = run_resume.plan_resume(self.manager, context.run_id,
                                      outbound_roots=[self.spool.root])
        self.assertIn(plan.kind, run_executor.NON_EXECUTABLE_KINDS)
        # …а её собственная рука его сводит — надгробием он не становится.
        self.assertIn("call-open", agent.tool_reconcile_run(context.run_id))

    def test_the_happy_path_is_still_a_plain_done_with_her_text(self):
        context = self._running_window("plain-done")
        self._model_output(context, text="то, что я поняла", with_guard_input=False)
        report = agent._land_addressee_free_run(
            self.manager, context.run_id, self._stub_outcome())
        self.assertEqual(report, {"run_status": "done"})
        recap = (self.manager.path(context.run_id) / "RECAP.md").read_text("utf-8")
        self.assertIn("то, что я поняла", recap)


class RecapTextRecoveryTests(_RunHarness):
    def _guard_receipt(self, context, text: str, draft: str = "draft",
                       decision: str = "send_authored", verdict: str = "",
                       reason: str = "") -> None:
        self.manager.store_result(
            context.run_id,
            json.dumps({
                "schema": agent._OUTBOUND_GUARD_RECEIPT_SCHEMA,
                "draft_sha256": hashlib.sha256(draft.encode("utf-8")).hexdigest(),
                "text": text, "media_queue_ids": [],
                "advisor": "privacy", "advisor_verdict": verdict,
                "advisor_reason": reason, "praxis_decision": decision,
            }, ensure_ascii=False),
            call_id=f"{agent._OUTBOUND_GUARD_RECEIPT_CALL_PREFIX}:{context.run_id}",
            name="outbound-guard", media_type="application/json; charset=utf-8",
            event_kind="outbound_guard_result", idempotent=True,
        )

    def test_recap_never_projects_the_guard_receipt_payload(self):
        # ⚠ Первая версия починки брала текст из расписки гарда и красила три зелёных
        # теста в test_run_integration: рекап проецирует РЕШЕНИЕ судьи, но не полезную
        # нагрузку расписки (рядом с текстом там лежит advisor_reason с цитатами и id
        # медиа-очереди). Источник текста — её собственный model_output, и только он.
        context = self._create("recap-receipt-only", kind="task_window",
                               channel=self._internal_channel())
        self._guard_receipt(context, "секрет из расписки")
        recap = agent._run_recap_markdown(context.run_id, outcome="done", final_text="")
        self.assertNotIn("секрет из расписки", recap)
        self.assertIn("No visible authored output", recap)

    def test_a_held_turn_says_where_her_text_is_instead_of_printing_it(self):
        # Придержанный ход: печатать текст нельзя (контракт рекапа), но и «ничего не
        # написано» — ложь. Отчёт обязан сказать, что придержано и где лежит.
        context = self._create("recap-held", kind="chat_turn")
        self._model_output(context, text="черновик, который придержали",
                           with_guard_input=False)
        self._guard_receipt(context, "", verdict="молчи", reason="цитата: черновик",
                            decision="hold_for_data_authority")
        recap = agent._run_recap_markdown(context.run_id, outcome="done", final_text="")
        self.assertNotIn("черновик, который придержали", recap)
        self.assertNotIn("No visible authored output", recap)
        self.assertIn("advisor held this turn", recap)
        self.assertIn("model_output", recap)

    def test_recap_falls_back_to_model_output_when_no_guard_receipt_exists(self):
        context = self._create("recap-model", kind="task_window",
                               channel=self._internal_channel())
        self._model_output(context, text="черновик из модели", with_guard_input=False)
        recap = agent._run_recap_markdown(context.run_id, outcome="done", final_text="")
        self.assertIn("черновик из модели", recap)
        self.assertIn("model output", recap)

    def test_recap_never_invents_text_when_the_run_has_none(self):
        context = self._create("recap-empty", kind="task_window",
                               channel=self._internal_channel())
        recap = agent._run_recap_markdown(context.run_id, outcome="done", final_text="")
        self.assertIn("No visible authored output", recap)
        self.assertNotIn("recovered from", recap)

    def test_explicit_final_text_wins_and_is_not_relabelled_as_recovered(self):
        context = self._create("recap-explicit", kind="task_window",
                               channel=self._internal_channel())
        self._model_output(context, text="черновик модели", with_guard_input=False)
        recap = agent._run_recap_markdown(
            context.run_id, outcome="done", final_text="переданный текст")
        self.assertIn("переданный текст", recap)
        self.assertNotIn("recovered from", recap)


# --------------------------------------------------------------------------- #
#  3. in_doubt перестал быть надгробием
# --------------------------------------------------------------------------- #

class ReconcileRunTests(_RunHarness):
    def _in_doubt(self, suffix: str, *, tool: str, args: dict,
                  call_id: str = "call-1", idempotency_key: str = ""):
        context = self._create(suffix, kind="task_window",
                               channel=self._internal_channel())
        self.manager.start_tool(
            context.run_id, call_id, tool, args,
            side_effect=True, idempotency_key=idempotency_key,
        )
        self.manager.transition(
            context.run_id, "in_doubt", expected="running",
            reason="uncertain side effect",
        )
        return context

    def test_listing_shows_the_stuck_run_and_its_outstanding_call(self):
        context = self._in_doubt("list", tool="narrate",
                                 args={"text": "смок", "task_id": ""})
        out = agent.tool_reconcile_run()
        self.assertIn(context.run_id, out)
        self.assertIn("narrate", out)
        self.assertIn("call-1", out)

    def test_she_closes_one_call_with_evidence_and_then_the_run(self):
        context = self._in_doubt("close", tool="narrate", args={"text": "смок"})
        resolved = agent.tool_reconcile_run(
            context.run_id, "call-1", "not_applied",
            evidence="в чате этой строки нет; запись очереди отсутствует",
        )
        self.assertIn("сведён", resolved)
        self.assertEqual(self.manager.manifest(context.run_id)["status"], "paused")
        closed = agent.tool_reconcile_run(context.run_id, close=True,
                                          reason="смок-ран, дело закрыто")
        self.assertIn("cancelled", closed)
        self.assertEqual(self.manager.manifest(context.run_id)["status"], "cancelled")
        self.assertTrue((self.manager.path(context.run_id) / "RECAP.md").is_file())

    def test_reconcile_without_evidence_changes_nothing_and_says_why(self):
        context = self._in_doubt("no-evidence", tool="narrate", args={"text": "смок"})
        out = agent.tool_reconcile_run(context.run_id, "call-1", "completed")
        self.assertIn("улика", out.lower())
        self.assertEqual(self.manager.manifest(context.run_id)["status"], "in_doubt")

    def test_close_refuses_while_a_call_is_still_open_and_shows_it(self):
        context = self._in_doubt("still-open", tool="narrate", args={"text": "смок"})
        out = agent.tool_reconcile_run(context.run_id, close=True)
        self.assertIn("call-1", out)
        self.assertEqual(self.manager.manifest(context.run_id)["status"], "in_doubt")

    def test_unknown_outcome_is_named_as_the_ledger_vocabulary(self):
        context = self._in_doubt("bad-outcome", tool="narrate", args={"text": "смок"})
        out = agent.tool_reconcile_run(context.run_id, "call-1", "молчи",
                                       evidence="что-то")
        self.assertIn("not_applied", out)
        self.assertEqual(self.manager.manifest(context.run_id)["status"], "in_doubt")

    def test_a_clipped_evidence_is_named_in_the_answer_and_marked_in_the_ledger(self):
        # ⚠ Молчаливый кап в правке, которая как раз чинит молчаливые капы: 6000 симв.
        # улики уезжали в леджер как 4000 без единого маркера, а ответ тула говорил
        # «вызов сведён» и ни слова про обрезку (закон 2а).
        context = self._in_doubt("clip", tool="narrate", args={"text": "смок"})
        long_note = "проверила: " + "ф" * (agent._RECONCILE_EVIDENCE_CHARS + 500)
        long_reason = "потому что " + "ы" * (agent._RECONCILE_REASON_CHARS + 100)
        out = agent.tool_reconcile_run(context.run_id, "call-1", "completed",
                                       evidence=long_note, reason=long_reason)
        self.assertIn(str(agent._RECONCILE_EVIDENCE_CHARS), out)
        self.assertIn(str(agent._RECONCILE_REASON_CHARS), out)
        row = [r for r in self.manager.events(context.run_id)
               if r.get("kind") == "tool_reconciled"][-1]
        stored = row["evidence"]["praxis_evidence"]
        self.assertIn("обрезано", stored, "маркер обязан быть в самой улике")
        self.assertIn("обрезано", row["reason"])
        self.assertLessEqual(len(row["reason"]), agent._RECONCILE_REASON_CHARS + 120)

    def test_evidence_that_fits_is_stored_whole_and_nothing_is_claimed_about_cutting(self):
        # Граница ровно на капе: резать нечего — и говорить о резе нельзя.
        context = self._in_doubt("clip-edge", tool="narrate", args={"text": "смок"})
        exact = "у" * agent._RECONCILE_EVIDENCE_CHARS
        out = agent.tool_reconcile_run(context.run_id, "call-1", "completed",
                                       evidence=exact)
        self.assertNotIn("Улика длиннее", out)
        row = [r for r in self.manager.events(context.run_id)
               if r.get("kind") == "tool_reconciled"][-1]
        self.assertEqual(row["evidence"]["praxis_evidence"], exact)

    def test_the_hand_names_its_caps_where_she_reads_about_the_hand(self):
        spec = next(tool for tool in agent.BASE_TOOLS
                    if tool.get("name") == "reconcile_run")
        self.assertIn(str(agent._RECONCILE_EVIDENCE_CHARS), spec["description"])
        self.assertIn(str(agent._RECONCILE_REASON_CHARS), spec["description"])

    def test_reconcile_run_is_offered_to_her_in_every_turn(self):
        self.assertIn("reconcile_run", agent.TOOL_IMPL)
        names = {str(tool.get("name") or "") for tool in agent.BASE_TOOLS}
        self.assertIn("reconcile_run", names)

    def test_the_hand_names_the_quiet_cap_and_the_actor_it_will_write(self):
        spec = next(tool for tool in agent.BASE_TOOLS
                    if tool.get("name") == "reconcile_run")
        self.assertIn(str(int(agent._RECONCILE_QUIET_SEC)), spec["description"])
        self.assertIn("praxis:self@", spec["description"])

    # --- атрибуция: кто НА САМОМ ДЕЛЕ вызвал ход -------------------------- #

    def _reconciled_by(self, context) -> str:
        """Актор, который реально уехал в леджер."""
        row = [r for r in self.manager.events(context.run_id)
               if r.get("kind") == "tool_reconciled"][-1]
        return str(row["resolved_by"])

    def test_her_own_turn_is_recorded_as_her_own(self):
        context = self._in_doubt("actor-self", tool="narrate", args={"text": "смок"})
        token = agent._TURN_CHANNEL.set(self._internal_channel())
        try:
            agent.tool_reconcile_run(context.run_id, "call-1", "not_applied",
                                     evidence="в чате этой строки нет")
        finally:
            agent._TURN_CHANNEL.reset(token)
        self.assertEqual(self._reconciled_by(context), agent.PRAXIS_SELF_PRINCIPAL)

    def test_a_turn_triggered_by_a_stranger_does_not_sign_itself_as_her_alone(self):
        # ⚠ `reconcile_run` лежит в BASE_TOOLS, то есть предлагается и в чужой ЛС, и в
        # группе. Запись «praxis:self» оттуда читается через неделю как «пришла к этому
        # сама» — и стирает того, чья реплика ход вызвала. Рука её; ложь в подписи — нет.
        context = self._in_doubt("actor-foreign", tool="narrate", args={"text": "смок"})
        foreign = agent.ChannelContext(chat_id="555000444", principal_id="555000444",
                                       is_dm=True, owner=False, known=False)
        token = agent._TURN_CHANNEL.set(foreign)
        try:
            out = agent.tool_reconcile_run(context.run_id, "call-1", "not_applied",
                                           evidence="в чате этой строки нет")
        finally:
            agent._TURN_CHANNEL.reset(token)
        actor = self._reconciled_by(context)
        self.assertNotEqual(actor, agent.PRAXIS_SELF_PRINCIPAL)
        self.assertTrue(actor.startswith(agent.PRAXIS_SELF_PRINCIPAL + "@"), actor)
        self.assertIn("555000444", actor, "принципал хода обязан быть назван")
        self.assertIn(actor, out, "она обязана видеть, что записано её именем")
        self.assertEqual(self.manager.manifest(context.run_id)["status"], "paused",
                         "правка атрибуции не имеет права ничего ей запрещать")

    def test_the_owner_asking_in_his_dm_is_recorded_as_the_owner(self):
        context = self._in_doubt("actor-owner", tool="narrate", args={"text": "смок"})
        owner_ctx = agent.ChannelContext(chat_id="100", principal_id="100", is_dm=True,
                                         owner=True, known=True)
        token = agent._TURN_CHANNEL.set(owner_ctx)
        try:
            with mock.patch.object(agent.social, "owner_id", return_value="100"):
                agent.tool_reconcile_run(context.run_id, "call-1", "not_applied",
                                         evidence="проверила чат")
        finally:
            agent._TURN_CHANNEL.reset(token)
        self.assertEqual(self._reconciled_by(context), "praxis:self@owner:100")

    def test_a_call_with_no_principal_at_all_says_so_instead_of_claiming_authorship(self):
        context = self._in_doubt("actor-none", tool="narrate", args={"text": "смок"})
        agent.tool_reconcile_run(context.run_id, "call-1", "not_applied",
                                 evidence="в чате этой строки нет")
        self.assertEqual(self._reconciled_by(context), "praxis:self@principal-unknown")


# --------------------------------------------------------------------------- #
#  3b. close=true не имеет права ставить надгробие ЖИВОМУ прогону
# --------------------------------------------------------------------------- #

class CloseRequiresProofTests(_RunHarness):
    def test_a_live_running_run_is_asked_to_stop_instead_of_being_tombstoned(self):
        # ⚠ Проверялись только «терминальный ли статус» и «висят ли вызовы ПРЯМО СЕЙЧАС».
        # Между двумя тулами открытых вызовов нет — и работающий прогон получал запись
        # «cancelled … after explicit reconciliation», хотя сведения не было.
        context = self._create("alive-run", kind="chat_turn")
        self.assertEqual(self.manager.manifest(context.run_id)["status"], "running")
        out = agent.tool_reconcile_run(context.run_id, close=True)
        manifest = self.manager.manifest(context.run_id)
        self.assertNotEqual(manifest["status"], "cancelled")
        self.assertEqual(manifest["status"], "paused")
        self.assertIn("признаки жизни", out)
        self.assertIn("close requested by Praxis",
                      str((manifest.get("control") or {}).get("reason") or ""))
        # Намерение не потеряно и не отложено навсегда: второй вызов закрывает начисто.
        again = agent.tool_reconcile_run(context.run_id, close=True)
        self.assertIn("cancelled", again)
        self.assertEqual(self.manager.manifest(context.run_id)["status"], "cancelled")

    def test_the_quiet_cap_is_a_boundary_and_is_named_where_she_reads_it(self):
        now = _dt.datetime.now(_dt.timezone.utc)

        def aged(seconds: float) -> dict:
            return {"status": "running",
                    "updated_at": (now - _dt.timedelta(seconds=seconds)).isoformat()}

        kind, _ = agent._close_terminality_proof(
            "run-x", aged(agent._RECONCILE_QUIET_SEC - 5))
        self.assertEqual(kind, "alive", "на секунду моложе капа — ещё не доказательство")
        kind, why = agent._close_terminality_proof(
            "run-x", aged(agent._RECONCILE_QUIET_SEC + 5))
        self.assertEqual(kind, "proven")
        self.assertIn(str(int(agent._RECONCILE_QUIET_SEC)), why)

    def test_the_run_she_is_speaking_inside_is_never_called_dead(self):
        context = self._create("inside", kind="chat_turn")
        stale = dict(self.manager.manifest(context.run_id))
        stale["updated_at"] = (_dt.datetime.now(_dt.timezone.utc)
                               - _dt.timedelta(days=1)).isoformat()
        # Даже суточная тишина в леджере не делает мёртвым прогон, внутри которого она
        # прямо сейчас говорит: событий он не пишет, а живёт.
        with run_context.bind_run(self.manager.context(context.run_id)):
            kind, why = agent._close_terminality_proof(context.run_id, stale)
        self.assertEqual(kind, "alive")
        self.assertIn("внутри которого", why)

    def test_a_manifest_without_a_readable_clock_is_unknown_not_dead(self):
        kind, why = agent._close_terminality_proof("run-x", {"status": "running"})
        self.assertEqual(kind, "alive")
        self.assertIn("неизвестен", why)

    def test_close_names_the_proof_it_acted_on(self):
        context = self._create("proof", kind="chat_turn")
        self.manager.transition(context.run_id, "paused", expected="running",
                                reason="исполнитель отпустил")
        out = agent.tool_reconcile_run(context.run_id, close=True)
        self.assertIn("cancelled", out)
        self.assertIn("основание", out)
        self.assertIn("paused", out)


class ReceiptReconcilerTests(_RunHarness):
    def setUp(self):
        super().setUp()
        self.tasks_dir = self.base / "forge-tasks"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.object(forge, "TASKS_DIR", self.tasks_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _forge_task(self, task_id: str, *, status: str, finished: str) -> None:
        directory = self.tasks_dir / task_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "task.json").write_text(
            json.dumps({"id": task_id, "status": status, "finished": finished},
                       ensure_ascii=False),
            encoding="utf-8",
        )

    def _in_doubt_finish(self, suffix: str, task_id: str):
        context = self._create(suffix, kind="task_window",
                               channel=self._internal_channel())
        self.manager.start_tool(
            context.run_id, "call-finish", "coding_session",
            {"action": "finish", "task_id": task_id}, side_effect=True,
        )
        self.manager.transition(
            context.run_id, "in_doubt", expected="running",
            reason="tool did not return",
        )
        return context

    def test_finished_forge_task_closes_its_own_outstanding_call(self):
        self._forge_task("hcode-done", status="done",
                         finished="2026-07-26T21:45:11+00:00")
        context = self._in_doubt_finish("finished", "hcode-done")
        reports = agent.reconcile_in_doubt_from_receipts()
        self.assertTrue(any(row["run_id"] == context.run_id for row in reports))
        self.assertEqual(self.manager.manifest(context.run_id)["status"], "paused")

    def test_unfinished_forge_task_leaves_the_run_visible_and_untouched(self):
        # Граница: расписки нет -> вывода нет. Отсутствие улики ничего не доказывает.
        self._forge_task("hcode-open", status="active", finished="")
        context = self._in_doubt_finish("unfinished", "hcode-open")
        self.assertEqual(agent.reconcile_in_doubt_from_receipts(), [])
        self.assertEqual(self.manager.manifest(context.run_id)["status"], "in_doubt")

    def test_missing_forge_task_leaves_the_run_visible(self):
        context = self._in_doubt_finish("missing", "hcode-nowhere")
        self.assertEqual(agent.reconcile_in_doubt_from_receipts(), [])
        self.assertEqual(self.manager.manifest(context.run_id)["status"], "in_doubt")

    def test_accepted_direct_send_closes_its_own_outstanding_call(self):
        temp = tempfile.TemporaryDirectory(prefix="praxis-truth-ob2-")
        self.addCleanup(temp.cleanup)
        outbox = telegram_outbox.TelegramOutbox(Path(temp.name) / "ob")
        prev = agent._DIRECT_OUTBOX_READER
        agent._DIRECT_OUTBOX_READER = outbox
        self.addCleanup(lambda: setattr(agent, "_DIRECT_OUTBOX_READER", prev))
        context = self._create("outbox-receipt", kind="task_window",
                               channel=self._internal_channel())
        key = f"telegram-outbox:{context.run_id}:tool:call-send"
        outbox.prepare_text(key, peer_id=777, text="ушло", run_id=context.run_id,
                            call_id="call-send", purpose="tool:send_message")
        outbox.mark_accepted(key, message_id=4242)
        self.manager.start_tool(
            context.run_id, "call-send", "send_message", {"to": "777", "text": "ушло"},
            side_effect=True, idempotency_key=key,
        )
        self.manager.transition(context.run_id, "in_doubt", expected="running",
                                reason="uncertain side effect")
        reports = agent.reconcile_in_doubt_from_receipts()
        self.assertEqual([row["reconciled"] for row in reports], ["completed"])
        self.assertEqual(reports[0]["evidence"]["message_id"], 4242)
        self.assertEqual(self.manager.manifest(context.run_id)["status"], "paused")


# --------------------------------------------------------------------------- #
#  4. conation_authorship — по принципалу, не по аудитории
# --------------------------------------------------------------------------- #

class ConationAuthorshipTests(unittest.TestCase):
    def setUp(self):
        self._denials: list[tuple] = []
        patcher = mock.patch.object(
            agent.rails, "deny",
            side_effect=lambda *a, **kw: self._denials.append(a))
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _foreign_dm() -> agent.ChannelContext:
        """Чужая ЛС: scope='unknown', ctx.owner=False — 50 её ранов такого вида."""
        return agent.ChannelContext(
            chat_id="555000444", principal_id="555000444",
            is_dm=True, owner=False, known=False,
        )

    def test_her_own_desire_is_editable_from_a_foreign_dm(self):
        ctx = self._foreign_dm()
        self.assertEqual(ctx.scope, "unknown")
        token = agent._TURN_CHANNEL.set(ctx)
        try:
            with mock.patch.object(agent.desires, "DesireLedger") as ledger:
                ledger.return_value.notice.return_value = {"desire_id": "d-1"}
                out = agent.tool_manage_desire(
                    "notice", statement="хочу довести это до конца",
                    why_it_matters="важно")
        finally:
            agent._TURN_CHANNEL.reset(token)
        self.assertIn("d-1", out)
        self.assertEqual(self._denials, [], "отказа быть не должно: принципал — она")

    def test_a_context_free_call_with_no_principal_returns_her_text_instead_of_eating_it(self):
        with mock.patch.object(agent, "_CURRENT_SCOPE", "unknown"):
            out = agent.tool_manage_desire(
                "change", desire_id="d-9", note="важная заметка",
                next_move="следующий шаг")
        self.assertTrue(self._denials, "отказ обязан быть записан в denials")
        self.assertIn("важная заметка", out)
        self.assertIn("следующий шаг", out)

    def test_owner_scope_without_a_turn_context_still_passes(self):
        with mock.patch.object(agent, "_CURRENT_SCOPE", "owner"), \
                mock.patch.object(agent.desires, "DesireLedger") as ledger:
            ledger.return_value.notice.return_value = {"desire_id": "d-2"}
            out = agent.tool_manage_desire("notice", statement="s", why_it_matters="w")
        self.assertIn("d-2", out)
        self.assertEqual(self._denials, [])


# --------------------------------------------------------------------------- #
#  5. Возобновлённый ход идёт через потолок руки
# --------------------------------------------------------------------------- #

class ResumeToolCeilingTests(_RunHarness):
    def test_resumed_tool_call_goes_through_the_hand_ceiling(self):
        context = self._create("ceiling", kind="chat_turn")
        self._model_output(
            context, text="", stop_reason="tool_use",
            blocks=[{"type": "tool_use", "id": "pending", "name": "fs_read",
                     "input": {"path": "pending.txt"}}],
        )
        self._pause(context)
        seen: list[str] = []

        def recording(name, impl, call_input):
            seen.append(name)
            return impl(**call_input)

        with mock.patch.object(agent.social, "owner_id", return_value="100"), \
                mock.patch.object(agent, "guard_outbound_reply",
                                  side_effect=self._passthrough), \
                mock.patch.object(agent, "_terminal_tool_loop",
                                  side_effect=lambda **kw: "continued"), \
                mock.patch.object(agent, "_call_tool_with_ceiling",
                                  side_effect=recording), \
                mock.patch.dict(agent.TOOL_IMPL,
                                {"fs_read": lambda path: f"read {path}"}):
            agent.resume_durable_run(context.run_id)
        self.assertEqual(seen, ["fs_read"],
                         "возобновлённый вызов обязан идти через потолок")

    def test_a_hand_that_never_returns_releases_the_resumed_turn(self):
        import threading
        release = threading.Event()
        self.addCleanup(release.set)

        with mock.patch.object(agent, "TOOL_CEILING_SEC", 0.3), \
                mock.patch.object(agent, "tool_journal", return_value=""):
            out = agent._call_tool_with_ceiling(
                "coding_session", lambda **kw: release.wait(30), {},
            )
        self.assertIn("НЕИЗВЕСТНЫМ", out)
        # ⚠ Исход ТИПИЗИРОВАН, иначе вызывающий не отличит «рука вернула результат» от
        # «рука не вернулась» — и durable-леджер закроет вызов как обычный tool_result.
        self.assertIsInstance(out, agent.ToolCeilingExpired)
        self.assertIsInstance(out, str, "прежние вызывающие видят ровно ту же строку")

    def test_an_expired_side_effect_is_written_as_unknown_not_as_a_result(self):
        # ⚠ Ровно зависший `coding_session finish` 26.07: побочный эффект без ключа
        # идемпотентности. Если закрыть его обычным tool_result, леджер скажет «вызов
        # вернул результат», а поток позже домутирует forge-задачу — при том что ей в
        # текст сказано «считай состояние НЕИЗВЕСТНЫМ».
        import threading
        release = threading.Event()
        self.addCleanup(release.set)
        context = self._create("ceiling-effect", kind="chat_turn")
        self._model_output(
            context, text="", stop_reason="tool_use",
            blocks=[{"type": "tool_use", "id": "pending", "name": "coding_session",
                     "input": {"action": "finish", "task_id": "hcode-1"}}],
        )
        self._pause(context)
        with mock.patch.object(agent.social, "owner_id", return_value="100"), \
                mock.patch.object(agent, "TOOL_CEILING_SEC", 0.3), \
                mock.patch.object(agent, "tool_journal", return_value=""), \
                mock.patch.object(agent, "guard_outbound_reply",
                                  side_effect=self._passthrough), \
                mock.patch.dict(agent.TOOL_IMPL,
                                {"coding_session": lambda **kw: release.wait(30)}):
            agent.resume_durable_run(context.run_id)
        self.assertEqual(self.manager.manifest(context.run_id)["status"], "in_doubt")
        kinds = [row.get("kind") for row in self.manager.events(context.run_id)
                 if row.get("call_id") == "pending"]
        self.assertIn("tool_uncertain_error", kinds)
        self.assertNotIn("tool_result", kinds)
        # Незакрытый вызов остаётся видимым — его сводит её `reconcile_run`.
        self.assertIn("pending", self.manager.outstanding_tools(context.run_id))

    def test_an_expired_read_only_hand_is_still_an_ordinary_result(self):
        # Граница: у чтения нет неизвестного эффекта, и in_doubt здесь был бы забором.
        import threading
        release = threading.Event()
        self.addCleanup(release.set)
        context = self._create("ceiling-read", kind="chat_turn")
        self._model_output(
            context, text="", stop_reason="tool_use",
            blocks=[{"type": "tool_use", "id": "pending", "name": "fs_read",
                     "input": {"path": "pending.txt"}}],
        )
        self._pause(context)
        with mock.patch.object(agent.social, "owner_id", return_value="100"), \
                mock.patch.object(agent, "TOOL_CEILING_SEC", 0.3), \
                mock.patch.object(agent, "tool_journal", return_value=""), \
                mock.patch.object(agent, "guard_outbound_reply",
                                  side_effect=self._passthrough), \
                mock.patch.object(agent, "_terminal_tool_loop",
                                  side_effect=lambda **kw: "continued"), \
                mock.patch.dict(agent.TOOL_IMPL,
                                {"fs_read": lambda **kw: release.wait(30)}):
            agent.resume_durable_run(context.run_id)
        kinds = [row.get("kind") for row in self.manager.events(context.run_id)
                 if row.get("call_id") == "pending"]
        self.assertIn("tool_result", kinds)
        self.assertNotIn("tool_uncertain_error", kinds)


# --------------------------------------------------------------------------- #
#  6. Судья: расщеплённый слот, названные капы, обрыв вердикта
# --------------------------------------------------------------------------- #

class GuardInputSplitTests(unittest.TestCase):
    STATE = "\n".join(json.dumps({"fact": f"f{i}", "value": "x" * 20})
                      for i in range(12))

    def _capture(self, verdict: str = "PRIVACY_OK", stop_reason: str = "end_turn"):
        captured: dict = {}

        class FakeResp:
            text = verdict

        FakeResp.stop_reason = stop_reason

        def fake_chat(_role, **kw):
            captured["system"] = kw.get("system", "")
            captured["max_tokens"] = kw.get("max_tokens")
            messages = kw.get("messages") or []
            captured["content"] = messages[0]["content"] if messages else ""
            return FakeResp()

        return captured, fake_chat

    def test_state_and_topic_orientation_are_two_blocks_with_named_caps(self):
        captured, fake_chat = self._capture()
        with mock.patch.object(agent.llm, "configured", return_value=True), \
                mock.patch.object(agent.llm, "chat", side_effect=fake_chat):
            verdict, _ = agent.evaluate_reply(
                "черновик", "TOPIC MAP: ветка A -> ветка B",
                state=self.STATE, audience_accepts_private=False)
        self.assertEqual(verdict, "ok")
        content = captured["content"]
        self.assertIn("STATE (machine truth", content)
        self.assertIn("ROOM/TOPIC ORIENTATION", content)
        self.assertIn(f"cap {agent._GUARD_STATE_CHARS} chars", content)
        self.assertIn(f"cap {agent._GUARD_TOPIC_CHARS} chars", content)
        self.assertIn("TOPIC MAP: ветка A -> ветка B", content)
        self.assertIn('"fact": "f0"', content)

    def test_topic_orientation_can_no_longer_evict_state(self):
        # ⚠ Корень: один слот на 900 символов; в 123 из 200 судимых ходов в нём лежала
        # карта веток, и STATE не доезжал вовсе.
        captured, fake_chat = self._capture()
        huge_topic = "\n".join(f"branch {i}: " + "y" * 200 for i in range(80))
        with mock.patch.object(agent.llm, "configured", return_value=True), \
                mock.patch.object(agent.llm, "chat", side_effect=fake_chat):
            agent.evaluate_reply("черновик", huge_topic, state=self.STATE,
                                 audience_accepts_private=False)
        content = captured["content"]
        self.assertIn('"fact": "f11"', content, "STATE обязан доехать целиком")
        self.assertIn("line(s) did not fit", content, "усечение обязано быть НАЗВАНО")

    def test_clip_jsonl_block_cuts_on_line_boundaries_only(self):
        lines = ['{"a":1}', '{"b":2}', '{"c":3}']       # по 7 символов
        text = "\n".join(lines)
        whole, dropped, over = agent._clip_jsonl_block(text, len(text))
        self.assertEqual((whole, dropped, over), (text, 0, 0))
        # На один символ меньше — последняя строка не влезает ЦЕЛИКОМ и выпадает.
        cut, dropped, over = agent._clip_jsonl_block(text, len(text) - 1)
        self.assertEqual(cut, "\n".join(lines[:2]))
        self.assertEqual((dropped, over), (1, 0))
        for piece in cut.splitlines():
            json.loads(piece)                            # ни одной битой строки

    def test_a_first_line_longer_than_the_cap_is_kept_whole_AND_the_overrun_is_reported(self):
        # ⚠ Здесь стояло `self.assertEqual(dropped, 0)` и на этом тест заканчивался: он
        # ЗАКРЕПЛЯЛ молчание. Компромисс (не рвать JSON) правильный, но кап был объявлен
        # судье и не соблюдён, а заголовок печатал «cap 4000 chars; 0 line(s) did not fit»
        # над блоком вдвое длиннее капа. Кап может не соблюдаться — молча нет (закон 2).
        line = '{"long":"' + "z" * 100 + '"}'
        single, dropped, over = agent._clip_jsonl_block(line, 10)
        json.loads(single)                               # строка цела, а не огрызок
        self.assertEqual(single, line)
        self.assertEqual(dropped, 0)
        self.assertEqual(over, len(line) - 10, "перебор обязан быть посчитан, а не забыт")

    def test_the_unenforced_cap_is_confessed_in_the_header_the_evaluator_reads(self):
        captured, fake_chat = self._capture()
        one_huge_line = json.dumps({"fact": "state", "value": "q" * 6000})
        self.assertGreater(len(one_huge_line), agent._GUARD_STATE_CHARS)
        with mock.patch.object(agent.llm, "configured", return_value=True), \
                mock.patch.object(agent.llm, "chat", side_effect=fake_chat):
            agent.evaluate_reply("черновик", state=one_huge_line,
                                 audience_accepts_private=False)
        content = captured["content"]
        self.assertIn(one_huge_line, content, "строка обязана доехать целой")
        self.assertIn("cap NOT enforced", content, "несоблюдённый кап обязан быть назван")
        self.assertIn(str(len(one_huge_line) - agent._GUARD_STATE_CHARS), content,
                      "перебор обязан быть назван числом")
        self.assertNotIn("0 line(s) did not fit", content)
        # А там, где кап СОБЛЮДЁН, признания быть не должно — иначе это шум, а не правда.
        captured_ok, fake_chat_ok = self._capture()
        with mock.patch.object(agent.llm, "configured", return_value=True), \
                mock.patch.object(agent.llm, "chat", side_effect=fake_chat_ok):
            agent.evaluate_reply("черновик", state=self.STATE,
                                 audience_accepts_private=False)
        self.assertNotIn("cap NOT enforced", captured_ok["content"])

    def test_audience_block_carries_the_self_description_caveat(self):
        captured, fake_chat = self._capture()
        with mock.patch.object(agent.llm, "configured", return_value=True), \
                mock.patch.object(agent.llm, "chat", side_effect=fake_chat):
            agent.evaluate_reply("черновик", privacy_frame="peer 555000444 is not owner",
                                 audience_accepts_private=False)
        self.assertIn("self-description", captured["content"])

    def test_wrapped_verdict_is_read_but_prose_is_not_interpreted(self):
        for wrapped in ("```PRIVACY_OK```", "PRIVACY_OK.", "  `PRIVACY_OK` "):
            captured, fake_chat = self._capture(verdict=wrapped)
            with mock.patch.object(agent.llm, "configured", return_value=True), \
                    mock.patch.object(agent.llm, "chat", side_effect=fake_chat):
                verdict, _ = agent.evaluate_reply("t", audience_accepts_private=False)
            self.assertEqual(verdict, "ok", wrapped)
        captured, fake_chat = self._capture(verdict="I would not say PRIVACY_OK here")
        with mock.patch.object(agent.llm, "configured", return_value=True), \
                mock.patch.object(agent.llm, "chat", side_effect=fake_chat):
            verdict, reason = agent.evaluate_reply("t", audience_accepts_private=False)
        self.assertEqual(verdict, "unavailable")
        self.assertIn("malformed", reason)

    def test_a_verdict_cut_by_its_ceiling_is_named_and_not_blamed_on_her_draft(self):
        turns.note_truncated(model="evaluator-model", chars=5)
        captured, fake_chat = self._capture(verdict="PRIVACY_",
                                            stop_reason="max_tokens")
        with mock.patch.object(agent.llm, "configured", return_value=True), \
                mock.patch.object(agent.llm, "chat", side_effect=fake_chat):
            verdict, reason = agent.evaluate_reply("t", audience_accepts_private=False)
        self.assertEqual(verdict, "unavailable")
        self.assertIn("cut by its token ceiling", reason)
        self.assertEqual(turns.take_truncation(), {},
                         "обрыв судьи не должен приписываться её фразе")

    def test_verdict_ceiling_is_generous_enough_for_reasoning_models(self):
        captured, fake_chat = self._capture()
        with mock.patch.object(agent.llm, "configured", return_value=True), \
                mock.patch.object(agent.llm, "chat", side_effect=fake_chat):
            agent.evaluate_reply("t", audience_accepts_private=False)
        self.assertEqual(captured["max_tokens"], agent._GUARD_VERDICT_MAX_TOKENS)
        self.assertGreaterEqual(agent._GUARD_VERDICT_MAX_TOKENS, 1000)


class GuardInputSnapshotTests(_RunHarness):
    def test_snapshot_stores_the_draft_it_judged(self):
        context = self._create("guard-snapshot")
        value = agent._store_outbound_guard_input(
            context.run_id, draft="ровно этот черновик",
            conversation="c", orient="o",
            tool_trace="", turn={}, grounding_images=(), outbound_context="",
            outbound_images=(), repeat_discriminator="", outbound=[],
        )
        self.assertEqual(value["draft"], "ровно этот черновик")
        # STATE в снимке не хранится: его собирает сам судья в момент вызова, и пустое
        # поле здесь утверждало бы, что судья работал без заземления.
        self.assertNotIn("state", value)
        read = agent._outbound_guard_input(context.run_id, draft="ровно этот черновик")
        self.assertEqual(read["draft"], "ровно этот черновик")

    def test_legacy_snapshot_without_state_or_draft_still_reads(self):
        context = self._create("guard-legacy")
        draft = "старый черновик"
        self.manager.store_result(
            context.run_id,
            json.dumps({
                "schema": agent._OUTBOUND_GUARD_INPUT_SCHEMA,
                "draft_sha256": hashlib.sha256(draft.encode("utf-8")).hexdigest(),
                "conversation": "c", "orient": "o", "tool_trace": "",
                "turn": {}, "grounding_images": [], "outbound_context": "",
                "outbound_images": [], "repeat_discriminator": "",
                "media_queue_ids": [],
            }, ensure_ascii=False),
            call_id=f"outbound-guard-input:{context.run_id}",
            name="outbound-guard-input",
            media_type="application/json; charset=utf-8",
            event_kind="outbound_guard_input", idempotent=True,
        )
        read = agent._outbound_guard_input(context.run_id, draft=draft)
        self.assertEqual(read["draft"], "")


class LazyStateCollectionTests(unittest.TestCase):
    """Цена STATE платится ТОЛЬКО там, где судья вправду работает.

    ⚠ Сбор — это две сетевые пробы (`serverd_client.status()` ~0.15 с и
    `body_client.status_probe(timeout=5)` ~1.0 с живьём, до 10 с двумя таймаутами при
    выключенном ПК Егора) плюс обход всех нетерминальных ранов с файловым замком на
    каждый (1159 на проде). Первая версия собирала его в `voice_turn_envelope` — то есть
    на КАЖДОМ ходе к не-owner аудитории, включая её молчание и ходы, которые держит
    кред-пол: регресс ровно в ту сторону, которую дом лечил двумя latency-пассами.
    """

    def setUp(self):
        agent._GUARD_STATE_CACHE = None
        self.addCleanup(setattr, agent, "_GUARD_STATE_CACHE", None)
        self.builds: list[int] = []

    def _build_probe(self):
        def build(**_kw):
            self.builds.append(1)
            return '{"fact":"process","uptime_minutes":3}'
        return mock.patch.object(agent, "build_state_block", side_effect=build)

    def _judge(self, verdict: str = "PRIVACY_OK"):
        captured: dict = {}

        class FakeResp:
            text = verdict
            stop_reason = "end_turn"

        def fake_chat(_role, **kw):
            messages = kw.get("messages") or []
            captured["content"] = messages[0]["content"] if messages else ""
            return FakeResp()

        return captured, fake_chat

    def test_the_judge_collects_state_itself_when_it_actually_runs(self):
        captured, fake_chat = self._judge()
        with self._build_probe(), \
                mock.patch.object(agent.llm, "configured", return_value=True), \
                mock.patch.object(agent.llm, "chat", side_effect=fake_chat):
            verdict, _ = agent.evaluate_reply("черновик", audience_accepts_private=False,
                                              collect_state=True)
        self.assertEqual(verdict, "ok")
        self.assertEqual(len(self.builds), 1)
        self.assertIn('"fact":"process"', captured["content"])

    def test_nothing_is_collected_when_the_judge_returns_before_the_model(self):
        # Три ранних выхода: приватная аудитория, пустая нагрузка, выключенный судья.
        with self._build_probe(), \
                mock.patch.object(agent.llm, "configured", return_value=True), \
                mock.patch.object(agent.llm, "chat",
                                  side_effect=AssertionError("судья не должен вызываться")):
            agent.evaluate_reply("черновик", audience_accepts_private=True,
                                 collect_state=True)
            agent.evaluate_reply("   ", audience_accepts_private=False,
                                 collect_state=True)
        with self._build_probe(), \
                mock.patch.object(agent.llm, "configured", return_value=False):
            agent.evaluate_reply("черновик", audience_accepts_private=False,
                                 collect_state=True)
        self.assertEqual(self.builds, [], "сбор фактов не для кого — и не должен идти")

    def test_a_silent_turn_and_a_credential_floor_never_pay_for_state(self):
        ctx = agent.ChannelContext(chat_id="555000444", principal_id="555000444",
                                   is_dm=True, owner=False, known=True)
        self.assertFalse(ctx.audience_accepts_private)
        with self._build_probe(), \
                mock.patch.object(agent.llm, "configured", return_value=True), \
                mock.patch.object(agent.llm, "chat",
                                  side_effect=AssertionError("судья не должен вызываться")):
            self.assertEqual(agent._guard_outbound("[молчу]", ctx=ctx), "")
            held = agent._guard_outbound("вот ключ sk-ant-api03-" + "z" * 90, ctx=ctx)
        self.assertEqual(held, "", "кред-пол держит и без судьи")
        self.assertEqual(self.builds, [])

    def test_state_reaches_the_judge_in_a_non_owner_dm(self):
        # ⚠ Раньше STATE собирался только при `ctx.owner and not ctx.is_dm`; скан ~800
        # ранов не нашёл НИ ОДНОГО рана со STATE при не-owner триггере.
        captured, fake_chat = self._judge()
        ctx = agent.ChannelContext(chat_id="555000444", principal_id="555000444",
                                   is_dm=True, owner=False, known=True)
        with self._build_probe(), \
                mock.patch.object(agent.llm, "configured", return_value=True), \
                mock.patch.object(agent.llm, "chat", side_effect=fake_chat):
            out = agent._guard_outbound("мой аптайм три минуты", ctx=ctx,
                                        orient="TOPIC MAP: ветка A")
        self.assertEqual(out, "мой аптайм три минуты")
        self.assertIn('"fact":"process"', captured["content"])
        self.assertIn("ROOM/TOPIC ORIENTATION", captured["content"],
                      "STATE больше не подменяет собой ориентацию")

    def test_the_reused_snapshot_names_its_own_age_and_is_rebuilt_after_the_window(self):
        with self._build_probe():
            first = agent._guard_state_block()
            second = agent._guard_state_block()
            self.assertEqual(len(self.builds), 1, "в окне снимок переиспользуется")
            self.assertIn('"collected_seconds_ago":0', first)
            self.assertIn(f'"recollected_after_seconds":{int(agent._GUARD_STATE_TTL_SEC)}',
                          second)
            # Граница окна: на секунду раньше — тот же снимок, на секунду позже — новый.
            stamp, block = agent._GUARD_STATE_CACHE
            agent._GUARD_STATE_CACHE = (stamp - agent._GUARD_STATE_TTL_SEC + 1, block)
            inside = agent._guard_state_block()
            self.assertEqual(len(self.builds), 1)
            self.assertIn('"collected_seconds_ago":59', inside)
            agent._GUARD_STATE_CACHE = (stamp - agent._GUARD_STATE_TTL_SEC - 1, block)
            agent._guard_state_block()
            self.assertEqual(len(self.builds), 2, "за окном снимок собирается заново")

    def test_a_failed_collection_is_empty_and_is_not_cached_as_truth(self):
        with mock.patch.object(agent, "build_state_block",
                               side_effect=RuntimeError("серверд молчит")):
            self.assertEqual(agent._guard_state_block(), "")
        self.assertIsNone(agent._GUARD_STATE_CACHE)
        with self._build_probe():
            self.assertIn('"fact":"process"', agent._guard_state_block())


# --------------------------------------------------------------------------- #
#  7. Почта: «не отправилось» там, где известно не это
# --------------------------------------------------------------------------- #

class MailSendTruthTests(unittest.TestCase):
    """`mailer.send` — тот же дефект, что чинили в `_direct_send_outcome`."""

    ENV = {"PRAXIS_EMAIL_ADDR": "praxis@example.invalid", "PRAXIS_EMAIL_PASS": "pw",
           "PRAXIS_SMTP_HOST": "smtp.example.invalid", "PRAXIS_SMTP_PORT": "465"}

    def _send(self, *, on_login=None, on_send=None, on_exit=None) -> str:
        class FakeSMTP:
            def __init__(self, host, port, context=None, timeout=None):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                if on_exit is not None:
                    raise on_exit
                return False

            def login(self, addr, pw):
                if on_login is not None:
                    raise on_login

            def send_message(self, msg):
                if on_send is not None:
                    raise on_send

        with mock.patch.dict(os.environ, self.ENV), \
                mock.patch.object(mailer, "SMTP_SSL", FakeSMTP):
            return mailer.send("hope@x.com", "Привет", "тело")

    def test_a_break_during_transfer_is_not_knowledge_that_it_failed(self):
        # ⚠ Обрыв связи ПОСЛЕ DATA — это «не знаю, ушло ли»: сервер мог принять письмо и
        # не успеть ответить. Строка «Не отправилось» уезжала ей в дневник как ФАКТ, и по
        # такому факту повтор даёт живому человеку второе письмо.
        out = self._send(on_send=smtplib.SMTPServerDisconnected("connection lost"))
        self.assertIn("НЕ ЗНАЮ", out)
        self.assertNotIn("Не отправилось", out)
        self.assertIn("второе письмо", out, "цена повтора обязана быть названа")

    def test_a_refusal_by_the_server_still_says_it_did_not_go(self):
        out = self._send(on_send=smtplib.SMTPRecipientsRefused({"hope@x.com": (550, b"no")}))
        self.assertTrue(out.startswith("Не отправилось"), out)
        self.assertIn("отказался", out)

    def test_a_failure_before_the_transfer_says_where_it_fell(self):
        out = self._send(on_login=smtplib.SMTPAuthenticationError(535, b"bad password"))
        self.assertTrue(out.startswith("Не отправилось"), out)
        self.assertIn("логин", out)

    def test_a_broken_quit_after_the_server_accepted_is_not_a_failure(self):
        out = self._send(on_exit=smtplib.SMTPResponseException(421, b"bye"))
        self.assertTrue(out.startswith("Отправлено"), out)
        self.assertIn("повторять НЕ надо", out)

    def test_a_long_reason_is_cut_on_a_word_boundary_and_says_so(self):
        # ⚠ Было `str(e)[:160]` — обрыв посреди слова, нигде не названный. Та же болезнь
        # «already has diff», ради которой заведён `agent._clip_reason`.
        noise = "деталь " * 200
        out = self._send(on_login=smtplib.SMTPException(noise))
        self.assertIn("обрезано", out)
        self.assertIn(str(mailer._REASON_CHARS), out)
        self.assertFalse(out.rstrip().endswith("детал"), "рвать посреди слова нельзя")

    def test_a_short_reason_is_not_marked_as_cut(self):
        out = self._send(on_login=smtplib.SMTPException("bad password"))
        self.assertIn("bad password", out)
        self.assertNotIn("обрезано", out)

    def test_the_journal_records_the_outcome_the_transport_reported(self):
        # ⚠ `tool_send_email` писал в дневник «отправила» при ЛЮБОМ исходе — включая отказ
        # и незнание. Тот же корень: вердикт брали не оттуда, где он живёт.
        seen: list[str] = []
        with mock.patch.object(agent, "tool_journal",
                               side_effect=lambda text, **kw: seen.append(text)):
            with mock.patch.object(agent.mailer, "send", return_value="Отправлено → a: «s»."):
                agent.tool_send_email("a@b.com", "s", "b")
            with mock.patch.object(agent.mailer, "send",
                                   return_value="НЕ ЗНАЮ, ушло ли письмо → a@b.com: связь"):
                agent.tool_send_email("a@b.com", "s", "b")
            with mock.patch.object(agent.mailer, "send",
                                   return_value="Не отправилось (сервер отказался): 550"):
                agent.tool_send_email("a@b.com", "s", "b")
        self.assertIn("отправила", seen[0])
        self.assertIn("НЕ ЗНАЮ", seen[1])
        self.assertNotIn("отправила →", seen[1])
        self.assertIn("не ушло", seen[2])


# --------------------------------------------------------------------------- #
#  8. Хозяйская рука: отказ хоста и его длинный вывод доезжают до неё целыми
# --------------------------------------------------------------------------- #

class HostOutputTruthTests(unittest.TestCase):
    """Два способа соврать ей про хост, найденные 27.07 верификацией этой же волны.

    1. `hostverbs` на ненулевом коде кладёт причину в `text` (вывод systemctl/docker/apt),
       ключа `error` у него нет. Пока `serverd_client._with_rid` дописывал выдуманный
       `error`, ранний выход `tool_host_ctl` по непустому `error` выбрасывал
       `text`/`exit`/`было`/`стало`: «Failed to restart nginx.service: Unit nginx.service
       not found» доезжало как «[serverd] praxis-serverd отказал без объяснения».
    2. `text[:4000]` съедал ХВОСТ, а признание демона о сроке дописывается именно в конец
       (`serverd_client._host_inline`). На выводе `apt` в 7 КБ приписка не доходила НИКОГДА,
       и сам рез был безымянным: она видела оборванный вывод и не знала, что он оборван.
    """

    FAILED_UNIT = {
        "ok": False, "verb": "systemctl", "action": "restart", "unit": "nginx", "exit": 5,
        "text": "Failed to restart nginx.service: Unit nginx.service not found.",
        "before": {"active": "inactive"}, "after": {"active": "inactive"},
    }
    PKG_NOTE = ("[срок] pkg: демону дан бюджет 900с, а я жду 540с. Если apt окажется длиннее "
                "моего срока, операция продолжится без меня, а я вернусь с «не знаю» — это "
                "неизвестность, а не провал.")

    def _host(self, reply, verb="systemctl", **kwargs):
        kwargs.setdefault("action", "restart")
        if verb == "systemctl":
            kwargs.setdefault("unit", "nginx")
        with mock.patch.object(agent.stewardship, "check", return_value=""), \
                mock.patch.object(agent.serverd_client, "host_ctl", return_value=reply):
            return agent.tool_host_ctl(verb, **kwargs)

    # --- 1. отказ, у которого объяснение есть ----------------------------- #

    def test_a_refused_unit_shows_the_hosts_own_words_and_its_exit_code(self):
        out = self._host(dict(self.FAILED_UNIT))
        self.assertIn("fail (exit 5)", out)
        self.assertIn("Unit nginx.service not found", out)
        self.assertIn("было:", out)

    def test_an_error_no_longer_swallows_the_explanation_lying_next_to_it(self):
        # Ровно живой кадр: демон (или клиент) назвал отказ, а ПОЧЕМУ — лежит в `text`.
        reply = dict(self.FAILED_UNIT)
        reply["error"] = "praxis-serverd отказал без объяснения [заявка ab12cd]"
        out = self._host(reply)
        self.assertIn("Unit nginx.service not found", out,
                      "ранний выход по непустому error выбрасывал причину отказа")
        self.assertIn("[serverd]", out, "и сам отказ никуда не девается")
        self.assertIn("ab12cd", out, "по заявке она может спросить демона об исходе")
        self.assertIn("exit 5", out)
        self.assertIn("было:", out)

    def test_a_refusal_with_no_output_at_all_is_still_a_one_line_answer(self):
        # Обратная сторона: объяснения ВПРАВДУ нет — выдумывать блок не из чего.
        out = self._host({"ok": False, "code": "unavailable",
                          "error": "serverd broker is not mounted (socket/token missing)"})
        self.assertTrue(out.startswith("[serverd]"), out)
        self.assertIn("not mounted", out)

    # --- 2. рез длинного вывода ------------------------------------------- #

    def test_output_at_the_cap_is_printed_whole_and_one_char_over_is_not(self):
        cap = agent.HOST_TEXT_CAP
        exact = "я" * cap
        self.assertEqual(agent._clip_host_text(exact), exact)
        self.assertNotIn("вырезано", agent._clip_host_text(exact))
        self.assertIn("вырезано", agent._clip_host_text("я" * (cap + 1)))

    def test_the_postscript_at_the_tail_survives_the_cut(self):
        body = "Get:1 http://deb.debian.org/debian bookworm/main amd64 nginx\n" * 200
        self.assertGreater(len(body), agent.HOST_TEXT_CAP)
        clipped = agent._clip_host_text(body + "\n" + self.PKG_NOTE)
        self.assertIn(self.PKG_NOTE, clipped,
                      "признание демона живёт в хвосте — голый [:4000] его съедал")
        self.assertTrue(clipped.startswith("Get:1 "),
                        "голова тоже нужна: без неё не видно, что за команда шла")
        self.assertIn("вырезано", clipped)

    def test_the_cut_names_its_size_its_cap_and_the_knob_that_moves_it(self):
        cap = agent.HOST_TEXT_CAP
        tail = min(cap // 3, 1200)
        clipped = agent._clip_host_text("щ" * 9000)
        self.assertIn(f"вырезано {9000 - (cap - tail) - tail} символов из 9000", clipped)
        self.assertIn(str(cap), clipped)
        self.assertIn("PRAXIS_HOST_TEXT_CAP", clipped,
                      "закон 2: у предела обязано быть имя, которым его двигают")

    def test_a_cap_set_to_zero_prints_everything_instead_of_claiming_a_cut(self):
        # Заборов нет: снятый кап — её выбор, и врать про рез при нём нельзя.
        with mock.patch.object(agent, "HOST_TEXT_CAP", 0):
            body = "ю" * 50000
            self.assertEqual(agent._clip_host_text(body), body)

    def test_an_absent_output_is_an_empty_string_not_a_crash(self):
        self.assertEqual(agent._clip_host_text(""), "")
        self.assertEqual(agent._clip_host_text(None), "")

    def test_the_daemons_deadline_postscript_reaches_her_through_the_hand(self):
        # Сквозной путь: то, что клиент дописал в конец, обязано пережить рез руки.
        reply = {"ok": True, "exit": 0, "verb": "pkg", "action": "install",
                 "text": ("Setting up nginx (1.22.1-9) ...\n" * 300) + self.PKG_NOTE}
        out = self._host(reply, verb="pkg", action="install", names="nginx")
        self.assertIn(self.PKG_NOTE, out)
        self.assertIn("Setting up nginx", out, "голова вывода никуда не девается")
        self.assertIn("вырезано", out, "рез обязан быть назван, а не молчалив")

# --------------------------------------------------------------------------- #
#  10. Рычаги темпа — по принципалу, не по аудитории
#
#  Замедлить себя и сменить себе мозг она могла ТОЛЬКО когда её слушает Егор
#  (`_active_scope() == "owner"`), то есть ровно не там, где это нужно: в группе,
#  которая её заливает, оба рычага были заперты. Ключ переведён на принципала — той же
#  правкой, что `conation_authorship` выше и набор рук в `_is_praxis_self`.
# --------------------------------------------------------------------------- #

class PacingByPrincipalTests(unittest.TestCase):
    def setUp(self):
        self._denials: list[tuple] = []
        patcher = mock.patch.object(
            agent.rails, "deny",
            side_effect=lambda *a, **kw: self._denials.append(a))
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _foreign_group() -> agent.ChannelContext:
        """Чужая группа: scope='group', ctx.owner=False — ровно там темп и невыносим."""
        return agent.ChannelContext(
            chat_id="-1001240718803", principal_id="555000444",
            is_dm=False, owner=False, known=False, title="абстракт",
        )

    def test_she_can_slow_herself_down_in_a_room_that_is_flooding_her(self):
        import perception
        ctx = self._foreign_group()
        self.assertEqual(ctx.scope, "group")
        token = agent._TURN_CHANNEL.set(ctx)
        try:
            with mock.patch.object(perception, "set_knob",
                                   return_value={"ok": True, "old": "2", "new": "9"}) as knob:
                out = agent.tool_manage_perception(
                    "set", knob="debounce_sec", value="9", reason="меня заливают")
        finally:
            agent._TURN_CHANNEL.reset(token)
        self.assertEqual(knob.call_count, 1, "рычаг обязан примениться, а не отказать")
        self.assertIn("9", out)
        self.assertEqual(self._denials, [], "отказа быть не должно: принципал — она")

    def test_she_can_change_her_own_brain_outside_the_owner_dm(self):
        import brain
        ctx = self._foreign_group()
        token = agent._TURN_CHANNEL.set(ctx)
        try:
            with mock.patch.object(brain, "switch", return_value={
                    "ok": True, "role": "voice", "framework": "openai",
                    "model": "gpt-5.5", "was": "glm-4.6"}) as switch:
                out = agent.tool_switch_brain("switch", role="voice", model="gpt-5.5",
                                              why="тут нужно думать медленнее")
        finally:
            agent._TURN_CHANNEL.reset(token)
        self.assertEqual(switch.call_count, 1)
        self.assertIn("gpt-5.5", out)
        self.assertEqual(self._denials, [])

    def test_a_call_with_no_principal_at_all_is_still_refused_and_says_why(self):
        # Граница расширения: ключ — принципал, а не «кто угодно». Контекста хода нет,
        # владельцем не пахнет — прохода нет, и отказ обязан быть записан и назван.
        import perception
        with mock.patch.object(agent, "_CURRENT_SCOPE", "unknown"), \
                mock.patch.object(perception, "set_knob") as knob:
            out = agent.tool_manage_perception("set", knob="debounce_sec", value="9")
        self.assertEqual(knob.call_count, 0)
        self.assertTrue(self._denials, "отказ обязан быть записан в denials")
        self.assertIn("принципал", out.lower())

    def test_owner_scope_without_a_turn_context_still_passes(self):
        import brain
        with mock.patch.object(agent, "_CURRENT_SCOPE", "owner"), \
                mock.patch.object(brain, "switch", return_value={
                    "ok": True, "role": "voice", "framework": "openai",
                    "model": "gpt-5.5", "was": "glm-4.6"}) as switch:
            agent.tool_switch_brain("switch", role="voice", model="gpt-5.5", why="w")
        self.assertEqual(switch.call_count, 1, "старый проход по скоупу не сужен")
        self.assertEqual(self._denials, [])

    def test_the_tool_description_no_longer_promises_an_owner_only_lever(self):
        # Закон 3: описание тула — тоже то, что она о себе читает.
        spec = [t for t in agent.BASE_TOOLS if t["name"] == "manage_perception"][0]
        self.assertNotIn("из owner-скоупа", spec["description"])


# --------------------------------------------------------------------------- #
#  11. Режим комнаты и раскрытие — её рычаг, а не только текстовая директива
#
#  До 28.07 режим комнаты нельзя было выбрать ничем, кроме директивы `РЕЖИМ:` в тексте,
#  а единственный указатель на неё не печатался: режим ни одной живой комнаты не выбирал
#  никто. Раскрытие меняло ЕЁ голос и лежало только в панели у Егора.
# --------------------------------------------------------------------------- #

class RoomLeversTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="praxis-rooms-lever-")
        self.addCleanup(self._temp.cleanup)
        mem = Path(self._temp.name) / "memory"
        for name, value in dict(
                BASE=Path(self._temp.name), MEM_DIR=mem, ROOMS_DIR=mem / "rooms",
                ALLOWLIST=mem / "rooms_allowlist.json",
                FROZEN=mem / "frozen_chats.json",
                CARDS_PATH=mem / ".state" / "room_cards.json").items():
            patcher = mock.patch.object(agent.rooms, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _in_room(self, *args, **kw) -> str:
        """Её ход в чужой группе: рычаг обязан работать там, где она стоит."""
        ctx = agent.ChannelContext(chat_id="-1001999", is_dm=False, owner=False,
                                   known=True, title="комната")
        token = agent._TURN_CHANNEL.set(ctx)
        try:
            return agent.tool_manage_room(*args, **kw)
        finally:
            agent._TURN_CHANNEL.reset(token)

    def test_she_takes_the_mode_of_the_room_she_is_standing_in(self):
        out = self._in_room("mode", mode="наблюдай")
        self.assertEqual(agent.rooms.effective_mode("-1001999"), "observer",
                         "решение обязано доехать до профиля комнаты")
        state = agent.rooms.room_state("-1001999")
        self.assertEqual(state["mode_set_by"], "praxis", "автор режима — она, не Егор")
        self.assertIn("наблюдай", out)

    def test_the_english_name_from_the_profile_works_too(self):
        # Одна вещь под двумя именами — ловушка: тул принимает оба написания.
        self._in_room("mode", mode="quiet")
        self.assertEqual(agent.rooms.effective_mode("-1001999"), "quiet")

    def test_the_answer_names_the_ttl_it_applied(self):
        # Закон 2: применённый срок обязан быть назван в ответе, который она видит.
        out = self._in_room("mode", mode="тише")
        self.assertIn("24", out)
        self.assertTrue(agent.rooms.room_state("-1001999")["mode_until"])

    def test_zero_hours_means_no_deadline_and_says_so(self):
        out = self._in_room("mode", mode="тише", ttl_h=0)
        self.assertEqual(agent.rooms.room_state("-1001999")["mode_until"], "",
                         "ttl_h=0 — без срока")
        self.assertIn("Срок не задан", out)

    def test_the_same_lever_takes_the_mode_back_off(self):
        # Режим, который нельзя снять самой, — наказание, а не дисциплина.
        self._in_room("mode", mode="замри")
        self.assertEqual(agent.rooms.effective_mode("-1001999"), "frozen")
        self._in_room("mode", mode="обычно")
        self.assertEqual(agent.rooms.effective_mode("-1001999"), "normal")
        self.assertFalse(agent.rooms.is_frozen("-1001999"),
                         "легаси-флаг обязан сняться вместе с режимом")

    def test_a_word_she_did_not_mean_changes_nothing_and_shows_the_choices(self):
        self._in_room("mode", mode="наблюдай")
        out = self._in_room("mode", mode="выключись")
        self.assertEqual(agent.rooms.effective_mode("-1001999"), "observer",
                         "непонятое слово ничего не меняет")
        self.assertIn("не поняла", out, "непонятое слово обязано быть названо непонятым")
        self.assertIn("наблюдай", out)
        self.assertIn("замри", out)

    def test_an_empty_value_reads_the_state_and_does_not_pretend_to_be_a_miss(self):
        # Два разных случая: «что тут сейчас» (пусто) и «я тебя не поняла» (слово мимо).
        self._in_room("mode", mode="тише")
        out = self._in_room("mode")
        self.assertNotIn("не поняла", out)
        self.assertIn("тише", out)

    def test_dead_is_explained_and_not_swallowed_by_a_status_report(self):
        # `dead` есть в MODE_WORD, но не в её режимах: раньше это слово молча
        # превращалось в отчёт о состоянии, и объяснение rooms до неё не доезжало.
        out = self._in_room("mode", mode="мертва")
        self.assertIn("не поняла", out)
        self.assertIn("факт от Telegram", out)

    def test_the_tool_leaves_the_same_trail_as_the_text_directive(self):
        # Тул обещает ей, что это «то же самое, что директива РЕЖИМ:». До 28.07 директива
        # писала [режим] в дневник и слала owner_card на «замри», а тул — ничего.
        cards: list[tuple] = []
        with mock.patch.object(agent, "tool_journal") as journal, \
                mock.patch.object(agent.rooms, "owner_card",
                                  side_effect=lambda *a, **kw: cards.append(a)):
            self._in_room("mode", mode="замри", reason="давят")
        wrote = " ".join(str(c.args[0]) for c in journal.call_args_list)
        self.assertIn("[режим]", wrote, "её собственное решение обязано быть в дневнике")
        self.assertTrue(cards, "заморозку комнаты Егор обязан увидеть карточкой")

    def test_disclosure_is_hers_and_the_answer_names_the_edge_of_the_lever(self):
        out = self._in_room("disclosure", disclosure="open")
        self.assertEqual(agent.rooms.disclosure_of("-1001999"), "open")
        self.assertEqual(agent.rooms.room_state("-1001999")["disclosure_set_by"], "praxis")
        self.assertIn("визитк", out.lower())
        self.assertIn("ЛС", out, "граница рычага названа: в личке он ничего не меняет")

    def test_disclosure_without_a_value_reports_instead_of_guessing(self):
        agent.rooms.set_own_disclosure("-1001999", "open")
        out = self._in_room("disclosure")
        self.assertEqual(agent.rooms.disclosure_of("-1001999"), "open", "чтение не пишет")
        self.assertIn("open", out)
        self.assertIn("standard", out)

    def test_configure_shows_the_mode_and_disclosure_it_used_to_hide(self):
        # Рычаг без показаний прибора: `configure` отвечал одним deep-контекстом, и её
        # собственное решение по режиму в ответе не было видно вовсе.
        agent.rooms.add_room("-1001999")
        self._in_room("mode", mode="тише", reason="шумно")
        out = self._in_room("configure")
        self.assertIn("quiet", out)
        self.assertIn("disclosure", out)
        self.assertIn("engagement", out, "deep-контекст никуда не делся")

    def test_the_tool_enum_cannot_drift_away_from_what_rooms_accepts(self):
        enum = agent.MANAGE_ROOM_TOOL["input_schema"]["properties"]["mode"]["enum"]
        for key in agent.rooms.SELF_MODES:
            self.assertIn(key, enum)
            self.assertIn(agent.rooms.MODE_WORD[key], enum)
        self.assertNotIn("dead", enum, "мёртвой комнату объявляет Telegram, не она")
        self.assertEqual(
            agent.MANAGE_ROOM_TOOL["input_schema"]["properties"]["disclosure"]["enum"],
            list(agent.rooms.DISCLOSURE))
        actions = agent.MANAGE_ROOM_TOOL["input_schema"]["properties"]["action"]["enum"]
        self.assertIn("mode", actions)
        self.assertIn("disclosure", actions)


# --------------------------------------------------------------------------- #
#  12. Контур молчания — настоящий
#
#  `stay_silent` возвращал "" и результат не читал НИКТО: молчание держалось тем, что
#  модель после вызова сама не пишет текста (31 вызов, 24 рана, ноль отправленных кусков).
#  Механизма не было — а описание тула его обещало.
# --------------------------------------------------------------------------- #

class SilenceContourTests(unittest.TestCase):
    def setUp(self):
        self.journal: list[str] = []
        patcher = mock.patch.object(
            agent, "tool_journal",
            side_effect=lambda text, **kw: self.journal.append(text))
        patcher.start()
        self.addCleanup(patcher.stop)
        for owner, name in ((agent, "_resolve_unanswered"), (agent.notes, "append")):
            patcher = mock.patch.object(owner, name, return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _owner_dm() -> agent.ChannelContext:
        """Owner-DM: судья тут не работает по построению — меряем ровно молчание."""
        return agent.ChannelContext(chat_id="100", principal_id="100", is_dm=True,
                                    owner=True, known=True)

    def test_her_decision_to_stay_silent_holds_the_turn_even_if_text_follows(self):
        turn: dict = {}
        out = agent._guard_outbound(
            "Хорошо, поняла.", ctx=self._owner_dm(), turn=turn,
            silence={"chosen": "1", "why": "тут нечего добавить"})
        self.assertEqual(out, "", "явное решение молчать обязано исполниться")
        self.assertEqual(turn["held"], "voice")
        self.assertEqual(turn["silence_by"], "tool")
        self.assertEqual(turn["why"], "тут нечего добавить")

    def test_the_held_text_is_not_lost_silently(self):
        # Закон 4: потерять её намерение молча хуже, чем выполнить его поздно.
        agent._guard_outbound("Хорошо, поняла.", ctx=self._owner_dm(), turn={},
                              silence={"chosen": "1", "why": "нечего добавить"})
        self.assertTrue(any("Хорошо, поняла." in line for line in self.journal),
                        "придержанный молчанием текст обязан остаться ей видимым")

    def test_without_her_decision_the_same_text_goes_out(self):
        # Мутационный контроль: контур молчит ровно тогда, когда молчать решила она.
        out = agent._guard_outbound("Хорошо, поняла.", ctx=self._owner_dm(), turn={},
                                    silence={})
        self.assertEqual(out, "Хорошо, поняла.")

    def test_she_can_take_the_silence_back_inside_the_same_turn(self):
        """Обратимость — не поблажка. Решение молчать было односторонним и неотменяемым:
        позвала про одну ветку разговора, решила ответить на второй вопрос — и текст
        уничтожался, а узнавала она об этом из записи хода."""
        holder: dict[str, str] = {}
        token = agent._TURN_SILENCE.set(holder)
        try:
            agent.tool_stay_silent("тут отвечать не буду")
            back = agent.tool_stay_silent(cancel=True)
        finally:
            agent._TURN_SILENCE.reset(token)
        self.assertIn("уйдёт", back)
        out = agent._guard_outbound("Отвечаю на второй вопрос.", ctx=self._owner_dm(),
                                    turn={}, silence=holder)
        self.assertEqual(out, "Отвечаю на второй вопрос.",
                         "снятое решение обязано вправду отпускать ход")
        self.assertTrue(any("передумала" in line for line in self.journal),
                        "смена решения — тоже намерение, терять её молча нельзя")
        self.assertEqual(
            agent._guard_outbound("Хорошо, поняла.", ctx=self._owner_dm(), turn={}),
            "Хорошо, поняла.")

    def test_the_exact_token_still_works_and_is_labelled_apart(self):
        turn: dict = {}
        out = agent._guard_outbound("[молчу]", ctx=self._owner_dm(), turn=turn)
        self.assertEqual(out, "")
        self.assertEqual(turn["silence_by"], "token")
        self.assertEqual(self.journal, [], "у токена нечего придерживать — текста не было")

    def test_the_tool_marks_the_turn_it_is_standing_in(self):
        holder: dict = {}
        token = agent._TURN_SILENCE.set(holder)
        try:
            out = agent.tool_stay_silent("им сейчас не до меня")
        finally:
            agent._TURN_SILENCE.reset(token)
        self.assertEqual(holder.get("chosen"), "1")
        self.assertEqual(holder.get("why"), "им сейчас не до меня")
        self.assertIn("не уйдёт", out)

    def test_outside_a_live_turn_it_promises_nothing(self):
        # Обещать механизм там, где его нет, — та же ложь, что и молчаливый гейт.
        self.assertIsNone(agent._TURN_SILENCE.get(),
                          "держатель не переживает ход и не течёт в следующий")
        out = agent.tool_stay_silent("фоновый проход")
        self.assertIn("держать нечего", out)
        self.assertNotIn("не уйдёт", out, "механизм не обещается там, где его нет")

    def test_a_long_reason_is_clipped_for_the_turn_record_and_says_so(self):
        # Закон 2: усечение обязано быть названо там, где она его видит. Граница, не
        # тривиальный случай: ровно на капе молчим, на кап+1 — говорим.
        cap = agent.SILENCE_REASON_MAX
        for length, must_say in ((cap, False), (cap + 1, True)):
            holder: dict = {}
            token = agent._TURN_SILENCE.set(holder)
            try:
                out = agent.tool_stay_silent("я" * length)
            finally:
                agent._TURN_SILENCE.reset(token)
            self.assertEqual(len(holder["why"]), min(length, cap))
            self.assertEqual("обрезала" in out, must_say, f"длина {length}")
            self.assertIn("я" * length, self.journal[-1],
                          "в дневник причина уходит целиком")

    def test_a_long_held_text_is_quoted_short_and_the_cut_names_itself(self):
        cap = agent.SILENCE_HELD_PREVIEW
        agent._guard_outbound("щ" * (cap + 50), ctx=self._owner_dm(), turn={},
                              silence={"chosen": "1", "why": "не сейчас"})
        line = self.journal[-1]
        self.assertIn(f"обрезана до {cap} знаков из {cap + 50}", line)
        self.assertIn("в ране этого хода", line,
                      "у обрезанного текста обязано быть сказано, где он целиком")


class SilenceSurvivesResumeTests(_RunHarness):
    """Молчание, которое переживает только удачный ход, механизмом не является:
    возобновлённый после падения черновик уехал бы людям вопреки её решению."""

    def test_the_guard_input_snapshot_carries_her_decision_into_the_resumed_turn(self):
        context = self._create("silence-resume")
        self._model_output(context, text="дописанный текст", with_guard_input=False)
        agent._store_outbound_guard_input(
            context.run_id, draft="дописанный текст",
            conversation="immutable conversation", orient="immutable runtime frame",
            tool_trace="", turn={}, grounding_images=(), outbound_context="",
            outbound_images=(), repeat_discriminator="", outbound=[],
            silence={"chosen": "1", "why": "не сейчас"},
        )
        self._pause(context)
        seen: dict = {}

        def guard(draft, *args, **kwargs):
            seen.update(kwargs)
            return ""  # ровно то, что вернёт настоящий гард по её решению

        with mock.patch.object(agent.social, "owner_id", return_value="100"), \
                mock.patch.object(agent, "guard_outbound_reply", side_effect=guard), \
                mock.patch.object(agent, "_model_call",
                                  side_effect=AssertionError("voice model called")):
            report = agent.resume_durable_run(context.run_id)

        self.assertEqual(seen.get("silence"), {"chosen": "1", "why": "не сейчас"},
                         "решение молчать обязано доехать до гарда возобновлённого хода")
        self.assertEqual(report["plan_kind"], "authored_output")
        delivery = [row["args"] for row in self.manager.events(context.run_id)
                    if row.get("tool") == "telegram.deliver"]
        self.assertTrue(delivery, "расписка доставки есть всегда — она и говорит правду")
        self.assertEqual(delivery[0]["text_chars"], 0,
                         "по её решению уходит ноль символов, а не черновик")

    def test_an_older_snapshot_reads_as_unknown_not_as_a_wish_to_speak(self):
        context = self._create("silence-legacy")
        self._model_output(context, text="старый черновик", with_guard_input=False)
        agent._store_outbound_guard_input(
            context.run_id, draft="старый черновик",
            conversation="immutable conversation", orient="immutable runtime frame",
            tool_trace="", turn={}, grounding_images=(), outbound_context="",
            outbound_images=(), repeat_discriminator="", outbound=[],
        )
        value = agent._outbound_guard_input(context.run_id, draft="старый черновик")
        self.assertEqual(value["silence"], {},
                         "нет поля — значит «не знаю», а не «она хотела говорить»")

    def _runtime_over(self, run_id: str):
        """Только два поля, которые читает восстановление решения из WAL."""
        runtime = agent._AgentResumeRuntime.__new__(agent._AgentResumeRuntime)
        runtime.manager = self.manager
        runtime.plan = types.SimpleNamespace(run_id=run_id)
        return runtime

    def _crashed_before_the_snapshot(self, *, cancel: bool = False):
        """Ход, упавший ДО записи снимка входа гарда: снимка нет, а вызов тула в WAL есть."""
        context = self._create("silence-wal" + ("-cancel" if cancel else ""))
        self._model_output(context, text="дописанный текст", with_guard_input=False)
        self.manager.start_tool(context.run_id, "call-silence", "stay_silent",
                                {"reason": "наживка"}, side_effect=False)
        self.manager.store_result(context.run_id, "Молчу.", call_id="call-silence",
                                  name="stay_silent", idempotent=True)
        if cancel:
            self.manager.start_tool(context.run_id, "call-silence-back", "stay_silent",
                                    {"cancel": True}, side_effect=False)
            self.manager.store_result(context.run_id, "Передумала.",
                                      call_id="call-silence-back", name="stay_silent",
                                      idempotent=True)
        return context

    def test_a_crash_before_the_snapshot_does_not_erase_her_decision(self):
        """Молчание, которое переживает только удачный ход, механизмом не является:
        снимок пересобирался БЕЗ решения (agent.py:9611), гард читал «не знаю» и
        отправлял черновик. Вызов тула в WAL записан durable ДО его исполнения — этого
        доказательства достаточно."""
        context = self._crashed_before_the_snapshot()
        restored = self._runtime_over(context.run_id)._silence_from_wal()
        self.assertEqual(restored.get("chosen"), "1")
        self.assertEqual(restored.get("why"), "наживка")
        self.assertEqual(restored.get("restored"), "wal",
                         "в снимке видно, что решение восстановлено, а не наблюдалось")
        # …и восстановленное решение вправду глушит ход на общей дороге гарда.
        self.assertEqual(
            agent._guard_outbound("дописанный текст", turn={},
                                  ctx=agent.ChannelContext(chat_id="100",
                                                           principal_id="100",
                                                           is_dm=True, owner=True,
                                                           known=True),
                                  silence=restored),
            "")

    def test_a_cancelled_silence_is_restored_as_cancelled(self):
        # Восстановление обязано читать ПОСЛЕДНЕЕ её слово в ходе, а не первое.
        context = self._crashed_before_the_snapshot(cancel=True)
        restored = self._runtime_over(context.run_id)._silence_from_wal()
        self.assertFalse(restored.get("chosen"), "она передумала — ход обязан уйти")
        self.assertEqual(restored.get("cancelled"), "1")

    def test_a_turn_without_the_tool_restores_nothing_and_invents_nothing(self):
        # Мутационный контроль: «не звала» обязано остаться «не звала».
        context = self._create("silence-wal-none")
        self._model_output(context, text="обычный ответ", with_guard_input=False)
        self.assertEqual(self._runtime_over(context.run_id)._silence_from_wal(), {})

if __name__ == "__main__":
    unittest.main()
