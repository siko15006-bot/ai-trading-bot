#!/bin/bash
pgrep -f 'scalp_bot.py' > /dev/null && exit 0
cd /home/ubuntu/trading-bot
nohup venv/bin/python3 -u scalp_bot.py >> /tmp/scalp_out.log 2>&1 &
disown $!
