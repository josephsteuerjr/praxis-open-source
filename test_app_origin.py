"""Публичные адреса контура не хранятся примерами в исходнике.

⚠ 03.08.2026. Кнопка мини-аппа в Telegram вела на `praxis.203.0.113.10.nip.io/px` —
адрес из диапазона RFC 5737, отведённого под ДОКУМЕНТАЦИЮ. Переменные окружения заданы
не были, поэтому побеждали захардкоженные заглушки, и бот на старте сам ставил по ним
кнопку. Приложение не открывалось вовсе, при этом ошибки не было нигде: и бот, и сервер
отвечали 200 — кнопка просто вела в никуда.
"""
from __future__ import annotations

import importlib
import os
import unittest
from unittest import mock

import mailroom_bot as mb

#: Диапазоны, отведённые RFC 5737/3849 под документацию: в бою их не существует.
DOC_RANGES = ("203.0.113.", "192.0.2.", "198.51.100.", "2001:db8:")


class PublicOriginsAreDerivedNotExamples(unittest.TestCase):
    def test_no_documentation_address_survives_as_a_default(self):
        for name in ("WEBAPP_URL", "PRAXIS_APP_URL", "PANEL_URL"):
            value = str(getattr(mb, name, "") or "")
            for doc in DOC_RANGES:
                self.assertNotIn(doc, value,
                                 f"{name} указывает в документационный диапазон: {value}")

    def test_origin_follows_the_server_host(self):
        with mock.patch.object(mb, "_SERVER_HOST", "198.51.100.7"):
            self.assertEqual(mb._nip_origin("praxis", "/px"),
                             "https://praxis.198.51.100.7.nip.io/px")

    def test_unknown_host_yields_emptiness_not_a_plausible_address(self):
        """Правдоподобный, но мёртвый адрес хуже пустоты: его никто не заметит."""
        with mock.patch.object(mb, "_SERVER_HOST", ""):
            self.assertEqual(mb._nip_origin("praxis", "/px"), "")

    def test_the_module_derives_from_environment_on_import(self):
        with mock.patch.dict(os.environ, {"SERVER_HOST": "198.51.100.9"}, clear=False):
            for name in ("PRAXIS_APP_URL", "PRAXIS_MAILAPP_URL", "PRAXIS_PANEL_URL"):
                os.environ.pop(name, None)
            fresh = importlib.reload(mb)
            try:
                self.assertEqual(fresh.PRAXIS_APP_URL,
                                 "https://praxis.198.51.100.9.nip.io/px")
                self.assertTrue(fresh.WEBAPP_URL.startswith("https://mail.198.51.100.9."))
            finally:
                importlib.reload(mb)


if __name__ == "__main__":
    unittest.main()
