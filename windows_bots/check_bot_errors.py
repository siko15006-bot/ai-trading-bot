"""
check_bot_errors.py — scans every bot log (local + Oracle) for errors newer
than the last check and prints them, so failures surface in Claude's monitoring
cycle instead of waiting for Ahmed to notice missing activity (2026-07-06).

Prints nothing when all clean (exit 0). State in last_error_check.json.
"""
import json
import os
import re
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BOT = r"C:\TradingBot\Bot_Active"
STATE = os.path.join(BOT, "last_error_check.json")
PATTERNS = re.compile(
    r"retcode=(?!10009)\d+|failed|error|exception|traceback|disabled by client|invalid",
    re.I)
NOISE = re.compile(
    # telethon self-healing disconnects — it always reconnects; the fatal telegram
    # errors (NOT AUTHORIZED / AuthKeyDuplicated) do NOT match this and still alert
    r"reconnect|Attempt \d+ at connecting|Server closed the connection|"
    r"WinError (?:1232|1236|64|10054)|no error")

LOCAL_LOGS = [
    "orb_eth_exness.log", "orb_eth_em.log", "fx_signal_exec.log",
    "fx_signal_exec_out.log", "ema_adx_bot_BA.log", "ema_adx_bot_EA.log",
    "ea_shield_out.log", "ea_shield_ea_out.log", "ea_shield_em_out.log",
    "ladder_guard_ba.log", "ladder_guard_ea.log", "ladder_guard_em.log",
    "swing_pending_ea.log", "swing_pending_ba.log",
]
ORACLE_LOGS = ["/tmp/tg_signal.log", "/tmp/orb_out.log", "/tmp/signal_eval.log", "/tmp/tv_rating.log"]
SSH = ["ssh", "-i", r"C:\Users\ahmed\Desktop\oracle_key", "-o", "StrictHostKeyChecking=no", "ubuntu@<REDACTED_ORACLE_IP>"]


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def check_file(path, offset):
    """Return (new_offset, error_lines) for content after byte offset."""
    try:
        size = os.path.getsize(path)
        if size < offset:
            offset = 0  # rotated/truncated
        with open(path, encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            chunk = f.read()
        errs = [l for l in chunk.splitlines()
                if PATTERNS.search(l) and not NOISE.search(l)]
        return size, errs
    except FileNotFoundError:
        return offset, []


def main():
    state = load_state()
    found = False

    for name in LOCAL_LOGS:
        path = os.path.join(BOT, name)
        off, errs = check_file(path, state.get(name, 0))
        state[name] = off
        for e in errs[-5:]:
            print(f"[{name}] {e.strip()}")
            found = True

    # Oracle: tail recent lines, filter same patterns (no offset tracking — cheap tail)
    try:
        out = subprocess.run(SSH + ["tail -n 30 " + " ".join(ORACLE_LOGS) + " 2>/dev/null"],
                             capture_output=True, text=True, timeout=30).stdout
        current = None
        seen_ts = state.get("_oracle_seen", [])
        new_seen = []
        for line in out.splitlines():
            if line.startswith("==>"):
                current = line.strip("=> ")
                continue
            if PATTERNS.search(line) and not NOISE.search(line):
                key = f"{current}:{line.strip()}"
                new_seen.append(key)
                if key not in seen_ts:
                    print(f"[oracle {current}] {line.strip()}")
                    found = True
        state["_oracle_seen"] = new_seen[-50:]
    except Exception as e:
        print(f"[sentinel] oracle check failed: {e}")
        found = True

    with open(STATE, "w") as f:
        json.dump(state, f)
    if not found:
        print("ALL_CLEAN")


if __name__ == "__main__":
    main()
