"""Transport normalization and the narrow outbound data-authority boundary."""
import sys
import types
import unittest
from unittest import mock

# Заглушки до импорта agent: load_dotenv(override=True) в agent НЕ должен тащить боевой
# .env в os.environ (иначе тесты зависят от порядка и от деплой-конфига). См. §4.
_fa = types.ModuleType("anthropic")
_fa.Anthropic = lambda **kw: None
sys.modules.setdefault("anthropic", _fa)
_fd = types.ModuleType("dotenv")
_fd.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _fd)

import agent  # noqa: E402


class TestStripThink(unittest.TestCase):
    def test_plain_unchanged(self):
        self.assertEqual(agent._strip_think("просто ответ по делу"), "просто ответ по делу")

    def test_full_block_before_answer(self):
        self.assertEqual(agent._strip_think("<think>рассуждаю</think>ответ"), "ответ")

    def test_full_block_after_answer(self):
        self.assertEqual(agent._strip_think("ответ<think>хвост-мысль</think>"), "ответ")

    def test_block_with_newlines(self):
        self.assertEqual(
            agent._strip_think("<think>про jome doe\nдумаю</think>\n\nответ по делу"),
            "ответ по делу",
        )

    def test_dangling_close_keeps_answer_tail(self):
        # нормальный формат GLM: рассуждение, затем </think>, затем ответ
        self.assertEqual(agent._strip_think("рассуждение про скилл</think>вот ответ"), "вот ответ")

    def test_open_tag_stripped(self):
        self.assertEqual(agent._strip_think("<think>x ответ"), "x ответ")

    def test_empty(self):
        self.assertEqual(agent._strip_think(""), "")


class TestPrivateSpeechBoundary(unittest.TestCase):
    def setUp(self):
        self.old_evaluate = agent.evaluate_reply
        self.old_journal = agent.tool_journal
        self.old_resolve = agent._resolve_unanswered
        self.old_notes_append = agent.notes.append
        self.old_said_recently = agent.notes.said_recently
        agent.tool_journal = lambda *_args, **_kwargs: ""
        agent._resolve_unanswered = lambda *_args, **_kwargs: None
        agent.notes.append = lambda *_args, **_kwargs: None

    def tearDown(self):
        agent.evaluate_reply = self.old_evaluate
        agent.tool_journal = self.old_journal
        agent._resolve_unanswered = self.old_resolve
        agent.notes.append = self.old_notes_append
        agent.notes.said_recently = self.old_said_recently

    def test_owner_dm_bypasses_data_advisor_verbatim(self):
        calls = []

        def forbidden(name):
            def fail(*_args, **_kwargs):
                calls.append(name)
                raise AssertionError(f"{name} must not run in owner DM")
            return fail

        agent.evaluate_reply = forbidden("evaluate")
        ctx = agent.ChannelContext(chat_id="owner", is_dm=True, owner=True, known=True)
        draft = "Результат тула: отправила → Контакт (@example_user, id 234567890)"

        self.assertEqual(agent.guard_outbound_reply(draft, ctx=ctx), draft)
        quoted_control = "Я буквально сказала [молчу], а потом продолжила."
        suffixed_control = "[молчу] — здесь это цитата, не команда."
        self.assertEqual(agent.guard_outbound_reply(quoted_control, ctx=ctx), quoted_control)
        self.assertEqual(agent.guard_outbound_reply(suffixed_control, ctx=ctx), suffixed_control)
        self.assertEqual(agent.guard_outbound_reply("[молчу]", ctx=ctx), "")
        self.assertEqual(calls, [])

    def test_praxis_self_to_owner_dm_uses_owner_audience_bypass(self):
        def forbidden(*_args, **_kwargs):
            raise AssertionError("owner-audience output must not reach evaluator")

        agent.evaluate_reply = forbidden
        ctx = agent.ChannelContext(
            chat_id="owner", principal_id=agent.PRAXIS_SELF_PRINCIPAL,
            is_dm=True, owner=False, known=True, _scope_override="owner",
        )
        draft = "Я сама решила написать тебе — без второго голоса."

        self.assertTrue(ctx.owner_audience)
        self.assertFalse(ctx.owner)
        self.assertEqual(agent.guard_outbound_reply(draft, ctx=ctx), draft)

    def test_nonowner_scope_override_does_not_bypass_private_guard(self):
        calls = []

        def hold(*_args, **_kwargs):
            calls.append(True)
            return "deny", "privacy:test"

        agent.evaluate_reply = hold
        ctx = agent.ChannelContext(
            chat_id="owner", principal_id="999", is_dm=True, owner=False,
            known=True, _scope_override="owner",
        )
        self.assertFalse(ctx.owner_audience)
        self.assertEqual(agent.guard_outbound_reply("не должно обойти guard", ctx=ctx), "")
        self.assertEqual(calls, [True])

    def test_private_dm_does_not_cut_authored_speaker_or_mode_lines(self):
        agent.evaluate_reply = lambda *_args, **_kwargs: ("ok", "")
        ctx = agent.ChannelContext(chat_id="known", is_dm=True, owner=False, known=True)
        draft = "Мой текст.\nЕгор: это цитата\nРЕЖИМ: тише"
        convo = "Егор: исходное сообщение"

        self.assertEqual(agent.guard_outbound_reply(draft, convo, ctx=ctx), draft)

    def test_private_dm_style_verdict_has_no_authority(self):
        seen = {}

        def fake_evaluate(_reply, _orient, **kwargs):
            seen.update(kwargs)
            return "правь", "лесть [form:flattery]"

        agent.evaluate_reply = fake_evaluate
        ctx = agent.ChannelContext(chat_id="known", is_dm=True, owner=False, known=True)

        self.assertEqual(agent.guard_outbound_reply("Сырой ответ.", ctx=ctx), "Сырой ответ.")
        self.assertTrue(seen["privacy_only"])

    def test_private_dm_has_no_code_level_anti_repeat(self):
        agent.evaluate_reply = lambda *_args, **_kwargs: ("ok", "")
        agent.notes.said_recently = lambda *_args, **_kwargs: True
        ctx = agent.ChannelContext(chat_id="known", is_dm=True, owner=False, known=True)

        out = agent.guard_outbound_reply("Повторяю свой ответ.", "Гость: [Стикер]", ctx=ctx)
        self.assertEqual(out, "Повторяю свой ответ.")

    def test_non_owner_dm_still_holds_cross_person_secret(self):
        agent.evaluate_reply = lambda *_args, **_kwargs: ("deny", "чужой секрет")
        ctx = agent.ChannelContext(chat_id="known", is_dm=True, owner=False, known=True)

        self.assertEqual(agent.guard_outbound_reply("Секрет Маши", ctx=ctx), "")

    def test_privacy_advisor_runs_independently_of_removed_speech_evaluator(self):
        response = types.SimpleNamespace(text=agent._PRIVATE_DM_PRIVACY_OK)
        with mock.patch.object(agent.llm, "configured", return_value=True), \
                mock.patch.object(agent.llm, "chat", return_value=response) as chat:
            verdict = self.old_evaluate(
                "обычная реплика", privacy_only=True,
                audience_accepts_private=False,
            )

        self.assertEqual(verdict, ("ok", ""))
        chat.assert_called_once()

    def test_privacy_only_unconfigured_is_explicitly_unavailable(self):
        with mock.patch.object(agent.llm, "configured", return_value=False), \
                mock.patch.object(agent.llm, "chat",
                                  side_effect=AssertionError("unconfigured role must not be called")):
            verdict = self.old_evaluate(
                "возможный секрет", privacy_only=True,
                audience_accepts_private=False,
            )

        self.assertEqual(verdict,
                         ("unavailable", "privacy advisor unavailable for non-owner audience"))

    def test_privacy_only_accepts_only_closed_exact_codes(self):
        malformed = ("", "ok", "молчи: стиль", "PRIVACY_HOLD_STYLE",
                     "PRIVACY_OK because it seems fine")
        for raw in malformed:
            with self.subTest(raw=raw), \
                    mock.patch.object(agent.llm, "configured", return_value=True), \
                    mock.patch.object(agent.llm, "chat",
                                      return_value=types.SimpleNamespace(text=raw)):
                verdict = self.old_evaluate(
                    "возможный секрет", privacy_only=True,
                    audience_accepts_private=False,
                )
            self.assertEqual(verdict,
                             ("unavailable", "privacy advisor returned malformed verdict"))

        for code, reason in agent._PRIVATE_DM_PRIVACY_HOLDS.items():
            with self.subTest(code=code), \
                    mock.patch.object(agent.llm, "configured", return_value=True), \
                    mock.patch.object(agent.llm, "chat",
                                      return_value=types.SimpleNamespace(text=code)):
                verdict = self.old_evaluate(
                    "возможный секрет", privacy_only=True,
                    audience_accepts_private=False,
                )
            self.assertEqual(verdict, ("deny", reason))

    def test_group_guard_sends_benign_reply_when_advisor_is_unavailable(self):
        agent.evaluate_reply = lambda *_args, **_kwargs: (
            "unavailable", "privacy advisor unavailable for non-owner audience")
        ctx = agent.ChannelContext(chat_id="-100", is_dm=False, owner=False, known=True)
        turn = {"kind": "chat"}

        self.assertEqual(agent.guard_outbound_reply("Обычная реплика", ctx=ctx, turn=turn),
                         "Обычная реплика")
        self.assertEqual(turn["advisor_verdict"], "unavailable")
        self.assertNotEqual(turn.get("held"), "privacy")

    def test_group_guard_still_holds(self):
        agent.evaluate_reply = lambda *_args, **_kwargs: ("deny", "утечка в группу")
        ctx = agent.ChannelContext(chat_id="-100", is_dm=False, owner=False, known=True)

        self.assertEqual(agent.guard_outbound_reply("Секрет Маши", ctx=ctx), "")


if __name__ == "__main__":
    unittest.main()
