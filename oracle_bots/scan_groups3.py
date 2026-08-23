"""
scan_groups2.py — scan every group/channel on Ahmed's account for parseable
trading signals (crypto AND gold/forex), so we can pick new sources to track.
Run ONLY while tg_signal_bot is stopped (shared Telethon session).
"""
import asyncio
import os
import re
import sys

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv('/home/ubuntu/.env')
load_dotenv('/home/ubuntu/trading-bot/.env')

sys.path.insert(0, '/home/ubuntu/trading-bot')
from tg_signal_bot import parse_signal, parse_fx_gold_signal, GROUPS, NOTIFY_ONLY_GROUPS, RECORD_ONLY_GROUPS

KNOWN = set(GROUPS) | set(NOTIFY_ONLY_GROUPS) | set(RECORD_ONLY_GROUPS)
SAMPLE = 50


async def main():
    client = TelegramClient('/home/ubuntu/trading-bot/tg_session',
                            int(os.getenv('TG_API_ID')), os.getenv('TG_API_HASH'))
    await client.connect()
    if not await client.is_user_authorized():
        print('NOT AUTHORIZED')
        return
    results = []
    async for d in client.iter_dialogs():
        if not (d.is_group or d.is_channel) or d.id in KNOWN:
            continue
        crypto = fxgold = total = 0
        try:
            async for msg in client.iter_messages(d.id, limit=SAMPLE):
                t = msg.raw_text or ''
                if not t:
                    continue
                total += 1
                if parse_signal(t):
                    crypto += 1
                elif parse_fx_gold_signal(t):
                    fxgold += 1
        except Exception as e:
            print(f'skip {d.name}: {e}')
            continue
        if crypto or fxgold:
            results.append((crypto + fxgold, d.id, d.name, crypto, fxgold, total))
    results.sort(reverse=True)
    print(f'\n{"hits":>4} {"id":>15}  {"crypto":>6} {"fx/gold":>7} {"msgs":>4}  name')
    for hits, gid, name, c, fx, total in results:
        print(f'{hits:>4} {gid:>15}  {c:>6} {fx:>7} {total:>4}  {name[:60]}')
    if not results:
        print('no new signal-posting groups found')


asyncio.run(main())
