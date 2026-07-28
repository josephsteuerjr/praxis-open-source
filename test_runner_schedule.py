"""
Планировщик раннера: одноразовая window-задача (focus/rest) помечается сработавшей при
РЕАЛЬНОМ открытии окна — внутри ``_task_window`` под ``_ONE_MIND``, до ``client.disconnect()``.

История. Регрессия 06.07 (руминация-петля): window-задача внутри ``_fire_task`` рвёт Telethon
(``client.disconnect``), из-за чего loop останавливался ПОСРЕДИ окна, а ``mark_fired`` в
``finally`` не выполнялся — одноразовая задача оставалась pending и перевыстреливала на каждом
рестарте (одна открылась 50+ раз). Первый фикс метил ``mark_fired`` ПЕРЕД ``_fire_task``.

Но это ломало focus/rest: если на тике ``_ONE_MIND`` занят живым ходом, ``_task_window``
откладывается (``return None``), а задача уже помечена ``done`` — намерение тихо терялось (F3).

Текущий канон: ``_fire_due_tasks`` НЕ метит window до исполнения — метка делается через
``on_open`` внутри ``_task_window`` ровно когда окно открылось (замок взят), до disconnect.
Отложенное окно намерение не съедает; рестарт посреди окна по-прежнему не даёт дубля.

Запуск:  python praxis_test.py test_runner_schedule -v
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

# mtproto_runner конструирует TelegramClient на импорте — дать фиктивные креды и temp-сессию.
os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "x")
os.environ.setdefault("TELEGRAM_SESSION", str(Path(tempfile.gettempdir()) / "praxis_test_runner"))

import mtproto_runner  # noqa: E402
import tasks  # noqa: E402


class TestFireDueOrder(unittest.TestCase):
    def setUp(self):
        self._orig = {"fire": mtproto_runner._fire_task,
                      "due": tasks.due, "mark_fired": tasks.mark_fired,
                      "email_auto": mtproto_runner._email_autonomous_enabled}

    def tearDown(self):
        mtproto_runner._fire_task = self._orig["fire"]
        tasks.due = self._orig["due"]
        tasks.mark_fired = self._orig["mark_fired"]
        mtproto_runner._email_autonomous_enabled = self._orig["email_auto"]

    def test_window_marking_is_delegated_not_pre_marked(self):
        # window: _fire_due_tasks НЕ метит до _fire_task — метку ставит _task_window.on_open при
        # реальном открытии (иначе отложенное окно теряет одноразовый focus/rest, F3).
        order: list[tuple] = []

        async def fake_fire_task(t):
            order.append(("fire", t["id"]))

        def fake_mark_fired(tid):
            order.append(("mark", tid))

        tasks.due = lambda: [{"id": "abc", "kind": "window", "goal": "разбор"}]
        tasks.mark_fired = fake_mark_fired
        mtproto_runner._fire_task = fake_fire_task

        asyncio.run(mtproto_runner._fire_due_tasks())
        self.assertEqual(order, [("fire", "abc")],
                         "window: планировщик делегирует метку в _task_window.on_open, "
                         "а не метит до исполнения (отложенное окно не должно теряться)")

    def test_nonwindow_kind_is_marked_before_fire(self):
        # Не-window legacy-вид (автономный email) не использует single-flight-окно — граница
        # at-most-once «пометить ДО исполнения» для него сохраняется.
        order: list[tuple] = []

        async def fake_fire_task(t):
            order.append(("fire", t["id"]))

        def fake_mark_fired(tid):
            order.append(("mark", tid))

        mtproto_runner._email_autonomous_enabled = lambda: True  # email -> не durable-telegram
        tasks.due = lambda: [{"id": "e1", "kind": "email", "goal": "письмо", "target": "x@y"}]
        tasks.mark_fired = fake_mark_fired
        mtproto_runner._fire_task = fake_fire_task

        asyncio.run(mtproto_runner._fire_due_tasks())
        self.assertEqual(order, [("mark", "e1"), ("fire", "e1")],
                         "не-window: помечаем ДО исполнения, как и раньше")


class TestWakeKeepsTheLineOpen(unittest.TestCase):
    """kind=wake: её будильник поднимает живой ход и НЕ рвёт Telethon.

    Единственное отличие от окна — отсутствие ``client.disconnect()``, и оно же вся суть:
    26.07 она девятнадцать раз подряд просила о связи окном, потому что попросить иначе
    было нечем, и каждая просьба эту связь и обрывала. Проверяется именно шов, а не
    намерение: соврать соседнему органу тут нечем — disconnect либо позван, либо нет.
    """

    # Под полным discover telethon подменён заглушкой из test_deep_group_context, и её
    # клиент вовсе не имеет connect/disconnect — читать их в setUp нельзя, только ставить
    # и снимать по факту наличия. Иначе тест шва зелен в одиночку и падает в гейте.
    _SENTINEL = object()

    def _swap(self, obj, name, value):
        prior = getattr(obj, name, self._SENTINEL)
        setattr(obj, name, value)

        def restore():
            if prior is self._SENTINEL:
                try:
                    delattr(obj, name)
                except AttributeError:
                    pass
            else:
                setattr(obj, name, prior)

        self.addCleanup(restore)

    def setUp(self):
        self._orig = {"due": tasks.due, "mark_fired": tasks.mark_fired}
        self.transport: list[str] = []
        self.woke: list[str] = []

        async def note_disconnect():
            self.transport.append("disconnect")

        async def note_connect():
            self.transport.append("connect")

        self._swap(mtproto_runner.client, "disconnect", note_disconnect)
        self._swap(mtproto_runner.client, "connect", note_connect)
        self._swap(mtproto_runner.agent, "wake_turn",
                   lambda goal="", **kw: self.woke.append(goal) or "")
        self.windows: list[tuple] = []
        self._swap(mtproto_runner.agent, "task_window",
                   lambda goal="", **kw: self.windows.append((goal, kw)) or "")

    def tearDown(self):
        tasks.due = self._orig["due"]
        tasks.mark_fired = self._orig["mark_fired"]

    def test_a_wake_never_touches_the_transport(self):
        """Плюс двухфазность: при подъёме намерение БЕРЁТСЯ, а гасится при создании рана.

        Её находка: пометить раньше рана — значит сжечь намерение, если между этими
        мгновениями оборвётся. Здесь ход подделан и рана не создаёт, поэтому подтверждения
        нет — и намерение обязано остаться живым, а не исчезнуть.
        """
        marked: list[str] = []
        claimed: list[str] = []
        tasks.mark_fired = lambda tid: marked.append(tid)
        self._swap(tasks, "claim_open", lambda tid, kind="": claimed.append(tid) or True)
        asyncio.run(mtproto_runner._fire_task(
            {"id": "w1", "kind": "wake", "goal": "прочитать диалоги и ответить"}))
        self.assertEqual(self.transport, [], "связь не тронута — в этом весь смысл вида")
        self.assertEqual(self.woke, ["прочитать диалоги и ответить"])
        self.assertEqual(claimed, ["w1"], "взято в работу при реальном подъёме")
        self.assertEqual(marked, [], "но не погашено: рана-то не появилось")

    def test_a_window_still_does_break_it(self):
        """Контроль: ретрит остался ретритом, иначе проверка выше ничего не значит."""
        tasks.mark_fired = lambda tid: None
        asyncio.run(mtproto_runner._fire_task(
            {"id": "w2", "kind": "window", "goal": "разобрать память"}))
        self.assertEqual(self.transport, ["disconnect", "connect"])

    def test_a_busy_mind_defers_the_wake_without_eating_it(self):
        """Замок занят живым ходом — намерение остаётся due, а не теряется молча (урок F3)."""
        marked: list[str] = []
        tasks.mark_fired = lambda tid: marked.append(tid)

        async def busy():
            async with mtproto_runner._ONE_MIND:
                return await mtproto_runner._wake_pass("потом", on_open=None)

        self.assertIsNone(asyncio.run(busy()))
        self.assertEqual((self.woke, marked, self.transport), ([], [], []))

    def test_the_hourly_pulse_keeps_the_line_up(self):
        """Пульс — её единственное регулярное автономное пробуждение, и он социальный
        насквозь: открытые нити, почта, фолоуапы. Он делал всё это с закрытым Telegram,
        то есть работа, ради которой он есть, была в нём невозможна. Разрыв здесь стоял
        против ПАРАЛЛЕЛЬНЫХ пробуждений (регрессия PASS 25); сегодня от них защищает
        _ONE_MIND, которого тогда не было, и всякий входящий проход его ЖДЁТ."""
        self._swap(mtproto_runner.client, "is_connected", lambda: True)
        asyncio.run(mtproto_runner._task_window("часовой пульс", keep_transport=True))
        self.assertEqual(self.transport, [], "связь не рвётся")
        self.assertEqual(self.windows[0][1].get("transport"), "connected",
                         "рамке отдано наблюдённое состояние линии")

    def test_the_pulse_reports_the_line_it_observes_not_the_one_it_intended(self):
        """«Я не рвал связь» и «связь есть» — разные утверждения. Сокет мог отвалиться сам.

        Сказать ей «Telegram открыт», не посмотрев, — ровно та ложь в рамке, ради которой
        всё это и переписывалось.
        """
        self.fake_connected = False
        self._swap(mtproto_runner.client, "is_connected", lambda: self.fake_connected)
        asyncio.run(mtproto_runner._task_window("пульс", keep_transport=True))
        self.assertEqual(self.windows[0][1].get("transport"), "disconnected")
        self.fake_connected = True
        asyncio.run(mtproto_runner._task_window("пульс", keep_transport=True))
        self.assertEqual(self.windows[1][1].get("transport"), "connected")

    def test_an_ordinary_window_still_breaks_it_and_says_so(self):
        """Контроль: ретрит остался ретритом, и рамка про него не соврала."""
        asyncio.run(mtproto_runner._task_window("разобрать память"))
        self.assertEqual(self.transport, ["disconnect", "connect"])
        self.assertEqual(self.windows[0][1].get("transport"), "closed_for_window")

    def test_a_recurring_wake_honours_the_owners_background_pause(self):
        """«Останови фон» — слово Егора, и мой новый вид не смеет его молча сузить.

        Рекуррентное окно этот рычаг держит с 18.4. Если рекуррентное пробуждение его не
        держит, то один и тот же её замысел, выраженный другим видом, обходит рычаг, о
        котором Егору сказано словами в манифесте рельсов, — и он думает, что фон стоит,
        пока фон идёт.
        """
        import appetite
        marked: list[str] = []
        tasks.mark_fired = lambda tid: marked.append(tid)
        orig_hold = appetite.background_hold
        appetite.background_hold = lambda: "Егор попросил остановить фон"
        try:
            asyncio.run(mtproto_runner._fire_task(
                {"id": "r1", "kind": "wake", "goal": "ежедневный обзор",
                 "recur": "daily 09:00"}))
        finally:
            appetite.background_hold = orig_hold
        self.assertEqual(self.woke, [], "на паузе фона расписание не поднимает её")
        self.assertEqual(marked, ["r1"], "вхождение потреблено и сдвинуто, не потеряно")

    def test_a_one_shot_wake_is_not_held_by_it(self):
        """Разовое — текущее дело (возврат к обещанию, решение по придержке), как и окно."""
        import appetite
        tasks.mark_fired = lambda tid: None
        orig_hold = appetite.background_hold
        appetite.background_hold = lambda: "Егор попросил остановить фон"
        try:
            asyncio.run(mtproto_runner._fire_task(
                {"id": "r2", "kind": "wake", "goal": "довести обещанное"}))
        finally:
            appetite.background_hold = orig_hold
        self.assertEqual(self.woke, ["довести обещанное"])

    def test_the_scheduler_delegates_the_mark_for_wakes_too(self):
        order: list[tuple] = []
        orig_fire = mtproto_runner._fire_task

        async def fake_fire_task(t):
            order.append(("fire", t["id"]))

        tasks.due = lambda: [{"id": "w3", "kind": "wake", "goal": "подъём"}]
        tasks.mark_fired = lambda tid: order.append(("mark", tid))
        mtproto_runner._fire_task = fake_fire_task
        try:
            asyncio.run(mtproto_runner._fire_due_tasks())
        finally:
            mtproto_runner._fire_task = orig_fire
        self.assertEqual(order, [("fire", "w3")],
                         "метку ставит _wake_pass.on_open при реальном подъёме")


class TestALiveLineDoesNotDeafenAChat(unittest.TestCase):
    """Шов, который держался на разрыве связи, а не на собственной корректности.

    `_run_pass` регистрирует чат в `_passing` ДО того, как встанет в очередь за
    `_ONE_MIND`, а ожидание замка — снаружи try/finally. Отмена ровно на этом await
    оставляла бы chat_id в `_passing` навсегда: дальше каждый проход этого чата выходит
    на первой же строке, и комната глохнет насовсем.

    Раньше сюда было не попасть: пока автономное окно держало замок, Telethon был
    отключён, новое сообщение не приходило — а значит и `_arm` не отменял дебаунс-таск.
    26.07 пульс перестал рвать связь, ждать замка стало можно минутами под живым потоком
    входящих, и второе сообщение в тот же чат отменяет таск именно там. Проверка
    воспроизводит это, а не проверяет наличие try.
    """

    def setUp(self):
        # Модульный замок привязывается к первому же циклу, который его тронул, а соседние
        # тесты гоняют свой asyncio.run — берём свежий на время этой проверки.
        self._lock = mtproto_runner._ONE_MIND
        mtproto_runner._ONE_MIND = mtproto_runner._OneMind()
        self.addCleanup(lambda: setattr(mtproto_runner, "_ONE_MIND", self._lock))
        self.chat = "deaf-chat"
        mtproto_runner._passing.discard(self.chat)
        self.addCleanup(mtproto_runner._passing.discard, self.chat)
        self.addCleanup(mtproto_runner._last_pass.pop, self.chat, None)
        self.addCleanup(mtproto_runner._dm_rearm.discard, self.chat)

    def test_cancellation_while_queued_for_the_lock_releases_the_chat(self):
        async def run():
            await mtproto_runner._ONE_MIND.acquire()  # пульс держит замок
            try:
                task = asyncio.create_task(mtproto_runner._run_pass(self.chat))
                for _ in range(500):
                    await asyncio.sleep(0)
                    if self.chat in mtproto_runner._passing:
                        break
                registered = self.chat in mtproto_runner._passing
                task.cancel()  # пришло второе сообщение -> _arm отменил дебаунс-таск
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            finally:
                mtproto_runner._ONE_MIND.release()
            return registered, self.chat in mtproto_runner._passing

        registered, still_marked = asyncio.run(run())
        self.assertTrue(registered, "проход дошёл до очереди за замком — сцена та самая")
        self.assertFalse(still_marked,
                         "чат отпущен: иначе он оглох бы навсегда, до рестарта процесса")

    def test_and_the_chat_can_pass_again_afterwards(self):
        """Глухота проявляется не в момент отмены, а на СЛЕДУЮЩЕМ сообщении."""
        async def run():
            await mtproto_runner._ONE_MIND.acquire()
            try:
                task = asyncio.create_task(mtproto_runner._run_pass(self.chat))
                for _ in range(500):
                    await asyncio.sleep(0)
                    if self.chat in mtproto_runner._passing:
                        break
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            finally:
                mtproto_runner._ONE_MIND.release()
            # следующий проход обязан снова дойти до замка, а не выйти на первой строке
            second = asyncio.create_task(mtproto_runner._run_pass(self.chat))
            for _ in range(500):
                await asyncio.sleep(0)
                if self.chat in mtproto_runner._passing:
                    break
            reached = self.chat in mtproto_runner._passing
            second.cancel()
            try:
                await second
            except asyncio.CancelledError:
                pass
            return reached

        self.assertTrue(asyncio.run(run()), "комната слышит следующее сообщение")


class TestTheLockAnswersHonestly(unittest.TestCase):
    """Праксис: «_one_mind_is_free() опирается на приватное поле asyncio.Lock._waiters;
    там возможна хрупкость и не покрыт тестом fair-handoff с ожидающим конкурентом.»

    Оба замечания верны и закрыты здесь. Вопрос, на который отвечает замок, — не «кто им
    владеет», а «возьмётся ли он, не засыпая»: у asyncio.Lock честная очередь, и сразу
    после release() владельца уже нет, а очередь есть — значит acquire() уснёт. Заботам,
    которых зовут прямо из часов, засыпать нельзя.
    """

    def setUp(self):
        self._lock = mtproto_runner._ONE_MIND
        mtproto_runner._ONE_MIND = mtproto_runner._OneMind()
        self.addCleanup(lambda: setattr(mtproto_runner, "_ONE_MIND", self._lock))

    def test_the_gap_between_release_and_handoff_is_not_called_free(self):
        """Тот самый fair-handoff: владельца уже нет, конкурент ещё не проснулся."""
        async def run():
            lock = mtproto_runner._ONE_MIND
            await lock.acquire()
            rival = asyncio.create_task(lock.acquire())
            await asyncio.sleep(0)                     # конкурент встал в очередь
            while_held = mtproto_runner._one_mind_is_free()
            lock.release()                             # locked() уже False...
            in_the_gap = mtproto_runner._one_mind_is_free()   # ...но очередь непуста
            await rival
            lock.release()
            after_all = mtproto_runner._one_mind_is_free()
            return while_held, in_the_gap, after_all

        while_held, in_the_gap, after_all = asyncio.run(run())
        self.assertFalse(while_held, "занят владельцем")
        self.assertFalse(in_the_gap, "владельца нет, но захват всё равно уснул бы")
        self.assertTrue(after_all, "очередь пуста — только теперь он свободен")

    def test_the_clock_side_caller_never_sleeps_in_that_gap(self):
        """Поведение, а не внутренности: тик обязан пройти насквозь."""
        import absence
        orig = {k: getattr(absence, k) for k in ("due", "window", "schedule_note")}
        absence.due = lambda: [{"chat_id": "77", "name": "Арет"}]
        absence.window = lambda: {"until": 1}
        absence.schedule_note = lambda: "нет до вечера"
        self.addCleanup(lambda: [setattr(absence, k, v) for k, v in orig.items()])

        async def run():
            lock = mtproto_runner._ONE_MIND
            await lock.acquire()
            rival = asyncio.create_task(lock.acquire())
            await asyncio.sleep(0)
            lock.release()                             # ровно тот зазор
            await asyncio.wait_for(mtproto_runner._absence_once(), timeout=2.0)
            await rival
            lock.release()

        asyncio.run(run())  # TimeoutError здесь означал бы вставшие часы

    def test_the_answer_survives_a_python_that_renames_its_internals(self):
        """Хрупкость, которую она и назвала: молча деградировать нельзя.

        Старая реализация читала `_waiters` напрямую; на версии, где поле переименовано
        или стало другим типом, она бы упала или начала врать — а выглядела бы рабочей.
        """
        lock = mtproto_runner._OneMind()
        lock._waiters = "в будущем это может быть чем угодно"
        self.assertTrue(lock.free_now(), "ответ не зависит от приватных полей замка")


class TestAbsenceWaitsWithoutFreezingTheClock(unittest.TestCase):
    """`_absence_once` зовёт модель и ОТПРАВЛЯЕТ людям, поэтому он обязан идти под общим
    замком: иначе, пока пульс работает с живой связью, он заговорит одновременно с ней —
    два её голоса в Telegram в одну секунду.

    Но «под замком» здесь не значит «ждать замка». Часы вызывают эту заботу напрямую, и
    блокирующий захват остановил бы весь тик: буферы, планировщик, outbox, доставку. Гейт
    обязан ПРОПУСКАТЬ ход, а не занимать его, и проверка с захватом обязаны стоять без
    await между собой, иначе замок успевает уйти в промежутке.
    """

    def setUp(self):
        import absence
        self.absence = absence
        self._lock = mtproto_runner._ONE_MIND
        mtproto_runner._ONE_MIND = mtproto_runner._OneMind()
        self.addCleanup(lambda: setattr(mtproto_runner, "_ONE_MIND", self._lock))
        self._orig = {k: getattr(absence, k) for k in ("due", "window", "schedule_note")}
        absence.due = lambda: [{"chat_id": "77", "name": "Арет"}]
        absence.window = lambda: {"until": 1}
        absence.schedule_note = lambda: "нет до вечера"
        self.addCleanup(lambda: [setattr(absence, k, v) for k, v in self._orig.items()])
        self._compose = mtproto_runner.agent.compose_absence_reply

        def never(*a, **kw):
            raise AssertionError("голос не должен звучать поверх занятого замка")

        mtproto_runner.agent.compose_absence_reply = never
        self.addCleanup(lambda: setattr(mtproto_runner.agent, "compose_absence_reply",
                                        self._compose))

    def test_a_busy_mind_skips_the_tick_instead_of_stalling_it(self):
        async def run():
            await mtproto_runner._ONE_MIND.acquire()  # её живой ход / пульс
            try:
                # wait_for и есть проверка: блокирующий захват не уложится в таймаут
                await asyncio.wait_for(mtproto_runner._absence_once(), timeout=2.0)
            finally:
                mtproto_runner._ONE_MIND.release()

        asyncio.run(run())  # TimeoutError здесь означал бы вставшие часы

    def test_a_postponed_absence_reply_is_visible_to_her_too(self):
        seen = []
        orig = mtproto_runner.perception.note_skip
        mtproto_runner.perception.note_skip = lambda *a, **kw: seen.append(a)
        self.addCleanup(lambda: setattr(mtproto_runner.perception, "note_skip", orig))

        async def run():
            await mtproto_runner._ONE_MIND.acquire()
            try:
                await asyncio.wait_for(mtproto_runner._absence_once(), timeout=2.0)
            finally:
                mtproto_runner._ONE_MIND.release()

        asyncio.run(run())
        self.assertTrue(seen, "молча пропущенный ответ человеку — тот же слепой гейт")

    def test_and_the_lock_is_left_free_afterwards(self):
        async def run():
            await mtproto_runner._ONE_MIND.acquire()
            try:
                await asyncio.wait_for(mtproto_runner._absence_once(), timeout=2.0)
            finally:
                mtproto_runner._ONE_MIND.release()
            return mtproto_runner._ONE_MIND.locked()

        self.assertFalse(asyncio.run(run()))


class TestTheNightCycleTakesItsTurn(unittest.TestCase):
    """Ночная работа переписывает то, из чего она в этот же миг может думать.

    Свернуть прожитое в долгую память, выкопать новые основания, заново спросить «всё ещё
    так?» про характер — это не разговор, но и не мелочь: живой ход в четыре утра шёл
    поверх переписываемой памяти. Раньше туда попадал только ночной вопрос Егора; с
    появлением будильника способов стало больше.

    Как и у отсутствия, «под замком» здесь не значит «ждать замка»: заботу зовут часы, и
    блокирующий захват остановил бы весь тик.
    """

    def setUp(self):
        import sleep as _sleep
        self.sleep = _sleep
        self._lock = mtproto_runner._ONE_MIND
        mtproto_runner._ONE_MIND = mtproto_runner._OneMind()
        self.addCleanup(lambda: setattr(mtproto_runner, "_ONE_MIND", self._lock))
        mtproto_runner._SLEEP_TASK = None
        self.addCleanup(lambda: setattr(mtproto_runner, "_SLEEP_TASK", None))
        self._orig = {k: getattr(_sleep, k) for k in ("due", "run_scheduled")}
        self.ran = []
        _sleep.due = lambda *a, **kw: True
        _sleep.run_scheduled = lambda *a, **kw: (self.ran.append(1), "свела ночь")[1]
        self.addCleanup(lambda: [setattr(_sleep, k, v) for k, v in self._orig.items()])

    def test_the_clock_tick_never_waits_for_her(self):
        """Заботу зовут прямо часы: заснуть в ней — остановить буферы, планировщик, outbox."""
        async def run():
            await mtproto_runner._ONE_MIND.acquire()  # её живой ход в четыре утра
            try:
                await asyncio.wait_for(mtproto_runner._consolidate_once(), timeout=2.0)
                started = mtproto_runner._SLEEP_TASK
                self.assertEqual(self.ran, [], "память не переписывается под идущим ходом")
            finally:
                mtproto_runner._ONE_MIND.release()
            await asyncio.wait_for(started, timeout=5.0)
            return self.ran

        # ...и ночь НЕ пропущена: она дождалась своей очереди и прошла
        self.assertEqual(asyncio.run(run()), [1],
                         "ночь ждёт замок, а не уезжает на сутки")

    def test_the_night_is_not_skipped_on_the_last_tick_of_the_window(self):
        """Пропуск стоил бы суток: следующий тик уже вне окна, и due() ответил бы «нет».

        Поэтому ночь ЖДЁТ, а не пропускается — иначе одно неудачно занятое полчаса
        отменяли бы всю ночную работу до завтра.
        """
        async def run():
            await mtproto_runner._ONE_MIND.acquire()
            try:
                await mtproto_runner._consolidate_once()
                task = mtproto_runner._SLEEP_TASK
                self.assertIsNotNone(task, "ночь взята в работу, а не выброшена")
                self.assertFalse(task.done(), "и ждёт замка")
                # окно закрылось: с этого мгновения due() сказал бы «нет»
                self.sleep.due = lambda *a, **kw: False
            finally:
                mtproto_runner._ONE_MIND.release()
            await asyncio.wait_for(task, timeout=5.0)
            return self.ran

        self.assertEqual(asyncio.run(run()), [1])

    def test_a_second_tick_does_not_start_a_second_night(self):
        async def run():
            await mtproto_runner._ONE_MIND.acquire()
            try:
                await mtproto_runner._consolidate_once()
                first = mtproto_runner._SLEEP_TASK
                await mtproto_runner._consolidate_once()
                same = mtproto_runner._SLEEP_TASK is first
            finally:
                mtproto_runner._ONE_MIND.release()
            await asyncio.wait_for(first, timeout=5.0)
            return same, self.ran

        same, ran = asyncio.run(run())
        self.assertTrue(same, "single-flight: вторую ночь не заводим")
        self.assertEqual(ran, [1])

    def test_a_free_mind_lets_the_night_run_and_gives_the_lock_back(self):
        async def run():
            await asyncio.wait_for(mtproto_runner._consolidate_once(), timeout=5.0)
            await asyncio.wait_for(mtproto_runner._SLEEP_TASK, timeout=5.0)
            return mtproto_runner._ONE_MIND.locked()

        self.assertFalse(asyncio.run(run()), "замок отпущен после ночной работы")
        self.assertEqual(self.ran, [1], "ночь всё-таки прошла")

    def test_the_lock_is_released_even_if_the_night_falls_over(self):
        def boom(*a, **kw):
            raise RuntimeError("ночь упала")

        self.sleep.run_scheduled = boom

        async def run():
            await mtproto_runner._consolidate_once()
            try:
                await mtproto_runner._SLEEP_TASK
            except RuntimeError:
                pass
            return mtproto_runner._ONE_MIND.locked()

        self.assertFalse(asyncio.run(run()),
                         "упавшая ночь не имеет права запереть её навсегда")


class TestAfterRunRelease(unittest.TestCase):
    """Микро-намерения «после рана»: удержание снимает планировщик, и только когда ран
    действительно терминален — живой ран намерение не выпускает."""

    def setUp(self):
        self._orig = {"fire": mtproto_runner._fire_task, "due": tasks.due,
                      "holds": tasks.after_run_holds, "clear": tasks.clear_after_run,
                      "terminal": mtproto_runner.agent.run_is_terminal}

    def tearDown(self):
        mtproto_runner._fire_task = self._orig["fire"]
        tasks.due = self._orig["due"]
        tasks.after_run_holds = self._orig["holds"]
        tasks.clear_after_run = self._orig["clear"]
        mtproto_runner.agent.run_is_terminal = self._orig["terminal"]

    def test_hold_released_only_when_run_terminal(self):
        cleared: list[str] = []
        tasks.after_run_holds = lambda: [
            {"id": "w1", "kind": "window", "after_run": "run-done"},
            {"id": "w2", "kind": "window", "after_run": "run-live"},
        ]
        tasks.clear_after_run = lambda tid: (cleared.append(tid), True)[1]
        mtproto_runner.agent.run_is_terminal = lambda rid: rid == "run-done"
        tasks.due = lambda: []

        asyncio.run(mtproto_runner._fire_due_tasks())
        self.assertEqual(cleared, ["w1"],
                         "снимается только удержание завершённого рана; живой ждёт дальше")

    def test_terminal_check_failure_keeps_the_hold(self):
        cleared: list[str] = []
        tasks.after_run_holds = lambda: [{"id": "w3", "kind": "window", "after_run": "run-x"}]
        tasks.clear_after_run = lambda tid: (cleared.append(tid), True)[1]

        def boom(_rid):
            raise RuntimeError("runs store unavailable")

        mtproto_runner.agent.run_is_terminal = boom
        tasks.due = lambda: []

        asyncio.run(mtproto_runner._fire_due_tasks())
        self.assertEqual(cleared, [], "сбой проверки не должен терять намерение")


if __name__ == "__main__":
    unittest.main(verbosity=2)
