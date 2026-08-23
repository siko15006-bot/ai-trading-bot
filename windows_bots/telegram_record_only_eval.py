"""Offline XAU evaluation for parsed record-only Telegram signals."""

import ast
import bisect
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import MetaTrader5 as mt5

from fx_signal_exec import ENTRY_TOL, _atr_sl_from_bars, _calc_ema, is_countertrend
from portfolio_analytics import ACCOUNTS


LOG = r"C:\TradingBot\Bot_Active\record_only_signals.log"
LINE_RE = re.compile(r"^\[([^]]+)] PARSED \((.*?)\): (\{.*\})$")
LOCAL_TZ = ZoneInfo("Africa/Cairo")


def parse_signals(path=LOG):
    rows = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = LINE_RE.match(line.rstrip())
            if not match:
                continue
            try:
                signal = ast.literal_eval(match.group(3))
                ts = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=LOCAL_TZ).astimezone(timezone.utc)
            except (SyntaxError, ValueError):
                continue
            if signal.get("symbol_base") == "XAUUSD":
                rows.append({"time": ts, "group": match.group(2), **signal})

    # Telegram channels often repeat the same call as a follow-up message.
    deduped, last_seen = [], {}
    for row in sorted(rows, key=lambda item: item["time"]):
        key = (row["group"], row["side"], round(row["entry_low"], 2), round(row["entry_high"], 2))
        previous = last_seen.get(key)
        if previous and (row["time"] - previous).total_seconds() <= 300:
            continue
        last_seen[key] = row["time"]
        deduped.append(row)
    return deduped


def load_rates(signals):
    if not signals:
        return [], []
    cfg = ACCOUNTS["EA"]
    if not mt5.initialize(path=cfg["path"], login=cfg["login"], password=cfg["password"], server=cfg["server"]):
        raise RuntimeError(f"EA MT5 initialize failed: {mt5.last_error()}")
    try:
        start = signals[0]["time"] - timedelta(days=4)
        end = datetime.now(timezone.utc)
        m1 = mt5.copy_rates_range("XAUUSDm", mt5.TIMEFRAME_M1, start, end)
        h1 = mt5.copy_rates_range("XAUUSDm", mt5.TIMEFRAME_H1, start, end)
        if m1 is None or h1 is None:
            raise RuntimeError(f"MT5 history read failed: {mt5.last_error()}")
        return list(m1), list(h1)
    finally:
        mt5.shutdown()


def setup_at(h1, h1_times, ts, side):
    # H1 bars are stamped at open; only bars closed before the signal are legal.
    end = bisect.bisect_right(h1_times, ts.timestamp() - 3600)
    closed = h1[:end]
    if len(closed) < 55:
        return None
    closes = [float(bar["close"]) for bar in closed[-50:]]
    trend_up = closes[-1] > _calc_ema(closes, 50)
    if is_countertrend(trend_up, side):
        return "countertrend"
    atr_source = closed[-15:]
    bars = [(atr_source[i]["high"], atr_source[i]["low"], atr_source[i - 1]["close"])
            for i in range(1, len(atr_source))]
    return _atr_sl_from_bars(bars)


def evaluate(signals, m1, h1):
    m1_times = [int(bar["time"]) for bar in m1]
    h1_times = [int(bar["time"]) for bar in h1]
    results = []
    for signal in signals:
        index = bisect.bisect_left(m1_times, int(signal["time"].timestamp()))
        if index >= len(m1):
            results.append({**signal, "result": "no_data"})
            continue
        price = float(m1[index]["open"])
        lo = signal["entry_low"] * (1 - ENTRY_TOL)
        hi = signal["entry_high"] * (1 + ENTRY_TOL)
        if not lo <= price <= hi:
            results.append({**signal, "result": "outside_entry"})
            continue
        stop_distance = setup_at(h1, h1_times, signal["time"], signal["side"])
        if stop_distance == "countertrend":
            results.append({**signal, "result": "countertrend"})
            continue
        if stop_distance is None:
            results.append({**signal, "result": "no_data"})
            continue
        if signal["side"] == "buy":
            sl, tp = price - stop_distance, price + stop_distance * 3
        else:
            sl, tp = price + stop_distance, price - stop_distance * 3
        result = "open"
        for bar in m1[index:]:
            sl_hit = bar["low"] <= sl if signal["side"] == "buy" else bar["high"] >= sl
            tp_hit = bar["high"] >= tp if signal["side"] == "buy" else bar["low"] <= tp
            if sl_hit:  # conservative when both levels fall inside one M1 candle
                result = "loss"
                break
            if tp_hit:
                result = "win"
                break
        results.append({**signal, "result": result})
    return results


def summarize(results, minimum=5):
    grouped = defaultdict(list)
    for row in results:
        grouped[row["group"]].append(row["result"])
    report = []
    for group, outcomes in grouped.items():
        resolved = [outcome for outcome in outcomes if outcome in ("win", "loss")]
        if len(resolved) < minimum:
            continue
        wins = resolved.count("win")
        net_r = wins * 3 - resolved.count("loss")
        report.append({
            "group": group,
            "resolved": len(resolved),
            "win_rate": round(wins / len(resolved) * 100, 1),
            "net_r": net_r,
            "expectancy_r": round(net_r / len(resolved), 3),
            "outside_entry": outcomes.count("outside_entry"),
            "countertrend": outcomes.count("countertrend"),
            "status": "REVIEW" if len(resolved) >= 15 else "INSUFFICIENT",
        })
    return sorted(report, key=lambda row: row["expectancy_r"], reverse=True)


def _self_test():
    rows = [
        {"group": "A", "result": "win"}, {"group": "A", "result": "loss"},
        {"group": "A", "result": "outside_entry"},
    ]
    report = summarize(rows, minimum=2)[0]
    assert report["net_r"] == 2 and report["expectancy_r"] == 1.0
    print("telegram_record_only_eval selftest OK")


def main():
    if "--test" in sys.argv:
        _self_test()
        return
    signals = parse_signals()
    m1, h1 = load_rates(signals)
    results = evaluate(signals, m1, h1)
    print("group,resolved,win_rate,net_r,expectancy_r,outside_entry,countertrend,status")
    for row in summarize(results):
        print("{group},{resolved},{win_rate},{net_r},{expectancy_r},{outside_entry},{countertrend},{status}".format(**row))
    counts = defaultdict(int)
    for row in results:
        counts[row["result"]] += 1
    print(f"# parsed_xau={len(signals)} outcomes={dict(counts)}", file=sys.stderr)


if __name__ == "__main__":
    main()
