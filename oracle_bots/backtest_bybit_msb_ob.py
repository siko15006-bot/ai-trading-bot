"""
backtest_bybit_msb_ob.py — test the forwarded "Market Structure Break (MSB) +
Order Block" post (2026-07-10) on real Bybit H1 data.

MSB = the same 5-bar fractal structure-break definition already validated in
backtest_bybit_cisd.py (reused via find_fractals). The post's actual edge
over plain CISD is that entry is NOT at the break candle — it's a retest
limit into the "order block" (the last opposing-colour candle before the
breakout leg), which is what makes this an Order Block strategy rather than
a market-order structure break. SL = beyond the OB zone. TP = RR-based.
Pending OB retest orders expire after OB_WINDOW bars if never filled.
"""
import pandas as pd

from backtest_bybit_cisd import fetch, find_fractals

SYMBOLS = ["ETH/USDT:USDT", "SOL/USDT:USDT", "BTC/USDT:USDT"]
OB_WINDOW = 10  # bars a pending OB retest order stays live before cancel


def find_order_block(opens, closes, lows, highs, break_idx, direction, lookback=20):
    """Nearest opposite-colour candle before break_idx -> its [low, high] zone."""
    start = max(0, break_idx - lookback)
    for j in range(break_idx - 1, start - 1, -1):
        bearish = closes[j] < opens[j]
        bullish = closes[j] > opens[j]
        if direction == 1 and bearish:
            return lows[j], highs[j]
        if direction == -1 and bullish:
            return lows[j], highs[j]
    return None


MAKER_FEE = 0.0002  # Bybit USDT-perp standard maker (0.02%) -- OB entry & TP fills
TAKER_FEE = 0.00055  # standard taker (0.055%) -- SL is a stop-market fill


def simulate(df, rr, label, with_fees=False):
    o, h, l, c = df["open"].values, df["high"].values, df["low"].values, df["close"].values
    fh, fl = find_fractals(h, l)

    FIXED_RISK = 1.0
    bal = peak = 100.0
    wins = losses = 0
    max_dd = 0.0
    in_trade = False
    direction = sl = tp = entry = 0
    pending = None  # dict(direction, zone_hi, zone_lo, expire)
    prev_fl = prev_fh = None

    def notional():
        return FIXED_RISK * entry / abs(entry - sl)

    for i in range(1, len(df) - 1):
        if in_trade:
            fee = 0.0
            if direction == 1:
                if l[i] <= sl:
                    if with_fees: fee = notional() * (MAKER_FEE + TAKER_FEE)
                    bal -= FIXED_RISK + fee; losses += 1; in_trade = False
                elif h[i] >= tp:
                    if with_fees: fee = notional() * (MAKER_FEE + MAKER_FEE)
                    bal += FIXED_RISK * rr - fee; wins += 1; in_trade = False
            else:
                if h[i] >= sl:
                    if with_fees: fee = notional() * (MAKER_FEE + TAKER_FEE)
                    bal -= FIXED_RISK + fee; losses += 1; in_trade = False
                elif l[i] <= tp:
                    if with_fees: fee = notional() * (MAKER_FEE + MAKER_FEE)
                    bal += FIXED_RISK * rr - fee; wins += 1; in_trade = False
            peak = max(peak, bal)
            max_dd = max(max_dd, (peak - bal) / peak * 100)
            if bal <= 0: break
            if in_trade:
                continue

        if pending is not None:
            if i > pending["expire"]:
                pending = None
            elif pending["direction"] == 1 and l[i] <= pending["zone_hi"]:
                entry = pending["zone_hi"]; sl = pending["zone_lo"]
                if entry - sl > 0:
                    tp = entry + (entry - sl) * rr; direction = 1; in_trade = True
                pending = None
                continue
            elif pending["direction"] == -1 and h[i] >= pending["zone_lo"]:
                entry = pending["zone_lo"]; sl = pending["zone_hi"]
                if sl - entry > 0:
                    tp = entry - (sl - entry) * rr; direction = -1; in_trade = True
                pending = None
                continue

        cur_fl, cur_fh = fl[i], fh[i]
        if cur_fl is None or cur_fh is None:
            prev_fl, prev_fh = cur_fl, cur_fh
            continue

        if pending is None:
            if prev_fl is not None and c[i] < prev_fl and c[i - 1] >= prev_fl:
                ob = find_order_block(o, c, l, h, i, -1)
                if ob:
                    pending = dict(direction=-1, zone_lo=ob[0], zone_hi=ob[1], expire=i + OB_WINDOW)
            elif prev_fh is not None and c[i] > prev_fh and c[i - 1] <= prev_fh:
                ob = find_order_block(o, c, l, h, i, 1)
                if ob:
                    pending = dict(direction=1, zone_lo=ob[0], zone_hi=ob[1], expire=i + OB_WINDOW)

        prev_fl, prev_fh = cur_fl, cur_fh

    tot = wins + losses
    wr = wins / tot * 100 if tot else 0
    return dict(label=label, trades=tot, wr=round(wr, 1), pnl=round(bal - 100, 2), max_dd=round(max_dd, 1))


if __name__ == "__main__":
    all_results = []
    for symbol in SYMBOLS:
        df = fetch(symbol)
        print(f"{symbol}: {len(df)} bars | {df['time'].iloc[0].date()} to {df['time'].iloc[-1].date()}")
        for rr in (2.0, 3.0):
            all_results.append(simulate(df, rr, f"{symbol} MSB+OB RR1:{rr:.0f} (no fees)", with_fees=False))
            all_results.append(simulate(df, rr, f"{symbol} MSB+OB RR1:{rr:.0f} (w/ fees)", with_fees=True))
        half = len(df) // 2
        all_results.append(simulate(df.iloc[:half].reset_index(drop=True), 2.0, f"{symbol} H1(first) RR2 (w/ fees)", with_fees=True))
        all_results.append(simulate(df.iloc[half:].reset_index(drop=True), 2.0, f"{symbol} H2(second) RR2 (w/ fees)", with_fees=True))

    print(f"\n{'Config':<32} {'Tr':>4} {'WR%':>5} {'PnL$':>7} {'MaxDD':>6}")
    print("-" * 58)
    for r in sorted(all_results, key=lambda r: -r["pnl"]):
        print(f"{r['label']:<32} {r['trades']:>4} {r['wr']:>4.0f}% {r['pnl']:>+7.2f} {r['max_dd']:>5.0f}%")
