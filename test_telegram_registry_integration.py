from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "x")
os.environ.setdefault(
    "TELEGRAM_SESSION",
    str(Path(tempfile.gettempdir()) / "praxis_registry_integration_test"),
)

from telethon.tl import functions, types  # noqa: E402

import mtproto_runner as runner  # noqa: E402
import telegram_confirmation  # noqa: E402


class FakeLiveClient:
    """Network-free stand-in for the one client owned by mtproto_runner."""

    def __init__(self):
        self.calls = []
        self.resolutions = []

    async def get_input_entity(self, value):
        self.resolutions.append(value)
        if value == "me":
            return types.InputPeerSelf()
        if value == "@room":
            return types.InputPeerChannel(channel_id=88, access_hash=808)
        return types.InputPeerUser(user_id=77, access_hash=707)

    async def __call__(self, request):
        self.calls.append(request)
        return {"message_id": 901, "peer_id": 77, "kind": type(request).__name__}


class LiveRegistryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.saved = {
            "client": runner.client,
            "owner_id": runner.OWNER_ID,
            "dispatcher": runner._TELEGRAM_DISPATCHER,
            "confirmations": runner._TELEGRAM_CONFIRMATIONS,
            "challenges": runner._TELEGRAM_CRITICAL_CHALLENGES,
            "threadsafe": runner._threadsafe_result,
            "scope": runner.agent._CURRENT_SCOPE,
            "hook": runner.agent._TELETHON.get("telegram_account"),
            "owner_env": os.environ.get("PRAXIS_OWNER_ID"),
        }
        os.environ["PRAXIS_OWNER_ID"] = "42"
        self.channel_token = runner.agent._TURN_CHANNEL.set(runner.agent.ChannelContext(
            chat_id="42", principal_id=42, is_dm=True, owner=True, known=True,
        ))
        self.fake = FakeLiveClient()
        runner.client = self.fake
        runner.OWNER_ID = 42
        runner.agent._CURRENT_SCOPE = "owner"
        runner._threadsafe_result = lambda factory, timeout: asyncio.run(factory())
        dispatcher = runner._install_telegram_dispatcher()
        self.temp = tempfile.TemporaryDirectory(prefix="praxis-registry-live-")
        self.addCleanup(self.temp.cleanup)
        state = Path(self.temp.name)
        runner._TELEGRAM_CONFIRMATIONS = telegram_confirmation.ConfirmationStore(
            owner_id=42, path=state / "proofs.jsonl", ttl_seconds=300,
            confirmable_principals=(runner.agent.PRAXIS_SELF_PRINCIPAL,),
        )
        runner._TELEGRAM_CRITICAL_CHALLENGES = (
            telegram_confirmation.CriticalChallengeStore(
                owner_id=42, path=state / "challenges.jsonl", ttl_seconds=300,
                initiator_principals=(runner.agent.PRAXIS_SELF_PRINCIPAL,),
            )
        )
        dispatcher.confirmation_verifier = (
            runner._TELEGRAM_CONFIRMATIONS.verify_and_consume
        )
        runner.agent._TELETHON["telegram_account"] = runner._sync_telegram_account
        self.origin = {
            "run_id": "run-owner-one", "chat_id": "42", "message_id": 100,
            "principal_id": "42", "is_dm": True, "raw_text": "do it",
        }
        self.execution = {
            "run_id": "run-owner-one", "call_id": "tool-one",
            "tool": "telegram_account", "args": {}, "side_effect": True,
            "idempotency_key": "",
        }
        self.origin_patch = mock.patch.object(
            runner.agent, "current_origin_evidence",
            side_effect=lambda: dict(self.origin),
        )
        self.execution_patch = mock.patch.object(
            runner.agent, "current_tool_execution",
            side_effect=lambda: dict(self.execution),
        )
        self.origin_patch.start()
        self.execution_patch.start()
        self.assertIs(dispatcher.caller, self.fake)

    def tearDown(self):
        runner.client = self.saved["client"]
        runner.OWNER_ID = self.saved["owner_id"]
        runner._TELEGRAM_DISPATCHER = self.saved["dispatcher"]
        runner._TELEGRAM_CONFIRMATIONS = self.saved["confirmations"]
        runner._TELEGRAM_CRITICAL_CHALLENGES = self.saved["challenges"]
        runner._threadsafe_result = self.saved["threadsafe"]
        runner.agent._CURRENT_SCOPE = self.saved["scope"]
        if self.saved["hook"] is None:
            runner.agent._TELETHON.pop("telegram_account", None)
        else:
            runner.agent._TELETHON["telegram_account"] = self.saved["hook"]
        runner.agent._TURN_CHANNEL.reset(self.channel_token)
        if self.saved["owner_env"] is None:
            os.environ.pop("PRAXIS_OWNER_ID", None)
        else:
            os.environ["PRAXIS_OWNER_ID"] = self.saved["owner_env"]
        self.execution_patch.stop()
        self.origin_patch.stop()

    def tool(self, action, **kwargs):
        return runner.agent.tool_telegram_account(action, **kwargs)

    def new_owner_message(self, text: str, *, run_id: str = "run-owner-two",
                          message_id: int = 101, call_id: str = "tool-two"):
        self.origin = {
            "run_id": run_id, "chat_id": "42", "message_id": message_id,
            "principal_id": "42", "is_dm": True, "raw_text": text,
        }
        self.execution = {
            "run_id": run_id, "call_id": call_id, "tool": "telegram_account",
            "args": {}, "side_effect": True, "idempotency_key": "",
        }

    def test_tool_lists_searches_describes_and_calls_installed_schema(self):
        listed = json.loads(self.tool("list", limit=1))
        self.assertEqual(listed["action"], "list")
        self.assertEqual(len(listed["items"]), 1)

        searched = json.loads(self.tool("search", query="send message", limit=1))
        exact = searched["items"][0]["name"]
        self.assertEqual(exact, "functions.messages.SendMessageRequest")

        described = json.loads(self.tool("describe", request=exact))
        self.assertEqual(described["request"]["name"], exact)
        self.assertIn("peer", described["request"]["schema"]["required"])

        called = json.loads(
            self.tool(
                "call",
                request=exact,
                params_json=json.dumps({"peer": "me", "message": "hello"}),
            )
        )
        receipt = called["receipt"]
        self.assertEqual(receipt["status"], "ok", receipt)
        self.assertEqual(receipt["principal"], "42")
        self.assertEqual(receipt["policy"]["raw_sovereign_only"], True)
        self.assertTrue(receipt["policy"]["sovereign"])
        self.assertEqual(receipt["result"]["message_id"], 901)
        self.assertEqual(self.fake.resolutions, ["me"])
        self.assertEqual(len(self.fake.calls), 1)
        self.assertIsInstance(self.fake.calls[0], functions.messages.SendMessageRequest)
        self.assertIsInstance(self.fake.calls[0].peer, types.InputPeerSelf)

        channel_call = json.loads(
            self.tool(
                "call",
                request="functions.channels.GetFullChannelRequest",
                params_json='{"channel":"@room"}',
            )
        )
        self.assertEqual(channel_call["receipt"]["status"], "ok")
        self.assertIsInstance(self.fake.calls[1], functions.channels.GetFullChannelRequest)
        self.assertIsInstance(self.fake.calls[1].channel, types.InputChannel)

    def test_raw_alias_and_account_critical_calls_use_fresh_owner_challenge(self):
        alias = json.loads(
            self.tool(
                "call",
                request="messages.SendMessageRequest",
                params_json='{"peer":"me","message":"no"}',
            )
        )
        self.assertEqual(alias["receipt"]["status"], "denied")
        self.assertIn("exact constructor name", alias["receipt"]["error"]["message"])

        # Account-critical aliases resolve to the canonical descriptor before
        # dispatch, so they cannot fall through into a denied receipt carrying
        # the submitted login parameters.
        self.execution["call_id"] = "tool-critical-alias"
        login_secret = "otp-731946"
        critical_alias_raw = self.tool(
            "call", request="auth.SignInRequest",
            params_json=json.dumps({
                "phone_number": "+100000000",
                "phone_code_hash": "hash-one",
                "phone_code": login_secret,
            }),
        )
        critical_alias = json.loads(critical_alias_raw)
        self.assertEqual(critical_alias["action"], "challenge")
        self.assertEqual(
            critical_alias["challenge"]["request_name"],
            "functions.auth.SignInRequest",
        )
        self.assertNotIn(login_secret, critical_alias_raw)
        for path in Path(self.temp.name).rglob("*"):
            if path.is_file():
                self.assertNotIn(login_secret.encode("utf-8"), path.read_bytes(), str(path))

        self.execution["call_id"] = "tool-one"

        critical_raw = self.tool("call", request="functions.auth.LogOutRequest")
        critical = json.loads(critical_raw)
        self.assertEqual(critical["action"], "challenge")
        challenge = critical["challenge"]
        self.assertEqual(challenge["status"], "pending")
        self.assertEqual(challenge["request_name"], "functions.auth.LogOutRequest")
        self.assertIn(challenge["challenge_id"], challenge["exact_phrase"])
        self.assertEqual(self.fake.calls, [])

        # A model-controlled confirm string is not forwarded and cannot become a
        # dispatcher proof.  The same durable tool intent gets the same challenge.
        fake_proof = json.loads(self.tool(
            "call", request="functions.auth.LogOutRequest", confirm="yes-really"
        ))
        self.assertEqual(
            fake_proof["challenge"]["challenge_id"], challenge["challenge_id"]
        )
        self.assertEqual(self.fake.calls, [])

        # Even exact text cannot self-confirm inside the originating run/message.
        self.origin["raw_text"] = challenge["exact_phrase"]
        same_run = self.tool("confirm", challenge_id=challenge["challenge_id"])
        self.assertIn("new owner message", same_run)
        self.assertEqual(self.fake.calls, [])

        self.new_owner_message("almost " + challenge["exact_phrase"])
        wrong_text = self.tool("confirm", challenge_id=challenge["challenge_id"])
        self.assertIn("does not exactly match", wrong_text)
        self.assertEqual(self.fake.calls, [])

        self.new_owner_message(challenge["exact_phrase"], message_id=102)
        confirmed_raw = self.tool("confirm", challenge_id=challenge["challenge_id"])
        confirmed = json.loads(confirmed_raw)
        self.assertEqual(confirmed["receipt"]["receipt"]["status"], "ok")
        self.assertEqual(confirmed["challenge"]["status"], "completed")
        self.assertEqual(len(self.fake.calls), 1)
        self.assertIsInstance(self.fake.calls[0], functions.auth.LogOutRequest)
        self.assertNotIn("token_urlsafe", confirmed_raw)
        self.assertNotIn("CriticalConfirmation", confirmed_raw)

        replay = self.tool("confirm", challenge_id=challenge["challenge_id"])
        self.assertIn("cannot be replayed", replay)
        self.assertEqual(len(self.fake.calls), 1)

    def test_pending_and_cancel_confirmation_are_owner_dm_operations(self):
        created = json.loads(
            self.tool("call", request="functions.auth.LogOutRequest")
        )["challenge"]
        pending = json.loads(self.tool("pending_confirmations"))
        self.assertEqual(pending["count"], 1)
        self.assertEqual(pending["items"][0]["challenge_id"], created["challenge_id"])

        cancelled = json.loads(self.tool(
            "cancel_confirmation", challenge_id=created["challenge_id"],
        ))
        self.assertTrue(cancelled["cancelled"])
        after = json.loads(self.tool("pending_confirmations"))
        self.assertEqual(after["items"], [])
        self.assertEqual(self.fake.calls, [])

    def test_praxis_self_is_sovereign_but_human_gate_is_repeated(self):
        internal = runner.agent.ChannelContext(
            principal_id=runner.agent.PRAXIS_SELF_PRINCIPAL,
            is_dm=True, owner=False, known=True, _scope_override="owner",
        )
        token = runner.agent._TURN_CHANNEL.set(internal)
        try:
            result = json.loads(self.tool(
                "call", request="functions.messages.SendMessageRequest",
                params={"peer": "me", "message": "from Praxis"},
            ))
        finally:
            runner.agent._TURN_CHANNEL.reset(token)
        self.assertEqual(result["receipt"]["status"], "ok")
        self.assertEqual(result["receipt"]["principal"], runner.agent.PRAXIS_SELF_PRINCIPAL)
        self.assertEqual(len(self.fake.calls), 1)

        trusted = runner.agent.ChannelContext(
            chat_id="99", principal_id="99", is_dm=True, owner=False, known=True,
        )
        token = runner.agent._TURN_CHANNEL.set(trusted)
        try:
            # Решение Егора 26.07 (вариант 1): в её ходе действует ОНА, кто бы ни
            # заговорил. Сырой MTProto — её собственный аккаунт, и чужая реплика не
            # делает её на нём гостем. Раньше здесь ожидался отказ.
            allowed_by_agent = self.tool("search", query="send message")
            self.assertNotIn("Отказ", allowed_by_agent)
            allowed_by_runner = runner._sync_telegram_account(
                action="search", query="send message"
            )
            self.assertNotIn("Отказ", allowed_by_runner)
        finally:
            runner.agent._TURN_CHANNEL.reset(token)
        self.assertEqual(len(self.fake.calls), 1,
                         'search не доходит до фейкового клиента — важно, что он больше не отказ')

        # Even an erroneously owner-labelled context cannot substitute another
        # principal id for PRAXIS_OWNER_ID at the runner/dispatcher boundary.
        context = runner.agent.ChannelContext(
            chat_id="99", principal_id="99", is_dm=True, owner=True, known=True
        )
        token = runner.agent._TURN_CHANNEL.set(context)
        try:
            denied_wrong_principal = self.tool("search", query="send message")
        finally:
            runner.agent._TURN_CHANNEL.reset(token)
        self.assertIn("только владелец", denied_wrong_principal)
        self.assertEqual(len(self.fake.calls), 1)

    def test_praxis_self_requests_critical_action_and_owner_confirms_exact_intent(self):
        internal = runner.agent.ChannelContext(
            principal_id=runner.agent.PRAXIS_SELF_PRINCIPAL,
            is_dm=True, owner=False, known=True, _scope_override="owner",
        )
        token = runner.agent._TURN_CHANNEL.set(internal)
        try:
            self.execution = {
                "run_id": "run-praxis-self", "call_id": "tool-self-critical",
                "tool": "telegram_account", "args": {}, "side_effect": True,
                "idempotency_key": "telegram-critical-self-one",
            }
            created = json.loads(self.tool(
                "call", request="functions.auth.LogOutRequest",
            ))
            pending = json.loads(self.tool("pending_confirmations"))
        finally:
            runner.agent._TURN_CHANNEL.reset(token)

        challenge = created["challenge"]
        self.assertEqual(created["action"], "challenge")
        self.assertEqual(challenge["requested_by"], runner.agent.PRAXIS_SELF_PRINCIPAL)
        self.assertEqual(pending["count"], 1)
        self.assertEqual(self.fake.calls, [])

        self.new_owner_message(challenge["exact_phrase"], message_id=120)
        confirmed = json.loads(self.tool(
            "confirm", challenge_id=challenge["challenge_id"],
        ))
        self.assertEqual(confirmed["requested_by"], runner.agent.PRAXIS_SELF_PRINCIPAL)
        self.assertEqual(confirmed["confirmed_by"], "42")
        receipt = confirmed["receipt"]["receipt"]
        self.assertEqual(receipt["principal"], runner.agent.PRAXIS_SELF_PRINCIPAL)
        self.assertEqual(receipt["requested_by"], runner.agent.PRAXIS_SELF_PRINCIPAL)
        self.assertEqual(receipt["confirmed_by"], "42")
        self.assertTrue(receipt["receipt_id"])
        self.assertNotIn("receipt_sha256", receipt)
        self.assertNotIn("submitted_parameters", receipt)
        self.assertNotIn("serialized_parameters", receipt)
        self.assertNotIn("result", receipt)
        self.assertEqual(len(self.fake.calls), 1)
        self.assertIsInstance(self.fake.calls[0], functions.auth.LogOutRequest)


if __name__ == "__main__":
    unittest.main()
