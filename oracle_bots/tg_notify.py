"""
tg_notify.py -- minimal relay: send one Telegram message using the same
bot token/chat already hardcoded in liquidity_sweep_bot.py/tg_signal_bot.py/
live_decay_watch.py (proven live, bot=Siko_Trading_Bot). No new credential.

2026-08-08 (Ahmed, Telegram End-to-End reopen): Windows-side alert_manager.py
has no Telegram credential of its own and none should be copied there --
instead it SSHes the alert text to Oracle (same channel already used for
RISK_HALT sync) and this script does the actual send, since the token
already lives here.

Usage: venv/bin/python3 tg_notify.py "message text"  (reads stdin if no arg)
Prints "OK" on success, "FAIL <reason>" on failure. Never raises.
"""
import sys
import urllib.parse
import urllib.request

TG_TOKEN = '8002641228:AAHAqcHwuI4h0MYNuH6MkY8iqDDSE4Vg03A'
TG_CHAT = '682191881'


def main():
    text = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    text = text[:4000]
    try:
        url = f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage'
        data = urllib.parse.urlencode({'chat_id': TG_CHAT, 'text': text}).encode()
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10) as r:
            if r.status == 200:
                print('OK')
            else:
                print(f'FAIL http_status={r.status}')
    except Exception as e:
        print(f'FAIL {e}')


if __name__ == '__main__':
    main()
