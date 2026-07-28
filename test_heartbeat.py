"""
Тесты автономного тика heartbeat (SPEC §4.2 + руки). Герметичны.

Запуск:  python praxis_test.py test_heartbeat -v
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

_fa = types.ModuleType("anthropic")
_fa.Anthropic = lambda **kw: None
sys.modules.setdefault("anthropic", _fa)
_fd = types.ModuleType("dotenv")
_fd.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _fd)

import memory_index as mi  # noqa: E402
import agent  # noqa: E402
import llm  # noqa: E402
import people  # noqa: E402
import heartbeat  # noqa: E402
import unanswered  # noqa: E402


class FakeResp:
    def __init__(self, text):
        self.stop_reason = "end_turn"
        self.content = [types.SimpleNamespace(type="text", text=text)]


class FakeClient:
    def __init__(self, reply="ок"):
        self.reply = reply
        self.last = {}

        class _M:
            def create(_s, **kw):
                self.last = kw
                return FakeResp(self.reply)
        self.messages = _M()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_hb_"))
        mem = self.tmp / "memory"
        soul = self.tmp / "soul"
        for d in (mem / "people", mem / "rooms", mem / "journal", mem / ".vectors", soul / "skills"):
            d.mkdir(parents=True, exist_ok=True)
        for n in ("SOUL", "self", "emotions", "being_with"):
            (soul / f"{n}.md").write_text(f"# {n}\nперсона.\n", encoding="utf-8")
        self._orig = []
        patch = {
            heartbeat: dict(BASE=self.tmp),
            agent: dict(BASE=self.tmp, SOUL_DIR=soul, SKILLS_DIR=soul / "skills", MEM_DIR=mem,
                        PEOPLE_DIR=mem / "people", ROOMS_DIR=mem / "rooms",
                        JOURNAL_DIR=mem / "journal", REFLECTIONS=mem / "reflections.md",
                        INDEX_MD=mem / "INDEX.md", WORKDIR=str(self.tmp / "workspace"),
                        _CURRENT_CHAT="stale", _CURRENT_HISTORY=["stale"]),
            people: dict(BASE=self.tmp, PEOPLE_DIR=mem / "people"),
            unanswered: dict(BASE=self.tmp, STATE_DIR=mem / ".state",
                             PATH=mem / ".state" / "unanswered.json"),
            mi: dict(BASE=self.tmp, MEM_DIR=mem, SOUL_DIR=soul, SKILLS_DIR=soul / "skills",
                     PEOPLE_DIR=mem / "people", VECTORS_DIR=mem / ".vectors",
                     INDEX_JSON=mem / ".vectors" / "index.json", INDEX_MD=mem / "INDEX.md"),
        }
        for module, attrs in patch.items():
            for k, val in attrs.items():
                self._orig.append((module, k, getattr(module, k)))
                setattr(module, k, val)
        mi._CACHE = {"mtime": None, "index": None}
        self._orig.append((mi, "embed", mi.embed))
        mi.embed = lambda t: (_ for _ in ()).throw(ConnectionError("no ollama"))
        self._env = {k: os.environ.get(k) for k in
                     ("PRAXIS_HEARTBEAT_HOURS", "PRAXIS_HEARTBEAT_MIN_AGE_DAYS", "PRAXIS_HEARTBEAT_MAX_AGE_DAYS",
                      "PRAXIS_TZ", "PRAXIS_TZ_OFFSET_H")}
        for k in self._env:
            os.environ.pop(k, None)

    def tearDown(self):
        for module, k, val in reversed(self._orig):
            setattr(module, k, val)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _loop(self, slug, text, days_ago):
        d = (dt.date.today() - dt.timedelta(days=days_ago)).isoformat()
        (people.PEOPLE_DIR / f"{slug}.md").write_text(
            f"# {slug}\n\n## Открытые нити\n- [ ] {text} _({d})_\n", encoding="utf-8")

    def _journal(self):
        return "".join(p.read_text(encoding="utf-8") for p in agent.JOURNAL_DIR.glob("*.md"))


class TestCandidates(Base):
    def test_ripeness_window(self):
        self._loop("свежая", "сегодня", 0)
        self._loop("маша", "собеседование", 3)
        self._loop("древняя", "давнее", 40)
        self.assertEqual([c["text"] for c in heartbeat.candidates()], ["собеседование"])

    def test_loops_context_lists_open(self):
        self._loop("маша", "собеседование", 3)
        ctx = heartbeat._loops_context()
        self.assertIn("маша", ctx)
        self.assertIn("собеседование", ctx)


# ⚠ Отсюда удалён класс TestWindowGoal: он целиком проверял
# heartbeat.window_goal/record_decision/mark_window/WINDOW_DECIDE_SYS — контур,
# который не вызывался в проде ниоткуда и выброшен 25.07 по решению Егора.
# Стоит запомнить цифру: мёртвый путь был покрыт дюжиной зелёных тестов.
# Покрытие говорило о том, что код работает, а не о том, что он вызывается.

class TestHeartbeatTurn(Base):
    def test_tick_has_owner_tools_and_frame(self):
        fc = FakeClient("навела порядок, Егору сказать нечего")
        llm.use_test_client(fc)
        try:
            out = agent.heartbeat_turn("Открытые нити:\n- маша: собеседование")
        finally:
            llm.clear_test_clients()
        self.assertEqual(out, "навела порядок, Егору сказать нечего")
        names = [t["name"] for t in fc.last.get("tools", [])]
        self.assertIn("shell", names, "у тика должны быть руки (owner-тулы)")
        self.assertIn("write_skill", names)
        self.assertTrue({"manage_room", "telegram_account", "computer"}.issubset(names))
        self.assertTrue({"admit", "computer_access"}.isdisjoint(names),
                        "Praxis-self не должна делегировать права другим людям")
        sysp = fc.last.get("system", "")
        sysp = "".join(b["text"] for b in sysp) if isinstance(sysp, list) else sysp
        self.assertIn("own initiative window", sysp, "нет рамки автономного времени")
        # стейл-контекст сброшен
        self.assertIsNone(agent._CURRENT_CHAT)
        self.assertIsNone(agent._CURRENT_HISTORY)

    def test_no_client_returns_empty(self):
        llm.use_test_client(None)
        try:
            self.assertEqual(agent.heartbeat_turn("ctx"), "")
        finally:
            llm.clear_test_clients()


class TestTaskWindow(Base):
    def test_window_has_hands_and_frame(self):
        fc = FakeClient("навела порядок в ориентации, перезапускаюсь")
        llm.use_test_client(fc)
        try:
            out = agent.task_window("путаюсь, где мои файлы")
        finally:
            llm.clear_test_clients()
        self.assertIn("ориентац", out)
        names = [t["name"] for t in fc.last.get("tools", [])]
        self.assertIn("shell", names, "в окне должны быть руки")
        self.assertIn("restart_self", names)
        self.assertTrue({"manage_room", "telegram_account", "computer"}.issubset(names))
        self.assertTrue({"admit", "computer_access"}.isdisjoint(names))
        sysp = fc.last.get("system", "")
        sysp = "".join(b["text"] for b in sysp) if isinstance(sysp, list) else sysp
        # ⚠ Было assertIn("Telegram remains online"). Единственный вызывающий
        # task_window доходит до него строго после client.disconnect(), так что тест
        # требовал от промпта ложного факта. Проверяем то, ради чего он писался, —
        # что рамка фонового run на месте, — но по правдивому признаку.
        self.assertIn("This is your own work run", sysp, "нет рамки фонового run")
        self.assertIn("Telegram is disconnected", sysp,
                      "рамка обязана называть транспорт как есть")
        self.assertNotIn("Telegram remains online", sysp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
