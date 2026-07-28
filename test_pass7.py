"""PASS 7: обвязка графа и контура предложений в agent/runner (smoke + гейты)."""
import json
import unittest

import agent
import graph
import people
import selfdev


class ToolsWired(unittest.TestCase):
    def test_tool_impl_has_new_tools(self):
        for name in ("connections", "start_proposal", "submit_proposal", "list_proposals"):
            self.assertIn(name, agent.TOOL_IMPL)

    def test_owner_tools_expose_new_tools(self):
        names = {t["name"] for t in agent.OWNER_TOOLS}
        for name in ("connections", "start_proposal", "submit_proposal", "list_proposals"):
            self.assertIn(name, names)
        base_names = {t["name"] for t in agent.BASE_TOOLS}
        self.assertNotIn("connections", base_names)   # карта и контур — не для чужих каналов

    def test_remember_schema_has_relates(self):
        rem = next(t for t in agent.BASE_TOOLS if t["name"] == "remember")
        self.assertIn("relates_to", rem["input_schema"]["properties"])
        self.assertIn("relation", rem["input_schema"]["properties"])


class RememberWritesEdge(unittest.TestCase):
    def setUp(self):
        for p in people.PEOPLE_DIR.glob("*.md"):
            if not p.stem.startswith("_"):
                p.unlink()
        if graph.GRAPH_MD.exists():
            graph.GRAPH_MD.unlink()

    def test_remember_with_relates_to(self):
        out = agent.tool_remember("Вика", "работает юристом", relates_to="Егор", relation="коллега")
        self.assertIn("Запомнила", out)
        self.assertIn("Связала", out)
        self.assertTrue(graph.edges())

    def test_remember_without_relates_is_unchanged(self):
        out = agent.tool_remember("Вика", "работает юристом")
        self.assertIn("Запомнила", out)
        self.assertEqual(graph.edges(), [])


class ConnectionsScopeGate(unittest.TestCase):
    def tearDown(self):
        agent._CURRENT_SCOPE = "owner"

    def test_owner_sees(self):
        agent._CURRENT_SCOPE = "owner"
        self.assertNotIn("не для этого канала", agent.tool_connections("Егор"))

    def test_group_refused(self):
        for scope in ("group", "known", "unknown"):
            agent._CURRENT_SCOPE = scope
            self.assertIn("не для этого канала", agent.tool_connections("Егор"))


class StateShowsProposals(unittest.TestCase):
    def test_pending_proposal_in_state(self):
        selfdev.LEDGER.parent.mkdir(parents=True, exist_ok=True)
        selfdev.LEDGER.write_text(json.dumps([{
            "id": "abc12345", "title": "тестовое", "why": "", "branch": "proposal/abc12345",
            "status": "proposed", "zone": "review", "files": ["agent.py"], "diffstat": "",
            "tests": {"ok": True, "summary": "1 тест"}, "created": "", "updated": "",
            "notified": True, "decided_by": "", "reason": ""}], ensure_ascii=False),
            encoding="utf-8")
        try:
            state = agent.build_state_block()
            evidence = agent.build_state_evidence_block()
            self.assertIn('"fact":"selfdev","pending_review_count":1', state)
            self.assertNotIn("abc12345", state)
            self.assertIn("abc12345", evidence)
        finally:
            selfdev.LEDGER.unlink()

    def test_no_proposals_no_line(self):
        selfdev.LEDGER.unlink(missing_ok=True)
        self.assertIn('"fact":"selfdev","pending_review_count":0', agent.build_state_block())


class RunnerControlJob(unittest.TestCase):
    def test_clock_has_control_job(self):
        import mtproto_runner as r
        jobs = r._clock_jobs()
        self.assertIn("control", jobs)
        period, care = jobs["control"]
        self.assertGreaterEqual(period, 5.0)
        self.assertTrue(callable(care))


class GraphTierInPrompt(unittest.TestCase):
    def setUp(self):
        for p in people.PEOPLE_DIR.glob("*.md"):
            if not p.stem.startswith("_"):
                p.unlink()

    def test_links_tier_owner_only(self):
        people.write("vika", "Вика", {})
        graph.add_edge("vika", "egor-hozyain", "коллега")
        _, dyn_owner = agent.build_system_parts(speaker="Вика", owner=True, known=True, is_dm=True)
        self.assertNotIn("Связи Вика (граф памяти)", dyn_owner)
        _, dyn_group = agent.build_system_parts(speaker="Вика", owner=False, known=True, is_dm=False)
        self.assertNotIn("граф памяти", dyn_group)


if __name__ == "__main__":
    unittest.main()
