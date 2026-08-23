import asyncio, os
from dotenv import load_dotenv
from telethon import TelegramClient
load_dotenv('/home/ubuntu/.env')
load_dotenv('/home/ubuntu/trading-bot/.env')

async def main():
    client = TelegramClient('/home/ubuntu/trading-bot/tg_session',
                            int(os.getenv('TG_API_ID')), os.getenv('TG_API_HASH'))
    await client.connect()
    if not await client.is_user_authorized():
        print('NOT AUTHORIZED'); return
    async for d in client.iter_dialogs():
        if (d.is_group or d.is_channel) and 'gold' in (d.name or '').lower():
            print(d.id, '|', d.name)
    await client.disconnect()

asyncio.run(main())
