"""alert_watch_loop.py -- 2026-08-16. Forward-looking, live, read-only
loop: while any WATCHING entry exists in alert_watches.json, checks each
one's symbol for a newly-CLOSED M15 candle and re-evaluates it (item 2:
"automatic re-run on every new M15 candle for the same symbol while
WATCHING"). Idle-sleeps (no MT5 calls at all) when there are zero active
watches, so this costs nothing when the gate hasn't flagged anything.

Research/analysis/alerting only -- no order-placing import anywhere in
this file (grep-verifiable), matching alert_decision_gate.py's and
alert_watch.py's boundary.
"""
import atexit
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, r"C:\TradingBot\Bot_Active")
import alert_watch as aw  # noqa: E402
from heartbeat import write_heartbeat  # noqa: E402 -- shared heartbeat writer, same one every other bot uses

POLL_SEC = 60
ACCOUNT_CFG = dict(path=r"C:\MT5_Portable_2\terminal64.exe",
                     login=<REDACTED_MT5_LOGIN_EA>, password="<REDACTED_MT5_PASSWORD_EA>", server="Exness-MT5Real33")

# 2026-08-16 op-hardening (Ahmed-approved, operational only -- no strategy/
# entry/timeout/risk logic touched): single-instance lock, same
# heartbeat-based pattern as gold_btc_bot.py's _acquire_lock (stale-timeout
# + PID-liveness double-check, so a hard crash can never permanently block
# a legitimate restart, and two instances can never run blind to each
# other and double-write alert_watches.json).
LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alert_watch_loop.lock")
LOCK_STALE_MULTIPLIER = 3


def log(msg):
    print(f'[{datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}] {msg}', flush=True)


def _touch_lock():
    tmp = LOCK_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "ts": time.time()}, f)
    os.replace(tmp, LOCK_PATH)


def _pid_alive(pid):
    """Same stdlib-only Windows liveness check as gold_btc_bot.py's
    _pid_alive -- a bare PID-exists check isn't enough (Windows recycles
    PIDs quickly), so this also confirms the PID's own command line still
    mentions this script."""
    try:
        out = subprocess.run(
            ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
        ).stdout
        return "alert_watch_loop.py" in out
    except Exception:
        return None


def _release_lock():
    try:
        with open(LOCK_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("pid") == os.getpid():
            os.remove(LOCK_PATH)
    except Exception:
        pass


def _acquire_lock():
    if os.path.exists(LOCK_PATH):
        try:
            with open(LOCK_PATH, encoding="utf-8") as f:
                data = json.load(f)
            owner_alive = _pid_alive(data.get("pid"))
            if owner_alive is False:
                pass  # confirmed dead -- take over
            else:
                age = time.time() - data["ts"]
                if age < POLL_SEC * LOCK_STALE_MULTIPLIER:
                    return False, data
        except Exception:
            pass  # unreadable/corrupt lock -- treat as stale, take over
    _touch_lock()
    return True, None


def _fetch(mt5, symbol):
    m15 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M15, 0, 60)
    h1 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 60)
    cols = ["time", "open", "high", "low", "close", "tick_volume"]
    to_dicts = lambda rows: [dict(zip(cols, [r[c] for c in cols])) for r in rows] if rows is not None else []
    return to_dicts(m15), to_dicts(h1)


def _connect_mt5(mt5):
    delay = 5
    while True:
        mt5.shutdown()
        if mt5.initialize(**ACCOUNT_CFG):
            return
        log(f"mt5 init failed {mt5.last_error()} -- retrying in {delay}s")
        time.sleep(delay)
        delay = min(60, delay * 2)


def run_once(mt5) -> int:
    """One pass over all active watches. Returns count of watches that
    changed state this pass (for observability only)."""
    watches = aw.load_watches()
    active = {k: w for k, w in watches.items() if w.status == "WATCHING"}
    if not active:
        return 0

    by_symbol: dict[str, list] = {}
    for w in active.values():
        by_symbol.setdefault(w.symbol, []).append(w)

    changed = 0
    now = time.time()
    for symbol, symbol_watches in by_symbol.items():
        try:
            m15_rows, h1_rows = _fetch(mt5, symbol)
        except Exception as e:
            log(f"{symbol} fetch failed: {e} -- all its watches fail closed this pass")
            for w in symbol_watches:
                aw.log_reevaluation(w, "ANALYSIS_INCOMPLETE", {"reason": f"fetch failed: {e}"},
                                     w.last_price or 0.0, "UNKNOWN", "UNKNOWN", now)
                changed += 1
            continue

        latest_closed_m15_time = max((b["time"] for b in m15_rows if b["time"] + 900 <= now), default=None)

        for w in symbol_watches:
            if latest_closed_m15_time is not None and w.last_bar_time_checked == latest_closed_m15_time:
                continue  # no new closed M15 candle since last check for this symbol -- item 2's exact trigger
            # latest_closed_m15_time is None (no data at all, e.g. bad symbol or full outage) -- must NOT
            # be treated as "nothing new, skip"; fall through so reevaluate_watch fails closed properly
            status, detail = aw.reevaluate_watch(w, m15_rows, h1_rows, now)
            price = detail.get("price", w.last_price)
            h1_state = f"{len(h1_rows)} H1 bars available"
            m15_state = f"{len(m15_rows)} M15 bars, latest closed @ {latest_closed_m15_time}"
            aw.log_reevaluation(w, status, detail, price, h1_state, m15_state, now)
            w.last_bar_time_checked = latest_closed_m15_time
            changed += 1
            log(f"{symbol} watch={w.watch_id} reeval#{w.reevaluation_count} -> {status} ({detail.get('reason')})")

    aw.save_watches(watches)
    return changed


def main():
    ok, existing = _acquire_lock()
    if not ok:
        log(f"ABORT -- another alert_watch_loop instance looks alive "
            f"(lock heartbeat {time.time() - existing['ts']:.0f}s old, pid={existing.get('pid')}). "
            f"Refusing to start a second one.")
        return
    atexit.register(_release_lock)

    import MetaTrader5 as mt5
    log("alert_watch_loop started -- READ-ONLY, no order-placing import in this process")
    _connect_mt5(mt5)
    while True:
        try:
            _touch_lock()
            if not mt5.terminal_info():
                _connect_mt5(mt5)
            n = run_once(mt5)
            active_count = sum(1 for w in aw.load_watches().values() if w.status == "WATCHING")
            write_heartbeat("alert_watch_loop", active_watches=active_count, changed_this_cycle=n)
        except Exception as e:
            log(f"loop error: {e}")
        time.sleep(POLL_SEC)


def _self_test():
    """ponytail: smallest check that fails if the lock logic breaks."""
    import tempfile
    global LOCK_PATH
    orig = LOCK_PATH
    d = tempfile.mkdtemp()
    LOCK_PATH = os.path.join(d, "test.lock")
    try:
        ok1, _ = _acquire_lock()
        assert ok1 is True, "first acquire should succeed"
        ok2, existing = _acquire_lock()
        assert ok2 is False and existing["pid"] == os.getpid(), "second acquire (same live pid) must be refused"
        _release_lock()
        assert not os.path.exists(LOCK_PATH), "release must remove our own lock"
        ok3, _ = _acquire_lock()
        assert ok3 is True, "acquire after release should succeed again"
        # simulate a stale lock from a dead pid
        with open(LOCK_PATH, "w", encoding="utf-8") as f:
            json.dump({"pid": 999999999, "ts": time.time()}, f)
        ok4, _ = _acquire_lock()
        assert ok4 is True, "a lock owned by a dead pid must be taken over immediately"
    finally:
        LOCK_PATH = orig
    print("self-test OK (lock acquire/refuse/release/stale-takeover)")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _self_test()
    else:
        main()
