#!/bin/bash
pgrep -f 'venv/bin/python3 -u trama_trend_bot.py' > /dev/null && exit 0
cd /home/ubuntu/trading-bot
nohup venv/bin/python3 -u trama_trend_bot.py >> /tmp/trama_out.log 2>&1 &
disown $!
