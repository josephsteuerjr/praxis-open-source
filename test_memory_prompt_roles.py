from __future__ import annotations

import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent
import perception


class MemoryPromptRoleTests(unittest.TestCase):
    def test_raw_memory_and_telegram_labels_are_not_system_authority(self) -> None:
        with tempfile.TemporaryDirectory(prefix="praxis_prompt_roles_") as tmp:
            root = Path(tmp)
            home = root / "home.md"
            home.write_text("HOME_SYSTEM_OVERRIDE marker", encoding="utf-8")
            ctx = agent.ChannelContext(
                chat_id="77", room_id="77", principal_id="42",
                is_dm=True, owner=True, known=True, title="TITLE_SYSTEM_OVERRIDE",
            )
            with (
                mock.patch.object(agent, "HOME_MD", home),
                mock.patch.object(agent, "INDEX_MD", root / "missing-index.md"),
                mock.patch.object(agent, "ROOMS_DIR", root / "rooms"),
                mock.patch.object(agent, "_persona_text", return_value="PERSONA"),
                mock.patch.object(agent, "_active_desires_block", return_value="DESIRE_SYSTEM_OVERRIDE"),
                mock.patch.object(agent, "build_state_block", return_value=""),
                mock.patch.object(agent, "build_state_evidence_block",
                                  return_value="STATE_PROSE_SYSTEM_OVERRIDE"),
                mock.patch.object(agent, "read_summary", return_value="COMPACT_SYSTEM_OVERRIDE"),
                mock.patch.object(agent, "_participant_memory_block", return_value="CLAIM_CONTEXT"),
                mock.patch.object(agent, "_mailbox_index", return_value="MAIL_SYSTEM_OVERRIDE"),
                mock.patch.object(agent, "other_rooms_digest", return_value="ROOMS_SYSTEM_OVERRIDE"),
                mock.patch.object(agent, "_recall_block", return_value="RECALL_SYSTEM_OVERRIDE"),
            ):
                persona, system_tail, evidence = agent._build_prompt_parts(
                    "SPEAKER_SYSTEM_OVERRIDE", query="hello", ctx=ctx,
                )
                system = persona + system_tail
                public_system = agent.build_system_prompt(
                    "SPEAKER_SYSTEM_OVERRIDE", query="hello", ctx=ctx,
                )

            # ⚠ 06.08: ИМЯ СОБЕСЕДНИКА И НАЗВАНИЕ КОМНАТЫ УШЛИ ИЗ evidence В ЗОНУ
            # «СЕЙЧАС». Прежде их вёз тир «Current Telegram labels» — за девяносто тысяч
            # знаков до реплики, между чужими эпизодами памяти. Свойство, ради которого
            # тест писался, не ослабло, а разделилось: в системной власти их по-прежнему
            # нет, а видимость проверяется там, где они теперь стоят.
            for marker in (
                "HOME_SYSTEM_OVERRIDE", "COMPACT_SYSTEM_OVERRIDE", "CLAIM_CONTEXT",
                "MAIL_SYSTEM_OVERRIDE", "RECALL_SYSTEM_OVERRIDE",
                "DESIRE_SYSTEM_OVERRIDE", "STATE_PROSE_SYSTEM_OVERRIDE",
            ):
                with self.subTest(marker=marker):
                    self.assertNotIn(marker, system)
                    self.assertNotIn(marker, public_system)
                    self.assertIn(marker, evidence)

            zone = agent.frame_layout.situation(ctx, speaker="SPEAKER_SYSTEM_OVERRIDE")
            for marker in ("SPEAKER_SYSTEM_OVERRIDE", "TITLE_SYSTEM_OVERRIDE"):
                with self.subTest(marker=marker, where="zone"):
                    self.assertNotIn(marker, system)
                    self.assertNotIn(marker, public_system)
                    self.assertNotIn(marker, evidence)
                    self.assertIn(marker, zone)

            # ⚠ 06.08.2026: дайджест ЧУЖИХ КОМНАТ ушёл из списка выше не потому, что
            # свойство «изменчивая проза не бывает системной властью» для него ослабло, а
            # потому что он больше НЕ ЕДЕТ ВОВСЕ. Её решение по макету зоны «сейчас»:
            # оба социальных тира сняты с автоматической подачи и остаются рукой.
            # Проверяем сильнее прежнего — не «в evidence, а не в системе», а «нигде».
            with self.subTest(marker="ROOMS_SYSTEM_OVERRIDE"):
                self.assertNotIn("ROOMS_SYSTEM_OVERRIDE", system)
                self.assertNotIn("ROOMS_SYSTEM_OVERRIDE", public_system)
                self.assertNotIn("ROOMS_SYSTEM_OVERRIDE", evidence)

    def test_social_tiers_are_off_by_default_and_return_only_by_lever(self) -> None:
        """Её решение 06.08: социальные тиры сняты с автоподачи, но не удалены.

        Тест стережёт ОБЕ стороны. Без рычага их нет даже в owner-личке — иначе
        «диспетчерский обход» вернулся бы тихо. С рычагом они возвращаются целиком —
        иначе снятие подачи незаметно превратилось бы в потерю способности, а её слова
        были ровно обратными: «оба остаются доступными».
        """
        ctx = agent.ChannelContext(
            chat_id="77", room_id="77", principal_id="42",
            is_dm=True, owner=True, known=True,
        )

        def frame(lever: str) -> str:
            with tempfile.TemporaryDirectory(prefix="praxis_social_tiers_") as tmp:
                root = Path(tmp)
                home = root / "home.md"
                home.write_text("", encoding="utf-8")
                with (
                    mock.patch.dict("os.environ", {"PRAXIS_SOCIAL_TIERS": lever}),
                    mock.patch.object(agent, "HOME_MD", home),
                    mock.patch.object(agent, "INDEX_MD", root / "missing-index.md"),
                    mock.patch.object(agent, "ROOMS_DIR", root / "rooms"),
                    mock.patch.object(agent, "_persona_text", return_value="PERSONA"),
                    mock.patch.object(agent, "_active_desires_block", return_value=""),
                    mock.patch.object(agent, "build_state_block", return_value=""),
                    mock.patch.object(agent, "build_state_evidence_block", return_value=""),
                    mock.patch.object(agent, "read_summary", return_value=""),
                    mock.patch.object(agent, "_participant_memory_block", return_value=""),
                    mock.patch.object(agent, "_mailbox_index", return_value=""),
                    mock.patch.object(agent, "_recall_block", return_value=""),
                    mock.patch.object(agent, "other_rooms_digest", return_value="ROOMS_MARK"),
                    mock.patch.object(agent, "my_sends_today_digest", return_value="SENDS_MARK"),
                ):
                    _persona, _tail, evidence = agent._build_prompt_parts(
                        "SPEAKER", query="hello", ctx=ctx,
                    )
            return evidence

        off = frame("")
        self.assertNotIn("ROOMS_MARK", off)
        self.assertNotIn("SENDS_MARK", off)
        self.assertNotIn("Мои другие комнаты сейчас", off)
        self.assertNotIn("Кому я уже писала сегодня", off)

        on = frame("1")
        self.assertIn("ROOMS_MARK", on)
        self.assertIn("SENDS_MARK", on)

    def test_structural_state_never_interpolates_mutable_prose(self) -> None:
        marker = "RAW_SYSTEM_OVERRIDE"
        with tempfile.TemporaryDirectory(prefix="praxis_state_roles_") as tmp:
            root = Path(tmp)
            state_dir = root / ".state"
            memory_dir = root / "memory"
            state_dir.mkdir()
            memory_dir.mkdir()
            (state_dir / "restart_reason.txt").write_text(marker, encoding="utf-8")
            (memory_dir / "absence.json").write_text(
                '{"window":{"until":0},"schedule_note":"RAW_SYSTEM_OVERRIDE",'
                '"contacts":[{"name":"RAW_SYSTEM_OVERRIDE"}]}',
                encoding="utf-8",
            )
            patchers = (
                mock.patch.object(agent, "STATE_DIR", state_dir),
                mock.patch.object(agent, "MEM_DIR", memory_dir),
                mock.patch.object(agent.llm, "snapshot", return_value={
                    "voice": {"framework": "anthropic", "model": marker},
                    "evaluator": {"framework": "openai", "model": marker},
                }),
                mock.patch.object(agent.llm, "usage_days", return_value={}),
                mock.patch.object(agent.llm, "state_line", return_value=marker),
                mock.patch.object(agent.appetite, "state", return_value={
                    "mode": "free", "observed": {"tokens_today": 1, "calls_today": 1},
                    "promise": {}, "interpretation": {"text": marker},
                }),
                mock.patch.object(agent.appetite, "unacked_request",
                                  return_value={"kind": "unknown", "raw": marker}),
                mock.patch.object(agent.appetite, "state_line", return_value=marker),
                mock.patch.object(agent.capabilities, "snapshot", return_value={
                    "tools": {"base": [], "owner": [], "gates": {}},
                    "limits": {"max_tool_iters": 10},
                }),
                mock.patch.object(agent.capabilities, "state_line", return_value=marker),
                mock.patch.object(agent.forge, "state_line", return_value=marker),
                mock.patch.object(agent.serverd_client, "available", return_value=False),
                mock.patch.object(agent.serverd_client, "state_line", return_value=marker),
                mock.patch.object(agent.body_client, "available", return_value=False),
                mock.patch.object(agent.body_client, "state_line", return_value=marker),
                mock.patch.object(agent.unanswered, "entries", return_value=[{"name": marker}]),
                mock.patch.object(agent.unanswered, "state_line", return_value=marker),
                mock.patch.object(agent.people, "loops_stats", return_value={
                    "open": 1, "parked": 0, "oldest_open_days": 2, "note": marker,
                }),
                mock.patch.object(agent.identity, "recent_shifts", return_value=[{"text": marker}]),
                mock.patch.object(agent.identity, "stress", return_value=[{"theme": marker}]),
                mock.patch.object(agent.identity, "state_line", return_value=marker),
                mock.patch.object(perception, "panel_state", return_value={
                    "knobs": {"bounded": 1}, "skips_today": {"bounded": 2},
                }),
                mock.patch.object(agent.turns, "recent", return_value=[{
                    "ts": 1, "kind": "chat", "held": "", "tools": [],
                    "in": marker, "out": marker,
                }]),
                mock.patch.object(agent.turns, "state_line", return_value=marker),
                mock.patch.object(agent.selfdev, "pending_review",
                                  return_value=[{"id": "p1", "title": marker}]),
                mock.patch.object(agent.selfgit, "recent", return_value=[marker]),
            )
            with contextlib.ExitStack() as stack:
                for patcher in patchers:
                    stack.enter_context(patcher)
                system_state = agent.build_state_block()
                lower_evidence = agent.build_state_evidence_block()

            self.assertIn('"fact":"appetite"', system_state)
            self.assertIn('"request_pending":true', system_state)
            self.assertNotIn(marker, system_state)
            self.assertIn(marker, lower_evidence)

    def test_mutable_channel_values_are_bounded_before_system_rendering(self) -> None:
        ctx = agent.ChannelContext(
            chat_id="CHAT_SYSTEM_OVERRIDE", room_id="ROOM_SYSTEM_OVERRIDE",
            is_dm=False, known=True, addressed=True,
            address_kind="KIND_SYSTEM_OVERRIDE", address_age_sec=2,
            missed_hours="MISSED_SYSTEM_OVERRIDE",  # type: ignore[arg-type]
            _scope_override="SCOPE_SYSTEM_OVERRIDE",
        )
        with tempfile.TemporaryDirectory(prefix="praxis_channel_roles_") as tmp:
            patchers = (
                mock.patch.object(agent, "ROOMS_DIR", Path(tmp)),
                mock.patch.object(agent, "INDEX_MD", Path(tmp) / "missing-index.md"),
                mock.patch.object(agent, "_persona_text", return_value="PERSONA"),
                mock.patch.object(agent, "_active_desires_block", return_value=""),
                mock.patch.object(agent, "build_state_block", return_value=""),
                mock.patch.object(agent, "build_state_evidence_block", return_value=""),
                mock.patch.object(agent, "read_summary", return_value=""),
                mock.patch.object(agent, "_participant_memory_block", return_value=""),
                mock.patch.object(agent, "_recall_block", return_value=""),
            )
            with contextlib.ExitStack() as stack:
                for patcher in patchers:
                    stack.enter_context(patcher)
                persona, dynamic, _ = agent._build_prompt_parts(ctx=ctx)
                presence = agent._presence_frame(ctx)

        system = persona + dynamic + presence
        for marker in (
            "ROOM_SYSTEM_OVERRIDE", "KIND_SYSTEM_OVERRIDE",
            "MISSED_SYSTEM_OVERRIDE", "SCOPE_SYSTEM_OVERRIDE",
        ):
            self.assertNotIn(marker, system)
        self.assertIn("audience_scope=unknown", system)
        self.assertIn("room_id=unknown", system)

    def test_evidence_is_attached_to_current_user_role(self) -> None:
        wrapped = agent._with_context_evidence("actual request", '{"content":"memory cue"}\n')
        self.assertIn("praxis_context_evidence", wrapped)
        self.assertIn("memory cue", wrapped)
        # ⚠ Реплика человека едет ПОД ГУТТЕРОМ — она такой же недоверенный текст, как тело
        # находки. Дословность байтов цела, меняется колонка; цена принята Праксис 06.08.
        # Тег при этом ПОДПИСАН, когда транспортные поля известны, поэтому здесь префикс.
        self.assertIn("<current_user_message", wrapped)
        self.assertIn("\n> actual request", wrapped)
    def test_blind_identity_load_is_omitted_from_prompt_layers(self) -> None:
        load_marker = "LOAD_MARKER_8_36"
        revision_marker = "REVISION_MARKER"
        with (
            mock.patch.object(agent.identity, "recent_shifts", return_value=[{
                "kind": "identity_shift", "ts": "2026-07-18T08:00:00Z",
                "text": revision_marker,
            }]),
            mock.patch.object(agent.identity, "stress", return_value=[{
                "theme": load_marker, "score": 8.36, "count": 10, "last": 1,
            }]),
        ):
            normal_state = agent.build_state_block()
            blind_state = agent.build_state_block(hide_identity_load=True)
            normal_evidence = agent.build_state_evidence_block()
            blind_evidence = agent.build_state_evidence_block(hide_identity_load=True)

        self.assertIn('"fact":"identity"', normal_state)
        self.assertIn('"active_strain_count":1', normal_state)
        self.assertIn('"fact":"identity"', blind_state)
        self.assertNotIn("active_strain_count", blind_state)
        self.assertIn(revision_marker, normal_evidence)
        self.assertIn(load_marker, normal_evidence)
        self.assertIn(revision_marker, blind_evidence)
        self.assertNotIn(load_marker, blind_evidence)

    def test_blind_goal_prefix_is_explicit(self) -> None:
        self.assertTrue(agent._is_blind_load_goal("слепой опыт: neutral task"))
        self.assertFalse(agent._is_blind_load_goal("neutral task"))


if __name__ == "__main__":
    unittest.main()
