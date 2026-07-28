from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent
import computer_access
import run_context
import run_manager


class AuthorityContextTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"PRAXIS_OWNER_ID": "101"})
        self.env.start()
        self.saved_scope = agent._CURRENT_SCOPE
        agent._CURRENT_SCOPE = "unknown"

    def tearDown(self):
        agent._CURRENT_SCOPE = self.saved_scope
        self.env.stop()

    def _channel(self, principal, *, owner=False, is_dm=True):
        return agent.ChannelContext(
            chat_id="-10077__topic__9" if not is_dm else str(principal),
            room_id="-10077" if not is_dm else None,
            principal_id=principal, is_dm=is_dm, owner=owner, known=True,
        )

    def test_owner_scope_or_boolean_without_numeric_principal_fails_closed(self):
        agent._CURRENT_SCOPE = "owner"
        self.assertFalse(agent._is_human_owner())
        token = agent._TURN_CHANNEL.set(agent.ChannelContext(owner=True, is_dm=True))
        try:
            self.assertFalse(agent._is_human_owner())
            self.assertIn("только владелец", agent.tool_admit("Маша", id="202"))
        finally:
            agent._TURN_CHANNEL.reset(token)

    def test_owner_in_group_topic_is_proven_by_sender_user_id(self):
        token = agent._TURN_CHANNEL.set(self._channel(101, owner=True, is_dm=False))
        try:
            self.assertTrue(agent._is_human_owner())
            with mock.patch.object(agent.rooms, "list_rooms", return_value=["-10077"]):
                self.assertIn("-10077", agent.tool_manage_room("list"))
        finally:
            agent._TURN_CHANNEL.reset(token)

    def test_delegating_trust_stays_with_the_human_owner(self):
        """Решение Егора 26.07, вариант 1: в её ходе действует ОНА, кто бы ни заговорил. «Она и сама уже сейчас откажет, если там бред.» За Егором остались только admit и computer_access — раздача ЕГО доверия другим людям, а не её способности.

        Раздать доверие — не её способность, а его: `admit` делает человека доверенным,
        `computer_access` выдаёт кому-то доступ к его машине. Действует при этом она
        сама (`_computer_actor` = praxis:self), и это правильно: руки её.
        """
        token = agent._TURN_CHANNEL.set(self._channel(202))
        try:
            self.assertFalse(agent._is_human_owner())
            self.assertIn("только владелец", agent.tool_admit("Друг", id="303"))
            result = agent.tool_computer_access("list")
            self.assertIn("только владелец", result.lower())
            self.assertEqual(agent._computer_actor(), agent.PRAXIS_SELF_PRINCIPAL,
                             "в её ходе действует она, а не заговоривший человек")
        finally:
            agent._TURN_CHANNEL.reset(token)

    def test_bound_run_principal_is_valid_but_live_channel_takes_precedence(self):
        run = run_context.RunContext.create(
            kind="voice", goal="authority test", principal_id="101", scope="owner",
        )
        with run_context.bind_run(run):
            self.assertTrue(agent._is_human_owner())
            token = agent._TURN_CHANNEL.set(self._channel(202))
            try:
                self.assertFalse(agent._is_human_owner())
            finally:
                agent._TURN_CHANNEL.reset(token)

    def test_praxis_self_has_full_hands_but_cannot_delegate_human_trust(self):
        internal = agent.ChannelContext(
            principal_id=agent.PRAXIS_SELF_PRINCIPAL, owner=False, known=True,
            _scope_override="owner",
        )
        token = agent._TURN_CHANNEL.set(internal)
        try:
            self.assertTrue(internal.praxis_self)
            self.assertFalse(agent._is_human_owner())
            self.assertTrue(agent._is_praxis_self())
            self.assertTrue(agent._is_sovereign_actor())
            names = {str(tool.get("name") or "") for tool in agent.PRAXIS_SELF_TOOLS}
            self.assertTrue(
                {"shell", "send_message", "manage_room", "telegram_account", "computer"}
                .issubset(names)
            )
            self.assertTrue({"admit", "computer_access"}.isdisjoint(names))
            self.assertEqual(agent._computer_actor(), agent.PRAXIS_SELF_PRINCIPAL)
            self.assertTrue(agent._computer_allowed("computer.process"))
            self.assertFalse(agent._computer_allowed("computer.unknown"))
        finally:
            agent._TURN_CHANNEL.reset(token)

    def test_raw_account_hook_is_hers_in_every_turn(self):
        calls = []
        saved = agent._TELETHON.get("telegram_account")
        agent._TELETHON["telegram_account"] = lambda **kw: calls.append(kw) or "ok"
        try:
            # Решение Егора 26.07, вариант 1: в её ходе действует ОНА, кто бы ни заговорил. «Она и сама уже сейчас откажет, если там бред.» За Егором остались только admit и computer_access — раздача ЕГО доверия другим людям, а не её способности.
            # Сырой MTProto — её собственный аккаунт и её тело; чужая реплика не делает
            # её безрукой. Раньше здесь ожидался отказ «только владелец».
            trusted = agent._TURN_CHANNEL.set(self._channel(202))
            try:
                self.assertEqual(agent.tool_telegram_account("list"), "ok")
            finally:
                agent._TURN_CHANNEL.reset(trusted)
            owner = agent._TURN_CHANNEL.set(self._channel(101, owner=True, is_dm=False))
            try:
                self.assertEqual(agent.tool_telegram_account("list"), "ok")
            finally:
                agent._TURN_CHANNEL.reset(owner)
            internal = agent._TURN_CHANNEL.set(agent.ChannelContext(
                principal_id=agent.PRAXIS_SELF_PRINCIPAL, is_dm=True, known=True,
                _scope_override="owner",
            ))
            try:
                self.assertEqual(agent.tool_telegram_account("list"), "ok")
            finally:
                agent._TURN_CHANNEL.reset(internal)
            # три вызова: доверенный человек, Егор, её собственный ход — во всех действует она
            self.assertEqual(len(calls), 3)
        finally:
            if saved is None:
                agent._TELETHON.pop("telegram_account", None)
            else:
                agent._TELETHON["telegram_account"] = saved

    def test_mail_reading_is_hers_and_sending_stays_owner(self):
        offered = []

        def terminal(*, tools, **kwargs):
            offered.append({tool.get("name") for tool in tools if isinstance(tool, dict)})
            return "ok"

        contexts = [
            self._channel(101, owner=True),
            agent.ChannelContext(
                principal_id=agent.PRAXIS_SELF_PRINCIPAL, is_dm=True, known=True,
                _scope_override="owner",
            ),
            self._channel(202),
        ]
        with (
            mock.patch.object(agent, "_build_prompt_parts", return_value=("persona", "dynamic", "")),
            mock.patch.object(agent, "_system", return_value="system"),
            mock.patch.object(agent, "_terminal_tool_loop", side_effect=terminal),
            mock.patch.object(agent.mailer, "configured", return_value=True),
            mock.patch.object(agent.webtool, "enabled", return_value=False),
            mock.patch.object(agent, "_hosted_web_search_tool", return_value=None),
        ):
            for context in contexts:
                self.assertEqual(agent._voice_impl("hello", [], None, ctx=context), "ok")

        inbound_mail_tools = {"mail_read", "mail_draft_reply"}
        self.assertTrue(inbound_mail_tools <= offered[0])
        self.assertIn("send_email", offered[0])
        self.assertTrue(inbound_mail_tools <= offered[1])
        self.assertNotIn("send_email", offered[1])
        # Решение Егора 26.07: читать письмо и готовить черновик — её работа в любом
        # ходе. Отправка наружу остаётся отдельным owner-действием (mailroom approval).
        self.assertTrue(inbound_mail_tools <= offered[2])
        self.assertNotIn("send_email", offered[2])

    def test_room_mutations_preserve_owner_vs_self_and_legacy_goals_are_absent(self):
        mode_calls = []
        with (
            mock.patch.object(
                agent.rooms, "set_mode",
                side_effect=lambda *args, **kwargs: mode_calls.append((args, kwargs)) or "frozen",
            ),
            mock.patch.object(
                agent.rooms, "sovereign_raise",
                side_effect=lambda *args, **kwargs: mode_calls.append((args, kwargs)) or "quiet",
            ),
            mock.patch.object(agent, "tool_journal", return_value="ok"),
        ):
            owner = agent._TURN_CHANNEL.set(self._channel(101, owner=True))
            try:
                agent.tool_freeze_chat(True, chat_id="-1001")
            finally:
                agent._TURN_CHANNEL.reset(owner)

            internal = agent._TURN_CHANNEL.set(agent.ChannelContext(
                principal_id=agent.PRAXIS_SELF_PRINCIPAL, known=True,
                _scope_override="owner",
            ))
            try:
                agent.tool_freeze_chat(True, chat_id="-1002")
                agent.tool_freeze_chat(False, chat_id="-1002")
            finally:
                agent._TURN_CHANNEL.reset(internal)

            # Решение Егора 26.07 (вариант 1): в её ходе действует она, кто бы ни
            # заговорил. Раньше здесь ожидался «Отказ» — то есть чужая реплика делала
            # её безрукой в её же комнатах. Авторство при этом остаётся честным:
            # set_by='praxis', а не 'owner' — решила она, а не Егор.
            trusted = agent._TURN_CHANNEL.set(self._channel(202))
            try:
                self.assertNotIn("Отказ", agent.tool_freeze_chat(True, chat_id="-1003"))
            finally:
                agent._TURN_CHANNEL.reset(trusted)

        self.assertEqual(mode_calls[0][1]["set_by"], "owner")
        self.assertEqual(mode_calls[1][1]["set_by"], "praxis")
        self.assertEqual(mode_calls[2][1]["set_by"], "praxis")
        self.assertEqual(mode_calls[3][1]["set_by"], "praxis")
        self.assertFalse(hasattr(agent, "tool_set_goal"))
        self.assertNotIn("set_goal", agent.TOOL_IMPL)
        self.assertIn("manage_desire", agent.TOOL_IMPL)

    def test_owner_origin_comes_only_from_hash_verified_run_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="praxis-origin-evidence-") as temp:
            manager = run_manager.RunManager(Path(temp))
            channel = agent.ChannelContext(
                chat_id="101", room_id="101", principal_id="101",
                origin_message_id=777,
                origin_text="ПОДТВЕРЖДАЮ TELEGRAM tgcritical_0123456789abcdef",
                is_dm=True, owner=True, known=True, addressed=True,
                address_message_id=777, address_kind="direct",
                reply_targets=((777, "Yegor", "visible transcript may differ"),),
            )
            context = run_context.RunContext.create(
                run_id="run-origin-owner", kind="chat_turn", goal="confirm",
                principal_id="101", scope="owner", origin_chat_id="101",
                origin_message_ids=agent._run_origin_message_ids(channel),
                delivery_chat_id="101", model_profile="voice",
            )
            persisted = manager.create(
                context,
                agent._run_context_markdown(
                    ctx=channel, kind=context.kind, goal=context.goal,
                    conversation="mutable-looking transcript", history=[], extra="",
                ),
            )
            mutable = agent._TURN_CHANNEL.set(agent.ChannelContext(
                chat_id="101", principal_id="101", origin_message_id=999,
                origin_text="forged process-local text", is_dm=True, owner=True,
            ))
            prior_manager = agent._RUN_MANAGER
            agent._RUN_MANAGER = manager
            try:
                with run_context.bind_run(persisted):
                    evidence = agent.current_origin_evidence()
                    self.assertEqual(evidence, {
                        "run_id": persisted.run_id,
                        "chat_id": "101",
                        "message_id": 777,
                        "principal_id": "101",
                        "is_dm": True,
                        "raw_text": channel.origin_text,
                    })

                    # A modified context.md no longer authenticates the origin.
                    (manager.path(persisted.run_id) / "context.md").write_text(
                        "tampered", encoding="utf-8",
                    )
                    self.assertIsNone(agent.current_origin_evidence())
            finally:
                agent._RUN_MANAGER = prior_manager
                agent._TURN_CHANNEL.reset(mutable)


if __name__ == "__main__":
    unittest.main()
