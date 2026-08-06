"""
Регрессии раннего Telegram-контура: буфер-персист (#1), _named/addressed (#2),
read_chat/read_context (#3,#4), компакт-субагент + сводка сверху (#6), калибровка тишины (#7).
Герметичны: anthropic/dotenv застаблены, пути ядра перенаправлены в tmp.

Запуск:  python praxis_test.py test_pass2 -v
"""
from __future__ import annotations

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

import agent  # noqa: E402
import bufstore  # noqa: E402
import llm  # noqa: E402
import people as pe  # noqa: E402
import memory_index as mi  # noqa: E402


class FakeResp:
    def __init__(self, text):
        self.stop_reason = "end_turn"
        self.content = [types.SimpleNamespace(type="text", text=text)]


class ScriptedClient:
    """Захватывает kwargs каждого create(); отдаёт заранее заданные ответы."""
    def __init__(self, replies=None):
        self._r = list(replies or [])
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return FakeResp(self._r.pop(0) if self._r else "")


def _sys_text(system):
    if isinstance(system, list):
        return "".join(b.get("text", "") for b in system)
    return system or ""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_p2_"))
        mem = self.tmp / "memory"
        soul = self.tmp / "soul"
        for d in (mem / "people", mem / "rooms", mem / "journal", mem / ".summaries",
                  mem / ".buffers", soul / "skills"):
            d.mkdir(parents=True, exist_ok=True)
        (soul / "SOUL.md").write_text("# Конституция\n\n## Кто я по характеру\nтёплая.\n", encoding="utf-8")
        for n in ("self", "emotions", "being_with"):
            (soul / f"{n}.md").write_text(f"# {n}\n", encoding="utf-8")
        self._orig = []
        patch = {
            agent: dict(BASE=self.tmp, SOUL_DIR=soul, SKILLS_DIR=soul / "skills", MEM_DIR=mem,
                        PEOPLE_DIR=mem / "people", ROOMS_DIR=mem / "rooms",
                        JOURNAL_DIR=mem / "journal", REFLECTIONS=mem / "reflections.md",
                        INDEX_MD=mem / "INDEX.md", SUMMARIES_DIR=mem / ".summaries", _TELETHON={},
                        _CURRENT_CHAT=None),
            bufstore: dict(BASE=self.tmp, BUF_DIR=mem / ".buffers"),
            pe: dict(BASE=self.tmp, PEOPLE_DIR=mem / "people"),
            mi: dict(BASE=self.tmp, MEM_DIR=mem, SOUL_DIR=soul, SKILLS_DIR=soul / "skills",
                     PEOPLE_DIR=mem / "people", VECTORS_DIR=mem / ".vectors",
                     INDEX_JSON=mem / ".vectors" / "index.json", INDEX_MD=mem / "INDEX.md"),
        }
        for module, attrs in patch.items():
            for k, val in attrs.items():
                self._orig.append((module, k, getattr(module, k)))
                setattr(module, k, val)
        self._env = {k: os.environ.get(k) for k in
                     ("PRAXIS_CONSOLIDATE_NUDGE", "PRAXIS_PROMPT_CACHE",
                      "PRAXIS_WEB_SEARCH")}
        for k in self._env:
            os.environ.pop(k, None)
        llm.clear_test_clients()
        self.addCleanup(llm.clear_test_clients)

    def tearDown(self):
        for module, k, val in reversed(self._orig):
            setattr(module, k, val)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)


# ----------------------------------------------------------------------- #1
class TestBufferPersist(Base):
    def test_roundtrip(self):
        bufstore.save("123456789", ["Егор: привет", "Praxis: привет"])
        self.assertEqual(bufstore.load("123456789"), ["Егор: привет", "Praxis: привет"])

    def test_load_all_keys_match_chat_ids(self):
        bufstore.save("123456789", ["a: 1"])
        bufstore.save("-1001234567", ["b: 2"])  # групповой id с минусом
        allb = bufstore.load_all()
        self.assertEqual(set(allb), {"123456789", "-1001234567"})
        self.assertEqual(allb["-1001234567"], ["b: 2"])

    def test_missing_is_empty(self):
        self.assertEqual(bufstore.load("nope"), [])

    def test_corrupt_is_empty(self):
        bufstore.path_for("x").write_text("{not json", encoding="utf-8")
        self.assertEqual(bufstore.load("x"), [])


# ----------------------------------------------------------------------- #2
class TestNamed(Base):
    def test_named_variants(self):
        for t in ("эй, praxis, ты тут?", "Пракс, глянь", "@praxis_intelligence привет",
                  "@praxisintelligence ау", "ПРАКСИС!", "ну шо, Праксис?",
                  "(Пракс)", "Praxis—глянь"):
            self.assertTrue(agent._named(t), t)

    def test_not_named(self):
        for t in ("просто болтаем", "practice makes perfect", "praxis_test.py",
                  "метапраксис", "праксический", "@notpraxis", ""):
            self.assertFalse(agent._named(t), t)


# ----------------------------------------------------------------------- #3,#4
class TestReadTools(Base):
    def test_read_chat_unavailable(self):
        self.assertIn("недоступно", agent.tool_read_chat("Маша").lower())

    def test_read_chat_delegates(self):
        agent._TELETHON["read_chat"] = lambda ref, limit: f"got {ref} {limit}"
        out = agent.tool_read_chat("Маша", 10)
        self.assertIn("PRIVATE CROSS-CHAT READ", out)
        self.assertIn("got Маша 10", out)

    def test_read_context_needs_current_chat(self):
        agent._TELETHON["fetch_context"] = lambda cid, limit: f"ctx {cid} {limit}"
        agent._CURRENT_CHAT = None
        self.assertIn("не вижу", agent.tool_read_context().lower())
        agent._CURRENT_CHAT = "777"
        self.assertEqual(agent.tool_read_context(20), "ctx 777 20")

    def test_read_tools_are_shared_perception_not_owner_shell(self):
        self.assertIn("read_chat", agent.TOOL_IMPL)
        self.assertIn("read_context", agent.TOOL_IMPL)
        shared = {t.get("name") for t in agent.SHARED_CONTEXT_TOOLS}
        self.assertIn("read_chat", shared)
        self.assertIn("read_context", shared)
        self.assertIn("search_private_messages", shared)
        self.assertIn("inbox_read", shared)
        owner = {t.get("name") for t in agent.OWNER_TOOLS}
        self.assertNotIn("read_chat", owner, "shared tool must not be duplicated in API schema")
        self.assertNotIn("shell", shared)

    def test_private_message_search_delegates(self):
        agent._TELETHON["search_private_messages"] = lambda query, limit: f"{query}:{limit}"
        self.assertEqual(agent.tool_search_private_messages("срок", 7), "срок:7")


# ----------------------------------------------------------------------- #6
class TestCompact(Base):
    def test_uses_evaluator_channel_not_voice_no_tools(self):
        # герметично: свои модели ролей (общий sandbox-конфиг могли трогать другие тесты)
        prev = llm.snapshot()
        llm.update_config({"roles": {"voice": {"model": "p2-voice"},
                                     "evaluator": {"model": "p2-eval"}}})
        self.addCleanup(lambda: llm.update_config(
            {"roles": {"voice": {"model": prev["voice"]["model"]},
                       "evaluator": {"model": prev["evaluator"]["model"]}}}))
        sc = ScriptedClient(["сводка: договорились про деплой"])
        llm.use_test_client(sc)
        out = agent.compact("777", ["Егор: давай задеплоим", "Praxis: ок"])
        self.assertIn("деплой", out)
        self.assertEqual(sc.calls[0]["model"], "p2-eval")   # канал оценщика, не голос
        self.assertNotIn("tools", sc.calls[0])  # компакт без инструментов

    def test_writes_and_merges_running_summary(self):
        agent.write_summary("777", "ранее: познакомились")
        sc = ScriptedClient(["ранее: познакомились; затем: решили деплой"])
        llm.use_test_client(sc)
        agent.compact("777", ["Егор: деплоим"])
        # прежняя сводка ушла в подсказку модели
        sent = sc.calls[0]["messages"][0]["content"]
        self.assertIn("ранее: познакомились", sent)
        # новая сводка записана на диск
        self.assertIn("деплой", agent.read_summary("777"))

    def test_summary_injected_on_top(self):
        agent.write_summary("777", "СВОДКА-МАРКЕР раньше тут было важное")
        _persona, dynamic, evidence = agent._build_prompt_parts(
            speaker=None, chat_id="777")
        self.assertNotIn("СВОДКА-МАРКЕР", dynamic)
        # ⚠ Ярлык тира приезжает в кадр ЗАГОЛОВКОМ секции — капсом, между правилами
        # `────`. Литерал ярлыка в кадре больше не встречается, и это не пропажа:
        # заголовок виден сильнее прежней JSON-строки.
        self.assertIn("РАНЕЕ В ЭТОМ ДИАЛОГЕ", evidence)
        self.assertIn("СВОДКА-МАРКЕР", evidence)

    def test_compact_empty_lines_noop(self):
        sc = ScriptedClient(["не должно вызваться"])
        llm.use_test_client(sc)
        agent.compact("777", [])
        self.assertEqual(sc.calls, [])


# ----------------------------------------------------------------------- #6 (nudge demote)
class TestConsolidateNudgeDemoted(Base):
    def _voice_system(self):
        sc = ScriptedClient(["ответ"])
        llm.use_test_client(sc)
        hist = []
        for i in range(agent.CONSOLIDATE_AT + 2):
            hist.append({"role": "user", "content": f"m{i}"})
            hist.append({"role": "assistant", "content": f"a{i}"})
        agent._voice("ещё", hist, speaker="Егор", chat_id="777", is_owner=True)
        return _sys_text(sc.calls[0]["system"])

    def test_no_nudge_by_default(self):
        self.assertNotIn("Context is almost full", self._voice_system())

    def test_nudge_when_enabled(self):
        os.environ["PRAXIS_CONSOLIDATE_NUDGE"] = "1"
        self.assertIn("Context is almost full", self._voice_system())


# ----------------------------------------------------------------------- #7
class TestTalkativeness(Base):
    def test_perceive_gone_presence_frame_lives(self):
        # PASS 8.1: привратник снесён — вместо PERCEIVE_INSTRUCTION живёт presence-фрейм голоса
        self.assertFalse(hasattr(agent, "PERCEIVE_INSTRUCTION"))
        self.assertFalse(hasattr(agent, "perceive"))
        self.assertIn("[молчу]", agent._GROUP_PRESENCE_FRAME)
        self.assertIn("live group", agent._GROUP_PRESENCE_FRAME)

    def test_private_tail_has_grounding_without_style_coercion(self):
        _, private = agent.build_system_parts(speaker="Егор", chat_id="777")
        _, public = agent.build_system_parts(
            speaker="Егор", ctx=agent.ChannelContext(chat_id="-777", is_dm=False),
        )
        self.assertIn("Operational continuity", private)
        self.assertNotIn("no bureaucratese", private)
        self.assertNotIn("Never narrate inner feelings", private)
        self.assertNotIn("Don't meta-comment", private)
        self.assertIn("whether to speak are yours", private)
        self.assertNotIn("no bureaucratese", public)
        self.assertNotIn("без эссе и списков", public)

    def test_family_and_unknown_dm_frames_are_authority_facts_not_style_orders(self):
        _, family = agent.build_system_parts(
            speaker="Мама", ctx=agent.ChannelContext(
                chat_id="1", is_dm=True, known=True, family=True,
            ),
        )
        _, unknown = agent.build_system_parts(
            speaker="Новый", ctx=agent.ChannelContext(
                chat_id="2", is_dm=True, known=False,
            ),
        )
        self.assertIn("owner-assigned FAMILY role", family)
        self.assertNotIn("Full warmth", family)
        self.assertIn("has no delegated owner authority", unknown)
        self.assertNotIn("Be warm", unknown)


if __name__ == "__main__":
    unittest.main(verbosity=2)
