"""
tv_rating_signals.py — records TradingView technical-rating flips as paper signals
so they land in the same signals_all.json ledger the telegram groups are judged by.
Purpose: a free benchmark "provider" — if a paid group can't beat TV's own
STRONG_BUY/STRONG_SELL rating, it's not worth following.

Uses the public scanner.tradingview.com endpoint (no auth). Runs every 15 min
via cron. A signal is recorded only on a rating TRANSITION into strong
(|Recommend.All| >= 0.5) so we don't spam the ledger every run.
SL/TP are synthetic scalp levels from 15m ATR(14): SL = 1*ATR, TP = 1.5*ATR.

2026-07-08 PROMOTED TO LIVE (probation): 4 days of paper tracking measured
58% WR / +0.35R per trade over 67 closed signals — best measured crypto edge
we have (every coded backtest candidate was rejected, see
project_strategy_verdicts). Crypto symbols now EXECUTE on Bybit at
RISK_PCT probation risk with the same synthetic SL/TP attached; the gold
proxy stays paper-only. Same promotion path as the telegram groups.
"""
import json
import math
import os
import time

import ccxt
import requests
from dotenv import load_dotenv
from oracle_entry_freeze import entry_freeze_status

load_dotenv('/home/ubuntu/.env')

SIGNALS = '/home/ubuntu/trading-bot/signals_all.json'
STATE = '/home/ubuntu/trading-bot/tv_rating_state.json'
GROUP = 'TV_RATING_15M'
STRONG = 0.5
RISK_PCT = 0.01      # probation, same as an unproven telegram group
LEVERAGE = 3
EXECUTE = False      # 2026-07-12: demoted back to paper -- live WR fell below breakeven post-promotion (see comment below)

# TV ticker -> (ledger symbol, bybit symbol for price/ATR)
SYMBOLS = {
    'BYBIT:BTCUSDT': ('BTC/USDT:USDT', 'BTC/USDT:USDT'),
    'BYBIT:ETHUSDT': ('ETH/USDT:USDT', 'ETH/USDT:USDT'),
    'BYBIT:SOLUSDT': ('SOL/USDT:USDT', 'SOL/USDT:USDT'),
    'BYBIT:XAUTUSDT': ('XAUUSD', 'XAUT/USDT'),  # gold proxy, paper-only anyway
}
PAPER_ONLY = {'XAUUSD'}

ex = ccxt.bybit({
    'apiKey': os.getenv('BYBIT_API_KEY'),
    'secret': os.getenv('BYBIT_API_SECRET'),
    'options': {'defaultType': 'linear'},
    'enableRateLimit': True,
})


def fetch_ratings():
    r = requests.post('https://scanner.tradingview.com/crypto/scan', json={
        'symbols': {'tickers': list(SYMBOLS.keys()), 'query': {'types': []}},
        'columns': ['Recommend.All|15'],
    }, timeout=15)
    r.raise_for_status()
    return {d['s']: d['d'][0] for d in r.json()['data']}


def atr15(sym, n=14):
    bars = ex.fetch_ohlcv(sym, '15m', limit=n + 1)
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i][2], bars[i][3], bars[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


def record(sig):
    data = []
    if os.path.exists(SIGNALS):
        with open(SIGNALS) as f:
            data = json.load(f)
    data.append(sig)
    with open(SIGNALS, 'w') as f:
        json.dump(data, f)


EXEC_STATE = '/home/ubuntu/trading-bot/tv_exec_state.json'
MAX_PER_SYMBOL_DAY = 2   # repeats ARE profitable historically (paper: repeats
MAX_TOTAL_DAY = 4        # +21.4R of the total +24R) — so don't ban them, just
                         # cap the daily bleed a whipsaw day can cause (first
                         # live day 2026-07-08: 5 straight stops, -2.9%).


def exec_quota_used(symbol):
    """True if today's execution quota (per-symbol or global) is exhausted."""
    today = time.strftime('%Y-%m-%d', time.gmtime())
    st = {}
    if os.path.exists(EXEC_STATE):
        try:
            with open(EXEC_STATE) as f:
                st = json.load(f)
        except Exception:
            st = {}
    if st.get('date') != today:
        st = {'date': today, 'traded': {}}
    count = st['traded'].get(symbol, 0)
    if isinstance(count, bool):  # migrate old true/false format
        count = 1 if count else 0
    total = sum(1 if isinstance(v, bool) and v else v for v in st['traded'].values())
    if count >= MAX_PER_SYMBOL_DAY or total >= MAX_TOTAL_DAY:
        return True
    st['traded'][symbol] = count + 1
    with open(EXEC_STATE, 'w') as f:
        json.dump(st, f)
    return False


def execute(symbol, side, price, sl, tp):
    """Live Bybit entry with mandatory SL/TP, sized to RISK_PCT of free balance.
    Skips if a position is already open on the symbol (same rule as tg_signal_bot)."""
    try:
        freeze = entry_freeze_status('tv_rating_signals', account='BAA')
        if freeze.get('frozen'):
            print(f"ENTRY BLOCKED -- central freeze active ({freeze.get('reason')})")
            return
        freeze = entry_freeze_status('tv_rating_signals', account='BAA')
        if freeze.get('frozen'):
            print(f"ENTRY BLOCKED -- central freeze active ({freeze.get('reason')})")
            return
        if exec_quota_used(symbol):
            print(f'skip {symbol}: daily execution quota reached '
                  f'({MAX_PER_SYMBOL_DAY}/symbol, {MAX_TOTAL_DAY} total)')
            return
        for p in ex.fetch_positions():
            if p['symbol'] == symbol and float(p.get('contracts') or 0) > 0:
                print(f'skip {symbol}: position already open')
                return
        free = float(ex.fetch_balance().get('USDT', {}).get('free', 0))
        dist = abs(price - sl)
        if dist <= 0 or free <= 0:
            print(f'skip {symbol}: bad dist/balance')
            return
        market = ex.market(symbol)
        step = float(market['precision']['amount'] or 0.001)
        qty = math.floor((free * RISK_PCT / dist) / step) * step
        qty = round(qty, 8)
        if qty * price < 5.0:
            print(f'skip {symbol}: notional too small ({qty * price:.2f})')
            return
        try:
            ex.set_margin_mode('isolated', symbol)
        except Exception:
            pass
        try:
            ex.set_leverage(LEVERAGE, symbol)
        except Exception:
            pass
        ex.create_order(symbol, 'market', side, qty, params={
            'stopLoss': str(round(sl, 6)), 'slTriggerBy': 'MarkPrice',
            'takeProfit': str(round(tp, 6)), 'tpTriggerBy': 'MarkPrice',
        })
        print(f'EXECUTED {side.upper()} {symbol} qty={qty} SL={sl:.4f} TP={tp:.4f} risk={RISK_PCT:.0%}')
    except Exception as e:
        print(f'execute error {symbol}: {str(e)[:200]}')


def main():
    state = {}
    if os.path.exists(STATE):
        with open(STATE) as f:
            state = json.load(f)
    try:
        ratings = fetch_ratings()
    except Exception as e:
        print(f'ratings fetch failed: {e}')
        return
    for tick, rating in ratings.items():
        ledger_sym, bybit_sym = SYMBOLS[tick]
        prev = state.get(tick, 0)
        state[tick] = rating
        side = None
        if rating >= STRONG and prev < STRONG:
            side = 'buy'
        elif rating <= -STRONG and prev > -STRONG:
            side = 'sell'
        if side is None:
            continue
        try:
            price = ex.fetch_ticker(bybit_sym)['last']
            a = atr15(bybit_sym)
        except Exception as e:
            print(f'price/atr failed {bybit_sym}: {e}')
            continue
        sl = price - a if side == 'buy' else price + a
        tp = price + 1.5 * a if side == 'buy' else price - 1.5 * a
        record({'ts': int(time.time()), 'group': GROUP, 'symbol': ledger_sym,
                'side': side, 'entry_low': price * 0.999, 'entry_high': price * 1.001,
                'sl': round(sl, 6), 'targets': [round(tp, 6)], 'status': 'pending'})
        print(f'recorded {side} {ledger_sym} @ {price} rating={rating:.2f} SL={sl:.4f} TP={tp:.4f}')
        if EXECUTE and ledger_sym not in PAPER_ONLY:
            execute(bybit_sym, side, price, sl, tp)
    with open(STATE, 'w') as f:
        json.dump(state, f)


if __name__ == '__main__':
    main()
