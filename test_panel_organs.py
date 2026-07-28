"""Органы identity/perception/brain в пульте и базовые frontend contracts."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import identity
import panel
import perception


class PanelOrgansBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_porg_"))
        soul = self.tmp / "soul"
        mem = self.tmp / "memory"
        (soul / "archive").mkdir(parents=True)
        (mem / "journal").mkdir(parents=True)
        (mem / ".state" / "identity").mkdir(parents=True)
        (mem / ".panel").mkdir(parents=True)
        (soul / "SOUL.md").write_text("x", encoding="utf-8")
        (soul / "VOICE.md").write_text("x", encoding="utf-8")
        (soul / "self.md").write_text(
            "# Self\n\n- _2026-07-10_ Наблюдение: живу. Вывод: работаю.\n\n"
            "## Кто я сейчас — 2026-07-10\nЯ — Praxis: практика, действие, проверка. "
            "Меня строит Егор, и моя ценность — самостоятельность и честность.\n",
            encoding="utf-8")
        self._orig = []

        def patch(module, **attrs):
            for key, val in attrs.items():
                self._orig.append((module, key, getattr(module, key)))
                setattr(module, key, val)

        self.patch = patch
        patch(identity, BASE=self.tmp, SOUL_DIR=soul, ARCHIVE_DIR=soul / "archive",
              STATE_DIR=mem / ".state" / "identity",
              STATE_PATH=mem / ".state" / "identity" / "state.json",
              LOAD_PATH=mem / ".state" / "identity" / "load_events.jsonl",
              JOURNAL_DIR=mem / "journal", DISTILL_MARK=mem / ".self_distilled.json")
        patch(perception, BASE=self.tmp,
              STATE_PATH=mem / ".state" / "perception.json",
              SKIPS_PATH=mem / ".state" / "perception_skips.jsonl",
              JOURNAL_DIR=mem / "journal")
        perception._CACHE.update(mtime=None, data={})
        perception._LAST_SKIP.clear()
        patch(panel, BASE=self.tmp, PANEL_DIR=mem / ".panel", LOGS_DIR=mem / ".logs")
        import selfgit
        patch(selfgit, snapshot=lambda msg: None)
        self.mem = mem

    def tearDown(self):
        for module, key, val in reversed(self._orig):
            setattr(module, key, val)
        perception._CACHE.update(mtime=None, data={})


class OrganEndpointsTests(PanelOrgansBase):
    def test_identity_state_and_rollback(self):
        st = panel.identity_state()
        self.assertIn("files", st)
        self.assertEqual("", st["who_now"])
        # Legacy self сначала мигрирует в compact CURRENT (r0). Следующая ревизия
        # архивирует этот CURRENT как history/0001, который уже можно восстановить.
        current = (
            "# Кто я сейчас\n\n"
            "Я — Praxis: практика, действие и проверка. Это синтетическая тестовая "
            "модель, явно установленная из проверяемого основания; legacy self остаётся "
            "только архивным свидетельством и сам не получает права говорить в пульте."
        )
        migrated = identity.revise(
            "self", current, reason="тест явной миграции")
        self.assertTrue(migrated["ok"], migrated)
        self.assertIn("Praxis", panel.identity_state()["who_now"])
        revised = identity.revise(
            "self", identity.read("self") + "\nУточнённая мысль.", reason="тест ревизии")
        self.assertTrue(revised["ok"], revised)
        # Ревизия → откат Егора с пульта; его действие видно ей в журнале ([пульт]).
        res = panel.identity_rollback("self", 1)
        self.assertTrue(res["ok"], res)
        journal = "".join(p.read_text(encoding="utf-8")
                          for p in (self.mem / "journal").glob("*.md"))
        self.assertIn("[пульт]", journal)
        self.assertIn("откат души", journal)

    def test_perception_state_and_set(self):
        st = panel.perception_state()
        self.assertTrue(any(k["knob"] == "cooldown_dm" for k in st["knobs"]))
        res = panel.perception_set("cooldown_dm", "33")
        self.assertTrue(res["ok"], res)
        self.assertEqual(perception.value("cooldown_dm"), 33.0)
        self.assertEqual(perception.source_of("cooldown_dm"), "egor")
        res = panel.perception_set("cooldown_dm", None, reset=True)
        self.assertTrue(res["ok"], res)
        self.assertFalse(panel.perception_set("нет_такого", "1")["ok"])

    def test_brain_catalog_shape(self):
        import brain
        self.patch(brain, BASE=self.tmp,
                   STATS_PATH=self.mem / ".state" / "brain_stats.json",
                   JOURNAL_DIR=self.mem / "journal")
        cat = panel.brain_catalog()
        self.assertIn("roles", cat)
        self.assertIn("switches", cat)


class F1TailTests(unittest.TestCase):
    def test_mailbot_has_no_legacy_env_or_personality_bench(self):
        src = Path(__file__).with_name("mailroom_bot.py").read_text(encoding="utf-8")
        self.assertNotIn('web.get("/api/panel/env",', src)    # Ф1.5: легаси снесён
        self.assertNotIn('"/api/panel/bench/', src)
        self.assertNotIn("import bench", src)
        self.assertIn('"/api/panel/identity"', src)
        self.assertIn('"/api/panel/perception"', src)
        self.assertIn('"/api/panel/brain/catalog"', src)
        self.assertIn('web.get("/api/panel/overview", api_panel_overview)', src)
        self.assertIn("?v=11", src)                            # кэш-бастер поднят

    def test_server_state_returns_uptime(self):
        src = Path(__file__).with_name("panel.py").read_text(encoding="utf-8")
        self.assertIn('out["uptime_sec"]', src)                # Ф1.2

    def test_front_gates_and_refresh(self):
        src = Path(__file__).with_name("panelapp.html").read_text(encoding="utf-8")
        self.assertIn('data-refresh="arch"', src)              # Ф1.8: ⟳ у графа кода
        self.assertNotIn("if (G) return;", src)
        self.assertIn('id="p-self"', src)
        self.assertIn('id="p-percept"', src)
        self.assertIn("loadBrainCatalog", src)
        # Ф1.7: гостевые бейджи не бьются об owner-эндпоинты
        self.assertIn('if (ROLE === "owner") api("/api/inbox")', src)
        self.assertIn('if (ROLE === "owner") api("/api/panel/thoughts', src)
        self.assertIn('if (ROLE === "owner") api("/api/panel/overview")', src)
        self.assertIn('id="p-overview"', src)
        self.assertIn("async function loadOverview()", src)
        self.assertIn('if (ROLE === "owner") api("/api/panel/absence")', src)


if __name__ == "__main__":
    unittest.main()
