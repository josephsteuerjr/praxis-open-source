"""Hermetic tests for the typed, scope-isolated media spool.

Run with:  python praxis_test.py test_media -v
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest import mock

import media


def jpeg(size: int = 16) -> bytes:
    return b"\xff\xd8\xff\xe0" + b"J" * max(0, size - 4)


def ogg(size: int = 16) -> bytes:
    return b"OggS" + b"O" * max(0, size - 4)


class MediaBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_media_"))
        self.root = self.tmp / "workspace" / "media"
        self.spool = media.MediaSpool(
            self.root,
            photo_max_bytes=1024,
            audio_max_bytes=2048,
            document_max_bytes=4096,
            max_total_bytes=8192,
            ttl_seconds=60,
            max_queue=4,
            max_turn_media=4,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_photo(
        self,
        *,
        chat_id: int | str = 777,
        message_id: int | str = 10,
        scope: str = "owner",
        caption: str = "caption",
    ) -> media.MediaRef:
        return self.spool.ingest_bytes(
            jpeg(),
            kind="photo",
            filename="photo.jpeg",
            chat_id=chat_id,
            message_id=message_id,
            scope=scope,
            caption=caption,
        )


class TestTypesAndSniffing(MediaBase):
    def test_refs_envelopes_and_outbound_are_frozen(self) -> None:
        ref = self.add_photo()
        with self.assertRaises(FrozenInstanceError):
            ref.caption = "changed"  # type: ignore[misc]

        outbound = self.spool.resolve_outbound(ref)
        with self.assertRaises(FrozenInstanceError):
            outbound.voice_note = True  # type: ignore[misc]

        turn = media.TurnEnvelope(text="ok", outbound=[outbound], media=[ref])
        self.assertIsInstance(turn.outbound, tuple)
        self.assertIsInstance(turn.media, tuple)
        self.assertTrue(turn.has_media)
        self.assertTrue(turn.has_outbound)
        with self.assertRaises(FrozenInstanceError):
            turn.text = "changed"  # type: ignore[misc]

    def test_magic_wins_over_extension(self) -> None:
        disguised = self.tmp / "voice.jpg"
        disguised.write_bytes(ogg())
        self.assertEqual(media.sniff_mime(disguised), "audio/ogg")
        self.assertEqual(media.media_kind(media.sniff_mime(disguised)), "audio")
        self.assertIsNone(media.sniff_mime(b"not media"))
        with self.assertRaises(media.UnsupportedMediaError):
            self.spool.ingest_path(
                disguised,
                kind="photo",
                chat_id=777,
                message_id=1,
                scope="owner",
            )

    def test_common_photo_and_audio_magic(self) -> None:
        cases = {
            b"\x89PNG\r\n\x1a\nrest": "image/png",
            b"GIF89arest": "image/gif",
            b"RIFF\x00\x00\x00\x00WEBPrest": "image/webp",
            b"RIFF\x00\x00\x00\x00WAVErest": "audio/wav",
            b"fLaCrest": "audio/flac",
            b"ID3rest": "audio/mpeg",
            b"\xff\xf1rest": "audio/aac",
        }
        for raw, expected in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(media.sniff_mime(raw), expected)

    def test_document_keeps_visible_name_and_survives_outbox_restart(self) -> None:
        source = self.tmp / "build report 1.txt"
        source.write_bytes(b"verified build output\n")
        queued = self.spool.queue_outbound(
            source,
            kind="document",
            target_chat_id=777,
            reply_to_message_id=42,
            scope="owner",
            caption="result",
        )
        self.assertEqual(queued.kind, "document")
        self.assertEqual(queued.mime, "application/octet-stream")
        self.assertTrue(queued.path.name.endswith("build_report_1.txt"))
        self.assertEqual(media.delivery_basename(queued.path), "build_report_1.txt")
        self.assertEqual(len(queued.sha256), 64)

        restarted = media.MediaSpool(
            self.root,
            photo_max_bytes=1024,
            audio_max_bytes=2048,
            document_max_bytes=4096,
            max_total_bytes=8192,
            ttl_seconds=60,
            max_queue=4,
            max_turn_media=4,
        )
        self.assertEqual(restarted.pending(), (queued,))

        queued.path.write_bytes(b"tampered build output\n")
        with self.assertRaisesRegex(media.MediaValidationError, "sha256"):
            restarted.validate_outbound(queued)

    def test_recognised_photo_cannot_be_disguised_as_document(self) -> None:
        disguised = self.tmp / "payload.bin"
        disguised.write_bytes(jpeg())
        with self.assertRaises(media.UnsupportedMediaError):
            self.spool.resolve_outbound(
                disguised,
                kind="document",
                target_chat_id=777,
                scope="owner",
            )


class TestNamesAndContainment(MediaBase):
    def test_basename_strips_paths_and_reserved_names(self) -> None:
        self.assertEqual(media.safe_basename(r"..\..\folder\photo 1?.jpg"), "photo_1_.jpg")
        self.assertEqual(media.safe_basename("CON.jpg"), "_CON.jpg")
        self.assertEqual(media.safe_basename("../../"), "media")

    def test_contained_path_refuses_traversal(self) -> None:
        inside = media.contained_path(self.root, "inbound/file")
        self.assertTrue(inside.is_relative_to(self.root.resolve()))
        with self.assertRaises(media.MediaSecurityError):
            media.contained_path(self.root, "../escape.jpg")
        with self.assertRaises(media.MediaSecurityError):
            media.contained_path(self.root, self.tmp / "outside.jpg")

    def test_ingest_uses_scope_chat_and_canonical_extension(self) -> None:
        source = self.tmp / "wrong.exe"
        source.write_bytes(jpeg())
        ref = self.spool.ingest_path(
            source,
            kind="photo",
            chat_id=-100123,
            message_id=55,
            scope="group",
            caption="hello",
            filename="../../payload.exe",
        )
        rel = ref.path.relative_to(self.root.resolve())
        self.assertEqual(rel.parts[:2], ("inbound", "group"))
        self.assertTrue(rel.parts[2].startswith("chat-"))
        self.assertEqual(ref.path.suffix, ".jpg")
        self.assertNotIn("payload.exe", ref.path.name)
        self.assertTrue(source.exists(), "copy is the safe default")
        self.assertIs(self.spool.validate_ref(ref), ref)

    def test_path_and_metadata_cannot_be_rebound(self) -> None:
        ref = self.add_photo()
        with self.assertRaises(media.MediaSecurityError):
            self.spool.validate_ref(replace(ref, scope="group"))
        with self.assertRaises(media.MediaSecurityError):
            self.spool.validate_ref(replace(ref, chat_id=778))
        with self.assertRaises(media.MediaSecurityError):
            self.spool.validate_ref(ref, expected_scope="group")

        outside = self.tmp / "outside.jpg"
        outside.write_bytes(jpeg())
        escaped = replace(ref, path=outside, size=outside.stat().st_size)
        with self.assertRaises(media.MediaSecurityError):
            self.spool.validate_ref(escaped)

    def test_magic_and_size_are_rechecked(self) -> None:
        ref = self.add_photo()
        with self.assertRaises(media.MediaValidationError):
            self.spool.validate_ref(replace(ref, mime="image/png"))
        ref.path.write_bytes(b"X" * ref.size)
        with self.assertRaises(media.MediaValidationError):
            self.spool.validate_ref(ref)


class TestCapsAndTurns(MediaBase):
    def test_per_file_and_total_caps(self) -> None:
        tiny = media.MediaSpool(
            self.tmp / "tiny",
            photo_max_bytes=10,
            audio_max_bytes=10,
            max_total_bytes=15,
            ttl_seconds=10,
            max_queue=2,
            max_turn_media=2,
        )
        with self.assertRaises(media.MediaTooLargeError):
            tiny.ingest_bytes(
                jpeg(11), kind="photo", filename="big.jpg",
                chat_id=1, message_id=1, scope="owner",
            )
        tiny.ingest_bytes(
            jpeg(8), kind="photo", filename="one.jpg",
            chat_id=1, message_id=1, scope="owner",
        )
        with self.assertRaises(media.MediaTooLargeError):
            tiny.ingest_bytes(
                jpeg(8), kind="photo", filename="two.jpg",
                chat_id=1, message_id=2, scope="owner",
            )

    def test_turn_rejects_empty_mixed_chat_and_over_cap(self) -> None:
        with self.assertRaises(media.MediaValidationError):
            self.spool.validate_turn(media.TurnEnvelope())
        first = self.add_photo(chat_id=1, message_id=1)
        second = self.add_photo(chat_id=2, message_id=2)
        with self.assertRaises(media.MediaSecurityError):
            self.spool.envelope(media=(first, second))

        one_only = media.MediaSpool(
            self.tmp / "one-only",
            photo_max_bytes=1024,
            audio_max_bytes=1024,
            max_total_bytes=4096,
            ttl_seconds=60,
            max_queue=2,
            max_turn_media=1,
        )
        a = one_only.ingest_bytes(
            jpeg(), kind="photo", filename="a.jpg",
            chat_id=1, message_id=1, scope="owner",
        )
        b = one_only.ingest_bytes(
            jpeg(), kind="photo", filename="b.jpg",
            chat_id=1, message_id=2, scope="owner",
        )
        with self.assertRaises(media.MediaValidationError):
            one_only.envelope(media=(a, b))


class TestOutboundQueue(MediaBase):
    def test_resolve_is_not_send_or_queue(self) -> None:
        source = self.tmp / "answer.any"
        source.write_bytes(ogg())
        item = self.spool.resolve_outbound(
            source,
            kind="audio",
            target_chat_id=-100,
            reply_to_message_id=42,
            scope="group",
            caption="listen",
            voice_note=True,
        )
        self.assertEqual(self.spool.pending(), ())
        self.assertEqual(item.target_chat_id, -100)
        self.assertEqual(item.chat_id, -100)
        self.assertEqual(item.reply_to_message_id, 42)
        self.assertEqual(item.message_id, 42)
        self.assertTrue(item.voice_note)
        self.assertEqual(item.path.suffix, ".ogg")
        self.assertEqual(item.path.relative_to(self.root.resolve()).parts[0], "outbound")
        self.assertIs(self.spool.validate_outbound(item), item)

    def test_queue_validates_and_never_sends(self) -> None:
        source = self.tmp / "answer.ogg"
        source.write_bytes(ogg())
        item = self.spool.queue_outbound(
            source,
            kind="audio",
            target_chat_id=777,
            reply_to_message_id=3,
            scope="owner",
            voice_note=True,
        )
        self.assertEqual(self.spool.pending(), (item,))
        self.assertEqual(self.spool.peek(), item)
        self.assertEqual(self.spool.dequeue(), item)
        self.assertIsNone(self.spool.dequeue())
        self.assertEqual(self.spool.pending(), (item,),
                         "a local claim must not erase durable delivery intent")

        self.assertTrue(self.spool.discard(item.queue_id))
        self.assertEqual(self.spool.pending(), ())
        self.assertEqual(self.spool.outbox_results("delivered")[0]["queue_id"],
                         item.queue_id)
        with self.assertRaises(media.MediaValidationError):
            self.spool.enqueue(item)
        self.assertFalse(self.spool.discard(item.queue_id))

    def test_pending_outbox_survives_restart_with_every_delivery_field(self) -> None:
        source = self.tmp / "restart.ogg"
        source.write_bytes(ogg())
        item = self.spool.resolve_outbound(
            source,
            kind="audio",
            target_chat_id=-100777,
            reply_to_message_id=91,
            scope="group",
            caption="точная подпись",
            voice_note=True,
        )
        item = replace(item, run_id="run-restart-proof")
        self.spool.enqueue(item)

        restored_spool = media.MediaSpool(
            self.root,
            photo_max_bytes=1024,
            audio_max_bytes=2048,
            max_total_bytes=8192,
            ttl_seconds=60,
            max_queue=4,
            max_turn_media=4,
        )
        restored = restored_spool.pending()
        self.assertEqual(len(restored), 1)
        recovered = restored[0]
        self.assertEqual(recovered, item)
        self.assertEqual(recovered.kind, "audio")
        self.assertEqual(recovered.path, item.path)
        self.assertEqual(recovered.mime, "audio/ogg")
        self.assertEqual(recovered.size, item.size)
        self.assertEqual(recovered.target_chat_id, -100777)
        self.assertEqual(recovered.scope, "group")
        self.assertEqual(recovered.caption, "точная подпись")
        self.assertEqual(recovered.reply_to_message_id, 91)
        self.assertTrue(recovered.voice_note)
        self.assertEqual(recovered.queue_id, item.queue_id)
        self.assertEqual(recovered.run_id, "run-restart-proof")

        self.assertTrue(restored_spool.discard(
            recovered.queue_id, receipt={"message_id": 991, "random_id": 123},
        ))
        third = media.MediaSpool(
            self.root,
            photo_max_bytes=1024,
            audio_max_bytes=2048,
            max_total_bytes=8192,
            ttl_seconds=60,
            max_queue=4,
            max_turn_media=4,
        )
        self.assertEqual(third.pending(), ())
        result = third.outbox_results("delivered")[0]
        self.assertEqual(result["item"]["run_id"], "run-restart-proof")
        self.assertEqual(result["item"]["caption"], "точная подпись")
        self.assertEqual(result["result"]["message_id"], 991)
        self.assertEqual(result["result"]["random_id"], 123)

    def test_receipt_tombstone_wins_after_sender_removed_uploaded_file(self) -> None:
        item = self.spool.queue_outbound_bytes(
            ogg(), kind="audio", filename="uploaded.ogg", target_chat_id=777,
            reply_to_message_id=8, scope="owner", voice_note=True,
        )
        item.path.unlink()  # current sender removes its copy immediately after acceptance

        self.assertTrue(self.spool.discard(item.queue_id))
        self.assertEqual(self.spool.pending(), ())
        result = self.spool.outbox_results("delivered")[0]
        self.assertEqual(result["queue_id"], item.queue_id)
        self.assertEqual(result["result"]["status"], "delivered")

    def test_two_spool_instances_serialize_concurrent_enqueues(self) -> None:
        other = media.MediaSpool(
            self.root,
            photo_max_bytes=1024,
            audio_max_bytes=2048,
            max_total_bytes=8192,
            ttl_seconds=60,
            max_queue=4,
            max_turn_media=4,
        )
        first = self.spool.resolve_outbound_bytes(
            ogg(), kind="audio", filename="first.ogg", target_chat_id=1,
            reply_to_message_id=1, scope="owner", voice_note=True,
        )
        second = other.resolve_outbound_bytes(
            ogg(), kind="audio", filename="second.ogg", target_chat_id=1,
            reply_to_message_id=2, scope="owner", voice_note=True,
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.spool.enqueue, first),
                       pool.submit(other.enqueue, second)]
            for future in futures:
                future.result(timeout=10)

        restored = media.MediaSpool(
            self.root,
            photo_max_bytes=1024,
            audio_max_bytes=2048,
            max_total_bytes=8192,
            ttl_seconds=60,
            max_queue=4,
            max_turn_media=4,
        )
        self.assertEqual({item.queue_id for item in restored.pending()},
                         {first.queue_id, second.queue_id})

    @unittest.skipUnless(os.name == "nt", "Windows-specific no-signal regression")
    def test_windows_lock_contention_never_signals_the_owner(self) -> None:
        other = media.MediaSpool(
            self.root,
            photo_max_bytes=1024,
            audio_max_bytes=2048,
            max_total_bytes=8192,
            ttl_seconds=60,
            max_queue=4,
            max_turn_media=4,
        )
        first = self.spool.resolve_outbound_bytes(
            ogg(), kind="audio", filename="safe-first.ogg", target_chat_id=1,
            reply_to_message_id=1, scope="owner", voice_note=True,
        )
        second = other.resolve_outbound_bytes(
            ogg(), kind="audio", filename="safe-second.ogg", target_chat_id=1,
            reply_to_message_id=2, scope="owner", voice_note=True,
        )
        entered = threading.Event()
        release = threading.Event()

        def hold_ledger() -> None:
            with self.spool._ledger_guard():
                entered.set()
                self.assertTrue(release.wait(timeout=5))

        with mock.patch("process_liveness.os.kill", side_effect=AssertionError(
                "Windows lock contention must never call os.kill")):
            with ThreadPoolExecutor(max_workers=2) as pool:
                holder = pool.submit(hold_ledger)
                self.assertTrue(entered.wait(timeout=5))
                waiter = pool.submit(other.enqueue, second)
                time.sleep(0.05)
                release.set()
                holder.result(timeout=5)
                waiter.result(timeout=15)
            self.spool.enqueue(first)

        self.assertEqual(len(media.MediaSpool(
            self.root,
            photo_max_bytes=1024,
            audio_max_bytes=2048,
            max_total_bytes=8192,
            ttl_seconds=60,
            max_queue=4,
            max_turn_media=4,
        ).pending()), 2)

    def test_unacknowledged_claim_is_recovered_by_a_fresh_spool(self) -> None:
        item = self.spool.queue_outbound_bytes(
            ogg(), kind="audio", filename="claimed.ogg", target_chat_id=7,
            reply_to_message_id=3, scope="owner", voice_note=True,
        )
        self.assertEqual(self.spool.dequeue(), item)
        self.assertIsNone(self.spool.dequeue())

        restarted = media.MediaSpool(
            self.root,
            photo_max_bytes=1024,
            audio_max_bytes=2048,
            max_total_bytes=8192,
            ttl_seconds=60,
            max_queue=4,
            max_turn_media=4,
        )
        self.assertEqual(restarted.dequeue(), item)
        self.assertTrue(restarted.discard(item.queue_id))
        self.assertEqual(media.MediaSpool(
            self.root,
            photo_max_bytes=1024,
            audio_max_bytes=2048,
            max_total_bytes=8192,
            ttl_seconds=60,
            max_queue=4,
            max_turn_media=4,
        ).pending(), ())

    def test_failed_run_can_terminally_remove_pending_upload(self) -> None:
        item = self.spool.queue_outbound_bytes(
            ogg(), kind="audio", filename="stop.ogg", target_chat_id=7,
            reply_to_message_id=3, scope="owner", voice_note=True,
        )
        self.assertTrue(self.spool.fail(item.queue_id, reason="run failed before retry"))
        self.assertEqual(self.spool.pending(), ())
        result = self.spool.outbox_results("failed")[0]
        self.assertEqual(result["queue_id"], item.queue_id)
        self.assertIn("run failed", result["result"]["reason"])

    def test_ownerless_partial_lock_is_recovered(self) -> None:
        lock = self.root / "outbox-ledger" / ".lock"
        lock.write_bytes(b"")
        old = time.time() - 2
        os.utime(lock, (old, old))

        recovered = media.MediaSpool(
            self.root,
            photo_max_bytes=1024,
            audio_max_bytes=2048,
            max_total_bytes=8192,
            ttl_seconds=60,
            max_queue=4,
            max_turn_media=4,
        )
        self.assertEqual(recovered.pending(), ())
        self.assertFalse(lock.exists())

    def test_queue_is_bounded_and_ref_cannot_be_forwarded_implicitly(self) -> None:
        ref = self.add_photo()
        with self.assertRaises(media.MediaSecurityError):
            self.spool.resolve_outbound(ref, target_chat_id=999)
        with self.assertRaises(media.MediaSecurityError):
            self.spool.resolve_outbound(ref, scope="group")
        with self.assertRaises(media.MediaValidationError):
            self.spool.resolve_outbound(ref, voice_note=True)

        bounded = media.MediaSpool(
            self.tmp / "bounded",
            photo_max_bytes=1024,
            audio_max_bytes=1024,
            max_total_bytes=4096,
            ttl_seconds=60,
            max_queue=1,
            max_turn_media=2,
        )
        bounded.queue_outbound_bytes(
            ogg(), kind="audio", filename="one.ogg", target_chat_id=1,
            reply_to_message_id=1, scope="owner", voice_note=True,
        )
        with self.assertRaises(media.MediaQueueFullError):
            bounded.queue_outbound_bytes(
                ogg(), kind="audio", filename="two.ogg", target_chat_id=1,
                reply_to_message_id=2, scope="owner", voice_note=True,
            )

    def test_turn_carries_explicit_guardable_outbound(self) -> None:
        item = self.spool.resolve_outbound_bytes(
            ogg(), kind="audio", filename="voice.ogg", target_chat_id=777,
            reply_to_message_id=8, scope="owner", voice_note=True,
        )
        turn = self.spool.envelope(
            text="reply",
            outbound=(item,),
            expected_scope="owner",
            expected_chat_id=777,
        )
        self.assertEqual(turn.outbound, (item,))
        with self.assertRaises(media.MediaSecurityError):
            self.spool.validate_turn(turn, expected_chat_id=778)

    def test_dequeue_revalidates_file(self) -> None:
        item = self.spool.queue_outbound_bytes(
            ogg(), kind="audio", filename="voice.ogg", target_chat_id=777,
            reply_to_message_id=8, scope="owner", voice_note=True,
        )
        item.path.write_bytes(b"X" * item.size)
        with self.assertRaises(media.MediaValidationError):
            self.spool.dequeue()
        self.assertEqual(self.spool.pending(), ())


class TestCleanup(MediaBase):
    def test_ttl_cleanup_removes_only_stale_managed_files_and_queue_entries(self) -> None:
        old = self.spool.queue_outbound_bytes(
            ogg(), kind="audio", filename="old.ogg", target_chat_id=1,
            reply_to_message_id=1, scope="owner", voice_note=True,
        )
        fresh = self.spool.queue_outbound_bytes(
            ogg(), kind="audio", filename="fresh.ogg", target_chat_id=1,
            reply_to_message_id=2, scope="owner", voice_note=True,
        )
        now = time.time()
        os.utime(old.path, (now - 120, now - 120))
        os.utime(fresh.path, (now, now))
        outside = self.tmp / "outside.ogg"
        outside.write_bytes(ogg())
        os.utime(outside, (now - 120, now - 120))

        removed = self.spool.cleanup(now=now, ttl_seconds=60)
        self.assertIn(old.path, removed)
        self.assertFalse(old.path.exists())
        self.assertTrue(fresh.path.exists())
        self.assertTrue(outside.exists(), "cleanup must not leave the managed root")
        self.assertEqual(self.spool.pending(), (fresh,))
        expired = self.spool.outbox_results("expired")
        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0]["queue_id"], old.queue_id)
        self.assertEqual(expired[0]["item"]["run_id"], old.run_id)
        self.assertIn("TTL", expired[0]["result"]["reason"])

        restarted = media.MediaSpool(
            self.root,
            photo_max_bytes=1024,
            audio_max_bytes=2048,
            max_total_bytes=8192,
            ttl_seconds=60,
            max_queue=4,
            max_turn_media=4,
        )
        self.assertEqual(restarted.pending(), (fresh,))
        self.assertEqual(restarted.outbox_results("expired")[0]["queue_id"], old.queue_id)

    def test_terminal_outbox_history_is_bounded_and_prune_is_observable(self) -> None:
        bounded = media.MediaSpool(
            self.tmp / "bounded-results",
            photo_max_bytes=1024,
            audio_max_bytes=2048,
            max_total_bytes=8192,
            ttl_seconds=3600,
            max_queue=8,
            max_turn_media=4,
            outbox_result_ttl_seconds=3600,
            outbox_result_max=2,
        )
        queue_ids: list[str] = []
        base_time = time.time()
        for index in range(4):
            item = bounded.queue_outbound_bytes(
                ogg(), kind="audio", filename=f"sent-{index}.ogg",
                target_chat_id=1, reply_to_message_id=index + 1,
                scope="owner", voice_note=True,
            )
            queue_ids.append(item.queue_id)
            self.assertTrue(bounded.discard(item.queue_id))
            record_path = bounded._record_path(item.queue_id)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["updated_at"] = base_time + index
            record_path.write_text(json.dumps(record), encoding="utf-8")

        live = bounded.queue_outbound_bytes(
            ogg(), kind="audio", filename="still-pending.ogg",
            target_chat_id=1, reply_to_message_id=99,
            scope="owner", voice_note=True,
        )
        removed = bounded.cleanup(now=base_time + 10, ttl_seconds=3600)

        remaining = bounded.outbox_results("delivered")
        self.assertEqual(len(remaining), 2)
        self.assertEqual({row["queue_id"] for row in remaining}, set(queue_ids[-2:]))
        self.assertEqual(bounded.pending(), (live,), "prune must never touch live intent")
        self.assertEqual(len([path for path in removed if path.suffix == ".json"]), 2)
        maintenance = bounded.outbox_maintenance()["last_prune"]
        self.assertEqual(maintenance["pruned"], 2)
        self.assertEqual(maintenance["pruned_by_count"], 2)
        self.assertEqual(maintenance["pruned_by_age"], 0)
        self.assertEqual(maintenance["retained_terminal"], 2)
        self.assertEqual(maintenance["error_count"], 0)

    def test_terminal_outbox_age_prune_never_removes_pending(self) -> None:
        bounded = media.MediaSpool(
            self.tmp / "aged-results",
            photo_max_bytes=1024,
            audio_max_bytes=2048,
            max_total_bytes=8192,
            ttl_seconds=3600,
            max_queue=4,
            max_turn_media=4,
            outbox_result_ttl_seconds=60,
            outbox_result_max=5000,
        )
        terminal = bounded.queue_outbound_bytes(
            ogg(), kind="audio", filename="old-result.ogg", target_chat_id=1,
            reply_to_message_id=1, scope="owner", voice_note=True,
        )
        self.assertTrue(bounded.discard(terminal.queue_id))
        pending = bounded.queue_outbound_bytes(
            ogg(), kind="audio", filename="pending.ogg", target_chat_id=1,
            reply_to_message_id=2, scope="owner", voice_note=True,
        )

        future = time.time() + 120
        bounded.cleanup(now=future, ttl_seconds=3600)

        self.assertEqual(bounded.outbox_results("delivered"), ())
        self.assertEqual(bounded.pending(), (pending,))
        maintenance = bounded.outbox_maintenance()["last_prune"]
        self.assertEqual(maintenance["pruned_by_age"], 1)
        self.assertEqual(maintenance["error_count"], 0)


if __name__ == "__main__":
    unittest.main()
