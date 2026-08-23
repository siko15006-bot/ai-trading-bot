"""
ladder_guard_bybit.py — mirror of ladder_guard.py (MT5) for BAA (Bybit).
Every 15s: for every open position with a stop-loss, manage it progressively
toward locking in profit as price moves favorably. Naked positions (no SL)
are left to bybit_shield.py — this script only manages positions that
already have an SL.

Two paths, depending on whether a TP exists:

1) TP set (e.g. liquidity_sweep_bot's own trades): progress = fraction of
   the entry->TP distance covered. Steps the SL tighter as progress
   increases (>=25% cut original risk, >=50% breakeven, >=75% lock 50% of
   TP distance), and at >=90% converts to a runner: TP removed, SL trails
   25% of the original TP distance behind price.

2) No TP (manual entries, or anything only bybit_shield has touched):
   2026-07-26, Ahmed explicitly rejected inventing a TP/target for these
   ("اجعل الإصلاح إدارة آمنة للصفقات بلا TP وليس افترض TP من عندك") --
   this path NEVER assumes or sets a take-profit. It manages purely off R,
   the position's own real risk distance (entry to its actual SL, whether
   that SL came from the trader or from bybit_shield's ATR-based default).
   Progress is measured in R-multiples gained instead of %-of-TP: as the
   position moves 0.5R/1.0R/1.5R in its favor, the SL ratchets in the same
   step-wise way, and past 2R it converts to an uncapped trailing runner
   (trails 0.5R behind price, no TP ever set -- the trade can run as far
   as the market takes it). A position with an SL whose distance can't be
   computed (should not happen in practice) is logged as an explicit
   UNMANAGED POSITION rather than silently skipped.
"""
import json
import os
import time
from heartbeat import write_heartbeat  # 2026-08-06: functional heartbeat rollout
import ccxt
from dotenv import load_dotenv
from ntfy_alert import alert  # 2026-08-03: push Ahmed's phone even with no Claude session open

load_dotenv("/home/ubuntu/.env")

# 2026-07-26: read-only state export for the monitoring dashboard (Ahmed's
# request). Pure side-channel -- written after decisions are made, never
# read back by this script, never affects any set_stop call, threshold, or
# order. The dashboard reads this instead of recomputing R itself, so it can
# never show a different "original R" than the one actually driving the
# live ladder (Ahmed: "Never reset R because SL moved" applies to any
# display of R too, not just the trading logic).
STATE_FILE = "/tmp/ladder_guard_bybit_state.json"

INTERVAL = 15
STEPS = [(0.90, 0.70), (0.75, 0.40), (0.55, 0.0)]  # 2026-08-02: matched to the
# proven MT5 crypto ladder (ladder_guard.py STEPS_BY_CLASS["CRYPTO"]) per
# Ahmed's direct instruction, after tg_signal_bot/orb_bot/swing_pending_bybit
# were all found sharing the same premature-capping symptom (XAUUSDT 17% WR/
# -$2.82 net on this ladder) -- any TP-bearing position on this account was
# getting its stop tightened starting at just 25% progress, well before a
# real backtested edge gets a chance to run. Was: 25%/50%/75%/90% with a
# still-negative lock at the first rung; now: 55%/75%/90%, first rung is
# breakeven, matching the MT5 side exactly. STEPS values only -- nothing
# else in this file changed.
RUNNER_TRIGGER = 0.90
RUNNER_TRAIL_FRACTION = 0.25

# no-TP path (R-multiple based, see module docstring). Checked against
# RUNNER_TRIGGER_R first, same control flow as the TP-based path above.
R_STEPS = [(1.5, 0.5), (1.0, 0.0), (0.5, -0.5)]
RUNNER_TRIGGER_R = 2.0
RUNNER_TRAIL_R = 0.5

exchange = ccxt.bybit({
    "apiKey": os.environ["BYBIT_API_KEY"],
    "secret": os.environ["BYBIT_API_SECRET"],
    # 2026-07-26: Oracle's clock drifts a few seconds between NTP syncs (cloud
    # hypervisor jitter) — Bybit's default 5s recv_window rejected requests
    # during those windows ("invalid request... check server timestamp"),
    # silently breaking the ladder for however long the drift lasted. Widening
    # to 20s tolerates normal cloud clock jitter without weakening anything
    # else (Bybit still rejects genuinely stale/replayed requests past 20s).
    "options": {"recvWindow": 20000},
})

_runner_trail = {}  # symbol -> trail distance (price units)
_original_r = {}    # symbol -> R captured at first sight (no-TP path only, never recomputed)
_management = {}    # symbol -> "target" | "no_target", set once at first sight, kept stable
                     # afterward even if a target-path runner later zeroes its own TP field
                     # (dashboard-only bookkeeping, doesn't affect any trading decision)


def save_state():
    try:
        positions = {}
        for sym in set(_management) | set(_original_r) | set(_runner_trail):
            positions[sym] = {
                "management": _management.get(sym),
                "original_r": _original_r.get(sym),
                "is_runner": sym in _runner_trail,
                "runner_trail_dist": _runner_trail.get(sym),
            }
        state = {
            "updated_ts": time.time(),
            "constants": {
                "steps": STEPS, "runner_trigger": RUNNER_TRIGGER,
                "runner_trail_fraction": RUNNER_TRAIL_FRACTION,
                "r_steps": R_STEPS, "runner_trigger_r": RUNNER_TRIGGER_R,
                "runner_trail_r": RUNNER_TRAIL_R,
            },
            "positions": positions,
        }
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)  # atomic -- dashboard never reads a half-written file
    except Exception as e:
        log(f"save_state error: {str(e)[:150]}")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _round_tick(symbol, price):
    # Bybit rejects trading-stop updates that round to the same tick as the
    # current value (retCode 34040 "not modified") — sending an unrounded
    # float made this script retry the same rejected value every 15s forever
    # (found 2026-07-25, same bug class as MT5 ladder_guard's _round_price).
    try:
        return float(exchange.price_to_precision(symbol, price))
    except Exception:
        return round(price, 1)


def set_stop(symbol, sl=None, tp=None):
    params = {
        "category": "linear",
        "symbol": symbol.replace("/", "").replace(":USDT", ""),
        "slTriggerBy": "MarkPrice",
        "tpTriggerBy": "MarkPrice",
        "positionIdx": 0,
    }
    if sl is not None:
        params["stopLoss"] = str(round(sl, 6))
    if tp is not None:
        params["takeProfit"] = str(round(tp, 6)) if tp else "0"
    try:
        r = exchange.private_post_v5_position_trading_stop(params)
        # 2026-08-02: every set_stop call was logging (FAIL) in production
        # with zero exceptions raised -- only possible if the call succeeds
        # but the retCode comparison itself is wrong. Bybit's v5 API is
        # inconsistent about returning retCode as int 0 vs string "0"; accept
        # both instead of guessing which one this account/endpoint returns.
        log(f"DIAG raw set_stop response: {r}")
        return r.get("retCode") in (0, "0")
    except Exception as e:
        log(f"set_stop failed {symbol}: {str(e)[:150]}")
        alert(f"ladder_guard_bybit: set_stop failed {symbol}: {str(e)[:150]}", key="ladder_bybit_setstop_fail")
        return False


def _manage_with_target(symbol, buy, entry, price, sl, tp):
    dist = (tp - entry) if buy else (entry - tp)
    if dist <= 0:
        return
    progress = ((price - entry) if buy else (entry - price)) / dist

    if progress >= RUNNER_TRIGGER:
        trail_dist = RUNNER_TRAIL_FRACTION * dist
        new_sl = _round_tick(symbol, price - trail_dist if buy else price + trail_dist)
        better = (new_sl > sl) if buy else (new_sl < sl)
        if not better:
            _runner_trail[symbol] = trail_dist
            return
        ok = set_stop(symbol, sl=new_sl, tp=0)
        if ok:
            _runner_trail[symbol] = trail_dist
        log(f"{symbol} progress={progress:.0%} -> RUNNER (TP removed, trailing {trail_dist}) ({'OK' if ok else 'FAIL'})")
        return

    for threshold, lock in STEPS:
        if progress >= threshold:
            new_sl = _round_tick(symbol, entry + (lock * dist if buy else -lock * dist))
            better = (new_sl > sl) if buy else (new_sl < sl)
            if better:
                ok = set_stop(symbol, sl=new_sl, tp=tp)
                log(f"{symbol} progress={progress:.0%} SL {sl} -> {new_sl} ({'OK' if ok else 'FAIL'})")
            break


def _manage_no_target(symbol, buy, entry, price, sl):
    # R must be captured ONCE (the first time we see this position with a
    # valid SL) and reused for its whole lifetime -- recomputing from the
    # SL's current value would shrink R every time the ladder itself moves
    # the stop (e.g. R->0 right after a breakeven step), corrupting every
    # gain_R calculation after that (Ahmed, 2026-07-26: "ما نحسبش R من 100
    # -> 102 ... المرجع الأصلي يفضل 1R").
    if symbol not in _original_r:
        R0 = abs(entry - sl)
        if R0 <= 0:
            log(f"{symbol} UNMANAGED POSITION -- SL distance invalid (entry={entry} sl={sl})")
            return
        _original_r[symbol] = R0
    R = _original_r[symbol]
    gain_R = ((price - entry) if buy else (entry - price)) / R

    if gain_R >= RUNNER_TRIGGER_R:
        trail_dist = RUNNER_TRAIL_R * R
        new_sl = _round_tick(symbol, price - trail_dist if buy else price + trail_dist)
        better = (new_sl > sl) if buy else (new_sl < sl)
        if not better:
            _runner_trail[symbol] = trail_dist
            return
        ok = set_stop(symbol, sl=new_sl)  # tp intentionally omitted -- never set, never touched
        if ok:
            _runner_trail[symbol] = trail_dist
        log(f"{symbol} gain={gain_R:.2f}R (no-TP) -> RUNNER (trailing {trail_dist}) ({'OK' if ok else 'FAIL'})")
        return

    for threshold, lock_r in R_STEPS:
        if gain_R >= threshold:
            new_sl = _round_tick(symbol, entry + (lock_r * R if buy else -lock_r * R))
            better = (new_sl > sl) if buy else (new_sl < sl)
            if better:
                ok = set_stop(symbol, sl=new_sl)
                log(f"{symbol} gain={gain_R:.2f}R (no-TP) SL {sl} -> {new_sl} ({'OK' if ok else 'FAIL'})")
            break


def check_positions():
    seen = set()
    for p in exchange.fetch_positions():
        qty = float(p.get("contracts") or 0)
        if qty == 0:
            continue
        info = p.get("info", {})
        sl = float(info.get("stopLoss") or 0)
        tp = float(info.get("takeProfit") or 0)
        if not sl:
            continue  # naked — bybit_shield's job
        symbol = p["symbol"]
        seen.add(symbol)
        if symbol not in _management:
            _management[symbol] = "target" if tp else "no_target"
        side = p["side"]  # "long" or "short"
        buy = side == "long"
        entry = float(p["entryPrice"])
        price = float(p.get("markPrice") or entry)

        if symbol in _runner_trail:
            trail_dist = _runner_trail[symbol]
            new_sl = _round_tick(symbol, price - trail_dist if buy else price + trail_dist)
            better = (new_sl > sl) if buy else (new_sl < sl)
            if better:
                ok = set_stop(symbol, sl=new_sl, tp=0 if tp else None)
                log(f"{symbol} RUNNER trail SL {sl} -> {new_sl} ({'OK' if ok else 'FAIL'})")
            continue

        if tp:
            _manage_with_target(symbol, buy, entry, price, sl, tp)
        else:
            _manage_no_target(symbol, buy, entry, price, sl)

    # drop state for anything no longer open, so a later reopen on the same
    # symbol starts fresh instead of inheriting a stale R, trail distance,
    # or management-path classification
    for d in (_runner_trail, _original_r, _management):
        for sym in list(d.keys()):
            if sym not in seen:
                d.pop(sym, None)

    save_state()


def main():
    exchange.load_markets()
    log("ladder_guard_bybit started (BAA) — every 15s, TP-path: BE@50% lock50@75% lock75@90% runner@90% | "
        "no-TP path (R-based, never sets a target): lock@0.5R BE@1.0R lock0.5R@1.5R runner@2.0R")
    while True:
        try:
            check_positions()
            write_heartbeat('ladder_guard_bybit')
        except Exception as e:
            log(f"loop error: {str(e)[:150]}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
