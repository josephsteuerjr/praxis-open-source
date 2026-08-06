"""Уничтоженное слово Егора не показывается ей как его слово.

29.07 просьба «останови фон» легла в её договор как 75 знаков вопроса: кириллицу выбило
ascii-заменой ещё на стороне клиента. 01.08 поставлен сторож — записать такое ВПРЕДЬ
нельзя. Но запись старше сторожа продолжала показываться и в кадре ориентации, и в
`describe()` строкой «Просьба Егора (panel, 29.07 15:44): ???????????? …».

С её стороны это неотличимо от того, что он так и написал. Восстановить байты нельзя;
назвать утрату утратой — можно, и класс просьбы при этом настоящий и уцелел.
"""
from __future__ import annotations

import unittest

import appetite

DESTROYED = "???????????? ???????????????: ?? ???????? ????? ??????? ???? ?? ?????? ??????????????."


class DestroyedWordIsNamedNotShown(unittest.TestCase):
    def test_live_text_is_passed_through_untouched(self):
        req = {"kind": "pause_background", "raw": "останови фон, пожалуйста", "source": "panel"}
        self.assertEqual(appetite.owner_request_text(req), "останови фон, пожалуйста")

    def test_destroyed_text_is_replaced_by_an_honest_marker(self):
        req = {"kind": "pause_background", "raw": DESTROYED, "source": "panel"}
        out = appetite.owner_request_text(req)
        self.assertNotIn("???", out, "ряд знаков вопроса не показывается как его слова")
        self.assertIn("утрачен", out)
        self.assertIn("останови фон", out, "класс просьбы настоящий и должен уцелеть")

    def test_empty_text_falls_back_to_the_class(self):
        self.assertEqual(appetite.owner_request_text({"kind": "free", "raw": ""}),
                         "(не экономь)")

    def test_a_genuine_question_survives(self):
        """Граница: у настоящего вопроса есть буквы — его резать не за что."""
        req = {"kind": "text", "raw": "а можно тратить больше???", "source": "telegram"}
        self.assertEqual(appetite.owner_request_text(req), "а можно тратить больше???")

    def test_missing_request_is_harmless(self):
        self.assertEqual(appetite.owner_request_text(None), "(своими словами)")

    def test_describe_does_not_quote_destroyed_bytes(self):
        state = appetite.state()
        original = state.get("owner_request")
        try:
            appetite._save({**appetite._load(),
                            "owner_request": {"kind": "pause_background", "raw": DESTROYED,
                                              "ts": 1.0, "source": "panel"}})
            text = appetite.describe()
            self.assertIn("Просьба Егора", text)
            self.assertNotIn("???", text)
            self.assertIn("утрачен", text)
        finally:
            appetite._save({**appetite._load(), "owner_request": original})


if __name__ == "__main__":
    unittest.main()
