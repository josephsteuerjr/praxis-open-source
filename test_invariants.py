"""
Инварианты швов (PASS 11.5) — парные сторы, кодирующие одну правду, не должны
рассинхронизироваться. Класс багов «глухие комнаты» (d5a931b): профиль комнаты говорил
normal-by-owner, а allowlist был пуст — и раннер молча глушил всех, кроме владельца.

Иммунитет (immune.py) ловит ДИФФ её самоправок; эти тесты ловят СМЫСЛ — если её (или наша)
правка нарушит семантику пары, деплой-гейт падает здесь, а не в проде.

Запуск:  python praxis_test.py test_invariants -v
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import time
import unittest
from pathlib import Path

from test_perceive import Base
import heartbeat
import people
import rooms
import unanswered


class TestProcessTestBoundary(unittest.TestCase):
    def test_unittest_process_is_redirected_before_praxis_imports(self):
        base = Path(os.environ.get("PRAXIS_BASE") or "").resolve()
        self.assertEqual(
            os.environ.get("PRAXIS_TEST"), "1",
            "run this suite through `python praxis_test.py ...`, not bare unittest",
        )
        self.assertNotEqual(base, Path(__file__).resolve().parent,
                            "unit tests must never share the checkout runtime tree")


def _iso(days: int = 0) -> str:
    return (_dt.date.today() + _dt.timedelta(days=days)).isoformat()


class RoomsHarness(Base):
    """rooms-пути в песочницу (идиом RoomBase из test_pass10, без наследования его тестов)."""

    def setUp(self):
        super().setUp()
        mem = self.tmp / "memory"
        for k, v in dict(BASE=self.tmp, MEM_DIR=mem, ROOMS_DIR=mem / "rooms",
                         ALLOWLIST=mem / "rooms_allowlist.json",
                         FROZEN=mem / "frozen_chats.json",
                         CARDS_PATH=mem / ".state" / "room_cards.json").items():
            self._orig.append((rooms, k, getattr(rooms, k)))
            setattr(rooms, k, v)


class TestRoomInvariants(RoomsHarness):
    def test_mode_and_frozen_flag_always_synced(self):
        """Одна правда «заморожена» живёт в шапке профиля И в frozen_chats.json."""
        for mode, want_frozen in [("frozen", True), ("dead", True), ("normal", False),
                                  ("observer", False), ("quiet", False)]:
            rooms.set_mode("-101", mode, set_by="owner")
            self.assertEqual(rooms.is_frozen("-101"), want_frozen,
                             f"mode={mode}: легаси-флаг разъехался с профилем")

    def test_owner_raise_implies_allowlist(self):
        """Подъём владельцем = допуск: без allowlist комната глуха при любом режиме."""
        for cid, start in [("-201", "frozen"), ("-202", "quiet"), ("-203", "observer")]:
            rooms.set_mode(cid, start, set_by="owner")
            rooms.owner_raise(cid)
            self.assertIn(cid, rooms.allowed_chats(), f"raise из {start} не дал допуск")

    def test_owner_raise_idempotent_allowlist(self):
        rooms.profile_update("-204", mode="normal", mode_set_by="owner")
        rooms.owner_raise("-204")
        rooms.owner_raise("-204")
        import json
        raw = json.loads(rooms.ALLOWLIST.read_text(encoding="utf-8"))
        self.assertEqual(raw.count("-204"), 1, "повторный подъём не плодит дублей")

    def test_self_can_revise_an_owner_set_room_mode(self):
        rooms.set_mode("-205", "frozen", set_by="owner")
        ok, _ = rooms.self_demote("-205", "quiet")
        self.assertTrue(ok, "Praxis may revise a reversible room mode")
        self.assertEqual(rooms.effective_mode("-205"), "quiet")


class TestLoopInvariants(Base):
    def test_states_mutually_exclusive(self):
        """Каждая строка нити — ровно одно из [ ]/[~]/[x]; [~] всегда с валидной датой."""
        people.add_open_loop("vasya", "Вася", "нить про маршрут в горы")
        people.park_loop("vasya", "маршрут", _iso(3))
        people.add_open_loop("vasya", "Вася", "нить про книгу")
        people.close_open_loop("vasya", "книгу")
        _, body = people.read("vasya")
        for l in (body.get(people.LOOPS, "")).splitlines():
            s = l.strip()
            if not s:
                continue
            marks = sum(s.startswith(f"- [{m}]") for m in (" ", "~", "x"))
            self.assertEqual(marks, 1, f"строка не в одном состоянии: {s}")
            if s.startswith("- [~]"):
                m = people.PARKED_UNTIL.search(s)
                self.assertIsNotNone(m, f"[~] без даты: {s}")
                _dt.date.fromisoformat(m.group(1))  # валидная ISO или ValueError

    def test_park_unpark_park_no_duplicates(self):
        people.add_open_loop("vasya", "Вася", "одна и та же нить")
        people.park_loop("vasya", "та же", _iso(2))
        people.unpark_loops("vasya")
        people.park_loop("vasya", "та же", _iso(4))
        _, body = people.read("vasya")
        keys = [people._loop_key(l) for l in (body.get(people.LOOPS, "")).splitlines()
                if l.strip()]
        self.assertEqual(len(keys), len(set(keys)), "roundtrip наплодил дублей")
        self.assertEqual(len(keys), 1)


class DecisionRingHarness(Base):
    def setUp(self):
        super().setUp()
        self._orig.append((heartbeat, "DECISIONS_PATH", heartbeat.DECISIONS_PATH))
        heartbeat.DECISIONS_PATH = self.tmp / "memory" / ".state" / "window_decisions.json"

    def _ring(self):
        return json.loads(heartbeat.DECISIONS_PATH.read_text(encoding="utf-8"))


class TestOpenedTodayCountsLiveWakes(DecisionRingHarness):
    """⚠ Здесь был TestWindowRingInvariants — он проверял record_decision/mark_window,
    то есть контур, который не вызывался в проде ниоткуда (выброшен 25.07). Но он же
    держал ЕДИНСТВЕННУЮ в дереве проверку `opened_today`, а тот считал по кольцу,
    которое никто не писал, — и потому возвращал 0 всегда, отдавая этот ноль ей в промпт
    («автономных окон сегодня») и в пульт. Считаем теперь по живым распискам пульса,
    и покрываем именно это."""

    def _pulse(self, runs):
        import social_pulse
        path = self.tmp / "memory" / ".state" / "social_pulse.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"runs": runs}), encoding="utf-8")
        self._orig.append((social_pulse, "STATE_PATH", social_pulse.STATE_PATH))
        social_pulse.STATE_PATH = path

    def test_counts_todays_pulse_runs(self):
        now = time.time()
        midnight = heartbeat.local_now(now).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        self._pulse([
            {"id": "p1", "started_at": midnight - 3600, "status": "done"},   # вчера
            {"id": "p2", "started_at": midnight + 60, "status": "done"},
            {"id": "p3", "started_at": midnight + 120, "status": "failed"},  # упавшее не в счёт
            {"id": "p4", "started_at": now, "status": "running"},
        ])
        self.assertEqual(heartbeat.opened_today(now), 2,
                         "счётчик обязан видеть реальные пробуждения, а не мёртвое кольцо")

    def test_zero_when_she_has_not_woken(self):
        self._pulse([])
        self.assertEqual(heartbeat.opened_today(), 0)


class UnansweredHarness(Base):
    def setUp(self):
        super().setUp()
        st = self.tmp / "memory" / ".state"
        for k, v in dict(STATE_DIR=st, PATH=st / "unanswered.json").items():
            self._orig.append((unanswered, k, getattr(unanswered, k)))
            setattr(unanswered, k, v)


class TestUnansweredInvariants(UnansweredHarness):
    def test_resolve_idempotent(self):
        unanswered.note_incoming("42", "Евгений")
        unanswered.resolve("42")
        unanswered.resolve("42")  # второй раз — тихий no-op, не исключение
        self.assertEqual(unanswered.entries(now=time.time() + 10 * 3600), [])

    def test_note_incoming_keeps_first_since(self):
        t0 = time.time() - 3600
        unanswered.note_incoming("42", "Евгений", ts=t0)
        unanswered.note_incoming("42", "Евгений", ts=time.time())
        items = unanswered.entries(now=time.time() + 10 * 3600)
        self.assertEqual(len(items), 1)
        self.assertAlmostEqual(items[0]["since"], t0, delta=1.0,
                               msg="since честно держит ПЕРВУЮ неотвеченную")


if __name__ == "__main__":
    unittest.main()
