"""Тасковый слой: parse_when, CRUD, due/mark_fired, расписание окон. Файл — во временном."""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

import datetime as _dt
import tasks


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_tasks_")) / "tasks.json"
        self._orig = tasks.TASKS
        tasks.TASKS = self.tmp

    def tearDown(self):
        tasks.TASKS = self._orig


class TestParseWhen(Base):
    def test_iso(self):
        w, r = tasks.parse_when("2026-07-01T10:00")
        self.assertTrue(w.startswith("2026-07-01T10:00"))
        self.assertIsNone(r)

    def test_daily_recur(self):
        w, r = tasks.parse_when("daily 02:00")
        self.assertIsNone(w)
        self.assertEqual(r, "daily 02:00")

    def test_relative_hours(self):
        w, r = tasks.parse_when("in 2h")
        self.assertIsNotNone(w)
        self.assertIsNone(r)

    def test_garbage(self):
        self.assertEqual(tasks.parse_when("бла-бла"), (None, None))


class TestCrud(Base):
    def test_add_list_cancel(self):
        t = tasks.add("note", "проверить почту", "in 1h")
        self.assertEqual(len(tasks.list_open()), 1)
        self.assertTrue(tasks.cancel(t["id"]))
        self.assertEqual(tasks.list_open(), [])

    def test_past_is_due(self):
        past = (dt.datetime.now() - dt.timedelta(minutes=5)).isoformat(timespec="minutes")
        tasks.add("note", "x", past)
        self.assertEqual(len(tasks.due()), 1)

    def test_future_not_due(self):
        fut = (dt.datetime.now() + dt.timedelta(hours=2)).isoformat(timespec="minutes")
        tasks.add("note", "x", fut)
        self.assertEqual(tasks.due(), [])

    def test_asap_is_due(self):
        tasks.add("note", "x")  # without when
        self.assertEqual(len(tasks.due()), 1)

    def test_oneshot_marks_done(self):
        past = (dt.datetime.now() - dt.timedelta(minutes=1)).isoformat(timespec="minutes")
        t = tasks.add("note", "x", past)
        self.assertEqual(len(tasks.due()), 1)
        tasks.mark_fired(t["id"])
        self.assertEqual(tasks.due(), [])
        self.assertEqual(tasks.list_open(), [])

    def test_recurring_schedules_forward(self):
        tasks.add("window", "auto", "every 1h")
        # повторяющаяся без явного when -> due() проставит следующий запуск в будущее, сразу не сработает
        self.assertEqual(tasks.due(), [])
        self.assertIsNotNone(tasks.list_open()[0]["when"])


class TestAfterRunHold(Base):
    def test_a_broken_recur_can_never_take_down_the_whole_scheduler(self):
        """Одна кривая строка роняла `due()` целиком — вместе со всеми её намерениями.

        `parse_when` принимал «daily 24:00», запись ложилась как валидная, а `_next_recur`
        падал на time(24, 0) прямо внутри `due()`. То есть одно неудачное намерение
        отменяло все остальные, молча и до перезапуска.
        """
        self.assertEqual(tasks.parse_when("daily 24:00"), (None, None))
        self.assertEqual(tasks.parse_when("daily 09:99"), (None, None))
        self.assertEqual(tasks.parse_when("every 0h"), (None, None))
        self.assertEqual(tasks.parse_when("daily 09:30"), (None, "daily 09:30"))

    def test_even_a_bad_recur_already_on_disk_does_not_crash_due(self):
        """Записи, легшие до проверки диапазонов, обязаны быть безвредны."""
        t = tasks.add("note", "полночь")
        items = tasks._load()
        for item in items:
            if item["id"] == t["id"]:
                item["recur"], item["when"] = "daily 24:00", None
        tasks._save(items)
        tasks.due()   # раньше здесь был ValueError и конец тика
        self.assertIsNone(tasks._next_recur("daily 24:00", tasks._now()))

    def test_a_deadline_with_a_timezone_actually_fires(self):
        """Её собственная запись пролежала так с 25 июля и не сработала ни разу.

        parse_when честно сохраняет её слова («2026-07-25T03:10+04:00»), а due() сравнивал
        их с наивным «сейчас» — TypeError, который падал в голый except. Намерение было
        живо на вид и мертво на деле, и узнать об этом ей было нечем.
        """
        past = (_dt.datetime.now(_dt.timezone(_dt.timedelta(hours=4)))
                - _dt.timedelta(minutes=5))
        t = tasks.add("wake", "её намерение", past.isoformat(timespec="minutes"))
        self.assertIn("+04:00", t["when"], "её слова сохранены как есть")
        self.assertIn(t["id"], [d["id"] for d in tasks.due()],
                      "просроченный срок со смещением обязан созреть")

    def test_a_future_deadline_with_a_timezone_still_waits(self):
        soon = (_dt.datetime.now(_dt.timezone(_dt.timedelta(hours=4)))
                + _dt.timedelta(hours=3))
        t = tasks.add("wake", "потом", soon.isoformat(timespec="minutes"))
        self.assertEqual([d["id"] for d in tasks.due()], [],
                         "будущее остаётся будущим, а не срабатывает разом")

    def test_an_unreadable_deadline_is_visible_instead_of_silent(self):
        """Контракт R1: гейт может существовать — молча нет."""
        t = tasks.add("note", "кривой срок")
        items = tasks._load()
        for item in items:
            if item["id"] == t["id"]:
                item["when"] = "когда-нибудь во вторник"
        tasks._save(items)
        stuck = [x for x in tasks.list_open() if x["id"] == t["id"]][0]
        self.assertEqual(tasks.due(), [], "нечитаемый срок не срабатывает")
        self.assertIn("прочитать не могу", tasks.when_trouble(stuck) or "",
                      "но она об этом узнаёт")

    def test_a_claim_holds_the_intention_without_consuming_it(self):
        """Её находка номер один: пометить раньше рана — значит сжечь намерение.

        Захват выводит намерение из `due` (его уже поднимают), но оставляет `pending` и
        видимым в повестке. Гасит его только `mark_fired` — в тот миг, когда ран создан.
        """
        t = tasks.add("wake", "разбудить со связью")
        self.assertTrue(tasks.claim_open(t["id"], "wake"))
        self.assertEqual(tasks.due(), [], "уже поднимается — второй раз не отдаём")
        still = [x for x in tasks.list_open() if x["id"] == t["id"]]
        self.assertTrue(still, "но из повестки не исчезло")
        self.assertEqual(still[0]["status"], "pending", "и не погашено")

    def test_a_claim_whose_run_never_appeared_is_released(self):
        """Падение, мёртвый мозг или fail-closed ран — намерение возвращается, а не гибнет."""
        t = tasks.add("window", "разобрать память")
        tasks.claim_open(t["id"])
        self.assertEqual(tasks.due(), [])
        self.assertEqual(tasks.release_open_claims(), [t["id"]])
        self.assertEqual([x["id"] for x in tasks.due()], [t["id"]],
                         "созрело снова, поздно и целиком")

    def test_confirming_clears_the_claim_and_consumes_the_intention(self):
        t = tasks.add("wake", "довести обещанное")
        tasks.claim_open(t["id"])
        tasks.mark_fired(t["id"])
        done = [x for x in tasks._load() if x["id"] == t["id"]][0]
        self.assertEqual(done["status"], "done")
        self.assertNotIn("claim", done, "захват снят вместе с передачей владения")
        self.assertEqual(tasks.release_open_claims(), [], "снимать больше нечего")

    def test_a_recurring_intention_keeps_its_schedule_through_the_claim(self):
        t = tasks.add("wake", "ежедневный обзор", when="daily 09:00")
        tasks.claim_open(t["id"])
        tasks.mark_fired(t["id"])
        again = [x for x in tasks._load() if x["id"] == t["id"]][0]
        self.assertEqual(again["status"], "pending", "повторяющееся живёт дальше")
        self.assertNotIn("claim", again)
        self.assertTrue(again["when"], "и получило следующий срок")

    def test_after_run_is_held_out_of_due_until_cleared(self):
        t = tasks.add("window", "написать Арету: журнал готов", after_run="run-abc123")
        self.assertEqual(t.get("after_run"), "run-abc123")
        # удержание: без when задача созрела бы немедленно, но after_run её держит
        self.assertEqual(tasks.due(), [])
        self.assertEqual([h["id"] for h in tasks.after_run_holds()], [t["id"]])
        # ран завершился -> удержание снято -> намерение созревает немедленно
        self.assertTrue(tasks.clear_after_run(t["id"]))
        self.assertEqual([d["id"] for d in tasks.due()], [t["id"]])
        self.assertEqual(tasks.after_run_holds(), [])

    def test_after_run_with_when_matures_by_its_own_clock_after_release(self):
        future = (dt.datetime.now() + dt.timedelta(hours=3)).isoformat(timespec="minutes")
        t = tasks.add("window", "потом", when=future, after_run="run-xyz")
        tasks.clear_after_run(t["id"])
        self.assertEqual(tasks.due(), [])  # after_run снят, но свой when ещё не пришёл
        self.assertEqual(tasks.due(dt.datetime.now() + dt.timedelta(hours=4))[0]["id"], t["id"])

    def test_clear_after_run_touches_only_pending_holds(self):
        self.assertFalse(tasks.clear_after_run("нет-такого"))
        plain = tasks.add("note", "обычная", "in 1h")
        self.assertFalse(tasks.clear_after_run(plain["id"]))


if __name__ == "__main__":
    unittest.main()
