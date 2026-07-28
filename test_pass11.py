"""
Тесты PASS 11 — устойчивое ядро: жизненный цикл нити (парковка [~]), дедуп целей окон,
фаза «жвачка» сна, STATE-строка нитей. Герметичны (харнесс test_perceive.Base, модель — фейк).

Запуск:  python praxis_test.py test_pass11 -v
"""

from __future__ import annotations

import datetime as _dt
import unittest

from test_perceive import Base, FakeClient, FakeResp  # noqa: F401  (герметичный харнесс)
import heartbeat
import people
import llm  # noqa: F401


def _iso(days_from_today: int = 0) -> str:
    return (_dt.date.today() + _dt.timedelta(days=days_from_today)).isoformat()


# --------------------------------------------------------------------------- #
#  11.0: парковка нити — [~] спит до даты
# --------------------------------------------------------------------------- #

class TestParkLoop(Base):
    def test_park_and_unpark_roundtrip(self):
        people.add_open_loop("vasya", "Вася", "спросить про маршрут")
        self.assertTrue(people.park_loop("vasya", "маршрут", _iso(7)))
        raw = people.read_text("vasya")
        self.assertIn("[~]", raw)
        self.assertIn(f"_(спит до {_iso(7)})_", raw)
        self.assertEqual(people.open_loops("vasya"), [], "спящая нить не открыта")
        self.assertEqual(people.unpark_loops("vasya"), 1)
        raw2 = people.read_text("vasya")
        self.assertIn("- [ ]", raw2)
        self.assertNotIn("спит до", raw2, "метка сна снята")
        self.assertEqual(len(people.open_loops("vasya")), 1)

    def test_park_invalid_until_honest_false(self):
        people.add_open_loop("vasya", "Вася", "нить")
        self.assertFalse(people.park_loop("vasya", "нить", "послезавтра"))
        self.assertFalse(people.park_loop("vasya", "нить", ""))
        self.assertIn("- [ ]", people.read_text("vasya"), "нить не тронута")

    def test_unpark_only_expired(self):
        people.add_open_loop("vasya", "Вася", "живая ещё спит")
        people.add_open_loop("vasya", "Вася", "эта проснулась")
        self.assertTrue(people.park_loop("vasya", "живая ещё", _iso(5)))
        self.assertTrue(people.park_loop("vasya", "проснулась", _iso(-1)))
        self.assertEqual(people.unpark_loops("vasya", only_expired=True), 1)
        self.assertEqual(len(people.open_loops("vasya")), 1)
        self.assertEqual(people.loops_stats()["parked"], 1, "живая парковка не тронута")

    def test_orphan_parked_treated_expired(self):
        nm, body = people.read("vasya")
        body[people.LOOPS] = "- [~] сирота без даты _(2026-01-01)_"
        people.write("vasya", "Вася", body)
        self.assertEqual(people.unpark_loops("vasya", only_expired=True), 1,
                         "[~] без даты — сирота (рукоправка), будим")

    def test_closed_loops_untouched(self):
        people.add_open_loop("vasya", "Вася", "закрою")
        people.close_open_loop("vasya", "закрою")
        self.assertEqual(people.unpark_loops("vasya"), 0)
        self.assertFalse(people.park_loop("vasya", "закрою", _iso(3)), "закрытую не паркуем")
        self.assertIn("[x]", people.read_text("vasya"))

    def test_loops_stats(self):
        st = people.loops_stats()
        self.assertEqual((st["open"], st["parked"]), (0, 0))
        people.add_open_loop("vasya", "Вася", "нить раз")
        people.add_open_loop("petya", "Петя", "нить два")
        people.park_loop("petya", "нить два", _iso(2))
        st = people.loops_stats()
        self.assertEqual((st["open"], st["parked"]), (1, 1))
        self.assertEqual(st["oldest_open_days"], 0)


class TestParkedCandidates(Base):
    def _mk(self, line: str) -> None:
        nm, body = people.read("vasya")
        body[people.LOOPS] = line
        people.write("vasya", "Вася", body)

    def test_parked_alive_skipped(self):
        self._mk(f"- [~] спящая нить о деле _({_iso(-3)})_ _(спит до {_iso(5)})_")
        self.assertEqual(heartbeat.candidates(), [], "живая парковка не кандидат")

    def test_parked_expired_counts_open(self):
        self._mk(f"- [~] проснувшаяся нить о деле _({_iso(-3)})_ _(спит до {_iso(-1)})_")
        cands = heartbeat.candidates()
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["age"], 3, "возраст от исходной даты, не от даты сна")
        self.assertNotIn("спит", cands[0]["text"])
        self.assertIn("[~]", people.read_text("vasya"),
                      "candidates файл не переписывает — метку снимает сон/unpark")


# --------------------------------------------------------------------------- #
#  11.1: тул manage_loop + рамка окна требует решения
# --------------------------------------------------------------------------- #

import agent  # noqa: E402


class TestManageLoop(Base):
    def setUp(self):
        super().setUp()
        people.add_open_loop("vasya", "Вася", "спросить про маршрут")

    def test_close(self):
        out = agent.tool_manage_loop("close", "vasya", match="маршрут")
        self.assertIn("Закрыла", out)
        self.assertIn("[x]", people.read_text("vasya"))

    def test_close_no_match_honest(self):
        out = agent.tool_manage_loop("close", "vasya", match="про другое")
        self.assertIn("Не нашла", out)
        self.assertIn("- [ ]", people.read_text("vasya"))

    def test_park_default_week(self):
        out = agent.tool_manage_loop("park", "vasya", match="маршрут")
        self.assertIn(_iso(7), out, "пустая дата = +7 дней")
        self.assertIn("[~]", people.read_text("vasya"))

    def test_park_explicit_and_reopen(self):
        agent.tool_manage_loop("park", "vasya", match="маршрут", until=_iso(3))
        self.assertEqual(people.open_loops("vasya"), [])
        out = agent.tool_manage_loop("reopen", "vasya")
        self.assertIn("1", out)
        self.assertEqual(len(people.open_loops("vasya")), 1)

    def test_park_bad_date_honest(self):
        out = agent.tool_manage_loop("park", "vasya", match="маршрут", until="скоро")
        self.assertIn("Не запарковалось", out)

    def test_list_shows_states(self):
        people.add_open_loop("vasya", "Вася", "вторая нить о деле")
        agent.tool_manage_loop("park", "vasya", match="вторая", until=_iso(2))
        out = agent.tool_manage_loop("list", "vasya")
        self.assertIn("[ ]", out)
        self.assertIn("[~]", out)

    def test_no_dossier_honest(self):
        out = agent.tool_manage_loop("close", "нет-такого-человека", match="x")
        self.assertIn("Не вижу досье", out)

    def test_owner_tool_and_dispatch_registered(self):
        self.assertIn("manage_loop", [t["name"] for t in agent.OWNER_TOOLS])
        self.assertIn("manage_loop", agent.TOOL_IMPL)

    def test_window_frame_demands_decision(self):
        self.assertIn("manage_loop park", agent._TASK_WINDOW_FRAME)
        self.assertIn("rumination", agent._TASK_WINDOW_FRAME)

    def test_dispatch_through_respond(self):
        from test_rooms_and_admission import ScriptedClient, _tool_use
        steps = [
            _tool_use("t1", "manage_loop",
                      {"action": "park", "person": "vasya", "match": "маршрут"}),
            FakeResp("запарковала, вернусь через неделю"),
        ]
        llm.use_test_client(ScriptedClient(steps))
        try:
            reply = agent.respond("отложи нить про маршрут", [], "Егор",
                                  force_voice=True, is_owner=True, chat_id="7777")
        finally:
            llm.clear_test_clients()
        self.assertIn("запарковала", reply)
        self.assertIn("[~]", people.read_text("vasya"),
                      "manage_loop через respond не запарковал нить")


# --------------------------------------------------------------------------- #
#  11.2: дедуп целей окон — суждение + механический предохранитель
# --------------------------------------------------------------------------- #

class CountingFake:
    def __init__(self, reply="цель дня"):
        self.calls = 0
        outer = self

        class _M:
            def create(_s, **kw):
                outer.calls += 1
                outer.last = kw
                return FakeResp(outer.reply)
        self.reply = reply
        self.messages = _M()


# --------------------------------------------------------------------------- #
#  11.3: сон — фаза «жвачка» + мётла парковок
# --------------------------------------------------------------------------- #

import sleep as sleep_mod


class RuminationBase(Base):
    def setUp(self):
        super().setUp()
        self._orig.append((sleep_mod, "TRASH_DIR", sleep_mod.TRASH_DIR))
        sleep_mod.TRASH_DIR = self.tmp / "memory" / ".trash"

    def _j(self, day_offset: int, lines: list[str]) -> None:
        d = _iso(day_offset)
        p = self.tmp / "memory" / "journal" / f"{d}.md"
        body = "\n".join(f"- 1{i}:00 (s2) {t}" for i, t in enumerate(lines))
        p.write_text(f"# {d}\n\n{body}\n", encoding="utf-8")

    def _text(self, day_offset: int) -> str:
        p = self.tmp / "memory" / "journal" / f"{_iso(day_offset)}.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""


class TestRumination(RuminationBase):
    def test_rumination_preserves_every_journal_row_without_model(self):
        chew = "перформансный слой в промптах оценщика надо зачистить до конца"
        self._j(-2, [chew, "видела красивый закат над рекой"])
        self._j(-1, [chew + " опять"])
        self._j(0, [chew + " снова думаю"])
        before = {offset: self._text(offset) for offset in (-2, -1, 0)}
        fc = CountingFake()
        llm.use_test_client(fc)
        groups, removed = sleep_mod.rumination_pass()
        self.assertEqual((groups, removed), (0, 0))
        self.assertEqual(fc.calls, 0)
        self.assertEqual({offset: self._text(offset) for offset in (-2, -1, 0)}, before)
        self.assertEqual(list((self.tmp / "memory" / ".trash").rglob("rumination.md")), [])

    def test_no_group_no_model_calls(self):
        self._j(0, ["одна мысль", "совсем другая тема", "третье о погоде"])
        fc = CountingFake()
        llm.use_test_client(fc)
        self.assertEqual(sleep_mod.rumination_pass(), (0, 0))
        self.assertEqual(fc.calls, 0, "нет групп — ноль токенов")

    def test_two_similar_not_a_group(self):
        chew = "мысль о навыке сна и его отчёте"
        self._j(-1, [chew])
        self._j(0, [chew + " ещё раз"])
        self.assertEqual(sleep_mod.rumination_groups(
            sleep_mod._journal_entries(14)), [], "два повтора — ещё не жвачка")

    def test_sleep_reports_excluded(self):
        rep = "[сон] сон: слито 0, гипотез 1, подрезано 0"
        self._j(-2, [rep])
        self._j(-1, [rep])
        self._j(0, [rep])
        self.assertEqual(sleep_mod._journal_entries(14), [],
                         "служебные записи сна не жвачка — иначе слипнутся его же отчёты")

    def test_legacy_cap_does_not_reenable_rumination(self):
        for k, base_text in enumerate(["первая тема про код окна и капы",
                                       "вторая тема про панель и плитки мозга"]):
            for off in (-2, -1, 0):
                p = self.tmp / "memory" / "journal" / f"{_iso(off)}.md"
                cur = p.read_text(encoding="utf-8") if p.exists() else f"# {_iso(off)}\n\n"
                p.write_text(cur + f"- 0{k}:0{k} (s2) {base_text} вариант {off}\n",
                             encoding="utf-8")
        self._orig.append((sleep_mod, "RUMINATION_CAP", sleep_mod.RUMINATION_CAP))
        sleep_mod.RUMINATION_CAP = 1
        fc = CountingFake()
        llm.use_test_client(fc)
        groups, _ = sleep_mod.rumination_pass()
        self.assertEqual(groups, 0)
        self.assertEqual(fc.calls, 0)

    def test_unpark_expired_loops_broom(self):
        people.add_open_loop("vasya", "Вася", "спящая с истёкшим сроком")
        people.park_loop("vasya", "истёкшим", _iso(-1))
        people.add_open_loop("petya", "Петя", "спящая ещё живая")
        people.park_loop("petya", "живая", _iso(5))
        self.assertEqual(sleep_mod.unpark_expired_loops(), 1)
        self.assertEqual(len(people.open_loops("vasya")), 1)
        self.assertEqual(people.open_loops("petya"), [])


# --------------------------------------------------------------------------- #
#  11.4: скилл thread_discipline + STATE-строка нитей
# --------------------------------------------------------------------------- #

from pathlib import Path as _Path


class TestThreadDisciplineSkill(unittest.TestCase):
    REPO = _Path(__file__).resolve().parent

    def test_skill_exists_and_indexed(self):
        skill = self.REPO / "soul" / "skills" / "thread_discipline.md"
        self.assertTrue(skill.exists(), "скилл дисциплины нитей в репо")
        body = skill.read_text(encoding="utf-8")
        self.assertIn("manage_loop", body)
        self.assertIn("руминация", body.lower())
        self.assertIn("машинный veto", body.lower())
        idx = (self.REPO / "soul" / "skills" / "INDEX.md").read_text(encoding="utf-8")
        self.assertIn("thread_discipline", idx, "скилл виден в INDEX")


class TestLoopsState(Base):
    def test_state_line_present(self):
        people.add_open_loop("vasya", "Вася", "нить о маршруте")
        people.add_open_loop("vasya", "Вася", "вторая нить о деле")
        people.park_loop("vasya", "вторая", _iso(4))
        st = agent.build_state_block()
        self.assertIn('"fact":"loops","open":1,"parked":1,"oldest_open_days":0', st)

    def test_state_line_absent_when_empty(self):
        self.assertIn('"fact":"loops","open":0,"parked":0', agent.build_state_block())


if __name__ == "__main__":
    unittest.main()
