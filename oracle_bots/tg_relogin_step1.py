import asyncio, os
from dotenv import load_dotenv
load_dotenv(os.path.expanduser('~/.env'))
from telethon import TelegramClient

API_ID = int(os.getenv('TG_API_ID'))
API_HASH = os.getenv('TG_API_HASH')
SESSION = '/home/ubuntu/trading-bot/tg_session'
PHONE = '<REDACTED_PHONE>'

async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    sent = await client.send_code_request(PHONE)
    print(f'CODE_SENT phone_code_hash={sent.phone_code_hash} type={sent.type}')
    await client.disconnect()

asyncio.run(main())
