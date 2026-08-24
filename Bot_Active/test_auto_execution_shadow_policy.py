import os
import sys
import json
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auto_execution_shadow_policy as p


def _read_jsonl(path):
    rows = []
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rec["source_file"] = path
            rows.append(rec)
    return rows


def _gold_trace():
    return {
        "source_file": r"C:\TradingBot\Bot_Active\gold_btc_trace_2026-08-24.jsonl",
        "receive_time": p.now().isoformat(),
        "account": "EA",
        "symbol": "XAUUSDm",
        "price": 4636.915,
        "decision": "FILLED",
        "risk_eligible": True,
        "risk_halt_active": False,
        "direction": "buy",
        "gates": {
            "adx_min_used": 22,
            "adx": 40.01,
            "trend_gate_passed": True,
            "rsi": 64.44,
            "momentum_gate_passed": "True",
            "atr": 8.33,
            "atr_gate_passed": "True",
        },
        "sl": 4632.7471,
        "tp": 4645.2507,
        "rr": 2.0,
        "retcode": 10009,
        "order_ticket": 324017421,
    }


def test_gold_btc_trace_filled_requires_gate_semantics():
    c = p.normalize_source_record(_gold_trace())[0]
    assert c["source"] == "gold_btc_trace"
    assert c["eligible"] is True
    assert c["asset"] == "XAU"
    assert c["side"] == "LONG"

    filled_only = {"source_file": _gold_trace()["source_file"], "decision": "FILLED"}
    assert p.normalize_source_record(filled_only)[0]["eligible"] is False


def test_pending_signal_maps_to_canonical_candidate():
    rec = {
        "source_file": r"C:\TradingBot\Research_PendingSignalShadow_2026\data\pending_signal_shadow_engine\setups.jsonl",
        "order_id": 1,
        "symbol": "BTCUSDm",
        "direction": "BUY",
        "order_type": "BUY_STOP",
        "entry": 70449.84,
        "sl": 69785.61,
        "tp_levels": [71114.08, 71778.31],
        "expected_rr_after_cost": 0.98,
        "risk_distance": 664.23,
        "signal_timestamp": p.now().isoformat(),
        "created_timestamp": p.now().isoformat(),
        "real_exposure_conflict": False,
        "state": "WAITING",
    }
    c = p.normalize_source_record(rec)[0]
    assert c["source"] == "pending_signal"
    assert c["eligible"] is True
    assert c["asset"] == "BTC"
    assert c["side"] == "LONG"
    assert c["order_type"] == "BUY_STOP"


def test_pending_breakout_maps_to_two_xau_candidates():
    rec = {
        "source_file": r"C:\TradingBot\Research_PendingBreakoutShadow_2026\data\pending_breakout_shadow\setups.jsonl",
        "setup_id": 7,
        "timestamp_created": p.now().isoformat(),
        "buy_stop": 4340.149,
        "sell_stop": 4337.769,
        "base_distance": 1.02,
        "atr14_at_arm": 2.04,
        "ask_at_arm": 4339.129,
        "bid_at_arm": 4338.789,
    }
    rows = p.normalize_source_record(rec)
    assert [r["side"] for r in rows] == ["LONG", "SHORT"]
    assert all(r["source"] == "pending_breakout" and r["eligible"] for r in rows)
    assert all(r["asset"] == "XAU" for r in rows)


def test_normalized_candidate_reaches_later_gate(monkeypatch=None):
    c = p.normalize_source_record(_gold_trace())[0]
    c["candidate_ts"] = p.now() - timedelta(seconds=p.MAX_SIGNAL_AGE_SEC + 1)

    old_capability = p.capability
    old_snapshot = p.mt5_snapshot
    try:
        p.capability = lambda account: (True, {"available": True})
        p.mt5_snapshot = lambda account, symbol, side: {
            "ok": True,
            "price": 1,
            "bid": 1,
            "ask": 1.1,
            "spread": 0.1,
            "tick_age_sec": 0,
            "trade_allowed": True,
            "positions": 0,
            "orders": 0,
            "duplicate": False,
        }
        _key, row = p.decide(c, "EA", "XAUUSDm")
    finally:
        p.capability = old_capability
        p.mt5_snapshot = old_snapshot

    assert row["shadow_decision"] == "VETO"
    assert row["veto_stage"] == "feed_freshness"
    assert row["reason"].startswith("signal_stale_")


def test_real_source_rows_normalize_to_eligible_candidates():
    sources = {
        "gold_btc_trace": os.path.join(p.BASE, "gold_btc_trace_2026-08-24.jsonl"),
        "pending_signal": os.path.join(
            p.ROOT, "Research_PendingSignalShadow_2026", "data",
            "pending_signal_shadow_engine", "setups.jsonl",
        ),
        "pending_breakout": os.path.join(
            p.ROOT, "Research_PendingBreakoutShadow_2026", "data",
            "pending_breakout_shadow", "setups.jsonl",
        ),
    }
    proof = {}
    for name, path in sources.items():
        rows = [c for r in _read_jsonl(path) for c in p.normalize_source_record(r)]
        proof[name] = sum(1 for c in rows if c.get("eligible") and c.get("grade") == "A")
    assert proof["gold_btc_trace"] > 0, proof
    assert proof["pending_signal"] > 0, proof
    assert proof["pending_breakout"] > 0, proof


def test_real_source_candidates_reach_later_gates():
    paths = [
        os.path.join(p.BASE, "gold_btc_trace_2026-08-24.jsonl"),
        os.path.join(p.ROOT, "Research_PendingSignalShadow_2026", "data",
                     "pending_signal_shadow_engine", "setups.jsonl"),
        os.path.join(p.ROOT, "Research_PendingBreakoutShadow_2026", "data",
                     "pending_breakout_shadow", "setups.jsonl"),
    ]
    candidates = []
    for path in paths:
        for rec in _read_jsonl(path):
            for c in p.normalize_source_record(rec):
                if c.get("eligible") and c.get("grade") == "A":
                    candidates.append(c)
                    break
            if candidates and candidates[-1].get("source_file") == path:
                break
    assert {c["source"] for c in candidates} == {"gold_btc_trace", "pending_signal", "pending_breakout"}

    old_capability = p.capability
    old_snapshot = p.mt5_snapshot
    try:
        p.capability = lambda account: (True, {"available": True})
        p.mt5_snapshot = lambda account, symbol, side: {
            "ok": True,
            "price": 1,
            "bid": 1,
            "ask": 1.01,
            "spread": 0.01,
            "tick_age_sec": 0,
            "trade_allowed": True,
            "positions": 0,
            "orders": 0,
            "duplicate": False,
        }
        stages = {}
        for c in candidates:
            c["candidate_ts"] = p.now() - timedelta(seconds=p.MAX_SIGNAL_AGE_SEC + 1)
            _key, row = p.decide(c, "EA", "XAUUSDm" if c["asset"] == "XAU" else f"{c['asset']}USDm")
            stages[c["source"]] = row["veto_stage"]
    finally:
        p.capability = old_capability
        p.mt5_snapshot = old_snapshot

    assert stages == {
        "gold_btc_trace": "feed_freshness",
        "pending_signal": "feed_freshness",
        "pending_breakout": "feed_freshness",
    }


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name}: OK")
