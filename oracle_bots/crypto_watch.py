"""
crypto_watch.py — Bybit (BAA) mirror of fast_move_watch.py/grind_watch.py.
2026-07-20: MT5 tripwire only ever covered MT5 symbols — BAA (Bybit ccxt) had
zero live tripwire, so a BTC/ETH/XAU move or a bot opening a naked position on
Bybit was only ever noticed by manually SSHing in. Same detection logic as the
MT5 watchers, ported to ccxt tickers, one process covering both the fast-spike
and slow-grind windows so we don't need a second script for one exchange.
"""
import time
from heartbeat import write_heartbeat  # 2026-08-06: functional heartbeat rollout
import os
import ccxt
from dotenv import load_dotenv

load_dotenv("/home/ubuntu/.env")

SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "XAU/USDT:USDT",
           "SOL/USDT:USDT", "DOGE/USDT:USDT", "XRP/USDT:USDT", "ADA/USDT:USDT", "AAVE/USDT:USDT"]
POLL_SECONDS = 60
FAST_WINDOW = 180
GRIND_WINDOW = 720
# 2026-07-25: SOL/DOGE/XRP/ADA added — Ahmed had open positions in these on
# BAA with zero tripwire coverage (only BTC/ETH/XAU were watched), so a real
# DOGE rally (+4.95%/24h, peaked then rolled over) went completely unnoticed
# until he asked directly. Altcoin thresholds set a bit wider than BTC/ETH
# since they're naturally choppier — avoids alert spam on normal noise.
FAST_THRESHOLDS = {"BTC/USDT:USDT": 0.005, "ETH/USDT:USDT": 0.006, "XAU/USDT:USDT": 0.004,
                    "SOL/USDT:USDT": 0.008, "DOGE/USDT:USDT": 0.012, "XRP/USDT:USDT": 0.008,
                    "ADA/USDT:USDT": 0.008, "AAVE/USDT:USDT": 0.008}
GRIND_THRESHOLDS = {"BTC/USDT:USDT": 0.0025, "ETH/USDT:USDT": 0.003, "XAU/USDT:USDT": 0.002,
                     "SOL/USDT:USDT": 0.004, "DOGE/USDT:USDT": 0.006, "XRP/USDT:USDT": 0.004,
                     "ADA/USDT:USDT": 0.004, "AAVE/USDT:USDT": 0.004}
COOLDOWN_SECONDS = 300

exchange = ccxt.bybit({
    "apiKey": os.environ["BYBIT_API_KEY"],
    "secret": os.environ["BYBIT_API_SECRET"],
})

history = {s: [] for s in SYMBOLS}  # (ts, price)
last_fast_alert = {s: 0.0 for s in SYMBOLS}
last_grind_alert = {s: 0.0 for s in SYMBOLS}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def check(sym):
    ticker = exchange.fetch_ticker(sym)
    price = ticker["last"]
    now = time.time()
    h = history[sym]
    h.append((now, price))
    while h and now - h[0][0] > GRIND_WINDOW:
        h.pop(0)
    if len(h) < 2:
        return

    fast_ref = next((p for t, p in h if now - t <= FAST_WINDOW), h[0][1])
    fast_move = (price - fast_ref) / fast_ref
    if abs(fast_move) >= FAST_THRESHOLDS[sym] and now - last_fast_alert[sym] > COOLDOWN_SECONDS:
        last_fast_alert[sym] = now
        direction = "UP" if fast_move > 0 else "DOWN"
        log(f"FAST MOVE {sym} {direction} {fast_move*100:+.2f}% in {FAST_WINDOW}s ({fast_ref:.3f} -> {price:.3f})")

    grind_ref = h[0][1]
    grind_move = (price - grind_ref) / grind_ref
    if abs(grind_move) >= GRIND_THRESHOLDS[sym] and now - last_grind_alert[sym] > COOLDOWN_SECONDS:
        last_grind_alert[sym] = now
        direction = "UP" if grind_move > 0 else "DOWN"
        log(f"GRIND {sym} {direction} {grind_move*100:+.2f}% over {int(now-h[0][0])}s ({grind_ref:.3f} -> {price:.3f})")


def main():
    log(f"crypto_watch started - polling {SYMBOLS} every {POLL_SECONDS}s")
    while True:
        for s in SYMBOLS:
            try:
                check(s)
            except Exception as e:
                log(f"error {s}: {e}")
        write_heartbeat('crypto_watch', symbols_checked=len(SYMBOLS))
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
