"""
Scalp Bot v3.0 — Volume + ATR + News Filter
Strategy: M5 RSI momentum + EMA9/21 + VWAP
BUY:  RSI > 65  AND EMA9 > EMA21  AND price > VWAP  AND vol > avg  AND no news
SELL: RSI < 35  AND EMA9 < EMA21  AND price < VWAP  AND vol > avg  AND no news
SL:   max(fixed, 1.5×ATR)   TP: SL × 3   (1:3 RR always)
Exit: TP/SL hit  OR  60 min time exit
"""
import ccxt, os, time, math, calendar as _cal
import requests as _req
from datetime import datetime, timezone
from dotenv import load_dotenv
from oracle_entry_freeze import entry_freeze_status

load_dotenv('/home/ubuntu/.env')

exchange = ccxt.bybit({
    'apiKey':  os.getenv('BYBIT_API_KEY'),
    'secret':  os.getenv('BYBIT_API_SECRET'),
    'options': {'defaultType': 'linear'},
    'timeout': 10000,
    'enableRateLimit': True,
})

SYMBOLS = {
    'BTC/USDT:USDT':  {'sl': 150.0,  'tp': 450.0,  'step': 0.001, 'min': 0.001, 'max': 0.1},
    'ETH/USDT:USDT':  {'sl': 40.0,   'tp': 120.0,  'step': 0.01,  'min': 0.01,  'max': 5.0},
    'SOL/USDT:USDT':  {'sl': 1.0,    'tp': 3.0,    'step': 0.1,   'min': 0.1,   'max': 100.0},
    'WIF/USDT:USDT':  {'sl': 0.003,  'tp': 0.009,  'step': 1,     'min': 1,     'max': 2000},
    'DOGE/USDT:USDT': {'sl': 0.0012, 'tp': 0.0036, 'step': 1,     'min': 1,     'max': 5000},
}  # 2026-07-01: BTC/SOL restored — earlier removal was based on crypto_bot (old swing bot) history, not scalp_bot. Under scalp_bot itself (since 06-30 19:35) BTC/SOL are profitable/breakeven on tiny samples. WIF/DOGE added as they also perform well.

RSI_BUY       = 35
RSI_SELL      = 65
MAX_OPEN      = 3
RISK_PCT      = 0.015
TICK_SEC      = 30
MAX_AGE       = 20 * 60
COOLDOWN_SECS = 5 * 60
PENDING_TIMEOUT = 5 * 60

# ATR config
ATR_SL_MULT = 0.8   # SL = max(fixed, ATR × 1.5)
ATR_MAX_MULT = 3.0  # cap: SL ≤ fixed × 3  (avoid insane stops)

# News filter
NEWS_BLOCK_MINS = 15
NEWS_CACHE_TTL  = 3600
_news_ts   = []
_news_upd  = 0

cooldowns      = {}
positions      = {}
pending_orders = {}
partials       = {}


# ── Helpers ──────────────────────────────────────────────────────

def now_utc():
    return datetime.now(timezone.utc).strftime('%H:%M:%S')

def safe_float(v, d=0.0):
    try: return float(v) if v is not None else d
    except: return d

def get_balance():
    try:
        b = exchange.fetch_balance()
        return float(b.get('USDT', {}).get('free', 0))
    except: return 0.0


def central_freeze_active():
    freeze = entry_freeze_status('scalp_bot', account='BAA')
    if freeze.get('frozen'):
        print(f"ENTRY BLOCKED -- central freeze active ({freeze.get('reason')})")
        return True
    return False


# ── Indicators ───────────────────────────────────────────────────

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    g, l = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        g.append(max(d, 0)); l.append(max(-d, 0))
    ag = sum(g[:period]) / period
    al = sum(l[:period]) / period
    for i in range(period, len(g)):
        ag = (ag * (period-1) + g[i]) / period
        al = (al * (period-1) + l[i]) / period
    return 100 - (100 / (1 + ag/al)) if al else 100

def calc_ema(closes, period):
    if len(closes) < period:
        return closes[-1] if closes else 0
    k = 2 / (period + 1)
    e = closes[0]
    for c in closes[1:]: e = c*k + e*(1-k)
    return e

def calc_atr(ohlcv, period=14):
    if len(ohlcv) < period + 1:
        return None
    trs = []
    for i in range(1, len(ohlcv)):
        h, l, pc = ohlcv[i][2], ohlcv[i][3], ohlcv[i-1][4]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr*(period-1) + tr) / period
    return atr

def calc_vwap(ohlcv):
    now = datetime.now(timezone.utc)
    midnight = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()*1000)
    today = [c for c in ohlcv if c[0] >= midnight] or ohlcv[-20:]
    tpv = sum(((c[2]+c[3]+c[4])/3)*c[5] for c in today)
    vol = sum(c[5] for c in today)
    return tpv/vol if vol else 0


# ── News filter ──────────────────────────────────────────────────

def is_bullish_engulfing(ohlcv):
    """Last CLOSED candle (index -2) bullish-engulfs the one before it (index -3)."""
    if len(ohlcv) < 3:
        return False
    prev, cur = ohlcv[-3], ohlcv[-2]
    prev_open, prev_close = prev[1], prev[4]
    cur_open, cur_close = cur[1], cur[4]
    return prev_close < prev_open and cur_close > cur_open and cur_open <= prev_close and cur_close >= prev_open

def is_bearish_engulfing(ohlcv):
    """Last CLOSED candle (index -2) bearish-engulfs the one before it (index -3)."""
    if len(ohlcv) < 3:
        return False
    prev, cur = ohlcv[-3], ohlcv[-2]
    prev_open, prev_close = prev[1], prev[4]
    cur_open, cur_close = cur[1], cur[4]
    return prev_close > prev_open and cur_close < cur_open and cur_open >= prev_close and cur_close <= prev_open

def _et_offset():
    return 4 if 3 <= datetime.now().month <= 11 else 5

def _parse_ff_ts(date_str, time_str):
    try:
        t = (time_str or '').strip().upper()
        if not t or t in ('ALL DAY', 'TENTATIVE'):
            t = '12:00PM'
        dt = datetime.strptime(f'{date_str} {t}', '%b %d, %Y %I:%M%p')
        return _cal.timegm(dt.timetuple()) + _et_offset() * 3600
    except:
        return None

def refresh_news():
    global _news_ts, _news_upd
    try:
        r = _req.get('https://nfs.faireconomy.media/ff_calendar_thisweek.json', timeout=8)
        events = r.json()
        _news_ts = [ts for e in events
                    if e.get('country') == 'USD' and e.get('impact') == 'High'
                    for ts in [_parse_ff_ts(e.get('date',''), e.get('time',''))]
                    if ts]
        _news_upd = time.time()
        print(f'[NEWS] {len(_news_ts)} high-impact USD events this week')
    except Exception as ex:
        print(f'[NEWS] fetch failed (non-fatal): {ex}')

def is_news_blocked():
    if time.time() - _news_upd > NEWS_CACHE_TTL:
        refresh_news()
    blk = NEWS_BLOCK_MINS * 60
    blocked = any(abs(time.time() - ts) < blk for ts in _news_ts)
    if blocked:
        mins = min(abs(time.time()-ts) for ts in _news_ts) / 60
        print(f'[NEWS] BLOCKED — event in {mins:.0f} min')
    return blocked


# ── Signal ───────────────────────────────────────────────────────

def get_signal(symbol, cfg):
    """Returns (signal, rsi, price, sl_dist, tp_dist)."""
    try:
        ohlcv  = exchange.fetch_ohlcv(symbol, '5m', limit=60)
        closes = [c[4] for c in ohlcv]
        price  = closes[-1]
        closed = closes[:-1]

        rsi   = calc_rsi(closed)
        ema9  = calc_ema(closed, 9)
        ema21 = calc_ema(closed, 21)
        vwap  = calc_vwap(ohlcv)
        atr   = calc_atr(ohlcv)

        # Volume filter — must beat 20-bar average (kills choppy entries)
        vols  = [c[5] for c in ohlcv]
        avg_v = sum(vols[-21:-1]) / 20
        if vols[-2] < avg_v:
            return None, rsi, price, 0, 0

        # ATR-based SL/TP — adapts to current volatility
        if atr:
            sl_dist = min(max(cfg['sl'], atr * ATR_SL_MULT), cfg['sl'] * ATR_MAX_MULT)
        else:
            sl_dist = cfg['sl']
        tp_dist = sl_dist * 3.0  # RR 1:3

        # Candlestick confirmation added 2026-07-02 (user request — worked well for them before):
        # require a bullish/bearish engulfing on the last closed candle, not just indicator alignment.
        if rsi > RSI_SELL and ema9 > ema21 and (not vwap or price > vwap) and is_bullish_engulfing(ohlcv):
            return 'BUY',  rsi, price, sl_dist, tp_dist
        if rsi < RSI_BUY  and ema9 < ema21 and (not vwap or price < vwap) and is_bearish_engulfing(ohlcv):
            return 'SELL', rsi, price, sl_dist, tp_dist

        return None, rsi, price, 0, 0
    except Exception as e:
        print(f'signal error {symbol}: {e}')
        return None, 50, None, 0, 0


# ── Orders ───────────────────────────────────────────────────────

def calc_qty(cfg, balance, sl_dist):
    risk_usd = balance * RISK_PCT
    raw  = risk_usd / sl_dist
    step = cfg['step']
    qty  = math.floor(raw / step) * step
    return max(cfg['min'], min(cfg['max'], round(qty, 6)))

def check_pending_orders():
    for symbol in list(pending_orders.keys()):
        po = pending_orders[symbol]
        try:
            order  = exchange.fetch_order(po['id'], symbol)
            status = order.get('status', '')
            if status == 'closed':
                positions[symbol] = {'entry_time': time.time(), 'side': po['side']}
                print(f'FILLED {symbol[:12]} {po["side"].upper()}')
                del pending_orders[symbol]
            elif status in ('canceled', 'rejected'):
                print(f'CANCELLED {symbol[:12]}')
                del pending_orders[symbol]
            elif time.time() - po['placed_at'] > PENDING_TIMEOUT:
                if central_freeze_active():
                    po['placed_at'] = time.time()
                    continue
                exchange.cancel_order(po['id'], symbol)
                p = safe_float(exchange.fetch_ticker(symbol).get('last'))
                exchange.create_order(symbol, 'market', po['side'], po['qty'], params={
                    'stopLoss':   str(round(float(po['sl']), 4)), 'slTriggerBy': 'MarkPrice',
                    'takeProfit': str(round(float(po['tp']), 4)), 'tpTriggerBy': 'MarkPrice',
                })
                positions[symbol] = {'entry_time': time.time(), 'side': po['side']}
                print(f'MARKET FALLBACK {symbol[:12]} {po["side"].upper()} @{p}')
                del pending_orders[symbol]
        except Exception as e:
            po2 = pending_orders.get(symbol, {})
            if po2 and time.time() - po2.get('placed_at', 0) > PENDING_TIMEOUT * 2:
                del pending_orders[symbol]
                print(f'FORCE REMOVE {symbol}: {e}')
            else:
                print(f'pending error {symbol}: {e}')

def place_order(symbol, side, qty, sl, tp, price):
    try:
        if central_freeze_active():
            return False
        offset = 0.0005
        lp = round(price*(1-offset) if side=='buy' else price*(1+offset), 6)
        order = exchange.create_order(symbol, 'limit', side, qty, lp, params={
            'stopLoss':    str(round(float(sl), 4)), 'slTriggerBy': 'MarkPrice',
            'takeProfit':  str(round(float(tp), 4)), 'tpTriggerBy': 'MarkPrice',
            'timeInForce': 'GTC',
        })
        pending_orders[symbol] = {'id': order['id'], 'placed_at': time.time(),
                                   'side': side, 'qty': qty, 'sl': sl, 'tp': tp}
        print(f'SCALP {side.upper()} {symbol} qty={qty} @{lp} SL={sl:.4f} TP={tp:.4f}')
        return True
    except Exception as e:
        err = str(e)
        if '110007' in err or 'ab not enough' in err:
            cooldowns[symbol] = time.time() + 10*60
            print(f'SKIP {symbol}: margin — cooldown 10min')
        elif '10006' in err or 'rate limit' in err.lower():
            time.sleep(10)
        else:
            print(f'order error {symbol}: {e}')
        return False

def manage_trailing_sl(open_map):
    for symbol, pos in open_map.items():
        if symbol not in SYMBOLS:
            continue
        try:
            entry = safe_float(pos.get('entryPrice'))
            mark  = safe_float(pos.get('markPrice'))
            sl    = safe_float(pos.get('stopLoss'))
            tp    = safe_float(pos.get('takeProfit'))
            side  = pos.get('side', '')
            if not all([entry>0, mark>0, sl>0, tp>0]):
                continue

            if side == 'long':
                tp_dist  = tp - entry
                cur_move = mark - entry
                be_sl    = round(entry, 6)
                lock_sl  = round(entry + tp_dist * 0.5, 6)
                should_lock, should_be = sl < lock_sl, sl < be_sl
            else:
                tp_dist  = entry - tp
                cur_move = entry - mark
                be_sl    = round(entry, 6)
                lock_sl  = round(entry - tp_dist * 0.5, 6)
                should_lock, should_be = sl > lock_sl, sl > be_sl

            if tp_dist <= 0 or cur_move <= 0:
                continue
            progress = cur_move / tp_dist

            # Partial close 33% at 40% TP
            if progress >= 0.40 and symbol not in partials:
                try:
                    total = safe_float(pos.get('contracts'))
                    step  = SYMBOLS[symbol]['step']
                    cqty  = math.floor(total * 0.33 / step) * step
                    if cqty >= step:
                        cs = 'sell' if side=='long' else 'buy'
                        exchange.create_order(symbol, 'market', cs, cqty,
                                              params={'reduceOnly': True})
                        partials[symbol] = True
                        print(f'PARTIAL33 {symbol[:12]} {cqty} @{progress:.0%}TP')
                except Exception as pe:
                    print(f'partial error {symbol}: {pe}')

            new_sl, reason = None, ''
            if progress >= 0.65 and should_lock:
                new_sl = lock_sl; reason = f'65%→lock50% p={progress:.0%}'
            elif progress >= 0.25 and should_be:
                new_sl = be_sl;   reason = f'25%→BE p={progress:.0%}'
            if new_sl:
                sym = symbol.replace('/USDT:USDT', 'USDT')
                exchange.privatePostV5PositionTradingStop({
                    'category': 'linear', 'symbol': sym,
                    'stopLoss': str(new_sl), 'slTriggerBy': 'MarkPrice', 'positionIdx': '0',
                })
                print(f'TRAIL {symbol[:12]}: {sl:.4f}→{new_sl:.4f} [{reason}]')
        except Exception as e:
            print(f'trail error {symbol}: {e}')

def load_existing_orders():
    for symbol in SYMBOLS:
        try:
            for o in exchange.fetch_open_orders(symbol):
                if o['status'] == 'open' and symbol not in pending_orders:
                    pending_orders[symbol] = {
                        'id': o['id'], 'placed_at': time.time()-240,
                        'side': o['side'], 'qty': o['amount'], 'sl': 0, 'tp': 0,
                    }
                    print(f'Loaded stale order {symbol} → expires ~60s')
        except:
            pass


# ── Main loop ────────────────────────────────────────────────────

def run():
    print(f'Scalp Bot v3.0 — {now_utc()} UTC')
    print(f'Pairs: {list(SYMBOLS.keys())}')
    print(f'RSI BUY<{RSI_BUY} SELL>{RSI_SELL} | MaxOpen={MAX_OPEN} | Risk={RISK_PCT*100:.1f}%')
    print(f'ATR SL×{ATR_SL_MULT} TP×{ATR_SL_MULT*3} cap×{ATR_MAX_MULT} | News ±{NEWS_BLOCK_MINS}min')
    load_existing_orders()
    refresh_news()

    while True:
        try:
            balance = get_balance()
            if balance < 15.0:
                print(f'CIRCUIT BREAKER: {balance:.2f} < 15 — stopped')
                break

            all_pos    = exchange.fetch_positions()
            open_map   = {p['symbol']: p for p in all_pos if float(p.get('contracts',0)) > 0}
            open_count = len([s for s in SYMBOLS if s in open_map])

            print(f'\n--- scalp {now_utc()} | bal={balance:.2f} | open={open_count} ---')

            check_pending_orders()
            manage_trailing_sl(open_map)

            # Time-based exit (60 min)
            for sym, pos in list(open_map.items()):
                if sym in positions:
                    age = time.time() - positions[sym]['entry_time']
                    if age > MAX_AGE:
                        cs  = 'sell' if pos.get('side')=='long' else 'buy'
                        qty = safe_float(pos.get('contracts'))
                        try:
                            exchange.create_order(sym, 'market', cs, qty,
                                                  params={'reduceOnly': True})
                            pnl = safe_float(pos.get('unrealizedPnl'))
                            print(f'TIME EXIT {sym} {int(age/60)}min pnl={pnl:+.2f}')
                            cooldowns[sym] = time.time() + COOLDOWN_SECS
                            del positions[sym]
                        except Exception as e:
                            print(f'time exit error {sym}: {e}')

            # Track closed positions
            for sym in list(positions.keys()):
                if sym not in open_map:
                    cooldowns[sym] = time.time() + COOLDOWN_SECS
                    print(f'CLOSED {sym} — cooldown 10min')
                    del positions[sym]
                    partials.pop(sym, None)

            # News block
            if is_news_blocked():
                time.sleep(TICK_SEC)
                continue

            # Scan entries
            for symbol, cfg in SYMBOLS.items():
                try:
                    if symbol in open_map:
                        pnl = safe_float(open_map[symbol].get('unrealizedPnl'))
                        print(f'{symbol[:12]}: open pnl={pnl:+.2f}')
                        continue
                    if open_count >= MAX_OPEN:
                        continue
                    if symbol in pending_orders:
                        print(f'{symbol[:12]}: pending')
                        continue
                    if symbol in cooldowns and time.time() < cooldowns[symbol]:
                        print(f'{symbol[:12]}: cooldown {int(cooldowns[symbol]-time.time())}s')
                        continue

                    sig, rsi, price, sl_dist, tp_dist = get_signal(symbol, cfg)
                    if not price:
                        continue

                    if sig == 'BUY':
                        sl  = round(price - sl_dist, 6)
                        tp  = round(price + tp_dist, 6)
                        qty = calc_qty(cfg, balance, sl_dist)
                        if place_order(symbol, 'buy', qty, sl, tp, price):
                            positions[symbol] = {'entry_time': time.time(), 'side': 'long'}
                            open_count += 1
                    elif sig == 'SELL':
                        sl  = round(price + sl_dist, 6)
                        tp  = round(price - tp_dist, 6)
                        qty = calc_qty(cfg, balance, sl_dist)
                        if place_order(symbol, 'sell', qty, sl, tp, price):
                            positions[symbol] = {'entry_time': time.time(), 'side': 'short'}
                            open_count += 1
                    else:
                        print(f'{symbol[:12]}: RSI={rsi:.1f} waiting')

                except Exception as e:
                    print(f'{symbol} error: {e}')

        except Exception as e:
            print(f'tick error: {e}')

        time.sleep(TICK_SEC)


if __name__ == '__main__':
    run()
