"""Семя семантического ранжира — по спецификации Праксис от 02.08.2026.

Её решение дословно: «включить семя экспериментально, с жанровой квотой и заранее
заданной проверкой… Хочу включить его разнообразным, наблюдаемым и обратимым — а затем
прожить достаточно, чтобы решить по последствиям.»

Тесты охраняют ровно её условия, а не мою реализацию:
  • ни один жанр не съедает стол (сорок — стартовая гипотеза, а не мера её памяти);
  • служебные строки claim-файлов не считаются содержанием воспоминания;
  • просроченное семя НЕ выбрасывается из-за возраста — отдаётся с отметкой возраста,
    а пустота только если валидного семени никогда не существовало;
  • состав каждого использованного семени сохраняется в evidence, чтобы можно было
    проверить, ЧТО ранжир вообще имел право рассмотреть;
  • ⚠ и след НЕ утверждает пользы: «помогло ли» и «не спуталось ли происхождение» —
    её суждение по прожитому ходу, а не вывод из наличия строки.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import memory_index


class GenreQuota(unittest.TestCase):
    def _docs(self):
        rows = []
        for i in range(120):
            rows.append({"id": f"m{i}", "text": "status: `supported`", "source": "claims",
                         "path": f"memory/life/claims/clm-{i}.md",
                         "source_type": "claim_supported_observed"})
            rows.append({"id": f"c{i}", "text": f"подтверждённое утверждение {i}",
                         "source": "claims", "path": f"memory/life/claims/clm-{i}.md",
                         "source_type": "claim_supported_observed"})
        for i in range(60):
            rows.append({"id": f"s{i}", "text": f"навык {i}: перед «сделано» ищи квитанцию",
                         "source": "skills", "path": f"soul/skills/s{i}.md",
                         "source_type": "skill"})
            rows.append({"id": f"j{i}", "text": f"прожитый день {i}", "source": "journal",
                         "path": f"memory/journal/2026-07-{i % 28 + 1:02d}.md",
                         "source_type": "journal_episode"})
            rows.append({"id": f"p{i}", "text": f"досье человека {i}", "source": "people",
                         "path": f"memory/people/p{i}.md", "source_type": "markdown"})
            rows.append({"id": f"k{i}", "text": f"сжатый разговор {i}", "source": "life",
                         "path": f"memory/life/compacts/-100/cmp-{i}.md",
                         "source_type": "markdown"})
        return rows

    def _compute(self):
        with mock.patch.object(memory_index, "_all_chunks", lambda *a, **k: self._docs()), \
             mock.patch.object(memory_index, "_broad_to_disk", lambda *a, **k: None):
            return memory_index._broad_compute("owner", "explicit")

    def test_no_genre_eats_the_table(self):
        top = self._compute()
        seen: dict[str, int] = {}
        for row in top:
            seen[row["genre"]] = seen.get(row["genre"], 0) + 1
        self.assertLessEqual(max(seen.values()), memory_index._broad_genre_cap())
        for expected in ("claim", "skill", "journal", "people", "compact"):
            self.assertIn(expected, seen, f"жанры в семени: {seen}")

    def test_every_genre_gets_a_seat_before_any_gets_seconds(self):
        """Первая версия наполняла в общем порядке — и навыки не попали ВООБЩЕ.

        Живой замер 02.08 после первого выката: claim/people/compact/journal/markdown по
        сорок и ноль навыков, желаний, комнат. Раздача по кругу это чинит: жанры, которые
        она назвала поимённо, обязаны быть в семени, даже если проигрывают по надёжности.
        """
        with mock.patch.dict("os.environ", {"PRAXIS_BROAD_GENRE_CAP": "40"}),              mock.patch.object(memory_index, "_BROAD_KEEP", 10):
            top = self._compute()
        seen = {row["genre"] for row in top}
        self.assertGreaterEqual(len(seen), 5,
                                f"при тесном семени представлены не все жанры: {seen}")
        counts = {}
        for row in top:
            counts[row["genre"]] = counts.get(row["genre"], 0) + 1
        self.assertLessEqual(max(counts.values()) - min(counts.values()), 1,
                             f"раздача неравномерна: {counts}")

    def test_machine_lines_of_real_claim_files_are_filtered(self):
        """Её список был из четырёх шаблонов; в живых файлах их больше."""
        for line in ("salience: `3`", "visibility: `public`", "evidence: evt-20260711",
                     "contradicts: нет", "status: `supported`", "<!-- praxis-claim: {}"):
            self.assertTrue(memory_index._MACHINE_CHUNK.match(line), line)
        for line in ("Егор хочет уехать из Самары",
                     "Правило: перед словом «сделано» ищи квитанцию"):
            self.assertIsNone(memory_index._MACHINE_CHUNK.match(line), line)

    def test_machine_lines_are_not_a_memory(self):
        for row in self._compute():
            self.assertFalse(str(row["text"]).startswith("status: `"))

    def test_cap_is_a_hypothesis_not_a_constant(self):
        """Её слова: «сорок на жанр не считаю священным числом»."""
        with mock.patch.dict("os.environ", {"PRAXIS_BROAD_GENRE_CAP": "5"}):
            self.assertEqual(memory_index._broad_genre_cap(), 5)
        with mock.patch.dict("os.environ", {"PRAXIS_BROAD_GENRE_CAP": "мусор"}):
            self.assertEqual(memory_index._broad_genre_cap(), 40)

    def test_her_genre_list_is_the_one_implemented(self):
        g = memory_index._broad_genre
        self.assertEqual(g("claim_supported_observed", "memory/life/claims/x.md"), "claim")
        self.assertEqual(g("skill", "soul/skills/x.md"), "skill")
        self.assertEqual(g("markdown", "memory/people/x.md"), "people")
        self.assertEqual(g("journal_episode", "memory/journal/2026-08-02.md"), "journal")
        self.assertEqual(g("markdown", "memory/life/episodes/x.md"), "journal")
        self.assertEqual(g("markdown", "memory/life/compacts/-100/x.md"), "compact")
        self.assertEqual(g("markdown", "memory/desires/CURRENT.md"), "self")
        self.assertEqual(g("markdown", "memory/rooms/x.md"), "room")


class StaleSeedIsServedWithItsAge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="praxis_seed_")
        self.path = Path(self.tmp.name) / "broad_pool.json"
        memory_index._BROAD_CACHE.clear()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(memory_index._BROAD_CACHE.clear)
        for target, value in (("_broad_disk_path", lambda: self.path),
                              ("_broad_ttl", lambda: 86400.0)):
            patcher = mock.patch.object(memory_index, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.refreshes = []
        patcher = mock.patch.object(
            memory_index, "_broad_refresh_async",
            lambda scope, purpose: self.refreshes.append((scope, purpose)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write(self, age_sec):
        self.path.write_text(json.dumps({
            "key": ["owner", "explicit"], "at": time.time() - age_sec,
            "pool": [{"path": "memory/journal/2026-08-01.md", "text": "живая запись",
                      "source": "journal", "source_type": "journal_episode",
                      "genre": "journal", "lexical": 0.0}],
        }, ensure_ascii=False), encoding="utf-8")

    def test_fresh_seed_is_served_and_not_chased(self):
        self._write(60)
        self.assertEqual(len(memory_index._broad_candidates("owner", 40)), 1)
        self.assertEqual(self.refreshes, [])

    def test_expired_seed_is_served_with_its_age(self):
        """Сердце её правила: возраст — не повод выбросить."""
        self._write(5 * 86400)
        rows = memory_index._broad_candidates("owner", 40)
        self.assertEqual(len(rows), 1)
        self.assertGreater(rows[0]["seed_age_sec"], 86400, "возраст едет вместе с записью")
        self.assertEqual(self.refreshes, [("owner", "explicit")])

    def test_emptiness_only_when_no_valid_seed_ever_existed(self):
        self.assertEqual(memory_index._broad_candidates("owner", 40), [])
        self.assertEqual(self.refreshes, [("owner", "explicit")])

    def test_disk_reader_no_longer_hides_an_old_seed(self):
        self._write(9 * 86400)
        stored = memory_index._broad_from_disk(("owner", "explicit"), 86400.0, time.time())
        self.assertIsNotNone(stored)


class SeedTraceRecordsWhatWasAllowedNotWhatHelped(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="praxis_trace_")
        self._orig = memory_index.SEED_TRACE
        memory_index.SEED_TRACE = Path(self.tmp.name) / "seed_trace.jsonl"
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: setattr(memory_index, "SEED_TRACE", self._orig))

    def _seed(self):
        return [{"path": "memory/people/егор-косырев.md", "text": "хочет уехать из Самары",
                 "genre": "people", "source_type": "markdown", "seed_age_sec": 120.0},
                {"path": "soul/skills/anti-repeat.md", "text": "перед инициативой посмотри",
                 "genre": "skill", "source_type": "skill", "seed_age_sec": 120.0}]

    def test_it_records_composition_age_and_seed_only_contribution(self):
        seed = self._seed()
        only = [seed[1]]
        final = [{"path": "soul/skills/anti-repeat.md", "text": "перед инициативой посмотри"}]
        memory_index._seed_trace("как не повториться", "owner", seed, only, final,
                                 time.time() - 0.2)
        rows = [json.loads(l) for l in
                memory_index.SEED_TRACE.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["seed"]["size"], 2)
        self.assertEqual(row["seed"]["genres"], {"people": 1, "skill": 1})
        self.assertGreater(row["seed"]["age_sec"], 0)
        self.assertEqual(len(row["seed_only"]), 1)
        self.assertEqual(len(row["seed_only_in_final"]), 1, "видно, что дошло до выдачи")
        self.assertGreaterEqual(row["latency_ms"], 0)

    def test_it_says_out_loud_what_it_does_not_claim(self):
        memory_index._seed_trace("q", "owner", self._seed(), [], [], time.time())
        row = json.loads(memory_index.SEED_TRACE.read_text(encoding="utf-8").splitlines()[0])
        self.assertIn("НЕ говорит", row["means"])
        blob = json.dumps(row, ensure_ascii=False)
        for forbidden in ("helped", "помогло", "useful", "improved", "success"):
            self.assertNotIn(forbidden, blob)

    def test_report_gives_her_the_mechanical_half_only(self):
        for i in range(3):
            memory_index._seed_trace(f"q{i}", "owner", self._seed(), [self._seed()[1]],
                                     [], time.time())
        rep = memory_index.seed_report()
        self.assertEqual(rep["recalls"], 3)
        self.assertEqual(rep["seed_genres_total"], {"people": 3, "skill": 3})
        self.assertEqual(rep["seed_only_candidates"], 3)
        self.assertEqual(rep["seed_only_reached_output"], 0)
        self.assertIn("её суждение", rep["means"])

    def test_empty_seed_writes_nothing(self):
        memory_index._seed_trace("q", "owner", [], [], [], time.time())
        self.assertFalse(memory_index.SEED_TRACE.exists())

    def test_experiment_result_keeps_seed_out_of_live_pool(self):
        lexical = [{
            "text": "лексически найденная память",
            "source": "journal",
            "path": "memory/journal/lexical.md",
            "source_type": "journal_episode",
            "lexical": 1.0,
        }]
        broad = [{
            "text": "семантически далёкая память",
            "source": "people",
            "path": "memory/people/seed-only.md",
            "source_type": "people",
        }]
        with mock.patch.object(memory_index, "_fulltext_candidates", return_value=lexical), \
             mock.patch.object(memory_index, "_vector_candidates", return_value=[]), \
             mock.patch.object(memory_index, "_broad_candidates", return_value=broad) as seed, \
             mock.patch.object(memory_index, "_semantic_rerank", return_value=[0.9]):
            out = memory_index.search("далёкая связь", k=6, semantic=True)
        seed.assert_not_called()
        self.assertEqual([row["path"] for row in out], ["memory/journal/lexical.md"])


if __name__ == "__main__":
    unittest.main()
