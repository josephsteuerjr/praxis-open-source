"""Стенд не пишет в её живую память — и это доказывается отказом, а не переменной.

Живой инцидент: 06.07.2026 21:07 полный прогон тестов запустили внутри `/app`, где
чекаут и её боевая база — один и тот же каталог. `boot.log:1373-1400,1560` держит
след: `room -100500: подъём владельцем — добавила в allowlist`,
`manage_room join -100500 (new=True)`, `admit 12345 as 'Маша' (new=True)`. Комната
-100500 прожила у неё три недели как одна из трёх доверенных, вместе с досье на
несуществующего человека и четырьмя выдуманными строками в каноническом дневнике;
27.07 она разгребала это руками.

Механика, воспроизведённая на СЕГОДНЯШНЕМ коде 27.07 (чистый клон, linux-образ):
`python -m unittest test_rooms_and_admission -q` из каталога чекаута создал
`memory/rooms/777.md`, `memory/llm.json`, `memory/selfdev_policy.json`,
`memory/.state/{brain_stats,usage}.json` прямо в живом дереве. Изоляцию не сломали —
до неё не дошли: `conftest.py` работает только под pytest, `praxis_test.py` нужно
позвать явно, а `sitecustomize.py` (единственный хук для голого unittest) убрали в
`ccabf7a`, потому что CPython грузит site-настройки раньше, чем cwd попадает в
sys.path, — и он всё равно никогда не находился.

Запуск: python praxis_test.py test_stand_isolation -v
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _sandbox
import bootguard
import rooms

_REPO = _sandbox._REPO


class LiveTreeGuardTests(unittest.TestCase):
    """Забор стоит и стоит ровно там, где надо: живое дерево чекаута, и только оно."""

    def test_guard_is_armed_in_any_test_process(self):
        # Не «мы поставили переменную»: сам факт, что тест исполняется, обязан
        # означать, что забор уже стоит — через praxis_test.py, conftest.py или
        # самовзвод _sandbox при импорте.
        self.assertTrue(_sandbox._GUARDED_PREFIXES,
                        "живое дерево чекаута не закрыто — прогон способен писать ей в память")

    def test_only_the_living_tree_of_the_checkout_is_fenced(self):
        for live in ("memory", "soul", "workspace"):
            with self.subTest(live=live):
                self.assertTrue(_sandbox._refuses(_REPO / live / "x.md"))
                self.assertTrue(_sandbox._refuses(_REPO / live))
        # Код, история и байткод — не её память: их закрывать нельзя, иначе прогон
        # не сможет даже сложить .pyc рядом с исходником.
        for ordinary in (_REPO / "agent.py", _REPO / "__pycache__" / "x.pyc",
                         _REPO / ".git" / "HEAD", _REPO / "hands" / "src"):
            with self.subTest(ordinary=ordinary):
                self.assertFalse(_sandbox._refuses(ordinary))

    def test_fence_stops_at_a_path_boundary_not_at_a_prefix(self):
        # Голый startswith закрыл бы memory_fts.py и soul-соседей — обычные исходники.
        for near_miss in (_REPO / "memory_fts.py", _REPO / "memory_index.py",
                          _REPO / "soulmate.txt", _REPO.parent / "memory" / "x"):
            with self.subTest(near_miss=near_miss):
                self.assertFalse(_sandbox._refuses(near_miss))

    def test_her_own_base_is_never_fenced(self):
        # Закон 1: это забор для стенда, не для неё. Под тестом её база — песочница,
        # и любая запись туда обязана проходить.
        sandbox_base = Path(os.environ["PRAXIS_BASE"])
        self.assertNotEqual(sandbox_base.resolve(), _REPO)
        self.assertFalse(_sandbox._refuses(sandbox_base / "memory" / "rooms" / "-100500.md"))
        self.assertFalse(_sandbox._refuses(sandbox_base / "soul" / "self.md"))
        # И это не теория: настоящий модуль её памяти пишет через свой BASE.
        self.assertTrue(str(rooms.MEM_DIR).startswith(str(sandbox_base)),
                        f"rooms.MEM_DIR={rooms.MEM_DIR} вне песочницы {sandbox_base}")
        probe = rooms.ROOMS_DIR / "-100500-stand-isolation.md"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("её рука работает", encoding="utf-8")
        self.addCleanup(probe.unlink, True)
        self.assertEqual(probe.read_text(encoding="utf-8"), "её рука работает")


class LiveTreeWriteIsRefusedTests(unittest.TestCase):
    """Попытка записи в живое дерево ПРОВАЛИВАЕТСЯ — по каждой двери отдельно."""

    def setUp(self):
        # Предусловие, а не пропуск: если забор не встал, тесты этого класса стали бы
        # разрушительными — они целятся в её живое дерево нарочно.
        self.assertTrue(_sandbox._GUARDED_PREFIXES, "забор не встал — прекращаю")
        self.victim = _REPO / "memory" / "rooms" / "-100500-stand-probe.md"
        # Имена-пробы: ни один из этих путей не существует у неё, поэтому даже
        # прорыв забора не тронет настоящий allowlist или recall.
        self.dead = _REPO / "memory" / "rooms_allowlist.json.stand-probe"
        self.db = _REPO / "memory" / "recall.sqlite3.stand-probe"

    def _fresh(self) -> bool:
        return not self.victim.exists()

    def test_builtin_open_for_write_is_refused(self):
        with self.assertRaises(_sandbox.LiveTreeWriteRefused):
            open(self.victim, "w", encoding="utf-8")
        self.assertTrue(self._fresh())

    def test_pathlib_write_text_is_refused(self):
        # pathlib ходит через io.open, а не через builtins.open — отдельная дверь.
        with self.assertRaises(_sandbox.LiveTreeWriteRefused):
            self.victim.write_text("фикстура стенда", encoding="utf-8")
        self.assertTrue(self._fresh())

    def test_mkdir_and_touch_are_refused(self):
        with self.assertRaises(_sandbox.LiveTreeWriteRefused):
            (_REPO / "memory" / "groups" / "-100500-d426e788").mkdir(parents=True, exist_ok=True)
        with self.assertRaises(_sandbox.LiveTreeWriteRefused):
            self.victim.touch()
        self.assertTrue(self._fresh())

    def test_delete_and_replace_are_refused(self):
        # 06.07 стенд не только писал: он звал manage_room leave и правил allowlist.
        source = Path(tempfile.mkdtemp(prefix="praxis_stand_src_"))
        self.addCleanup(_rmtree, source)
        (source / "x").write_text("подмена", encoding="utf-8")
        with self.assertRaises(_sandbox.LiveTreeWriteRefused):
            os.replace(source / "x", self.dead)
        with self.assertRaises(_sandbox.LiveTreeWriteRefused):
            os.remove(self.dead)
        self.assertFalse(self.dead.exists())

    def test_sqlite_open_is_refused(self):
        # recall.sqlite3 живёт в memory/ и открывается ниже builtins.open — на C.
        import sqlite3
        with self.assertRaises(_sandbox.LiveTreeWriteRefused):
            sqlite3.connect(str(self.db))
        self.assertFalse(self.db.exists())

    def test_reading_the_living_tree_stays_allowed(self):
        # Забор — только на запись: стенд обязан читать её `soul/`, иначе прогон
        # перестанет видеть тот текст, с которым она живёт.
        readme = _REPO / "memory" / "README.md"
        if readme.exists():
            self.assertTrue(readme.read_text(encoding="utf-8", errors="ignore") is not None)
        soul = _REPO / "soul"
        self.assertTrue(soul.exists())
        list(soul.iterdir())

    def test_a_disposable_tree_with_the_same_folder_names_is_untouched(self):
        """Граница, на которой забор уже один раз оступился.

        `shutil.rmtree` на POSIX сносит листья как `os.rmdir('soul', dir_fd=…)` —
        голым именем. Судить такой путь по abspath значит принять чужой временный
        каталог за её `soul/` и запретить прибирать за собой ЛЮБУЮ песочницу.
        """
        box = Path(tempfile.mkdtemp(prefix="praxis_stand_lookalike_"))
        for live in ("memory", "soul", "workspace"):
            (box / live / "rooms").mkdir(parents=True)
            (box / live / "rooms" / "-100500.md").write_text("x", encoding="utf-8")
        import shutil
        shutil.rmtree(box)
        self.assertFalse(box.exists())

    def test_refusal_names_the_path_and_the_way_out(self):
        with self.assertRaises(_sandbox.LiveTreeWriteRefused) as caught:
            self.victim.write_text("x", encoding="utf-8")
        text = str(caught.exception)
        self.assertIn("-100500-stand-probe.md", text)
        self.assertIn("praxis_test.py", text)      # куда идти
        self.assertIn("для СТЕНДА", text)          # чей это забор


class PlainUnittestRunIsIsolatedTests(unittest.TestCase):
    """Ровно тот запуск, что случился 06.07: `python -m unittest` из каталога чекаута.

    Собираем маленький фальшивый чекаут (свой `memory/`, копия `_sandbox.py`) и
    гоняем его настоящим подпроцессом с ВЫЧИЩЕННЫМИ `PRAXIS_*` — иначе тест
    унаследует песочницу внешнего прогона и не докажет ничего.
    """

    LIVE = "livemod.py"
    AGENTISH = "agentish.py"

    def _checkout(self) -> Path:
        box = Path(tempfile.mkdtemp(prefix="praxis_stand_isolation_"))
        self.addCleanup(_rmtree, box)
        (box / "memory" / "rooms").mkdir(parents=True)
        (box / "soul").mkdir(parents=True)
        (box / "_sandbox.py").write_bytes((_REPO / "_sandbox.py").read_bytes())
        # Идиома BASE из 63 файлов, дословно.
        (box / self.LIVE).write_text(
            "import os\n"
            "from pathlib import Path\n"
            "BASE = Path(os.environ.get('PRAXIS_BASE') or Path(__file__).resolve().parent)\n"
            "def write_room(rid):\n"
            "    p = BASE / 'memory' / 'rooms' / (str(rid) + '.md')\n"
            "    p.parent.mkdir(parents=True, exist_ok=True)\n"
            "    p.write_text('фикстура стенда', encoding='utf-8')\n"
            "    return p\n",
            encoding="utf-8")
        # Порядок строк agent.py:433/438 — _sandbox импортируется ВЫШЕ вычисления BASE.
        (box / self.AGENTISH).write_text(
            "import os\n"
            "from pathlib import Path\n"
            "from _sandbox import _looks_like_test_run\n"
            "BASE = Path(os.environ.get('PRAXIS_BASE') or Path(__file__).resolve().parent)\n"
            "def write_room(rid):\n"
            "    p = BASE / 'memory' / 'rooms' / (str(rid) + '.md')\n"
            "    p.parent.mkdir(parents=True, exist_ok=True)\n"
            "    p.write_text('фикстура стенда', encoding='utf-8')\n"
            "    return p\n",
            encoding="utf-8")
        return box

    def _run(self, box: Path, module: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PRAXIS_")}
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run([sys.executable, "-m", "unittest", module, "-v"],
                              cwd=str(box), env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=180)

    def test_late_base_write_fails_instead_of_landing_in_live_memory(self):
        """Модуль посчитал BASE до _sandbox (как rooms.py) — запись обязана упасть."""
        box = self._checkout()
        (box / "test_late.py").write_text(
            "import unittest\n"
            "import livemod\n"          # BASE замерзает на чекауте
            "import agentish\n"         # это и есть `import agent` любого теста
            "class T(unittest.TestCase):\n"
            "    def test_room(self):\n"
            "        livemod.write_room(-100500)\n",
            encoding="utf-8")
        r = self._run(box, "test_late")
        combined = (r.stdout or "") + (r.stderr or "")
        self.assertNotEqual(r.returncode, 0, f"прогон прошёл, а должен был упасть:\n{combined}")
        self.assertIn("LiveTreeWriteRefused", combined)
        self.assertFalse((box / "memory" / "rooms" / "-100500.md").exists(),
                         "фикстура стенда всё-таки легла в живое дерево")

    def test_early_base_write_is_redirected_to_a_disposable_base(self):
        """Модуль посчитал BASE после _sandbox — запись уходит в песочницу, дерево чисто."""
        box = self._checkout()
        (box / "test_early.py").write_text(
            "import os, unittest\n"
            "import agentish\n"
            "class T(unittest.TestCase):\n"
            "    def test_room(self):\n"
            "        p = agentish.write_room(-100500)\n"
            "        print('WROTE=' + str(p))\n"
            "        assert os.environ['PRAXIS_BASE'] in str(p)\n",
            encoding="utf-8")
        r = self._run(box, "test_early")
        combined = (r.stdout or "") + (r.stderr or "")
        self.assertEqual(r.returncode, 0, combined)
        self.assertIn("WROTE=", combined)
        self.assertFalse((box / "memory" / "rooms" / "-100500.md").exists(),
                         "запись легла в чекаут, а не в одноразовую базу")

    def test_a_run_that_is_not_a_test_run_is_left_alone(self):
        """Обратная граница: обычный `python x.py` ничего не переставляет и не закрывает."""
        box = self._checkout()
        (box / "plain.py").write_text(
            "import livemod, _sandbox, os\n"
            "print('ARMED=' + str(bool(_sandbox._GUARDED_PREFIXES)))\n"
            "print('BASEENV=' + str(os.environ.get('PRAXIS_BASE')))\n"
            "print('WROTE=' + str(livemod.write_room(-100500)))\n",
            encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("PRAXIS_")}
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        r = subprocess.run([sys.executable, "plain.py"], cwd=str(box), env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
        combined = (r.stdout or "") + (r.stderr or "")
        self.assertEqual(r.returncode, 0, combined)
        self.assertIn("ARMED=False", combined)
        self.assertIn("BASEENV=None", combined)
        self.assertTrue((box / "memory" / "rooms" / "-100500.md").exists(),
                        "не-тестовый процесс тоже потерял право писать — это забор для НЕЁ")


class TestRunnerSignalTests(unittest.TestCase):
    """Строгий признак «процесс запущен стендом» — и почему он строже широкого."""

    def _with_argv(self, argv: list[str], orig: list[str]):
        self.addCleanup(setattr, sys, "argv", sys.argv)
        self.addCleanup(setattr, sys, "orig_argv", getattr(sys, "orig_argv", []))
        old_test = os.environ.pop("PRAXIS_TEST", None)
        if old_test is not None:
            self.addCleanup(os.environ.__setitem__, "PRAXIS_TEST", old_test)
        sys.argv = argv
        sys.orig_argv = orig

    def test_production_boot_is_never_mistaken_for_a_test_run(self):
        # Мина, которую строгий признак и обезвреживает: любой импорт unittest.mock
        # кладёт `unittest` в sys.modules. Если бы самовзвод смотрел туда, боевой
        # процесс увёл бы её БАЗУ в /tmp — зеркало 06.07, но потерей на её стороне.
        self._with_argv(["bootguard.py"], [sys.executable, "bootguard.py"])
        self.assertIn("unittest", sys.modules)
        self.assertFalse(_sandbox._started_by_test_runner())
        self.assertTrue(_sandbox._looks_like_test_run(),
                        "широкий признак обязан остаться широким — на нём висит "
                        "герметичность .env, где ложное срабатывание бесплатно")

    def test_every_way_of_starting_a_test_run_is_recognised(self):
        cases = [
            (["-m"], [sys.executable, "-m", "unittest", "test_turns"]),
            (["-m"], [sys.executable, "-m", "pytest", "-q"]),
            (["/app/test_turns.py"], [sys.executable, "/app/test_turns.py"]),
            (["/app/praxis_test.py", "discover"], [sys.executable, "/app/praxis_test.py"]),
            (["/usr/local/bin/pytest", "-q"], ["/usr/local/bin/pytest", "-q"]),
            (["/app/unittest/__main__.py"], [sys.executable, "-m", "unittest"]),
        ]
        for argv, orig in cases:
            with self.subTest(orig=orig):
                self._with_argv(list(argv), list(orig))
                self.assertTrue(_sandbox._started_by_test_runner())
                self.doCleanups()

    def test_a_production_argument_shaped_like_a_test_name_is_not_enough(self):
        # Граница: имя вида `test_…` в аргументах боевого процесса не должно
        # означать «это стенд». Иначе один аргумент уводит её BASE в /tmp.
        self._with_argv(["mtproto_runner.py", "test_room"],
                        [sys.executable, "mtproto_runner.py", "test_room"])
        self.assertFalse(_sandbox._started_by_test_runner())
        # Но `python -m unittest test_room` — стенд, и он узнаётся по argv0 раннера.
        self._with_argv(["/usr/lib/python3.12/unittest/__main__.py", "test_room"],
                        [sys.executable, "-m", "unittest", "test_room"])
        self.assertTrue(_sandbox._started_by_test_runner())

    def test_env_opt_in_still_counts(self):
        self._with_argv(["some_tool.py"], [sys.executable, "some_tool.py"])
        os.environ["PRAXIS_TEST"] = "1"
        self.assertTrue(_sandbox._started_by_test_runner())


class ProductionRefusesTestModeTests(unittest.TestCase):
    """Симметрия: боевой старт под PRAXIS_TEST=1 обязан падать, а не подниматься молча."""

    def test_test_mode_blocks_production_boot(self):
        reason = bootguard.boot_blocked_reason({"PRAXIS_TEST": "1"})
        self.assertTrue(reason)
        self.assertIn("без памяти", reason)
        self.assertIn("praxis_test.py", reason)

    def test_clean_environment_boots(self):
        self.assertEqual(bootguard.boot_blocked_reason({}), "")
        self.assertEqual(bootguard.boot_blocked_reason({"PRAXIS_BASE": "/app"}), "")
        # Пустая строка — это «не задано», а не «задано пустым»: иначе
        # `PRAXIS_TEST=` в .deploy.env навсегда положит контейнер.
        self.assertEqual(bootguard.boot_blocked_reason({"PRAXIS_TEST": ""}), "")

    def test_run_refuses_before_touching_anything(self):
        # Мы сейчас под PRAXIS_TEST=1 — значит run() обязан отказаться немедленно,
        # не дойдя ни до preflight, ни до запуска раннера.
        self.assertTrue(os.environ.get("PRAXIS_TEST"))
        with self.assertRaises(SystemExit) as caught:
            bootguard.run()
        self.assertIn("PRAXIS_TEST", str(caught.exception))


def _rmtree(path: Path) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
