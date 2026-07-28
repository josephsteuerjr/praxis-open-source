from __future__ import annotations

import atexit
import inspect
import os
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

_SESSION_DIR = Path(tempfile.mkdtemp(prefix="praxis-direct-outbox-session-"))
atexit.register(shutil.rmtree, _SESSION_DIR, True)
os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test")
os.environ["TELEGRAM_SESSION"] = str(_SESSION_DIR / "telethon")
os.environ.setdefault("PRAXIS_TEST", "1")

import agent  # noqa: E402
import mtproto_runner as runner  # noqa: E402
import tasks  # noqa: E402
import telegram_outbox  # noqa: E402


class MutableClock:
    def __init__(self, value: float = 1000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class DirectIdentityTests(unittest.TestCase):
    def test_direct_send_requires_matching_durable_tool_identity(self):
        with patch.object(agent, "current_tool_execution", return_value=None):
            with self.assertRaises(agent.DurableExecutionError):
                runner._direct_tool_execution("send_message")
        with patch.object(agent, "current_tool_execution", return_value={
            "run_id": "run-1", "call_id": "call-1", "tool": "send_file",
        }):
            with self.assertRaises(agent.DurableExecutionError):
                runner._direct_tool_execution("send_message")

    def test_startup_splits_structural_recovery_from_executable_resume(self):
        source = inspect.getsource(runner.main)
        positions = [source.index(marker) for marker in (
            'agent._TELETHON["project_direct_outbox_acceptance"]',
            "agent.recover_durable_state",
            "bufstore.load_all",
            "_direct_outbox_once()",
            "_media_cleanup_once()",
            "_text_outbox_once()",
            "agent.resume_durable_runs",
        )]
        self.assertEqual(positions, sorted(positions))


class DirectOutboxReplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="praxis-direct-outbox-")
        self.clock = MutableClock()
        self.old_outbox = runner._DIRECT_OUTBOX
        self.old_reconciled = runner._DIRECT_OUTBOX_RECONCILED
        runner._DIRECT_OUTBOX = telegram_outbox.TelegramOutbox(
            Path(self.tempdir.name) / "outbox",
            clock=self.clock,
            base_backoff_seconds=1.0,
            max_backoff_seconds=10.0,
        )
        runner._DIRECT_OUTBOX_RECONCILED = set()

    async def asyncTearDown(self):
        runner._DIRECT_OUTBOX = self.old_outbox
        runner._DIRECT_OUTBOX_RECONCILED = self.old_reconciled
        self.tempdir.cleanup()

    async def test_accepted_text_is_never_sent_again(self):
        entry = runner._direct_outbox().prepare_text(
            "telegram-outbox:run-1:tool:call-1",
            peer_id=42,
            topic_id=101,
            reply_to=101,
            text="hello",
            run_id="run-1",
            call_id="call-1",
            purpose="tool:send_message",
        )
        entity = types.SimpleNamespace(id=42)
        send = AsyncMock(return_value=(
            types.SimpleNamespace(id=91), entry["random_id"],
        ))
        resolve = AsyncMock(return_value=entity)
        with (
            patch.object(agent, "direct_outbox_prepared", return_value=True),
            patch.object(runner, "_resolve_entity", resolve),
            patch.object(runner, "_send_message_idempotent", send),
        ):
            accepted = await runner._send_direct_outbox_entry(entry)
            replay = await runner._send_direct_outbox_entry(accepted)

        self.assertEqual(accepted["state"], "accepted")
        self.assertEqual(replay["receipt"]["message_id"], 91)
        self.assertEqual(send.await_count, 1)
        self.assertEqual(resolve.await_count, 1)
        self.assertEqual(send.await_args.kwargs["random_id"], entry["random_id"])
        self.assertEqual(send.await_args.kwargs["reply_to"], 101)

    async def test_clock_retries_pending_with_the_same_random_id_without_model(self):
        entry = runner._direct_outbox().prepare_text(
            "telegram-task:t1:occurrence-1",
            peer_id=42,
            text="scheduled",
            run_id="schedule:t1",
            call_id="occurrence:1",
            purpose="task:message",
        )
        resolve = AsyncMock(return_value=types.SimpleNamespace(id=42))
        send = AsyncMock(side_effect=ConnectionError("offline"))
        with (
            patch.object(runner, "_resolve_entity", resolve),
            patch.object(runner, "_send_message_idempotent", send),
        ):
            await runner._direct_outbox_once()
            retry = runner._direct_outbox().get(entry["key"])
            self.assertEqual(retry["state"], "retry")
            self.clock.value += 2.0
            send.side_effect = None
            send.return_value = (types.SimpleNamespace(id=92), entry["random_id"])
            await runner._direct_outbox_once()
            await runner._direct_outbox_once()

        accepted = runner._direct_outbox().get(entry["key"])
        self.assertEqual(accepted["state"], "accepted")
        self.assertEqual(accepted["receipt"]["message_id"], 92)
        self.assertEqual(send.await_count, 2)
        self.assertTrue(all(
            call.kwargs["random_id"] == entry["random_id"]
            for call in send.await_args_list
        ))

    async def test_file_replay_uses_staged_blob_but_visible_original_name(self):
        source = Path(self.tempdir.name) / "report.txt"
        source.write_text("result", encoding="utf-8")
        entry = runner._direct_outbox().prepare_file(
            "telegram-outbox:run-file:tool:call-file",
            peer_id=-10042,
            topic_id=7,
            reply_to=7,
            source=source,
            visible_filename="report.txt",
            mime="text/plain",
            caption="ready",
            run_id="run-file",
            call_id="call-file",
            purpose="tool:send_file",
        )
        send = AsyncMock(return_value=(
            types.SimpleNamespace(id=93), entry["random_id"],
        ))
        with (
            patch.object(agent, "direct_outbox_prepared", return_value=True),
            patch.object(runner, "_send_file_idempotent", send),
        ):
            accepted = await runner._send_direct_outbox_entry(
                entry, entity=types.SimpleNamespace(id=42),
            )

        item = send.await_args.args[1]
        self.assertEqual(accepted["state"], "accepted")
        self.assertEqual(item.kind, "document")
        self.assertEqual(item.path.name, f"{entry['id']}.blob")
        self.assertEqual(send.await_args.kwargs["visible_filename"], "report.txt")
        self.assertEqual(send.await_args.kwargs["random_id"], entry["random_id"])
        self.assertEqual(send.await_args.kwargs["reply_to"], 7)

    async def test_startup_receipt_reconciles_before_strict_resume_care(self):
        entry = runner._direct_outbox().prepare_text(
            "telegram-outbox:run-recover:tool:call-send",
            peer_id=42,
            text="already accepted",
            run_id="run-recover",
            call_id="call-send",
            purpose="tool:send_message",
        )
        runner._direct_outbox().mark_accepted(
            entry["key"], message_id=96, random_id=entry["random_id"],
        )
        order = []

        def reconcile(accepted):
            order.append(("receipt", accepted["run_id"]))
            return True

        def resume(*, limit):
            order.append(("resume", limit))
            return [{
                "run_id": "run-recover", "plan_kind": "continue_checkpoint",
                "status": "completed", "phase": "checkpoint",
            }]

        with (
            patch.object(agent, "run_direct_outbox_accepted", side_effect=reconcile),
            patch.object(agent, "resume_durable_runs", side_effect=resume),
        ):
            await runner._direct_outbox_once()
            await runner._durable_resume_once()

        self.assertEqual(order, [("receipt", "run-recover"), ("resume", 20)])

    async def test_owner_file_acceptance_projects_one_pwa_delivery(self):
        source = Path(self.tempdir.name) / "voice-result.txt"
        source.write_text("result", encoding="utf-8")
        entry = runner._direct_outbox().prepare_file(
            "telegram-outbox:run-owner-file:tool:call-file",
            peer_id=42,
            source=source,
            visible_filename="voice-result.txt",
            mime="text/plain",
            caption="Готово",
            run_id="run-owner-file",
            call_id="call-file",
            purpose="tool:send_file",
        )
        accepted = runner._direct_outbox().mark_accepted(
            entry["key"], message_id=101, random_id=entry["random_id"],
        )
        emit = Mock(return_value={"id": "delivery-test"})
        with (
            patch.object(runner, "OWNER_ID", 42),
            patch.object(agent, "run_direct_outbox_accepted", return_value=True),
            patch.object(runner.owner_delivery.LEDGER, "emit", emit),
        ):
            self.assertTrue(await runner._reconcile_direct_outbox_entry(accepted))
            self.assertTrue(await runner._reconcile_direct_outbox_entry(accepted))
        emit.assert_called_once()
        self.assertEqual(emit.call_args.args[0], "file_ready")
        self.assertEqual(emit.call_args.kwargs["transports"], ("pwa",))
        self.assertEqual(emit.call_args.kwargs["correlation"]["run_id"], "run-owner-file")

    async def test_scheduled_note_is_durably_claimed_before_mark_fired(self):
        task = {
            "id": "note-1",
            "kind": "note",
            "goal": "remember this",
            "when": "2030-01-01T10:00",
            "created": "2029-12-01T10:00",
        }
        observed = []

        def mark_fired(task_id):
            rows = runner._direct_outbox().accepted()
            observed.append((task_id, len(rows), rows[0]["purpose"] if rows else ""))

        send = AsyncMock(side_effect=lambda _entity, _text, **kwargs: (
            types.SimpleNamespace(id=94), kwargs["random_id"],
        ))
        with (
            patch.object(runner, "OWNER_ID", 42),
            patch.object(runner, "_send_message_idempotent", send),
            patch.object(tasks, "due", return_value=[task]),
            patch.object(tasks, "mark_fired", side_effect=mark_fired),
        ):
            await runner._fire_due_tasks()

        self.assertEqual(observed, [("note-1", 1, "task:note")])
        self.assertEqual(send.await_count, 1)

    async def test_email_disabled_notification_is_claimed_before_mark_fired(self):
        task = {
            "id": "email-1",
            "kind": "email",
            "goal": "send the report",
            "target": "owner@example.invalid",
            "when": "2030-01-01T10:00",
            "created": "2029-12-01T10:00",
        }
        observed = []

        def mark_fired(task_id):
            rows = runner._direct_outbox().accepted()
            observed.append((task_id, len(rows), rows[0]["purpose"] if rows else ""))

        send = AsyncMock(side_effect=lambda _entity, _text, **kwargs: (
            types.SimpleNamespace(id=95), kwargs["random_id"],
        ))
        with (
            patch.dict(os.environ, {"PRAXIS_EMAIL_AUTONOMOUS": "0"}),
            patch.object(runner, "OWNER_ID", 42),
            patch.object(runner, "_send_message_idempotent", send),
            patch.object(tasks, "due", return_value=[task]),
            patch.object(tasks, "mark_fired", side_effect=mark_fired),
        ):
            await runner._fire_due_tasks()

        self.assertEqual(observed, [("email-1", 1, "task:email-disabled")])
        self.assertEqual(send.await_count, 1)

    async def test_autonomous_email_keeps_legacy_mark_before_effect_boundary(self):
        task = {
            "id": "email-2",
            "kind": "email",
            "goal": "send the report",
            "target": "owner@example.invalid",
        }
        order = []

        def mark_fired(task_id):
            order.append(("mark", task_id))

        def send_email(*_args):
            order.append(("email", task["id"]))

        with (
            patch.dict(os.environ, {"PRAXIS_EMAIL_AUTONOMOUS": "1"}),
            patch.object(agent, "tool_send_email", side_effect=send_email),
            patch.object(tasks, "due", return_value=[task]),
            patch.object(tasks, "mark_fired", side_effect=mark_fired),
        ):
            await runner._fire_due_tasks()

        self.assertEqual(order, [("mark", "email-2"), ("email", "email-2")])


if __name__ == "__main__":
    unittest.main()
