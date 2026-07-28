from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import memory_fts
import memory_index


class MemoryFtsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.memory = self.base / "memory"
        self.skills = self.base / "soul" / "skills"
        (self.memory / "people").mkdir(parents=True)
        self.skills.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def search(self, query: str, *, scope: str = "owner",
               purpose: str = "explicit") -> list[dict]:
        return memory_fts.search(
            query,
            base=self.base,
            memory_dir=self.memory,
            skills_dir=self.skills,
            scope=scope,
            purpose=purpose,
        )

    def test_indexes_markdown_and_jsonl_with_visibility_and_provenance(self):
        person = self.memory / "people" / "egor.md"
        person.write_text(
            "# Egor\n\n"
            "- [public] \u0441\u0435\u0432\u0435\u0440\u043d\u043e\u0435 \u0441\u0438\u044f\u043d\u0438\u0435\n"
            "- [private] \u0444\u0438\u043e\u043b\u0435\u0442\u043e\u0432\u044b\u0439 \u043f\u0430\u0440\u043e\u043b\u044c\n",
            encoding="utf-8",
        )
        events = self.memory / "life" / "events" / "2026-07-13.jsonl"
        events.parent.mkdir(parents=True)
        event = {
            "schema": "praxis.life.event.v1",
            "id": "evt-1",
            "ts": "2026-07-13T12:00:00Z",
            "run_id": "run-kraken",
            "text": "\u0432\u0438\u0434\u0435\u043b\u0430 \u043f\u043e\u043b\u044f\u0440\u043d\u043e\u0435 \u0441\u0438\u044f\u043d\u0438\u0435 \u043d\u0430\u0434 \u0434\u043e\u043c\u043e\u043c",
            "refs": ["telegram:message:77"],
            "supersedes": ["evt-0"],
        }
        events.write_text(
            json.dumps(event, ensure_ascii=False)
            + "\n"
            + json.dumps({**event, "text": event["text"] + " repeated"}, ensure_ascii=False)
            + "\n{broken tail\n",
            encoding="utf-8",
        )

        state = memory_fts.rebuild(
            base=self.base, memory_dir=self.memory, skills_dir=self.skills
        )
        self.assertEqual(state["corrupt_lines"], 1)
        refreshed = memory_fts.upsert(
            events, base=self.base, memory_dir=self.memory, skills_dir=self.skills
        )
        self.assertTrue(refreshed["indexed"])
        ensured = memory_fts.ensure(
            base=self.base, memory_dir=self.memory, skills_dir=self.skills
        )
        self.assertEqual(ensured["corrupt_lines"], 1)

        public = self.search("\u0441\u0438\u044f\u043d\u0438\u0435", scope="group")
        self.assertTrue(any(row["path"].endswith("people/egor.md") for row in public))
        self.assertFalse(self.search("\u0444\u0438\u043e\u043b\u0435\u0442\u043e\u0432\u044b\u0439", scope="group"))
        self.assertTrue(self.search("\u0444\u0438\u043e\u043b\u0435\u0442\u043e\u0432\u044b\u0439", scope="owner"))

        recalled = self.search("\u043f\u043e\u043b\u044f\u0440\u043d\u043e\u0435")
        row = next(item for item in recalled if item["event_id"] == "evt-1")
        self.assertEqual(row["source_type"], "life_event")
        self.assertEqual(row["run_id"], "run-kraken")
        self.assertEqual(row["refs"], ["telegram:message:77"])
        self.assertEqual(row["supersedes"], ["evt-0"])
        self.assertEqual(row["provenance"], ["telegram:message:77", "evt-0"])

    def test_generated_views_are_excluded_and_rebuild_is_logically_deterministic(self):
        canonical = self.memory / "home.md"
        canonical.write_text("# Home\n\ncanonical narwhal memory\n", encoding="utf-8")
        (self.memory / "INDEX.md").write_text(
            "# Index\n\nindexphantom must not enter recall\n", encoding="utf-8"
        )
        maps = self.memory / "maps"
        maps.mkdir()
        (maps / "PEOPLE.md").write_text(
            "# People\n\nmapphantom must not enter recall\n", encoding="utf-8"
        )
        computer = self.memory / "computer"
        (computer / "devices").mkdir(parents=True)
        (computer / "tasks").mkdir()
        (computer / "inventory" / "windows-pc").mkdir(parents=True)
        (computer / "MAP.md").write_text("# Map\n\ncomputermapphantom\n", encoding="utf-8")
        (computer / "devices" / "windows-pc.md").write_text(
            "# Device\n\ndeviceprojectionphantom\n", encoding="utf-8",
        )
        (computer / "tasks" / "task-1.md").write_text(
            "# Task\n\ntaskprojectionphantom\n", encoding="utf-8",
        )
        (computer / "inventory" / "windows-pc" / "CURRENT.md").write_text(
            "# Inventory\n\ninventoryprojectionphantom\n", encoding="utf-8",
        )
        desires = self.memory / "desires"
        desires.mkdir()
        (desires / "CURRENT.md").write_text(
            "<!-- praxis-generated: {} -->\n# Desires\n\ndesireprojectionphantom\n",
            encoding="utf-8",
        )
        (desires / "events.jsonl").write_text(
            json.dumps({
                "schema": "praxis.desire.event.v1",
                "event_id": "desire-event-1",
                "ts": "2026-07-13T12:00:00Z",
                "desire_id": "desire-kraken",
                "stage": "wanted",
                "note": "learn deliberate kraken scrolling",
                "run_id": "run-kraken",
            }) + "\n" + json.dumps({
                "schema": "praxis.desire.event.v1",
                "event_id": "desire-event-2",
                "ts": "2026-07-13T12:10:00Z",
                "desire_id": "desire-kraken",
                "stage": "chosen",
                "note": "choose deliberate kraken scrolling",
                "run_id": "run-kraken",
            }) + "\n" + json.dumps({
                "schema": "praxis.desire.event.v1",
                "event_id": "desire-event-3",
                "ts": "2026-07-13T12:20:00Z",
                "desire_id": "desire-kraken",
                "stage": "changed",
                "status": "satisfied",
                "note": "release obsolete scrolling intention",
                "run_id": "run-kraken",
            }) + "\n",
            encoding="utf-8",
        )

        first_state = memory_fts.rebuild(
            base=self.base, memory_dir=self.memory, skills_dir=self.skills
        )
        first = [(row["id"], row["path"], row["text"]) for row in self.search("narwhal")]
        self.assertTrue(first)
        self.assertFalse(self.search("indexphantom"))
        self.assertFalse(self.search("mapphantom"))
        self.assertFalse(self.search("computermapphantom"))
        self.assertFalse(self.search("deviceprojectionphantom"))
        self.assertFalse(self.search("taskprojectionphantom"))
        self.assertFalse(self.search("inventoryprojectionphantom"))
        self.assertFalse(self.search("desireprojectionphantom"))
        desired = self.search("kraken scrolling")
        self.assertEqual(desired[0]["source_type"], "desire_event")
        self.assertEqual(desired[0]["run_id"], "run-kraken")
        self.assertEqual(desired[0]["desire_id"], "desire-kraken")
        self.assertEqual(sum(row["source_type"] == "desire_event" for row in desired), 1)
        automatic_desired = self.search("kraken scrolling", purpose="automatic")
        self.assertEqual(len(automatic_desired), 1)
        self.assertEqual(automatic_desired[0]["event_id"], "desire-event-3")
        self.assertIn("satisfied", automatic_desired[0]["text"])

        database = self.base / first_state["database"]
        database.unlink()
        second_state = memory_fts.rebuild(
            base=self.base, memory_dir=self.memory, skills_dir=self.skills
        )
        second = [(row["id"], row["path"], row["text"]) for row in self.search("narwhal")]
        self.assertEqual(first, second)
        self.assertEqual(first_state["fingerprint"], second_state["fingerprint"])

        with mock.patch.multiple(
            memory_index,
            BASE=self.base,
            MEM_DIR=self.memory,
            SKILLS_DIR=self.skills,
            VECTORS_DIR=self.memory / ".vectors",
            INDEX_MD=self.memory / "INDEX.md",
        ):
            vector_sources = {path.resolve() for path in memory_index._iter_source_files()}
        self.assertIn(canonical.resolve(), vector_sources)
        for projection in (
            self.memory / "INDEX.md", maps / "PEOPLE.md", computer / "MAP.md",
            computer / "devices" / "windows-pc.md", computer / "tasks" / "task-1.md",
            computer / "inventory" / "windows-pc" / "CURRENT.md", desires / "CURRENT.md",
        ):
            self.assertNotIn(projection.resolve(), vector_sources)

        projection = computer / "MAP.md"
        projection_rel = projection.relative_to(self.base).as_posix()
        legacy_vector = {
            "model": "legacy",
            "files": {projection_rel: projection.stat().st_mtime},
            "records": [{
                "id": "legacy-generated-map-record", "path": projection_rel,
                "text": "computermapphantom", "vector": [1.0],
            }],
        }
        with mock.patch.multiple(
            memory_index,
            BASE=self.base,
            MEM_DIR=self.memory,
            SKILLS_DIR=self.skills,
            VECTORS_DIR=self.memory / ".vectors",
            INDEX_MD=self.memory / "INDEX.md",
        ), mock.patch.object(memory_index, "_embeddings_on", return_value=True), \
             mock.patch.object(memory_index, "_load", return_value=legacy_vector), \
             mock.patch.object(memory_index, "_save") as save:
            memory_index.upsert(projection)
        cleaned = save.call_args.args[0]
        self.assertNotIn(projection_rel, cleaned["files"])
        self.assertEqual(cleaned["records"], [])

    def test_latest_canonical_inventory_and_self_history_are_recallable(self):
        device = self.memory / "computer" / "inventory" / "windows-pc"
        device.mkdir(parents=True)
        old = {
            "schema": "praxis.computer.inventory.v1", "device_id": "windows-pc",
            "captured_at": "2099-07-12T12:00:00Z",
            "payload": {"hostname": "OLD", "apps": [{"name": "OldAppPhantom"}]},
        }
        current = {
            "schema": "praxis.computer.inventory.v1", "device_id": "windows-pc",
            "captured_at": "2026-07-13T12:00:00Z", "observed_at": "2026-07-13T12:00:01Z",
            "payload": {
                "hostname": "LOVE", "user": "yegor",
                "os": {"caption": "Windows 11 Pro", "build": "26200"},
                "apps": [{"name": "CurrentAppNarwhal", "publisher": "Praxis"}],
                "tools": [{"name": "cargo", "path": r"C:\\Rust\\cargo.exe"}],
                "project_roots": [r"C:\\Users\\yegor\\Downloads\\Praxis"],
            },
        }
        # Legacy filenames came from the Windows clock and may be skewed far
        # into the future.  observed_at on the new record must win.
        (device / "20990712T120000Z.json").write_text(json.dumps(old), encoding="utf-8")
        latest = device / "20260713T120001Z.json"
        latest.write_text(json.dumps(current), encoding="utf-8")
        (device / "CURRENT.json").write_text(json.dumps(current), encoding="utf-8")
        (device / "CURRENT.md").write_text(
            "# Computer\n\ninventoryprojectionphantom\n", encoding="utf-8",
        )

        history = self.base / "soul" / "self" / "history"
        history.mkdir(parents=True)
        (history / "0001.md").write_text(
            "# Previous self\n\nI learned deliberate lighthouse checking.\n", encoding="utf-8",
        )

        memory_fts.rebuild(base=self.base, memory_dir=self.memory, skills_dir=self.skills)
        app = self.search("CurrentAppNarwhal")
        self.assertEqual(app[0]["source_type"], "inventory_snapshot")
        self.assertEqual(app[0]["path"], latest.relative_to(self.base).as_posix())
        self.assertEqual(app[0]["visibility"], "owner")
        self.assertFalse(self.search("OldAppPhantom"))
        self.assertFalse(self.search("inventoryprojectionphantom"))
        self.assertEqual(self.search("lighthouse checking")[0]["source_type"], "self_history")
        self.assertFalse(self.search("CurrentAppNarwhal", scope="group"))

        with mock.patch.multiple(
            memory_index,
            BASE=self.base,
            MEM_DIR=self.memory,
            SKILLS_DIR=self.skills,
            VECTORS_DIR=self.memory / ".vectors",
            INDEX_MD=self.memory / "INDEX.md",
        ):
            vector_sources = {path.resolve() for path in memory_index._iter_source_files()}
        self.assertIn((history / "0001.md").resolve(), vector_sources)
        self.assertNotIn(latest.resolve(), vector_sources)  # canonical JSON is covered by FTS

    def test_upsert_tracks_edits_and_deletion_without_touching_source(self):
        person = self.memory / "people" / "egor.md"
        person.write_text("# Egor\n\n- [public] copper albatross\n", encoding="utf-8")
        original = person.read_bytes()
        memory_fts.rebuild(base=self.base, memory_dir=self.memory, skills_dir=self.skills)
        self.assertTrue(self.search("albatross"))
        self.assertEqual(person.read_bytes(), original)

        person.write_text("# Egor\n\n- [public] silver cuttlefish\n", encoding="utf-8")
        memory_fts.upsert(
            person, base=self.base, memory_dir=self.memory, skills_dir=self.skills
        )
        self.assertFalse(self.search("albatross"))
        self.assertTrue(self.search("cuttlefish"))

        person.unlink()
        result = memory_fts.upsert(
            person, base=self.base, memory_dir=self.memory, skills_dir=self.skills
        )
        self.assertFalse(result["indexed"])
        self.assertFalse(self.search("cuttlefish"))

    def test_computer_goal_is_explicit_only_while_outcome_remains_automatic(self):
        events = self.memory / "computer" / "events" / "2026-07-15.jsonl"
        events.parent.mkdir(parents=True)
        events.write_text(
            json.dumps({
                "id": "obs-social-pulse",
                "at": "2026-07-15T15:15:44Z",
                "task_id": "task-social-pulse",
                "goal": "hourly wake prompt recursive marker",
                "summary": "body status succeeded compact outcome marker",
                "capability": "body.status",
                "status": "succeeded",
                "refs": ["computer:receipt:social-pulse"],
            }) + "\n",
            encoding="utf-8",
        )

        explicit_goal = self.search("hourly wake prompt recursive", purpose="explicit")
        self.assertTrue(any("hourly wake prompt recursive marker" in row["text"]
                            for row in explicit_goal))
        self.assertFalse(self.search("hourly wake prompt recursive", purpose="automatic"))
        automatic_outcome = self.search("compact outcome marker", purpose="automatic")
        self.assertTrue(automatic_outcome)
        self.assertNotIn("hourly wake prompt recursive marker", automatic_outcome[0]["text"])

    def test_existing_memory_search_api_returns_jsonl_provenance(self):
        events = self.memory / "computer" / "events" / "2026-07-13.jsonl"
        events.parent.mkdir(parents=True)
        events.write_text(
            json.dumps(
                {
                    "id": "obs-9",
                    "at": "2026-07-13T12:00:00Z",
                    "task_id": "task-kraken",
                    "summary": "scroll calibration completed for messenger",
                    "refs": ["computer:receipt:9"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with mock.patch.multiple(
            memory_index,
            BASE=self.base,
            MEM_DIR=self.memory,
            SKILLS_DIR=self.skills,
        ), mock.patch.dict(os.environ, {"PRAXIS_EMBEDDINGS": "0"}):
            result = memory_index.search("scroll calibration", k=3, scope="owner")
        self.assertTrue(result)
        self.assertEqual(result[0]["event_id"], "obs-9")
        self.assertEqual(result[0]["source_type"], "computer_event")
        self.assertEqual(result[0]["refs"], ["computer:receipt:9"])
        self.assertEqual(result[0]["provenance"], ["computer:receipt:9"])

    def test_explicit_ensure_reconciles_incrementally_without_full_reparse(self):
        person = self.memory / "people" / "egor.md"
        person.write_text("- любит скорость\n", encoding="utf-8")
        run_events = self.memory / "runs" / "2026-07" / "run-1" / "events.jsonl"
        run_events.parent.mkdir(parents=True)
        run_events.write_text(
            json.dumps({"kind": "run_event", "text": "first tool step"}) + "\n",
            encoding="utf-8",
        )
        self.assertTrue(self.search("first tool"))

        with run_events.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "run_event", "text": "second unheard step"}) + "\n")
        original = memory_fts._source_chunks
        parsed: list[str] = []

        def spy(source):
            parsed.append(source.rel)
            return original(source)

        # A run event append is the every-turn case: it must refresh only that
        # source in place, never re-parse the whole corpus (the old minutes-long
        # rebuild) and never touch unrelated sources.
        with mock.patch.object(
            memory_fts, "rebuild",
            side_effect=AssertionError("full rebuild on live explicit path"),
        ), mock.patch.object(memory_fts, "_source_chunks", side_effect=spy):
            rows = self.search("unheard")
        self.assertTrue(any("second unheard step" in row["text"] for row in rows))
        self.assertIn("memory/runs/2026-07/run-1/events.jsonl", parsed)
        self.assertNotIn("memory/people/egor.md", parsed)

    def test_explicit_ensure_drops_removed_sources_incrementally(self):
        person = self.memory / "people" / "guest.md"
        person.write_text("- transient visitor fact\n", encoding="utf-8")
        keeper = self.memory / "people" / "keeper.md"
        keeper.write_text("- keeper stays around\n", encoding="utf-8")
        self.assertTrue(self.search("transient visitor"))

        person.unlink()
        with mock.patch.object(
            memory_fts, "rebuild",
            side_effect=AssertionError("full rebuild on live explicit path"),
        ):
            self.assertEqual(self.search("transient visitor"), [])
            self.assertTrue(self.search("keeper stays"))

    def test_life_change_rederives_all_life_event_sources(self):
        first = self.memory / "life" / "events" / "2026-07-01.jsonl"
        first.parent.mkdir(parents=True)
        first.write_text(
            json.dumps({"schema": "praxis.life.event.v1", "id": "evt-a",
                        "ts": "2026-07-01T10:00:00Z", "text": "aurora over the house"})
            + "\n", encoding="utf-8",
        )
        second = self.memory / "life" / "events" / "2026-07-02.jsonl"
        second.write_text(
            json.dumps({"schema": "praxis.life.event.v1", "id": "evt-b",
                        "ts": "2026-07-02T10:00:00Z", "text": "quiet morning walk"})
            + "\n", encoding="utf-8",
        )
        bystander = self.memory / "people" / "egor.md"
        bystander.write_text("- untouched bystander\n", encoding="utf-8")
        self.assertTrue(self.search("aurora"))

        with first.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"schema": "praxis.life.event.v1", "id": "evt-c",
                                 "ts": "2026-07-01T11:00:00Z",
                                 "text": "aurora faded slowly"}) + "\n")
        original = memory_fts._source_chunks
        parsed: list[str] = []

        def spy(source):
            parsed.append(source.rel)
            return original(source)

        # Eligibility of life events (and claim kinds) is derived from the
        # provenance evidence index, so touching any memory/life source must
        # re-derive every life_event source — but still not the whole corpus.
        with mock.patch.object(memory_fts, "_source_chunks", side_effect=spy):
            self.search("faded")
        self.assertIn("memory/life/events/2026-07-01.jsonl", parsed)
        self.assertIn("memory/life/events/2026-07-02.jsonl", parsed)
        self.assertNotIn("memory/people/egor.md", parsed)


if __name__ == "__main__":
    unittest.main()
