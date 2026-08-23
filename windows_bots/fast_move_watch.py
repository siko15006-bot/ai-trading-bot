"""
fast_move_watch.py — polls MT5 tick prices every 30s and prints an alert line
the moment any watched symbol moves faster than normal, so Claude finds out
mid-move instead of only at the next scheduled 15-min cycle (Ahmed's complaint
2026-07-08: BTC entry came late/after confirmation because the crash was only
noticed once the candle had already closed).

This does NOT place trades — it's a tripwire. Claude still waits for a
confirmed candle close before entering; this just shortens how long it takes
to notice something is happening.

Usage: python fast_move_watch.py
"""
import time
import collections

import MetaTrader5 as mt5
from heartbeat import write_heartbeat  # 2026-08-06: functional heartbeat rollout, see NOTIFICATION_POLICY.md

SYMBOLS = ["XAUUSDm", "BTCUSDm", "EURUSDm", "GBPUSDm",
           "USOILm", "USTECm", "UKOILm", "US30m", "NZDJPYm"]
WINDOW_SECONDS = 180          # look back this far for the % move
POLL_SECONDS = 30
INIT_RETRY_BASE = 5
INIT_RETRY_MAX = 60
THRESHOLDS = {                # % move within WINDOW_SECONDS that counts as "fast"
    "XAUUSDm": 0.004,         # 2026-07-19: lowered 0.006→0.004 (Ahmed: too many good moves slip by unfelt)
    "BTCUSDm": 0.005,         # 2026-07-19: lowered 0.008→0.005
    # 2026-08-06: recalibrated from 30-day M1 history (see fx_threshold_study,
    # Ahmed approved). Old 0.25% sat at the 99.98th percentile of 3-min moves
    # -- essentially dead (0.47 alerts/week each). New values sit at the 99.8th
    # percentile, ~7-8 alerts/week each. 1-week before/after review scheduled.
    "EURUSDm": 0.0008,
    "GBPUSDm": 0.00084,
    # 2026-07-20: added — these are exactly what Ahmed trades manually on BA
    # and the old 4-symbol list was blind to all of them (his complaint).
    "USOILm": 0.005,
    "USTECm": 0.0035,
    "UKOILm": 0.005,
    "US30m": 0.003,
    "NZDJPYm": 0.003,
}
COOLDOWN_SECONDS = 300        # don't re-alert on the same symbol within this long

history = {s: collections.deque() for s in SYMBOLS}  # (timestamp, price)
last_alert = {s: 0.0 for s in SYMBOLS}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _connect_mt5():
    delay = INIT_RETRY_BASE
    while True:
        mt5.shutdown()
        if mt5.initialize(path=r"C:\MT5_Portable_2\terminal64.exe"):
            log(f"fast_move_watch started — polling {SYMBOLS} every {POLL_SECONDS}s, "
                f"thresholds {THRESHOLDS}")
            return
        log(f"MT5 init failed {mt5.last_error()} -- retrying in {delay}s")
        time.sleep(delay)
        delay = min(INIT_RETRY_MAX, delay * 2)


def check(sym):
    tick = mt5.symbol_info_tick(sym)
    if not tick:
        return
    now = time.time()
    price = (tick.bid + tick.ask) / 2
    hist = history[sym]
    hist.append((now, price))
    while hist and now - hist[0][0] > WINDOW_SECONDS:
        hist.popleft()
    if len(hist) < 2:
        return
    oldest_price = hist[0][1]
    move = (price - oldest_price) / oldest_price
    if abs(move) >= THRESHOLDS[sym] and now - last_alert[sym] > COOLDOWN_SECONDS:
        last_alert[sym] = now
        direction = "UP" if move > 0 else "DOWN"
        log(f"FAST MOVE {sym} {direction} {move*100:+.2f}% in {WINDOW_SECONDS}s "
            f"({oldest_price:.3f} -> {price:.3f})")


def main():
    _connect_mt5()
    # 2026-08-06: NZDJPYm was silently blind for its whole life -- not in this
    # terminal's Market Watch, so symbol_info_tick() returned None every poll
    # and check() just returned early with zero errors logged. symbol_select
    # forces it (and any future symbol with the same issue) into Market Watch
    # at startup so a missing symbol shows up here instead of vanishing silently.
    for s in SYMBOLS:
        if not mt5.symbol_select(s, True):
            log(f"WARNING: symbol_select({s}) failed -- this symbol will silently "
                f"never alert, see mt5.last_error(): {mt5.last_error()}")
    while True:
        try:
            if mt5.terminal_info() is None:
                _connect_mt5()
                continue
            for s in SYMBOLS:
                check(s)
            write_heartbeat("fast_move_watch", symbols_checked=len(SYMBOLS))
        except Exception as e:
            log(f"error: {e}")
            _connect_mt5()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
