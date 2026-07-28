from __future__ import annotations

import json
import hashlib
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import owner_delivery
import praxis_app


class OwnerDeliveryLedgerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="praxis-owner-delivery-")
        self.path = Path(self.temp.name) / "private" / "events.jsonl"
        self.ledger = owner_delivery.OwnerDeliveryLedger(self.path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def emit(self, *, dedupe="answer:1", coalesce="") -> dict:
        return self.ledger.emit(
            "followup_answer",
            title="Миша ответил",
            body="Короткий ответ",
            thread_key="telegram-followup:1",
            correlation={"followup_id": "tgfu_1", "message_id": 77},
            reason="Пришёл ответ в отслеживаемой нити.",
            provenance={"source": "telegram_followups", "source_id": "tgfu_1"},
            expectation="Посмотреть и решить, продолжать ли разговор.",
            action={"domain": "telegram", "action": "open_followup"},
            result="Короткий ответ",
            dedupe_key=dedupe,
            coalesce_key=coalesce,
        )

    def test_typed_item_lifecycle_is_append_only_and_revision_bound(self):
        item = self.emit()
        self.assertEqual(item["status"], "queued")
        self.assertEqual(item["revision"], 1)
        delivered = self.ledger.mark_delivered(
            item["id"], transport="telegram", receipt={"message_id": 901},
        )
        self.assertEqual(delivered["status"], "delivered")
        self.assertEqual(delivered["delivery"]["receipt"]["message_id"], 901)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.ledger.transition(
                item["id"], "read", expected_revision=1,
            )
        read = self.ledger.transition(
            item["id"], "read", expected_revision=delivered["revision"],
            detail={"surface": "pwa"},
        )
        acted = self.ledger.transition(
            item["id"], "acted", expected_revision=read["revision"],
            detail={"command": "continue"},
        )
        self.assertEqual(acted["status"], "acted")
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 4)

    def test_reading_a_queued_pwa_item_records_implicit_delivery(self):
        item = self.emit()
        read = self.ledger.transition(
            item["id"], "read", expected_revision=item["revision"],
            detail={"surface": "pwa"},
        )
        self.assertEqual(read["status"], "read")
        self.assertEqual(read["revision"], 3)
        self.assertTrue(read["delivered_at"])
        self.assertEqual(read["last_detail"], {"surface": "pwa"})

    def test_dedupe_is_exact_and_newer_coalesced_item_supersedes_old(self):
        first = self.emit(dedupe="same", coalesce="thread:42")
        replay = self.emit(dedupe="same", coalesce="thread:42")
        self.assertEqual(replay["id"], first["id"])
        newer = self.emit(dedupe="new", coalesce="thread:42")
        self.assertNotEqual(newer["id"], first["id"])
        self.assertEqual(self.ledger.get(first["id"])["status"], "superseded")
        self.assertEqual([row["id"] for row in self.ledger.list()], [newer["id"]])

    def test_coalesce_replay_repairs_crash_after_newer_create(self):
        first = self.emit(dedupe="old", coalesce="thread:crash")
        with mock.patch.object(
            self.ledger, "_transition_unlocked", side_effect=OSError("power loss"),
        ):
            with self.assertRaises(OSError):
                self.emit(dedupe="new-after-crash", coalesce="thread:crash")
        repaired = self.emit(dedupe="new-after-crash", coalesce="thread:crash")
        self.assertEqual(repaired["status"], "queued")
        self.assertEqual(self.ledger.get(first["id"])["status"], "superseded")
        self.assertEqual(len(self.ledger.list()), 1)

    def test_final_torn_tail_is_repaired_with_hashed_provenance(self):
        item = self.emit()
        with self.path.open("ab") as target:
            target.write(f'{{"schema":"{owner_delivery.SCHEMA}"'.encode("ascii"))
        restarted = owner_delivery.OwnerDeliveryLedger(self.path)
        self.assertEqual(restarted.get(item["id"])["status"], "queued")
        delivered = restarted.mark_delivered(
            item["id"], transport="telegram", receipt={"message_id": 4},
        )
        self.assertEqual(delivered["status"], "delivered")
        self.assertTrue(self.path.read_bytes().endswith(b"\n"))
        self.assertIn(b'"op":"repair"', self.path.read_bytes())
        self.assertEqual(
            owner_delivery.OwnerDeliveryLedger(self.path).get(item["id"])["status"],
            "delivered",
        )

    def test_valid_final_row_without_newline_is_recovered(self):
        item = self.emit()
        self.path.write_bytes(self.path.read_bytes().rstrip(b"\n"))
        restarted = owner_delivery.OwnerDeliveryLedger(self.path)
        self.assertEqual(restarted.get(item["id"])["status"], "queued")
        restarted.mark_delivered(
            item["id"], transport="telegram", receipt={"message_id": 5},
        )
        self.assertNotIn(b'"op":"repair"', self.path.read_bytes())

    def test_chain_detects_middle_deletion_and_line_boundary_tail_truncation(self):
        middle_path = Path(self.temp.name) / "middle" / "events.jsonl"
        middle = owner_delivery.OwnerDeliveryLedger(middle_path)
        for index in range(3):
            middle.emit("attention", title=f"row {index}", dedupe_key=f"row:{index}")
        rows = middle_path.read_bytes().splitlines(keepends=True)
        self.assertEqual(len(rows), 3)
        middle_path.write_bytes(rows[0] + rows[2])
        with self.assertRaisesRegex(owner_delivery.OwnerDeliveryCorruption, "chain"):
            middle.snapshot()

        tail_path = Path(self.temp.name) / "tail" / "events.jsonl"
        tail = owner_delivery.OwnerDeliveryLedger(tail_path)
        tail.emit("attention", title="kept", dedupe_key="kept")
        tail.emit("attention", title="removed", dedupe_key="removed")
        committed = tail_path.read_bytes().splitlines(keepends=True)
        tail_path.write_bytes(committed[0])
        with self.assertRaisesRegex(owner_delivery.OwnerDeliveryCorruption, "truncated"):
            tail.snapshot()

    def test_crash_after_ledger_fsync_before_head_update_recovers_chain(self):
        item = self.emit()
        head_before = self.ledger.head_path.read_bytes()
        with mock.patch.object(
            self.ledger, "_write_head_unlocked", side_effect=OSError("power loss"),
        ):
            with self.assertRaisesRegex(OSError, "power loss"):
                self.ledger.mark_delivered(
                    item["id"], transport="telegram", receipt={"message_id": 55},
                )
        self.assertEqual(self.ledger.head_path.read_bytes(), head_before)

        restarted = owner_delivery.OwnerDeliveryLedger(self.path)
        recovered = restarted.get(item["id"])
        self.assertEqual(recovered["status"], "delivered")
        self.assertNotEqual(restarted.head_path.read_bytes(), head_before)

    def test_nonempty_legacy_ledger_fails_closed(self):
        current = self.emit()
        del current
        row = json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])
        row.pop("seq")
        row.pop("prev_sha256")
        row["schema"] = "praxis.owner-delivery.v1"
        legacy_path = Path(self.temp.name) / "legacy" / "events.jsonl"
        legacy_path.parent.mkdir(parents=True)
        legacy_path.write_text(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(owner_delivery.OwnerDeliveryCorruption):
            owner_delivery.OwnerDeliveryLedger(legacy_path).snapshot()

    @unittest.skipIf(os.name == "nt", "POSIX permission and inode semantics")
    def test_private_modes_are_repaired_and_unsafe_file_types_are_rejected(self):
        self.emit()
        os.chmod(self.path.parent, 0o755)
        for private in (self.path, self.ledger.lock_path, self.ledger.head_path):
            os.chmod(private, 0o644)
        self.ledger.snapshot()
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        for private in (self.path, self.ledger.lock_path, self.ledger.head_path):
            self.assertEqual(private.stat().st_mode & 0o777, 0o600)

        def install(path: Path, kind: str) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            target = path.parent / f"target-{kind}"
            target.write_bytes(b"target")
            if kind == "symlink":
                os.symlink(target, path)
            elif kind == "hardlink":
                os.link(target, path)
            else:
                os.mkfifo(path)

        for private_name in ("events.jsonl", ".lock"):
            for kind in ("symlink", "hardlink", "fifo"):
                with self.subTest(private_name=private_name, kind=kind):
                    base = Path(self.temp.name) / f"unsafe-{private_name}-{kind}"
                    victim = base / "private" / private_name
                    install(victim, kind)
                    ledger = owner_delivery.OwnerDeliveryLedger(
                        base / "private" / "events.jsonl"
                    )
                    with self.assertRaises(OSError):
                        ledger.snapshot()

    def test_complete_malformed_and_duplicate_rows_fail_closed(self):
        self.emit()
        original = self.path.read_text(encoding="utf-8")
        ambiguous = original.rstrip().replace(
            f'"schema":"{owner_delivery.SCHEMA}"',
            f'"schema":"{owner_delivery.SCHEMA}","schema":"evil"',
            1,
        )
        with self.path.open("a", encoding="utf-8") as target:
            target.write(ambiguous + "\n")
        with self.assertRaisesRegex(owner_delivery.OwnerDeliveryCorruption, "invalid committed"):
            self.ledger.snapshot()

        duplicate_path = Path(self.temp.name) / "duplicate" / "events.jsonl"
        duplicate = owner_delivery.OwnerDeliveryLedger(duplicate_path)
        duplicate.emit("attention", title="one")
        first_raw = duplicate_path.read_bytes().rstrip(b"\n")
        row = json.loads(first_raw)
        row["seq"] = 2
        row["prev_sha256"] = hashlib.sha256(first_raw).hexdigest()
        with duplicate_path.open("a", encoding="utf-8") as target:
            target.write(json.dumps(
                row, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ) + "\n")
        with self.assertRaisesRegex(owner_delivery.OwnerDeliveryCorruption, "duplicate"):
            duplicate.snapshot()

    def test_invalid_committed_state_transition_fails_closed(self):
        item = self.emit()
        self.ledger.mark_delivered(
            item["id"], transport="telegram", receipt={"message_id": 6},
        )
        rows = self.path.read_text(encoding="utf-8").splitlines()
        invalid = json.loads(rows[-1])
        invalid["event_id"] = "delivery-event-" + "f" * 32
        invalid["seq"] += 1
        invalid["prev_sha256"] = hashlib.sha256(rows[-1].encode("utf-8")).hexdigest()
        invalid["data"]["expected_revision"] = 99
        with self.path.open("a", encoding="utf-8") as target:
            target.write(json.dumps(
                invalid, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ) + "\n")
        with self.assertRaisesRegex(owner_delivery.OwnerDeliveryCorruption, "state transition"):
            self.ledger.snapshot()

    def test_concurrent_same_dedupe_key_creates_one_item(self):
        barrier = threading.Barrier(12)
        ids: list[str] = []
        failures: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait()
                ids.append(self.emit(dedupe="one-concurrent-receipt")["id"])
            except BaseException as exc:  # evidence from worker threads
                failures.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertFalse(failures)
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(self.ledger.list()), 1)

    def test_snapshot_has_stable_counts_and_no_superseded_noise(self):
        first = self.emit(dedupe="first", coalesce="one-thread")
        second = self.emit(dedupe="second", coalesce="one-thread")
        self.ledger.mark_delivered(
            second["id"], transport="telegram", receipt={"message_id": 9},
        )
        snapshot = self.ledger.snapshot()
        self.assertRegex(snapshot["revision"], r"^[0-9a-f]{24}$")
        self.assertEqual(snapshot["unread"], 1)
        self.assertEqual(snapshot["queued"], 0)
        self.assertEqual(snapshot["counts"]["superseded"], 1)
        self.assertEqual(snapshot["items"][0]["id"], second["id"])
        self.assertNotEqual(snapshot["items"][0]["id"], first["id"])
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_schema_rejects_unknown_type_and_nonfinite_or_oversized_data(self):
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.ledger.emit("advertisement", title="no")
        with self.assertRaisesRegex(ValueError, "finite JSON"):
            self.ledger.emit("attention", title="no", correlation={"x": float("nan")})
        with self.assertRaisesRegex(ValueError, "too large"):
            self.ledger.emit("attention", title="no", action={"payload": "x" * 9000})

    def test_compact_telegram_formatter_carries_partial_result_and_continuation(self):
        item = self.ledger.emit(
            "run_result",
            title="Деплой завершён частично",
            outcome="partial",
            result="Сервис собран, но live healthcheck не прошёл.",
            reason="Удалённый процесс не поднялся после перезапуска.",
            expectation="Проверить журнал службы и продолжить с сохранённого run.",
            action={"label": "Открыть run", "domain": "praxis", "action": "run.open"},
        )
        rendered = owner_delivery.format_telegram(item)
        self.assertIn("Статус: частично", rendered)
        self.assertIn("Результат:", rendered)
        self.assertIn("Почему:", rendered)
        self.assertIn("Дальше:", rendered)
        self.assertIn("Действие: Открыть run", rendered)
        self.assertNotIn(item["id"], rendered)

    def test_followup_telegram_formatter_is_a_human_notification(self):
        item = self.ledger.emit(
            "followup_answer",
            title="Вася ответил",
            body="Страница открывается, нужен доступ в админку.",
            outcome="success",
            reason="Пришёл ответ в отслеживаемой Telegram-нити.",
            expectation="Прочитать ответ и решить, нужно ли продолжение.",
            action={"label": "Открыть нить", "domain": "telegram", "action": "followup.open"},
        )
        rendered = owner_delivery.format_telegram(item)
        self.assertEqual(
            rendered,
            "↩️ Вася ответил:\nСтраница открывается, нужен доступ в админку.",
        )
        self.assertNotIn("Статус:", rendered)
        self.assertNotIn("Почему:", rendered)
        self.assertNotIn("Дальше:", rendered)
        self.assertNotIn("Действие:", rendered)

    def test_praxis_snapshot_and_device_command_share_the_same_inbox(self):
        service = praxis_app.PraxisAppService(
            Path(self.temp.name) / "app", owner_id=123,
            body_probe=lambda **_kwargs: {"ok": False},
        )
        item = service.deliveries.emit(
            "attention", title="Нужно посмотреть", dedupe_key="attention:one",
        )
        service._body_status = lambda: {
            "configured": False, "online": False, "state": "not_configured",
        }
        service._inventory = lambda _viewer: {"available": False}
        service._computer_evidence = lambda _viewer: []
        service._runs_snapshot = lambda: {"items": [], "counts": {}}
        service._telegram = lambda: {
            "rooms": [], "followups": [], "membership": [], "pending_followups": 0,
        }
        service._memory_health = lambda: {"maps": [], "index": {}}
        service._system = lambda: {"api": "v1"}
        owner = service.viewer(123)
        snapshot = service.snapshot(owner)
        self.assertEqual(snapshot["inbox"]["items"][0]["id"], item["id"])
        self.assertEqual(snapshot["now"]["inbox_unread"], 1)

        device = praxis_app.Viewer("dev_test", "device", ("praxis.snapshot",))
        with self.assertRaisesRegex(ValueError, "positive integer"):
            service.command(device, {
                "domain": "inbox", "action": "read", "delivery_id": item["id"],
                "expected_revision": True,
            })
        result = service.command(device, {
            "domain": "inbox", "action": "read", "delivery_id": item["id"],
            "expected_revision": item["revision"],
        })
        self.assertEqual(result["delivery"]["status"], "read")

    def test_praxis_projection_does_not_turn_corruption_into_empty_inbox(self):
        service = praxis_app.PraxisAppService(
            Path(self.temp.name) / "corrupt-app", owner_id=123,
            body_probe=lambda **_kwargs: {"ok": False},
        )
        service.deliveries.emit("attention", title="must remain visible")
        with service.deliveries.path.open("a", encoding="utf-8") as target:
            target.write("not-json-but-committed\n")
        with self.assertRaises(owner_delivery.OwnerDeliveryCorruption):
            service._inbox()

    def test_snapshot_history_limit_never_hides_old_actionable_items(self):
        queued = self.ledger.emit(
            "attention", title="old queued", dedupe_key="old-queued",
        )
        read = self.ledger.emit(
            "attention", title="old read", dedupe_key="old-read",
        )
        read = self.ledger.transition(
            read["id"], "read", expected_revision=read["revision"],
        )
        for index in range(14):
            item = self.ledger.emit(
                "run_result", title=f"acted {index}", dedupe_key=f"acted:{index}",
            )
            self.ledger.transition(
                item["id"], "acted", expected_revision=item["revision"],
            )
        snapshot = self.ledger.snapshot(limit=5)
        ids = [item["id"] for item in snapshot["items"]]
        self.assertEqual(len(ids), 5)
        self.assertIn(queued["id"], ids)
        self.assertIn(read["id"], ids)
        self.assertEqual(snapshot["unread"], 1)

        for index in range(6):
            self.ledger.emit(
                "attention", title=f"overflow {index}", dedupe_key=f"overflow:{index}",
            )
        overflow = self.ledger.snapshot(limit=5)
        self.assertGreater(len(overflow["items"]), 5)
        self.assertTrue(all(
            item["status"] in {"queued", "delivered", "read"}
            for item in overflow["items"]
        ))


if __name__ == "__main__":
    unittest.main()
