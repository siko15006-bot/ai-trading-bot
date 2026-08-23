import os, json, asyncio
from dotenv import load_dotenv
from telethon import TelegramClient
load_dotenv('/home/ubuntu/.env')

async def main():
    client = TelegramClient('/home/ubuntu/trading-bot/tg_session',
                            int(os.getenv('TG_API_ID')), os.getenv('TG_API_HASH'))
    await client.connect()
    if await client.is_user_authorized():
        print('ALREADY AUTHORIZED')
        return
    sent = await client.send_code_request(os.getenv('TG_PHONE'))
    with open('/tmp/tg_code_hash.json', 'w') as f:
        json.dump({'hash': sent.phone_code_hash}, f)
    print('CODE SENT — check Telegram')

asyncio.run(main())
