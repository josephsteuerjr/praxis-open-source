"""
Пункт 5, датчик расщепления комнаты.

Смысл: утверждение «обычная супергруппа разъезжается на псевдо-темы» перестаёт быть
выводом из чтения кода и становится числом. И остаётся прибором после починки —
если ключи не схлопнулись, починки не было.

Запуск:  python praxis_test.py test_route_split_probe -v
"""

from __future__ import annotations

import types
import unittest

import telegram_topics as tt


def _msg(mid: int, *, top=None, parent=None, forum=False, opener=False):
    """Сообщение в форме, которую читает route_for_message."""
    header = None
    if top is not None or parent is not None:
        header = types.SimpleNamespace(
            reply_to_top_id=top, reply_to_msg_id=parent, forum_topic=forum)
    action = types.SimpleNamespace() if not opener else _Opener()
    return types.SimpleNamespace(id=mid, reply_to=header, action=action)


class _Opener:
    pass


_Opener.__name__ = "MessageActionTopicCreate"


class TestOrdinarySupergroup(unittest.TestCase):
    def test_reply_chain_splits_today_and_collapses_without_forum(self):
        """Ровно спорный случай: одна человеческая беседа, несколько технических."""
        msgs = [
            _msg(100),                          # корень
            _msg(101, parent=100),              # первый ответ
            _msg(102, top=100, parent=101),     # вглубь
            _msg(103, top=100, parent=102),
            _msg(104, top=100, parent=103),
        ]
        r = tt.measure_split("-1001240718803", msgs)
        self.assertEqual(r["messages"], 5)
        self.assertGreater(r["keys_live"], r["keys_if_not_forum"],
                           "сегодня комната обязана расщепляться — иначе датчик слеп")
        self.assertEqual(r["keys_if_not_forum"], 1,
                         "обычная супергруппа — одна комната")
        self.assertLess(r["largest_branch_share"], 1.0,
                        "с самой крупной ветки видно не всю комнату")

    def test_probe_is_pure_and_reports_the_gap_in_numbers(self):
        msgs = [_msg(200)] + [_msg(200 + i, top=200, parent=200 + i - 1)
                              for i in range(1, 30)]
        r = tt.measure_split("-100", msgs)
        self.assertEqual(r["messages"], 30)
        self.assertEqual(r["keys_if_not_forum"], 1)
        self.assertEqual(sum(r["top_keys"].values()), 30,
                         "распределение обязано покрывать вход без остатка")


class TestTrueForumIsNotBroken(unittest.TestCase):
    def test_forum_topics_stay_separate_under_both_routes(self):
        """Датчик не должен агитировать за схлопывание НАСТОЯЩИХ тем — иначе он
        оправдывал бы починку, которая ломает форумы."""
        msgs = [
            _msg(700, opener=True),
            _msg(701, top=700, parent=700, forum=True),
            _msg(800, opener=True),
            _msg(801, top=800, parent=800, forum=True),
        ]
        r = tt.measure_split("-1001152779373", msgs)
        self.assertEqual(r["messages"], 4)
        self.assertGreaterEqual(r["keys_live"], 2, "две темы — два ключа")


class TestEmptyAndJunk(unittest.TestCase):
    def test_no_messages(self):
        r = tt.measure_split("-100", [])
        self.assertEqual(r["messages"], 0)
        self.assertEqual(r["largest_branch_share"], 0.0)

    def test_items_without_id_are_skipped(self):
        r = tt.measure_split("-100", [types.SimpleNamespace(id=None), _msg(1)])
        self.assertEqual(r["messages"], 1)


class TestProbeLedgerIsAnInstrumentNotMemory(unittest.TestCase):
    def test_probe_path_is_outside_the_index(self):
        import memory_fts
        import context_envelope as ce
        from pathlib import Path
        rel = Path(ce.PROBE_PATH)
        self.assertIn(".state", rel.as_posix())
        self.assertIsNone(
            memory_fts._selected_jsonl(rel, rel.parent.parent),
            "показания приборов не должны попадать в её recall")


if __name__ == "__main__":
    unittest.main()
