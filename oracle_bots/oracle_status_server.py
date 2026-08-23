"""
oracle_status_server.py — lightweight HTTP observability endpoint for Oracle,
independent of SSH. Runs on port 8090. READ-ONLY: no order_send/create_order
anywhere in this file, cannot place or touch trades.

Why: 2026-07-25/26, SSH to Oracle went flaky/hung multiple times (sshd-level,
not a full network outage — raw TCP:22 stayed reachable). Every observability
path (status_dashboard's Oracle section, my own manual checks, log pulls)
went through SSH, so an SSH hiccup looked exactly like "Oracle might be down"
even though every bot kept running the whole time (cron watchdogs restart
crashed bots independently of SSH or this script). This gives a second,
protocol-independent path to the same information — Ahmed's point: SSH
failure / server failure / process failure / execution failure are four
different things and should be distinguishable, not conflated.
"""
from flask import Flask, jsonify
import os
import time
import glob

app = Flask(__name__)
BOT_DIR = "/home/ubuntu/trading-bot"

# each bot's log file, used both to check liveness (mtime) and to tail
BOTS = {
    "bybit_shield": "/tmp/bybit_shield.log",
    "ladder_guard_bybit": "/tmp/ladder_guard_bybit.log",
    "crypto_watch": "/tmp/crypto_watch.log",
    "tg_signal_bot": "/tmp/tg_signal.log",
    "liquidity_sweep_bot": "/tmp/liquidity_sweep.log",
    "orb_bot": "/tmp/orb_bot.log",
}
STALE_AFTER_SECONDS = 300  # 5 min — matches the cron watchdog interval


def tail(path, n=20):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]
    except Exception:
        return []


@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": time.time()})


@app.route("/bots")
def bots():
    out = {}
    for name, path in BOTS.items():
        if os.path.exists(path):
            age = time.time() - os.path.getmtime(path)
            out[name] = {
                "log_exists": True,
                "last_write_seconds_ago": round(age, 1),
                "stale": age > STALE_AFTER_SECONDS,
            }
        else:
            out[name] = {"log_exists": False, "stale": True}
    return jsonify(out)


@app.route("/logs/<name>")
def logs(name):
    path = BOTS.get(name)
    if not path:
        return jsonify({"error": f"unknown bot '{name}'", "known": list(BOTS.keys())}), 404
    return jsonify({"name": name, "path": path, "lines": tail(path)})


@app.route("/")
def index():
    return jsonify({
        "service": "oracle_status_server",
        "routes": ["/health", "/bots", "/logs/<name>"],
        "known_bots": list(BOTS.keys()),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090)
