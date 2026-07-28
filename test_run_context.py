from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from run_context import RunContext, bind_run, current_run


def _context(run_id: str, scope: str = "owner") -> RunContext:
    return RunContext.create(
        run_id=run_id,
        kind="computer",
        goal=f"goal for {run_id}",
        principal_id=f"telegram:{run_id}",
        scope=scope,
        origin_chat_id="100",
        origin_message_ids=[1, 2],
        delivery_chat_id="100",
        model_profile="voice/default",
    )


class TestRunContext(unittest.TestCase):
    def test_roundtrip_is_frozen_and_typed(self):
        original = _context("run-roundtrip")
        restored = RunContext.from_dict(original.to_dict())
        self.assertEqual(restored, original)
        self.assertEqual(restored.origin_message_ids, (1, 2))
        with self.assertRaises(Exception):
            restored.scope = "group"  # type: ignore[misc]

    def test_nested_binding_restores_previous_context(self):
        outer = _context("run-outer")
        inner = _context("run-inner", "group")
        self.assertIsNone(current_run())
        with bind_run(outer):
            self.assertEqual(current_run(required=True), outer)
            with bind_run(inner):
                self.assertEqual(current_run(required=True), inner)
            self.assertEqual(current_run(required=True), outer)
        self.assertIsNone(current_run())
        with self.assertRaises(RuntimeError):
            current_run(required=True)

    def test_concurrent_bindings_do_not_bleed_authority_or_address(self):
        barrier = threading.Barrier(2)
        owner = _context("run-owner", "owner")
        group = RunContext.create(
            run_id="run-group", kind="conversation", goal="answer group",
            principal_id="telegram:42", scope="group", origin_chat_id="-10042",
            origin_message_ids=[91], delivery_chat_id="-10042",
        )

        def observe(ctx: RunContext) -> tuple[str, str, str | None]:
            with bind_run(ctx):
                barrier.wait(timeout=5)
                current = current_run(required=True)
                barrier.wait(timeout=5)
                return current.run_id, current.scope, current.delivery_chat_id

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(observe, owner), pool.submit(observe, group)]
            results = {future.result(timeout=10) for future in futures}
        self.assertEqual(results, {
            ("run-owner", "owner", "100"),
            ("run-group", "group", "-10042"),
        })
        self.assertIsNone(current_run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
