"""assert-based self-check for autonomy_challenge.py -- run directly:
    python test_autonomy_challenge.py
Monkeypatches STATE_FILE/SNAPSHOTS_FILE to a temp dir so this never
touches the real challenge state."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

import autonomy_challenge as ac


def _fresh_paths():
    d = tempfile.mkdtemp()
    ac.STATE_FILE = os.path.join(d, "state.json")
    ac.SNAPSHOTS_FILE = os.path.join(d, "snapshots.jsonl")


def test_init_baseline():
    _fresh_paths()
    state = ac.init_challenge(198.62, 198.62)
    assert state["starting_balance"] == 198.62
    assert state["starting_equity"] == 198.62
    assert state["target_equity"] == 300.0
    assert state["status"] == "ACTIVE"
    assert state["peak_equity"] == 198.62
    start = datetime.fromisoformat(state["start_timestamp_utc"])
    end = datetime.fromisoformat(state["end_timestamp_utc"])
    assert abs((end - start) - timedelta(days=7)) < timedelta(seconds=1)
    print("test_init_baseline OK")


def test_init_refuses_while_active():
    _fresh_paths()
    ac.init_challenge(100.0, 100.0)
    try:
        ac.init_challenge(200.0, 200.0)
        assert False, "should have refused"
    except RuntimeError:
        pass
    state = ac.read_state()
    assert state["starting_equity"] == 100.0, "must not have been overwritten"
    print("test_init_refuses_while_active OK")


def test_snapshot_math():
    _fresh_paths()
    ac.init_challenge(100.0, 100.0)
    accounts = [{"balance": 60.0, "equity": 60.0}, {"balance": 50.0, "equity": 50.0, "positions": [1, 2]}]
    trades = [{"pnl": 5.0}, {"pnl": -2.0}, {"pnl": 3.0}]
    state = ac.record_snapshot(accounts, trades, oracle_fills_in_window=4, risk_halted=False)
    assert state["current_equity"] == 110.0
    assert state["current_balance"] == 110.0
    assert state["net_pnl"] == 10.0
    assert state["return_pct"] == 10.0
    assert state["peak_equity"] == 110.0
    assert state["current_drawdown_pct"] == 0.0
    assert state["wins"] == 2 and state["losses"] == 1 and state["total_trades"] == 3
    assert state["oracle_fills_in_window"] == 4
    assert state["open_positions"] == 2
    # equity drops -- drawdown should now show, peak should NOT drop
    accounts2 = [{"balance": 40.0, "equity": 40.0}, {"balance": 50.0, "equity": 50.0}]
    state2 = ac.record_snapshot(accounts2, [], 0, risk_halted=False)
    assert state2["current_equity"] == 90.0
    assert state2["peak_equity"] == 110.0
    assert round(state2["current_drawdown_pct"], 2) == round((110 - 90) / 110 * 100, 2)
    assert state2["max_drawdown_pct"] == state2["current_drawdown_pct"]
    print("test_snapshot_math OK")


def test_risk_halt_events_edge_triggered():
    _fresh_paths()
    ac.init_challenge(100.0, 100.0)
    accounts = [{"balance": 100.0, "equity": 100.0}]
    s1 = ac.record_snapshot(accounts, [], 0, risk_halted=True)
    assert s1["risk_halt_events"] == 1
    s2 = ac.record_snapshot(accounts, [], 0, risk_halted=True)  # still halted -- no new event
    assert s2["risk_halt_events"] == 1
    s3 = ac.record_snapshot(accounts, [], 0, risk_halted=False)  # cleared
    assert s3["risk_halt_events"] == 1
    s4 = ac.record_snapshot(accounts, [], 0, risk_halted=True)  # halts again -- 2nd event
    assert s4["risk_halt_events"] == 2
    print("test_risk_halt_events_edge_triggered OK")


def test_completes_after_end_time():
    _fresh_paths()
    state = ac.init_challenge(100.0, 100.0)
    state["end_timestamp_utc"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    ac._atomic_write(state)
    result = ac.record_snapshot([{"balance": 100.0, "equity": 100.0}], [], 0, risk_halted=False)
    assert result["status"] == "COMPLETED"
    # a further snapshot on a COMPLETED challenge must be a no-op, not restart it
    before = ac.read_state()
    noop = ac.record_snapshot([{"balance": 999.0, "equity": 999.0}], [], 0, risk_halted=False)
    after = ac.read_state()
    assert after == before, "COMPLETED challenge must not be mutated by further snapshots"
    print("test_completes_after_end_time OK")


def test_survives_simulated_restart():
    _fresh_paths()
    ac.init_challenge(198.62, 198.62)
    ac.record_snapshot([{"balance": 205.0, "equity": 205.0}], [{"pnl": 1.0}], 1, risk_halted=False)
    state_path = ac.STATE_FILE
    # simulate a process restart: read fresh from disk with no in-memory state carried over
    reloaded = None
    with open(state_path, encoding="utf-8") as f:
        import json
        reloaded = json.load(f)
    assert reloaded["current_equity"] == 205.0
    assert reloaded["total_trades"] == 1
    print("test_survives_simulated_restart OK")


def test_dashboard_survives_module_exception():
    _fresh_paths()
    # record_snapshot on a totally missing state file must return None, not raise
    result = ac.record_snapshot([{"balance": 1.0, "equity": 1.0}], [], 0, risk_halted=False)
    assert result is None
    print("test_dashboard_survives_module_exception OK")


if __name__ == "__main__":
    test_init_baseline()
    test_init_refuses_while_active()
    test_snapshot_math()
    test_risk_halt_events_edge_triggered()
    test_completes_after_end_time()
    test_survives_simulated_restart()
    test_dashboard_survives_module_exception()
    print("ALL autonomy_challenge TESTS PASSED")
