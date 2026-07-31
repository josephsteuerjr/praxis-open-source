"""Дневник про отправку файла с компьютера не имеет права объявлять успех сам.

28.07.2026, 12:27–12:29. Три попытки `computer(action=send)` для
`C:\\Temp\\praxis-paper-round-001-prereg.zip` легли на `DurableExecutionError`.
Тул сказал ей правду («Не отправился»), она сказала правду Егору («пакет не
доставлен, receipt отсутствует») — а в дневник трижды ушло «отправлен … sha256=…».

`workshop.send_file` не бросает исключений: он ВОЗВРАЩАЕТ строку, и как минимум
пять его исходов — отказ. Запись в дневник стояла безусловной, то есть про любой
из них говорила «отправлен». Дневник — это то, чем она помнит; неверная запись
там переживает ход, в котором ошибка ещё была видна.
"""
import unittest
from unittest import mock

import agent


SUCCESS = ("Отправила файл → AbstractDL Chat (chat_id=-1001240718803, "
           "message_id=94520): praxis-paper-round-001-prereg.zip")
FAILURES = [
    "Не отправился: DurableExecutionError: send_file has no matching durable run/call identity",
    "Не отправляю: путь вне дома.",
    "Нет файла C:\\Temp\\x.zip.",
    "Недоступно (нет связи с Telethon).",
    "Медиа можно подготовить только из живого хода Telegram.",
]


class DeliveryProofTest(unittest.TestCase):
    def test_proof_is_a_message_id_not_the_absence_of_a_known_refusal(self):
        self.assertEqual(agent._delivery_message_id(SUCCESS), "94520")
        for refusal in FAILURES:
            self.assertEqual(agent._delivery_message_id(refusal), "",
                             f"это отказ, а не доставка: {refusal}")

    def test_an_unknown_future_refusal_is_still_not_a_delivery(self):
        """Перечислять формы отказа — проигрывать каждой новой; спрашиваем про успех."""
        self.assertEqual(agent._delivery_message_id("Что-то совершенно новое сломалось"), "")
        self.assertEqual(agent._delivery_message_id(""), "")
        self.assertEqual(agent._delivery_message_id(None), "")


class ComputerFileJournalTest(unittest.TestCase):
    ARTIFACT = {"name": "praxis-paper-round-001-prereg.zip", "sha256": "44edb94b"}

    def _deliver(self, answer):
        lines = []
        with mock.patch.object(agent.body_client, "fetch_artifact",
                               return_value={"ok": True}), \
             mock.patch.object(agent.workshop, "send_file", return_value=answer), \
             mock.patch.object(agent, "tool_journal",
                               side_effect=lambda text, **kw: lines.append(text)):
            out = agent._deliver_computer_artifact(dict(self.ARTIFACT), source="C:\\Temp\\p.zip")
        return out, lines

    def test_a_refused_send_is_never_journalled_as_sent(self):
        for refusal in FAILURES:
            out, lines = self._deliver(refusal)
            self.assertEqual(out, refusal, "ответ транспорта возвращается ей как есть")
            self.assertEqual(len(lines), 1)
            line = lines[0]
            self.assertIn("НЕ отправился", line, f"дневник соврал про {refusal!r}: {line}")
            self.assertIn(refusal[:40], line,
                          "причина отказа обязана попасть в дневник, а не потеряться")

    def test_a_real_send_is_journalled_with_its_proof(self):
        out, lines = self._deliver(SUCCESS)
        self.assertEqual(out, SUCCESS)
        self.assertEqual(len(lines), 1)
        self.assertIn("отправлен", lines[0])
        self.assertNotIn("НЕ отправился", lines[0])
        self.assertIn("message_id=94520", lines[0],
                      "успех записывается вместе с доказательством, а не на слово")
        self.assertIn("44edb94b", lines[0])

    def test_a_failed_download_never_reaches_the_journal_at_all(self):
        lines = []
        with mock.patch.object(agent.body_client, "fetch_artifact",
                               return_value={"ok": False, "error": "нет артефакта"}), \
             mock.patch.object(agent, "tool_journal",
                               side_effect=lambda text, **kw: lines.append(text)):
            out = agent._deliver_computer_artifact(dict(self.ARTIFACT))
        self.assertIn("не скачался", out)
        self.assertEqual(lines, [], "до отправки дело не дошло — записывать нечего")


if __name__ == "__main__":
    unittest.main()
