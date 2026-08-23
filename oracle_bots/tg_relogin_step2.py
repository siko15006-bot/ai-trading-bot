import asyncio, os, sys
from dotenv import load_dotenv
load_dotenv(os.path.expanduser('~/.env'))
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

API_ID = int(os.getenv('TG_API_ID'))
API_HASH = os.getenv('TG_API_HASH')
SESSION = '/home/ubuntu/trading-bot/tg_session'
PHONE = '<REDACTED_PHONE>'
CODE = sys.argv[1]
HASH = sys.argv[2]

async def main():
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()
    try:
        await client.sign_in(phone=PHONE, code=CODE, phone_code_hash=HASH)
    except SessionPasswordNeededError:
        print('NEEDS_2FA_PASSWORD')
        await client.disconnect()
        return
    me = await client.get_me()
    print(f'SIGNED_IN as {me.first_name} (id={me.id})')
    await client.disconnect()

asyncio.run(main())
