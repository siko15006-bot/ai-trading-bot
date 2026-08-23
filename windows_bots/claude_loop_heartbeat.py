"""
claude_loop_heartbeat.py -- OS-level pulse for the dashboard's "Claude
Monitoring Loop" panel (status_dashboard.py's claude_loop_status()).
Proves the scheduling/monitoring infrastructure itself is alive, independent
of whether Claude's own analysis cycle is actually running -- Ahmed
2026-07-27: "Heartbeat alive != Claude analysis alive != Analysis
completed". Only ever writes heartbeat_ts/next_tick_ts -- never touches
claude_awake_ts/analysis_completed_ts/last_cycle_status, which belong to
claude_loop_report.py (run by Claude itself at the start/end of a woken
cycle). Read-modify-write so the two scripts never clobber each other's keys.

Usage: python claude_loop_heartbeat.py
"""
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

STATUS_FILE = os.path.join(os.path.dirname(__file__), "claude_loop_status.json")
NOTIFY_SECONDS = 900  # must match status_dashboard.py's CLAUDE_LOOP_NOTIFY_SECONDS
TICK_SECONDS = 60

# 2026-08-03: healthchecks.io dead-man's-switch pilot -- lowest-stakes bot in
# the whole system (pure OS pulse, zero trading logic) chosen deliberately as
# the first-ever test of this integration. Empty = fully disabled/no-op until
# Ahmed provides the real ping URL from a free healthchecks.io check. No
# auto-restart, no action of any kind on failure -- ping only.
HEALTHCHECK_URL = "https://hc-ping.com/86ab2f84-204c-4fe1-893c-57fdd03e1a66"


def _ping_healthcheck():
    if not HEALTHCHECK_URL:
        return
    try:
        urllib.request.urlopen(HEALTHCHECK_URL, timeout=5)
    except Exception:
        pass  # never allowed to affect the heartbeat loop itself


def _load():
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data):
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, STATUS_FILE)  # atomic replace -- dashboard never reads a half-written file


def tick():
    now = datetime.now(timezone.utc)
    data = _load()
    data["heartbeat_ts"] = now.isoformat()
    data["next_tick_ts"] = (now + timedelta(seconds=NOTIFY_SECONDS)).isoformat()
    _save(data)


def _demo():
    """ponytail: smallest check that fails if the read-modify-write logic
    breaks -- must set its two keys and leave any pre-existing key untouched."""
    global STATUS_FILE
    real_file = STATUS_FILE
    STATUS_FILE = STATUS_FILE + ".selftest"
    try:
        _save({"claude_awake_ts": "PRESERVE_ME"})
        tick()
        data = _load()
        assert "heartbeat_ts" in data and "next_tick_ts" in data
        assert data["claude_awake_ts"] == "PRESERVE_ME", "must never touch claude_loop_report.py's keys"
        print("claude_loop_heartbeat self-check OK")
    finally:
        if os.path.exists(STATUS_FILE):
            os.remove(STATUS_FILE)
        STATUS_FILE = real_file


def main():
    while True:
        tick()
        _ping_healthcheck()
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        _demo()
    else:
        main()
