from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import telegram_confirmation as confirmation_module

from telegram_confirmation import (
    ConfirmationStore,
    CriticalIntentOrigin,
    CriticalChallengeError,
    CriticalChallengeStore,
    OwnerOrigin,
)
from telegram_registry import (
    ConfirmationBinding,
    CriticalConfirmation,
    TelegramAccountDispatcher,
    TelegramCapabilityRegistry,
)


class TelegramConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="praxis-tg-confirm-")
        self.path = Path(self.temp.name) / "confirmations.jsonl"
        self.now = 100.0
        self.counter = 0

        def token():
            self.counter += 1
            return f"proof-{self.counter:04d}-" + "x" * 32

        self.token = token
        self.binding = ConfirmationBinding(
            request_name="functions.auth.LogOutRequest",
            parameter_commitment="a" * 64,
            principal="101",
            scope="telegram.account",
        )
        self.store = ConfirmationStore(
            owner_id=101, path=self.path, ttl_seconds=60,
            clock=lambda: self.now, token_factory=self.token,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_issue_is_owner_only_bound_and_does_not_persist_secret(self):
        with self.assertRaises(PermissionError):
            self.store.issue(self.binding, principal=202)
        proof = self.store.issue(self.binding, principal=101)
        self.assertEqual(len(self.store.pending()), 1)
        persisted = self.path.read_text(encoding="utf-8")
        self.assertNotIn(proof.token, persisted)
        self.assertNotIn(hashlib.sha256(proof.token.encode("utf-8")).hexdigest(), persisted)
        self.assertNotIn(proof.token, repr(proof))

    def test_exact_proof_is_consumed_once_across_store_reopen_in_same_process(self):
        proof = self.store.issue(self.binding, principal=101)
        restarted = ConfirmationStore(
            owner_id=101, path=self.path, ttl_seconds=60,
            clock=lambda: self.now, token_factory=self.token,
        )
        self.assertTrue(restarted.verify_and_consume(proof, self.binding))
        self.assertFalse(restarted.verify_and_consume(proof, self.binding))
        self.assertEqual(restarted.pending(), [])

    def test_process_key_rotation_invalidates_unconsumed_proof_fail_closed(self):
        proof = self.store.issue(self.binding, principal=101)
        with mock.patch.object(
            confirmation_module, "_PROOF_COMMITMENT_KEY", os.urandom(32),
        ):
            restarted = ConfirmationStore(
                owner_id=101, path=self.path, ttl_seconds=60,
                clock=lambda: self.now, token_factory=self.token,
            )
            self.assertFalse(restarted.verify_and_consume(proof, self.binding))

    def test_mismatch_does_not_burn_proof_but_expiry_does_deny_it(self):
        proof = self.store.issue(self.binding, principal=101)
        wrong = ConfirmationBinding(
            request_name=self.binding.request_name,
            parameter_commitment="b" * 64,
            principal="101",
            scope="telegram.account",
        )
        self.assertFalse(self.store.verify_and_consume(proof, wrong))
        self.assertTrue(self.store.verify_and_consume(proof, self.binding))

        second = self.store.issue(self.binding, principal=101)
        self.now = 161.0
        self.assertFalse(self.store.verify_and_consume(second, self.binding))
        self.assertEqual(self.store.pending(), [])

    def test_cancel_is_durable_and_cannot_be_delegated(self):
        proof = self.store.issue(self.binding, principal=101)
        with self.assertRaises(PermissionError):
            self.store.cancel(proof, principal=202)
        self.assertTrue(self.store.cancel(proof, principal=101))
        self.assertFalse(self.store.cancel(proof, principal=101))
        self.assertFalse(self.store.verify_and_consume(proof, self.binding))

    def test_confirmation_object_carries_only_opaque_token(self):
        proof = self.store.issue(self.binding, principal=101)
        self.assertEqual(set(CriticalConfirmation.__dataclass_fields__), {"token"})
        self.assertTrue(self.store.verify_and_consume(proof, self.binding))

    def test_owner_can_issue_one_use_proof_bound_to_praxis_self(self):
        binding = ConfirmationBinding(
            request_name=self.binding.request_name,
            parameter_commitment=self.binding.parameter_commitment,
            principal="praxis:self",
            scope=self.binding.scope,
        )
        store = ConfirmationStore(
            owner_id=101, path=self.path, ttl_seconds=60,
            clock=lambda: self.now, token_factory=self.token,
            confirmable_principals=("praxis:self",),
        )
        proof = store.issue(binding, principal=101)
        self.assertTrue(store.verify_and_consume(proof, binding))
        self.assertFalse(store.verify_and_consume(proof, binding))

    def test_store_is_a_dispatcher_verifier_without_an_adapter(self):
        calls = []

        async def caller(request):
            calls.append(type(request).__name__)
            return {"logged_out": True}

        dispatcher = TelegramAccountDispatcher(
            caller=caller,
            entity_resolver=None,
            owner_id=101,
            registry=TelegramCapabilityRegistry(),
            confirmation_verifier=self.store.verify_and_consume,
        )
        binding = dispatcher.confirmation_binding(
            "functions.auth.LogOutRequest", {}, principal=101,
        )
        proof = self.store.issue(binding, principal=101)
        first = asyncio.run(dispatcher.dispatch(
            binding.request_name, {}, principal=101, confirmation=proof,
        ))
        second = asyncio.run(dispatcher.dispatch(
            binding.request_name, {}, principal=101, confirmation=proof,
        ))
        self.assertTrue(first.ok, first.to_dict())
        self.assertEqual(second.status, "denied")
        self.assertEqual(calls, ["LogOutRequest"])


class TelegramCriticalChallengeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="praxis-tg-critical-")
        self.path = Path(self.temp.name) / "critical-challenges.jsonl"
        self.now = 100.0
        self.binding = ConfirmationBinding(
            request_name="functions.auth.LogOutRequest",
            parameter_commitment="c" * 64,
            principal="101",
            scope="telegram.account",
        )
        self.store = CriticalChallengeStore(
            owner_id=101,
            path=self.path,
            ttl_seconds=30,
            clock=lambda: self.now,
            initiator_principals=("praxis:self",),
        )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def origin(
        *,
        run_id="run-prepare",
        message_id=11,
        principal_id="101",
        is_dm=True,
        raw_text="private owner request marker",
    ):
        return OwnerOrigin(
            run_id=run_id,
            chat_id="101",
            message_id=message_id,
            principal_id=principal_id,
            is_dm=is_dm,
            raw_text=raw_text,
        )

    def prepare(self, *, origin=None, parameters=None, idempotency_key="tool-call-1"):
        source = origin or self.origin()
        if isinstance(source, OwnerOrigin):
            source = CriticalIntentOrigin.from_owner(
                source, call_id=idempotency_key,
            )
        return self.store.prepare(
            self.binding,
            {"reason": "owner requested"} if parameters is None else parameters,
            origin=source,
            idempotency_key=idempotency_key,
        )

    def dispatch_receipt(self, challenge: dict, *, principal="101", secret="receipt-secret"):
        return {
            "action": "call",
            "receipt": {
                "receipt_id": "receipt-1",
                "status": "ok",
                "request_name": challenge["request_name"],
                "scope": challenge["scope"],
                "mode": "raw",
                "principal": principal,
                "parameter_commitment": "e" * 64,
                "submitted_parameters": None,
                "serialized_parameters": None,
                "result": None,
                "result_sha256": None,
                "identifiers": {},
                "error": None,
                "delivery_context": {
                    "challenge_id": challenge["challenge_id"],
                    "requested_by": principal,
                    "confirmed_by": "101",
                },
            },
        }

    def test_owner_origin_mapping_validates_and_hides_raw_text(self):
        origin = OwnerOrigin.from_mapping({
            "run_id": "run-1",
            "chat_id": 101,
            "message_id": "17",
            "principal_id": 101,
            "is_dm": True,
            "raw_text": "do not persist this owner text",
        })
        self.assertEqual(origin.message_id, 17)
        public = origin.public_dict()
        self.assertNotIn("raw_text", public)
        self.assertNotIn("raw_text_sha256", public)
        with self.assertRaises(TypeError):
            OwnerOrigin.from_mapping({**public, "is_dm": "true", "raw_text": "x"})
        with self.assertRaises(ValueError):
            self.origin(run_id="bad/run")
        with self.assertRaises(ValueError):
            self.origin(message_id=0)

    def test_prepare_is_sovereign_and_persists_no_raw_text_parameters_or_token(self):
        with self.assertRaises(PermissionError):
            self.prepare(origin=self.origin(is_dm=False))
        with self.assertRaises(PermissionError):
            self.prepare(origin=self.origin(principal_id="202"))

        owner_origin = self.origin(raw_text="unique owner request secret 81f4")
        challenge = self.prepare(origin=owner_origin)
        self.assertEqual(challenge["status"], "pending")
        self.assertEqual(challenge["request_name"], self.binding.request_name)
        self.assertTrue(challenge["exact_phrase"])
        self.assertNotIn("parameters", challenge)
        self.assertNotIn("principal", challenge)

        raw_ledger = self.path.read_text(encoding="utf-8")
        self.assertNotIn(owner_origin.raw_text, raw_ledger)
        self.assertNotIn("owner requested", raw_ledger)
        self.assertNotIn(
            hashlib.sha256(owner_origin.raw_text.encode("utf-8")).hexdigest(),
            raw_ledger,
        )
        secret_payload = json.dumps(
            {"reason": "owner requested"}, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertNotIn(hashlib.sha256(secret_payload).hexdigest(), raw_ledger)
        rows = [json.loads(line) for line in raw_ledger.splitlines()]
        persisted_keys = set()

        def collect_keys(value):
            if isinstance(value, dict):
                persisted_keys.update(value)
                for nested in value.values():
                    collect_keys(nested)
            elif isinstance(value, list):
                for nested in value:
                    collect_keys(nested)

        collect_keys(rows)
        self.assertNotIn("raw_text", persisted_keys)
        self.assertNotIn("parameters", persisted_keys)
        self.assertNotIn("token", persisted_keys)
        self.assertNotIn("proof", persisted_keys)
        self.assertNotIn("parameters_sha256", persisted_keys)
        self.assertNotIn("parameters_payload_sha256", persisted_keys)
        self.assertNotIn("raw_text_sha256", persisted_keys)
        self.assertNotIn("bytes", persisted_keys)
        sealed = tuple((self.path.parent / "telegram_critical_secrets").glob("*.sealed"))
        self.assertEqual(len(sealed), 1)
        self.assertNotIn(b"owner requested", sealed[0].read_bytes())

    def test_praxis_self_intent_survives_restart_and_only_owner_dm_claims_it(self):
        binding = ConfirmationBinding(
            request_name=self.binding.request_name,
            parameter_commitment="d" * 64,
            principal="praxis:self",
            scope=self.binding.scope,
        )
        intent = CriticalIntentOrigin.background(
            run_id="run-praxis-self", call_id="tool-self-1",
            principal_id="praxis:self", confirmation_owner_id="101",
        )
        prepared = self.store.prepare(
            binding, {"secret": "do not grep me"}, origin=intent,
            idempotency_key="self-critical-1",
        )
        self.assertEqual(prepared["requested_by"], "praxis:self")
        restarted = CriticalChallengeStore(
            owner_id=101, path=self.path, ttl_seconds=30, clock=lambda: self.now,
            initiator_principals=("praxis:self",),
        )
        claimed = restarted.claim(
            prepared["challenge_id"],
            origin=self.origin(
                run_id="run-owner-confirm", message_id=77,
                raw_text=prepared["exact_phrase"],
            ),
        )
        self.assertEqual(claimed["principal"], "praxis:self")
        self.assertEqual(claimed["parameters"], {"secret": "do not grep me"})
        self.assertEqual(claimed["confirmed_by"], "101")
        self.assertFalse(
            (restarted.secret_dir / f"{prepared['challenge_id']}.sealed").exists()
        )

        changed_owner = CriticalChallengeStore(
            owner_id=202, path=self.path, ttl_seconds=30, clock=lambda: self.now,
            initiator_principals=("praxis:self",),
        )
        self.assertEqual(changed_owner.list(), [])

    def test_pending_old_owner_envelope_is_removed_after_owner_change(self):
        pending = self.prepare(parameters={"secret": "old-owner-only"})
        sealed = self.store.secret_dir / f"{pending['challenge_id']}.sealed"
        self.assertTrue(sealed.is_file())

        changed_owner = CriticalChallengeStore(
            owner_id=202, path=self.path, ttl_seconds=30, clock=lambda: self.now,
            initiator_principals=("praxis:self",),
        )

        self.assertEqual(changed_owner.list(), [])
        self.assertFalse(sealed.exists())

    def test_prepare_is_idempotent_but_rejects_key_reuse_for_other_intent(self):
        first = self.prepare()
        repeated = self.prepare()
        self.assertEqual(repeated["challenge_id"], first["challenge_id"])
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 1)

        with self.assertRaisesRegex(CriticalChallengeError, "different intent"):
            self.prepare(parameters={"reason": "different"})
        with self.assertRaisesRegex(CriticalChallengeError, "different intent"):
            self.prepare(origin=self.origin(message_id=12))

    def test_idempotent_prepare_fails_closed_when_sealed_parameters_are_missing(self):
        first = self.prepare(parameters={"password": "never-in-ledger"})
        sealed = self.store.secret_dir / f"{first['challenge_id']}.sealed"
        sealed.unlink()

        with self.assertRaisesRegex(CriticalChallengeError, "cannot be repaired"):
            self.prepare(parameters={"password": "never-in-ledger"})
        self.assertFalse(sealed.exists())

    def test_idempotent_prepare_does_not_restore_expired_parameters(self):
        first = self.prepare(parameters={"password": "expired-secret"})
        sealed = self.store.secret_dir / f"{first['challenge_id']}.sealed"
        self.now = 131.0

        repeated = self.prepare(parameters={"password": "expired-secret"})

        self.assertEqual(repeated["status"], "expired")
        self.assertFalse(sealed.exists())

    def test_claim_requires_new_owner_run_message_and_exact_raw_phrase(self):
        prepared = self.prepare(parameters={"delete": False})
        challenge_id = prepared["challenge_id"]
        phrase = prepared["exact_phrase"]

        invalid_origins = (
            self.origin(run_id="run-prepare", message_id=12, raw_text=phrase),
            self.origin(run_id="run-confirm", message_id=11, raw_text=phrase),
            self.origin(run_id="run-confirm", message_id=12, raw_text=phrase + "."),
        )
        for invalid in invalid_origins:
            with self.subTest(origin=invalid):
                with self.assertRaises(CriticalChallengeError):
                    self.store.claim(challenge_id, origin=invalid)

        claimed = self.store.claim(
            challenge_id,
            origin=self.origin(
                run_id="run-confirm", message_id=12, raw_text=phrase,
            ),
        )
        self.assertEqual(claimed["status"], "in_doubt")
        self.assertEqual(claimed["parameters"], {"delete": False})
        self.assertEqual(claimed["principal"], "101")
        self.assertFalse(
            (self.store.secret_dir / f"{challenge_id}.sealed").exists()
        )

    def test_claim_rejects_surrounding_whitespace_without_burning_challenge(self):
        prepared = self.prepare(idempotency_key="exact-raw-text")
        challenge_id = prepared["challenge_id"]
        phrase = prepared["exact_phrase"]
        with self.assertRaises(CriticalChallengeError):
            self.store.claim(
                challenge_id,
                origin=self.origin(
                    run_id="run-confirm", message_id=12, raw_text=phrase + " ",
                ),
            )
        claimed = self.store.claim(
            challenge_id,
            origin=self.origin(
                run_id="run-confirm", message_id=12, raw_text=phrase,
            ),
        )
        self.assertEqual(claimed["status"], "in_doubt")

    def test_trusted_principal_and_group_cannot_claim_owner_challenge(self):
        prepared = self.prepare()
        challenge_id = prepared["challenge_id"]
        phrase = prepared["exact_phrase"]
        with self.assertRaises(PermissionError):
            self.store.claim(
                challenge_id,
                origin=self.origin(
                    run_id="trusted-run",
                    message_id=12,
                    principal_id="202",
                    raw_text=phrase,
                ),
            )
        with self.assertRaises(PermissionError):
            self.store.claim(
                challenge_id,
                origin=self.origin(
                    run_id="group-run",
                    message_id=12,
                    is_dm=False,
                    raw_text=phrase,
                ),
            )
        self.assertEqual(self.store.list()[0]["status"], "pending")

    def test_claim_is_fsynced_before_return_and_cannot_replay_after_restart(self):
        prepared = self.prepare()
        challenge_id = prepared["challenge_id"]
        confirmation = self.origin(
            run_id="run-confirm",
            message_id=12,
            raw_text=prepared["exact_phrase"],
        )
        claimed = self.store.claim(challenge_id, origin=confirmation)
        self.assertEqual(claimed["status"], "in_doubt")

        restarted = CriticalChallengeStore(
            owner_id=101,
            path=self.path,
            ttl_seconds=30,
            clock=lambda: self.now,
            initiator_principals=("praxis:self",),
        )
        self.assertEqual(restarted.list()[0]["status"], "in_doubt")
        with self.assertRaisesRegex(CriticalChallengeError, "cannot be replayed"):
            restarted.claim(challenge_id, origin=confirmation)
        completed = restarted.finish(
            challenge_id, receipt=self.dispatch_receipt(claimed),
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(
            CriticalChallengeStore(
                owner_id=101,
                path=self.path,
                ttl_seconds=30,
                clock=lambda: self.now,
                initiator_principals=("praxis:self",),
            ).list()[0]["status"],
            "completed",
        )

    def test_completion_persists_only_validated_opaque_receipt(self):
        prepared = self.prepare(parameters={"password": "sealed-input-secret"})
        claimed = self.store.claim(
            prepared["challenge_id"],
            origin=self.origin(
                run_id="run-confirm", message_id=12,
                raw_text=prepared["exact_phrase"],
            ),
        )
        receipt = self.dispatch_receipt(claimed, secret="never-write-this-receipt-secret")
        completed = self.store.finish(prepared["challenge_id"], receipt=receipt)

        persisted = self.path.read_text(encoding="utf-8")
        self.assertNotIn("sealed-input-secret", persisted)
        self.assertNotIn("never-write-this-receipt-secret", persisted)
        summary = completed["terminal"]["receipt"]
        self.assertEqual(summary["requested_by"], "101")
        self.assertEqual(summary["confirmed_by"], "101")
        for forbidden in (
            "submitted_parameters", "serialized_parameters", "result", "error",
            "delivery_context", "identifiers", "parameter_commitment",
            "parameters_sha256", "result_sha256", "receipt_sha256",
        ):
            self.assertNotIn(forbidden, summary)

    def test_completion_rejects_a_secret_bearing_dispatch_receipt(self):
        prepared = self.prepare(parameters={"password": "still-sealed"})
        claimed = self.store.claim(
            prepared["challenge_id"],
            origin=self.origin(
                run_id="run-confirm", message_id=12,
                raw_text=prepared["exact_phrase"],
            ),
        )
        receipt = self.dispatch_receipt(claimed)
        receipt["receipt"]["submitted_parameters"] = {"password": "must-not-persist"}

        with self.assertRaisesRegex(CriticalChallengeError, "exposed secret"):
            self.store.finish(prepared["challenge_id"], receipt=receipt)
        self.assertNotIn(
            "must-not-persist", self.path.read_text(encoding="utf-8"),
        )
        self.assertEqual(self.store.list()[0]["status"], "in_doubt")

    def test_mismatched_completion_receipt_does_not_terminalize_challenge(self):
        prepared = self.prepare(parameters={"password": "still-sealed"})
        claimed = self.store.claim(
            prepared["challenge_id"],
            origin=self.origin(
                run_id="run-confirm", message_id=12,
                raw_text=prepared["exact_phrase"],
            ),
        )
        receipt = self.dispatch_receipt(claimed)
        receipt["receipt"]["principal"] = "202"

        with self.assertRaisesRegex(CriticalChallengeError, "does not match"):
            self.store.finish(prepared["challenge_id"], receipt=receipt)
        self.assertEqual(self.store.list()[0]["status"], "in_doubt")

    def test_failed_completion_persists_only_safe_error_type(self):
        prepared = self.prepare()
        self.store.claim(
            prepared["challenge_id"],
            origin=self.origin(
                run_id="run-confirm", message_id=12,
                raw_text=prepared["exact_phrase"],
            ),
        )
        failed = self.store.finish(
            prepared["challenge_id"], error="DispatchError: secret failure detail",
        )
        persisted = self.path.read_text(encoding="utf-8")
        self.assertNotIn("secret failure detail", persisted)
        self.assertEqual(failed["terminal"]["error"]["type"], "DispatchError")
        failed_row = next(
            json.loads(line) for line in persisted.splitlines()
            if json.loads(line).get("kind") == "failed"
        )
        self.assertEqual(failed_row["data"]["error"], {"type": "DispatchError"})

    def test_colonless_error_is_never_treated_as_durable_exception_type(self):
        prepared = self.prepare(idempotency_key="colonless-error")
        self.store.claim(
            prepared["challenge_id"],
            origin=self.origin(
                run_id="run-confirm-colonless", message_id=13,
                raw_text=prepared["exact_phrase"],
            ),
        )
        secret = "TOPSECRETWITHOUTCOLON"
        failed = self.store.finish(prepared["challenge_id"], error=secret)

        persisted = self.path.read_text(encoding="utf-8")
        self.assertNotIn(secret, persisted)
        self.assertNotIn(hashlib.sha256(secret.encode("utf-8")).hexdigest(), persisted)
        self.assertEqual(
            failed["terminal"]["error"]["type"], "CriticalDispatchError",
        )

    def test_legacy_failed_row_never_projects_unsafe_type_or_digest(self):
        prepared = self.prepare(idempotency_key="legacy-failed-row")
        self.store.claim(
            prepared["challenge_id"],
            origin=self.origin(
                run_id="run-confirm-legacy", message_id=14,
                raw_text=prepared["exact_phrase"],
            ),
        )
        self.store.finish(
            prepared["challenge_id"], error="DispatchError: old detail",
        )
        rows = [json.loads(line) for line in self.path.read_text("utf-8").splitlines()]
        legacy_digest = "b" * 64
        legacy_secret = "LEGACYCOLONLESSSECRET"
        for row in rows:
            if row.get("kind") == "failed":
                row["data"]["error"] = {
                    "type": legacy_secret, "sha256": legacy_digest,
                }
        self.path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )

        restarted = CriticalChallengeStore(
            owner_id=101, path=self.path, ttl_seconds=30,
            clock=lambda: self.now, initiator_principals=("praxis:self",),
        )
        public = restarted.list()[0]
        rendered = json.dumps(public, ensure_ascii=False)
        self.assertEqual(public["status"], "failed")
        self.assertEqual(
            public["terminal"]["error"], {"type": "CriticalDispatchError"},
        )
        self.assertNotIn(legacy_secret, rendered)
        self.assertNotIn(legacy_digest, rendered)

    def test_cancel_and_expiry_are_durable_and_not_claimable(self):
        cancelled = self.prepare(idempotency_key="cancel-me")
        self.assertTrue(self.store.cancel(
            cancelled["challenge_id"], origin=self.origin(message_id=20),
        ))
        self.assertFalse(self.store.cancel(
            cancelled["challenge_id"], origin=self.origin(message_id=21),
        ))
        with self.assertRaisesRegex(CriticalChallengeError, "cannot be replayed"):
            self.store.claim(
                cancelled["challenge_id"],
                origin=self.origin(
                    run_id="run-confirm",
                    message_id=22,
                    raw_text=cancelled["exact_phrase"],
                ),
            )

        expiring = self.prepare(idempotency_key="expire-me")
        self.now = 130.0
        statuses = {
            item["challenge_id"]: item["status"] for item in self.store.list()
        }
        self.assertEqual(statuses[cancelled["challenge_id"]], "cancelled")
        self.assertEqual(statuses[expiring["challenge_id"]], "expired")
        with self.assertRaisesRegex(CriticalChallengeError, "expired"):
            self.store.claim(
                expiring["challenge_id"],
                origin=self.origin(
                    run_id="run-expired",
                    message_id=30,
                    raw_text=expiring["exact_phrase"],
                ),
            )


if __name__ == "__main__":
    unittest.main()
