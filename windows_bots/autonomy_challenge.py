"""
autonomy_challenge.py -- read-only tracking for the "7-Day Autonomy / $300"
challenge (Ahmed's 2026-08-09 request). Monitoring/dashboard scope ONLY:
this module never calls any order/execution function, never reads or
writes RISK_HALT.flag or portfolio_risk_state.json (those stay
portfolio_risk_guard.py's alone -- Ahmed explicitly required the two
baselines to stay completely separate). $300 is a display/measurement
target only -- nothing here branches trading behavior on progress toward it.
"""
import json
import os
from datetime import datetime, timedelta, timezone

STATE_FILE = os.path.join(os.path.dirname(__file__), "autonomy_challenge_state.json")
SNAPSHOTS_FILE = os.path.join(os.path.dirname(__file__), "autonomy_challenge_snapshots.jsonl")
TARGET_EQUITY = 300.0
DURATION_DAYS = 7


def _now():
    return datetime.now(timezone.utc)


def _atomic_write(state):
    # same temp-file + os.replace pattern as portfolio_risk_guard.py's
    # save_state() -- a plain open("w") there once left a state file
    # empty mid-write; this avoids repeating that bug.
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def read_state():
    """Read-only. Always hits disk -- never cached in memory -- so this
    survives a status_dashboard.py restart or a full machine restart
    without any special recovery step."""
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def init_challenge(starting_balance, starting_equity):
    """One-time. Refuses to run if an ACTIVE challenge already exists --
    never silently resets or overwrites a running challenge's baseline."""
    existing = read_state()
    if existing and existing.get("status") == "ACTIVE":
        raise RuntimeError(
            f"An ACTIVE challenge already exists "
            f"(challenge_id={existing.get('challenge_id')}, "
            f"started {existing.get('start_timestamp_utc')}) -- refusing to reinitialize."
        )
    now = _now()
    state = {
        "challenge_id": f"autonomy_7d_{now.strftime('%Y-%m-%d')}",
        "start_timestamp_utc": now.isoformat(),
        "end_timestamp_utc": (now + timedelta(days=DURATION_DAYS)).isoformat(),
        "starting_balance": round(starting_balance, 2),
        "starting_equity": round(starting_equity, 2),
        "target_equity": TARGET_EQUITY,
        "current_equity": round(starting_equity, 2),
        "current_balance": round(starting_balance, 2),
        "net_pnl": 0.0,
        "return_pct": 0.0,
        "peak_equity": round(starting_equity, 2),
        "current_drawdown_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "oracle_fills_in_window": 0,
        "open_positions": 0,
        "risk_halt_events": 0,
        "_last_risk_halted": False,
        "status": "ACTIVE",
        "last_snapshot_utc": None,
    }
    _atomic_write(state)
    return state


def _sum_accounts(accounts):
    ok = [a for a in accounts if "error" not in a]
    incomplete = len(ok) != len(accounts)  # an account errored (e.g. BAA/Oracle SSH down)
    balance = sum(a.get("balance", 0.0) or 0.0 for a in ok)
    equity = sum(a.get("equity", 0.0) or 0.0 for a in ok)
    positions = sum(len(a.get("positions") or []) for a in ok)
    return balance, equity, positions, incomplete


def record_snapshot(accounts, mt5_trades_in_window, oracle_fills_in_window, risk_halted):
    """Read-only. Meant to be called once per existing status_dashboard.py
    refresh_loop() cycle -- no new thread/schedule. Never raises: any
    failure here must never break the dashboard's own refresh, so the
    whole body is guarded.

    execution_errors / rejected_orders / bot_health are deliberately NOT
    tracked here as a cumulative count -- a persistent recurring error
    re-scanned every cycle would double-count exactly like the false
    positives fixed earlier in last_error()'s recency window. Those three
    stay live-only fields, read fresh at request time by /api/monitor
    (which already computes them), not persisted into this state file.
    """
    try:
        state = read_state()
        if not state or state.get("status") != "ACTIVE":
            return state  # no active challenge, or already COMPLETED -- nothing to do
        now = _now()
        end = datetime.fromisoformat(state["end_timestamp_utc"])
        balance, equity, positions, incomplete = _sum_accounts(accounts)
        if incomplete:
            # An account is unreadable this cycle (e.g. BAA/Oracle SSH down). A
            # partial sum is a FALSE equity/drawdown -- never persist it, and never
            # let it poison the cumulative max_drawdown_pct. Skip the write and keep
            # the last good snapshot. (Codex-flagged monitoring bug, 2026-08-09.)
            state["last_incomplete_utc"] = now.isoformat()
            _atomic_write(state)
            return state
        starting_equity = state["starting_equity"]
        net_pnl = equity - starting_equity
        peak_equity = max(state["peak_equity"], equity)
        current_dd = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0.0
        max_dd = max(state["max_drawdown_pct"], current_dd)
        wins = sum(1 for t in mt5_trades_in_window if t["pnl"] > 0)
        losses = sum(1 for t in mt5_trades_in_window if t["pnl"] < 0)
        was_halted = state.get("_last_risk_halted", False)
        risk_halt_events = state["risk_halt_events"] + (1 if (risk_halted and not was_halted) else 0)
        status = "COMPLETED" if now >= end else "ACTIVE"
        state.update(
            current_balance=round(balance, 2),
            current_equity=round(equity, 2),
            net_pnl=round(net_pnl, 2),
            return_pct=round(net_pnl / starting_equity * 100, 2) if starting_equity else 0.0,
            peak_equity=round(peak_equity, 2),
            current_drawdown_pct=round(current_dd, 2),
            max_drawdown_pct=round(max_dd, 2),
            total_trades=len(mt5_trades_in_window),
            wins=wins,
            losses=losses,
            oracle_fills_in_window=oracle_fills_in_window,
            open_positions=positions,
            risk_halt_events=risk_halt_events,
            _last_risk_halted=risk_halted,
            status=status,
            last_snapshot_utc=now.isoformat(),
        )
        _atomic_write(state)
        with open(SNAPSHOTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": now.isoformat(),
                "equity": state["current_equity"],
                "balance": state["current_balance"],
                "net_pnl": state["net_pnl"],
                "drawdown_pct": state["current_drawdown_pct"],
                "total_trades": state["total_trades"],
                "status": status,
            }) + "\n")
        return state
    except Exception:
        return None


def _selftest():
    # incomplete account set (e.g. BAA errored) must NOT produce a partial sum
    full = [{"equity": 100.0, "balance": 100.0}, {"equity": 50.0, "balance": 50.0}]
    b, e, p, inc = _sum_accounts(full)
    assert e == 150.0 and inc is False, (e, inc)
    b, e, p, inc = _sum_accounts(full + [{"error": "ssh down"}])
    assert inc is True, "errored account must flag the set incomplete"
    print("autonomy_challenge selftest OK")


if __name__ == "__main__":
    _selftest()
