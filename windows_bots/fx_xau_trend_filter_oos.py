"""Read-only OOS check for the proposed XAU H1 trend-alignment filter."""
import json
from datetime import datetime, timezone

import MetaTrader5 as mt5

TRACE = r"C:\TradingBot\Bot_Active\fx_signal_trace.jsonl"
TERMINALS = {"EA": r"C:\MT5_Portable_2\terminal64.exe", "BA": r"C:\Program Files\MetaTrader 5\terminal64.exe"}
OOS_START = datetime(2026, 8, 7, tzinfo=timezone.utc)


def ema50(values):
    value = values[0]
    for close in values[1:]:
        value = close * 2 / 51 + value * 49 / 51
    return value


def trend_up(symbol, ts):
    rates = mt5.copy_rates_from(symbol, mt5.TIMEFRAME_H1, ts, 60)
    hour = int(ts.timestamp() // 3600 * 3600)
    closes = [float(row["close"]) for row in (rates if rates is not None else []) if int(row["time"]) < hour]
    return None if len(closes) < 50 else closes[-1] > ema50(closes[-50:])


def pnl_for_order(ticket, since):
    deals = mt5.history_deals_get(since, datetime.now(timezone.utc)) or []
    entry = next((deal for deal in deals if deal.order == ticket and deal.entry == 0), None)
    if entry is None:
        return None
    position_deals = mt5.history_deals_get(position=entry.position_id) or []
    if not any(deal.entry == 1 for deal in position_deals):
        return None
    return round(sum(deal.profit + deal.swap + deal.commission for deal in position_deals), 2)


def is_countertrend(trend_is_up, side):
    return (trend_is_up is True and side == "sell") or (trend_is_up is False and side == "buy")


def main():
    rows = [json.loads(line) for line in open(TRACE, encoding="utf-8") if line.strip()]
    rows = [row for row in rows if row["symbol"].startswith("XAU") and row.get("order_ticket")]
    results = []
    for account, path in TERMINALS.items():
        if not mt5.initialize(path=path):
            raise RuntimeError(f"MT5 init failed for {account}: {mt5.last_error()}")
        for row in (row for row in rows if row["account"] == account):
            ts = datetime.fromisoformat(row["receive_time"])
            up = trend_up(row["symbol"], ts)
            blocked = is_countertrend(up, row["side"])
            results.append((ts, account, row["group"], row["side"], up, blocked,
                            pnl_for_order(row["order_ticket"], ts)))
        mt5.shutdown()
    for split, selected in (("DEV", [r for r in results if r[0] < OOS_START]),
                            ("OOS", [r for r in results if r[0] >= OOS_START])):
        blocked = [r for r in selected if r[5]]
        closed = [r for r in blocked if r[6] is not None]
        print(split, "trades", len(selected), "blocked", len(blocked),
              "blocked_closed_pnl", round(sum(r[6] for r in closed), 2), "open", len(blocked) - len(closed))
        for r in selected:
            print(r[0].isoformat(), r[1], r[2], r[3], "h1_up=" + str(r[4]),
                  "would_block=" + str(r[5]), "pnl=" + str(r[6]))


if __name__ == "__main__":
    assert is_countertrend(True, "sell")
    assert is_countertrend(False, "buy")
    assert not is_countertrend(True, "buy")
    assert not is_countertrend(False, "sell")
    main()
