"""Phase 1B synthetic validation. Deterministic ticks only -- no MT5, no
live data. Run: python test_phase1b.py
"""
import os
import sys
import glob
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("PENDING_BREAKOUT_SHADOW_DATA_DIR", tempfile.mkdtemp(prefix="pbs_phase1b_"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pending_breakout_shadow.engine import ShadowEngine, Tick, MarketContext
from pending_breakout_shadow.config import Config, State
from pending_breakout_shadow import storage
from pending_breakout_shadow.metrics import classify_session, r_multiple, update_mfe_mae

T0 = datetime(2026, 8, 19, 9, 0, 0, tzinfo=timezone.utc)  # 09:00 UTC = LONDON


def mk_ctx(atr14=1.0, atr5=1.0, atr50=1.0, min_dist=0.05):
    return MarketContext(atr5=atr5, atr14=atr14, atr50=atr50, broker_minimum_distance=min_dist)


def mk_tick(seconds, bid, ask):
    return Tick(timestamp=T0 + timedelta(seconds=seconds), bid=bid, ask=ask)


def new_engine(**cfg_overrides):
    cfg = Config(**{**{}, **cfg_overrides})
    return ShadowEngine(config=cfg, symbol="XAUUSD_TEST")


def test_1_buy_triggers_on_rise():
    e = new_engine()
    ctx = mk_ctx(atr14=1.0, min_dist=0.05)
    setup = e.arm(mk_tick(0, 100.00, 100.10), ctx)
    assert e.state == State.ARMED
    assert setup.buy_stop > 100.10 and setup.sell_stop < 100.00
    trade = None
    price = 100.10
    for s in range(1, 50):
        price += 0.02
        trade = e.process_tick(mk_tick(s, price - 0.10, price), ctx)
        if trade:
            break
    assert trade is not None and trade.direction == "LONG"
    assert e.state == State.LONG_TRIGGERED
    print("1/16 PASS: price rises -> BUY triggers")


def test_2_sell_triggers_on_fall():
    e = new_engine()
    ctx = mk_ctx(atr14=1.0, min_dist=0.05)
    e.arm(mk_tick(0, 100.00, 100.10), ctx)
    trade = None
    price = 100.00
    for s in range(1, 50):
        price -= 0.02
        trade = e.process_tick(mk_tick(s, price, price + 0.10), ctx)
        if trade:
            break
    assert trade is not None and trade.direction == "SHORT"
    assert e.state == State.SHORT_TRIGGERED
    print("2/16 PASS: price falls -> SELL triggers")


def _run_to_trigger_long(e, ctx, start_price=100.10, step=0.02):
    price = start_price
    for s in range(1, 50):
        price += step
        t = e.process_tick(mk_tick(s, price - 0.10, price), ctx)
        if t:
            return t, s
    raise AssertionError("never triggered")


def _run_to_trigger_short(e, ctx, start_price=100.00, step=0.02):
    price = start_price
    for s in range(1, 50):
        price -= step
        t = e.process_tick(mk_tick(s, price, price + 0.10), ctx)
        if t:
            return t, s
    raise AssertionError("never triggered")


def test_3_buy_then_sl():
    e = new_engine()
    ctx = mk_ctx(atr14=1.0, min_dist=0.05)
    e.arm(mk_tick(0, 100.00, 100.10), ctx)
    trade, s = _run_to_trigger_long(e, ctx)
    price = trade.entry_price
    for k in range(1, 30):
        price -= 0.05
        closed = e.process_tick(mk_tick(s + k, price, price + 0.10), ctx)
        if closed:
            assert closed.exit_reason == "SL"
            assert closed.exit_price == trade.SL
            print("3/16 PASS: BUY triggers then SL")
            return
    raise AssertionError("SL never hit")


def test_4_sell_then_sl():
    e = new_engine()
    ctx = mk_ctx(atr14=1.0, min_dist=0.05)
    e.arm(mk_tick(0, 100.00, 100.10), ctx)
    trade, s = _run_to_trigger_short(e, ctx)
    price = trade.entry_price
    for k in range(1, 30):
        price += 0.05
        closed = e.process_tick(mk_tick(s + k, price - 0.10, price), ctx)
        if closed:
            assert closed.exit_reason == "SL"
            print("4/16 PASS: SELL triggers then SL")
            return
    raise AssertionError("SL never hit")


def test_5_buy_then_tp():
    e = new_engine()
    ctx = mk_ctx(atr14=1.0, min_dist=0.05)
    e.arm(mk_tick(0, 100.00, 100.10), ctx)
    trade, s = _run_to_trigger_long(e, ctx)
    price = trade.entry_price
    for k in range(1, 30):
        price += 0.05
        closed = e.process_tick(mk_tick(s + k, price - 0.10, price), ctx)
        if closed:
            assert closed.exit_reason == "TP"
            print("5/16 PASS: BUY triggers then TP")
            return
    raise AssertionError("TP never hit")


def test_6_sell_then_tp():
    e = new_engine()
    ctx = mk_ctx(atr14=1.0, min_dist=0.05)
    e.arm(mk_tick(0, 100.00, 100.10), ctx)
    trade, s = _run_to_trigger_short(e, ctx)
    price = trade.entry_price
    for k in range(1, 30):
        price -= 0.05
        closed = e.process_tick(mk_tick(s + k, price, price + 0.10), ctx)
        if closed:
            assert closed.exit_reason == "TP"
            print("6/16 PASS: SELL triggers then TP")
            return
    raise AssertionError("TP never hit")


def test_7_rapid_whipsaw():
    e = new_engine()
    ctx = mk_ctx(atr14=1.0, min_dist=0.02)
    setup = e.arm(mk_tick(0, 100.00, 100.03), ctx)
    trade = e.process_tick(mk_tick(1, setup.sell_stop - 0.02, setup.sell_stop + 0.02), ctx)
    assert trade is not None and trade.direction == "SHORT"
    # immediately spike back up through where buy_stop was -> opposite level touch
    closed = None
    price = trade.entry_price
    for k in range(2, 40):
        price += 0.05
        r = e.process_tick(mk_tick(k, price - 0.02, price + 0.02), ctx)
        if trade.opposite_touch_after_entry_ms is not None and closed is None:
            pass
        if r:
            closed = r
            break
    assert trade.opposite_touch_after_entry_ms is not None or trade.whipsaw_detected
    print("7/16 PASS: rapid BUY/SELL whipsaw recorded "
          f"(opposite_touch_after_entry_ms={trade.opposite_touch_after_entry_ms}, "
          f"whipsaw_detected={trade.whipsaw_detected})")


def test_8_spread_spike_changes_distance():
    e1 = new_engine()
    e2 = new_engine()
    ctx = mk_ctx(atr14=1.0, min_dist=0.02)
    tight = e1.arm(mk_tick(0, 100.00, 100.01), ctx)   # spread 0.01
    wide = e2.arm(mk_tick(0, 100.00, 100.50), ctx)     # spread 0.50 -> spread*3.0 dominates
    assert wide.base_distance > tight.base_distance
    assert wide.base_distance == round(0.50 * 3.0, 10) or abs(wide.base_distance - 1.50) < 1e-9
    print("8/16 PASS: spread spike changes base_distance correctly "
          f"(tight={tight.base_distance:.4f}, wide={wide.base_distance:.4f})")


def test_9_invalid_tick_fails_safely():
    e = new_engine()
    ctx = mk_ctx()
    try:
        e.arm(Tick(timestamp=T0, bid=100.10, ask=100.00), ctx)  # crossed quote
        raise AssertionError("did not reject crossed quote")
    except ValueError:
        pass
    assert e.state == State.IDLE, "state must not change on a rejected tick"
    print("9/16 PASS: missing/invalid tick fails safely (ValueError, state unchanged)")


def test_10_double_side_touch_deterministic():
    e = new_engine()
    ctx = mk_ctx(atr14=1.0, min_dist=0.02)
    setup = e.arm(mk_tick(0, 100.00, 100.03), ctx)
    # construct a tick where both sides are touched simultaneously
    t = mk_tick(1, setup.sell_stop - 0.01, setup.buy_stop + 0.01)
    trade = e.process_tick(t, ctx)
    assert trade is not None
    assert trade.double_trigger is True
    assert trade.direction == "LONG"  # documented tie-break: BUY wins
    print("10/16 PASS: double-side touch handled deterministically "
          f"(double_trigger=True, direction={trade.direction})")


def test_11_cooldown_prevents_immediate_rearm():
    e = new_engine(cooldown_seconds=10)
    ctx = mk_ctx(atr14=1.0, min_dist=0.05)
    e.arm(mk_tick(0, 100.00, 100.10), ctx)
    trade, s = _run_to_trigger_long(e, ctx)
    price = trade.entry_price
    for k in range(1, 30):
        price -= 0.05
        closed = e.process_tick(mk_tick(s + k, price, price + 0.10), ctx)
        if closed:
            break
    assert e.state == State.COOLDOWN
    try:
        e.arm(mk_tick(s + 100, 100.00, 100.10), ctx)
        raise AssertionError("arm() succeeded during COOLDOWN")
    except RuntimeError:
        pass
    print("11/16 PASS: cooldown prevents immediate re-arm (arm() raises)")


def test_12_mfe_mae_direction_correct():
    mfe, mae = 0.0, 0.0
    mfe, mae = update_mfe_mae("LONG", 100.0, 101.0, mfe, mae)
    assert mfe == 1.0 and mae == 0.0
    mfe, mae = update_mfe_mae("LONG", 100.0, 99.5, mfe, mae)
    assert mfe == 1.0 and mae == 0.5
    mfe2, mae2 = 0.0, 0.0
    mfe2, mae2 = update_mfe_mae("SHORT", 100.0, 99.0, mfe2, mae2)
    assert mfe2 == 1.0 and mae2 == 0.0
    mfe2, mae2 = update_mfe_mae("SHORT", 100.0, 100.7, mfe2, mae2)
    assert mfe2 == 1.0 and abs(mae2 - 0.7) < 1e-9
    print("12/16 PASS: MFE/MAE calculations are direction-correct")


def test_13_r_level_analytics():
    e = new_engine()
    ctx = mk_ctx(atr14=1.0, min_dist=0.05)
    e.arm(mk_tick(0, 100.00, 100.10), ctx)
    trade, s = _run_to_trigger_long(e, ctx)
    risk = trade._risk_distance
    price = trade.entry_price
    for k in range(1, 60):
        price += risk * 0.3
        closed = e.process_tick(mk_tick(s + k, price - 0.05, price), ctx)
        if closed:
            break
    touched = trade.r_level_touches_ms
    assert "0.5R" in touched
    assert all(touched[a] <= touched[b] for a, b in
               zip(list(touched)[:-1], list(touched)[1:])), "R levels must touch in increasing time order"
    print(f"13/16 PASS: 0.5R/1R/1.5R/2R/3R analytics work (touched: {list(touched.keys())})")


def test_14_heartbeat_output_valid():
    import time as _t
    storage.write_heartbeat({
        "status": "TEST", "pid": os.getpid(), "timestamp": _t.time(), "symbol": "XAUUSD_TEST",
        "state": "ARMED", "bid": 100.0, "ask": 100.1, "spread": 0.1, "atr14": 1.0,
        "current_virtual_buy_stop": 100.2, "current_virtual_sell_stop": 99.9,
        "total_setups": 1, "total_trades": 0,
    })
    assert os.path.exists(storage.HEARTBEAT_PATH)
    import json
    with open(storage.HEARTBEAT_PATH, encoding="utf-8") as f:
        data = json.load(f)
    for key in ("status", "pid", "timestamp", "symbol", "state", "bid", "ask", "spread",
                "atr14", "current_virtual_buy_stop", "current_virtual_sell_stop",
                "total_setups", "total_trades"):
        assert key in data, f"missing heartbeat field: {key}"
    print("14/16 PASS: heartbeat output is valid JSON with all required fields")


def test_15_storage_isolated():
    original = {
        "DATA_DIR": storage.DATA_DIR,
        "SETUPS_PATH": storage.SETUPS_PATH,
        "TRADES_PATH": storage.TRADES_PATH,
        "HEARTBEAT_PATH": storage.HEARTBEAT_PATH,
    }
    with tempfile.TemporaryDirectory() as tmp:
        storage.DATA_DIR = tmp
        storage.SETUPS_PATH = os.path.join(tmp, "setups.jsonl")
        storage.TRADES_PATH = os.path.join(tmp, "trades.jsonl")
        storage.HEARTBEAT_PATH = os.path.join(tmp, "heartbeat.json")
        storage.append_jsonl(storage.TRADES_PATH, {"test": True})
        assert os.path.exists(storage.TRADES_PATH)
        try:
            storage.append_jsonl(os.path.join(storage.PROJECT_ROOT, "Bot_Active", "hack.jsonl"), {"x": 1})
            raise AssertionError("wrote outside isolated data dir without error")
        except RuntimeError:
            pass
    for key, value in original.items():
        setattr(storage, key, value)
    print("15/16 PASS: JSONL storage writes only inside the isolated data directory")


def test_16_phase1a_protections_still_pass():
    from pending_breakout_shadow.shadow_broker import ShadowBroker, install_mt5_execution_guard, SHADOW_MODE_ERROR
    for method_name in ("place_order", "modify_order", "close_order"):
        try:
            getattr(ShadowBroker(), method_name)()
            raise AssertionError(f"{method_name} did not raise")
        except RuntimeError as e:
            assert SHADOW_MODE_ERROR in str(e)
    guarded = install_mt5_execution_guard()
    if guarded:
        import MetaTrader5 as mt5
        try:
            mt5.order_send()
            raise AssertionError("order_send not blocked")
        except RuntimeError as e:
            assert SHADOW_MODE_ERROR in str(e)
    print(f"16/16 PASS: Phase 1A safety protections still pass (mt5_guard_installed={guarded})")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\nALL {len(tests)} PHASE 1B TESTS PASSED")
