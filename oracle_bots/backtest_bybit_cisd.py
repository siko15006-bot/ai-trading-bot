"""
backtest_bybit_cisd.py — test the queued "CISD / fractal reversal" candidate
(see project_strategy_verdicts queue) on real Bybit history via ccxt.

Strategy: 5-bar fractal swing highs/lows define market structure. A bearish
signal fires when price closes below the most recent confirmed swing LOW
that formed during an uptrend (a "higher low" breaking = structure shift) —
mirrored for bullish. SL beyond the swing high/low that preceded the break,
RR-based TP. No indicators, no trend filter — the structure break IS the
signal, which is the whole point of this family vs. EMA/Donchian above.
"""
import time
import ccxt
import pandas as pd

SYMBOLS = ["ETH/USDT:USDT", "SOL/USDT:USDT", "BTC/USDT:USDT"]
TIMEFRAME = "1h"
LOOKBACK_DAYS = 180
FRACTAL_N = 2  # 2 bars either side -> 5-bar fractal

exchange = ccxt.bybit({"options": {"defaultType": "linear"}, "enableRateLimit": True})


def fetch(symbol):
    limit_per_call = 1000
    since = exchange.milliseconds() - LOOKBACK_DAYS * 24 * 60 * 60 * 1000
    rows = []
    while True:
        for attempt in range(5):
            try:
                batch = exchange.fetch_ohlcv(symbol, TIMEFRAME, since=since, limit=limit_per_call)
                break
            except ccxt.RateLimitExceeded:
                time.sleep(5 * (attempt + 1))
        else:
            raise RuntimeError(f"rate limited too many times fetching {symbol}")
        if not batch:
            break
        rows += batch
        since = batch[-1][0] + 1
        if since >= exchange.milliseconds():
            break
        time.sleep(0.5)
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    return df


def find_fractals(highs, lows, n=FRACTAL_N):
    """Return arrays: last confirmed fractal-high price and fractal-low price
    known as of bar i (using only bars < i - n, since a fractal needs n bars
    after it to confirm — no lookahead)."""
    N = len(highs)
    fractal_high = [None] * N
    fractal_low = [None] * N
    last_fh = last_fl = None
    for i in range(n, N - n):
        if highs[i] == max(highs[i - n:i + n + 1]):
            last_fh = highs[i]
        if lows[i] == min(lows[i - n:i + n + 1]):
            last_fl = lows[i]
        # confirmed only n bars later -> record on the confirming bar
        fractal_high[i + n] = last_fh
        fractal_low[i + n] = last_fl
    # forward-fill any gaps
    for i in range(1, N):
        if fractal_high[i] is None:
            fractal_high[i] = fractal_high[i - 1]
        if fractal_low[i] is None:
            fractal_low[i] = fractal_low[i - 1]
    return fractal_high, fractal_low


def simulate(df, rr, label):
    highs, lows, closes = df["high"].values, df["low"].values, df["close"].values
    fh, fl = find_fractals(highs, lows)

    FIXED_RISK = 1.0
    bal = peak = 100.0
    wins = losses = 0
    max_dd = 0.0
    in_trade = False
    direction = sl = tp = 0
    prev_fl = prev_fh = None

    for i in range(1, len(df) - 1):
        if in_trade:
            if direction == 1:
                if lows[i] <= sl: bal -= FIXED_RISK; losses += 1; in_trade = False
                elif highs[i] >= tp: bal += FIXED_RISK * rr; wins += 1; in_trade = False
            else:
                if highs[i] >= sl: bal -= FIXED_RISK; losses += 1; in_trade = False
                elif lows[i] <= tp: bal += FIXED_RISK * rr; wins += 1; in_trade = False
            peak = max(peak, bal)
            max_dd = max(max_dd, (peak - bal) / peak * 100)
            if bal <= 0: break
            if in_trade:
                continue

        cur_fl, cur_fh = fl[i], fh[i]
        if cur_fl is None or cur_fh is None:
            continue

        # bearish CISD: price closes below the last swing low (structure break down)
        if prev_fl is not None and closes[i] < prev_fl and closes[i - 1] >= prev_fl:
            direction = -1; entry = closes[i]; sl = cur_fh
            if sl - entry > 0:
                tp = entry - (sl - entry) * rr; in_trade = True
        # bullish CISD: price closes above the last swing high (structure break up)
        elif prev_fh is not None and closes[i] > prev_fh and closes[i - 1] <= prev_fh:
            direction = 1; entry = closes[i]; sl = cur_fl
            if entry - sl > 0:
                tp = entry + (entry - sl) * rr; in_trade = True

        prev_fl, prev_fh = cur_fl, cur_fh

    tot = wins + losses
    wr = wins / tot * 100 if tot else 0
    return dict(label=label, trades=tot, wr=round(wr, 1), pnl=round(bal - 100, 2), max_dd=round(max_dd, 1))


if __name__ == "__main__":
    all_results = []
    for symbol in SYMBOLS:
        df = fetch(symbol)
        print(f"{symbol}: {len(df)} bars | {df['time'].iloc[0].date()} to {df['time'].iloc[-1].date()}")
        for rr in (1.5, 2.0, 3.0):
            r = simulate(df, rr, f"{symbol} CISD RR1:{rr:.1f}")
            all_results.append(r)

    print(f"\n{'Config':<32} {'Tr':>4} {'WR%':>5} {'PnL$':>7} {'MaxDD':>6}")
    print("-" * 58)
    for r in sorted(all_results, key=lambda r: -r["pnl"]):
        print(f"{r['label']:<32} {r['trades']:>4} {r['wr']:>4.0f}% {r['pnl']:>+7.2f} {r['max_dd']:>5.0f}%")
