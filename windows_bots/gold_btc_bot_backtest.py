"""
gold_btc_bot_backtest.py -- walk-forward backtest for gold_btc_bot.py that
reuses evaluate_signal() directly (no duplicated decision logic) to measure
what each gate (trend/ADX, momentum/RSI, volatility/ATR) actually
contributes, individually and combined, before any of today's threshold
values in gold_btc_bot_config.json are treated as final (Ahmed 2026-08-04).

Every trade's R-multiple is exact by construction (TP hit = +rr_min R, SL
hit = -1R, since TP = rr_min x SL distance) -- $ P/L is ALSO reported,
converted via each symbol's real mt5.symbol_info().trade_contract_size (not
a guessed point value), at the lot size configured per account.

News blackout is NOT simulated here (the calendar feed is live-only, no
historical archive) -- every combination below runs with news_blackout
forced off. Known backtest limitation, not an oversight; the gate still
applies normally once this ever runs live.

Usage:
    python gold_btc_bot_backtest.py [SYMBOL ...] [--days N] [--acc EA]
    (default: XAUUSDm BTCUSDm, --days 180, --acc EA)
"""
import copy
import sys
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5

from gold_btc_bot import evaluate_signal, asset_class, load_config, TIMEFRAME_MAP

COMBINATIONS = {
    "baseline (direction only, no gates)": {"trend": False, "momentum": False, "atr": False, "news_blackout": False},
    "trend (ADX) only":                     {"trend": True,  "momentum": False, "atr": False, "news_blackout": False},
    "momentum (RSI) only":                  {"trend": False, "momentum": True,  "atr": False, "news_blackout": False},
    "volatility (ATR) only":                {"trend": False, "momentum": False, "atr": True,  "news_blackout": False},
    "all combined":                         {"trend": True,  "momentum": True,  "atr": True,  "news_blackout": False},
}


def _connect(acc="EA"):
    from ladder_guard import ACCOUNTS
    if not mt5.initialize(**ACCOUNTS[acc]):
        raise SystemExit(f"mt5.initialize failed: {mt5.last_error()}")


def _fetch(symbol, timeframe, start, end):
    rates = mt5.copy_rates_range(symbol, timeframe, start, end)
    if rates is None or len(rates) == 0:
        raise SystemExit(f"no historical data for {symbol} -- check symbol name / mt5 connection")
    return rates


def _h1_window_asof(h1_rates, h1_ptr, m15_time, need_h1):
    """Advance h1_ptr forward while the NEXT h1 bar's time is still <=
    m15_time (two-pointer, O(n) total across the whole walk-forward loop
    instead of re-scanning from the start every M15 bar)."""
    while h1_ptr + 1 < len(h1_rates) and h1_rates[h1_ptr + 1]["time"] <= m15_time:
        h1_ptr += 1
    if h1_ptr + 1 < need_h1:
        return h1_ptr, None
    return h1_ptr, h1_rates[h1_ptr + 1 - need_h1:h1_ptr + 1]


def simulate(symbol, h1_rates, m15_rates, base_cfg, filters_enabled, need_h1, need_m15,
             lot_size, contract_size):
    cfg = copy.deepcopy(base_cfg)
    cfg["filters_enabled"] = filters_enabled
    trades = []
    open_trade = None
    h1_ptr = need_h1 - 1

    for i in range(need_m15, len(m15_rates)):
        bar = m15_rates[i]
        if open_trade is not None:
            hit = None
            if open_trade["direction"] == "buy":
                if bar["low"] <= open_trade["sl"]:
                    hit = "sl"
                elif bar["high"] >= open_trade["tp"]:
                    hit = "tp"
            else:
                if bar["high"] >= open_trade["sl"]:
                    hit = "sl"
                elif bar["low"] <= open_trade["tp"]:
                    hit = "tp"
            if hit:
                r = cfg["risk"]["rr_min"] if hit == "tp" else -1.0
                usd = r * open_trade["sl_dist"] * lot_size * contract_size
                exit_price = open_trade["tp"] if hit == "tp" else open_trade["sl"]
                trades.append({
                    "symbol": symbol, "entry_time": open_trade["time"], "exit_time": int(bar["time"]),
                    "direction": open_trade["direction"], "exit": hit, "r": r, "usd": usd,
                    "entry_price": open_trade["entry_price"], "sl": open_trade["sl"], "tp": open_trade["tp"],
                    "sl_dist": open_trade["sl_dist"], "exit_price": exit_price,
                    "exit_bar_high": float(bar["high"]), "exit_bar_low": float(bar["low"]),
                })
                open_trade = None
            continue  # one position at a time -- no new entry while one is open

        h1_ptr, h1_window = _h1_window_asof(h1_rates, h1_ptr, bar["time"], need_h1)
        if h1_window is None:
            continue
        m15_window = m15_rates[i + 1 - need_m15:i + 1]
        decision = evaluate_signal(symbol, h1_window, m15_window, cfg)
        if not decision["passed"] or decision["sl_dist"] is None:
            continue

        direction = decision["direction"]
        cls = asset_class(symbol)
        sl_mult = cfg["risk"]["sl_atr_mult"].get(cls, cfg["risk"]["sl_atr_mult"]["default"])
        sl_dist = decision["sl_dist"] * sl_mult
        price = bar["close"]
        sl = price - sl_dist if direction == "buy" else price + sl_dist
        tp = price + sl_dist * cfg["risk"]["rr_min"] if direction == "buy" else price - sl_dist * cfg["risk"]["rr_min"]
        open_trade = {"direction": direction, "sl": sl, "tp": tp, "time": int(bar["time"]), "sl_dist": sl_dist,
                      "entry_price": price}

    return trades


def _max_drawdown(trades, key):
    """Largest peak-to-trough decline in the cumulative equity curve, in
    whatever unit `key` is ('r' or 'usd')."""
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        equity += t[key]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def _stats(trades):
    n = len(trades)
    if n == 0:
        return None
    wins_r = [t["r"] for t in trades if t["r"] > 0]
    losses_r = [t["r"] for t in trades if t["r"] <= 0]
    net_r = sum(t["r"] for t in trades)
    net_usd = sum(t["usd"] for t in trades)
    gross_win_r = sum(wins_r)
    gross_loss_r = abs(sum(losses_r))
    pf = (gross_win_r / gross_loss_r) if gross_loss_r > 0 else (float("inf") if gross_win_r > 0 else 0.0)
    return {
        "n": n,
        "wr": 100 * len(wins_r) / n,
        "profit_factor": pf,
        "net_r": net_r,
        "net_usd": net_usd,
        "expectancy_r": net_r / n,      # == average R multiple, see report note
        "avg_r": net_r / n,
        "max_dd_r": _max_drawdown(trades, "r"),
        "max_dd_usd": _max_drawdown(trades, "usd"),
    }


def _fmt_pf(pf):
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def print_combination_report(label, trades_by_symbol, symbols):
    all_trades = [t for ts in trades_by_symbol.values() for t in ts]
    overall = _stats(all_trades)
    print(f"\n### {label}")
    if overall is None:
        print("  No qualifying trades on any symbol in this window.")
        return overall, {s: None for s in symbols}
    print(f"  Overall:  n={overall['n']:4d}  WR={overall['wr']:5.1f}%  "
          f"ProfitFactor={_fmt_pf(overall['profit_factor'])}  "
          f"Net={overall['net_r']:+.2f}R (${overall['net_usd']:+.2f})  "
          f"Expectancy={overall['expectancy_r']:+.3f}R/trade  "
          f"AvgR={overall['avg_r']:+.3f}  "
          f"MaxDD={overall['max_dd_r']:.2f}R (${overall['max_dd_usd']:.2f})")
    per_symbol = {}
    for symbol in symbols:
        s = _stats(trades_by_symbol.get(symbol, []))
        per_symbol[symbol] = s
        if s is None:
            print(f"    {symbol:10s}  n=0")
        else:
            print(f"    {symbol:10s}  n={s['n']:4d}  WR={s['wr']:5.1f}%  PF={_fmt_pf(s['profit_factor'])}  "
                  f"Net={s['net_r']:+.2f}R (${s['net_usd']:+.2f})  MaxDD={s['max_dd_r']:.2f}R (${s['max_dd_usd']:.2f})")
    return overall, per_symbol


def main():
    args = sys.argv[1:]
    days = 180
    acc = "EA"
    if "--days" in args:
        idx = args.index("--days")
        days = int(args[idx + 1])
        args = args[:idx] + args[idx + 2:]
    if "--acc" in args:
        idx = args.index("--acc")
        acc = args[idx + 1]
        args = args[:idx] + args[idx + 2:]
    symbols = args if args else ["XAUUSDm", "BTCUSDm"]

    cfg = load_config()
    need_h1 = max(cfg["trend"]["ema_slow"], 2 * cfg["trend"]["adx_period"] + 1, cfg["momentum"]["rsi_period"] + 1) + 5
    need_m15 = max(cfg["atr"]["period"], cfg["atr"]["avg_period"]) + 5

    _connect(acc)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    warm_start = start - timedelta(days=30)  # warm-up only, trimmed from reported results below
    start_ts = start.timestamp()

    print(f"gold_btc_bot backtest -- last {days} days, account={acc}, symbols={symbols}")
    print("(news blackout not simulated -- see module docstring for why)")
    print(f"(fixed RR = {cfg['risk']['rr_min']} for all trades, SL_ATR_MULT: {cfg['risk']['sl_atr_mult']})")

    symbol_data = {}
    for symbol in symbols:
        h1_tf = TIMEFRAME_MAP[cfg["trend"]["timeframe"]]
        m15_tf = TIMEFRAME_MAP[cfg["atr"]["timeframe"]]
        h1_rates = _fetch(symbol, h1_tf, warm_start, end)
        m15_rates = _fetch(symbol, m15_tf, warm_start, end)
        info = mt5.symbol_info(symbol)
        contract_size = info.trade_contract_size if info else 1.0
        lot_size = cfg["lot"].get(acc, 0.01)
        print(f"\n=== {symbol}: {len(h1_rates)} H1 bars, {len(m15_rates)} M15 bars fetched "
              f"(lot={lot_size}, contract_size={contract_size}) ===")
        symbol_data[symbol] = (h1_rates, m15_rates, lot_size, contract_size)

    combo_overall = {}
    combo_per_symbol = {}
    for label, filters_enabled in COMBINATIONS.items():
        trades_by_symbol = {}
        for symbol in symbols:
            h1_rates, m15_rates, lot_size, contract_size = symbol_data[symbol]
            trades = simulate(symbol, h1_rates, m15_rates, cfg, filters_enabled, need_h1, need_m15,
                               lot_size, contract_size)
            trades_by_symbol[symbol] = [t for t in trades if t["entry_time"] >= start_ts]
        overall, per_symbol = print_combination_report(label, trades_by_symbol, symbols)
        combo_overall[label] = overall
        combo_per_symbol[label] = per_symbol

    print("\n" + "=" * 78)
    print("Final comparison across all combinations (sorted by net R)")
    print("=" * 78)
    ranked = sorted(
        ((label, s) for label, s in combo_overall.items() if s is not None),
        key=lambda x: x[1]["net_r"], reverse=True,
    )
    for label, s in ranked:
        print(f"  {label:38s}  Net={s['net_r']:+7.2f}R (${s['net_usd']:+8.2f})  "
              f"MaxDD={s['max_dd_r']:5.2f}R (${s['max_dd_usd']:7.2f})  "
              f"PF={_fmt_pf(s['profit_factor']):>5s}  n={s['n']}")
    if ranked:
        best_net = ranked[0]
        best_risk_adj = min(ranked, key=lambda x: x[1]["max_dd_r"] / x[1]["net_r"] if x[1]["net_r"] > 0 else float("inf"))
        if best_net[0] != best_risk_adj[0]:
            print(f"\n[!] Highest net profit ({best_net[0]}) is NOT the best risk-adjusted "
                  f"combination (best DD-to-profit ratio: {best_risk_adj[0]}, "
                  f"MaxDD={best_risk_adj[1]['max_dd_r']:.2f}R vs its own net "
                  f"{best_risk_adj[1]['net_r']:+.2f}R) -- the biggest winner isn't automatically "
                  f"the safest choice.")

    mt5.shutdown()


if __name__ == "__main__":
    main()
