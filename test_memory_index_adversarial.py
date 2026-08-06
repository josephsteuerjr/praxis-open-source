from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import memory_fts
import memory_provenance


class MemoryIndexAdversarialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="praxis_memory_adversarial_")
        self.base = Path(self.tmp.name)
        self.memory = self.base / "memory"
        self.skills = self.base / "soul" / "skills"
        self.memory.mkdir(parents=True)
        self.skills.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _search(self, query: str, *, purpose: str = "explicit") -> list[dict]:
        return memory_fts.search(
            query,
            base=self.base,
            memory_dir=self.memory,
            skills_dir=self.skills,
            scope="owner",
            purpose=purpose,
            limit=30,
        )

    def _event(
        self,
        *,
        suffix: str,
        kind: str = "owner_clarification",
        source: str = "telegram",
        meta: dict | None = None,
        source_id: str | None = None,
    ) -> str:
        event_id = f"evt-20260714T120000000000Z-{suffix}"
        path = self.memory / "life" / "events" / "2026-07-14.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "schema": "praxis.life.event.v1",
            "id": event_id,
            "ts": "2026-07-14T12:00:00.000Z",
            "kind": kind,
            "stream": "owner",
            "chat_id": "owner",
            "actor": "Owner",
            "direction": "in",
            "text": "primary evidence",
            "source": source,
            "source_id": source_id if source_id is not None else suffix,
            "salience": 2,
            "refs": [],
            "meta": (
                {"authenticated_owner": True, "principal_id": "telegram:owner"}
                if meta is None and kind == "owner_clarification" and source == "telegram"
                else dict(meta or {})
            ),
        }
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        return event_id

    def _compact(self, event_id: str, *, suffix: str, legacy: bool) -> str:
        compact_id = f"cmp-20260714T120000000000Z-{suffix}"
        path = self.memory / "life" / "compacts" / "owner" / f"{compact_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "schema": "praxis.life.compact.v1",
            "id": compact_id,
            "chat_id": "owner",
            "created_at": "2026-07-14T12:00:00.000Z",
            "tier": 1,
            "depth": 1,
            "preservation_priority": 1.0,
            "source_event_ids": [event_id],
            "source_compact_ids": [],
            "event_count": 1,
            "continued": False,
            "legacy": legacy,
            "degraded": False,
            "first_ts": "2026-07-14T12:00:00.000Z",
            "last_ts": "2026-07-14T12:00:00.000Z",
            "path": path.relative_to(self.base).as_posix(),
        }
        path.write_text(
            f"<!-- praxis-compact: {json.dumps(meta, ensure_ascii=False, separators=(',', ':'))} -->\n"
            f"# Compact {compact_id}\n\n## Summary\nmodel-generated recap\n",
            encoding="utf-8",
        )
        return compact_id

    def _claim(
        self,
        *,
        statement: str,
        suffix: str,
        legacy: bool = False,
        revision: str = "ordinary revision note",
        confidence: str = "observed",
        event_kind: str = "owner_clarification",
        event_source: str = "telegram",
        event_meta: dict | None = None,
    ) -> Path:
        subject = "Adversarial Subject"
        event_id = self._event(
            suffix=suffix, kind=event_kind, source=event_source, meta=event_meta,
        )
        compact_id = self._compact(event_id, suffix=suffix, legacy=legacy)
        digest = hashlib.sha256(
            f"{subject.casefold()}\0{statement.casefold()}".encode("utf-8")
        ).hexdigest()[:16]
        claim_id = f"clm-{digest}"
        path = self.memory / "life" / "claims" / f"{claim_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        contradicts = ["clm-0000000000000000"]
        meta = {
            "schema": memory_provenance.CLAIM_SCHEMA,
            "id": claim_id,
            "subject": subject,
            "kind": "person",
            "status": "supported",
            "confidence": confidence,
            "salience": 2,
            "visibility": "private",
            "evidence_ids": [compact_id],
            "contradicts": contradicts,
            "updated_at": "2026-07-14T12:00:00.000Z",
            "last_run": "rr-20260714T120000000000Z-00000000",
            "path": path.relative_to(self.base).as_posix(),
        }
        path.write_text(
            f"<!-- praxis-claim: {json.dumps(meta, ensure_ascii=False, separators=(',', ':'))} -->\n"
            f"# Claim {claim_id}\n\n"
            f"## Statement\n**{subject}** \u2014 {statement}\n\n"
            "## Status\n"
            "- status: `supported`\n"
            f"- confidence: `{confidence}`\n"
            "- salience: `2`\n"
            "- visibility: `private`\n"
            f"- evidence: {compact_id}\n"
            f"- contradicts: {', '.join(contradicts)}\n\n"
            "## Revisions\n"
            f"- 2026-07-14T12:00:00.000Z - {revision}\n",
            encoding="utf-8",
        )
        return path

    def test_automatic_recall_uses_broad_canon_without_generated_or_bench_corpora(self) -> None:
        raw_event = self.memory / "life" / "events" / "2026-07-14.jsonl"
        raw_event.parent.mkdir(parents=True)
        raw_event.write_text(
            json.dumps(
                {
                    "schema": "praxis.life.event.v1",
                    "id": "evt-20260714T120000000000Z-deadbeef",
                    "ts": "2026-07-14T12:00:00.000Z",
                    "kind": "conversation_message",
                    "stream": "7",
                    "chat_id": "7",
                    "actor": "Somebody",
                    "direction": "in",
                    "text": "rawlife hydra marker",
                    "source": "telegram",
                    "source_id": "77",
                    "salience": 1,
                    "refs": [],
                    "meta": {},
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        run_context = self.memory / "runs" / "run-adversarial" / "context.md"
        run_context.parent.mkdir(parents=True)
        run_context.write_text("# Context\n\nruncontext basilisk marker\n", encoding="utf-8")
        bench = self.memory / "bench" / "scenes" / "adversarial.md"
        bench.parent.mkdir(parents=True)
        bench.write_text("# Bench\n\nbench chimera marker\n", encoding="utf-8")
        inventory = self.memory / "computer" / "inventory" / "windows-pc"
        inventory.mkdir(parents=True)
        (inventory / "20260714T120000Z.json").write_text(
            json.dumps(
                {
                    "schema": "praxis.computer.inventory.v1",
                    "device_id": "windows-pc",
                    "captured_at": "2026-07-14T12:00:00Z",
                    "observed_at": "2026-07-14T12:00:01Z",
                    "payload": {"apps": [{"name": "inventory manticore marker"}]},
                }
            ),
            encoding="utf-8",
        )
        generic = self.memory / "home.md"
        generic.write_text("# Home\n\ngeneric griffin marker\n", encoding="utf-8")
        skill = self.skills / "trusted.md"
        skill.write_text("# Skill\n\nskill phoenix marker\n", encoding="utf-8")
        (self.skills / "INDEX.md").write_text(
            "# Generated navigation\n\nskillindex sphinx marker\n", encoding="utf-8"
        )

        memory_fts.rebuild(
            base=self.base, memory_dir=self.memory, skills_dir=self.skills
        )

        # Praxis gets canonical source memory as context. Generated run context remains
        # explicitly searchable for audit, but never feeds itself into a later prompt.
        for query in (
            "rawlife hydra",
            "inventory manticore",
            "generic griffin",
            "skill phoenix",
        ):
            with self.subTest(query=query):
                hits = self._search(query, purpose="automatic")
                self.assertTrue(hits)
                self.assertTrue(all(row["automatic_canonical"] for row in hits))
        self.assertEqual(
            self._search("runcontext basilisk", purpose="automatic"), []
        )

        # 02.08.2026, решение Праксис и Егора по замеру: транспортный снимок хода
        # (`memory/runs/**/context.md`) больше не подмешивается в ОБЫЧНЫЙ явный recall.
        # Прежняя посылка «explicit recall sees run artifacts» была верна по намерению
        # («остаётся доступным для аудита»), но на практике означала другое: в
        # context.md лежит дословный origin_text прошлых реплик, поэтому FTS вытаскивал
        # его по точным словам на любом «вспомни», и её журнал конкурировал с 154 488
        # транспортными кусками (64.9% текста индекса). Граница теперь проходит по ЦЕЛИ
        # обращения: аудит достаёт всё, память отвечает своим. Файлы и куски на месте.
        for query in (
            "rawlife hydra",
            "inventory manticore",
            "generic griffin",
        ):
            with self.subTest(explicit=query):
                self.assertTrue(self._search(query))
        self.assertEqual(self._search("runcontext basilisk"), [])
        self.assertTrue(self._search("runcontext basilisk", purpose="audit"))
        # RECAP и события прогона — не транспорт, они остаются и в обычном recall.
        self.assertTrue(self._search("rawlife hydra"))
        self.assertEqual(self._search("bench chimera"), [])
        self.assertEqual(self._search("skillindex sphinx"), [])

        self.assertEqual(self._search("bench chimera", purpose="automatic"), [])
        self.assertEqual(self._search("skillindex sphinx", purpose="automatic"), [])
        allowed = self._search("skill phoenix", purpose="automatic")
        self.assertEqual(len(allowed), 1)
        self.assertEqual(allowed[0]["path"], "soul/skills/trusted.md")
        self.assertEqual(allowed[0]["source_type"], "skill")
        self.assertTrue(allowed[0]["automatic_canonical"])

    def test_sqlite_tamper_cannot_affect_automatic_canonical_recall(self) -> None:
        skill = self.skills / "tamperproof.md"
        canonical = "canonical skill narwhal marker"
        poison = "sqlite cache poison marker"
        persistent = "persistent sqlite poison marker"
        skill.write_text(f"# Skill\n\n{canonical}\n", encoding="utf-8")
        state = memory_fts.rebuild(
            base=self.base, memory_dir=self.memory, skills_dir=self.skills
        )
        database = self.base / state["database"]

        def tamper(text: str) -> None:
            db = sqlite3.connect(database)
            try:
                db.execute(
                    """UPDATE chunks
                       SET text = ?, terms = ?, source_type = 'skill', automatic_eligible = 1
                       WHERE path = 'soul/skills/tamperproof.md'""",
                    (text, memory_fts._terms(text)),
                )
                db.commit()
            finally:
                db.close()

        tamper(poison)
        self.assertEqual(self._search("sqlite cache poison", purpose="automatic"), [])
        recovered = self._search("canonical skill narwhal", purpose="automatic")
        self.assertEqual([row["text"] for row in recovered], [canonical])
        db = sqlite3.connect(database)
        try:
            stored = db.execute(
                "SELECT text FROM chunks WHERE path = 'soul/skills/tamperproof.md'"
            ).fetchone()[0]
        finally:
            db.close()
        # Automatic recall neither trusts nor synchronously repairs the disposable
        # explicit-search cache.  Its result above came straight from canon.
        self.assertEqual(stored, poison)

        # The live automatic path must stay canonical and read-only even when cache
        # maintenance is unavailable.  Explicit search/rebuild owns later repair.
        tamper(persistent)
        with mock.patch.object(memory_fts, "ensure", side_effect=AssertionError("hot-path ensure")), \
             mock.patch.object(memory_fts, "rebuild", side_effect=AssertionError("hot-path rebuild")):
            self.assertEqual(
                self._search("persistent sqlite poison", purpose="automatic"), []
            )
            recovered = self._search("canonical skill narwhal", purpose="automatic")
        self.assertEqual([row["text"] for row in recovered], [canonical])

    def test_automatic_recall_does_not_parse_excluded_run_sources(self) -> None:
        run_dir = self.memory / "runs" / "2026-07" / "run-expensive"
        run_dir.mkdir(parents=True)
        (run_dir / "context.md").write_text(
            "# Durable run context\n\nruncontext should stay explicit only\n",
            encoding="utf-8",
        )
        (run_dir / "events.jsonl").write_text(
            json.dumps({"kind": "run_event", "text": "run event receipt"}) + "\n",
            encoding="utf-8",
        )
        skill = self.skills / "fast-path.md"
        skill.write_text("# Skill\n\ncanonical fastpath phoenix\n", encoding="utf-8")
        original = memory_fts._source_chunks

        def guarded(source):
            if source.rel.startswith("memory/runs/"):
                raise AssertionError("automatic recall parsed an excluded run source")
            return original(source)

        with mock.patch.object(memory_fts, "_source_chunks", side_effect=guarded):
            rows = self._search("fastpath phoenix", purpose="automatic")
        self.assertEqual([row["text"] for row in rows], ["canonical fastpath phoenix"])

    def test_fts_shadow_only_poison_cannot_select_automatic_memory(self) -> None:
        canonical = "canonical shadowproof orca marker"
        poison = "shadowonly fts poison marker"
        skill = self.skills / "shadowproof.md"
        skill.write_text(f"# Skill\n\n{canonical}\n", encoding="utf-8")
        state = memory_fts.rebuild(
            base=self.base, memory_dir=self.memory, skills_dir=self.skills
        )
        database = self.base / state["database"]
        db = sqlite3.connect(database)
        try:
            row = db.execute(
                """SELECT id, text, terms, entities FROM chunks
                   WHERE path = 'soul/skills/shadowproof.md'"""
            ).fetchone()
            self.assertIsNotNone(row)
            row_id, text, terms, entities = row
            db.execute(
                """INSERT INTO chunks_fts(chunks_fts, rowid, text, terms, entities)
                   VALUES ('delete', ?, ?, ?, ?)""",
                (row_id, text, terms, entities),
            )
            db.execute(
                "INSERT INTO chunks_fts(rowid, text, terms, entities) VALUES (?, ?, ?, ?)",
                (row_id, poison, memory_fts._terms(poison), ""),
            )
            db.commit()
            unchanged = db.execute(
                "SELECT text FROM chunks WHERE id = ?", (row_id,)
            ).fetchone()[0]
        finally:
            db.close()
        self.assertEqual(unchanged, canonical, "only the FTS shadow was poisoned")

        self.assertEqual(self._search("shadowonly fts poison", purpose="automatic"), [])
        recovered = self._search("canonical shadowproof orca", purpose="automatic")
        self.assertEqual([row["text"] for row in recovered], [canonical])

    def test_run_derived_events_remain_explicit_without_becoming_automatic_cues(self) -> None:
        run_id = "run-20260715T151544000000Z-echo"
        run_text = "recursive social pulse wake prompt marker"
        self._event(
            suffix="run-echo",
            kind="run_episode",
            source="run_manager",
            source_id=run_id,
            meta={
                "run_id": run_id,
                "status": "done",
                "recap": f"memory/runs/2026-07/{run_id}/RECAP.md",
                "context_snapshot": f"memory/runs/2026-07/{run_id}/context.md",
            },
        )
        life_path = self.memory / "life" / "events" / "2026-07-14.jsonl"
        rows = [json.loads(line) for line in life_path.read_text(encoding="utf-8").splitlines()]
        rows[-1]["text"] = run_text
        life_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )

        self_events = self.memory / "self" / "observations.jsonl"
        self_events.parent.mkdir(parents=True, exist_ok=True)
        self_events.write_text(
            json.dumps({
                "id": "self-run-reflection",
                "ts": "2026-07-15T15:16:00Z",
                "kind": "run_reflection",
                "text": "full generated run reflection echo marker",
            }, ensure_ascii=False) + "\n" + json.dumps({
                "id": "self-lived-observation",
                "ts": "2026-07-15T15:17:00Z",
                "kind": "self_observation",
                "text": "compact lived observation marker",
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        explicit_run = self._search("recursive social pulse wake prompt", purpose="explicit")
        self.assertEqual(len(explicit_run), 1)
        self.assertIn(run_text, explicit_run[0]["text"])
        self.assertEqual(self._search("recursive social pulse wake prompt", purpose="automatic"), [])

        explicit_reflection = self._search("full generated run reflection", purpose="explicit")
        self.assertTrue(any(
            "full generated run reflection echo marker" in row["text"]
            for row in explicit_reflection
        ))
        self.assertEqual(self._search("full generated run reflection", purpose="automatic"), [])
        automatic_observation = self._search("compact lived observation", purpose="automatic")
        self.assertEqual(len(automatic_observation), 1)

    def test_automatic_gate_rejects_unknown_types_and_unsafe_paths(self) -> None:
        gate = memory_provenance.automatic_recall_allowed
        self.assertTrue(
            gate(source_type="skill", path="soul/skills/safe.md", text="safe")
        )
        self.assertFalse(
            gate(
                source_type="markdown",
                path="memory/runs/2026-07/run-example/context.md",
                text="CANONICAL AUTOMATIC MEMORY CUES: recursive",
            )
        )
        self.assertFalse(
            gate(
                source_type="markdown",
                path="memory/runs/2026-07/run-example/RECAP.md",
                text="generated recap",
            )
        )
        self.assertTrue(
            gate(source_type="markdown", path="memory/home.md", text="household fact")
        )
        for source_type, path in (
            ("", "soul/skills/safe.md"),
            ("unknown", "soul/skills/safe.md"),
            ("skill", ""),
            ("skill", "C:/Praxis/soul/skills/safe.md"),
            ("skill", "/opt/praxis/soul/skills/safe.md"),
            ("skill", "../soul/skills/safe.md"),
            ("skill", "soul/skills/../safe.md"),
            ("skill", "soul/skills/INDEX.md"),
            ("skill", "soul/skills/_generated.md"),
            ("skill", r"\\server\share\soul\skills\safe.md"),
        ):
            with self.subTest(source_type=source_type, path=path):
                self.assertFalse(
                    gate(source_type=source_type, path=path, text="safe")
                )

    def test_claim_body_mirrors_only_validate_inside_exact_status_section(self) -> None:
        claim = self._claim(
            statement="sectionbound kelpie marker", suffix="5ec71001"
        )
        kind, _ = memory_provenance.claim_source(claim)
        self.assertEqual(kind, memory_provenance.CLAIM_SUPPORTED_OBSERVED_KIND)

        text = claim.read_text(encoding="utf-8")
        status_at = text.index("## Status")
        revisions_at = text.index("## Revisions")
        mirrors = [
            line for line in text[status_at:revisions_at].splitlines()[1:] if line
        ]
        old_revisions = text[revisions_at + len("## Revisions"):].strip()
        claim.write_text(
            text[:status_at]
            + "## Status\n\n## Revisions\n"
            + "\n".join([*mirrors, old_revisions])
            + "\n",
            encoding="utf-8",
        )
        tampered_kind, tampered = memory_provenance.claim_source(claim)
        self.assertEqual(tampered_kind, memory_provenance.CLAIM_INVALID_KIND)
        self.assertFalse(tampered.get("_automatic_eligible"))

    def test_supported_uncertain_claim_remains_explicit_only(self) -> None:
        self._claim(
            statement="uncertain hippocampus marker",
            suffix="5ec71002",
            confidence="uncertain",
        )
        memory_fts.rebuild(
            base=self.base, memory_dir=self.memory, skills_dir=self.skills
        )
        explicit = self._search("uncertain hippocampus")
        self.assertTrue(explicit)
        self.assertTrue(
            any(
                row["source_type"]
                == memory_provenance.CLAIM_SUPPORTED_UNCERTAIN_KIND
                for row in explicit
            )
        )
        self.assertEqual(
            self._search("uncertain hippocampus", purpose="automatic"), []
        )

    def test_source_kind_pairs_default_deny_but_telegram_messages_remain_cues(self) -> None:
        self._claim(
            statement="authenticated direct dryad marker", suffix="5ec71003"
        )
        self._claim(
            statement="telegram conversation nymph marker",
            suffix="5ec71004",
            event_kind="conversation_message",
            event_meta={},
        )
        self._claim(
            statement="unauthenticated owner siren marker",
            suffix="5ec71005",
            event_meta={},
        )
        self._claim(
            statement="formation source golem marker",
            suffix="5ec71006",
            event_source="formation",
            event_meta={"authenticated_owner": True, "principal_id": "telegram:owner"},
        )
        self._claim(
            statement="model unknown wraith marker",
            suffix="5ec71007",
            event_kind="model_guess",
            event_source="model",
            event_meta={},
        )
        memory_fts.rebuild(
            base=self.base, memory_dir=self.memory, skills_dir=self.skills
        )

        direct = self._search("authenticated direct dryad", purpose="automatic")
        self.assertEqual(
            {row["source_type"] for row in direct},
            {memory_provenance.CLAIM_SUPPORTED_OBSERVED_KIND},
        )
        conversation = self._search(
            "telegram conversation nymph", purpose="automatic"
        )
        self.assertEqual(
            {row["source_type"] for row in conversation},
            {
                "life_event",
                memory_provenance.CLAIM_SUPPORTED_INFERRED_KIND,
            },
        )
        for query in ("siren", "golem", "wraith"):
            with self.subTest(query=query):
                self.assertTrue(self._search(query), "receipt remains explicitly readable")
                self.assertEqual(self._search(query, purpose="automatic"), [])

    def test_event_and_compact_v1_are_fully_typed_and_bound(self) -> None:
        event_id = self._event(suffix="5ec71008")
        event_path = self.memory / "life" / "events" / "2026-07-14.jsonl"
        row = json.loads(event_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertTrue(memory_provenance._valid_event(row, event_path))
        invalid_events = []
        missing = dict(row)
        missing.pop("meta")
        invalid_events.append(("missing required key", missing, event_path))
        invalid_events.append(("bool salience", {**row, "salience": True}, event_path))
        invalid_events.append(("extra key", {**row, "surprise": 1}, event_path))
        invalid_events.append(("wrong stream", {**row, "stream": "other"}, event_path))
        invalid_events.append((
            "id timestamp mismatch",
            {**row, "ts": "2026-07-14T12:00:01.000Z"},
            event_path,
        ))
        invalid_events.append((
            "wrong event file",
            row,
            event_path.with_name("2026-07-15.jsonl"),
        ))
        for name, candidate, path in invalid_events:
            with self.subTest(event=name):
                self.assertFalse(memory_provenance._valid_event(candidate, path))

        compact_id = self._compact(event_id, suffix="5ec71008", legacy=False)
        compact_path = (
            self.memory / "life" / "compacts" / "owner" / f"{compact_id}.md"
        )
        compact_meta, compact_text = memory_provenance._load_json_first_line(
            compact_path, memory_provenance._ARTIFACT_META_RE, 2
        )
        self.assertTrue(
            memory_provenance._valid_compact(
                compact_meta, compact_text, compact_path, self.memory
            )
        )
        invalid_compacts = []
        missing_compact = dict(compact_meta)
        missing_compact.pop("first_ts")
        invalid_compacts.append(("missing required key", missing_compact, compact_text))
        invalid_compacts.append((
            "wrong chat path", {**compact_meta, "chat_id": "other"}, compact_text
        ))
        invalid_compacts.append((
            "bool tier", {**compact_meta, "tier": True}, compact_text
        ))
        invalid_compacts.append((
            "id timestamp mismatch",
            {**compact_meta, "created_at": "2026-07-14T12:00:01.000Z"},
            compact_text,
        ))
        invalid_compacts.append((
            "wrong title", compact_meta, compact_text.replace(
                f"# Compact {compact_id}", "# Compact another"
            )
        ))
        for name, candidate, body in invalid_compacts:
            with self.subTest(compact=name):
                self.assertFalse(
                    memory_provenance._valid_compact(
                        candidate, body, compact_path, self.memory
                    )
                )

    def test_duplicate_event_and_compact_ids_globally_invalidate_evidence(self) -> None:
        event_claim = self._claim(
            statement="duplicate event cerberus marker", suffix="5ec71009"
        )
        event_file = self.memory / "life" / "events" / "2026-07-14.jsonl"
        lines = event_file.read_text(encoding="utf-8").splitlines()
        event_file.write_text("\n".join([*lines, lines[0]]) + "\n", encoding="utf-8")

        compact_claim = self._claim(
            statement="duplicate compact minotaur marker", suffix="5ec7100a"
        )
        _, compact_meta = memory_provenance.claim_source(compact_claim)
        compact_id = compact_meta["evidence_ids"][0]
        compact = (
            self.memory / "life" / "compacts" / "owner" / f"{compact_id}.md"
        )
        duplicate = (
            self.memory / "life" / "compacts" / "shadow" / f"{compact_id}.md"
        )
        duplicate.parent.mkdir(parents=True)
        duplicate.write_bytes(compact.read_bytes())

        evidence = memory_provenance.claim_evidence_index(self.memory)
        event_ref = memory_provenance.claim_source(event_claim)[1]["evidence_ids"][0]
        duplicated_event_id = evidence["compacts"][event_ref]["source_event_ids"][0]
        self.assertIn(duplicated_event_id, evidence["duplicate_events"])
        self.assertNotIn(duplicated_event_id, evidence["events"])
        event_leaf = memory_provenance.compact_evidence(event_ref, evidence)["leaves"]
        self.assertEqual(event_leaf, [], "duplicate event invalidates its compact chain")
        self.assertIn(compact_id, evidence["duplicate_compacts"])
        self.assertNotIn(compact_id, evidence["compacts"])

        memory_fts.rebuild(
            base=self.base, memory_dir=self.memory, skills_dir=self.skills
        )
        self.assertEqual(
            self._search("cerberus", purpose="automatic"), []
        )
        self.assertEqual(
            self._search("minotaur", purpose="automatic"), []
        )

    def test_evidence_cache_is_bound_to_content_not_only_size_and_mtime(self) -> None:
        event_id = self._event(suffix="5ec7100b")
        compact_id = self._compact(event_id, suffix="5ec7100b", legacy=False)
        first = memory_provenance.claim_evidence_index(self.memory)
        self.assertTrue(memory_provenance.compact_evidence(compact_id, first)["valid"])

        event_file = self.memory / "life" / "events" / "2026-07-14.jsonl"
        before = event_file.stat()
        original = event_file.read_bytes()
        tampered = original.replace(b"5ec7100b", b"5ec7100c")
        self.assertEqual(len(tampered), len(original))
        event_file.write_bytes(tampered)
        os.utime(event_file, ns=(before.st_atime_ns, before.st_mtime_ns))

        second = memory_provenance.claim_evidence_index(self.memory)
        self.assertNotIn(event_id, second["events"])
        self.assertFalse(
            memory_provenance.compact_evidence(compact_id, second)["valid"]
        )

    def test_legacy_compact_backed_claim_is_explicit_only(self) -> None:
        self._claim(
            statement="legacybacked leviathan marker",
            suffix="1e9ac001",
            legacy=True,
        )
        memory_fts.rebuild(
            base=self.base, memory_dir=self.memory, skills_dir=self.skills
        )

        explicit = self._search("legacybacked leviathan")
        self.assertTrue(explicit)
        self.assertTrue(
            any(
                row["source_type"] == memory_provenance.CLAIM_SUPPORTED_OBSERVED_KIND
                for row in explicit
            )
        )
        self.assertEqual(
            self._search("legacybacked leviathan", purpose="automatic"), []
        )

    def test_claim_revision_text_is_never_automatic_authority(self) -> None:
        statement = "currentstatement selkie marker"
        revision = "revisiononly cockatrice marker"
        self._claim(
            statement=statement,
            suffix="1e9ac002",
            legacy=False,
            revision=revision,
        )
        memory_fts.rebuild(
            base=self.base, memory_dir=self.memory, skills_dir=self.skills
        )

        current = self._search("currentstatement selkie", purpose="automatic")
        self.assertEqual(len(current), 1)
        self.assertEqual(
            current[0]["source_type"],
            memory_provenance.CLAIM_SUPPORTED_OBSERVED_KIND,
        )
        self.assertEqual(
            self._search("revisiononly cockatrice", purpose="automatic"), []
        )
        explicit = self._search("revisiononly cockatrice")
        self.assertTrue(explicit)
        self.assertEqual(
            {row["source_type"] for row in explicit},
            {memory_provenance.CLAIM_RAW_KIND},
        )


if __name__ == "__main__":
    unittest.main()
