"""
Spot Bot v1.1 — fixes: poll order for fill, guard div/zero, correct qty calc
"""
import ccxt, time, math
from datetime import datetime, timezone

API_KEY    = '<REDACTED_BYBIT_SPOT_API_KEY>'
API_SECRET = '<REDACTED_BYBIT_SPOT_API_SECRET>'

TRADE_USDT = 18.0
PROFIT_PCT = 0.020
STOP_PCT   = 0.015
RSI_BUY    = 55
RSI_SELL   = 45
TICK_SEC   = 30

SYMBOLS = ['SOL/USDT', 'ETH/USDT', 'BTC/USDT']

exchange = ccxt.bybit({
    'apiKey':  API_KEY,
    'secret':  API_SECRET,
    'options': {'defaultType': 'spot'},
    'enableRateLimit': True,
})

holdings = {}

def now_utc():
    return datetime.now(timezone.utc).strftime('%H:%M:%S')

def safe_float(v, d=0.0):
    try: return float(v) if v is not None else d
    except: return d

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period-1) + gains[i]) / period
        al = (al * (period-1) + losses[i]) / period
    return 100 - (100 / (1 + ag/al)) if al != 0 else 100

def calc_ema(closes, period=9):
    if len(closes) < period:
        return closes[-1] if closes else 0
    k = 2 / (period + 1)
    e = closes[0]
    for c in closes[1:]:
        e = c * k + e * (1 - k)
    return e

def get_spot_usdt():
    try:
        b = exchange.fetch_balance()
        return safe_float(b.get('USDT', {}).get('free', 0))
    except Exception as e:
        print(f'balance error: {e}')
        return 0.0

def get_signal(symbol):
    try:
        ohlcv  = exchange.fetch_ohlcv(symbol, '5m', limit=60)
        closes = [c[4] for c in ohlcv]
        closed = closes[:-1]
        price  = closes[-1]
        rsi    = calc_rsi(closed)
        ema9   = calc_ema(closed, 9)
        ema21  = calc_ema(closed, 21)
        return rsi, ema9, ema21, price
    except Exception as e:
        print(f'signal error {symbol}: {e}')
        return 50, 0, 0, 0

def buy_spot(symbol, usdt_amount):
    try:
        ticker = exchange.fetch_ticker(symbol)
        price  = safe_float(ticker['last'])
        if price <= 0:
            print(f'BUY {symbol}: bad price')
            return False
        qty_raw = usdt_amount / price
        qty_str = exchange.amount_to_precision(symbol, qty_raw)
        qty     = float(qty_str)
        market  = exchange.market(symbol)
        min_amt = safe_float(market.get('limits', {}).get('amount', {}).get('min', 0))
        if qty <= 0 or (min_amt > 0 and qty < min_amt):
            print(f'BUY {symbol}: qty {qty} too small (min={min_amt})')
            return False
        order = exchange.create_order(symbol, 'market', 'buy', qty)
        oid   = order.get('id', '')
        filled_qty, filled_price = 0.0, 0.0
        for _ in range(5):
            time.sleep(1)
            try:
                o = exchange.fetch_order(oid, symbol)
                filled_qty   = safe_float(o.get('filled', 0))
                filled_price = safe_float(o.get('average', 0))
                if filled_qty > 0 and filled_price > 0:
                    break
            except:
                pass
        if filled_price <= 0:
            filled_price = price
        if filled_qty <= 0:
            print(f'BUY {symbol}: fill not confirmed, skipping')
            return False
        holdings[symbol] = {'qty': filled_qty, 'entry_price': filled_price, 'buy_time': time.time()}
        print(f'BUY  {symbol} qty={filled_qty} @{filled_price:.4f} cost=${round(filled_qty*filled_price,2)}')
        return True
    except Exception as e:
        print(f'buy error {symbol}: {e}')
        return False

def sell_spot(symbol, reason):
    try:
        h = holdings.get(symbol)
        if not h:
            return
        qty = h['qty']
        if qty <= 0:
            del holdings[symbol]
            return
        order = exchange.create_order(symbol, 'market', 'sell', qty)
        oid   = order.get('id', '')
        filled_price = 0.0
        for _ in range(5):
            time.sleep(1)
            try:
                o = exchange.fetch_order(oid, symbol)
                filled_price = safe_float(o.get('average', 0))
                if filled_price > 0:
                    break
            except:
                pass
        if filled_price <= 0:
            filled_price = safe_float(exchange.fetch_ticker(symbol).get('last', 0))
        entry   = h['entry_price']
        pnl_usd = round((filled_price - entry) * qty, 3) if entry > 0 else 0
        pnl_pct = ((filled_price - entry) / entry * 100) if entry > 0 else 0
        print(f'SELL {symbol} @{filled_price:.4f} pnl={pnl_usd:+.3f} ({pnl_pct:+.2f}%) [{reason}]')
        del holdings[symbol]
    except Exception as e:
        print(f'sell error {symbol}: {e}')
        if '170131' in str(e) or 'Insufficient balance' in str(e):
            holdings.pop(symbol, None)
            print(f'sell cleanup: removed {symbol} from holdings (balance=0)')

def run():
    print(f'Spot Bot v1.1 - {now_utc()} UTC')
    print(f'Pairs: {SYMBOLS} | Budget: ${TRADE_USDT}/trade | TP={PROFIT_PCT*100}% SL={STOP_PCT*100}%')
    exchange.load_markets()
    # Load any existing spot holdings worth >$5
    try:
        b = exchange.fetch_balance()
        for sym in SYMBOLS:
            base = sym.split('/')[0]
            qty  = safe_float(b.get(base, {}).get('free', 0))
            if qty > 0:
                price = safe_float(exchange.fetch_ticker(sym).get('last', 0))
                if price > 0 and qty * price >= 5:
                    holdings[sym] = {'qty': qty, 'entry_price': price, 'buy_time': time.time()}
                    print(f'Loaded {sym}: {qty} ~${round(qty*price,2)}')
    except Exception as e:
        print(f'load error: {e}')

    while True:
        try:
            usdt = get_spot_usdt()
            print(f'\n--- spot {now_utc()} | USDT={usdt:.2f} | holding={list(holdings.keys())} ---')
            for symbol in SYMBOLS:
                rsi, ema9, ema21, price = get_signal(symbol)
                if price <= 0:
                    continue
                if symbol in holdings:
                    h     = holdings[symbol]
                    entry = h['entry_price']
                    if entry <= 0:
                        sell_spot(symbol, 'bad_entry')
                        continue
                    pnl_pct = (price - entry) / entry
                    if pnl_pct >= PROFIT_PCT:
                        sell_spot(symbol, f'TP+{pnl_pct*100:.1f}%')
                    elif pnl_pct <= -STOP_PCT:
                        sell_spot(symbol, f'SL{pnl_pct*100:.1f}%')
                    elif rsi < RSI_SELL and ema9 < ema21:
                        sell_spot(symbol, f'reversal RSI={rsi:.1f}')
                    else:
                        age = int(time.time() - h['buy_time'])
                        print(f'{symbol}: holding pnl={pnl_pct*100:+.2f}% RSI={rsi:.1f} age={age}s')
                    continue
                if usdt < TRADE_USDT:
                    print(f'{symbol}: USDT low ({usdt:.2f})')
                    continue
                if rsi > RSI_BUY and ema9 > ema21:
                    print(f'{symbol}: BUY signal RSI={rsi:.1f}')
                    if buy_spot(symbol, TRADE_USDT):
                        usdt -= TRADE_USDT
                else:
                    print(f'{symbol}: RSI={rsi:.1f} waiting')
        except Exception as e:
            print(f'loop error: {e}')
        time.sleep(TICK_SEC)

if __name__ == '__main__':
    run()
