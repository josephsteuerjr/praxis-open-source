from __future__ import annotations

import json
import hashlib
import hmac
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

import praxis_app
import run_manager
from run_context import RunContext


class PraxisAppCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "memory" / "maps").mkdir(parents=True)
        for name in praxis_app.MAP_NAMES:
            (self.root / "memory" / "maps" / f"{name}.md").write_text(
                f"# {name}\n", encoding="utf-8",
            )
        self.woke = threading.Event()
        self.service = praxis_app.PraxisAppService(
            self.root,
            owner_id=123456789,
            body_probe=lambda **_kwargs: {
                "ok": True,
                "probe_execution": "interactive",
                "identity": {"kind": "user", "integrity": "high", "elevated": True},
                "manifest": {"execution_contexts": [
                    {"kind": "interactive", "available": True, "session_id": 1},
                    {"kind": "system", "available": True, "session_id": 0},
                ]},
            },
            resume_run=lambda _run_id: self.woke.set() or {"ok": True},
        )
        self.owner = self.service.viewer(123456789)
        self.assertIsNotNone(self.owner)

    def create_run(self, status="running"):
        context = RunContext.create(
            kind="telegram_turn", goal="verify the app", principal_id="telegram:123456789",
            scope="owner", origin_chat_id="123456789", origin_message_ids=(7,),
        )
        context = self.service.runs.create(context, "# Immutable context\n")
        if status == "running":
            self.service.runs.transition(context.run_id, "running", expected="pending")
        return context

    def test_viewer_is_owner_or_exact_scoped_trusted_principal(self):
        self.assertTrue(self.owner.owner)
        self.assertTrue(self.owner.may("computer.apps"))
        with mock.patch("praxis_app.computer_access.allowed",
                        side_effect=lambda actor, scope: scope == "computer.read"):
            trusted = self.service.viewer(22)
        self.assertEqual(trusted.role, "trusted")
        self.assertEqual(trusted.scopes, ("computer.read",))
        with mock.patch("praxis_app.computer_access.allowed", return_value=False):
            self.assertIsNone(self.service.viewer(23))

    def test_trusted_computer_command_honours_exact_scope_and_denies_system(self):
        trusted = praxis_app.Viewer("22", "trusted", ("computer.read",))
        with self.assertRaises(PermissionError):
            self.service.command(trusted, {
                "domain": "computer", "capability": "fs.read", "args": {"path": "x"},
            })
        with self.assertRaises(PermissionError):
            self.service.command(trusted, {
                "domain": "computer", "capability": "body.status",
                "execution": "system",
            })

    def test_snapshot_is_one_revisioned_projection_without_raw_journal(self):
        self.create_run()
        inventory = self.root / "memory" / "computer" / "inventory" / "windows-pc"
        inventory.mkdir(parents=True)
        (inventory / "CURRENT.json").write_text(json.dumps({
            "observed_at": "2026-07-14T00:00:00Z",
            "captured_at": "2026-07-14T00:00:00Z",
            "payload": {
                "hostname": "workstation",
                "os": {"caption": "Windows", "build": "26100"},
                "machine": {"model": "PC", "memory_bytes": 32 * 1024**3},
                "volumes": [], "tools": [{"name": "git"}],
                "apps": [{"name": "Editor"}], "project_roots": ["C:\\Code"],
                "known_roots": ["C:\\Users\\Owner"],
            },
        }), encoding="utf-8")
        with mock.patch("praxis_app.body_client.available", return_value=True), \
             mock.patch("praxis_app.body_client.device_id", return_value="windows-pc"), \
             mock.patch.object(self.service, "_computer_evidence", return_value=[]), \
             mock.patch.object(self.service, "_telegram", return_value={
                 "rooms": [], "followups": [], "membership": [], "pending_followups": 0,
             }), \
             mock.patch.object(self.service, "_trust", return_value={
                 "owner_only": True, "grants": [], "available_scopes": [],
             }), \
             mock.patch.object(self.service, "_system", return_value={"head": "abc"}):
            snapshot = self.service.snapshot(self.owner)
        self.assertEqual(snapshot["schema"], praxis_app.SCHEMA)
        self.assertRegex(snapshot["revision"], r"^[0-9a-f]{24}$")
        self.assertEqual(snapshot["computer"]["inventory"]["hostname"], "workstation")
        self.assertEqual(snapshot["runs"]["counts"]["running"], 1)
        self.assertFalse(snapshot["memory"]["raw_journal_is_normative"])

    def test_run_snapshot_keeps_all_history_counts_instead_of_recounting_cards(self):
        projection = {
            "schema": "praxis.run.listing.v1",
            "items": [{"run_id": "old-active", "status": "running"}],
            "counts": {"running": 1, "done": 913},
            "total": 914,
            "visible": 1,
            "limited": True,
            "active": 1,
            "attention": 0,
        }
        with mock.patch.object(
                self.service.runs, "run_listing", return_value=projection) as listing:
            snapshot = self.service._runs_snapshot()
        listing.assert_called_once_with(limit=40)
        self.assertEqual(snapshot, projection)
        self.assertEqual(snapshot["counts"]["done"], 913)

    def test_run_control_is_revision_bound_and_resume_has_a_real_executor_wake(self):
        context = self.create_run()
        initial = self.service.runs.status(context.run_id)
        paused = self.service.control_run(
            self.owner, context.run_id, "pause", reason="look",
            expected_revision=initial["revision"],
        )["run"]
        self.assertEqual(paused["status"], "paused")
        with self.assertRaisesRegex(Exception, "stale run revision"):
            self.service.control_run(
                self.owner, context.run_id, "resume",
                expected_revision=initial["revision"],
            )
        released = self.service.control_run(
            self.owner, context.run_id, "resume", reason="continue",
            expected_revision=paused["revision"],
        )["run"]
        self.assertEqual(released["status"], "paused")
        self.assertEqual(released["requested_control"], {})
        self.assertTrue(self.woke.wait(2), "authorized resume must wake the executor")

    def test_resume_without_resume_run_authorizes_only_no_in_process_brain(self):
        # Fix B (F1-B/F2): with resume_run=None (the production shape — mailroom_bot
        # constructs the service without a callback), the mini-app must NOT execute the
        # brain in-process; it only authorizes.  The runner clock resumes under _ONE_MIND.
        svc = praxis_app.PraxisAppService(self.root, owner_id=123456789)
        self.assertIsNone(svc._resume_run)
        context = svc.runs.create(
            RunContext.create(
                kind="telegram_turn", goal="verify", principal_id="telegram:123456789",
                scope="owner", origin_chat_id="123456789", origin_message_ids=(7,),
            ),
            "# ctx\n",
        )
        svc.runs.transition(context.run_id, "running", expected="pending")
        owner = svc.viewer(123456789)
        initial = svc.runs.status(context.run_id)
        paused = svc.control_run(
            owner, context.run_id, "pause", reason="look",
            expected_revision=initial["revision"],
        )["run"]
        with mock.patch("agent.resume_durable_run") as brain, \
                mock.patch.object(praxis_app.threading, "Thread") as thread_cls:
            released = svc.control_run(
                owner, context.run_id, "resume", reason="continue",
                expected_revision=paused["revision"],
            )["run"]
        self.assertEqual(released["status"], "paused")
        self.assertEqual(released["requested_control"], {})
        brain.assert_not_called()       # no brain executed in the mini-app process
        thread_cls.assert_not_called()  # no wake thread spawned when resume_run is None

    def test_artifact_download_is_bound_to_durable_hash(self):
        context = self.create_run()
        ref = self.service.runs.store_artifact(
            context.run_id, b"hello", name="proof.txt", media_type="text/plain",
        )
        path, card = self.service.artifact_path(
            self.owner, context.run_id, ref["artifact_id"],
        )
        self.assertEqual(path.read_bytes(), b"hello")
        self.assertEqual(card["sha256"], ref["sha256"])
        path.write_bytes(b"tampered")
        with self.assertRaisesRegex(Exception, "differs from its durable receipt"):
            self.service.artifact_path(self.owner, context.run_id, ref["artifact_id"])

    def test_artifact_download_ticket_is_opaque_bound_and_one_use(self):
        context = self.create_run()
        ref = self.service.runs.store_artifact(context.run_id, b"hello", name="proof.txt")
        detail = self.service.run_detail(self.owner, context.run_id)
        url = detail["artifacts"][0]["download_url"]
        ticket = url.split("ticket=", 1)[1]
        self.assertNotIn(context.run_id, ticket)
        self.assertIsNone(praxis_app.ARTIFACT_TICKETS.consume(
            ticket, context.run_id, "artifact-9999",
        ))
        detail = self.service.run_detail(self.owner, context.run_id)
        ticket = detail["artifacts"][0]["download_url"].split("ticket=", 1)[1]
        grant = praxis_app.ARTIFACT_TICKETS.consume(
            ticket, context.run_id, ref["artifact_id"],
        )
        self.assertEqual(grant.viewer, self.owner)
        self.assertEqual((grant.run_id, grant.artifact_id), (
            context.run_id, ref["artifact_id"],
        ))
        self.assertIsNone(praxis_app.ARTIFACT_TICKETS.consume(
            ticket, context.run_id, ref["artifact_id"],
        ))

    def test_event_tickets_are_opaque_short_lived_and_one_use(self):
        tickets = praxis_app.EventTickets(ttl_seconds=5)
        issued = tickets.issue(self.owner)
        self.assertNotIn(self.owner.actor_id, issued["ticket"])
        self.assertEqual(tickets.consume(issued["ticket"]), self.owner)
        self.assertIsNone(tickets.consume(issued["ticket"]))

    def test_event_tickets_are_bounded_per_principal(self):
        tickets = praxis_app.EventTickets(ttl_seconds=30, per_principal=2)
        first = tickets.issue(self.owner)["ticket"]
        second = tickets.issue(self.owner)["ticket"]
        third = tickets.issue(self.owner)["ticket"]
        self.assertIsNone(tickets.consume(first))
        self.assertEqual(tickets.consume(second), self.owner)
        self.assertEqual(tickets.consume(third), self.owner)

    def test_device_revocation_invalidates_already_issued_tickets(self):
        enrollment = self.service.issue_device_enrollment(
            self.owner, label="revocable PWA",
            scopes=["praxis.events", "praxis.snapshot"],
        )
        credential = self.service.redeem_device(
            enrollment["enrollment_token"], platform="Windows",
        )
        device = self.service.device_viewer(credential["device_token"])
        self.assertIsNotNone(device)
        context = self.create_run()
        ref = self.service.runs.store_artifact(
            context.run_id, b"revocable", name="revocable.txt",
        )
        events = praxis_app.EventTickets()
        event_ticket = events.issue(
            device, validator=self.service.revalidate_event_ticket,
        )["ticket"]
        artifacts = praxis_app.ArtifactTickets()
        head_ticket = artifacts.issue(
            device, context.run_id, ref["artifact_id"],
            validator=self.service.revalidate_artifact_ticket,
        )
        get_ticket = artifacts.issue(
            device, context.run_id, ref["artifact_id"],
            validator=self.service.revalidate_artifact_ticket,
        )

        self.assertTrue(self.service.revoke_device(
            self.owner, device.actor_id, reason="lost device",
        )["ok"])
        self.assertIsNone(events.consume(event_ticket))
        self.assertIsNone(artifacts.authorize(
            head_ticket, context.run_id, ref["artifact_id"], consume=False,
        ))
        self.assertIsNone(artifacts.consume(
            get_ticket, context.run_id, ref["artifact_id"],
        ))

    def test_artifact_ticket_is_exact_capability_not_snapshot_scope(self):
        context = self.create_run()
        first = self.service.runs.store_artifact(
            context.run_id, b"first", name="first.txt",
        )
        second = self.service.runs.store_artifact(
            context.run_id, b"second", name="second.txt",
        )
        files_only = praxis_app.Viewer("22", "trusted", ("computer.files",))
        with self.assertRaises(PermissionError):
            self.service.run_detail(files_only, context.run_id)
        tickets = praxis_app.ArtifactTickets()
        token = tickets.issue(
            files_only, context.run_id, first["artifact_id"],
            validator=self.service.revalidate_artifact_ticket,
        )
        self.assertIsNone(tickets.authorize(
            token, context.run_id, second["artifact_id"], consume=False,
        ))
        grant = tickets.consume(token, context.run_id, first["artifact_id"])
        self.assertIsNotNone(grant)
        path, _card = self.service.artifact_path_from_ticket(grant)
        self.assertEqual(path.read_bytes(), b"first")
        self.assertFalse(grant.viewer.may("praxis.snapshot"))
        self.assertIsNone(tickets.consume(
            token, context.run_id, first["artifact_id"],
        ))

    def test_command_queues_the_existing_server_execution_loop(self):
        import tasks
        task_path = self.root / "memory" / "tasks.json"
        with mock.patch.object(tasks, "TASKS", task_path):
            result = self.service.command(self.owner, {
                "domain": "praxis", "action": "run", "kind": "coding",
                "goal": "build and test the adapter",
                "idempotency_key": "queue-existing-loop-0001",
            })
        self.assertTrue(result["ok"])
        self.assertEqual(result["accepted"]["status"], "queued")
        queued = json.loads(task_path.read_text(encoding="utf-8"))
        self.assertTrue(queued[0]["goal"].startswith("код:"))

    def test_server_mutation_key_replays_after_restart_and_binds_actor_and_args(self):
        import tasks

        task_path = self.root / "memory" / "tasks.json"
        command = {
            "domain": "praxis", "action": "run", "kind": "window",
            "goal": "durably queue this once",
            "idempotency_key": "server-run-replay-0001",
        }
        with self.assertRaisesRegex(ValueError, "idempotency_key is required"):
            self.service.command(self.owner, {
                "domain": "praxis", "action": "run", "kind": "window",
                "goal": "missing key",
            })

        restarted = praxis_app.PraxisAppService(
            self.root, owner_id=123456789,
            body_probe=lambda **_kwargs: {"ok": False},
        )
        with mock.patch.object(tasks, "TASKS", task_path):
            first = self.service.command(self.owner, command)
            replay = restarted.command(self.owner, command)
            with self.assertRaisesRegex(run_manager.RunConflict, "different server command"):
                restarted.command(self.owner, {
                    **command, "goal": "same key, different intent",
                })
            other_actor = praxis_app.Viewer(
                "22", "trusted", ("praxis.work",),
            )
            independent = restarted.command(other_actor, {
                **command, "goal": "another principal may reuse a client key",
            })

        self.assertEqual(first, replay)
        self.assertNotEqual(first["praxis_audit"]["run_id"],
                            independent["praxis_audit"]["run_id"])
        queued = json.loads(task_path.read_text(encoding="utf-8"))
        self.assertEqual(len(queued), 2)

    def test_server_operation_claim_is_single_executor_across_service_instances(self):
        import concurrent.futures

        peer = praxis_app.PraxisAppService(
            self.root, owner_id=123456789,
            body_probe=lambda **_kwargs: {"ok": False},
        )
        entered = threading.Event()
        release = threading.Event()
        calls = 0
        calls_lock = threading.Lock()

        def mutate() -> dict:
            nonlocal calls
            with calls_lock:
                calls += 1
            entered.set()
            self.assertTrue(release.wait(3))
            return {"ok": True, "value": "one durable effect"}

        body = {"idempotency_key": "cross-service-claim-0001"}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(
                self.service._server_operation, self.owner, body,
                "test.concurrent", {"value": 1}, mutate,
            )
            self.assertTrue(entered.wait(2))
            second_future = pool.submit(
                peer._server_operation, self.owner, body,
                "test.concurrent", {"value": 1}, mutate,
            )
            time.sleep(0.1)
            release.set()
            first = first_future.result(timeout=5)
            second = second_future.result(timeout=5)

        self.assertEqual(calls, 1)
        self.assertEqual(first, second)

    def test_server_operation_rechecks_receipt_after_terminal_status_race(self):
        context = self.create_run()
        receipt = {"ok": True, "value": "committed between probes"}
        with (
            mock.patch.object(
                self.service, "_server_receipt", side_effect=[None, receipt],
            ) as read_receipt,
            mock.patch.object(
                self.service.runs, "status", return_value={"status": "done"},
            ),
        ):
            replay = self.service._unreceipted_server_operation(
                self.owner, context, "call-race", "test.race",
                "operation-race", {"attempt_id": "attempt-race"},
            )

        self.assertEqual(read_receipt.call_count, 2)
        self.assertTrue(replay["ok"])
        self.assertEqual(replay["value"], "committed between probes")

    def test_live_server_executor_renews_lease_and_reports_in_progress(self):
        import concurrent.futures

        peer = praxis_app.PraxisAppService(
            self.root, owner_id=123456789,
            body_probe=lambda **_kwargs: {"ok": False},
        )
        entered = threading.Event()
        release = threading.Event()

        def mutate() -> dict:
            entered.set()
            self.assertTrue(release.wait(3))
            return {"ok": True, "value": "heartbeat-complete"}

        body = {"idempotency_key": "live-server-heartbeat-0001"}
        with (
            mock.patch.object(praxis_app, "SERVER_OPERATION_LEASE_SECONDS", 0.25),
            mock.patch.object(praxis_app, "SERVER_OPERATION_HEARTBEAT_SECONDS", 0.04),
            mock.patch.object(praxis_app, "SERVER_OPERATION_WAIT_SECONDS", 0.08),
            concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool,
        ):
            first_future = pool.submit(
                self.service._server_operation, self.owner, body,
                "test.heartbeat", {"value": 1}, mutate,
            )
            self.assertTrue(entered.wait(2))
            # The initial lease is gone; only durable heartbeats can keep the
            # original executor authoritative now.
            time.sleep(0.35)
            second = peer._server_operation(
                self.owner, body, "test.heartbeat", {"value": 1},
                mock.Mock(return_value={"ok": True, "value": "duplicate"}),
            )
            self.assertEqual(second["code"], "operation_in_progress")
            release.set()
            first = first_future.result(timeout=5)

        self.assertTrue(first["ok"])
        run_id = first["praxis_audit"]["run_id"]
        heartbeats = [
            event for event in self.service.runs.iter_events(run_id, strict=True)
            if event.get("kind") == "server_operation_heartbeat"
        ]
        self.assertGreaterEqual(len(heartbeats), 2)

    def test_dead_unreceipted_server_claim_becomes_in_doubt_without_reexecution(self):
        body = {"idempotency_key": "dead-server-claim-0001"}
        client_key = self.service._client_key(body, required=True)
        digest = self.service._server_operation_digest(self.owner, client_key)
        context, call_id, operation_id = self.service._server_operation_run(
            self.owner, digest=digest, name="test.crash", args={"value": 1},
            client_key=client_key,
        )
        identity = self.service._refresh_server_executor_identity()
        identity.update({
            "executor_pid": 2_147_483_647,
            "executor_process_start": "dead-process-generation",
        })
        self.service.runs.append_event_once(
            context.run_id, "server_operation_claimed", f"{call_id}:execute",
            call_id=call_id, operation="test.crash", operation_id=operation_id,
            attempt_id="lost-at-crash",
            **identity,
            lease_expires_at=praxis_app._server_utc_after(30),
        )
        callback = mock.Mock(return_value={"ok": True})
        with mock.patch("run_manager._owner_alive", return_value=False):
            first = self.service._server_operation(
                self.owner, body, "test.crash", {"value": 1}, callback,
            )
            replay = self.service._server_operation(
                self.owner, body, "test.crash", {"value": 1}, callback,
            )

        self.assertEqual(first, replay)
        self.assertEqual(first["code"], "operation_in_doubt")
        self.assertEqual(self.service.runs.status(context.run_id)["status"], "in_doubt")
        callback.assert_not_called()

    def test_reused_pid_cannot_inherit_an_unreceipted_server_claim(self):
        body = {"idempotency_key": "reused-pid-claim-0001"}
        client_key = self.service._client_key(body, required=True)
        digest = self.service._server_operation_digest(self.owner, client_key)
        context, call_id, operation_id = self.service._server_operation_run(
            self.owner, digest=digest, name="test.pid-reuse", args={"value": 1},
            client_key=client_key,
        )
        identity = self.service._refresh_server_executor_identity()
        identity["executor_process_start"] = "original-process-generation"
        self.service.runs.append_event_once(
            context.run_id, "server_operation_claimed", f"{call_id}:execute",
            call_id=call_id, operation="test.pid-reuse",
            operation_id=operation_id, attempt_id="pid-was-reused",
            **identity, lease_expires_at=praxis_app._server_utc_after(30),
        )
        callback = mock.Mock(return_value={"ok": True})
        with (
            mock.patch.object(praxis_app, "SERVER_OPERATION_WAIT_SECONDS", 0.0),
            mock.patch("run_manager._owner_alive", return_value=True),
            mock.patch("praxis_app._server_process_start_identity",
                       return_value="replacement-process-generation"),
        ):
            result = self.service._server_operation(
                self.owner, body, "test.pid-reuse", {"value": 1}, callback,
            )

        self.assertEqual(result["code"], "operation_in_doubt")
        callback.assert_not_called()

    def test_stale_server_lease_is_in_doubt_even_if_process_is_still_alive(self):
        body = {"idempotency_key": "stale-live-claim-0001"}
        client_key = self.service._client_key(body, required=True)
        digest = self.service._server_operation_digest(self.owner, client_key)
        context, call_id, operation_id = self.service._server_operation_run(
            self.owner, digest=digest, name="test.stale", args={"value": 1},
            client_key=client_key,
        )
        self.service.runs.append_event_once(
            context.run_id, "server_operation_claimed", f"{call_id}:execute",
            call_id=call_id, operation="test.stale", operation_id=operation_id,
            attempt_id="heartbeat-stopped",
            **self.service._refresh_server_executor_identity(),
            lease_expires_at=praxis_app._server_utc_after(0.1),
        )
        time.sleep(0.15)
        callback = mock.Mock(return_value={"ok": True})
        with mock.patch.object(praxis_app, "SERVER_OPERATION_WAIT_SECONDS", 0.0):
            result = self.service._server_operation(
                self.owner, body, "test.stale", {"value": 1}, callback,
            )

        self.assertEqual(result["code"], "operation_in_doubt")
        callback.assert_not_called()

    def test_server_receipt_hash_tamper_fails_closed_without_callback(self):
        body = {"idempotency_key": "server-receipt-tamper-0001"}
        first = self.service._server_operation(
            self.owner, body, "test.tamper", {"value": 1},
            lambda: {"ok": True, "value": "original"},
        )
        run_id = first["praxis_audit"]["run_id"]
        event = next(
            row for row in self.service.runs.iter_events(run_id, reverse=True, strict=True)
            if row.get("kind") == "tool_result"
        )
        receipt_path = self.service.runs.path(run_id) / event["result"]["path"]
        receipt_path.write_bytes(b"{}")
        callback = mock.Mock(return_value={"ok": True, "value": "replacement"})
        with self.assertRaisesRegex(run_manager.RunConflict, "differs from its hash"):
            self.service._server_operation(
                self.owner, body, "test.tamper", {"value": 1}, callback,
            )
        callback.assert_not_called()

    def test_named_server_mutations_replay_their_durable_receipts(self):
        for action in ("join", "leave"):
            with self.subTest(action=action), mock.patch.object(
                    self.service.membership, "begin",
                    return_value={"id": f"membership-{action}", "status": "intent"},
            ) as mutate:
                command = {
                    "domain": "telegram", "action": action,
                    "target": "https://t.me/example",
                    "idempotency_key": f"telegram-{action}-replay-0001",
                }
                self.assertEqual(
                    self.service.command(self.owner, command),
                    self.service.command(self.owner, command),
                )
                mutate.assert_called_once_with(
                    action, "https://t.me/example", self.owner.actor_id,
                )

        with mock.patch.object(self.service.followups, "cancel", return_value=True) as cancel:
            command = {
                "domain": "telegram", "action": "followup.cancel",
                "followup_id": "followup-1",
                "idempotency_key": "followup-cancel-replay-0001",
            }
            self.assertEqual(
                self.service.command(self.owner, command),
                self.service.command(self.owner, command),
            )
            cancel.assert_called_once_with("followup-1")

        index = self.root / "memory" / "INDEX.md"
        with mock.patch("memory_catalog.rebuild", return_value=index) as catalog, \
             mock.patch("memory_fts.rebuild", return_value={
                 "ok": True, "schema": "fts", "database": "recall.sqlite3",
                 "sources": 1, "chunks": 2, "corrupt_lines": 0,
                 "fingerprint": "stable",
             }) as fts:
            command = {
                "domain": "memory", "action": "rebuild",
                "idempotency_key": "memory-rebuild-replay-0002",
            }
            self.assertEqual(
                self.service.command(self.owner, command),
                self.service.command(self.owner, command),
            )
            self.assertEqual(catalog.call_count, 1)
            self.assertEqual(fts.call_count, 1)

        with mock.patch("praxis_app.body_client.device_id", return_value="windows-pc"), \
             mock.patch("computer_inventory.refresh", return_value={
                 "ok": True, "device_id": "windows-pc",
             }) as refresh:
            command = {
                "domain": "inventory", "action": "refresh",
                "idempotency_key": "inventory-refresh-replay-0002",
            }
            self.assertEqual(
                self.service.command(self.owner, command),
                self.service.command(self.owner, command),
            )
            refresh.assert_called_once_with("windows-pc")

        with mock.patch("selfdev.request_restart") as restart:
            command = {
                "domain": "system", "action": "restart",
                "idempotency_key": "system-restart-replay-0001",
            }
            self.assertEqual(
                self.service.command(self.owner, command),
                self.service.command(self.owner, command),
            )
            restart.assert_called_once_with("Praxis mini-app: owner requested restart")

        with mock.patch.object(
                self.service, "revoke_device",
                return_value={"ok": True, "device_id": "device-1"},
        ) as revoke:
            command = {
                "domain": "device", "action": "revoke", "device_id": "device-1",
                "reason": "owner request",
                "idempotency_key": "device-revoke-replay-0001",
            }
            self.assertEqual(
                self.service.command(self.owner, command),
                self.service.command(self.owner, command),
            )
            revoke.assert_called_once_with(
                self.owner, "device-1", reason="owner request",
            )

    def test_ui_process_domain_compiles_to_typed_body_capability(self):
        with mock.patch("praxis_app.body_client.call", return_value={
                "ok": True, "operation_id": "op-1", "status": "accepted",
        }) as invoke:
            result = self.service.command(self.owner, {
                "domain": "process", "action": "start", "command": "git status",
                "cwd": "C:\\Code", "execution": "interactive",
                "idempotency_key": "ui-process-start-0001",
            })
        self.assertTrue(result["ok"])
        kwargs = invoke.call_args.kwargs
        self.assertEqual(invoke.call_args.args, (
            "process.start",
            {"command": "git status", "cwd": "C:\\Code", "shell": "power_shell"},
        ))
        self.assertEqual(kwargs["execution"], "interactive")
        self.assertEqual(kwargs["timeout"], 60.0)
        self.assertEqual(kwargs["operation_id"], result["praxis_audit"]["client_operation_id"])
        self.assertEqual(kwargs["request_id"], result["praxis_audit"]["request_id"])
        self.assertEqual(
            self.service.runs.status(result["praxis_audit"]["run_id"])["principal_id"],
            self.owner.principal_id,
        )

    def test_mutating_body_command_requires_key_and_retry_reuses_exact_wire_ids(self):
        command = {
            "domain": "process", "action": "start", "command": "git status",
            "idempotency_key": "same-human-submit-001",
        }
        with self.assertRaisesRegex(ValueError, "idempotency_key is required"):
            self.service.command(self.owner, {
                "domain": "process", "action": "start", "command": "git status",
            })
        with mock.patch("praxis_app.body_client.call", return_value={
                "ok": True, "status": "accepted",
        }) as invoke:
            first = self.service.command(self.owner, command)
            second = self.service.command(self.owner, command)
        self.assertEqual(first["praxis_audit"], second["praxis_audit"])
        self.assertEqual(invoke.call_count, 2)
        self.assertEqual(invoke.call_args_list[0].kwargs["request_id"],
                         invoke.call_args_list[1].kwargs["request_id"])
        self.assertEqual(invoke.call_args_list[0].kwargs["operation_id"],
                         invoke.call_args_list[1].kwargs["operation_id"])
        with self.assertRaisesRegex(run_manager.RunConflict, "different computer command"):
            self.service.command(self.owner, {**command, "command": "git clean -fd"})

    def test_transient_body_failure_reopens_same_run_and_retry_finishes(self):
        command = {
            "domain": "process", "action": "start", "command": "git status",
            "idempotency_key": "retry-after-transport-001",
        }
        with mock.patch("praxis_app.body_client.call", side_effect=[
            {"ok": False, "code": "transport", "error": "bridge reset"},
            {"ok": True, "status": "accepted"},
        ]) as invoke:
            first = self.service.command(self.owner, command)
            run_id = first["praxis_audit"]["run_id"]
            self.assertEqual(self.service.runs.status(run_id)["status"], "blocked")
            second = self.service.command(self.owner, command)

        self.assertEqual(first["praxis_audit"], second["praxis_audit"])
        self.assertEqual(self.service.runs.status(run_id)["status"], "done")
        self.assertEqual(invoke.call_count, 2)
        self.assertEqual(
            invoke.call_args_list[0].kwargs["request_id"],
            invoke.call_args_list[1].kwargs["request_id"],
        )
        receipts = [
            row for row in self.service.runs.events(run_id, strict=True)
            if row.get("kind") == "tool_result"
        ]
        self.assertEqual(len(receipts), 2)

    def test_body_in_doubt_receipt_requires_explicit_reconciliation(self):
        command = {
            "domain": "process", "action": "start", "command": "git status",
            "idempotency_key": "lost-body-executor-001",
        }
        with mock.patch("praxis_app.body_client.call", return_value={
                "ok": False, "code": "operation_in_doubt", "status": "in_doubt",
        }) as invoke:
            first = self.service.command(self.owner, command)
            run_id = first["praxis_audit"]["run_id"]
            self.assertEqual(self.service.runs.status(run_id)["status"], "in_doubt")
            with self.assertRaisesRegex(run_manager.RunConflict, "explicit run control"):
                self.service.command(self.owner, command)
        self.assertEqual(invoke.call_count, 1)

    def test_inventory_requires_read_scope_before_any_machine_metadata(self):
        trusted = praxis_app.Viewer("22", "trusted", ("computer.process",))
        inventory = self.service._inventory(trusted)
        self.assertEqual(inventory, {
            "available": False, "observed_at": "", "state": "scope_required",
        })

    def test_artifacts_are_discovered_outside_compact_event_window(self):
        context = self.create_run()
        ref = self.service.runs.store_artifact(
            context.run_id, b"old but durable", name="old.txt",
        )
        for index in range(170):
            self.service.runs.append_event(context.run_id, "noise", index=index)
        detail = self.service.run_detail(self.owner, context.run_id)
        self.assertEqual(len(detail["events"]), 160)
        self.assertEqual([row["artifact_id"] for row in detail["artifacts"]],
                         [ref["artifact_id"]])

    def test_memory_search_and_map_read_are_bounded_snapshot_views(self):
        device = praxis_app.Viewer(
            "dev_" + "a" * 32, "device", ("praxis.snapshot",),
        )
        with mock.patch("memory_index.search", return_value=[{
            "source": "Threads", "path": "memory/threads.md",
            "text": "x" * 4000, "score": 0.75,
            "source_type": "markdown", "visibility": "owner",
            "provenance": ["event-1"],
        }]) as search:
            result = self.service.command(device, {
                "domain": "memory", "action": "search", "query": "deploy", "limit": 9,
            })
        search.assert_called_once_with("deploy", k=9, scope="owner", semantic=False)
        self.assertEqual(len(result["results"][0]["snippet"]), 2400)
        map_result = self.service.command(device, {
            "domain": "memory", "action": "map.read", "map": "people",
        })
        self.assertEqual(map_result["map"], "PEOPLE")
        self.assertEqual(map_result["path"], "memory/maps/PEOPLE.md")
        with self.assertRaisesRegex(ValueError, "unknown memory map"):
            self.service.command(device, {
                "domain": "memory", "action": "map.read", "map": "../goals",
            })

    def test_memory_rebuild_and_inventory_refresh_have_durable_actor_receipts(self):
        index = self.root / "memory" / "INDEX.md"
        index.write_text("# index\n", encoding="utf-8")
        worker = praxis_app.Viewer(
            "dev_" + "b" * 32, "device", ("praxis.work", "computer.read"),
        )
        with mock.patch("memory_catalog.rebuild", return_value=index), \
             mock.patch("memory_fts.rebuild", return_value={
                 "ok": True, "schema": "fts", "database": "memory/.state/recall.sqlite3",
                 "sources": 4, "chunks": 12, "corrupt_lines": 0, "fingerprint": "abc",
             }):
            rebuilt = self.service.command(worker, {
                "domain": "memory", "action": "rebuild",
                "idempotency_key": "memory-rebuild-receipt-0001",
            })
        self.assertTrue(rebuilt["ok"])
        rebuilt_run = self.service.runs.status(rebuilt["praxis_audit"]["run_id"])
        self.assertEqual(rebuilt_run["principal_id"], worker.principal_id)
        self.assertEqual(rebuilt_run["status"], "done")

        with mock.patch("praxis_app.body_client.device_id", return_value="windows-pc"), \
             mock.patch("computer_inventory.refresh", return_value={
                 "ok": True, "device_id": "windows-pc", "observed_at": "now",
             }) as refresh:
            inventory = self.service.command(worker, {
                "domain": "inventory", "action": "refresh",
                "idempotency_key": "inventory-refresh-receipt-0001",
            })
        refresh.assert_called_once_with("windows-pc")
        self.assertEqual(
            self.service.runs.status(inventory["praxis_audit"]["run_id"])["principal_id"],
            worker.principal_id,
        )
        snapshot_only = praxis_app.Viewer(
            "dev_" + "c" * 32, "device", ("praxis.snapshot",),
        )
        with self.assertRaises(PermissionError):
            self.service.command(snapshot_only, {
                "domain": "inventory", "action": "refresh",
            })

    def test_windows_export_becomes_one_hash_bound_browser_download(self):
        payload = b"praxis-file\n"
        digest = hashlib.sha256(payload).hexdigest()

        def fetch(_artifact, destination):
            Path(destination).write_bytes(payload)
            return {"ok": True, "sha256": digest, "size": len(payload)}

        command = {
            "domain": "files", "action": "export",
            "path": r"C:\Users\Owner\report.txt",
            "execution": "interactive",
            "idempotency_key": "pwa-export-report-0001",
        }
        with mock.patch("praxis_app.body_client.call", return_value={
                "ok": True,
                "artifact": {
                    "sha256": digest, "size": len(payload), "name": "report.txt",
                    "mime": "text/plain", "source_device": "windows-pc",
                },
        }), mock.patch("praxis_app.body_client.fetch_artifact", side_effect=fetch) as download:
            first = self.service.command(self.owner, command)
            second = self.service.command(self.owner, command)
        self.assertTrue(first["ok"])
        self.assertEqual(first["download"]["sha256"], digest)
        self.assertIn("ticket=", first["download"]["download_url"])
        self.assertEqual(download.call_count, 1)
        detail = self.service.run_detail(self.owner, first["praxis_audit"]["run_id"])
        self.assertEqual(len(detail["artifacts"]), 1)
        self.assertEqual(second["download"], first["download"])

    def test_windows_capture_becomes_an_inline_hash_bound_image(self):
        payload = b"\x89PNG\r\n\x1a\npass24-capture"
        digest = hashlib.sha256(payload).hexdigest()

        def fetch(_artifact, destination):
            Path(destination).write_bytes(payload)
            return {"ok": True, "sha256": digest, "size": len(payload)}

        command = {
            "domain": "desktop", "action": "capture", "target": "desktop",
            "execution": "interactive",
            "idempotency_key": "pwa-capture-desktop-0001",
        }
        with mock.patch("praxis_app.body_client.call", return_value={
                "ok": True,
                "artifact": {
                    "sha256": digest, "size": len(payload), "name": "desktop.png",
                    "mime": "image/png", "source_device": "windows-pc",
                },
        }) as body_call, mock.patch(
            "praxis_app.body_client.fetch_artifact", side_effect=fetch,
        ) as download:
            captured = self.service.command(self.owner, command)

        self.assertTrue(captured["ok"])
        self.assertEqual(captured["download"]["presentation"], "image")
        self.assertEqual(captured["download"]["media_type"], "image/png")
        self.assertEqual(download.call_count, 1)
        body_call.assert_called_once()
        self.assertEqual(body_call.call_args.args[0], "desktop.screen.capture")
        self.assertEqual(body_call.call_args.args[1], {"target": "desktop"})
        run_id = captured["praxis_audit"]["run_id"]
        self.assertEqual(len(self.service.run_detail(self.owner, run_id)["artifacts"]), 1)
        token = captured["download"]["download_url"].split("ticket=", 1)[1]
        grant = praxis_app.ARTIFACT_TICKETS.authorize(
            token, run_id, captured["download"]["artifact_id"], consume=False,
        )
        self.assertIsNotNone(grant)
        self.assertEqual(grant.presentation, "image")

    def test_concurrent_same_export_materializes_once_and_replays_same_ticket(self):
        import concurrent.futures

        payload = b"one export under contention\n"
        digest = hashlib.sha256(payload).hexdigest()
        body_calls = 0
        body_calls_lock = threading.Lock()
        second_body_call = threading.Event()

        def invoke(*_args, **_kwargs):
            nonlocal body_calls
            with body_calls_lock:
                body_calls += 1
                if body_calls == 2:
                    second_body_call.set()
            return {
                "ok": True,
                "artifact": {
                    "sha256": digest, "size": len(payload), "name": "race.txt",
                    "mime": "text/plain", "source_device": "windows-pc",
                },
            }

        def fetch(_artifact, destination):
            self.assertTrue(
                second_body_call.wait(3),
                "the duplicate request must reach the durable body replay",
            )
            Path(destination).write_bytes(payload)
            return {"ok": True, "sha256": digest, "size": len(payload)}

        command = {
            "domain": "files", "action": "export",
            "path": r"C:\Users\Owner\race.txt",
            "execution": "interactive",
            "idempotency_key": "pwa-export-concurrent-0001",
        }
        with mock.patch("praxis_app.body_client.call", side_effect=invoke), \
             mock.patch("praxis_app.body_client.fetch_artifact", side_effect=fetch) as downloaded, \
             concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.service.command, self.owner, command) for _ in range(2)]
            results = [future.result(timeout=5) for future in futures]

        self.assertEqual(body_calls, 2)
        self.assertEqual(downloaded.call_count, 1)
        self.assertEqual(results[0]["download"], results[1]["download"])
        run_id = results[0]["praxis_audit"]["run_id"]
        created = [
            row for row in self.service.runs.events(run_id, strict=True)
            if row.get("kind") == "artifact_created"
        ]
        self.assertEqual(len(created), 1)

    def test_files_only_device_export_issues_exact_download_capability(self):
        payload = b"files-only\n"
        digest = hashlib.sha256(payload).hexdigest()
        enrollment = self.service.issue_device_enrollment(
            self.owner, label="files only export", scopes=["computer.files"],
        )
        credential = self.service.redeem_device(
            enrollment["enrollment_token"], platform="Windows",
        )
        device = self.service.device_viewer(credential["device_token"])
        command = {
            "domain": "files", "action": "export", "path": r"C:\proof.txt",
            "execution": "interactive",
            "idempotency_key": "files-only-export-0001",
        }

        def fetch(_artifact, destination):
            Path(destination).write_bytes(payload)
            return {"ok": True, "sha256": digest, "size": len(payload)}

        with mock.patch("praxis_app.body_client.call", return_value={
                "ok": True,
                "artifact": {
                    "sha256": digest, "size": len(payload), "name": "proof.txt",
                    "mime": "text/plain", "source_device": "windows-pc",
                },
        }), mock.patch("praxis_app.body_client.fetch_artifact", side_effect=fetch):
            exported = self.service.command(device, command)

        self.assertTrue(exported["ok"])
        run_id = exported["praxis_audit"]["run_id"]
        artifact_id = exported["download"]["artifact_id"]
        with self.assertRaises(PermissionError):
            self.service.run_detail(device, run_id)
        ticket = exported["download"]["download_url"].split("ticket=", 1)[1]
        other = self.service.runs.store_artifact(run_id, b"other", name="other.txt")
        self.assertIsNone(praxis_app.ARTIFACT_TICKETS.authorize(
            ticket, run_id, other["artifact_id"], consume=False,
        ))
        grant = praxis_app.ARTIFACT_TICKETS.consume(ticket, run_id, artifact_id)
        self.assertIsNotNone(grant)
        path, _card = self.service.artifact_path_from_ticket(grant)
        self.assertEqual(path.read_bytes(), payload)
        self.assertEqual(grant.viewer.scopes, ("computer.files",))
        self.assertIsNone(praxis_app.ARTIFACT_TICKETS.consume(
            ticket, run_id, artifact_id,
        ))

    def test_browser_upload_does_not_use_artifact_hash_as_destination_guard(self):
        source = self.root / "staged.upload"
        source.write_bytes(b"from-browser")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        uploaded = {
            "ok": True,
            "artifact": {
                "sha256": digest, "size": source.stat().st_size,
                "name": "from-browser.txt", "source_device": "praxis-server",
            },
            "complete": True, "reused": False,
        }
        with mock.patch("praxis_app.body_client.upload_artifact", return_value=uploaded) as offer, \
             mock.patch("praxis_app.body_client.call", return_value={
                 "ok": True, "path": r"C:\Users\Owner\Downloads\from-browser.txt",
                 "sha256": digest,
             }) as invoke:
            result = self.service.import_uploaded_file(
                self.owner, source, name="from-browser.txt", media_type="text/plain",
                destination=r"C:\Users\Owner\Downloads\from-browser.txt",
                execution="interactive", idempotency_key="pwa-import-browser-0001",
            )
        self.assertTrue(result["ok"])
        offer.assert_called_once_with(source, name="from-browser.txt", timeout=600)
        self.assertEqual(invoke.call_args.args[0], "fs.import")
        self.assertEqual(invoke.call_args.args[1], {
            "artifact": uploaded["artifact"],
            "path": r"C:\Users\Owner\Downloads\from-browser.txt",
        })
        self.assertNotIn("expected_sha256", invoke.call_args.args[1])
        self.assertEqual(invoke.call_args.kwargs["execution"], "interactive")


def signed_init(token: str, user_id: int) -> str:
    params = {
        "auth_date": str(int(time.time())),
        "query_id": "AA-PASS24",
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={params[key]}" for key in sorted(params))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**params, "hash": digest})


class PraxisAppHttpCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import mailroom_bot

        self.mb = mailroom_bot
        self.saved = (
            self.mb.BASE, self.mb.TOKEN, self.mb.OWNER_ID,
            self.mb._PRAXIS_APP_SERVICE, self.mb.PRAXIS_APP_URL,
        )
        self.root = Path(tempfile.mkdtemp())
        self.token = "123456:PASS24-TEST"
        self.owner_id = 123456789
        self.service = praxis_app.PraxisAppService(
            self.root, owner_id=self.owner_id,
            body_probe=lambda **_kwargs: {"ok": False},
        )
        self.service._body_status = lambda: {
            "configured": True, "online": False, "state": "offline", "device_id": "pc",
        }
        self.service._inventory = lambda _viewer: {"available": False}
        self.service._computer_evidence = lambda _viewer: []
        self.service._telegram = lambda: {
            "rooms": [], "followups": [], "membership": [], "pending_followups": 0,
        }
        self.service._trust = lambda _viewer: {
            "owner_only": True, "grants": [], "available_scopes": [],
        }
        self.service._system = lambda: {"head": "test"}
        self.mb.BASE = self.root
        self.mb.TOKEN = self.token
        self.mb.OWNER_ID = self.owner_id
        self.mb.PRAXIS_APP_URL = "https://praxis.test/app?v=24"
        self.mb._PRAXIS_APP_SERVICE = self.service
        static = self.root / "praxis_static"
        static.mkdir()
        (static / "manifest.webmanifest").write_text("{}", encoding="utf-8")
        (static / "sw.js").write_text("self.addEventListener('fetch',()=>{});", encoding="utf-8")
        self.client = TestClient(TestServer(self.mb.make_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        (self.mb.BASE, self.mb.TOKEN, self.mb.OWNER_ID,
         self.mb._PRAXIS_APP_SERVICE, self.mb.PRAXIS_APP_URL) = self.saved

    def owner_headers(self):
        return {"X-Telegram-Init-Data": signed_init(self.token, self.owner_id)}

    async def test_snapshot_requires_signed_initdata(self):
        denied = await self.client.get("/api/praxis/v1/snapshot")
        self.assertEqual(denied.status, 401)
        response = await self.client.get(
            "/api/praxis/v1/snapshot",
            headers={"X-Telegram-Init-Data": signed_init(self.token, self.owner_id)},
        )
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload["viewer"]["role"], "owner")
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_sse_ticket_is_opaque_and_one_use(self):
        init_data = signed_init(self.token, self.owner_id)
        response = await self.client.get(
            "/api/praxis/v1/events-ticket",
            headers={"X-Telegram-Init-Data": init_data},
        )
        self.assertEqual(response.status, 200)
        ticket = (await response.json())["ticket"]
        self.assertNotIn(init_data, ticket)
        viewer = praxis_app.TICKETS.consume(ticket)
        self.assertEqual(viewer.actor_id, str(self.owner_id))
        self.assertIsNone(praxis_app.TICKETS.consume(ticket))

    async def test_event_stream_missing_or_invalid_ticket_is_unauthorized(self):
        missing = await self.client.get("/api/praxis/v1/events")
        self.assertEqual(missing.status, 401)
        invalid = await self.client.get(
            "/api/praxis/v1/events", params={"ticket": "not-a-ticket"},
        )
        self.assertEqual(invalid.status, 401)

    async def test_new_api_rejects_query_initdata_even_when_signature_is_valid(self):
        init_data = signed_init(self.token, self.owner_id)
        query_only = await self.client.get(
            "/api/praxis/v1/snapshot", params={"_auth": init_data},
        )
        self.assertEqual(query_only.status, 401)
        ambiguous = await self.client.get(
            "/api/praxis/v1/snapshot", params={"_auth": init_data},
            headers={"X-Telegram-Init-Data": init_data},
        )
        self.assertEqual(ambiguous.status, 401)

    async def test_manifest_and_service_worker_have_pwa_headers(self):
        manifest = await self.client.get("/app/static/manifest.webmanifest")
        self.assertEqual(manifest.status, 200)
        self.assertEqual(manifest.content_type, "application/manifest+json")
        self.assertIn("no-cache", manifest.headers["Cache-Control"])
        worker = await self.client.get("/app/static/sw.js")
        self.assertEqual(worker.status, 200)
        self.assertEqual(worker.content_type, "text/javascript")
        self.assertEqual(worker.headers["Service-Worker-Allowed"], "/app")
        self.assertIn("no-cache", worker.headers["Cache-Control"])

    async def test_one_time_enrollment_bearer_exact_scopes_and_owner_revoke(self):
        issued = await self.client.post(
            "/api/praxis/v1/device/enrollment",
            headers=self.owner_headers(),
            json={
                "label": "Desktop PWA",
                "scopes": ["praxis.snapshot"],
                "ttl_seconds": 120,
            },
        )
        self.assertEqual(issued.status, 200)
        enrollment = (await issued.json())["enrollment"]
        parsed = urlsplit(enrollment["enrollment_url"])
        self.assertNotIn(enrollment["enrollment_token"], parsed.query)
        self.assertEqual(
            parse_qs(parsed.fragment)["enroll"], [enrollment["enrollment_token"]],
        )
        redeemed = await self.client.post(
            "/api/praxis/v1/device/enroll",
            json={"enrollment_token": enrollment["enrollment_token"],
                  "platform": "Windows 11"},
        )
        self.assertEqual(redeemed.status, 200)
        credential = await redeemed.json()
        bearer = credential["device_token"]
        device_id = credential["device"]["device_id"]
        device_headers = {"Authorization": f"Bearer {bearer}"}

        snapshot = await self.client.get(
            "/api/praxis/v1/snapshot", headers=device_headers,
        )
        self.assertEqual(snapshot.status, 200)
        projection = await snapshot.json()
        self.assertEqual(projection["viewer"]["role"], "device")
        self.assertEqual(projection["viewer"]["scopes"], ["praxis.snapshot"])
        self.assertFalse(projection["viewer"]["can_delegate"])
        events = await self.client.get(
            "/api/praxis/v1/events-ticket", headers=device_headers,
        )
        self.assertEqual(events.status, 403)
        computer = await self.client.post(
            "/api/praxis/v1/command", headers=device_headers,
            json={"domain": "files", "action": "list", "path": "C:\\"},
        )
        self.assertEqual(computer.status, 403)
        delegate = await self.client.post(
            "/api/praxis/v1/device/enrollment", headers=device_headers,
            json={"label": "forbidden"},
        )
        self.assertEqual(delegate.status, 403)
        self_revoke = await self.client.post(
            "/api/praxis/v1/command", headers=device_headers,
            json={"domain": "device", "action": "revoke", "device_id": device_id},
        )
        self.assertEqual(self_revoke.status, 403)

        listed = await self.client.get(
            "/api/praxis/v1/devices", headers=self.owner_headers(),
        )
        self.assertEqual(listed.status, 200)
        self.assertEqual((await listed.json())["items"][0]["device_id"], device_id)
        revoked = await self.client.post(
            "/api/praxis/v1/command", headers=self.owner_headers(),
            json={"domain": "device", "action": "revoke", "device_id": device_id,
                  "idempotency_key": "owner-device-revoke-0001"},
        )
        self.assertEqual(revoked.status, 200)
        after = await self.client.get(
            "/api/praxis/v1/snapshot", headers=device_headers,
        )
        self.assertEqual(after.status, 401)
        replay = await self.client.post(
            "/api/praxis/v1/device/enroll",
            json={"enrollment_token": enrollment["enrollment_token"],
                  "platform": "Windows 11"},
        )
        self.assertEqual(replay.status, 400)
        self.assertEqual((await replay.json())["error"], "invalid_enrollment")

    async def test_revoke_denies_preissued_event_and_artifact_urls(self):
        enrollment = self.service.issue_device_enrollment(
            self.service.viewer(self.owner_id), label="revocation test",
            scopes=["praxis.events", "praxis.snapshot"],
        )
        credential = self.service.redeem_device(
            enrollment["enrollment_token"], platform="Windows",
        )
        headers = {"Authorization": f"Bearer {credential['device_token']}"}
        device_id = credential["device"]["device_id"]
        context = self.service.runs.create(
            RunContext.create(
                kind="telegram_turn", goal="revocation proof",
                principal_id="telegram:owner", scope="owner",
            ),
            "# Context\n",
        )
        self.service.runs.store_artifact(
            context.run_id, b"secret after revoke", name="revoked.txt",
        )
        detail = await self.client.get(
            f"/api/praxis/v1/runs/{context.run_id}", headers=headers,
        )
        self.assertEqual(detail.status, 200)
        artifact_url = (await detail.json())["artifacts"][0]["download_url"]
        events = await self.client.get(
            "/api/praxis/v1/events-ticket", headers=headers,
        )
        self.assertEqual(events.status, 200)
        event_ticket = (await events.json())["ticket"]

        self.assertTrue(self.service.revoke_device(
            self.service.viewer(self.owner_id), device_id, reason="test revoke",
        )["ok"])
        artifact = await self.client.get(artifact_url)
        self.assertEqual(artifact.status, 401)
        stream = await self.client.get(
            "/api/praxis/v1/events", params={"ticket": event_ticket},
        )
        self.assertEqual(stream.status, 401)

    async def test_files_only_device_ticket_downloads_exact_artifact_once(self):
        enrollment = self.service.issue_device_enrollment(
            self.service.viewer(self.owner_id), label="files only",
            scopes=["computer.files"],
        )
        credential = self.service.redeem_device(
            enrollment["enrollment_token"], platform="Windows",
        )
        headers = {"Authorization": f"Bearer {credential['device_token']}"}
        device = self.service.device_viewer(credential["device_token"])
        context = self.service.runs.create(
            RunContext.create(
                kind="computer_operation", goal="exact artifact",
                principal_id=device.principal_id, scope="owner",
            ),
            "# Context\n",
        )
        first = self.service.runs.store_artifact(
            context.run_id, b"first", name="first.txt",
        )
        second = self.service.runs.store_artifact(
            context.run_id, b"second", name="second.txt",
        )
        run_detail = await self.client.get(
            f"/api/praxis/v1/runs/{context.run_id}", headers=headers,
        )
        self.assertEqual(run_detail.status, 403)

        ticket = praxis_app.ARTIFACT_TICKETS.issue(
            device, context.run_id, first["artifact_id"],
            validator=self.service.revalidate_artifact_ticket,
        )
        url = (
            f"/api/praxis/v1/runs/{context.run_id}/artifacts/"
            f"{first['artifact_id']}?ticket={ticket}"
        )
        fetched = await self.client.get(url)
        self.assertEqual(fetched.status, 200)
        self.assertEqual(await fetched.read(), b"first")
        replay = await self.client.get(url)
        self.assertEqual(replay.status, 401)

        wrong_ticket = praxis_app.ARTIFACT_TICKETS.issue(
            device, context.run_id, first["artifact_id"],
            validator=self.service.revalidate_artifact_ticket,
        )
        wrong = await self.client.get(
            f"/api/praxis/v1/runs/{context.run_id}/artifacts/"
            f"{second['artifact_id']}?ticket={wrong_ticket}",
        )
        self.assertEqual(wrong.status, 401)

    async def test_artifact_ticket_survives_head_then_is_consumed_by_get(self):
        context = self.service.runs.create(
            RunContext.create(
                kind="telegram_turn", goal="artifact", principal_id="telegram:owner",
                scope="owner",
            ),
            "# Context\n",
        )
        ref = self.service.runs.store_artifact(
            context.run_id, b"hello", name="proof.txt", media_type="text/plain",
        )
        url = self.service.run_detail(self.service.viewer(self.owner_id), context.run_id)[
            "artifacts"
        ][0]["download_url"]
        head = await self.client.head(url)
        self.assertEqual(head.status, 200)
        self.assertEqual(head.headers["X-Praxis-SHA256"], ref["sha256"])
        fetched = await self.client.get(url)
        self.assertEqual(fetched.status, 200)
        self.assertEqual(await fetched.read(), b"hello")
        replay = await self.client.get(url)
        self.assertEqual(replay.status, 401)

    async def test_only_image_presentation_ticket_serves_safe_raster_inline(self):
        context = self.service.runs.create(
            RunContext.create(
                kind="computer_operation", goal="show capture",
                principal_id="telegram:owner", scope="owner",
            ),
            "# Context\n",
        )
        ref = self.service.runs.store_artifact(
            context.run_id, b"\x89PNG\r\n\x1a\npreview", name="desktop.png",
            media_type="image/png",
        )
        owner = self.service.viewer(self.owner_id)
        image_ticket = praxis_app.ARTIFACT_TICKETS.issue(
            owner, context.run_id, ref["artifact_id"],
            validator=self.service.revalidate_artifact_ticket,
            presentation="image",
        )
        image_url = (
            f"/api/praxis/v1/runs/{context.run_id}/artifacts/"
            f"{ref['artifact_id']}?ticket={image_ticket}"
        )
        inline = await self.client.get(image_url)
        self.assertEqual(inline.status, 200)
        self.assertTrue(inline.headers["Content-Disposition"].startswith("inline;"))

        download_url = self.service.run_detail(owner, context.run_id)["artifacts"][0][
            "download_url"
        ]
        attachment = await self.client.get(download_url)
        self.assertEqual(attachment.status, 200)
        self.assertTrue(
            attachment.headers["Content-Disposition"].startswith("attachment;")
        )

    async def test_artifact_requires_header_xor_ticket_and_burns_ambiguous_ticket(self):
        context = self.service.runs.create(
            RunContext.create(
                kind="telegram_turn", goal="auth modes",
                principal_id="telegram:owner", scope="owner",
            ),
            "# Context\n",
        )
        ref = self.service.runs.store_artifact(
            context.run_id, b"auth modes", name="auth.txt",
        )
        direct = (
            f"/api/praxis/v1/runs/{context.run_id}/artifacts/{ref['artifact_id']}"
        )
        missing = await self.client.get(direct)
        self.assertEqual(missing.status, 401)
        invalid = await self.client.get(direct + "?ticket=invalid")
        self.assertEqual(invalid.status, 401)

        ticket_url = self.service.run_detail(
            self.service.viewer(self.owner_id), context.run_id,
        )["artifacts"][0]["download_url"]
        ambiguous = await self.client.get(ticket_url, headers=self.owner_headers())
        self.assertEqual(ambiguous.status, 401)
        spent = await self.client.get(ticket_url)
        self.assertEqual(spent.status, 401)

        header_only = await self.client.get(direct, headers=self.owner_headers())
        self.assertEqual(header_only.status, 200)
        self.assertEqual(await header_only.read(), b"auth modes")

    async def test_browser_upload_is_streamed_to_private_stage_and_removed(self):
        observed: dict = {}

        def import_file(viewer, source, **kwargs):
            observed.update({
                "viewer": viewer, "source": Path(source),
                "payload": Path(source).read_bytes(), **kwargs,
            })
            return {"ok": True, "destination": kwargs["destination"]}

        data = FormData()
        data.add_field(
            "file", b"browser-payload", filename="evidence.txt",
            content_type="text/plain",
        )
        data.add_field("destination", r"C:\Users\Owner\Downloads\evidence.txt")
        data.add_field("execution", "interactive")
        data.add_field("idempotency_key", "http-file-import-0001")
        with mock.patch.object(
                self.service, "import_uploaded_file", side_effect=import_file,
        ) as transfer:
            response = await self.client.post(
                "/api/praxis/v1/files/import", headers=self.owner_headers(), data=data,
            )
        self.assertEqual(response.status, 200)
        self.assertEqual(observed["payload"], b"browser-payload")
        self.assertEqual(observed["name"], "evidence.txt")
        self.assertEqual(observed["execution"], "interactive")
        self.assertFalse(observed["source"].exists())
        transfer.assert_called_once()

    async def test_browser_upload_bounds_scalar_fields_and_removes_failed_stage(self):
        data = FormData()
        data.add_field(
            "file", b"small-file", filename="evidence.txt",
            content_type="text/plain",
        )
        data.add_field("destination", "x" * 4097)
        data.add_field("execution", "interactive")
        data.add_field("idempotency_key", "http-file-import-bounded-0001")
        with mock.patch.object(self.service, "import_uploaded_file") as transfer:
            response = await self.client.post(
                "/api/praxis/v1/files/import", headers=self.owner_headers(), data=data,
            )
        self.assertEqual(response.status, 400)
        transfer.assert_not_called()
        upload_dir = self.root / "memory" / ".state" / "praxis_app" / "uploads"
        self.assertEqual(list(upload_dir.glob("*.upload")), [])


if __name__ == "__main__":
    unittest.main()
