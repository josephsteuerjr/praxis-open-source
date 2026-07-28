import inspect
import hashlib
import json
import pkgutil
import unittest

from telethon.tl import functions, types
from telethon.tl.tlobject import TLRequest

from telegram_registry import (
    CriticalConfirmation,
    ParameterValidationError,
    RegistryLookupError,
    TelegramAccountDispatcher,
    TelegramCapabilityRegistry,
    classify_request,
)


def _installed_request_names():
    modules = [functions]
    modules.extend(
        __import__(item.name, fromlist=["*"])
        for item in pkgutil.iter_modules(functions.__path__, functions.__name__ + ".")
    )
    names = set()
    for module in modules:
        for value in vars(module).values():
            if (
                inspect.isclass(value)
                and value is not TLRequest
                and issubclass(value, TLRequest)
                and value.__module__ == module.__name__
            ):
                suffix = value.__module__.split(".functions", 1)[1].lstrip(".")
                names.add("functions" + (f".{suffix}" if suffix else "") + f".{value.__name__}")
    return names


class RegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = TelegramCapabilityRegistry()

    def test_registry_is_exactly_the_installed_tl_request_schema(self):
        installed = _installed_request_names()
        listed = set()
        offset = 0
        while True:
            page = self.registry.list(offset=offset, limit=500)
            listed.update(item["name"] for item in page["items"])
            offset += len(page["items"])
            if offset >= page["total"]:
                break
        self.assertEqual(listed, installed)
        self.assertEqual(self.registry.metadata["request_count"], len(installed))
        self.assertGreater(len(installed), 700)
        self.assertTrue(self.registry.metadata["telethon_version"])
        self.assertIsInstance(self.registry.metadata["tl_layer"], int)
        self.assertEqual(len(self.registry.metadata["fingerprint"]), 64)

    def test_search_list_and_describe_are_deterministic_and_json_safe(self):
        hit = self.registry.search("send message", limit=5)["items"][0]
        self.assertEqual(hit["name"], "functions.messages.SendMessageRequest")
        page = self.registry.list(scope="telegram.membership", limit=500)
        self.assertIn(
            "functions.channels.JoinChannelRequest",
            {item["name"] for item in page["items"]},
        )
        detail = self.registry.describe("messages.SendMessageRequest")
        request = detail["request"]
        self.assertEqual(request["schema"]["required"], ["peer", "message"])
        self.assertFalse(request["schema"]["additionalProperties"])
        self.assertTrue(request["schema"]["properties"]["peer"]["oneOf"])
        json.dumps(detail)

    def test_exact_aliases_work_but_unknown_names_fail_closed(self):
        expected = "functions.messages.SendMessageRequest"
        self.assertEqual(self.registry.get(expected).name, expected)
        self.assertEqual(self.registry.get("messages.SendMessageRequest").name, expected)
        self.assertEqual(self.registry.get("telethon.tl." + expected).name, expected)
        with self.assertRaises(RegistryLookupError):
            self.registry.get("messages.DefinitelyNotARequest")

    def test_validation_is_strict_and_builds_nested_installed_tl_types(self):
        with self.assertRaises(ParameterValidationError):
            self.registry.validate("messages.SendMessageRequest", {"peer": "me"})
        with self.assertRaises(ParameterValidationError):
            self.registry.validate(
                "messages.SendMessageRequest",
                {"peer": "me", "message": "x", "surprise": True},
            )
        with self.assertRaises(ParameterValidationError):
            self.registry.validate(
                "messages.SendMessageRequest", {"peer": "me", "message": 7}
            )
        params = self.registry.validate(
            "functions.messages.SendMessageRequest",
            {
                "peer": "me",
                "message": "x",
                "schedule_date": "2026-07-13T12:30:00+00:00",
                "reply_to": {
                    "_": "types.InputReplyToMessage",
                    "reply_to_msg_id": 5,
                    "poll_option": {"$bytes_base64": "AQI=", "size": 2},
                },
            },
        )
        self.assertIsInstance(params["reply_to"], types.InputReplyToMessage)
        self.assertEqual(params["reply_to"].poll_option, b"\x01\x02")
        self.assertEqual(params["schedule_date"].year, 2026)
        with self.assertRaises(ParameterValidationError):
            self.registry.validate(
                "messages.SendMessageRequest",
                {
                    "peer": "me",
                    "message": "x",
                    "reply_to": {
                        "_": "InputReplyToMessage",
                        "reply_to_msg_id": 5,
                        "poll_option": {"$bytes_base64": "AQI=", "size": 999},
                    },
                },
            )

    def test_scope_and_risk_classification_covers_the_contract(self):
        self.assertEqual(
            classify_request("functions.messages.GetHistoryRequest")[0], "telegram.read"
        )
        self.assertEqual(
            classify_request("functions.messages.SendMessageRequest")[0],
            "telegram.communicate",
        )
        self.assertEqual(
            classify_request("functions.channels.EditAdminRequest")[0],
            "telegram.moderate",
        )
        self.assertEqual(
            classify_request("functions.channels.LeaveChannelRequest")[0],
            "telegram.membership",
        )
        self.assertEqual(
            classify_request("functions.chatlists.JoinChatlistInviteRequest")[0],
            "telegram.membership",
        )
        self.assertEqual(
            classify_request("functions.chatlists.LeaveChatlistRequest")[0],
            "telegram.membership",
        )
        self.assertEqual(
            classify_request("functions.chatlists.GetLeaveChatlistSuggestionsRequest")[0],
            "telegram.account",
        )
        self.assertEqual(
            classify_request("functions.account.UpdateProfileRequest")[0],
            "telegram.account",
        )
        self.assertEqual(
            classify_request("functions.auth.LogOutRequest")[1], "account_critical"
        )
        self.assertEqual(
            classify_request("functions.InvokeWithLayerRequest")[1], "transport_internal"
        )
        self.assertEqual(
            classify_request("functions.DestroySessionRequest")[1], "account_critical"
        )


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = TelegramCapabilityRegistry()

    async def test_owner_raw_call_resolves_entities_and_returns_full_receipt(self):
        calls = []
        resolved = []

        async def resolver(value, expected, field, request_name):
            resolved.append((value, expected, field, request_name))
            user_id = 42 if value == "@top" else 43
            return types.InputPeerUser(user_id=user_id, access_hash=9000 + user_id)

        async def caller(request):
            calls.append(request)
            return {"message_id": 901, "peer_id": 42, "text": "sent"}

        dispatcher = TelegramAccountDispatcher(
            caller=caller,
            entity_resolver=resolver,
            owner_id="owner",
            registry=self.registry,
        )
        receipt = await dispatcher.dispatch(
            "functions.messages.SendMessageRequest",
            {
                "peer": "@top",
                "message": "hello",
                "reply_to": {
                    "_": "InputReplyToMessage",
                    "reply_to_msg_id": 7,
                    "reply_to_peer_id": "@nested",
                },
            },
            principal="owner",
            delivery_context={"chat_id": 777, "message_id": 8},
        )
        self.assertTrue(receipt.ok, receipt.to_dict())
        self.assertEqual(len(calls), 1)
        self.assertIsInstance(calls[0].peer, types.InputPeerUser)
        self.assertIsInstance(calls[0].reply_to.reply_to_peer_id, types.InputPeerUser)
        self.assertEqual(len(resolved), 2)
        self.assertEqual(receipt.serialized_parameters["_"], "SendMessageRequest")
        self.assertEqual(receipt.serialized_parameters["message"], "hello")
        self.assertEqual(receipt.result["text"], "sent")
        self.assertIn(42, receipt.identifiers["peer_ids"])
        self.assertIn(901, receipt.identifiers["message_ids"])
        self.assertEqual(len(receipt.parameter_commitment), 64)
        self.assertEqual(len(receipt.result_sha256), 64)
        json.dumps(receipt.to_dict())

    async def test_raw_is_sovereign_only_even_if_trusted_principal_has_scope(self):
        called = False

        async def caller(request):
            nonlocal called
            called = True

        dispatcher = TelegramAccountDispatcher(
            caller=caller,
            entity_resolver=lambda value, *args: (
                types.InputChannel(channel_id=88, access_hash=808)
                if value == "@room" else types.InputPeerSelf()
            ),
            owner_id="owner",
            registry=self.registry,
        )
        receipt = await dispatcher.dispatch(
            "functions.messages.SendMessageRequest",
            {"peer": "me", "message": "no"},
            principal="trusted",
            granted_scopes={"telegram.communicate"},
        )
        self.assertEqual(receipt.status, "denied")
        self.assertEqual(receipt.error["type"], "PermissionDenied")
        self.assertFalse(called)

        alias = await dispatcher.dispatch(
            "messages.SendMessageRequest",
            {"peer": "me", "message": "no"},
            principal="owner",
        )
        self.assertEqual(alias.status, "denied")
        self.assertIn("exact constructor name", alias.error["message"])
        self.assertFalse(called)

    async def test_praxis_self_can_use_raw_and_account_scopes_without_human_grants(self):
        calls = []

        async def caller(request):
            calls.append(request)
            return {"ok": True}

        dispatcher = TelegramAccountDispatcher(
            caller=caller,
            entity_resolver=lambda value, *args: (
                types.InputChannel(channel_id=88, access_hash=808)
                if value == "@room" else types.InputPeerSelf()
            ),
            owner_id="owner",
            sovereign_principals={"praxis:self"},
            registry=self.registry,
        )
        raw = await dispatcher.dispatch(
            "functions.messages.SendMessageRequest",
            {"peer": "me", "message": "from self"},
            principal="praxis:self",
        )
        self.assertTrue(raw.ok, raw.to_dict())
        membership = await dispatcher.dispatch(
            "functions.channels.JoinChannelRequest",
            {"channel": "@room"},
            principal="praxis:self",
        )
        self.assertTrue(membership.ok, membership.to_dict())
        self.assertEqual(len(calls), 2)

    async def test_empty_owner_id_never_makes_empty_principal_sovereign(self):
        called = False

        async def caller(request):
            nonlocal called
            called = True

        dispatcher = TelegramAccountDispatcher(
            caller=caller,
            entity_resolver=lambda *args: types.InputPeerSelf(),
            owner_id="",
            sovereign_principals={"praxis:self", ""},
            registry=self.registry,
        )
        receipt = await dispatcher.dispatch(
            "functions.messages.SendMessageRequest",
            {"peer": "me", "message": "must fail"},
            principal="",
        )
        self.assertEqual(receipt.status, "denied")
        self.assertFalse(called)

    async def test_typed_mode_cannot_be_used_to_downgrade_arbitrary_raw_calls(self):
        calls = []

        async def caller(request):
            calls.append(request)
            return {"message_id": 1}

        resolver = lambda *args: types.InputPeerSelf()
        closed = TelegramAccountDispatcher(
            caller=caller,
            entity_resolver=resolver,
            owner_id="owner",
            registry=self.registry,
        )
        denied = await closed.dispatch(
            "functions.messages.SendMessageRequest",
            {"peer": "me", "message": "x"},
            principal="trusted",
            granted_scopes={"telegram.communicate"},
            mode="typed",
        )
        self.assertEqual(denied.status, "denied")
        self.assertFalse(calls)

        open_typed = TelegramAccountDispatcher(
            caller=caller,
            entity_resolver=resolver,
            owner_id="owner",
            registry=self.registry,
            typed_allowlist={
                "functions.messages.SendMessageRequest",
                "functions.channels.JoinChannelRequest",
            },
        )
        allowed = await open_typed.dispatch(
            "functions.messages.SendMessageRequest",
            {"peer": "me", "message": "x"},
            principal="trusted",
            granted_scopes={"telegram.communicate"},
            mode="typed",
        )
        self.assertTrue(allowed.ok)
        membership = await open_typed.dispatch(
            "functions.channels.JoinChannelRequest",
            {"channel": "@room"},
            principal="trusted",
            granted_scopes={"telegram.membership"},
            mode="typed",
        )
        self.assertEqual(membership.status, "denied")
        self.assertEqual(len(calls), 1)

    async def test_account_critical_requires_separate_bound_consumable_confirmation(self):
        calls = []
        verified = []

        async def caller(request):
            calls.append(request)
            return {"logged_out": True}

        async def verifier(proof, binding):
            verified.append((proof, binding))
            return proof.token == "one-use-owner-proof"

        dispatcher = TelegramAccountDispatcher(
            caller=caller,
            entity_resolver=None,
            owner_id="owner",
            registry=self.registry,
            confirmation_verifier=verifier,
        )
        denied = await dispatcher.dispatch(
            "functions.auth.LogOutRequest", {}, principal="owner"
        )
        self.assertEqual(denied.status, "denied")
        self.assertEqual(denied.error["type"], "ConfirmationRequired")
        self.assertFalse(calls)

        binding = dispatcher.confirmation_binding(
            "functions.auth.LogOutRequest", {}, principal="owner"
        )
        wrong = CriticalConfirmation(token="wrong-one-use-owner-proof")
        rejected = await dispatcher.dispatch(
            "functions.auth.LogOutRequest", {}, principal="owner", confirmation=wrong
        )
        self.assertEqual(rejected.status, "denied")
        self.assertFalse(calls)

        proof = CriticalConfirmation(token="one-use-owner-proof")
        allowed = await dispatcher.dispatch(
            "functions.auth.LogOutRequest", {}, principal="owner", confirmation=proof
        )
        self.assertTrue(allowed.ok, allowed.to_dict())
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(verified), 2)
        rendered = json.dumps(allowed.to_dict())
        self.assertNotIn("one-use-owner-proof", rendered)
        self.assertTrue(allowed.policy["confirmation"]["verified"])

    async def test_account_critical_receipt_has_no_secret_or_plain_verifier(self):
        parameters = {
            "phone_number": "+19995550123",
            "phone_code_hash": "short-hash-secret",
            "phone_code": "104729",
        }

        async def caller(_request):
            return {
                "authorization": "result-auth-secret",
                "phone_code": parameters["phone_code"],
            }

        async def verifier(proof, _binding):
            return proof.token == "opaque-owner-proof"

        dispatcher = TelegramAccountDispatcher(
            caller=caller,
            entity_resolver=None,
            owner_id="owner",
            registry=self.registry,
            confirmation_verifier=verifier,
        )
        binding = dispatcher.confirmation_binding(
            "functions.auth.SignInRequest", parameters, principal="owner",
        )
        receipt = await dispatcher.dispatch(
            binding.request_name,
            parameters,
            principal="owner",
            confirmation=CriticalConfirmation(token="opaque-owner-proof"),
        )

        self.assertTrue(receipt.ok, receipt.to_dict())
        self.assertIsNone(receipt.submitted_parameters)
        self.assertIsNone(receipt.serialized_parameters)
        self.assertIsNone(receipt.result)
        self.assertIsNone(receipt.result_sha256)
        self.assertEqual(receipt.identifiers, {})
        self.assertRegex(receipt.parameter_commitment, r"^[0-9a-f]{64}$")
        encoded = json.dumps(
            parameters, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
        self.assertNotEqual(
            receipt.parameter_commitment, hashlib.sha256(encoded).hexdigest(),
        )
        rendered = json.dumps(receipt.to_dict(), ensure_ascii=False, sort_keys=True)
        for secret in (*parameters.values(), "result-auth-secret", "opaque-owner-proof"):
            self.assertNotIn(secret, rendered)
            self.assertNotIn(hashlib.sha256(secret.encode("utf-8")).hexdigest(), rendered)
        self.assertNotIn("proof_sha256", rendered)

    async def test_transport_envelopes_are_listed_but_never_dispatchable(self):
        called = False

        async def caller(request):
            nonlocal called
            called = True

        dispatcher = TelegramAccountDispatcher(
            caller=caller,
            entity_resolver=None,
            owner_id="owner",
            registry=self.registry,
        )
        receipt = await dispatcher.dispatch(
            "functions.PingRequest", {"ping_id": 1}, principal="owner"
        )
        self.assertEqual(receipt.status, "denied")
        self.assertEqual(receipt.policy["risk"], "transport_internal")
        self.assertFalse(called)

    async def test_retryable_rpc_errors_become_receipts_without_sleeping(self):
        class FloodWaitError(Exception):
            seconds = 37

        async def caller(request):
            raise FloodWaitError("wait")

        dispatcher = TelegramAccountDispatcher(
            caller=caller,
            entity_resolver=lambda *args: types.InputPeerSelf(),
            owner_id="owner",
            registry=self.registry,
        )
        receipt = await dispatcher.dispatch(
            "functions.messages.SendMessageRequest",
            {"peer": "me", "message": "later"},
            principal="owner",
        )
        self.assertEqual(receipt.status, "retryable_error")
        self.assertEqual(receipt.retry["state"], "flood_wait")
        self.assertEqual(receipt.retry["retry_after_seconds"], 37)
        self.assertFalse(receipt.retry["sleep_performed"])

    async def test_bad_entity_resolver_and_bad_parameters_leave_denied_receipts(self):
        async def caller(request):
            raise AssertionError("must not call")

        dispatcher = TelegramAccountDispatcher(
            caller=caller,
            entity_resolver=lambda *args: "still-not-an-input-peer",
            owner_id="owner",
            registry=self.registry,
        )
        bad_entity = await dispatcher.dispatch(
            "functions.messages.SendMessageRequest",
            {"peer": "me", "message": "x"},
            principal="owner",
        )
        self.assertEqual(bad_entity.status, "denied")
        self.assertEqual(bad_entity.error["type"], "ParameterValidationError")
        bad_schema = await dispatcher.dispatch(
            "functions.messages.SendMessageRequest",
            {"peer": "me"},
            principal="owner",
        )
        self.assertEqual(bad_schema.status, "denied")
        self.assertEqual(bad_schema.error["type"], "ParameterValidationError")

    async def test_compact_handle_surface_has_list_search_describe_and_call(self):
        calls = []

        async def caller(request):
            calls.append(request)
            return {"message_id": 3}

        dispatcher = TelegramAccountDispatcher(
            caller=caller,
            entity_resolver=lambda *args: types.InputPeerSelf(),
            owner_id="owner",
            registry=self.registry,
        )
        listed = await dispatcher.handle(
            "list", {"namespace": "channels", "limit": 2}, principal="anyone"
        )
        self.assertEqual(listed["action"], "list")
        self.assertEqual(len(listed["items"]), 2)
        searched = await dispatcher.handle(
            "search", {"query": "send message", "limit": 1}, principal="anyone"
        )
        exact = searched["items"][0]["name"]
        described = await dispatcher.handle(
            "describe", {"name": exact}, principal="anyone"
        )
        self.assertEqual(described["request"]["name"], exact)
        called = await dispatcher.handle(
            "call",
            {
                "name": "functions.messages.SendMessageRequest",
                "parameters": {"peer": "me", "message": "hello"},
            },
            principal="owner",
        )
        self.assertEqual(called["receipt"]["status"], "ok")
        self.assertEqual(len(calls), 1)
        with self.assertRaises(ParameterValidationError):
            await dispatcher.handle("search", {}, principal="anyone")


if __name__ == "__main__":
    unittest.main()
