"""Read-only shadow report for per-bot period guards."""

from datetime import datetime, timezone
from pathlib import Path

from bot_period_guard import check
from portfolio_analytics import fetch_all_closed_trades


BOTS = {
    "fx_signal_exec": 884400,
    "gold_btc_bot": 996600,
    "participation_pilot_btc_range": 995502,
    "orb_eth_exness": 992201,
}
ACCOUNTS = ("EA", "EM", "BA")
STATE_DIR = Path(__file__).resolve().parent / "bot_period_guard_shadow_state"


def period_pnl(trades, account, magic, now):
    day = now.strftime("%Y-%m-%d")
    week = now.strftime("%G-W%V")
    scoped = [trade for trade in trades if trade["account"] == account and trade["magic"] == magic]
    daily = sum(trade["pnl"] for trade in scoped if trade["exit_time"].strftime("%Y-%m-%d") == day)
    weekly = sum(trade["pnl"] for trade in scoped if trade["exit_time"].strftime("%G-W%V") == week)
    return daily, weekly


def build_report(trades, balances, now=None, state_dir=STATE_DIR):
    now = now or datetime.now(timezone.utc)
    rows = []
    for bot, magic in BOTS.items():
        for account in ACCOUNTS:
            balance = balances.get(account)
            if balance is None or balance <= 0:
                continue
            daily, weekly = period_pnl(trades, account, magic, now)
            decision = check(bot, account, balance, daily, weekly, now, state_dir)
            rows.append({"bot": bot, "account": account, "magic": magic, **decision})
    return rows


def current_balances():
    import json
    import urllib.request

    names = {"E2 (Exness)": "EA", "E1 (Exness)": "EM", "Bybit MT5": "BA"}
    with urllib.request.urlopen("http://127.0.0.1:5001/api/status", timeout=20) as response:
        accounts = json.load(response)["accounts"]
    return {names[row["name"]]: row["balance"] for row in accounts if row["name"] in names}


def _self_test():
    now = datetime(2026, 8, 18, 12, tzinfo=timezone.utc)
    trades = [
        {"account": "EA", "magic": 884400, "pnl": 3.0,
         "exit_time": datetime(2026, 8, 18, 10, tzinfo=timezone.utc)},
        {"account": "EA", "magic": 884400, "pnl": -1.0,
         "exit_time": datetime(2026, 8, 17, 10, tzinfo=timezone.utc)},
    ]
    assert period_pnl(trades, "EA", 884400, now) == (3.0, 2.0)
    print("bot_period_guard_shadow selftest OK")


def main():
    trades, errors = fetch_all_closed_trades(days=14)
    if errors:
        raise RuntimeError(f"MT5 history errors: {errors}")
    rows = build_report(trades, current_balances())
    print("bot,account,magic,allow_new_entries,reason,daily_pnl,weekly_pnl")
    for row in rows:
        print("{bot},{account},{magic},{allow_new_entries},{reason},{daily_pnl},{weekly_pnl}".format(**row))


if __name__ == "__main__":
    import sys
    _self_test() if "--test" in sys.argv else main()
