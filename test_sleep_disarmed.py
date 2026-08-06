"""Ночная сводка не выдаёт снятые фазы за измерения.

Найдено 03.08.2026: четыре числа отчёта сна были константами (`pruned = 0`,
`woke = 0`, `goal_lines = []`, `rumination_pass()` — заглушка `return 0, 0`).
Каждое снятие было осознанным и обосновано в коде: durable-правки идут через
formation claims, состояние людей меняет явный инструмент, желания продвигает
она сама. Но в журнал они попадали как «подрезано рёбер 0» — то есть как
результат работы. Это ровно тот класс, что и остальная неправда о ней самой:
ХРАНИМОЕ (здесь — снятое) подаётся как ТЕКУЩИЙ ФАКТ.

Тесты стерегут два обещания, а не формулировку:
  • ни одна снятая фаза не печатается цифрой;
  • если снятая фаза когда-нибудь сработает — отчёт кричит, а не молчит.
"""
from __future__ import annotations

import unittest
from unittest import mock

import sleep as sleep_mod


class DisarmedPhasesAreNamedNotCounted(unittest.TestCase):
    def _report(self):
        stub = lambda *a, **kw: None
        with mock.patch.object(sleep_mod.consolidate, "run", lambda *a, **k: "Сведено дней: 0."), \
             mock.patch.object(sleep_mod.formation, "run", lambda *a, **k: {"summary": "ок"}), \
             mock.patch.object(sleep_mod, "svs_dossier_pass", lambda **k: (0, 0)), \
             mock.patch.object(sleep_mod, "crystallisation_pass", stub), \
             mock.patch.object(sleep_mod, "rem_pass", lambda **k: (0, False)), \
             mock.patch.object(sleep_mod, "sweep_inbox", lambda *a, **k: 0), \
             mock.patch.object(sleep_mod, "_journal", stub):
            return sleep_mod.run(depth="full")

    def test_no_disarmed_phase_is_printed_as_a_number(self):
        out = self._report()
        for dead in ("подрезано рёбер", "нитей проснулось", "целей-событий"):
            self.assertNotIn(dead, out, "снятая фаза снова выдаётся за измерение")

    def test_the_report_names_what_was_removed(self):
        out = self._report()
        self.assertIn("снято намеренно:", out)
        for name in sleep_mod.DISARMED:
            self.assertIn(name, out)

    def test_every_disarmed_phase_carries_its_reason(self):
        """Реестр обязан объяснять, а не только перечислять."""
        self.assertTrue(sleep_mod.DISARMED)
        for name, why in sleep_mod.DISARMED.items():
            self.assertGreater(len(why), 20, f"{name}: причина не записана")

    def test_a_disarmed_phase_that_fires_is_shouted_not_swallowed(self):
        """Заглушка может однажды перестать быть заглушкой — молча это пройти нельзя."""
        with mock.patch.object(sleep_mod, "rumination_pass", lambda: (3, 7)):
            out = self._report()
        self.assertIn("СНЯТАЯ ФАЗА СРАБОТАЛА", out)
        self.assertIn("жвачка: 7", out)

    def test_live_phases_keep_their_numbers(self):
        out = self._report()
        for live in ("слито", "предложено слияний", "гипотез", "inbox"):
            self.assertIn(live, out)


if __name__ == "__main__":
    unittest.main()
