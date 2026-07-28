from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import self_model


LEGACY = """# Self — legacy evidence

- _2026-01-01_ Наблюдение: один случай. Вывод: проверять факты до рассказа о себе.
- _2026-02-02_ Наблюдение: второй случай. Вывод: отвечать по сути, не описывать фильтр.
- _2026-03-03_ Наблюдение: третий случай. Вывод: тишина лучше сообщения без нового.
- _2026-04-04_ Наблюдение: четвёртый случай. Вывод: доводить работу до наблюдаемой проверки.
- _2026-05-05_ Наблюдение: пятый случай. Вывод: новый опыт может изменить прежний вывод.
"""

COMPACT = """# Кто я сейчас

Я проверяю факты до рассказа о себе и не выдаю механизм за переживание.

## Устойчивое

- Довожу работу до наблюдаемой проверки.
- Отвечаю по сути и не описываю внутренний фильтр.

## Живое изменение

- Новый подтверждённый опыт может изменить эту модель; старая версия останется в истории.
"""


class SelfModelTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="praxis_self_model_"))
        (self.base / "soul").mkdir(parents=True)
        (self.base / "soul" / "self.md").write_text(LEGACY, encoding="utf-8")
        self.store = self_model.SelfModel(self.base)

    def _observations(self):
        return [json.loads(line) for line in self.store.observations_path.read_text(encoding="utf-8").splitlines()]

    def test_prompt_fails_closed_and_never_reads_legacy(self):
        forbidden = mock.Mock()
        forbidden.read_bytes.side_effect = AssertionError("legacy prompt read")
        self.store.legacy_path = forbidden

        info = self.store.current_prompt_info()
        self.assertEqual(info.source, "missing")
        self.assertEqual(info.text, "")
        self.assertTrue(info.provenance["legacy_quarantined"])
        self.assertFalse(self.store.current_path.exists())
        self.assertFalse(self.store.history_dir.exists())
        self.assertFalse(self.store.observations_path.exists())

    def test_empty_or_corrupt_current_fails_closed_read_only(self):
        self.store.current_path.parent.mkdir(parents=True)
        self.store.current_path.write_text("broken", encoding="utf-8")
        before = self.store.current_path.read_bytes()
        info = self.store.current_prompt_info()
        self.assertEqual(info.source, "missing")
        self.assertEqual(info.text, "")
        self.assertEqual(self.store.current_path.read_bytes(), before)
        self.assertFalse(self.store.observations_path.exists())

    def test_unprovenanced_long_current_also_fails_closed(self):
        self.store.current_path.parent.mkdir(parents=True)
        self.store.current_path.write_text("# Unprovenanced\n\n" + "text " * 100, encoding="utf-8")
        info = self.store.current_prompt_info()
        self.assertEqual(info.source, "missing")
        self.assertEqual(info.text, "")

    def test_incomplete_marker_is_not_a_prompt_and_cannot_be_migrated_over(self):
        self.store.current_path.parent.mkdir(parents=True)
        raw = self.store._current_document(COMPACT, {
            "schema": self_model.SCHEMA,
            "revision": 7,
            "source": "forged",
        })
        self.store.current_path.write_bytes(raw)

        info = self.store.current_prompt_info()
        self.assertEqual(info.source, "missing")
        self.assertEqual(info.text, "")
        result = self.store.migrate(reason="must recover, not overwrite", compact_text=COMPACT)
        self.assertFalse(result["ok"])
        self.assertIn("invalid", result["error"])
        self.assertEqual(self.store.current_path.read_bytes(), raw)

    def test_provenanced_but_oversized_current_is_not_prompted_or_revised(self):
        self.store.current_path.parent.mkdir(parents=True)
        body = "# Current\n\n" + "x" * (self_model.MAX_CURRENT_CHARS + 1)
        raw = self.store._current_document(body, {
            "schema": self_model.SCHEMA,
            "revision": 7,
            "source": "forged",
        })
        self.store.current_path.write_bytes(raw)

        self.assertEqual(self.store.current_prompt_info().source, "missing")
        result = self.store.revise(COMPACT, reason="must not bless invalid current")
        self.assertFalse(result["ok"])
        self.assertIn("compact bounds", result["error"])
        self.assertEqual(self.store.current_path.read_bytes(), raw)

    def test_explicit_authored_migration_preserves_exact_legacy(self):
        original = self.store.legacy_path.read_bytes()
        result = self.store.migrate(
            reason="PASS 24 compact self",
            compact_text=COMPACT,
            evidence_refs=["run-25", "evt-1"],
            run_id="run-25",
            by="praxis",
        )
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["migrated"])
        self.assertEqual(self.store.legacy_path.read_bytes(), original)
        self.assertEqual((self.store.history_dir / "0000.md").read_bytes(), original)
        info = self.store.current_prompt_info()
        self.assertEqual(info.source, "current")
        self.assertEqual(info.text.strip(), COMPACT.strip())
        self.assertNotIn("praxis-self-current", info.text)
        self.assertEqual(info.revision, 0)
        self.assertEqual(info.provenance["run_id"], "run-25")
        self.assertEqual(info.provenance["derivation"], "authored")
        event = self._observations()[0]
        self.assertEqual(event["kind"], "migration")
        self.assertTrue(event["meta"]["legacy_untouched"])
        self.assertEqual(event["evidence_refs"], ["run-25", "evt-1"])

        again = self.store.migrate(reason="retry", compact_text=COMPACT)
        self.assertTrue(again["ok"])
        self.assertFalse(again["migrated"])
        self.assertEqual(len(self._observations()), 1)

    def test_default_migration_is_bounded_evidence_extraction(self):
        result = self.store.migrate(reason="deterministic bootstrap")
        self.assertTrue(result["ok"], result)
        info = self.store.current_prompt_info()
        self.assertLessEqual(len(info.text), self_model.MAX_CURRENT_CHARS)
        self.assertIn("проверять факты", info.text)
        self.assertEqual(info.provenance["derivation"], "deterministic_extraction")

    def test_revision_archives_exact_current_and_records_provenance(self):
        self.store.migrate(reason="bootstrap", compact_text=COMPACT)
        previous = self.store.current_path.read_bytes()
        changed = COMPACT.replace(
            "Новый подтверждённый опыт",
            "Результат run, подтверждённый артефактом,",
        )
        result = self.store.revise(
            changed,
            reason="run outcome changed the model",
            evidence_refs=["memory/runs/run-77/RECAP.md", "artifact:sha256:abc"],
            run_id="run-77",
            confidence="observed",
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["revision"], 1)
        self.assertEqual((self.store.history_dir / "0001.md").read_bytes(), previous)
        self.assertEqual(self.store.current_prompt_info().text.strip(), changed.strip())
        provenance = self.store.current_prompt_info().provenance
        self.assertEqual(provenance["run_id"], "run-77")
        self.assertEqual(provenance["confidence"], "observed")
        self.assertEqual(provenance["evidence_refs"][0], "memory/runs/run-77/RECAP.md")
        self.assertEqual([item["version"] for item in self.store.history()], [0, 1])
        self.assertEqual([event["kind"] for event in self._observations()], ["migration", "revision"])

    def test_observation_is_evidence_not_an_implicit_rewrite(self):
        self.store.migrate(reason="bootstrap", compact_text=COMPACT)
        before = self.store.current_path.read_bytes()
        event = self.store.record_observation(
            "В двух runs Praxis проверила результат перед отчётом.",
            source="recap",
            evidence_refs=["run-a", "run-b"],
            run_id="run-b",
        )
        self.assertEqual(event["kind"], "observation")
        self.assertEqual(self.store.current_path.read_bytes(), before)

    def test_observation_append_repairs_only_a_torn_tail(self):
        self.store.record_observation("first", source="test")
        with self.store.observations_path.open("ab") as fh:
            fh.write(b'{"schema":"praxis.self.observation.v1"')
        self.store.record_observation("second", source="test")
        events = self._observations()
        self.assertEqual([event["text"] for event in events], ["first", "second"])

    def test_observation_dedupe_is_restart_safe(self):
        first = self.store.record_observation(
            "one grounded run reflection", source="recap", dedupe_key="run-1:self",
        )
        second = self_model.SelfModel(self.base).record_observation(
            "would have duplicated", source="recap", dedupe_key="run-1:self",
        )
        self.assertEqual(second["event_id"], first["event_id"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(len(self._observations()), 1)

    def test_distill_budget_never_cites_observations_cut_from_input(self):
        import consolidate

        self.store.migrate(reason="bootstrap", compact_text=COMPACT)
        events = [
            self.store.record_observation(
                f"evidence-marker-{index:02d} " + ("grounded detail " * 80),
                source="recap",
            )
            for index in range(40)
        ]

        evidence, refs = consolidate._self_distill_evidence(self.store)

        self.assertLessEqual(len(evidence), 32_000)
        cited = [ref.rsplit("#", 1)[-1] for ref in refs
                 if ref.startswith("memory/self/observations.jsonl#")]
        self.assertTrue(cited)
        self.assertLess(len(cited), len(events), "fixture must exercise the evidence budget")
        for event_id in cited:
            self.assertIn(event_id, evidence)
        self.assertIn(events[0]["event_id"], cited)

    def test_distill_consumed_recovery_does_not_forget_old_provenance(self):
        import consolidate

        old_ids = [f"selfevt-old-{index:04d}" for index in range(2_101)]
        recovered = consolidate._consumed_self_observation_ids(self.store, old_ids)

        self.assertEqual(recovered, old_ids)

    def test_revision_requires_explicit_migration_and_meaningful_text(self):
        self.assertFalse(self.store.revise(COMPACT, reason="premature")["ok"])
        self.store.migrate(reason="bootstrap", compact_text=COMPACT)
        self.assertFalse(self.store.revise("short", reason="bad")["ok"])
        self.assertFalse(self.store.revise(COMPACT, reason="same")["ok"])
        self.assertFalse(self.store.revise(COMPACT + "\nnew", reason=" ")["ok"])

    def test_rollback_reapplies_history_as_a_new_revision(self):
        self.store.migrate(reason="bootstrap", compact_text=COMPACT)
        changed = COMPACT.replace("Довожу работу", "Всегда довожу работу")
        self.assertTrue(self.store.revise(changed, reason="observed change")["ok"])
        current_before = self.store.current_path.read_bytes()

        result = self.store.rollback(1, reason="change was too broad", run_id="run-rb")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["restored_version"], 1)
        self.assertEqual(self.store.current_prompt_info().text.strip(), COMPACT.strip())
        self.assertEqual((self.store.history_dir / "0002.md").read_bytes(), current_before)
        provenance = self.store.current_prompt_info().provenance
        self.assertEqual(provenance["run_id"], "run-rb")
        self.assertEqual(provenance["operation"], "rollback")
        self.assertEqual(provenance["restored_version"], 1)
        event = self._observations()[-1]
        self.assertEqual(event["kind"], "rollback")
        self.assertEqual(event["meta"]["restored_version"], 1)
        self.assertIn("soul/self/history/0001.md", event["evidence_refs"])

    def test_rollback_rejects_missing_or_oversized_legacy(self):
        self.store.migrate(reason="bootstrap", compact_text=COMPACT)
        self.assertFalse(self.store.rollback(99, reason="missing")["ok"])
        # Version zero is exact legacy evidence.  This fixture is compact enough, so
        # make the bound explicit before checking that an oversized legacy stays archival.
        (self.store.history_dir / "0000.md").write_text("x" * (self_model.MAX_CURRENT_CHARS + 1),
                                                        encoding="utf-8")
        result = self.store.rollback(0, reason="do not bloat CURRENT")
        self.assertFalse(result["ok"])
        self.assertIn("too large", result["error"])


if __name__ == "__main__":
    unittest.main()
