# -*- coding: utf-8 -*-
"""Строки computer.evidence обязаны называться тем, что в них лежит."""
from __future__ import annotations

import unittest
from pathlib import Path

import praxis_app


class EvidenceKeysReachTheCard(unittest.TestCase):
    """Дефект: _computer_evidence отдаёт id/at/task_id/capability/status/subject/summary,
    а карточка искала name/filename/artifact_id/size/sha256. Пересечение — пустое, и
    все строки раздела получали одинаковый литерал «Артефакт» с подписью «— · 2д».
    Тест проверяет договор между двумя сторонами, а не совпадение слов на экране."""

    def setUp(self):
        self.here = Path(praxis_app.__file__).resolve().parent
        self.js = (self.here / "praxis_static" / "app.js").read_text(encoding="utf-8")
        self.card = self.js.split("function artifactCard(artifact) {", 1)[1].split(
            "function artifactPreview", 1)[0]

    def test_server_evidence_keys_are_stable(self):
        source = (self.here / "praxis_app.py").read_text(encoding="utf-8")
        body = source.split("def _computer_evidence", 1)[1].split("def _runs_snapshot", 1)[0]
        for key in ("capability", "subject", "summary", "status"):
            self.assertIn(f'"{key}": str(row.get("{key}")', body,
                          "форма evidence изменилась — карточку надо сверить заново")

    def test_card_falls_back_to_evidence_identity(self):
        for key in ("artifact.subject", "artifact.capability", "artifact.summary"):
            self.assertIn(key, self.card,
                          "строка evidence снова называется словом «Артефакт»")

    def test_missing_size_is_not_printed_as_a_dash(self):
        self.assertIn("artifact.size !== undefined ? bytes(artifact.size)", self.card,
                      "bytes(undefined) возвращает «—» и проходит filter(Boolean)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
