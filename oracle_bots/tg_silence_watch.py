"""
tg_silence_watch.py — catches tg_signal_bot going silent on its live-trading
groups while the process itself stays alive (2026-07-15: bot restarted at
14:42 UTC, kept running, but 6 live groups produced zero log lines for 3.5h
straight during a huge gold rally — a real bug, not a quiet market, and it
took Ahmed noticing to catch it). Checks every run for the most recent log
line mentioning ANY of the six live groups; alerts if that's older than
LIVE_GROUP_MAX_SILENCE_HOURS.
"""
import re
import time
from datetime import datetime, timezone, timedelta

LOG_FILE = "/tmp/tg_signal.log"
LIVE_GROUPS = ["SPARTA_CRYPTO", "BINANCE_360", "XAUUSD_ANALYSIS", "NEXA_TRADE", "GOLD98_SURE", "FOREX_GOLD_SIGNALS"]
MAX_SILENCE_HOURS = 3

line_re = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\]")


def last_activity_time(lines):
    latest = None
    for line in lines:
        if not any(g in line for g in LIVE_GROUPS):
            continue
        m = line_re.match(line)
        if not m:
            continue
        h, mi, s = map(int, m.groups())
        now = datetime.now(timezone.utc)
        t = now.replace(hour=h, minute=mi, second=s, microsecond=0)
        if t > now:
            t -= timedelta(days=1)
        if latest is None or t > latest:
            latest = t
    return latest


def main():
    with open(LOG_FILE, encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()[-500:]
    latest = last_activity_time(lines)
    now = datetime.now(timezone.utc)
    if latest is None:
        print(f"[{now:%H:%M:%S}] SILENCE-WATCH: no live-group activity found in recent log at all")
        return
    gap_hours = (now - latest).total_seconds() / 3600
    if gap_hours > MAX_SILENCE_HOURS:
        print(f"[{now:%H:%M:%S}] SILENCE-WATCH ALERT: no activity from {LIVE_GROUPS} in {gap_hours:.1f}h "
              f"(last: {latest:%H:%M:%S} UTC) — bot may be alive but deaf to these chats, check tg_signal_bot")


if __name__ == "__main__":
    main()
