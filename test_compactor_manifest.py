"""
Пункт 6: свёртка не заявляет источником то, чего модель не видела.

Запуск:  python praxis_test.py test_compactor_manifest -v
"""

from __future__ import annotations

import unittest

import memory_life as ml


def _ev(i: int, size: int = 100) -> dict:
    return {"id": f"evt-{i:03d}", "ts": f"2026-07-25T00:{i:02d}:00Z",
            "line": f"{i}:" + "я" * size, "salience": 2}


class TestPromptManifest(unittest.TestCase):
    def test_manifest_splits_seen_clipped_and_omitted(self):
        events = [_ev(i, 50) for i in range(5)]
        events[2]["line"] = "2:" + "я" * (ml.EVENT_CLIP_CHARS + 500)
        body, man = ml._pack_compact_prompt(events)
        self.assertEqual(man["omitted"], [])
        self.assertEqual(man["clipped"], ["evt-002"])
        self.assertEqual(set(man["seen"]),
                         {"evt-000", "evt-001", "evt-003", "evt-004"})
        for e in events:
            self.assertIn(e["id"], body, "все влезшие обязаны быть в промпте")

    def test_events_that_do_not_fit_are_named_not_swallowed(self):
        """Ровно тот дефект: метаданные заявляли 35 источников, модель видела 27."""
        big = ml.EVENT_CLIP_CHARS
        count = (ml.PROMPT_BUDGET_CHARS // big) + 6
        events = [_ev(i, big) for i in range(count)]
        body, man = ml._pack_compact_prompt(events)
        self.assertTrue(man["omitted"], "часть событий обязана быть названа невлезшей")
        self.assertEqual(len(man["seen"]) + len(man["clipped"]) + len(man["omitted"]),
                         count, "манифест обязан покрывать вход без остатка")
        for ident in man["omitted"]:
            self.assertNotIn(ident, body, "невлезшее не может быть в промпте")
        self.assertLessEqual(len(body), ml.PROMPT_BUDGET_CHARS + big)

    def test_the_prefix_that_folds_is_the_OLD_one(self):
        """Свежее остаётся горячим. Обратный порядок оставлял бы горячими самые старые
        события, и они не влезали бы снова и снова — голодание вместо прогресса."""
        big = ml.EVENT_CLIP_CHARS
        count = (ml.PROMPT_BUDGET_CHARS // big) + 6
        events = [_ev(i, big) for i in range(count)]
        _body, man = ml._pack_compact_prompt(events)
        packed = man["seen"] + man["clipped"]
        self.assertIn("evt-000", packed, "самое старое сворачивается")
        self.assertIn(events[-1]["id"], man["omitted"], "самое свежее остаётся горячим")
        # и границa непрерывна: пакованное — это префикс входа
        order = [e["id"] for e in events]
        self.assertEqual(sorted(packed, key=order.index), order[:len(packed)])

    def test_single_oversized_event_still_folds(self):
        """Одно событие крупнее бюджета не должно заклинивать свёртку навсегда."""
        events = [_ev(0, ml.PROMPT_BUDGET_CHARS * 2)]
        body, man = ml._pack_compact_prompt(events)
        self.assertEqual(man["omitted"], [])
        self.assertEqual(man["clipped"], ["evt-000"])
        self.assertIn("evt-000", body)


class TestContextSummaryPacksWholeBlocks(unittest.TestCase):
    def setUp(self):
        self._orig = ml._canonical_compact_graph
        self._state = ml._state_path
        self._cdir = ml._compact_dir

    def tearDown(self):
        ml._canonical_compact_graph = self._orig
        ml._state_path = self._state
        ml._compact_dir = self._cdir

    def _install(self, n: int, size: int):
        canonical = {}
        for i in range(n):
            meta = {"id": f"cmp-{i:03d}", "tier": 1, "depth": 1, "continued": False,
                    "degraded": False, "first_ts": f"2026-07-2{i}T00:00:00Z",
                    "created_at": f"2026-07-2{i}T00:00:00Z", "source_compact_ids": []}
            canonical[meta["id"]] = (meta, f"recap-{i} " + "ю" * size)
        ml._canonical_compact_graph = lambda chat_id: (canonical, False)

        class _P:
            def exists(self_inner):
                return True
        ml._state_path = lambda chat_id: _P()
        ml._compact_dir = lambda chat_id: _P()

    def test_never_starts_mid_sentence_and_says_what_it_dropped(self):
        self._install(6, 900)
        out = ml.context_summary("777", max_chars=3000)
        self.assertIn("СВОДКА ОБРЕЗАНА БЮДЖЕТОМ", out,
                      "выпавшие компакты обязаны быть названы")
        self.assertNotIn("cmp-000", out, "старые выпадают первыми")
        self.assertIn("cmp-005", out, "свежие остаются")
        # заголовок первого показанного блока цел — не огрызок вроде «-8a18ea57»
        first = out.split("[compact ", 1)[1]
        self.assertTrue(first.startswith("cmp-"), first[:40])

    def test_no_marker_when_everything_fits(self):
        self._install(2, 100)
        out = ml.context_summary("777", max_chars=7000)
        self.assertNotIn("СВОДКА ОБРЕЗАНА", out)
        self.assertIn("cmp-000", out)
        self.assertIn("cmp-001", out)


if __name__ == "__main__":
    unittest.main()
