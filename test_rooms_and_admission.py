"""
Тесты текущего контракта: само-управление комнатами + впуск незнакомцев.

Герметичны: без сети/ключей/Ollama. `anthropic`/`dotenv` подменяются заглушками,
пути модулей уводятся во временную папку.

Запуск:  python praxis_test.py test_rooms_and_admission -v
"""

from __future__ import annotations

import json
import os
import contextlib
import shutil
import sys
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# --- заглушки внешних зависимостей ДО импорта agent ------------------------- #
_fa = types.ModuleType("anthropic")
_fa.Anthropic = lambda **kw: None
sys.modules.setdefault("anthropic", _fa)
_fd = types.ModuleType("dotenv")
_fd.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _fd)

import memory_index as mi  # noqa: E402
import people  # noqa: E402
import rooms  # noqa: E402
import social  # noqa: E402
import agent  # noqa: E402
import llm  # noqa: E402


class FakeResp:
    def __init__(self, text: str):
        self.stop_reason = "end_turn"
        self.content = [types.SimpleNamespace(type="text", text=text)]


class FakeClient:
    def __init__(self, reply: str = "ок"):
        self.reply = reply
        self.last: dict = {}

        class _M:
            def create(_s, **kw):
                self.last = kw
                return FakeResp(self.reply)

        self.messages = _M()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_ra_"))
        mem = self.tmp / "memory"
        soul = self.tmp / "soul"
        for d in (mem / "people", mem / "rooms", mem / "journal", mem / ".vectors", soul / "skills"):
            d.mkdir(parents=True, exist_ok=True)
        for n in ("SOUL", "self", "emotions", "being_with"):
            (soul / f"{n}.md").write_text(f"# {n}\n", encoding="utf-8")

        self._orig: list[tuple] = []
        patch = {
            rooms: dict(BASE=self.tmp, MEM_DIR=mem, ALLOWLIST=mem / "rooms_allowlist.json",
                        FROZEN=mem / "frozen_chats.json"),
            social: dict(BASE=self.tmp, MEM_DIR=mem, KNOWN_IDS=mem / "known_ids.json",
                         UNKNOWN_COUNTS=mem / ".unknown_counts.json"),
            mi: dict(BASE=self.tmp, MEM_DIR=mem, SOUL_DIR=soul, SKILLS_DIR=soul / "skills",
                     PEOPLE_DIR=mem / "people", ROOMS_DIR=mem / "rooms",
                     VECTORS_DIR=mem / ".vectors", INDEX_JSON=mem / ".vectors" / "index.json",
                     INDEX_MD=mem / "INDEX.md"),
            agent: dict(BASE=self.tmp, SOUL_DIR=soul, SKILLS_DIR=soul / "skills", MEM_DIR=mem,
                        PEOPLE_DIR=mem / "people", ROOMS_DIR=mem / "rooms",
                        JOURNAL_DIR=mem / "journal", REFLECTIONS=mem / "reflections.md",
                        INDEX_MD=mem / "INDEX.md", WORKDIR=str(self.tmp / "workspace"),
                        _CURRENT_CHAT=None, _CURRENT_SCOPE="owner"),
            people: dict(BASE=self.tmp, PEOPLE_DIR=mem / "people"),
        }
        for module, attrs in patch.items():
            for k, val in attrs.items():
                self._orig.append((module, k, getattr(module, k)))
                setattr(module, k, val)
        mi._CACHE = {"mtime": None, "index": None}
        self._orig.append((mi, "embed", mi.embed))
        mi.embed = lambda t: (_ for _ in ()).throw(ConnectionError("no ollama"))  # индекс не нужен
        # чистое окружение
        self._env = {k: os.environ.get(k) for k in ("TELEGRAM_ALLOWED_CHATS", "PRAXIS_OWNER_ID")}
        for k in self._env:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for module, k, val in reversed(self._orig):
            setattr(module, k, val)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    @contextlib.contextmanager
    def owner_turn(self, chat_id="123456789", *, is_dm=True, room_id=None):
        previous = os.environ.get("PRAXIS_OWNER_ID")
        os.environ["PRAXIS_OWNER_ID"] = "123456789"
        token = agent._TURN_CHANNEL.set(agent.ChannelContext(
            chat_id=chat_id, room_id=room_id, principal_id=123456789,
            is_dm=is_dm, owner=True, known=True,
        ))
        try:
            yield
        finally:
            agent._TURN_CHANNEL.reset(token)
            if previous is None:
                os.environ.pop("PRAXIS_OWNER_ID", None)
            else:
                os.environ["PRAXIS_OWNER_ID"] = previous


class TestRooms(Base):
    def test_file_union_env(self):
        rooms.add_room("-100111")
        os.environ["TELEGRAM_ALLOWED_CHATS"] = "-100999, -100888"
        allowed = rooms.allowed_chats()
        self.assertEqual(allowed, {"-100111", "-100999", "-100888"})

    def test_add_remove_live(self):
        self.assertNotIn("-100222", rooms.allowed_chats())

    def test_remove_masks_legacy_env_and_rejoin_clears_mask(self):
        os.environ["TELEGRAM_ALLOWED_CHATS"] = "-100333"
        self.assertIn("-100333", rooms.allowed_chats())
        self.assertTrue(rooms.remove_room("-100333"))
        self.assertNotIn("-100333", rooms.allowed_chats())
        self.assertFalse(rooms.remove_room("-100333"))
        self.assertTrue(rooms.add_room("-100333"))
        self.assertIn("-100333", rooms.allowed_chats())
        self.assertTrue(rooms.add_room("-100222"))
        self.assertFalse(rooms.add_room("-100222"))         # дубль
        self.assertIn("-100222", rooms.allowed_chats())     # видно сразу
        self.assertTrue(rooms.remove_room("-100222"))
        self.assertFalse(rooms.remove_room("-100222"))
        self.assertNotIn("-100222", rooms.allowed_chats())

    def test_list_rooms(self):
        rooms.add_room("-2")
        rooms.add_room("-1")
        self.assertEqual(rooms.list_rooms(), ["-1", "-2"])

    def test_concurrent_room_updates_do_not_lose_or_truncate_entries(self):
        expected = {f"-1009{index:02d}" for index in range(20)}
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(rooms.add_room, expected))
        self.assertTrue(expected.issubset(rooms.allowed_chats()))
        self.assertEqual(
            set(json.loads(rooms.ALLOWLIST.read_text(encoding="utf-8"))), expected,
        )

    def test_joined_room_is_visible_until_explicit_leave(self):
        # This predicate is called for a Telegram update from an account membership;
        # the departed mask, not an old catalogue, is the explicit exclusion.
        self.assertTrue(rooms.is_allowed("-100777", is_owner=False))
        self.assertTrue(rooms.is_allowed("-100777", is_owner=True))
        rooms.remove_room("-100777")
        self.assertFalse(rooms.is_allowed("-100777", is_owner=False))
        rooms.add_room("-100777")
        self.assertTrue(rooms.is_allowed("-100777", is_owner=False))

    def test_freeze_unfreeze(self):
        self.assertFalse(rooms.is_frozen("-100"))
        self.assertTrue(rooms.freeze("-100"))
        self.assertTrue(rooms.is_frozen("-100"))
        self.assertFalse(rooms.freeze("-100"))   # дубль
        self.assertTrue(rooms.unfreeze("-100"))
        self.assertFalse(rooms.is_frozen("-100"))


class TestManageRoom(Base):
    def test_owner_topic_admits_root_room_not_conversation_key(self):
        with self.owner_turn(
                "-100500__topic__77", is_dm=False, room_id="-100500"):
            out = agent.tool_manage_room("join")
        self.assertIn("-100500", out)
        self.assertIn("-100500", rooms.allowed_chats())
        self.assertNotIn("-100500__topic__77", rooms.allowed_chats())

    def test_join_current_chat(self):
        with self.owner_turn("-100500", is_dm=False, room_id="-100500"):
            out = agent.tool_manage_room("join")
        self.assertIn("-100500", out)
        self.assertIn("-100500", rooms.allowed_chats())
        self.assertTrue((agent.ROOMS_DIR / "-100500.md").exists(), "не создана заглушка комнаты")

    def test_join_in_private_guard(self):
        with self.owner_turn(None):
            out = agent.tool_manage_room("join")
        self.assertIn("личке", out.lower())
        self.assertEqual(rooms.list_rooms(), [])

    def test_leave_and_list(self):
        rooms.add_room("-100600")
        with self.owner_turn():
            self.assertIn("-100600", agent.tool_manage_room("list"))
            out = agent.tool_manage_room("leave", "-100600")
        self.assertIn("-100600", out)
        self.assertNotIn("-100600", rooms.allowed_chats())

    def test_room_management_is_hers_in_every_turn(self):
        """Решение Егора 26.07: набор рук НЕ зависит от того, кто заговорил. «У неё не должно быть вообще никаких ограничений, когда к ней обращаюсь не я»; «она и сама уже сейчас откажет, если там бред». За Егором остались только admit и computer_access — раздача ЕГО доверия другим людям."""
        names = _tools_for(False)
        self.assertIn("manage_room", names, "комнаты — её дом, а не привилегия Егора")
        self.assertNotIn("admit", names, "но впустить нового человека решает он")

    def test_in_tools_for_owner(self):
        names = _tools_for(True)
        self.assertIn("manage_room", names)
        self.assertIn("admit", names)
        self.assertIn("shell", names)

    def test_praxis_can_freeze_current_non_owner_chat_without_approval(self):
        agent._CURRENT_CHAT = "777"
        agent._CURRENT_SCOPE = "unknown"
        out = agent.tool_freeze_contact("спам и давление")
        self.assertIn("Заморозила", out)
        self.assertTrue(rooms.is_frozen("777"))
        self.assertIn("freeze_contact", _tools_for(False))


class TestSocial(Base):
    def test_category(self):
        os.environ["PRAXIS_OWNER_ID"] = "123456789"
        self.assertEqual(social.category(123456789), "owner")
        self.assertEqual(social.category(123), "unknown")
        social.add_known("123", "Гость")
        self.assertEqual(social.category(123), "known")

    def test_admit_creates_person_and_known(self):
        with self.owner_turn():
            out = agent.tool_admit("Маша", "12345")
        self.assertIn("Маша", out)
        self.assertEqual(social.category("12345"), "known")
        self.assertTrue((agent.PEOPLE_DIR / "маша.md").exists())

    def test_admit_needs_id(self):
        with self.owner_turn():
            self.assertIn("id", agent.tool_admit("Безымянный").lower())

    def test_admit_refuses_owner(self):
        with self.owner_turn():
            out = agent.tool_admit("Я сам", "123456789")
            self.assertIn("ты сам", out.lower())
            self.assertEqual(social.category("123456789"), "owner")

    def test_owner_id_read_lazily(self):
        # регрессия env-бага: owner распознаётся, даже если env выставлен ПОСЛЕ импорта модуля
        self.assertEqual(social.category("424242"), "unknown")
        os.environ["PRAXIS_OWNER_ID"] = "424242"
        self.assertEqual(social.category("424242"), "owner")


class TestAdmission(Base):
    def test_heads_up_once_and_cap(self):
        sid, day, cap = "777", "2026-06-27", 3
        # первый контакт — first=True ровно один раз
        a1 = social.admission_check(sid, day, cap)
        self.assertTrue(a1["first"])
        self.assertEqual(a1["count"], 1)
        self.assertFalse(a1["over_cap"])
        for _ in range(2):
            a = social.admission_check(sid, day, cap)
            self.assertFalse(a["first"], "heads-up должен быть один раз на id")
        self.assertEqual(a["count"], 3)
        self.assertFalse(a["over_cap"])
        a4 = social.admission_check(sid, day, cap)  # 4-е > cap=3
        self.assertTrue(a4["over_cap"], "не сработал дневной cap")

    def test_cap_resets_next_day(self):
        sid, cap = "888", 2
        social.admission_check(sid, "2026-06-27", cap)
        social.admission_check(sid, "2026-06-27", cap)
        over = social.admission_check(sid, "2026-06-27", cap)["over_cap"]
        self.assertTrue(over)
        nxt = social.admission_check(sid, "2026-06-28", cap)  # новый день
        self.assertFalse(nxt["first"], "first остаётся False на следующий день")
        self.assertEqual(nxt["count"], 1, "счётчик сбросился на новый день")
        self.assertFalse(nxt["over_cap"])


class ScriptedClient:
    """Отдаёт заранее заданную последовательность ответов (tool_use -> текст)."""

    def __init__(self, steps):
        self._steps = list(steps)
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return self._steps.pop(0)


def _tool_use(tid, name, inp):
    blk = types.SimpleNamespace(type="tool_use", id=tid, name=name, input=inp)
    return types.SimpleNamespace(stop_reason="tool_use", content=[blk])


class TestIntegration(Base):
    def test_owner_join_through_respond(self):
        """respond ставит _CURRENT_CHAT из chat_id; ReAct-цикл диспатчит manage_room."""
        steps = [
            _tool_use("t1", "manage_room", {"action": "join"}),
            FakeResp("впустила, готово"),
        ]
        llm.use_test_client(ScriptedClient(steps))
        try:
            with self.owner_turn("-100900", is_dm=False, room_id="-100900"):
                reply = agent.respond(
                    "добавь эту группу в доверенные", [], "Егор",
                    force_voice=True, is_owner=True, chat_id="-100900",
                    principal_id=123456789,
                )
        finally:
            llm.clear_test_clients()
        self.assertIn("готово", reply)
        self.assertIn("-100900", rooms.allowed_chats(), "manage_room через respond не добавил текущий чат")
        self.assertTrue((agent.ROOMS_DIR / "-100900.md").exists())


def _tools_for(is_owner: bool) -> list[str]:
    fc = FakeClient()
    llm.use_test_client(fc)
    try:
        agent.respond("привет", [], "Гость", force_voice=True, is_owner=is_owner)
    finally:
        llm.clear_test_clients()
    return [t["name"] for t in fc.last.get("tools", [])]


if __name__ == "__main__":
    unittest.main(verbosity=2)
