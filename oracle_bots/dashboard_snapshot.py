"""
dashboard_snapshot.py — READ-ONLY. Run on Oracle by status_dashboard.py over
SSH. Prints one JSON object to stdout: BAA balance + positions (with real
SL/TP straight from the exchange) merged with ladder_guard_bybit's own
persisted state (R-multiple, management path, runner status, and the
ladder's actual threshold constants). Never places, modifies, or cancels
any order -- read calls only (fetch_balance, fetch_positions, plus reading
a JSON file another process already wrote).

Why merge instead of recomputing R/thresholds here: the dashboard must
never show a different "original R" or threshold than the one actually
driving the live ladder (Ahmed, 2026-07-26: never re-derive that
denominator independently). Reading ladder_guard_bybit's own state file is
the only way to guarantee that.
"""
import ccxt
import json
import os
import subprocess
import time
from dotenv import load_dotenv

load_dotenv("/home/ubuntu/.env")

LADDER_STATE_FILE = "/tmp/ladder_guard_bybit_state.json"
LIQSWEEP_STATE_FILE = "/tmp/liqsweep_state.json"


def read_ladder_state():
    try:
        with open(LADDER_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def read_liqsweep_open_since():
    # liquidity_sweep_bot.py's own state file has open_since_ms set only
    # while IT believes it currently holds an open position. Using this
    # (rather than guessing "BTC short = liquidity_sweep" from symbol/side
    # alone) avoids mislabeling Ahmed's own manual BTC shorts as the bot's.
    try:
        with open(LIQSWEEP_STATE_FILE) as f:
            return json.load(f).get("open_since_ms")
    except Exception:
        return None


def process_alive(name_fragment):
    try:
        out = subprocess.check_output(['ps', '-eo', 'pid=,args='], text=True)
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            _, args = parts
            if name_fragment in args:
                return True
        return False
    except Exception:
        return None  # unknown, not False -- don't claim STANDBY when we can't check


def main():
    out = {"fetched_ts": time.time(), "error": None}
    try:
        ex = ccxt.bybit({
            "apiKey": os.getenv("BYBIT_API_KEY"),
            "secret": os.getenv("BYBIT_API_SECRET"),
            "options": {"defaultType": "linear", "recvWindow": 20000},
        })
        bal = ex.fetch_balance()
        out["balance"] = bal["USDT"]["total"]
        out["equity"] = bal["USDT"]["total"]

        ladder_state = read_ladder_state()
        ladder_positions = (ladder_state or {}).get("positions", {})
        out["ladder_constants"] = (ladder_state or {}).get("constants")
        out["ladder_state_age_sec"] = (
            time.time() - ladder_state["updated_ts"] if ladder_state else None
        )

        positions = []
        for p in ex.fetch_positions():
            qty = float(p.get("contracts") or 0)
            if qty == 0:
                continue
            info = p.get("info", {})
            sl = float(info.get("stopLoss") or 0)
            tp = float(info.get("takeProfit") or 0)
            ladder = ladder_positions.get(p["symbol"])
            positions.append({
                "symbol": p["symbol"],
                "side": p["side"],
                "volume": qty,
                "entry": p.get("entryPrice"),
                "mark": p.get("markPrice"),
                "profit": p.get("unrealizedPnl") or 0,
                "sl": sl or None,
                "tp": tp or None,
                "ladder": ladder,  # None if ladder_guard_bybit hasn't recorded this symbol
            })
        out["positions"] = positions
        out["liquidity_sweep_open_since_ms"] = read_liqsweep_open_since()

        out["bybit_shield_process_alive"] = process_alive("bybit_shield.py")
        out["ladder_guard_process_alive"] = process_alive("ladder_guard_bybit.py")
        out["tg_signal_bot_process_alive"] = process_alive("tg_signal_bot.py")
        out["liquidity_sweep_process_alive"] = process_alive("liquidity_sweep_bot.py")
    except Exception as e:
        out["error"] = str(e)

    print(json.dumps(out))


if __name__ == "__main__":
    main()
