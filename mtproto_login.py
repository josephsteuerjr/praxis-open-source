"""Одноразовый логин Praxis в Telegram (MTProto), двушаговый, без интерактивного ввода.

  python mtproto_login.py send         # отправит код на аккаунт
  python mtproto_login.py code 12345   # введёт код (и 2FA из TELEGRAM_2FA при необходимости)
"""
import asyncio, os, sys
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from dotenv import load_dotenv

load_dotenv()
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
PHONE = os.getenv("TELEGRAM_PHONE", "")
TWO_FA = os.getenv("TELEGRAM_2FA", "")
SESSION = os.getenv("TELEGRAM_SESSION", "praxis")


async def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "send"
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        print("Уже залогинен как @%s (id %s)" % (me.username, me.id))
        await client.disconnect(); return
    if cmd == "send":
        sent = await client.send_code_request(PHONE)
        open(SESSION + ".codehash", "w").write(sent.phone_code_hash)
        print("Код отправлен на", PHONE, "— прочитай его в Telegram и запусти: code <код>")
    elif cmd == "code":
        code = sys.argv[2]
        phash = open(SESSION + ".codehash").read().strip()
        try:
            await client.sign_in(PHONE, code, phone_code_hash=phash)
        except SessionPasswordNeededError:
            await client.sign_in(password=TWO_FA)
        me = await client.get_me()
        print("Готово. Залогинен как @%s (id %s)" % (me.username, me.id))
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
