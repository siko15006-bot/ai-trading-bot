#!/bin/bash
TG_TOKEN="8002641228:AAHAqcHwuI4h0MYNuH6MkY8iqDDSE4Vg03A"
TG_CHAT="682191881"
for BOT in orb_bot tg_signal_bot trama_trend_bot; do
  pgrep -f "${BOT}" | grep -v $$ >/dev/null || curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" -d "chat_id=${TG_CHAT}" -d "text=ALERT: ${BOT}.py is DOWN on Oracle and its watchdog cron failed to recover it." >/dev/null
done

# Claude monitoring heartbeat (added 2026-07-16): the local Claude session
# touches /tmp/claude_heartbeat every monitoring cycle (~15 min) over SSH.
# Stale >40 min = the discretionary monitoring layer is down -> tell Ahmed.
HB=/tmp/claude_heartbeat
if [ ! -f "$HB" ] || [ $(( $(date +%s) - $(stat -c %Y "$HB") )) -gt 2400 ]; then
  curl -s -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" -d "chat_id=${TG_CHAT}" -d "text=ALERT: Claude monitoring heartbeat is STALE (>40 min). The trading bots keep running, but the discretionary monitoring layer is down - reopen the Claude session." >/dev/null
fi
