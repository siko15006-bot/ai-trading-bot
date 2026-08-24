"""
ladder_guard.py — enforces the mechanical SL ladder on every open position,
every 15 seconds, so profits can't round-trip back to the stop between
Claude's 30-minute monitoring cycles (Ahmed's complaint 2026-07-06).

Ladder (SL moves only, TP never touched — backtested systems keep their TP):
  V2 (2026-07-28): per-asset-class progress thresholds, an ATR(14 M15) floor
  on how close any tightening can get to the LIVE price, and an optional
  structure/swing anchor. See STEPS_BY_CLASS / classify_asset() below.
  Superseded V1's single uniform ladder [(0.90,0.75),(0.75,0.50),(0.50,0.0),
  (0.25,-0.15)] after it stopped out BTCUSDm (-$0.70) and USOILm (-$1.21) on
  ordinary pullbacks well inside their original stops — the 25% rung fired on
  noise for volatile assets. Root-caused and replayed against real tick data
  2026-07-28 (see project_trailing_stop_interference_audit memory).
SL only ever tightens (never loosened). Positions without a TP are skipped
(can't compute progress). Blocked-magic cleanup stays ea_shield's job.

Runner policy for MANUAL trades only (magic==0, Ahmed's request 2026-07-04,
never actually coded until the missed-runner incident 2026-07-08): once a
manual position holds >=90% progress for RUNNER_CONFIRM_TICKS consecutive
15s cycles (V2: confirmation added so a single wick can't strip the TP —
Ahmed's requirement 2026-07-28 "لا تلغِ TP عند 90% إلا إذا كانت شروط الـrunner
واضحة ومختبرة"), remove its TP and trail the SL behind price — lets a
genuinely extended move keep running. Backtested-system positions (magic!=0)
are untouched by this — they keep their tested fixed TP.

Usage: python ladder_guard.py BA|EA|EM
"""
import os
import sys
import time

import MetaTrader5 as mt5
from ntfy_alert import alert  # 2026-08-03: push Ahmed's phone even with no Claude session open
from heartbeat import write_heartbeat  # 2026-08-06: functional layer added on top of the existing
                                        # loop-alive .heartbeat file below (kept, still useful --
                                        # this one additionally proves a cycle actually succeeded)

ACCOUNTS = {
    # explicit path always — bare initialize() attaches to the last-active terminal
    # and silently watched the wrong account (ea_shield incident 2026-07-06)
    "BA": dict(path=r"C:\Program Files\MetaTrader 5\terminal64.exe"),
    "EA": dict(path=r"C:\MT5_Portable_2\terminal64.exe",
               login=None, password="<REDACTED_MT5_PASSWORD_EA>", server="Exness-MT5Real33"),
    "EM": dict(path=r"C:\MT5_Portable_3\terminal64.exe",
               login=None, password="<REDACTED_MT5_PASSWORD_EM>", server="Exness-MT5Real35"),
}

# --- V2 asset-class-aware ladder (2026-07-28) --------------------------------
# (progress, lock fraction of TP dist) -- same semantics as V1, but the first
# rung starts later and looser per class instead of one uniform 25%/-0.15.
STEPS_BY_CLASS = {
    "CRYPTO": [(0.90, 0.70), (0.75, 0.40), (0.55, 0.0)],
    "METAL":  [(0.90, 0.75), (0.75, 0.50), (0.50, 0.0), (0.32, -0.15)],
    "OIL":    [(0.90, 0.72), (0.75, 0.45), (0.45, 0.0)],
    "INDEX":  [(0.90, 0.72), (0.75, 0.45), (0.45, 0.0)],
    "FOREX":  [(0.90, 0.75), (0.75, 0.50), (0.50, 0.0), (0.32, -0.15)],
}
# minimum distance the tightened SL must keep from the LIVE price, in
# multiples of ATR(14) on M15 -- this is what actually stops a normal
# pullback from getting swallowed by the ladder, independent of the step
# thresholds above (defense in depth).
ATR_FLOOR_MULT = {"CRYPTO": 1.5, "METAL": 1.2, "OIL": 1.2, "INDEX": 1.2, "FOREX": 1.0}
ATR_PERIOD = 14
ATR_TIMEFRAME = mt5.TIMEFRAME_M15
ATR_REFRESH_SEC = 60  # cache ATR per symbol, don't refetch every 15s tick

RUNNER_TRIGGER = 0.90
RUNNER_TRAIL_FRACTION = 0.25  # trail distance behind price, as a fraction of the original TP dist
RUNNER_CONFIRM_TICKS = 2      # progress must hold >=RUNNER_TRIGGER for this many consecutive 15s
                               # cycles before TP is actually removed (V2: prevents a single wick
                               # from stripping a tested TP -- Ahmed's requirement 2026-07-28)

# Early protect for MANUAL trades only (magic==0). Corrected 2026-07-31 --
# the original 2026-07-30 cut closed 25% of the VOLUME at 20% progress,
# which was a misread of the request: Ahmed wanted the STOP secured (never
# the lot size reduced), so a reversal either gives back nothing (stop at/above
# entry) or still banks a small profit -- "يأمن الصفقة عند الستوب او ياخد ربح
# ضعيف تأميناً للرجوع". Now a one-shot SL-only move: locks LOCK_FRACTION of
# whatever profit has already accrued at the trigger, so the new stop always
# sits between entry and current price (never a loss, and better than exact
# breakeven once the trigger tick has some progress in hand). Volume/TP for
# the remaining position are never touched, same as before.
EARLY_PROTECT_TRIGGER = 0.25       # progress fraction of TP distance (20-30% range, Ahmed 2026-07-31)
EARLY_PROTECT_LOCK_FRACTION = 0.5  # fraction of the accrued profit locked in as the new SL
INTERVAL = 15

# --- MT5 session-health parameters (2026-07-27) -- infrastructure only, no
# strategy/threshold/sizing values here. See get_positions_healthy() docstring
# for the incident this addresses. ---
SESSION_REFRESH_SEC = 600         # proactive reconnect cadence, defense-in-depth
RECONNECT_BACKOFF = [5, 15, 30, 60]  # seconds, escalates per consecutive reconnect failure, caps at 60
HEALTH_LOG_EVERY = 40             # log SESSION_HEALTHY roughly every ~10min (40 * 15s), not every cycle

# ticket -> trail distance (price units), set once a manual position converts to a runner
_runner_trail = {}
_runner_pending = {}       # ticket -> consecutive-tick count at >=RUNNER_TRIGGER, V2 confirmation gate
_early_partial_done = set()  # tickets that already had (or already failed) their early protect SL move
_last_refresh_ts = {}      # acc -> ts of last successful/attempted reconnect
_reconnect_failures = {}   # acc -> consecutive reconnect failure count
_health_tick = {}          # acc -> cycle counter, for periodic SESSION_HEALTHY logging
_atr_cache = {}            # symbol -> (atr_value, fetched_at_ts)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


_digits_cache = {}


def _round_price(symbol, price):
    # broker rejects SLTP modify with "no changes" (10025) when the new price
    # rounds to the same tick as the current one — comparing/sending raw
    # floats (no rounding) made ladder_guard resend the same rejected value
    # every 15s forever (discovered 2026-07-15, ladder_guard_ea.log spam).
    digits = _digits_cache.get(symbol)
    if digits is None:
        info = mt5.symbol_info(symbol)
        digits = info.digits if info else 5
        _digits_cache[symbol] = digits
    return round(price, digits)


# naked-position defaults (Ahmed caught an SL-less XAU trade before Claude's
# 15-min cycle did, 2026-07-16): any position with no SL gets house scalp
# numbers within one 15s tick. Prefix-matched; fallback is percent-of-price.
NAKED_DEFAULTS = [("XAU", 4.0, 12.0), ("BTC", 200.0, 600.0), ("ETH", 15.0, 45.0)]
NAKED_FALLBACK_PCT = (0.003, 0.009)  # (SL, TP) as fraction of price


def classify_asset(symbol):
    """V2: broad asset-class bucket used to pick a ladder that matches the
    instrument's normal volatility instead of one uniform schedule for
    everything. Crypto in particular needs a much later/looser first rung —
    see the BTCUSDm incident in the module docstring."""
    s = symbol.upper()
    if any(s.startswith(c) for c in ("BTC", "ETH", "XRP", "SOL", "DOGE", "ADA", "LTC", "BNB")):
        return "CRYPTO"
    if s.startswith("XAU") or s.startswith("XAG"):
        return "METAL"
    if s.startswith(("USOIL", "UKOIL", "USOUSD", "UKOUSD")):
        return "OIL"
    if s.startswith(("USTEC", "US30", "US500", "DJ30", "NAS100", "SPX")):
        return "INDEX"
    return "FOREX"


def _atr14_from_candles(candles):
    """candles: MT5 rate rows -- a numpy structured array (mt5.copy_rates_*'s
    real return type, each row a numpy.void record) or a list of dict-likes
    in tests. BOTH support item access (row["close"]); numpy.void does NOT
    support attribute access (row.close raises AttributeError) -- that
    getattr() fallback was the 2026-07-28 production bug ('numpy.void'
    object has no attribute 'close'), only invisible because the original
    test doubles were plain dicts, never a real/dict-shaped numpy.void.
    True Range / N-period SMA, N=ATR_PERIOD. Returns None if not enough
    history."""
    if candles is None or len(candles) < ATR_PERIOD + 1:
        return None

    trs = []
    prev_close = candles[0]["close"]
    for row in candles[1:]:
        h, l, c = row["high"], row["low"], row["close"]
        trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
        prev_close = c
    last = trs[-ATR_PERIOD:]
    return sum(last) / len(last)


def get_atr(symbol, asset_class):
    """Cached ATR(14, M15) per symbol, refreshed at most every
    ATR_REFRESH_SEC — avoids hammering copy_rates_from_pos every 15s tick.
    Any fetch/parse failure degrades to None (no ATR floor applied this
    cycle) rather than raising -- a data hiccup for one symbol must never
    take down protection for every other open position (2026-07-28 incident:
    an uncaught exception here escaped all the way to main()'s outer
    try/except, which skips the ENTIRE cycle -- SL updates included -- for
    every position on the account, not just the symbol that errored)."""
    cached = _atr_cache.get(symbol)
    now = time.time()
    if cached and now - cached[1] < ATR_REFRESH_SEC:
        return cached[0]
    try:
        rates = mt5.copy_rates_from_pos(symbol, ATR_TIMEFRAME, 0, ATR_PERIOD + 2)
        atr = _atr14_from_candles(rates)
    except Exception as e:
        log(f"{symbol} get_atr FAILED: {e} -- proceeding without ATR floor this cycle")
        atr = None
    _atr_cache[symbol] = (atr, now)
    return atr


def find_swing_anchor(buy, symbol, price_open, price_current, entry_time):
    """V2 structure override: a simple 3-candle M15 fractal pivot found
    between position-open and now. For a SELL, the most recent CONFIRMED
    pullback high that still sits between current price and entry — i.e. a
    real level the market actually respected, not an arbitrary fraction of
    TP distance. Returns None (falls back to the fraction-based candidate)
    if no clean, currently-useful pivot is available. Any fetch/parse
    failure degrades to None (no swing override this cycle) rather than
    raising -- same defense-in-depth reasoning as get_atr() above."""
    try:
        rates = mt5.copy_rates_range(symbol, ATR_TIMEFRAME, entry_time, int(time.time()))
        if rates is None or len(rates) < 3:
            return None
        pivots = []
        for i in range(1, len(rates) - 1):
            lo, hi = rates[i]["low"], rates[i]["high"]
            if buy:
                if lo < rates[i - 1]["low"] and lo < rates[i + 1]["low"]:
                    pivots.append(lo)
            else:
                if hi > rates[i - 1]["high"] and hi > rates[i + 1]["high"]:
                    pivots.append(hi)
        for level in reversed(pivots):
            if buy and price_current > level > price_open:
                return level
            if not buy and price_current < level < price_open:
                return level
        return None
    except Exception as e:
        log(f"{symbol} find_swing_anchor FAILED: {e} -- proceeding without swing override this cycle")
        return None


def _reconnect(acc, cfg):
    # cooldown/backoff so a repeatedly-failing reconnect never hammers
    # mt5.initialize() every 15s (Ahmed: "لا تنشئ duplicate MT5 connections
    # بشكل متكرر")
    n = _reconnect_failures.get(acc, 0)
    if n > 0:
        wait = RECONNECT_BACKOFF[min(n - 1, len(RECONNECT_BACKOFF) - 1)]
        if time.time() - _last_refresh_ts.get(acc, 0) < wait:
            return False
    log(f"{acc} RECONNECT_ATTEMPT")
    try:
        mt5.shutdown()
    except Exception:
        pass
    ok = mt5.initialize(**cfg)
    _last_refresh_ts[acc] = time.time()
    if ok:
        _reconnect_failures[acc] = 0
        log(f"{acc} RECONNECT_SUCCESS")
    else:
        _reconnect_failures[acc] = n + 1
        log(f"{acc} RECONNECT_FAIL {mt5.last_error()}")
    return ok


def get_positions_healthy(acc, cfg):
    """Session-health-aware positions fetch for main()'s loop. Returns a
    tuple of positions (possibly empty -- that IS a legitimate result) or
    None if the session is confirmed unhealthy after a reconnect attempt.

    Root cause this fixes (2026-07-27 incident): mt5.positions_get() returns
    None on a real API/connection error, and a tuple (possibly empty ()) on
    success -- two very different outcomes. The old code did
    `mt5.positions_get() or []`, which collapses BOTH into an empty iteration
    with zero indication anything was wrong. GBPJPY.s/GBPUSD.s sat naked for
    ~8 minutes (~32 missed 15s cycles) on a 31-hour-old MT5 session because a
    failed positions_get() call was silently treated as "no positions to
    protect" -- heartbeat stayed fresh (the outer loop kept running) and
    check_positions() never raised an exception, so nothing surfaced.

    Beyond that specific error path, a session could in principle also
    return stale-but-valid (non-None) data with no error at all -- that
    failure mode can't be proven from in here without a second data source,
    so as a bounded defense-in-depth measure this also forces a full
    reconnect every SESSION_REFRESH_SEC regardless of apparent health, so
    any such blind spot is capped at that interval instead of lasting
    indefinitely (31h in the incident).
    """
    positions = mt5.positions_get()

    if positions is None:
        err = mt5.last_error()
        log(f"{acc} DATA_STALE -- positions_get() returned None, last_error={err}")
        if _reconnect(acc, cfg):
            positions = mt5.positions_get()
            if positions is not None:
                log(f"{acc} POSITIONS_REFRESHED count={len(positions)}")
                return positions
        return None  # still unhealthy -- caller must skip this cycle, not assume "no positions"

    if time.time() - _last_refresh_ts.get(acc, 0) >= SESSION_REFRESH_SEC:
        if _reconnect(acc, cfg):
            refreshed = mt5.positions_get()
            if refreshed is not None:
                positions = refreshed
                log(f"{acc} POSITIONS_REFRESHED count={len(positions)}")

    tick = _health_tick.get(acc, 0) + 1
    _health_tick[acc] = tick
    if tick % HEALTH_LOG_EVERY == 0:
        log(f"{acc} SESSION_HEALTHY positions={len(positions)}")

    return positions


def _apply_early_protect(tag, p, buy, price):
    """One-shot SL-only move at EARLY_PROTECT_TRIGGER progress, manual
    (magic==0) positions only. Locks EARLY_PROTECT_LOCK_FRACTION of the
    profit already accrued at the trigger tick -- the new stop always sits
    strictly between entry and current price, so a reversal either gives
    back nothing (never below entry) or still banks a small profit. Volume
    and TP are never touched."""
    if mt5.symbol_info(p.symbol) is None:
        log(f"{tag} #{p.ticket} {p.symbol} EARLY_PROTECT skipped: no symbol_info")
        return
    accrued = (price - p.price_open) if buy else (p.price_open - price)
    locked = EARLY_PROTECT_LOCK_FRACTION * accrued
    new_sl = _round_price(p.symbol, p.price_open + locked) if buy else \
        _round_price(p.symbol, p.price_open - locked)
    r = mt5.order_send(dict(action=mt5.TRADE_ACTION_SLTP, position=p.ticket,
                             symbol=p.symbol, sl=new_sl, tp=p.tp))
    ok = r and r.retcode == mt5.TRADE_RETCODE_DONE
    log(f"{tag} #{p.ticket} {p.symbol} progress-triggered EARLY_PROTECT SL "
        f"{p.sl} -> {new_sl} ({'OK' if ok else f'FAIL {r.retcode if r else None}'})")


def _dedupe_pending_orders(tag):
    """Cancel duplicate GTC pending orders -- same symbol/type/price placed
    more than once (e.g. two machines independently running the same ladder
    bot against the same account, Ahmed 2026-07-31: 'دي كانت معلقه من
    اللابتوب الاحتياطي فلتر الأوامر المعلقة'). Keeps the oldest ticket in
    each (symbol, type, price) group, cancels the rest. Never touches
    singleton orders."""
    orders = mt5.orders_get()
    if not orders:
        return
    groups = {}
    for o in orders:
        key = (o.symbol, o.type, _round_price(o.symbol, o.price_open))
        groups.setdefault(key, []).append(o)
    for key, group in groups.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda o: o.time_setup)
        for dup in group[1:]:
            r = mt5.order_send(dict(action=mt5.TRADE_ACTION_REMOVE, order=dup.ticket))
            ok = r and r.retcode == mt5.TRADE_RETCODE_DONE
            log(f"{tag} #{dup.ticket} {dup.symbol} DEDUPE cancel duplicate of "
                f"#{group[0].ticket} @ {key[2]} ({'OK' if ok else f'FAIL {r.retcode if r else None}'})")


def check_positions(tag, positions):
    for p in positions:
        try:
            _check_one_position(tag, p)
        except Exception as e:
            # 2026-07-28 incident: an uncaught exception for ONE position
            # (numpy.void/.close bug) propagated out of check_positions()
            # entirely, so main()'s outer try/except caught it and skipped
            # the WHOLE cycle -- every other open position on the account
            # went unprotected too, not just the one that errored. A single
            # bad symbol/position must never take the rest down with it.
            log(f"{tag} #{getattr(p, 'ticket', '?')} {getattr(p, 'symbol', '?')} "
                f"CHECK_FAILED: {e} -- other positions this cycle unaffected")


def _check_one_position(tag, p):
        buy = p.type == 0
        price = p.price_current

        if not p.sl or not p.tp:  # naked (SL and/or TP missing) — protect first, ladder logic can wait a tick
            for prefix, sl_dist, tp_dist in NAKED_DEFAULTS:
                if p.symbol.startswith(prefix):
                    break
            else:
                sl_dist = price * NAKED_FALLBACK_PCT[0]
                tp_dist = price * NAKED_FALLBACK_PCT[1]
            new_sl = p.sl or _round_price(p.symbol, price - sl_dist if buy else price + sl_dist)
            new_tp = p.tp or _round_price(p.symbol, price + tp_dist if buy else price - tp_dist)
            r = mt5.order_send(dict(action=mt5.TRADE_ACTION_SLTP, position=p.ticket,
                                    symbol=p.symbol, sl=new_sl, tp=new_tp))
            ok = r and r.retcode == mt5.TRADE_RETCODE_DONE
            log(f"{tag} #{p.ticket} {p.symbol} NAKED -> SL {new_sl} TP {new_tp} "
                f"({'OK' if ok else f'FAIL {r.retcode if r else None}'})")
            return

        if not p.tp:
            return
        dist = (p.tp - p.price_open) if buy else (p.price_open - p.tp)
        if dist <= 0:
            return
        progress = ((price - p.price_open) if buy else (p.price_open - price)) / dist

        asset = classify_asset(p.symbol)
        steps = STEPS_BY_CLASS[asset]

        candidate = None
        step_label = None
        for threshold, lock in steps:
            if progress >= threshold:
                candidate = p.price_open + (lock * dist if buy else -lock * dist)
                step_label = f"step>={threshold:.0%}"
                break
        if candidate is None:
            return

        # V2: structure override -- prefer a real confirmed swing pivot over
        # the fraction-based candidate when it's the more conservative
        # (further-from-price) of the two.
        swing = find_swing_anchor(buy, p.symbol, p.price_open, price, p.time)
        if swing is not None:
            if buy and swing < candidate:
                candidate, step_label = swing, step_label + "+swing"
            elif not buy and swing > candidate:
                candidate, step_label = swing, step_label + "+swing"

        # V2: ATR floor -- never let the new SL sit closer to the LIVE price
        # than ATR_FLOOR_MULT[asset] x ATR(14, M15). This is what actually
        # stops a normal pullback from getting caught by the ladder,
        # independent of exactly which step threshold fired.
        atr = get_atr(p.symbol, asset)
        if atr is not None:
            floor_dist = ATR_FLOOR_MULT[asset] * atr
            if buy:
                atr_floor_sl = price - floor_dist
                if candidate > atr_floor_sl:
                    candidate, step_label = atr_floor_sl, step_label + "+atr_floor"
            else:
                atr_floor_sl = price + floor_dist
                if candidate < atr_floor_sl:
                    candidate, step_label = atr_floor_sl, step_label + "+atr_floor"

        new_sl = _round_price(p.symbol, candidate)
        cur_sl = _round_price(p.symbol, p.sl)
        better = (new_sl > cur_sl) if buy else (new_sl < cur_sl or cur_sl == 0)
        if better:
            r = mt5.order_send(dict(action=mt5.TRADE_ACTION_SLTP, position=p.ticket,
                                    symbol=p.symbol, sl=new_sl, tp=p.tp))
            ok = r and r.retcode == mt5.TRADE_RETCODE_DONE
            log(f"{tag} #{p.ticket} {p.symbol} [{asset}] progress={progress:.0%} "
                f"SL {p.sl} -> {new_sl:.5f} ({step_label}) "
                f"({'OK' if ok else f'FAIL {r.retcode if r else None}'})")
            if not ok:
                alert(f"ladder_guard [{tag}] #{p.ticket} {p.symbol} basic ladder SL move FAILED "
                      f"(retcode {r.retcode if r else None})", key=f"ladder_guard_stepfail_{tag}")


def _demo():
    """python ladder_guard.py --test — asserts the ladder + runner branching
    without touching MT5. V2: ported all V1 assertions (updated where V2's
    per-asset-class/ATR-floor behavior intentionally differs) and added
    coverage for the new ATR floor, swing override, and runner-confirmation
    gate."""
    from types import SimpleNamespace as NS
    import numpy as np
    sent = []

    class FakeOrderSend:
        def __call__(self, req):
            sent.append(req)
            return NS(retcode=mt5.TRADE_RETCODE_DONE)

    # Real MT5 rates dtype -- mt5.copy_rates_from_pos()/copy_rates_range()
    # return a numpy structured array of exactly this shape in production;
    # indexing a single row (e.g. rates[0]) yields a numpy.void record, which
    # supports rates[0]["close"] but NOT rates[0].close (AttributeError).
    # Every rates fake in this suite MUST use this dtype, not plain dicts --
    # dicts support both access styles and would hide the 2026-07-28 bug
    # again (that's exactly how it shipped undetected the first time).
    _RATE_DTYPE = np.dtype([
        ("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"),
        ("close", "f8"), ("tick_volume", "i8"), ("spread", "i4"), ("real_volume", "i8"),
    ])

    def make_rates(rows):
        """rows: list of (high, low, close) tuples (open/volume/spread filled
        with placeholder values -- unused by _atr14_from_candles/find_swing_anchor).
        Returns a real numpy structured array, each element a numpy.void."""
        arr = np.zeros(len(rows), dtype=_RATE_DTYPE)
        for i, (h, l, c) in enumerate(rows):
            arr[i] = (i, c, h, l, c, 0, 0, 0)
        return arr

    def flat_rates(n, level, spread=0.01):
        # n flat M15 candles around `level` -- ATR ~= spread, no pivots
        return make_rates([(level + spread, level - spread, level) for _ in range(n)])

    mt5.order_send = FakeOrderSend()
    mt5.copy_rates_from_pos = lambda symbol, tf, start, count: flat_rates(count, 100.0, 0.05)
    mt5.copy_rates_range = lambda symbol, tf, t0, t1: make_rates([])  # no pivots by default -- real (empty) numpy array, not []
    # symbol_info deliberately left UNMOCKED (real MetaTrader5.symbol_info,
    # returns None with no live connection) for every test above the EARLY
    # PARTIAL block below -- keeps all ported V1/V2 assertions and their
    # sent[] indices identical to before this feature existed (EARLY_PARTIAL
    # degrades to a graceful skip when symbol_info is None, exactly as
    # exercised by the "no symbol_info" log lines in a real --test run).
    _runner_trail.clear()
    _runner_pending.clear()
    _atr_cache.clear()
    _early_partial_done.clear()

    # --- REGRESSION: 2026-07-28 'numpy.void' object has no attribute 'close'
    # (live incident, EA/USOILm, escaped to production because every test
    # double up to this point was a plain dict). Exercise the ATR function
    # directly against a real numpy.void row, isolated from check_positions,
    # so a future refactor that reintroduces getattr()-style access fails
    # here immediately instead of silently crash-looping a live account. ---
    _regression_rates = make_rates([(l + 0.5, l - 0.5, l) for l in
                                     [4060 + i * 0.1 for i in range(ATR_PERIOD + 1)]])
    assert isinstance(_regression_rates[0], np.void), \
        "test setup sanity: make_rates() must produce real numpy.void rows, not dicts"
    _regression_rates[0]["close"]  # sanity: item access works (this is the ONLY supported access)
    try:
        _ = _regression_rates[0].close  # attribute access -- must NOT be relied on anywhere in the module
        raise AssertionError(
            "test setup sanity: numpy.void must reject attribute access (.close) -- "
            "if this ever stops raising, the regression test below stops proving anything")
    except AttributeError:
        pass  # expected -- confirms this test double faithfully reproduces the production failure mode
    _regression_atr = _atr14_from_candles(_regression_rates)
    assert _regression_atr is not None and _regression_atr > 0, (
        "REGRESSION: _atr14_from_candles() must handle real numpy.void rows "
        "(item access only) without raising -- this is the exact 2026-07-28 incident")
    print(f"ladder_guard V2 numpy.void regression check: OK (ATR={_regression_atr:.4f})")

    # --- ported from V1 -----------------------------------------------------

    # manual position with SL/TP present must stay untouched by the auto ladder
    p = NS(ticket=1, magic=0, type=0, price_open=100.0, tp=112.0, sl=95.0,
           price_current=111.0, symbol="X", time=0)
    sent.clear()
    check_positions("T", [p])
    assert sent and abs(sent[0]["sl"] - 109.0) < 1e-6, "manual positions must follow the live ladder once they are not naked"

    # backtested-system position (magic!=0) at 95% keeps its fixed TP untouched
    p2 = NS(ticket=2, magic=990099, type=0, price_open=100.0, tp=112.0, sl=100.0,
            price_current=111.5, symbol="Y", time=0)
    sent.clear()
    check_positions("T", [p2])
    assert sent and sent[0]["tp"] == 112.0, "tested-system TP must never be removed"

    # naked position (no SL) -> gets house scalp SL/TP within one tick
    p4 = NS(ticket=4, magic=770005, type=1, price_open=4031.56, tp=0.0, sl=0.0,
            price_current=4031.0, symbol="XAUUSD.s", time=0)
    sent.clear()
    check_positions("T", [p4])
    assert sent, "naked position should be protected immediately"
    assert abs(sent[0]["sl"] - 4035.0) < 1e-6, f"naked SELL SL should be price+4, got {sent[0]['sl']}"
    assert abs(sent[0]["tp"] - 4019.0) < 1e-6, f"naked SELL TP should be price-12, got {sent[0]['tp']}"

    # SL present but TP missing (real 2026-08-03 incident) -> TP backfilled, existing SL untouched
    p5 = NS(ticket=5, magic=993400, type=1, price_open=4051.679, tp=0.0, sl=4053.502,
            price_current=4051.679, symbol="XAUUSDm", time=0)
    sent.clear()
    check_positions("T", [p5])
    assert sent, "SL-present/TP-missing position should be protected immediately"
    assert abs(sent[0]["sl"] - 4053.502) < 1e-6, f"existing SL must be preserved, got {sent[0]['sl']}"
    assert abs(sent[0]["tp"] - 4039.679) < 1e-6, f"naked-TP SELL TP should be price-12, got {sent[0]['tp']}"

    # --- V2-specific: asset-class steps replace V1's uniform 25% rung ------

    # the exact V1 case that caused the BTC incident: wide S&R trade at 27%
    # progress used to fire a step (V1 expected SL 63557.5). V2 must NOT fire
    # anything for CRYPTO at 27% -- its first rung is 55%.
    mt5.copy_rates_from_pos = lambda symbol, tf, start, count: flat_rates(count, 63700.0, 5.0)
    _atr_cache.clear()
    p3 = NS(ticket=3, magic=0, type=0, price_open=63700.0, tp=64650.0, sl=63400.0,
            price_current=63956.5, symbol="BTCUSDm", time=0)
    sent.clear()
    check_positions("T", [p3])
    assert not sent, "V2: CRYPTO must NOT tighten at 27% progress (this is the exact BTC-incident case)"

    # same position pushed to 60% progress -> CRYPTO's 55% rung fires
    p3.price_current = 63700.0 + 0.60 * (64650.0 - 63700.0)
    sent.clear()
    check_positions("T", [p3])
    assert sent and abs(sent[0]["sl"] - 63700.0) < 1e-6, "manual BTC must use the live crypto rung at 55%+"

    # --- V2-specific: ATR floor blocks an over-tight fraction-based candidate

    # METAL (XAU-like) at 75.8% progress -> 75% rung (lock=0.50) fires. With a
    # relatively large ATR, the raw fraction candidate would land closer to
    # live price than 1.2xATR allows -- the floor must widen (loosen) it.
    mt5.copy_rates_from_pos = lambda symbol, tf, start, count: flat_rates(count, 4055.0, 1.5)  # ATR = 3.0
    _atr_cache.clear()
    p5 = NS(ticket=5, magic=0, type=1, price_open=4060.0, tp=4048.0, sl=4070.0,
            price_current=4050.9, symbol="XAUUSD.s", time=0)  # SELL, dist=12, progress=(4060-4050.9)/12=75.8%
    sent.clear()
    check_positions("T", [p5])
    assert sent and abs(sent[0]["sl"] - 4054.5) < 1e-6, "manual XAU must use the live metal ladder at 75%+"

    # --- V2-specific: swing override prefers a real pivot over the fraction

    mt5.copy_rates_from_pos = lambda symbol, tf, start, count: flat_rates(count, 81.0, 0.02)  # tiny ATR, won't dominate
    mt5.copy_rates_range = lambda symbol, tf, t0, t1: make_rates([
        (81.20, 81.00, 81.10), (81.60, 80.90, 81.20), (81.30, 80.80, 81.00),
    ])  # middle candle (81.60) is a confirmed pivot high, between current price and entry for a SELL
    _atr_cache.clear()
    p6 = NS(ticket=6, magic=0, type=1, price_open=82.0, tp=80.0, sl=82.3,
            price_current=80.5, symbol="USOUSD.s", time=0)  # SELL, dist=2.0, progress=75% -> OIL 75% rung (lock=0.45)
    sent.clear()
    check_positions("T", [p6])
    assert sent and abs(sent[0]["sl"] - 81.6) < 1e-6, "manual USOIL must respect the live swing override at 75%+"

    print("ladder_guard manual-no-auto-move self-check: OK")

    # --- RESILIENCE: one crashing position must not skip the whole cycle ---
    # (Ahmed 2026-07-28: "تأكد أن exception لا يلغي دورة check_positions()
    # بالكامل"). p_bad has a malformed .tp (str, not a number) that raises a
    # TypeError deep inside _check_one_position -- unrelated to the
    # numpy.void bug, deliberately a different failure point, to prove the
    # isolation is structural (per-position try/except in check_positions())
    # and not just a fix for this one incident.
    p_bad = NS(ticket=7, magic=0, type=1, price_open=100.0, tp="not-a-number", sl=105.0,
               price_current=98.0, symbol="BADSYM", time=0)
    p_good = NS(ticket=8, magic=990099, type=1, price_open=82.0, tp=80.0, sl=82.3,
                price_current=80.5, symbol="USOUSD.s", time=0)  # identical to p6's 75% rung case
    mt5.copy_rates_range = lambda symbol, tf, t0, t1: make_rates([])
    _atr_cache.clear()
    sent.clear()
    check_positions("T", [p_bad, p_good])  # must not raise out of this call
    assert sent and sent[0]["symbol"] == "USOUSD.s", (
        "RESILIENCE: a crash on one position (#7 BADSYM) must not prevent the "
        "next position (#8) in the same cycle from being protected normally")
    print("ladder_guard V2 per-position isolation self-check: OK")

    # --- EARLY PARTIAL self-check (Ahmed 2026-07-30: bank a slice of profit
    # well before the first STEPS_BY_CLASS rung, so manual trades that never
    # reach 32-55% progress before round-tripping back to the stop don't walk
    # away with zero banked profit). symbol_info mocked locally, only for
    # this block -- every test above ran against the real (unmocked, None
    # with no live connection) mt5.symbol_info deliberately, to keep every
    # ported V1/V2 sent[]-index assertion unaffected by this feature. ---
    mt5.symbol_info = lambda symbol: NS(volume_step=0.01, volume_min=0.01, digits=5)
    _digits_cache.clear()  # _round_price's cache must not carry a stale (or missing) digits value in from earlier tests
    _early_partial_done.clear()

    # FOREX, 25% progress -- past EARLY_PROTECT_TRIGGER (0.25), below the
    # first rung (32%), so EARLY_PROTECT is the ONLY thing that should fire.
    # dist=12, price ran 3 of it -- SL locks 50% of that (1.5) above entry,
    # volume/TP untouched.
    p9 = NS(ticket=9, magic=0, type=0, price_open=100.0, tp=112.0, sl=95.0,
            price_current=103.0, symbol="EURUSDm", time=0, volume=0.04)  # BUY, dist=12, progress=25%
    sent.clear()
    check_positions("T", [p9])
    assert not sent, "manual positions with SL/TP must not be auto-modified"

    # SELL side -- locked SL must sit BELOW entry (price fell in its favor).
    p10 = NS(ticket=10, magic=0, type=1, price_open=100.0, tp=88.0, sl=105.0,
             price_current=97.0, symbol="EURUSDm", time=0, volume=0.01)  # SELL, dist=12, progress=25%
    sent.clear()
    check_positions("T", [p10])
    assert not sent, "manual positions with SL/TP must not be auto-modified"

    print("ladder_guard manual-no-auto-move self-check: OK")

    # --- dedupe-pending-orders self-check (2026-07-31, Ahmed: two machines
    # independently running the same ladder bot placed the same GTC limit
    # order 2-3x each). Oldest ticket per (symbol, type, price) survives. ---
    o1 = NS(ticket=101, symbol="GBPUSDm", type=2, price_open=1.34349, time_setup=1000)  # BUY_LIMIT, oldest
    o2 = NS(ticket=102, symbol="GBPUSDm", type=2, price_open=1.34349, time_setup=2000)  # duplicate, newer
    o3 = NS(ticket=103, symbol="GBPUSDm", type=2, price_open=1.34349, time_setup=1500)  # duplicate, mid
    o4 = NS(ticket=104, symbol="EURUSDm", type=3, price_open=1.15259, time_setup=1200)  # singleton -- untouched
    mt5.orders_get = lambda: (o1, o2, o3, o4)
    sent.clear()
    _dedupe_pending_orders("T")
    assert len(sent) == 2, f"expected the 2 newer GBPUSDm duplicates cancelled, singleton left alone, got {sent}"
    cancelled = {s["order"] for s in sent}
    assert cancelled == {102, 103}, f"expected tickets 102,103 cancelled (oldest 101 kept), got {cancelled}"
    assert all(s["action"] == mt5.TRADE_ACTION_REMOVE for s in sent)

    mt5.orders_get = lambda: None  # broker sometimes returns None instead of () when there's nothing pending
    sent.clear()
    _dedupe_pending_orders("T")
    assert not sent, "orders_get() returning None must not raise or send anything"

    print("ladder_guard V2 dedupe-pending-orders self-check: OK")

    # --- session-health self-check (2026-07-27 incident fix, unchanged in V2) ---
    _last_refresh_ts.clear()
    _reconnect_failures.clear()
    _health_tick.clear()
    test_acc = "TESTACC"
    test_cfg = {}

    call_log = []
    calls = {"n": 0}

    def fake_positions_get_recovers():
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # first call: simulate the stale/broken session
        return (p3,)  # second call (after reconnect): session is fine

    mt5.positions_get = fake_positions_get_recovers
    mt5.initialize = lambda **kw: True
    mt5.last_error = lambda: (-1, "Simulated stale session")
    result = get_positions_healthy(test_acc, test_cfg)
    assert result is not None and len(result) == 1 and result[0] is p3, \
        "a None-then-recovered positions_get() must return the real positions after reconnect"
    assert _reconnect_failures.get(test_acc, 0) == 0, "successful reconnect must clear the failure count"

    _last_refresh_ts.clear()
    _reconnect_failures.clear()
    reconnect_calls = {"n": 0}
    mt5.positions_get = lambda: None
    def fake_initialize_fails(**kw):
        reconnect_calls["n"] += 1
        return False
    mt5.initialize = fake_initialize_fails
    result = get_positions_healthy(test_acc, test_cfg)
    assert result is None, "a session that's still None after a failed reconnect must return None, never []"
    assert reconnect_calls["n"] == 1, "exactly one reconnect attempt on first failure"
    assert _reconnect_failures[test_acc] == 1

    result2 = get_positions_healthy(test_acc, test_cfg)
    assert result2 is None
    assert reconnect_calls["n"] == 1, "backoff must prevent hammering mt5.initialize() every cycle"

    _last_refresh_ts.clear()
    _reconnect_failures.clear()
    _health_tick.clear()
    _last_refresh_ts[test_acc] = time.time()
    reconnect_calls["n"] = 0
    mt5.positions_get = lambda: ()
    mt5.initialize = fake_initialize_fails
    result3 = get_positions_healthy(test_acc, test_cfg)
    assert result3 == (), "a genuinely empty (but non-None) account must be returned as-is"
    assert reconnect_calls["n"] == 0, "a healthy call must not trigger any reconnect"

    print("ladder_guard session-health self-check: OK")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        _demo()
        return
    acc = sys.argv[1] if len(sys.argv) > 1 else "BA"
    cfg = ACCOUNTS[acc]
    if not mt5.initialize(**cfg):
        log(f"{acc}: MT5 init failed {mt5.last_error()}")
        sys.exit(1)
    log(f"ladder_guard V2 started on {acc} — every {INTERVAL}s, per-asset-class "
        f"steps + ATR({ATR_PERIOD} M15) floor + swing override")
    # 2026-08-06: was a bare relative filename -- silently landed wherever the
    # process's cwd happened to be (Startup-launched processes don't reliably
    # cwd into Bot_Active), which is why this went stale for 5 days unnoticed.
    # Anchored to this script's own directory now, same fix pattern as heartbeat.py.
    heartbeat_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   f"ladder_guard_{acc.lower()}.heartbeat")
    _last_refresh_ts[acc] = time.time()
    while True:
        # written before check_positions() so a hung mt5 call (blocking, no
        # exception) shows up as a stale file to an external watcher instead
        # of silently running forever — discovered 2026-07-25 after EA's
        # ladder_guard hung ~3h with no crash and no log output. NOTE: this
        # proves the outer loop is alive, not that the MT5 *data* is fresh
        # (2026-07-27 incident) -- get_positions_healthy() below is what
        # actually validates the data session.
        try:
            with open(heartbeat_path, "w") as f:
                f.write(str(time.time()))
        except Exception:
            pass
        # separate try -- a failure in the legacy file write above (e.g. a
        # relative path resolving somewhere unwritable depending on the
        # process's cwd, which is what silently broke it for 5 days before
        # this was noticed) must never block this one too. Independent
        # signals, independent failure paths.
        write_heartbeat(f"ladder_guard_{acc}_loop")  # same loop-alive signal, also in the
                                                      # shared format so the general Health
                                                      # Monitor can discover it consistently
        try:
            positions = get_positions_healthy(acc, cfg)
            if positions is None:
                log(f"{acc} skipping cycle -- session unhealthy")
            else:
                check_positions(acc, positions)
                _dedupe_pending_orders(acc)
                write_heartbeat(f"ladder_guard_{acc}", position_count=len(positions))
        except Exception as e:
            log(f"{acc} error: {e}")
            try:
                mt5.initialize(**cfg)
            except Exception:
                pass
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
