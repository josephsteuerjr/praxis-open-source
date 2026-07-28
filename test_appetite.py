"""
PASS 18.3/18.4 — appetite: договор об аппетитах без права вето.

Учёт и толкование: слово Егора (кнопка/текст) фиксируется кодом, режим и план меняет
только ЕЁ рука (interpret/pledge); гейты фона читают её план; сверка обещания честная.

Запуск:  python praxis_test.py test_appetite -v
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import appetite
import llm


class _NoCallClient:
    def __init__(self):
        self.messages = self

    def create(self, **kw):
        raise AssertionError("appetite не должен звать модель")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_appetite_"))
        self._orig = [(appetite, "BASE", appetite.BASE),
                      (appetite, "STATE_PATH", appetite.STATE_PATH),
                      (appetite, "CONTRACT_MD", appetite.CONTRACT_MD),
                      (appetite, "JOURNAL_DIR", appetite.JOURNAL_DIR),
                      (llm, "USAGE_PATH", llm.USAGE_PATH),
                      (llm, "CONFIG_PATH", llm.CONFIG_PATH),
                      (llm, "_CACHE", llm._CACHE)]
        appetite.BASE = self.tmp
        appetite.STATE_PATH = self.tmp / "memory" / ".state" / "appetite.json"
        appetite.CONTRACT_MD = self.tmp / "memory" / "appetite.md"
        appetite.JOURNAL_DIR = self.tmp / "memory" / "journal"
        llm.USAGE_PATH = self.tmp / "memory" / ".state" / "usage.json"
        llm.CONFIG_PATH = self.tmp / "memory" / "llm.json"
        llm._CACHE = {"mtime": None, "cfg": None}
        llm.use_test_client(_NoCallClient())
        self.addCleanup(llm.clear_test_clients)

    def tearDown(self):
        for m, k, v in self._orig:
            setattr(m, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_usage(self, role="voice", t_in=0, t_out=0, calls=1, models=None):
        day = _dt.date.today().isoformat()
        rec = {"in": t_in, "out": t_out, "calls": calls, "fallback": 0}
        if models:
            rec["models"] = models
        llm.USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        llm.USAGE_PATH.write_text(json.dumps({day: {role: rec}}), encoding="utf-8")


class TestOwnerWord(Base):
    def test_default_free_no_hold(self):
        s = appetite.state()
        self.assertEqual(s["mode"], "free")
        self.assertIsNone(appetite.background_hold())
        self.assertIsNone(appetite.unacked_request())
        self.assertEqual(s["observed"]["provider_remaining"], "unknown")

    def test_pause_button_holds_until_interpreted(self):
        appetite.set_owner_request("pause_background", "", source="panel")
        hold = appetite.background_hold()
        self.assertIsNotNone(hold)
        self.assertIn("Егора", hold)              # помечено как ЕГО слово, класс 2
        self.assertIsNotNone(appetite.unacked_request())
        self.assertEqual(appetite.sleep_depth(), "skip")
        md = appetite.CONTRACT_MD.read_text(encoding="utf-8")
        self.assertIn("просьба Егора", md)

    def test_text_request_does_not_change_mode(self):
        appetite.set_owner_request("text", "давай поэкономнее на этой неделе", source="panel")
        self.assertEqual(appetite.state()["mode"], "free")   # код не толкует слова
        self.assertIsNone(appetite.background_hold())        # текст ≠ кнопка «останови фон»
        self.assertIsNotNone(appetite.unacked_request())

    def test_bad_kind_becomes_text(self):
        appetite.set_owner_request("explode", "что-то", source="panel")
        self.assertEqual(appetite.state()["owner_request"]["kind"], "text")


class TestHerHand(Base):
    def test_interpret_acks_and_sets_plan(self):
        appetite.set_owner_request("pause_background", "", source="panel")
        out = appetite.interpret("background_paused", "доделаю атомарное, новых не начинаю",
                                 windows=False, sleep_depth="skip", note="окна и сон откладываю")
        self.assertIn("background_paused", out)
        self.assertIsNone(appetite.unacked_request())        # просьба истолкована
        hold = appetite.background_hold()
        self.assertIn("моё принятое состояние", hold)        # теперь это ЕЁ состояние
        md = appetite.CONTRACT_MD.read_text(encoding="utf-8")
        self.assertIn("моё толкование", md)
        j = appetite.JOURNAL_DIR / f"{_dt.date.today().isoformat()}.md"
        self.assertIn("[аппетит]", j.read_text(encoding="utf-8"))

    def test_interpret_rejects_alien_mode(self):
        out = appetite.interpret("turbo", "нет такого")
        self.assertIn("не из договора", out)
        self.assertEqual(appetite.state()["mode"], "free")

    def test_considerate_defaults(self):
        appetite.interpret("considerate", "умеряю")
        self.assertEqual(appetite.sleep_depth(), "light")    # дефолт бережливого сна
        self.assertIsNone(appetite.background_hold())        # considerate ≠ пауза

    def test_window_plan_is_recorded_but_moves_nothing(self):
        """⚠ Было `test_windows_off_is_her_plan` + проверки considerate_hint. Обе функции
        читал только heartbeat.window_goal, а его — только _heartbeat_once, который не
        стоял ни в одной строке _clock_jobs(). Контур выброшен 25.07 по решению Егора.
        План остаётся наблюдаемой записью её намерения; рычагом он не был никогда."""
        appetite.interpret("considerate", "окна закрываю сама", windows=False)
        plan = (appetite.state().get("interpretation") or {}).get("plan") or {}
        self.assertIs(plan.get("windows"), False)
        self.assertFalse(hasattr(appetite, "windows_off"))
        self.assertFalse(hasattr(appetite, "considerate_hint"))

    def test_free_interpret_lifts_everything(self):
        appetite.set_owner_request("pause_background", "", source="panel")
        appetite.interpret("background_paused", "стоп фон")
        appetite.interpret("free", "Егор сказал не экономить")
        self.assertIsNone(appetite.background_hold())
        self.assertEqual(appetite.sleep_depth(), "full")
        plan = (appetite.state().get("interpretation") or {}).get("plan") or {}
        self.assertIsNot(plan.get("windows"), False)


class TestPromise(Base):
    def test_pledge_and_check_within(self):
        self._write_usage(t_in=300, t_out=100)
        out = appetite.pledge(daily_tokens=10_000, text="десяти тысяч хватит")
        self.assertIn("Обещание записано", out)
        self.assertEqual(appetite.state()["mode"], "pledged")
        self.assertIsNone(appetite.promise_check())          # 400 из 10к — в рамках
        self.assertIn("в рамках", appetite.promise_line())

    def test_check_warns_near_and_over(self):
        self._write_usage(t_in=900, t_out=0)
        appetite.pledge(daily_tokens=1000)
        self.assertIn("впритык", appetite.promise_check())
        self._write_usage(t_in=1500, t_out=0)
        self.assertIn("ПРЕВЫШЕНО", appetite.promise_check())
        self.assertIn("не тихий срыв", appetite.promise_line())
        self.assertIn("⚠", appetite.state_line())            # видно в STATE

    def test_pledge_clear(self):
        appetite.pledge(daily_tokens=1000)
        out = appetite.pledge(text="снимаем")
        self.assertIn("снято", out)
        self.assertIsNone(appetite.promise_check())
        self.assertEqual(appetite.state()["mode"], "free")


class TestObserved(Base):
    def test_provider_remaining_from_relay_snapshot(self):
        original = appetite.provider_limits
        appetite.provider_limits = lambda: {
            "schema": "relay.openai.limits.v1",
            "rate_limit": {
                "primary_window": {"remaining_percent": 75.0, "reset_after_seconds": 3660},
                "secondary_window": {"used_percent": 40.0, "reset_after_seconds": 90000},
            },
        }
        try:
            observed = appetite.observed_today()
        finally:
            appetite.provider_limits = original
        self.assertIn("5ч: 75.0%", observed["provider_remaining"])
        self.assertIn("неделя: 60.0%", observed["provider_remaining"])
        self.assertEqual(observed["provider_limits"]["schema"], "relay.openai.limits.v1")

    def test_cost_by_model(self):
        self._write_usage(t_in=1_000_000, t_out=0, models={
            "glm-5.2": {"in": 500_000, "out": 0, "calls": 1},
            "gpt-5.5": {"in": 500_000, "out": 0, "calls": 1}})
        orig = llm.pricing
        llm.pricing = lambda: {"glm-5.2": {"in_per_1m": 1.0, "out_per_1m": 2.0},
                               "gpt-5.5": {"in_per_1m": 3.0, "out_per_1m": 6.0}}
        try:
            self.assertAlmostEqual(appetite.estimated_cost_today(), 2.0)  # 0.5 + 1.5
        finally:
            llm.pricing = orig

    def test_cost_none_without_pricing(self):
        self._write_usage(t_in=1000)
        self.assertIsNone(appetite.estimated_cost_today())

    def test_usage_delta(self):
        self._write_usage(t_in=1000, t_out=500, calls=3)
        snap = appetite.usage_delta()
        self._write_usage(t_in=3000, t_out=500, calls=5)
        line = appetite.usage_delta(snap)
        self.assertIn("2.0к ток", line)
        self.assertIn("2 выз.", line)

    def test_usage_delta_marks_missing_tokens_and_real_provider_change(self):
        snapshots = iter([
            {"tokens_today": 0, "calls_today": 10, "estimated_cost_today": 0.0,
             "provider_limits": {"rate_limit": {
                 "primary_window": {"remaining_percent": 14.0},
                 "secondary_window": {"remaining_percent": 84.0}}}},
            {"tokens_today": 0, "calls_today": 19, "estimated_cost_today": 0.0,
             "provider_limits": {"rate_limit": {
                 "primary_window": {"remaining_percent": 12.0},
                 "secondary_window": {"remaining_percent": 84.0}}}},
        ])
        original = appetite.observed_today
        appetite.observed_today = lambda: next(snapshots)
        try:
            before = appetite.usage_delta()
            line = appetite.usage_delta(before)
        finally:
            appetite.observed_today = original
        self.assertIn("токены не сообщены relay (9 выз.)", line)
        self.assertIn("5ч: 14.0→12.0% остатка", line)
        self.assertIn("неделя: 84.0→84.0% остатка", line)
        self.assertNotIn("≈$0", line)

    def test_describe_names_live_relay_snapshot(self):
        original = appetite.provider_limits
        appetite.provider_limits = lambda: {
            "schema": "relay.openai.limits.v1",
            "rate_limit": {"primary_window": {"remaining_percent": 42.0}},
        }
        try:
            text = appetite.describe()
        finally:
            appetite.provider_limits = original
        self.assertIn("снимок relay", text)
        self.assertNotIn("честно: не виден", text)

    def test_state_line_flags_unacked(self):
        appetite.set_owner_request("considerate", "умерь аппетиты", source="panel")
        line = appetite.state_line()
        self.assertIn("не истолкована", line)
        self.assertIn("manage_appetite", line)

    def test_no_model_calls(self):
        appetite.describe()
        appetite.state_line()
        appetite.observed_today()                            # _NoCallClient молчит = ок


if __name__ == "__main__":
    unittest.main(verbosity=2)
