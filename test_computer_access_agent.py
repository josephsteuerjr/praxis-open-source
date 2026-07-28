import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class AgentComputerAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"PRAXIS_BASE": self.tmp.name, "PRAXIS_OWNER_ID": "101", "PRAXIS_TEST": "1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_her_body_hands_do_not_shrink_when_someone_else_speaks(self):
        """Решение Егора 26.07 (вариант 1): в её ходе действует ОНА, кто бы ни заговорил,
        включая его машину. «Она и сама уже сейчас откажет, если там бред.»

        Раньше здесь ожидалось «Нет выданного» и отказ на `execution=system`: чужая
        реплика превращала её в гостя на её же теле. Гранты `computer_access` остаются
        механизмом раздачи ЕГО доверия людям, но её собственные руки не ограничивают.
        """
        import agent
        ctx = agent.ChannelContext(chat_id="202", principal_id="202", is_dm=True, known=True)
        token = agent._TURN_CHANNEL.set(ctx)
        try:
            self.assertTrue(agent._is_praxis_self(), "в её ходе действует она")
            for scope in ("computer.read", "computer.files",
                          "computer.process", "computer.apps"):
                self.assertTrue(agent._computer_allowed(scope), scope)
            self.assertTrue(agent._is_sovereign_actor(),
                            "запись и execution=system — тоже её")
            with mock.patch("body_client.state_line", return_value="online"):
                self.assertEqual(agent.tool_computer("status"), "online")
        finally:
            agent._TURN_CHANNEL.reset(token)

    def test_owner_can_manage_but_trusted_cannot(self):
        import agent
        owner = agent.ChannelContext(chat_id="101", principal_id="101", is_dm=True, owner=True)
        token = agent._TURN_CHANNEL.set(owner)
        try:
            with mock.patch.object(agent, "tool_journal"):
                self.assertIn("grant", agent.tool_computer_access(
                    "grant", "202", "A", ["computer.files"],
                ))
        finally:
            agent._TURN_CHANNEL.reset(token)

    def test_praxis_self_needs_no_human_grant_and_can_use_system(self):
        import agent

        ctx = agent.ChannelContext(
            principal_id=agent.PRAXIS_SELF_PRINCIPAL, is_dm=True, known=True,
            _scope_override="owner",
        )
        token = agent._TURN_CHANNEL.set(ctx)
        try:
            with mock.patch("body_client.process_start", return_value={
                "ok": True, "operation_id": "system-op",
            }) as start:
                result = agent.tool_computer(
                    "run", command="whoami.exe", execution="system",
                )
            self.assertIn("system-op", result)
            start.assert_called_once_with(
                command="whoami.exe", shell="power_shell", cwd="", execution="system",
            )
        finally:
            agent._TURN_CHANNEL.reset(token)

    def test_z_computer_send_uses_clean_filename_in_unique_transfer_directory(self):
        import agent

        artifact = {
            "name": "Отчёт 2026.txt",
            "sha256": "a" * 64,
            "size": 7,
        }
        destinations = []
        sent = []

        def fake_fetch(_artifact, destination):
            destination = Path(destination)
            destinations.append(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("content", encoding="utf-8")
            return {"ok": True, "path": str(destination), "sha256": artifact["sha256"], "size": 7}

        def fake_send(path, caption=""):
            path = Path(path)
            self.assertTrue(path.is_file())
            sent.append((path, caption))
            return "sent"

        memory = Path(self.tmp.name) / "memory"
        exported = {"ok": True, "artifact": artifact}
        with mock.patch.object(agent, "MEM_DIR", memory), \
                mock.patch.object(agent, "_computer_allowed", return_value=True), \
                mock.patch("body_client.call", return_value=exported), \
                mock.patch("body_client.fetch_artifact", side_effect=fake_fetch), \
                mock.patch("workshop.send_file", side_effect=fake_send), \
                mock.patch.object(agent, "tool_journal"):
            self.assertEqual(agent.tool_computer("send", path=r"C:\\work\\Отчёт 2026.txt"), "sent")
            self.assertEqual(agent.tool_computer("send", path=r"C:\\work\\Отчёт 2026.txt"), "sent")

        self.assertEqual([path.name for path in destinations], [artifact["name"], artifact["name"]])
        self.assertEqual([path.name for path, _caption in sent], [artifact["name"], artifact["name"]])
        self.assertNotEqual(destinations[0].parent, destinations[1].parent)
        for path in destinations:
            self.assertEqual(path.parent.parent, memory / ".outbox" / "computer")
            self.assertFalse(path.parent.exists(), "transfer directory must be removed after send")

    def test_computer_visible_name_is_utf8_byte_bounded_and_keeps_extension(self):
        import agent

        name = agent._computer_artifact_name("я" * 180 + ".txt")
        self.assertLessEqual(len(name.encode("utf-8")), 240)
        self.assertTrue(name.endswith(".txt"))

    def test_computer_apps_grant_routes_typed_native_desktop_actions(self):
        import agent
        import computer_access

        computer_access.change("grant", "202", name="A", scopes=["computer.apps"], actor="101")
        ctx = agent.ChannelContext(chat_id="202", principal_id="202", is_dm=True, known=True)
        token = agent._TURN_CHANNEL.set(ctx)
        try:
            ok = {"ok": True, "value": "native"}
            with mock.patch("body_client.desktop_status", return_value=ok) as status, \
                    mock.patch("body_client.desktop_window_list", return_value=ok) as windows, \
                    mock.patch("body_client.desktop_window_activate", return_value=ok) as activate, \
                    mock.patch("body_client.desktop_input_perform", return_value=ok) as perform, \
                    mock.patch("body_client.desktop_clipboard_read", return_value=ok) as clip_read, \
                    mock.patch("body_client.desktop_clipboard_write", return_value=ok) as clip_write, \
                    mock.patch("body_client.os_process_list", return_value=ok) as processes:
                self.assertIn("native", agent.tool_computer("desktop_status"))
                self.assertIn("native", agent.tool_computer("windows"))
                self.assertIn("native", agent.tool_computer("activate", hwnd="0x2a"))
                self.assertIn("native", agent.tool_computer(
                    "input", events=[{"type": "text", "text": "hello"}],
                    expected_foreground="0x2a", expected_pid=7,
                ))
                self.assertIn("native", agent.tool_computer(
                    "scroll", steps=3, direction="down", x=10, y=20,
                    expected_foreground="0x2a", expected_pid=7,
                ))
                self.assertIn("native", agent.tool_computer("clipboard_read"))
                self.assertIn("native", agent.tool_computer("clipboard_write", text="hello"))
                self.assertIn("native", agent.tool_computer("processes"))

            status.assert_called_once_with(execution="interactive")
            windows.assert_called_once_with(
                offset=0, limit=2048, visible_only=True, pid=None,
                title_contains="", execution="interactive",
            )
            activate.assert_called_once_with(
                "0x2a", expected_pid=None, restore=True, timeout_ms=1500,
                execution="interactive",
            )
            self.assertEqual(perform.call_count, 2)
            self.assertEqual(perform.call_args_list[0], mock.call(
                [{"type": "text", "text": "hello"}], expected_foreground="0x2a",
                expected_pid=7, inter_event_delay_ms=0, execution="interactive",
            ))
            self.assertEqual(perform.call_args_list[1], mock.call(
                [{"type": "wheel", "delta": -360, "horizontal": False, "x": 10, "y": 20}],
                expected_foreground="0x2a", expected_pid=7,
                inter_event_delay_ms=0, execution="interactive",
            ))
            clip_read.assert_called_once_with(limit_chars=1_000_000, execution="interactive")
            clip_write.assert_called_once_with("hello", execution="interactive")
            processes.assert_called_once_with(
                offset=0, limit=2048, name_contains="", session_id=None,
                execution="interactive",
            )
        finally:
            agent._TURN_CHANNEL.reset(token)

    def test_a_narrow_grant_does_not_narrow_HER(self):
        """Решение Егора 26.07 (вариант 1): в её ходе действует ОНА, кто бы ни заговорил. Гранты computer_access остаются раздачей ЕГО доверия людям, но её собственные руки они не ограничивают.

        Грант «только computer.read», выданный человеку, ограничивал ЕЁ саму, когда
        этот человек к ней обращался: `windows` отвечал «нужен computer.apps».
        """
        import agent
        import computer_access

        computer_access.change("grant", "202", name="A", scopes=["computer.read"], actor="101")
        ctx = agent.ChannelContext(chat_id="202", principal_id="202", is_dm=True, known=True)
        token = agent._TURN_CHANNEL.set(ctx)
        try:
            with mock.patch("body_client.desktop_window_list",
                            return_value=[]) as windows:
                agent.tool_computer("windows")
            windows.assert_called_once()
        finally:
            agent._TURN_CHANNEL.reset(token)

    def test_computer_input_schema_keeps_typed_events_in_openai_strict_mode(self):
        import agent
        import llm

        tool = llm.tools_to_openai([agent.COMPUTER_TOOL])[0]
        schema = tool["function"]["parameters"]
        events = schema["properties"]["events"]["anyOf"][0]
        item = events["items"]
        self.assertIn("type", item["properties"])
        self.assertIn("delta", item["properties"])
        self.assertIn("wheel", item["properties"]["type"]["enum"])
        self.assertFalse(item["additionalProperties"])

    def test_typed_scroll_requires_a_target_and_rejects_ambiguous_delta(self):
        import agent

        with mock.patch.object(agent, "_computer_allowed", return_value=True), \
                mock.patch("body_client.desktop_input_perform") as perform:
            self.assertIn("x/y", agent.tool_computer("scroll", steps=-2))
            self.assertIn("кратен 120", agent.tool_computer(
                "scroll", delta=-1, x=10, y=20,
            ))
        perform.assert_not_called()

    def test_typed_scroll_targets_guarded_window_centre(self):
        import agent

        with mock.patch.object(agent, "_computer_allowed", return_value=True), \
                mock.patch("body_client.desktop_window_list", return_value={
                    "ok": True, "items": [{
                        "hwnd": "0x2a",
                        "rect": {"left": 100, "top": 200, "width": 600, "height": 400},
                    }],
                }), \
                mock.patch("body_client.desktop_input_perform", return_value={"ok": True}) as perform:
            agent.tool_computer("scroll", direction="up", expected_foreground="0x2A")
        perform.assert_called_once_with(
            [{"type": "wheel", "delta": 120, "horizontal": False, "x": 400, "y": 400}],
            expected_foreground="0x2A", expected_pid=None,
            inter_event_delay_ms=0, execution="interactive",
        )

    def test_desktop_calls_reject_session_zero_even_for_owner(self):
        import agent

        owner = agent.ChannelContext(chat_id="101", principal_id="101", is_dm=True, owner=True)
        token = agent._TURN_CHANNEL.set(owner)
        try:
            with mock.patch("body_client.desktop_window_list") as windows:
                result = agent.tool_computer("windows", execution="system")
            self.assertIn("Session 0", result)
            windows.assert_not_called()
        finally:
            agent._TURN_CHANNEL.reset(token)

    def test_screenshot_exports_and_sends_capture_with_clean_name(self):
        import agent

        artifact = {"name": "Praxis desktop.png", "sha256": "b" * 64, "size": 3}
        destination = []

        def fake_fetch(_artifact, path):
            path = Path(path)
            destination.append(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"png")
            return {"ok": True, "path": str(path)}

        with mock.patch.object(agent, "MEM_DIR", Path(self.tmp.name) / "memory"), \
                mock.patch.object(agent, "_computer_allowed", return_value=True), \
                mock.patch("body_client.desktop_screen_capture", return_value={
                    "ok": True, "path": r"C:\\Temp\\Praxis desktop.png",
                }) as capture, \
                mock.patch("body_client.call", return_value={"ok": True, "artifact": artifact}) as export, \
                mock.patch("body_client.fetch_artifact", side_effect=fake_fetch), \
                mock.patch("workshop.send_file", return_value="sent") as send, \
                mock.patch.object(agent, "tool_journal"):
            result = agent.tool_computer("screenshot", caption="screen")

        self.assertEqual(result, "sent")
        capture.assert_called_once_with(
            target="desktop", hwnd=None, x=None, y=None, width=None, height=None,
            name="", execution="interactive",
        )
        export.assert_called_once_with(
            "fs.export", {"path": r"C:\\Temp\\Praxis desktop.png"},
            execution="interactive", timeout=600,
        )
        self.assertEqual(destination[0].name, "Praxis desktop.png")
        self.assertEqual(Path(send.call_args.args[0]).name, "Praxis desktop.png")
        self.assertEqual(send.call_args.kwargs["caption"], "screen")
        self.assertFalse(destination[0].parent.exists())

    def test_observe_returns_verified_pixels_to_the_model_without_sending(self):
        import agent

        artifact = {"name": "desktop.png", "sha256": "c" * 64, "size": 12}

        def fake_fetch(_artifact, path):
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\x89PNG\r\n\x1a\nDATA")
            return {"ok": True, "path": str(path), "sha256": "c" * 64, "size": 12}

        with mock.patch.object(agent, "MEM_DIR", Path(self.tmp.name) / "memory"), \
                mock.patch.object(agent, "_computer_allowed", return_value=True), \
                mock.patch("body_client.desktop_screen_capture", return_value={
                    "ok": True, "artifact": artifact,
                }), \
                mock.patch("body_client.fetch_artifact", side_effect=fake_fetch), \
                mock.patch("workshop.send_file") as send:
            result = agent.tool_computer("observe", target="desktop")

        self.assertIsInstance(result, agent.ToolObservation)
        self.assertEqual(result.images[0]["mime"], "image/png")
        self.assertTrue(Path(result.images[0]["path"]).is_file())
        send.assert_not_called()

    def test_the_body_tool_is_offered_in_every_turn(self):
        import agent
        import computer_access
        seen = []
        def fake_chat(*args, **kwargs):
            seen.append([tool["name"] for tool in kwargs.get("tools") or []])
            return SimpleNamespace(stop_reason="end_turn", text="ok", blocks=[])
        trusted = agent.ChannelContext(chat_id="202", principal_id="202", is_dm=True, known=True)
        with mock.patch("llm.chat", side_effect=fake_chat):
            agent._voice("hi", [], "A", ctx=trusted, max_iters=1)
        # Решение Егора 26.07: тело предлагается ей в любом ходе — оно её, а не гостя.
        self.assertIn("computer", seen[-1])
        computer_access.change("grant", "202", name="A", scopes=["computer.files"], actor="101")
        with mock.patch("llm.chat", side_effect=fake_chat):
            agent._voice("file", [], "A", ctx=trusted, max_iters=1)
        self.assertIn("computer", seen[-1])
        trusted = agent.ChannelContext(chat_id="202", principal_id="202", is_dm=True, known=True)
        token = agent._TURN_CHANNEL.set(trusted)
        try:
            self.assertIn("только владелец", agent.tool_computer_access("grant", "303", "B", ["computer.files"]).lower())
        finally:
            agent._TURN_CHANNEL.reset(token)


if __name__ == "__main__":
    unittest.main()
