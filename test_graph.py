"""Граф памяти: рёбра в маркдауне, BFS-обход, дедуп, размещение рёбер."""
import unittest

import graph
import people


def _mk_person(slug: str, name: str) -> None:
    people.write(slug, name, {})


def _clean() -> None:
    for p in people.PEOPLE_DIR.glob("*.md"):
        if not p.stem.startswith("_"):
            p.unlink()
    if graph.GRAPH_MD.exists():
        graph.GRAPH_MD.unlink()


class GraphEdges(unittest.TestCase):
    def setUp(self):
        _clean()

    def test_person_link_written_and_parsed(self):
        _mk_person("vika", "Вика")
        out = graph.add_edge("vika", "egor", "коллега по фирме")
        self.assertIn("Связала", out)
        self.assertIn(("egor", "коллега по фирме"), people.links("vika"))
        es = graph.edges()
        self.assertEqual(len(es), 1)
        self.assertEqual({es[0]["a"], es[0]["b"]}, {"vika", "egor"})

    def test_dedup_same_pair_and_label(self):
        _mk_person("vika", "Вика")
        graph.add_edge("vika", "egor", "коллега")
        out = graph.add_edge("egor", "Вика", "коллега")  # та же пара, другой порядок, имя вместо слага
        self.assertIn("уже есть", out)
        self.assertEqual(len(graph.edges()), 1)

    def test_resolve_name_to_person_file(self):
        _mk_person("vika", "Вика")
        self.assertEqual(graph.resolve("Вика"), "vika")
        self.assertEqual(graph.resolve("vika"), "vika")
        self.assertEqual(graph.resolve("тема-права"), "тема-права")

    def test_non_person_edge_goes_to_graph_md(self):
        out = graph.add_edge("тема-права", "суд-по-ип", "спор")
        self.assertIn("Связала", out)
        self.assertTrue(graph.GRAPH_MD.exists())
        self.assertEqual(len(graph.edges()), 1)

    def test_self_loop_rejected(self):
        _mk_person("vika", "Вика")
        self.assertIn("сам с собой", graph.add_edge("vika", "Вика"))

    def test_prefers_person_file_end(self):
        _mk_person("anna", "Анна")
        graph.add_edge("проект-икс", "anna", "ведёт")
        self.assertFalse(graph.GRAPH_MD.exists())          # легло в файл Анны
        self.assertIn(("проект-икс", "ведёт"), people.links("anna"))


class GraphTraverse(unittest.TestCase):
    def setUp(self):
        _clean()
        _mk_person("egor", "Егор")
        _mk_person("vika", "Вика")
        _mk_person("misha", "Миша")
        graph.add_edge("egor", "vika", "коллега")
        graph.add_edge("vika", "misha", "сокурсник")
        graph.add_edge("egor", "тема-праксис", "строит")

    def test_neighbors_depth1(self):
        t = graph.neighbors_text("egor", 1)
        self.assertIn("Вика", t)
        self.assertIn("коллега", t)
        self.assertNotIn("Миша", t)  # 2-й шаг при depth=1 не виден

    def test_neighbors_depth2_shows_chain(self):
        t = graph.neighbors_text("egor", 2)
        self.assertIn("Миша", t)
        self.assertIn("сокурсник", t)
        self.assertIn("Вика", t)     # цепочка проходит через Вику

    def test_path_found_and_readable(self):
        t = graph.path_text("misha", "тема-праксис")
        self.assertIn("Миша", t)     # миша — вика — егор — тема-праксис
        self.assertIn("Егор", t)
        self.assertIn("тема-праксис", t)

    def test_path_absent_is_honest(self):
        _mk_person("stranger", "Чужой")
        self.assertIn("не вижу", graph.path_text("stranger", "egor"))

    def test_no_links_yet(self):
        _mk_person("empty", "Пустой")
        self.assertIn("нет записанных связей", graph.neighbors_text("empty"))

    def test_related_lines_dedup_and_cap(self):
        lines = graph.related_lines(["egor", "vika"], cap=10)
        joined = "\n".join(lines)
        self.assertIn("—(коллега)—", joined)
        self.assertEqual(len(lines), len(set(lines)))       # без дублей
        self.assertLessEqual(len(lines), 10)

    def test_max_nodes_cap(self):
        for i in range(graph.MAX_NODES_OUT + 3):
            graph.add_edge("egor", f"узел-{i}", "тест")
        t = graph.neighbors_text("egor", 1)
        self.assertIn("не разворачиваю", t)


class Pass86Aliases(unittest.TestCase):
    """PASS 8.6: алиасы — «Егор» и yegor-kosyrev — один узел."""

    def setUp(self):
        _clean()
        _mk_person("yegor-kosyrev", "Yegor Kosyrev")

    def test_alias_roundtrip_survives_rewrite(self):
        self.assertTrue(people.add_alias("yegor-kosyrev", "Егор"))
        self.assertFalse(people.add_alias("yegor-kosyrev", "егор"), "дедуп по casefold")
        self.assertIn("Егор", people.aliases("yegor-kosyrev"))
        text = people.read_text("yegor-kosyrev")
        self.assertIn("Алиасы: Егор", text, "строка алиасов в досье")
        # перезапись файла (append_fact -> render) не теряет алиасы
        people.append_fact("yegor-kosyrev", "Yegor Kosyrev", "строит Praxis")
        self.assertIn("Егор", people.aliases("yegor-kosyrev"))

    def test_resolve_by_alias(self):
        people.add_alias("yegor-kosyrev", "Егор")
        self.assertEqual(graph.resolve("Егор"), "yegor-kosyrev")
        self.assertEqual(graph.resolve("егор"), "yegor-kosyrev")
        self.assertEqual(graph.resolve("Незнакомец"), "незнакомец")

    def test_edge_via_alias_lands_on_canonical(self):
        people.add_alias("yegor-kosyrev", "Егор")
        graph.add_edge("Егор", "тема-праксис", "строит")
        self.assertTrue(any(d == "тема-праксис" for d, _ in people.links("yegor-kosyrev")))

    def test_agent_tool_add_alias(self):
        import agent
        out = agent.tool_add_alias("Егор", "yegor-kosyrev")
        self.assertIn("Связала", out)
        self.assertEqual(graph.resolve("Егор"), "yegor-kosyrev")
        self.assertIn("и так", agent.tool_add_alias("Егор", "yegor-kosyrev"))
        self.assertIn("Не вижу", agent.tool_add_alias("Кто-то", "нет-такого-досье"))


class Pass86EdgeHygiene(unittest.TestCase):
    def setUp(self):
        _clean()
        _mk_person("vika", "Вика")

    def test_close_label_no_dup_date_touched(self):
        graph.add_edge("vika", "egor", "коллега")
        # подпись близкая («коллеги») — дубль не плодится, дата обновляется
        out = graph.add_edge("vika", "egor", "коллеги")
        self.assertIn("уже есть", out)
        self.assertIn("обновила дату", out)
        self.assertEqual(len(graph.edges()), 1)

    def test_sleep_edges_marked(self):
        graph.add_edge("vika", "тема-права", "спорит", source="сон")
        e = graph.edges()[0]
        self.assertIn("(сон)", e["label"])

    def test_forget_connection_both_stores(self):
        graph.add_edge("vika", "egor", "коллега")           # в файл Вики
        graph.add_edge("тема-права", "суд-по-ип", "спор")   # в graph.md
        out = graph.forget_connection("Вика", "egor")
        self.assertIn("Забыла", out)
        self.assertEqual([e for e in graph.edges() if {"vika", "egor"} == {e["a"], e["b"]}], [])
        out = graph.forget_connection("тема-права", "суд-по-ип")
        self.assertIn("Забыла", out)
        self.assertEqual(graph.edges(), [])
        self.assertIn("и так нет", graph.forget_connection("vika", "egor"))


class Pass86MemGraphPanel(unittest.TestCase):
    def setUp(self):
        _clean()

    def test_empty_graph_safe(self):
        import panel
        g = panel.memgraph()
        self.assertEqual(g["links"], [])
        self.assertIsInstance(g["nodes"], list)

    def test_nodes_links_shape(self):
        import panel
        _mk_person("vika", "Вика")
        graph.add_edge("vika", "тема-права", "спорит")
        g = panel.memgraph()
        by_id = {n["id"]: n for n in g["nodes"]}
        self.assertEqual(by_id["vika"]["kind"], "people")
        self.assertEqual(by_id["vika"]["label"], "Вика")
        self.assertEqual(by_id["тема-права"]["kind"], "topic")
        self.assertEqual(by_id["vika"]["degree"], 1)
        self.assertEqual(g["links"][0]["label"], "спорит")

    def test_lonely_people_included(self):
        import panel
        _mk_person("solo", "Одинокий")
        ids = {n["id"] for n in panel.memgraph()["nodes"]}
        self.assertIn("solo", ids)


class Pass86SleepAliasSuggestion(unittest.TestCase):
    def setUp(self):
        _clean()

    def test_similar_name_suggests_not_merges(self):
        import agent
        import consolidate
        _mk_person("yegor-kosyrev", "Yegor Kosyrev")
        consolidate._apply_person("Yegor Kosyrew", {"facts": [{"fact": "тест"}]})
        # новое досье создано (молча НЕ слито)…
        self.assertTrue(people.path_for("yegor-kosyrew").exists())
        # …а в дневнике — предложение алиаса
        jr = "".join(p.read_text(encoding="utf-8") for p in agent.JOURNAL_DIR.glob("*.md"))
        self.assertIn("похоже на досье yegor-kosyrev", jr)
        self.assertIn("add_alias", jr)

    def test_alias_merges_without_suggestion(self):
        import consolidate
        _mk_person("yegor-kosyrev", "Yegor Kosyrev")
        people.add_alias("yegor-kosyrev", "Егор")
        consolidate._apply_person("Егор", {"facts": [{"fact": "любит горы"}]})
        self.assertFalse(people.path_for("егор").exists(), "алиас не должен плодить досье")
        self.assertIn("любит горы", people.read_text("yegor-kosyrev"))


if __name__ == "__main__":
    unittest.main()
