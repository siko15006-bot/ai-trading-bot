"""
swing_pending_bybit.py -- BAA (Bybit) port of swing_pending_bot.py (MT5).
Same mechanical swing-fractal level-marker (BTC/ETH/XAU -- no forex symbols
exist on Bybit; XAU added 2026-08-02 per Ahmed's direct instruction, a
BAA-only exception to the standing gold exclusion on the MT5 side -- see the
MT5 version's docstring and the SYMBOLS comment below). Every CHECK_INTERVAL
seconds, per symbol:
  1. Fetch M15 OHLCV, find the most recent CONFIRMED 3-candle fractal swing
     high and swing low (identical pivot definition to the MT5 version).
  2. Compute SL = SL_ATR_MULT x ATR14.
  3. Place (or replace, if the pivot moved) a resting BUY LIMIT at the swing
     low and a SELL LIMIT at the swing high -- two-sided, no directional guess.

Design difference from the MT5 version, deliberate (2026-08-02): orders here
carry an SL but NO take-profit. ladder_guard_bybit.py has no per-bot
exclusion mechanism like MT5's ea_shield SAFE_MAGICS -- ANY position with a
TP set gets its aggressive %-of-TP ladder (25%/50%/75% cuts, same class of
premature-capping bug just fixed on the MT5 side, see swing_pending_bot.py's
MAGIC constant docstring). Bybit has no magic-number equivalent to exempt
from that. Going SL-only routes this bot through ladder_guard_bybit.py's
OTHER path instead -- the no-TP, R-multiple-based ladder purpose-built for
"a real risk anchor, no artificial cap" (0.5R/1.0R/1.5R steps, uncapped
runner past 2R). Same proven system already used for liquidity_sweep_bot's
sibling paths -- not a new mechanism, just routing into the correct existing
one for what this bot actually needs.

Usage: venv/bin/python3 swing_pending_bybit.py
"""
import json
import os
import signal
import time
from datetime import datetime, timezone
from uuid import uuid4
from heartbeat import write_heartbeat  # 2026-08-06: functional heartbeat rollout
from attribution_hooks import make_tag, parse_tag
from oracle_entry_freeze import entry_freeze_status
from risk_halt_gate import is_portfolio_halted  # 2026-08-08: portfolio-wide RISK_HALT, synced from Windows

import ccxt
from dotenv import load_dotenv

load_dotenv("/home/ubuntu/.env")

SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "XAU/USDT:USDT"]
# XAU added 2026-08-02 per Ahmed's direct instruction ("خليه يشتغل دهب كمان،
# كده كده بيبقى ضعيف للربح والخسارة") -- an explicit exception to the
# standing 2026-07-30 exclusion (gold stays manual/Claude-judgment only on
# the MT5 side, see swing_pending_bot.py's docstring). BAA-only, not
# mirrored back to EA/BA.
QTY = {"BTC/USDT:USDT": 0.001, "ETH/USDT:USDT": 0.01, "XAU/USDT:USDT": 0.01}
LEV = 10

# 2026-08-19 (Ahmed-approved, analytics-driven): new XAU entries paused.
# TAG-attributed (orderLinkId, 100% separated from tg_signal_bot, fee-clean
# -- Bybit charges $0 fee on this contract for this account, confirmed from
# raw fee data): 96 closed trades, WR 34.4%, PF 0.21, Net -$20.36; last 7
# days alone: 66 trades, PF 0.49, Net -$5.24 -- still negative after clean
# separation. BTC (PF 3.89) and ETH (PF 2.67) are unaffected and keep
# running exactly as before. This does NOT touch position management --
# ladder_guard_bybit.py manages any existing open XAU position independently
# of this bot, which only ever places NEW resting entry orders.
PAUSED_SYMBOLS = {"XAU/USDT:USDT"}

TIMEFRAME = "15m"
ATR_PERIOD = 14
SL_ATR_MULT = 1.5   # same as the MT5 version -- widened 2026-08-01 after the 0.8x whipsaw incident
MIN_PIVOT_AGE_BARS = 2
LEVEL_MOVE_TOL_MULT = 0.5
CHECK_INTERVAL = 900
ORDER_TAG = "swing_pending"

_placed_levels = {}  # (symbol, "high"|"low") -> (price, order_id)
_adopted_positions = set()  # (symbol, "high"|"low") broker-live position adopted at startup
LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "swing_pending_bybit.lock")
_RUN_ID = None
_LOCK_HELD = False

exchange = ccxt.bybit({
    "apiKey": os.environ["BYBIT_API_KEY"],
    "secret": os.environ["BYBIT_API_SECRET"],
})


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _read_lock_record():
    if not os.path.exists(LOCK_PATH):
        return None
    try:
        with open(LOCK_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _write_lock_record():
    payload = {
        "pid": os.getpid(),
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_id": _RUN_ID,
    }
    tmp = LOCK_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, LOCK_PATH)


def _acquire_single_instance_lock():
    global _LOCK_HELD
    record = _read_lock_record()
    if record is not None:
        pid = record.get("pid")
        if pid is not None and _pid_alive(pid):
            return False, {
                "status": "LOCKED",
                "lock_pid": pid,
                "lock_started_at_utc": record.get("started_at_utc"),
                "run_id": record.get("run_id"),
                "mismatch_reason": "live pid already holds lock",
            }
        try:
            os.remove(LOCK_PATH)
        except Exception:
            pass
    _write_lock_record()
    _LOCK_HELD = True
    return True, {
        "status": "LOCK_ACQUIRED",
        "lock_pid": os.getpid(),
        "lock_started_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_id": _RUN_ID,
        "mismatch_reason": None,
    }


def _release_single_instance_lock():
    global _LOCK_HELD
    if not _LOCK_HELD:
        return
    try:
        os.remove(LOCK_PATH)
    except Exception:
        pass
    _LOCK_HELD = False


def _order_link_id(order):
    info = order.get("info") or {}
    for key in ("orderLinkId", "clientOrderId"):
        value = order.get(key) or info.get(key)
        if value:
            return str(value)
    return None


def _attribution_evidence(order, symbol=None):
    tag = _order_link_id(order)
    parsed = parse_tag(tag) if tag else None
    if not parsed:
        return None
    if parsed.get("strategy") != ORDER_TAG and not (tag.startswith(ORDER_TAG) if tag else False):
        return None
    info = order.get("info") or {}
    return {
        "order_id": order.get("id"),
        "symbol": order.get("symbol") or symbol,
        "side": order.get("side"),
        "price": order.get("price"),
        "orderLinkId": tag,
        "timestamp": order.get("timestamp") or int(info.get("createdTime") or 0) or None,
        "datetime": order.get("datetime") or None,
        "strategy": parsed.get("strategy"),
        "bot": parsed.get("bot"),
        "symbol_tag": parsed.get("symbol"),
        "direction_tag": parsed.get("direction"),
        "event_id": parsed.get("event_id"),
        "parent": parsed.get("parent"),
    }


def _fetch_open_orders_with_retry(symbol, max_retries=3, retry_sleep_sec=5):
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            orders = exchange.fetch_open_orders(symbol) or []
            return orders, None
        except Exception as exc:
            last_error = str(exc)
            log(f"{symbol} fetch_open_orders attempt {attempt}/{max_retries} failed: {last_error[:180]}")
            if attempt < max_retries:
                time.sleep(retry_sleep_sec * attempt)
    return None, last_error


def _fetch_positions_with_retry(symbol, max_retries=3, retry_sleep_sec=5):
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            positions = exchange.fetch_positions([symbol]) or []
            return positions, None
        except Exception as exc:
            last_error = str(exc)
            log(f"{symbol} fetch_positions attempt {attempt}/{max_retries} failed: {last_error[:180]}")
            if attempt < max_retries:
                time.sleep(retry_sleep_sec * attempt)
    return None, last_error


def _position_size(position):
    info = position.get("info") or {}
    for key in ("contracts", "size"):
        value = position.get(key) if key in position else info.get(key)
        try:
            if value is not None:
                return abs(float(value))
        except Exception:
            pass
    return 0.0


def _position_side_key(position):
    info = position.get("info") or {}
    side = str(position.get("side") or info.get("side") or "").lower()
    if side in ("long", "buy"):
        return "low"
    if side in ("short", "sell"):
        return "high"
    return None


def reconcile_on_startup(max_retries=3, retry_sleep_sec=5):
    """Rebuild startup state from Bybit broker truth only.

    0 matches          -> leave local state absent for that symbol/side.
    1 order match      -> adopt pending order truth.
    1 position match   -> adopt live position truth and block same-side pending.
    conflicting matches -> AMBIGUOUS / CRITICAL / fail closed.
    """
    global _placed_levels, _adopted_positions
    startup_ts = datetime.now(timezone.utc)
    local_before = {f"{sym}:{side}": price for (sym, side), (price, _oid) in _placed_levels.items()}
    reconciled = {}
    adopted_positions = set()
    broker_matches = {}
    attribution_evidence = []

    for symbol in SYMBOLS:
        orders, err = _fetch_open_orders_with_retry(symbol, max_retries=max_retries, retry_sleep_sec=retry_sleep_sec)
        if orders is None:
            report = {
                "startup_id": _RUN_ID,
                "timestamp": startup_ts.isoformat(),
                "status": "CRITICAL",
                "mismatch_reason": "fetch_open_orders_failed",
                "symbol": symbol,
                "local_before": local_before,
                "reconciled_after": {},
                "broker_matches": broker_matches,
                "attribution_evidence": attribution_evidence,
                "error": err,
            }
            log(f"{symbol} startup reconciliation failed: {json.dumps(report, ensure_ascii=False, default=str)}")
            return False, report
        positions, err = _fetch_positions_with_retry(symbol, max_retries=max_retries, retry_sleep_sec=retry_sleep_sec)
        if positions is None:
            report = {
                "startup_id": _RUN_ID,
                "timestamp": startup_ts.isoformat(),
                "status": "CRITICAL",
                "mismatch_reason": "fetch_positions_failed",
                "symbol": symbol,
                "local_before": local_before,
                "reconciled_after": {},
                "broker_matches": broker_matches,
                "attribution_evidence": attribution_evidence,
                "error": err,
            }
            log(f"{symbol} startup reconciliation failed: {json.dumps(report, ensure_ascii=False, default=str)}")
            return False, report

        for order in orders:
            evidence = _attribution_evidence(order, symbol=symbol)
            if evidence is None:
                continue
            side = "low" if (order.get("side") or "").lower() == "buy" else "high" if (order.get("side") or "").lower() == "sell" else None
            if side is None:
                continue
            key = (symbol, side)
            broker_matches.setdefault(symbol, {"low": [], "high": [], "total": 0})
            broker_matches[symbol][side].append({
                "order_id": order.get("id"),
                "price": order.get("price"),
                "side": order.get("side"),
                "orderLinkId": evidence["orderLinkId"],
                "timestamp": evidence["timestamp"],
            })
            broker_matches[symbol]["total"] += 1
            attribution_evidence.append(evidence)

        for position in positions:
            if (position.get("symbol") or symbol) != symbol or _position_size(position) <= 0:
                continue
            side = _position_side_key(position)
            if side is None:
                report = {
                    "startup_id": _RUN_ID,
                    "timestamp": startup_ts.isoformat(),
                    "status": "AMBIGUOUS / CRITICAL",
                    "mismatch_reason": f"position_side_unknown:{symbol}",
                    "symbol": symbol,
                    "local_before": local_before,
                    "reconciled_after": {},
                    "broker_matches": broker_matches,
                    "attribution_evidence": attribution_evidence,
                    "error": None,
                }
                log(f"{symbol} startup reconciliation failed: {json.dumps(report, ensure_ascii=False, default=str)}")
                return False, report
            broker_matches.setdefault(symbol, {"low": [], "high": [], "positions": {"low": [], "high": []}, "total": 0})
            broker_matches[symbol].setdefault("positions", {"low": [], "high": []})
            broker_matches[symbol]["positions"][side].append({
                "position_id": position.get("id") or (position.get("info") or {}).get("positionIdx"),
                "side": position.get("side") or (position.get("info") or {}).get("side"),
                "contracts": _position_size(position),
                "entryPrice": position.get("entryPrice") or (position.get("info") or {}).get("avgPrice"),
            })
            broker_matches[symbol]["total"] += 1

        for side in ("low", "high"):
            key = (symbol, side)
            matches = broker_matches.get(symbol, {}).get(side, [])
            pos_matches = broker_matches.get(symbol, {}).get("positions", {}).get(side, [])
            if matches and pos_matches:
                report = {
                    "startup_id": _RUN_ID,
                    "timestamp": startup_ts.isoformat(),
                    "status": "AMBIGUOUS / CRITICAL",
                    "mismatch_reason": f"order_and_position_match:{symbol}:{side}",
                    "symbol": symbol,
                    "local_before": local_before,
                    "reconciled_after": {},
                    "broker_matches": broker_matches,
                    "attribution_evidence": attribution_evidence,
                    "error": None,
                }
                log(f"{symbol} startup reconciliation failed: {json.dumps(report, ensure_ascii=False, default=str)}")
                return False, report
            if len(matches) > 1:
                report = {
                    "startup_id": _RUN_ID,
                    "timestamp": startup_ts.isoformat(),
                    "status": "AMBIGUOUS / CRITICAL",
                    "mismatch_reason": f"multiple_matches:{symbol}:{side}",
                    "symbol": symbol,
                    "local_before": local_before,
                    "reconciled_after": {},
                    "broker_matches": broker_matches,
                    "attribution_evidence": attribution_evidence,
                    "error": None,
                }
                log(f"{symbol} startup reconciliation failed: {json.dumps(report, ensure_ascii=False, default=str)}")
                return False, report
            if len(pos_matches) > 1:
                report = {
                    "startup_id": _RUN_ID,
                    "timestamp": startup_ts.isoformat(),
                    "status": "AMBIGUOUS / CRITICAL",
                    "mismatch_reason": f"multiple_position_matches:{symbol}:{side}",
                    "symbol": symbol,
                    "local_before": local_before,
                    "reconciled_after": {},
                    "broker_matches": broker_matches,
                    "attribution_evidence": attribution_evidence,
                    "error": None,
                }
                log(f"{symbol} startup reconciliation failed: {json.dumps(report, ensure_ascii=False, default=str)}")
                return False, report
            if len(matches) == 1:
                match = matches[0]
                reconciled[key] = (float(match["price"]), match["order_id"])
            elif len(pos_matches) == 1:
                adopted_positions.add(key)

    local_before_report = dict(local_before)
    after_report = {f"{sym}:{side}": price for (sym, side), (price, _oid) in reconciled.items()}
    adopted_report = [f"{sym}:{side}" for (sym, side) in sorted(adopted_positions)]
    _placed_levels = reconciled
    _adopted_positions = adopted_positions
    report = {
        "startup_id": _RUN_ID,
        "timestamp": startup_ts.isoformat(),
        "status": "SUCCESS",
        "mismatch_reason": None,
        "local_before": local_before_report,
        "reconciled_after": after_report,
        "adopted_positions": adopted_report,
        "broker_matches": broker_matches,
        "attribution_evidence": attribution_evidence,
        "error": None,
    }
    log(f"startup reconciliation: {json.dumps(report, ensure_ascii=False, default=str)}")
    try:
        write_heartbeat('swing_pending_bybit',
                        startup_id=_RUN_ID,
                        status=report["status"],
                        local_before=len(local_before_report),
                        reconciled_after=len(after_report),
                        adopted_positions=len(adopted_report),
                        broker_matches=sum(v["total"] for v in broker_matches.values()),
                        mismatch_reason=None)
    except Exception:
        pass
    return True, report


def _true_range(h, l, prev_close):
    return max(h - l, abs(h - prev_close), abs(l - prev_close))


def _atr(symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=ATR_PERIOD + 2)
        if not ohlcv or len(ohlcv) < ATR_PERIOD + 1:
            return None
        trs = []
        prev_close = ohlcv[0][4]
        for row in ohlcv[1:]:
            h, l, c = row[2], row[3], row[4]
            trs.append(_true_range(h, l, prev_close))
            prev_close = c
        return sum(trs[-ATR_PERIOD:]) / ATR_PERIOD
    except Exception as e:
        log(f"{symbol} ATR error: {str(e)[:120]}")
        return None


def _find_pivots(symbol):
    """Most recent CONFIRMED 3-candle fractal swing high/low, identical
    definition to swing_pending_bot.py (MT5). ohlcv row = [ts,o,h,l,c,v]."""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, TIMEFRAME, limit=60)
        if not ohlcv or len(ohlcv) < 10:
            return None, None
        swing_high = swing_low = None
        for i in range(len(ohlcv) - 1 - MIN_PIVOT_AGE_BARS, 1, -1):
            hi, lo = ohlcv[i][2], ohlcv[i][3]
            if swing_high is None and hi > ohlcv[i - 1][2] and hi > ohlcv[i + 1][2]:
                swing_high = float(hi)
            if swing_low is None and lo < ohlcv[i - 1][3] and lo < ohlcv[i + 1][3]:
                swing_low = float(lo)
            if swing_high is not None and swing_low is not None:
                break
        return swing_high, swing_low
    except Exception as e:
        log(f"{symbol} pivot scan error: {str(e)[:120]}")
        return None, None


def _existing_order_ok(symbol, order_id):
    # 2026-08-02 bug fix: ccxt.bybit's fetch_order() raises ArgumentsRequired
    # ("can only access an order if it is in last 500 orders... set
    # params['acknowledged']=True") for a perfectly live resting order -- the
    # exception was silently caught and treated as "order gone", so the old
    # order never got cancelled and a fresh one was placed on top of it every
    # single 15-min cycle (found: 4 duplicate BTC sell orders stacked at the
    # same price). fetch_open_orders() + membership check is the reliable way
    # ccxt itself recommends for Bybit; matches the pattern already used in
    # _demo()'s test double, just wasn't used in the real implementation.
    try:
        open_orders = exchange.fetch_open_orders(symbol)
        return any(o["id"] == order_id for o in open_orders)
    except Exception as e:
        log(f"{symbol} fetch_open_orders error: {str(e)[:120]}")
        return False


def _place_or_replace(symbol, side, level, atr):
    """side: 'low' (buy limit at swing low) or 'high' (sell limit at swing high)."""
    key = (symbol, side)
    if key in _adopted_positions:
        log(f"{symbol} {side} skipped -- broker position adopted at startup")
        return
    freeze = entry_freeze_status("swing_pending_bybit", account="BAA")
    if freeze.get("frozen"):
        log(f"{symbol} {side} ENTRY BLOCKED -- freeze active ({freeze.get('reason')})")
        return
    if is_portfolio_halted():
        # 2026-08-08: portfolio-wide RISK_HALT -- full no-op, any existing resting
        # order at this level is left completely untouched (neither cancelled nor
        # replaced), only a brand-new order would count as a new entry
        log(f"{symbol} {side} ENTRY BLOCKED -- portfolio-wide RISK_HALT active (synced from Windows)")
        return
    price = float(exchange.price_to_precision(symbol, level))
    try:
        ticker = exchange.fetch_ticker(symbol)
    except Exception as e:
        log(f"{symbol} ticker error: {str(e)[:120]}")
        return
    live = ticker["bid"] if side == "low" else ticker["ask"]
    if side == "low" and price >= live:
        return
    if side == "high" and price <= live:
        return

    prev = _placed_levels.get(key)
    if prev is not None:
        prev_price, prev_id = prev
        if _existing_order_ok(symbol, prev_id):
            if atr and abs(price - prev_price) < LEVEL_MOVE_TOL_MULT * atr:
                return
            try:
                exchange.cancel_order(prev_id, symbol)
            except Exception:
                pass
        _placed_levels.pop(key, None)

    sl_dist = SL_ATR_MULT * atr if atr else None
    if sl_dist is None or sl_dist <= 0:
        return
    qty = QTY[symbol]
    if side == "low":
        order_side, sl = "buy", price - sl_dist
    else:
        order_side, sl = "sell", price + sl_dist

    try:
        o = exchange.create_order(symbol, "limit", order_side, qty, price, params={
            "stopLoss": str(round(sl, 2)), "slTriggerBy": "MarkPrice",
            "timeInForce": "GTC", "category": "linear", "positionIdx": 0,
            "orderLinkId": make_tag(ORDER_TAG, symbol, side),
        })
        ok = True
    except Exception as e:
        o = None
        ok = False
        log(f"{symbol} {order_side} order error: {str(e)[:150]}")

    if ok and o:
        _placed_levels[key] = (price, o["id"])
    log(f"{symbol} {'BUY' if side == 'low' else 'SELL'} LIMIT @ {price} SL {sl:.2f} "
        f"({'OK' if ok else 'FAIL'})")


def check_symbol(symbol):
    if symbol in PAUSED_SYMBOLS:
        # 2026-08-19 (Ahmed-approved): new entries paused for this symbol only --
        # see PAUSED_SYMBOLS comment above for the numbers behind this decision.
        log(f"{symbol} skipped -- new entries paused (Ahmed-approved 2026-08-19)")
        return
    high, low = _find_pivots(symbol)
    atr = _atr(symbol)
    if atr is None:
        log(f"{symbol} skipped -- no ATR available")
        return
    if low is not None:
        _place_or_replace(symbol, "low", low, atr)
    if high is not None:
        _place_or_replace(symbol, "high", high, atr)


def _demo():
    """python swing_pending_bybit.py --test -- asserts pivot detection,
    ATR math, no-place-through-live-price, and no-duplicate-replace logic,
    without touching the real exchange. Mirrors swing_pending_bot.py's
    (MT5) self-check suite so both ports stay verifiably in sync."""
    global exchange, _RUN_ID, _LOCK_HELD, LOCK_PATH, _adopted_positions, is_portfolio_halted
    sent = []

    open_ids = set()

    class FakeExchange:
        def price_to_precision(self, symbol, price):
            return round(price, 2)

        def fetch_ticker(self, symbol):
            return {"bid": 1.12, "ask": 1.1202}

        def fetch_open_orders(self, symbol):
            return [{"id": oid} for oid in open_ids]

        def cancel_order(self, order_id, symbol):
            sent.append({"action": "cancel", "id": order_id})
            open_ids.discard(order_id)

        def create_order(self, symbol, otype, side, qty, price, params=None):
            oid = f"fake-{len(sent)}"
            sent.append({"action": "create", "side": side, "price": price,
                         "sl": float(params["stopLoss"]), "qty": qty, "id": oid})
            open_ids.add(oid)
            return {"id": oid}

    class ReconcileExchange:
        def __init__(self, orders, positions=None, fail=False, fail_positions=False):
            self.orders = orders
            self.positions = positions or {}
            self.fail = fail
            self.fail_positions = fail_positions

        def fetch_open_orders(self, symbol):
            if self.fail:
                raise RuntimeError("boom")
            return [o for o in self.orders.get(symbol, [])]

        def fetch_positions(self, symbols):
            if self.fail_positions:
                raise RuntimeError("positions boom")
            wanted = set(symbols or [])
            return [p for sym, rows in self.positions.items() if not wanted or sym in wanted for p in rows]

        def create_order(self, *a, **k):
            raise AssertionError("reconcile tests must not create orders")

        def cancel_order(self, *a, **k):
            raise AssertionError("reconcile tests must not cancel orders")

    exchange = FakeExchange()
    QTY["TEST"] = 0.01
    _placed_levels.clear()
    halt_hook = is_portfolio_halted
    is_portfolio_halted = lambda: False

    def make_ohlcv(rows):
        return [[i, r[2], r[0], r[1], r[2], 0] for i, r in enumerate(rows)]  # ts,o,h,l,c,v order fixed below

    # ohlcv row shape must be [ts, o, h, l, c, v]
    def rows_to_ohlcv(rows):
        out = []
        for i, (h, l, c) in enumerate(rows):
            out.append([i, c, h, l, c, 0])
        return out

    # --- pivot detection ---
    rows = [(1.10, 1.09, 1.095) for _ in range(20)]
    rows[10] = (1.15, 1.09, 1.10)
    rows[15] = (1.10, 1.04, 1.08)
    ohlcv = rows_to_ohlcv(rows)
    exchange.fetch_ohlcv = lambda symbol, tf, limit: ohlcv[-limit:] if limit <= len(ohlcv) else ohlcv
    high, low = _find_pivots("TEST")
    assert high == 1.15, f"expected swing high 1.15, got {high}"
    assert low == 1.04, f"expected swing low 1.04, got {low}"
    print("swing_pending_bybit pivot detection self-check: OK")

    # --- ATR ---
    atr_rows = [(1.102 + 0.001 * (i % 3), 1.098 - 0.001 * (i % 3), 1.10) for i in range(ATR_PERIOD + 2)]
    atr_ohlcv = rows_to_ohlcv(atr_rows)
    exchange.fetch_ohlcv = lambda symbol, tf, limit: atr_ohlcv
    atr = _atr("TEST")
    assert atr is not None and atr > 0
    print(f"swing_pending_bybit ATR self-check: OK (ATR={atr:.5f})")

    # --- placement + magic-equivalent (SL-only, no TP) ---
    sent.clear()
    global entry_freeze_status
    frozen_hook = entry_freeze_status
    try:
        entry_freeze_status = lambda *a, **k: {"frozen": False, "reason": "self-test override"}
        _place_or_replace("TEST", "low", 1.10, atr)
        assert len(sent) == 1 and sent[0]["action"] == "create" and sent[0]["side"] == "buy"
        assert abs(sent[0]["sl"] - round(1.10 - SL_ATR_MULT * atr, 2)) < 1e-6
        print("swing_pending_bybit BUY placement self-check: OK (SL-only, no TP -- see module docstring)")
    finally:
        entry_freeze_status = frozen_hook

    # --- never place through live price ---
    sent.clear()
    _place_or_replace("TEST", "low", 1.13, atr)  # 1.13 > bid 1.12
    assert not sent
    sent.clear()
    _place_or_replace("TEST", "high", 1.11, atr)  # 1.11 < ask 1.1202
    assert not sent
    print("swing_pending_bybit no-place-through-price self-check: OK")

    # --- no duplicate replace ---
    sent.clear()
    _place_or_replace("TEST", "low", 1.10, atr)
    assert not sent
    print("swing_pending_bybit no-duplicate-replace self-check: OK")

    # --- pivot moved: cancel + replace ---
    sent.clear()
    frozen_hook = entry_freeze_status
    try:
        entry_freeze_status = lambda *a, **k: {"frozen": False, "reason": "self-test override"}
        _place_or_replace("TEST", "low", 1.10 - 5 * atr, atr)
        assert any(s["action"] == "cancel" for s in sent)
        assert any(s["action"] == "create" for s in sent)
        print("swing_pending_bybit pivot-moved replace self-check: OK")
    finally:
        entry_freeze_status = frozen_hook

    # --- regression: _existing_order_ok must use fetch_open_orders, not
    # fetch_order (2026-08-02 bug: ccxt.bybit's fetch_order() throws
    # ArgumentsRequired on a perfectly live order, which used to get
    # swallowed and silently treated as "gone", stacking duplicate orders
    # every cycle instead of ever cancelling the old one) ---
    live_id = list(open_ids)[0]
    assert _existing_order_ok("TEST", live_id), "an order actually in fetch_open_orders must read as OK"
    assert not _existing_order_ok("TEST", "not-a-real-id"), "an order NOT in fetch_open_orders must read as gone"
    print("swing_pending_bybit _existing_order_ok self-check: OK")

    # --- 2026-08-19: paused symbol must be a full no-op, zero exchange calls ---
    def _boom(*a, **k):
        raise AssertionError("paused symbol must not touch the exchange at all")
    exchange.fetch_ohlcv = _boom
    exchange.fetch_ticker = _boom
    check_symbol("XAU/USDT:USDT")  # must return immediately via PAUSED_SYMBOLS gate
    print("swing_pending_bybit XAU pause self-check: OK (zero exchange calls)")

    # --- reconcile_on_startup: no orders => success, state absent ---
    import tempfile
    old_lock, old_run_id, old_lock_held = LOCK_PATH, _RUN_ID, _LOCK_HELD
    old_log = globals()["log"]
    globals()["log"] = lambda _msg: None
    with tempfile.TemporaryDirectory() as td:
        LOCK_PATH = os.path.join(td, "swing_pending_bybit.lock")
        _RUN_ID = "test-run"
        _LOCK_HELD = False
        exchange = ReconcileExchange({sym: [] for sym in SYMBOLS})
        _placed_levels.clear()
        _adopted_positions.clear()
        ok, report = reconcile_on_startup()
        assert ok and report["status"] == "SUCCESS"
        assert _placed_levels == {}
        assert report["reconciled_after"] == {}
        assert report["adopted_positions"] == []
        print("swing_pending_bybit reconcile no-order self-check: OK")

        # one matching order => adopt broker truth, no create/cancel
        order = {"id": "ord-1", "side": "buy", "price": 111.11, "clientOrderId": "swing_pending-BTC-low-1", "timestamp": 1111111111}
        exchange = ReconcileExchange({"BTC/USDT:USDT": [order]})
        _placed_levels.clear()
        _adopted_positions.clear()
        ok, report = reconcile_on_startup()
        assert ok and _placed_levels == {("BTC/USDT:USDT", "low"): (111.11, "ord-1")}
        assert _adopted_positions == set()
        assert report["reconciled_after"] == {"BTC/USDT:USDT:low": 111.11}
        assert report["attribution_evidence"][0]["symbol"] == "BTC/USDT:USDT"
        assert report["attribution_evidence"][0]["timestamp"] is not None
        print("swing_pending_bybit reconcile one-match self-check: OK")

        # one matching position => adopt broker truth and block same-side pending
        pos = {"id": "pos-1", "symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.001, "entryPrice": 111.11}
        exchange = ReconcileExchange({"BTC/USDT:USDT": []}, {"BTC/USDT:USDT": [pos]})
        _placed_levels.clear()
        _adopted_positions.clear()
        ok, report = reconcile_on_startup()
        assert ok and _placed_levels == {}, report
        assert _adopted_positions == {("BTC/USDT:USDT", "low")}, _adopted_positions
        assert report["adopted_positions"] == ["BTC/USDT:USDT:low"], report
        print("swing_pending_bybit reconcile one-position self-check: OK")

        # multiple matching orders => fail closed, local state unchanged
        exchange = ReconcileExchange({"BTC/USDT:USDT": [
            {"id": "ord-a", "side": "buy", "price": 111.11, "clientOrderId": "swing_pending-BTC-low-a"},
            {"id": "ord-b", "side": "buy", "price": 112.22, "clientOrderId": "swing_pending-BTC-low-b", "timestamp": 1111111112},
        ]})
        _placed_levels.clear()
        _adopted_positions.clear()
        ok, report = reconcile_on_startup()
        assert not ok and report["status"] == "AMBIGUOUS / CRITICAL"
        assert _placed_levels == {}
        print("swing_pending_bybit reconcile multiple-match self-check: OK")

        # partial mismatch: same symbol/side has both order and position => fail closed
        exchange = ReconcileExchange(
            {"BTC/USDT:USDT": [order]},
            {"BTC/USDT:USDT": [pos]},
        )
        _placed_levels.clear()
        _adopted_positions.clear()
        ok, report = reconcile_on_startup()
        assert not ok and report["mismatch_reason"] == "order_and_position_match:BTC/USDT:USDT:low", report
        assert _placed_levels == {} and _adopted_positions == set()
        print("swing_pending_bybit reconcile partial-mismatch self-check: OK")

        # retry failure => fail closed
        exchange = ReconcileExchange({"BTC/USDT:USDT": []}, fail=True)
        _placed_levels.clear()
        _adopted_positions.clear()
        ok, report = reconcile_on_startup(max_retries=2, retry_sleep_sec=0)
        assert not ok and report["status"] == "CRITICAL"
        assert report["mismatch_reason"] == "fetch_open_orders_failed"
        print("swing_pending_bybit reconcile fetch-failure self-check: OK")

        # idempotency: same broker state twice -> same result
        exchange = ReconcileExchange({"ETH/USDT:USDT": [
            {"id": "ord-z", "side": "sell", "price": 222.22, "orderLinkId": "swing_pending-ETH-high-9", "timestamp": 2222222222},
        ]})
        _placed_levels.clear()
        _adopted_positions.clear()
        ok1, report1 = reconcile_on_startup()
        ok2, report2 = reconcile_on_startup()
        assert ok1 and ok2 and report1["reconciled_after"] == report2["reconciled_after"] == {"ETH/USDT:USDT:high": 222.22}
        print("swing_pending_bybit reconcile idempotency self-check: OK")

        # stale local state vs flat broker truth => broker truth wins, local state cleared
        exchange = ReconcileExchange({sym: [] for sym in SYMBOLS})
        _placed_levels.clear()
        _placed_levels[("BTC/USDT:USDT", "low")] = (999.0, "stale-local")
        _adopted_positions.clear()
        _adopted_positions.add(("ETH/USDT:USDT", "high"))
        ok, report = reconcile_on_startup()
        assert ok and _placed_levels == {} and _adopted_positions == set(), report
        assert report["local_before"] == {"BTC/USDT:USDT:low": 999.0}, report
        assert report["reconciled_after"] == {} and report["adopted_positions"] == [], report
        print("swing_pending_bybit reconcile stale-local-state self-check: OK")

    LOCK_PATH, _RUN_ID, _LOCK_HELD = old_lock, old_run_id, old_lock_held
    globals()["log"] = old_log

    # adopted live position must suppress duplicate pending placement without exchange calls
    def _must_not_touch_exchange(*a, **k):
        raise AssertionError("adopted position must suppress duplicate pending before exchange access")
    exchange = FakeExchange()
    exchange.price_to_precision = _must_not_touch_exchange
    _adopted_positions.clear()
    _adopted_positions.add(("TEST", "low"))
    sent.clear()
    _place_or_replace("TEST", "low", 1.10, atr)
    assert not sent
    _adopted_positions.clear()
    print("swing_pending_bybit adopted-position duplicate-suppression self-check: OK")
    is_portfolio_halted = halt_hook

    LOCK_PATH, _RUN_ID, _LOCK_HELD = old_lock, old_run_id, old_lock_held


def main():
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        _demo()
        return
    global _RUN_ID
    _RUN_ID = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    ok, report = _acquire_single_instance_lock()
    if not ok:
        log(f"startup aborted: {json.dumps(report, ensure_ascii=False, default=str)}")
        raise SystemExit(1)
    try:
        ok, report = reconcile_on_startup()
        if not ok:
            log(f"startup reconciliation blocked: {json.dumps(report, ensure_ascii=False, default=str)}")
            raise SystemExit(1)
        log(f"swing_pending_bybit started -- symbols {SYMBOLS}, every {CHECK_INTERVAL}s")
        for symbol in SYMBOLS:
            try:
                exchange.set_leverage(LEV, symbol)
            except Exception as e:
                log(f"{symbol} set_leverage warning: {str(e)[:120]}")
        while True:
            for symbol in SYMBOLS:
                try:
                    check_symbol(symbol)
                except Exception as e:
                    log(f"{symbol} CHECK_FAILED: {str(e)[:150]} -- other symbols unaffected")
            write_heartbeat('swing_pending_bybit', symbols_checked=len(SYMBOLS), startup_id=_RUN_ID)
            time.sleep(CHECK_INTERVAL)
    finally:
        _release_single_instance_lock()


if __name__ == "__main__":
    main()
