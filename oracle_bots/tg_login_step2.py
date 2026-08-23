import os, sys, json, asyncio
from dotenv import load_dotenv
from telethon import TelegramClient
load_dotenv('/home/ubuntu/.env')

async def main():
    code = sys.argv[1]
    with open('/tmp/tg_code_hash.json') as f:
        h = json.load(f)['hash']
    client = TelegramClient('/home/ubuntu/trading-bot/tg_session',
                            int(os.getenv('TG_API_ID')), os.getenv('TG_API_HASH'))
    await client.connect()
    await client.sign_in(os.getenv('TG_PHONE'), code, phone_code_hash=h)
    me = await client.get_me()
    print('LOGGED IN AS:', me.first_name, me.username or '', me.phone)

asyncio.run(main())
