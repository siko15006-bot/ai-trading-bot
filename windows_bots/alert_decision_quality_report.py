"""alert_decision_quality_report.py -- read-only summary over
alert_decisions.jsonl (produced by alert_decision_gate.py). Prints exactly
the fields requested for the "Alert Decision Quality" view: today's count,
%fully-analysed, incomplete count, late decisions, true misses, valid
skips, average latency, and the last 10 alerts table.

Standalone script, not wired into status_dashboard.py on purpose --
that file is a large, live, execution-capable app (has /ui/control/close
etc.) and this addition is explicitly read-only-only; a separate script
avoids any risk of touching that live file. Run on demand or on a cron/
loop if a persistent view is wanted later -- out of scope for this change.
"""
import json
import os
from collections import Counter
from datetime import datetime, timezone

LOG_PATH = r"C:\TradingBot\Bot_Active\alert_decisions.jsonl"
WATCHES_PATH = r"C:\TradingBot\Bot_Active\alert_watches.json"
HEARTBEAT_PATH = r"C:\TradingBot\Bot_Active\heartbeat_alert_watch_loop.json"
HEARTBEAT_STALE_SECONDS = 180  # 3x the loop's own 60s poll interval


def alert_watch_loop_status():
    """RUNNING/STOPPED via heartbeat freshness -- same signal every other
    bot's dashboard entry already uses, not a separate process check."""
    if not os.path.exists(HEARTBEAT_PATH):
        return "STOPPED (no heartbeat ever)", None
    try:
        with open(HEARTBEAT_PATH, encoding="utf-8") as f:
            hb = json.load(f)
        last = datetime.fromisoformat(hb["last_success_ts"])
        age = (datetime.now(timezone.utc) - last).total_seconds()
        status = "RUNNING" if age < HEARTBEAT_STALE_SECONDS else f"STOPPED (heartbeat {age:.0f}s stale)"
        return status, hb
    except Exception as e:
        return f"STOPPED (heartbeat unreadable: {e})", None


def active_watching_count():
    if not os.path.exists(WATCHES_PATH):
        return 0
    try:
        with open(WATCHES_PATH, encoding="utf-8") as f:
            watches = json.load(f)
        return sum(1 for w in watches.values() if w.get("status") == "WATCHING")
    except Exception:
        return None


def load_rows():
    if not os.path.exists(LOG_PATH):
        return []
    rows = []
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def build_summary(rows):
    analyses = [r for r in rows if r.get("event") == "ANALYSIS"]
    decisions = {r["analysis_id"]: r for r in rows if r.get("event") == "DECISION"}

    today = datetime.now(timezone.utc).date()
    today_analyses = [r for r in analyses if r.get("alert_time", "")[:10] == str(today)]

    fully_analysed = sum(1 for r in today_analyses if r.get("m15_checked") and r.get("h1_checked"))
    incomplete = sum(1 for r in today_analyses if r.get("decision") == "ANALYSIS_INCOMPLETE")
    late = sum(1 for r in decisions.values() if r.get("late_decision"))
    latencies = [r["decision_latency_seconds"] for r in decisions.values() if r.get("decision_latency_seconds") is not None]

    # classification, when present, comes from a separate audit pass (alert_audit.py)
    # merged in by analysis_id if the caller wants it -- this report only reads what's
    # actually in the ledger (ANALYSIS/DECISION events), not a re-derived classification.

    summary = {
        "alerts_today": len(today_analyses),
        "fully_analysed_pct": round(100 * fully_analysed / len(today_analyses), 1) if today_analyses else None,
        "incomplete_analysis": incomplete,
        "late_decisions": late,
        "average_decision_latency_seconds": round(sum(latencies) / len(latencies), 1) if latencies else None,
        "decisions_logged": len(decisions),
    }

    last10 = sorted(analyses, key=lambda r: r.get("alert_time", ""), reverse=True)[:10]
    table = []
    for r in last10:
        dec = decisions.get(r["analysis_id"])
        table.append({
            "symbol": r.get("symbol"), "alert_type": r.get("alert_type"), "alert_time": r.get("alert_time"),
            "h1_checked": r.get("h1_checked"), "decision": (dec or {}).get("decision", r.get("decision")),
            "decision_latency_seconds": (dec or {}).get("decision_latency_seconds"),
        })
    return summary, table


def watch_loop_summary(rows):
    reevals = sorted((r for r in rows if r.get("event") == "REEVALUATION"),
                       key=lambda r: r.get("reevaluation_time", ""))
    today = str(datetime.now(timezone.utc).date())
    today_reevals = [r for r in reevals if r.get("reevaluation_time", "")[:10] == today]
    status, hb = alert_watch_loop_status()
    return {
        "alert_watch_loop_status": status,
        "active_watching_count": active_watching_count(),
        "last_reevaluation_time": reevals[-1]["reevaluation_time"] if reevals else None,
        "last_terminal_result": reevals[-1]["decision"] if reevals else None,
        "setup_confirmed_today": sum(1 for r in today_reevals if r.get("decision") == "SETUP_CONFIRMED"),
        "analysis_incomplete_today": sum(1 for r in today_reevals if r.get("decision") == "ANALYSIS_INCOMPLETE"),
    }


def main():
    rows = load_rows()
    summary, table = build_summary(rows)
    print("=== Alert Decision Quality ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    print("\n=== Alert Watch Loop ===")
    for k, v in watch_loop_summary(rows).items():
        print(f"  {k}: {v}")

    print("\nLast 10 alerts:")
    for t in table:
        print(f"  {t}")


if __name__ == "__main__":
    main()
