# -*- coding: utf-8 -*-
"""Счётчик заказанных отчётов не должен зависеть от поля, которого нет в карточке."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import praxis_app


class FollowupCardCarriesCountingKeys(unittest.TestCase):
    """Дефект: _telegram() отбирал строки по row["notify_owner"], перебирая список
    карточек _followup_card, а карточка этого ключа не отдавала. Условие было ложным
    ВСЕГДА — не «нет заказанных отчётов», а «нечем проверить». Проверяем не число,
    а наличие признака ровно там, где по нему считают."""

    def test_card_carries_notify_owner_and_notified_at(self):
        card = praxis_app.PraxisAppService._followup_card({
            "id": "tgfu_1", "status": "answered", "target_label": "кто-то",
            "notify_owner": True, "notified_at": None,
            "response": {"received_at": "2026-08-03T00:00:00Z", "text": "ага"},
        })
        self.assertIn("notify_owner", card, "признак заказа не доезжает до счёта")
        self.assertTrue(card["notify_owner"])
        self.assertIn("notified_at", card, "отметка о доставке не доезжает до фильтра")

    def test_trace_without_order_is_not_counted_but_order_is(self):
        root = Path(tempfile.mkdtemp())
        (root / "memory" / "maps").mkdir(parents=True)
        service = praxis_app.PraxisAppService(root, owner_id=1)
        rows = [
            {"id": "a", "status": "pending", "notify_owner": True},   # заказан
            {"id": "b", "status": "pending", "notify_owner": False},  # её след
            {"id": "c", "status": "answered", "notify_owner": True},  # заказан
            {"id": "d", "status": "expired", "notify_owner": True},   # закрыт
        ]
        with mock.patch.object(service.followups, "list", return_value=rows), \
                mock.patch.object(service.membership, "pending", return_value=[]):
            payload = service._telegram()
        self.assertEqual(payload["pending_followups"], 2,
                         "при живых заказанных отчётах счётчик обязан быть ненулевым")


class ClientRepeatsTheServerRule(unittest.TestCase):
    """Два пересказа одного факта расходятся всегда. Клиентский бейдж обязан считать
    по тем же двум признакам, что и сервер, иначе он снова покажет её собственный
    след как ожидание Егора."""

    def test_pending_followups_filters_by_notify_owner(self):
        js = (Path(praxis_app.__file__).resolve().parent
              / "praxis_static" / "app.js").read_text(encoding="utf-8")
        body = js.split("function pendingFollowups(telegram) {", 1)[1].split("}", 1)[0]
        self.assertIn("row.notify_owner", body,
                      "клиент считает нити, которых сервер намеренно не считает")


if __name__ == "__main__":
    unittest.main(verbosity=2)
