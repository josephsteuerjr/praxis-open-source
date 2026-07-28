"""
Тесты PASS 18 — суверенитет и аппетиты: тул manage_appetite через respond, STATE-строка,
гейты фона (окна/сон) по её плану и слову Егора, расход окна в кольце, ручки пульта,
фиксы контролов. Герметичны (test_perceive.Base / test_pass10.RingBase, модель — фейк).

Запуск:  python praxis_test.py test_pass18 -v
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import time
import types
import unittest
from pathlib import Path

from test_perceive import Base, FakeClient, FakeResp  # noqa: F401
from test_rooms_and_admission import ScriptedClient, _tool_use

import agent
import appetite
import heartbeat
import unanswered
import llm
import panel
import rails
import sleep


class AppetiteBase(Base):
    """Hermetic appetite, wake-receipt, rails and sleep paths."""

    def setUp(self):
        super().setUp()
        mem = self.tmp / "memory"
        self._orig.extend([
            (heartbeat, "DECISIONS_PATH", heartbeat.DECISIONS_PATH),
            (appetite, "BASE", appetite.BASE),
            (appetite, "STATE_PATH", appetite.STATE_PATH),
            (appetite, "CONTRACT_MD", appetite.CONTRACT_MD),
            (appetite, "JOURNAL_DIR", appetite.JOURNAL_DIR),
            (rails, "BASE", rails.BASE),
            (rails, "RAILS_MD", rails.RAILS_MD),
            (rails, "DENIALS_PATH", rails.DENIALS_PATH),
            (sleep, "STATE_PATH", sleep.STATE_PATH),
            (llm, "USAGE_PATH", llm.USAGE_PATH),
        ])
        heartbeat.DECISIONS_PATH = mem / ".state" / "window_decisions.json"
        appetite.BASE = self.tmp
        appetite.STATE_PATH = mem / ".state" / "appetite.json"
        appetite.CONTRACT_MD = mem / "appetite.md"
        appetite.JOURNAL_DIR = mem / "journal"
        rails.BASE = self.tmp
        rails.RAILS_MD = self.tmp / "soul" / "rails.md"
        rails.DENIALS_PATH = mem / ".state" / "denials.jsonl"
        sleep.STATE_PATH = mem / ".state" / "sleep.json"
        llm.USAGE_PATH = mem / ".state" / "usage.json"

    def _ring(self):
        return json.loads(heartbeat.DECISIONS_PATH.read_text(encoding="utf-8"))


class TestTool(AppetiteBase):
    def test_registered(self):
        self.assertIn("manage_appetite", agent.TOOL_IMPL)
        self.assertIn("manage_appetite", [t["name"] for t in agent.OWNER_TOOLS])

    def test_dispatch_through_respond(self):
        steps = [
            _tool_use("t1", "manage_appetite",
                      {"action": "interpret", "mode": "considerate",
                       "text": "умеряю: окна реже, сон лёгкий",
                       "sleep_depth": "light", "raw_request": "умерь аппетиты"}),
            FakeResp("умерила: фон стал бережливым, жертвую глубокой археологией"),
        ]
        llm.use_test_client(ScriptedClient(steps))
        try:
            reply = agent.respond("умерь аппетиты", [], "Егор",
                                  force_voice=True, is_owner=True, chat_id="7777")
        finally:
            llm.clear_test_clients()
        self.assertIn("умерила", reply)
        s = appetite.state()
        self.assertEqual(s["mode"], "considerate")
        self.assertEqual(s["owner_request"]["raw"], "умерь аппетиты")  # слово Егора из чата
        self.assertIn("моё толкование", appetite.CONTRACT_MD.read_text(encoding="utf-8"))

    def test_status_action_direct(self):
        out = agent.tool_manage_appetite("status")
        self.assertIn("Режим: free", out)
        self.assertIn("unknown", out)          # остаток провайдера — честно

    def test_state_block_line(self):
        appetite.set_owner_request("considerate", "умерь аппетиты", source="panel")
        block = agent.build_state_block()
        evidence = agent.build_state_evidence_block()
        self.assertIn('"fact":"appetite"', block)
        self.assertIn('"request_pending":true', block)
        self.assertNotIn("умерь аппетиты", block)
        self.assertIn("не истолкована", evidence)


class TestWindowGates(AppetiteBase):
    def test_background_hold_is_the_only_live_appetite_lever(self):
        """Пауза фона держит ровно то, что умеет держать — и манифест говорит то же.

        ⚠ Здесь было три теста через `heartbeat.window_goal()` — путь, который
        не вызывался в проде ниоткуда. Они проверяли паузу, план по окнам и
        подсказку бережливого режима — и все трое были зелёными, пока ничего
        из этого не работало. Контур выброшен 25.07; проверяем живой рычаг.
        """
        appetite.set_owner_request("pause_background", "", source="panel")
        self.assertTrue(appetite.background_hold(),
                        "«останови фон» держит окна и пробуждения по расписанию, сон, formation")
        row = next((r for r in rails.registry()
                    if isinstance(r, dict) and r.get("id") == "appetite_pause"), None)
        self.assertIsNotNone(row, "рычаг обязан быть назван в манифесте")
        text = str(row)
        self.assertIn("часовой пульс", text,
                      "манифест называет и то, что рычаг НЕ держит")
    def test_appetite_plan_is_recorded_but_is_not_a_lever(self):
        """План по окнам — наблюдаемая запись, а не рычаг, и это теперь сказано прямо.

        ⚠ Здесь было два теста: `test_windows_off_is_her_plan` и
        `test_considerate_hint_reaches_decision`. Оба звали `heartbeat.window_goal()`
        и проверяли, что план и подсказка «доходят до решения». На уровне модуля это
        было правдой — и полной фикцией в проде: единственный вызывающий window_goal
        (`_heartbeat_once`) не стоял ни в одной строке `_clock_jobs()`. Ровно тот
        случай, ради которого написан CONTRACTS.md: тест проходил, соврав соседу.
        Контур выброшен 25.07 по решению Егора; проверяем то, что осталось правдой.
        """
        appetite.interpret("considerate", "окна закрываю", windows=False)
        plan = (appetite.state().get("interpretation") or {}).get("plan") or {}
        self.assertIs(plan.get("windows"), False, "её намерение записано и наблюдаемо")
        self.assertFalse(hasattr(appetite, "windows_off"),
                         "рычага, которого не существовало, больше нет и в коде")
        self.assertFalse(hasattr(appetite, "considerate_hint"))
        self.assertFalse(hasattr(heartbeat, "window_goal"),
                         "мёртвый контур решения об окне удалён целиком")

    def test_scheduled_skips_on_pause(self):
        appetite.set_owner_request("pause_background", "", source="panel")

        class NoCall:
            def __getattr__(self, k):
                raise AssertionError("сон не должен звать модель при паузе")
        llm.use_test_client(NoCall())
        out = sleep.run_scheduled()
        self.assertIn("сон отложен", out)
        st = json.loads(sleep.STATE_PATH.read_text(encoding="utf-8"))
        self.assertIn("пропуск", st.get("last_error", ""))
        self.assertFalse(st.get("last_run_ts"), "метка не ставится — снятие паузы вернёт сон")

    def test_depth_resolution(self):
        appetite.interpret("considerate", "умеряю")
        self.assertEqual(appetite.sleep_depth(), "light")
        appetite.interpret("free", "не экономим")
        self.assertEqual(appetite.sleep_depth(), "full")


class TestPanelAppetite(AppetiteBase):
    def test_state_shape(self):
        appetite.set_owner_request("considerate", "умерь", source="panel")
        s = panel.appetite_state()
        for k in ("mode", "owner_request", "observed", "promise",
                  "background_hold", "denials", "denials_today", "promise_check"):
            self.assertIn(k, s)
        self.assertEqual(s["owner_request"]["kind"], "considerate")

    def test_intent_writes_owner_word(self):
        r = panel.appetite_intent("pause_background", "")
        self.assertTrue(r["ok"])
        self.assertIsNotNone(appetite.background_hold())
        r2 = panel.appetite_intent("explode", "")
        self.assertFalse(r2["ok"])          # честный отказ на мусорный kind
        r3 = panel.appetite_intent("text", "")
        self.assertFalse(r3["ok"])          # пустая свободная просьба

    def test_http_owner_gate(self):
        import mailroom_bot as mb
        req = types.SimpleNamespace(headers={}, query={}, match_info={})
        for h in (mb.api_panel_appetite, mb.api_panel_appetite_intent):
            resp = asyncio.run(h(req))
            self.assertEqual(resp.status, 403, f"{h.__name__} пускает без owner-подписи")


class TestUsageByModel(AppetiteBase):
    def test_usage_add_records_models(self):
        llm._usage_add("voice", {"in": 100, "out": 50}, model="glm-5.2")
        llm._usage_add("voice", {"in": 10, "out": 5}, fallback=True, model="gpt-5.5")
        day = llm.usage_days(1)[_dt.date.today().isoformat()]["voice"]
        self.assertEqual(day["in"], 110)
        self.assertEqual(day["calls"], 2)
        self.assertEqual(day["models"]["glm-5.2"]["in"], 100)
        self.assertEqual(day["models"]["gpt-5.5"]["calls"], 1)

    def test_panel_cost_prefers_actual_model(self):
        llm._usage_add("voice", {"in": 1_000_000, "out": 0}, model="cheap-x")
        orig = llm.pricing
        llm.pricing = lambda: {"cheap-x": {"in_per_1m": 1.0, "out_per_1m": 1.0},
                               "expensive-y": {"in_per_1m": 100.0, "out_per_1m": 100.0}}
        try:
            u = panel.llm_usage()
        finally:
            llm.pricing = orig
        day = _dt.date.today().isoformat()
        self.assertAlmostEqual(u["cost"][day]["voice"], 1.0,
                               msg="cost должен считаться по фактической модели вызова")


class TestControlFixes(AppetiteBase):
    HTML = (Path(__file__).resolve().parent / "panelapp.html").read_text(encoding="utf-8")

    def test_env_apply_sends_confirm(self):
        self.assertIn('post("/api/panel/env/apply", { changes: envChanges, confirm: true })',
                      self.HTML, "«Параметры → Применить» снова шлёт без confirm (вечный 400)")

    def test_retired_behavior_controls_absent(self):
        self.assertNotIn('id="bWindows"', self.HTML)
        self.assertNotIn("data-bdrift", self.HTML)

    def test_appetite_section_exists(self):
        self.assertIn('id="brainAppetite"', self.HTML)
        self.assertIn("/api/panel/appetite/intent", self.HTML)


if __name__ == "__main__":
    unittest.main(verbosity=2)
