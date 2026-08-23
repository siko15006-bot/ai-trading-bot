"""
risk_halt_gate.py -- fail-closed portfolio-wide RISK_HALT check for Oracle/Bybit bots.

2026-08-08 (Ahmed, explicit approval, Risk Change): the Windows-side
portfolio_risk_guard.py already halts new entries on EA/EM/BA when daily
drawdown >= 15% (see RISK_HALT.flag), but Oracle bots run on a separate
server with no shared filesystem and never saw that state. This closes the
gap: risk_guard_autorun.py (Windows, every 5 min via Task Scheduler) pushes
{"halted": bool, "pushed_epoch": <unix ts>} to this file over the same SSH
channel status_dashboard.py already uses to pull Oracle data, just in the
opposite direction.

Fail-closed by design: missing file, corrupt JSON, or a push older than
STALE_SEC (connectivity loss, Windows crash, Task Scheduler stopped) all
mean "halted". An unknown state must never be treated as safe to trade on
a live account.
"""
import json
import os
import time

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "risk_halt_state.json")
STALE_SEC = 900  # 3x the 5-min push interval -- same staleness convention used elsewhere in this project


def is_portfolio_halted(path=STATE_FILE):
    try:
        with open(path) as f:
            data = json.load(f)
        age = time.time() - float(data["pushed_epoch"])
        if age > STALE_SEC:
            return True  # fail-closed: stale sync
        return bool(data["halted"])
    except Exception:
        return True  # fail-closed: missing/corrupt/unreadable


def _demo():
    import tempfile
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    try:
        # halted=true, fresh -> blocked
        json.dump({"halted": True, "pushed_epoch": time.time()}, tmp)
        tmp.close()
        assert is_portfolio_halted(tmp.name) is True, "fresh halted=true must block"

        # halted=false, fresh -> allowed
        with open(tmp.name, "w") as f:
            json.dump({"halted": False, "pushed_epoch": time.time()}, f)
        assert is_portfolio_halted(tmp.name) is False, "fresh halted=false must allow"

        # halted=false, but stale -> fail-closed, blocked anyway
        with open(tmp.name, "w") as f:
            json.dump({"halted": False, "pushed_epoch": time.time() - STALE_SEC - 1}, f)
        assert is_portfolio_halted(tmp.name) is True, "stale push must fail-closed to blocked"

        # missing file -> fail-closed, blocked
        assert is_portfolio_halted("/tmp/does_not_exist_risk_halt.json") is True, \
            "missing state file must fail-closed to blocked"

        # corrupt file -> fail-closed, blocked
        with open(tmp.name, "w") as f:
            f.write("not valid json{{{")
        assert is_portfolio_halted(tmp.name) is True, "corrupt state file must fail-closed to blocked"

        print("risk_halt_gate self-check OK")
    finally:
        os.unlink(tmp.name)


if __name__ == "__main__":
    _demo()
