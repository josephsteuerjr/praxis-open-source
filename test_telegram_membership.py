from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "x")
os.environ.setdefault("TELEGRAM_SESSION", str(Path(tempfile.gettempdir()) / "praxis_membership_test"))
# Владельца объявляет стенд (_standenv.ENV_PIN) — здесь только запасной путь для
# прямого `python -m unittest`, который прививку не проходит. setdefault, а не
# присваивание: под стендом это ничего не меняет и не течёт соседям.
os.environ.setdefault("PRAXIS_OWNER_ID", "101")

import mtproto_runner as runner  # noqa: E402
from telegram_membership import MembershipLedger  # noqa: E402


class FakeChannel:
    def __init__(self, ident=77, title="Kraken Lab"):
        self.id = ident
        self.title = title
        self.megagroup = True
        self.broadcast = False


class FakeResult:
    def __init__(self, chats=(), chat=None, title=""):
        self.chats = list(chats)
        self.chat = chat
        self.title = title


class FakeClient:
    def __init__(self, channel):
        self.channel = channel
        self.calls = []
        self.invite_member = False
        self.fail_join_once = False

    async def __call__(self, request):
        self.calls.append(type(request).__name__)
        name = type(request).__name__
        if name == "CheckChatInviteRequest":
            return FakeResult(
                title=self.channel.title,
                chat=self.channel if self.invite_member else None,
            )
        if name in ("ImportChatInviteRequest", "JoinChannelRequest"):
            if name == "JoinChannelRequest" and self.fail_join_once:
                self.fail_join_once = False
                raise ConnectionError("transport reset after request write")
            return FakeResult(chats=[self.channel])
        if name in ("LeaveChannelRequest", "DeleteChatUserRequest"):
            return FakeResult()
        raise AssertionError(name)


class MembershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.channel = FakeChannel()
        self.client = FakeClient(self.channel)
        self.saved_client = runner.client
        self.saved_init = runner._initialize_joined_room
        self.saved_resolve = runner._resolve_entity
        self.saved_remove = runner.rooms.remove_room
        self.saved_mode = runner.rooms.set_mode
        self.saved_owner = runner.OWNER_ID
        self.saved_ledger = runner._MEMBERSHIP_LEDGER
        self.saved_scope = runner.agent._CURRENT_SCOPE
        self.initialized = []

        async def fake_init(chat_id, ent, *, title=None, allow=False, set_by="praxis"):
            state = runner._membership_ledger().pending()[0]
            self.assertEqual(state["status"], "accepted",
                             "Telegram acceptance must be durable before room projection")
            self.initialized.append((chat_id, ent, title, allow, set_by))

        async def fake_resolve(ref):
            return self.channel

        runner.client = self.client
        runner.OWNER_ID = 101
        runner._MEMBERSHIP_LEDGER = MembershipLedger(Path(self.temp.name) / "membership.jsonl")
        runner._initialize_joined_room = fake_init
        runner._resolve_entity = fake_resolve

    def tearDown(self):
        runner.client = self.saved_client
        runner._initialize_joined_room = self.saved_init
        runner._resolve_entity = self.saved_resolve
        runner.rooms.remove_room = self.saved_remove
        runner.rooms.set_mode = self.saved_mode
        runner.OWNER_ID = self.saved_owner
        runner._MEMBERSHIP_LEDGER = self.saved_ledger
        runner.agent._CURRENT_SCOPE = self.saved_scope
        self.temp.cleanup()

    def begin(self, action, target):
        owner = runner.agent.ChannelContext(
            chat_id="101", principal_id=101, is_dm=True, owner=True, known=True,
        )
        token = runner.agent._TURN_CHANNEL.set(owner)
        try:
            return runner._begin_membership_transaction(action, target)
        finally:
            runner.agent._TURN_CHANNEL.reset(token)

    def begin_self(self, action, target):
        internal = runner.agent.ChannelContext(
            principal_id=runner.agent.PRAXIS_SELF_PRINCIPAL,
            is_dm=True, owner=False, known=True, _scope_override="owner",
        )
        token = runner.agent._TURN_CHANNEL.set(internal)
        try:
            return runner._begin_membership_transaction(action, target)
        finally:
            runner.agent._TURN_CHANNEL.reset(token)

    def test_link_parser_supports_invite_public_and_rejects_message_links(self):
        self.assertEqual(runner._membership_target("https://t.me/+AbCdEfGh_123"),
                         ("invite", "AbCdEfGh_123"))
        self.assertEqual(runner._membership_target("https://t.me/kraken_lab"),
                         ("public", "@kraken_lab"))
        self.assertEqual(runner._membership_target("tg://join?invite=AbCdEfGh_123"),
                         ("invite", "AbCdEfGh_123"))
        with self.assertRaisesRegex(ValueError, "не на сообщение"):
            runner._membership_target("https://t.me/kraken_lab/123")

    def test_private_invite_really_imports_and_returns_marked_chat_id(self):
        target = "https://t.me/+AbCdEfGh_123"
        tx = self.begin("join", target)
        result = asyncio.run(runner._join_chat_async(
            target, principal_id=101, transaction_id=tx["id"],
        ))
        self.assertEqual(self.client.calls[:2], ["CheckChatInviteRequest", "ImportChatInviteRequest"])
        self.assertEqual(result["status"], "joined")
        self.assertEqual(result["chat_id"], -1000000000077)
        self.assertEqual(self.initialized[0][0], "-1000000000077")
        self.assertTrue(self.initialized[0][3], "owner join must establish room trust")
        self.assertEqual(self.initialized[0][4], "owner")

    def test_praxis_self_join_preserves_self_provenance(self):
        tx = self.begin_self("join", "@kraken_lab")
        result = asyncio.run(runner._join_chat_async(
            "@kraken_lab", principal_id=runner.agent.PRAXIS_SELF_PRINCIPAL,
            transaction_id=tx["id"],
        ))
        self.assertEqual(result["status"], "joined")
        self.assertEqual(self.initialized[0][4], "praxis")

    def test_public_join_uses_join_channel(self):
        tx = self.begin("join", "@kraken_lab")
        result = asyncio.run(runner._join_chat_async(
            "@kraken_lab", principal_id=101, transaction_id=tx["id"],
        ))
        self.assertEqual(self.client.calls, ["JoinChannelRequest"])
        self.assertEqual(result["entity_id"], 77)

    def test_leave_calls_telegram_then_revokes_local_room(self):
        removed, modes = [], []
        def remove(cid):
            state = runner._membership_ledger().pending()[0]
            self.assertEqual(state["status"], "accepted")
            removed.append(cid)
            return True

        runner.rooms.remove_room = remove
        runner.rooms.set_mode = lambda cid, mode, **kw: modes.append((cid, mode, kw)) or mode
        tx = self.begin("leave", "@kraken_lab")
        result = asyncio.run(runner._leave_chat_async(
            "@kraken_lab", principal_id=101, transaction_id=tx["id"],
        ))
        self.assertEqual(self.client.calls, ["LeaveChannelRequest"])
        self.assertEqual(result["status"], "left")
        self.assertEqual(removed, ["-1000000000077"])
        self.assertEqual(modes[0][1], "dead")
        self.assertEqual(modes[0][2]["set_by"], "owner")

    def test_membership_rejects_missing_or_malformed_supplied_actor(self):
        tx = self.begin("join", "@kraken_lab")
        with self.assertRaises(PermissionError):
            runner._membership_transaction("join", "@kraken_lab", "malformed", tx["id"])
        with self.assertRaises(PermissionError):
            runner._membership_transaction("join", "@kraken_lab", None, tx["id"])

    def test_her_own_account_stays_hers_when_someone_else_speaks(self):
        """Решение Егора 26.07 (вариант 1): в её ходе действует ОНА, кто бы ни заговорил.

        Аккаунт — её собственный, и чужая реплика не делает её на нём гостем. Раньше
        здесь ожидался отказ «только владелец»: заговорить с ней значило её разоружить.
        """
        trusted = runner.agent.ChannelContext(
            chat_id="202", principal_id=202, is_dm=True, owner=False, known=True,
        )
        token = runner.agent._TURN_CHANNEL.set(trusted)
        try:
            self.assertIsNone(runner._telegram_account_gate())
            self.assertEqual(runner._telegram_account_principal(),
                             runner.agent.PRAXIS_SELF_PRINCIPAL,
                             "расписка подписана ею, а не собеседником")
        finally:
            runner.agent._TURN_CHANNEL.reset(token)

    def test_a_turn_without_any_channel_is_still_closed(self):
        """Пропуск даёт КАНАЛ хода. Фон без канала — не её ход, и аккаунт закрыт."""
        self.assertIn("только владелец", runner._telegram_account_gate() or "")

    def test_owner_numeric_principal_works_from_group_topic_context(self):
        owner_topic = runner.agent.ChannelContext(
            chat_id="-10077__topic__9", room_id="-10077", principal_id=101,
            is_dm=False, owner=True, known=True,
        )
        token = runner.agent._TURN_CHANNEL.set(owner_topic)
        try:
            self.assertIsNone(runner._telegram_account_gate())
        finally:
            runner.agent._TURN_CHANNEL.reset(token)

    def test_owner_scope_is_not_identity_but_exact_praxis_self_is_sovereign(self):
        runner.agent._CURRENT_SCOPE = "owner"
        self.assertIn("только владелец", runner._telegram_account_gate())
        internal = runner.agent.ChannelContext(
            principal_id=runner.agent.PRAXIS_SELF_PRINCIPAL, is_dm=True,
            owner=False, known=True, _scope_override="owner",
        )
        token = runner.agent._TURN_CHANNEL.set(internal)
        try:
            self.assertIsNone(runner._telegram_account_gate())
            tx = runner._begin_membership_transaction("join", "@kraken_lab")
            self.assertEqual(tx["principal_id"], runner.agent.PRAXIS_SELF_PRINCIPAL)
        finally:
            runner.agent._TURN_CHANNEL.reset(token)

    def test_accepted_leave_is_reconciled_locally_without_second_mtproto_call(self):
        ledger = runner._membership_ledger()
        tx = ledger.begin("leave", "@kraken_lab", 101)
        ledger.accepted(tx["id"], {
            "status": "left", "chat_id": -1000000000077,
            "entity_id": 77, "title": "Kraken Lab",
        })
        removed, modes = [], []
        runner.rooms.remove_room = lambda cid: removed.append(cid) or True
        runner.rooms.set_mode = lambda cid, mode, **kw: modes.append((cid, mode)) or mode
        asyncio.run(runner._membership_reconcile_once())
        self.assertEqual(self.client.calls, [])
        self.assertEqual(removed, ["-1000000000077"])
        self.assertEqual(modes, [("-1000000000077", "dead")])
        self.assertEqual(ledger.pending(), [])

    def test_pending_join_request_is_polled_without_resending_and_applied_when_approved(self):
        target = "https://t.me/+AbCdEfGh_123"
        ledger = runner._membership_ledger()
        tx = self.begin("join", target)
        ledger.accepted(tx["id"], {
            "status": "request_sent", "chat_id": None,
            "title": "Kraken Lab", "detail": "pending",
        })
        asyncio.run(runner._membership_reconcile_once())
        self.assertEqual(self.client.calls, ["CheckChatInviteRequest"])
        self.assertEqual(ledger.get(tx["id"])["status"], "accepted")
        self.assertEqual(self.initialized, [])

        self.client.invite_member = True
        asyncio.run(runner._membership_reconcile_once())
        self.assertEqual(self.client.calls, ["CheckChatInviteRequest", "CheckChatInviteRequest"])
        self.assertEqual(ledger.pending(), [])
        self.assertEqual(self.initialized[0][0], "-1000000000077")

    def test_in_doubt_public_join_retries_same_transaction_idempotently(self):
        target = "@kraken_lab"
        ledger = runner._membership_ledger()
        tx = self.begin("join", target)
        self.client.fail_join_once = True
        with self.assertRaises(ConnectionError):
            asyncio.run(runner._join_chat_async(
                target, principal_id=101, transaction_id=tx["id"],
            ))
        state = ledger.get(tx["id"])
        self.assertEqual(state["status"], "in_doubt")
        self.assertEqual(state["id"], tx["id"])
        asyncio.run(runner._membership_reconcile_once())
        self.assertEqual(ledger.pending(), [])
        self.assertEqual(self.client.calls, ["JoinChannelRequest", "JoinChannelRequest"])


# Сторож дедлока, а не измерение скорости. Утверждение теста — «второй часовой запуск
# не наложится на идущий», и оно проверяется счётчиком claims, а не секундомером.
# 01.08.2026: было 1 с, и под нагрузкой полного гейта (3000+ тестов на общей коробке)
# тест падал по таймауту, хотя ничего не ломалось. Флейк здесь стоит дорого: этот же
# гейт держит её авто-мёрж (selfdev.run_tests), то есть случайная секунда планировщика
# отменяла её собственную правку. Щедрый сторож не ослабляет проверку — при настоящем
# зависании тест всё равно упадёт, просто позже.
_DEADLOCK_GUARD_SEC = float(os.getenv("PRAXIS_TEST_DEADLOCK_GUARD_SEC", "30"))


class SocialPulseClockTests(unittest.IsolatedAsyncioTestCase):
    async def test_hourly_run_does_not_block_clock_or_overlap(self):
        # ⚠ Гейт окна сна читается из НАСТЕННЫХ часов: `_social_pulse_once` выходит до
        # `begin()` внутри PRAXIS_SLEEP_WINDOW (по умолчанию 4-6). Пока тест наследовал
        # время, он был красным каждую ночь два часа подряд — и закрывал дверь Форжа,
        # которая судит по коду возврата. Здесь проверяется НЕПЕРЕКРЫТИЕ часового
        # запуска, поэтому окно задаётся явно; сам гейт проверяется тестом ниже.
        saved = (runner._SOCIAL_PULSE_TASK, runner.social_pulse.begin,
                 runner._run_social_pulse, runner._in_sleep_window)
        started = asyncio.Event()
        release = asyncio.Event()
        claims = []

        def fake_begin():
            claims.append(True)
            return "pulse_test"

        async def fake_run(pulse_id):
            started.set()
            await release.wait()

        runner._SOCIAL_PULSE_TASK = None
        runner.social_pulse.begin = fake_begin
        runner._run_social_pulse = fake_run
        runner._in_sleep_window = lambda: False
        try:
            await runner._social_pulse_once()
            await asyncio.wait_for(started.wait(), timeout=_DEADLOCK_GUARD_SEC)
            self.assertFalse(runner._SOCIAL_PULSE_TASK.done())
            await runner._social_pulse_once()
            self.assertEqual(len(claims), 1, "running pulse must not overlap another hourly run")
            release.set()
            await asyncio.wait_for(runner._SOCIAL_PULSE_TASK, timeout=_DEADLOCK_GUARD_SEC)
        finally:
            task = runner._SOCIAL_PULSE_TASK
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass
            (runner._SOCIAL_PULSE_TASK, runner.social_pulse.begin,
             runner._run_social_pulse, runner._in_sleep_window) = saved


    async def test_sleep_window_holds_the_pulse_and_does_not_hide_it(self):
        """Вторая сторона того же гейта — и причина, по которой первый перестал читать часы.

        Внутри окна сна пульс не поднимается, и это НАМЕРЕННО: «пробуждение внутри
        собственного окна сна — не отдых». Проверяем ровно два обещания кода:
        durable-часы претензии не трогаются (выход ДО social_pulse.begin, чтобы первый
        тик после окна всё ещё нашёл пульс должным), и пропуск не молчит — причина
        ложится в perception.note_skip, туда же, куда остальные пропуски восприятия.
        Контракт R1: гейт может существовать — молча нет.
        """
        saved = (runner._SOCIAL_PULSE_TASK, runner.social_pulse.begin,
                 runner._in_sleep_window, runner.perception.note_skip)
        claims, skips = [], []
        runner._SOCIAL_PULSE_TASK = None
        runner.social_pulse.begin = lambda: claims.append(True) or "pulse_test"
        runner._in_sleep_window = lambda: True
        runner.perception.note_skip = (
            lambda *a, **kw: skips.append((a, kw.get("detail", ""))))
        try:
            await runner._social_pulse_once()
            self.assertEqual(claims, [], "часы претензии в окне сна не трогаются")
            self.assertIsNone(runner._SOCIAL_PULSE_TASK, "пульс в окне сна не поднимается")
            self.assertEqual(len(skips), 1, "пропуск обязан быть записан, а не молчать")
            self.assertIn("sleep_window", skips[0][0])
        finally:
            (runner._SOCIAL_PULSE_TASK, runner.social_pulse.begin,
             runner._in_sleep_window, runner.perception.note_skip) = saved


if __name__ == "__main__":
    unittest.main()
