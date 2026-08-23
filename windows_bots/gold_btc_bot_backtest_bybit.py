"""gold_btc_bot_backtest_bybit.py -- 2026-08-16, Ahmed-requested follow-up
after the literal Bybit port of gold_btc_bot was rejected
(project_gold_btc_bot_bybit_port.md): that test copy-pasted the MT5-tuned
thresholds unchanged onto Bybit data and failed ("same edge tested literally,
symbol/broker-specific, not general"). This does NOT repeat that experiment
-- it re-runs the SAME gate-ablation methodology as gold_btc_bot_backtest.py
(reuses evaluate_signal/simulate/COMBINATIONS unmodified, zero duplicated
decision logic) but on Bybit's own H1/M15 data, so a verdict here reflects
whether the underlying trend/momentum/volatility gate LOGIC works on Bybit's
price action at all, before any parameter re-tuning is considered.

Read-only research. Zero execution, zero live-bot changes. Reuses BTC and
XAU (BAA's shared symbols, per swing_pending_bybit.py's SYMBOLS) since those
are what BAA actually trades -- a verdict here also has to be read against
feedback_shared_position_measurement_policy.md if this ever goes live
(shared Bybit position with swing_pending_bybit / tg_signal_bot).
"""
import sys
import time
from datetime import datetime, timedelta, timezone

import numpy as np

sys.path.insert(0, r"C:\TradingBot\Bot_Active")
from gold_btc_bot import evaluate_signal, asset_class, load_config, TIMEFRAME_MAP  # noqa: E402,F401
from gold_btc_bot_backtest import (  # noqa: E402
    COMBINATIONS, simulate, print_combination_report, _stats, _fmt_pf,
)

# Bybit taker fee, both sides (0.055% x2) -- simulate()'s R-multiples assume
# perfect fills with zero cost (fine for isolating gate quality, but the
# project's own standing rule is that a PF near 1.0 routinely gets eaten by
# real cost -- see project_research_review_comprehensive.md pattern 3).
# Applied here as a post-hoc R deduction per trade, not inside simulate()
# (shared with the MT5 backtest script; changing its PnL model is a
# separate, bigger decision than this Bybit-specific check).
ROUND_TRIP_FEE_PCT = 0.0011


def _apply_costs(trades):
    out = []
    for t in trades:
        dollar_per_r = t["usd"] / t["r"]  # r is never 0 by construction (+rr_min or -1)
        cost_r = (t["entry_price"] * ROUND_TRIP_FEE_PCT) / t["sl_dist"]
        new_r = t["r"] - cost_r
        out.append({**t, "r": new_r, "usd": new_r * dollar_per_r})
    return out

_RATE_DTYPE = np.dtype([
    ("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"),
    ("close", "f8"), ("tick_volume", "i8"), ("spread", "i4"), ("real_volume", "i8"),
])

BYBIT_SYMBOL = {"XAUUSDm": "XAU/USDT:USDT", "BTCUSDm": "BTC/USDT:USDT"}
QTY = {"XAUUSDm": 0.01, "BTCUSDm": 0.001}  # matches swing_pending_bybit.py's own sizing
CONTRACT_SIZE = 1.0  # USDT-margined linear perp: pnl = qty * price_diff directly


def _exchange():
    import ccxt
    return ccxt.bybit({"enableRateLimit": True, "options": {"defaultType": "linear"}})


def _fetch_bybit(ex, symbol, timeframe, since_ms, until_ms):
    tf_ms = {"15m": 900_000, "1h": 3_600_000}[timeframe]
    rows = []
    cursor = since_ms
    while cursor < until_ms:
        batch = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1][0]) + tf_ms
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(batch) < 1000:
            break
    arr = np.zeros(len(rows), dtype=_RATE_DTYPE)
    for i, (t, o, h, l, c, v) in enumerate(rows):
        arr[i] = (int(t) // 1000, o, h, l, c, int(v), 0, 0)  # ms -> s, matches MT5's copy_rates_range unit
    return arr


def main():
    days = 180
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    symbols = ["XAUUSDm", "BTCUSDm"]

    cfg = load_config()
    need_h1 = max(cfg["trend"]["ema_slow"], 2 * cfg["trend"]["adx_period"] + 1, cfg["momentum"]["rsi_period"] + 1) + 5
    need_m15 = max(cfg["atr"]["period"], cfg["atr"]["avg_period"]) + 5

    ex = _exchange()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    warm_start = start - timedelta(days=30)
    start_ts = start.timestamp()
    since_ms = int(warm_start.timestamp() * 1000)
    until_ms = int(end.timestamp() * 1000)

    print(f"gold_btc_bot backtest on BYBIT data -- last {days} days, symbols {symbols}")
    print("(news blackout not simulated; fixed RR + SL_ATR_MULT same as MT5 config -- gate-logic test, not re-tuned)")

    symbol_data = {}
    for symbol in symbols:
        bybit_symbol = BYBIT_SYMBOL[symbol]
        h1_rates = _fetch_bybit(ex, bybit_symbol, "1h", since_ms, until_ms)
        m15_rates = _fetch_bybit(ex, bybit_symbol, "15m", since_ms, until_ms)
        lot_size = QTY[symbol]
        print(f"\n=== {symbol} ({bybit_symbol}): {len(h1_rates)} H1 bars, {len(m15_rates)} M15 bars "
              f"(qty={lot_size}, contract_size={CONTRACT_SIZE}) ===")
        symbol_data[symbol] = (h1_rates, m15_rates, lot_size, CONTRACT_SIZE)

    # 60/80% time cutpoints across the reported window (start_ts -> end),
    # same IS/VAL/OOS split convention as backtest_mean_reversion_grid.py.
    is_end = start_ts + 0.60 * (end.timestamp() - start_ts)
    val_end = start_ts + 0.80 * (end.timestamp() - start_ts)

    combo_overall = {}
    combo_oos = {}
    for label, filters_enabled in COMBINATIONS.items():
        trades_by_symbol = {}
        all_trades_cost = []
        for symbol in symbols:
            h1_rates, m15_rates, lot_size, contract_size = symbol_data[symbol]
            trades = simulate(symbol, h1_rates, m15_rates, cfg, filters_enabled, need_h1, need_m15,
                               lot_size, contract_size)
            trades = [t for t in trades if t["entry_time"] >= start_ts]
            trades = _apply_costs(trades)
            trades_by_symbol[symbol] = trades
            all_trades_cost.extend(trades)
        overall, _ = print_combination_report(label + " [after Bybit taker fee, no IS/OOS split]", trades_by_symbol, symbols)
        combo_overall[label] = overall

        is_trades = [t for t in all_trades_cost if t["entry_time"] < is_end]
        val_trades = [t for t in all_trades_cost if is_end <= t["entry_time"] < val_end]
        oos_trades = [t for t in all_trades_cost if t["entry_time"] >= val_end]
        is_s, val_s, oos_s = _stats(is_trades), _stats(val_trades), _stats(oos_trades)
        combo_oos[label] = oos_s
        print(f"  IS  (60%): {('n=%d WR=%.1f%% PF=%s Net=%+.2fR' % (is_s['n'], is_s['wr'], _fmt_pf(is_s['profit_factor']), is_s['net_r'])) if is_s else 'n=0'}")
        print(f"  VAL (20%): {('n=%d WR=%.1f%% PF=%s Net=%+.2fR' % (val_s['n'], val_s['wr'], _fmt_pf(val_s['profit_factor']), val_s['net_r'])) if val_s else 'n=0'}")
        print(f"  OOS (20%): {('n=%d WR=%.1f%% PF=%s Net=%+.2fR ($%+.2f)' % (oos_s['n'], oos_s['wr'], _fmt_pf(oos_s['profit_factor']), oos_s['net_r'], oos_s['net_usd'])) if oos_s else 'n=0'}")

    print("\n" + "=" * 78)
    print("Final comparison -- AFTER Bybit taker fee, ranked by OOS net R (the bar that matters)")
    print("=" * 78)
    ranked = sorted(
        ((l, s) for l, s in combo_oos.items() if s is not None),
        key=lambda x: x[1]["net_r"], reverse=True,
    )
    for label, s in ranked:
        print(f"  {label:38s}  OOS: Net={s['net_r']:+7.2f}R (${s['net_usd']:+8.2f})  PF={_fmt_pf(s['profit_factor']):>5s}  n={s['n']}")
    zero_oos = [l for l, s in combo_oos.items() if s is None]
    if zero_oos:
        print(f"  (zero OOS trades: {zero_oos})")


if __name__ == "__main__":
    main()
