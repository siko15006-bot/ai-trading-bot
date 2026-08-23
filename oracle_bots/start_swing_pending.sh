#!/bin/bash
pgrep -f 'venv/bin/python3 -u swing_pending_bybit.py' > /dev/null && exit 0
cd /home/ubuntu/trading-bot
nohup venv/bin/python3 -u swing_pending_bybit.py >> /tmp/swing_pending_bybit_out.log 2>&1 &
disown $!
