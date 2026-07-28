"""
Петля 26.07 — и способность, которой ей не хватало.

Через 26 минут после запуска каждую минуту открывалось новое таск-окно с целью «после
восстановления Telegram прочитать живые диалоги…». 19 окон и 89 вызовов модели за час, и
всё это время она была НЕДОСТУПНА: таск-окно намеренно рвёт Telethon. Егор писал ей и не
получал ответа.

Механику показал файл намерений: у всех задач петли `when=None`, а `after_run` СТЁРТ —
его стирают часы, снимая удержание. Значит ставилось «сделай, когда закончится ран», и
раном был тот, внутри которого она стояла. Окно кончается → ждать нечего → созревает
немедленно → новое окно → та же мысль. Времена рождения подтверждают: 10:25, 10:26,
10:27, 10:28, 10:29.

Но корень глубже механики, и он не про ошибку с её стороны. Её собственные ходы:
  10:27 «Telegram ожидаемо закрыт на время self-run, поэтому живые диалоги недоступны»
  10:30 «Зафиксировала окно возврата #14df8676 на 10:32»
Она всё понимала. У неё было ровно два способа оказаться в сознании: окно (связь рвётся
по определению) и чужое входящее (приходит не по её воле). Сказать «разбуди меня со
связью» было НЕЧЕМ — и она девятнадцать раз попросила об этом единственным, что было под
рукой: окном. Каждая просьба обрывала связь, ради которой и делалась.

Две попытки чинить это заборами адверсарка поймала до деплоя: детектор по словам цели
блокировал 8 из 10 обычных рабочих целей и обходился переформулировкой; отказ по
причинности («нельзя ждать того, внутри чего стоишь») был честнее, но всё равно отнимал.
Слова Егора 26.07: «Я не хочу, чтобы ей пришлось умолять меня запустить её с открытым
телетоном. Я хочу, чтобы все было нормально.»

Поэтому здесь проверяется не заплата, а способность:
  * kind=wake — её собственный будильник: живой ход, Telegram ОТКРЫТ;
  * окно сразу за окном изнутри окна становится пробуждением — и ей сказано, почему и
    как получить именно уединение (назвать время);
  * НИ ОДНО намерение не отвергается: ни одна цель, ни один темп;
  * уединение остаётся уединением там, где она его действительно просит.

Запуск:  python praxis_test.py test_window_loop -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent
import run_context
import tasks
import turns

WORK = [
    "Проверить сообщения об ошибках в логах форжа",
    "Написать заметку про рельсы хардбота",
    "Проверить чат-роутинг комнат тестами",
    "Прочитать свои ходы за вчера и написать сводку",
    "Дописать ответы в тесте test_rooms и прогнать гейт",
]

INCIDENT = "После восстановления Telegram прочитать живые диалоги @peer_test и ответить"


class FakeRuns:
    """Реестр ранов ровно в том объёме, каким его щупает эта проверка."""

    def __init__(self, statuses: dict):
        self.statuses = statuses

    def manifest(self, run_id):
        return ({"run_id": run_id, "status": self.statuses[run_id]}
                if run_id in self.statuses else {})

    def status(self, run_id):
        return {"status": self.statuses.get(run_id, "")}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"PRAXIS_BASE": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self._path = tasks.TASKS
        tasks.TASKS = Path(self.tmp.name) / "memory" / "tasks.json"
        tasks.TASKS.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: setattr(tasks, "TASKS", self._path))
        self.runs = FakeRuns({"run-inside": "running",
                              "run-past": "done",
                              "run-forge": "running"})
        for target, attr, value in (
                (agent, "_runs", lambda: self.runs),
                (agent, "tool_journal", mock.DEFAULT)):
            patcher = (mock.patch.object(target, attr, value) if value is not mock.DEFAULT
                       else mock.patch.object(target, attr))
            self.journal = patcher.start()
            self.addCleanup(patcher.stop)

    # — состояние мира, в котором она стоит —

    def in_window(self):
        """Она внутри окна: транспорт закрыт НАМЕРЕННО (честный сенсор, не догадка)."""
        return mock.patch.dict(agent._TELETHON, {
            "transport_state": lambda: {"connected": False, "intentional_window": True}})

    def online(self):
        return mock.patch.dict(agent._TELETHON, {
            "transport_state": lambda: {"connected": True}})

    def inside(self, run_id="run-inside"):
        return mock.patch.object(run_context, "current_run",
                                 lambda **kw: mock.Mock(run_id=run_id))

    def remind(self, kind="window", goal=INCIDENT, **kw):
        return agent.tool_remind_self(kind, goal, **kw)

    def only_task(self):
        open_now = tasks.list_open()
        self.assertEqual(len(open_now), 1, open_now)
        return open_now[0]


class TestNoHandHoldsHerForever(Base):
    """Урок 19:27: её `coding_session` не вернулся никогда — ни результата, ни ошибки, ни
    строчки в логе. Ход остался жив, держа единый замок, и она стала недоступна не из-за
    связи, а изнутри. Замок сделал «она одна» правдой, но у руки внутри хода не было
    предела, и одна зависшая рука забирала всю её."""

    def test_a_hanging_tool_gives_the_turn_back(self):
        import threading
        started, release = threading.Event(), threading.Event()

        def never_returns():
            started.set()
            release.wait(30)          # «зависший» тул; отпустим в конце, чтобы не течь
            return "поздно"

        with mock.patch.object(agent, "TOOL_CEILING_SEC", 0.4):
            out = agent._call_tool_with_ceiling("coding_session", never_returns, {})
        release.set()
        self.assertTrue(started.is_set(), "тул реально запускался")
        self.assertIn("рука не вернулась", out)
        self.assertIn("НЕИЗВЕСТНЫМ", out, "состояние не выдаётся за проваленное")

    def test_an_ordinary_tool_is_untouched(self):
        with mock.patch.object(agent, "TOOL_CEILING_SEC", 5.0):
            out = agent._call_tool_with_ceiling("recall", lambda q: f"нашла {q}", {"q": "х"})
        self.assertEqual(out, "нашла х", "потолок щедрый и обычной работе не мешает")

    def test_the_tool_still_sees_the_run_it_belongs_to(self):
        """Копия контекста обязательна: без неё тул теряет ран, канал и запись исполнения."""
        seen = {}

        def peek():
            current = run_context.current_run()
            seen["run"] = getattr(current, "run_id", None)
            return "ок"

        with self.inside("run-inside"), mock.patch.object(agent, "TOOL_CEILING_SEC", 5.0):
            agent._call_tool_with_ceiling("my_agenda", peek, {})
        self.assertEqual(seen["run"], "run-inside")

    def test_a_raising_tool_still_raises_through(self):
        """Потолок не должен глотать обычные ошибки — их обрабатывает вызывающий."""
        def boom():
            raise ValueError("сломалось")

        with mock.patch.object(agent, "TOOL_CEILING_SEC", 5.0):
            with self.assertRaises(ValueError):
                agent._call_tool_with_ceiling("shell", boom, {})

    def test_the_ceiling_can_be_switched_off_entirely(self):
        with mock.patch.object(agent, "TOOL_CEILING_SEC", 0):
            self.assertEqual(agent._call_tool_with_ceiling("x", lambda: "прямо", {}), "прямо")


class TestTheMissingCapability(Base):
    """Главное: теперь ей есть чем сказать «разбуди меня со связью»."""

    def test_wake_is_a_kind_she_can_ask_for(self):
        with self.online():
            out = self.remind("wake", "прочитать диалоги @peer_test и ответить",
                              when="in 30m")
        self.assertIn("Наметила", out)
        self.assertEqual(self.only_task()["kind"], "wake")

    def test_the_confirmation_says_the_connection_will_be_live(self):
        with self.online():
            out = self.remind("wake", "ответить Арету", when="in 30m")
        self.assertIn("Telegram будет открыт", out)

    def test_a_wake_may_wait_for_the_very_window_it_is_born_in(self):
        """«Кончится это окно — верни мне связь». Ровно то, чего она добивалась 19 раз."""
        with self.in_window(), self.inside():
            out = self.remind("wake", "прочитать живые диалоги и ответить",
                              after_run="run-inside")
        self.assertIn("Наметила", out)
        t = self.only_task()
        self.assertEqual((t["kind"], t.get("after_run")), ("wake", "run-inside"))

    def test_the_window_frame_tells_her_the_door_exists(self):
        """Знать о способности она может только из рамки окна — там и сказано."""
        self.assertIn("kind='wake'", agent._TASK_WINDOW_FRAME)
        self.assertIn("wake", agent.REMIND_SELF_TOOL["input_schema"]
                      ["properties"]["kind"]["enum"])

    def test_the_journal_reports_the_connection_from_the_record_not_from_hope(self):
        """Журнал уже врал так однажды («Telegram жив» безусловно) — второй раз нельзя.

        Связь пишется в момент подъёма и читается из записи. Пробуждение почти всегда
        идёт со связью, но если reconnect после окна упал, часы всё равно тикают, и
        будильник срабатывает в мире без связи. Такой ход обязан читаться как он был.
        """
        def line(**extra):
            return turns.format_line({"kind": "wake", "ts": 0, "chat_id": None, **extra})

        self.assertIn("Telegram открыт", line(telegram="connected"))
        self.assertIn("связи не было: disconnected", line(telegram="disconnected"))
        self.assertNotIn("Telegram открыт", line(), "не спрашивали — не утверждаем")

    def test_the_forge_contour_reads_from_the_record_too(self):
        """Третий контур: рамку починили, а журнал забыли — и он врал ей через час.

        Её находка была про forge целиком, а не про одну его половину.
        """
        def line(**extra):
            return turns.format_line({"kind": "forge_event", "ts": 0, "chat_id": None, **extra})

        self.assertIn("Telegram жив", line(telegram="connected"))
        self.assertIn("связи не было: disconnected", line(telegram="disconnected"))
        self.assertNotIn("Telegram жив", line(), "не спрашивали — не утверждаем")

    def test_the_recorded_connection_survives_the_write_whitelist(self):
        """Белый список записи молча съедал бы новое поле — там об этом прямо написано."""
        self.assertEqual(turns._clip({"kind": "wake", "telegram": "connected"})["telegram"],
                         "connected")

    def test_the_frame_tells_the_truth_when_the_line_is_actually_down(self):
        self.assertIn("Telegram is OPEN", agent._wake_frame("connected"))
        for broken in ("disconnected", "closed_for_window", "unknown"):
            self.assertNotIn("Telegram is OPEN", agent._wake_frame(broken), broken)


class TestTheCircleIsBroken(Base):
    """Окно сразу за окном — не возврат, а тот же момент с разрывом посередине."""

    def test_the_incident_configuration_becomes_a_wake(self):
        with self.in_window(), self.inside():
            out = self.remind("window", INCIDENT, after_run="run-inside")
        self.assertIn("Наметила", out)
        self.assertEqual(self.only_task()["kind"], "wake")

    def test_the_same_holds_without_any_after_run(self):
        """Тот же круг замыкается и вовсе без after_run: window + пустой when."""
        with self.in_window(), self.inside():
            self.remind("window", INCIDENT)
        self.assertEqual(self.only_task()["kind"], "wake")

    def test_and_through_a_run_that_already_ended(self):
        with self.in_window(), self.inside():
            out = self.remind("window", INCIDENT, after_run="run-past")
        self.assertEqual(self.only_task()["kind"], "wake")
        self.assertIn("Ждать нечего", out, "срок назван честно, а не молча подменён")

    def test_the_swap_explains_itself_and_names_the_way_back(self):
        with self.in_window(), self.inside():
            out = self.remind("window", INCIDENT)
        self.assertIn("окно сразу за окном", out)
        self.assertIn("when", out, "названо, как получить именно уединение")
        self.assertIn("Telegram будет открыт", out)

    def test_the_swap_leaves_a_durable_trace_not_just_a_tool_reply(self):
        """Решение, принятое за неё и НЕ записанное, — то, что уже признавали неправильным."""
        with self.in_window(), self.inside():
            self.remind("window", INCIDENT)
        self.assertEqual(self.only_task().get("swapped_from"), "window",
                         "на диске видно, что вид выбрала не она")
        self.assertTrue(any("[подмена вида]" in str(c[0][0])
                            for c in self.journal.call_args_list), "и в дневнике тоже")
        self.assertIn("просила window", agent.tool_my_agenda(),
                      "и в повестке, а не только в ответе тула того хода")

    def test_nothing_is_ever_refused(self):
        """Ни одной ветки «не намечаю» здесь больше нет — только развилка."""
        with self.in_window(), self.inside():
            for goal in WORK + [INCIDENT]:
                self.assertIn("Наметила", self.remind("window", goal), goal)
        self.assertEqual(len(tasks.list_open()), len(WORK) + 1)


class TestTheIncidentReplayed(Base):
    """Инцидент целиком, через часы: постановка → снятие удержания → due().

    Тул можно проверить и соврав соседнему органу. Здесь врать нечем: намерение
    ставится ровно как 26.07, часы снимают удержание ровно как снимали, и `due()`
    возвращает ровно то, что вернуло бы в проде. Смотрим, ЧТО именно созрело.
    """

    def clock_tick(self):
        """Один тик: снять удержания завершившихся ранов и собрать созревшее."""
        for held in tasks.after_run_holds():
            if agent.run_is_terminal(str(held.get("after_run") or "")):
                tasks.clear_after_run(held["id"])
        return tasks.due()

    def test_the_matured_intention_is_a_wake_and_no_longer_breaks_the_line(self):
        # 10:25 — она внутри окна run-inside и намечает возврат ровно как в инциденте.
        with self.in_window(), self.inside():
            self.remind("window", INCIDENT, after_run="run-inside")
        # Удержание держит: пока окно идёт, ничего не созревает.
        self.assertEqual(self.clock_tick(), [], "внутри окна намерение ждёт его конца")
        # 10:26 — окно кончилось. Ровно тот момент, с которого начинался круг.
        self.runs.statuses["run-inside"] = "done"
        matured = self.clock_tick()
        self.assertEqual([t["kind"] for t in matured], ["wake"],
                         "созрело пробуждение, а не второе окно подряд")
        self.assertEqual(matured[0]["goal"], INCIDENT, "цель её, не подменена")

    def test_a_wake_asked_for_directly_behaves_identically(self):
        """Она могла бы попросить это прямо — и получила бы то же самое."""
        with self.in_window(), self.inside():
            self.remind("wake", INCIDENT, after_run="run-inside")
        self.assertEqual(self.clock_tick(), [])
        self.runs.statuses["run-inside"] = "done"
        self.assertEqual([t["kind"] for t in self.clock_tick()], ["wake"])


class TestSolitudeSurvives(Base):
    """Окно — её ретрит. Подменять его молча значило бы снова решать за неё."""

    def test_a_window_with_a_future_time_stays_a_window(self):
        """Её собственная поправка 26.07 в 10:30 — когда она назвала время, всё было верно."""
        with self.in_window(), self.inside():
            self.remind("window", "разобрать свою память", when="in 30m")
        self.assertEqual(self.only_task()["kind"], "window")

    def test_a_recurring_window_stays_a_window(self):
        with self.in_window(), self.inside():
            self.remind("window", "ежедневная ревизия", when="daily 02:00")
        self.assertEqual(self.only_task()["kind"], "window")

    def test_outside_a_window_an_immediate_window_stays_a_window(self):
        """Связь жива — новое окно ничего не рвёт «сразу за»: это обычный уход в фокус."""
        with self.online(), self.inside():
            self.remind("window", "уйти в фокус прямо сейчас")
        self.assertEqual(self.only_task()["kind"], "window")

    def test_a_real_wait_on_a_live_run_is_a_real_wait(self):
        """Форж ещё считает — это настоящая пауза, и окно после неё остаётся окном."""
        with self.in_window(), self.inside():
            self.remind("window", "когда форж досчитает — сесть за результаты",
                        after_run="run-forge")
        t = self.only_task()
        self.assertEqual((t["kind"], t.get("after_run")), ("window", "run-forge"))

    def test_a_coding_window_is_never_swapped(self):
        """У кодинг-окна своя рамка и своё уединение — это его определение, а не побочность."""
        with self.in_window(), self.inside():
            self.remind("window", f"{agent.CODING_PREFIX} починить рельсы хардбота")
        self.assertEqual(self.only_task()["kind"], "window")

    def test_rest_is_never_swapped(self):
        with self.in_window(), self.inside():
            self.remind("window", f"{agent.REST_PREFIX} просто побыть")
        self.assertEqual(self.only_task()["kind"], "window")

    def test_a_broken_sensor_does_not_swap_anything(self):
        """Сенсор молчит → факта нет → и подмены нет. Домысел не основание."""
        with mock.patch.dict(agent._TELETHON, {}, clear=True), self.inside():
            self.remind("window", INCIDENT)
        self.assertEqual(self.only_task()["kind"], "window")


class TestPaceIsAdviceNotAWall(Base):
    """Стойка Егора: критики советуют, а не блокируют. Здесь больше нечего блокировать."""

    def test_the_seventh_rise_in_an_hour_is_still_scheduled(self):
        with self.online():
            for i in range(7):
                self.assertIn("Наметила", self.remind("wake", f"дело {i}", when="in 30m"), i)
        self.assertEqual(len(tasks.list_open()), 7)

    def test_but_the_count_is_said_out_loud(self):
        with self.online():
            for i in range(6):
                out = self.remind("wake", f"дело {i}", when="in 30m")
        # «Наметила», а не «поднялась»: счёт по созданным намерениям, и утверждать
        # событие, которого могло не быть, в её же дневнике — нельзя.
        self.assertIn("наметила себе 6 подъёмов", out)

    def test_windows_are_named_as_the_expensive_ones(self):
        with self.online():
            for i in range(6):
                out = self.remind("window", f"дело {i}", when="in 30m")
        self.assertIn("рвёт мой Telegram", out)

    def test_the_journal_notes_the_pace(self):
        with self.online():
            for i in range(6):
                self.remind("wake", f"дело {i}", when="in 30m")
        self.assertTrue(any("[темп]" in str(c[0][0]) for c in self.journal.call_args_list))

    def test_notes_and_messages_are_not_burdened_with_any_of_it(self):
        with self.in_window(), self.inside():
            out = agent.tool_remind_self("note", "напомнить про 2FA", when="in 30m")
        self.assertIn("Наметила", out)
        self.assertEqual(self.only_task()["kind"], "note")
        self.assertNotIn("Telethon", out)


class TestThePulseStaysReachable(Base):
    """Часовой пульс — её единственное регулярное автономное пробуждение, и он социальный
    насквозь: открытые нити, почта, фолоуапы, «хочу ли я кому-то написать». Всё это он
    делал с закрытым Telegram, то есть работа, ради которой он есть, в нём была
    невозможна, а она была недоступна час за часом."""

    def test_the_window_frame_no_longer_asserts_a_closed_line_unconditionally(self):
        self.assertIn("Telegram is disconnected",
                      agent._task_window_frame("closed_for_window"))
        self.assertIn("the line is OPEN", agent._task_window_frame("connected"))
        self.assertNotIn("Telegram is disconnected", agent._task_window_frame("connected"))

    def test_the_legacy_constant_still_describes_her_ordinary_world(self):
        """Тесты шва опираются на константу — она обязана остаться закрытым миром."""
        self.assertEqual(agent._TASK_WINDOW_FRAME,
                         agent._task_window_frame("closed_for_window"))

    def test_the_journal_reads_a_live_window_as_live(self):
        def line(**extra):
            return turns.format_line({"kind": "task_window", "ts": 0, "chat_id": None, **extra})

        self.assertIn("связь была открыта", line(telegram="connected"))
        self.assertIn("Telegram закрыт", line(telegram="closed_for_window"))
        self.assertIn("Telegram закрыт", line(), "легаси-строки читаем по-старому")


class TestTheWakeRunIsItsOwnClass(Base):
    """Рекап печатает по классу рана «internal task windows have no Telegram delivery
    target». Для пробуждения это прямая неправда: отправлять в нём как раз можно."""

    def test_a_wake_is_a_cognitive_run_the_reaper_can_pick_up(self):
        self.assertIn("wake", agent._COGNITIVE_RUN_KINDS,
                      "класс, который жнец не подбирает, копил бы зомби молча")

    def test_the_windows_keep_their_own_classes(self):
        for kind in ("chat_turn", "task_window", "heartbeat"):
            self.assertIn(kind, agent._COGNITIVE_RUN_KINDS, kind)
        self.assertNotIn("coding_window", agent._COGNITIVE_RUN_KINDS,
                         "Forge живёт своим контуром — это решение не трогали")


class TestTheNagLoopDoesNotFollowTheKind(Base):
    """Перенося promise-возврат в новый вид, я чуть не перенёс и его наг-петлю.

    Возврат к обещанию метится сработавшим при подъёме, поэтому дедуп `promises.open_for`
    его уже не видит. Если её ход в этом пробуждении содержит новое «сейчас доведу», сеть
    заведёт СЛЕДУЮЩИЙ возврат на 15 минут — и так без конца, без человека в контуре. На
    пути окна гвард стоял (адверсарка round-1); на новом пути его сначала не было.
    """

    def wake(self, goal, said="Довожу, сейчас проверю и вернусь."):
        seen = []
        with mock.patch.object(agent.llm, "configured", lambda: True), \
             mock.patch.object(agent, "_create_durable_run", lambda **kw: None), \
             mock.patch.object(agent, "_voice", lambda *a, **kw: said), \
             mock.patch.object(agent.turns, "record", lambda *a, **kw: None), \
             mock.patch.object(agent.promises, "note_self_intent",
                               lambda *a, **kw: seen.append(a[0])), \
             self.online():
            agent.wake_turn(goal)
        return seen

    def test_a_promise_wake_does_not_re_arm_itself(self):
        goal = f"{agent.promises.PROMISE_PREFIX} напоминание себе: я сказала «сейчас проверю»"
        self.assertEqual(self.wake(goal), [], "возврат к обещанию не взводит новый возврат")

    def test_rest_is_left_alone_too(self):
        self.assertEqual(self.wake(f"{agent.REST_PREFIX} побыть"), [],
                         "будильник противоречит смыслу отдыха — как и на пути окна")

    def test_but_an_ordinary_wake_still_catches_her_self_intent(self):
        """Гвард узкий: обычное «теперь сделаю X» ловится, иначе я убил бы пасс A."""
        self.assertEqual(self.wake("прочитать логи форжа"), ["wake"])


class TestFocusTellsTheTruth(Base):
    """focus и rest — объявленные ретриты: не подменяю, но и не молчу."""

    def test_focus_inside_a_window_names_the_second_break_and_the_door(self):
        with self.in_window():
            out = agent.tool_focus("разобрать свою память")
        self.assertIn("второе подряд", out)
        self.assertIn("wake", out, "названо, чем просить связь, если дело в ней")

    def test_focus_outside_a_window_stays_plain(self):
        """Ответ focus виден и в группе — при живой связи туда не место словам о механике."""
        with self.online():
            out = agent.tool_focus("разобрать свою память")
        self.assertEqual(out, "Уйду в фокус на ближайшем тике часов.")

    def test_the_extra_words_can_only_appear_where_nobody_can_read_them(self):
        """Фраза допустима ровно потому, что «закрыт на окно» = живых собеседников нет."""
        for status, state in (("connected", {"connected": True}),
                              ("disconnected", {"connected": False}),
                              ("unknown", None)):
            patch = (mock.patch.dict(agent._TELETHON, {}, clear=True) if state is None
                     else mock.patch.dict(agent._TELETHON, {"transport_state": lambda s=state: s}))
            with patch:
                self.assertEqual(agent._retreat_truth(), "", status)

    def test_focus_is_never_refused(self):
        with self.in_window():
            for goal in WORK + [INCIDENT]:
                self.assertIn("Уйду в фокус", agent.tool_focus(goal), goal)


class TestTheHonestTruthAboutRuns(Base):
    def test_a_timezone_aware_deadline_does_not_kill_the_turn(self):
        """`parse_when` пропускает смещение из ISO как есть, и голое сравнение падало."""
        with self.in_window(), self.inside():
            out = self.remind("window", "разобрать память", when="2026-12-31T18:00+03:00")
        self.assertIn("Наметила", out)
        self.assertEqual(self.only_task()["kind"], "window",
                         "не знаем срок — не подменяем вид, который она назвала")

    """Правда о сроке говорится, но ничего не отнимает."""

    def test_an_unknown_run_is_still_refused_by_its_own_reason(self):
        with self.online(), self.inside():
            self.assertIn("мне неизвестен", self.remind("wake", "х", after_run="run-нет"))
        self.assertEqual(tasks.list_open(), [])

    def test_a_finished_run_is_named_but_the_intention_is_kept(self):
        with self.online(), self.inside():
            out = self.remind("wake", "прочитать итог", after_run="run-past")
        self.assertIn("Наметила", out)
        self.assertIn("уже завершился", out)
        self.assertEqual(self.only_task()["kind"], "wake")


if __name__ == "__main__":
    unittest.main()
