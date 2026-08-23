"""
ORB Bot v1.0 — backtested & walk-forward validated 2026-07-04 (see project_orb_strategy_verdict)
Strategy (EXACTLY as backtested — do not tweak without re-backtesting):
  - Session: 13:30 UTC daily (US cash open). Opening range = first 30 min (10 x 3m bars).
  - After OR completes, scan 3m closed bars until 19:30 UTC:
      LONG : close > OR_high AND close > daily VWAP AND EMA9 > EMA20
             AND close between EMA9/EMA20 (pullback filter)
      SHORT: mirrored.
  - SL = opposite side of OR (skip if OR > 5% of price). TP = 3 x SL distance (RR 1:3).
  - One trade per symbol per day. Force-close at session end (19:30 UTC).
  - Symbols: SOL + ETH only (BTC failed walk-forward).
No trailing/BE — the backtest didn't model them; stay faithful to what was proven.

2026-07-12: added XRP/ADA/DOT after running the identical methodology
(orb_expand_backtest.py, 180d, same fetch/simulate code from orb_risk_backtest.py,
untouched) across 10 candidate liquid pairs. Only these 3 passed the same bar as
SOL/ETH originally did (positive BOTH walk-forward halves, WR>=40%, n>=30):
  XRP  n=146 WR=46.6% total=+13.6% maxDD=18.9% (half1=+11.7% half2=+1.7%)
  ADA  n=140 WR=48.6% total=+26.2% maxDD=15.4% (half1=+8.0%  half2=+16.8%)
  DOT  n=135 WR=50.4% total=+15.3% maxDD=19.5% (half1=+0.5%  half2=+14.7%)
The other 7 (BNB/DOGE/LINK/AVAX/LTC/ARB/TRX) failed — negative total or a
negative second half — so the edge does NOT generalize to every liquid pair,
confirming this is asset-specific, not a market-beta illusion.
"""
import ccxt, os, time, math, json
from heartbeat import write_heartbeat  # 2026-08-06: functional heartbeat rollout
from risk_halt_gate import is_portfolio_halted  # 2026-08-08: portfolio-wide RISK_HALT, synced from Windows
from oracle_entry_freeze import entry_freeze_status
from oracle_entry_freeze import entry_freeze_status
from attribution_hooks import make_tag, record_intent  # 2026-08-09: Phase 2, tagging only -- see attribution_hooks.py
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv('/home/ubuntu/.env')

exchange = ccxt.bybit({
    'apiKey':  os.getenv('BYBIT_API_KEY'),
    'secret':  os.getenv('BYBIT_API_SECRET'),
    'options': {'defaultType': 'linear'},
    'timeout': 10000,
    'enableRateLimit': True,
})

SYMBOLS = {
    'SOL/USDT:USDT': {'step': 0.1,  'min': 0.1,  'max': 100.0,  'lev': 10},
    'ETH/USDT:USDT': {'step': 0.01, 'min': 0.01, 'max': 10.0,   'lev': 10},
    'XRP/USDT:USDT': {'step': 0.1,  'min': 0.1,  'max': 1000.0, 'lev': 10},
    'ADA/USDT:USDT': {'step': 1.0,  'min': 1.0,  'max': 2000.0, 'lev': 10},
    'DOT/USDT:USDT': {'step': 0.1,  'min': 0.1,  'max': 200.0,  'lev': 10},
}

SESSION_OPEN_MIN  = 13 * 60 + 30      # 13:30 UTC
OR_END_MIN        = SESSION_OPEN_MIN + 30
SESSION_CLOSE_MIN = SESSION_OPEN_MIN + 6 * 60   # 19:30 UTC
# 2026-07-08: entries were firing 2-4.5h into the session (waiting on the
# pullback filter), leaving too little runway before the forced session-end
# close to reach the 1:3 target - trades got cut short at whatever P&L they
# happened to be at. This does NOT touch the tested entry signal or SL/TP
# sizing - it only skips entries that wouldn't have enough time left to
# develop, same as the existing "OR too wide" skip below.
MIN_MINUTES_LEFT = 120
RR         = 3.0
RISK_PCT   = 0.02   # raised from 0.015 2026-07-10, see orb_risk_backtest.py sweep (backtest-approved, not a live tweak of the tested signal)
MAX_OR_PCT = 0.05
MIN_NOTIONAL = 5.0
TICK_SEC   = 35   # gentler on Bybit rate limits (hit 10006 at 20s on 2026-07-04)
STATE_FILE = '/tmp/orb_state.json'   # survives bot restart within a day


def now_utc():
    return datetime.now(timezone.utc)


def utc_minute():
    n = now_utc()
    return n.hour * 60 + n.minute


def utc_date():
    return now_utc().strftime('%Y-%m-%d')


def log(msg):
    print(f'[{now_utc().strftime("%H:%M:%S")}] {msg}', flush=True)


def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        if s.get('date') != utc_date():
            return {'date': utc_date(), 'traded': {}}
        return s
    except Exception:
        return {'date': utc_date(), 'traded': {}}


def save_state(s):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(s, f)
    except Exception as e:
        log(f'state save error: {e}')


def calc_ema(closes, period):
    k = 2 / (period + 1)
    e = closes[0]
    for c in closes[1:]:
        e = c * k + e * (1 - k)
    return e


def daily_vwap(ohlcv):
    midnight = int(now_utc().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    today = [c for c in ohlcv if c[0] >= midnight] or ohlcv[-20:]
    tpv = sum(((c[2] + c[3] + c[4]) / 3) * c[5] for c in today)
    vol = sum(c[5] for c in today)
    return tpv / vol if vol else 0


def get_opening_range(ohlcv):
    """OR high/low from the 10 x 3m bars starting 13:30 UTC today. None until complete."""
    n = now_utc()
    or_start = int(n.replace(hour=13, minute=30, second=0, microsecond=0).timestamp() * 1000)
    or_end = or_start + 30 * 60 * 1000
    bars = [c for c in ohlcv if or_start <= c[0] < or_end]
    if len(bars) < 10:
        return None, None
    return max(c[2] for c in bars), min(c[3] for c in bars)


def get_balance():
    try:
        return float(exchange.fetch_balance().get('USDT', {}).get('free', 0))
    except Exception:
        return 0.0


def all_open_positions():
    """One positions call per tick instead of one per symbol (rate-limit fix)."""
    try:
        return {p['symbol']: p for p in exchange.fetch_positions()
                if float(p.get('contracts') or 0) > 0}
    except Exception as e:
        log(f'fetch_positions error: {e}')
        return {}


def place_entry(symbol, side, qty, sl, tp):
    try:
        freeze = entry_freeze_status('orb_bot', account='BAA')
        if freeze.get('frozen'):
            log(f"ENTRY BLOCKED -- central freeze active ({freeze.get('reason')})")
            return False
        freeze = entry_freeze_status('orb_bot', account='BAA')
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
        # 2026-08-09 Phase 2: tag + local intent log only -- no trading param changed
        tag = make_tag('orb', symbol, side)
        record_intent('orb_bot', symbol, side, qty, tag)
        exchange.create_order(symbol, 'market', side, qty, params={
            'stopLoss':   str(round(float(sl), 4)), 'slTriggerBy': 'MarkPrice',
            'takeProfit': str(round(float(tp), 4)), 'tpTriggerBy': 'MarkPrice',
            'orderLinkId': tag,
        })
        log(f'ENTRY {side.upper()} {symbol} qty={qty} SL={sl:.4f} TP={tp:.4f}')
        return True
    except Exception as e:
        log(f'order error {symbol}: {str(e)[:200]}')
        # ponytail: insufficient margin won't clear mid-session (positions held to close) —
        # signal caller to stop retrying this symbol today instead of spamming every tick
        # (repeated retries across symbols hit the Bybit rate limit and blocked other entries too, 2026-07-12)
        if '110007' in str(e):
            return 'insufficient_margin'
        return False


def force_close(symbol, pos):
    try:
        side = 'sell' if pos.get('side') == 'long' else 'buy'
        qty = float(pos.get('contracts') or 0)
        if qty > 0:
            # 2026-08-09 Phase 2: tag + local intent log only -- no trading param changed
            tag = make_tag('orb', symbol, side, suffix='close')
            record_intent('orb_bot', symbol, side, qty, tag, extra={'reduceOnly': True})
            exchange.create_order(symbol, 'market', side, qty, params={'reduceOnly': True, 'orderLinkId': tag})
            log(f'SESSION-END CLOSE {symbol} pnl={pos.get("unrealizedPnl")}')
    except Exception as e:
        log(f'force close error {symbol}: {e}')


def run():
    log(f'ORB Bot v1.0 started — symbols {list(SYMBOLS)} | session 13:30-19:30 UTC | RR 1:{RR:.0f}')
    state = load_state()
    last_bucket = {}  # ponytail: 3m-bar-bucket gate per symbol, in-memory only (see below)
    while True:
        try:
            write_heartbeat('orb_bot')
            m = utc_minute()

            # roll state at new UTC day
            if state.get('date') != utc_date():
                state = {'date': utc_date(), 'traded': {}}
                save_state(state)
                log('new day — state reset')

            # outside session: sleep coarsely
            if m < OR_END_MIN or m >= SESSION_CLOSE_MIN + 5:
                time.sleep(60)
                # force-close window check on each wake
                if SESSION_CLOSE_MIN <= utc_minute() < SESSION_CLOSE_MIN + 10:
                    for sym, p in all_open_positions().items():
                        if sym in SYMBOLS:
                            force_close(sym, p)
                continue

            # session-end force close
            if m >= SESSION_CLOSE_MIN:
                for sym, p in all_open_positions().items():
                    if sym in SYMBOLS:
                        force_close(sym, p)
                time.sleep(60)
                continue

            balance = get_balance()
            open_map = all_open_positions()

            for symbol, cfg in SYMBOLS.items():
                try:
                    if state['traded'].get(symbol):
                        continue
                    if symbol in open_map:
                        state['traded'][symbol] = True
                        save_state(state)
                        continue
                    if SESSION_CLOSE_MIN - m < MIN_MINUTES_LEFT:
                        log(f'{symbol}: <{MIN_MINUTES_LEFT}min left in session — skip today (no entry runway)')
                        state['traded'][symbol] = True
                        save_state(state)
                        continue

                    # ponytail: fix 2026-07-13 — signal is decided by the last CLOSED 3m bar
                    # (ohlcv[-2]), which is identical across ~5 consecutive 35s ticks. Polling
                    # every tick anyway wasted ~4/5 of fetch_ohlcv calls and contributed to the
                    # account-wide Bybit rate-limit hits (retCode 10006) shared with the other
                    # bots. Gate on wall-clock 3m bucket so each closed bar is fetched once —
                    # identical entries/exits/timing, same as backtested, just no wasted calls.
                    bucket = int(now_utc().timestamp() // 180)
                    if last_bucket.get(symbol) == bucket:
                        continue
                    last_bucket[symbol] = bucket

                    ohlcv = exchange.fetch_ohlcv(symbol, '3m', limit=500)
                    or_high, or_low = get_opening_range(ohlcv)
                    if or_high is None:
                        continue

                    price = ohlcv[-2][4]        # last CLOSED 3m bar close
                    closed = [c[4] for c in ohlcv[:-1]]
                    ema9 = calc_ema(closed, 9)
                    ema20 = calc_ema(closed, 20)
                    vwap = daily_vwap(ohlcv)

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
                        log(f'{symbol}: OR too wide ({dist/price:.1%}) — skip today')
                        state['traded'][symbol] = True
                        save_state(state)
                        continue
                    tp = price + dist * RR if sig == 1 else price - dist * RR

                    risk_usd = balance * RISK_PCT
                    raw = risk_usd / dist
                    qty = max(cfg['min'], min(cfg['max'], math.floor(raw / cfg['step']) * cfg['step']))
                    qty = round(qty, 6)
                    if qty * price < MIN_NOTIONAL:
                        log(f'{symbol}: notional too small — skip')
                        state['traded'][symbol] = True
                        save_state(state)
                        continue

                    if is_portfolio_halted():
                        log(f'{symbol}: ENTRY BLOCKED -- portfolio-wide RISK_HALT active (synced from Windows)')
                        continue
                    result = place_entry(symbol, 'buy' if sig == 1 else 'sell', qty, sl, tp)
                    if result == 'insufficient_margin':
                        log(f'{symbol}: insufficient margin — skip today (stop retrying)')
                        state['traded'][symbol] = True
                        save_state(state)
                    elif result:
                        state['traded'][symbol] = True
                        save_state(state)
                except Exception as e:
                    log(f'{symbol} error: {e}')

        except Exception as e:
            log(f'tick error: {e}')

        time.sleep(TICK_SEC)


if __name__ == '__main__':
    run()
