"""Read-only ORBIT shadow scoreboard normalizer.

No MT5/order APIs. It labels suspicious shadows conservatively so dashboard
consumers do not promote a paper edge from broken or single-regime metrics.
"""
import json
import os
from datetime import datetime, timezone

BASE = os.getenv("TRADINGBOT_BASE", r"C:\TradingBot\Bot_Active")
ROOT = os.path.dirname(BASE)
OUT = os.path.join(BASE, "orbit_shadow_scoreboard.json")


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def read_jsonl(path):
    rows = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return rows


def r_value(row):
    return next((float(row[k]) for k in ("r_result", "result_r", "r") if isinstance(row.get(k), (int, float))), None)


def gold_btc_quality(outcomes):
    rs = [r_value(r) for r in outcomes]
    rs = [r for r in rs if r is not None]
    maes = [float(r.get("mae")) for r in outcomes if isinstance(r.get("mae"), (int, float))]
    suspicious = bool(rs) and (all(r > 0 for r in rs) or len(set(round(r, 6) for r in rs)) == 1 or all(m == 0 for m in maes))
    return {
        "quality_label": "SINGLE_REGIME / SUSPICIOUS_METRICS / NEED_MORE_DATA" if suspicious else "NEED_MORE_DATA",
        "metric_caveat": "do not treat as proven edge until exact MAE=0/exact R=2.0 and regime coverage are explained",
    }


def pending_signal_gate(summary, heartbeat, bridge_state):
    reason = summary.get("readiness") or "NOT_READY"
    # Timestamp/feed proof is only accepted when both engine heartbeat and bridge state expose a recent timestamp.
    feed_ts = bridge_state.get("last_signal_ts") or bridge_state.get("last_message_ts")
    hb_ts = heartbeat.get("last_success_ts")
    if not feed_ts or not hb_ts:
        reason = "NOT_READY/timestamp-feed path not proven"
    return reason


def build():
    gb_rows = read_jsonl(os.path.join(BASE, "gold_btc_momentum_shadow.jsonl"))
    gb_out = [r for r in gb_rows if r.get("type") == "outcome"]
    psd = os.path.join(ROOT, "Research_PendingSignalShadow_2026", "data", "pending_signal_shadow_engine")
    ps = read_json(os.path.join(psd, "dashboard_summary.json"), {}) or {}
    hb = read_json(os.path.join(psd, "heartbeat.json"), {}) or {}
    bridge = read_json(os.path.join(psd, "signal_bridge_state.json"), {}) or {}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema": 2,
        "items": [
            {
                "name": "gold_btc_momentum_shadow",
                "samples": len(gb_out),
                "verdict": "NEED_MORE_DATA",
                **gold_btc_quality(gb_out),
            },
            {
                "name": "pending_signal_shadow",
                "samples": ps.get("closed_total"),
                "wr_pct": ps.get("win_rate_pct"),
                "pf": ps.get("profit_factor"),
                "expectancy_r": ps.get("expectancy_r"),
                "blocking_gate": pending_signal_gate(ps, hb, bridge),
                "verdict": "NOT_READY",
            },
        ],
    }


def _demo():
    out = gold_btc_quality([{"r_result": 2.0, "mae": 0.0} for _ in range(29)])
    assert "SUSPICIOUS_METRICS" in out["quality_label"]
    assert pending_signal_gate({}, {"last_success_ts": "x"}, {}) == "NOT_READY/timestamp-feed path not proven"
    print("orbit_shadow_scoreboard self-check OK")


if __name__ == "__main__":
    if "--test" in os.sys.argv:
        _demo()
    else:
        data = build()
        tmp = OUT + f".tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, OUT)
        print(json.dumps(data, indent=2, default=str))
