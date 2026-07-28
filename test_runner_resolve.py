"""
HOTFIX 07.07 (баг 1) — send_message таймаутил на ПЕРВОМ резолве по имени после рестарта.

Корень: _dialog_name_cache строился лениво на первом обращении, и холодный iter_dialogs()
платился из общего 30-секундного бюджета _sync_send_message («напиши Евгению» в 13:04/13:06
умирало голым TimeoutError). Теперь кэш греется фоном сразу после connect
(_start_dialog_warmup), холодный резолв по имени ждёт прогрев СВОИМ бюджетом
(DIALOG_WARMUP_WAIT_SEC), а резолв и отправка в _sync_send_message разделены по бюджетам
и логируют, какой шаг сколько ел.

Запуск:  python praxis_test.py test_runner_resolve -v
"""
from __future__ import annotations

import asyncio
import collections
import copy
import os
import tempfile
import threading
import time
import pathlib
import unittest
from pathlib import Path

os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "x")
os.environ.setdefault("TELEGRAM_SESSION", str(Path(tempfile.gettempdir()) / "praxis_test_resolve"))

import mtproto_runner  # noqa: E402


class FakeEnt:
    def __init__(self, id, name="", username=None, usernames=None):
        self.id = id
        self.name = name
        self.first_name = name
        self.username = username
        self.usernames = usernames or ()

    def __repr__(self):
        return f"FakeEnt({self.id}, {self.name!r})"


class FakeDialog:
    def __init__(self, id, name):
        self.id = id
        self.name = name
        self.entity = FakeEnt(id, name)


class FakeUsername:
    def __init__(self, username, active):
        self.username = username
        self.active = active


class FakeResolveClient:
    """Клиент для резолвера: get_entity знает только entities (id/@username-путь),
    iter_dialogs отдаёт диалоги с настраиваемой задержкой (эмуляция холодного скана).

    .loop ОТРАВЛЕН: с telethon>=1.39 client.loop — динамический get_running_loop(),
    который из воркер-треда молча отдаёт НОВЫЙ МЁРТВЫЙ луп (так 07.07 умерли все
    sync-тулы разом). Обёртки обязаны ходить через mtproto_runner._main_loop()."""

    def __init__(self, dialogs=(), dialog_delay=0.0, entities=None, fail_scans=0,
                 participants=None):
        self._dialogs = list(dialogs)
        self._delay = dialog_delay
        self._entities = dict(entities or {})
        self._participants = dict(participants or {})  # chat_id -> [FakeEnt]
        self.fail_scans = fail_scans  # первые N сканов падают, не отдав ничего
        self.scan_calls = 0
        self.get_entity_calls = []
        self.sent = []
        self.sent_files = []

    @property
    def loop(self):
        raise AssertionError("sync-обёртки не должны трогать client.loop — "
                             "из воркер-треда это мёртвый луп (telethon>=1.39)")

    async def get_entity(self, ref):
        self.get_entity_calls.append(ref)
        if ref in self._entities:
            return self._entities[ref]
        raise ValueError(f"нет такого: {ref!r}")

    async def get_participants(self, chat_id, search="", limit=10):
        q = search.lower()
        return [p for p in self._participants.get(int(chat_id), [])
                if q in (p.name or "").lower()][:limit]

    async def iter_dialogs(self):
        self.scan_calls += 1
        if self.fail_scans > 0:
            self.fail_scans -= 1
            raise ConnectionError("скан отвалился (тест)")
        for d in self._dialogs:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield d

    async def send_message(self, ent, text):
        # send_error — чтобы шов отказа проверялся тем же путём, каким идёт настоящая
        # отправка: классификация постоянных отказов смотрит на ИМЯ класса по всему MRO,
        # поэтому герметичная подделка здесь честна.
        if getattr(self, "send_error", None) is not None:
            raise self.send_error
        self.sent.append((ent, text))
        return type("SentMessage", (), {"id": len(self.sent)})()

    async def send_file(self, ent, path, **kwargs):
        if getattr(self, "send_error", None) is not None:
            raise self.send_error
        self.sent_files.append((ent, path, kwargs))
        return type("SentMessage", (), {"id": len(self.sent_files)})()


class Base(unittest.TestCase):
    """Готчи спеки: _entity_cache/_dialog_name_cache — модульные globals, каждый кейс
    обязан явно сбрасывать их (иначе видит кэш соседнего теста)."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix="praxis_contacts_")
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self._saved = {k: getattr(mtproto_runner, k) for k in
                       ("client", "_dialog_name_cache", "_entity_cache",
                        "_dialog_warmup_task", "DIALOG_WARMUP_WAIT_SEC", "_LOOP",
                        "_DIRECT_OUTBOX", "_direct_tool_execution")}
        mtproto_runner._DIRECT_OUTBOX = mtproto_runner.telegram_outbox.TelegramOutbox(
            Path(self.tmpdir.name) / "telegram_outbox",
        )
        self._direct_proofs = {}
        self._saved_agent_outbox = (
            mtproto_runner.agent.run_direct_outbox_prepared,
            mtproto_runner.agent.direct_outbox_prepared,
            mtproto_runner.agent.project_direct_outbox_acceptance,
        )

        def _prepared(entry, *, target_label="", target_user_id=None,
                      pulse_id="", followup_request=""):
            proof = {
                "schema": "praxis.test.direct-outbox-intent.v1",
                "entry": copy.deepcopy(entry),
                "projection": {
                    "target_ref": str(target_label or entry.get("peer_id") or ""),
                    "target_label": str(target_label or ""),
                    "target_user_id": target_user_id,
                    "pulse_id": str(pulse_id or ""),
                    "followup_request": str(followup_request or ""),
                },
            }
            self._direct_proofs[str(entry["key"])] = proof
            return proof

        def _project(entry):
            proof = self._direct_proofs[str(entry["key"])]
            mtproto_runner._project_direct_outbox_acceptance(proof, entry)
            return True

        # These resolver tests own the Telethon-facing unit boundary. Durable
        # RunManager binding/projection is covered by the run integration
        # suites; emulate that adapter here without inventing a nonexistent run.
        mtproto_runner.agent.run_direct_outbox_prepared = _prepared
        mtproto_runner.agent.direct_outbox_prepared = (
            lambda entry: str(entry.get("key") or "") in self._direct_proofs
        )
        mtproto_runner.agent.project_direct_outbox_acceptance = _project
        self._tool_call_seq = 0

        def _execution(tool):
            self._tool_call_seq += 1
            call_id = f"call-{self._tool_call_seq}"
            return {
                "run_id": "run-test-resolve",
                "call_id": call_id,
                "tool": tool,
                "idempotency_key": f"telegram-outbox:run-test-resolve:tool:{call_id}",
            }

        mtproto_runner._direct_tool_execution = _execution
        self._contacts_path = mtproto_runner.telegram_contacts.CONTACTS_PATH
        mtproto_runner.telegram_contacts.CONTACTS_PATH = Path(self.tmpdir.name) / "contacts.json"
        mtproto_runner.telegram_contacts.reset_cache()
        self._orig_unanswered_resolve = mtproto_runner.unanswered.resolve
        mtproto_runner.unanswered.resolve = lambda *a, **k: None
        mtproto_runner._dialog_name_cache = None
        mtproto_runner._entity_cache = {}
        mtproto_runner._dialog_warmup_task = None
        mtproto_runner._LOOP = self.loop  # как в проде: main() захватывает живой луп

    def tearDown(self):
        task = mtproto_runner._dialog_warmup_task
        if task is not None and not task.done():
            async def _kill():
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass
            asyncio.run_coroutine_threadsafe(_kill(), self.loop).result(timeout=5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()
        for k, v in self._saved.items():
            setattr(mtproto_runner, k, v)
        (mtproto_runner.agent.run_direct_outbox_prepared,
         mtproto_runner.agent.direct_outbox_prepared,
         mtproto_runner.agent.project_direct_outbox_acceptance) = self._saved_agent_outbox
        mtproto_runner.telegram_contacts.CONTACTS_PATH = self._contacts_path
        mtproto_runner.telegram_contacts.reset_cache()
        self.tmpdir.cleanup()
        mtproto_runner.unanswered.resolve = self._orig_unanswered_resolve

    def use_client(self, **kw):
        fake = FakeResolveClient(**kw)
        mtproto_runner.client = fake
        return fake

    def start_warmup(self):
        """Как main() после connect: прогрев фоном, не дожидаясь."""
        done = threading.Event()
        self.loop.call_soon_threadsafe(lambda: (mtproto_runner._start_dialog_warmup(), done.set()))
        self.assertTrue(done.wait(2), "не удалось запустить прогрев на лупе")


class TestTelegramHandles(unittest.TestCase):
    def test_active_secondary_username_is_visible_but_inactive_alias_is_not(self):
        ent = FakeEnt(
            42, "Евгений Петров",
            usernames=[FakeUsername("old_evgeniy", active=False),
                       FakeUsername("evgeniy_live", active=True)],
        )

        self.assertEqual(mtproto_runner._telegram_handle(ent), "evgeniy_live")
        self.assertEqual(
            mtproto_runner._sender_name(type("Message", (), {"out": False, "sender": ent})()),
            "Евгений Петров (@evgeniy_live)",
        )
        self.assertEqual(
            mtproto_runner._ent_label(ent),
            "Евгений Петров (@evgeniy_live, id 42)",
        )

    def test_primary_username_still_wins_over_secondary_alias(self):
        ent = FakeEnt(42, "Евгений", username="evgeniy_main",
                      usernames=[FakeUsername("evgeniy_live", active=True)])
        self.assertEqual(mtproto_runner._telegram_handle(ent), "evgeniy_main")


class TestWarmupDoesNotBlockFastPath(Base):
    def test_id_username_send_completes_while_scan_still_running(self):
        # скан долгий (20 × 0.2с = 4с), но отправка по @username не должна его ждать вовсе
        vasya = FakeEnt(42, "Вася")
        fake = self.use_client(
            dialogs=[FakeDialog(100 + i, f"чат {i}") for i in range(20)],
            dialog_delay=0.2, entities={"@vasya": vasya})
        self.start_warmup()
        out = mtproto_runner._sync_send_message("@vasya", "привет")
        self.assertIn("Отправила", out)
        self.assertEqual(fake.sent, [(vasya, "привет")])
        self.assertIsNone(mtproto_runner._dialog_name_cache,
                          "скан ещё не должен был закончиться — иначе тест не проверяет параллельность")

    def test_a_permanent_refusal_is_an_answer_not_a_dead_turn(self):
        """Живой инцидент 26.07: пост в «@abstractDL» — канал, прав нет, отказ навсегда.

        Раньше это летело исключением и уносило весь её ход: она не узнавала ни что не
        ушло, ни почему, и не могла поправить адрес. А журнал считал отказ икотой, и часы
        резюма поднимали тот же ран каждые 45 секунд — бесконечно.
        """
        class ChatAdminRequiredError(Exception):
            pass

        vasya = FakeEnt(42, "Вася")
        fake = self.use_client(entities={"@vasya": vasya})
        fake.send_error = ChatAdminRequiredError("нет прав в этом канале")

        out = mtproto_runner._sync_send_message("@vasya", "камбэк")
        self.assertIsInstance(out, mtproto_runner.agent.DirectSendRefusal,
                              "отказ вернулся ответом, а не убил ход")
        self.assertIn("ChatAdminRequiredError", str(out))
        self.assertIn("повторять этот я не буду", str(out),
                      "и сказано прямо, что повторов не будет")

    def test_a_file_refused_forever_is_an_answer_too(self):
        """Ту же дверь я сначала починил только с одной стороны.

        Текстовый путь научился отвечать на постоянный отказ, а файловый остался прежним:
        исключение уносило её ход, запись оставалась в `retry`, и повтор раз в 45 секунд
        шёл вечно — тот же самый цикл, только с документом вместо текста. Нашла адверсарка.
        """
        class ChatAdminRequiredError(Exception):
            pass

        import tempfile
        doc = pathlib.Path(tempfile.mkdtemp()) / "otchet.md"
        doc.write_text("итог", encoding="utf-8")

        vasya = FakeEnt(42, "Вася")
        fake = self.use_client(entities={"@vasya": vasya})
        fake.send_error = ChatAdminRequiredError("нет прав в этом канале")

        out = mtproto_runner._sync_send_file(str(doc), "итог", "@vasya")
        self.assertIsInstance(out, mtproto_runner.agent.DirectSendRefusal)
        self.assertIn("ChatAdminRequiredError", str(out))
        self.assertIn("повторять этот я не буду", str(out))

    def test_a_transient_failure_still_stays_pending_for_retry(self):
        """Контроль: «сейчас не вышло» и «здесь нельзя» — разные миры, и второй не съел первый."""
        vasya = FakeEnt(42, "Вася")
        fake = self.use_client(entities={"@vasya": vasya})
        fake.send_error = TimeoutError("сеть моргнула")

        with self.assertRaises(mtproto_runner.agent.DurableSideEffectPending):
            mtproto_runner._sync_send_message("@vasya", "привет")

    def test_her_own_words_come_back_into_the_conversation(self):
        """Сказанное по своей инициативе обязано вернуться в разговор.

        Обычный голос кладёт себя в буфер после отправки, ответ в отсутствие — тоже, а
        прямая отправка (send_message/narrate: пульс, окно, будильник) не клала никуда.
        Живьём 26.07 это стоило двух вещей сразу: следующий проход по чату не видел её
        ответа и говорил то же самое заново, и сказанное не попадало в её память жизни.
        """
        vasya = FakeEnt(42, "Вася")
        self.use_client(entities={"@vasya": vasya})
        mtproto_runner._buf.pop("42", None)
        out = mtproto_runner._sync_send_message("@vasya", "я уже ответила на это")
        self.assertIn("Отправила", out)
        said = [line for line in mtproto_runner._buf.get("42", ())
                if "я уже ответила на это" in str(line)]
        self.assertTrue(said, "её реплика вернулась в буфер чата")
        self.assertTrue(str(said[0]).startswith("Praxis:"),
                        "и подписана ею, а не собеседником")

    def test_fast_path_never_triggers_scan_at_all(self):
        # регрессия спеки: резолв по id/@username (шаг 2) не подорожал и не сломался
        vasya = FakeEnt(42, "Вася")
        fake = self.use_client(entities={"@vasya": vasya})
        out = mtproto_runner._sync_send_message("@vasya", "хей")
        self.assertIn("Отправила", out)
        self.assertEqual(fake.scan_calls, 0, "быстрый путь не должен трогать iter_dialogs")


class TestColdNameResolve(Base):
    def test_waits_for_running_warmup_instead_of_second_scan(self):
        evg = FakeDialog(7, "Евгений Петров")
        fake = self.use_client(dialogs=[FakeDialog(1, "мама"), evg], dialog_delay=0.05)
        self.start_warmup()
        out = mtproto_runner._sync_send_message("Евгений", "ты тут?")
        self.assertIn("Отправила", out)
        self.assertEqual(fake.sent[0][0], evg.entity)
        self.assertEqual(fake.scan_calls, 1, "холодный резолв должен ЖДАТЬ идущий прогрев, не сканить сам")
        # второй резолв того же имени — уже из кэша, без нового скана
        mtproto_runner._sync_send_message("Евгений", "ещё")
        self.assertEqual(fake.scan_calls, 1)

    def test_stuck_warmup_fails_with_honest_word_not_bare_timeout(self):
        # прогрев безнадёжно завис -> холодный резолв отваливается по СВОЕМУ бюджету,
        # с внятным словом (что именно не успело), а не голым TimeoutError на 30-й секунде
        mtproto_runner.DIALOG_WARMUP_WAIT_SEC = 0.2
        self.use_client(dialogs=[FakeDialog(7, "Евгений")], dialog_delay=30.0)
        t0 = time.time()
        with self.assertRaisesRegex(TimeoutError, "кэш диалогов"):
            mtproto_runner._sync_send_message("Евгений", "ау")
        self.assertLess(time.time() - t0, 5, "должен отвалиться по своему бюджету, не по чужим 30с")

    def test_failed_scan_resets_cache_and_next_resolve_retries(self):
        evg = FakeDialog(7, "Евгений Петров")
        fake = self.use_client(dialogs=[evg], fail_scans=1)
        with self.assertRaisesRegex(TimeoutError, "скан диалогов"):
            mtproto_runner._sync_send_message("Евгений", "ау")
        self.assertIsNone(mtproto_runner._dialog_name_cache,
                          "пустой упавший скан не должен оставить кэш-пустышку навсегда")
        out = mtproto_runner._sync_send_message("Евгений", "ау-2")
        self.assertIn("Отправила", out)
        self.assertEqual(fake.scan_calls, 2, "после пустого провала следующий резолв сканит заново")


class TestSyncBridgeUsesMainLoop(Base):
    """HOTFIX 07.07 (вечер), корень «беды с тулзами»: с telethon>=1.39 client.loop из
    воркер-треда (все sync-тулы бегут через to_thread) молча создаёт НОВЫЙ мёртвый луп —
    run_coroutine_threadsafe планировал корутины в никуда, и КАЖДЫЙ Telethon-тул выедал
    свой таймаут-бюджет впустую (send_message 120с, get_id 20с -> «[не нашла]»).
    Обёртки обязаны ходить через _main_loop() — луп, захваченный в main()."""

    def test_wrappers_work_even_with_poisoned_client_loop(self):
        # .loop фейка кидает AssertionError при ЛЮБОМ обращении — если обёртка вернулась
        # к client.loop, тест упадёт мгновенно, а не тихо затаймаутит, как в проде
        vasya = FakeEnt(42, "Вася")
        fake = self.use_client(entities={"@vasya": vasya})
        out = mtproto_runner._sync_send_message("@vasya", "привет")
        self.assertIn("Отправила", out)
        self.assertEqual(fake.sent, [(vasya, "привет")])

    def test_send_file_defaults_to_current_group_not_owner_dm(self):
        fake = self.use_client()
        source = Path(self.tmpdir.name) / "result.txt"
        source.write_text("готово", encoding="utf-8")
        old_chat = mtproto_runner.agent._CURRENT_CHAT
        mtproto_runner.agent._CURRENT_CHAT = "-100777"
        try:
            out = mtproto_runner._sync_send_file(str(source), "результат")
        finally:
            mtproto_runner.agent._CURRENT_CHAT = old_chat
        self.assertIn("-100777", out)
        self.assertEqual(fake.sent_files[0][0], -100777)
        self.assertTrue(fake.sent_files[0][2]["force_document"])

    def test_uncaptured_loop_fails_loud_not_silent_timeout(self):
        # main() ещё не захватил луп -> честный RuntimeError с внятным словом сразу,
        # а не пустой TimeoutError через 120 секунд
        self.use_client(entities={"@vasya": FakeEnt(42, "Вася")})
        mtproto_runner._LOOP = None
        with self.assertRaisesRegex(RuntimeError, "не захвачен"):
            mtproto_runner._sync_send_message("@vasya", "привет")

    def test_bridge_canary_reports_alive_and_dead(self):
        with self.assertLogs("praxis-mt", level="INFO") as cm:
            mtproto_runner._bridge_canary()
        self.assertTrue(any("жив" in line for line in cm.output), cm.output)
        mtproto_runner._LOOP = None
        with self.assertLogs("praxis-mt", level="ERROR") as cm:
            mtproto_runner._bridge_canary()
        self.assertTrue(any("МЁРТВ" in line for line in cm.output), cm.output)


class TestNameVisibility(Base):
    """Видимость (07.07, вечер): по имени — только СВОИ диалоги; session-БД Telegram
    (все, кого она видела в группах, включая тёзок-незнакомцев) для имён закрыта —
    в 15:18 «напиши Евгению» ушло не тому. Неоднозначно/вне видимости — честный отказ
    со списком кандидатов, не молчаливое первое совпадение."""

    def test_unique_dialog_name_still_resolves(self):
        e1 = FakeEnt(1, "Евгений Петров", username="evpetrov")
        self.use_client()
        mtproto_runner._dialog_name_cache = [("евгений петров", e1), ("мама", FakeEnt(2, "мама"))]
        fut = asyncio.run_coroutine_threadsafe(
            mtproto_runner._resolve_entity("Евгений"), self.loop)
        self.assertIs(fut.result(timeout=5), e1)

    def test_ambiguous_name_uses_most_recent_dialog_and_receipt(self):
        e1 = FakeEnt(1, "Евгений Петров", username="evpetrov")
        e2 = FakeEnt(2, "Евгений Сидоров")
        fake = self.use_client()
        mtproto_runner._dialog_name_cache = [("евгений петров", e1), ("евгений сидоров", e2)]
        out = mtproto_runner._sync_send_message("Евгений", "привет")
        self.assertIn("Отправила", out)
        self.assertIn("@evpetrov", out)
        self.assertEqual(fake.sent, [(e1, "привет")])

    def test_fuzzy_name_never_falls_to_global_lookup(self):
        # «Анна Комарова» видна в session-БД (эмулируем через entities), но лички нет:
        # отправка по имени обязана отказать, НЕ трогая get_entity (глобальный путь)
        stranger = FakeEnt(999, "Анна Комарова", username="annak")
        fake = self.use_client(entities={"Анна Комарова": stranger})
        mtproto_runner._dialog_name_cache = [("мама", FakeEnt(2, "мама"))]
        out = mtproto_runner._sync_send_message("Анна Комарова", "привет")
        self.assertIn("нет в моей адресной книге", out)
        self.assertEqual(fake.sent, [])
        self.assertEqual(fake.get_entity_calls, [],
                         "имя не должно резолвиться через get_entity/session-БД — это глобальная видимость")

    def test_explicit_username_still_global_and_receipt_names_target(self):
        vasya = FakeEnt(42, "Вася", username="vasya")
        fake = self.use_client(entities={"@vasya": vasya})
        mtproto_runner._dialog_name_cache = []
        out = mtproto_runner._sync_send_message("@vasya", "привет")
        self.assertIn("Отправила", out)
        self.assertIn("@vasya", out, "квитанция называет реального адресата")
        self.assertIn("id 42", out)
        self.assertIn("chat_id=42", out)
        self.assertIn("message_id=1", out)
        self.assertEqual(fake.sent, [(vasya, "привет")])

    def test_owner_followup_request_binds_real_sent_message_id(self):
        vasya = FakeEnt(42, "Вася", username="vasya")
        fake = self.use_client(entities={"@vasya": vasya})
        old = {
            "owner": mtproto_runner.OWNER_ID,
            "owner_env": os.environ.get("PRAXIS_OWNER_ID"),
            "chat": mtproto_runner.agent._CURRENT_CHAT,
            "scope": mtproto_runner.agent._CURRENT_SCOPE,
            "ledger": mtproto_runner.telegram_followups.LEDGER,
            "buf": mtproto_runner._buf,
        }
        channel_token = None
        try:
            mtproto_runner.OWNER_ID = 809
            os.environ["PRAXIS_OWNER_ID"] = "809"
            channel_token = mtproto_runner.agent._TURN_CHANNEL.set(
                mtproto_runner.agent.ChannelContext(
                    chat_id="809", principal_id=809, is_dm=True, owner=True, known=True,
                )
            )
            mtproto_runner.agent._CURRENT_CHAT = "809"
            mtproto_runner.agent._CURRENT_SCOPE = "owner"
            mtproto_runner._buf = collections.defaultdict(lambda: collections.deque(maxlen=20))
            mtproto_runner._buf["809"].append(
                "Егор: Напиши Васе «привет» и сообщи, когда он ответит")
            path = Path(self.tmpdir.name) / "followups.json"
            mtproto_runner.telegram_followups.LEDGER = (
                mtproto_runner.telegram_followups.FollowUpLedger(path))
            out = mtproto_runner._sync_send_message("@vasya", "привет")
            item = mtproto_runner.telegram_followups.LEDGER.list(status="pending")[0]
        finally:
            if channel_token is not None:
                mtproto_runner.agent._TURN_CHANNEL.reset(channel_token)
            mtproto_runner.OWNER_ID = old["owner"]
            if old["owner_env"] is None:
                os.environ.pop("PRAXIS_OWNER_ID", None)
            else:
                os.environ["PRAXIS_OWNER_ID"] = old["owner_env"]
            mtproto_runner.agent._CURRENT_CHAT = old["chat"]
            mtproto_runner.agent._CURRENT_SCOPE = old["scope"]
            mtproto_runner.telegram_followups.LEDGER = old["ledger"]
            mtproto_runner._buf = old["buf"]
        self.assertIn("message_id=1", out)
        self.assertEqual(item["target_peer_id"], "42")
        self.assertEqual(item["sent_message_id"], 1)
        self.assertEqual(fake.sent, [(vasya, "привет")])

    def test_get_id_falls_back_to_shared_room_participants(self):
        anna = FakeEnt(8878, "Анна Комарова", username="annak71")
        fake = self.use_client(participants={-100: [anna, FakeEnt(1, "Анна Чалый")]})
        mtproto_runner._dialog_name_cache = [("мама", FakeEnt(2, "мама"))]
        self._saved_rooms = mtproto_runner.rooms.allowed_chats
        mtproto_runner.rooms.allowed_chats = lambda: ["-100"]
        try:
            out = mtproto_runner._sync_get_id("Анна")
        finally:
            mtproto_runner.rooms.allowed_chats = self._saved_rooms
        self.assertIn("участников общих комнат", out)
        self.assertIn("@annak71", out)
        self.assertIn("id 8878", out)
        self.assertIn("точный @username/id", out, "адресация по-прежнему требует точный адрес")


if __name__ == "__main__":
    unittest.main(verbosity=2)
