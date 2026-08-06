"""Транспортный след не притворяется её памятью.

02.08.2026, решение Праксис по замеру: из 379 977 кусков индекса 154 488 (40.7%) и
64.9% всего текста — это `memory/runs/**/context.md`, техническая карточка хода, которую
собирает система. Её слова: «транспортный след притворяется моей памятью».

Тонкость, которую пришлось найти по дороге: автоматический recall его и так не видел.
Вред жил на ЯВНОМ пути — в `context.md` лежит дословный `origin_text` прошлых реплик,
поэтому на обычном «вспомни» FTS вытаскивал его по точным словам, и её собственный
журнал конкурировал с транспортом сто к одному.

Поэтому граница проходит по ЦЕЛИ обращения, а не по составу индекса:
  automatic — не видит (как и раньше);
  explicit  — не видит (новое);
  audit     — видит всё (прежний контракт «explicitly searchable for audit» цел).
Файлы и их куски на месте: ничего не удалено и не разындексировано.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import memory_fts


class TransportIsReachableOnlyOnPurposeAudit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="praxis_transport_")
        self.base = Path(self.tmp.name)
        self.mem = self.base / "memory"
        self.skills = self.base / "soul" / "skills"
        self.skills.mkdir(parents=True)
        run = self.mem / "runs" / "2026-08" / "run-20260802T000000Z-abcdef"
        run.mkdir(parents=True)
        (run / "context.md").write_text(
            "# Immutable run context\n\n## Authority and address\n"
            'origin_text: единорогмаркер про переезд\n', encoding="utf-8")
        (run / "RECAP.md").write_text(
            "# Итог хода\n\nвыводмаркер: путала подготовленное с доставленным\n",
            encoding="utf-8")
        # `summary` — индексируемое поле (`_TEXT_FIELDS`); `tool` таковым не является.
        (run / "events.jsonl").write_text(
            json.dumps({"kind": "tool_started", "tool": "telegram.deliver",
                        "summary": "телегадоставкамаркер"},
                       ensure_ascii=False) + "\n", encoding="utf-8")
        (self.mem / "journal").mkdir(parents=True)
        (self.mem / "journal" / "2026-08-02.md").write_text(
            "# 2026-08-02\n\n- 10:00 (s2) журналмаркер про переезд\n", encoding="utf-8")
        memory_fts.rebuild(base=self.base, memory_dir=self.mem, skills_dir=self.skills)
        self.addCleanup(self.tmp.cleanup)

    def _paths(self, query, purpose="explicit"):
        return [r["path"] for r in memory_fts.search(
            query, base=self.base, memory_dir=self.mem, skills_dir=self.skills,
            scope="owner", purpose=purpose, limit=30)]

    def test_ordinary_recall_does_not_return_the_envelope(self):
        self.assertEqual(self._paths("единорогмаркер"), [])

    def test_audit_still_reaches_the_envelope(self):
        paths = self._paths("единорогмаркер", purpose="audit")
        self.assertTrue(any(p.endswith("/context.md") for p in paths), paths)

    def test_recap_and_events_stay_in_ordinary_recall(self):
        self.assertTrue(any(p.endswith("/RECAP.md") for p in self._paths("выводмаркер")))
        self.assertTrue(any(p.endswith("/events.jsonl")
                            for p in self._paths("телегадоставкамаркер")))

    def test_her_own_journal_is_not_crowded_out(self):
        """Один и тот же вопрос: раньше конверт конкурировал с её записью."""
        paths = self._paths("переезд")
        self.assertTrue(any("journal" in p for p in paths), paths)
        self.assertFalse(any(p.endswith("/context.md") for p in paths), paths)

    def test_nothing_left_the_index(self):
        """Ничего не удалено и не разындексировано — иначе аудит был бы пустым словом."""
        self.assertTrue(self._paths("единорогмаркер", purpose="audit"))

    def test_automatic_is_unchanged(self):
        self.assertEqual(self._paths("единорогмаркер", purpose="automatic"), [])

    def test_predicate_names_both_conditions(self):
        self.assertTrue(memory_fts._is_transport_snapshot(
            "memory/runs/2026-08/run-x/context.md"))
        self.assertFalse(memory_fts._is_transport_snapshot(
            "memory/runs/2026-08/run-x/RECAP.md"))
        self.assertFalse(memory_fts._is_transport_snapshot("memory/projects/context.md"))


if __name__ == "__main__":
    unittest.main()
