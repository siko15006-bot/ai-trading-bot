#!/bin/bash
cd /home/ubuntu/trading-bot
if ! pgrep -f "oracle_status_server.py" > /dev/null; then
    nohup venv/bin/python3 -u oracle_status_server.py >> /tmp/oracle_status_server.log 2>&1 &
fi
