from __future__ import annotations

import concurrent.futures
import threading
import types
import unittest
from unittest import mock

import agent


class TurnContextIsolationTests(unittest.TestCase):
    def test_parallel_voice_calls_do_not_bleed_scope_chat_or_history(self):
        barrier = threading.Barrier(2)
        seen: dict[str, tuple[str, str | None, str]] = {}

        def fake_chat(*_args, **_kwargs):
            ctx = agent._TURN_CHANNEL.get()
            barrier.wait(timeout=5)
            seen[str(ctx.chat_id)] = (
                agent._active_scope(), agent._active_chat(), agent._active_history()[0]["content"],
            )
            return types.SimpleNamespace(stop_reason="end_turn", text="ok", blocks=[])

        owner = agent.ChannelContext(
            chat_id="101", principal_id="101", is_dm=True, owner=True,
        )
        group = agent.ChannelContext(
            chat_id="-202", principal_id="303", is_dm=False, owner=False, known=True,
        )

        def run(ctx, marker):
            return agent._voice(
                "go", [{"role": "user", "content": marker}], "speaker",
                ctx=ctx, no_tools=True,
            )

        with mock.patch.object(agent, "build_system_parts", return_value=("persona", "dynamic")), \
                mock.patch.object(agent, "_system", return_value="system"), \
                mock.patch.object(agent.llm, "chat", side_effect=fake_chat):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                answers = list(pool.map(lambda args: run(*args), [
                    (owner, "owner-history"), (group, "group-history"),
                ]))

        self.assertEqual(answers, ["ok", "ok"])
        self.assertEqual(seen["101"], ("owner", "101", "owner-history"))
        self.assertEqual(seen["-202"], ("group", "-202", "group-history"))
        self.assertIsNone(agent._TURN_CHANNEL.get())
        self.assertIsNone(agent._TURN_HISTORY.get())


if __name__ == "__main__":
    unittest.main()
