"""
gold_btc_bot_gridsearch.py -- per-symbol parameter grid search for
gold_btc_bot.py, using the SAME simulate()/evaluate_signal() engine as
gold_btc_bot_backtest.py (no parallel fast-path) -- Ahmed 2026-08-04
explicitly asked to verify backtest correctness before trusting any new
optimization, so this deliberately does NOT introduce a second, unverified
computation path. Slower, but provably consistent with the just-verified
engine.

Filters are held at "all combined" (the quality-over-quantity philosophy
already agreed on) -- only the RISK parameters (SL_ATR_MULT, RR_min) are
swept, separately for XAU and BTC, ranked by NET USD (not R -- Ahmed
2026-08-04: R alone can be misleading when contract value differs this
much between the two symbols).

Usage:
    python gold_btc_bot_gridsearch.py [--days N] [--acc EA]
"""
import copy
import sys
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5

from gold_btc_bot import load_config, TIMEFRAME_MAP
from gold_btc_bot_backtest import simulate, _fetch, _stats, _fmt_pf

SL_MULT_GRID = [1.0, 1.3, 1.5, 1.8, 2.2]
RR_GRID = [2.0, 2.5, 3.0, 3.5]

ALL_FILTERS_ON = {"trend": True, "momentum": True, "atr": True, "news_blackout": False}


def _connect(acc="EA"):
    from ladder_guard import ACCOUNTS
    if not mt5.initialize(**ACCOUNTS[acc]):
        raise SystemExit(f"mt5.initialize failed: {mt5.last_error()}")


def run_grid(symbol, h1_rates, m15_rates, base_cfg, need_h1, need_m15, lot_size, contract_size, start_ts):
    results = []
    cls = "XAU" if symbol.startswith("XAU") else "BTC"
    for sl_mult in SL_MULT_GRID:
        for rr in RR_GRID:
            cfg = copy.deepcopy(base_cfg)
            cfg["risk"]["sl_atr_mult"] = {"XAU": sl_mult, "BTC": sl_mult, "default": sl_mult}
            cfg["risk"]["rr_min"] = rr
            trades = simulate(symbol, h1_rates, m15_rates, cfg, ALL_FILTERS_ON, need_h1, need_m15,
                               lot_size, contract_size)
            trades = [t for t in trades if t["entry_time"] >= start_ts]
            s = _stats(trades)
            results.append({"sl_atr_mult": sl_mult, "rr_min": rr, "stats": s})
    return results


def print_top10(symbol, results):
    valid = [r for r in results if r["stats"] is not None]
    valid.sort(key=lambda r: r["stats"]["net_usd"], reverse=True)
    print(f"\n=== {symbol}: top 10 of {len(results)} settings tried, ranked by Net USD ===")
    print(f"  {'SL_mult':>7s} {'RR':>5s} {'n':>5s} {'WR%':>6s} {'PF':>6s} {'NetR':>8s} {'NetUSD':>10s} {'MaxDD_R':>8s} {'MaxDD$':>9s}")
    for r in valid[:10]:
        s = r["stats"]
        print(f"  {r['sl_atr_mult']:7.1f} {r['rr_min']:5.1f} {s['n']:5d} {s['wr']:6.1f} "
              f"{_fmt_pf(s['profit_factor']):>6s} {s['net_r']:+8.2f} {s['net_usd']:+10.2f} "
              f"{s['max_dd_r']:8.2f} {s['max_dd_usd']:9.2f}")
    zero_trade = [r for r in results if r["stats"] is None]
    if zero_trade:
        print(f"  ({len(zero_trade)} settings produced zero trades in this window -- omitted above)")
    return valid


def main():
    args = sys.argv[1:]
    days = 90
    acc = "EA"
    if "--days" in args:
        idx = args.index("--days")
        days = int(args[idx + 1]); args = args[:idx] + args[idx + 2:]
    if "--acc" in args:
        idx = args.index("--acc")
        acc = args[idx + 1]; args = args[:idx] + args[idx + 2:]

    cfg = load_config()
    need_h1 = max(cfg["trend"]["ema_slow"], 2 * cfg["trend"]["adx_period"] + 1, cfg["momentum"]["rsi_period"] + 1) + 5
    need_m15 = max(cfg["atr"]["period"], cfg["atr"]["avg_period"]) + 5

    _connect(acc)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    warm_start = start - timedelta(days=30)
    start_ts = start.timestamp()

    print(f"gold_btc_bot grid search -- last {days} days, account={acc}")
    print(f"SL_ATR_MULT grid: {SL_MULT_GRID}")
    print(f"RR_min grid: {RR_GRID}")
    print("(filters fixed at all-combined; news blackout not simulated -- see backtest module docstring)")

    all_results = {}
    for symbol in ["XAUUSDm", "BTCUSDm"]:
        h1_tf = TIMEFRAME_MAP[cfg["trend"]["timeframe"]]
        m15_tf = TIMEFRAME_MAP[cfg["atr"]["timeframe"]]
        h1_rates = _fetch(symbol, h1_tf, warm_start, end)
        m15_rates = _fetch(symbol, m15_tf, warm_start, end)
        info = mt5.symbol_info(symbol)
        lot_size = cfg["lot"].get(acc, 0.01)
        results = run_grid(symbol, h1_rates, m15_rates, cfg, need_h1, need_m15,
                            lot_size, info.trade_contract_size, start_ts)
        all_results[symbol] = print_top10(symbol, results)

    mt5.shutdown()
    return all_results


if __name__ == "__main__":
    main()
