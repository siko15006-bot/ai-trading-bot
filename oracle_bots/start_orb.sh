#!/bin/bash
pgrep -f 'venv/bin/python3 -u orb_bot.py' > /dev/null && exit 0
cd /home/ubuntu/trading-bot
nohup venv/bin/python3 -u orb_bot.py >> /tmp/orb_out.log 2>&1 &
disown $!
