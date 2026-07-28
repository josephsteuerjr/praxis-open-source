from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path

from telegram_outbox import (
    TelegramOutbox,
    TelegramOutboxConflict,
    TelegramOutboxIntegrityError,
    TelegramOutboxSecurityError,
    TelegramOutboxValidationError,
    stable_random_id,
)


class Clock:
    def __init__(self, value: float = 1_700_000_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class TelegramOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "outbox"
        self.clock = Clock()
        self.box = TelegramOutbox(self.root, clock=self.clock)

    @staticmethod
    def text_args(**overrides):
        values = {
            "peer_id": -1009990000003,
            "topic_id": 81,
            "reply_to": 93,
            "text": "точный текст",
            "run_id": "run-01",
            "call_id": "call-02",
            "purpose": "scheduled-followup",
        }
        values.update(overrides)
        return values

    def test_stable_random_id_is_nonzero_signed_int64_from_exact_key(self) -> None:
        first = stable_random_id("run-1:call-2:chunk-0")
        self.assertEqual(first, stable_random_id("run-1:call-2:chunk-0"))
        self.assertNotEqual(first, stable_random_id("run-1:call-2:chunk-1"))
        self.assertNotEqual(first, 0)
        self.assertGreaterEqual(first, -(1 << 63))
        self.assertLessEqual(first, (1 << 63) - 1)
        with self.assertRaises(TelegramOutboxValidationError):
            stable_random_id("bad\nkey")

    def test_text_intent_is_exact_restart_safe_and_idempotent(self) -> None:
        first = self.box.prepare_text("owner-key-1", **self.text_args())
        second = self.box.prepare_text("owner-key-1", **self.text_args())
        self.assertEqual(first, second)
        self.assertEqual(first["state"], "pending")
        self.assertEqual(first["peer_id"], -1009990000003)
        self.assertEqual(first["topic_id"], 81)
        self.assertEqual(first["reply_to"], 93)
        self.assertEqual(first["payload"], {"text": "точный текст"})
        self.assertEqual(first["run_id"], "run-01")
        self.assertEqual(first["call_id"], "call-02")
        self.assertEqual(first["purpose"], "scheduled-followup")
        restarted = TelegramOutbox(self.root, clock=self.clock)
        self.assertEqual(restarted.get("owner-key-1"), first)
        self.assertEqual(restarted.pending(), (first,))
        with self.assertRaises(TelegramOutboxConflict):
            restarted.prepare_text("owner-key-1", **self.text_args(text="other"))

    def test_concurrent_same_key_has_one_intent(self) -> None:
        other = TelegramOutbox(self.root, clock=self.clock)
        results: list[dict] = []
        errors: list[BaseException] = []

        def prepare(box: TelegramOutbox) -> None:
            try:
                results.append(box.prepare_text("shared-key", **self.text_args()))
            except BaseException as exc:  # test captures a thread failure for assertion
                errors.append(exc)

        threads = [threading.Thread(target=prepare, args=(box,)) for box in (self.box, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        journal = next((self.root / "entries").glob("*.jsonl"))
        self.assertEqual(len(journal.read_text(encoding="utf-8").splitlines()), 1)

    def test_acceptance_receipt_is_fsynced_idempotent_and_immutable(self) -> None:
        intent = self.box.prepare_text("accepted-key", **self.text_args())
        accepted = self.box.mark_accepted(
            "accepted-key", message_id=777, random_id=intent["random_id"]
        )
        self.assertEqual(accepted["state"], "accepted")
        self.assertEqual(
            accepted["receipt"], {"message_id": 777, "random_id": intent["random_id"]}
        )
        self.assertEqual(self.box.pending(), ())
        restarted = TelegramOutbox(self.root, clock=self.clock)
        self.assertEqual(restarted.get("accepted-key"), accepted)
        self.assertEqual(restarted.accepted(), (accepted,))
        self.assertEqual(
            restarted.mark_accepted("accepted-key", message_id=777), accepted
        )
        with self.assertRaises(TelegramOutboxConflict):
            restarted.mark_accepted("accepted-key", message_id=778)

    def test_retry_backoff_is_bounded_then_retained_as_dead_letter(self) -> None:
        box = TelegramOutbox(
            self.root,
            clock=self.clock,
            max_attempts=3,
            base_backoff_seconds=2,
            max_backoff_seconds=3,
        )
        box.prepare_text("retry-key", **self.text_args())
        first = box.record_retry("retry-key", "temporary", now=100)
        self.assertEqual(first["attempts"], 1)
        self.assertEqual(first["next_attempt_at"], 102)
        self.assertEqual(box.pending(due_only=True, now=101), ())
        self.assertEqual(len(box.pending(due_only=True, now=102)), 1)
        second = box.record_retry("retry-key", "again", now=101)
        self.assertEqual(second["attempts"], 2)
        self.assertEqual(second["next_attempt_at"], 104)
        dead = box.record_retry("retry-key", "exhausted\ntrace", now=102)
        self.assertEqual(dead["state"], "dead_letter")
        self.assertEqual(dead["attempts"], 3)
        self.assertEqual(dead["last_error"], "exhausted\ntrace")
        self.assertEqual(box.pending(), ())
        self.assertEqual(box.dead_letters(), (dead,))
        restarted = TelegramOutbox(
            self.root,
            clock=self.clock,
            max_attempts=3,
            base_backoff_seconds=2,
            max_backoff_seconds=3,
        )
        self.assertEqual(restarted.dead_letters(), (dead,))
        requeued = restarted.requeue("retry-key", reason="owner requested retry")
        self.assertEqual(requeued["state"], "pending")
        self.assertEqual(requeued["attempts"], 0)

    def test_explicit_dead_letter_keeps_intent_and_late_receipt_wins(self) -> None:
        intent = self.box.prepare_text("manual-dead", **self.text_args())
        dead = self.box.dead_letter("manual-dead", "operator stopped delivery")
        self.assertEqual(dead["state"], "dead_letter")
        self.assertEqual(self.box.get("manual-dead"), dead)
        accepted = self.box.mark_accepted(
            "manual-dead", message_id=901, random_id=intent["random_id"]
        )
        self.assertEqual(accepted["state"], "accepted")
        self.assertEqual(self.box.dead_letters(), ())

    def test_file_is_copied_with_exact_delivery_metadata_and_sha(self) -> None:
        source = Path(self.temp.name) / "source.bin"
        original = b"PK\x03\x04durable document bytes"
        source.write_bytes(original)
        first = self.box.prepare_file(
            "file-key",
            peer_id="-1009990000004",
            topic_id=None,
            reply_to="55",
            source=source,
            visible_filename="Итоговый отчёт.docx",
            mime="Application/Vnd.Openxmlformats-Officedocument.Wordprocessingml.Document",
            caption="готово",
            run_id="run-file",
            call_id="call-file",
            purpose="owner-requested-file",
        )
        payload = first["payload"]
        staged = Path(payload["staged_path"])
        self.assertNotEqual(staged, source)
        self.assertEqual(staged.read_bytes(), original)
        self.assertEqual(payload["visible_filename"], "Итоговый отчёт.docx")
        self.assertEqual(
            payload["mime"],
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertEqual(payload["caption"], "готово")
        self.assertEqual(payload["size"], len(original))
        self.assertEqual(payload["sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(
            self.box.prepare_file(
                "file-key",
                peer_id=-1009990000004,
                topic_id=None,
                reply_to=55,
                source=source,
                visible_filename="Итоговый отчёт.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                caption="готово",
                run_id="run-file",
                call_id="call-file",
                purpose="owner-requested-file",
            ),
            first,
        )
        source.write_bytes(b"different")
        with self.assertRaises(TelegramOutboxConflict):
            self.box.prepare_file(
                "file-key", peer_id=-1009990000004, source=source,
                visible_filename="Итоговый отчёт.docx", mime=payload["mime"], caption="готово",
                run_id="run-file", call_id="call-file", purpose="owner-requested-file",
                reply_to=55,
            )
        self.assertEqual(staged.read_bytes(), original)

    def test_staged_file_tamper_fails_closed_but_record_is_not_deleted(self) -> None:
        source = Path(self.temp.name) / "proof.txt"
        source.write_text("proof", encoding="utf-8")
        intent = self.box.prepare_file(
            "tamper-key", peer_id=123, source=source,
            visible_filename="proof.txt", mime="text/plain", caption="",
            run_id="run", call_id="call", purpose="send-proof",
        )
        Path(intent["payload"]["staged_path"]).write_text("tampered", encoding="utf-8")
        with self.assertRaises(TelegramOutboxIntegrityError):
            self.box.pending()
        retained = self.box.get("tamper-key", verify_file=False)
        self.assertIsNotNone(retained)
        self.assertEqual(retained["state"], "pending")

    def test_unterminated_tail_is_quarantined_and_last_valid_intent_survives(self) -> None:
        intent = self.box.prepare_text("torn-key", **self.text_args())
        journal = next((self.root / "entries").glob("*.jsonl"))
        fragment = b'{"schema":"torn"'
        with journal.open("ab") as stream:
            stream.write(fragment)
            stream.flush()
        restarted = TelegramOutbox(self.root, clock=self.clock)
        self.assertEqual(restarted.get("torn-key"), intent)
        records = restarted.quarantine_records()
        self.assertTrue(any(row["reason"] == "unterminated crash tail" for row in records))
        artifacts = [self.root / "quarantine" / row["artifact"] for row in records]
        self.assertTrue(any(path.read_bytes() == fragment for path in artifacts))

    def test_invalid_complete_event_is_quarantined_without_hiding_intent(self) -> None:
        intent = self.box.prepare_text("corrupt-line", **self.text_args())
        journal = next((self.root / "entries").glob("*.jsonl"))
        with journal.open("ab") as stream:
            stream.write(b'{"schema":"wrong"}\n')
            stream.flush()
        self.assertEqual(self.box.get("corrupt-line"), intent)
        self.assertTrue(
            any("invalid event" in row["reason"] for row in self.box.quarantine_records())
        )
        self.assertEqual(len(journal.read_text(encoding="utf-8").splitlines()), 1)

    def test_recovery_quarantines_orphan_staging_instead_of_deleting_it(self) -> None:
        orphan = self.root / "files" / ("outbox-" + "a" * 64 + ".blob")
        orphan.write_bytes(b"orphan evidence")
        result = self.box.recover()
        self.assertGreaterEqual(result["quarantined"], 1)
        self.assertFalse(orphan.exists())
        records = self.box.quarantine_records()
        artifacts = [self.root / "quarantine" / row["artifact"] for row in records]
        self.assertTrue(any(path.read_bytes() == b"orphan evidence" for path in artifacts))

    def test_strict_ids_names_mime_source_and_utf8_byte_limit(self) -> None:
        with self.assertRaises(TelegramOutboxValidationError):
            self.box.prepare_text("bad-peer", **self.text_args(peer_id=0))
        with self.assertRaises(TelegramOutboxValidationError):
            self.box.prepare_text("bad-topic", **self.text_args(topic_id=-1))
        with self.assertRaises(TelegramOutboxValidationError):
            self.box.prepare_text("bad-run", **self.text_args(run_id="run\nother"))
        tiny = TelegramOutbox(Path(self.temp.name) / "tiny", max_text_bytes=3)
        with self.assertRaises(TelegramOutboxValidationError):
            tiny.prepare_text("utf8", **self.text_args(text="яя"))
        source = Path(self.temp.name) / "file.txt"
        source.write_text("x", encoding="utf-8")
        with self.assertRaises(TelegramOutboxValidationError):
            self.box.prepare_file(
                "bad-name", peer_id=1, source=source, visible_filename="../file.txt",
                mime="text/plain", caption="", run_id="r", call_id="c", purpose="p",
            )
        with self.assertRaises(TelegramOutboxValidationError):
            self.box.prepare_file(
                "bad-mime", peer_id=1, source=source, visible_filename="file.txt",
                mime="not-a-mime", caption="", run_id="r", call_id="c", purpose="p",
            )
        symlink = Path(self.temp.name) / "link.txt"
        try:
            symlink.symlink_to(source)
        except OSError:
            return
        with self.assertRaises(TelegramOutboxSecurityError):
            self.box.prepare_file(
                "bad-link", peer_id=1, source=symlink, visible_filename="file.txt",
                mime="text/plain", caption="", run_id="r", call_id="c", purpose="p",
            )


if __name__ == "__main__":
    unittest.main()
