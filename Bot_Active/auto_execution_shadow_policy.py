"""
auto_execution_shadow_policy.py -- shadow-only execution policy audit.

Reads existing signal/candidate logs and records what the execution layer
would do for A-grade BTC/ETH/XAU setups. It never sends orders.

Gate order:
confirmed setup -> execution availability -> feed freshness -> spread/cost
-> duplicate/exposure -> protection/risk -> EXECUTE/VETO
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5

from heartbeat import write_heartbeat
from ladder_guard import ACCOUNTS as LIVE_MT5_ACCOUNTS

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(BASE, ".."))
OUT = os.path.join(BASE, "auto_execution_shadow_decisions.jsonl")
STATE = os.path.join(BASE, "auto_execution_shadow_state.json")
RISK_HALT = os.path.join(BASE, "RISK_HALT.flag")
CONTROL_SECRET = os.path.join(BASE, "control_secret.txt")
CONTROL_BASE = "http://127.0.0.1:5002"
POLL_SEC = 60
MAX_SIGNAL_AGE_SEC = 20 * 60
MAX_TICK_AGE_SEC = 90
MAX_SPREAD_ATR = 0.20
ASSETS = {"BTC", "ETH", "XAU"}

def now():
    return datetime.now(timezone.utc)


def parse_ts(v):
    if not v:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, timezone.utc)
    s = str(v).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S%z")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def truthy(v):
    return v is True or str(v).lower() == "true"


def asset_of(symbol):
    s = (symbol or "").upper()
    if s.startswith("BTC"):
        return "BTC"
    if s.startswith("ETH"):
        return "ETH"
    if s.startswith("XAU") or s == "XAUUSD":
        return "XAU"
    return None


def side_of(v):
    s = str(v or "").upper()
    if s in ("BUY", "LONG"):
        return "LONG"
    if s in ("SELL", "SHORT"):
        return "SHORT"
    return None


def order_style(rec):
    ot = str(rec.get("order_type") or "").upper()
    if "STOP" in ot or "LIMIT" in ot:
        return "PENDING_RETEST"
    return "MARKET"


def _gates_passed(gates):
    if not isinstance(gates, dict):
        return False
    checks = [
        gates.get("trend_gate_passed"),
        gates.get("momentum_gate_passed"),
        gates.get("atr_gate_passed"),
    ]
    return all(truthy(v) for v in checks)


def normalize_source_record(rec):
    """Map known shadow sources to one candidate schema.

    FILLED is accepted only for gold_btc_trace because that file is emitted
    after the live bot's own gates. Even there, the recorded gates must pass.
    """
    source_file = rec.get("source_file", "")
    base = os.path.basename(source_file)
    source = None
    if base.startswith("gold_btc_trace"):
        source = "gold_btc_trace"
    elif "pending_signal_shadow_engine" in source_file or base == "setups.jsonl" and "tp_levels" in rec:
        source = "pending_signal"
    elif "pending_breakout_shadow" in source_file or {"buy_stop", "sell_stop", "atr14_at_arm"} <= set(rec):
        source = "pending_breakout"
    elif base == "participation_pilot_range_signals.jsonl":
        source = "participation_pilot"

    if source == "gold_btc_trace":
        ts = parse_ts(rec.get("receive_time") or rec.get("logged_at"))
        symbol = rec.get("symbol")
        eligible = bool(
            str(rec.get("decision") or "").upper() == "FILLED"
            and truthy(rec.get("risk_eligible"))
            and not truthy(rec.get("risk_halt_active"))
            and _gates_passed(rec.get("gates"))
            and rec.get("sl") and rec.get("tp")
        )
        return [{
            "source": source,
            "source_file": source_file,
            "source_id": rec.get("order_ticket") or rec.get("receive_time") or rec.get("logged_at"),
            "candidate_ts": ts,
            "symbol": symbol,
            "asset": asset_of(symbol),
            "side": side_of(rec.get("direction")),
            "grade": "A" if eligible else "REJECTED",
            "eligible": eligible,
            "eligibility_reason": "gold_btc_trace_filled_with_passed_gates" if eligible else "gold_btc_trace_not_gate_confirmed",
            "order_type": "MARKET",
            "entry": rec.get("price"),
            "sl": rec.get("sl"),
            "tp": rec.get("tp"),
            "rr": rec.get("rr"),
            "atr": (rec.get("gates") or {}).get("atr"),
            "raw": rec,
        }]

    if source == "pending_signal":
        ts = parse_ts(rec.get("created_timestamp") or rec.get("signal_timestamp"))
        tp = (rec.get("tp_levels") or [None])[0]
        eligible = (
            rec.get("entry") and rec.get("sl") and tp
            and rec.get("expected_rr_after_cost") is not None
            and not truthy(rec.get("real_exposure_conflict"))
            and str(rec.get("state") or "WAITING").upper() in ("WAITING", "CREATED", "TRIGGERED")
        )
        symbol = rec.get("symbol")
        return [{
            "source": source,
            "source_file": source_file,
            "source_id": rec.get("order_id") or rec.get("created_timestamp") or rec.get("signal_timestamp"),
            "candidate_ts": ts,
            "symbol": symbol,
            "asset": asset_of(symbol),
            "side": side_of(rec.get("direction") or rec.get("order_type")),
            "grade": "A" if eligible else "REJECTED",
            "eligible": bool(eligible),
            "eligibility_reason": "pending_signal_valid_setup" if eligible else "pending_signal_incomplete_or_blocked",
            "order_type": rec.get("order_type"),
            "entry": rec.get("entry"),
            "sl": rec.get("sl"),
            "tp": tp,
            "rr": rec.get("expected_rr_after_cost"),
            "atr": rec.get("risk_distance"),
            "raw": rec,
        }]

    if source == "pending_breakout":
        ts = parse_ts(rec.get("timestamp_created"))
        eligible = bool(rec.get("buy_stop") and rec.get("sell_stop") and rec.get("atr14_at_arm"))
        rows = []
        for side, field in (("LONG", "buy_stop"), ("SHORT", "sell_stop")):
            rows.append({
                "source": source,
                "source_file": source_file,
                "source_id": f"{rec.get('setup_id')}:{field}:{rec.get('timestamp_created')}",
                "candidate_ts": ts,
                "symbol": "XAUUSD",
                "asset": "XAU",
                "side": side,
                "grade": "A" if eligible else "REJECTED",
                "eligible": eligible,
                "eligibility_reason": "pending_breakout_armed_straddle" if eligible else "pending_breakout_incomplete",
                "order_type": "BUY_STOP" if side == "LONG" else "SELL_STOP",
                "entry": rec.get(field),
                "sl": None,
                "tp": None,
                "rr": None,
                "atr": rec.get("atr14_at_arm"),
                "raw": rec,
            })
        return rows

    if source == "participation_pilot":
        eligible = str(rec.get("decision") or "").upper() == "ELIGIBLE" and truthy(rec.get("signal"))
        symbol = rec.get("symbol")
        return [{
            "source": source,
            "source_file": source_file,
            "source_id": rec.get("logged_at") or rec.get("bar_time"),
            "candidate_ts": parse_ts(rec.get("logged_at") or rec.get("bar_time")),
            "symbol": symbol,
            "asset": asset_of(symbol),
            "side": side_of(rec.get("bar_dir")),
            "grade": "A" if eligible else "REJECTED",
            "eligible": eligible,
            "eligibility_reason": "participation_pilot_eligible_signal" if eligible else "participation_pilot_not_eligible",
            "order_type": "MARKET",
            "entry": rec.get("price"),
            "sl": None,
            "tp": None,
            "rr": None,
            "atr": rec.get("atr14"),
            "raw": rec,
        }]

    return []


def is_a_grade(rec):
    norm = normalize_source_record(rec)
    if norm:
        return any(c.get("eligible") and c.get("grade") == "A" for c in norm)
    grade = str(rec.get("grade") or rec.get("setup_grade") or "").upper()
    if grade in ("A", "A_GRADE", "A-GRADE"):
        return True
    if rec.get("source_file") == "participation_pilot_range_signals.jsonl":
        return rec.get("decision") == "ELIGIBLE" and truthy(rec.get("signal"))
    if str(rec.get("decision") or "").upper() in ("ELIGIBLE", "EXECUTE"):
        return True
    return False


def read_jsonl_tail(path, max_lines=250):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()[-max_lines:]
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        rec["source_file"] = path
        out.append(rec)
    return out


def source_paths():
    return [
        os.path.join(BASE, f"gold_btc_trace_{now():%Y-%m-%d}.jsonl"),
        os.path.join(BASE, "participation_pilot_range_signals.jsonl"),
        os.path.join(ROOT, "Research_PendingSignalShadow_2026", "data",
                     "pending_signal_shadow_engine", "setups.jsonl"),
        os.path.join(ROOT, "Research_PendingBreakoutShadow_2026", "data",
                     "pending_breakout_shadow", "setups.jsonl"),
    ]


def candidates():
    cutoff = now() - timedelta(hours=24)
    for rec in [r for p in source_paths() for r in read_jsonl_tail(p)]:
        for c in normalize_source_record(rec):
            ts = c.get("candidate_ts")
            if c.get("asset") in ASSETS and ts and ts >= cutoff and c.get("side"):
                yield c


def append(row):
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def load_seen():
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding="utf-8") as f:
                return set(json.load(f).get("seen", []))
        except Exception:
            return set()
    return set()


def save_seen(seen):
    tmp = STATE + f".tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"seen": sorted(seen)[-5000:]}, f)
    os.replace(tmp, STATE)


def capability(account):
    try:
        headers = {}
        if os.path.exists(CONTROL_SECRET):
            headers["X-Control-Secret"] = open(CONTROL_SECRET, encoding="utf-8").read().strip()
        req = urllib.request.Request(f"{CONTROL_BASE}/api/control/capability/{account}", headers=headers)
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
        return bool(data.get("available")), data
    except Exception as e:
        return False, {"error": str(e)}


def connect(account):
    mt5.shutdown()
    cfg = LIVE_MT5_ACCOUNTS.get(account)
    if not cfg:
        return False
    return mt5.initialize(**dict(cfg))


def mt5_snapshot(account, symbol, side):
    if not connect(account):
        return {"ok": False, "error": str(mt5.last_error())}
    mt5.symbol_select(symbol, True)
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    positions = mt5.positions_get(symbol=symbol) or []
    orders = mt5.orders_get(symbol=symbol) or []
    if not tick or not info:
        return {"ok": False, "error": "missing tick or symbol_info"}
    price = tick.ask if side == "LONG" else tick.bid
    tick_time = datetime.fromtimestamp(tick.time, timezone.utc)
    return {
        "ok": True,
        "price": price,
        "bid": tick.bid,
        "ask": tick.ask,
        "spread": tick.ask - tick.bid,
        "tick_age_sec": (now() - tick_time).total_seconds(),
        "trade_allowed": bool((mt5.terminal_info() or object()).trade_allowed),
        "positions": len(positions),
        "orders": len(orders),
        "duplicate": any(getattr(p, "symbol", None) == symbol for p in positions)
                     or any(getattr(o, "symbol", None) == symbol for o in orders),
    }


def veto(row, stage, reason):
    row.update({"shadow_decision": "VETO", "veto_stage": stage, "reason": reason})
    return row


def decide(candidate, account, symbol):
    ts = candidate["candidate_ts"]
    asset = candidate["asset"]
    side = candidate["side"]
    key = "|".join([candidate.get("source_file", "?"), str(candidate.get("source_id")),
                    account, symbol, side])
    row = {
        "logged_at": now().isoformat(),
        "candidate_ts": ts.isoformat(),
        "account": account,
        "asset": asset,
        "symbol": symbol,
        "side": side,
        "setup_source": candidate.get("source"),
        "setup_source_file": candidate.get("source_file"),
        "setup_grade": candidate.get("grade", "UNKNOWN"),
        "source_eligibility_reason": candidate.get("eligibility_reason"),
        "intended_order_type": order_style(candidate),
        "no_live_order": True,
        "missed_opportunity": False,
        "late_entry_avoidance": False,
        "counterfactual_r": None,
        "key": key,
    }
    if not candidate.get("eligible"):
        return key, veto(row, "confirmed_setup", candidate.get("eligibility_reason") or "setup_not_a_grade")
    ok, cap = capability(account)
    row["execution_capability"] = cap
    if not ok:
        return key, veto(row, "execution_availability", "execution_surface_unavailable_or_policy_blocked")
    snap = mt5_snapshot(account, symbol, side)
    row["mt5_snapshot"] = snap
    if not snap.get("ok"):
        return key, veto(row, "feed_freshness", snap.get("error", "mt5_snapshot_failed"))
    age = (now() - ts).total_seconds()
    if age > MAX_SIGNAL_AGE_SEC:
        row["late_entry_avoidance"] = True
        row["missed_opportunity"] = True
        return key, veto(row, "feed_freshness", f"signal_stale_{age:.0f}s")
    if snap["tick_age_sec"] > MAX_TICK_AGE_SEC:
        return key, veto(row, "feed_freshness", f"tick_stale_{snap['tick_age_sec']:.0f}s")
    atr = candidate.get("atr")
    if atr and snap["spread"] / float(atr) > MAX_SPREAD_ATR:
        return key, veto(row, "spread_cost", f"spread_atr_too_high:{snap['spread'] / float(atr):.3f}")
    if snap["duplicate"]:
        return key, veto(row, "duplicate_exposure", "existing_position_or_pending_order")
    if os.path.exists(RISK_HALT):
        return key, veto(row, "protection_risk", "RISK_HALT.flag_active")
    if not snap["trade_allowed"]:
        return key, veto(row, "protection_risk", "terminal_trade_not_allowed")
    row.update({"shadow_decision": "EXECUTE", "veto_stage": None, "reason": "shadow_only_passed_all_gates"})
    return key, row


def accounts_for(asset):
    if asset == "XAU":
        return [("EA", "XAUUSDm"), ("BA", "XAUUSD.s")]
    if asset in ("BTC", "ETH"):
        return [("EA", "BTCUSDm" if asset == "BTC" else "ETHUSDm")]
    return []


def run_once():
    seen = load_seen()
    emitted = 0
    for candidate in candidates():
        if not candidate.get("eligible"):
            continue
        for account, acct_symbol in accounts_for(candidate["asset"]):
            key, row = decide(candidate, account, acct_symbol)
            if key in seen:
                continue
            seen.add(key)
            append(row)
            emitted += 1
    save_seen(seen)
    write_heartbeat("auto_execution_shadow_policy", emitted=emitted)
    return emitted


def _demo():
    assert asset_of("BTCUSDm") == "BTC"
    assert asset_of("ETHUSDm") == "ETH"
    assert asset_of("XAUUSD.s") == "XAU"
    assert side_of("sell") == "SHORT"
    assert truthy("True")
    assert is_a_grade({
        "source_file": "gold_btc_trace_2026-08-24.jsonl", "decision": "FILLED",
        "risk_eligible": True, "risk_halt_active": False, "symbol": "XAUUSDm",
        "direction": "buy", "sl": 1, "tp": 2,
        "gates": {"trend_gate_passed": True, "momentum_gate_passed": True, "atr_gate_passed": True},
    })
    assert not is_a_grade({"source_file": "gold_btc_trace_2026-08-24.jsonl", "decision": "FILLED"})
    assert not is_a_grade({"decision": "REJECTED"})
    print("auto_execution_shadow_policy self-check: OK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    if args.test:
        _demo()
        return
    if args.once:
        print(f"emitted={run_once()}")
        return
    while True:
        try:
            print(f"[{now().isoformat()}] emitted={run_once()}", flush=True)
        except Exception as e:
            print(f"[{now().isoformat()}] ERROR {e}", flush=True)
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
