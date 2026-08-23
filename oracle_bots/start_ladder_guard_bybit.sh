#!/bin/bash
cd /home/ubuntu/trading-bot
if ! pgrep -f "ladder_guard_bybit.py" > /dev/null; then
    nohup venv/bin/python3 -u ladder_guard_bybit.py >> /tmp/ladder_guard_bybit.log 2>&1 &
fi
