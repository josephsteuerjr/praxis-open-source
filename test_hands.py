"""
Тесты PASS 16.3/16.5 — компилируемый пол рук (`hands/`) и эргономика мастерской.

Главная мысль пасса: рельсы живут ВНЕ самоизменяемого питона. Значит проверять надо три
вещи, и все три здесь:
  1. рельсы бинаря СГЕНЕРИРОВАНЫ из питон-констант и не разъехались (gen_rails --check),
     а табличное правило секретности эквивалентно питоновому regex (корпус имён);
  2. семантика ОДНА: питон-фолбэк и бинарь дают один ответ на одних входах
     (тесты гоняются дважды — с PRAXIS_HANDS=off и как есть; если бинаря нет, второй
     прогон вырождается в первый, и это честно сообщается);
  3. мост никогда не роняет ход: нет бинаря / мусор на stdout / таймаут → None → питон.

Запуск:  python praxis_test.py test_hands -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "hands"))

import gen_rails  # noqa: E402  (генератор рельсов — он же источник табличного правила)

import hands  # noqa: E402
import workshop  # noqa: E402


RAILS_RS = Path(__file__).resolve().parent / "hands" / "src" / "rails.rs"


# --------------------------------------------------------------------------- #
#  1. Рельсы не разъезжаются с питоном
# --------------------------------------------------------------------------- #

class TestRailsAreGenerated(unittest.TestCase):
    def test_rails_rs_is_fresh(self):
        self.assertTrue(RAILS_RS.exists(), "hands/src/rails.rs должен быть в репо")
        self.assertEqual(RAILS_RS.read_text(encoding="utf-8"), gen_rails.render(),
                         "rails.rs устарел — python hands/gen_rails.py")

    def test_filenames_are_not_a_secret_boundary(self):
        """Compatibility helpers agree that filenames never create a boundary."""
        corpus = [
            ".env", ".env.local", ".ENV", "x.deploy.env", "praxis.session",
            "praxis.session-journal", "llm.json", "LLM.JSON", "agent.py",
            "environment.md", "sessions.md", "session_notes.txt", "my.env.backup",
            "readme.env.md", "deploy.env", "llm.json.bak", "workspace/.env",
        ]
        for name in corpus:
            base = name.rsplit("/", 1)[-1]
            self.assertEqual(
                bool(workshop._SECRET_NAME.search(base)), gen_rails.is_secret_name(base),
                f"расхождение правил секретности на «{base}»")

    def test_floor_and_zones_come_from_python(self):
        import selfdev
        text = RAILS_RS.read_text(encoding="utf-8")
        for pat in selfdev.PROTECTED_PATTERNS:
            self.assertIn(f'"{pat}"', text, f"пол {pat} должен уехать в rails.rs")
        for zone in workshop.WRITE_ZONES:
            self.assertIn(f'"{zone}"', text)

    def test_fingerprint_parsed_matches_generator(self):
        """Рукопожатие рельсов: отпечаток, который мост вычитывает из rails.rs, — это
        ровно тот, что генератор считает из питон-констант (иначе сверять нечего)."""
        self.assertEqual(hands.rails_fp_expected(), gen_rails.fingerprint())
        self.assertTrue(hands.rails_fp_expected(), "отпечаток обязан быть в rails.rs")

    def test_container_binary_survives_live_app_bind_mount(self):
        dockerfile = (Path(__file__).resolve().parent / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("/usr/local/bin/praxis-hands", dockerfile)
        self.assertIn("ENV PRAXIS_HANDS=/usr/local/bin/praxis-hands", dockerfile)
        self.assertNotIn("/app/bin/praxis-hands", dockerfile)


# --------------------------------------------------------------------------- #
#  2. Одна семантика: питон и бинарь отвечают одинаково
# --------------------------------------------------------------------------- #

class WorkshopContract:
    """Корпус фактов о мастерской. НЕ TestCase сам по себе (иначе прогонялся бы третий,
    бессмысленный раз) — его наследуют два конкретных класса: питоновый пол и бинарь."""

    use_hands = False

    @classmethod
    def setUpClass(cls):
        if cls.use_hands and not hands.available():
            raise unittest.SkipTest("бинарь praxis-hands не собран — проверяю только питон")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_hands_t_"))
        for d in ("workspace/projects", "soul", "memory/.state"):
            (self.tmp / d).mkdir(parents=True)
        (self.tmp / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.tmp / ".env").write_text("SECRET=x\n", encoding="utf-8")
        (self.tmp / "memory" / "llm.json").write_text('{"k":"secret"}', encoding="utf-8")

        self._orig = [(workshop, k, getattr(workshop, k)) for k in ("BASE", "REPO", "PROJECTS")]
        workshop.BASE = workshop.REPO = self.tmp
        workshop.PROJECTS = self.tmp / "workspace" / "projects"

        self._env = os.environ.get("PRAXIS_HANDS")
        if not self.use_hands:
            os.environ["PRAXIS_HANDS"] = "off"
        elif self._env and self._env.lower() == "off":
            os.environ.pop("PRAXIS_HANDS")

    def tearDown(self):
        for m, k, v in self._orig:
            setattr(m, k, v)
        if self._env is None:
            os.environ.pop("PRAXIS_HANDS", None)
        else:
            os.environ["PRAXIS_HANDS"] = self._env
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- зоны; имя файла не является границей ----------------------------- #

    def test_core_write_needs_proposal(self):
        out = workshop.fs_write("core.py", "x = 2")
        self.assertIn("предложение", out.lower())
        self.assertEqual((self.tmp / "core.py").read_text(encoding="utf-8"), "VALUE = 1\n")

    def test_credential_named_file_can_be_edited(self):
        out = workshop.fs_edit("memory/llm.json", "secret", "changed")
        self.assertIn("1 вхождение", out)
        self.assertEqual((self.tmp / "memory" / "llm.json").read_text(encoding="utf-8"),
                         '{"k":"changed"}')

    def test_escape_from_home_refused(self):
        out = workshop.fs_write("../../etc/evil.txt", "boom")
        self.assertTrue(out.startswith("Не пишу"), out)

    # --- fs_write: перезапись и гард усечения (16.5) ------------------------ #

    def test_new_file_written(self):
        self.assertIn("Записала", workshop.fs_write("workspace/a.py", "print(1)\n"))
        self.assertEqual((self.tmp / "workspace" / "a.py").read_text(encoding="utf-8"), "print(1)\n")

    def test_existing_file_refused_without_overwrite(self):
        workshop.fs_write("workspace/a.py", "x" * 100)
        out = workshop.fs_write("workspace/a.py", "y" * 100)
        self.assertIn("уже существует", out)
        self.assertIn("overwrite", out)

    def test_overwrite_allowed_when_asked(self):
        workshop.fs_write("workspace/a.py", "x" * 100)
        out = workshop.fs_write("workspace/a.py", "y" * 100, overwrite=True)
        self.assertIn("Записала", out)
        self.assertEqual((self.tmp / "workspace" / "a.py").read_text(encoding="utf-8"), "y" * 100)

    def test_shrink_guard_blocks_truncation(self):
        workshop.fs_write("workspace/a.py", "x" * 100)
        out = workshop.fs_write("workspace/a.py", "y" * 50, overwrite=True)
        self.assertIn("усечение", out)
        self.assertEqual(len((self.tmp / "workspace" / "a.py").read_text(encoding="utf-8")), 100)

    def test_shrink_guard_lifted_by_force(self):
        workshop.fs_write("workspace/a.py", "x" * 100)
        out = workshop.fs_write("workspace/a.py", "y" * 50, overwrite=True, force=True)
        self.assertIn("Записала", out)
        self.assertEqual(len((self.tmp / "workspace" / "a.py").read_text(encoding="utf-8")), 50)

    # --- fs_edit: точность и подсказки строк (16.5) ------------------------- #

    def test_edit_unique_occurrence(self):
        workshop.fs_write("workspace/a.py", "a = 1\nb = 2\n")
        out = workshop.fs_edit("workspace/a.py", "b = 2", "b = 3")
        self.assertIn("1 вхождение", out)
        self.assertIn("b = 3", (self.tmp / "workspace" / "a.py").read_text(encoding="utf-8"))

    def test_edit_zero_matches_is_honest(self):
        workshop.fs_write("workspace/a.py", "a = 1\n")
        out = workshop.fs_edit("workspace/a.py", "нет такого", "x")
        self.assertIn("0 совпадений", out)

    def test_edit_ambiguous_names_line_numbers(self):
        workshop.fs_write("workspace/a.py", "dup\nx\ndup\ny\ndup\n")
        out = workshop.fs_edit("workspace/a.py", "dup", "new")
        self.assertIn("3 совпадений", out)
        self.assertIn("строка 1", out)
        self.assertIn("строка 3", out)
        self.assertIn("dup\nx\ndup", (self.tmp / "workspace" / "a.py").read_text(encoding="utf-8"))

    def test_edit_missing_file(self):
        self.assertIn("Нет файла", workshop.fs_edit("workspace/нет.py", "a", "b"))

    # --- code_outline (16.5) ----------------------------------------------- #

    def test_outline_lists_defs_and_classes(self):
        workshop.fs_write("workspace/m.py", "import os\n\n\nclass A:\n    def m(self):\n        pass\n\n\ndef top():\n    pass\n")
        out = workshop.code_outline("workspace/m.py")
        self.assertIn("class A", out)
        self.assertIn("def m", out)
        self.assertIn("def top", out)
        self.assertIn("строка 4", out)

    def test_outline_of_non_python_file_is_not_secret_refusal(self):
        out = workshop.code_outline("memory/llm.json")
        self.assertNotIn("секрет", out.lower())

    # --- run: таймаут и потолок -------------------------------------------- #

    def test_run_captures_output(self):
        workshop.project_create("демо", "бриф")
        out = workshop.run(f'"{sys.executable}" -c "print(42)"', "демо")
        self.assertIn("42", out)

    def test_run_timeout_is_reported(self):
        workshop.project_create("демо", "бриф")
        out = workshop.run(f'"{sys.executable}" -c "import time; time.sleep(5)"', "демо", timeout=1)
        self.assertIn("та", out.lower())  # «таймаут» / «тайм-ауту»

    # --- чтение и поиск: рельсы одни на обе реализации ----------------------- #

    def test_credential_named_files_are_readable(self):
        out = workshop.fs_read(".env")
        self.assertIn("SECRET=x", out)
        out = workshop.fs_read("memory/llm.json")
        self.assertIn("secret", out)

    def test_search_literal_same_hits(self):
        workshop.fs_write("workspace/alpha.py", "needle_alpha = 1\n")
        workshop.fs_write("workspace/beta.py", "x = 0\nneedle_alpha = 2\n")
        out = workshop.fs_search("needle_alpha", glob="**/*.py").replace("\\", "/")
        self.assertIn("workspace/alpha.py:1", out)
        self.assertIn("workspace/beta.py:2", out)

    def test_search_includes_credential_named_files(self):
        out = workshop.fs_search("SECRET", glob="**/*").replace("\\", "/")
        self.assertIn(".env", out)


class TestWorkshopPython(WorkshopContract, unittest.TestCase):
    """Питоновый пол (бинарь выключен) — эталон семантики."""
    use_hands = False


class TestWorkshopHands(WorkshopContract, unittest.TestCase):
    """Тот же корпус через компилируемый пол. Скип, если бинарь не собран."""
    use_hands = True

    def test_binary_is_actually_used(self):
        self.assertTrue(hands.available(), "этот класс не должен был запуститься без бинаря")
        r = hands.call("version")
        self.assertTrue(r and r.get("ok"), r)

    def test_binary_rails_are_current(self):
        """Гейт-рельс: бинарь обязан быть собран под ТЕКУЩИЕ таблицы. Расхождение —
        не косметика (решения принимает бинарь) → пересобери praxis-hands и перевыложи."""
        r = hands.call("version") or {}
        self.assertEqual(r.get("rails_fp"), hands.rails_fp_expected(),
                         "рельсы бинаря отстали от кода — docker run … rust:alpine cargo build")
        self.assertEqual(hands.rails_status(), "fresh")

    def test_stale_rails_visible_in_state_line(self):
        """Подмена отпечатка в ожидании → state_line честно предупреждает."""
        orig = hands.rails_fp_expected
        hands.rails_fp_expected = lambda: "0" * 16
        try:
            self.assertIn("пересобери", hands.state_line())
        finally:
            hands.rails_fp_expected = orig

    def test_receipts_are_written(self):
        workshop.fs_write("workspace/a.py", "print(1)\n")
        rec = hands.receipts_path(self.tmp)
        self.assertTrue(rec.exists(), "каждая операция обязана оставить расписку")
        self.assertIn('"op":"write"', rec.read_text(encoding="utf-8"))

    def test_timeout_flag_actually_reaches_the_binary(self):
        """Регресс: `timeout` съедался сигнатурой моста и не уезжал флагом — бинарь молча
        ждал свои дефолтные 120с. Рельс, который не доехал, — это не рельс."""
        workshop.project_create("демо", "бриф")
        import time as _t
        t0 = _t.monotonic()
        out = workshop.run(f'"{sys.executable}" -c "import time; time.sleep(20)"', "демо", timeout=2)
        spent = _t.monotonic() - t0
        self.assertLess(spent, 15, "бинарь обязан убить процесс по своему --timeout")
        self.assertIn("тайм-ауту", out)

    def test_guard_verdicts_match_python(self):
        cases = ["core.py", "workspace/a.py", "memory/llm.json", "soul/x.md", "../evil"]
        for path in cases:
            rust = hands.guard(path, op="write", base=self.tmp)
            py_ok = workshop._resolve_write(path)[0] is not None
            self.assertIsNotNone(rust, "бинарь обязан ответить")
            self.assertEqual(rust["ok"], py_ok, f"вердикты разошлись на «{path}»")


# --------------------------------------------------------------------------- #
#  3. Мост не роняет ход
# --------------------------------------------------------------------------- #

class TestBridgeNeverBreaksHer(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("PRAXIS_HANDS")

    def tearDown(self):
        if self._env is None:
            os.environ.pop("PRAXIS_HANDS", None)
        else:
            os.environ["PRAXIS_HANDS"] = self._env

    def test_off_switch(self):
        os.environ["PRAXIS_HANDS"] = "off"
        self.assertIsNone(hands.binary())
        self.assertFalse(hands.available())
        self.assertIsNone(hands.call("version"))

    def test_missing_binary_returns_none(self):
        os.environ["PRAXIS_HANDS"] = str(Path(tempfile.gettempdir()) / "нет-такого-бинаря")
        self.assertIsNone(hands.call("version"), "нет бинаря → None → питоновый путь")

    def test_garbage_stdout_returns_none(self):
        script = Path(tempfile.mkdtemp()) / "junk.py"
        script.write_text("print('не json')\n", encoding="utf-8")
        real_run = subprocess.run

        def fake_run(argv, **kw):
            return real_run([sys.executable, str(script)], **kw)

        subprocess.run = fake_run
        os.environ["PRAXIS_HANDS"] = sys.executable  # любой существующий исполняемый файл
        try:
            self.assertIsNone(hands.call("version"), "не-JSON → None, а не исключение")
        finally:
            subprocess.run = real_run

    def test_state_line_is_honest(self):
        os.environ["PRAXIS_HANDS"] = "off"
        self.assertIn("питон", hands.state_line())
        self.assertNotIn("компилируемый пол (", hands.state_line())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
