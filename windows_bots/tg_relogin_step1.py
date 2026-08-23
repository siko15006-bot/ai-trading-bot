import asyncio
from telethon import TelegramClient

API_ID = <REDACTED_TG_API_ID>
API_HASH = "<REDACTED_TG_API_HASH>"
SESSION = r"C:\TradingBot\Bot_Active\fx_session_new"
PHONE = "<REDACTED_PHONE>"
PROXY = {"proxy_type": "socks5", "addr": "127.0.0.1", "port": 1080}


async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH, proxy=PROXY)
    await client.connect()
    sent = await client.send_code_request(PHONE)
    print(f"CODE_SENT phone_code_hash={sent.phone_code_hash} type={sent.type} next_type={sent.next_type} timeout={sent.timeout}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
