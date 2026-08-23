"""alert_watch.py -- 2026-08-16, extends alert_decision_gate.py: an alert
that's structurally relevant but not yet actionable (breakout without a
confirmed retest) gets tracked as WATCHING and re-checked as new M15
candles close, instead of being judged once and forgotten -- which is
exactly the gap the BTC incident exposed (the FIRST check was honestly
correct given what was available at that instant; nobody re-checked as
the retest actually happened a few candles later).

State machine: WATCHING -> {SETUP_CONFIRMED, INVALIDATED, EXPIRED,
ANALYSIS_INCOMPLETE}. All transitions are terminal except staying in
WATCHING. Re-evaluation tracks the ORIGINAL breakout_level from the alert
that created the watch (not a freshly-recomputed range each time -- the
level being tracked must stay fixed, or "retest" would be a moving
target).

Research/analysis/alerting only -- no import of any order-placing
function anywhere in this file, matching alert_decision_gate.py's
boundary exactly.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

BASE_DIR = r"C:\TradingBot\Bot_Active"
WATCHES_PATH = os.path.join(BASE_DIR, "alert_watches.json")
DECISIONS_LOG = os.path.join(BASE_DIR, "alert_decisions.jsonl")

MAX_WATCH_SECONDS = 90 * 60
RETEST_TOLERANCE = 0.0015           # same 15bps tolerance as the original gate's retest check
INVALIDATION_TOLERANCE = 0.0005     # a close back beyond the level by >5bps invalidates the breakout
TERMINAL_STATES = {"SETUP_CONFIRMED", "INVALIDATED", "EXPIRED", "ANALYSIS_INCOMPLETE"}


@dataclass
class WatchEntry:
    watch_id: str
    symbol: str
    direction: str               # "UP" or "DOWN"
    breakout_level: float
    original_alert_time: str     # ISO
    original_alert_time_epoch: float
    original_alert_price: float
    status: str = "WATCHING"
    reevaluation_count: int = 0
    last_price: float | None = None
    last_bar_time_checked: int | None = None
    created_at_utc: str = ""
    updated_at_utc: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- persistence

def load_watches() -> dict[str, WatchEntry]:
    if not os.path.exists(WATCHES_PATH):
        return {}
    with open(WATCHES_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    return {k: WatchEntry(**v) for k, v in raw.items()}


def save_watches(watches: dict[str, WatchEntry]) -> None:
    tmp = WATCHES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({k: v.to_dict() for k, v in watches.items()}, f, indent=2, default=str)
    os.replace(tmp, WATCHES_PATH)


def _log(row: dict) -> None:
    row["logged_at_utc"] = datetime.now(timezone.utc).isoformat()
    with open(DECISIONS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())


# ---------------------------------------------------------------- dedup (item 7)

def find_matching_watch(watches: dict[str, WatchEntry], symbol: str, direction: str,
                          breakout_level: float, tolerance: float = RETEST_TOLERANCE) -> WatchEntry | None:
    for w in watches.values():
        if w.status != "WATCHING" or w.symbol != symbol or w.direction != direction:
            continue
        if abs(w.breakout_level - breakout_level) <= breakout_level * tolerance:
            return w
    return None


def start_or_update_watch(watches: dict[str, WatchEntry], symbol: str, direction: str,
                            breakout_level: float, alert_time: str, alert_price: float) -> WatchEntry:
    """Creates a new WATCHING entry, or -- if a matching one already exists
    (item 7, no duplicate watches for the same setup) -- updates its
    last_price/updated_at and returns the EXISTING entry with its ORIGINAL
    alert_time untouched (the 90-minute clock started at first detection,
    a later duplicate alert must not reset it)."""
    existing = find_matching_watch(watches, symbol, direction, breakout_level)
    now_iso = datetime.now(timezone.utc).isoformat()
    if existing:
        existing.last_price = alert_price
        existing.updated_at_utc = now_iso
        save_watches(watches)
        return existing

    alert_epoch = datetime.fromisoformat(alert_time.replace("Z", "+00:00")).timestamp()
    watch_id = f"{symbol}_{direction}_{round(breakout_level, 2)}_{alert_time}"
    w = WatchEntry(watch_id=watch_id, symbol=symbol, direction=direction, breakout_level=breakout_level,
                    original_alert_time=alert_time, original_alert_time_epoch=alert_epoch,
                    original_alert_price=alert_price, last_price=alert_price,
                    created_at_utc=now_iso, updated_at_utc=now_iso)
    watches[watch_id] = w
    save_watches(watches)
    return w


# ---------------------------------------------------------------- re-evaluation (pure, no I/O)

def reevaluate_watch(watch: WatchEntry, m15_rows: list[dict], h1_rows: list[dict],
                       now_epoch: float) -> tuple[str, dict]:
    """Pure function. m15_rows/h1_rows must ALREADY be restricted by the
    caller to bars available at now_epoch (no look-ahead enforced by the
    caller, not here -- same division of responsibility as
    alert_decision_gate.analyze()). Returns (new_status, detail_dict).
    Never raises: any missing/malformed data fails closed to
    ANALYSIS_INCOMPLETE, matching item 4/9's fail-closed requirement."""
    try:
        elapsed = now_epoch - watch.original_alert_time_epoch
        if elapsed >= MAX_WATCH_SECONDS:
            return "EXPIRED", {"elapsed_seconds": elapsed, "reason": f"no resolution within {MAX_WATCH_SECONDS}s"}

        if not m15_rows or not h1_rows:
            return "ANALYSIS_INCOMPLETE", {"reason": "missing M15 or H1 data at re-evaluation"}

        m15 = sorted(m15_rows, key=lambda r: r["time"])
        h1 = sorted(h1_rows, key=lambda r: r["time"])
        if len(h1) < 2 or len(m15) < 1:
            return "ANALYSIS_INCOMPLETE", {"reason": "insufficient bars at re-evaluation"}

        level = watch.breakout_level
        up = watch.direction == "UP"

        # only consider bars strictly AFTER the original alert -- price action
        # that predates the alert cannot retroactively confirm or invalidate it
        m15_since = [b for b in m15 if b["time"] >= watch.original_alert_time_epoch]
        if not m15_since:
            return "WATCHING", {"reason": "no new M15 bars since original alert yet"}

        latest = m15_since[-1]
        latest_price = latest["close"]

        # invalidation: a CLOSE back beyond the level in the wrong direction
        if up and latest_price < level * (1 - INVALIDATION_TOLERANCE):
            return "INVALIDATED", {"reason": f"close {latest_price} fell back below breakout level {level}",
                                      "price": latest_price}
        if not up and latest_price > level * (1 + INVALIDATION_TOLERANCE):
            return "INVALIDATED", {"reason": f"close {latest_price} rose back above breakout level {level}",
                                      "price": latest_price}

        # retest: any bar's low (UP) / high (DOWN) touched near the level,
        # AND price is still holding beyond it now (continuation, not reversal)
        touched = any(
            (up and b["low"] <= level * (1 + RETEST_TOLERANCE)) or
            (not up and b["high"] >= level * (1 - RETEST_TOLERANCE))
            for b in m15_since
        )
        holding = (latest_price > level) if up else (latest_price < level)
        if touched and holding:
            return "SETUP_CONFIRMED", {"reason": "retest touched and price is holding beyond the level",
                                          "price": latest_price, "touched_level": level}

        return "WATCHING", {"reason": "breakout intact, no retest confirmed yet", "price": latest_price}
    except Exception as e:
        return "ANALYSIS_INCOMPLETE", {"reason": f"exception during re-evaluation: {type(e).__name__}: {e}"}


def log_reevaluation(watch: WatchEntry, new_status: str, detail: dict, price: float,
                       h1_state: str, m15_state: str, now_epoch: float) -> None:
    watch.reevaluation_count += 1
    watch.last_price = price
    watch.updated_at_utc = datetime.now(timezone.utc).isoformat()
    row = {
        "event": "REEVALUATION", "watch_id": watch.watch_id, "symbol": watch.symbol,
        "original_alert_time": watch.original_alert_time, "reevaluation_time": watch.updated_at_utc,
        "reevaluation_number": watch.reevaluation_count, "price": price,
        "h1_state": h1_state, "m15_state": m15_state,
        "breakout_level": watch.breakout_level, "direction": watch.direction,
        "decision": new_status, "detail": detail,
        "seconds_since_original_alert": round(now_epoch - watch.original_alert_time_epoch, 1),
    }
    _log(row)
    watch.status = new_status


def _self_test() -> None:
    """ponytail: the 5 requested cases, using reevaluate_watch() directly (pure)."""
    import tempfile
    global WATCHES_PATH, DECISIONS_LOG
    d = tempfile.mkdtemp()
    WATCHES_PATH, DECISIONS_LOG = os.path.join(d, "w.json"), os.path.join(d, "d.jsonl")

    t0 = 1_800_000_000.0  # arbitrary fixed epoch

    def m15(t, o, h, l, c):
        return {"time": t, "open": o, "high": h, "low": l, "close": c, "tick_volume": 100}

    watches: dict[str, WatchEntry] = {}
    w = start_or_update_watch(watches, "TEST", "UP", 100.0, datetime.fromtimestamp(t0, tz=timezone.utc).isoformat(), 100.5)
    assert w.status == "WATCHING" and w.reevaluation_count == 0

    # Case 1: breakout -> no retest -> retest next candle -> SETUP_CONFIRMED
    bars_no_retest = [m15(t0 + 900, 100.5, 101.0, 100.4, 100.8)]  # still above level, no dip
    status, detail = reevaluate_watch(w, bars_no_retest, [{"time": t0, "high": 101, "low": 99, "close": 100.5},
                                                              {"time": t0 - 3600, "high": 100.3, "low": 99.5, "close": 100}], t0 + 900)
    assert status == "WATCHING", status
    dummy_h1 = [{"time": t0 - 3600, "high": 100.3, "low": 99.5, "close": 100}, {"time": t0, "high": 101, "low": 99, "close": 100.5}]
    bars_retest = bars_no_retest + [m15(t0 + 1800, 100.8, 100.9, 99.95, 100.6)]  # dips to touch 100, holds above
    status, detail = reevaluate_watch(w, bars_retest, dummy_h1, t0 + 1800)
    assert status == "SETUP_CONFIRMED", (status, detail)

    # Case 2: breakout -> price returns inside range -> INVALIDATED
    w2 = start_or_update_watch(watches, "TEST", "UP", 100.0,
                                 datetime.fromtimestamp(t0, tz=timezone.utc).isoformat(), 100.5)
    bars_fail = [m15(t0 + 900, 100.5, 100.6, 99.0, 99.2)]  # closes well below the level
    status, detail = reevaluate_watch(w2, bars_fail, dummy_h1, t0 + 900)
    assert status == "INVALIDATED", status

    # Case 3: no development for 90 minutes -> EXPIRED
    w3 = start_or_update_watch(watches, "TEST2", "UP", 100.0,
                                 datetime.fromtimestamp(t0, tz=timezone.utc).isoformat(), 100.5)
    status, detail = reevaluate_watch(w3, [m15(t0 + 6000, 100.5, 100.6, 100.4, 100.5)], dummy_h1, t0 + 91 * 60)
    assert status == "EXPIRED", status

    # Case 4: duplicate alert -> same watch, not a new one
    watches4: dict[str, WatchEntry] = {}
    a = start_or_update_watch(watches4, "TEST3", "UP", 200.0, datetime.fromtimestamp(t0, tz=timezone.utc).isoformat(), 200.1)
    b = start_or_update_watch(watches4, "TEST3", "UP", 200.05, datetime.fromtimestamp(t0 + 300, tz=timezone.utc).isoformat(), 200.2)
    assert a.watch_id == b.watch_id, "duplicate breakout (within tolerance) must reuse the existing watch"
    assert len(watches4) == 1
    assert b.original_alert_time_epoch == t0, "original alert time must not reset on a duplicate"

    # Case 5: missing H1 during re-evaluation -> fail closed
    w5 = start_or_update_watch(watches, "TEST4", "UP", 100.0,
                                 datetime.fromtimestamp(t0, tz=timezone.utc).isoformat(), 100.5)
    status, detail = reevaluate_watch(w5, [], [], t0 + 900)
    assert status == "ANALYSIS_INCOMPLETE", status

    print("self-test OK (watch cases 1-5)")


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        _self_test()
