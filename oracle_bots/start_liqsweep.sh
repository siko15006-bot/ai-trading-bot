#!/bin/bash
pgrep -f 'venv/bin/python3 -u liquidity_sweep_bot.py' > /dev/null && exit 0
cd /home/ubuntu/trading-bot
nohup venv/bin/python3 -u liquidity_sweep_bot.py >> /tmp/liqsweep_out.log 2>&1 &
disown $!
