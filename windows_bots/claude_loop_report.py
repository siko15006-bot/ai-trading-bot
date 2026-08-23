"""
claude_loop_report.py -- Claude's own liveness signal for the dashboard's
"Claude Monitoring Loop" panel. Run `awake` at the START of a woken
monitoring cycle and `done` (or `error` on a failed/incomplete cycle) at
the END -- this is the only thing that can make the "Claude awake" /
"Analysis" rows go green; claude_loop_heartbeat.py's OS pulse can never
fake it (see status_dashboard.py's claude_loop_status() docstring for why
the two must stay independent: pulse firing != Claude actually working).

Usage:
  python claude_loop_report.py awake
  python claude_loop_report.py done
  python claude_loop_report.py error
"""
import json
import os
import sys
from datetime import datetime, timezone

STATUS_FILE = os.path.join(os.path.dirname(__file__), "claude_loop_status.json")


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
    os.replace(tmp, STATUS_FILE)


def report(action):
    now_iso = datetime.now(timezone.utc).isoformat()
    data = _load()
    if action == "awake":
        data["claude_awake_ts"] = now_iso
    elif action in ("done", "error"):
        data["analysis_completed_ts"] = now_iso
        data["last_cycle_status"] = "ok" if action == "done" else "error"
    else:
        raise SystemExit(f"unknown action {action!r} -- use awake|done|error")
    _save(data)


def _demo():
    global STATUS_FILE
    real_file = STATUS_FILE
    STATUS_FILE = STATUS_FILE + ".selftest"
    try:
        report("awake")
        data = _load()
        assert "claude_awake_ts" in data
        report("done")
        data = _load()
        assert data["last_cycle_status"] == "ok" and "claude_awake_ts" in data, "done must not erase awake_ts"
        report("error")
        data = _load()
        assert data["last_cycle_status"] == "error"
        print("claude_loop_report self-check OK")
    finally:
        if os.path.exists(STATUS_FILE):
            os.remove(STATUS_FILE)
        STATUS_FILE = real_file


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        _demo()
    elif len(sys.argv) > 1:
        report(sys.argv[1])
    else:
        raise SystemExit("usage: python claude_loop_report.py awake|done|error")
