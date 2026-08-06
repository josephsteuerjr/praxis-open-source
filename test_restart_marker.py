"""Смёрженное предложение стоит ОДНОГО перезапуска, а не двух.

31.07 на живом дереве: `selfdev.merge()` пишет заявку контура, она читает в дневнике
«перезапущусь на новом коде» и зовёт `restart_self` (07:04), а поднявшийся процесс
находит заявку непогашенной и уходит снова («перезапуск по запросу контура» 07:06:34).
Два перезапуска подряд на каждый её самомёрж — и второй она не заказывала.
"""
from __future__ import annotations

import unittest
from unittest import mock

import agent
import selfdev


class RestartMarkerIsSatisfiedByAnyRestart(unittest.TestCase):
    def setUp(self):
        selfdev.clear_restart_request()
        self.addCleanup(selfdev.clear_restart_request)

    def test_her_own_restart_clears_the_contour_request(self):
        selfdev.request_restart("proposal deadbeef merged")
        self.assertIn("merged", selfdev.restart_requested())
        with mock.patch.object(agent, "_schedule_exit"):
            agent.tool_restart_self("загрузиться на новом коде")
        self.assertEqual(selfdev.restart_requested(), "",
                         "заявка контура уже удовлетворена — второй перезапуск лишний")

    def test_restart_without_a_pending_request_is_harmless(self):
        with mock.patch.object(agent, "_schedule_exit"):
            agent.tool_restart_self("просто так")
        self.assertEqual(selfdev.restart_requested(), "")

    def test_reason_still_lands_in_state_for_the_next_boot(self):
        with mock.patch.object(agent, "_schedule_exit"):
            agent.tool_restart_self("проверка причины")
        text = (agent.STATE_DIR / "restart_reason.txt").read_text(encoding="utf-8")
        self.assertIn("проверка причины", text)
        self.assertIn("restart_self", text)


if __name__ == "__main__":
    unittest.main()
