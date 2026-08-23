"""
grind_watch.py — catches gradual multi-candle grinds that fast_move_watch's
3-minute spike window misses (e.g. 2026-07-15: XAU ground 4028->4066 over 75
minutes, no single 3-min window tripped the spike tripwire). Polls every 90s,
alerts once per symbol per direction when the cumulative move over the last
~12 minutes crosses a modest threshold, then cools down so it doesn't spam.
"""
import time
import MetaTrader5 as mt5
from heartbeat import write_heartbeat  # 2026-08-06: functional heartbeat rollout, see NOTIFICATION_POLICY.md

SYMBOLS = ["XAUUSDm", "BTCUSDm", "EURUSDm", "GBPUSDm",
           "USOILm", "USTECm", "UKOILm", "US30m", "NZDJPYm"]
WINDOW_SECONDS = 720
POLL_SECONDS = 90
INIT_RETRY_BASE = 5
INIT_RETRY_MAX = 60
# 2026-07-19: XAU 0.003→0.002, BTC 0.004→0.0025 (Ahmed: good moves slipping by between cycles)
# 2026-07-20: added USOIL/NAS100/UKOIL/DJ30/NZDJPY — matches BA's actual manual trades
# 2026-08-14: USOIL/UKOIL 0.0025→0.004 — observed 2+ hrs of repeated benign alerts
# (0.25-0.47% over 12min, same range each time, ~every 15-20min i.e. cooldown-gated
# but still routine chop not a real break); oil's normal noise floor sits closer to
# its old threshold than other symbols', so it fired far more often for no signal.
THRESHOLDS = {"XAUUSDm": 0.002, "BTCUSDm": 0.0025, "EURUSDm": 0.0015, "GBPUSDm": 0.0015,
              "USOILm": 0.004, "USTECm": 0.0018, "UKOILm": 0.004, "US30m": 0.0015, "NZDJPYm": 0.0015}
COOLDOWN_SECONDS = 900

history = {s: [] for s in SYMBOLS}
last_alert = {s: 0.0 for s in SYMBOLS}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _connect_mt5():
    delay = INIT_RETRY_BASE
    while True:
        mt5.shutdown()
        if mt5.initialize(path=r"C:\MT5_Portable_2\terminal64.exe", login=<REDACTED_MT5_LOGIN_EA>, password="<REDACTED_MT5_PASSWORD_EA>", server="Exness-MT5Real33"):
            log(f"grind_watch started - polling {SYMBOLS} every {POLL_SECONDS}s, thresholds {THRESHOLDS}")
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
    h = history[sym]
    h.append((now, price))
    while h and now - h[0][0] > WINDOW_SECONDS:
        h.pop(0)
    if len(h) < 2:
        return
    old_price = h[0][1]
    move = (price - old_price) / old_price
    if abs(move) >= THRESHOLDS[sym] and now - last_alert[sym] > COOLDOWN_SECONDS:
        last_alert[sym] = now
        direction = "UP" if move > 0 else "DOWN"
        log(f"GRIND {sym} {direction} {move*100:+.2f}% over {int(now-h[0][0])}s ({old_price:.3f} -> {price:.3f})")


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
            write_heartbeat("grind_watch", symbols_checked=len(SYMBOLS))
        except Exception as e:
            log(f"error: {e}")
            _connect_mt5()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
