"""
webhook_server.py — TradingView webhook receiver + Multi-pair scanner
Port: 8767
Webhook URL: http://<REDACTED_ORACLE_IP>:8767/webhook?token=siko2026
Scanner: runs every 15min, picks top 3 signals from 20 pairs
"""

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
import ccxt, os, time, threading
from dotenv import load_dotenv
from datetime import datetime

load_dotenv('/home/ubuntu/.env')

app = FastAPI()

WEBHOOK_TOKEN = "siko2026"
RISK_PCT      = 0.005   # 0.5% per trade
MAX_OPEN      = 4       # total across scanner + webhook
SCANNER_INTERVAL = 900  # 15 min

SCAN_SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "XAU/USDT:USDT",
    "ADA/USDT:USDT", "AVAX/USDT:USDT", "DOGE/USDT:USDT", "LINK/USDT:USDT",
    "DOT/USDT:USDT", "NEAR/USDT:USDT", "APT/USDT:USDT", "ARB/USDT:USDT",
    "OP/USDT:USDT",  "SUI/USDT:USDT",  "INJ/USDT:USDT", "TIA/USDT:USDT",
    "WIF/USDT:USDT", "HYPE/USDT:USDT", "JUP/USDT:USDT", "ATOM/USDT:USDT",
]

ex = ccxt.bybit({
    'apiKey': os.getenv('BYBIT_API_KEY'),
    'secret': os.getenv('BYBIT_API_SECRET'),
    'enableRateLimit': True,
    'options': {'recvWindow': 30000},
})

# ─── EMA / RSI ───────────────────────────────────────────────────────────────
def calc_ema(prices, period):
    k = 2 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
    return ema

def calc_rsi(closes, period=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    return 100 - 100 / (1 + ag / al) if al else 100

# ─── SIGNAL SCORE ─────────────────────────────────────────────────────────────
def score_symbol(symbol):
    """Returns (signal, score, price) — score 0-100, higher = stronger."""
    try:
        ohlcv = ex.fetch_ohlcv(symbol, '1h', limit=80)
        closes = [c[4] for c in ohlcv]
        vols   = [c[5] for c in ohlcv]
        price  = closes[-1]

        ema20  = calc_ema(closes, 20)
        ema50  = calc_ema(closes, 50)
        ema20p = calc_ema(closes[:-4], 20)
        rsi    = calc_rsi(closes[:-1])
        avg_vol = sum(vols[-21:-1]) / 20
        vol_ok  = vols[-2] > avg_vol * 0.5

        if not vol_ok:
            return None, 0, price

        gap   = abs(ema20 - ema50) / ema50 * 100   # % separation
        slope = abs(ema20 - ema20p) / ema20p * 100  # % slope strength
        score = min(100, int((gap * 20) + (slope * 40)))

        if ema20 > ema50 and ema20 > ema20p and price > ema20 and 40 < rsi < 72:
            return 'BUY', score, price
        if ema20 < ema50 and ema20 < ema20p and price < ema20 and 28 < rsi < 60:
            return 'SELL', score, price
        return None, 0, price
    except Exception as e:
        print(f"  score_symbol {symbol}: {e}")
        return None, 0, 0

# ─── PLACE ORDER ─────────────────────────────────────────────────────────────
def place_order(symbol, side, sl_pct=0.015, tp_pct=0.04):
    """Place market order with % SL/TP. sl_pct=1.5%, tp_pct=4%"""
    try:
        bal     = float(ex.fetch_balance().get('USDT', {}).get('free', 0))
        risk    = bal * RISK_PCT
        ticker  = ex.fetch_ticker(symbol)
        price   = ticker['last']

        sl_dist = price * sl_pct
        qty_raw = risk / sl_dist
        mkt     = ex.market(symbol)
        step    = mkt.get('precision', {}).get('amount', 0.001)
        import math
        qty = max(mkt.get('limits', {}).get('amount', {}).get('min', step),
                  math.floor(qty_raw / step) * step)

        sl = round(price * (1 - sl_pct) if side == 'buy' else price * (1 + sl_pct), 2)
        tp = round(price * (1 + tp_pct) if side == 'buy' else price * (1 - tp_pct), 2)

        order = ex.create_market_order(symbol, side, qty, params={
            'stopLoss':   {'triggerPrice': str(sl), 'slTriggerBy': 'MarkPrice'},
            'takeProfit': {'triggerPrice': str(tp), 'tpTriggerBy': 'MarkPrice'},
        })
        msg = f"[{datetime.utcnow():%H:%M}] {side.upper()} {symbol} qty={qty} @{price:.4f} SL={sl} TP={tp}"
        print(msg)
        return {"status": "ok", "msg": msg, "id": order.get('id')}
    except Exception as e:
        print(f"  place_order error: {e}")
        return {"status": "error", "msg": str(e)}

def open_count():
    try:
        return len([p for p in ex.fetch_positions() if float(p.get('contracts', 0) or 0) > 0])
    except:
        return 0

# ─── WEBHOOK ENDPOINT ────────────────────────────────────────────────────────
@app.post("/webhook")
async def webhook(request: Request, token: str = ""):
    if token != WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    data = await request.json()
    symbol = data.get("symbol", "").replace("USDT.P", "/USDT:USDT").replace("USDT", "/USDT:USDT")
    if "/USDT:USDT" not in symbol:
        symbol = symbol + "/USDT:USDT"
    side   = data.get("action", "").lower()
    if side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="action must be buy or sell")
    if open_count() >= MAX_OPEN:
        return {"status": "skip", "reason": "MAX_OPEN reached"}
    return place_order(symbol, side)

@app.get("/status")
async def status():
    try:
        pos  = [p for p in ex.fetch_positions() if float(p.get('contracts', 0) or 0) > 0]
        bal  = ex.fetch_balance().get('USDT', {})
        return {
            "time": datetime.utcnow().strftime("%H:%M UTC"),
            "free": float(bal.get('free', 0)),
            "positions": [{"symbol": p['symbol'], "side": p['side'],
                           "pnl": float(p.get('unrealizedPnl', 0) or 0)} for p in pos]
        }
    except Exception as e:
        return {"error": str(e)}

# ─── SCANNER LOOP ────────────────────────────────────────────────────────────
def scanner_loop():
    while True:
        time.sleep(SCANNER_INTERVAL)
        try:
            print(f"\n[SCANNER {datetime.utcnow():%H:%M}] Scanning {len(SCAN_SYMBOLS)} pairs...")
            if open_count() >= MAX_OPEN:
                print("  MAX_OPEN reached — skip")
                continue

            results = []
            for sym in SCAN_SYMBOLS:
                sig, score, price = score_symbol(sym)
                if sig and score > 0:
                    results.append((score, sym, sig, price))
                time.sleep(0.5)

            results.sort(reverse=True)
            print(f"  Top signals: {[(s, sym, sig) for s, sym, sig, _ in results[:5]]}")

            for score, sym, sig, price in results[:2]:
                if open_count() >= MAX_OPEN:
                    break
                # skip if already have position
                pos = ex.fetch_positions()
                open_syms = {p['symbol'] for p in pos if float(p.get('contracts', 0) or 0) > 0}
                if sym in open_syms:
                    continue
                print(f"  → Entering {sym} {sig} score={score}")
                place_order(sym, sig.lower())
                time.sleep(2)
        except Exception as e:
            print(f"  Scanner error: {e}")

# Start scanner in background
threading.Thread(target=scanner_loop, daemon=True).start()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8767)
