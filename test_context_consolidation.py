"""
Тесты автономной консолидации контекста (спека Praxis). Герметичны.

Запуск:  python praxis_test.py test_context_consolidation -v
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import types
import os
import unittest
from pathlib import Path

_fa = types.ModuleType("anthropic")
_fa.Anthropic = lambda **kw: None
sys.modules.setdefault("anthropic", _fa)
_fd = types.ModuleType("dotenv")
_fd.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _fd)

import memory_index as mi  # noqa: E402
import people as pe  # noqa: E402
import agent  # noqa: E402
import llm  # noqa: E402


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
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_cc_"))
        mem = self.tmp / "memory"
        soul = self.tmp / "soul"
        for d in (mem / "people", mem / "rooms", mem / "journal", mem / ".vectors", soul / "skills"):
            d.mkdir(parents=True, exist_ok=True)
        for n in ("SOUL", "self", "emotions", "being_with"):
            (soul / f"{n}.md").write_text(f"# {n}\nперсона.\n", encoding="utf-8")
        self._orig = []
        patch = {
            agent: dict(BASE=self.tmp, SOUL_DIR=soul, SKILLS_DIR=soul / "skills", MEM_DIR=mem,
                        PEOPLE_DIR=mem / "people", ROOMS_DIR=mem / "rooms",
                        JOURNAL_DIR=mem / "journal", REFLECTIONS=mem / "reflections.md",
                        INDEX_MD=mem / "INDEX.md", WORKDIR=str(self.tmp / "workspace"),
                        _CURRENT_CHAT=None, _CURRENT_HISTORY=None),
            pe: dict(BASE=self.tmp, PEOPLE_DIR=mem / "people"),
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

    def tearDown(self):
        for module, k, val in reversed(self._orig):
            setattr(module, k, val)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _journal(self):
        return "".join(p.read_text(encoding="utf-8") for p in agent.JOURNAL_DIR.glob("*.md"))

    def _hist(self, n):
        return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"сообщение {i}"} for i in range(n)]


class TestConfig(Base):
    def test_history_turns_raised(self):
        """Умолчание хвоста — 100, и это проверяется как УМОЛЧАНИЕ.

        ⚠ Константы считаются на импорте `agent` из PRAXIS_HISTORY_TURNS, поэтому до
        03.08.2026 тест утверждал не умолчание, а то, что было выставлено на сервере.
        Стенд теперь снимает переменную до первого импорта (_standenv), так что здесь
        действительно проверяется значение из кода.
        """
        self.assertIsNone(os.environ.get("PRAXIS_HISTORY_TURNS"),
                          "порог задан средой — тест проверял бы не умолчание")
        self.assertEqual(agent.HISTORY_TURNS, 100)
        self.assertEqual(agent.CONSOLIDATE_AT, 90)

    def test_tool_registered(self):
        names = [t["name"] for t in agent.BASE_TOOLS]
        self.assertIn("consolidate_context", names)
        self.assertIn("consolidate_context", agent.TOOL_IMPL)


class TestConsolidateTool(Base):
    def test_fold_with_note(self):
        hist = self._hist(26)
        agent._CURRENT_HISTORY = hist
        out = agent.tool_consolidate_context("итог: договорились созвониться в пятницу")
        self.assertEqual(len(hist), 26 - agent.CONSOLIDATE_FOLD, "история не обрезана на месте")
        j = self._journal()
        self.assertIn("[контекст]", j)
        self.assertIn("договорились созвониться", j)
        self.assertIn("освобождён", out)

    def test_fold_without_note_uses_summary(self):
        orig = agent._summarize_history
        agent._summarize_history = lambda recs: "сжатая суть уходящего"
        try:
            hist = self._hist(20)
            agent._CURRENT_HISTORY = hist
            agent.tool_consolidate_context()
            self.assertEqual(len(hist), 20 - agent.CONSOLIDATE_FOLD)
            self.assertIn("сжатая суть уходящего", self._journal())
        finally:
            agent._summarize_history = orig

    def test_short_history_noop(self):
        hist = self._hist(5)
        agent._CURRENT_HISTORY = hist
        out = agent.tool_consolidate_context("неважно")
        self.assertEqual(len(hist), 5, "короткую историю не трогаем")
        self.assertEqual(self._journal(), "")
        self.assertIn("рано", out.lower())

    def test_keeps_tail_alive(self):
        hist = self._hist(10)  # fold = min(12, 10-6)=4
        agent._CURRENT_HISTORY = hist
        agent.tool_consolidate_context("суть")
        self.assertEqual(len(hist), 6, "должен оставить живой хвост")


class TestNudge(Base):
    def _capture_system(self, history):
        fc = FakeClient()
        llm.use_test_client(fc)
        try:
            agent.respond("привет", history, "Егор", force_voice=True)
        finally:
            llm.clear_test_clients()
        s = fc.last.get("system", "")
        return "".join(b["text"] for b in s) if isinstance(s, list) else s

    def test_nudge_when_near_full(self):
        # §6 пакета 2: авто-подсказка теперь за флагом (компактирование берёт субагент);
        # как ручной override она по-прежнему работает.
        import os
        prev = os.environ.get("PRAXIS_CONSOLIDATE_NUDGE")
        os.environ["PRAXIS_CONSOLIDATE_NUDGE"] = "1"
        try:
            sysp = self._capture_system(self._hist(agent.CONSOLIDATE_AT))
        finally:
            if prev is None:
                os.environ.pop("PRAXIS_CONSOLIDATE_NUDGE", None)
            else:
                os.environ["PRAXIS_CONSOLIDATE_NUDGE"] = prev
        self.assertIn("almost full", sysp)
        self.assertIn("consolidate_context", sysp)

    def test_nudge_off_by_default(self):
        # по умолчанию авто-подсказки нет — компактирование идёт субагентом, не её голосом
        sysp = self._capture_system(self._hist(agent.CONSOLIDATE_AT))
        self.assertNotIn("almost full", sysp)

    def test_no_nudge_when_short(self):
        sysp = self._capture_system(self._hist(5))
        self.assertNotIn("almost full", sysp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
