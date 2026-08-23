"""
local_crypto_watch.py — direct-from-laptop mirror of crypto_watch.py (Oracle),
using PUBLIC Bybit market data only (no API key needed — fetch_ohlcv/fetch_ticker
are public endpoints). Zero dependency on SSH or Oracle reachability.

Why: 2026-07-26, found tripwire alerts from crypto_watch.log (Oracle) were
reaching tripwire_explainer up to 3 HOURS late, because every pull went
through SSH and most attempts were silently failing/timing out all night
(confirmed repeated SSH outages this session) — the fallback logic could only
grab the single newest line on a lucky successful pull, so most real alerts
were silently dropped, not delayed. This runs the same detection logic
directly against Bybit's API from this machine, so detection no longer
depends on SSH working at all. Oracle's crypto_watch.py keeps running too
(protects BAA execution bots that live there) — this is a second, independent
detection path feeding the SAME tripwire_explained.log Claude reads.
"""
import time
import ccxt
from heartbeat import write_heartbeat  # 2026-08-06: functional heartbeat rollout;
                                        # deployed as a standing service 2026-08-08 (final audit)

SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "XAU/USDT:USDT",
           "SOL/USDT:USDT", "DOGE/USDT:USDT", "XRP/USDT:USDT", "ADA/USDT:USDT",
           "AAVE/USDT:USDT"]
POLL_SECONDS = 30
FAST_WINDOW = 180
GRIND_WINDOW = 720
FAST_THRESHOLDS = {"BTC/USDT:USDT": 0.005, "ETH/USDT:USDT": 0.006, "XAU/USDT:USDT": 0.004,
                    "SOL/USDT:USDT": 0.008, "DOGE/USDT:USDT": 0.012, "XRP/USDT:USDT": 0.008,
                    "ADA/USDT:USDT": 0.008, "AAVE/USDT:USDT": 0.008}
GRIND_THRESHOLDS = {"BTC/USDT:USDT": 0.0025, "ETH/USDT:USDT": 0.003, "XAU/USDT:USDT": 0.002,
                     "SOL/USDT:USDT": 0.004, "DOGE/USDT:USDT": 0.006, "XRP/USDT:USDT": 0.004,
                     "ADA/USDT:USDT": 0.004, "AAVE/USDT:USDT": 0.004}
COOLDOWN_SECONDS = 300

exchange = ccxt.bybit()  # public data only, no auth needed

history = {s: [] for s in SYMBOLS}
last_fast_alert = {s: 0.0 for s in SYMBOLS}
last_grind_alert = {s: 0.0 for s in SYMBOLS}
LOG_FILE = r"C:\TradingBot\Bot_Active\local_crypto_watch.log"


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)


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
        log(f"FAST MOVE {sym} {direction} {fast_move*100:+.2f}% in {FAST_WINDOW}s ({fast_ref:.4f} -> {price:.4f})")

    grind_ref = h[0][1]
    grind_move = (price - grind_ref) / grind_ref
    if abs(grind_move) >= GRIND_THRESHOLDS[sym] and now - last_grind_alert[sym] > COOLDOWN_SECONDS:
        last_grind_alert[sym] = now
        direction = "UP" if grind_move > 0 else "DOWN"
        log(f"GRIND {sym} {direction} {grind_move*100:+.2f}% over {int(now-h[0][0])}s ({grind_ref:.4f} -> {price:.4f})")


def main():
    log(f"local_crypto_watch started (direct, no SSH) - polling {SYMBOLS} every {POLL_SECONDS}s")
    while True:
        for s in SYMBOLS:
            try:
                check(s)
            except Exception as e:
                log(f"error {s}: {e}")
        write_heartbeat("local_crypto_watch", symbols_checked=len(SYMBOLS))
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
