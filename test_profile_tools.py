"""Её лицо/слова/жесты: аватарка, профиль, реакции — тулы, impl и мост.

Мост (mtproto_runner) конструирует TelegramClient на импорте — тесты моста идут в контейнере.
Здесь проверяем agent-сторону (тулы предложены, impl зовёт мост, гейт держит) без сети.
"""
from __future__ import annotations

import unittest

import agent


class ProfileToolsOffered(unittest.TestCase):
    def test_three_tools_are_in_her_own_toolset(self):
        names = {t["name"] for t in agent.PRAXIS_SELF_TOOLS}
        self.assertIn("set_avatar", names)
        self.assertIn("update_profile", names)
        self.assertIn("react", names)

    def test_tools_are_registered_in_impl(self):
        for name in ("set_avatar", "update_profile", "react"):
            self.assertIn(name, agent.TOOL_IMPL)
            self.assertTrue(callable(agent.TOOL_IMPL[name]))

    def test_schemas_declare_required_fields(self):
        by_name = {t["name"]: t for t in
                   (agent.SET_AVATAR_TOOL, agent.UPDATE_PROFILE_TOOL, agent.REACT_TOOL)}
        self.assertEqual(by_name["set_avatar"]["input_schema"]["required"], ["path"])
        self.assertEqual(by_name["update_profile"]["input_schema"]["required"], [])
        self.assertEqual(set(by_name["react"]["input_schema"]["required"]),
                         {"emoji", "message_id"})


class ProfileImplCallsBridge(unittest.TestCase):
    def setUp(self):
        self._saved = dict(agent._TELETHON)

    def tearDown(self):
        agent._TELETHON.clear()
        agent._TELETHON.update(self._saved)

    def test_set_avatar_routes_to_bridge(self):
        seen = {}
        agent._TELETHON["set_profile_photo"] = lambda path: (seen.update(path=path), "ok-photo")[1]
        self.assertEqual(agent.tool_set_avatar("workspace/me.png"), "ok-photo")
        self.assertEqual(seen["path"], "workspace/me.png")

    def test_update_profile_needs_a_field(self):
        agent._TELETHON["update_profile"] = lambda **kw: "should-not-run"
        self.assertIn("Нечего менять", agent.tool_update_profile())

    def test_update_profile_passes_fields(self):
        seen = {}
        agent._TELETHON["update_profile"] = lambda **kw: seen.update(kw) or "ok-profile"
        self.assertEqual(agent.tool_update_profile(about="я — Praxis"), "ok-profile")
        self.assertEqual(seen, {"about": "я — Praxis", "first_name": "", "last_name": ""})

    def test_react_routes_to_bridge(self):
        seen = {}
        agent._TELETHON["send_reaction"] = lambda **kw: seen.update(kw) or "ok-react"
        self.assertEqual(agent.tool_react("🔥", 42), "ok-react")
        self.assertEqual(seen, {"chat": "", "message_id": 42, "emoji": "🔥", "remove": False})

    def test_missing_bridge_is_honest(self):
        for name, call in (
            ("set_profile_photo", lambda: agent.tool_set_avatar("x.png")),
            ("update_profile", lambda: agent.tool_update_profile(about="x")),
            ("send_reaction", lambda: agent.tool_react("🔥", 1)),
        ):
            agent._TELETHON.pop(name, None)
            self.assertIn("недоступен", call())


if __name__ == "__main__":
    unittest.main()
