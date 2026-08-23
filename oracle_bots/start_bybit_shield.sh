#!/bin/bash
cd /home/ubuntu/trading-bot
if ! pgrep -f "bybit_shield.py" > /dev/null; then
    nohup venv/bin/python3 -u bybit_shield.py >> /tmp/bybit_shield.log 2>&1 &
fi
