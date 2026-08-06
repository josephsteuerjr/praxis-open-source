"""04.08 — рука без потолка и качели прогона: две поломки одного хода.

Ход `de0781f6` отвечал человеку в комнате и провёл четыре с половиной часа в петле.
Причин было две, и они независимы:

1. `fs_search("10 млн", glob="**/*", root="/app/memory")` не вернулся. Пробел в паттерне
   выключил быстрый бинарный путь (с Python 3.7 `re.escape` экранирует пробел, поэтому
   проверка «литерал == re.escape(литерал)» ложна для любого запроса из двух слов), а
   питоновский запасной путь материализовал и отсортировал ВСЁ дерево памяти. Ни потолка
   обойдённых путей, ни потолка времени там не было — только потолок совпадений.
2. `RunManager.recover()` видел статус `running` и уводил в `paused`; резюмер возвращал
   обратно. 230 оборотов, 460 из 479 событий хода. Ни счётчика, ни эскалации, ни жалобы.

Запуск:  python praxis_test.py test_stuck_tool_and_flap -v
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import workshop
from run_context import RunContext
from run_manager import InvalidTransition, RunManager


# --------------------------------------------------------------- рука без потолка


class SearchBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_search_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._orig = [(workshop, k, getattr(workshop, k)) for k in ("BASE", "REPO")]
        self.addCleanup(lambda: [setattr(m, k, v) for m, k, v in self._orig])
        workshop.BASE = self.tmp
        workshop.REPO = self.tmp

    def make_tree(self, files: int, needle: str = "иголка") -> None:
        for index in range(files):
            folder = self.tmp / f"dir{index % 7}"
            folder.mkdir(exist_ok=True)
            (folder / f"f{index}.txt").write_text(
                f"строка раз\nвот {needle} номер {index}\nстрока три\n", encoding="utf-8")


class TheLiteralTestSurvivesASpace(SearchBase):
    """Запрос из двух слов — литерал, а не регулярка."""

    def test_a_space_no_longer_disqualifies_the_bounded_binary_path(self):
        seen = {}

        def fake_search(pattern, glob="", root="", cap=0, base=None):
            seen["pattern"] = pattern
            return {"ok": True, "hits": ["a.txt:1: 10 млн"], "files_seen": 1, "capped": False}

        with mock.patch.object(workshop.hands, "search", fake_search):
            out = workshop.fs_search("10 млн", glob="**/*")
        self.assertEqual(seen.get("pattern"), "10 млн",
                         "пробел снова выкинул запрос из быстрого пути в необрезанный обход")
        self.assertIn("10 млн", out)

    def test_real_metacharacters_still_take_the_python_path(self):
        called = []

        def fake_search(*_a, **_kw):
            called.append(1)
            return {"ok": True, "hits": [], "files_seen": 0, "capped": False}

        self.make_tree(3, needle="альфа")
        with mock.patch.object(workshop.hands, "search", fake_search):
            out = workshop.fs_search(r"аль\w+", glob="**/*.txt")
        self.assertFalse(called, "регулярка ушла в литеральный путь")
        self.assertIn("альфа", out)

    def test_a_leading_dash_is_still_refused_by_the_fast_path(self):
        called = []
        with mock.patch.object(workshop.hands, "search", lambda *a, **k: called.append(1)):
            workshop.fs_search("-rf", glob="**/*.txt")
        self.assertFalse(called)


class TheWalkHasCeilings(SearchBase):
    def test_the_scan_cap_stops_the_walk_and_says_so(self):
        self.make_tree(60)
        with mock.patch.object(workshop, "SEARCH_SCAN_CAP", 12), \
             mock.patch.object(workshop, "SEARCH_SECONDS", 0.0):
            out = workshop.fs_search(r"игол\w+", glob="**/*.txt")
        self.assertIn("обошла 12 путей", out, "обход оборвался молча")
        self.assertIn("путей обошла", out)

    def test_the_clock_stops_the_walk_and_says_so(self):
        # Файлы БЕЗ искомого: иначе потолок совпадений оборвал бы обход раньше часов,
        # и тест проверял бы не то, что назван проверять.
        self.make_tree(900, needle="сено")
        ticks = iter([0.0] + [999.0] * 4000)
        with mock.patch.object(workshop, "SEARCH_SECONDS", 5.0), \
             mock.patch.object(workshop, "SEARCH_SCAN_CAP", 0), \
             mock.patch.object(workshop.time, "monotonic", lambda: next(ticks)):
            out = workshop.fs_search(r"игол\w+", glob="**/*.txt")
        self.assertIn("искала 5с", out, "часы не остановили обход")
        self.assertIn("Ничего не нашла", out)

    def test_an_interrupted_walk_never_reports_nothing_found(self):
        """Самая опасная ложь: «ничего не нашла» после оборванного обхода."""
        self.make_tree(60, needle="альфа")
        with mock.patch.object(workshop, "SEARCH_SCAN_CAP", 3), \
             mock.patch.object(workshop, "SEARCH_SECONDS", 0.0):
            out = workshop.fs_search(r"бет\w+", glob="**/*.txt")
        self.assertIn("Ничего не нашла", out)
        self.assertIn("обошла 3 путей", out,
                      "она решит, что искомого нет, а мы просто не дошли")

    def test_zero_removes_the_ceiling_completely(self):
        self.make_tree(20)
        with mock.patch.object(workshop, "SEARCH_SCAN_CAP", 0), \
             mock.patch.object(workshop, "SEARCH_SECONDS", 0.0):
            out = workshop.fs_search(r"игол\w+", glob="**/*.txt")
        self.assertNotIn("остановилась", out, "ноль обязан снимать предел, а не ставить свой")

    def test_the_whole_tree_is_never_materialised(self):
        """Прежде здесь стоял sorted(base.glob(...)): всё дерево ДО первого чтения."""
        self.make_tree(30)
        real_glob = Path.glob
        materialised = []

        def spy(self_path, pattern):
            gen = real_glob(self_path, pattern)
            materialised.append(gen)
            return gen

        with mock.patch.object(Path, "glob", spy), \
             mock.patch.object(workshop, "SEARCH_SCAN_CAP", 5), \
             mock.patch.object(workshop, "SEARCH_SECONDS", 0.0):
            workshop.fs_search(r"игол\w+", glob="**/*.txt")
        self.assertTrue(materialised)
        for gen in materialised:
            self.assertFalse(isinstance(gen, (list, tuple)),
                             "обход снова материализует дерево целиком")


class TheOrderOfHitsStaysStable(SearchBase):
    def test_hits_are_sorted_by_path_and_then_by_line_number(self):
        (self.tmp / "b.txt").write_text("\n".join(["игла"] * 12), encoding="utf-8")
        (self.tmp / "a.txt").write_text("нет\nигла\n", encoding="utf-8")
        with mock.patch.object(workshop, "SEARCH_SCAN_CAP", 0), \
             mock.patch.object(workshop, "SEARCH_SECONDS", 0.0):
            out = workshop.fs_search(r"игл\w", glob="**/*.txt")
        rows = [line for line in out.splitlines() if ".txt:" in line]
        self.assertTrue(rows[0].startswith("a.txt:"), "порядок выдачи поехал")
        numbers = [int(r.split(":")[1]) for r in rows if r.startswith("b.txt:")]
        self.assertEqual(numbers, sorted(numbers), "строки внутри файла не по порядку")


# ------------------------------------------------------------------- качели прогона


class FlapBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.manager = RunManager(Path(self._tmp.name))

    def create(self, suffix: str = "flap") -> RunContext:
        ctx = RunContext.create(
            run_id=f"run-{suffix}", kind="chat_turn", goal="ответить человеку",
            principal_id="telegram:1", scope="group", origin_chat_id="-100",
            origin_message_ids=[1], delivery_chat_id="-100",
            model_profile="voice/test", forge_task_id="",
        )
        return self.manager.create(ctx, "# Context\n")

    def hang_a_read_only_call(self, run_id: str) -> None:
        """Ровно тот вызов, что завис: только чтение, идемпотентный, без результата."""
        self.manager.start_tool(run_id, "call-stuck", "fs_search",
                                {"glob": "**/*", "pattern": "10 млн"},
                                side_effect=False, idempotency_key="fs-search-1")

    def spin(self, run_id: str, cycles: int) -> None:
        for _ in range(cycles):
            self.manager.transition(run_id, "paused", expected="running",
                                    reason="сон уборщика")
            self.manager.resume(run_id, actor="resumer", reason="подъём резюмером")


class OneRestartIsStillJustAPause(FlapBase):
    def test_a_single_recovery_pauses_as_before(self):
        run = self.create("once")
        self.manager.transition(run.run_id, "running", expected="pending")
        self.hang_a_read_only_call(run.run_id)
        reports = [r for r in self.manager.recover() if r.get("run_id") == run.run_id]
        self.assertEqual(reports[0]["to"], "paused")
        self.assertNotIn("flapping", reports[0])
        self.assertEqual(self.manager.manifest(run.run_id)["status"], "paused")


class TheFlapEndsInsteadOfSpinningForever(FlapBase):
    def test_a_run_that_only_flips_status_is_escalated(self):
        run = self.create("spin")
        self.manager.transition(run.run_id, "running", expected="pending")
        self.hang_a_read_only_call(run.run_id)
        self.spin(run.run_id, 4)  # восемь смен статуса подряд, ни одного события работы

        reports = [r for r in self.manager.recover() if r.get("run_id") == run.run_id]
        self.assertEqual(reports[0]["to"], "in_doubt")
        self.assertGreaterEqual(reports[0]["flapping"], 6)
        self.assertEqual(reports[0]["outstanding_call_ids"], ["call-stuck"],
                         "жалоба не называет вызов, из-за которого всё встало")

    def test_the_escalated_run_cannot_be_flipped_back(self):
        """Смысл именно `in_doubt`: из него нет обычной дороги в running."""
        run = self.create("stop")
        self.manager.transition(run.run_id, "running", expected="pending")
        self.hang_a_read_only_call(run.run_id)
        self.spin(run.run_id, 4)
        self.manager.recover()

        with self.assertRaises(InvalidTransition):
            self.manager.transition(run.run_id, "running", expected="in_doubt")
        with self.assertRaises(InvalidTransition):
            self.manager.resume(run.run_id, actor="resumer")
        self.assertEqual(self.manager.manifest(run.run_id)["status"], "in_doubt")

    def test_a_run_doing_real_work_between_flips_is_not_escalated(self):
        """Смены статуса вперемешку с работой — не качели."""
        run = self.create("busy")
        self.manager.transition(run.run_id, "running", expected="pending")
        self.hang_a_read_only_call(run.run_id)
        for index in range(4):
            self.manager.transition(run.run_id, "paused", expected="running",
                                    reason="пауза")
            self.manager.resume(run.run_id, actor="resumer")
            self.manager.store_result(run.run_id, f"работа {index}",
                                      call_id=f"call-work-{index}", name="shell")
        reports = [r for r in self.manager.recover() if r.get("run_id") == run.run_id]
        self.assertEqual(reports[0]["to"], "paused")
        self.assertNotIn("flapping", reports[0])

    def test_a_flapping_run_without_outstanding_calls_is_left_alone(self):
        """Без висящего вызова ход терминализуем: это другая поломка, и гадать не наше дело."""
        run = self.create("clean")
        self.manager.transition(run.run_id, "running", expected="pending")
        self.spin(run.run_id, 4)
        reports = [r for r in self.manager.recover() if r.get("run_id") == run.run_id]
        self.assertEqual(reports[0]["to"], "paused")

    def test_an_uncertain_call_still_wins_over_the_flap_reason(self):
        run = self.create("uncertain")
        self.manager.transition(run.run_id, "running", expected="pending")
        self.manager.start_tool(run.run_id, "call-send", "send_message", {"text": "x"},
                                side_effect=True)
        self.spin(run.run_id, 4)
        reports = [r for r in self.manager.recover() if r.get("run_id") == run.run_id]
        self.assertEqual(reports[0]["to"], "in_doubt")
        self.assertNotIn("flapping", reports[0],
                         "неопределённый побочный эффект — своя причина, не качели")


if __name__ == "__main__":
    unittest.main()
