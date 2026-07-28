from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import agent
import authored_notes
import run_context
import run_manager


class AuthoredNotesAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.previous_manager = agent._RUN_MANAGER
        agent._RUN_MANAGER = run_manager.RunManager(self.base)

    def tearDown(self):
        agent._RUN_MANAGER = self.previous_manager
        self.temp.cleanup()

    def test_tool_is_available_to_owner_and_praxis_self(self):
        self.assertIn("manage_notes", agent.TOOL_IMPL)
        self.assertIn("manage_notes", {tool["name"] for tool in agent.OWNER_TOOLS})
        self.assertIn("manage_notes", {tool["name"] for tool in agent.PRAXIS_SELF_TOOLS})

    def test_write_binds_run_and_chat_provenance(self):
        context = run_context.RunContext.create(
            kind="task_window",
            goal="think",
            principal_id="praxis:self",
            scope="owner",
            origin_chat_id="42",
            origin_message_ids=(7,),
        )
        context = agent._RUN_MANAGER.create(context, "immutable context")
        agent._RUN_MANAGER.transition(context.run_id, "running")
        context = context.with_status("running")
        turn = agent.ChannelContext(
            chat_id="42",
            principal_id="praxis:self",
            origin_message_id=7,
            _scope_override="owner",
        )
        execution = {"call_id": "call-1"}
        with mock.patch.object(agent, "BASE", self.base), \
                run_context.bind_run(context):
            channel_token = agent._TURN_CHANNEL.set(turn)
            execution_token = agent._TOOL_EXECUTION.set(execution)
            try:
                result = agent.tool_manage_notes(
                    "write", text="живой след", kind="scratch", scope="run",
                )
            finally:
                agent._TOOL_EXECUTION.reset(execution_token)
                agent._TURN_CHANNEL.reset(channel_token)

        self.assertIn("Записала scratch", result)
        rows = authored_notes.AuthoredNoteLedger(self.base).list(status="", run_id=context.run_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["chat_id"], "42")
        self.assertEqual(rows[0]["message_id"], 7)
        self.assertEqual(rows[0]["source_ref"], f"run:{context.run_id}:call:call-1")

    def test_recap_lists_note_metadata_without_text_or_self_reflection(self):
        manager = agent._RUN_MANAGER
        context = run_context.RunContext.create(
            kind="task_window", goal="think", principal_id="praxis:self", scope="owner",
        )
        context = manager.create(context, "immutable context")
        manager.transition(context.run_id, "running")
        ledger = authored_notes.AuthoredNoteLedger(self.base)
        row = ledger.write(
            "private scratch content", kind="reflection", scope="run", run_id=context.run_id,
        )

        with mock.patch.object(agent, "BASE", self.base):
            recap = agent._run_recap_markdown(context.run_id, outcome="done", final_text="finished")

        self.assertIn("## Authored notes", recap)
        self.assertIn(row["id"], recap)
        self.assertNotIn("private scratch content", recap)
        self.assertNotIn("`open`", recap)
        self.assertNotIn("## My reflection", recap)

    def test_chat_scoped_notes_do_not_cross_chat_boundary(self):
        ledger = authored_notes.AuthoredNoteLedger(self.base)
        note_a = ledger.write("chat A", scope="chat", chat_id="A")
        note_b = ledger.write("chat B", scope="chat", chat_id="B")
        with mock.patch.object(agent, "BASE", self.base):
            token = agent._TURN_CHANNEL.set(agent.ChannelContext(chat_id="A", principal_id="1", owner=True))
            try:
                listing = agent.tool_manage_notes("list", status="open")
                read_b = agent.tool_manage_notes("read", note_id=note_b["id"])
                close_b = agent.tool_manage_notes("close", note_id=note_b["id"])
            finally:
                agent._TURN_CHANNEL.reset(token)
        self.assertIn(note_a["id"], listing)
        self.assertNotIn(note_b["id"], listing)
        self.assertEqual(read_b, "Заметка относится к другому чату.")
        self.assertEqual(close_b, "Заметка относится к другому чату.")
        self.assertEqual(ledger.get(note_b["id"])["status"], "open")

    def test_terminal_run_rejects_new_run_scoped_note(self):
        manager = agent._RUN_MANAGER
        context = run_context.RunContext.create(
            kind="task_window", goal="done", principal_id="praxis:self", scope="owner",
        )
        context = manager.create(context, "immutable context")
        manager.transition(context.run_id, "running")
        manager.transition(context.run_id, "done")
        with mock.patch.object(agent, "BASE", self.base), run_context.bind_run(context.with_status("done")):
            result = agent.tool_manage_notes("write", text="too late", scope="run")
        self.assertIn("завершённый run уже заморожен", result)
        self.assertEqual(authored_notes.AuthoredNoteLedger(self.base).metadata_for_run(context.run_id), [])

    def test_terminal_transition_wins_against_waiting_run_note_write(self):
        manager = agent._RUN_MANAGER
        context = run_context.RunContext.create(
            kind="task_window", goal="race", principal_id="praxis:self", scope="owner",
        )
        context = manager.create(context, "immutable context")
        manager.transition(context.run_id, "running")
        context = context.with_status("running")
        ledger = authored_notes.AuthoredNoteLedger(self.base)
        started = threading.Event()
        result: list[str] = []

        def write_note():
            started.set()
            with mock.patch.object(agent, "BASE", self.base), run_context.bind_run(context):
                result.append(agent.tool_manage_notes("write", text="too late", scope="run"))

        with ledger.locked():
            thread = threading.Thread(target=write_note)
            thread.start()
            self.assertTrue(started.wait(1))
            manager.transition(context.run_id, "done")
        thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertIn("завершённый run уже заморожен", result[0])
        self.assertEqual(ledger.metadata_for_run(context.run_id), [])

    def test_global_note_is_not_attached_to_unrelated_run_recap(self):
        manager = agent._RUN_MANAGER
        context = run_context.RunContext.create(
            kind="task_window", goal="think", principal_id="praxis:self", scope="owner",
        )
        context = manager.create(context, "immutable context")
        manager.transition(context.run_id, "running")
        authored_notes.AuthoredNoteLedger(self.base).write("global thought", scope="global")

        with mock.patch.object(agent, "BASE", self.base):
            recap = agent._run_recap_markdown(context.run_id, outcome="done")

        self.assertIn("No explicit authored notes were attached", recap)
        self.assertNotIn("global thought", recap)


if __name__ == "__main__":
    unittest.main()
