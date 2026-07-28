"""PASS 30 Этап 2 — наррация по ходу: класс narration (только твёрдые полы).

Контракты: мимо трибунала; кред-пол механический (без hex-эвристики — git-SHA
её честный материал); дедуп дословного повтора; зазор/выключатель — её рычаги;
адресат — тред задачи (origin_chat) или текущий тред; из чужого канала — только
его собственный тред.

Запуск:  python praxis_test.py test_pass30_stage2 -v
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent
import forge
from core import narration as core_narration
from core import subagents as core_subagents


class NarrationModuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="narr_"))
        self._orig = core_narration.LEDGER
        core_narration.LEDGER = self.tmp / "narration.jsonl"

    def tearDown(self):
        core_narration.LEDGER = self._orig

    def test_kill_switch(self):
        with mock.patch.dict(os.environ, {"PRAXIS_NARRATION": "0"}):
            self.assertFalse(core_narration.enabled())
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop("PRAXIS_NARRATION", None)
            self.assertTrue(core_narration.enabled(), "дефолт — включено")

    def test_credential_floor_catches_tokens_not_engineering(self):
        self.assertTrue(core_narration.credential_floor("вот ключ sk-" + "a" * 24))
        self.assertTrue(core_narration.credential_floor("-----BEGIN RSA PRIVATE KEY-----"))
        self.assertTrue(core_narration.credential_floor("AKIA" + "A" * 16))
        # формы ЕЁ хозяйства, которые уже текли (судья ревью):
        self.assertTrue(core_narration.credential_floor(
            "проверила getMe: 8842770083:AAF4xJq9AbCdEfGh12345678901234567890Qk мёртв"),
            "telegram bot token")
        self.assertTrue(core_narration.credential_floor(
            "ключ d41d8cd98f00b204e9800998ecf8427e.Ab3dEfGh12Jk5678 жив"), "zai key")
        # git-SHA и sha256 — честный инженерный материал, НЕ пол
        self.assertEqual(core_narration.credential_floor(
            "закоммитила " + "a1b2c3d4" * 5), "")
        self.assertEqual(core_narration.credential_floor(
            "expected_sha256=" + "ff" * 32), "")

    def test_dedup_same_text_same_dest(self):
        core_narration.note("chat1", "Собрала тесты, гоняю")
        self.assertTrue(core_narration.is_duplicate("chat1", "Собрала тесты,  гоняю "))
        self.assertFalse(core_narration.is_duplicate("chat2", "Собрала тесты, гоняю"),
                         "другой тред — не дубль")
        self.assertFalse(core_narration.is_duplicate("chat1", "Тесты зелёные, деплою"))

    def test_gap_per_dest(self):
        core_narration.note("chat1", "раз")
        self.assertGreater(core_narration.gap_remaining("chat1", 60.0), 0)
        self.assertEqual(core_narration.gap_remaining("chat2", 60.0), 0.0)
        self.assertEqual(core_narration.gap_remaining("chat1", 0.0), 0.0, "0 = без зазора")

    def test_ledger_ring_compacts(self):
        for i in range(core_narration.KEEP * 2 + 10):
            core_narration.note("c", f"строка {i}")
        rows = core_narration._load()
        self.assertLessEqual(len(rows), core_narration.KEEP + 10)
        self.assertIn("строка", rows[-1]["gist"])


class NarrateToolTests(unittest.TestCase):
    """Тул narrate: адресация, полы, рычаги — без Telegram (мок _TELETHON)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="narr_tool_"))
        self._led = core_narration.LEDGER
        core_narration.LEDGER = self.tmp / "narration.jsonl"
        self.sent: list[tuple] = []
        self._tele = mock.patch.dict(
            agent._TELETHON,
            {"send_message": lambda to, text: self.sent.append((to, text)) or "SENT ok"})
        self._tele.start()
        self.addCleanup(self._tele.stop)

    def tearDown(self):
        core_narration.LEDGER = self._led

    def _ctx(self, chat_id="777", owner=True, scope=None):
        ctx = agent.ChannelContext(
            chat_id=chat_id, principal_id="101", is_dm=True, owner=owner, known=True,
            _scope_override=("owner" if owner else None) if scope is None else scope)
        token = agent._TURN_CHANNEL.set(ctx)
        self.addCleanup(agent._TURN_CHANNEL.reset, token)

    def test_narrates_to_current_thread(self):
        self._ctx(chat_id="777")
        out = agent.tool_narrate("Собрала каркас, иду в тесты")
        self.assertIn("SENT", out)
        self.assertEqual(self.sent[0][0], "777")

    def test_off_switch_honest(self):
        self._ctx()
        with mock.patch.dict(os.environ, {"PRAXIS_NARRATION": "0"}):
            out = agent.tool_narrate("x")
        self.assertIn("выключена", out)
        self.assertEqual(self.sent, [])

    def test_empty_and_cap(self):
        self._ctx()
        self.assertIn("Пустую", agent.tool_narrate("  "))
        self.assertIn("короткая строка", agent.tool_narrate("х" * 2000))
        self.assertEqual(self.sent, [])

    def test_credential_floor_refuses(self):
        self._ctx()
        out = agent.tool_narrate("токен sk-" + "b" * 30)
        self.assertIn("Пол", out)
        self.assertEqual(self.sent, [])

    def test_dedup_second_send_skipped(self):
        self._ctx(chat_id="777")
        with mock.patch.dict(os.environ, {"PRAXIS_NARRATION_GAP": "0"}):
            agent.tool_narrate("Тесты зелёные")
            out = agent.tool_narrate("Тесты  зелёные ")
        self.assertIn("дедуп", out)
        self.assertEqual(len(self.sent), 1)

    def test_gap_is_her_lever(self):
        self._ctx(chat_id="777")
        with mock.patch.dict(os.environ, {"PRAXIS_NARRATION_GAP": "120"}):
            agent.tool_narrate("шаг 1")
            out = agent.tool_narrate("шаг 2")
        self.assertIn("narration_gap_sec", out)
        self.assertEqual(len(self.sent), 1, "второе — после зазора, не потеряно навсегда")

    def test_task_origin_addressing(self):
        self._ctx(chat_id=None, owner=True)
        with mock.patch.object(forge, "task_origin", return_value="-100123#topic:42"):
            out = agent.tool_narrate("Воркер закончил, приняла дифф", task_id="code-x")
        self.assertIn("SENT", out)
        self.assertEqual(self.sent[0][0], "-100123#topic:42")

    def test_task_without_origin_honest(self):
        self._ctx(chat_id="777")
        with mock.patch.object(forge, "task_origin", return_value=""):
            out = agent.tool_narrate("x", task_id="code-y")
        self.assertIn("не записан тред-заказчик", out)
        self.assertEqual(self.sent, [])

    def test_foreign_channel_only_its_own_thread(self):
        self._ctx(chat_id="555", owner=False, scope="private")
        with mock.patch.object(forge, "task_origin", return_value="999"):
            out = agent.tool_narrate("x", task_id="code-z")
        self.assertIn("только в его собственный тред", out)
        self.assertEqual(self.sent, [])
        # свой тред — можно
        out2 = agent.tool_narrate("работаю, вернусь с результатом")
        self.assertIn("SENT", out2)
        self.assertEqual(self.sent[0][0], "555")

    def test_owner_fallback_when_no_thread_is_named(self):
        self._ctx(chat_id=None, owner=True)
        with mock.patch.dict(os.environ, {"PRAXIS_OWNER_ID": "101"}):
            out = agent.tool_narrate("окно: собрала, гоняю тесты")
        self.assertIn("SENT", out)
        self.assertEqual(self.sent[0][0], "101")
        self.assertIn("ЛС Егора", out, "фолбэк в owner-ЛС назван, не молчалив")

    def test_transport_refusal_not_noted(self):
        """Судья ревью: строка-отказ транспорта — НЕ доставка; леджер не травится."""
        self._ctx(chat_id="777")
        with mock.patch.dict(agent._TELETHON, {"send_message":
                lambda to, text: agent.DirectSendRefusal("(не нашла, кому: 777)")}):
            out = agent.tool_narrate("строка процесса")
        self.assertIn("не нашла", out)
        self.assertEqual(core_narration.recent(5), [], "фантомной записи нет")
        # ретрай после починки причины — проходит, а не «уже уходило»
        out2 = agent.tool_narrate("строка процесса")
        self.assertIn("SENT", out2)

    def test_durable_pending_reraises_after_note(self):
        self._ctx(chat_id="777")

        def raise_pending(to, text):
            raise agent.DurableSideEffectPending("k", "telethon closed")

        with mock.patch.dict(agent._TELETHON, {"send_message": raise_pending}):
            with self.assertRaises(agent.DurableSideEffectPending):
                agent.tool_narrate("из окна: доедет после reconnect")
        self.assertTrue(core_narration.recent(1), "леджер записан — дубль не уйдёт")


class DurableContractTests(unittest.TestCase):
    """P1 ревью: narrate — полноправный хозяин durable direct-outbox контракта.

    Реальный шов (не мок моста): identity-гейт, idempotency-key, pause-регексп,
    intent-identity. Мой мок _TELETHON прятал мёртвую фичу — эти тесты живут
    ниже шва."""

    def _exec_ctx(self, tool):
        token = agent._TOOL_EXECUTION.set(
            {"run_id": "r1", "call_id": "c1", "tool": tool})
        self.addCleanup(agent._TOOL_EXECUTION.reset, token)

    def test_identity_gate_accepts_narrate_and_send_message(self):
        os.environ.setdefault("TELEGRAM_API_ID", "1")
        os.environ.setdefault("TELEGRAM_API_HASH", "x")
        os.environ.setdefault(
            "TELEGRAM_SESSION",
            str(Path(tempfile.gettempdir()) / "praxis_test_runner"))
        import mtproto_runner
        self._exec_ctx("narrate")
        ex = mtproto_runner._direct_tool_execution(("send_message", "narrate"))
        self.assertEqual(ex["tool"], "narrate")
        self._exec_ctx("send_message")
        ex2 = mtproto_runner._direct_tool_execution(("send_message", "narrate"))
        self.assertEqual(ex2["tool"], "send_message")
        self._exec_ctx("shell")
        with self.assertRaises(agent.DurableExecutionError):
            mtproto_runner._direct_tool_execution(("send_message", "narrate"))

    def test_idempotency_key_for_narrate(self):
        import types
        current = types.SimpleNamespace(run_id="run-9")
        key = agent._tool_idempotency_key(current, "call-3", "narrate", {"text": "x"})
        self.assertEqual(key, "telegram-outbox:run-9:tool:call-3",
                         "тот же exact-once леджер, что у send_message")

    def test_recovery_pause_regexp_knows_narrate(self):
        import run_resume
        self.assertTrue(run_resume._DIRECT_OUTBOX_PAUSE.match(
            "durable narrate intent awaits Telegram acceptance"))
        self.assertTrue(run_resume._DIRECT_OUTBOX_PAUSE.match(
            "durable send_message intent awaits Telegram acceptance"),
            "существующий протокол не тронут")
        self.assertFalse(run_resume._DIRECT_OUTBOX_PAUSE.match(
            "durable shell intent awaits Telegram acceptance"))

    def test_intent_identity_accepts_narrate_text(self):
        import telegram_outbox
        key = "telegram-outbox:r1:tool:c1"
        entry = {"run_id": "r1", "call_id": "c1", "purpose": "tool:narrate",
                 "key": key, "kind": "text",
                 "random_id": telegram_outbox.stable_random_id(key),
                 "peer_id": 101, "topic_id": None, "reply_to": None,
                 "payload": {"text": "строка процесса"}}
        identity = agent._direct_outbox_identity(entry, verify_file=False)
        self.assertEqual(identity["tool"], "narrate")
        bad = dict(entry, kind="file")
        with self.assertRaises(agent.DurableExecutionError):
            agent._direct_outbox_identity(bad, verify_file=False)


class OriginPlumbingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="narr_forge_"))
        self._orig = {"tasks": forge.TASKS_DIR, "state": forge.STATE_DIR}
        forge.TASKS_DIR = self.tmp / "tasks"
        forge.STATE_DIR = self.tmp
        forge.TASKS_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        forge.TASKS_DIR = self._orig["tasks"]
        forge.STATE_DIR = self._orig["state"]

    def test_start_stores_origin_and_task_origin_reads_it(self):
        target = self.tmp / "proj"
        target.mkdir()
        out = forge.start("smoke наррации", target=str(target), isolation="direct",
                          origin_chat="-100123__topic__42")
        import re
        tid = re.search(r"(code-[a-f0-9]+)", out).group(1)
        self.assertEqual(forge.task_origin(tid), "-100123__topic__42")
        self.assertEqual(forge.task_origin("code-нет"), "")

    def test_subagent_payload_carries_origin(self):
        p = core_subagents.normalize("t", "a", {"status": "done"},
                                     task={"goal": "g", "origin_chat": "chatX"})
        self.assertEqual(p["origin_chat"], "chatX")


class FrameInvitationTests(unittest.TestCase):
    def test_frames_invite_not_oblige(self):
        self.assertIn("narrate", agent._CODING_WINDOW_FRAME)
        self.assertIn("narrate", agent._FORGE_EVENT_FRAME)
        for frame in (agent._CODING_WINDOW_FRAME, agent._FORGE_EVENT_FRAME):
            self.assertIn("invitation", frame.lower())

    def test_knob_exists(self):
        import perception
        self.assertIn("narration_gap_sec", perception.KNOBS)

    def test_tool_registered(self):
        self.assertIn("narrate", agent.TOOL_IMPL)
        names = [t["name"] for t in agent.OWNER_TOOLS]
        self.assertIn("narrate", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
