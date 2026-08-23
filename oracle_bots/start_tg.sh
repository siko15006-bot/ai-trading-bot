#!/bin/bash
pgrep -f 'venv/bin/python3 -u tg_signal_bot.py' > /dev/null && exit 0
cd /home/ubuntu/trading-bot
nohup venv/bin/python3 -u tg_signal_bot.py >> /tmp/tg_signal.log 2>&1 &
disown $!
