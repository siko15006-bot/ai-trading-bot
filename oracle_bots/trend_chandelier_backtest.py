"""
New strategy family (not tried before): Donchian breakout entry + ADX/EMA trend
filter + Chandelier ATR trailing stop (let winners run, no fixed TP). Everything
tested so far on this account (ORB, CISD, SMA+BBP, VWAP+PSAR, MSB+OB, Fibonacci)
used a FIXED RR target - none let profit run with a trailing stop, which is the
likely mechanic behind "random" traders doing well in a trending market.

Rules (checked bar-by-bar on H1 closed candles, one direction at a time per symbol):
  Entry LONG:  close > highest_high(N) of the N bars *before* this one
               AND EMA20 > EMA50  AND ADX(14) >= ADX_MIN
  Entry SHORT: mirrored (close < lowest_low(N), EMA20<EMA50, ADX>=ADX_MIN)
  Initial stop: entry -/+ ATR_MULT_INIT * ATR(14)
  Trailing (Chandelier): stop = highest_close_since_entry - ATR_MULT_TRAIL*ATR(14)
                          (mirrored for shorts), stop only ever moves in favor
  Exit: trailing stop hit (intrabar via high/low). No fixed TP.
  Fees: 0.055% taker per side (entry + exit), applied in price terms.

Walk-forward: first/second half of the sample, same as every other backtest here.
ponytail: one file, no framework — read the printout, no dashboards.
"""
import ccxt, os, time, sys
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv('/home/ubuntu/.env')

exchange = ccxt.bybit({
    'apiKey': os.getenv('BYBIT_API_KEY'),
    'secret': os.getenv('BYBIT_API_SECRET'),
    'options': {'defaultType': 'linear'},
    'enableRateLimit': True,
})

DAYS = 180
N_DONCHIAN = 20
ADX_MIN = 20
ATR_MULT_INIT = 2.0
ATR_MULT_TRAIL = 2.5
TAKER_FEE = 0.00055
SYMBOLS = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT',
    'XRP/USDT:USDT', 'ADA/USDT:USDT', 'DOT/USDT:USDT',
    'LINK/USDT:USDT', 'AVAX/USDT:USDT', 'ARB/USDT:USDT',
]


def fetch_all(symbol, days, timeframe='60'):
    tf = '1h'
    since = exchange.milliseconds() - days * 24 * 60 * 60 * 1000
    out = []
    while True:
        for attempt in range(6):
            try:
                batch = exchange.fetch_ohlcv(symbol, tf, since=since, limit=1000)
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


def calc_ema(vals, period):
    out = [None] * len(vals)
    k = 2 / (period + 1)
    e = None
    for i, v in enumerate(vals):
        if e is None:
            if i == period - 1:
                e = sum(vals[:period]) / period
                out[i] = e
            continue
        e = v * k + e * (1 - k)
        out[i] = e
    return out


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
    plus_di = [None] * n
    minus_di = [None] * n
    dx = [None] * n
    for i in range(n):
        if atr[i] and atr[i] > 0 and plus_di_raw[i] is not None and minus_di_raw[i] is not None:
            plus_di[i] = 100 * plus_di_raw[i] / atr[i]
            minus_di[i] = 100 * minus_di_raw[i] / atr[i]
            denom = plus_di[i] + minus_di[i]
            dx[i] = 100 * abs(plus_di[i] - minus_di[i]) / denom if denom else 0
    adx = wilder_ema([d if d is not None else 0 for d in dx], period)
    return adx


def simulate(bars):
    n = len(bars)
    closes = [b[4] for b in bars]
    atr = calc_atr(bars)
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, 50)
    adx = calc_adx(bars)

    trades = []
    pos = None  # {'side','entry_i','entry_price','stop','peak'}
    warmup = 60

    for i in range(warmup, n):
        b = bars[i]
        if pos is None:
            if atr[i] is None or ema50[i] is None or adx[i] is None:
                continue
            hh = max(x[2] for x in bars[i - N_DONCHIAN:i])
            ll = min(x[3] for x in bars[i - N_DONCHIAN:i])
            long_sig = b[4] > hh and ema20[i] > ema50[i] and adx[i] >= ADX_MIN
            short_sig = b[4] < ll and ema20[i] < ema50[i] and adx[i] >= ADX_MIN
            if long_sig:
                stop = b[4] - ATR_MULT_INIT * atr[i]
                pos = {'side': 1, 'entry_i': i, 'entry': b[4], 'stop': stop, 'peak': b[4]}
            elif short_sig:
                stop = b[4] + ATR_MULT_INIT * atr[i]
                pos = {'side': -1, 'entry_i': i, 'entry': b[4], 'stop': stop, 'peak': b[4]}
            continue

        # manage open position on this bar
        if atr[i] is None:
            continue
        if pos['side'] == 1:
            pos['peak'] = max(pos['peak'], b[2])
            trail = pos['peak'] - ATR_MULT_TRAIL * atr[i]
            pos['stop'] = max(pos['stop'], trail)
            if b[3] <= pos['stop']:
                exit_price = pos['stop']
                raw_pct = (exit_price - pos['entry']) / pos['entry']
                net_pct = raw_pct - 2 * TAKER_FEE
                trades.append({'i': pos['entry_i'], 'pct': net_pct})
                pos = None
        else:
            pos['peak'] = min(pos['peak'], b[3])
            trail = pos['peak'] + ATR_MULT_TRAIL * atr[i]
            pos['stop'] = min(pos['stop'], trail)
            if b[2] >= pos['stop']:
                exit_price = pos['stop']
                raw_pct = (pos['entry'] - exit_price) / pos['entry']
                net_pct = raw_pct - 2 * TAKER_FEE
                trades.append({'i': pos['entry_i'], 'pct': net_pct})
                pos = None

    return trades


def evaluate(trades, risk_per_trade=0.02, start=100):
    if not trades:
        return None
    bal = start
    peak = bal
    max_dd = 0.0
    for t in trades:
        # normalize: treat each trade's raw pct move as if risking risk_per_trade
        # of balance at ATR_MULT_INIT stop distance -> approximate with direct pct
        # since ATR-based stop already sizes risk, use pct move directly scaled by
        # a fixed leverage-equivalent risk unit for comparability across symbols
        bal += bal * t['pct'] * (risk_per_trade / 0.02)  # pct is raw price move %, not R
        peak = max(peak, bal)
        dd = (peak - bal) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    n = len(trades)
    wins = sum(1 for t in trades if t['pct'] > 0)
    wr = wins / n * 100
    total_return = (bal / start - 1) * 100
    half = n // 2
    def half_ret(lst):
        b = start
        for t in lst:
            b += b * t['pct'] * (risk_per_trade / 0.02)
        return (b / start - 1) * 100
    return {
        'n': n, 'wr': wr, 'total_pct': total_return, 'max_dd_pct': max_dd * 100,
        'half1_pct': half_ret(trades[:half]), 'half2_pct': half_ret(trades[half:]),
        'avg_pct_per_trade': sum(t['pct'] for t in trades) / n * 100,
    }


if __name__ == '__main__':
    print(f'Donchian({N_DONCHIAN}) + EMA20/50 + ADX>={ADX_MIN} + Chandelier trail({ATR_MULT_TRAIL}xATR), H1, {DAYS}d')
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
        print(f"{sym:22s} n={res['n']:>3} WR={res['wr']:5.1f}% avg/trade={res['avg_pct_per_trade']:6.2f}% "
              f"total={res['total_pct']:8.1f}% maxDD={res['max_dd_pct']:5.1f}% "
              f"half1={res['half1_pct']:7.1f}% half2={res['half2_pct']:7.1f}%")

    print('\n--- verdict (needs: positive BOTH halves, WR>=30% [trend systems win less often but bigger], n>=15) ---')
    for sym, res in results:
        both = res['half1_pct'] > 0 and res['half2_pct'] > 0
        passes = both and res['wr'] >= 30 and res['n'] >= 15
        print(f"{sym:22s} {'PASS' if passes else 'reject'}")
