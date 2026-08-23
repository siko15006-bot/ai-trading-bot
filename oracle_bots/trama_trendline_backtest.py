"""
Backtest of the posted strategy: TRAMA trend filter + confirmed trendline
breakout (LuxAlgo "Trendlines with Breaks" style) + RSI 70/30 exit.

Honest approximations (can't run someone's exact closed-source Pine indicator
outside TradingView):
  - TRAMA -> KAMA (Kaufman Adaptive MA), same family (efficiency-ratio-based
    adaptive trend line), not the literal LuxAlgo formula.
  - "Trendlines with Breaks" -> pivot-high/pivot-low trendline projection
    (connect the last two swing highs for the descending line, last two swing
    lows for the ascending line), confirmed break = a closed bar beyond the
    projected line, not just a wick.
  - The original post has NO stop-loss at all (exit is RSI-only). Untested
    that way is reckless — added an ATR protective stop so the backtest is
    honest about what we'd actually risk if this went live.

Rules (H1 closed bars):
  LONG:  close > TRAMA AND close breaks above the current descending
         (resistance) trendline -> enter, exit at RSI(14) >= 70 or SL hit
  SHORT: close < TRAMA AND close breaks below the current ascending
         (support) trendline -> enter, exit at RSI(14) <= 30 or SL hit
  SL: entry -/+ 2*ATR(14) (added, not in the original post)
  Fees: 0.055% taker per side.

Walk-forward split, same methodology as every backtest in this project.
ponytail: one file, no framework — read the printout.
"""
import ccxt, os, time
from dotenv import load_dotenv

load_dotenv('/home/ubuntu/.env')

exchange = ccxt.bybit({
    'apiKey': os.getenv('BYBIT_API_KEY'),
    'secret': os.getenv('BYBIT_API_SECRET'),
    'options': {'defaultType': 'linear'},
    'enableRateLimit': True,
})

DAYS = 180
RSI_PERIOD = 14
RSI_EXIT_LONG = 70
RSI_EXIT_SHORT = 30
PIVOT_LOOKBACK = 5   # bars each side for pivot high/low
ATR_MULT_SL = 2.0
KAMA_PERIOD = 10
TAKER_FEE = 0.00055
SYMBOLS = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT',
    'XRP/USDT:USDT', 'ADA/USDT:USDT', 'DOT/USDT:USDT',
]


def fetch_all(symbol, days):
    since = exchange.milliseconds() - days * 24 * 60 * 60 * 1000
    out = []
    while True:
        for attempt in range(6):
            try:
                batch = exchange.fetch_ohlcv(symbol, '1h', since=since, limit=1000)
                break
            except ccxt.RateLimitExceeded:
                time.sleep(3 * (attempt + 1))
        else:
            raise RuntimeError(f'{symbol}: rate limit retries exhausted')
        if not batch:
            break
        out += batch
        since = batch[-1][0] + 1
        time.sleep(0.25)
        if since >= exchange.milliseconds():
            break
    return out


def kama(closes, period=10, fast=2, slow=30):
    n = len(closes)
    out = [None] * n
    if n <= period:
        return out
    out[period] = closes[period]
    fast_sc = 2 / (fast + 1)
    slow_sc = 2 / (slow + 1)
    for i in range(period + 1, n):
        change = abs(closes[i] - closes[i - period])
        volatility = sum(abs(closes[j] - closes[j - 1]) for j in range(i - period + 1, i + 1))
        er = change / volatility if volatility else 0
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        out[i] = out[i - 1] + sc * (closes[i] - out[i - 1])
    return out


def rsi(closes, period=14):
    n = len(closes)
    out = [None] * n
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        ch = closes[i] - closes[i - 1]
        gains[i] = max(ch, 0)
        losses[i] = max(-ch, 0)
    if n <= period:
        return out
    avg_gain = sum(gains[1:period + 1]) / period
    avg_loss = sum(losses[1:period + 1]) / period
    out[period] = 100 - 100 / (1 + (avg_gain / avg_loss if avg_loss else 999))
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss else 999
        out[i] = 100 - 100 / (1 + rs)
    return out


def wilder_ema(vals, period):
    out = [None] * len(vals)
    if len(vals) < period:
        return out
    s = sum(vals[:period]) / period
    out[period - 1] = s
    for i in range(period, len(vals)):
        s = (s * (period - 1) + vals[i]) / period
        out[i] = s
    return out


def calc_atr(bars, period=14):
    trs = [None]
    for i in range(1, len(bars)):
        h, l, pc = bars[i][2], bars[i][3], bars[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return wilder_ema([t if t is not None else 0 for t in trs], period)


def find_pivots(bars, lb=5):
    """Returns lists of (index, price) for pivot highs and pivot lows."""
    n = len(bars)
    highs, lows = [], []
    for i in range(lb, n - lb):
        h = bars[i][2]
        if h == max(bars[j][2] for j in range(i - lb, i + lb + 1)):
            highs.append((i, h))
        l = bars[i][3]
        if l == min(bars[j][3] for j in range(i - lb, i + lb + 1)):
            lows.append((i, l))
    return highs, lows


def trendline_value(p1, p2, at_index):
    """Project the line through p1=(i1,v1), p2=(i2,v2) to at_index."""
    i1, v1 = p1
    i2, v2 = p2
    if i2 == i1:
        return v2
    slope = (v2 - v1) / (i2 - i1)
    return v2 + slope * (at_index - i2)


def simulate(bars):
    closes = [b[4] for b in bars]
    n = len(bars)
    trama = kama(closes, KAMA_PERIOD)
    rsi_v = rsi(closes, RSI_PERIOD)
    atr = calc_atr(bars)
    pivot_highs, pivot_lows = find_pivots(bars, PIVOT_LOOKBACK)

    trades = []
    pos = None
    warmup = 60

    ph_i, pl_i = 0, 0  # pointer into pivot lists, only pivots CONFIRMED by bar i (i.e. index+lb) count

    for i in range(warmup, n):
        b = bars[i]
        if trama[i] is None or rsi_v[i] is None or atr[i] is None:
            continue

        # only use pivots that are confirmed by this bar (pivot at idx needs idx+lb <= i)
        avail_highs = [p for p in pivot_highs if p[0] + PIVOT_LOOKBACK <= i]
        avail_lows = [p for p in pivot_lows if p[0] + PIVOT_LOOKBACK <= i]

        if pos is None:
            if len(avail_highs) >= 2:
                res_line = trendline_value(avail_highs[-2], avail_highs[-1], i)
                if b[4] > trama[i] and b[4] > res_line and closes[i - 1] <= trendline_value(
                        avail_highs[-2], avail_highs[-1], i - 1):
                    sl = b[4] - ATR_MULT_SL * atr[i]
                    pos = {'side': 1, 'entry': b[4], 'sl': sl}
                    continue
            if len(avail_lows) >= 2:
                sup_line = trendline_value(avail_lows[-2], avail_lows[-1], i)
                if b[4] < trama[i] and b[4] < sup_line and closes[i - 1] >= trendline_value(
                        avail_lows[-2], avail_lows[-1], i - 1):
                    sl = b[4] + ATR_MULT_SL * atr[i]
                    pos = {'side': -1, 'entry': b[4], 'sl': sl}
                    continue
        else:
            exit_price = None
            if pos['side'] == 1:
                if b[3] <= pos['sl']:
                    exit_price = pos['sl']
                elif rsi_v[i] >= RSI_EXIT_LONG:
                    exit_price = b[4]
            else:
                if b[2] >= pos['sl']:
                    exit_price = pos['sl']
                elif rsi_v[i] <= RSI_EXIT_SHORT:
                    exit_price = b[4]
            if exit_price is not None:
                raw = (exit_price - pos['entry']) / pos['entry'] if pos['side'] == 1 else \
                      (pos['entry'] - exit_price) / pos['entry']
                trades.append({'pct': raw - 2 * TAKER_FEE})
                pos = None

    return trades


def evaluate(trades, start=100):
    if not trades:
        return None
    bal = start
    peak = bal
    max_dd = 0.0
    for t in trades:
        bal += bal * t['pct']
        peak = max(peak, bal)
        dd = (peak - bal) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    n = len(trades)
    wins = sum(1 for t in trades if t['pct'] > 0)
    total = (bal / start - 1) * 100
    half = n // 2
    def half_ret(lst):
        b = start
        for t in lst:
            b += b * t['pct']
        return (b / start - 1) * 100
    return {
        'n': n, 'wr': wins / n * 100, 'total_pct': total, 'max_dd_pct': max_dd * 100,
        'half1_pct': half_ret(trades[:half]), 'half2_pct': half_ret(trades[half:]),
    }


if __name__ == '__main__':
    print(f'TRAMA(KAMA proxy)+trendline-break+RSI70/30 exit, ATR2x SL added, H1, {DAYS}d')
    results = []
    for sym in SYMBOLS:
        try:
            bars = fetch_all(sym, DAYS)
        except Exception as e:
            print(f'{sym}: FETCH FAILED {e}')
            continue
        if len(bars) < 300:
            print(f'{sym}: too little data')
            continue
        trades = simulate(bars)
        res = evaluate(trades)
        if res is None or res['n'] < 15:
            print(f"{sym:22s} n={0 if res is None else res['n']:>3} — too thin, skip")
            continue
        results.append((sym, res))
        print(f"{sym:22s} n={res['n']:>3} WR={res['wr']:5.1f}% total={res['total_pct']:8.1f}% "
              f"maxDD={res['max_dd_pct']:5.1f}% half1={res['half1_pct']:7.1f}% half2={res['half2_pct']:7.1f}%")

    print('\n--- verdict (needs: positive BOTH halves, WR>=35%, n>=15) ---')
    for sym, res in results:
        both = res['half1_pct'] > 0 and res['half2_pct'] > 0
        passes = both and res['wr'] >= 35 and res['n'] >= 15
        print(f"{sym:22s} {'PASS' if passes else 'reject'}")
