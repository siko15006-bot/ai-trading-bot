"""
backtest_bybit_vwap_psar.py — test the forwarded "VWAP + Parabolic SAR" post
(2026-07-10) exactly as specified, on real Bybit H1 data:

LONG when, on a closed bar:
  1) close > VWAP (daily-anchored, closest analog to "session VWAP" on a 24/7 market)
  2) Parabolic SAR is below price (bullish flip)
  3) close > previous bar's high (confirmation breakout, same as other backtest_bybit_* scripts)
SHORT mirrored. SL = the PSAR value itself (it IS a trailing stop by design).
TP = RR 1:2 and 1:3. Same harness/verdict format as backtest_bybit_sma_bbp.py.
"""
import pandas as pd
import pandas_ta as ta

from backtest_bybit_cisd import fetch  # reuse the paginated, rate-limited fetcher

SYMBOLS = ["ETH/USDT:USDT", "SOL/USDT:USDT", "BTC/USDT:USDT"]


def prep(df):
    df = df.copy().reset_index(drop=True)
    idx = pd.DatetimeIndex(df["time"])
    h, l, c, v = (df[col].copy() for col in ("high", "low", "close", "volume"))
    h.index = l.index = c.index = v.index = idx
    vwap = ta.vwap(h, l, c, v, anchor="D")
    psar = ta.psar(df["high"], df["low"], df["close"], af0=0.02, af=0.02, max_af=0.2)
    long_col = [c for c in psar.columns if c.startswith("PSARl")][0]
    short_col = [c for c in psar.columns if c.startswith("PSARs")][0]
    df["vwap"] = vwap.values
    df["psar"] = psar[long_col].fillna(psar[short_col]).values
    df["psar_bullish"] = psar[long_col].notna().values
    df.dropna(subset=["vwap", "psar"], inplace=True)
    return df.reset_index(drop=True)


def simulate(df, rr, label):
    highs, lows, closes = df["high"].values, df["low"].values, df["close"].values
    vwap, psar, bullish = df["vwap"].values, df["psar"].values, df["psar_bullish"].values

    FIXED_RISK = 1.0
    bal = peak = 100.0
    wins = losses = 0
    max_dd = 0.0
    in_trade = False
    direction = sl = tp = 0

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
            if in_trade:
                continue

        long_sig = closes[i] > vwap[i] and bullish[i] and closes[i] > highs[i - 1]
        short_sig = closes[i] < vwap[i] and not bullish[i] and closes[i] < lows[i - 1]
        if long_sig:
            entry = closes[i]; sl = psar[i]
            if entry - sl <= 0: continue
            tp = entry + (entry - sl) * rr; direction = 1; in_trade = True
        elif short_sig:
            entry = closes[i]; sl = psar[i]
            if sl - entry <= 0: continue
            tp = entry - (sl - entry) * rr; direction = -1; in_trade = True

    tot = wins + losses
    wr = wins / tot * 100 if tot else 0
    return dict(label=label, trades=tot, wr=round(wr, 1), pnl=round(bal - 100, 2), max_dd=round(max_dd, 1))


if __name__ == "__main__":
    all_results = []
    for symbol in SYMBOLS:
        df = prep(fetch(symbol))
        print(f"{symbol}: {len(df)} bars | {df['time'].iloc[0].date()} to {df['time'].iloc[-1].date()}")
        for rr in (2.0, 3.0):
            all_results.append(simulate(df, rr, f"{symbol} VWAP+PSAR RR1:{rr:.0f}"))
        half = len(df) // 2
        all_results.append(simulate(df.iloc[:half].reset_index(drop=True), 2.0, f"{symbol} H1(first) RR2"))
        all_results.append(simulate(df.iloc[half:].reset_index(drop=True), 2.0, f"{symbol} H2(second) RR2"))

    print(f"\n{'Config':<36} {'Tr':>4} {'WR%':>5} {'PnL$':>7} {'MaxDD':>6}")
    print("-" * 62)
    for r in sorted(all_results, key=lambda r: -r["pnl"]):
        print(f"{r['label']:<36} {r['trades']:>4} {r['wr']:>4.0f}% {r['pnl']:>+7.2f} {r['max_dd']:>5.0f}%")
