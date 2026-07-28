from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from run_context import RunContext
from run_manager import InvalidTransition, RunConflict, RunError, RunManager


class RunManagerBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.manager = RunManager(self.base)

    def create(self, suffix: str = "one", *, manager: RunManager | None = None) -> RunContext:
        mgr = manager or self.manager
        ctx = RunContext.create(
            run_id=f"run-{suffix}", kind="computer", goal=f"inspect {suffix}",
            principal_id="telegram:1", scope="owner", origin_chat_id="1",
            origin_message_ids=[10, 11], delivery_chat_id="1",
            model_profile="voice/test", forge_task_id="wcode-test",
        )
        return mgr.create(ctx, f"# Context {suffix}\n\nExact source snapshot.\n")


class TestRunLayoutAndEvents(RunManagerBase):
    def test_manifest_first_listing_counts_all_history_and_reduces_only_visible_wals(self):
        old_running = self.create("old-running")
        self.manager.transition(old_running.run_id, "running", expected="pending")
        old_paused = self.create("old-paused")
        self.manager.transition(old_paused.run_id, "running", expected="pending")
        self.manager.transition(old_paused.run_id, "paused", expected="running")
        old_failed = self.create("old-failed")
        self.manager.transition(old_failed.run_id, "failed", expected="pending")

        for index in range(7):
            context = self.create(f"recent-done-{index}")
            self.manager.transition(context.run_id, "running", expected="pending")
            self.manager.transition(context.run_id, "done", expected="running")

        with mock.patch.object(
                self.manager, "_tool_ledger_locked",
                wraps=self.manager._tool_ledger_locked) as reduce_ledger:
            listing = self.manager.run_listing(limit=4)

        self.assertEqual(listing["schema"], "praxis.run.listing.v1")
        self.assertEqual(listing["total"], 10)
        self.assertEqual(listing["counts"]["running"], 1)
        self.assertEqual(listing["counts"]["paused"], 1)
        self.assertEqual(listing["counts"]["failed"], 1)
        self.assertEqual(listing["counts"]["done"], 7)
        self.assertEqual(listing["active"], 2)
        self.assertEqual(listing["attention"], 2)
        self.assertEqual(listing["visible"], 4)
        self.assertTrue(listing["limited"])
        self.assertEqual(reduce_ledger.call_count, 4)
        visible_ids = {row["run_id"] for row in listing["items"]}
        self.assertTrue({
            old_running.run_id, old_paused.run_id, old_failed.run_id,
        }.issubset(visible_ids))

    def test_positive_listing_limit_is_soft_for_nonterminal_runs(self):
        contexts = [self.create(f"live-{index}") for index in range(3)]
        with mock.patch.object(
                self.manager, "_tool_ledger_locked",
                wraps=self.manager._tool_ledger_locked) as reduce_ledger:
            listing = self.manager.run_listing(limit=1)
        self.assertEqual(listing["visible"], 3)
        self.assertFalse(listing["limited"])
        self.assertEqual(reduce_ledger.call_count, 3)
        self.assertEqual(
            {row["run_id"] for row in listing["items"]},
            {context.run_id for context in contexts},
        )

    def test_dot_path_segments_are_never_valid_run_ids(self):
        for bad_id in (".", "..", " . ", " .. "):
            with self.subTest(run_id=bad_id):
                context = RunContext.create(
                    run_id=bad_id, kind="computer", goal="must reject",
                    principal_id="telegram:1", scope="owner",
                )
                with self.assertRaisesRegex(ValueError, "invalid run_id"):
                    self.manager.create(context, "# rejected\n")
                with self.assertRaisesRegex(ValueError, "invalid run_id"):
                    self.manager.path(bad_id)
        self.assertEqual(list(self.manager.root.glob("*/*/manifest.json")), [])

    def test_layout_manifest_and_context_are_durable(self):
        ctx = self.create()
        run_dir = self.manager.path(ctx.run_id)
        self.assertRegex(run_dir.parent.name, r"^\d{4}-\d{2}$")
        self.assertEqual((run_dir / "context.md").read_text(encoding="utf-8"),
                         "# Context one\n\nExact source snapshot.\n")
        for name in ("manifest.json", "context.md", "events.jsonl"):
            self.assertTrue((run_dir / name).is_file(), name)
        for name in ("results", "artifacts"):
            self.assertTrue((run_dir / name).is_dir(), name)
        manifest = self.manager.manifest(ctx.run_id)
        self.assertEqual(manifest["schema"], "praxis.run.v1")
        self.assertEqual(manifest["context"]["origin_message_ids"], [10, 11])
        self.assertEqual(manifest["context"]["context_snapshot"], ctx.context_snapshot)
        context_bytes = (run_dir / "context.md").read_bytes()
        self.assertEqual(manifest["context_snapshot_ref"], {
            "schema": "praxis.run.context-snapshot.v1",
            "path": "context.md",
            "sha256": hashlib.sha256(context_bytes).hexdigest(),
            "size": len(context_bytes),
        })
        created = self.manager.events(ctx.run_id)[0]
        self.assertEqual(created["kind"], "run_created")
        self.assertEqual(created["context_snapshot"], manifest["context_snapshot_ref"])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE((run_dir / "manifest.json").stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o700)

    def test_event_sequences_are_unique_under_concurrent_writers(self):
        ctx = self.create("events")
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(self.manager.append_event, ctx.run_id, "observation", n=n)
                       for n in range(40)]
            for future in futures:
                future.result(timeout=10)
        events = self.manager.events(ctx.run_id, strict=True)
        seqs = [row["seq"] for row in events]
        self.assertEqual(seqs, list(range(1, 42)))  # run_created + 40 observations
        self.assertEqual(len({row["id"] for row in events}), len(events))

    def test_reverse_tail_finds_sequence_after_event_larger_than_old_tail_window(self):
        ctx = self.create("large-event")
        first = self.manager.append_event(ctx.run_id, "large", payload="x" * 200_000)
        second = self.manager.append_event(ctx.run_id, "after-large")
        self.assertEqual((first["seq"], second["seq"]), (2, 3))
        newest = next(self.manager.iter_events(ctx.run_id, reverse=True))
        self.assertEqual((newest["kind"], newest["seq"]), ("after-large", 3))

    def test_corrupt_tail_is_skipped_and_next_sequence_recovers(self):
        ctx = self.create("corrupt")
        path = self.manager.path(ctx.run_id) / "events.jsonl"
        with path.open("ab") as stream:
            stream.write(b"{broken tail\n")
        row = self.manager.append_event(ctx.run_id, "after_corrupt")
        self.assertEqual(row["seq"], 2)
        self.assertEqual([event["kind"] for event in self.manager.events(ctx.run_id)],
                         ["run_created", "after_corrupt"])
        with self.assertRaises(Exception):
            self.manager.events(ctx.run_id, strict=True)

    def test_unterminated_crash_tail_is_quarantined_before_next_event(self):
        ctx = self.create("torn-tail")
        run_dir = self.manager.path(ctx.run_id)
        path = run_dir / "events.jsonl"
        fragment = b'{"schema":"praxis.run.event.v1","seq":2'
        with path.open("ab") as stream:
            stream.write(fragment)

        row = self.manager.append_event(ctx.run_id, "after_torn_tail")

        self.assertEqual(row["seq"], 2)
        self.assertEqual([event["kind"] for event in self.manager.events(ctx.run_id, strict=True)],
                         ["run_created", "after_torn_tail"])
        quarantined = list(run_dir.glob("events.corrupt-tail-*.bin"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), fragment)

    def test_valid_but_unterminated_tail_is_not_replayed_as_committed(self):
        ctx = self.create("valid-torn-tail")
        run_dir = self.manager.path(ctx.run_id)
        path = run_dir / "events.jsonl"
        fragment = json.dumps({
            "schema": "praxis.run.event.v1",
            "id": f"{ctx.run_id}:evt:00000002",
            "run_id": ctx.run_id,
            "seq": 2,
            "at": "2026-07-13T00:00:00.000Z",
            "kind": "status_changed",
            "from_status": "pending",
            "to_status": "running",
        }).encode("utf-8")
        with path.open("ab") as stream:
            stream.write(fragment)  # deliberately no JSONL commit newline

        self.assertEqual(self.manager.manifest(ctx.run_id)["status"], "pending")
        row = self.manager.append_event(ctx.run_id, "after-valid-torn-tail")

        self.assertEqual(row["seq"], 2)
        self.assertEqual([event["kind"] for event in self.manager.events(ctx.run_id, strict=True)],
                         ["run_created", "after-valid-torn-tail"])
        quarantined = list(run_dir.glob("events.corrupt-tail-*.bin"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), fragment)

    def test_manifest_replays_transition_event_left_by_interrupted_writer(self):
        ctx = self.create("wal-transition")
        run_dir = self.manager.path(ctx.run_id)
        event = {
            "schema": "praxis.run.event.v1",
            "id": f"{ctx.run_id}:evt:00000002",
            "run_id": ctx.run_id,
            "seq": 2,
            "at": "2026-07-13T00:00:00.000Z",
            "kind": "status_changed",
            "from_status": "pending",
            "to_status": "running",
            "reason": "writer stopped after durable event append",
        }
        with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")

        repaired = self.manager.manifest(ctx.run_id)
        self.assertEqual(repaired["status"], "running")
        self.assertEqual(repaired["event_seq"], 2)
        self.assertEqual(repaired["started_at"], event["at"])
        revision = repaired["revision"]
        self.assertEqual(self.manager.manifest(ctx.run_id)["revision"], revision)

    def test_orphan_result_file_is_preserved_and_sequence_advances(self):
        ctx = self.create("orphan-result")
        run_dir = self.manager.path(ctx.run_id)
        orphan = run_dir / "results" / "0001-orphan.log"
        orphan.write_bytes(b"evidence written before interrupted manifest commit")

        ref = self.manager.store_result(ctx.run_id, "new result", name="stdout")

        self.assertEqual(ref["result_id"], "result-0002")
        self.assertEqual(orphan.read_bytes(),
                         b"evidence written before interrupted manifest commit")
        manifest = self.manager.manifest(ctx.run_id)
        self.assertEqual(manifest["result_seq"], 2)
        self.assertEqual(manifest["recovery"]["orphan_result_files"], 1)

    def test_idempotent_start_and_result_receipt_are_atomic(self):
        ctx = self.create("receipt-once")
        starts = [self.manager.start_tool(
            ctx.run_id, "delivery:one", "telegram.deliver", {"text_chars": 5},
            side_effect=True,
        ) for _ in range(2)]
        self.assertEqual(starts[0]["id"], starts[1]["id"])

        def persist(_number: int) -> dict:
            return self.manager.store_result(
                ctx.run_id, '{"text":"hello"}', call_id="delivery:one",
                name="telegram-text", media_type="application/json", idempotent=True,
            )

        with ThreadPoolExecutor(max_workers=6) as pool:
            refs = [future.result(timeout=10)
                    for future in [pool.submit(persist, number) for number in range(12)]]
        self.assertEqual({ref["result_id"] for ref in refs}, {"result-0001"})
        receipts = [row for row in self.manager.events(ctx.run_id)
                    if row.get("kind") == "tool_result"
                    and row.get("name") == "telegram-text"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(len(list((self.manager.path(ctx.run_id) / "results").iterdir())), 1)

        with self.assertRaises(RunConflict):
            self.manager.store_result(
                ctx.run_id, '{"text":"other"}', call_id="delivery:one",
                name="telegram-text", media_type="application/json", idempotent=True,
            )

    def test_advisory_lock_times_out_and_recovers_stale_owner_metadata(self):
        ctx = self.create("lock-advisory")
        run_dir = self.manager.path(ctx.run_id)
        lock_path = run_dir / ".run.lock"
        lock_path.write_text("2147483647:legacy-stale", encoding="ascii")
        peer = RunManager(self.base)
        with self.manager._locked(run_dir, timeout=1):
            self.assertTrue(lock_path.exists())
            with self.assertRaises(TimeoutError):
                with peer._locked(run_dir, timeout=0.1):
                    self.fail("held advisory lock must not be entered")
        self.assertTrue(lock_path.exists())
        self.assertIn(":advisory-v1:", lock_path.read_text(encoding="ascii"))

    def test_stale_lock_recovery_never_unlinks_or_overlaps_two_managers(self):
        ctx = self.create("lock-stale-race")
        run_dir = self.manager.path(ctx.run_id)
        lock_path = run_dir / ".run.lock"
        lock_path.write_text("2147483647:legacy-stale", encoding="ascii")
        peer = RunManager(self.base)
        start = threading.Barrier(3)
        state_guard = threading.Lock()
        active = 0
        maximum = 0

        def contend(manager: RunManager) -> None:
            nonlocal active, maximum
            start.wait(timeout=2)
            with manager._locked(run_dir, timeout=2):
                with state_guard:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.05)
                with state_guard:
                    active -= 1

        # A held lock path is never removed.  This makes the old stale-token
        # TOCTOU failure deterministic while also checking the actual overlap
        # invariant across separate RunManager instances.
        with mock.patch.object(
                Path, "unlink",
                side_effect=AssertionError("a held run lock path must never be unlinked")):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(contend, manager)
                           for manager in (self.manager, peer)]
                start.wait(timeout=2)
                for future in futures:
                    future.result(timeout=5)

        self.assertEqual(maximum, 1)
        self.assertEqual(active, 0)
        self.assertTrue(lock_path.exists())


class TestRunTransitionsAndRecovery(RunManagerBase):
    def test_authorize_resume_keeps_pause_and_is_revision_bound(self):
        ctx = self.create("owner-resume")
        self.manager.transition(ctx.run_id, "running", expected="pending")
        paused = self.manager.request_pause(
            ctx.run_id, actor="telegram:1", reason="inspect first",
        )
        with self.assertRaises(RunConflict):
            self.manager.authorize_resume(
                ctx.run_id, actor="telegram:1",
                expected_revision=int(paused["revision"]) - 1,
            )
        released = self.manager.authorize_resume(
            ctx.run_id, actor="telegram:1", reason="continue",
            expected_revision=paused["revision"],
        )
        self.assertEqual(released["status"], "paused")
        self.assertEqual(released["control"], {})
        self.assertEqual(self.manager.events(ctx.run_id)[-1]["kind"],
                         "resume_authorized")
        import run_resume
        self.assertTrue(run_resume._is_recovery_pause(
            self.manager.events(ctx.run_id), "paused", {},
        ))

    def test_terminal_transition_is_idempotent_and_immutable(self):
        ctx = self.create("terminal")
        self.manager.transition(ctx.run_id, "running", expected="pending")
        terminal = self.manager.transition(ctx.run_id, "done", reason="verified")
        before_events = len(self.manager.events(ctx.run_id))
        repeated = self.manager.transition(ctx.run_id, "done", reason="ignored duplicate")
        self.assertEqual(repeated["revision"], terminal["revision"])
        self.assertEqual(len(self.manager.events(ctx.run_id)), before_events)
        self.assertEqual(repeated["recap"]["status"], "pending")
        with self.assertRaises(InvalidTransition):
            self.manager.transition(ctx.run_id, "failed")

    def test_expected_status_is_compare_and_swap(self):
        ctx = self.create("cas")
        with self.assertRaises(RunConflict):
            self.manager.transition(ctx.run_id, "running", expected="paused")
        self.assertEqual(self.manager.manifest(ctx.run_id)["status"], "pending")

    def test_transport_reconcile_claim_loses_owner_control_race(self):
        ctx = self.create("transport-cas")
        self.manager.transition(ctx.run_id, "running", expected="pending")
        self.manager.transition(
            ctx.run_id, "paused", expected="running",
            reason="process restarted; no uncertain side effect observed",
        )
        planned = self.manager.manifest(ctx.run_id)
        self.manager.request_pause(
            ctx.run_id, actor="telegram:1", reason="keep this paused",
        )

        with self.assertRaisesRegex(RunConflict, "stale transport plan"):
            self.manager.claim_transport_reconcile(
                ctx.run_id,
                expected_revision=planned["revision"],
                expected_event_seq=planned["event_seq"],
                actor="transport:test",
            )

        current = self.manager.manifest(ctx.run_id)
        with self.assertRaisesRegex(RunConflict, "control is pending"):
            self.manager.claim_transport_reconcile(
                ctx.run_id,
                expected_revision=current["revision"],
                expected_event_seq=current["event_seq"],
                actor="transport:test",
            )
        self.assertFalse(any(
            row.get("kind") == "transport_reconcile_claimed"
            for row in self.manager.events(ctx.run_id)
        ))

    def test_recovery_pauses_clean_work_and_marks_uncertain_effect_in_doubt(self):
        clean = self.create("clean")
        uncertain = self.create("uncertain")
        completed = self.create("completed")
        retryable = self.create("retryable")
        for ctx in (clean, uncertain, completed, retryable):
            self.manager.transition(ctx.run_id, "running")
        self.manager.start_tool(uncertain.run_id, "call-unsafe", "send_message",
                                {"to": "2"}, side_effect=True)
        self.manager.start_tool(retryable.run_id, "call-exact", "body.process.start",
                                {"command": "build"}, side_effect=True,
                                idempotency_key="request-123")
        self.manager.start_tool(completed.run_id, "call-read", "fs.read",
                                {"path": "x"}, side_effect=False)
        self.manager.store_result(completed.run_id, "complete", call_id="call-read", name="read")

        reports = self.manager.recover()
        self.assertTrue(reports)
        self.assertEqual(self.manager.manifest(clean.run_id)["status"], "paused")
        self.assertEqual(self.manager.manifest(completed.run_id)["status"], "paused")
        self.assertEqual(self.manager.manifest(retryable.run_id)["status"], "paused")
        in_doubt = self.manager.manifest(uncertain.run_id)
        self.assertEqual(in_doubt["status"], "in_doubt")
        last = self.manager.events(uncertain.run_id)[-1]
        self.assertEqual(last["kind"], "status_changed")
        self.assertEqual(last["details"]["uncertain_call_ids"], ["call-unsafe"])


class TestFullResultsAndArtifacts(RunManagerBase):
    def test_full_result_survives_inline_clipping_and_cursor_reads(self):
        ctx = self.create("result")
        text = "A" * 9000 + "\nUNIQUE_MIDDLE_EVIDENCE\n" + "Я" * 9000 + "\nlast line\n"
        self.manager.start_tool(ctx.run_id, "call-1", "shell", {"command": "huge"},
                                side_effect=False)
        ref = self.manager.store_result(ctx.run_id, text, call_id="call-1", name="stdout",
                                        inline_chars=400)
        result_path = self.manager.path(ctx.run_id) / ref["path"]
        self.assertEqual(result_path.read_text(encoding="utf-8"), text)
        raw = text.encode("utf-8")
        self.assertEqual(ref["size"], len(raw))
        self.assertEqual(ref["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertTrue(ref["inline"]["truncated"])
        self.assertNotIn("UNIQUE_MIDDLE_EVIDENCE",
                         ref["inline"]["head"] + ref["inline"]["tail"])
        event_raw = json.dumps(self.manager.events(ctx.run_id)[-1], ensure_ascii=False)
        self.assertNotIn("UNIQUE_MIDDLE_EVIDENCE", event_raw)

        gathered = bytearray()
        offset = 0
        while True:
            page = self.manager.read_result(ctx.run_id, ref["result_id"],
                                            byte_offset=offset, byte_limit=257)
            gathered.extend(base64.b64decode(page["data_base64"]))
            offset = page["next_offset"]
            if page["eof"]:
                break
        self.assertEqual(bytes(gathered), raw)
        lines = self.manager.read_result(ctx.run_id, ref["path"], line_start=2, line_count=1)
        self.assertEqual(lines["text"], "UNIQUE_MIDDLE_EVIDENCE\n")
        self.assertEqual(lines["sha256"], ref["sha256"])

    def test_result_cursor_can_defer_full_hash_and_enforces_deadline(self):
        ctx = self.create("result-deferred-hash")
        ref = self.manager.store_result(
            ctx.run_id, b"bounded assembler payload", name="payload",
        )

        with mock.patch(
                "run_manager._file_sha256",
                side_effect=AssertionError("unexpected full-file hash")):
            page = self.manager.read_result(
                ctx.run_id, ref["result_id"], verify_sha256=False,
            )

        self.assertNotIn("sha256", page)
        self.assertEqual(
            base64.b64decode(page["data_base64"]),
            b"bounded assembler payload",
        )
        with self.assertRaisesRegex(RunError, "planning-time budget exceeded"):
            self.manager.read_result(
                ctx.run_id, ref["result_id"], verify_sha256=False,
                deadline_monotonic=0.0,
            )

    def test_binary_artifact_is_hash_addressed(self):
        ctx = self.create("artifact")
        payload = bytes(range(256)) * 4
        ref = self.manager.store_artifact(ctx.run_id, payload, name="capture.bin")
        path = self.manager.path(ctx.run_id) / ref["path"]
        self.assertEqual(path.read_bytes(), payload)
        self.assertEqual(ref["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(self.manager.events(ctx.run_id)[-1]["kind"], "artifact_created")

    def test_large_file_artifact_is_streamed_and_verified_without_read_bytes(self):
        ctx = self.create("large-artifact")
        source = self.base / "large-source.bin"
        block = bytes(range(256)) * 4096
        with source.open("wb") as stream:
            for _ in range(9):
                stream.write(block)
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)

        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("buffered read")):
            ref = self.manager.store_artifact(
                ctx.run_id, source, name="large.bin",
                expected_sha256=digest.hexdigest(), expected_size=source.stat().st_size,
            )

        stored = self.manager.path(ctx.run_id) / ref["path"]
        self.assertEqual(ref["size"], 9 * len(block))
        self.assertEqual(ref["sha256"], digest.hexdigest())
        self.assertEqual(stored.stat().st_size, ref["size"])
        self.assertEqual(hashlib.sha256(stored.read_bytes()).hexdigest(), digest.hexdigest())

    def test_idempotent_file_artifact_has_one_receipt_under_concurrent_writers(self):
        ctx = self.create("artifact-race")
        source = self.base / "export.bin"
        source.write_bytes(b"same body CAS" * 4096)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        second_manager = RunManager(self.base)

        def persist(manager: RunManager) -> dict:
            return manager.store_artifact(
                ctx.run_id, source, name="export.bin", media_type="application/octet-stream",
                idempotency_key="browser-export:stable-operation",
                expected_sha256=digest, expected_size=source.stat().st_size,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            refs = list(pool.map(persist, (self.manager, second_manager)))

        self.assertEqual(refs[0], refs[1])
        created = [
            row for row in self.manager.events(ctx.run_id, strict=True)
            if row.get("kind") == "artifact_created"
        ]
        self.assertEqual(len(created), 1)
        numbered = list((self.manager.path(ctx.run_id) / "artifacts").glob("[0-9]*-*"))
        self.assertEqual(len(numbered), 1)


class TestRecapPromotion(RunManagerBase):
    def test_recap_and_promotion_are_idempotent(self):
        calls: list[str] = []

        def hook(ctx: RunContext, recap_path: Path, manifest: dict):
            calls.append(ctx.run_id)
            self.assertIn("verified evidence", recap_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "done")
            return {"id": "life-event-1", "kind": "run_episode"}

        manager = RunManager(self.base, promotion_hook=hook)
        ctx = self.create("recap", manager=manager)
        manager.transition(ctx.run_id, "running")
        manager.transition(ctx.run_id, "done", reason="all green")
        first = manager.write_recap(ctx.run_id, "# Recap\n\nverified evidence")
        second = manager.write_recap(ctx.run_id, "# Recap\n\nverified evidence")
        self.assertEqual(calls, [ctx.run_id])
        self.assertEqual(first["recap"]["promotion"]["status"], "done")
        self.assertEqual(second["recap"]["promotion"]["event_id"], "life-event-1")
        kinds = [row["kind"] for row in manager.events(ctx.run_id)]
        self.assertEqual(kinds.count("recap_written"), 1)
        self.assertEqual(kinds.count("run_promoted"), 1)
        with self.assertRaises(RunConflict):
            manager.write_recap(ctx.run_id, "different recap")

    def test_recap_requires_terminal_and_hook_is_optional(self):
        ctx = self.create("pending-recap")
        with self.assertRaises(InvalidTransition):
            self.manager.write_recap(ctx.run_id, "not terminal")
        self.manager.transition(ctx.run_id, "cancelled", reason="owner cancelled")
        manifest = self.manager.write_recap(ctx.run_id, "# Cancelled\n\nNothing delivered.")
        self.assertEqual(manifest["recap"]["promotion"]["status"], "not_configured")


if __name__ == "__main__":
    unittest.main(verbosity=2)
