"""
regime_detector.py — answers a question none of the existing detectors
answer: not "did something just move" (fast_move_watch/grind_watch/
local_crypto_watch already do that), but "what kind of market are we in
right now, and is this move consistent with it?" (Ahmed, 2026-07-26).

Runs independently (public Bybit ccxt, same SYMBOLS as local_crypto_watch.py,
zero dependency on any other bot). Every POLL_SECONDS, per symbol:

  base_regime  = TRENDING_UP | TRENDING_DOWN | RANGE
      via Kaufman's Efficiency Ratio (ER) over TREND_WINDOW M15 bars:
      ER = |close[-1] - close[-N]| / sum(|close[i]-close[i-1]|)
      ER=1 -> a straight-line move (maximally efficient/trending).
      ER=0 -> pure back-and-forth, no net progress (ranging).
      A genuinely random walk of N independent steps has an EXPECTED
      ER ~= sqrt(2/(pi*N)) (~0.18 at N=20) -- TREND_ER_THRESHOLD=0.35 is
      roughly 2x that noise floor, i.e. "meaningfully more directional
      than randomness alone would produce," not an arbitrary round number.

  vol_state    = HIGH_VOL | LOW_VOL | NORMAL_VOL
      ratio of realized volatility over the short (TREND_WINDOW) window vs
      a longer BASELINE_VOL_WINDOW (24h of M15 bars) -- same "current vs
      baseline" comparison Ahmed described.

  regime changes (base_regime flips) are logged as REVERSAL events;
  transitions into/out of HIGH_VOL are logged as EXPANSION/CONTRACTION.
  Steady-state (no change) is NOT logged every cycle -- only transitions,
  same "alert on event, not on every poll" pattern as the other detectors.

Current full state (for every symbol, updated every cycle regardless of
whether it changed) is persisted to REGIME_STATE_FILE so other components
(dashboard, tripwire_explainer, the outcome/calibration log Ahmed wants
next) can read it without recomputing anything themselves -- same
single-source-of-truth pattern as ladder_guard_bybit_state.json.
"""
import json
import os
import time
from heartbeat import write_heartbeat  # 2026-08-06: functional heartbeat rollout (not currently
                                        # running -- code-only patch, not tested live)

import ccxt

SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "XAU/USDT:USDT",
           "SOL/USDT:USDT", "DOGE/USDT:USDT", "XRP/USDT:USDT", "ADA/USDT:USDT",
           "AAVE/USDT:USDT"]
TIMEFRAME = "15m"
TREND_WINDOW = 20          # M15 bars (~5h) for ER + short-vol
BASELINE_VOL_WINDOW = 96   # M15 bars (~24h) for the volatility baseline
POLL_SECONDS = 60

TREND_ER_THRESHOLD = 0.35  # see module docstring for the ~0.18 random-walk baseline this is measured against
HIGH_VOL_RATIO = 1.8
LOW_VOL_RATIO = 0.5

BA = r"C:\TradingBot\Bot_Active"
LOG_FILE = os.path.join(BA, "regime_watch.log")
STATE_FILE = os.path.join(BA, "regime_state.json")

exchange = ccxt.bybit()
_last_regime = {}    # symbol -> last logged base_regime (change detection only)
_last_vol_state = {}  # symbol -> last logged vol_state


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def efficiency_ratio(closes):
    if len(closes) < 2:
        return None
    net = abs(closes[-1] - closes[0])
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    return net / path if path > 0 else 0.0


def stdev(values):
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return var ** 0.5


def returns(closes):
    return [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes)) if closes[i - 1]]


def classify(closes):
    """Pure function: given a list of closes (oldest->newest, length >=
    BASELINE_VOL_WINDOW+1 ideally), return the regime dict. Never touches
    the network/exchange -- easy to unit-test with synthetic data."""
    if len(closes) < TREND_WINDOW + 1:
        return None
    trend_slice = closes[-(TREND_WINDOW + 1):]
    er = efficiency_ratio(trend_slice)

    if er is None:
        base_regime = "UNKNOWN"
    elif er >= TREND_ER_THRESHOLD:
        base_regime = "TRENDING_UP" if trend_slice[-1] > trend_slice[0] else "TRENDING_DOWN"
    else:
        base_regime = "RANGE"

    short_rets = returns(trend_slice)
    short_vol = stdev(short_rets)
    baseline_slice = closes[-(BASELINE_VOL_WINDOW + 1):] if len(closes) >= BASELINE_VOL_WINDOW + 1 else closes
    baseline_rets = returns(baseline_slice)
    baseline_vol = stdev(baseline_rets)

    if short_vol is None or baseline_vol in (None, 0):
        vol_state, vol_ratio = "UNKNOWN", None
    else:
        vol_ratio = short_vol / baseline_vol
        if vol_ratio >= HIGH_VOL_RATIO:
            vol_state = "HIGH_VOL"
        elif vol_ratio <= LOW_VOL_RATIO:
            vol_state = "LOW_VOL"
        else:
            vol_state = "NORMAL_VOL"

    return {
        "base_regime": base_regime, "er": round(er, 4) if er is not None else None,
        "vol_state": vol_state, "vol_ratio": round(vol_ratio, 3) if vol_ratio is not None else None,
        "last_close": closes[-1],
    }




def _legacy_regime_payload(result):
    if result is None:
        return {
            "regime": "UNKNOWN",
            "adx": 0.0,
            "pdi": 0.0,
            "ndi": 0.0,
            "ema_slope": 0.0,
            "atr": 0.0,
            "volatility_ratio": 1.0,
            "lot_scale": 0.8,
            "signal_filter": "ALL",
            "confidence": 0.0,
            "reason": "Not enough data",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

    base = result["base_regime"]
    er = result.get("er") or 0.0
    vol_ratio = result.get("vol_ratio") or 1.0

    if base == "TRENDING_UP":
        regime = base
        pdi, ndi, ema_slope = 32.0, 18.0, max(0.0001, er / 100.0)
        signal_filter = "TREND_ONLY"
        lot_scale = 1.0 if result.get("vol_state") != "HIGH_VOL" else 0.5
        reason = f"ER={er:.4f} trend up"
    elif base == "TRENDING_DOWN":
        regime = base
        pdi, ndi, ema_slope = 18.0, 32.0, -max(0.0001, er / 100.0)
        signal_filter = "TREND_ONLY"
        lot_scale = 1.0 if result.get("vol_state") != "HIGH_VOL" else 0.5
        reason = f"ER={er:.4f} trend down"
    else:
        regime = "RANGING" if base == "RANGE" else base
        pdi = ndi = 25.0
        ema_slope = 0.0
        signal_filter = "REVERSAL_ONLY" if base == "RANGE" else "ALL"
        lot_scale = 0.8
        reason = f"ER={er:.4f} range" if base == "RANGE" else "Insufficient data"

    adx = max(10.0, min(40.0, 10.0 + er * 60.0))
    confidence = round(min(1.0, max(0.0, er if base.startswith("TRENDING") else 1.0 - er)), 2)

    return {
        "regime": regime,
        "adx": round(adx, 1),
        "pdi": round(pdi, 1),
        "ndi": round(ndi, 1),
        "ema_slope": round(ema_slope, 5),
        "atr": 0.0,
        "volatility_ratio": round(vol_ratio, 2),
        "lot_scale": lot_scale,
        "signal_filter": signal_filter,
        "confidence": confidence,
        "reason": reason,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


class RegimeDetector:
    """Backward-compatible wrapper for callers that still expect the old API."""

    def __init__(self, adx_period: int = 14, lookback: int = 50):
        self.adx_period = adx_period
        self.lookback = lookback

    def detect(self, bars: list, symbol: str = "XAUUSD") -> dict:
        closes = [bar["close"] if isinstance(bar, dict) else bar[4] for bar in bars]
        closes = closes[-max(self.lookback, TREND_WINDOW + 1):]
        return _legacy_regime_payload(classify(closes))


def get_regime(bars: list, symbol: str = "XAUUSD") -> dict:
    return RegimeDetector().detect(bars, symbol)
def save_state(state):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log(f"save_state error: {str(e)[:150]}")


def check(symbol, state):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=BASELINE_VOL_WINDOW + 5)
    except Exception as e:
        log(f"fetch error {symbol}: {str(e)[:100]}")
        return
    closes = [c[4] for c in ohlcv]
    result = classify(closes)
    if result is None:
        return
    result["updated_ts"] = time.time()
    state[symbol] = result

    prev_regime = _last_regime.get(symbol)
    if prev_regime is not None and prev_regime != result["base_regime"]:
        log(f"REVERSAL {symbol} {prev_regime} -> {result['base_regime']} (ER={result['er']})")
    _last_regime[symbol] = result["base_regime"]

    prev_vol = _last_vol_state.get(symbol)
    if prev_vol is not None and prev_vol != result["vol_state"]:
        if result["vol_state"] == "HIGH_VOL":
            log(f"VOLATILITY EXPANSION {symbol} {result['vol_ratio']}x baseline "
                f"({prev_vol} -> {result['vol_state']})")
        elif prev_vol == "HIGH_VOL":
            log(f"VOLATILITY CONTRACTION {symbol} {result['vol_ratio']}x baseline "
                f"({prev_vol} -> {result['vol_state']})")
    _last_vol_state[symbol] = result["vol_state"]


def main():
    log(f"regime_detector started — polling {SYMBOLS} every {POLL_SECONDS}s "
        f"(trend_window={TREND_WINDOW} bars, baseline_vol_window={BASELINE_VOL_WINDOW} bars)")
    state = {}
    while True:
        try:
            for s in SYMBOLS:
                check(s, state)
            save_state(state)
            write_heartbeat("regime_detector", symbols_checked=len(SYMBOLS))
        except Exception as e:
            log(f"loop error: {str(e)[:150]}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
