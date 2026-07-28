"""PASS 22 contract: каталог мозга, per-model наблюдения, switch_brain с рукопожатием."""
from __future__ import annotations

import datetime as _dt
import json
import tempfile
import unittest
from pathlib import Path

import brain
import llm


class Pass22Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_p22_"))
        mem = self.tmp / "memory"
        (mem / ".state").mkdir(parents=True)
        (mem / "journal").mkdir(parents=True)
        self._orig = []

        def patch(module, **attrs):
            for key, val in attrs.items():
                self._orig.append((module, key, getattr(module, key)))
                setattr(module, key, val)

        self.patch = patch
        patch(brain, BASE=self.tmp, STATS_PATH=mem / ".state" / "brain_stats.json",
              JOURNAL_DIR=mem / "journal")
        patch(llm, CONFIG_PATH=mem / "llm.json", USAGE_PATH=mem / ".state" / "usage.json")
        llm._CACHE.update(mtime=None, cfg=None)
        # чистый конфиг без env-миграции
        cfg = llm._normalize({
            "frameworks": {"anthropic": {"base_url": "https://api.z.ai/api/anthropic",
                                         "api_key": "test-key-a"},
                           "openai": {"base_url": "http://127.0.0.1:5012",
                                      "api_key": "test-key-o"}},
            "roles": {"voice": {"framework": "anthropic", "model": "glm-5.2",
                                "fallback_model": "gpt-5.6-sol", "max_tokens": 1024},
                      "evaluator": {"framework": "openai", "model": "gpt-5.6-terra",
                                    "fallback_model": "glm-4.7", "max_tokens": 400}},
            "limits": {}})
        llm.save_config(cfg)
        llm._CACHE.update(mtime=None, cfg=None)
        patch(llm, _available_models=lambda fw: {
            "anthropic": ["glm-5.2", "glm-4.7"],
            "openai": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.5"]}.get(fw, []))
        self.mem = mem

    def tearDown(self):
        for module, key, val in reversed(self._orig):
            setattr(module, key, val)
        llm._CACHE.update(mtime=None, cfg=None)


class StatsTests(Pass22Base):
    def test_note_call_aggregates(self):
        brain.note_call("voice", "anthropic", "glm-5.2", ok=True, latency_ms=1000)
        brain.note_call("voice", "anthropic", "glm-5.2", ok=True, latency_ms=2000)
        brain.note_call("voice", "anthropic", "glm-5.2", ok=False,
                        error="RateLimitError: 429", empty=False)
        brain.note_call("evaluator", "anthropic", "glm-5.2", ok=False,
                        error="EmptyResponseError: пусто", empty=True, fallback=False)
        m = brain.model_stats()["anthropic/glm-5.2"]
        self.assertEqual(m["calls"], 4)
        self.assertEqual(m["ok"], 2)
        self.assertEqual(m["errors"]["RateLimitError"], 1)
        self.assertEqual(m["empty"], 1)
        self.assertEqual(m["roles"], {"voice": 3, "evaluator": 1})
        self.assertEqual(m["lat_ms_ema"], 1200)  # EMA 0.2: 1000 -> 1200
        self.assertEqual(m["last_error"]["class"], "EmptyResponseError")

    def test_broken_stats_file_restarts(self):
        brain.STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        brain.STATS_PATH.write_text("{битый json", encoding="utf-8")
        brain.note_call("voice", "openai", "gpt-5.5", ok=True)
        self.assertEqual(brain.model_stats()["openai/gpt-5.5"]["calls"], 1)


class CatalogTests(Pass22Base):
    def test_allowlist_live_plus_configured(self):
        al = brain.allowlist("openai")
        self.assertIn("gpt-5.5", al)          # живой список
        self.assertIn("gpt-5.6-sol", al)      # fallback голоса живёт на openai
        self.patch(llm, _available_models=lambda fw: [])
        al = brain.allowlist("anthropic")
        self.assertIn("glm-5.2", al)          # провайдер молчит — сконфигурированные имена остаются

    def test_catalog_shape(self):
        cat = brain.catalog()
        self.assertEqual(cat["roles"]["voice"]["model"], "glm-5.2")
        self.assertIn("allowlist", cat["frameworks"]["openai"])
        self.assertIn("provider_remaining", cat)
        self.assertIn("мозг", brain.describe().lower())


class SwitchTests(Pass22Base):
    def test_switch_same_framework(self):
        self.patch(llm, ping=lambda role: (True, ""))
        res = brain.switch("evaluator", "gpt-5.5", why="дешевле на рутине")
        self.assertTrue(res["ok"], res)
        rc = llm._config()["roles"]["evaluator"]
        self.assertEqual((rc["framework"], rc["model"]), ("openai", "gpt-5.5"))
        self.assertEqual(rc["fallback_model"], "glm-4.7")  # фолбэк не тронут
        j = (self.mem / "journal" / f"{_dt.date.today().isoformat()}.md").read_text(encoding="utf-8")
        self.assertIn("свитч мозга", j)

    def test_switch_cross_framework_keeps_trio(self):
        self.patch(llm, ping=lambda role: (True, ""))
        res = brain.switch("voice", "gpt-5.6-sol", why="сложные задачи недели")
        self.assertTrue(res["ok"], res)
        rc = llm._config()["roles"]["voice"]
        self.assertEqual(rc["framework"], "openai")
        self.assertEqual(rc["model"], "gpt-5.6-sol")
        self.assertEqual(rc["fallback_model"], "glm-5.2")  # прежняя основная — запасной

    def test_handshake_failure_reverts(self):
        self.patch(llm, ping=lambda role: (False, "APIConnectionError: refused"))
        res = brain.switch("voice", "gpt-5.6-sol", why="проверка")
        self.assertFalse(res["ok"])
        self.assertIn("рукопожатие", res["error"])
        rc = llm._config()["roles"]["voice"]
        self.assertEqual((rc["framework"], rc["model"]), ("anthropic", "glm-5.2"))  # откат

    def test_guards(self):
        self.assertFalse(brain.switch("нет-такой", "glm-5.2", why="w")["ok"])
        self.assertFalse(brain.switch("voice", "", why="w")["ok"])
        self.assertFalse(brain.switch("voice", "glm-9000", why="w")["ok"])
        self.assertFalse(brain.switch("voice", "gpt-5.5", why=" ")["ok"])   # без «зачем» нельзя
        self.assertFalse(brain.switch("voice", "glm-5.2", why="w")["ok"])   # уже стоит

    def test_switch_writes_spine_event(self):
        import memory_life as life
        self.patch(life, BASE=self.tmp, LIFE_DIR=self.mem / "life",
                   EVENTS_DIR=self.mem / "life" / "events",
                   STATE_DIR=self.mem / ".state" / "life")
        (self.mem / "life" / "events").mkdir(parents=True)
        self.patch(llm, ping=lambda role: (True, ""))
        brain.switch("evaluator", "gpt-5.5", why="наблюдение за неделю")
        evs = []
        for p in (self.mem / "life" / "events").glob("*.jsonl"):
            evs += [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(evs[0]["kind"], "brain_switch")
        self.assertEqual(evs[0]["meta"]["model"], "gpt-5.5")


class ChatHookTests(Pass22Base):
    def test_usage_and_stats_use_actual_model(self):
        # _call возвращает ФАКТИЧЕСКОЕ имя (ротация): usage и статистика идут по нему
        self.patch(llm, _call=lambda fw, model, **kw: llm.LLMResponse(
            text="ok", usage={"in": 10, "out": 5}, framework=fw, model="glm-5.2-rotated"))
        resp = llm.chat("voice", messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(resp.text, "ok")
        usage = json.loads(llm.USAGE_PATH.read_text(encoding="utf-8"))
        day = usage[_dt.date.today().isoformat()]["voice"]
        self.assertIn("glm-5.2-rotated", day["models"])
        self.assertIn("anthropic/glm-5.2-rotated", brain.model_stats())

    def test_failure_then_fallback_noted(self):
        calls = {"n": 0}

        def flaky(fw, model, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise llm.EmptyResponseError("пустой стрим relay")
            return llm.LLMResponse(text="спасена", usage={"in": 1, "out": 1},
                                   framework=fw, model=model)

        self.patch(llm, _call=flaky, _client_for=lambda fw: object())
        resp = llm.chat("voice", messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(resp.text, "спасена")
        stats = brain.model_stats()
        self.assertEqual(stats["anthropic/glm-5.2"]["empty"], 1)          # сбой основной
        self.assertEqual(stats["openai/gpt-5.6-sol"]["fallback_used"], 1)  # успех запасной


class ToolTests(Pass22Base):
    def test_tool_scope_guard_and_status(self):
        import agent
        import rails
        self.patch(rails, DENIALS_PATH=self.mem / ".state" / "denials.jsonl")
        self.patch(agent, _CURRENT_SCOPE="group")
        out = agent.tool_switch_brain("switch", role="voice", model="gpt-5.5", why="w")
        self.assertIn("owner-скоуп", out)
        self.patch(agent, _CURRENT_SCOPE="owner")
        self.assertIn("каталог", agent.tool_switch_brain("status").lower())
        self.patch(llm, ping=lambda role: (True, ""))
        out = agent.tool_switch_brain("switch", role="evaluator", model="gpt-5.5",
                                      why="рутина дешевле")
        self.assertIn("Сменила", out)


if __name__ == "__main__":
    unittest.main()
