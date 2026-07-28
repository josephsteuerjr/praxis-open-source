"""Reaper (reap_orphaned_cognitive_runs) — Fix C + F4 preservation.

A cognitive run's ``running`` lifetime outlives its model-loop-under-_ONE_MIND phase:
after the loop it stays ``running`` through an off-lock TRANSPORT-DELIVERY tail owned by
the outbox/delivery-recovery clock.  The reaper must reap only true model-loop orphans
(no transport intent), and defer transport-owned tails to delivery recovery — otherwise it
false-reaps a healthy run mid-delivery.  It must still reap genuine orphans (F4).
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

# Stub Telethon creds before any (lazy) mtproto_runner import in the single-flight
# regression, mirroring test_runner_reconnect.py — the runner builds a client at import.
os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "x")
os.environ.setdefault(
    "TELEGRAM_SESSION", str(Path(tempfile.gettempdir()) / "praxis_test_reaper"))

import agent
import media
import run_context
import run_manager


class ReaperTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory(prefix="praxis-reaper-")
        self.addCleanup(self._temp.cleanup)
        self.base = Path(self._temp.name)
        self.manager = run_manager.RunManager(self.base)
        self.spool = media.MediaSpool(self.base / "workspace" / "media")
        self._prev_mgr = agent._RUN_MANAGER
        self._prev_spool = agent._MEDIA_SPOOL
        agent._RUN_MANAGER = self.manager
        agent._MEDIA_SPOOL = self.spool
        self.addCleanup(self._restore)

    def _restore(self):
        agent._RUN_MANAGER = self._prev_mgr
        agent._MEDIA_SPOOL = self._prev_spool

    def _running(self, suffix: str, *, kind: str = "chat_turn") -> str:
        context = run_context.RunContext.create(
            run_id=f"run-reap-{suffix}", kind=kind, goal=f"reap {suffix}",
            principal_id="100", scope="owner",
            origin_chat_id="100", origin_message_ids=(7,),
            delivery_chat_id="100", model_profile="voice",
        )
        persisted = self.manager.create(context, "# immutable ctx\n")
        self.manager.transition(persisted.run_id, "running", expected="pending")
        return persisted.run_id

    def _status(self, run_id: str) -> str:
        return str(self.manager.manifest(run_id).get("status") or "")

    def _reaped_ids(self) -> list[str]:
        return [row["run_id"] for row in agent.reap_orphaned_cognitive_runs()]

    def test_reaps_true_model_loop_orphan_to_terminal_failed(self):
        # F4 + PASS28: a running cognitive run with no transport intent AND no
        # outstanding tool call is an abandoned model loop with nothing to
        # reconcile.  It must reach a TERMINAL status (``failed``): routing it to
        # ``in_doubt`` would be a permanent trap, since resolve_in_doubt needs an
        # outstanding call id and there is none, so the run could never leave.
        run_id = self._running("orphan")
        self.assertIn(run_id, self._reaped_ids())
        self.assertEqual(self._status(run_id), "failed")
        self.assertIn("failed", run_manager.TERMINAL_STATUSES)
        kinds = [row.get("kind") for row in self.manager.iter_events(run_id, reverse=True)]
        self.assertIn("cognitive_run_orphan_reaped", kinds)

    def test_reaps_orphan_with_outstanding_effect_to_in_doubt(self):
        # An orphan that died with a genuinely outstanding (uncertain) non-transport
        # side effect must go to ``in_doubt`` — NOT terminalized blind — so the owner
        # can reconcile it from evidence.  in_doubt is reachable/leavable here because
        # the outstanding call id is exactly what resolve_in_doubt needs.
        run_id = self._running("uncertain")
        self.manager.start_tool(
            run_id, "call-shell-1", "shell",
            {"cmd": "deploy.sh"}, side_effect=True, idempotency_key="",
        )
        self.assertIn(run_id, self._reaped_ids())
        self.assertEqual(self._status(run_id), "in_doubt")
        # And it is genuinely resolvable: resolve_in_doubt closes the outstanding call.
        self.manager.resolve_in_doubt(
            run_id, "call-shell-1", "not_applied",
            evidence={"probe": "no side effect observed"},
            reason="reconciled after reaper", actor="owner",
        )
        self.assertEqual(self._status(run_id), "paused")

    def test_skips_transport_owned_running_run(self):
        # Fix C: model loop done, delivery in flight -> owned by outbox recovery, not reaper.
        run_id = self._running("delivering")
        self.manager.start_tool(
            run_id, f"delivery:{run_id}", "telegram.deliver",
            {"chat_id": "100", "text_chars": 5, "media_count": 0},
            side_effect=True, idempotency_key=f"telegram-delivery:{run_id}",
        )
        self.assertNotIn(run_id, self._reaped_ids())
        self.assertEqual(self._status(run_id), "running")

    def test_orphan_with_unknown_result_routes_to_in_doubt_not_stuck_running(self):
        # PASS28 hole-B: an orphan can have EMPTY outstanding yet still be
        # non-terminalizable — e.g. an outcome receipt with no matching tool_started
        # (unknown_result).  Routing it to `failed` would raise RunConflict inside
        # _assert_terminalizable, be swallowed, and leave a permanent 'running'
        # zombie the reaper re-attempts forever.  It must go to in_doubt instead:
        # a non-terminal ATTENTION status that terminalizes out of 'running'.
        run_id = self._running("ghost-outcome")
        # An outcome event whose call_id was never started -> unknown_results, but
        # outstanding stays empty (no un-closed start).
        self.manager.append_event(run_id, "tool_completed", call_id="ghost-1", tool="web.search")
        status = self.manager.status(run_id)
        self.assertEqual(status["outstanding_call_ids"], [])
        self.assertIn("ghost-1", status["unknown_result_call_ids"])
        self.assertFalse(status["terminalizable"])
        self.assertIn(run_id, self._reaped_ids())
        self.assertEqual(self._status(run_id), "in_doubt")   # not a stuck 'running' zombie

    def test_orphan_with_conflicting_starts_routes_to_in_doubt(self):
        # PASS28 hole-B: conflicting starts (same call_id, differing intent) also block
        # terminalization while leaving outstanding empty once a closing outcome exists.
        run_id = self._running("conflict")
        # start_tool guards against a differing re-start, so forge the two conflicting
        # tool_started rows (and a closing outcome, so outstanding ends up empty) via
        # raw events — exactly the malformed WAL state recovery must survive.
        self.manager.append_event(run_id, "tool_started", call_id="call-C",
                                  tool="web.search", args={"q": "a"},
                                  side_effect=True, idempotency_key="k1")
        self.manager.append_event(run_id, "tool_started", call_id="call-C",
                                  tool="web.search", args={"q": "b"},
                                  side_effect=True, idempotency_key="k2")
        self.manager.append_event(run_id, "tool_completed", call_id="call-C", tool="web.search")
        status = self.manager.status(run_id)
        self.assertEqual(status["outstanding_call_ids"], [])
        self.assertIn("call-C", status["conflicting_start_call_ids"])
        self.assertFalse(status["terminalizable"])
        self.assertIn(run_id, self._reaped_ids())
        self.assertEqual(self._status(run_id), "in_doubt")

    def test_skips_non_cognitive_kind(self):
        # Existing invariant preserved: Forge/coding_window runs its own contour, never reaped.
        run_id = self._running("forge", kind="coding_window")
        self.assertNotIn(run_id, self._reaped_ids())
        self.assertEqual(self._status(run_id), "running")

    def test_recovery_terminalizes_stale_restart_uncertainty_only(self):
        stale_id = self._running("stale-restart")
        self.manager.append_event(
            stale_id, "tool_started", call_id="late-call", tool="shell",
            side_effect=True,
        )
        self.manager.recover()
        self.manager.append_event(
            stale_id, "tool_result", call_id="late-call", tool="shell",
        )

        generic_clean_id = self._running("generic-clean")
        self.manager.transition(
            generic_clean_id, "in_doubt", expected="running",
            reason="operator requires explicit inspection",
        )

        delivery_id = self._running("delivery")
        self.manager.append_event(
            delivery_id, "tool_started", call_id="delivery-call",
            tool="telegram.deliver", side_effect=True,
        )
        self.manager.recover()
        self.manager.append_event(
            delivery_id, "tool_result", call_id="delivery-call",
            tool="telegram.deliver",
        )

        uncertain_id = self._running("legacy-uncertain")
        self.manager.append_event(
            uncertain_id, "tool_started", call_id="effect-1", tool="side.effect",
            side_effect=True,
        )
        self.manager.append_event(
            uncertain_id, "cognitive_run_orphan_reaped",
            call_id=f"reaper:{uncertain_id}", tool="runner.single_flight",
            error="single-flight idle but run still running; executor presumed dead",
        )
        self.manager.transition(
            uncertain_id, "in_doubt", expected="running",
            reason="orphan has an unreconciled effect",
        )

        reports = agent.recover_durable_state()

        self.assertEqual(self._status(stale_id), "failed")
        self.assertIn(
            {"run_id": stale_id, "stale_restart_in_doubt": "failed"}, reports,
        )
        self.assertTrue((self.manager.path(stale_id) / "RECAP.md").is_file())
        self.assertEqual(self._status(generic_clean_id), "in_doubt")
        self.assertNotIn(
            {"run_id": generic_clean_id, "stale_restart_in_doubt": "failed"}, reports,
        )
        self.assertEqual(self._status(delivery_id), "in_doubt")
        self.assertNotIn(
            {"run_id": delivery_id, "stale_restart_in_doubt": "failed"}, reports,
        )
        self.assertEqual(self._status(uncertain_id), "in_doubt")
        self.assertNotIn(
            {"run_id": uncertain_id, "stale_restart_in_doubt": "failed"}, reports,
        )

        second = agent.recover_durable_state()
        self.assertNotIn(
            {"run_id": stale_id, "stale_restart_in_doubt": "failed"}, second,
        )

    def test_reap_wrapper_defers_to_single_flight(self):
        # PASS28 P2: the reaper CLOCK tick must never reap while a live pass holds the
        # single-flight lock — under that lock a cognitive run is legitimately
        # 'running'.  Only an IDLE single-flight proves the run is orphaned.  This
        # pins the wrapper's ``if _ONE_MIND.locked(): return`` guard, without which
        # the reaper could false-reap a healthy run racing an in-progress pass.
        import asyncio as _asyncio
        import mtproto_runner
        prev_lock = mtproto_runner._ONE_MIND
        mtproto_runner._ONE_MIND = mtproto_runner._OneMind()
        self.addCleanup(lambda: setattr(mtproto_runner, "_ONE_MIND", prev_lock))
        run_id = self._running("live-pass")

        async def scenario():
            await mtproto_runner._ONE_MIND.acquire()  # эмулируем идущий живой ход
            try:
                await mtproto_runner._reap_orphans_once()  # busy -> must skip
            finally:
                mtproto_runner._ONE_MIND.release()
            deferred = self._status(run_id)
            await mtproto_runner._reap_orphans_once()      # idle -> reaps
            return deferred

        deferred = _asyncio.run(scenario())
        self.assertEqual(deferred, "running", "живой ход держит замок -> прогон не тронут")
        self.assertEqual(self._status(run_id), "failed", "свободный single-flight -> сирота пожата")


if __name__ == "__main__":
    unittest.main()
