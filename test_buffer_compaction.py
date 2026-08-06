"""Свёртка режет кольцо там, где свёрнутый блок лежит, а не только в голове.

Кольцо раннера и горячее кольцо места — параллельные последовательности одних и тех же
строк с РАЗНОЙ ёмкостью (BUF_MAXLEN=525 против HOT_HARD_HI=125), без курсора и без id
событий. Проверка закреплялась по нулевому индексу и молча предполагала, что свёрнутый
блок обязан лежать в начале буфера. Стоит буферу хоть раз оказаться длиннее горячего
кольца — совпадение головы ложно НАВСЕГДА, перезакрепиться нечем, состояние поглощающее.

Замер на живом проде 31.07: свёрнутый блок стоял на 449-й позиции; успешных срезов за
8 часов — ноль; старое уходило слепым вытеснением `deque(maxlen=…)` вместо осознанной
свёртки — в трёх горячих местах, включая личку Егора.

Запуск:  python praxis_test.py test_buffer_compaction -v
"""

from __future__ import annotations

import asyncio
import unittest
from collections import deque
from unittest import mock

import mtproto_runner as runner


class CompactTrimsTheRingTests(unittest.TestCase):
    CHAT = "7007"

    def setUp(self) -> None:
        self._orig = runner._buf.get(self.CHAT)
        self.addCleanup(self._restore)
        runner._compacting.discard(self.CHAT)
        self.addCleanup(runner._compacting.discard, self.CHAT)

    def _restore(self) -> None:
        if self._orig is None:
            runner._buf.pop(self.CHAT, None)
        else:
            runner._buf[self.CHAT] = self._orig

    def _ring(self, lines: list[str]) -> None:
        runner._buf[self.CHAT] = deque(lines, maxlen=runner.BUF_MAXLEN)

    def _compact(self, result: dict) -> None:
        with mock.patch.object(runner.memory_life, "place_key", lambda chat: str(chat)), \
             mock.patch.object(runner.memory_life, "compact_if_due", lambda place: result):
            asyncio.run(runner._maybe_compact(self.CHAT))

    def test_trims_when_the_fold_is_not_at_the_head(self):
        old = [f"старое {i}" for i in range(400)]
        folded = [f"свёрнутое {i}" for i in range(10)]
        live = [f"живое {i}" for i in range(5)]
        self._ring(old + folded + live)

        self._compact({"folded": len(folded), "folded_lines": folded,
                       "compact_id": "cmp-x", "hot": 5, "reason": "тест"})

        left = list(runner._buf[self.CHAT])
        self.assertEqual(left, live,
                         "срезано не по конец свёрнутого блока — кольцо снова копится")

    def test_head_anchored_fold_still_works(self):
        folded = [f"свёрнутое {i}" for i in range(3)]
        live = [f"живое {i}" for i in range(4)]
        self._ring(folded + live)
        self._compact({"folded": len(folded), "folded_lines": folded,
                       "compact_id": "cmp-y", "hot": 4, "reason": "тест"})
        self.assertEqual(list(runner._buf[self.CHAT]), live)

    def test_a_fold_that_is_not_in_this_ring_is_never_cut(self):
        """Свёртка охватила несколько веток комнаты, а этот буфер — только одну.
        Резать вслепую нельзя: снесём непредставленное сообщение."""
        ring = [f"моя ветка {i}" for i in range(6)]
        self._ring(ring)
        self._compact({"folded": 3, "folded_lines": ["чужая ветка 1", "чужая ветка 2"],
                       "compact_id": "cmp-z", "hot": 6, "reason": "тест"})
        self.assertEqual(list(runner._buf[self.CHAT]), ring, "срезано вслепую")

    def test_without_folded_lines_the_old_count_is_honoured(self):
        """Старый контракт: нет списка строк — режем по числу свёрнутых."""
        ring = [f"строка {i}" for i in range(10)]
        self._ring(ring)
        self._compact({"folded": 4, "compact_id": "cmp-w", "hot": 6, "reason": "тест"})
        self.assertEqual(list(runner._buf[self.CHAT]), ring[4:])

    def test_nothing_folded_leaves_the_ring_alone(self):
        ring = [f"строка {i}" for i in range(5)]
        self._ring(ring)
        self._compact({"folded": 0, "plan": {"reason": "open_episode"}})
        self.assertEqual(list(runner._buf[self.CHAT]), ring)


if __name__ == "__main__":
    unittest.main(verbosity=2)
