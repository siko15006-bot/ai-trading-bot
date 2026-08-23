"""Read-only realized P&L report for attributed Telegram signals."""

import re
import sys
from collections import defaultdict

from portfolio_analytics import fetch_all_closed_trades


TAG_RE = re.compile(r"^L2T:([^:]+):([0-9a-f]{6})$", re.I)


def summarize(trades):
    signals = defaultdict(lambda: {"pnl": 0.0, "accounts": set()})
    for trade in trades:
        if trade.get("magic") != 884400:
            continue
        match = TAG_RE.match(trade.get("comment") or "")
        if not match:
            continue
        group, fingerprint = match.groups()
        row = signals[(group, fingerprint.lower())]
        row["pnl"] += trade["pnl"]
        row["accounts"].add(trade["account"])

    grouped = defaultdict(list)
    for (group, _), row in signals.items():
        grouped[group].append(row["pnl"])

    report = []
    for group, pnls in grouped.items():
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        report.append({
            "group": group,
            "signals": len(pnls),
            "win_rate": round(len(wins) / len(pnls) * 100, 1),
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else float("inf"),
            "net_pnl": round(sum(pnls), 2),
            "expectancy": round(sum(pnls) / len(pnls), 2),
        })
    return sorted(report, key=lambda row: row["net_pnl"], reverse=True)


def _self_test():
    sample = [
        {"magic": 884400, "comment": "L2T:ALPHA:abc123", "pnl": 2.0, "account": "EA"},
        {"magic": 884400, "comment": "L2T:ALPHA:abc123", "pnl": 3.0, "account": "BA"},
        {"magic": 884400, "comment": "L2T:ALPHA:def456", "pnl": -1.0, "account": "EA"},
        {"magic": 996600, "comment": "L2T:ALPHA:fff999", "pnl": 50.0, "account": "EA"},
    ]
    row = summarize(sample)[0]
    assert row == {"group": "ALPHA", "signals": 2, "win_rate": 50.0,
                   "profit_factor": 5.0, "net_pnl": 4.0, "expectancy": 2.0}
    print("telegram_source_report selftest OK")


def main():
    if "--test" in sys.argv:
        _self_test()
        return
    trades, errors = fetch_all_closed_trades(days=180)
    if errors:
        print(f"Account read errors: {errors}", file=sys.stderr)
    print("group,signals,win_rate,profit_factor,net_pnl,expectancy")
    for row in summarize(trades):
        print("{group},{signals},{win_rate},{profit_factor},{net_pnl},{expectancy}".format(**row))


if __name__ == "__main__":
    main()
