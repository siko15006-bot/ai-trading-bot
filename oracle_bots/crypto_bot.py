"""
Crypto Bot v3.18 BTC_SIMPLE — Bybit Exchange (USDT Perps)
===============================================
Signal: RSI-14 M15 (Wilder) + EMA200 D1 trend filter
BUY:  RSI < 35  AND price > EMA200(D1)   ← D1 macro uptrend only
SELL: RSI > thr AND price < EMA200(D1)   ← D1 macro downtrend only
      AND price < EMA50(M15) AND price < EMA50(H1) AND MACD(H1) < Signal(H1)
BE:   Move SL to entry when PnL >= 50% TP

New in v3.7:
  1. EMA200(D1) trend filter — prevents LONGs in macro downtrends and vice versa
     BUY  only when price > EMA200(D1)  (macro uptrend)
     SELL only when price < EMA200(D1)  (macro downtrend)
  2. All v3.6 features retained
"""
import ccxt, os, time, math, json
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

RISK_PCT  = 0.005   # 2% max risk per trade
ADX_MIN   = 20      # H1 trend-strength gate — skip ranging chop (added after 44 flat MAX_HOLD trades 2026-07-04)
MAX_OPEN  = 3

# Swing bot: major pairs only (BTC/ETH/SOL/XAU)
# Altcoins (XRP/SUI/DOGE/WIF/HYPE/AAVE) handled exclusively by scalp bot (scalp_bot.py)
SYMBOLS = {
    'BTC/USDT:USDT': {'sl': 150,  'tp': 300,  'lev': 3, 'step': 0.001, 'min': 0.001, 'max': 0.5},
    'ETH/USDT:USDT': {'sl': 40,   'tp': 120,  'lev': 3, 'step': 0.01,  'min': 0.01,  'max': 10.0},
    'SOL/USDT:USDT': {'sl': 0.8,  'tp': 2.4,  'lev': 3, 'step': 0.1,   'min': 0.1,   'max': 100.0},
}  # XAU removed — LOWVOL every tick

# Per-symbol SELL RSI threshold (backtest result: volatile pairs need higher threshold)
SELL_RSI_THR = {
    'BTC/USDT:USDT': 65,
    'ETH/USDT:USDT': 65,
    'SOL/USDT:USDT': 65,
    'XAU/USDT:USDT': 65,
    'HYPE/USDT:USDT': 65,   # strong buyback = upward bias, standard threshold
    'AAVE/USDT:USDT': 65,
}

positions     = {}
cooldowns     = {}          # symbol -> unix timestamp when cooldown expires
pending_orders = {}         # symbol -> {id, placed_at, side, qty, sl, tp, limit}
LIMIT_OFFSET  = 0.001       # 0.1% offset from current price for limit entry
BTC_BUY_ZONES = [
    (49000, 51000, 'Zone1_PrevATH',      47500, 65000),
    (39000, 41500, 'Zone2_GoldenPocket', 37000, 55000),
]
ZONE_RISK_PCT  = 0.005   # 2% max risk per trade5
BTC_BUY_ZONES = [
    # (price_low, price_high, name, fixed_sl, fixed_tp)
    (49000, 51000, 'Zone1_PrevATH',      47500, 65000),  # SL below 9K floor, TP 5K
    (39000, 41500, 'Zone2_GoldenPocket', 37000, 55000),  # SL below 9K floor, TP 5K
]
ZONE_RISK_PCT  = 0.005   # 2% max risk per trade5  # 1.5% risk for BTC zone entries
COOLDOWN_SECS = 4 * 90     # 4 bars × 90s per bar = 6 minutes
SKIP_HOURS_UTC = set(range(22, 24)) | set(range(0, 2))


def now_utc():
    return datetime.now(timezone.utc).strftime('%H:%M:%S UTC')


def current_hour_utc():
    return datetime.now(timezone.utc).hour


def safe_float(val, default=0.0):
    try:
        return float(val) if val is not None else default
    except Exception:
        return default


def get_balance():
    try:
        bal = exchange.fetch_balance()
        return float(bal.get('USDT', {}).get('free', 0))
    except Exception:
        return 0.0


MIN_NOTIONAL = 5.0   # Bybit minimum order value in USDT

MAX_RISK_PCT = 0.05   # absolute ceiling: 5% risk per trade

def set_leverage_safe(symbol: str, leverage: int) -> bool:
    """Set isolated margin + leverage. Gracefully ignores PM mode (110077)."""
    # Try to set isolated margin first
    try:
        exchange.set_margin_mode('isolated', symbol)
    except Exception as e:
        if '110077' not in str(e) and '3400045' not in str(e) and '110073' not in str(e) and '3400111' not in str(e):
            print(f'set_margin_mode isolated {symbol}: {e}')
        # PM mode or already isolated — continue

    # Then set leverage
    try:
        exchange.set_leverage(leverage, symbol)
        return True
    except Exception as e:
        if '110077' in str(e) or '110073' in str(e):
            return True   # PM/negative equity — leverage managed at account level
        print(f'set_leverage {symbol} {leverage}x: {e}')
        return False


def calc_qty(cfg, balance, price=None, risk=None):
    """
    Strict 2% risk sizing.
    Returns (qty, skip_reason) — skip_reason is None on success.
    """
    max_risk = balance * MAX_RISK_PCT                  # hard cap: 5% regardless of risk arg
    risk_usd = min(balance * (risk or RISK_PCT), max_risk)

    if cfg['sl'] <= 0 or price is None or price <= 0:
        return cfg['min'], None                        # fallback to minimum

    # qty = risk_usd / sl_distance (in price)
    raw  = risk_usd / cfg['sl']
    step = cfg['step']
    qty  = math.floor(raw / step) * step
    qty  = round(qty, 6)
    qty  = max(cfg['min'], min(cfg['max'], qty))

    # Minimum notional value check
    notional = qty * price
    if notional < MIN_NOTIONAL:
        return 0.0, f"notional  < min  at {price:.2f}"

    return qty, None


# Wilder smoothed RSI — matches TradingView
def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    return round(100 - (100 / (1 + ag / al)), 1) if al != 0 else 100.0


def calc_ema(closes, period=50):
    k = 2 / (period + 1)
    ema = closes[0]
    for c in closes[1:]:
        ema = c * k + ema * (1 - k)
    return ema


def calc_adx(highs, lows, closes, period=14):
    """Wilder ADX — trend-strength gate, same convention as ema_adx_bot/ai_trading_manager."""
    n = len(closes)
    if n < period * 2:
        return 0.0
    plus_dm, minus_dm, tr = [], [], []
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    def _wilder_smooth(vals, period):
        sm = sum(vals[:period])
        out = [sm]
        for v in vals[period:]:
            sm = sm - (sm / period) + v
            out.append(sm)
        return out
    tr_s = _wilder_smooth(tr, period)
    pdm_s = _wilder_smooth(plus_dm, period)
    mdm_s = _wilder_smooth(minus_dm, period)
    dx = []
    for trv, pdv, mdv in zip(tr_s, pdm_s, mdm_s):
        if trv == 0:
            dx.append(0.0)
            continue
        pdi = 100 * pdv / trv
        mdi = 100 * mdv / trv
        s = pdi + mdi
        dx.append(100 * abs(pdi - mdi) / s if s else 0.0)
    if len(dx) < period:
        return round(dx[-1], 1) if dx else 0.0
    adx = sum(dx[:period]) / period
    for d in dx[period:]:
        adx = (adx * (period - 1) + d) / period
    return round(adx, 1)


def calc_macd(closes, fast=12, slow=26, sig=9):
    """Returns (macd_val, signal_val) for the last bar."""
    def _ema(cs, span):
        k = 2 / (span + 1)
        e = cs[0]
        for c in cs[1:]:
            e = c * k + e * (1 - k)
        return e

    # Need at least slow+sig bars
    if len(closes) < slow + sig:
        return 0.0, 0.0

    # Compute full MACD line
    k12 = 2 / (fast + 1)
    k26 = 2 / (slow + 1)
    e12 = closes[0]; e26 = closes[0]
    macd_line = []
    for c in closes:
        e12 = c * k12 + e12 * (1 - k12)
        e26 = c * k26 + e26 * (1 - k26)
        macd_line.append(e12 - e26)

    # Signal line EMA over macd_line
    k9 = 2 / (sig + 1)
    sig_val = macd_line[0]
    for m in macd_line[1:]:
        sig_val = m * k9 + sig_val * (1 - k9)

    return macd_line[-1], sig_val


def calc_vwap(ohlcv):
    """Daily VWAP from M15 candles — resets at 00:00 UTC each day."""
    from datetime import timezone
    import datetime
    now_utc = datetime.datetime.now(timezone.utc)
    midnight = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    midnight_ms = int(midnight.timestamp() * 1000)
    today = [c for c in ohlcv if c[0] >= midnight_ms]
    if len(today) < 2:
        today = ohlcv[-20:]  # fallback: last 20 bars
    cum_tp_vol = sum(((c[2]+c[3]+c[4])/3) * c[5] for c in today)
    cum_vol    = sum(c[5] for c in today)
    return cum_tp_vol / cum_vol if cum_vol > 0 else 0


def get_signal(symbol):
    try:
        ohlcv_m15  = exchange.fetch_ohlcv(symbol, '15m', limit=71)
        closes_m15 = [c[4] for c in ohlcv_m15]
        volumes    = [c[5] for c in ohlcv_m15]
        price      = closes_m15[-1]
        closed_cls = closes_m15[:-1]
        rsi        = calc_rsi(closed_cls)
        avg_vol = sum(volumes[-21:-1]) / 20
        if volumes[-2] < avg_vol * 0.3:
            return 'LOWVOL', rsi, price
        time.sleep(0.8)
        ohlcv_h1   = exchange.fetch_ohlcv(symbol, '1h', limit=80)
        closes_h1  = [c[4] for c in ohlcv_h1]
        highs_h1   = [c[2] for c in ohlcv_h1]
        lows_h1    = [c[3] for c in ohlcv_h1]
        ema20_h1   = calc_ema(closes_h1, 20)
        ema50_h1   = calc_ema(closes_h1, 50)
        ema20_prev = calc_ema(closes_h1[:-4], 20)
        adx_h1     = calc_adx(highs_h1, lows_h1, closes_h1)
        rising     = ema20_h1 > ema20_prev
        falling    = ema20_h1 < ema20_prev
        if ema20_h1 > ema50_h1 and rising and price > ema20_h1 and rsi < 72 and adx_h1 >= ADX_MIN:
            return 'BUY', rsi, price
        if ema20_h1 < ema50_h1 and falling and price < ema20_h1 and rsi > 28 and adx_h1 >= ADX_MIN:
            return 'SELL', rsi, price
        reason = f'EMA20={ema20_h1:.2f} EMA50={ema50_h1:.2f} rising={rising} ADX={adx_h1} RSI={rsi}'
        return ('REASON:' + reason), rsi, price
    except Exception as e:
        print(f'signal error {symbol}: {e}')
        return None, 50, None


def set_sl(symbol, sl_price):
    try:
        mkt = exchange.market(symbol)
        exchange.private_post_v5_position_trading_stop({
            'symbol':      mkt['id'],
            'stopLoss':    str(round(sl_price, 4)),
            'positionIdx': '0',
            'category':    'linear',
            'slTriggerBy': 'LastPrice',
        })
        return True
    except Exception as e:
        print(f'set_sl error {symbol}: {e}')
        return False


def place_order(symbol, side, qty, sl_price, tp_price, price=None):
    # Skip if we already have a pending limit order for this symbol
    if symbol in pending_orders:
        print(f'{symbol[:10]}: pending order already exists, skipping')
        return False
    # Set safe leverage before placing
    lev = SYMBOLS.get(symbol, {}).get('lev', 3)
    set_leverage_safe(symbol, lev)
    try:
        if price and LIMIT_OFFSET > 0:
            if side == 'buy':
                lp = round(price * (1 - LIMIT_OFFSET), 6)
            else:
                lp = round(price * (1 + LIMIT_OFFSET), 6)
            order = exchange.create_order(symbol, 'limit', side, qty, lp, params={
                'stopLoss': str(round(float(sl_price), 4)), 'slTriggerBy': 'MarkPrice',
                'takeProfit': str(round(float(tp_price), 4)), 'tpTriggerBy': 'MarkPrice',
                'timeInForce': 'GTC',
            })
            oid = order.get('id', '')
            pending_orders[symbol] = {'id': oid, 'placed_at': time.time(), 'side': side,
                                       'qty': qty, 'sl': sl_price, 'tp': tp_price, 'limit': lp}
            print(f'LIMIT {side} {symbol} qty={qty} @{lp} SL={sl_price} TP={tp_price} id={oid}')
        else:
            exchange.create_order(symbol, 'market', side, qty, params={
                'stopLoss': str(round(float(sl_price), 4)), 'slTriggerBy': 'MarkPrice',
                'takeProfit': str(round(float(tp_price), 4)), 'tpTriggerBy': 'MarkPrice',
            })
            print(f'ENTRY {side} {symbol} qty={qty} SL={sl_price} TP={tp_price}')
        return True
    except Exception as e:
        err = str(e)
        print(f'order error {symbol}: {err[:200]}')
        if '110007' in err or 'ab not enough' in err or '3400111' in err:
            cooldowns[symbol] = time.time() + 5 * 60
            print(f'SKIP {symbol}: margin fail - cooldown 5min')
        elif '10006' in err or 'rate limit' in err.lower():
            print(f'RATE LIMIT - sleeping 15s')
            time.sleep(15)
        return False


def check_pending_orders():
    """Check if limit orders filled or expired (5 min). Cancel expired, market-fill them."""
    now = time.time()
    to_remove = []
    for sym, od in list(pending_orders.items()):
        age = now - od['placed_at']
        try:
            open_orders = exchange.fetch_open_orders(sym)
            still_open = any(o.get('id') == od['id'] for o in open_orders)
            if not still_open:
                print(f'LIMIT FILLED {sym} side={od["side"]} @{od["limit"]}')
                to_remove.append(sym)
            elif age > 300:
                # 5 minutes — cancel and market
                try:
                    exchange.cancel_order(od['id'], sym)
                    print(f'LIMIT TIMEOUT {sym} → cancelling, switching to market')
                except Exception as ce:
                    print(f'cancel error {sym}: {ce}')
                try:
                    exchange.create_order(sym, 'market', od['side'], od['qty'], params={
                        'stopLoss': str(round(float(od['sl']), 4)), 'slTriggerBy': 'MarkPrice',
                        'takeProfit': str(round(float(od['tp']), 4)), 'tpTriggerBy': 'MarkPrice',
                    })
                    print(f'MARKET FALLBACK {sym} side={od["side"]} qty={od["qty"]}')
                except Exception as me:
                    print(f'market fallback error {sym}: {me}')
                to_remove.append(sym)
        except Exception as e:
            print(f'check_pending {sym}: {e}')
    for s in to_remove:
        pending_orders.pop(s, None)


def close_position(symbol, side, qty):
    try:
        close_side = 'sell' if side == 'long' else 'buy'
        exchange.create_order(symbol, 'market', close_side, qty,
                              params={'reduceOnly': True})
        print(f'CLOSED {symbol} qty={qty}')
    except Exception as e:
        print(f'close error {symbol}: {e}')


def manage_position(symbol, pos):
    entry   = safe_float(pos.get('entryPrice'))
    side    = pos.get('side', 'long')
    qty     = safe_float(pos.get('contracts'))
    cfg     = SYMBOLS[symbol]
    tp_dist = cfg['tp']
    price   = safe_float(pos.get('markPrice') or pos.get('lastPrice'), entry)
    if entry == 0 or qty == 0:
        return

    pnl_pts = (price - entry) if side == 'long' else (entry - price)
    pnl_pct = pnl_pts / tp_dist if tp_dist else 0

    if symbol not in positions:
        sl = entry - cfg['sl'] if side == 'long' else entry + cfg['sl']
        tp = entry + tp_dist   if side == 'long' else entry - tp_dist
        positions[symbol] = {'side': side, 'entry': entry, 'sl': sl, 'tp': tp,
                             'qty': qty, 'be_done': False}

    state = positions[symbol]
    if pnl_pct >= 0.5 and not state['be_done']:
        if set_sl(symbol, entry):
            print(f'BE: SL -> entry {entry} on {symbol}')
            state['be_done'] = True
            state['sl'] = entry


def in_cooldown(symbol) -> bool:
    """Returns True if symbol is still in post-exit cooldown."""
    exp = cooldowns.get(symbol, 0)
    return time.time() < exp


def set_cooldown(symbol):
    cooldowns[symbol] = time.time() + COOLDOWN_SECS
    print(f'Cooldown {symbol} for {COOLDOWN_SECS}s')


STATUS_FILE = '/tmp/oracle_status.json'

def write_status(balance, open_map):
    try:
        pos_list = []
        total_pnl = 0.0
        for sym, p in open_map.items():
            pnl = safe_float(p.get('unrealizedPnl'))
            total_pnl += pnl
            pos_list.append({
                'symbol': sym.replace('/USDT:USDT', ''),
                'side':   p.get('side', '?'),
                'entry':  safe_float(p.get('entryPrice')),
                'price':  safe_float(p.get('markPrice') or p.get('lastPrice')),
                'pnl':    round(pnl, 2),
            })
        with open(STATUS_FILE, 'w') as f:
            json.dump({
                'version':    '3.8',
                'balance':    round(balance, 2),
                'pnl_total':  round(total_pnl, 2),
                'positions':  pos_list,
                'open_count': len(pos_list),
                'last_tick':  now_utc(),
                'ts':         int(time.time()),
            }, f)
    except Exception as e:
        print(f'write_status error: {e}')



def manage_trailing_sl(open_map):
    """Move SL to breakeven at 50% TP, lock 50% profit at 75% TP."""
    for symbol, pos in open_map.items():
        try:
            entry  = safe_float(pos.get('entryPrice'))
            mark   = safe_float(pos.get('markPrice'))
            sl     = safe_float(pos.get('stopLoss'))
            tp     = safe_float(pos.get('takeProfit'))
            side   = pos.get('side', '')
            if not all([entry > 0, mark > 0, sl > 0, tp > 0]):
                continue
            if side == 'long':
                tp_dist  = tp - entry
                cur_move = mark - entry
                be_sl    = round(entry, 6)
                lock_sl  = round(entry + tp_dist * 0.5, 6)
                should_lock = sl < lock_sl
                should_be   = sl < be_sl
            else:
                tp_dist  = entry - tp
                cur_move = entry - mark
                be_sl    = round(entry, 6)
                lock_sl  = round(entry - tp_dist * 0.5, 6)
                should_lock = sl > lock_sl
                should_be   = sl > be_sl
            if tp_dist <= 0 or cur_move <= 0:
                continue
            progress = cur_move / tp_dist
            new_sl, reason = None, ''
            if progress >= 0.65 and should_lock:
                new_sl = lock_sl
                reason = f'65%TP→lock50% progress={progress:.0%}'
            elif progress >= 0.25 and should_be:
                new_sl = be_sl
                reason = f'25%TP→BE progress={progress:.0%}'
            if new_sl:
                sym = symbol.replace('/USDT:USDT', 'USDT')
                exchange.privatePostV5PositionTradingStop({
                    'category':    'linear',
                    'symbol':      sym,
                    'stopLoss':    str(new_sl),
                    'slTriggerBy': 'MarkPrice',
                    'positionIdx': '0',
                })
                print(f'TRAIL_SL {symbol[:12]}: {sl:.4f}->{new_sl:.4f} [{reason}]')
        except Exception as e:
            print(f'trail_sl error {symbol}: {e}')

def run():
    print('Crypto Bot v3.18 BTC_SIMPLE — MACD(H1) SELL filter + per-symbol RSI thr + cooldown')
    while True:
        try:
            balance = get_balance()
            check_pending_orders()
            print(f'\n--- tick {now_utc()} | balance={balance:.2f} USDT ---')
            h = current_hour_utc()

            try:
                all_pos  = exchange.fetch_positions()
                open_map = {p['symbol']: p for p in all_pos
                            if p and safe_float(p.get('contracts')) > 0}
            except Exception as e:
                print(f'fetch_positions error: {e}')
                open_map = {}

            write_status(balance, open_map)

            open_count = len(open_map)

            for symbol, cfg in SYMBOLS.items():
                try:
                    pos = open_map.get(symbol)
                    if pos:
                        manage_position(symbol, pos)
                        # auto-close if held > 8 hours
                        opened = safe_float(pos.get('timestamp', 0)) / 1000
                        if opened > 0 and (time.time() - opened) > 28800:
                            close_side = 'sell' if pos.get('side') == 'long' else 'buy'
                            qty = safe_float(pos.get('contracts'))
                            try:
                                exchange.create_order(symbol, 'market', close_side, qty, params={'reduceOnly': True})
                                print(f'MAX_HOLD 8h: closed {symbol}')
                            except Exception as mhe:
                                print(f'max_hold close error {symbol}: {mhe}')
                        pnl = safe_float(pos.get('unrealizedPnl'))
                        # Set cooldown when position closes (detected next tick via absence)
                        if symbol in positions:
                            positions[symbol]['_was_open'] = True
                        side_ch = 'L' if pos['side'] == 'long' else 'S'
                        print(f'{symbol[:10]}: {side_ch} pnl={pnl:+.2f}')
                        continue

                    # Position just closed — start cooldown
                    if symbol in positions and positions[symbol].get('_was_open'):
                        set_cooldown(symbol)
                        if symbol in pending_orders:
                            try:
                                exchange.cancel_order(pending_orders[symbol]['id'], symbol)
                                print(f'Cancelled pending order {symbol} (position closed)')
                            except Exception:
                                pass
                            del pending_orders[symbol]
                        del positions[symbol]
                    elif symbol in positions:
                        del positions[symbol]

                    if open_count >= MAX_OPEN:
                        continue

                    if h in SKIP_HOURS_UTC and symbol != 'XAU/USDT:USDT':
                        continue

                    # Cooldown check
                    if in_cooldown(symbol):
                        remaining = int(cooldowns[symbol] - time.time())
                        print(f'{symbol[:10]}: cooldown {remaining}s')
                        continue

                    sig, rsi, price = get_signal(symbol)
                    time.sleep(1.0)  # rate limit spacing
                    if not price:
                        continue

                    if sig == 'LOWVOL':
                        print(f'{symbol[:10]}: LOWVOL RSI={rsi}')
                        continue

                    sell_thr = SELL_RSI_THR.get(symbol, 65)

                    if sig == 'BUY':
                        sl = round(price - cfg['sl'], 6)
                        tp = round(price + cfg['tp'], 6)
                        qty, skip_reason = calc_qty(cfg, balance, price)
                        if skip_reason:
                            print(f'SKIP {symbol}: {skip_reason}')
                            continue
                        if symbol == 'BTC/USDT:USDT':
                            for z_low, z_high, z_name, z_sl, z_tp in BTC_BUY_ZONES:
                                if z_low <= price <= z_high:
                                    sl = z_sl
                                    tp = z_tp
                                    sl_dist = max(price - z_sl, 100)
                                    risk_usd = balance * ZONE_RISK_PCT
                                    import math as _m
                                    raw = risk_usd / sl_dist
                                    qty = max(cfg['min'], min(cfg['max'], _m.floor(raw/cfg['step'])*cfg['step']))
                                    print(f'BTC ZONE ENTRY: {z_name} @ {price:.0f} SL={z_sl} TP={z_tp} qty={qty}')
                                    break
                        if place_order(symbol, 'buy', qty, sl, tp, price=price):
                            open_count += 1
                    elif sig == 'SELL':
                        sl = round(price + cfg['sl'], 6)
                        tp = round(price - cfg['tp'], 6)
                        qty, skip_reason = calc_qty(cfg, balance, price)
                        if skip_reason:
                            print(f'SKIP {symbol}: {skip_reason}')
                            continue
                        if place_order(symbol, 'sell', qty, sl, tp, price=price):
                            open_count += 1
                    elif sig and sig.startswith('REASON:'):
                        print(f'{symbol[:10]}: {sig[7:]}')
                    else:
                        print(f'{symbol[:10]}: RSI={rsi} waiting')

                except Exception as e:
                    print(f'{symbol} error: {e}')

        except Exception as e:
            print(f'tick error: {e}')

        time.sleep(90)


if __name__ == '__main__':
    run()
