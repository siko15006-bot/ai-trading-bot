#!/bin/bash
pgrep -f 'venv/bin/python3 -u crypto_watch.py' > /dev/null && exit 0
cd /home/ubuntu/trading-bot
nohup venv/bin/python3 -u crypto_watch.py >> /tmp/crypto_watch.log 2>&1 &
disown $!
