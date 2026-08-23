import asyncio
import sys
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

API_ID = <REDACTED_TG_API_ID>
API_HASH = "<REDACTED_TG_API_HASH>"
SESSION = r"C:\TradingBot\Bot_Active\fx_session_new"
PHONE = "<REDACTED_PHONE>"
PROXY = {"proxy_type": "socks5", "addr": "127.0.0.1", "port": 1080}
CODE = sys.argv[1]
PHONE_CODE_HASH = sys.argv[2]


async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH, proxy=PROXY)
    await client.connect()
    try:
        await client.sign_in(phone=PHONE, code=CODE, phone_code_hash=PHONE_CODE_HASH)
    except SessionPasswordNeededError:
        print("NEEDS_2FA_PASSWORD")
        await client.disconnect()
        return
    me = await client.get_me()
    print(f"SIGNED_IN as {me.first_name} (id={me.id})")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
