"""Mailroom-бот: headless runtime, restart signal and retired HTTP compatibility helpers."""
import asyncio
import hashlib
import hmac
import os
import sys
import tempfile
import inspect
import time
import types
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

# Заглушки до импорта mailroom_bot (он импортирует agent → load_dotenv не должен тащить .env). §4
_fa = types.ModuleType("anthropic")
_fa.Anthropic = lambda **kw: None
sys.modules.setdefault("anthropic", _fa)
_fd = types.ModuleType("dotenv")
_fd.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("dotenv", _fd)

import mailroom_bot as mb  # noqa: E402

TOKEN = "123456:TEST-BOT-TOKEN"
OWNER = 123456789


def make_init(token=TOKEN, user_id=OWNER, auth_date=None, tamper=False) -> str:
    user = f'{{"id":{user_id},"first_name":"Yegor"}}'
    params = {"auth_date": str(auth_date if auth_date is not None else int(time.time())),
              "query_id": "AAExample", "user": user}
    dcs = "\n".join(f"{k}={params[k]}" for k in sorted(params))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    if tamper:
        h = ("0" if h[0] != "0" else "1") + h[1:]
    return urlencode({**params, "hash": h})


class TestInitData(unittest.TestCase):
    def test_valid_returns_user(self):
        user = mb.check_init_data(make_init(), TOKEN)
        self.assertIsNotNone(user)
        self.assertEqual(user["id"], OWNER)

    def test_tampered_hash_rejected(self):
        self.assertIsNone(mb.check_init_data(make_init(tamper=True), TOKEN))

    def test_wrong_token_rejected(self):
        # подпись сделана правильным токеном, проверяем чужим → не сходится
        self.assertIsNone(mb.check_init_data(make_init(), "999:OTHER-TOKEN"))

    def test_missing_hash_rejected(self):
        self.assertIsNone(mb.check_init_data("auth_date=1&user=%7B%7D", TOKEN))

    def test_empty_rejected(self):
        self.assertIsNone(mb.check_init_data("", TOKEN))
        self.assertIsNone(mb.check_init_data(make_init(), ""))

    def test_stale_rejected_with_max_age(self):
        old = int(time.time()) - 10000
        self.assertIsNone(mb.check_init_data(make_init(auth_date=old), TOKEN, max_age=3600))
        # без max_age то же самое принимается (подпись-то верна)
        self.assertIsNotNone(mb.check_init_data(make_init(auth_date=old), TOKEN, max_age=0))

    def test_fresh_passes_max_age(self):
        self.assertIsNotNone(mb.check_init_data(make_init(), TOKEN, max_age=3600))


class TestRestartSignal(unittest.TestCase):
    """PASS 12.x: mailbot слушает файл-флаг от praxis (сопряжённый рестарт, не Docker-сокет)."""

    def test_flag_consumed_bot_notified_and_exits(self):
        tmp = Path(tempfile.mkdtemp()) / ".restart_mailbot"
        tmp.write_text("2026-07-06 03:40 -- тест (restart_mailbot)", encoding="utf-8")
        orig_flag, orig_exit = mb.agent.RESTART_MAILBOT_FLAG, mb._exit_process
        mb.agent.RESTART_MAILBOT_FLAG = tmp
        exited = {}
        mb._exit_process = lambda: exited.setdefault("hit", True)
        sent = {}

        class FakeBot:
            async def send_message(self, chat_id, text):
                sent["chat_id"], sent["text"] = chat_id, text

        try:
            did = asyncio.run(mb._check_restart_signal(FakeBot()))
        finally:
            mb.agent.RESTART_MAILBOT_FLAG, mb._exit_process = orig_flag, orig_exit

        self.assertTrue(did)
        self.assertTrue(exited.get("hit"), "должна была выйти")
        self.assertFalse(tmp.exists(), "флаг должен быть снесён")
        self.assertEqual(sent.get("chat_id"), mb.OWNER_ID)
        self.assertIn("тест", sent.get("text", ""))

    def test_no_flag_no_exit(self):
        tmp = Path(tempfile.mkdtemp()) / ".restart_mailbot"  # намеренно не создаём
        orig_flag, orig_exit = mb.agent.RESTART_MAILBOT_FLAG, mb._exit_process
        mb.agent.RESTART_MAILBOT_FLAG = tmp
        mb._exit_process = lambda: self.fail("не должна выходить без флага")
        try:
            did = asyncio.run(mb._check_restart_signal(object()))
        finally:
            mb.agent.RESTART_MAILBOT_FLAG, mb._exit_process = orig_flag, orig_exit
        self.assertFalse(did)


class TestHeadlessRuntime(unittest.TestCase):
    """Production entrypoint must not resurrect the retired Mini App/PWA surface."""

    def test_main_has_no_http_listener_and_clears_menu(self):
        source = inspect.getsource(mb.main)
        self.assertNotIn("AppRunner", source)
        self.assertNotIn("TCPSite", source)
        self.assertIn("MenuButtonDefault", source)

    def test_reply_keyboard_has_no_webapp_button(self):
        keyboard = mb._reply_kb()
        buttons = [button for row in keyboard.keyboard for button in row]
        self.assertTrue(buttons)
        self.assertTrue(all(button.web_app is None for button in buttons))

    def test_mail_notification_has_no_webapp_keyboard(self):
        source = inspect.getsource(mb._poll)
        self.assertNotIn("_open_kb", source)
        self.assertNotIn("web_app", source)


HOME_TOKEN = "987654:HOME-BOT-TOKEN"


class TestMultiTokenAuth(unittest.TestCase):
    """A: initData, подписанная ЛЮБЫМ доверенным ботом, принимается; owner-гейт не ослаблен."""

    def test_multi_accepts_either_bot_signature(self):
        init_home = make_init(token=HOME_TOKEN)
        self.assertIsNone(mb.check_init_data(init_home, TOKEN))            # одиночная mail-проверка отвергает
        user = mb.check_init_data_multi(init_home, [TOKEN, HOME_TOKEN])    # мульти — принимает
        self.assertIsNotNone(user)
        self.assertEqual(user["id"], OWNER)
        self.assertIsNotNone(mb.check_init_data_multi(make_init(token=TOKEN), [TOKEN, HOME_TOKEN]))

    def test_multi_rejects_forged_across_all_tokens(self):
        self.assertIsNone(mb.check_init_data_multi(make_init(tamper=True), [TOKEN, HOME_TOKEN]))
        self.assertIsNone(mb.check_init_data_multi(make_init(token="000:UNKNOWN"), [TOKEN, HOME_TOKEN]))

    def test_multi_honours_max_age(self):
        old = int(time.time()) - 10000
        self.assertIsNone(mb.check_init_data_multi(
            make_init(token=HOME_TOKEN, auth_date=old), [TOKEN, HOME_TOKEN], max_age=3600))

    def test_app_bot_tokens_lists_mail_first_dedup(self):
        with mock.patch.object(mb, "TOKEN", TOKEN), \
             mock.patch.dict(os.environ, {"PRAXIS_APP_BOT_TOKENS": f" {HOME_TOKEN}, {TOKEN} "}):
            toks = mb._app_bot_tokens()
        self.assertEqual(toks[0], TOKEN)     # mail-бот всегда первый
        self.assertIn(HOME_TOKEN, toks)
        self.assertEqual(len(toks), 2)       # дубликат mail-токена схлопнут

    def test_non_owner_signature_valid_but_not_owner(self):
        # чужой пользователь второго бота: подпись верна, но owner-гейт (id==OWNER) отсечёт
        user = mb.check_init_data_multi(make_init(token=HOME_TOKEN, user_id=999), [TOKEN, HOME_TOKEN])
        self.assertEqual(user["id"], 999)
        self.assertNotEqual(user["id"], OWNER)


class TestPraxisAppShellLoadsSDK(unittest.TestCase):
    """B (корневая причина): мини-апп ОБЯЗАН грузить Telegram SDK — иначе initData есть только
    во фрагменте URL, который срезался на первом рендере → «нужна авторизация» на любом reload."""

    def test_praxisapp_html_loads_telegram_sdk(self):
        html = (Path(mb.__file__).resolve().parent / "praxisapp.html").read_text(encoding="utf-8")
        self.assertIn("telegram.org/js/telegram-web-app.js", html)

    def test_praxis_app_theme_is_dark_only(self):
        js = (Path(mb.__file__).resolve().parent / "praxis_static" / "app.js").read_text(
            encoding="utf-8",
        )
        apply_theme = js.split("function applyTheme()", 1)[1].split(
            "function attachSessionAuth", 1,
        )[0]
        self.assertIn('document.documentElement.dataset.theme = "dark";', apply_theme)
        self.assertIn('ambient.setState({ theme: "dark" });', apply_theme)
        self.assertNotIn("colorScheme", apply_theme)
        self.assertNotIn("prefers-color-scheme", apply_theme)


if __name__ == "__main__":
    unittest.main(verbosity=2)
