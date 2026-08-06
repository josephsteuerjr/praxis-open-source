"""Audience scope: full internal perception, disclosure checked at outbound."""
import sys
import tempfile
import types
import unittest
from pathlib import Path

# Заглушки до импорта agent: load_dotenv(override=True) в agent НЕ должен тащить боевой
# .env в os.environ (иначе тесты зависят от порядка и от деплой-конфига). См. §4.
_fa = types.ModuleType("anthropic")
_fa.Anthropic = lambda **kw: None
sys.modules.setdefault("anthropic", _fa)
_fd = types.ModuleType("dotenv")
_fd.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _fd)

import agent  # noqa: E402
import memory_index  # noqa: E402
import people  # noqa: E402


class TestScopeOf(unittest.TestCase):
    def test_owner(self):
        self.assertEqual(agent._scope_of(True, True, True), "owner")

    def test_known_dm(self):
        self.assertEqual(agent._scope_of(True, False, True), "known")

    def test_unknown_dm(self):
        self.assertEqual(agent._scope_of(True, False, False), "unknown")

    def test_group(self):
        self.assertEqual(agent._scope_of(False, False, True), "group")
        # даже владелец в группе → group: его реплику видят все, личка не должна течь
        self.assertEqual(agent._scope_of(False, True, True), "group")


class TestChannelContext(unittest.TestCase):
    """Единый объект канала: деривация scope, разделение owner-факта и приватности, осознанность."""

    def test_scope_derivation(self):
        CC = agent.ChannelContext
        self.assertEqual(CC(is_dm=True, owner=True).scope, "owner")
        self.assertEqual(CC(is_dm=True, owner=False, known=True).scope, "known")
        self.assertEqual(CC(is_dm=True, owner=False, known=False).scope, "unknown")
        self.assertEqual(CC(is_dm=False).scope, "group")

    def test_owner_in_group_stays_group_but_owner_flag_kept(self):
        # owner в группе: аудитория group, но Praxis не ампутирует собственную память.
        cc = agent.ChannelContext(is_dm=False, owner=True, chat_id="-100")
        self.assertEqual(cc.scope, "group")
        self.assertTrue(cc.owner)
        self.assertTrue(cc.sees_private)
        self.assertTrue(cc.sees_journal)
        self.assertFalse(cc.audience_accepts_private)

    def test_visibility_flags(self):
        CC = agent.ChannelContext
        for ctx in (CC(is_dm=True, owner=True), CC(is_dm=True, owner=False, known=True),
                    CC(is_dm=False)):
            self.assertTrue(ctx.sees_journal)
            self.assertTrue(ctx.sees_private)
        self.assertTrue(CC(is_dm=True, owner=True).audience_accepts_private)
        self.assertFalse(CC(is_dm=True, owner=False).audience_accepts_private)
        self.assertFalse(CC(is_dm=False, owner=True).audience_accepts_private)

    def test_praxis_self_owner_audience_is_not_human_owner_authority(self):
        cc = agent.ChannelContext(
            chat_id="100", principal_id=agent.PRAXIS_SELF_PRINCIPAL,
            is_dm=True, owner=False, known=True, _scope_override="owner",
        )
        self.assertTrue(cc.owner_audience)
        self.assertTrue(cc.audience_accepts_private)
        self.assertFalse(cc.owner, "destination must not forge human-owner actor authority")
        self.assertTrue(cc.praxis_self)
        self.assertIn("with Yegor", agent._presence_frame(cc))
        _, dynamic = agent.build_system_parts(speaker="Егор", ctx=cc)
        self.assertIn("private owner channel", dynamic)

    def test_scope_override_alone_cannot_forge_owner_audience(self):
        cc = agent.ChannelContext(
            chat_id="100", principal_id="999", is_dm=True, owner=False,
            known=True, _scope_override="owner",
        )
        self.assertEqual(cc.scope, "owner")
        self.assertFalse(cc.owner_audience)
        self.assertFalse(cc.audience_accepts_private)

    def test_owner_actor_in_group_keeps_state_context_but_public_audience(self):
        old_state = agent.build_state_block
        old_home = agent.HOME_MD
        agent.build_state_block = lambda **_kw: "STATE_OWNER_GROUP_MARKER"
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "HOME.md"
            home.write_text("PRIVATE_HOME_GROUP_MARKER", encoding="utf-8")
            agent.HOME_MD = home
            try:
                cc = agent.ChannelContext(
                    chat_id="-100", principal_id="100", is_dm=False,
                    owner=True, known=True,
                )
                _, dynamic = agent.build_system_parts(speaker="Егор", ctx=cc)
            finally:
                agent.build_state_block = old_state
                agent.HOME_MD = old_home
        self.assertIn("STATE_OWNER_GROUP_MARKER", dynamic)
        self.assertIn("pending reply is still public", dynamic)
        self.assertNotIn("PRIVATE_HOME_GROUP_MARKER", dynamic)
        self.assertFalse(cc.owner_audience)

    def test_scope_override_respected(self):
        # from_legacy с явным scope уважает его (легаси/тестовые вызовы)
        cc = agent.ChannelContext.from_legacy("-100", is_dm=True, owner=False, scope="group")
        self.assertEqual(cc.scope, "group")

    def test_awareness_line_group_vs_dm(self):
        # ⚠ 03.08.2026: размеры 800 и 5 сравнивались с ПЛАВАЮЩИМ порогом
        # (PRAXIS_GROUP_BIG). Задаём порог сами — тогда проверяется ветка, а не
        # настройка сервера.
        self.assertEqual(agent.GROUP_BIG_THRESHOLD, 50, "порог не тот, что в коде")
        big = agent.ChannelContext(is_dm=False, title="abstractDL", size=800, chat_id="-100")
        line = big.awareness_line()
        self.assertIn("abstractDL", line)
        self.assertIn("800", line)
        self.assertIn("-100", line)
        self.assertIn("большой публичный", line)  # size >= порога
        small = agent.ChannelContext(is_dm=False, title="кухня", size=5)
        self.assertNotIn("большой публичный", small.awareness_line())
        self.assertEqual(agent.ChannelContext(is_dm=True, owner=True).awareness_line(), "")  # личка — пусто


class TestRecallScopeFilter(unittest.TestCase):
    def test_owner_sees_everything(self):
        self.assertTrue(memory_index._scope_allows("2026-06-29", "- [private] секрет", "owner"))

    def test_group_drops_private(self):
        self.assertFalse(memory_index._scope_allows("yegor-kosyrev", "- [private] секрет", "group"))

    def test_group_drops_journal_day_files(self):
        self.assertFalse(memory_index._scope_allows("2026-06-29", "- наблюдение", "group"))

    def test_group_drops_index_and_reflections(self):
        self.assertFalse(memory_index._scope_allows("INDEX", "- [yegor]", "group"))
        self.assertFalse(memory_index._scope_allows("reflections", "вывод во сне", "group"))

    def test_group_drops_other_room_context(self):
        self.assertFalse(memory_index._scope_allows("-1009990000002", "контекст чужой комнаты", "group"))

    def test_group_keeps_public_people_and_skills(self):
        self.assertTrue(memory_index._scope_allows("yegor-kosyrev", "- [public] любит grep", "group"))
        self.assertTrue(memory_index._scope_allows("holding_ground", "держу достоинство", "group"))


class TestPortraitPrivateFilter(unittest.TestCase):
    SLUG = "zz-test-scope-person"

    def setUp(self):
        people.write(self.SLUG, "ZZ Test", {people.FACTS: "- [public] любит grep\n- [private] секрет про семью"})

    def tearDown(self):
        p = people.path_for(self.SLUG)
        if p.exists():
            p.unlink()

    def test_group_hides_private(self):
        prose, _ = people.portrait_and_loops(self.SLUG, include_private=False)
        self.assertIn("любит grep", prose)
        self.assertNotIn("секрет про семью", prose)

    def test_dm_shows_private(self):
        prose, _ = people.portrait_and_loops(self.SLUG, include_private=True)
        self.assertIn("секрет про семью", prose)


class TestRecallToolUsesScope(unittest.TestCase):
    def test_tool_recall_keeps_full_internal_scope(self):
        captured = {}
        orig = memory_index.search
        agent._CURRENT_SCOPE = "group"
        memory_index.search = lambda q, k=6, scope="owner": (captured.__setitem__("scope", scope), [])[1]
        try:
            agent.tool_recall("что-то")
        finally:
            memory_index.search = orig
            agent._CURRENT_SCOPE = "owner"
        self.assertEqual(captured.get("scope"), "owner",
                         "audience must not amputate Praxis's internal recall")

    def test_automatic_recall_never_injects_neighbour_dm_summary(self):
        orig = memory_index.search
        memory_index.search = lambda *_a, **_kw: [
            {"source": "809", "path": "memory/dialogues/809.md", "text": "RAW_DM_MARKER"},
            {"source": "0001", "path": "soul/self/history/0001.md",
             "source_type": "self_history", "text": "ARCHIVED_SELF_MARKER"},
            {"source": "legacy", "path": "memory/people/legacy.md", "text": "UNSOURCED_LEGACY"},
            {"source": "вася", "path": "memory/people/вася.md",
             "text": "DURABLE_FACT [source:clm-123]"},
            {"source": "safe", "path": "soul/skills/safe.md", "source_type": "skill",
             "text": "SAFE_SKILL", "automatic_canonical": True},
        ]
        try:
            block = agent._recall_block("что-то", "group")
        finally:
            memory_index.search = orig
        self.assertIn("SAFE_SKILL", block)
        self.assertNotIn("DURABLE_FACT", block)
        self.assertNotIn("UNSOURCED_LEGACY", block)
        self.assertNotIn("RAW_DM_MARKER", block)
        self.assertNotIn("ARCHIVED_SELF_MARKER", block)


class TestBuildSystemPartsScope(unittest.TestCase):
    SLUG = "zz-participant-internal"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="praxis_scope_")
        root = Path(self.tmp.name)
        self.old_people = people.PEOPLE_DIR
        self.old_index = agent.INDEX_MD
        self.old_journal = agent.JOURNAL_DIR
        people.PEOPLE_DIR = root / "people"
        people.PEOPLE_DIR.mkdir(parents=True)
        agent.INDEX_MD = root / "INDEX.md"
        agent.INDEX_MD.write_text("- карта-тест", encoding="utf-8")
        agent.JOURNAL_DIR = root / "journal"
        agent.JOURNAL_DIR.mkdir()
        (agent.JOURNAL_DIR / f"{agent._today()}.md").write_text(
            f"# {agent._today()}\n\n- внутренний дневник-тест\n", encoding="utf-8")
        people.write(self.SLUG, "ZZ Participant Internal", {
            people.WHO: "знакомая Praxis",
            people.FACTS: "- [public] любит grep\n- [private] личный медицинский факт",
        })

    def tearDown(self):
        people.PEOPLE_DIR = self.old_people
        agent.INDEX_MD = self.old_index
        agent.JOURNAL_DIR = self.old_journal
        self.tmp.cleanup()

    def test_group_carries_profiles_but_not_raw_journal_or_neighbour_chat(self):
        ctx = agent.ChannelContext(chat_id="-100", is_dm=False, owner=False, known=True,
                                   reply_targets=((1, "Новый Человек (@newbie)", "привет"),
                                                  (2, "ZZ Participant Internal (@zz)", "ответ")))
        _, dyn, evidence = agent._build_prompt_parts(
            speaker="ZZ Participant Internal (@zz)", query="медицинский", ctx=ctx,
        )
        self.assertNotIn("Карта памяти — ВНУТРЕННЯЯ", dyn)
        self.assertIn("Карта памяти — ВНУТРЕННЯЯ", evidence)
        self.assertNotIn("личный медицинский факт", dyn)
        self.assertNotIn("личный медицинский факт", evidence)
        self.assertNotIn("Новый Человек (@newbie) | пока нет устойчивого досье", dyn)
        self.assertNotIn("ВНУТРЕННИЙ хвост", dyn)
        self.assertNotIn("внутренний дневник-тест", dyn)
        self.assertNotIn("PRIVATE CROSS-CHAT READ", dyn,
                         "raw DMs enter only through an explicit tool call")

    def test_compact_profile_keeps_visibility_labels(self):
        card = people.compact_profile(self.SLUG, include_private=True)
        self.assertIn("[private]", card)
        self.assertIn("[public]", card)
        self.assertIn("legacy-unverified", card)
        self.assertLessEqual(len(card), 700)

    def test_authenticated_principal_resolves_exact_bound_dossier(self):
        people.set_telegram_id(self.SLUG, "ZZ Participant Internal", "424242")
        self.assertEqual(people.slug_for_principal("424242"), self.SLUG)
        self.assertEqual(people.slug_for_principal("424243"), "")


class TestOutboundAudienceFrame(unittest.TestCase):
    def test_guard_passes_audience_and_current_channel_to_advisor(self):
        captured = {}
        old = agent.evaluate_reply
        agent.evaluate_reply = lambda text, context="", **kw: (captured.update(kw) or ("ok", ""))
        try:
            ctx = agent.ChannelContext(chat_id=None, is_dm=False, title="группа")
            out = agent.guard_outbound_reply("ответ", "Вася: только в этой группе", ctx=ctx)
        finally:
            agent.evaluate_reply = old
        self.assertEqual(out, "ответ")
        self.assertFalse(captured["audience_accepts_private"])
        self.assertIn("group/public audience", captured["privacy_frame"])
        self.assertIn("только в этой группе", captured["conversation"])

    def test_non_owner_reports_privacy_advisor_unavailable_without_claiming_a_hold(self):
        old_configured, old_chat = agent.llm.configured, agent.llm.chat
        agent.llm.configured = lambda *_a, **_kw: True

        def boom(*_a, **_kw):
            raise RuntimeError("advisor down")

        agent.llm.chat = boom
        try:
            self.assertEqual(
                agent.evaluate_reply("текст", privacy_frame="group",
                                     audience_accepts_private=False),
                ("unavailable", "privacy advisor unavailable for non-owner audience"))
            self.assertEqual(
                agent.evaluate_reply("текст", privacy_frame="owner",
                                     audience_accepts_private=True),
                ("ok", ""))
        finally:
            agent.llm.configured, agent.llm.chat = old_configured, old_chat


if __name__ == "__main__":
    unittest.main()
