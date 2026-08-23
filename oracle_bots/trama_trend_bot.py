"""
trama_trend_bot.py — live version of the backtested TRAMA+trendline-break+RSI
exit strategy (see project_strategy_verdicts memory + trama_trendline_backtest.py).
Only SOL and ADA deployed — the only 2 of 6 tested symbols that passed
(positive both walk-forward halves, WR>=35%, n>=15). BTC/ETH/XRP/DOT rejected.

Same indicator logic as the backtest (KAMA proxy for TRAMA, pivot-trendline
break, RSI(14) 70/30 exit), same honesty note: the original posted strategy
had NO stop-loss, we added one (2x ATR) because untested-and-stopless isn't
something we deploy with real money.

No fixed TP — exit is dynamic (RSI threshold), so unlike orb_bot this bot
must poll and actively close on the RSI condition, not just set-and-forget
exchange SL/TP. Runs every H1 close.
"""
import ccxt
import os
import time
import json
from datetime import datetime, timezone
from dotenv import load_dotenv
from oracle_entry_freeze import entry_freeze_status

load_dotenv('/home/ubuntu/.env')

exchange = ccxt.bybit({
    'apiKey': os.getenv('BYBIT_API_KEY'),
    'secret': os.getenv('BYBIT_API_SECRET'),
    'options': {'defaultType': 'linear'},
    'timeout': 10000,
    'enableRateLimit': True,
})

SYMBOLS = {
    'SOL/USDT:USDT': {'step': 0.1, 'min': 0.1, 'max': 100.0, 'lev': 10},
    'ADA/USDT:USDT': {'step': 1.0, 'min': 1.0, 'max': 2000.0, 'lev': 10},
}

RSI_PERIOD = 14
RSI_EXIT_LONG = 70
RSI_EXIT_SHORT = 30
PIVOT_LOOKBACK = 5
ATR_MULT_SL = 2.0
KAMA_PERIOD = 10
RISK_PCT = 0.02
TICK_SEC = 300  # ponytail: bot only acts once per H1 close anyway (see bar-ts gate in process_symbol); 60s polling was pure wasted API calls contributing to shared account rate-limit hits (2026-07-13). 5min still catches every new bar within a wide margin.
STATE_FILE = '/tmp/trama_state.json'
BARS_NEEDED = 250


def now_utc():
    return datetime.now(timezone.utc)


def log(msg):
    print(f'[{now_utc().strftime("%H:%M:%S")}] {msg}', flush=True)


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
    i1, v1 = p1
    i2, v2 = p2
    if i2 == i1:
        return v2
    slope = (v2 - v1) / (i2 - i1)
    return v2 + slope * (at_index - i2)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(s, f)
    except Exception as e:
        log(f'state save error: {e}')


def free_usdt():
    try:
        return float(exchange.fetch_balance().get('USDT', {}).get('free', 0))
    except Exception:
        return 0.0


def open_position(symbol):
    try:
        return exchange.fetch_positions([symbol])[0] if exchange.fetch_positions([symbol]) else None
    except Exception as e:
        log(f'{symbol} fetch_positions error: {e}')
        return None


def place_entry(symbol, side, qty, sl):
    try:
        freeze = entry_freeze_status('trama_trend_bot', account='BAA')
        if freeze.get('frozen'):
            log(f"ENTRY BLOCKED -- central freeze active ({freeze.get('reason')})")
            return False
        freeze = entry_freeze_status('trama_trend_bot', account='BAA')
        if freeze.get('frozen'):
            log(f"ENTRY BLOCKED -- central freeze active ({freeze.get('reason')})")
            return False
        try:
            exchange.set_margin_mode('isolated', symbol)
        except Exception:
            pass
        try:
            exchange.set_leverage(SYMBOLS[symbol]['lev'], symbol)
        except Exception:
            pass
        exchange.create_order(symbol, 'market', side, qty, params={
            'stopLoss': str(round(float(sl), 4)), 'slTriggerBy': 'MarkPrice',
        })
        log(f'ENTRY {side.upper()} {symbol} qty={qty} SL={sl:.4f} (RSI-exit, no fixed TP)')
        return True
    except Exception as e:
        log(f'order error {symbol}: {str(e)[:200]}')
        return False


def close_position(symbol, pos, reason):
    try:
        side = 'sell' if pos.get('side') == 'long' else 'buy'
        qty = float(pos.get('contracts') or 0)
        if qty > 0:
            exchange.create_order(symbol, 'market', side, qty, params={'reduceOnly': True})
            log(f'EXIT {symbol} reason={reason} pnl={pos.get("unrealizedPnl")}')
    except Exception as e:
        log(f'close error {symbol}: {e}')


def process_symbol(symbol, state):
    cfg = SYMBOLS[symbol]
    market_symbol = symbol.replace('/', '').replace(':USDT', '')

    bars = exchange.fetch_ohlcv(symbol, '1h', limit=BARS_NEEDED)
    if len(bars) < 100:
        return
    closes = [b[4] for b in bars]
    trama = kama(closes, KAMA_PERIOD)
    rsi_v = rsi(closes, RSI_PERIOD)
    atr = calc_atr(bars)
    pivot_highs, pivot_lows = find_pivots(bars, PIVOT_LOOKBACK)
    i = len(bars) - 2  # last fully CLOSED bar (last row is still forming)
    b = bars[i]

    # ponytail: bug fix 2026-07-13 — bot polls every 60s but strategy is H1-close-based.
    # Old code treated the still-forming bar as closed, so RSI/trendline signals
    # recomputed on a wiggling live candle caused rapid enter/exit whipsaw
    # (20 ADA round-trips in 15 min, net -$0.59). Gate on bar timestamp so each
    # closed bar is acted on exactly once, matching the backtest and the docstring's
    # stated "runs every H1 close".
    gate_key = f'{symbol}_last_bar_ts'
    if state.get(gate_key) == b[0]:
        return
    state[gate_key] = b[0]

    if trama[i] is None or rsi_v[i] is None or atr[i] is None:
        return

    pos = None
    try:
        positions = exchange.fetch_positions([symbol])
        pos = next((p for p in positions if float(p.get('contracts') or 0) > 0), None)
    except Exception as e:
        log(f'{symbol} fetch_positions error: {e}')
        return

    if pos:
        side = pos.get('side')
        if (side == 'long' and rsi_v[i] >= RSI_EXIT_LONG) or \
           (side == 'short' and rsi_v[i] <= RSI_EXIT_SHORT):
            close_position(symbol, pos, f'RSI={rsi_v[i]:.1f}')
        return

    avail_highs = [p for p in pivot_highs if p[0] + PIVOT_LOOKBACK <= i]
    avail_lows = [p for p in pivot_lows if p[0] + PIVOT_LOOKBACK <= i]

    long_sig = False
    short_sig = False
    if len(avail_highs) >= 2:
        res_now = trendline_value(avail_highs[-2], avail_highs[-1], i)
        res_prev = trendline_value(avail_highs[-2], avail_highs[-1], i - 1)
        long_sig = b[4] > trama[i] and b[4] > res_now and closes[i - 1] <= res_prev
    if len(avail_lows) >= 2:
        sup_now = trendline_value(avail_lows[-2], avail_lows[-1], i)
        sup_prev = trendline_value(avail_lows[-2], avail_lows[-1], i - 1)
        short_sig = b[4] < trama[i] and b[4] < sup_now and closes[i - 1] >= sup_prev

    if not (long_sig or short_sig):
        return

    balance = free_usdt()
    if balance < 5:
        return
    risk_usd = balance * RISK_PCT
    sl_dist = ATR_MULT_SL * atr[i]
    qty = risk_usd / sl_dist if sl_dist else 0
    qty = max(cfg['min'], min(cfg['max'], round(qty / cfg['step']) * cfg['step']))
    if qty * b[4] < 5.0:
        return

    if long_sig:
        place_entry(symbol, 'buy', qty, b[4] - sl_dist)
    elif short_sig:
        place_entry(symbol, 'sell', qty, b[4] + sl_dist)


def run():
    log(f'TRAMA trend bot started — symbols {list(SYMBOLS)} | RSI exit 70/30 | ATR{ATR_MULT_SL}x SL')
    state = load_state()
    while True:
        for symbol in SYMBOLS:
            try:
                process_symbol(symbol, state)
            except Exception as e:
                log(f'{symbol} error: {str(e)[:200]}')
        save_state(state)
        time.sleep(TICK_SEC)


def _selftest():
    """ponytail: smallest check that fails if the indicator math breaks."""
    closes = [100 + i * 0.5 for i in range(60)]
    k = kama(closes, 10)
    assert k[59] is not None and k[59] > k[20], "KAMA should trend up with rising closes"
    r = rsi(closes, 14)
    assert r[59] is not None and r[59] > 70, "RSI should be overbought on a clean uptrend"
    p1, p2 = (0, 100.0), (10, 110.0)
    assert abs(trendline_value(p1, p2, 20) - 120.0) < 1e-9, "trendline projection should extrapolate linearly"
    print('trama_trend_bot selftest OK')


if __name__ == '__main__':
    import sys
    if '--test' in sys.argv:
        _selftest()
    else:
        run()
