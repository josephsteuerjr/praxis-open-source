from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent
import desires
import run_context
import self_model


LEGACY = "# Legacy self\n\nUNIQUE_LEGACY_SENTINEL " + ("old evidence " * 30) + "\n"
CURRENT = (
    "# Кто я сейчас\n\nUNIQUE_CURRENT_SENTINEL. Я меняю представление о себе только "
    "из проверяемого опыта и сохраняю прежнюю версию как evidence.\n\n"
    "## Как я действую\n\nДовожу действие до наблюдаемого результата и называю остаток.\n"
)


class SelfDesireIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.soul = self.base / "soul"
        self.soul.mkdir(parents=True)
        (self.soul / "SOUL.md").write_text("# Soul\n\ntruth\n", encoding="utf-8")
        (self.soul / "VOICE.md").write_text("# Voice\n\nplain\n", encoding="utf-8")
        (self.soul / "self.md").write_text(LEGACY, encoding="utf-8")
        self.store = self_model.SelfModel(self.base)
        self.store.migrate(reason="test", compact_text=CURRENT)
        self.patches = [
            mock.patch.object(agent, "BASE", self.base),
            mock.patch.object(agent, "SOUL_DIR", self.soul),
            mock.patch.object(agent, "MEM_DIR", self.base / "memory"),
            mock.patch.object(agent, "_CURRENT_SCOPE", "owner"),
            mock.patch.object(agent, "_CURRENT_CHAT", None),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.tmp.cleanup()

    def test_persona_uses_current_and_never_legacy_after_migration(self):
        persona = agent._persona_text()
        self.assertIn("UNIQUE_CURRENT_SENTINEL", persona)
        self.assertNotIn("UNIQUE_LEGACY_SENTINEL", persona)

    def test_update_self_records_observation_without_rewriting_either_self(self):
        legacy = self.store.legacy_path.read_bytes()
        current = self.store.current_path.read_bytes()
        result = agent.tool_update_self("В двух проверенных runs я сначала смотрела результат.")
        self.assertIn("CURRENT не меняла", result)
        self.assertEqual(self.store.legacy_path.read_bytes(), legacy)
        self.assertEqual(self.store.current_path.read_bytes(), current)
        events = [json.loads(line) for line in self.store.observations_path.read_text(
            encoding="utf-8").splitlines()]
        self.assertEqual(events[-1]["kind"], "observation")

    def test_active_run_is_linked_to_causal_desire_and_visible_in_prompt(self):
        noticed = json.loads(agent.tool_manage_desire(
            "notice", statement="научиться уверенно работать с форумными топиками",
            source="Micellium", why_it_matters="разговоры не должны смешиваться",
            next_move="проверить topic routing",
        ))
        did = noticed["desire_id"]
        agent.tool_manage_desire("want", desire_id=did, note="это повторяемая реальная боль")
        agent.tool_manage_desire("choose", desire_id=did, note="беру topic routing в PASS 24")
        run = run_context.RunContext.create(
            kind="computer", goal="topic routing", principal_id="owner", scope="owner",
            run_id="run-topic",
        )
        with run_context.bind_run(run):
            acted = json.loads(agent.tool_manage_desire(
                "act", desire_id=did, note="запустила отдельный проверяемый run",
            ))
        self.assertIn("run-topic", acted["state"]["run_ids"])
        block = agent._active_desires_block()
        self.assertIn(did, block)
        self.assertIn("проверить topic routing", block)

    def test_promotion_is_idempotent_and_observes_but_does_not_satisfy(self):
        ledger = desires.DesireLedger(self.base)
        event = ledger.notice(
            "довести run до evidence", source="test", why_it_matters="truth",
        )
        did = event["desire_id"]
        ledger.want(did, note="want")
        ledger.choose(did, note="choose")
        ledger.act(did, note="act", run_id="run-promote")
        run_dir = self.base / "memory" / "runs" / "2026-07" / "run-promote"
        run_dir.mkdir(parents=True)
        recap = run_dir / "RECAP.md"
        recap.write_text(
            "# RECAP\n\n## My reflection\n\nЯ увидела фактический результат и остаток.\n\n"
            "## Evidence\n\n- result\n",
            encoding="utf-8",
        )
        ctx = run_context.RunContext.create(
            kind="test", goal="promote", principal_id="owner", scope="owner",
            run_id="run-promote",
        )
        with mock.patch.object(agent.run_manager, "life_event_promotion", return_value="evt-run"):
            agent._promote_run(ctx, recap, {"status": "done", "recap": {"path": "RECAP.md"}})
            agent._promote_run(ctx, recap, {"status": "done", "recap": {"path": "RECAP.md"}})
        state = ledger.get(did)
        self.assertEqual(state["stage"], "observed")
        self.assertEqual(state["status"], "active")
        observation_events = [
            row for row in ledger.events(did) if row.get("stage") == "observed"
        ]
        self.assertEqual(len(observation_events), 1)
        self_events = [json.loads(line) for line in self.store.observations_path.read_text(
            encoding="utf-8").splitlines()]
        self.assertEqual(sum(row.get("kind") == "run_reflection" for row in self_events), 1)


if __name__ == "__main__":
    unittest.main()
