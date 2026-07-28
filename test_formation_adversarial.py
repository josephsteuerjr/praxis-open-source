"""Adversarial gates for the PASS 19 formation authority boundary."""
from __future__ import annotations

import formation
import graph
import memory_provenance
import memory_life as life
import people

from test_pass19 import Pass19Base


class TestFormationAdversarial(Pass19Base):
    def _fresh_compact(self, *, start: float = 1_700_000_000.0):
        raw = self.add_messages("7", 6, start=start, gap_after={3})
        result = life.compact_if_due("7")
        self.assertTrue(result.get("ok") and result.get("compact_id"), result)
        return result["compact_id"], raw

    def _install_supported_model(self, current: dict) -> None:
        self._orig.append((formation, "_ask", formation._ask))

        def fake(system, user, max_tokens=0):
            claim = current["claim"]
            evidence_id = current["evidence_id"]
            if "harvest phase" in system:
                return {"entities": [{"name": claim["subject"], "kind": claim["kind"]}],
                        "claims": [claim], "open_threads": [], "contradictions": []}
            if "DIG phase" in system:
                return {"evidence_found": [evidence_id], "questions": [],
                        "changed_conclusion": "", "stop_now": False, "stop_reason": ""}
            key = formation._clean_claim(claim, {evidence_id})["key"]
            return {"verdicts": [{"key": key, "status": "supported",
                                   "reason": "primary event supports the candidate",
                                   "evidence_ids": [evidence_id]}],
                    "counterexamples": [], "nothing_added": False}

        formation._ask = fake

    def test_attack_cannot_substitute_another_allowed_event(self):
        _compact_id, raw = self._fresh_compact()
        frozen_id, substitute_id = raw[0]["id"], raw[1]["id"]
        claim = {"subject": "Егор", "kind": "person",
                 "text": "предпочитает строгое происхождение памяти",
                 "visibility": "private", "salience": 3, "confidence": "inferred",
                 "evidence_ids": [frozen_id]}
        key = formation._clean_claim(claim, {frozen_id, substitute_id})["key"]
        self._orig.append((formation, "_ask", formation._ask))

        def fake(system, user, max_tokens=0):
            if "harvest phase" in system:
                return {"entities": [{"name": "Егор", "kind": "person"}],
                        "claims": [claim]}
            if "DIG phase" in system:
                return {"evidence_found": [substitute_id], "questions": [],
                        "changed_conclusion": "", "stop_now": False, "stop_reason": ""}
            return {"verdicts": [{"key": key, "status": "supported",
                                   "reason": "substituted a different allowed event",
                                   "evidence_ids": [substitute_id]}]}

        formation._ask = fake
        out = formation.run("light")

        self.assertTrue(out["ok"])
        claim_path = life.CLAIMS_DIR / f"clm-{key}.md"
        _kind, meta = memory_provenance.claim_source(claim_path)
        self.assertEqual(meta["status"], "unsupported")
        self.assertEqual(meta["evidence_ids"], [])
        self.assertFalse(people.path_for(graph.resolve("Егор")).exists())

    def test_compact_dossier_and_graph_prose_never_reaches_formation_prompts(self):
        compact_id, _raw = self._fresh_compact()
        markers = {
            "COMPACT_RECAP_AUTHORITY_POISON",
            "DOSSIER_AUTHORITY_POISON",
            "GRAPH_AUTHORITY_POISON",
        }
        compact_meta = life.compact_meta(compact_id, "7")
        compact_path = self.tmp / compact_meta["path"]
        compact_path.write_text(
            compact_path.read_text(encoding="utf-8").replace(
                "## Суть\n", "## Суть\nCOMPACT_RECAP_AUTHORITY_POISON\n", 1),
            encoding="utf-8",
        )
        people.path_for("egor").write_text(
            "# Егор\n\n## Факты\n- DOSSIER_AUTHORITY_POISON\n", encoding="utf-8")
        graph.GRAPH_MD.write_text(
            "# Граф\n\n- GRAPH_AUTHORITY_POISON\n", encoding="utf-8")
        calls: list[tuple[str, str]] = []
        self._orig.append((formation, "_ask", formation._ask))

        def fake(system, user, max_tokens=0):
            calls.append((system, user))
            if "harvest phase" in system:
                return {"entities": [{"name": "Егор", "kind": "person"}], "claims": []}
            if "DIG phase" in system:
                return {"evidence_found": [], "questions": [], "changed_conclusion": "",
                        "stop_now": False, "stop_reason": ""}
            return {"verdicts": [], "counterexamples": [], "nothing_added": True}

        formation._ask = fake
        out = formation.run("light")

        self.assertTrue(out["ok"])
        self.assertEqual(out["run"]["passes"], 1)
        self.assertEqual(len(calls), 3)
        for _system, user in calls:
            for marker in markers:
                self.assertNotIn(marker, user)
        self.assertIn("# PRIMARY EVENT EVIDENCE", calls[0][1])
        self.assertIn("# PRIMARY EVENT EVIDENCE", calls[2][1])

    def test_supported_contradiction_contests_previous_supported_claim(self):
        _compact_id, raw = self._fresh_compact()
        first = {"subject": "Егор", "kind": "person", "text": "любит ранние подъёмы",
                 "visibility": "private", "salience": 2, "confidence": "inferred",
                 "evidence_ids": [raw[0]["id"]]}
        current = {"claim": first, "evidence_id": raw[0]["id"]}
        self._install_supported_model(current)

        first_out = formation.run("light")
        self.assertTrue(first_out["ok"])
        first_key = formation._clean_claim(first, {raw[0]["id"]})["key"]
        first_id = f"clm-{first_key}"
        first_path = life.CLAIMS_DIR / f"{first_id}.md"
        _kind, first_meta = memory_provenance.claim_source(first_path)
        self.assertEqual(first_meta["status"], "supported")
        self.assertTrue(first_meta["_automatic_eligible"])

        _compact_id, raw = self._fresh_compact(start=1_700_001_000.0)
        second = {"subject": "Егор", "kind": "person", "text": "не любит ранние подъёмы",
                  "visibility": "private", "salience": 3, "confidence": "inferred",
                  "evidence_ids": [raw[0]["id"]], "contradicts": [first_id]}
        current.update(claim=second, evidence_id=raw[0]["id"])

        second_out = formation.run("light")
        self.assertTrue(second_out["ok"])
        second_key = formation._clean_claim(second, {raw[0]["id"]})["key"]
        second_id = f"clm-{second_key}"
        second_path = life.CLAIMS_DIR / f"{second_id}.md"
        _kind, second_meta = memory_provenance.claim_source(second_path)
        _kind, first_meta = memory_provenance.claim_source(first_path)

        self.assertEqual(second_meta["status"], "supported")
        self.assertTrue(second_meta["_automatic_eligible"])
        self.assertEqual(second_meta["contradicts"], [first_id])
        self.assertEqual(first_meta["status"], "contested")
        self.assertFalse(first_meta["_automatic_eligible"])
        self.assertIn(second_id, first_meta["contradicts"])


if __name__ == "__main__":
    import unittest
    unittest.main()
