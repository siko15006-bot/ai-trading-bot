"""
Faithful backtest of orb_bot.py (live logic, unchanged) to test different RISK_PCT
values on real Bybit 3m data. Entry/exit/SL/TP/filters are copied EXACTLY from
orb_bot.py - only RISK_PCT is varied, nothing about the tested signal changes.
"""
import ccxt, os, math, time
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv('/home/ubuntu/.env')

exchange = ccxt.bybit({
    'apiKey':  os.getenv('BYBIT_API_KEY'),
    'secret':  os.getenv('BYBIT_API_SECRET'),
    'options': {'defaultType': 'linear'},
    'enableRateLimit': True,
})

SYMBOLS = {
    'SOL/USDT:USDT': {'step': 0.1,  'min': 0.1,  'max': 100.0},
    'ETH/USDT:USDT': {'step': 0.01, 'min': 0.01, 'max': 10.0},
}

SESSION_OPEN_MIN  = 13 * 60 + 30
OR_END_MIN        = SESSION_OPEN_MIN + 30
SESSION_CLOSE_MIN = SESSION_OPEN_MIN + 6 * 60
MIN_MINUTES_LEFT  = 120
RR         = 3.0
MAX_OR_PCT = 0.05
MIN_NOTIONAL = 5.0
TAKER_FEE  = 0.00055   # per side, market fills (entry + SL/TP/session-close)
DAYS       = 180


def fetch_all(symbol, days):
    since = exchange.milliseconds() - days * 24 * 60 * 60 * 1000
    out = []
    while True:
        for attempt in range(6):
            try:
                batch = exchange.fetch_ohlcv(symbol, '3m', since=since, limit=1000)
                break
            except ccxt.RateLimitExceeded:
                time.sleep(3 * (attempt + 1))
        else:
            raise RuntimeError(f'{symbol}: rate limit retries exhausted')
        if not batch:
            break
        out += batch
        since = batch[-1][0] + 1
        time.sleep(0.3)
        if since >= exchange.milliseconds():
            break
    return out


def calc_ema(closes, period):
    k = 2 / (period + 1)
    e = closes[0]
    for c in closes[1:]:
        e = c * k + e * (1 - k)
    return e


def day_key(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d')


def minute_of_day(ms):
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.hour * 60 + dt.minute


def simulate_symbol(bars, cfg, risk_pct):
    """Returns list of (day, pnl_pct_of_balance_at_entry, pnl_usd_per_1_risk_unit)."""
    by_day = {}
    for b in bars:
        by_day.setdefault(day_key(b[0]), []).append(b)

    trades = []
    for day, dbars in sorted(by_day.items()):
        dbars.sort(key=lambda x: x[0])
        or_bars = [b for b in dbars if SESSION_OPEN_MIN <= minute_of_day(b[0]) < OR_END_MIN]
        if len(or_bars) < 10:
            continue
        or_high = max(b[2] for b in or_bars)
        or_low  = min(b[3] for b in or_bars)

        session_bars = [b for b in dbars if minute_of_day(b[0]) >= OR_END_MIN]
        if not session_bars:
            continue

        # daily VWAP uses all bars from midnight up to current bar (as in live)
        midnight_bars = sorted(dbars, key=lambda x: x[0])

        entry = None
        for i, b in enumerate(session_bars):
            m = minute_of_day(b[0])
            if m >= SESSION_CLOSE_MIN:
                break
            if SESSION_CLOSE_MIN - m < MIN_MINUTES_LEFT:
                break  # no entry runway left - matches live skip

            # closed bars up to and including this bar (this bar just closed)
            idx_in_day = midnight_bars.index(b)
            closed_so_far = midnight_bars[:idx_in_day + 1]
            closes = [c[4] for c in closed_so_far]
            if len(closes) < 20:
                continue
            price = b[4]
            ema9 = calc_ema(closes, 9)
            ema20 = calc_ema(closes, 20)

            tpv = sum(((c[2] + c[3] + c[4]) / 3) * c[5] for c in closed_so_far)
            vol = sum(c[5] for c in closed_so_far)
            vwap = tpv / vol if vol else 0

            lo_e, hi_e = min(ema9, ema20), max(ema9, ema20)
            pullback_ok = lo_e <= price <= hi_e

            sig = 0
            if price > or_high and price > vwap and ema9 > ema20 and pullback_ok:
                sig = 1
            elif price < or_low and price < vwap and ema9 < ema20 and pullback_ok:
                sig = -1
            if not sig:
                continue

            sl = or_low if sig == 1 else or_high
            dist = abs(price - sl)
            if dist <= 0 or dist / price > MAX_OR_PCT:
                break  # OR too wide - skip today (matches live: marks traded, stop)

            tp = price + dist * RR if sig == 1 else price - dist * RR
            entry = {'idx': i, 'sig': sig, 'price': price, 'sl': sl, 'tp': tp, 'dist': dist}
            break

        if entry is None:
            continue

        # walk forward bars after entry to find SL/TP/session-end outcome
        exit_price = None
        for b in session_bars[entry['idx'] + 1:]:
            m = minute_of_day(b[0])
            if entry['sig'] == 1:
                if b[3] <= entry['sl']:
                    exit_price = entry['sl']; break
                if b[2] >= entry['tp']:
                    exit_price = entry['tp']; break
            else:
                if b[2] >= entry['sl']:
                    exit_price = entry['sl']; break
                if b[3] <= entry['tp']:
                    exit_price = entry['tp']; break
            if m >= SESSION_CLOSE_MIN:
                exit_price = b[4]; break
        if exit_price is None:
            exit_price = session_bars[-1][4]

        # pnl in R-multiples (dist = 1R), then fee drag in R terms
        raw_r = (exit_price - entry['price']) / entry['dist'] if entry['sig'] == 1 else \
                (entry['price'] - exit_price) / entry['dist']
        # fee as fraction of notional, converted to R: fee_usd = 2*TAKER_FEE*notional,
        # risk_usd = notional*dist/price (approx) -> fee_in_R = 2*TAKER_FEE*price/dist
        fee_in_r = 2 * TAKER_FEE * entry['price'] / entry['dist']
        net_r = raw_r - fee_in_r
        trades.append({'day': day, 'r': net_r})

    return trades


def run_equity(trades_by_symbol, risk_pct, start_balance=68.93):
    """Merge trades from both symbols by day, compound balance day by day."""
    all_trades = []
    for sym, trs in trades_by_symbol.items():
        for t in trs:
            all_trades.append((t['day'], t['r']))
    all_trades.sort(key=lambda x: x[0])

    balance = start_balance
    peak = balance
    max_dd = 0.0
    curve = [balance]
    for day, r in all_trades:
        pnl = balance * risk_pct * r
        balance += pnl
        peak = max(peak, balance)
        dd = (peak - balance) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
        curve.append(balance)

    n = len(all_trades)
    wins = sum(1 for _, r in all_trades if r > 0)
    wr = wins / n * 100 if n else 0
    total_return = (balance / start_balance - 1) * 100

    half = n // 2
    first = all_trades[:half]
    second = all_trades[half:]

    def half_return(lst):
        bal = start_balance
        for _, r in lst:
            bal += bal * risk_pct * r
        return (bal / start_balance - 1) * 100

    return {
        'n_trades': n, 'wr': wr, 'total_return_pct': total_return,
        'max_dd_pct': max_dd * 100, 'final_balance': balance,
        'first_half_pct': half_return(first) if first else 0,
        'second_half_pct': half_return(second) if second else 0,
    }


if __name__ == '__main__':
    print(f'Fetching {DAYS}d of 3m bars for {list(SYMBOLS)}...')
    bars_by_symbol = {}
    for sym in SYMBOLS:
        bars = fetch_all(sym, DAYS)
        bars_by_symbol[sym] = bars
        print(f'{sym}: {len(bars)} bars, {bars[0][0]} -> {bars[-1][0]}')

    trades_by_symbol = {}
    for sym, cfg in SYMBOLS.items():
        trades_by_symbol[sym] = simulate_symbol(bars_by_symbol[sym], cfg, None)
        print(f'{sym}: {len(trades_by_symbol[sym])} trades')

    print('\n--- RISK_PCT sweep (current live = 1.5%) ---')
    for risk_pct in [0.015, 0.02, 0.025, 0.03, 0.04, 0.05]:
        res = run_equity(trades_by_symbol, risk_pct)
        print(f"risk={risk_pct*100:>4.1f}%  trades={res['n_trades']:>3}  WR={res['wr']:>5.1f}%  "
              f"total={res['total_return_pct']:>8.1f}%  maxDD={res['max_dd_pct']:>6.1f}%  "
              f"final=${res['final_balance']:>8.2f}  "
              f"half1={res['first_half_pct']:>7.1f}%  half2={res['second_half_pct']:>7.1f}%")
