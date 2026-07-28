"""Групповой инвариант: Telegram mention/reply или отдельное имя будят listener.

Запуск:  python praxis_test.py test_group_modes -v
"""
from __future__ import annotations

import sys
import types
import unittest
import importlib.util
from pathlib import Path
from unittest.mock import patch

_fa = types.ModuleType("anthropic")
_fa.Anthropic = lambda **kw: None
sys.modules.setdefault("anthropic", _fa)
_fd = types.ModuleType("dotenv")
_fd.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _fd)
# telethon/aiogram могут отсутствовать в дев-окружении — раннер импортируем со стабами
import llm  # noqa: E402
import panel  # noqa: E402


def _load_mtproto_runner():
    """Import the runner without leaking an optional Telethon stub globally."""

    if "mtproto_runner" in sys.modules:
        return sys.modules["mtproto_runner"]
    if importlib.util.find_spec("telethon") is not None:
        # The real package may be installed while test credentials deliberately
        # are not.  Patch only the constructor; keep all real TL types/imports.
        import telethon
        original = telethon.TelegramClient
        telethon.TelegramClient = lambda *a, **k: types.SimpleNamespace(
            on=lambda *aa, **kk: (lambda fn: fn),
        )
        try:
            import mtproto_runner
            return mtproto_runner
        finally:
            telethon.TelegramClient = original

    # Some minimal dev environments intentionally omit Telethon. The wake
    # tests need only the decorator surface, so keep that stub scoped to the
    # import and restore sys.modules before other test modules are collected.
    prior = sys.modules.get("telethon")
    fake = types.ModuleType("telethon")
    fake.TelegramClient = lambda *a, **k: types.SimpleNamespace(
        on=lambda *aa, **kk: (lambda fn: fn),
    )
    fake.events = types.SimpleNamespace(
        NewMessage=lambda **kw: None,
        ChatAction=object,
    )
    sys.modules["telethon"] = fake
    try:
        import mtproto_runner
        return mtproto_runner
    finally:
        if prior is None:
            sys.modules.pop("telethon", None)
        else:
            sys.modules["telethon"] = prior


class TestLimitsNormalize(unittest.TestCase):
    def test_legacy_presence_fields_are_dropped(self):
        cfg = llm._normalize({})
        cfg = llm._normalize({"limits": {"group_mode": "presence", "presence_cooldown_sec": 60}})
        self.assertNotIn("group_mode", cfg["limits"])
        self.assertNotIn("presence_cooldown_sec", cfg["limits"])

    def test_limits_reader_has_no_legacy_switch(self):
        lim = llm.Limits()
        self.assertFalse(hasattr(lim, "group_mode"))
        self.assertFalse(hasattr(lim, "presence_cooldown_sec"))


class TestPanelValidation(unittest.TestCase):
    def test_old_panel_or_direct_post_cannot_restore_presence(self):
        clean, errs = panel._llm_validate({"limits": {"group_mode": "presence"}})
        self.assertTrue(errs)
        self.assertNotIn("limits", clean)
        clean, errs = panel._llm_validate({"limits": {"presence_cooldown_sec": 600}})
        self.assertTrue(errs)
        self.assertNotIn("limits", clean)

    def test_panel_has_no_legacy_controls(self):
        html = Path(panel.__file__).with_name("panelapp.html").read_text(encoding="utf-8")
        self.assertNotIn("data-bgm", html)
        self.assertNotIn("bPresCd", html)
        self.assertNotIn("presence_cooldown_sec", html)


class TestWake(unittest.TestCase):
    """_should_wake/_cooldown не имеют конфигурируемого пути к фоновому wake."""

    def setUp(self):
        self.mr = _load_mtproto_runner()

    def test_name_addressing_wakes_and_short_ack_is_not_a_code_veto(self):
        self.assertFalse(self.mr._should_wake(False, False, "reply"))
        self.assertTrue(self.mr._should_wake(False, True, "reply"))
        self.assertTrue(self.mr._should_wake(True, False, "reply"))
        self.assertTrue(self.mr._should_wake(False, True, "ignore"))

    def test_even_legacy_runtime_object_cannot_broaden_wake_or_cooldown(self):
        stale = types.SimpleNamespace(group_mode="presence", presence_cooldown_sec=1200)
        with patch.object(self.mr.llm, "limits", return_value=stale):
            self.assertFalse(self.mr._should_wake(False, False, "reply"))
            self.assertEqual(self.mr._cooldown(False), self.mr.COOLDOWN_GROUP)

    def test_addressed_includes_separate_name(self):
        """Listener treats a bounded name as an address, without making ambient mode broad."""
        import inspect
        src = inspect.getsource(self.mr.on_new)
        self.assertIn("named = agent._named(text)", src)
        self.assertIn("addressed = mentioned or replied or named", src)


if __name__ == "__main__":
    unittest.main()
