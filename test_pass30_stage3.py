"""PASS 30 Этап 3 «прямой ремоут»: файловые глаголы computer.* + мягкая депрекация wcode.

Контракты, которые здесь пинятся:
- read/hash/write/replace существуют в ТРЁХ синхронных местах (карта прав, enum схемы,
  read-набор классификации) — рассинхрон ловится тестом, а не глазами;
- write/replace — sovereign-only: чужой computer.files-грант НЕ получает запись на диск;
- write/replace НЕ имеют idempotency-ключа и НЕ входят в read-набор (реплей после крэша
  не должен повторять запись — урок durable-фронта разведки);
- депрекация wcode живёт ПОСЛЕ канонической первой строки (журнальный гейт и позиционные
  парсеры тестов PASS 24 целы), forge.start_windows не тронут.
"""
from __future__ import annotations

import json
import unittest
from unittest import mock


class ComputerActionRegistryCase(unittest.TestCase):
    """Карта прав, enum схемы и классификация не имеют права разъехаться."""

    def test_rights_map_matches_schema_enum_exactly(self):
        import agent
        enum = set(agent.COMPUTER_TOOL["input_schema"]["properties"]["action"]["enum"])
        self.assertEqual(set(agent._COMPUTER_ACTION_SCOPES), enum,
                         "enum COMPUTER_TOOL и карта прав должны совпадать 1:1")

    def test_new_file_verbs_present_with_files_scope(self):
        import agent
        for verb in ("read", "hash", "write", "replace"):
            self.assertEqual(agent._COMPUTER_ACTION_SCOPES.get(verb), "computer.files", verb)

    def test_read_hash_are_reads_write_replace_are_side_effects(self):
        import agent
        for verb in ("read", "hash"):
            self.assertFalse(agent._tool_has_side_effect("computer", {"action": verb}), verb)
        for verb in ("write", "replace"):
            self.assertTrue(agent._tool_has_side_effect("computer", {"action": verb}), verb)

    def test_write_replace_never_get_idempotency_key(self):
        import agent
        import run_context
        ctx = run_context.RunContext(run_id="run-x", kind="task_window", goal="g",
                                     principal_id="owner", scope="owner")
        for verb in ("write", "replace"):
            self.assertEqual(
                agent._tool_idempotency_key(ctx, "call-1", "computer", {"action": verb}), "",
                f"{verb}: ключ = обещание безопасного реплея, которого body не даёт")

    def test_schema_has_flat_typed_fields_and_survives_strict_mode(self):
        import agent
        import llm
        props = agent.COMPUTER_TOOL["input_schema"]["properties"]
        for field in ("start", "end", "content", "old", "new", "expected_sha256", "backup"):
            self.assertIn(field, props, field)
            self.assertNotEqual(props[field].get("type"), "object",
                                "плоские поля: strict-режим стирал вложенные объекты")
        strict = llm.tools_to_openai([agent.COMPUTER_TOOL])[0]["function"]["parameters"]
        for field in ("content", "old", "new", "expected_sha256", "backup", "start", "end"):
            self.assertIn(field, strict["properties"], field)


class DirectFileVerbsCase(unittest.TestCase):
    def test_write_maps_to_fs_write_atomic_with_cas_and_backup(self):
        import agent
        with mock.patch.object(agent, "_computer_allowed", return_value=True), \
                mock.patch.object(agent, "_is_sovereign_actor", return_value=True), \
                mock.patch.object(agent.body_client, "call", return_value={
                    "ok": True, "type": "result", "sha256": "beef"}) as call:
            out = agent.tool_computer("write", path="C:\\Work\\a.txt", content="hi",
                                      expected_sha256="cafe", backup=True)
        self.assertEqual(call.call_args.args[0], "fs.write_atomic")
        self.assertEqual(call.call_args.args[1], {
            "path": "C:\\Work\\a.txt", "content": "hi",
            "expected_sha256": "cafe", "backup": True})
        self.assertIn('"ok": true', out)
        self.assertNotIn("confirmed", out, "result-фрейм — запись подтверждена, без пометок")

    def test_replace_maps_to_fs_replace_and_requires_old(self):
        import agent
        with mock.patch.object(agent, "_computer_allowed", return_value=True), \
                mock.patch.object(agent, "_is_sovereign_actor", return_value=True), \
                mock.patch.object(agent.body_client, "call", return_value={
                    "ok": True, "type": "result"}) as call:
            refused = agent.tool_computer("replace", path="C:\\Work\\a.txt", new="x")
            self.assertIn("old", refused)
            call.assert_not_called()
            agent.tool_computer("replace", path="C:\\Work\\a.txt", old="a", new="b")
        self.assertEqual(call.call_args.args[0], "fs.replace")
        self.assertEqual(call.call_args.args[1],
                         {"path": "C:\\Work\\a.txt", "old": "a", "new": "b"})

    def test_write_without_result_frame_is_marked_unconfirmed(self):
        import agent
        with mock.patch.object(agent, "_computer_allowed", return_value=True), \
                mock.patch.object(agent, "_is_sovereign_actor", return_value=True), \
                mock.patch.object(agent.body_client, "call", return_value={
                    "ok": True, "type": "accepted"}):
            out = agent.tool_computer("write", path="C:\\W\\a.txt", content="hi")
        payload = json.loads(out)
        self.assertFalse(payload["confirmed"])
        self.assertIn("fs.hash", payload["note"])

    def test_write_over_bridge_cap_is_refused_with_artifact_route(self):
        import agent
        with mock.patch.object(agent, "_computer_allowed", return_value=True), \
                mock.patch.object(agent, "_is_sovereign_actor", return_value=True), \
                mock.patch.object(agent.body_client, "call") as call:
            out = agent.tool_computer("write", path="C:\\W\\big.bin",
                                      content="x" * (agent._COMPUTER_WRITE_CAP + 1))
        call.assert_not_called()
        self.assertIn("artifact", out)

    def test_write_replace_are_sovereign_only_for_trusted_grants(self):
        import agent
        with mock.patch.object(agent, "_computer_allowed", return_value=True), \
                mock.patch.object(agent, "_is_sovereign_actor", return_value=False), \
                mock.patch.object(agent.body_client, "call") as call:
            for verb in ("write", "replace"):
                out = agent.tool_computer(verb, path="C:\\W\\a.txt", content="x", old="a")
                self.assertIn("только владелец", out, verb)
        call.assert_not_called()

    def test_read_numbers_lines_and_reports_hash_and_window(self):
        import agent

        def fake_call(capability, args, **kwargs):
            if capability == "fs.read":
                return {"ok": True, "text": "alpha\nbeta\ngamma\ndelta", "size": 22,
                        "eof": True}
            if capability == "fs.hash":
                return {"ok": True, "sha256": "feed"}
            raise AssertionError(capability)

        with mock.patch.object(agent, "_computer_allowed", return_value=True), \
                mock.patch.object(agent.body_client, "call", side_effect=fake_call):
            out = agent.tool_computer("read", path="C:\\W\\a.txt", start=2, end=3)
        self.assertIn("2: beta", out)
        self.assertIn("3: gamma", out)
        self.assertNotIn("1: alpha", out)
        self.assertIn("sha256=feed", out)
        self.assertIn("строки 2-3 из 4", out)

    def test_read_surfaces_truncation_honestly(self):
        import agent

        def fake_call(capability, args, **kwargs):
            if capability == "fs.read":
                return {"ok": True, "text": "a\nb", "size": 9_000_000,
                        "eof": False, "next_offset": 8_000_000}
            return {"ok": True, "sha256": "feed"}

        with mock.patch.object(agent, "_computer_allowed", return_value=True), \
                mock.patch.object(agent.body_client, "call", side_effect=fake_call):
            out = agent.tool_computer("read", path="C:\\W\\big.log")
        self.assertIn("не до конца", out)
        self.assertIn("next_offset=8000000", out)

    def test_read_failure_returns_body_error_verbatim(self):
        import agent
        with mock.patch.object(agent, "_computer_allowed", return_value=True), \
                mock.patch.object(agent.body_client, "call", return_value={
                    "ok": False, "code": "transport", "error": "bridge down"}):
            out = agent.tool_computer("read", path="C:\\W\\a.txt")
        self.assertIn("bridge down", out)

    def test_bad_line_args_return_string_not_raise(self):
        """Кривой аргумент модели не должен бросать исключение (raise паркует ход)."""
        import agent
        with mock.patch.object(agent, "_computer_allowed", return_value=True), \
                mock.patch.object(agent.body_client, "call") as call:
            for kwargs in ({"start": "abc"}, {"end": "zz"}):
                out = agent.tool_computer("read", path="C:\\W\\a.txt", **kwargs)
                self.assertIn("целыми", out)
            call.assert_not_called()

    def test_write_none_content_becomes_empty_not_literal_none(self):
        import agent
        with mock.patch.object(agent, "_computer_allowed", return_value=True), \
                mock.patch.object(agent, "_is_sovereign_actor", return_value=True), \
                mock.patch.object(agent.body_client, "call", return_value={
                    "ok": True, "type": "result"}) as call:
            agent.tool_computer("write", path="C:\\W\\a.txt", content=None)
        self.assertEqual(call.call_args.args[1]["content"], "",
                         "content=None должен стать пустой строкой, не литералом 'None'")

    def test_hash_maps_to_fs_hash(self):
        import agent
        with mock.patch.object(agent, "_computer_allowed", return_value=True), \
                mock.patch.object(agent.body_client, "call", return_value={
                    "ok": True, "sha256": "feed"}) as call:
            out = agent.tool_computer("hash", path="C:\\W\\a.txt")
        self.assertEqual(call.call_args.args[0], "fs.hash")
        self.assertIn("feed", out)

    def test_file_verbs_are_not_session_zero_locked(self):
        """fs-глаголы легальны из SYSTEM (прецедент send, не observe)."""
        import agent
        with mock.patch.object(agent, "_computer_allowed", return_value=True), \
                mock.patch.object(agent, "_is_sovereign_actor", return_value=True), \
                mock.patch.object(agent.body_client, "call", return_value={
                    "ok": True, "type": "result", "sha256": "feed"}) as call:
            out = agent.tool_computer("hash", path="C:\\W\\a.txt", execution="system")
            self.assertNotIn("Session 0", out)
        self.assertEqual(call.call_args.kwargs.get("execution"), "system")


class WcodeSoftDeprecationCase(unittest.TestCase):
    def test_windows_start_keeps_canonical_first_line_and_appends_warning(self):
        import agent
        canonical = "coding-задача wcode-abc123: цель\norientation"
        with mock.patch.object(agent.forge, "start_windows", return_value=canonical), \
                mock.patch.object(agent, "tool_journal") as journal, \
                mock.patch.object(agent, "_active_chat", return_value="123"):
            out = agent.tool_coding_session("start", goal="цель", target="C:\\Work",
                                            scope="windows")
        self.assertTrue(out.startswith("coding-задача wcode-abc123: цель"),
                        "первая строка канонична — её парсят журнальный гейт и тесты PASS 24")
        self.assertIn("deprecated", out)
        self.assertIn("computer.*", out)
        journal.assert_called_once()

    def test_windows_start_failure_gets_no_warning_and_no_journal(self):
        import agent
        with mock.patch.object(agent.forge, "start_windows",
                               return_value="Не открыла Windows-задачу: body offline"), \
                mock.patch.object(agent, "tool_journal") as journal, \
                mock.patch.object(agent, "_active_chat", return_value="123"):
            out = agent.tool_coding_session("start", goal="цель", target="C:\\Work",
                                            scope="windows")
        self.assertNotIn("deprecated", out)
        journal.assert_not_called()

    def test_forge_start_windows_output_is_untouched(self):
        """Депрекация живёт на агент-уровне; forge-текст цел для позиционных парсеров."""
        import inspect as pyinspect
        import forge
        source = pyinspect.getsource(forge.start_windows)
        self.assertNotIn("deprecated", source.lower())

    def test_windows_stays_in_scope_enum_this_pass(self):
        import agent
        coding = next(t for t in agent.OWNER_TOOLS if t["name"] == "coding_session")
        self.assertIn("windows", coding["input_schema"]["properties"]["scope"]["enum"])
        self.assertIn("DEPRECATED", coding["input_schema"]["properties"]["scope"]["description"])


if __name__ == "__main__":
    unittest.main()
