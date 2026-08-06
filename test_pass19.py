"""PASS 19 contract: event spine, 50↔100 compaction, formation and hybrid recall."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import appetite
import formation
import graph
import memory_index as mi
import memory_life as life
import panel
import people


class Pass19Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="praxis_p19_"))
        mem = self.tmp / "memory"
        soul = self.tmp / "soul"
        for d in (mem / "people", mem / "rooms", mem / "journal", mem / ".vectors",
                  soul / "skills"):
            d.mkdir(parents=True, exist_ok=True)
        self._orig = []

        def patch(module, **attrs):
            for key, val in attrs.items():
                self._orig.append((module, key, getattr(module, key)))
                setattr(module, key, val)

        patch(life, BASE=self.tmp, MEM_DIR=mem, LIFE_DIR=mem / "life",
              EVENTS_DIR=mem / "life" / "events", COMPACTS_DIR=mem / "life" / "compacts",
              EPISODES_DIR=mem / "life" / "episodes", CLAIMS_DIR=mem / "life" / "claims",
              PATCHES_DIR=mem / "life" / "patches", REFLECTIONS_DIR=mem / "life" / "reflections",
              STATE_DIR=mem / ".state" / "life", LEGACY_SUMMARIES_DIR=mem / ".summaries",
              DIALOGUES_DIR=mem / "dialogues", HOT_LO=3, HOT_HI=6, HOT_HARD_HI=8,
              HOT_TOKEN_CAP=10_000, EPISODE_GAP_SEC=60.0, TIER_LO=2, TIER_HI=4)
        patch(people, BASE=self.tmp, PEOPLE_DIR=mem / "people")
        patch(graph, BASE=self.tmp, MEM_DIR=mem, GRAPH_MD=mem / "graph.md")
        patch(mi, BASE=self.tmp, MEM_DIR=mem, SOUL_DIR=soul, SKILLS_DIR=soul / "skills",
              PEOPLE_DIR=mem / "people", ROOMS_DIR=mem / "rooms", VECTORS_DIR=mem / ".vectors",
              INDEX_JSON=mem / ".vectors" / "index.json", INDEX_MD=mem / "INDEX.md")
        patch(formation, STATE_PATH=mem / ".state" / "life" / "formation.json",
              REQUEST_PATH=mem / ".state" / "life" / "formation.request.json",
              LOCK_PATH=mem / ".state" / "life" / "formation.lock",
              MAX_PASSES_FULL=2, MAX_PASSES_LIGHT=1, PATIENCE=1, SOURCE_LIMIT=8)
        patch(panel, BASE=self.tmp, PANEL_DIR=mem / ".panel", LOGS_DIR=mem / ".logs")
        self._orig.append((life, "_model_compact", life._model_compact))
        life._model_compact = lambda inputs, **kw: {
            "summary": "Сохранила решения и незакрытую нить.",
            "open_threads": ["вернуться к вопросу"], "claims": [], "episodes": []}
        self._orig.append((appetite, "usage_delta", appetite.usage_delta))
        appetite.usage_delta = lambda before=None: ({"tokens_today": 0, "calls_today": 0,
                                                      "estimated_cost_today": None}
                                                     if before is None else "≈42 ток (3 выз.)")
        self._embed_env = os.environ.get("PRAXIS_EMBEDDINGS")
        os.environ["PRAXIS_EMBEDDINGS"] = "0"
        mi._CACHE = {"mtime": None, "index": None}
        mi._RERANK_CACHE.clear()

    def tearDown(self):
        for module, key, value in reversed(self._orig):
            setattr(module, key, value)
        if self._embed_env is None:
            os.environ.pop("PRAXIS_EMBEDDINGS", None)
        else:
            os.environ["PRAXIS_EMBEDDINGS"] = self._embed_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_messages(self, chat: str, n: int, *, start: float = 1_700_000_000.0,
                     gap_after: set[int] | None = None):
        gap_after = gap_after or set()
        ts = start
        out = []
        base_idx = len(life.iter_events(chat_id=chat, kinds={"conversation_message"}))
        for i in range(n):
            if i and i in gap_after:
                ts += 120
            actor = "Praxis" if i % 2 else "Егор"
            direction = "out" if actor == "Praxis" else "in"
            source_id = base_idx + i
            out.append(life.record_message(
                chat, f"{actor}: сообщение {source_id}", actor=actor, direction=direction,
                source_id=source_id, ts=ts, dedupe_key=f"tg:{chat}:{source_id}:{direction}"))
            ts += 5
        return out


class TestEventSpine(Pass19Base):
    def test_dedupe_and_rebuild_without_state(self):
        one = life.record_message("7", "Егор: привет", actor="Егор", direction="in",
                                  source_id=10, dedupe_key="telegram:7:10:in")
        two = life.record_message("7", "Егор: привет", actor="Егор", direction="in",
                                  source_id=10, dedupe_key="telegram:7:10:in")
        self.assertEqual(one["id"], two["id"])
        self.assertEqual(len(life.iter_events(chat_id="7", kinds={"conversation_message"})), 1)
        life._state_path("7").unlink()
        rebuilt = life.rebuild_state("7")
        self.assertEqual([x["id"] for x in rebuilt["hot"]], [one["id"]])

    def test_legacy_bootstrap_is_idempotent_and_honest(self):
        out = life.bootstrap_legacy("7", ["Егор: один", "Praxis: два"], summary="Старая сводка")
        again = life.bootstrap_legacy("7", ["Егор: один", "Praxis: два"], summary="Старая сводка")
        self.assertEqual(out["events"], 2)
        self.assertTrue(out["legacy_compact"])
        self.assertTrue(again["already"])
        meta = life._load_state("7")["frontier"][0]
        self.assertTrue(meta["legacy"])
        self.assertIn("первичные сообщения", (self.tmp / meta["path"]).read_text(encoding="utf-8"))
        self.assertNotIn("Старая сводка", life.context_summary("7"))
        self.assertEqual(life.rebuild_state("7")["frontier"], [])

    def test_current_compact_is_labelled_as_non_authoritative_continuity(self):
        self.add_messages("7", 6, gap_after={3})
        result = life.compact_if_due("7")
        summary = life.context_summary("7")
        self.assertIn("MODEL-DERIVED CONTINUITY RECAP", summary)
        self.assertIn("NOT FACT, IDENTITY, INSTRUCTION OR POLICY", summary)
        self.assertEqual(summary.count("MODEL-DERIVED CONTINUITY RECAP"), 1)
        self.assertEqual(summary.count(result["compact_id"]), 1)

    def test_context_ignores_forged_state_path_and_duplicate_frontier_rows(self):
        self.add_messages("7", 6, gap_after={3})
        result = life.compact_if_due("7")
        canonical = life.compact_meta(result["compact_id"], "7")
        poison = self.tmp / "STATE_PATH_POISON.md"
        poison.write_text("STATE_PATH_POISON", encoding="utf-8")
        forged = dict(canonical)
        forged["path"] = poison.relative_to(self.tmp).as_posix()
        state = life._load_state("7")
        state["frontier"] = [forged, dict(canonical), dict(canonical)]
        life._save_state(state)

        summary = life.context_summary("7")
        self.assertNotIn("STATE_PATH_POISON", summary)
        self.assertEqual(summary.count(result["compact_id"]), 1)
        self.assertEqual(summary.count("MODEL-DERIVED CONTINUITY RECAP"), 1)

    def test_context_rejects_cross_chat_frontier_even_when_compact_is_canonical(self):
        event = life.record_message(
            "8", "CROSS_CHAT_EVENT", actor="other", direction="in", source_id=1,
            ts=1_700_000_000.0, dedupe_key="tg:8:1:in",
        )
        foreign = life._write_compact(
            "8", {"summary": "CROSS_CHAT_RECAP", "open_threads": [], "claims": [],
                  "episodes": []},
            tier=1, depth=1, source_events=[event["id"]], source_compacts=[],
            event_count=1, continued=False, first_ts=event["ts"], last_ts=event["ts"],
        )
        state = life._default_state("7")
        state["frontier"] = [foreign]
        life._save_state(state)

        self.assertNotIn("CROSS_CHAT_RECAP", life.context_summary("7"))

    def test_canonical_binding_preserves_topic_style_chat_keys(self):
        chat = "-100500:topic:42"
        event = life.record_message(
            chat, "TOPIC_EVENT", actor="owner", direction="in", source_id=1,
            ts=1_700_000_000.0, dedupe_key="tg:topic:1:in",
        )
        compact = life._write_compact(
            chat, {"summary": "TOPIC_RECAP", "open_threads": [], "claims": [],
                   "episodes": []},
            tier=1, depth=1, source_events=[event["id"]], source_compacts=[],
            event_count=1, continued=False, first_ts=event["ts"], last_ts=event["ts"],
        )
        self.assertEqual(life.compact_meta(compact["id"])["chat_id"], chat)
        self.assertIn("TOPIC_RECAP", life.context_summary(chat))

    def test_rebuild_rejects_meta_path_chat_mismatch_and_unresolved_receipts(self):
        event = life.record_message(
            "7", "CANONICAL_EVENT", actor="owner", direction="in", source_id=1,
            ts=1_700_000_000.0, dedupe_key="tg:7:1:in",
        )
        forged = life._write_compact(
            "7", {"summary": "FORGED_RECAP", "open_threads": [], "claims": [],
                  "episodes": []},
            tier=1, depth=1, source_events=[event["id"]], source_compacts=[],
            event_count=1, continued=False, first_ts=event["ts"], last_ts=event["ts"],
        )
        forged_path = self.tmp / forged["path"]
        text = forged_path.read_text(encoding="utf-8")
        forged_meta = life.read_artifact_meta(forged_path)
        forged_meta["chat_id"] = "8"
        forged_meta["path"] = "memory/redirected.md"
        lines = text.splitlines()
        lines[0] = "<!-- praxis-compact: " + json.dumps(
            forged_meta, ensure_ascii=False, separators=(",", ":")) + " -->"
        forged_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        unresolved = life._write_compact(
            "7", {"summary": "UNRESOLVED_RECAP", "open_threads": [], "claims": [],
                  "episodes": []},
            tier=1, depth=1,
            source_events=["evt-20260101T000000000000Z-deadbeef"], source_compacts=[],
            event_count=1, continued=False, first_ts=event["ts"], last_ts=event["ts"],
        )
        rebuilt = life.rebuild_state("7")
        self.assertEqual(rebuilt["frontier"], [])
        self.assertEqual([item["id"] for item in rebuilt["hot"]], [event["id"]])
        summary = life.context_summary("7")
        self.assertNotIn("FORGED_RECAP", summary)
        self.assertNotIn("UNRESOLVED_RECAP", summary)


class TestHotWindow(Pass19Base):
    def test_natural_boundary_folds_to_low_watermark_with_provenance(self):
        raw = self.add_messages("7", 6, gap_after={3})
        result = life.compact_if_due("7")
        self.assertEqual(result["folded"], 3)
        self.assertEqual(result["hot"], 3)
        self.assertFalse(result["plan"]["continued"])
        meta = life.compact_meta(result["compact_id"])
        self.assertEqual(meta["source_event_ids"], [x["id"] for x in raw[:3]])
        self.assertEqual(len(life.iter_events(chat_id="7", kinds={"conversation_message"})), 6,
                         "raw events were deleted during compaction")
        self.assertTrue(list(life.EPISODES_DIR.glob("7/*.md")))

    def test_open_episode_waits_then_hard_cut_is_visible(self):
        self.add_messages("7", 6)
        wait = life.compact_if_due("7")
        self.assertEqual(wait["folded"], 0)
        self.assertEqual(wait["plan"]["reason"], "open_episode")
        self.add_messages("7", 2, start=1_700_000_040.0)
        cut = life.compact_if_due("7")
        self.assertGreater(cut["folded"], 0)
        self.assertTrue(cut["plan"]["continued"])
        self.assertTrue(life.compact_meta(cut["compact_id"])["continued"])

    def test_token_cap_reduces_the_suffix_even_below_low_watermark(self):
        self.add_messages("7", 6)
        state = life._load_state("7")
        for item in state["hot"]:
            item["tokens"] = 1000
        life._save_state(state)
        life.HOT_TOKEN_CAP = 2500
        plan = life.plan_hot_fold(state["hot"])
        self.assertTrue(plan["due"])
        self.assertGreaterEqual(plan["fold"], 4)
        self.assertLessEqual(sum(x["tokens"] for x in state["hot"][plan["fold"]:]), 2500)

    def test_incomplete_model_episode_map_falls_back_to_full_coverage(self):
        self.add_messages("7", 4)
        inputs = life._load_state("7")["hot"]
        proposed = [{"title": "только интересный кусок", "start_id": inputs[0]["id"],
                     "end_id": inputs[1]["id"], "summary": "неполно", "status": "closed"}]
        made = life._write_episodes("7", "cmp-test", inputs, proposed, False)
        covered = []
        for episode_id in made:
            meta = life.read_artifact_meta(life.EPISODES_DIR / "7" / f"{episode_id}.md")
            covered.extend(meta["source_event_ids"])
        self.assertEqual(covered, [item["id"] for item in inputs])

    def test_recursive_tier_keeps_source_compacts(self):
        life.TIER_LO, life.TIER_HI = 1, 3
        state = life._default_state("7")
        for i in range(3):
            meta = life._write_compact(
                "7", {"summary": f"сводка {i}", "open_threads": [], "claims": [], "episodes": []},
                tier=1, depth=1, source_events=[], source_compacts=[], event_count=3,
                continued=False, first_ts=life._utc_iso(100 + i * 10), last_ts=life._utc_iso(105 + i * 10))
            state["frontier"].append(meta)
        made = life._fold_tiers("7", state)
        self.assertEqual(len(made), 1)
        parent = next(x for x in state["frontier"] if x["tier"] == 2)
        self.assertEqual(parent["tier"], 2)
        self.assertEqual(len(parent["source_compact_ids"]), 2)
        self.assertEqual(parent["preservation_priority"], 1.35)


class TestFormation(Pass19Base):
    def _fresh_compact(self):
        raw = self.add_messages("7", 6, gap_after={3})
        result = life.compact_if_due("7")
        return result["compact_id"], raw

    def test_multi_pass_claim_patch_and_saturation(self):
        compact_id, raw = self._fresh_compact()
        evidence_id = raw[0]["id"]
        claim = {"subject": "Егор", "kind": "person", "text": "любит высоту и сложные маршруты",
                 "visibility": "private", "salience": 3, "confidence": "observed",
                 "evidence_ids": [evidence_id]}
        self._orig.append((formation, "_ask", formation._ask))

        def fake(system, user, max_tokens=0):
            if "harvest phase" in system:
                return {"entities": [{"name": "Егор", "kind": "person", "salience": 3}],
                        "claims": [claim], "open_threads": [], "contradictions": []}
            if "DIG phase" in system:
                return {"evidence_found": [evidence_id], "claims": [claim], "relations": [],
                        "questions": [], "changed_conclusion": "портрет стал точнее",
                        "stop_now": False, "stop_reason": ""}
            key = formation._clean_claim(claim, {evidence_id})["key"]
            return {"verdicts": [{"key": key, "status": "supported", "reason": "есть свидетельство",
                                   "evidence_ids": [evidence_id]}], "counterexamples": [],
                    "nothing_added": False}

        formation._ask = fake
        self._orig.append((mi, "search", mi.search))
        mi.search = lambda *a, **kw: [{"text": claim["text"], "source": "claim",
                                       "path": f"memory/life/claims/clm-{formation._clean_claim(claim, {evidence_id})['key']}.md"}]
        out = formation.run("full")
        self.assertTrue(out["ok"])
        self.assertEqual(out["run"]["passes"], 2)
        self.assertEqual(out["run"]["stop_reason"], "saturation")
        person = people.read_text(graph.resolve("Егор"))
        self.assertIn("любит высоту", person)
        self.assertIn("[source:clm-", person)
        claims = list(life.CLAIMS_DIR.glob("*.md"))
        self.assertEqual(len(claims), 1)
        self.assertEqual(formation.memory_provenance.claim_source(claims[0])[1]["status"],
                         "supported")
        self.assertTrue((self.tmp / out["report"]).exists())
        self.assertTrue(life.iter_events(kinds={"reflection_run"}))

    def test_claim_without_real_evidence_is_not_applied(self):
        compact_id, raw = self._fresh_compact()
        evidence_id = raw[0]["id"]
        bad = {"subject": "Маша", "kind": "person", "text": "выдуманный факт",
               "salience": 3, "confidence": "observed", "evidence_ids": ["evt-does-not-exist"]}
        self._orig.append((formation, "_ask", formation._ask))

        def fake(system, user, max_tokens=0):
            if "harvest phase" in system:
                return {"entities": [{"name": "Маша", "kind": "person"}], "claims": [bad]}
            if "DIG phase" in system:
                return {"evidence_found": [], "claims": [bad], "relations": [], "stop_now": True,
                        "stop_reason": "нечего подтверждать"}
            key = formation._clean_claim(bad, {evidence_id})["key"]
            return {"verdicts": [{"key": key, "status": "supported", "reason": "ошибка модели",
                                   "evidence_ids": ["evt-does-not-exist"]}]}

        formation._ask = fake
        out = formation.run("full")
        self.assertTrue(out["ok"])
        self.assertFalse(people.path_for(graph.resolve("Маша")).exists())
        claim_file = next(life.CLAIMS_DIR.glob("*.md"))
        self.assertEqual(formation.memory_provenance.claim_source(claim_file)[1]["status"],
                         "unsupported")

    def test_dig_cannot_introduce_claim_or_relation(self):
        compact_id, raw = self._fresh_compact()
        evidence_id = raw[0]["id"]
        relation = {"a": "Егор", "b": "Маша", "how": "доверяет",
                    "evidence_ids": [evidence_id]}
        candidate = {"subject": "Егор", "kind": "relation",
                     "text": "Егор — доверяет — Маша", "other": "Маша",
                     "relation": "доверяет", "visibility": "private", "salience": 2,
                     "confidence": "inferred", "evidence_ids": [evidence_id]}
        key = formation._clean_claim(candidate, {evidence_id})["key"]
        self._orig.append((formation, "_ask", formation._ask))

        def fake(system, user, max_tokens=0):
            if "harvest phase" in system:
                return {"entities": [{"name": "Егор", "kind": "person"}], "claims": []}
            if "DIG phase" in system:
                return {"evidence_found": [evidence_id], "claims": [candidate],
                        "relations": [relation],
                        "stop_now": False, "stop_reason": ""}
            return {"verdicts": [{"key": key, "status": "unsupported",
                                    "reason": "свидетельство двусмысленно",
                                    "evidence_ids": [evidence_id]}]}

        formation._ask = fake
        out = formation.run("light")
        self.assertTrue(out["ok"])
        self.assertNotIn("доверяет", graph.GRAPH_MD.read_text(encoding="utf-8")
                         if graph.GRAPH_MD.exists() else "")
        self.assertEqual(list(life.CLAIMS_DIR.glob("*.md")), [])

    def test_supported_private_relation_stays_owner_only_claim(self):
        compact_id, raw = self._fresh_compact()
        evidence_id = raw[0]["id"]
        candidate = {"subject": "Егор", "kind": "relation",
                     "text": "Егор — доверяет — Маша", "other": "Маша",
                     "relation": "доверяет", "visibility": "private", "salience": 3,
                     "confidence": "inferred", "evidence_ids": [evidence_id]}
        key = formation._clean_claim(candidate, {evidence_id})["key"]
        self._orig.append((formation, "_ask", formation._ask))

        def fake(system, user, max_tokens=0):
            if "harvest phase" in system:
                return {"entities": [{"name": "Егор", "kind": "person"}],
                        "claims": [candidate]}
            if "DIG phase" in system:
                return {"evidence_found": [evidence_id], "claims": [candidate],
                        "relations": [], "stop_now": False}
            return {"verdicts": [{"key": key, "status": "supported", "reason": "подтверждено",
                                    "evidence_ids": [evidence_id]}]}

        formation._ask = fake
        out = formation.run("light")
        self.assertTrue(out["ok"])
        self.assertFalse(graph.GRAPH_MD.exists())
        owner_hits = mi.search("доверяет", k=4, scope="owner")
        self.assertTrue(any("доверяет" in hit["text"] for hit in owner_hits))
        self.assertEqual(mi.search("доверяет", k=4, scope="group"), [])


class TestHybridRecallAndPanel(Pass19Base):
    def test_semantic_rerank_only_reorders_lexical_hits_and_shows_provenance(self):
        """Договор recall после её решения от 03.08.2026 — и чем за него заплачено.

        Раньше здесь проверялось, что запрос БЕЗ единого общего слова («кто ходит в
        горы» против «занимается альпинизмом») всё равно находит запись. Это умело
        ровно одно место — broad seed, подмешивавший в кандидаты фон, не зависящий
        от запроса.

        Praxis завершила его экспериментом и убрала. Её числа: на 12 естественных
        recall seed дал 240 кандидатов и НОЛЬ попаданий в выдачу (680/0 всего) — то
        есть платил задержкой и деньгами, ничего не меняя. Её формулировка:
        «семантический rerank остаётся — он ранжирует уже найденных lexical
        кандидатов, не подмешивая фон».

        ⚠ Цена названа вслух, а не спрятана: находка по смыслу БЕЗ совпадения слов
        больше невозможна, и на проде тоже (режим hybrid-hosted-rerank, векторного
        пути нет). Синтетический случай ниже — ровно тот, который разменяли.
        Вернуть способность — отдельная работа (настоящие эмбеддинги либо узкий,
        не-фоновый источник кандидатов), а не правка этого файла.
        """
        # A compact is canonical Markdown and cites its primary event.
        event = life.record_message("7", "Егор: занимаюсь альпинизмом", actor="Егор", direction="in")
        meta = life._write_compact(
            "7", {"summary": "Егор занимается альпинизмом.", "open_threads": [], "claims": [], "episodes": []},
            tier=1, depth=1, source_events=[event["id"]], source_compacts=[], event_count=1,
            continued=False)
        self._orig.append((mi, "_semantic_rerank", mi._semantic_rerank))
        mi._semantic_rerank = lambda q, cs: [1.0 if "альпинизм" in c["text"] else 0.0 for c in cs]
        # Цена договора: фона в кандидатах больше нет.
        no_overlap = mi.search("кто ходит в горы", k=3, scope="owner", semantic=True)
        self.assertFalse([h for h in no_overlap if "альпинизм" in h["text"]],
                         "фон вернулся в кандидаты — это смена договора, а не теста")

        # А то, ради чего rerank и живёт: на запросе СО совпадением он задаёт
        # порядок, и происхождение записи видно.
        hits = mi.search("альпинизмом", k=3, scope="owner", semantic=True)
        hit = next(h for h in hits if "альпинизм" in h["text"])
        self.assertGreater(hit["signals"]["semantic"], 0.9)
        self.assertIn(event["id"], hit["provenance"])
        self.assertEqual(mi.search("альпинизм", k=3, scope="group", semantic=False), [],
                         "memory/life leaked into group scope")

    def test_panel_request_is_deferred_to_main_process(self):
        self._orig.append((mi, "retrieval_mode", mi.retrieval_mode))
        mi.retrieval_mode = lambda: "hybrid-hosted-rerank"
        out = panel.life_request("full")
        self.assertTrue(out["ok"])
        self.assertEqual(formation.requested()["depth"], "full")
        state = panel.life_state()
        self.assertEqual(state["recall_mode"], "hybrid-hosted-rerank")
        self.assertEqual(state["formation"]["request"]["depth"], "full")


if __name__ == "__main__":
    unittest.main(verbosity=2)
