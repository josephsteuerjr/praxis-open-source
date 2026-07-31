"""Директива `ОТВЕТ->#id`: снимается и когда модель поставила её НИЖЕ преамбулы.

30.07.2026. У разбора этой директивы не было ни одного теста. Живой случай на glm-5.2:
в комнату ушло «Теперь у меня полная картина. Отвечу Егору.\\n\\nОТВЕТ->#94884\\n\\n…» —
реплай раннер разобрал, а служебная строка осталась в теле сообщения и уехала читателю.
Регэксп якорился на самое начало текста, поэтому преамбула его прятала.

Второе требование теста — противоположное: разбор НЕ должен съедать строку из объяснения
самого протокола в середине длинного сообщения.
"""
import unittest

import agent


class ReplyDirectiveHead(unittest.TestCase):
    def test_first_line_unchanged(self):
        """Прежнее поведение байт-в-байт: директива первой строкой."""
        text, mid = agent.split_reply_directive("ОТВЕТ->#123\nвот ответ")
        self.assertEqual(text, "вот ответ")
        self.assertEqual(mid, 123)

    def test_directive_below_preamble_is_stripped(self):
        """Живой случай: план моделью напечатан в текст, директива — строкой ниже."""
        raw = ("Теперь у меня полная картина. Отвечу Егору.\n\n"
               "ОТВЕТ->#94884\n\n"
               "Thinking уже включён на статике промпта.")
        text, mid = agent.split_reply_directive(raw)
        self.assertEqual(mid, 94884)
        self.assertNotIn("ОТВЕТ", text)
        self.assertIn("Теперь у меня полная картина", text)
        self.assertIn("Thinking уже включён", text)

    def test_unknown_id_strips_directive_and_sends_plainly(self):
        raw = "Короткая преамбула.\nОТВЕТ->#777\nтело"
        text, mid = agent.split_reply_directive(raw, known_ids=[1, 2, 3])
        self.assertIsNone(mid)
        self.assertNotIn("ОТВЕТ", text)
        self.assertIn("тело", text)

    def test_seam_leaves_no_empty_paragraph(self):
        """Снятая строка не должна оставлять после себя пустой абзац."""
        raw = "преамбула\n\nОТВЕТ->#5\n\nтело"
        text, mid = agent.split_reply_directive(raw)
        self.assertEqual(mid, 5)
        self.assertEqual(text, "преамбула\n\nтело")
        self.assertNotIn("\n\n\n", text)

    def test_known_id_accepted_below_preamble(self):
        raw = "преамбула\nОТВЕТ->#42\nтело"
        text, mid = agent.split_reply_directive(raw, known_ids=[42])
        self.assertEqual(mid, 42)
        self.assertNotIn("ОТВЕТ", text)


class ReplyDirectiveLeftAlone(unittest.TestCase):
    def test_deep_in_body_is_not_eaten(self):
        """Объяснение протокола в середине сообщения — это её текст, не директива."""
        body = "\n".join(["строка %d" % i for i in range(1, 9)])
        raw = body + "\nОТВЕТ->#555\nи дальше про это"
        text, mid = agent.split_reply_directive(raw)
        self.assertIsNone(mid)
        self.assertEqual(text, raw)

    def test_inside_code_fence_is_not_eaten(self):
        raw = "вот как это работает:\n```\nОТВЕТ->#999\n```\nконец"
        text, mid = agent.split_reply_directive(raw)
        self.assertIsNone(mid)
        self.assertEqual(text, raw)

    def test_not_alone_on_line_is_not_a_directive(self):
        raw = "смотри: ОТВЕТ->#31337 — вот такая директива\nи текст"
        text, mid = agent.split_reply_directive(raw)
        self.assertIsNone(mid)
        self.assertEqual(text, raw)

    def test_plain_text_untouched(self):
        raw = "просто ответ без директив"
        text, mid = agent.split_reply_directive(raw)
        self.assertIsNone(mid)
        self.assertEqual(text, raw)

    def test_empty(self):
        self.assertEqual(agent.split_reply_directive(""), ("", None))
        self.assertEqual(agent.split_reply_directive(None), ("", None))


if __name__ == "__main__":
    unittest.main()
