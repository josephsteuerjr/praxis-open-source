"""Mailroom: ingest/дедуп/хэши, state-machine (draft→approve→sent), честный индекс. Герметично."""
import sys
import tempfile
import types
import unittest
from pathlib import Path

import mailroom

# Заглушки до import agent (load_dotenv не тащит боевой .env; Anthropic не нужен здесь). См. §4.
_fa = types.ModuleType("anthropic")
_fa.Anthropic = lambda **kw: None
sys.modules.setdefault("anthropic", _fa)
_fd = types.ModuleType("dotenv")
_fd.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _fd)

import agent  # noqa: E402
import llm  # noqa: E402


def _msg(frm, subj, body, mid="", date="Mon, 30 Jun 2026 00:00:00 +0000"):
    return {"from": frm, "subject": subj, "body": body, "msg_id": mid, "date": date}


class Base(unittest.TestCase):
    """PASS 13.0.b: this used to only sandbox mailroom.MAILBOX, so build_system_parts() (called by
    TestAgentMailTools.test_index_in_owner_context) read the REAL soul/SOUL.md+VOICE.md+self.md off
    disk. self.md grows daily in production (her own journaled distillations) and had already
    crossed ~26K chars -- bigger than the whole default PRAXIS_CONTEXT_BUDGET (24000) on its own,
    before any optional tier is even considered. Whether the mailbox tier then fit depended on how
    much OTHER shared-sandbox journal noise earlier tests in the same process had accumulated --
    order/environment-dependent (this is the historical recall-budget regression
    13.0.b: reproduced consistently in the Linux gate container and in isolated Windows runs, but
    happened to pass amid a full Windows `discover` run). Root cause is real -- as self.md keeps
    growing, the live owner context risks silently dropping mailbox/portrait/loops tiers too, not
    just this test -- see PASS 13 code-review notes. Fix here: sandbox agent's soul/memory paths to
    a private, fixed-size fixture like test_layer7.py/test_heartbeat.py already do, so this test's
    budget arithmetic is deterministic instead of riding on live self.md size."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_mbox_"))
        self.mailbox = self.tmp / "mailbox.json"
        self._orig_mailbox = mailroom.MAILBOX
        mailroom.MAILBOX = self.mailbox

        mem = self.tmp / "memory"
        soul = self.tmp / "soul"
        for d in (mem / "people", mem / "rooms", mem / "journal", soul / "skills"):
            d.mkdir(parents=True, exist_ok=True)
        (soul / "SOUL.md").write_text("# Конституция\n\n## Кто я по характеру\nтёплая.\n", encoding="utf-8")
        (soul / "self.md").write_text("# Self\n", encoding="utf-8")
        self._orig_agent = []
        for k, val in dict(BASE=self.tmp, SOUL_DIR=soul, SKILLS_DIR=soul / "skills", MEM_DIR=mem,
                            PEOPLE_DIR=mem / "people", ROOMS_DIR=mem / "rooms",
                            JOURNAL_DIR=mem / "journal", REFLECTIONS=mem / "reflections.md",
                            INDEX_MD=mem / "INDEX.md", HOME_MD=mem / "home.md").items():
            self._orig_agent.append((k, getattr(agent, k)))
            setattr(agent, k, val)

    def tearDown(self):
        mailroom.MAILBOX = self._orig_mailbox
        for k, val in self._orig_agent:
            setattr(agent, k, val)


class TestIngest(Base):
    def test_assigns_short_hash_and_new_status(self):
        added = mailroom.ingest([_msg("Hope <h@x.com>", "Привет", "тело", mid="<1@x>")])
        self.assertEqual(len(added), 1)
        e = added[0]
        self.assertEqual(len(e["hash"]), mailroom.HASH_LEN)
        self.assertEqual(e["status"], "new")
        self.assertEqual(e["subject"], "Привет")

    def test_dedup_by_message_id(self):
        mailroom.ingest([_msg("a@x", "s", "b", mid="<same@x>")])
        again = mailroom.ingest([_msg("a@x", "s", "b", mid="<same@x>")])
        self.assertEqual(again, [], "повторный Message-ID не должен заводить новую запись")
        self.assertEqual(len(mailroom.list_open()), 1)

    def test_dedup_by_content_when_no_msgid(self):
        mailroom.ingest([_msg("a@x", "s", "одинаковое тело")])
        again = mailroom.ingest([_msg("a@x", "s", "одинаковое тело")])
        self.assertEqual(again, [])

    def test_distinct_messages_get_distinct_hashes(self):
        a = mailroom.ingest([_msg("a@x", "s1", "b1", mid="<1@x>")])[0]
        b = mailroom.ingest([_msg("b@x", "s2", "b2", mid="<2@x>")])[0]
        self.assertNotEqual(a["hash"], b["hash"])
        self.assertEqual(len(mailroom.list_open()), 2)

    def test_empty_subject_falls_back(self):
        e = mailroom.ingest([_msg("a@x", "", "b", mid="<3@x>")])[0]
        self.assertEqual(e["subject"], "(без темы)")

    def test_index_can_exclude_already_seen_hashes(self):
        first = mailroom.ingest([_msg("a@x", "s1", "b1", mid="<4@x>")])[0]
        second = mailroom.ingest([_msg("b@x", "s2", "b2", mid="<5@x>")])[0]
        block = mailroom.index_block(exclude_hashes={first["hash"]})
        self.assertNotIn(first["hash"], block)
        self.assertIn(second["hash"], block)


class TestStateMachine(Base):
    def _one(self):
        return mailroom.ingest([_msg("Hope <h@x.com>", "Re: agents", "вопрос", mid="<m1@x>")])[0]["hash"]

    def test_draft_sets_status(self):
        h = self._one()
        self.assertTrue(mailroom.set_draft(h, "мой ответ"))
        e = mailroom.get(h)
        self.assertEqual(e["status"], "drafted")
        self.assertEqual(e["draft"], "мой ответ")

    def test_draft_missing_hash(self):
        self.assertFalse(mailroom.set_draft("zzzz", "x"))

    def test_approve_only_from_drafted(self):
        h = self._one()
        self.assertFalse(mailroom.approve(h), "approve до черновика недопустим")
        mailroom.set_draft(h, "ответ")
        self.assertTrue(mailroom.approve(h))
        self.assertEqual(mailroom.get(h)["status"], "approved")

    def test_approve_requires_nonempty_draft(self):
        h = self._one()
        mailroom.set_draft(h, "   ")
        self.assertFalse(mailroom.approve(h))

    def test_pending_approved_collects(self):
        h = self._one()
        mailroom.set_draft(h, "готово")
        mailroom.approve(h)
        pend = mailroom.pending_approved()
        self.assertEqual([e["hash"] for e in pend], [h])
        mailroom.mark_sent(h)
        self.assertEqual(mailroom.pending_approved(), [])
        self.assertEqual(mailroom.get(h)["status"], "sent")

    def test_reject(self):
        h = self._one()
        mailroom.set_draft(h, "ответ")
        self.assertTrue(mailroom.reject(h))
        self.assertEqual(mailroom.get(h)["status"], "rejected")
        self.assertFalse(mailroom.approve(h), "после reject approve нельзя")

    def test_sent_not_redraftable(self):
        h = self._one()
        mailroom.set_draft(h, "a")
        mailroom.approve(h)
        mailroom.mark_sent(h)
        self.assertFalse(mailroom.set_draft(h, "b"), "отправленное не переписать")


class TestIndexAndAddress(Base):
    def test_index_empty(self):
        self.assertEqual(mailroom.index_block(), "")

    def test_index_lists_open_with_hash_and_status(self):
        h = mailroom.ingest([_msg("Hope <h@x.com>", "Привет", "t", mid="<i1@x>")])[0]["hash"]
        block = mailroom.index_block()
        self.assertIn(h, block)
        self.assertIn("Hope", block)
        self.assertIn("new", block)

    def test_index_drops_sent(self):
        h = mailroom.ingest([_msg("a@x", "s", "t", mid="<i2@x>")])[0]["hash"]
        mailroom.set_draft(h, "x")
        mailroom.approve(h)
        mailroom.mark_sent(h)
        self.assertEqual(mailroom.index_block(), "", "отправленные не висят в индексе")

    def test_reply_address_parses_angle(self):
        h = mailroom.ingest([_msg("Hope <hope@x.com>", "s", "t", mid="<i3@x>")])[0]["hash"]
        self.assertEqual(mailroom.reply_address(h), "hope@x.com")

    def test_reply_address_plain(self):
        h = mailroom.ingest([_msg("plain@x.com", "s", "t", mid="<i4@x>")])[0]["hash"]
        self.assertEqual(mailroom.reply_address(h), "plain@x.com")


class TestAgentMailTools(Base):
    """Тонкие тулы agent + честный индекс. Ящик — owner-only, в группу не течёт."""

    def _one(self):
        return mailroom.ingest([_msg("Hope <h@x.com>", "Re: agents", "вопрос про память",
                                      mid="<am1@x>")])[0]["hash"]

    def test_mail_read_returns_body(self):
        h = self._one()
        out = agent.tool_mail_read(h)
        self.assertIn("вопрос про память", out)
        self.assertIn("Hope", out)

    def test_mail_read_missing(self):
        self.assertIn("Нет письма", agent.tool_mail_read("zzzz"))

    def test_mail_draft_sets_drafted(self):
        h = self._one()
        out = agent.tool_mail_draft_reply(h, "ответ Хоуп")
        self.assertIn("Жду", out)
        self.assertEqual(mailroom.get(h)["status"], "drafted")
        self.assertEqual(mailroom.get(h)["draft"], "ответ Хоуп")

    def test_mail_draft_empty_rejected(self):
        h = self._one()
        self.assertIn("Пустой", agent.tool_mail_draft_reply(h, "   "))
        self.assertEqual(mailroom.get(h)["status"], "new")

    def test_index_in_owner_context(self):
        h = self._one()
        _, system, evidence = agent._build_prompt_parts(owner=True, is_dm=True, scope="owner")
        self.assertNotIn(h, system, "строки ящика не должны получать SYSTEM-authority")
        self.assertIn(h, evidence, "честный индекс ящика должен быть в lower-role контексте")
        # ⚠ Ярлык тира приезжает в кадр ЗАГОЛОВКОМ секции — капсом, между правилами
        # `────`. Литерал ярлыка в кадре больше не встречается, и это не пропажа:
        # заголовок виден сильнее прежней JSON-строки.
        self.assertIn("ПОЧТОВЫЙ ЯЩИК", evidence)

    def test_autonomous_context_can_suppress_full_mailbox_index(self):
        h = self._one()
        ctx = agent.ChannelContext(
            principal_id=agent.PRAXIS_SELF_PRINCIPAL,
            is_dm=True,
            owner=False,
            known=True,
            _scope_override="owner",
            mailbox_index_override="",
        )
        _, _, evidence = agent._build_prompt_parts(ctx=ctx)
        self.assertNotIn(h, evidence)
        self.assertNotIn("Почтовый ящик", evidence)

    def test_index_is_visible_in_every_room_and_never_as_authority(self):
        """⚠ Здесь стоял приватностный сторож, который НИКОГДА не работал.

        Две прежние проверки заявляли «ящик owner-only, в группу не течёт», но
        смотрели в `build_system_parts()` — а он отдаёт только (persona, dynamic) и
        ВЫБРАСЫВАЕТ третье значение, evidence, куда единственно и попадает тир ящика.
        То есть `assertNotIn("Почтовый ящик", dyn)` не мог упасть ни при каком scope,
        ни при каком owner и ни при каком содержимом ящика.

        03.08.2026 решением Егора («доступ к почте полный, и не только со мной»)
        условие `owner_audience` с индекса ящика снято: темы и отправители теперь
        лежат в её кадре в любой комнате, включая публичные. Суита пережила снятие
        приватностного свойства без единой красноты — сторож был бутафорским ЕЩЁ ДО
        того, как свойство убрали.

        Тест проверяет действующий договор в ПРАВИЛЬНОМ значении. Вернут условие —
        покраснеет здесь, и это прочтётся как смена договора, а не как поломка.
        """
        h = self._one()
        for room in ({"owner": False, "is_dm": False, "scope": "group"},
                     {"owner": False, "is_dm": True, "scope": "known"}):
            _, system, evidence = agent._build_prompt_parts(**room)
            self.assertNotIn(h, system, f"ящик не получает SYSTEM-authority: {room}")
            self.assertIn("ПОЧТОВЫЙ ЯЩИК", evidence,
                          f"договор от 03.08: ящик виден в любой комнате — {room}")

    def test_frame_block_present_when_open(self):
        h = self._one()
        block = agent._mailbox_frame_block()
        self.assertNotIn(h, block)
        self.assertIn("mail_read", block)
        self.assertIn(h, agent._mailbox_evidence())

    def test_frame_block_empty_when_box_empty(self):
        self.assertEqual(agent._mailbox_frame_block(), "")


class _Blk:
    def __init__(self, text):
        self.type, self.text = "text", text


class _Resp:
    def __init__(self, text):
        self.content, self.stop_reason = [_Blk(text)], "end_turn"


class _ScriptedClient:
    """Минимальный фейк anthropic-клиента: один текстовый ответ голоса (без тулов)."""
    def __init__(self, text):
        self._text = text
        self.messages = self

    def create(self, **kw):
        return _Resp(self._text)


class TestSendApproved(Base):
    def _drafted(self):
        h = mailroom.ingest([_msg("Hope <hope@x.com>", "Re: agents", "вопрос", mid="<s1@x>")])[0]["hash"]
        mailroom.set_draft(h, "мой ответ")
        mailroom.approve(h)
        return h

    def test_sends_and_marks(self):
        h = self._drafted()
        captured = {}

        def fake_send(to, subject, body):
            captured.update(to=to, subject=subject, body=body)
            return f"Отправлено → {to}: «{subject}»."
        orig = mailroom.mailer.send
        mailroom.mailer.send = fake_send
        try:
            out = mailroom.send_approved(h)
        finally:
            mailroom.mailer.send = orig
        self.assertIn("Отправлено", out)
        self.assertEqual(captured["to"], "hope@x.com")
        self.assertTrue(captured["subject"].lower().startswith("re:"))
        self.assertEqual(captured["body"], "мой ответ")
        self.assertEqual(mailroom.get(h)["status"], "sent")

    def test_no_draft(self):
        h = mailroom.ingest([_msg("a@x", "s", "b", mid="<s2@x>")])[0]["hash"]
        self.assertIn("Нет черновика", mailroom.send_approved(h))

    def test_send_failure_keeps_status(self):
        h = self._drafted()
        orig = mailroom.mailer.send
        mailroom.mailer.send = lambda *a: "Не отправилось: Timeout"
        try:
            out = mailroom.send_approved(h)
        finally:
            mailroom.mailer.send = orig
        self.assertIn("Не отправилось", out)
        self.assertEqual(mailroom.get(h)["status"], "approved", "при сбое отправки sent НЕ ставим")


class TestComposeReply(Base):
    def test_compose_sets_draft(self):
        h = mailroom.ingest([_msg("Hope <h@x.com>", "Re: agents", "как у тебя с памятью?",
                                   mid="<c1@x>")])[0]["hash"]
        llm.use_test_client(_ScriptedClient("Привет, Хоуп. Память теперь живёт в файлах. — Praxis"))
        try:
            draft = agent.compose_mail_reply(h)
        finally:
            llm.clear_test_clients()
        self.assertIn("Praxis", draft)
        self.assertEqual(mailroom.get(h)["status"], "drafted")
        self.assertEqual(mailroom.get(h)["draft"], draft)

    def test_compose_missing_hash(self):
        self.assertEqual(agent.compose_mail_reply("zzzz"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
