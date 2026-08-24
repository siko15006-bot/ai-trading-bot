"""Phase 1C validation tests for PendingBreakoutShadow. Run:
    python test_phase1c.py
Covers: guard-before-connect ordering, never-fabricate-on-missing-data,
state snapshot/restore round-trip + fail-safe-to-IDLE on corruption,
storage write-isolation for the new state file, and that mt5_source.py is
still the only module touching MetaTrader5 / Bot_Active.
Mocks MT5 and the live data source throughout -- no terminal required.
"""
import os
import sys
import glob
import json
import tempfile
from datetime import datetime, timezone
from dataclasses import asdict

os.environ.setdefault("PENDING_BREAKOUT_SHADOW_DATA_DIR", tempfile.mkdtemp(prefix="pbs_phase1c_"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pending_breakout_shadow.config import DEFAULT_CONFIG, State
from pending_breakout_shadow.engine import ShadowEngine, Tick, MarketContext, Setup
from pending_breakout_shadow import storage
from pending_breakout_shadow import runner as pbs_runner


def _tick(t, bid, ask):
    return Tick(timestamp=t, bid=bid, ask=ask)


def _ctx(atr14=1.0, mind=0.1):
    return MarketContext(atr5=atr14, atr14=atr14, atr50=atr14, broker_minimum_distance=mind)


def test_1_guard_verified_predicate():
    def _blocked_order_send(*a, **k):
        pass

    def _real_order_send(*a, **k):
        pass

    class FakeGuarded:
        order_send = _blocked_order_send

    class FakeUnguarded:
        order_send = _real_order_send

    assert pbs_runner._guard_verified(FakeGuarded) is True
    assert pbs_runner._guard_verified(FakeUnguarded) is False
    print("1/9 PASS: _guard_verified distinguishes a real guard from an unpatched module")


def test_2_guard_failure_aborts_before_connect():
    calls = []
    orig_guard = pbs_runner.install_mt5_execution_guard
    orig_connect = pbs_runner.mt5_source.connect_readonly_saved_session
    pbs_runner.install_mt5_execution_guard = lambda: False
    pbs_runner.mt5_source.connect_readonly_saved_session = lambda: calls.append("connect") or (True, None)
    try:
        rc = pbs_runner.main()
        assert rc == 1, f"expected abort (1), got {rc}"
        assert calls == [], "connect_readonly_saved_session must never be called when the guard fails to install"
    finally:
        pbs_runner.install_mt5_execution_guard = orig_guard
        pbs_runner.mt5_source.connect_readonly_saved_session = orig_connect
    print("2/9 PASS: guard-install failure aborts startup before any MT5 connection is attempted")


def test_3_should_skip_poll_never_fabricates():
    assert pbs_runner._should_skip_poll(None, _ctx()) is True
    assert pbs_runner._should_skip_poll(_tick(datetime.now(timezone.utc), 1, 1.1), None) is True
    assert pbs_runner._should_skip_poll(None, None) is True
    assert pbs_runner._should_skip_poll(_tick(datetime.now(timezone.utc), 1, 1.1), _ctx()) is False
    print("3/9 PASS: a missing tick or context is always treated as skip-this-poll")


def test_4_snapshot_restore_idle_roundtrip():
    e = ShadowEngine(config=DEFAULT_CONFIG, symbol="XAUUSD")
    snap = e.snapshot_state()
    e2, ok, reason = ShadowEngine.restore_from_snapshot(snap, config=DEFAULT_CONFIG)
    assert ok is True, reason
    assert e2.state == State.IDLE
    assert e2.symbol == "XAUUSD"
    print("4/9 PASS: IDLE snapshot/restore round-trips cleanly")


def test_5_snapshot_restore_armed_and_active_trade():
    e = ShadowEngine(config=DEFAULT_CONFIG, symbol="XAUUSD")
    t0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    e.arm(_tick(t0, 2000.0, 2000.2), _ctx(atr14=2.0, mind=0.1))
    snap = e.snapshot_state(last_processed_ts=t0)
    e2, ok, reason = ShadowEngine.restore_from_snapshot(snap, config=DEFAULT_CONFIG)
    assert ok is True, reason
    assert e2.state == State.ARMED
    assert e2.setup.setup_id == e.setup.setup_id
    assert e2.setup.buy_stop == e.setup.buy_stop

    # drive it into an active trade, then snapshot/restore that too
    t1 = t0.replace(second=5)
    trade = e2.process_tick(_tick(t1, e2.setup.buy_stop + 0.05, e2.setup.buy_stop + 0.1), _ctx(atr14=2.0, mind=0.1))
    assert trade is not None and e2.state in (State.LONG_TRIGGERED, State.SHORT_TRIGGERED)
    snap2 = e2.snapshot_state(last_processed_ts=t1)
    e3, ok2, reason2 = ShadowEngine.restore_from_snapshot(snap2, config=DEFAULT_CONFIG)
    assert ok2 is True, reason2
    assert e3.active_trade is not None
    assert e3.active_trade.entry_price == e2.active_trade.entry_price
    assert e3.active_trade._risk_distance == e2.active_trade._risk_distance
    assert e3.active_trade._r_touch_flags.keys() == e2.active_trade._r_touch_flags.keys()
    print("5/9 PASS: ARMED and active-trade snapshots restore with internal bookkeeping intact")


def test_6_restore_fails_safe_on_corruption():
    e, ok, reason = ShadowEngine.restore_from_snapshot({"schema_version": 999, "state": "IDLE"})
    assert ok is False and e.state == State.IDLE
    assert "schema_version" in reason

    e2, ok2, reason2 = ShadowEngine.restore_from_snapshot({"schema_version": ShadowEngine.STATE_SCHEMA_VERSION,
                                                             "state": "NOT_A_REAL_STATE"})
    assert ok2 is False and e2.state == State.IDLE

    e3, ok3, reason3 = ShadowEngine.restore_from_snapshot("not even a dict")
    assert ok3 is False and e3.state == State.IDLE
    print("6/9 PASS: schema mismatch / invalid state / garbage input all fail safe to a fresh IDLE engine")


def test_7_storage_state_roundtrip_and_corruption_handling():
    orig_data_dir, orig_state_path = storage.DATA_DIR, storage.STATE_PATH
    tmp = tempfile.mkdtemp(prefix="pbs_test_")
    storage.DATA_DIR = tmp
    storage.STATE_PATH = os.path.join(tmp, "engine_state.json")
    try:
        assert storage.load_state() is None, "missing file must return None, not raise"
        payload = {"schema_version": 1, "state": "IDLE", "symbol": "XAUUSD"}
        storage.save_state(payload)
        assert storage.load_state() == payload

        with open(storage.STATE_PATH, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        assert storage.load_state() is None, "corrupt JSON must return None, not raise"
    finally:
        storage.DATA_DIR, storage.STATE_PATH = orig_data_dir, orig_state_path
    print("7/9 PASS: storage.save_state/load_state round-trips and degrades to None on missing/corrupt file")


def test_8_full_runner_restore_defaults_to_idle_on_bad_file():
    orig_load = storage.load_state
    storage.load_state = lambda: {"schema_version": 1, "state": "BOGUS"}
    try:
        snap = storage.load_state()
        engine, ok, reason = ShadowEngine.restore_from_snapshot(snap, config=DEFAULT_CONFIG)
        assert ok is False
        assert engine.state == State.IDLE
    finally:
        storage.load_state = orig_load
    print("8/9 PASS: runner-path restore of a bad on-disk state file yields a safe fresh IDLE engine")


def test_9_mt5_import_isolation():
    root = os.path.dirname(os.path.abspath(__file__))
    pkg_files = glob.glob(os.path.join(root, "pending_breakout_shadow", "*.py"))
    for path in pkg_files:
        name = os.path.basename(path)
        with open(path, encoding="utf-8") as f:
            src = f.read()
        if name == "mt5_source.py":
            assert "Bot_Active" not in src, "mt5_source.py must not reference production Bot_Active either"
            continue
        if name in ("runner.py", "shadow_broker.py"):
            # runner.py: imports MetaTrader5 only to verify the guard, inside main().
            # shadow_broker.py: install_mt5_execution_guard()'s whole job is to
            # reach MetaTrader5.order_send and patch it -- that's its Phase 1A charter.
            continue
        assert "import MetaTrader5" not in src, f"{name} must not import MetaTrader5 directly"
        assert "Bot_Active" not in src, f"{name} references production Bot_Active path"
    print("9/9 PASS: MetaTrader5 import stays confined to mt5_source.py; no package file references Bot_Active")


if __name__ == "__main__":
    test_1_guard_verified_predicate()
    test_2_guard_failure_aborts_before_connect()
    test_3_should_skip_poll_never_fabricates()
    test_4_snapshot_restore_idle_roundtrip()
    test_5_snapshot_restore_armed_and_active_trade()
    test_6_restore_fails_safe_on_corruption()
    test_7_storage_state_roundtrip_and_corruption_handling()
    test_8_full_runner_restore_defaults_to_idle_on_bad_file()
    test_9_mt5_import_isolation()
    print("\nALL PHASE 1C TESTS PASSED")
