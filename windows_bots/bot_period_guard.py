"""Per-bot daily/weekly profit and loss entry guard (no trade execution)."""

import re
import math
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import msvcrt

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research_ml"))
import state_io  # noqa: E402


DAILY_LOSS_PCT = 2.0
DAILY_PROFIT_PCT = 2.0
WEEKLY_LOSS_PCT = 5.0
WEEKLY_PROFIT_PCT = 5.0
STATE_DIR = Path(__file__).resolve().parent / "bot_period_guard_state"


def _safe_name(value):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value))


def _state_path(bot, account, state_dir=STATE_DIR):
    return Path(state_dir) / f"{_safe_name(bot)}_{_safe_name(account)}.json"


def manual_block_reason(bot, account, state_dir=STATE_DIR):
    """Return a manual override reason if this bot/account is explicitly blocked."""
    state = state_io.read_json_safe(_state_path(bot, account, state_dir), default=None)
    if isinstance(state, dict):
        reason = state.get("manual_lock")
        if reason:
            return str(reason)
    return None


def _trigger(pnl, baseline, loss_pct, profit_pct, period):
    if baseline <= 0:
        return f"{period}_invalid_baseline"
    pnl_pct = pnl / baseline * 100
    if pnl_pct <= -loss_pct:
        return f"{period}_loss_limit"
    if pnl_pct >= profit_pct:
        return f"{period}_profit_target"
    return None


@contextmanager
def _state_lock(path):
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def check(bot, account, balance, daily_realized_pnl, weekly_realized_pnl,
          now=None, state_dir=STATE_DIR):
    """Persist period locks and return a read-only entry decision."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    balance = float(balance)
    daily_realized_pnl = float(daily_realized_pnl)
    weekly_realized_pnl = float(weekly_realized_pnl)
    if balance <= 0 or not all(math.isfinite(value) for value in
                               (balance, daily_realized_pnl, weekly_realized_pnl)):
        return {"allow_new_entries": False, "reason": "invalid_input_review_required",
                "daily_pnl": None, "weekly_pnl": None, "state_path": None}
    day = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
    week = now.astimezone(timezone.utc).strftime("%G-W%V")
    path = _state_path(bot, account, state_dir)
    required = {"bot", "account", "day", "week", "daily_baseline", "weekly_baseline",
                "daily_pnl_offset", "weekly_pnl_offset", "daily_lock", "weekly_lock"}
    with _state_lock(path):
        existed = path.exists()
        state = state_io.read_json_safe(path, default=None)
        if existed and (not isinstance(state, dict) or not required.issubset(state)
                        or state["bot"] != bot or state["account"] != account):
            return {"allow_new_entries": False, "reason": "state_corrupt_review_required",
                    "daily_pnl": None, "weekly_pnl": None, "state_path": str(path)}

        if state is None:
            state = {
                "bot": bot, "account": account,
                "day": day, "week": week,
                "daily_baseline": balance, "weekly_baseline": balance,
                "daily_pnl_offset": daily_realized_pnl,
                "weekly_pnl_offset": weekly_realized_pnl,
                "daily_lock": None, "weekly_lock": None,
            }
        else:
            if state["week"] != week:
                state.update(week=week, weekly_baseline=balance - weekly_realized_pnl,
                             weekly_pnl_offset=0.0, weekly_lock=None)
            if state["day"] != day:
                state.update(day=day, daily_baseline=balance - daily_realized_pnl,
                             daily_pnl_offset=0.0, daily_lock=None)

        daily_pnl = daily_realized_pnl - state["daily_pnl_offset"]
        weekly_pnl = weekly_realized_pnl - state["weekly_pnl_offset"]
        state["daily_lock"] = state["daily_lock"] or _trigger(
            daily_pnl, state["daily_baseline"], DAILY_LOSS_PCT, DAILY_PROFIT_PCT, "daily")
        state["weekly_lock"] = state["weekly_lock"] or _trigger(
            weekly_pnl, state["weekly_baseline"], WEEKLY_LOSS_PCT, WEEKLY_PROFIT_PCT, "weekly")
        state.update(last_checked_utc=now.astimezone(timezone.utc).isoformat(),
                     daily_pnl=daily_pnl, weekly_pnl=weekly_pnl)
        state_io.atomic_write_json(path, state)

        reason = state["weekly_lock"] or state["daily_lock"]
        return {
            "allow_new_entries": reason is None,
            "reason": reason,
            "daily_pnl": round(daily_pnl, 4),
            "weekly_pnl": round(weekly_pnl, 4),
            "state_path": str(path),
        }


def _self_test():
    import tempfile

    root = Path(tempfile.mkdtemp())
    t1 = datetime(2026, 8, 18, 10, tzinfo=timezone.utc)
    first = check("bot", "EA", 100, 5, 7, t1, root)
    assert first["allow_new_entries"]  # first deployment starts measuring now
    profit = check("bot", "EA", 100, 7.1, 9.1, t1, root)
    assert not profit["allow_new_entries"] and profit["reason"] == "daily_profit_target"
    latched = check("bot", "EA", 100, 5, 7, t1, root)
    assert latched["reason"] == "daily_profit_target"
    next_day = check("bot", "EA", 100, 0, 7, datetime(2026, 8, 19, 1, tzinfo=timezone.utc), root)
    assert next_day["allow_new_entries"]
    weekly_loss = check("bot", "EA", 100, -1, 1.9, datetime(2026, 8, 19, 2, tzinfo=timezone.utc), root)
    assert not weekly_loss["allow_new_entries"] and weekly_loss["reason"] == "weekly_loss_limit"
    next_week = check("bot", "EA", 100, 0, 0, datetime(2026, 8, 24, 1, tzinfo=timezone.utc), root)
    assert next_week["allow_new_entries"]
    rollover_root = root / "rollover"
    check("rollover", "EA", 100, 0, 0, t1, rollover_root)
    check("rollover", "EA", 102, 2, 2, datetime(2026, 8, 19, 1, tzinfo=timezone.utc), rollover_root)
    rollover_state = state_io.read_json_safe(_state_path("rollover", "EA", rollover_root))
    assert rollover_state["daily_baseline"] == 100, "rollover baseline must exclude current-period PnL"
    corrupt = _state_path("bad", "EA", root)
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text("{broken", encoding="utf-8")
    blocked = check("bad", "EA", 100, 0, 0, t1, root)
    assert blocked["reason"] == "state_corrupt_review_required"
    assert corrupt.read_text(encoding="utf-8") == "{broken", "corrupt state must never be overwritten"
    wrong_type = _state_path("wrong_type", "EA", root)
    wrong_type.write_text("0", encoding="utf-8")
    assert check("wrong_type", "EA", 100, 0, 0, t1, root)["reason"] == "state_corrupt_review_required"
    invalid = check("invalid", "EA", float("nan"), 0, 0, t1, root)
    assert invalid["reason"] == "invalid_input_review_required"
    manual = _state_path("manual", "EA", root)
    state_io.atomic_write_json(manual, {
        "bot": "manual", "account": "EA",
        "day": "2026-08-18", "week": "2026-W34",
        "daily_baseline": 100, "weekly_baseline": 100,
        "daily_pnl_offset": 0, "weekly_pnl_offset": 0,
        "daily_lock": None, "weekly_lock": None,
        "manual_lock": "manual_block",
    })
    assert manual_block_reason("manual", "EA", root) == "manual_block"
    print("bot_period_guard selftest OK")


if __name__ == "__main__":
    _self_test()
