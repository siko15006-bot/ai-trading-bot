"""
risk_guard_autorun.py -- standalone, OS-scheduled wrapper around portfolio_risk_guard.py.

Built 2026-08-06 after RISK_HALT.flag stayed stuck overnight: portfolio_risk_guard.py
has its own daily UTC-date reset logic, but nothing invoked it except Claude's manual
loop tick. If that loop paused (session gap, restart, Ahmed asleep with no tick firing),
the flag could stay stuck indefinitely on any day, and a real drawdown could also go
undetected for the same reason. This script removes that dependency entirely: it pulls
equity from status_dashboard.py's own API (already aggregates all 4 accounts, including
Oracle) and calls portfolio_risk_guard.py itself, run on its own OS schedule via
Task Scheduler (task: PortfolioRiskGuard_AutoRun) -- independent of Claude, of any bot,
and of any human tick.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

from ntfy_alert import alert

STATUS_URL = "http://localhost:5001/api/status"
GUARD_SCRIPT = r"C:\TradingBot\Bot_Active\portfolio_risk_guard.py"
LOG_FILE = r"C:\TradingBot\Bot_Active\risk_guard_autorun.log"
FLAG_FILE = r"C:\TradingBot\Bot_Active\RISK_HALT.flag"

# 2026-08-08 (Ahmed, explicit approval, Risk Change): mirror RISK_HALT state
# to Oracle so its Bybit bots respect the same portfolio-wide halt. Same SSH
# key/host status_dashboard.py already uses to pull Oracle data, just
# pushing instead. A push failure never raises -- Oracle's own
# risk_halt_gate.py fails closed (blocks entries) on a missing/stale file,
# so a broken push degrades safely rather than silently.
ORACLE_SSH_KEY = r"C:\Users\ahmed\Desktop\oracle_key"
ORACLE_HOST = "ubuntu@<REDACTED_ORACLE_IP>"
ORACLE_STATE_PATH = "/home/ubuntu/trading-bot/risk_halt_state.json"
PUSH_STATE_FILE = r"C:\TradingBot\Bot_Active\risk_halt_push_state.json"
# 2026-08-08 (operational validation gap, Ahmed's RISK_HALT completeness review):
# a push failure alone was silent -- Oracle correctly fails closed (safe), but
# nobody would know WHY Oracle stopped taking new entries. 3 consecutive
# failures (~15 min at the 5-min schedule) before alerting, so one transient
# SSH blip doesn't page anyone; alert() itself already throttles repeats.
PUSH_FAIL_ALERT_THRESHOLD = 3


def _load_push_state():
    try:
        with open(PUSH_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"consecutive_failures": 0}


def _save_push_state(state):
    with open(PUSH_STATE_FILE, "w") as f:
        json.dump(state, f)


def push_to_oracle(halted):
    payload = json.dumps({"halted": halted, "pushed_epoch": time.time()})
    state = _load_push_state()
    ok = False
    try:
        result = subprocess.run(
            ["ssh", "-i", ORACLE_SSH_KEY, "-o", "StrictHostKeyChecking=no",
             "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", ORACLE_HOST,
             f"cat > {ORACLE_STATE_PATH}"],
            input=payload, capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            log(f"WARN Oracle RISK_HALT push failed rc={result.returncode} "
                f"stderr={result.stderr.strip()[:200]} -- Oracle fails closed until next successful push")
        else:
            ok = True
    except Exception as e:
        log(f"WARN Oracle RISK_HALT push exception: {e} -- Oracle fails closed until next successful push")

    if ok:
        if state.get("consecutive_failures", 0) >= PUSH_FAIL_ALERT_THRESHOLD:
            alert("risk_guard_autorun: Oracle RISK_HALT push RECOVERED -- sync resumed normally",
                  key="risk_guard_autorun_oracle_push_down")
        state["consecutive_failures"] = 0
    else:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        if state["consecutive_failures"] == PUSH_FAIL_ALERT_THRESHOLD:
            alert(f"risk_guard_autorun: Oracle RISK_HALT push failed {PUSH_FAIL_ALERT_THRESHOLD} times in a row "
                  f"(~{PUSH_FAIL_ALERT_THRESHOLD * 5} min) -- Oracle bots are failing-closed (blocking new "
                  f"entries) as designed, but sync itself needs attention",
                  key="risk_guard_autorun_oracle_push_down")
    _save_push_state(state)

NAME_TO_CODE = {
    "Bybit MT5": "ba",
    "E1 (Exness)": "em",
    "E2 (Exness)": "ea",
    "Oracle (Bybit ccxt)": "baa",
}


def log(msg):
    line = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def main():
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=10) as resp:
            data = json.load(resp)
    except Exception as e:
        log(f"ERROR fetching status_dashboard API: {e}")
        alert(f"risk_guard_autorun: status_dashboard API unreachable ({e}) -- "
              f"portfolio risk guard NOT running, RISK_HALT.flag may go stale",
              key="risk_guard_autorun_api_down")
        sys.exit(1)

    equity = {}
    for acc in data.get("accounts", []):
        code = NAME_TO_CODE.get(acc.get("name"))
        if code and acc.get("equity") is not None:
            equity[code] = acc["equity"]

    missing = [c for c in ("ea", "em", "ba", "baa") if c not in equity]
    if missing:
        log(f"ERROR missing equity for {missing} -- raw accounts={data.get('accounts')}")
        alert(f"risk_guard_autorun: missing equity for {missing} -- "
              f"portfolio risk guard NOT running this cycle",
              key="risk_guard_autorun_missing_equity")
        sys.exit(1)

    args = [sys.executable, GUARD_SCRIPT,
            "--ea", str(equity["ea"]), "--em", str(equity["em"]),
            "--ba", str(equity["ba"]), "--baa", str(equity["baa"])]
    result = subprocess.run(args, capture_output=True, text=True)
    out = result.stdout.strip()
    log(f"equity={equity} -> {out} {result.stderr.strip()}")
    if out.startswith("HALT "):
        alert(f"RISK_HALT triggered: {out}", key="risk_guard_autorun_halt")

    push_to_oracle(os.path.exists(FLAG_FILE))


if __name__ == "__main__":
    main()
