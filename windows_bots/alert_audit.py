"""alert_audit.py -- 2026-08-16, historical audit companion to
alert_decision_gate.py. Re-runs the gate against bars that were ACTUALLY
available at each past alert's timestamp (no look-ahead: only bars with
time < alert_time are used), then classifies the outcome using what
happened to price in the following hours.

Read-only research. No orders, no live-bot changes.

SCOPING NOTE: fast_move_watch.log/grind_watch.log lines have no date field
(only HH:MM:SS), and span multiple days -- alerts older than "today" can't
be reliably re-anchored to the correct historical bars without ambiguity
(same HH:MM:SS could be any of several days). This audit is scoped to
TODAY's alerts only, where the date is certain. Recommendation: add a date
to the watcher log format so future audits aren't limited this way.
"""
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, r"C:\TradingBot\Bot_Active")
from alert_decision_gate import analyze, MIN_H1_BARS, MIN_M15_BARS  # noqa: E402

TODAY = datetime(2026, 8, 16, tzinfo=timezone.utc).date()
LOCAL_OFFSET_HOURS = 3  # Ahmed's UTC+3, matches the bots' [HH:MM:SS] local log timestamps


def _fetch_range(symbol, timeframe, days_back=4):
    import MetaTrader5 as mt5
    account_cfg = dict(path=r"C:\MT5_Portable_2\terminal64.exe",
                         login=<REDACTED_MT5_LOGIN_EA>, password="<REDACTED_MT5_PASSWORD_EA>", server="Exness-MT5Real33")
    if not mt5.initialize(**account_cfg):
        raise RuntimeError(f"mt5.initialize failed: {mt5.last_error()}")
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days_back)
        rates = mt5.copy_rates_range(symbol, timeframe, start, end)
    finally:
        mt5.shutdown()
    if rates is None:
        raise RuntimeError("no data")
    cols = ["time", "open", "high", "low", "close", "tick_volume"]
    return [dict(zip(cols, [r[c] for c in cols])) for r in rates]


def _today_alerts(log_path, pattern):
    """Only returns alerts whose local HH:MM:SS is plausibly from today --
    approximated by taking the LAST occurrence of the watcher's most recent
    'started' marker as the cutover point (everything after it is today's
    continuous run). Explicit and conservative, not a guess."""
    lines = open(log_path, encoding="utf-8", errors="replace").read().splitlines()
    started_idxs = [i for i, l in enumerate(lines) if " started" in l and l.startswith("[")]
    if not started_idxs:
        return []
    cutover = started_idxs[-1]
    out = []
    for line in lines[cutover:]:
        m = re.match(r"^\[(\d\d):(\d\d):(\d\d)\] " + pattern, line)
        if not m:
            continue
        h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        local_dt = datetime.combine(TODAY, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=h, minutes=mi, seconds=s)
        utc_dt = local_dt - timedelta(hours=LOCAL_OFFSET_HOURS)
        out.append((utc_dt, line))
    return out


def audit_symbol(symbol_mt5, alerts):
    if not alerts:
        return []
    m15_all = _fetch_range(symbol_mt5, __import__("MetaTrader5").TIMEFRAME_M15)
    h1_all = _fetch_range(symbol_mt5, __import__("MetaTrader5").TIMEFRAME_H1)
    rows = []
    for alert_time, raw_line in alerts:
        cutoff = int(alert_time.timestamp())
        m15_before = [b for b in m15_all if b["time"] < cutoff]
        h1_before = [b for b in h1_all if b["time"] < cutoff]
        m15_before = m15_before[-max(MIN_M15_BARS, 60):]
        h1_before = h1_before[-max(MIN_H1_BARS, 60):]

        price_m = re.search(r"\(([\d.]+) -> ([\d.]+)\)", raw_line)
        alert_price = float(price_m.group(2)) if price_m else None

        gate = analyze(symbol_mt5, "GRIND" if "GRIND" in raw_line else "FAST_MOVE",
                         alert_time.isoformat(), alert_price, m15_before, h1_before)

        after = [b for b in h1_all if b["time"] >= cutoff][:4]  # next up to 4 H1 bars
        price_after = after[-1]["close"] if after else None
        moved_favorably = None
        if price_after is not None and alert_price:
            direction_up = "UP" in raw_line
            moved_favorably = (price_after > alert_price) if direction_up else (price_after < alert_price)

        if gate.decision == "ANALYSIS_INCOMPLETE":
            classification = "INSUFFICIENT_DATA"
        elif gate.breakout_status.startswith("BROKEN") and gate.retest_status == "RETESTED":
            classification = "TRUE_MISS" if moved_favorably else "FALSE_ALERT"
        elif gate.breakout_status.startswith("BROKEN") and gate.retest_status == "NO_RETEST_YET":
            classification = "LATE_CONFIRMATION" if moved_favorably else "VALID_SKIP"
        else:
            classification = "VALID_SKIP"

        rows.append({
            "raw_line": raw_line.strip(), "alert_time_utc": alert_time.isoformat(),
            "alert_price": alert_price, "breakout_status": gate.breakout_status,
            "retest_status": gate.retest_status, "structure_bias": gate.structure_bias,
            "price_after_4h1_bars": price_after, "moved_favorably": moved_favorably,
            "classification": classification,
        })
    return rows


def main():
    fast_move_log = r"C:\TradingBot\Bot_Active\fast_move_watch.log"
    grind_log = r"C:\TradingBot\Bot_Active\grind_watch.log"

    targets = {"BTCUSDm": [], "XAUUSDm": []}
    for symbol in targets:
        targets[symbol] += _today_alerts(fast_move_log, re.escape(f"FAST MOVE {symbol}"))
        targets[symbol] += _today_alerts(grind_log, re.escape(f"GRIND {symbol}"))

    all_rows = []
    for symbol, alerts in targets.items():
        alerts = sorted(set(alerts), key=lambda x: x[0])
        rows = audit_symbol(symbol, alerts)
        all_rows.extend(rows)

    print(f"{'time_utc':20s} {'line':55s} {'breakout':12s} {'retest':14s} {'class':18s}")
    for r in sorted(all_rows, key=lambda x: x["alert_time_utc"]):
        print(f"{r['alert_time_utc']:20s} {r['raw_line'][:55]:55s} {r['breakout_status']:12s} "
              f"{r['retest_status']:14s} {r['classification']:18s}")

    from collections import Counter
    counts = Counter(r["classification"] for r in all_rows)
    print(f"\nTotals: {dict(counts)}")


if __name__ == "__main__":
    main()
