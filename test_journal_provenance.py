from __future__ import annotations

import tempfile
import unittest
import hashlib
import json
import shutil
from pathlib import Path
from unittest import mock

import agent
import consolidate
import desires
import memory_fts
import memory_provenance
import self_model


_CURRENT_A = (
    "# Кто я сейчас\n\n"
    "Я опираюсь на проверяемые действия и сохраняю различие между событием, выводом и правилом. "
    "Неполное свидетельство остаётся вопросом, пока отдельный источник не подтвердит его.\n"
)
_CURRENT_B = (
    "# Кто я сейчас\n\n"
    "Я опираюсь на проверяемые действия и различаю событие, вывод, намерение и правило. "
    "Неполное свидетельство остаётся вопросом; независимый receipt или решение владельца может "
    "стать основанием для следующей аккуратной ревизии.\n"
)


class JournalProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="praxis_journal_provenance_"))

    def _store(self) -> self_model.SelfModel:
        store = self_model.SelfModel(self.base)
        store.legacy_path.parent.mkdir(parents=True, exist_ok=True)
        store.legacy_path.write_text(_CURRENT_A, encoding="utf-8")
        result = store.migrate(reason="test migration", compact_text=_CURRENT_A,
                               evidence_refs=["owner:test:migration"])
        self.assertTrue(result["ok"])
        return store

    def _event(self, memory: Path, *, source: str = "telegram",
               kind: str = "conversation_message", suffix: str = "00000000") -> str:
        event_id = f"evt-20260714T120000000000Z-{suffix}"
        path = memory / "life" / "events" / "2026-07-14.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "schema": "praxis.life.event.v1", "id": event_id,
            "ts": "2026-07-14T12:00:00.000Z", "kind": kind, "stream": "7",
            "chat_id": "7", "actor": "Owner", "direction": "in",
            "text": "primary evidence", "source": source, "source_id": "1",
            "salience": 2, "refs": [], "meta": {},
        }
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, separators=(",", ":")) + "\n")
        return event_id

    def _compact(self, memory: Path, event_ids: list[str], *, legacy: bool = False,
                 degraded: bool = False, suffix: str = "00000000") -> str:
        compact_id = f"cmp-20260714T120000000000Z-{suffix}"
        path = memory / "life" / "compacts" / "7" / f"{compact_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "schema": "praxis.life.compact.v1", "id": compact_id, "chat_id": "7",
            "created_at": "2026-07-14T12:00:00.000Z", "tier": 1, "depth": 1,
            "preservation_priority": 1.0, "source_event_ids": event_ids,
            "source_compact_ids": [], "event_count": len(event_ids),
            "continued": False, "legacy": legacy, "degraded": degraded,
            "first_ts": "2026-07-14T12:00:00.000Z" if event_ids else "",
            "last_ts": "2026-07-14T12:00:00.000Z" if event_ids else "",
            "path": path.relative_to(self.base).as_posix(),
        }
        path.write_text(
            f"<!-- praxis-compact: {json.dumps(meta, separators=(',', ':'))} -->\n"
            f"# Compact {compact_id}\n\n## Summary\nmodel recap\n",
            encoding="utf-8",
        )
        return compact_id

    def _claim(self, memory: Path, *, status: str, confidence: str = "observed",
               evidence: tuple[str, ...] | list[str] | None = None,
               text: str = "claim marker", subject: str = "Person",
               kind: str = "person", suffix: str = "00000000") -> Path:
        if evidence is None:
            event_id = self._event(memory, suffix=suffix)
            evidence = [self._compact(memory, [event_id], suffix=suffix)]
        key = hashlib.sha256(f"{subject.casefold()}\0{text.casefold()}".encode()).hexdigest()[:16]
        claim_id = f"clm-{key}"
        path = memory / "life" / "claims" / f"{claim_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "schema": memory_provenance.CLAIM_SCHEMA,
            "id": claim_id,
            "subject": subject,
            "kind": kind,
            "status": status,
            "confidence": confidence,
            "salience": 2,
            "visibility": "private",
            "evidence_ids": sorted(set(evidence)),
            "contradicts": [],
            "updated_at": "2026-07-14T12:00:00.000Z",
            "last_run": "rr-20260714T120000000000Z-00000000",
            "path": path.relative_to(self.base).as_posix(),
        }
        path.write_text(
            f"<!-- praxis-claim: {json.dumps(meta, separators=(',', ':'))} -->\n"
            f"# Claim {claim_id}\n\n## Утверждение\n**{subject}** — {text}\n\n"
            f"## Статус\n- status: `{status}`\n- confidence: `{confidence}`\n"
            f"- salience: `2`\n- visibility: `private`\n"
            f"- evidence: {', '.join(meta['evidence_ids']) or 'нет'}\n"
            f"- contradicts: нет\n\n## Revisions\n"
            f"- 2026-07-14T12:00:00.000Z · {meta['last_run']} · **{status}** · test\n",
            encoding="utf-8",
        )
        return path

    def test_diary_observation_is_preserved_but_not_normative_eligible(self):
        store = self._store()
        event = store.record_observation(
            "сырой вывод из старого дневника",
            source="memory/journal/2026-07-01.md",
            evidence_refs=["memory/journal/2026-07-01.md#entry-2"],
        )
        self.assertFalse(event["meta"]["normative_eligible"])
        self.assertFalse(memory_provenance.event_normative_eligible(event))
        self.assertTrue(store.observations_path.exists(), "episodic evidence must not be deleted")

    def test_self_revision_rejects_diary_ref_without_writing(self):
        store = self._store()
        before = store.current_path.read_bytes()
        result = store.revise(
            _CURRENT_B,
            reason="candidate revision",
            evidence_refs=["memory/journal/2026-07-01.md#entry-2"],
            trigger="distill",
        )
        self.assertFalse(result["ok"])
        self.assertIn("untrusted episodic", result["error"])
        self.assertEqual(store.current_path.read_bytes(), before)

    def test_desire_cannot_be_rooted_in_diary_but_run_evidence_is_allowed(self):
        ledger = desires.DesireLedger(self.base)
        with self.assertRaisesRegex(ValueError, "untrusted episodic"):
            ledger.notice(
                "переписать себя по дневнику",
                source="journal",
                why_it_matters="так написано",
                evidence_refs=["memory/journal/2026-07-01.md"],
            )
        event = ledger.notice(
            "проверить реальный результат run",
            source="run receipt",
            why_it_matters="результат наблюдаем",
            evidence_refs=["memory/runs/run-1/RECAP.md"],
        )
        self.assertEqual(event["stage"], "noticed")

    def test_fts_keeps_diary_searchable_with_an_explicit_source_type(self):
        memory = self.base / "memory"
        journal = memory / "journal" / "2026-07-01.md"
        journal.parent.mkdir(parents=True)
        journal.write_text("# log\n\n- episodic cue\n", encoding="utf-8")
        (memory / "reflections.md").write_text("# old derived reflection\n", encoding="utf-8")
        sources = {row.rel: row.kind for row in memory_fts.iter_sources(
            base=self.base, memory_dir=memory,
        )}
        self.assertEqual(sources["memory/journal/2026-07-01.md"], "journal_episode")
        self.assertEqual(sources["memory/reflections.md"], "journal_reflection")
        memory_fts.rebuild(base=self.base, memory_dir=memory)
        hits = memory_fts.search("episodic cue", base=self.base, memory_dir=memory,
                                 scope="owner")
        self.assertTrue(any(row["source_type"] == "journal_episode" for row in hits))

    def test_claim_status_is_indexed_and_restricted_before_automatic_top_k(self):
        memory = self.base / "memory"
        observed = self._claim(memory, status="supported", text="trustgap marker observed",
                               suffix="00000001")
        inferred = self._claim(memory, status="supported", confidence="inferred",
                               text="trustgap marker inferred", suffix="00000002")
        contested = self._claim(memory, status="contested", text="trustgap marker contested",
                                suffix="00000003")
        unsupported = self._claim(memory, status="unsupported", text="trustgap marker unsupported",
                                  suffix="00000004")
        invalid = self._claim(memory, status="supported", evidence=(),
                              text="trustgap marker invalid", suffix="00000005")

        sources = {row.path.name: row.kind for row in memory_fts.iter_sources(
            base=self.base, memory_dir=memory,
        )}
        self.assertEqual(sources[observed.name],
                         memory_provenance.CLAIM_SUPPORTED_OBSERVED_KIND)
        self.assertEqual(sources[inferred.name],
                         memory_provenance.CLAIM_SUPPORTED_INFERRED_KIND)
        self.assertEqual(sources[contested.name], memory_provenance.CLAIM_CONTESTED_KIND)
        self.assertEqual(sources[unsupported.name], memory_provenance.CLAIM_UNSUPPORTED_KIND)
        self.assertEqual(sources[invalid.name], memory_provenance.CLAIM_INVALID_KIND)

        memory_fts.rebuild(base=self.base, memory_dir=memory)
        explicit = memory_fts.search(
            "trustgap marker", base=self.base, memory_dir=memory, scope="owner", limit=30,
        )
        automatic = memory_fts.search(
            "trustgap marker", base=self.base, memory_dir=memory, scope="owner", limit=30,
            purpose="automatic",
        )
        # Telegram conversation evidence is a valid historical cue, not direct
        # observation of the proposition; both projections stay labelled inference.
        self.assertEqual({row["source_type"] for row in automatic}, {
            memory_provenance.CLAIM_SUPPORTED_INFERRED_KIND,
        })
        self.assertTrue({
            memory_provenance.CLAIM_CONTESTED_KIND,
            memory_provenance.CLAIM_UNSUPPORTED_KIND,
            memory_provenance.CLAIM_INVALID_KIND,
        }.issubset({row["source_type"] for row in explicit}))

    def test_blocked_rows_cannot_starve_safe_automatic_hit(self):
        memory = self.base / "memory"
        for index in range(30):
            journal = memory / "journal" / f"2026-06-{index + 1:02}.md"
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text(f"# journal\n\nstarvation marker {index}\n", encoding="utf-8")
            person = memory / "people" / f"legacy-{index:02}.md"
            person.parent.mkdir(parents=True, exist_ok=True)
            person.write_text(f"# legacy\n\nstarvation marker {index}\n", encoding="utf-8")
        safe = self.base / "soul" / "skills" / "safe.md"
        safe.parent.mkdir(parents=True, exist_ok=True)
        safe.write_text("# Skill\n\nstarvation marker safe skill\n", encoding="utf-8")

        memory_fts.rebuild(base=self.base, memory_dir=memory, skills_dir=safe.parent)
        hits = memory_fts.search(
            "starvation marker", base=self.base, memory_dir=memory, scope="owner", limit=1,
            purpose="automatic", skills_dir=safe.parent,
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["path"], "soul/skills/safe.md")

    def test_people_projection_revalidates_referenced_claim_status(self):
        memory = self.base / "memory"
        safe_claim = self._claim(memory, status="supported", text="separate evidence safe",
                                 suffix="00000011")
        bad_claim = self._claim(memory, status="contested", text="separate evidence bad",
                                suffix="00000012")
        people = memory / "people"
        people.mkdir(parents=True)
        (people / "safe.md").write_text(
            f"# Safe\n\n- projection marker safe [source:{safe_claim.stem}]\n", encoding="utf-8",
        )
        (people / "bad.md").write_text(
            f"# Bad\n\n- projection marker poison [source:{bad_claim.stem}]\n", encoding="utf-8",
        )

        memory_fts.rebuild(base=self.base, memory_dir=memory)
        automatic = memory_fts.search(
            "projection marker", base=self.base, memory_dir=memory, scope="owner", limit=10,
            purpose="automatic",
        )
        explicit = memory_fts.search(
            "projection marker", base=self.base, memory_dir=memory, scope="owner", limit=10,
        )
        self.assertEqual(automatic, [])
        self.assertEqual({row["path"] for row in explicit}, {
            "memory/people/safe.md", "memory/people/bad.md",
        })

    def test_automatic_recall_excludes_diary_but_explicit_recall_labels_it(self):
        hits = [
            {"path": "memory/journal/2026-07-01.md", "source": "2026-07-01",
             "source_type": "journal_episode", "text": "poison marker", "provenance": []},
            {"path": "memory/runs/run-1/RECAP.md", "source": "run-1",
             "source_type": "markdown", "text": "verified marker", "provenance": []},
            {"path": "soul/skills/verified.md", "source": "verified",
             "source_type": "skill", "text": "verified marker",
             "automatic_canonical": True, "provenance": []},
            {"path": "soul/self/history/0001.md", "source": "0001",
             "source_type": "self_history", "text": "legacy identity marker", "provenance": []},
        ]
        with mock.patch.object(agent.memory_index, "search", return_value=hits), \
                mock.patch.object(agent, "_graph_related", return_value=[]):
            automatic = agent._recall_block("marker")
            explicit = agent.tool_recall("marker")
        self.assertNotIn("poison marker", automatic)
        self.assertNotIn("legacy identity marker", automatic)
        self.assertIn("verified marker", automatic)
        self.assertIn("UNTRUSTED EPISODIC", explicit)
        self.assertIn("poison marker", explicit)
        self.assertIn("ARCHIVED NON-CURRENT SELF EVIDENCE", explicit)
        self.assertIn("legacy identity marker", explicit)

    def test_automatic_recall_blocks_contested_claim_and_labels_supported_inference(self):
        projection, _, _ = memory_provenance.claim_projection({
            "subject": "Person", "_statement": "supported inference marker",
            "_automatic_kind": memory_provenance.CLAIM_SUPPORTED_INFERRED_KIND,
            "_automatic_eligible": True,
        })
        hits = [
            {"path": "memory/life/claims/clm-contested.md", "source": "clm-contested",
             "source_type": memory_provenance.CLAIM_CONTESTED_KIND,
             "text": "contested marker", "provenance": []},
            {"path": "memory/life/claims/clm-inferred.md", "source": "clm-inferred",
             "source_type": memory_provenance.CLAIM_SUPPORTED_INFERRED_KIND,
             "text": projection, "automatic_canonical": True, "provenance": []},
        ]
        with mock.patch.object(agent.memory_index, "search", return_value=hits), \
                mock.patch.object(agent, "_graph_related", return_value=[]):
            automatic = agent._recall_block("marker")
            explicit = agent.tool_recall("marker")
        self.assertNotIn("contested marker", automatic)
        self.assertIn("SUPPORTED INFERENCE", automatic)
        self.assertIn("supported inference marker", automatic)
        self.assertIn("CONTESTED CLAIM - NOT CURRENT FACT", explicit)
        self.assertIn("contested marker", explicit)
        self.assertIn("CLAIM RECEIPTS CARRY STATUS", explicit)

    def test_legacy_vector_cache_restores_archived_self_trust_type(self):
        legacy = {"records": [{
            "id": "soul/self/history/0001.md#0",
            "path": "soul/self/history/0001.md",
            "source": "0001",
            "text": "legacy identity marker",
            "vector": [1.0],
        }]}
        with mock.patch.object(agent.memory_index, "_embeddings_on", return_value=True), \
                mock.patch.object(agent.memory_index, "embed", return_value=[1.0]), \
                mock.patch.object(agent.memory_index, "_ensure_index", return_value=legacy):
            hits = agent.memory_index._vector_candidates("legacy", 4, "owner")
        self.assertEqual(hits[0]["source_type"], memory_provenance.ARCHIVED_SELF_KIND)

    def test_legacy_vector_cache_cannot_mislabel_contested_claim_as_markdown(self):
        memory = self.base / "memory"
        claim = self._claim(memory, status="contested", text="legacy vector poison",
                            suffix="00000021")
        legacy = {"records": [{
            "id": f"memory/life/claims/{claim.name}#0",
            "path": f"memory/life/claims/{claim.name}",
            "source": claim.stem,
            "source_type": "markdown",
            "text": "legacy vector poison",
            "vector": [1.0],
        }]}
        with mock.patch.object(agent.memory_index, "BASE", self.base), \
                mock.patch.object(agent.memory_index, "_embeddings_on", return_value=True), \
                mock.patch.object(agent.memory_index, "embed", return_value=[1.0]), \
                mock.patch.object(agent.memory_index, "_ensure_index", return_value=legacy):
            explicit = agent.memory_index._vector_candidates("poison", 4, "owner")
            automatic = agent.memory_index._vector_candidates(
                "poison", 4, "owner", purpose="automatic",
            )
        self.assertEqual(explicit[0]["source_type"], memory_provenance.CLAIM_CONTESTED_KIND)
        self.assertEqual(automatic, [])

    def test_explicit_diary_recall_does_not_block_independent_run_evidence(self):
        store = self._store()
        hits = [{"path": "memory/journal/2026-07-01.md", "source": "2026-07-01",
                 "source_type": "journal_episode", "text": "cue", "provenance": []}]
        with mock.patch.object(agent.memory_index, "search", return_value=hits), \
                mock.patch.object(agent, "_graph_related", return_value=[]):
            self.assertIn("UNTRUSTED EPISODIC", agent.tool_recall("cue"))
        result = store.revise(
            _CURRENT_B,
            reason="independently verified run",
            evidence_refs=["memory/runs/run-verified/RECAP.md", "owner:decision:42"],
            trigger="tool",
        )
        self.assertTrue(result["ok"])

    def test_consolidator_never_reads_or_promotes_journal_prose(self):
        memory = self.base / "memory"
        journal = memory / "journal" / "2026-07-01.md"
        journal.parent.mkdir(parents=True)
        journal.write_text("# 2026-07-01\n\n- contaminated prose stays byte-identical\n",
                           encoding="utf-8")
        before = journal.read_bytes()
        with mock.patch.object(consolidate, "MARKER", memory / ".consolidated.json"), \
                mock.patch.object(consolidate, "SELF_DISTILL_MARK", memory / ".self_distilled.json"), \
                mock.patch.object(consolidate, "_maybe_distill_self",
                                  side_effect=AssertionError("nightly consolidate must not revise self")), \
                mock.patch.object(consolidate, "_collect",
                                  side_effect=AssertionError("must not collect diary prose")), \
                mock.patch.object(consolidate, "_extract", side_effect=AssertionError("must not read diary")), \
                mock.patch.object(consolidate, "_reflect", side_effect=AssertionError("must not read diary")), \
                mock.patch.object(consolidate, "_apply_person",
                                  side_effect=AssertionError("must not mutate people or graph")), \
                mock.patch.object(consolidate.memory_index, "rebuild_index_hooks"), \
                mock.patch.object(consolidate.memory_index, "build"):
            result = consolidate.run(days=["2026-07-01"])
        self.assertIn("автопереносов в durable memory: 0", result)
        self.assertIn("normative mutations: 0", result)
        self.assertEqual(journal.read_bytes(), before)

    def test_owner_clarification_is_archived_with_exact_provenance_and_rolls_back(self):
        # Раньше этот тест пинил ЖИВОЙ soul/self репозитория к revision 1 (owner-clarification).
        # Но её self — живой и самоавторский (§1.1): она законно переросла его в r2 (by=praxis,
        # night_pass), и пин противоречит инварианту «никто не пинит её soul-файлы». Плюс её
        # текущая история (0002.md) вне git-канона ломала проверку в клоне. Проверяем ту же
        # ГАРАНТИЮ — owner-clarification архивируется с точным провенансом и откат работает —
        # на изолированной фикстуре, не трогая и не пиная её живую душу.
        store = self._store()  # migrate -> базовая версия
        baseline_rev = store.current_prompt_info().revision

        # evidence_ref без слов journal/diary/дневник — иначе анти-инъекционный классификатор
        # (require_normative_provenance) справедливо отвергнет источник как эпизодический.
        clarification_ref = "owner-decision:2026-07-14:episodic-not-normative"
        result = store.revise(
            _CURRENT_B,
            reason="owner clarified the accumulated record is episodic, not normative",
            evidence_refs=[clarification_ref],
            by="owner-clarification",
            trigger="owner_clarification",
        )
        self.assertTrue(result["ok"], result)

        live = store.current_prompt_info()
        self.assertEqual(live.source, "current")
        self.assertEqual(live.provenance["by"], "owner-clarification")
        self.assertIn(clarification_ref, live.provenance["evidence_refs"])
        # Провенанс-цепочка цела: архив прежней версии существует и его sha точно совпадает.
        archived = self.base / live.provenance["history"]
        self.assertTrue(archived.is_file(), "прежняя версия заархивирована")
        self.assertEqual(hashlib.sha256(archived.read_bytes()).hexdigest(),
                         live.provenance["source_sha256"])

        # Откат к прежней версии обратим и не теряет историю (новая ревизия поверх).
        prev_rev = live.revision
        rb = store.rollback(
            baseline_rev,
            reason="test reversible owner clarification",
            evidence_refs=["owner:test:rollback"],
            by="test",
        )
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(rb["restored_version"], baseline_rev)
        self.assertEqual(store.current_prompt_info().revision, prev_rev + 1)


if __name__ == "__main__":
    unittest.main()
