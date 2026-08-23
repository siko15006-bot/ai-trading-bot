"""
gold_btc_bot_backtest_usoil.py -- research-only USOILm port of the existing
gold_btc_bot engine.

This reuses the same evaluate_signal()/simulate() path as gold_btc_bot.py and
gold_btc_bot_backtest.py, but points it at real MT5 EA historical data for
USOILm, keeps the same IS/OOS split discipline, and prints a compact trade
sample so the result is evidence-first instead of live-first.

No live order path, no startup wiring.
"""
import copy
import sys
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5

from gold_btc_bot import load_config, TIMEFRAME_MAP
from gold_btc_bot_backtest import COMBINATIONS, simulate, _fetch, _fmt_pf, _stats

SYMBOL = "USOILm"
CONTRACT_SIZE_FALLBACK = 1.0


def _connect(acc="EA"):
    from ladder_guard import ACCOUNTS
    if not mt5.initialize(**ACCOUNTS[acc]):
        raise SystemExit(f"mt5.initialize failed: {mt5.last_error()}")


def _overlay_cfg(cfg):
    cfg = copy.deepcopy(cfg)
    cfg["trend"]["adx_min"].setdefault("OIL", cfg["trend"]["adx_min"]["default"])
    cfg["risk"]["sl_atr_mult"].setdefault("OIL", 1.2)
    cfg["news_symbols"].setdefault(SYMBOL, ["USD"])
    return cfg


def _fmt_trade(t):
    return (f"{datetime.fromtimestamp(t['entry_time'], tz=timezone.utc).isoformat()} "
            f"{t['direction'].upper()} -> {t['exit'].upper()} "
            f"R={t['r']:+.2f} ${t['usd']:+.2f} "
            f"entry={t['entry_price']:.2f} sl={t['sl']:.2f} tp={t['tp']:.2f}")


def _print_sample(label, trades, max_rows=6):
    if not trades:
        print(f"  {label}: no trades")
        return
    rows = trades[:max_rows]
    if len(trades) > max_rows:
        rows = trades[: max_rows // 2] + trades[-(max_rows // 2):]
    print(f"  {label}: {len(trades)} trades")
    for t in rows:
        print(f"    - {_fmt_trade(t)}")


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

    if args:
        raise SystemExit("usage: python gold_btc_bot_backtest_usoil.py [--days N] [--acc EA]")

    cfg = _overlay_cfg(load_config())
    need_h1 = max(cfg["trend"]["ema_slow"], 2 * cfg["trend"]["adx_period"] + 1, cfg["momentum"]["rsi_period"] + 1) + 5
    need_m15 = max(cfg["atr"]["period"], cfg["atr"]["avg_period"]) + 5

    _connect(acc)
    info = mt5.symbol_info(SYMBOL)
    if info is None:
        mt5.shutdown()
        raise SystemExit(f"symbol_info({SYMBOL}) failed -- symbol missing on this terminal")
    if not info.visible:
        mt5.symbol_select(SYMBOL, True)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    warm_start = start - timedelta(days=30)
    start_ts = start.timestamp()
    is_end = start_ts + 0.60 * (end.timestamp() - start_ts)
    val_end = start_ts + 0.80 * (end.timestamp() - start_ts)

    h1_tf = TIMEFRAME_MAP[cfg["trend"]["timeframe"]]
    m15_tf = TIMEFRAME_MAP[cfg["atr"]["timeframe"]]
    h1_rates = _fetch(SYMBOL, h1_tf, warm_start, end)
    m15_rates = _fetch(SYMBOL, m15_tf, warm_start, end)
    lot_size = cfg["lot"].get(acc, 0.01)
    contract_size = info.trade_contract_size or CONTRACT_SIZE_FALLBACK

    print(f"gold_btc_bot USOILm backtest -- last {days} days, account={acc}")
    print(f"symbol={SYMBOL} lot={lot_size} contract_size={contract_size}")
    print(f"engine thresholds: ADX OIL={cfg['trend']['adx_min']['OIL']} | SL_ATR_MULT OIL={cfg['risk']['sl_atr_mult']['OIL']} | RR={cfg['risk']['rr_min']}")
    print("(IS/OOS split: 60% / 20% / 20%; news blackout held out of the backtest exactly like the MT5 baseline scripts)")
    print(f"data: {len(h1_rates)} H1 bars, {len(m15_rates)} M15 bars")

    combo_oos = {}
    combo_trades = {}
    for label, filters_enabled in COMBINATIONS.items():
        trades = simulate(SYMBOL, h1_rates, m15_rates, cfg, filters_enabled, need_h1, need_m15, lot_size, contract_size)
        trades = [t for t in trades if t["entry_time"] >= start_ts]
        combo_trades[label] = trades
        overall = _stats(trades)
        if overall is None:
            print(f"\n### {label}\n  No qualifying trades.")
            combo_oos[label] = None
            continue
        is_trades = [t for t in trades if t["entry_time"] < is_end]
        val_trades = [t for t in trades if is_end <= t["entry_time"] < val_end]
        oos_trades = [t for t in trades if t["entry_time"] >= val_end]
        combo_oos[label] = _stats(oos_trades)
        print(f"\n### {label}")
        print(f"  Overall: n={overall['n']:4d} WR={overall['wr']:5.1f}% PF={_fmt_pf(overall['profit_factor'])} "
              f"Net={overall['net_r']:+.2f}R (${overall['net_usd']:+.2f}) "
              f"MaxDD={overall['max_dd_r']:.2f}R (${overall['max_dd_usd']:.2f})")
        for name, subset in (("IS", is_trades), ("VAL", val_trades), ("OOS", oos_trades)):
            s = _stats(subset)
            print(f"  {name:3s}: {('n=%d WR=%.1f%% PF=%s Net=%+.2fR ($%+.2f)' % (s['n'], s['wr'], _fmt_pf(s['profit_factor']), s['net_r'], s['net_usd'])) if s else 'n=0'}")

    ranked = sorted(((label, s) for label, s in combo_oos.items() if s is not None), key=lambda x: x[1]["net_r"], reverse=True)
    print("\n" + "=" * 78)
    print("Final ranking by OOS net R")
    print("=" * 78)
    for label, s in ranked:
        print(f"  {label:38s}  OOS Net={s['net_r']:+7.2f}R (${s['net_usd']:+8.2f})  PF={_fmt_pf(s['profit_factor']):>5s}  n={s['n']}")

    if ranked:
        best_label = ranked[0][0]
        best_oos = [t for t in combo_trades[best_label] if t["entry_time"] >= val_end]
        print("\nSample OOS trades from best combo:", best_label)
        _print_sample("best-combo OOS", best_oos)

    mt5.shutdown()


if __name__ == "__main__":
    main()
