"""
detection_miss_monitor.py — independent watchdog for the crypto tripwire
detectors (crypto_watch.py / local_crypto_watch.py). Computes the same
FAST_MOVE/GRIND move using its OWN price history (never reads the
detectors' internal state, never restarts or touches them), then checks
whether a matching alert actually appeared in the detector's own log
within a grace window. If a MISS fires, the bug is provably in the
detector being watched, not in this script (Ahmed, 2026-07-26: "خليه
مراقبًا مستقلًا فقط ... نعرف أن العيب في detector الأصلي").

Scope: crypto side only (Bybit via public ccxt, mirrors
local_crypto_watch.py's SYMBOLS/THRESHOLDS exactly, cross-checked against
BOTH local_crypto_watch.log and Oracle's crypto_watch.log). MT5-side
(fast_move_watch.py/grind_watch.py) detection-miss coverage is a
follow-up, not built here.

MISS = a real move crossed threshold and no matching alert line appeared
in either detector's log within CHECK_GRACE_SECONDS of the crossing.
"""
import datetime
import json
import os
import subprocess
import time
import urllib.parse
from heartbeat import write_heartbeat  # 2026-08-06: functional heartbeat rollout;
                                        # deployed as a standing service 2026-08-08 (final audit)
import urllib.request

import ccxt

BA = r"C:\TradingBot\Bot_Active"
INCIDENT_LOG = os.path.join(BA, "detection_miss.log")
STATE_FILE = os.path.join(BA, "detection_miss_stats.json")

# mirrors local_crypto_watch.py exactly (2026-07-26, AAVE included)
SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "XAU/USDT:USDT",
           "SOL/USDT:USDT", "DOGE/USDT:USDT", "XRP/USDT:USDT", "ADA/USDT:USDT",
           "AAVE/USDT:USDT"]
POLL_SECONDS = 15  # tighter than the 30s detector poll -- don't add our own lag
FAST_WINDOW = 180
GRIND_WINDOW = 720
FAST_THRESHOLDS = {"BTC/USDT:USDT": 0.005, "ETH/USDT:USDT": 0.006, "XAU/USDT:USDT": 0.004,
                    "SOL/USDT:USDT": 0.008, "DOGE/USDT:USDT": 0.012, "XRP/USDT:USDT": 0.008,
                    "ADA/USDT:USDT": 0.008, "AAVE/USDT:USDT": 0.008}
GRIND_THRESHOLDS = {"BTC/USDT:USDT": 0.0025, "ETH/USDT:USDT": 0.003, "XAU/USDT:USDT": 0.002,
                     "SOL/USDT:USDT": 0.004, "DOGE/USDT:USDT": 0.006, "XRP/USDT:USDT": 0.004,
                     "ADA/USDT:USDT": 0.004, "AAVE/USDT:USDT": 0.004}

CHECK_GRACE_SECONDS = 90   # how long to wait for a matching alert before declaring MISS
COOLDOWN_SECONDS = 300     # don't re-flag the same symbol+kind repeatedly

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")

exchange = ccxt.bybit()

history = {s: [] for s in SYMBOLS}   # (ts, price) -- our own independent buffer
pending = {}                          # (symbol, kind) -> {"crossed_at", "move", "threshold"}
last_flag = {}                        # (symbol, kind) -> ts of last MISS alert (cooldown)
stats = {}                            # symbol -> {"expected", "detected", "missed"}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def notify(text):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
    except Exception as e:
        log(f"notify failed: {e}")


def load_stats():
    global stats
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            stats = json.load(f)
    except Exception:
        stats = {}
    for s in SYMBOLS:
        stats.setdefault(s, {"expected": 0, "detected": 0, "missed": 0})


def save_stats():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2)
    except Exception as e:
        log(f"stats save error: {e}")


def _parse_line_ts(line):
    # log lines start "[HH:MM:SS] ..." -- no date, so we anchor to today;
    # good enough given CHECK_GRACE_SECONDS is only 90s (no midnight-wrap
    # window realistically matters at that grace period).
    try:
        hms = line.strip()[1:9]
        t = datetime.datetime.strptime(hms, "%H:%M:%S").time()
        dt = datetime.datetime.combine(datetime.datetime.now().date(), t)
        return dt.timestamp()
    except Exception:
        return None


def local_log_lines():
    path = os.path.join(BA, "local_crypto_watch.log")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.readlines()[-200:]
    except Exception:
        return []


def oracle_log_lines():
    # best-effort secondary cross-check only -- SSH may be down, that's fine,
    # local_crypto_watch.log alone is sufficient to resolve MISS/OK.
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=6", "-o", "BatchMode=yes", "oracle",
             "tail -n 30 /tmp/crypto_watch.log"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10
        )
        if result.returncode == 0:
            return result.stdout.splitlines()
    except Exception:
        pass
    return []


def detector_fired(symbol, kind, since_ts):
    for line in local_log_lines() + oracle_log_lines():
        if symbol not in line or kind not in line:
            continue
        ts = _parse_line_ts(line)
        if ts is not None and ts >= since_ts - 5:
            return True
    return False


def check(symbol):
    try:
        price = exchange.fetch_ticker(symbol)["last"]
    except Exception as e:
        log(f"fetch error {symbol}: {str(e)[:100]}")
        return
    now = time.time()
    hist = history[symbol]
    hist.append((now, price))
    while hist and now - hist[0][0] > GRIND_WINDOW:
        hist.pop(0)
    if len(hist) < 2:
        return

    for kind, window, thresholds in [("FAST MOVE", FAST_WINDOW, FAST_THRESHOLDS),
                                      ("GRIND", GRIND_WINDOW, GRIND_THRESHOLDS)]:
        ref_price = None
        for ts, p in hist:
            if now - ts <= window:
                ref_price = p
                break
        if ref_price is None:
            continue
        move = abs(price - ref_price) / ref_price
        key = (symbol, kind)
        if move >= thresholds[symbol]:
            if key not in pending:
                pending[key] = {"crossed_at": now, "move": move, "threshold": thresholds[symbol]}
                stats[symbol]["expected"] += 1
        else:
            pending.pop(key, None)

    for key in [k for k in pending if k[0] == symbol]:
        entry = pending[key]
        if now - entry["crossed_at"] < CHECK_GRACE_SECONDS:
            continue
        sym, kind = key
        if detector_fired(sym, kind, entry["crossed_at"]):
            stats[sym]["detected"] += 1
            log(f"OK {sym} {kind} move={entry['move']*100:.2f}% -> detector fired within grace window")
        else:
            stats[sym]["missed"] += 1
            last = last_flag.get(key, 0)
            if now - last > COOLDOWN_SECONDS:
                last_flag[key] = now
                report = (f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\n{sym}\nkind={kind}\n"
                          f"threshold={entry['threshold']*100:.2f}%\nactual_move={entry['move']*100:.2f}%\n"
                          f"detector_triggered=false\nstatus=MISS")
                with open(INCIDENT_LOG, "a", encoding="utf-8") as f:
                    f.write(report + "\n\n")
                log(f"MISS {sym} {kind} move={entry['move']*100:.2f}% (threshold {entry['threshold']*100:.2f}%) "
                    f"-- no detector alert within {CHECK_GRACE_SECONDS}s")
                notify(f"DETECTION MISS\n{sym} {kind}\nmove={entry['move']*100:.2f}% "
                       f"(threshold {entry['threshold']*100:.2f}%)\nمفيش تنبيه من الديتكتور خلال {CHECK_GRACE_SECONDS}ث")
        pending.pop(key, None)
        save_stats()


def main():
    load_stats()
    log(f"detection_miss_monitor started (independent, crypto-only) — polling {SYMBOLS} every {POLL_SECONDS}s")
    tick = 0
    while True:
        try:
            for s in SYMBOLS:
                check(s)
            write_heartbeat("detection_miss_monitor", symbols_checked=len(SYMBOLS))
        except Exception as e:
            log(f"loop error: {str(e)[:150]}")
        tick += 1
        if tick % 40 == 0:
            save_stats()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
