"""Где она МОЖЕТ писать — сказано до того, как она напишет.

⚠ Долг с 28.07 («кадр говорит заранее: здесь читаю, писать не могу»), закрыт 03.08.
Живьём в тот же день в 14:08 она ответила в вещательный канал AbstractDL: доставка
вернула отказ, Егор поправил её вручную, а в её кольце ходов остались два
«не сказала: доставка не удалась». Знание «это канал» лежало в объекте апдейта
бесплатно — просто не спрашивалось.

Переадресации здесь нет НАМЕРЕННО. Её условие 28.07: «адрес должен быть виден до моего
выбора, а receipt — после». Мы показываем адрес; отвечать или молчать — её ход.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import telegram_routes


class WritingIsRecordedApartFromForumEpochs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="routes_writing_")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        root.mkdir(exist_ok=True)
        patcher = mock.patch.object(telegram_routes, "DIR", root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_nothing_known_says_nothing(self):
        """Молчание честно: про незнакомое место фразы быть не должно."""
        self.assertEqual(telegram_routes.writing_line("-100777"), "")

    def test_an_ordinary_group_says_nothing(self):
        telegram_routes.note_writing("-100777", broadcast=False)
        self.assertEqual(telegram_routes.writing_line("-100777"), "")

    def test_a_channel_without_a_known_discussion_admits_it(self):
        telegram_routes.note_writing("-100777", broadcast=True)
        line = telegram_routes.writing_line("-100777")
        self.assertIn("писать в него — нет", line)
        self.assertIn("не знаю", line, "незнание адреса выдано за знание")

    def test_a_channel_with_a_discussion_names_the_address(self):
        telegram_routes.note_writing("-1001341326876", broadcast=True,
                                     linked_chat_id="-1001240718803",
                                     linked_title="AbstractDL Chat")
        line = telegram_routes.writing_line("-1001341326876")
        self.assertIn("AbstractDL Chat", line)
        self.assertIn("-1001240718803", line, "адрес назван словами, но не адресом")

    def test_an_empty_observation_does_not_erase_what_is_known(self):
        """«Я не посмотрел» не должно выглядеть как «посмотрел, и там пусто»."""
        telegram_routes.note_writing("-100777", broadcast=True,
                                     linked_chat_id="-100888", linked_title="Обсуждение")
        telegram_routes.note_writing("-100777")
        rec = telegram_routes.writing_of("-100777")
        self.assertEqual(rec["linked_chat_id"], "-100888")
        self.assertTrue(rec["broadcast"])

    def test_writing_does_not_disturb_the_forum_epochs(self):
        """Два разных вопроса — две разные записи, и одна не сдвигает другую."""
        telegram_routes.observe("-100777", kind="get_forum_topics_ok", message_id=10)
        before = telegram_routes.current("-100777")
        telegram_routes.note_writing("-100777", broadcast=True)
        after = telegram_routes.current("-100777")
        self.assertEqual(after["forum_status"], before["forum_status"])
        self.assertEqual(after["epoch"], before["epoch"])

    def test_the_forum_epochs_do_not_disturb_writing(self):
        telegram_routes.note_writing("-100777", broadcast=True, linked_chat_id="-100888")
        telegram_routes.observe("-100777", kind="legacy_chat", message_id=11)
        self.assertEqual(telegram_routes.writing_of("-100777")["linked_chat_id"], "-100888")

    def test_the_line_never_offers_to_send_for_her(self):
        """Её условие: адрес виден ДО выбора. Выбор остаётся её — не наш."""
        telegram_routes.note_writing("-100777", broadcast=True,
                                     linked_chat_id="-100888", linked_title="Обсуждение")
        line = telegram_routes.writing_line("-100777")
        for word in ("перенаправ", "автоматически", "сама отправлю", "за тебя"):
            self.assertNotIn(word, line.casefold())


class TheFrameCarriesItBeforeSheWrites(unittest.TestCase):
    """Фраза обязана доехать в её кадр, а не остаться в реестре."""

    def test_the_prompt_reads_the_registry(self):
        import agent

        ctx = agent.ChannelContext.from_legacy(
            chat_id=-1001341326876, is_dm=False, owner=False, known=True)
        with mock.patch.object(telegram_routes, "writing_line",
                               return_value="ПРОБНАЯ ФРАЗА ПРО КАНАЛ"):
            parts = agent._build_prompt_parts(ctx=ctx)
        whole = "\n".join(str(x or "") for x in parts)
        self.assertIn("ПРОБНАЯ ФРАЗА ПРО КАНАЛ", whole,
                      "реестр знает, а её кадр — нет: ровно тот разрыв, что мы чиним")


if __name__ == "__main__":
    unittest.main()
