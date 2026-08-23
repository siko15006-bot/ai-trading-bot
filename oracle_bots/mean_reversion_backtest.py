"""
Mean-reversion candidate — the natural next test after trend-following (Chandelier
trail) failed on every symbol, which pointed at a ranging/choppy regime rather than
a trending one. Fades extremes back toward the mean INSTEAD OF riding a breakout.

Rules (H1 closed candles, one position at a time per symbol):
  Regime filter: ADX(14) < ADX_MAX  (only counter-trend-fade in a genuinely
                 ranging market — don't fight a real trend, that's what killed
                 the Chandelier test)
  Bollinger Bands(20, 2std) + RSI(14)
  LONG:  close <= lower_band AND RSI < RSI_OS  -> buy
  SHORT: close >= upper_band AND RSI > RSI_OB  -> sell
  SL: entry -/+ ATR_MULT * ATR(14) beyond the band (tight, band-relative)
  TP: middle band (SMA20) — adaptive target, not a fixed RR (bands widen/narrow
      with volatility, target moves with them)
  Time-stop: force close after MAX_BARS_HELD hours if neither hit (ranges drift)
  Fees: 0.055% taker per side.

Walk-forward split, same as every backtest in this project.
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
BB_PERIOD = 20
BB_STD = 2.0
RSI_PERIOD = 14
RSI_OS = 30
RSI_OB = 70
ADX_MAX = 20
ATR_MULT = 1.5
MAX_BARS_HELD = 48  # hours
TAKER_FEE = 0.00055
SYMBOLS = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT',
    'XRP/USDT:USDT', 'ADA/USDT:USDT', 'DOT/USDT:USDT',
    'LINK/USDT:USDT', 'AVAX/USDT:USDT', 'LTC/USDT:USDT',
    'BNB/USDT:USDT', 'DOGE/USDT:USDT', 'ARB/USDT:USDT', 'TRX/USDT:USDT',
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


def sma(vals, period):
    out = [None] * len(vals)
    for i in range(period - 1, len(vals)):
        out[i] = sum(vals[i - period + 1:i + 1]) / period
    return out


def std(vals, period, means):
    out = [None] * len(vals)
    for i in range(period - 1, len(vals)):
        m = means[i]
        window = vals[i - period + 1:i + 1]
        out[i] = (sum((v - m) ** 2 for v in window) / period) ** 0.5
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


def calc_adx(bars, period=14):
    n = len(bars)
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    trs = [0.0] * n
    for i in range(1, n):
        up = bars[i][2] - bars[i - 1][2]
        down = bars[i - 1][3] - bars[i][3]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
        h, l, pc = bars[i][2], bars[i][3], bars[i - 1][4]
        trs[i] = max(h - l, abs(h - pc), abs(l - pc))
    atr = wilder_ema(trs, period)
    plus_di_raw = wilder_ema(plus_dm, period)
    minus_di_raw = wilder_ema(minus_dm, period)
    dx = [None] * n
    for i in range(n):
        if atr[i] and atr[i] > 0 and plus_di_raw[i] is not None and minus_di_raw[i] is not None:
            pd = 100 * plus_di_raw[i] / atr[i]
            md = 100 * minus_di_raw[i] / atr[i]
            denom = pd + md
            dx[i] = 100 * abs(pd - md) / denom if denom else 0
    return wilder_ema([d if d is not None else 0 for d in dx], period)


def simulate(bars):
    closes = [b[4] for b in bars]
    n = len(bars)
    mid = sma(closes, BB_PERIOD)
    sd = std(closes, BB_PERIOD, mid)
    upper = [mid[i] + BB_STD * sd[i] if mid[i] is not None else None for i in range(n)]
    lower = [mid[i] - BB_STD * sd[i] if mid[i] is not None else None for i in range(n)]
    rsi_v = rsi(closes, RSI_PERIOD)
    adx = calc_adx(bars)
    atr = calc_atr(bars)

    trades = []
    pos = None
    warmup = 60
    for i in range(warmup, n):
        b = bars[i]
        if pos is None:
            if None in (mid[i], upper[i], lower[i], rsi_v[i], adx[i], atr[i]):
                continue
            if adx[i] >= ADX_MAX:
                continue
            if b[4] <= lower[i] and rsi_v[i] < RSI_OS:
                sl = b[4] - ATR_MULT * atr[i]
                pos = {'side': 1, 'entry_i': i, 'entry': b[4], 'sl': sl, 'tp': mid[i]}
            elif b[4] >= upper[i] and rsi_v[i] > RSI_OB:
                sl = b[4] + ATR_MULT * atr[i]
                pos = {'side': -1, 'entry_i': i, 'entry': b[4], 'sl': sl, 'tp': mid[i]}
            continue

        held = i - pos['entry_i']
        exit_price = None
        if pos['side'] == 1:
            if b[3] <= pos['sl']:
                exit_price = pos['sl']
            elif b[2] >= pos['tp']:
                exit_price = pos['tp']
        else:
            if b[2] >= pos['sl']:
                exit_price = pos['sl']
            elif b[3] <= pos['tp']:
                exit_price = pos['tp']
        if exit_price is None and held >= MAX_BARS_HELD:
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
    print(f'BollingerBand({BB_PERIOD},{BB_STD}) fade + RSI({RSI_PERIOD}) + ADX<{ADX_MAX} regime filter, '
          f'TP=mid-band, SL={ATR_MULT}xATR, H1, {DAYS}d')
    results = []
    for sym in SYMBOLS:
        try:
            bars = fetch_all(sym, DAYS)
        except Exception as e:
            print(f'{sym}: FETCH FAILED {e}')
            continue
        if len(bars) < 200:
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

    print('\n--- verdict (needs: positive BOTH halves, WR>=45%, n>=15) ---')
    for sym, res in results:
        both = res['half1_pct'] > 0 and res['half2_pct'] > 0
        passes = both and res['wr'] >= 45 and res['n'] >= 15
        print(f"{sym:22s} {'PASS' if passes else 'reject'}")
