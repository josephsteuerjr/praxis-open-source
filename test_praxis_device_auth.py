from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import threading
import unittest
import urllib.parse

from praxis_device_auth import (
    DEFAULT_SCOPES,
    DeviceAuthCorruption,
    DeviceAuthPermissionError,
    DeviceAuthStore,
    DeviceCredential,
    DevicePrincipal,
    EnrollmentConsumed,
    EnrollmentExpired,
    InvalidEnrollment,
    OwnerPrincipal,
    build_enrollment_url,
)


class MutableClock:
    def __init__(self, value: float = 1_800_000_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class DeviceAuthStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.clock = MutableClock()
        self.owner = OwnerPrincipal("owner-42")
        self.store = DeviceAuthStore(
            base=self.base, owner_id=self.owner.owner_id, clock=self.clock,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @property
    def events_path(self) -> Path:
        return self.base / "memory" / "access" / "devices" / "events.jsonl"

    @property
    def key_path(self) -> Path:
        return self.base / "memory" / ".state" / "praxis_device_auth.key"

    def enroll(self, *, label: str = "Owner laptop", ttl: int = 60,
               scopes=None):
        return self.store.create_enrollment(
            self.owner, label=label, ttl_seconds=ttl, scopes=scopes,
        )

    def issue(self, *, label: str = "Owner laptop", ttl: int = 60,
              scopes=None, platform: str = "Windows 11") -> DeviceCredential:
        enrollment = self.enroll(label=label, ttl=ttl, scopes=scopes)
        return self.store.redeem(enrollment.enrollment_token, platform=platform)

    def test_enrollment_redeems_once_and_returns_device_principal(self) -> None:
        enrollment = self.enroll(scopes=["praxis.events", "praxis.snapshot"])
        credential = self.store.redeem(
            enrollment.enrollment_token, platform="Windows 11 / Edge",
        )

        self.assertTrue(credential.bearer_token.startswith("praxis_device_dev_"))
        self.assertEqual("Owner laptop", credential.principal.label)
        self.assertEqual("Windows 11 / Edge", credential.principal.platform)
        self.assertEqual(
            ("praxis.events", "praxis.snapshot"), credential.principal.scopes,
        )
        self.assertEqual(
            credential.principal,
            self.store.validate_bearer(credential.bearer_token),
        )
        with self.assertRaises(EnrollmentConsumed):
            self.store.redeem(enrollment.enrollment_token, platform="Windows 11")

    def test_default_scope_and_required_scope_enforcement(self) -> None:
        credential = self.issue()

        self.assertEqual(DEFAULT_SCOPES, credential.principal.scopes)
        self.assertIsNotNone(self.store.validate_bearer(
            credential.bearer_token, required_scope="praxis.snapshot",
        ))
        self.assertIsNotNone(self.store.validate_bearer(
            credential.bearer_token, required_scope="praxis.events",
        ))
        self.assertIsNone(self.store.validate_bearer(
            credential.bearer_token, required_scope="devices.manage",
        ))

        computer = self.issue(
            label="Remote workstation",
            scopes=["computer.read", "computer.files", "computer.process", "computer.apps"],
        )
        for scope in computer.principal.scopes:
            self.assertIsNotNone(self.store.validate_bearer(
                computer.bearer_token, required_scope=scope,
            ))

    def test_expired_enrollment_is_rejected_at_exact_boundary(self) -> None:
        enrollment = self.enroll(ttl=5)
        self.clock.value += 5

        with self.assertRaises(EnrollmentExpired):
            self.store.redeem(enrollment.enrollment_token, platform="Android")

    def test_invalid_credential_does_not_consume_enrollment(self) -> None:
        enrollment = self.enroll()
        prefix, secret = enrollment.enrollment_token.rsplit(".", 1)
        wrong = prefix + "." + (("A" if secret[0] != "A" else "B") + secret[1:])

        with self.assertRaises(InvalidEnrollment):
            self.store.redeem(wrong, platform="Windows")
        credential = self.store.redeem(
            enrollment.enrollment_token, platform="Windows",
        )
        self.assertIsNotNone(self.store.validate_bearer(credential.bearer_token))

    def test_concurrent_redeem_has_exactly_one_winner(self) -> None:
        enrollment = self.enroll()
        other = DeviceAuthStore(
            base=self.base, owner_id=self.owner.owner_id, clock=self.clock,
        )
        barrier = threading.Barrier(2)

        def attempt(store: DeviceAuthStore):
            barrier.wait(timeout=5)
            try:
                return "issued", store.redeem(
                    enrollment.enrollment_token, platform="Windows",
                )
            except EnrollmentConsumed:
                return "consumed", None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, (self.store, other)))

        self.assertEqual(["consumed", "issued"], sorted(row[0] for row in results))
        winner = next(row[1] for row in results if row[0] == "issued")
        self.assertIsNotNone(self.store.validate_bearer(winner.bearer_token))

    def test_restart_persists_authority_and_revoke_is_immediate(self) -> None:
        credential = self.issue()
        restarted = DeviceAuthStore(
            base=self.base, owner_id=self.owner.owner_id, clock=self.clock,
        )

        self.assertIsNotNone(restarted.validate_bearer(credential.bearer_token))
        self.assertTrue(restarted.revoke_device(
            self.owner, credential.principal.device_id, reason="phone was lost",
        ))
        self.assertIsNone(self.store.validate_bearer(credential.bearer_token))
        self.assertFalse(restarted.revoke_device(
            self.owner, credential.principal.device_id,
        ))

        rows = restarted.list_devices(self.owner)
        self.assertEqual(1, len(rows))
        self.assertEqual("revoked", rows[0]["status"])
        self.assertEqual([], restarted.list_devices(
            self.owner, include_revoked=False,
        ))

    def test_device_principal_and_bearer_cannot_manage_authority(self) -> None:
        credential = self.issue()
        actors = (credential.principal, credential.bearer_token, object())

        for actor in actors:
            with self.subTest(actor=type(actor).__name__):
                with self.assertRaises(DeviceAuthPermissionError):
                    self.store.create_enrollment(actor, label="Delegated")
                with self.assertRaises(DeviceAuthPermissionError):
                    self.store.list_devices(actor)
                with self.assertRaises(DeviceAuthPermissionError):
                    self.store.revoke_device(
                        actor, credential.principal.device_id,
                    )
        with self.assertRaises(DeviceAuthPermissionError):
            self.store.list_devices(OwnerPrincipal("someone-else"))

    def test_owner_api_accepts_non_ascii_identity_without_timing_api_error(self) -> None:
        base = self.base / "unicode-owner"
        owner = OwnerPrincipal("владелец")
        store = DeviceAuthStore(base=base, owner_id=owner.owner_id, clock=self.clock)

        enrollment = store.create_enrollment(owner, label="Телефон")
        self.assertTrue(enrollment.enrollment_token.startswith("praxis_enroll_"))

    def test_url_helper_keeps_secret_only_in_fragment(self) -> None:
        enrollment = self.enroll()
        url = build_enrollment_url(
            "https://praxis.example/app?mode=pwa", enrollment.enrollment_token,
        )
        parsed = urllib.parse.urlsplit(url)

        self.assertEqual("mode=pwa", parsed.query)
        self.assertNotIn(enrollment.enrollment_token, parsed.query)
        self.assertEqual(
            enrollment.enrollment_token,
            urllib.parse.parse_qs(parsed.fragment)["enroll"][0],
        )
        self.assertEqual(url, enrollment.url("https://praxis.example/app?mode=pwa"))
        with self.assertRaises(ValueError):
            build_enrollment_url("/relative", enrollment.enrollment_token)
        with self.assertRaises(ValueError):
            build_enrollment_url("https://praxis.example", "not-a-token")

    def test_secrets_are_absent_from_events_projections_and_repr(self) -> None:
        enrollment = self.enroll()
        credential = self.store.redeem(
            enrollment.enrollment_token, platform="Windows",
        )
        event_text = self.events_path.read_text(encoding="utf-8")
        projection_text = json.dumps(
            self.store.list_devices(self.owner), ensure_ascii=False, sort_keys=True,
        )
        secret_values = (
            enrollment.enrollment_token,
            enrollment.enrollment_token.rsplit(".", 1)[1],
            credential.bearer_token,
            credential.bearer_token.rsplit(".", 1)[1],
        )

        for secret in secret_values:
            self.assertNotIn(secret, event_text)
            self.assertNotIn(secret, projection_text)
            self.assertNotIn(secret, repr(enrollment))
            self.assertNotIn(secret, repr(credential))
            self.assertNotIn(secret, repr(self.store))

        event_keys = {key for row in map(json.loads, event_text.splitlines()) for key in row}
        self.assertFalse(any("token" in key or "length" in key or "key" in key
                             for key in event_keys))
        self.assertNotIn("bearer_mac", projection_text)
        self.assertNotIn("enrollment_mac", projection_text)

    def test_event_lines_are_canonical_and_key_is_persistent(self) -> None:
        credential = self.issue()
        before = self.key_path.read_bytes()
        lines = self.events_path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(32, len(before))
        for line in lines:
            row = json.loads(line)
            canonical = json.dumps(
                row, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                allow_nan=False,
            )
            self.assertEqual(canonical, line)
        DeviceAuthStore(base=self.base, owner_id=self.owner.owner_id, clock=self.clock)
        self.assertEqual(before, self.key_path.read_bytes())
        self.assertIsNotNone(self.store.validate_bearer(credential.bearer_token))

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are not Windows ACLs")
    def test_private_files_and_directories_have_private_modes(self) -> None:
        self.issue()
        lock_path = self.base / "memory" / ".state" / "praxis_device_auth.lock"

        for path in (self.key_path, self.events_path, lock_path):
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode), path)
        for path in (self.key_path.parent, self.events_path.parent):
            self.assertEqual(0o700, stat.S_IMODE(path.stat().st_mode), path)

    def test_torn_final_record_is_ignored_then_archived_before_mutation(self) -> None:
        credential = self.issue()
        tail = b'{"schema":'
        with self.events_path.open("ab") as stream:
            stream.write(tail)
            stream.flush()
            os.fsync(stream.fileno())

        restarted = DeviceAuthStore(
            base=self.base, owner_id=self.owner.owner_id, clock=self.clock,
        )
        self.assertIsNotNone(restarted.validate_bearer(credential.bearer_token))
        restarted.create_enrollment(self.owner, label="Second device")

        evidence = list(self.events_path.parent.glob("events.torn-*.bin"))
        self.assertEqual(1, len(evidence))
        self.assertEqual(tail, evidence[0].read_bytes())
        self.assertTrue(self.events_path.read_bytes().endswith(b"\n"))
        DeviceAuthStore(base=self.base, owner_id=self.owner.owner_id, clock=self.clock)

    def test_complete_unterminated_revoke_remains_authoritative(self) -> None:
        credential = self.issue()
        self.assertTrue(self.store.revoke_device(
            self.owner, credential.principal.device_id,
        ))
        raw = self.events_path.read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        self.events_path.write_bytes(raw[:-1])

        restarted = DeviceAuthStore(
            base=self.base, owner_id=self.owner.owner_id, clock=self.clock,
        )
        self.assertIsNone(restarted.validate_bearer(credential.bearer_token))
        restarted.create_enrollment(self.owner, label="After recovery")
        self.assertTrue(self.events_path.read_bytes().endswith(b"\n"))
        self.assertIsNone(restarted.validate_bearer(credential.bearer_token))
        self.assertEqual([], list(self.events_path.parent.glob("events.torn-*.bin")))

    def test_complete_duplicate_json_key_fails_closed(self) -> None:
        self.enroll()
        with self.events_path.open("ab") as stream:
            stream.write(b'{"schema":"one","schema":"two"}\n')

        with self.assertRaises(DeviceAuthCorruption):
            DeviceAuthStore(
                base=self.base, owner_id=self.owner.owner_id, clock=self.clock,
            )

    def test_event_tampering_fails_closed_for_existing_and_new_store(self) -> None:
        credential = self.issue(label="LaptopAlpha")
        raw = self.events_path.read_bytes()
        self.assertIn(b"LaptopAlpha", raw)
        self.events_path.write_bytes(raw.replace(b"LaptopAlpha", b"LaptopOmega", 1))

        with self.assertRaises(DeviceAuthCorruption):
            self.store.validate_bearer(credential.bearer_token)
        with self.assertRaises(DeviceAuthCorruption):
            DeviceAuthStore(
                base=self.base, owner_id=self.owner.owner_id, clock=self.clock,
            )

    def test_noncanonical_event_encoding_and_owner_rebinding_fail_closed(self) -> None:
        self.enroll()
        lines = self.events_path.read_text(encoding="utf-8").splitlines()
        first = json.dumps(json.loads(lines[0]), ensure_ascii=False, sort_keys=False)
        self.assertNotEqual(lines[0], first)
        self.events_path.write_text(first + "\n", encoding="utf-8", newline="\n")

        with self.assertRaises(DeviceAuthCorruption):
            DeviceAuthStore(
                base=self.base, owner_id=self.owner.owner_id, clock=self.clock,
            )

        self.events_path.write_text(lines[0] + "\n", encoding="utf-8", newline="\n")
        with self.assertRaises(DeviceAuthCorruption):
            DeviceAuthStore(
                base=self.base, owner_id="replacement-owner", clock=self.clock,
            )

    def test_missing_or_modified_key_fails_closed(self) -> None:
        credential = self.issue()
        original = self.key_path.read_bytes()

        self.key_path.unlink()
        with self.assertRaises(DeviceAuthCorruption):
            self.store.validate_bearer(credential.bearer_token)
        with self.assertRaises(DeviceAuthCorruption):
            DeviceAuthStore(
                base=self.base, owner_id=self.owner.owner_id, clock=self.clock,
            )

        self.key_path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        with self.assertRaises(DeviceAuthCorruption):
            DeviceAuthStore(
                base=self.base, owner_id=self.owner.owner_id, clock=self.clock,
            )

    def test_malformed_and_unknown_bearers_do_not_authenticate(self) -> None:
        credential = self.issue()
        prefix, secret = credential.bearer_token.rsplit(".", 1)
        altered = prefix + "." + (("A" if secret[0] != "A" else "B") + secret[1:])
        unknown = "praxis_device_dev_" + "f" * 32 + "." + secrets.token_urlsafe(32)

        self.assertIsNone(self.store.validate_bearer(""))
        self.assertIsNone(self.store.validate_bearer("Bearer " + credential.bearer_token))
        self.assertIsNone(self.store.validate_bearer(altered))
        self.assertIsNone(self.store.validate_bearer(unknown))

    def test_invalid_creation_inputs_are_rejected_without_events(self) -> None:
        cases = (
            {"label": ""},
            {"label": "ok", "scopes": "praxis.snapshot"},
            {"label": "ok", "scopes": ["devices.manage"]},
            {"label": "ok", "ttl_seconds": 0},
            {"label": "ok", "ttl_seconds": True},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    self.store.create_enrollment(self.owner, **kwargs)
        self.assertFalse(self.events_path.exists())


if __name__ == "__main__":
    unittest.main()
