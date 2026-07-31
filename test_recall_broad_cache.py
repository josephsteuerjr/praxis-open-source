"""Общий пул recall: она НИКОГДА не ждёт его пересчёта.

Пул `_broad_candidates` — семя для семантического ранжира, от запроса не зависит, но
пересчитывался на каждый явный recall: полный обход источников, чтение и чанкование
всего markdown-канона, сортировка тремя оценщиками. Замер на живом проде 31.07 — **453.5
секунды**; её собственные прогоны показывали recall p50 471с, дважды сторож отпускал ход
по потолку 600с с висящей рукой.

Контракт после правки: пул отдаётся из памяти или с диска; если его нет или он протух —
возвращается ПУСТО, а пересчёт уходит в отдельный процесс. Пустое семя честно и дёшево:
точные попадания даёт FTS, он мгновенный. Ждать семь минут посреди хода нельзя никогда.

Запуск:  python praxis_test.py test_recall_broad_cache -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import memory_index


def _docs(n: int = 60) -> list[dict]:
    return [{"id": f"memory/m.md#{i}", "text": f"важное {i}", "source": "m",
             "path": "memory/m.md", "source_type": "markdown"} for i in range(n)]


class BroadPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        memory_index._BROAD_CACHE.clear()
        self.addCleanup(memory_index._BROAD_CACHE.clear)
        # Свой файл пула: тест не должен зависеть от того, увела ли песочница MEM_DIR.
        self.tmp = tempfile.TemporaryDirectory(prefix="praxis_broad_")
        self.addCleanup(self.tmp.cleanup)
        self.disk = Path(self.tmp.name) / "broad_pool.json"
        for target, repl in (("_broad_disk_path", lambda: self.disk),):
            patcher = mock.patch.object(memory_index, target, repl)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Ни один тест не имеет права породить настоящий процесс пересчёта.
        self.refreshes: list[tuple[str, str]] = []
        patcher = mock.patch.object(
            memory_index, "_broad_refresh_async",
            lambda scope, purpose: self.refreshes.append((scope, purpose)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _live_env(self, **extra):
        """Окружение «как в бою»: PRAXIS_TEST снят, значит кэш включён."""
        env = mock.patch.dict(os.environ, {"PRAXIS_BROAD_TTL": "3600", **extra})
        env.start()
        os.environ.pop("PRAXIS_TEST", None)
        self.addCleanup(env.stop)

    def test_cold_pool_never_blocks_the_turn(self):
        """Главный контракт: холодный вызов НЕ считает канон и возвращается сразу."""
        self._live_env()
        with mock.patch.object(memory_index, "_all_chunks",
                               mock.Mock(side_effect=AssertionError("канон считался в её ходе"))):
            pool = memory_index._broad_candidates("owner", 40, "explicit")
        self.assertEqual(pool, [], "пустое семя — это норма, пока пул не готов")
        self.assertEqual(self.refreshes, [("owner", "explicit")], "пересчёт не запрошен")

    def test_ready_pool_is_served_without_refresh(self):
        self._live_env()
        memory_index._broad_to_disk(("owner", "explicit"), _docs(60), memory_index.time.time())
        pool = memory_index._broad_candidates("owner", 40, "explicit")
        self.assertEqual(len(pool), 40)
        self.assertEqual(self.refreshes, [], "свежий пул не должен просить пересчёт")

    def test_pool_survives_a_restart(self):
        """Она перезапускает себя на каждом смёрженном proposal — пул обязан переживать это."""
        self._live_env()
        memory_index._broad_to_disk(("owner", "explicit"), _docs(60), memory_index.time.time())
        memory_index._BROAD_CACHE.clear()  # «рестарт»: память процесса пуста, диск цел
        pool = memory_index._broad_candidates("owner", 5, "explicit")
        self.assertEqual(len(pool), 5)
        self.assertEqual(self.refreshes, [])

    def test_refresh_starts_before_the_pool_expires(self):
        """Обновляемся заранее, иначе на истечении срока она получит пусто ровно тогда,
        когда пошла вспоминать."""
        self._live_env(PRAXIS_BROAD_TTL="100")
        old = memory_index.time.time() - 85  # 85% срока
        memory_index._broad_to_disk(("owner", "explicit"), _docs(60), old)
        pool = memory_index._broad_candidates("owner", 40, "explicit")
        self.assertEqual(len(pool), 40, "годный пул всё равно обязан быть отдан")
        self.assertEqual(self.refreshes, [("owner", "explicit")])

    def test_stale_and_broken_disk_pool_are_ignored(self):
        self._live_env(PRAXIS_BROAD_TTL="60")
        for payload in ('{"key": ["owner", "explicit"], "at": 1.0, "pool": [{"id": "x"}]}',
                        "не json вовсе",
                        '{"key": ["public", "explicit"], "at": 9e18, "pool": [{"id": "x"}]}'):
            self.disk.write_text(payload, encoding="utf-8")
            memory_index._BROAD_CACHE.clear()
            self.refreshes.clear()
            with mock.patch.object(memory_index, "_all_chunks",
                                   mock.Mock(side_effect=AssertionError("канон считался в её ходе"))):
                pool = memory_index._broad_candidates("owner", 3, "explicit")
            self.assertEqual(pool, [], f"взят негодный пул: {payload[:40]}")
            self.assertEqual(self.refreshes, [("owner", "explicit")])

    def test_scopes_do_not_share_a_pool(self):
        """Owner и public — разные видимости; смешать их кэшем значит утечь приватным."""
        self._live_env()
        memory_index._broad_to_disk(("owner", "explicit"),
                                    [dict(d, text=f"owner {d['text']}") for d in _docs(3)],
                                    memory_index.time.time())
        owner = memory_index._broad_candidates("owner", 3, "explicit")
        public = memory_index._broad_candidates("public", 3, "explicit")
        self.assertTrue(all(d["text"].startswith("owner") for d in owner))
        self.assertEqual(public, [], "public обслужен из owner-пула")
        self.assertIn(("public", "explicit"), self.refreshes)

    def test_caller_mutation_does_not_poison_the_pool(self):
        self._live_env()
        memory_index._broad_to_disk(("owner", "explicit"), _docs(3), memory_index.time.time())
        first = memory_index._broad_candidates("owner", 3, "explicit")
        first[0]["text"] = "испорчено вызывающим"
        second = memory_index._broad_candidates("owner", 3, "explicit")
        self.assertNotEqual(second[0]["text"], "испорчено вызывающим")

    def test_tests_and_disabled_cache_compute_inline(self):
        """Под PRAXIS_TEST (и при TTL=0) поведение прежнее: считаем на месте, синхронно —
        иначе кэш подделал бы результаты соседних тестов."""
        with mock.patch.dict(os.environ, {"PRAXIS_TEST": "1"}):
            with mock.patch.object(memory_index, "_all_chunks", lambda scope, purpose: _docs(5)):
                pool = memory_index._broad_candidates("owner", 5, "explicit")
        self.assertEqual(len(pool), 5)
        self.assertEqual(self.refreshes, [], "под тестами фоновых процессов быть не должно")

    def test_compute_writes_the_pool_to_disk(self):
        self._live_env()
        with mock.patch.object(memory_index, "_all_chunks", lambda scope, purpose: _docs(10)):
            top = memory_index._broad_compute("owner", "explicit")
        self.assertEqual(len(top), 10)
        self.assertTrue(self.disk.exists(), "фоновый пересчёт обязан оставить пул на диске")


if __name__ == "__main__":
    unittest.main(verbosity=2)
