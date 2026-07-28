"""PASS 21 contract: рычаги восприятия живьём, причины пропуска, оспоримая дисциплина."""
from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import heartbeat
import perception


class Pass21Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_p21_"))
        mem = self.tmp / "memory"
        (mem / ".state").mkdir(parents=True)
        (mem / "journal").mkdir(parents=True)
        self._orig = []

        def patch(module, **attrs):
            for key, val in attrs.items():
                self._orig.append((module, key, getattr(module, key)))
                setattr(module, key, val)

        self.patch = patch
        patch(perception, BASE=self.tmp,
              STATE_PATH=mem / ".state" / "perception.json",
              SKIPS_PATH=mem / ".state" / "perception_skips.jsonl",
              JOURNAL_DIR=mem / "journal")
        patch(heartbeat, BASE=self.tmp,
              DECISIONS_PATH=mem / ".state" / "window_decisions.json")
        perception._CACHE.update(mtime=None, data={})
        perception._LAST_SKIP.clear()
        perception._AMBIENT.clear()
        perception._AMBIENT_FLUSHED["ts"] = 0.0
        self._env = {}
        self.mem = mem

    def tearDown(self):
        for module, key, val in reversed(self._orig):
            setattr(module, key, val)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        perception._CACHE.update(mtime=None, data={})

    def setenv(self, key, val):
        self._env.setdefault(key, os.environ.get(key))
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


class KnobTests(Pass21Base):
    def test_default_env_override_chain(self):
        self.setenv("PRAXIS_COOLDOWN_DM", None)
        self.assertEqual(perception.value("cooldown_dm"), 8.0)
        self.assertEqual(perception.source_of("cooldown_dm"), "default")
        self.setenv("PRAXIS_COOLDOWN_DM", "15")
        self.assertEqual(perception.value("cooldown_dm"), 15.0)
        self.assertEqual(perception.source_of("cooldown_dm"), "env")
        res = perception.set_knob("cooldown_dm", "30", by="praxis", reason="хочу медленнее")
        self.assertTrue(res["ok"], res)
        self.assertEqual(perception.value("cooldown_dm"), 30.0)
        self.assertEqual(perception.source_of("cooldown_dm"), "praxis")
        # живое чтение: второй «процесс» (сброшенный кэш) видит то же
        perception._CACHE.update(mtime=None, data={})
        self.assertEqual(perception.value("cooldown_dm"), 30.0)
        res = perception.reset_knob("cooldown_dm")
        self.assertTrue(res["ok"])
        self.assertEqual(perception.value("cooldown_dm"), 15.0)

    def test_bounds_are_physics(self):
        res = perception.set_knob("debounce_sec", "9999")
        self.assertFalse(res["ok"])
        self.assertIn("границ", res["error"])
        self.assertFalse(perception.set_knob("cooldown_dm", "абракадабра")["ok"])
        self.assertFalse(perception.set_knob("несуществующий", "1")["ok"])

    def test_retired_quiet_hours_are_not_a_hidden_gate(self):
        result = perception.set_knob("quiet_hours", "22-6", by="egor")
        self.assertFalse(result["ok"])
        self.assertNotIn("quiet_hours", perception.KNOBS)

    def test_set_journals_and_spines(self):
        import memory_life as life
        self.patch(life, BASE=self.tmp, LIFE_DIR=self.mem / "life",
                   EVENTS_DIR=self.mem / "life" / "events",
                   STATE_DIR=self.mem / ".state" / "life")
        (self.mem / "life" / "events").mkdir(parents=True)
        perception.set_knob("group_ack_max_len", "0", reason="имя не шум")
        j = (self.mem / "journal" / f"{_dt.date.today().isoformat()}.md").read_text(encoding="utf-8")
        self.assertIn("group_ack_max_len", j)
        evs = []
        for p in (self.mem / "life" / "events").glob("*.jsonl"):
            evs += [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(evs[0]["kind"], "perception_change")

    def test_effective_table(self):
        rows = {r["knob"]: r for r in perception.effective()}
        self.assertEqual(set(rows), set(perception.KNOBS))
        self.assertIn("bounds", rows["cooldown_dm"])
        self.assertNotIn("quiet_hours", rows)


class SkipTests(Pass21Base):
    def test_note_skip_classes_and_coalesce(self):
        perception.note_skip("allowlist", "запретил_егор", chat_id="-100")
        for _ in range(5):  # повторы в окне схлопываются
            perception.note_skip("allowlist", "запретил_егор", chat_id="-100")
        perception.note_skip("reflex", "не_сочла_важным", chat_id="77", detail="[Стикер]")
        recs = perception.recent_skips(10)
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["class"], "запретил_егор")
        today = perception.skips_today()
        self.assertEqual(today["не_сочла_важным"], 1)
        self.assertGreaterEqual(today["запретил_егор"], 1)
        self.assertIn("Стикер", perception.skips_text())

    def test_skip_meta_is_allowlisted_and_visible(self):
        perception.note_skip(
            "room_mode", "запретил_егор", chat_id="-100", detail="режим frozen",
            meta={
                "mode": "frozen",
                "mode_set_by": "Praxis",
                "mode_reason": "слишком шумно",
                "mode_until": "2026-07-16T12:00:00+00:00",
                "secret": "не сохранять",
            },
        )
        rec = perception.recent_skips(1)[0]
        self.assertEqual(rec["meta"]["mode_set_by"], "Praxis")
        self.assertNotIn("secret", rec["meta"])
        text = perception.skips_text()
        self.assertIn("задал Praxis", text)
        self.assertIn("причина: слишком шумно", text)
        self.assertIn("до 2026-07-16T12:00:00+00:00", text)
        self.assertNotIn("задал Praxis", perception.skips_text(include_provenance=False))

    def test_changed_provenance_is_not_coalesced(self):
        perception.note_skip("room_mode", "запретил_егор", chat_id="-100",
                             detail="режим frozen", meta={"mode_reason": "первая"})
        perception.note_skip("room_mode", "запретил_егор", chat_id="-100",
                             detail="режим frozen", meta={"mode_reason": "вторая"})
        self.assertEqual(len(perception.recent_skips(10)), 2)

    def test_unknown_class_maps_to_blind(self):
        perception.note_skip("x", "неизвестный_класс")
        self.assertEqual(perception.recent_skips(1)[0]["class"], "не_увидела")

    def test_ring_compacts(self):
        perception._LAST_SKIP.clear()
        for i in range(2 * perception.SKIPS_KEEP + 5):
            perception.note_skip(f"s{i}", "отложила")  # разные stage — без схлопывания
        lines = perception.SKIPS_PATH.read_text(encoding="utf-8").splitlines()
        self.assertLess(len(lines), 2 * perception.SKIPS_KEEP)

    def test_ambient_counter_flushes(self):
        for _ in range(3):
            perception.note_ambient("-200")
        self.assertEqual(perception.ambient_today().get("-200"), 3)
        # персист не чаще раза в минуту, но первый флаш уже случился (ts=0 в setUp)
        perception._CACHE.update(mtime=None, data={})
        disk = (perception._state().get("ambient") or {})
        self.assertIn(_dt.date.today().isoformat(), disk)

    def test_never_raises(self):
        blocker = self.tmp / "not-a-directory"
        blocker.write_text("x", encoding="utf-8")
        self.patch(perception, SKIPS_PATH=blocker / "x.jsonl")
        perception.note_skip("s", "отложила")  # не должно бросить


def _import_runner():
    """Импорт mtproto_runner без живого Telethon (паттерн test_group_wake_snapshot)."""
    import sys
    import types
    if "mtproto_runner" in sys.modules:
        return sys.modules["mtproto_runner"]

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def on(self, *a, **k):
            return lambda fn: fn

    fake = types.ModuleType("telethon")
    fake.TelegramClient = _FakeClient
    fake.events = types.SimpleNamespace(NewMessage=lambda *a, **k: object(),
                                        ChatAction=lambda *a, **k: object())
    prior = sys.modules.get("telethon")
    sys.modules["telethon"] = fake
    try:
        import mtproto_runner as mr
        return mr
    finally:
        if prior is None:
            sys.modules.pop("telethon", None)
        else:
            sys.modules["telethon"] = prior


class WiringTests(Pass21Base):
    def test_runner_records_frozen_room_provenance(self):
        mr = _import_runner()
        captured = []
        self.patch(mr.rooms, profile_read=lambda peer_id: {
            "mode": "frozen", "mode_set_by": "praxis",
            "mode_reason": "слишком шумно", "mode_until": "2026-07-16T12:00:00+00:00",
        })
        self.patch(mr.perception, note_skip=lambda *args, **kwargs: captured.append((args, kwargs)))
        mr._note_room_mode_skip("-100", "-100", "frozen", stage="frozen")
        # ⚠ Здесь ожидался класс «запретил_егор» при mode_set_by == "praxis" — то есть
        # её собственная граница докладывалась ей как чужой запрет. Это единственный в
        # дереве тест, пиннивший выбор класса, и он пиннил подмену авторства.
        self.assertEqual(captured[0][0], ("frozen", "мой_ритм"))
        self.assertEqual(captured[0][1]["meta"]["mode_set_by"], "praxis")
        self.assertEqual(captured[0][1]["meta"]["mode_reason"], "слишком шумно")

    def test_runner_marks_owner_set_mode_as_his(self):
        """Зеркало предыдущего: без него схлопывание выбора в константу «мой_ритм»
        прошло бы незамеченным."""
        mr = _import_runner()
        captured = []
        self.patch(mr.rooms, profile_read=lambda peer_id: {
            "mode": "frozen", "mode_set_by": "owner", "mode_reason": "просил тишины",
        })
        self.patch(mr.perception, note_skip=lambda *args, **kwargs: captured.append((args, kwargs)))
        mr._note_room_mode_skip("-100", "-100", "frozen", stage="frozen")
        self.assertEqual(captured[0][0], ("frozen", "запретил_егор"))

    def test_runner_cooldown_uses_perception(self):
        mr = _import_runner()
        perception.set_knob("cooldown_dm", "42")
        perception.set_knob("cooldown_group", "600")
        perception.set_knob("cooldown_addressed", "180")
        self.assertEqual(mr._cooldown(True), 42.0)
        self.assertEqual(mr._cooldown(False, "normal"), 600.0)
        self.assertEqual(mr._cooldown(False, "normal", addressed=True), 180.0)
        self.assertEqual(mr._cooldown(False, "quiet"), 1200.0)
        self.assertEqual(mr._cooldown(False, "quiet", addressed=True), 360.0)

    def test_reflex_ack_len_knob(self):
        import reflex
        self.assertEqual(reflex.triage("коротко же", is_private=False), "maybe")
        perception.set_knob("group_ack_max_len", "20", reason="сама выбрала короткий фильтр")
        self.assertEqual(reflex.triage("коротко же", is_private=False), "ignore")
        perception.set_knob("group_ack_max_len", "0", reason="по длине не режу")
        self.assertEqual(reflex.triage("коротко же", is_private=False), "maybe")

    def test_unanswered_min_age_follows(self):
        import unanswered
        perception.set_knob("cooldown_dm", "120")
        self.assertEqual(unanswered._min_age(), 120.0)

    def test_manage_loop_ratchet_contestable(self):
        import agent
        import people
        self.patch(people, park_count=lambda slug, match: 3,
                   park_loop=lambda slug, match, u: True,
                   path_for=lambda slug: Path(__file__))
        import graph
        self.patch(graph, resolve=lambda name: "test-person")
        self.patch(agent, _reindex=lambda p: None,
                   tool_journal=lambda s: "ok")
        out = agent.tool_manage_loop("park", "test", match="нить")
        self.assertIn("force=true", out)
        out = agent.tool_manage_loop("park", "test", match="нить", force=True,
                                     reason="жду ответа поставщика, это не жвачка")
        self.assertIn("Запарковала", out)

    def test_tool_scope_guard(self):
        import agent
        import rails
        self.patch(rails, DENIALS_PATH=self.mem / ".state" / "denials.jsonl")
        self.patch(agent, _CURRENT_SCOPE="group")
        out = agent.tool_manage_perception("set", knob="cooldown_dm", value="1")
        self.assertIn("owner-скоуп", out)
        self.patch(agent, _CURRENT_SCOPE="owner")
        out = agent.tool_manage_perception("set", knob="cooldown_dm", value="20",
                                           reason="быстрее с Егором")
        self.assertIn("живо", out)
        self.assertIn("рычаг", agent.tool_manage_perception("list").lower())
        self.assertIn("пропуск", agent.tool_manage_perception("skips").lower())

    def test_skip_tool_hides_cross_room_provenance(self):
        import agent
        perception.note_skip("room_mode", "запретил_егор", chat_id="-100",
                             detail="режим frozen",
                             meta={"mode_set_by": "owner", "mode_reason": "личная причина"})
        perception.note_skip("reflex", "не_сочла_важным", chat_id="-200", detail="шум")
        self.patch(agent, _CURRENT_SCOPE="group", _CURRENT_CHAT="-100")
        out = agent.tool_manage_perception("skips")
        self.assertIn("режим frozen", out)
        self.assertNotIn("личная причина", out)
        self.assertNotIn("шум", out)

    def test_retired_quiet_hours_env_has_no_gate_left(self):
        """⚠ Было `test_heartbeat_ignores_retired_quiet_hours_env` через
        `heartbeat.window_goal()` — путь, который в проде не вызывался ниоткуда и
        выброшен 25.07. Предмет теста (снятые PRAXIS_QUIET_HOURS не должны ничего
        гейтить) остаётся в силе и проверяется прямо: гейта нет ни в коде, ни в
        манифесте — а тот единственный, что есть, окно сна, назван честно."""
        import heartbeat
        import rails
        self.setenv("PRAXIS_QUIET_HOURS", "0-23")
        self.assertFalse(hasattr(heartbeat, "window_goal"))
        runner_src = (Path(_import_runner().__file__)).read_text(encoding="utf-8")
        self.assertNotIn("PRAXIS_QUIET_HOURS", runner_src)
        row = next((r for r in rails.registry()
                    if isinstance(r, dict) and r.get("id") == "window_gates"), None)
        self.assertIsNotNone(row)
        self.assertIn("окно сна", str(row), "единственное вето названо в манифесте")

    def test_state_line(self):
        self.assertEqual(perception.state_line(), "")
        perception.set_knob("debounce_sec", "2")
        perception.note_skip("frozen", "запретил_егор", chat_id="-1")
        line = perception.state_line()
        self.assertIn("переопределено 1", line)
        self.assertIn("запретил_егор", line)


if __name__ == "__main__":
    unittest.main()
