"""
alert_manager.py -- READ-ONLY Monitoring & Alerting layer (2026-08-07,
Ahmed's request). Evaluates the SAME data status_dashboard.py's
refresh_loop() already fetches every REFRESH_SEC -- no new MT5 call, no
new SSH call, no new background loop of its own. Sends Telegram alerts on
new problems and automatic recovery notices when they clear, with
deduplication so a stuck condition doesn't spam.

Contains ZERO trading calls anywhere -- no order_send, no trading_stop, no
position modification, no bot restart, no config write. Pure read +
notify.

Telegram delivery note: no bot token lives on this machine, and none
should be copied here. Delivery works by SSHing the alert text to Oracle
(same key/host/pattern status_dashboard.py already uses to pull Oracle
data, and risk_guard_autorun.py already uses to push RISK_HALT state --
just another message over an already-proven channel) where tg_notify.py
sends it using the token already hardcoded there (Siko_Trading_Bot, the
same one liquidity_sweep_bot.py/tg_signal_bot.py/live_decay_watch.py
already use for their own notify() calls). Verified live 2026-08-08.
"""
import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(__file__)
STATE_FILE = os.path.join(BASE_DIR, "alert_state.json")
HISTORY_FILE = os.path.join(BASE_DIR, "alert_history.json")
HISTORY_MAX = 500  # cap so this never grows unbounded

# ---------------------------------------------------------------------------
# Configurable thresholds -- adjustable without touching any trading code.
# ---------------------------------------------------------------------------
DEDUP_SECONDS = 1800          # don't re-notify the same still-active alert more than once per 30 min
EQUITY_FLOOR = {"EA": 20.0, "EM": 0.20, "BA": 20.0, "Oracle": 20.0}  # per-account minimum equity, USD
DRAWDOWN_LIMIT_PCT = 15.0     # matches portfolio_risk_guard.py's own DEFAULT_THRESHOLD_PCT (reused, not re-invented)
HEARTBEAT_STALE_SEC = 300     # same 5-min convention used everywhere else in this project
EXPECTED_DORMANT_BOT_HEARTBEATS = {
    # EA leg was explicitly stopped; EM is the live leg and its heartbeat is the
    # truth signal that replaces the generic process-marker check.
    "orb_eth_exness": ("heartbeat_orb_eth_exness_EM.json",),
}
EXPECTED_STOPPED_ORACLE_SERVICES = {"liquidity_sweep_alive"}

# Bots expected to be running per the current Production baseline
# (2026-08-07). This is the one piece of "what SHOULD be running" data the
# read-only API doesn't expose on its own -- kept here as an explicit,
# documented, easily-updated list rather than guessed at render time.
EXPECTED_BOT_MARKERS = [
    "ea_shield", "ladder_guard.py", "fast_move_watch", "grind_watch",
    "fx_signal_exec", "gold_btc_bot", "participation_pilot_btc_range.py",
    "orb_eth_exness", "status_dashboard.py", "control_api.py",
    "claude_loop_heartbeat",
    # 2026-08-08 final audit: deployed as standing services for the first time
    "local_crypto_watch", "detection_miss_monitor",
    "hypergold_scalp_shadow",
    # "server.py" (MT5Bridge, London Breakout) removed 2026-08-16: that
    # strategy was stopped permanently 2026-08-15 (WR14%/PF0.131 live,
    # project_london_breakout_multi.md) -- this stale marker had been
    # firing a false "Bot down" alert every ~30min for ~42h straight since.
]

ORACLE_SSH_KEY = r"C:\Users\ahmed\Desktop\oracle_key"
ORACLE_HOST = "ubuntu@<REDACTED_ORACLE_IP>"
ORACLE_NOTIFY_CMD = "cd /home/ubuntu/trading-bot && venv/bin/python3 tg_notify.py"


def _relay_to_oracle(text):
    ssh_args = ["ssh", "-i", ORACLE_SSH_KEY, "-o", "StrictHostKeyChecking=no",
                "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", ORACLE_HOST,
                ORACLE_NOTIFY_CMD]
    # explicit UTF-8 (not text=True's locale default -- cp1252 on this
    # machine can't encode emoji/Arabic, which silently broke real alert
    # text; found via self-test 2026-08-08)
    result = subprocess.run(ssh_args, input=text.encode("utf-8"), capture_output=True, timeout=15)
    return result.stdout.decode("utf-8", errors="replace").strip() == "OK"


def telegram_notify(text):
    """Never raises, never blocks more than the timeout -- same safety
    contract as ntfy_alert.py's alert(). Relays through Oracle (see module
    docstring) since no bot token lives on this machine."""
    try:
        return _relay_to_oracle(text)
    except Exception:
        return False


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def _process_count(marker):
    """Read-only process count via wmic (same tool used elsewhere in this
    codebase for the exact same purpose -- status_dashboard.py's own
    running_bots(), TradingBot_Startup_Master.bat's :check_running)."""
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name like 'python%.exe'", "get", "CommandLine"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        ).stdout
        return out.count(marker)
    except Exception:
        return None  # unknown -- caller must not treat this as "zero"


def _recent_order_rejects(minutes=10):
    """Read-only query of the existing event store (events.db) -- same
    schema/table already used by the event-logging bots, zero new writer."""
    try:
        conn = sqlite3.connect(os.path.join(BASE_DIR, "events.db"))
        cur = conn.cursor()
        since = (datetime.now(timezone.utc).timestamp() - minutes * 60)
        since_iso = datetime.fromtimestamp(since, tz=timezone.utc).isoformat()
        cur.execute(
            "SELECT strategy, reason FROM events WHERE timestamp_utc >= ? "
            "AND event_type IN ('ORDER_FAILED','ERROR') ORDER BY timestamp_utc DESC LIMIT 5",
            (since_iso,),
        )
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def _recent_log_crash(log_filename, minutes=10):
    """Read-only tail-scan for Traceback/ImportError near the end of a
    bot's own log -- bounded read (last 200 lines), same safety pattern as
    portfolio_analytics.last_error()."""
    path = os.path.join(BASE_DIR, log_filename)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-200:]
        last_timestamped_line = max(
            (idx for idx, line in enumerate(lines) if re.match(r"^\[\d{4}-\d{2}-\d{2} ", line)),
            default=-1,
        )
        for line in reversed(lines):
            if re.search(r"Traceback|ImportError|ModuleNotFoundError", line):
                traceback_idx = next(idx for idx in range(len(lines) - 1, -1, -1) if lines[idx] == line)
                # If a newer timestamped entry exists after the traceback, the
                # crash is historical and the process has already moved on.
                if last_timestamped_line > traceback_idx:
                    return None
                return line.strip()[:200]
        return None
    except Exception:
        return None


def evaluate(accounts, bots, oracle_meta, oracle_conn):
    """Pure function: (current data) -> list of active alert dicts
    {key, severity, message}. No side effects, no I/O beyond the bounded
    read-only checks above -- easy to unit-test (see _demo())."""
    alerts = []

    # 1) Bot stopped (expected marker not found in the currently-detected list)
    bots_text = " ".join(bots)
    for marker in EXPECTED_BOT_MARKERS:
        if marker in EXPECTED_DORMANT_BOT_HEARTBEATS:
            hb_ok = False
            for hb_file in EXPECTED_DORMANT_BOT_HEARTBEATS[marker]:
                path = os.path.join(BASE_DIR, hb_file)
                if not os.path.exists(path):
                    continue
                try:
                    d = _load_json(path, {})
                    ts_key = next((k for k in d if "ts" in k.lower()), None)
                    ts = d.get(ts_key) if ts_key else None
                    if not ts:
                        continue
                    age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds()
                    if age <= HEARTBEAT_STALE_SEC:
                        hb_ok = True
                        break
                except Exception:
                    continue
            if hb_ok:
                continue
        if marker not in bots_text:
            alerts.append(dict(key=f"bot_down::{marker}", severity="critical",
                                message=f"Bot down or not detected: {marker}"))

    # 2) Duplicate process (more instances than the marker's own account-count logic expects
    #    for single-instance bots -- multi-instance bots like ea_shield/ladder_guard/gold_btc_bot
    #    already show their own count in the bots[] string, so only flag single-instance types).
    for marker in ["fast_move_watch", "grind_watch", "fx_signal_exec", "status_dashboard.py",
                   "control_api.py", "claude_loop_heartbeat", "participation_pilot_btc_range.py"]:
        count = _process_count(marker)
        if count is not None and count > 1:
            alerts.append(dict(key=f"duplicate::{marker}", severity="warning",
                                message=f"Duplicate process detected: {marker} ({count} instances)"))

    # 3) MT5 connection down per account
    for acc in accounts:
        if acc.get("error"):
            alerts.append(dict(key=f"mt5_down::{acc['name']}", severity="critical",
                                message=f"MT5 connection error on {acc['name']}: {acc['error']}"))

    # 4) Oracle/Bybit connection down
    if oracle_meta and (oracle_meta.get("consecutive_failures") or 0) > 2:
        alerts.append(dict(key="oracle_down", severity="critical",
                            message=f"Oracle/Bybit connection down ({oracle_meta['consecutive_failures']} consecutive failures)"))
    if oracle_meta:
        for svc in ("bybit_shield_alive", "ladder_guard_alive", "liquidity_sweep_alive"):
            if svc in EXPECTED_STOPPED_ORACLE_SERVICES:
                continue
            if oracle_meta.get(svc) is False:
                alerts.append(dict(key=f"oracle_service_down::{svc}", severity="critical",
                                    message=f"BAA service down: {svc.replace('_alive','')}"))

    # 5) Unprotected position
    for acc in accounts:
        for p in acc.get("positions", []):
            if p.get("protection") == "UNPROTECTED":
                alerts.append(dict(key=f"unprotected::{acc['name']}::{p.get('ticket')}", severity="critical",
                                    message=f"UNPROTECTED position: {acc['name']} {p['symbol']} ticket {p.get('ticket')}"))

    # 6) Order rejected due to an operational error (last 10 min, event store)
    for strategy, reason in _recent_order_rejects():
        alerts.append(dict(key=f"order_reject::{strategy}::{reason}"[:120], severity="critical",
                            message=f"Order rejected ({strategy}): {reason}"))

    # 7) Crash / Traceback / ImportError in active-bot logs (last ~200 lines)
    for log_file in ["gold_btc_bot_ea.log", "gold_btc_bot_em.log", "gold_btc_bot_ba.log",
                      "fx_signal_exec.log", "participation_pilot_btc_range.log", "orb_eth_exness.log"]:
        hit = _recent_log_crash(log_file)
        if hit:
            alerts.append(dict(key=f"crash::{log_file}", severity="critical",
                                message=f"Crash/Traceback in {log_file}: {hit}"))

    # 8) Heartbeat lost -- per-file threshold, not one flat number. Bots
    # with a longer own check-interval (gold_btc_bot: 900s) legitimately
    # go longer between heartbeat writes; flagging them at a generic 300s
    # would be a false alarm every single cycle. Same "3x own interval"
    # philosophy gold_btc_bot.py's own lock-staleness check already uses
    # (LOCK_STALE_MULTIPLIER), reused here for consistency rather than
    # invented fresh.
    HEARTBEAT_FILES = {
        "heartbeat_ea_shield_EA.json": HEARTBEAT_STALE_SEC, "heartbeat_ea_shield_EM.json": HEARTBEAT_STALE_SEC,
        "heartbeat_ea_shield_BA.json": HEARTBEAT_STALE_SEC,
        "heartbeat_ladder_guard_EA.json": HEARTBEAT_STALE_SEC, "heartbeat_ladder_guard_EM.json": HEARTBEAT_STALE_SEC,
        "heartbeat_ladder_guard_BA.json": HEARTBEAT_STALE_SEC,
        "heartbeat_gold_btc_bot_EA.json": 2700, "heartbeat_gold_btc_bot_EM.json": 2700,
        "heartbeat_gold_btc_bot_BA.json": 2700,  # 3x its own 900s check_interval_sec
        "heartbeat_fx_signal_exec.json": 600, "heartbeat_participation_pilot_btc_range.json": HEARTBEAT_STALE_SEC,
        "heartbeat_orb_eth_exness_EM.json": HEARTBEAT_STALE_SEC,
        "heartbeat_fast_move_watch.json": HEARTBEAT_STALE_SEC, "heartbeat_grind_watch.json": HEARTBEAT_STALE_SEC,
        # 2026-08-08 final audit: deployed as standing services for the first time
        "heartbeat_local_crypto_watch.json": HEARTBEAT_STALE_SEC,
        "heartbeat_detection_miss_monitor.json": HEARTBEAT_STALE_SEC,
    }
    for hb_file, stale_after in HEARTBEAT_FILES.items():
        path = os.path.join(BASE_DIR, hb_file)
        if not os.path.exists(path):
            continue
        try:
            d = _load_json(path, {})
            ts_key = next((k for k in d if "ts" in k.lower()), None)
            ts = d.get(ts_key) if ts_key else None
            if not ts:
                continue
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds()
            if age > stale_after:
                alerts.append(dict(key=f"heartbeat_lost::{hb_file}", severity="critical",
                                    message=f"Heartbeat lost: {hb_file} ({round(age)}s old, threshold {stale_after}s)"))
        except Exception:
            continue

    # 9) Portfolio drawdown -- reuses portfolio_risk_guard.py's OWN state file,
    #    does not recompute or duplicate its drawdown logic.
    risk_state = _load_json(os.path.join(BASE_DIR, "portfolio_risk_state.json"), None)
    if risk_state and risk_state.get("halted"):
        alerts.append(dict(key="portfolio_drawdown_halt", severity="critical",
                            message=f"Portfolio drawdown halt active (RISK_HALT.flag) -- baseline ${risk_state.get('baseline_equity')}"))

    # 10) Equity below floor per account
    for acc in accounts:
        if acc.get("error"):
            continue
        code = {"Bybit MT5": "BA", "E1 (Exness)": "EM", "E2 (Exness)": "EA",
                "Oracle (Bybit ccxt)": "Oracle"}.get(acc["name"])
        floor = EQUITY_FLOOR.get(code)
        if floor is not None and (acc.get("equity") or 0) < floor:
            alerts.append(dict(key=f"equity_floor::{code}", severity="warning",
                                message=f"{acc['name']} equity ${acc['equity']} below floor ${floor}"))

    return alerts


def process(current_alerts):
    """Diff current_alerts against persisted state: notify NEW actives
    (respecting DEDUP_SECONDS), notify RESOLVED (recovery), update state +
    append to history. Returns the updated state dict (also used by the
    /alerts page -- single source of truth, no second read path)."""
    state = _load_json(STATE_FILE, {})
    history = _load_json(HISTORY_FILE, [])
    now = time.time()
    now_iso = datetime.now(timezone.utc).isoformat()
    current_keys = {a["key"]: a for a in current_alerts}

    # new or still-active alerts
    for key, a in current_keys.items():
        existing = state.get(key)
        if existing is None or existing.get("status") != "active":
            state[key] = dict(status="active", severity=a["severity"], message=a["message"],
                               first_seen=now_iso, last_seen=now_iso, last_notified=now_iso, resolved_at=None)
            telegram_notify(f"🔴 ALERT [{a['severity'].upper()}] {a['message']}")
            history.append(dict(ts=now_iso, key=key, event="FIRED", message=a["message"]))
        else:
            existing["last_seen"] = now_iso
            existing["message"] = a["message"]
            if now - datetime.fromisoformat(existing["last_notified"]).timestamp() >= DEDUP_SECONDS:
                telegram_notify(f"🔴 STILL ACTIVE [{a['severity'].upper()}] {a['message']}")
                existing["last_notified"] = now_iso
                history.append(dict(ts=now_iso, key=key, event="REMINDER", message=a["message"]))

    # resolved alerts (were active, no longer present)
    for key, existing in list(state.items()):
        if existing.get("status") == "active" and key not in current_keys:
            existing["status"] = "resolved"
            existing["resolved_at"] = now_iso
            telegram_notify(f"✅ RECOVERED: {existing['message']}")
            history.append(dict(ts=now_iso, key=key, event="RESOLVED", message=existing["message"]))

    _save_json(STATE_FILE, state)
    _save_json(HISTORY_FILE, history[-HISTORY_MAX:])
    return state


def current_and_history():
    """Read-only accessor for the /alerts page -- no evaluation, just
    reads what the last process() call persisted."""
    state = _load_json(STATE_FILE, {})
    history = _load_json(HISTORY_FILE, [])
    active = {k: v for k, v in state.items() if v.get("status") == "active"}
    resolved = {k: v for k, v in state.items() if v.get("status") == "resolved"}
    return active, resolved, list(reversed(history[-100:]))


def _demo():
    """ponytail: exercises the full fire -> dedup -> recover cycle against
    a throwaway state/history file, zero real Telegram calls (TG_TOKEN
    unset in a clean test env -> telegram_notify() safely no-ops)."""
    global STATE_FILE, HISTORY_FILE
    import tempfile
    real_state, real_hist = STATE_FILE, HISTORY_FILE
    tmp = tempfile.mkdtemp()
    STATE_FILE = os.path.join(tmp, "state.json")
    HISTORY_FILE = os.path.join(tmp, "history.json")
    try:
        alerts_on = [dict(key="test_bot_down::demo", severity="critical", message="demo bot down")]
        s1 = process(alerts_on)
        assert s1["test_bot_down::demo"]["status"] == "active"

        # same alert again immediately -- must NOT re-notify (dedup), state unchanged in effect
        s2 = process(alerts_on)
        assert s2["test_bot_down::demo"]["last_notified"] == s1["test_bot_down::demo"]["last_notified"], \
            "dedup window must prevent an immediate re-notify"

        # condition clears -- must transition to resolved
        s3 = process([])
        assert s3["test_bot_down::demo"]["status"] == "resolved"
        assert s3["test_bot_down::demo"]["resolved_at"] is not None

        active, resolved, hist = current_and_history()
        assert len(active) == 0
        assert "test_bot_down::demo" in resolved
        kinds = [h["event"] for h in hist]
        assert "FIRED" in kinds and "RESOLVED" in kinds
        assert kinds.count("FIRED") == 1, "must not double-fire for the same still-active alert"

        # evaluate() itself against an all-healthy synthetic snapshot. Only
        # the checks fully determined by the function's OWN arguments can
        # be asserted zero here (mt5_down/oracle_down/oracle_service_down/
        # unprotected/equity_floor). heartbeat_lost/portfolio_drawdown_halt/
        # crash::/order_reject::/duplicate::/bot_down:: all read REAL
        # ambient files this test doesn't control (live heartbeats, the
        # real portfolio_risk_state.json, real running processes) -- that's
        # correct integration behavior, not something a unit test should
        # fight by asserting the live system into a particular shape.
        healthy_bots = [m + " running" for m in EXPECTED_BOT_MARKERS]
        healthy_accounts = [{"name": "Bybit MT5", "error": None, "equity": 100, "positions": []},
                             {"name": "E1 (Exness)", "error": None, "equity": 100, "positions": []},
                             {"name": "E2 (Exness)", "error": None, "equity": 100, "positions": []}]
        healthy_oracle_meta = {"consecutive_failures": 0, "bybit_shield_alive": True,
                                "ladder_guard_alive": True, "liquidity_sweep_alive": True}
        result = evaluate(healthy_accounts, healthy_bots, healthy_oracle_meta, {})
        arg_determined_prefixes = ("mt5_down::", "oracle_down", "oracle_service_down::", "unprotected::", "equity_floor::")
        arg_determined_alerts = [a for a in result if a["key"].startswith(arg_determined_prefixes)]
        assert arg_determined_alerts == [], \
            f"expected zero argument-determined alerts on a healthy synthetic snapshot, got {arg_determined_alerts}"

        # unhealthy synthetic snapshot -> each argument-determined check must fire
        bad_accounts = [{"name": "Bybit MT5", "error": "MT5 init failed", "equity": 0, "positions": []},
                         {"name": "E2 (Exness)", "error": None, "equity": 1.0,
                          "positions": [{"protection": "UNPROTECTED", "symbol": "XAUUSDm", "ticket": 1}]}]
        bad_oracle_meta = {"consecutive_failures": 5, "bybit_shield_alive": False,
                            "ladder_guard_alive": True, "liquidity_sweep_alive": True}
        bad_result = evaluate(bad_accounts, [], bad_oracle_meta, {})
        bad_keys = {a["key"].split("::")[0] for a in bad_result}
        for expected in ("mt5_down", "oracle_down", "oracle_service_down", "unprotected", "equity_floor"):
            assert expected in bad_keys, f"expected a '{expected}' alert on the unhealthy snapshot, got keys {bad_keys}"
        print("alert_manager self-check OK")
    finally:
        STATE_FILE, HISTORY_FILE = real_state, real_hist


if __name__ == "__main__":
    _demo()
