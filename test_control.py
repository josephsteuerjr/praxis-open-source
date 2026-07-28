"""
Тесты пульта (Слой 6): stay_silent / freeze_chat / panic + bootguard-простой. Герметичны.

Запуск:  python praxis_test.py test_control -v
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

_fa = types.ModuleType("anthropic")
_fa.Anthropic = lambda **kw: None
sys.modules.setdefault("anthropic", _fa)
_fd = types.ModuleType("dotenv")
_fd.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _fd)

import memory_index as mi  # noqa: E402
import people as pe  # noqa: E402
import rooms  # noqa: E402
import bootguard as bg  # noqa: E402
import agent  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_ctl_"))
        mem = self.tmp / "memory"
        for d in (mem / "people", mem / "journal", mem / ".vectors"):
            d.mkdir(parents=True, exist_ok=True)
        self._orig = []
        patch = {
            agent: dict(BASE=self.tmp, MEM_DIR=mem, PEOPLE_DIR=mem / "people",
                        JOURNAL_DIR=mem / "journal", PANIC_SENTINEL=mem / ".panic",
                        _CURRENT_CHAT=None),
            rooms: dict(BASE=self.tmp, MEM_DIR=mem, ALLOWLIST=mem / "rooms_allowlist.json",
                        FROZEN=mem / "frozen_chats.json"),
            bg: dict(BASE=self.tmp, PANIC_SENTINEL=mem / ".panic"),
            pe: dict(BASE=self.tmp, PEOPLE_DIR=mem / "people"),
            mi: dict(BASE=self.tmp, MEM_DIR=mem, PEOPLE_DIR=mem / "people",
                     VECTORS_DIR=mem / ".vectors", INDEX_JSON=mem / ".vectors" / "index.json"),
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


class TestStaySilent(Base):
    """28.07: контракт тула пересмотрен вместе с реализацией.

    Здесь стояло `assertEqual(out, "")` — тест на то, что тул возвращает пустую строку.
    Ровно эта пустота и была дырой: результат не читал НИКТО, молчание держалось тем,
    что модель после вызова сама не пишет текста, а комментарий рядом обещал механизм.
    Теперь тул отвечает словами и, в живом ходе, вправду глушит ход — тест сторожит
    оба исхода и то, что решение обратимо её же рукой.
    """

    def test_outside_a_live_turn_it_says_there_is_nothing_to_hold(self):
        out = agent.tool_stay_silent("наживка, отвечать = кормить")
        self.assertIn("держать нечего", out)
        self.assertIn("[молчу]", self._journal())
        self.assertIn("наживка", self._journal())

    def test_inside_a_live_turn_it_marks_the_holder(self):
        holder: dict[str, str] = {}
        token = agent._TURN_SILENCE.set(holder)
        try:
            out = agent.tool_stay_silent("тут отвечать не буду")
        finally:
            agent._TURN_SILENCE.reset(token)
        self.assertIn("не уйдёт", out)
        self.assertEqual(holder.get("chosen"), "1")
        self.assertIn("[молчу]", self._journal())

    def test_she_can_change_her_mind_within_the_same_turn(self):
        holder: dict[str, str] = {}
        token = agent._TURN_SILENCE.set(holder)
        try:
            agent.tool_stay_silent("сгоряча")
            out = agent.tool_stay_silent(cancel=True)
        finally:
            agent._TURN_SILENCE.reset(token)
        self.assertIn("Передумала", out)
        self.assertNotIn("chosen", holder)
        self.assertIn("[молчу→передумала]", self._journal())


class TestFreezeChat(Base):
    def test_freeze_current_chat(self):
        with mock.patch.object(agent.social, "owner_id", return_value="101"):
            token = agent._TURN_CHANNEL.set(agent.ChannelContext(
                chat_id="-100500", room_id="-100500", principal_id=101,
                owner=True, known=True, is_dm=False,
            ))
            try:
                agent.tool_freeze_chat(on=True)
                self.assertTrue(rooms.is_frozen("-100500"))
                agent.tool_freeze_chat(on=False, chat_id="-100500")
                self.assertFalse(rooms.is_frozen("-100500"))
            finally:
                agent._TURN_CHANNEL.reset(token)

    def test_guard_without_chat(self):
        with mock.patch.object(agent.social, "owner_id", return_value="101"):
            token = agent._TURN_CHANNEL.set(agent.ChannelContext(
                principal_id=101, owner=True, known=True,
            ))
            try:
                self.assertIn("укажи", agent.tool_freeze_chat(on=True).lower())
            finally:
                agent._TURN_CHANNEL.reset(token)


class TestPanic(Base):
    def test_writes_sentinel_and_schedules_exit(self):
        calls = []
        orig = agent._schedule_exit
        agent._schedule_exit = lambda: calls.append(1)
        try:
            out = agent.panic("стоп от Хоуп")
        finally:
            agent._schedule_exit = orig
        self.assertEqual(calls, [1], "выход не запланирован")
        self.assertTrue(agent.PANIC_SENTINEL.exists(), "sentinel не записан")
        self.assertTrue(bg.panic_active(), "bootguard не видит panic")
        self.assertIn("[panic]", self._journal())

    def test_bootguard_panic_active_reads_sentinel(self):
        self.assertFalse(bg.panic_active())
        bg.PANIC_SENTINEL.write_text("stop", encoding="utf-8")
        self.assertTrue(bg.panic_active())


class TestToolsets(Base):
    def test_owner_and_base_tools(self):
        owner = [t["name"] for t in agent.OWNER_TOOLS]
        base = [t["name"] for t in agent.BASE_TOOLS]
        self.assertIn("freeze_chat", owner)
        self.assertIn("panic", owner)
        self.assertIn("stay_silent", base)
        self.assertNotIn("panic", base, "panic не должен быть базовым (только владельцу)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
