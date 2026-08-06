"""Её слово не исчезает вместе с отвергнутым маршрутом.

Ветка постоянного отказа (`ChatAdminRequiredError` и родня) закрывает прогон терминально
— и это правильно: до неё доставка ретраилась вечно и била по аккаунту. Но у этой ветки
не было руки «сохранить произнесённое»: после терминализации текст недостижим по
построению, потому что `run_pending_text_deliveries` терминальные прогоны не переигрывает.

Цена за июль, посчитанная по её же ледерам прогонов: 12 прогонов, 5 адресатов,
**5265 символов** её авторского текста в никуда — включая 985 символов в личку Егора
(17.07). Единственным следом оставалась строка `delivery: failed` в кольце на 300 ходов:
без причины и без текста, и её оттуда вымывало.

Запуск:  python praxis_test.py test_undelivered_words -v
"""

from __future__ import annotations

import unittest
from unittest import mock

import agent


def _evidence(text: str = "Это я написала и хотела сказать.") -> dict:
    return {
        "replayable_text": True,
        "text_plan": {
            "conversation_id": "-1001341326876",
            "peer_id": "-1001341326876",
            "chunks": [{"text": text}],
        },
    }


class UndeliveredWordsAreSavedTests(unittest.TestCase):
    def _emitter(self):
        calls: list[dict] = []
        ledger = mock.Mock()
        ledger.emit.side_effect = lambda kind, **kw: calls.append(dict(kw, kind=kind))
        return calls, mock.patch.dict(
            "sys.modules", {"owner_delivery": mock.Mock(LEDGER=ledger)})

    def test_the_text_reaches_the_owner_receipt(self):
        calls, patcher = self._emitter()
        with patcher:
            agent._save_undelivered_words("run-1", _evidence(), "ChatAdminRequiredError")
        self.assertEqual(len(calls), 1, "расписки нет — её слово опять исчезло молча")
        got = calls[0]
        self.assertIn("Это я написала и хотела сказать.", got["body"])
        self.assertEqual(got["outcome"], "failure")
        self.assertIn("-1001341326876", got["title"])
        self.assertIn("ChatAdminRequiredError", got["reason"])
        self.assertEqual(got["correlation"]["run_id"], "run-1")

    def test_a_broken_receipt_never_blocks_terminalisation(self):
        """Если расписку записать не удалось, ход всё равно обязан закрыться — иначе
        доставка снова зациклится, ровно против чего эта ветка и строилась."""
        ledger = mock.Mock()
        ledger.emit.side_effect = RuntimeError("леджер недоступен")
        with mock.patch.dict("sys.modules", {"owner_delivery": mock.Mock(LEDGER=ledger)}):
            agent._save_undelivered_words("run-2", _evidence(), "ChatAdminRequiredError")

    def test_nothing_is_emitted_when_there_was_nothing_to_say(self):
        calls, patcher = self._emitter()
        with patcher:
            agent._save_undelivered_words("run-3", _evidence("   "), "ChatAdminRequiredError")
            agent._save_undelivered_words("run-4", {"text_plan": None}, "ChatAdminRequiredError")
            agent._save_undelivered_words("run-5", {}, "ChatAdminRequiredError")
        self.assertEqual(calls, [], "пустая расписка — тоже шум")

    def test_the_rescue_stands_on_the_permanent_refusal_path(self):
        """Страховка от рефакторинга: спасение обязано вызываться ИМЕННО там, где
        маршрут отвергнут навсегда, до закрытия вызова доставки."""
        import inspect

        src = inspect.getsource(agent.run_delivery_failed)
        self.assertIn("_save_undelivered_words", src)
        rescue = src.index("_save_undelivered_words")
        closed = src.index('"tool_failed"')
        self.assertLess(rescue, closed,
                        "спасать текст надо ДО закрытия вызова доставки")


if __name__ == "__main__":
    unittest.main(verbosity=2)
