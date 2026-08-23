"""
Status Dashboard — READ-ONLY position-monitoring screen. Serves account
balances/positions/protection state/ladder status on port 5001 for the
existing ngrok tunnel. Contains no order_send/create_order/trading_stop
call anywhere -- reads current state and derives display-only labels from
it. Purpose is monitoring, not encouraging premature exits: floating P/L
is shown small, protection/R/ladder state is shown first and biggest.

2026-07-26 rebuild (Ahmed's spec): adds real protection status (from the
actual current SL/TP fields, never inferred), R-multiple / ladder-stage
display for BAA positions (reusing ladder_guard_bybit's own persisted
state -- see dashboard_snapshot.py docstring for why that file is the
source of truth instead of recomputing R here), target-based progress for
MT5 positions with a real TP (using ladder_guard.py's own STEPS/
RUNNER_TRIGGER constants via direct import, zero duplication), and
explicit UNMANAGED/UNKNOWN states instead of ever inventing a TP or an R
reference this file didn't actually observe.
"""
from flask import Flask, jsonify, make_response, render_template_string, request
import MetaTrader5 as mt5
import subprocess
import json
import os
import re
import sys
import time
import threading
from datetime import datetime, timedelta, timezone
import urllib.request
import urllib.error
import urllib.parse

# Constants imported directly from the live execution bot -- this file must
# never hold its own copy of a threshold that could drift from what's
# actually managing trades. Importing has no side effects (mt5.initialize()
# only runs inside ladder_guard.main(), guarded by __main__).
DEPENDENCY_ERRORS = {}


def _dependency_failed(name, error):
    DEPENDENCY_ERRORS[name] = f"{type(error).__name__}: {error}"[:240]


try:
    from ladder_guard import (STEPS_BY_CLASS as MT5_STEPS_BY_CLASS,
                               classify_asset as mt5_classify_asset,
                               RUNNER_TRIGGER as MT5_RUNNER_TRIGGER,
                               ACCOUNTS as LIVE_MT5_ACCOUNTS)
except (ImportError, SyntaxError) as e:
    _dependency_failed("ladder_guard", e)
    MT5_STEPS_BY_CLASS = {}
    MT5_RUNNER_TRIGGER = None
    LIVE_MT5_ACCOUNTS = {}
    def mt5_classify_asset(_symbol):
        return "UNKNOWN"
try:
    from bot_period_guard import manual_block_reason as period_manual_block_reason
except (ImportError, SyntaxError) as e:
    _dependency_failed("bot_period_guard", e)
    def period_manual_block_reason(_bot, _acc):
        return "ERROR: bot_period_guard unavailable"

# 2026-08-07 (Ahmed's Portfolio Analytics/Risk/Reports request): read-only
# historical-performance module, zero shared runtime state with any live
# bot or trading logic -- see its own docstring for exactly what it does
# and does not have access to.
try:
    import portfolio_analytics as pa
except (ImportError, SyntaxError) as e:
    _dependency_failed("portfolio_analytics", e)
    class _PortfolioAnalyticsUnavailable:
        NOT_AVAILABLE = "N/A"
        ACTIVE_MAGICS = set()
        @staticmethod
        def last_heartbeat(_name):
            return None
        @staticmethod
        def gold_btc_bot_eligibility(_acc, _equity, _risk_halted):
            return None
        @staticmethod
        def risk_halt_status():
            return {"halted": None, "status": "ERROR", "error": "portfolio_analytics unavailable"}
        @staticmethod
        def risk_halt_detail(_accounts):
            return {"active": None, "status": "ERROR", "error": "portfolio_analytics unavailable"}
        @staticmethod
        def group_by_magic(_trades):
            return {}
        @staticmethod
        def no_trade_status(_by_magic):
            return [{"bot": "portfolio_analytics", "status": "EXECUTION_ERROR",
                     "reason": "dependency unavailable", "last_trade_time": None,
                     "time_since_last_trade_sec": None, "last_heartbeat_age_sec": None}]
        @staticmethod
        def asset_exposure_summary(_accounts):
            return {}
        @staticmethod
        def oracle_attribution_summary():
            return {"available": False, "reason": "portfolio_analytics unavailable"}
        @staticmethod
        def trade_source(_magic):
            return "UNKNOWN"
        @staticmethod
        def strategy_attribution(_magic, _comment=""):
            return "UNKNOWN"
        @staticmethod
        def fetch_all_closed_trades(days=7):
            return [], []
    pa = _PortfolioAnalyticsUnavailable()
try:
    import alert_manager as am
except (ImportError, SyntaxError) as e:
    _dependency_failed("alert_manager", e)
    class _AlertManagerUnavailable:
        DEDUP_SECONDS = 0
        @staticmethod
        def evaluate(*_args, **_kwargs):
            raise RuntimeError("alert_manager unavailable")
        @staticmethod
        def process(_alerts):
            return None
        @staticmethod
        def current_and_history():
            return ({"dependency": "ERROR"}, {}, [{"error": "alert_manager unavailable"}])
    am = _AlertManagerUnavailable()
try:
    import autonomy_challenge as achall
except (ImportError, SyntaxError) as e:
    _dependency_failed("autonomy_challenge", e)
    class _AutonomyChallengeUnavailable:
        @staticmethod
        def read_state():
            return None
        @staticmethod
        def record_snapshot(*_args, **_kwargs):
            raise RuntimeError("autonomy_challenge unavailable")
    achall = _AutonomyChallengeUnavailable()


def _pa_call(method, default, *args):
    try:
        return getattr(pa, method)(*args)
    except Exception as e:
        _dependency_failed("portfolio_analytics.runtime", e)
        return default

app = Flask(__name__)

REFRESH_SEC = 8
# 2026-07-26: Oracle/BAA data requires an SSH round-trip; polling it every
# 8s (same as the local/cheap MT5 fetches) meant a new SSH connection attempt
# roughly every 8-33s from this process alone, continuously -- on top of
# tripwire_explainer's own periodic pulls and any manual/diagnostic SSH use.
# Ahmed noticed SSH instability got noticeably more frequent right around
# when this dashboard went live; this cadence is the most likely reason
# (sshd's MaxStartups can start dropping/delaying connections under load).
# Fixed with adaptive backoff instead of a flat interval: normal cadence
# while healthy, escalating pauses on repeated failure so a genuinely down
# link doesn't keep hammering sshd, and an immediate reset back to normal
# the moment a fetch succeeds again.
ORACLE_NORMAL_INTERVAL = 30   # seconds, cadence while healthy (0 consecutive failures)
ORACLE_BACKOFF_STEPS = [30, 60, 120, 300]  # seconds, escalates per consecutive failure, caps at 300

# Always the same shape (every key present, None when unknown) -- a plain {}
# here caused a live 500 on every page load while Oracle was unreachable
# (Jinja2's `x.y is not none` doesn't catch a missing key: Undefined is not
# None either, so the guard passed and the `%.0f` format filter then crashed
# on the Undefined value). Every code path that can't reach Oracle now
# returns dict(EMPTY_ORACLE_META) instead of {}.
EMPTY_ORACLE_META = {
    "ladder_state_age_sec": None, "fetched_ts": None,
    "bybit_shield_alive": None, "ladder_guard_alive": None,
    "tg_signal_bot_alive": None, "liquidity_sweep_alive": None,
    "config_error": None,
}

_cache = {"accounts": [], "bots": [], "ts": 0, "oracle_meta": dict(EMPTY_ORACLE_META)}
_oracle_cache = {"data": None, "meta": dict(EMPTY_ORACLE_META), "ts": 0, "consecutive_failures": 0,
                  "last_success_ts": 0, "next_retry_ts": 0}
_cache_lock = threading.Lock()


def _oracle_interval():
    n = _oracle_cache["consecutive_failures"]
    if n <= 0:
        return ORACLE_NORMAL_INTERVAL
    return ORACLE_BACKOFF_STEPS[min(n - 1, len(ORACLE_BACKOFF_STEPS) - 1)]


def _read_json_file(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _pid_alive(pid, rows=None):
    if not pid:
        return False
    rows = rows if rows is not None else _wmic_pid_map()
    return any(proc_pid == pid for _, proc_pid in rows)


def _manual_entry_block_rows():
    rows = []
    targets = (
        ("fx_signal_exec", ("EA", "BA", "EM")),
        ("London Breakout", ("EA", "BA")),
        ("gold_btc_bot", ("EA", "BA", "EM")),
        ("orb_eth_exness", ("EA", "EM")),
    )
    for bot, accounts in targets:
        for acc in accounts:
            reason = _manual_block_reason_for(bot, acc)
            if reason:
                rows.append({
                    "bot": bot,
                    "account": acc,
                    "reason": reason,
                    "status": "BLOCKED (MANUAL)",
                })
    return rows


def _manual_block_reason_for(bot, acc):
    if bot == "fx_signal_exec":
        for block_acc in ("EA", "BA", "EM"):
            reason = period_manual_block_reason("fx_signal_exec", block_acc)
            if reason:
                return reason
        return None
    if bot == "London Breakout":
        return period_manual_block_reason("London Breakout", acc) if acc else None
    if bot in ("gold_btc_bot", "orb_eth_exness"):
        return period_manual_block_reason(bot, acc) if acc else None
    return None


def _control_equity_by_code(accounts):
    out = {}
    for a in accounts:
        if a.get("error"):
            continue
        code = CONTROL_ACCOUNT_CODES.get(a.get("name"), a.get("name"))
        if code in ("EA", "EM", "BA"):
            out[code] = a.get("equity")
    return out

ORACLE_CONFIG_PATH = os.environ.get(
    "STATUS_DASHBOARD_ORACLE_CONFIG",
    os.path.join(os.path.dirname(__file__), "status_dashboard.local.json"),
)


def _redacted(value):
    return isinstance(value, str) and "<REDACTED_" in value


def _oracle_config():
    cfg = {
        "host": os.environ.get("STATUS_DASHBOARD_ORACLE_HOST"),
        "ssh_key": os.environ.get("STATUS_DASHBOARD_ORACLE_SSH_KEY"),
    }
    if os.path.exists(ORACLE_CONFIG_PATH):
        with open(ORACLE_CONFIG_PATH, "r", encoding="utf-8") as f:
            local = json.load(f)
        for key in ("host", "ssh_key"):
            if not cfg.get(key) or _redacted(cfg.get(key)):
                cfg[key] = local.get(key)

    missing = [k for k in ("host", "ssh_key") if not cfg.get(k)]
    redacted = [k for k in ("host", "ssh_key") if _redacted(cfg.get(k))]
    if missing or redacted:
        bad = ", ".join(missing + redacted)
        raise RuntimeError(f"Oracle config missing/redacted: {bad}")
    return cfg


def deployment_preflight():
    _oracle_config()
    return True

def _account_config(code, path):
    """Use the existing live credential source; never store sanitized credentials here."""
    if code in LIVE_MT5_ACCOUNTS:
        return dict(LIVE_MT5_ACCOUNTS[code])
    return {"path": path, "dependency_error": "ladder_guard account config unavailable"}


ACCOUNTS = {
    "Bybit MT5": _account_config("BA", r"C:\Program Files\MetaTrader 5\terminal64.exe"),
    "E1 (Exness)": _account_config("EM", r"C:\MT5_Portable_3\terminal64.exe"),
    "E2 (Exness)": _account_config("EA", r"C:\MT5_Portable_2\terminal64.exe"),
}

# 2026-07-28 (Ahmed's UI-only Control Layer request): maps this dashboard's
# account display names to control_api.py's short account codes. The
# Oracle/Bybit-ccxt account has no entry here on purpose -- it's out of
# scope for the Control Layer in this phase (MT5-only), so no Close/
# Breakeven/Close-All UI is ever rendered for it. Hardcoded per Ahmed's
# spec rather than derived, so this mapping can never silently drift.
CONTROL_ACCOUNT_CODES = {"Bybit MT5": "BA", "E1 (Exness)": "EM", "E2 (Exness)": "EA"}

MAGIC_NAMES = {
    0: "Manual", 990099: "London Breakout", 990101: "London Breakout",
    993399: "EMA-ADX", 993400: "EMA-ADX",
    884400: "fx_signal_exec", 880088: "IFVGBridge",
    992200: "ORB ETH (Exness)", 992201: "ORB ETH (Exness)",
    770005: "SAP-v3.1", 771010: "GLB", 771020: "ICT_FVG",
    991199: "TrendRider FX8", 778802: "SB FX8 Elite",
    995500: "swing_pending_bot", 995501: "participation_pilot_btc (OLD, stopped)",
    995502: "participation_pilot_btc_range",
}


def magic_name(m):
    return MAGIC_NAMES.get(m, f"magic {m}")


BOT_MARKERS = ["ema_adx_bot", "server.py", "ea_shield", "ladder_guard.py", "orb_eth_exness",
               "fx_signal_exec", "fast_move_watch", "grind_watch",
               # 2026-08-08 final audit: "signal_watch_bot"/"local_llm_digest"/
               # "tripwire_explainer" removed -- none of the three files exist
               # anywhere in the codebase (dead markers, never matched anything).
               "local_crypto_watch",
               "detection_miss_monitor", "claude_loop_heartbeat",
               # 2026-08-05: found missing during a proactive monitoring-coverage
               # sweep -- both confirmed running via direct process list, but
               # invisible to this dashboard until now (pure observability gap,
               # zero strategy/execution change).
               "gold_btc_bot",
               "hypergold_scalp_shadow",
               # 2026-08-07: "participation_pilot_btc" (no suffix) as a single
               # marker was a substring of BOTH participation_pilot_btc.py (the
               # old, ungated bot -- must stay stopped, double-execution risk)
               # and participation_pilot_btc_range.py (the approved live pilot),
               # so this dashboard could never tell them apart -- found during a
               # full SPOF audit while confirming the old bot was truly stopped.
               # Split into two specific, non-overlapping markers.
               "participation_pilot_btc.py", "participation_pilot_btc_range.py",
               # 2026-08-07: alert_manager.py's bot_down check expected these
               # two to be trackable here and always fired false "down" alerts
               # for both, since neither was ever a marker (pure observability
               # gap, same class as the gold_btc_bot one above).
               "control_api.py", "status_dashboard.py",
               # 2026-08-18: add the read-only regime/outcome monitors so
               # dashboard liveness matches the startup/health-check set.
               "regime_detector.py", "outcome_calibration_log.py"]


# ---------------------------------------------------------------------------
# Pure, read-only display helpers. None of these ever call an exchange/MT5
# write function; they only turn already-fetched real data into labels.
# ---------------------------------------------------------------------------

def freshness_state(age_sec):
    if age_sec is None:
        return "UNKNOWN"
    if age_sec <= 10:
        return "LIVE"
    if age_sec <= 30:
        return "DELAYED"
    if age_sec <= 60:
        return "STALE WARNING"
    return "DATA STALE"


CLAUDE_LOOP_STATUS_FILE = os.path.join(os.path.dirname(__file__), "claude_loop_status.json")
CLAUDE_LOOP_NOTIFY_SECONDS = 900  # must match claude_loop_heartbeat.py's NOTIFY_SECONDS


def _fmt_ago(age_sec):
    if age_sec is None:
        return "never"
    age_sec = int(age_sec)
    if age_sec < 60:
        return f"{age_sec}s ago"
    return f"{age_sec // 60}m {age_sec % 60}s ago"


def _fmt_countdown(seconds_left):
    if seconds_left is None:
        return "unknown"
    if seconds_left <= 0:
        return "overdue"
    seconds_left = int(seconds_left)
    return f"{seconds_left // 60}m {seconds_left % 60}s"


def claude_loop_status():
    """Three genuinely independent liveness signals -- Ahmed, 2026-07-27:
    'Heartbeat alive != Claude analysis alive != Market analysis completed'.
    Conflating them into one green light hides exactly the failure modes
    that matter (pulse fires but nobody's listening; Claude wakes but the
    cycle hangs/errors before finishing). Each row here is written by a
    different, independent process:
      - heartbeat_ts, next_tick_ts:            claude_loop_heartbeat.py (OS pulse)
      - claude_awake_ts:                       Claude's turn, via
                                                `python claude_loop_report.py awake`
                                                at the START of a woken cycle
      - analysis_completed_ts/last_cycle_status: same script's `done`/`error`
                                                action at the END of a cycle
    A row can only go green from the process that's actually responsible for
    that fact -- this file makes it structurally impossible for "the timer
    exists" to be misread as "the analysis is happening"."""
    data = {}
    try:
        if os.path.exists(CLAUDE_LOOP_STATUS_FILE):
            with open(CLAUDE_LOOP_STATUS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
    except Exception:
        pass

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    def age_of(key):
        ts_str = data.get(key)
        if not ts_str:
            return None
        try:
            return (now - datetime.fromisoformat(ts_str)).total_seconds()
        except Exception:
            return None

    hb_age = age_of("heartbeat_ts")
    awake_age = age_of("claude_awake_ts")
    done_age = age_of("analysis_completed_ts")

    seconds_left = None
    next_tick_str = data.get("next_tick_ts")
    if next_tick_str:
        try:
            seconds_left = (datetime.fromisoformat(next_tick_str) - now).total_seconds()
        except Exception:
            pass

    heartbeat_ok = hb_age is not None and hb_age <= 150
    # Generous window (2 cycles) since awake/done only update once per
    # NOTIFY_SECONDS -- this is "hasn't Claude missed its last two expected
    # wakeups", not "did it wake up in the last minute".
    stale_window = CLAUDE_LOOP_NOTIFY_SECONDS * 2
    awake_ok = awake_age is not None and awake_age <= stale_window
    # A cycle only counts as genuinely completed if analysis_completed_ts is
    # not older than the most recent claude_awake_ts -- otherwise the last
    # wake never finished (still running, or died mid-cycle).
    done_ok = (
        done_age is not None and done_age <= stale_window
        and (awake_age is None or done_age <= awake_age + 1)
    )

    return {
        "heartbeat": {"ok": heartbeat_ok, "text": _fmt_ago(hb_age)},
        "claude_awake": {"ok": awake_ok, "text": _fmt_ago(awake_age)},
        "analysis": {
            "ok": done_ok,
            "text": (f"completed {_fmt_ago(done_age)}" if done_ok
                     else ("in progress" if awake_ok and not done_ok else _fmt_ago(done_age))),
            "status": data.get("last_cycle_status"),
        },
        "next_tick": {"ok": seconds_left is None or seconds_left > -60, "text": _fmt_countdown(seconds_left)},
    }


def ml_research_status():
    """READ-ONLY. 2026-08-08 (Ahmed's ML Readiness Monitor + Dashboard
    Integration request). Calls into research_ml/readiness_monitor.py --
    a standalone, offline module that only reads
    research_ml/dataset/*.csv (approved baseline + append-only file) and
    never touches MT5/orders/any Bot_Active file. This function itself
    never writes anything and has no code path back into research_ml
    beyond the single read-only get_status() call -- there is nothing
    here (or in research_ml) that can open/close/modify a trade, change
    SL/TP/lot, disable a strategy, or touch RISK_HALT."""
    try:
        research_ml_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "research_ml")
        if research_ml_dir not in sys.path:
            sys.path.insert(0, research_ml_dir)
        import readiness_monitor
        status = readiness_monitor.get_status()
        status["available"] = True
        return status
    except Exception as e:
        return {"available": False, "error": str(e)[:200]}


def autonomous_research_loop_status():
    """READ-ONLY. 2026-08-08 (Ahmed's Dashboard Research Card request).
    Reads only JSON/JSONL files already written by
    research_ml/autonomous_research_loop/ -- no import of that package's
    code (unlike ml_research_status() above, which does import
    readiness_monitor -- this is plain file I/O, an even narrower
    surface), no execution of anything, no way to start a cycle, change
    a registry, or touch RISK_HALT/Bot_Active/MT5 from this function. A
    missing file just means the loop hasn't run a cycle yet (the normal
    DORMANT state), not an error."""
    try:
        research_ml_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "research_ml")
        loop_dir = os.path.join(research_ml_dir, "autonomous_research_loop")

        def _read_json(path, default=None):
            if not os.path.exists(path):
                return default
            with open(path, encoding="utf-8") as f:
                return json.load(f)

        def _read_jsonl_last(path):
            if not os.path.exists(path):
                return None
            last = None
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        last = json.loads(line)
            return last

        def _count_jsonl_matching(path, event_type):
            if not os.path.exists(path):
                return 0
            n = 0
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and json.loads(line).get("event_type") == event_type:
                        n += 1
            return n

        loop_state = _read_json(os.path.join(loop_dir, "loop_state.json"), {"state": "STOPPED"})
        heartbeat = _read_json(os.path.join(loop_dir, "heartbeat_autonomous_research_loop.json")) or {}
        families = _read_json(os.path.join(research_ml_dir, "research_family_registry.json"), [])
        protocols = _read_json(os.path.join(research_ml_dir, "protocol_registry.json"), [])
        last_experiment = _read_jsonl_last(os.path.join(loop_dir, "experiment_registry.jsonl")) or {}
        protocol_errors = _count_jsonl_matching(os.path.join(loop_dir, "research_events.jsonl"), "protocol_integrity_failure")

        return {
            "available": True,
            "status": loop_state.get("state", "STOPPED"),
            "active_families": sum(1 for f in families if f.get("status") == "ACTIVE"),
            "total_families": len(families),
            "registered_protocols": len(protocols),
            "last_run": heartbeat.get("last_success_ts"),
            "last_cycle_outcome": heartbeat.get("last_cycle_outcome"),
            "last_experiment_id": last_experiment.get("experiment_id"),
            "last_experiment_decision": last_experiment.get("decision"),
            "protocol_errors": protocol_errors,
        }
    except Exception as e:
        return {"available": False, "error": str(e)[:200]}


def protection_status(sl, tp_required, tp):
    """tp_required: whether this position's strategy is expected to carry a
    real TP (e.g. liquidity_sweep). For manual/R-based positions TP is never
    expected, so its absence doesn't count against protection status."""
    has_sl = bool(sl)
    has_tp = bool(tp)
    if tp_required:
        if has_sl and has_tp:
            return "PROTECTED"
        if has_sl or has_tp:
            return "PARTIAL PROTECTION"
        return "UNPROTECTED"
    return "PROTECTED" if has_sl else "UNPROTECTED"


def _stage_label(lock_value):
    if lock_value is None:
        return "RUNNER"
    if abs(lock_value) < 1e-9:
        return "BREAKEVEN ARMED"
    if lock_value < 0:
        return "RISK REDUCED"
    return "PROFIT LOCK"


def ladder_progress_display(progress, steps, runner_trigger, unit="R"):
    """steps: list of (threshold, lock) as actually used by the live bot
    (any order -- sorted here for display only, values never altered)."""
    steps_asc = sorted(steps, key=lambda t: t[0])
    if progress is None or runner_trigger is None:
        return {"stage": "UNKNOWN", "stage_note": None, "next_label": None,
                "next_threshold": None, "distance_to_next": None}
    if progress >= runner_trigger:
        return {"stage": "RUNNER", "stage_note": f"+{runner_trigger:.2f}{unit} reached",
                "next_label": "RUNNER ACTIVE", "next_threshold": None, "distance_to_next": None}
    stage, stage_note = "WAITING", None
    next_threshold, next_label = (steps_asc[0][0] if steps_asc else runner_trigger), None
    for thr, lock in steps_asc:
        if progress >= thr:
            stage, stage_note = _stage_label(lock), f"+{thr:.2f}{unit} reached"
        else:
            next_threshold, next_label = thr, _stage_label(lock)
            break
    else:
        next_threshold, next_label = runner_trigger, "RUNNER"
    return {"stage": stage, "stage_note": stage_note, "next_label": next_label,
            "next_threshold": next_threshold,
            "distance_to_next": round(next_threshold - progress, 4) if next_threshold is not None else None}


def normalize_mt5_position(p, tag):
    buy = p.type == 0
    sl, tp = p.sl or None, p.tp or None
    strategy = magic_name(p.magic)
    is_target_strategy = p.magic != 0  # non-manual = a system that's supposed to carry a real TP
    pos = {
        "account": tag, "symbol": p.symbol, "side": "BUY" if buy else "SELL",
        "volume": p.volume, "entry": p.price_open, "current": p.price_current,
        "profit": round(p.profit, 2), "sl": sl, "tp": tp,
        "sl_status": "CONFIRMED" if sl else "MISSING",
        "tp_status": "CONFIRMED" if tp else "MISSING",
        "strategy": strategy, "ticket": p.ticket,
        "protection": protection_status(sl, is_target_strategy, tp),
    }
    if tp and sl:
        dist = (tp - p.price_open) if buy else (p.price_open - tp)
        progress = ((p.price_current - p.price_open) if buy else (p.price_open - p.price_current)) / dist if dist > 0 else None
        pos["management"] = "target"
        pos["target"] = {"tp": tp, "distance": round(dist, 5) if dist else None,
                          "progress": round(progress, 4) if progress is not None else None}
        steps_for_symbol = MT5_STEPS_BY_CLASS.get(mt5_classify_asset(p.symbol), [])
        pos["ladder"] = ladder_progress_display(progress, steps_for_symbol, MT5_RUNNER_TRIGGER, unit="")
    elif sl and not tp:
        # No persisted original-R state exists for MT5 yet (queued follow-up,
        # see project_mt5_ladder_no_tp_fallback_queued) -- inventing one here
        # would violate "never invent a risk value", so this is shown honestly.
        pos["management"] = "unmanaged"
        pos["r"] = None
        pos["ladder"] = {"stage": "UNMANAGED", "stage_note": None, "next_label": None,
                          "next_threshold": None, "distance_to_next": None}
    else:
        pos["management"] = "unmanaged"
        pos["r"] = None
        pos["ladder"] = {"stage": "UNMANAGED", "stage_note": None, "next_label": None,
                          "next_threshold": None, "distance_to_next": None}
    return pos


def normalize_baa_position(p, ladder_constants, liqsweep_open_since_ms=None):
    sl, tp = p.get("sl"), p.get("tp")
    ladder = p.get("ladder")  # from ladder_guard_bybit's own persisted state, or None
    # Attribution uses liquidity_sweep_bot's OWN state (open_since_ms set only
    # while it believes it holds a position), not a symbol/side guess -- a
    # guess would mislabel Ahmed's own manual BTC shorts as the bot's and
    # then falsely accuse them of a "missing TP" they were never meant to have.
    is_likely_liqsweep = (p["symbol"] == "BTC/USDT:USDT" and p["side"] == "short"
                           and liqsweep_open_since_ms is not None)
    strategy = "LIQUIDITY_SWEEP" if is_likely_liqsweep else "Manual/Other"
    is_target_strategy = is_likely_liqsweep
    pos = {
        "account": "Oracle / Bybit CCXT", "symbol": p["symbol"],
        "side": p["side"].upper(), "volume": p["volume"], "entry": p["entry"],
        "current": p["mark"], "profit": round(p["profit"], 4) if p.get("profit") is not None else None,
        "sl": sl, "tp": tp,
        "sl_status": "CONFIRMED" if sl else "MISSING",
        "tp_status": "CONFIRMED" if tp else "MISSING",
        "strategy": strategy,
        "protection": protection_status(sl, is_target_strategy, tp),
    }
    if ladder is None:
        # ladder_guard_bybit hasn't recorded this symbol -- could be a timing
        # gap (just opened) or the state file itself being stale; either way
        # we don't know enough to call it UNMANAGED vs ACTIVE, so say so.
        pos["management"] = "unknown"
        pos["r"] = None
        pos["ladder"] = {"stage": "UNKNOWN", "stage_note": None, "next_label": None,
                          "next_threshold": None, "distance_to_next": None}
        return pos

    pos["management"] = ladder["management"]
    if ladder["management"] == "no_target" and ladder.get("original_r"):
        R = ladder["original_r"]
        buy = p["side"] == "long"
        gain_R = (((p["mark"] - p["entry"]) if buy else (p["entry"] - p["mark"])) / R) if p.get("mark") is not None else None
        r_steps = (ladder_constants or {}).get("r_steps")
        runner_trigger_r = (ladder_constants or {}).get("runner_trigger_r")
        pos["r"] = {"original_r": R, "current_gain_r": round(gain_R, 4) if gain_R is not None else None,
                     "is_runner": ladder.get("is_runner", False)}
        if ladder.get("is_runner"):
            pos["ladder"] = {"stage": "RUNNER", "stage_note": "trailing active", "next_label": "RUNNER ACTIVE",
                              "next_threshold": None, "distance_to_next": None}
        elif r_steps and runner_trigger_r is not None and gain_R is not None:
            pos["ladder"] = ladder_progress_display(gain_R, r_steps, runner_trigger_r, unit="R")
        else:
            pos["ladder"] = {"stage": "UNKNOWN", "stage_note": None, "next_label": None,
                              "next_threshold": None, "distance_to_next": None}
    elif ladder["management"] == "target" and tp:
        buy = p["side"] == "long"
        dist = (tp - p["entry"]) if buy else (p["entry"] - tp)
        progress = (((p["mark"] - p["entry"]) if buy else (p["entry"] - p["mark"])) / dist) if dist and p.get("mark") is not None else None
        pos["target"] = {"tp": tp, "distance": round(dist, 5) if dist else None,
                          "progress": round(progress, 4) if progress is not None else None}
        steps = (ladder_constants or {}).get("steps")
        runner_trigger = (ladder_constants or {}).get("runner_trigger")
        if ladder.get("is_runner"):
            pos["ladder"] = {"stage": "RUNNER", "stage_note": "trailing active (TP removed)", "next_label": "RUNNER ACTIVE",
                              "next_threshold": None, "distance_to_next": None}
        elif steps and runner_trigger is not None and progress is not None:
            pos["ladder"] = ladder_progress_display(progress, steps, runner_trigger, unit="")
        else:
            pos["ladder"] = {"stage": "UNKNOWN", "stage_note": None, "next_label": None,
                              "next_threshold": None, "distance_to_next": None}
    else:
        pos["r"] = None
        pos["ladder"] = {"stage": "UNMANAGED", "stage_note": None, "next_label": None,
                          "next_threshold": None, "distance_to_next": None}
    return pos


def oracle_connection_status(oracle_meta, now=None):
    now = now if now is not None else time.time()
    n = (oracle_meta or {}).get("consecutive_failures") or 0
    if n == 0:
        emoji, label = "🟢", "Connected"
    elif n <= 2:
        emoji, label = "🟡", "Connection degraded"
    else:
        emoji, label = "🔴", "Connection unreachable"
    last_success = (oracle_meta or {}).get("last_success_ts")
    next_retry = (oracle_meta or {}).get("next_retry_ts")
    return {
        "emoji": emoji, "label": label,
        "last_success_ago": round(now - last_success) if last_success else None,
        "next_retry_in": max(0, round(next_retry - now)) if next_retry else None,
    }


STARTUP_VERSION_CONFLICT_FILE = os.path.join(os.path.dirname(__file__), "startup_version_conflict.json")


def startup_version_conflict():
    """Surfaces startup_version_guard.ps1's finding -- see TradingBot_Startup_Master.bat
    section 0 -- so a Startup-folder copy drifting from the canonical one (the
    2026-08-06 swing_pending_bot incident) shows up here instead of only in a
    log file nobody's tailing."""
    try:
        if os.path.exists(STARTUP_VERSION_CONFLICT_FILE):
            with open(STARTUP_VERSION_CONFLICT_FILE, "r", encoding="utf-8-sig") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def build_alerts(all_positions, oracle_meta, accounts):
    alerts = []
    conflict = startup_version_conflict()
    if conflict.get("conflict"):
        alerts.append(f"⚠ Startup-folder copy of TradingBot_Startup_Master.bat differs from the canonical "
                       f"Bot_Active copy (checked {conflict.get('checked_ts', '?')}) -- sync it")
    for p in all_positions:
        if p["sl_status"] == "MISSING":
            alerts.append(f"⚠ SL MISSING — {p['account']} {p['symbol']} ({p['side']})")
        if p.get("strategy") in ("LIQUIDITY_SWEEP",) and p["tp_status"] == "MISSING":
            alerts.append(f"⚠ TP unexpectedly missing for LIQUIDITY_SWEEP — {p['symbol']}")
        # 2026-08-10 (Ahmed): Oracle/Bybit non-ladder-managed positions (swing_pending_bybit /
        # unattributed "Manual/Other") set their own SL and are not ladder_guard-managed by design,
        # so a CONFIRMED exchange SL there is expected, NOT a fault -- don't raise this warning.
        _oracle_non_ladder = (p.get("account") == "Oracle / Bybit CCXT"
                              and p.get("strategy") == "Manual/Other")
        if p["ladder"]["stage"] == "UNMANAGED" and p["sl_status"] == "CONFIRMED" and not _oracle_non_ladder:
            alerts.append(f"⚠ Ladder unexpectedly inactive (SL present, no reference) — {p['account']} {p['symbol']}")
        if p["management"] == "unknown":
            alerts.append(f"ℹ Position cannot be classified yet — {p['account']} {p['symbol']}")
    for acc in accounts:
        if acc.get("error"):
            alerts.append(f"⚠ {acc['name']}: {acc['error']}")
    ladder_age = oracle_meta.get("ladder_state_age_sec")
    if ladder_age is not None and freshness_state(ladder_age) in ("STALE WARNING", "DATA STALE"):
        alerts.append(f"⚠ BAA ladder state is {freshness_state(ladder_age)} ({ladder_age:.0f}s old)")
    return alerts


# ---------------------------------------------------------------------------
# Data fetching (all read-only)
# ---------------------------------------------------------------------------

def fetch_account(name, cfg):
    if cfg.get("dependency_error"):
        return {"name": name, "error": f"ERROR: {cfg['dependency_error']}"}
    try:
        kwargs = {"path": cfg["path"]}
        if cfg.get("login"):
            kwargs.update(login=cfg["login"], password=cfg["password"], server=cfg["server"])
        if not mt5.initialize(**kwargs):
            time.sleep(2)
            if not mt5.initialize(**kwargs):
                return {"name": name, "error": "MT5 init failed"}
        info = mt5.account_info()
        positions = mt5.positions_get() or []
        norm_positions = [normalize_mt5_position(p, name) for p in positions]
        data = {
            "name": name, "balance": round(info.balance, 2), "equity": round(info.equity, 2),
            "profit": round(info.profit, 2), "positions": norm_positions,
            "protected": sum(1 for p in norm_positions if p["protection"] == "PROTECTED"),
            "unprotected": sum(1 for p in norm_positions if p["protection"] == "UNPROTECTED"),
            "ladder_active": sum(1 for p in norm_positions if p["ladder"]["stage"] not in ("UNMANAGED", "UNKNOWN")),
        }
        mt5.shutdown()
        return data
    except Exception as e:
        return {"name": name, "error": str(e)}


def fetch_oracle():
    # 2026-07-26: found live -- fetch_oracle()'s failure paths returned {}
    # for meta instead of the full key set, and the template's `oracle_meta.x
    # is not none` guard doesn't actually catch a missing/Undefined key in
    # Jinja2 (Undefined is not None, so the check passes and the format
    # filter then crashes on it) -- 500 error on every page load while
    # Oracle is unreachable, i.e. exactly when this page matters most.
    # Every return path now uses the same full-shaped dict.
    try:
        oracle_cfg = _oracle_config()
        result = subprocess.run(
            ["ssh", "-i", oracle_cfg["ssh_key"], "-o", "StrictHostKeyChecking=no",
             "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", oracle_cfg["host"],
             "cd /home/ubuntu/trading-bot && venv/bin/python3 dashboard_snapshot.py"],
            capture_output=True, text=True, timeout=25, stdin=subprocess.DEVNULL
        )
        if not result.stdout.strip():
            return ({"name": "Oracle (Bybit ccxt)",
                      "error": f"empty stdout, rc={result.returncode}, stderr={result.stderr[:300]}"},
                     dict(EMPTY_ORACLE_META))
        raw = json.loads(result.stdout.strip().splitlines()[-1])
        if raw.get("error"):
            return ({"name": "Oracle (Bybit ccxt)", "error": raw["error"]}, dict(EMPTY_ORACLE_META))
        ladder_constants = raw.get("ladder_constants")
        liqsweep_open_since = raw.get("liquidity_sweep_open_since_ms")
        norm_positions = [normalize_baa_position(p, ladder_constants, liqsweep_open_since)
                           for p in raw.get("positions", [])]
        data = {
            "name": "Oracle (Bybit ccxt)", "balance": round(raw["balance"], 2),
            "equity": round(raw["equity"], 2),
            "profit": round(sum(p["profit"] or 0 for p in norm_positions), 4),
            "positions": norm_positions,
            "protected": sum(1 for p in norm_positions if p["protection"] == "PROTECTED"),
            "unprotected": sum(1 for p in norm_positions if p["protection"] == "UNPROTECTED"),
            "ladder_active": sum(1 for p in norm_positions if p["ladder"]["stage"] not in ("UNMANAGED", "UNKNOWN")),
        }
        meta = {
            "ladder_state_age_sec": raw.get("ladder_state_age_sec"),
            "fetched_ts": raw.get("fetched_ts"),
            "bybit_shield_alive": raw.get("bybit_shield_process_alive"),
            "ladder_guard_alive": raw.get("ladder_guard_process_alive"),
            "tg_signal_bot_alive": raw.get("tg_signal_bot_process_alive"),
            "liquidity_sweep_alive": raw.get("liquidity_sweep_process_alive"),
        }
        return data, meta
    except Exception as e:
        meta = dict(EMPTY_ORACLE_META)
        if "Oracle config" in str(e):
            meta["config_error"] = str(e)
        return ({"name": "Oracle (Bybit ccxt)", "error": str(e)}, meta)


def _account_labels(flat_text, marker, count):
    """Display-only: for each occurrence of `marker` in the (whitespace-
    flattened, so wmic's column-wrapped lines can't split a token) process
    list text, look at the text right after it for a trailing account
    argument (EA/EM/BA — how ea_shield.py/ladder_guard.py/ema_adx_bot.py are
    invoked per-account). Returns one label (or None if not found) per
    occurrence -- never affects which processes are counted, only how the
    count is captioned."""
    labels = []
    start = 0
    for _ in range(count):
        idx = flat_text.find(marker, start)
        if idx == -1:
            break
        window = flat_text[idx + len(marker):idx + len(marker) + 40]
        m = re.search(r'\b(EA|EM|BA)\b', window)
        labels.append(m.group(1) if m else None)
        start = idx + len(marker)
    return labels


def running_bots():
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name like 'python%.exe'", "get", "CommandLine"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10
        ).stdout
        flat = re.sub(r"\s+", " ", out)
        found = []
        for marker in BOT_MARKERS:
            count = out.count(marker)
            if not count:
                continue
            label = f"{marker} — {count} instance(s) running"
            if count > 1:
                labels = _account_labels(flat, marker, count)
                # ema_adx_bot's BA leg is launched with no account argument at
                # all (its own default) -- the one case an unlabeled slot is
                # still known-good, everywhere else an unlabeled slot means
                # "don't guess" and we fall back to the plain count.
                if marker == "ema_adx_bot":
                    labels = [l or "BA" for l in labels]
                if all(labels) and len(labels) == count:
                    label = f"{marker} — {count} instance(s) running ({' + '.join(labels)})"
            found.append(label)
        return found or ["none detected"]
    except Exception as e:
        return [f"error checking: {e}"]


# ---------------------------------------------------------------------------
# Bot Health detail (2026-08-18, read-only, Ahmed's request): per-instance
# PID + heartbeat + role + trade-eligibility instead of just a name+count.
# Reuses the SAME wmic process list and heartbeat_*.json files already read
# elsewhere in this file -- no new data source, no process control of any
# kind (no start/stop/restart calls anywhere in this block).
# ---------------------------------------------------------------------------

# (marker as it appears in the command line, [account labels] or [None] for
# a single/unlabeled instance, Role, staleness threshold in seconds or None
# if this marker has no heartbeat file to check -- PID presence is then the
# only signal available for it).
BOT_REGISTRY = [
    # gold_btc_bot's own check_interval_sec (gold_btc_bot_config.json) is 900s
    # (one full XAU+BTC scan per heartbeat write) -- a 60s threshold would
    # show STALE on every single cycle by design, not on a real hang. Give it
    # one full interval of slack (found during this feature's own validation).
    ("gold_btc_bot",                    ["EA", "BA", "EM"], "TRADING",        1080),
    ("ea_shield",                        ["EA", "BA", "EM"], "PROTECTION",     30),
    ("ladder_guard.py",                  ["EA", "BA", "EM"], "PROTECTION",     30),
    ("fx_signal_exec",                   [None],              "TRADING",       120),
    ("MT5Bridge\\server.py",             ["BA"],              "TRADING",       120),
    ("MT5Bridge_E2\\server.py",          ["EA"],              "TRADING",       120),
    ("orb_eth_exness",                   ["EA", "EM"],        "TRADING",       120),
    ("ema_adx_bot",                      [None, "EA"],        "TRADING",       None),
    ("fast_move_watch",                  [None],              "SIGNAL/WATCH",  60),
    ("grind_watch",                      [None],              "SIGNAL/WATCH",  120),
    ("local_crypto_watch",               [None],              "SIGNAL/WATCH",  120),
    ("hypergold_scalp_shadow",           [None],              "RESEARCH",      3600),
    ("detection_miss_monitor",           [None],              "INFRASTRUCTURE",300),
    # claude_loop_heartbeat.py writes claude_loop_status.json, not a generic
    # heartbeat_*.json -- already surfaced separately via claude_loop_status()
    # elsewhere on this page, so PID presence is the only signal checked here.
    ("claude_loop_heartbeat",            [None],              "INFRASTRUCTURE",None),
    ("control_api.py",                   [None],              "INFRASTRUCTURE",None),
    ("status_dashboard.py",              [None],              "INFRASTRUCTURE",None),
    ("regime_detector.py",               [None],              "RESEARCH",      3600),
    ("outcome_calibration_log.py",       [None],              "RESEARCH",      3600),
    ("participation_pilot_btc_range.py", [None],              "TRADING",       300),
    ("participation_pilot_btc.py",       [None],              "TRADING",       None),  # must stay stopped
]


def _wmic_pid_map():
    """Read-only. Same wmic call shape as running_bots(), but also captures
    ProcessId so each row below can show a real PID instead of just a count."""
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name like 'python%.exe'", "get", "CommandLine,ProcessId"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10
        ).stdout
        rows = []
        for line in out.splitlines():
            line = line.rstrip()
            if not line.strip():
                continue
            m = re.search(r"(\d+)\s*$", line)
            if not m:
                continue
            rows.append((line[:m.start()].strip(), int(m.group(1))))
        return rows
    except Exception:
        return []


def _match_instances(rows, marker, accounts_wanted):
    """Pairs each occurrence of `marker` in the process list with an account
    label from `accounts_wanted`, in the order both appear. Display-only --
    same best-effort labelling _account_labels() already does, just also
    keeping the PID this time."""
    hits = [(cmd, pid) for cmd, pid in rows if marker in cmd]
    out = []
    remaining = list(accounts_wanted)
    for cmd, pid in hits:
        label = None
        m = re.search(r'\b(EA|EM|BA)\b', cmd[cmd.find(marker) + len(marker):cmd.find(marker) + len(marker) + 40])
        if m:
            label = m.group(1)
        elif remaining:
            label = remaining[0]
        if label in remaining:
            remaining.remove(label)
        out.append((pid, label))
    return out


def _heartbeat_name(marker, acc):
    base = marker[:-3] if marker.endswith(".py") else marker
    return f"{base}_{acc}" if acc else base


def bot_health_detail(accounts, risk_halted):
    """Read-only. Returns one row per known (bot, account) instance:
    PID, State (RUNNING/STALE/STOPPED/UNKNOWN), last heartbeat + age, Role,
    Can Trade (YES/NO/BLOCK ONLY/UNKNOWN), and -- for gold_btc_bot rows,
    where a real per-account gate exists -- Execution Eligibility."""
    rows_raw = _wmic_pid_map()
    equity_by_acc = _control_equity_by_code(accounts)
    out = []
    for marker, acc_list, role, stale_after in BOT_REGISTRY:
        instances = _match_instances(rows_raw, marker, [a for a in acc_list if a])
        seen_accs = {label for _, label in instances}
        # every configured account gets a row even if no matching process was found
        display_accs = acc_list if acc_list != [None] else [None]
        for acc in display_accs:
            match = next((pid_lbl for pid_lbl in instances
                          if pid_lbl[1] == acc or (acc is None and pid_lbl[1] not in seen_accs - {acc})), None)
            pid = match[0] if match else None
            hb_name = _heartbeat_name(marker, acc) if stale_after else None
            hb = _pa_call("last_heartbeat", None, hb_name) if hb_name else None
            if pid is None:
                state = "STOPPED"
            elif stale_after is None:
                state = "RUNNING" if pid else "UNKNOWN"
            elif hb is None:
                state = "UNKNOWN"
            elif hb["age_sec"] <= stale_after:
                state = "RUNNING"
            else:
                state = "STALE"

            manual_block = None
            manual_bot = None
            if marker == "fx_signal_exec":
                manual_bot = "fx_signal_exec"
            elif marker in ("MT5Bridge\\server.py", "MT5Bridge_E2\\server.py"):
                manual_bot = "London Breakout"
            elif marker in ("gold_btc_bot", "orb_eth_exness"):
                manual_bot = marker
            if manual_bot:
                manual_block = _manual_block_reason_for(manual_bot, acc)

            if role == "PROTECTION":
                can_trade = "BLOCK ONLY"
            elif role in ("SIGNAL/WATCH", "RESEARCH", "INFRASTRUCTURE"):
                can_trade = "NO"
            elif manual_block:
                can_trade = "BLOCKED (MANUAL)"
            elif state in ("STOPPED", "UNKNOWN"):
                can_trade = "UNKNOWN" if state == "UNKNOWN" else "NO"
            elif marker == "participation_pilot_btc.py":
                can_trade = "NO"
            elif risk_halted:
                can_trade = "NO"
            else:
                can_trade = "YES"

            eligibility = None
            if marker == "gold_btc_bot" and acc:
                eligibility = _pa_call("gold_btc_bot_eligibility", "ERROR: dependency unavailable",
                                       acc, equity_by_acc.get(acc), risk_halted)

            out.append({
                "bot": (
                    "London Breakout" if manual_bot == "London Breakout"
                    else (marker[:-3] if marker.endswith(".py") else marker)
                ),
                "account": acc or "—",
                "pid": pid or "—",
                "state": state,
                "heartbeat_ts": hb["ts"] if hb else None,
                "heartbeat_age": _fmt_ago(hb["age_sec"]) if hb else ("n/a" if stale_after is None else "never"),
                "role": role,
                "can_trade": can_trade,
                "manual_block_reason": manual_block,
                "eligibility": eligibility,
            })
    return out


def _demo():
    """ponytail: smallest check that fails if the account-label parsing breaks."""
    flat = ('"python.exe" -u "ema_adx_bot.py"   "python.exe" -u "ema_adx_bot.py" EA  '
            '"python.exe" -u "ladder_guard.py" EA   "python.exe" -u "ladder_guard.py" EM   '
            '"python.exe" -u "ladder_guard.py" BA')
    labels = _account_labels(flat, "ema_adx_bot", 2)
    assert labels == [None, "EA"], labels  # BA leg has no explicit token -- caller fills the default
    labels = _account_labels(flat, "ladder_guard.py", 3)
    assert labels == ["EA", "EM", "BA"], labels
    assert _account_labels(flat, "no_such_marker", 1) == []
    from datetime import datetime as _dt
    now = _dt.now(timezone.utc)
    sample = [
        {"account": "EA", "position_id": 1, "magic": 1, "comment": "", "pnl": 2.5,
         "exit_time": now, "entry_time": now.replace(hour=max(now.hour - 1, 0)),
         "symbol": "BTC", "side": "buy"},
        {"account": "EA", "position_id": 1, "magic": 1, "comment": "", "pnl": 2.5,
         "exit_time": now, "entry_time": now.replace(hour=max(now.hour - 1, 0)),
         "symbol": "BTC", "side": "buy"},
        {"account": "BA", "position_id": 2, "magic": 2, "comment": "", "pnl": -1.0,
         "exit_time": now.replace(day=max(now.day - 1, 1)), "entry_time": now.replace(day=max(now.day - 1, 1), hour=max(now.hour - 1, 0)),
         "symbol": "XAU", "side": "sell"},
    ]
    snap = _build_trade_history_snapshot(sample)
    assert snap["dup_count"] == 1, snap
    assert snap["grand_total"] == 2, snap
    assert snap["today_realized_pl"] == 2.5, snap

    equity_map = _control_equity_by_code([
        {"name": "Bybit MT5", "equity": 91.25},
        {"name": "E1 (Exness)", "equity": 42.0},
        {"name": "E2 (Exness)", "equity": 133.5},
    ])
    assert equity_map == {"BA": 91.25, "EM": 42.0, "EA": 133.5}, equity_map

    LIVE_MT5_ACCOUNTS["TEST"] = {"path": "terminal", "login": 123, "password": "secret", "server": "live"}
    try:
        assert _account_config("TEST", "fallback")["login"] == 123
    finally:
        del LIVE_MT5_ACCOUNTS["TEST"]

    orig_reason_fn = globals()["period_manual_block_reason"]
    try:
        def _fake_manual(bot, acc):
            if (bot, acc) == ("gold_btc_bot", "EM"):
                return "gold EM locked"
            if (bot, acc) == ("orb_eth_exness", "EA"):
                return "orb EA locked"
            return None
        globals()["period_manual_block_reason"] = _fake_manual
        rows = _manual_entry_block_rows()
        assert any(r["bot"] == "gold_btc_bot" and r["account"] == "EM" and r["status"] == "BLOCKED (MANUAL)" for r in rows), rows
        assert any(r["bot"] == "orb_eth_exness" and r["account"] == "EA" and r["status"] == "BLOCKED (MANUAL)" for r in rows), rows
    finally:
        globals()["period_manual_block_reason"] = orig_reason_fn

    def _demo_obs():
        return _observability_model(
            accounts=[{"name": "Bybit MT5", "positions": []}, {"name": "E1 (Exness)", "positions": []},
                      {"name": "E2 (Exness)", "positions": []}, {"name": "Oracle (Bybit ccxt)", "positions": []}],
            bot_health=[{"bot": "demo_shadow", "role": "RESEARCH", "state": "STALE", "heartbeat_age": "20m",
                         "pid": 123, "account": "—", "can_trade": "NO", "manual_block_reason": None,
                         "eligibility": None}],
            oracle_meta={"bybit_shield_alive": True, "ladder_guard_alive": True, "tg_signal_bot_alive": True,
                         "consecutive_failures": 0, "last_success_ts": 1, "next_retry_ts": 2},
            risk_halt={"halted": False}, no_trade_status=[],
            shadow={"overall": {"open": 0, "expired": 0, "closed": 0, "expectancy_r": 0}},
            pending_breakout=None, pending_signal_runner=None, pending_signal_bridge=None,
            pending_signal_shadow=None, manual_exit_shadow=None,
            market_context={"monitors": []}, oracle_attr=None, cache_age_sec=1,
        )

    import_errors = dict(DEPENDENCY_ERRORS)
    DEPENDENCY_ERRORS.clear()
    obs = _demo_obs()
    assert obs["score"] == 90, obs
    assert any(p["component"] == "demo_shadow" and p["status"] == "STALE" for p in obs["problems"]), obs
    DEPENDENCY_ERRORS["demo_dependency"] = "ImportError: simulated"
    failed_obs = _demo_obs()
    assert failed_obs["score"] == 70, failed_obs
    assert any(p["component"] == "demo_dependency" and p["status"] == "ERROR"
               for p in failed_obs["problems"]), failed_obs
    DEPENDENCY_ERRORS.clear()
    DEPENDENCY_ERRORS.update(import_errors)

    orig_config_path = globals()["ORACLE_CONFIG_PATH"]
    old_env = {k: os.environ.get(k) for k in ("STATUS_DASHBOARD_ORACLE_HOST", "STATUS_DASHBOARD_ORACLE_SSH_KEY")}
    try:
        globals()["ORACLE_CONFIG_PATH"] = os.path.join(os.path.dirname(__file__), "__missing_oracle_config.json")
        os.environ["STATUS_DASHBOARD_ORACLE_HOST"] = "ubuntu@<" + "REDACTED_ORACLE_IP>"
        os.environ["STATUS_DASHBOARD_ORACLE_SSH_KEY"] = ""
        oracle_data, oracle_meta = fetch_oracle()
        assert oracle_data["error"].startswith("Oracle config missing/redacted"), oracle_data
        assert oracle_meta["config_error"], oracle_meta
        obs = _observability_model(
            accounts=[oracle_data], bot_health=[], oracle_meta=dict(oracle_meta, consecutive_failures=1),
            risk_halt={"halted": False}, no_trade_status=[], shadow=None, pending_breakout=None,
            pending_signal_runner=None, pending_signal_bridge=None, pending_signal_shadow=None,
            manual_exit_shadow=None, market_context={"monitors": []}, oracle_attr=None, cache_age_sec=1,
        )
        assert any(p["component"] == "Oracle SSH/cache" and p["status"] == "ERROR" for p in obs["problems"]), obs
    finally:
        globals()["ORACLE_CONFIG_PATH"] = orig_config_path
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print("status_dashboard account-label self-check OK")


def refresh_loop():
    while True:
        now = time.time()
        if now - _oracle_cache["ts"] >= _oracle_interval():
            oracle_data, oracle_meta = fetch_oracle()
            failed = bool(oracle_data.get("error"))
            _oracle_cache["data"], _oracle_cache["meta"], _oracle_cache["ts"] = oracle_data, oracle_meta, now
            if failed:
                _oracle_cache["consecutive_failures"] += 1
            else:
                _oracle_cache["consecutive_failures"] = 0
                _oracle_cache["last_success_ts"] = now
            _oracle_cache["next_retry_ts"] = now + _oracle_interval()
        accounts = [fetch_account(name, cfg) for name, cfg in ACCOUNTS.items()]
        if _oracle_cache["data"] is not None:
            accounts.append(_oracle_cache["data"])
        bots = running_bots()
        with _cache_lock:
            _cache["accounts"], _cache["bots"] = accounts, bots
            _cache["oracle_meta"] = dict(
                _oracle_cache["meta"],
                consecutive_failures=_oracle_cache["consecutive_failures"],
                last_success_ts=_oracle_cache["last_success_ts"] or None,
                next_retry_ts=_oracle_cache["next_retry_ts"] or None,
            )
            _cache["ts"] = time.time()
        # 2026-08-07 (Ahmed's Monitoring & Alerting request): evaluated on
        # the SAME refresh cycle this loop already runs every REFRESH_SEC --
        # not a new polling loop, just one more read-only step on an
        # existing one. Never raises (each check inside evaluate() is its
        # own try/except) and never touches any trading call.
        try:
            current_alerts = am.evaluate(accounts, bots, _cache["oracle_meta"], _oracle_cache.get("data"))
            am.process(current_alerts)
        except Exception as e:
            _dependency_failed("alert_manager.runtime", e)
        # 2026-08-09 (Ahmed's 7-Day Autonomy / $300 challenge): same pattern
        # as the alert-manager step above -- one more read-only step on this
        # existing refresh cycle, wrapped so a failure here can never affect
        # the dashboard itself. No-op entirely when no challenge is ACTIVE.
        try:
            from datetime import datetime as _dt
            _chall_state = achall.read_state()
            if _chall_state and _chall_state.get("status") == "ACTIVE":
                _chall_start = _dt.fromisoformat(_chall_state["start_timestamp_utc"])
                _chall_trades, _, _ = _get_trades(days=180)
                _chall_mt5_trades = _filter_trades(_chall_trades, date_from=_chall_start)
                _chall_oracle = _get_oracle_attribution() or {}
                _chall_oracle_fills = 0
                for f in _chall_oracle.get("recent", []):
                    dt_str = f.get("datetime")
                    if not dt_str:
                        continue
                    try:
                        fdt = _dt.fromisoformat(dt_str.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if fdt >= _chall_start:
                        _chall_oracle_fills += 1
                achall.record_snapshot(
                    accounts, _chall_mt5_trades, _chall_oracle_fills,
                    risk_halted=pa.risk_halt_status().get("halted", False),
                )
        except Exception as e:
            _dependency_failed("autonomy_challenge.runtime", e)
        time.sleep(REFRESH_SEC)


if "--test" not in sys.argv and "--preflight" not in sys.argv:
    threading.Thread(target=refresh_loop, daemon=True).start()


PAGE = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Trading Status</title>
<style>
/* ===== Mobile-first UI rebuild (2026-08-07, Ahmed's spec) =====
   Front-end only (HTML/CSS/JS) -- zero backend/API/trading-logic change.
   Same render_template_string() context as before; all new aggregates
   below are computed IN Jinja2 from data already passed in, never from
   a new Python code path. */
:root{
  --bg:#f4f5f7; --card:#ffffff; --card2:#f8f9fb; --text:#1a1d21; --muted:#6b7280;
  --border:#e3e5e8; --accent:#2563eb;
  --good:#15803d; --good-bg:#dcfce7; --bad:#b91c1c; --bad-bg:#fee2e2;
  --wait:#1d4ed8; --wait-bg:#dbeafe; --warn:#b45309; --warn-bg:#fef3c7;
  --shadow:0 1px 3px rgba(0,0,0,.08);
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#0f1115; --card:#181b20; --card2:#1f2329; --text:#e8eaed; --muted:#9aa1ac;
    --border:#2a2e35; --accent:#5b9cff;
    --good:#4ade80; --good-bg:#0f2f1c; --bad:#f87171; --bad-bg:#3a1414;
    --wait:#7db2ff; --wait-bg:#122240; --warn:#fbbf24; --warn-bg:#3a2a0a;
    --shadow:0 1px 3px rgba(0,0,0,.4);
  }
}
:root[data-theme="dark"]{
  --bg:#0f1115; --card:#181b20; --card2:#1f2329; --text:#e8eaed; --muted:#9aa1ac;
  --border:#2a2e35; --accent:#5b9cff;
  --good:#4ade80; --good-bg:#0f2f1c; --bad:#f87171; --bad-bg:#3a1414;
  --wait:#7db2ff; --wait-bg:#122240; --warn:#fbbf24; --warn-bg:#3a2a0a;
  --shadow:0 1px 3px rgba(0,0,0,.4);
}
:root[data-theme="light"]{
  --bg:#f4f5f7; --card:#ffffff; --card2:#f8f9fb; --text:#1a1d21; --muted:#6b7280;
  --border:#e3e5e8; --accent:#2563eb;
  --good:#15803d; --good-bg:#dcfce7; --bad:#b91c1c; --bad-bg:#fee2e2;
  --wait:#1d4ed8; --wait-bg:#dbeafe; --warn:#b45309; --warn-bg:#fef3c7;
  --shadow:0 1px 3px rgba(0,0,0,.08);
}
*{box-sizing:border-box}
html,body{max-width:100%;overflow-x:hidden}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--text);
  margin:0;padding:0 0 calc(24px + env(safe-area-inset-bottom,0px)) 0;font-size:16px;line-height:1.45;
  -webkit-text-size-adjust:100%}
a{color:var(--accent)}

/* Sticky header */
.sticky-header{position:sticky;top:0;z-index:50;background:var(--card);border-bottom:1px solid var(--border);
  padding:10px 14px calc(10px + env(safe-area-inset-top,0px)) 14px;box-shadow:var(--shadow)}
.sh-inner{max-width:960px;margin:0 auto}
.sh-top{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.sh-equity{font-size:22px;font-weight:800}
.sh-stats{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:6px}
.sh-stat{font-size:12.5px;color:var(--muted);white-space:nowrap}
.sh-stat b{color:var(--text);font-weight:700}
.sh-stat b.good{color:var(--good)} .sh-stat b.bad{color:var(--bad)}
.freshness{display:inline-block;padding:3px 9px;border-radius:20px;font-weight:700;font-size:12px}
.f-LIVE{background:var(--good-bg);color:var(--good)}
.f-DELAYED{background:var(--warn-bg);color:var(--warn)}
.f-STALE-WARNING{background:var(--warn-bg);color:var(--warn)}
.f-DATA-STALE,.f-UNKNOWN{background:var(--bad-bg);color:var(--bad)}
.ctrl-status{display:inline-block;padding:3px 10px;border-radius:20px;font-weight:700;font-size:12px}
.ctrl-status.off{background:var(--warn-bg);color:var(--warn)}
.ctrl-status.ok{background:var(--good-bg);color:var(--good)}

.wrap{padding:12px;max-width:960px;margin:0 auto}
@media (min-width:820px){
  .wrap .acc-card{padding:16px}
  .summary-grid{grid-template-columns:repeat(4,1fr)}
  .bot-grid{grid-template-columns:repeat(3,1fr)}
}

/* Quick nav to the read-only sub-pages (Analytics/Risk/Reports/Trades/Alerts) */
.quick-nav{display:flex;gap:8px;padding:8px 14px 0 14px;max-width:960px;margin:0 auto;flex-wrap:wrap}
.quick-nav a{color:var(--accent);text-decoration:none;font-weight:700;font-size:12.5px;background:var(--card2);
  padding:6px 12px;border-radius:16px;border:1px solid var(--border)}

/* Trade History link card */
.th-card{background:var(--card);border-radius:14px;padding:14px 16px;margin-bottom:12px;box-shadow:var(--shadow);
  display:flex;justify-content:space-between;align-items:center;border-left:5px solid var(--accent);
  text-decoration:none;color:var(--text)}
.th-card:hover{background:var(--card2)}
.th-title{font-size:15px;font-weight:800}
.th-sub{font-size:12.5px;color:var(--muted);margin-top:2px}
.th-arrow{font-size:14px;font-weight:700;color:var(--accent);white-space:nowrap}

/* Alerts */
.alerts-bar{background:var(--bad-bg);border:1px solid var(--bad);border-radius:12px;padding:10px 12px;margin-bottom:12px}
.alerts-bar h2{margin:0 0 6px;font-size:15px;color:var(--bad)}
.alert-item{font-size:14px;color:var(--text);padding:4px 0;border-top:1px solid rgba(0,0,0,.06)}
.alert-item:first-of-type{border-top:none}

/* Summary grid */
.summary-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:14px}
@media (min-width:480px){.summary-grid{grid-template-columns:repeat(4,1fr)}}
.summary-cell{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:10px 12px;box-shadow:var(--shadow)}
.sc-label{font-size:12px;color:var(--muted);margin-bottom:2px}
.sc-value{font-size:18px;font-weight:700}
.sc-value.good{color:var(--good)} .sc-value.bad{color:var(--bad)}

/* Collapsible sections */
details.section{background:var(--card);border:1px solid var(--border);border-radius:14px;margin-bottom:12px;
  overflow:hidden;box-shadow:var(--shadow)}
details.section>summary{list-style:none;cursor:pointer;padding:14px 16px;font-size:17px;font-weight:700;
  display:flex;justify-content:space-between;align-items:center;-webkit-tap-highlight-color:transparent}
details.section>summary::-webkit-details-marker{display:none}
details.section>summary::after{content:'▾';color:var(--muted);transition:transform .15s;font-size:14px}
details.section[open]>summary::after{transform:rotate(180deg)}
details.section>summary:active{background:var(--card2)}
.section-body{padding:0 12px 12px 12px}

/* Account cards */
.acc-card{background:var(--card2);border-radius:12px;padding:12px;margin-bottom:12px;border-left:5px solid var(--muted)}
.acc-BA{border-left-color:#f59e0b}
.acc-EA{border-left-color:#3b82f6}
.acc-EM{border-left-color:#a855f7}
.acc-ORACLE{border-left-color:#14b8a6}
.acc-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.acc-name{font-size:16px;font-weight:700}
.acc-badge{font-size:11px;font-weight:800;padding:3px 8px;border-radius:6px;background:var(--card);border:1px solid var(--border);color:var(--muted)}
.acc-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;font-size:13px;margin-bottom:8px}
.acc-stat-label{color:var(--muted);font-size:11px}
.acc-stat-value{font-weight:700;font-size:15px}

/* Position cards */
.pos-card{background:var(--card);border-radius:12px;padding:12px;margin-bottom:10px;border-left:5px solid var(--muted);box-shadow:var(--shadow)}
.pos-card.protected{border-left-color:var(--good)}
.pos-card.partial{border-left-color:var(--warn)}
.pos-card.unprotected{border-left-color:var(--bad)}
.pos-top{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:6px}
.pos-sym{font-size:17px;font-weight:800}
.pos-side{display:inline-block;padding:2px 8px;border-radius:6px;font-size:12px;font-weight:800;margin-left:6px;vertical-align:middle}
.side-BUY,.side-long{background:var(--good-bg);color:var(--good)}
.side-SELL,.side-short{background:var(--bad-bg);color:var(--bad)}
.pos-lot{font-size:13px;color:var(--muted);font-weight:400}
.pos-pl{font-size:19px;font-weight:800;white-space:nowrap}
.pos-pl.pos{color:var(--good)} .pos-pl.neg{color:var(--bad)}
.pos-bot{font-size:13px;color:var(--muted);margin-bottom:6px}
.pos-prices{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:13px;color:var(--muted);margin-bottom:8px}
.pos-prices b{color:var(--text)}
.badge{display:inline-block;padding:3px 9px;border-radius:7px;font-size:12px;font-weight:700;margin-right:5px}
.b-PROTECTED{background:var(--good-bg);color:var(--good)}
.b-PARTIAL{background:var(--warn-bg);color:var(--warn)}
.b-UNPROTECTED{background:var(--bad-bg);color:var(--bad)}
.stage-row{font-size:14px;margin:8px 0 4px 0}
.stage-pill{display:inline-block;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:800}
.stage-WAITING{background:var(--wait-bg);color:var(--wait)}
.stage-BREAKEVEN{background:var(--warn-bg);color:var(--warn)}
.stage-PROFIT{background:var(--good-bg);color:var(--good)}
.stage-RUNNER{background:var(--good-bg);color:var(--good)}
.stage-RISK{background:var(--warn-bg);color:var(--warn)}
.stage-UNMANAGED,.stage-UNKNOWN{background:var(--bad-bg);color:var(--bad)}
.progress-track{background:var(--border);border-radius:10px;height:10px;overflow:hidden;margin:8px 0}
.progress-fill{background:linear-gradient(90deg,var(--wait),var(--good));height:100%;border-radius:10px;transition:width .3s}
.next-action{font-size:13px;color:var(--wait);margin-top:4px}

/* Bot health grid */
.bot-grid{display:grid;grid-template-columns:1fr;gap:8px}
@media (min-width:480px){.bot-grid{grid-template-columns:1fr 1fr}}
.bot-card{background:var(--card2);border-radius:10px;padding:10px 12px;display:flex;align-items:center;gap:8px;font-size:13px}
.bot-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.bot-up .bot-dot{background:var(--good)} .bot-down .bot-dot{background:var(--bad)}
.bot-name{font-weight:600;word-break:break-word}
.chip-row{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.chip{background:var(--card2);border-radius:20px;padding:6px 12px;font-size:13px;display:flex;align-items:center;gap:6px}
.halt-banner{background:var(--bad-bg);color:var(--bad);border-radius:10px;padding:12px;margin-bottom:10px;font-weight:700;text-align:center}
.mono-table{border-collapse:collapse;width:100%;font-size:12px;white-space:nowrap}
.mono-table th,.mono-table td{padding:5px 10px;text-align:left;border-bottom:1px solid var(--card2)}
.mono-table th{color:var(--muted);font-weight:600}

/* Owner control buttons -- same classnames as before, JS logic untouched */
.ctrl-row{margin-top:8px;display:flex;gap:8px;flex-wrap:wrap}
.ctrl-btn{background:var(--card2);color:var(--text);border:1.5px solid var(--border);border-radius:10px;
  padding:10px 16px;font-size:15px;font-weight:600;cursor:pointer;min-height:44px}
.ctrl-btn:disabled{opacity:.4;cursor:not-allowed}
.ctrl-btn.close-btn:not(:disabled){border-color:var(--bad);color:var(--bad)}
.ctrl-btn.be-btn:not(:disabled){border-color:var(--good);color:var(--good)}
.ctrl-btn.close-all-btn{width:100%;margin-bottom:6px}
.ctrl-btn.close-all-btn:not(:disabled){border-color:var(--bad);color:var(--bad);font-weight:800}

.footnote{color:var(--muted);font-size:12px;text-align:center;padding:10px 16px}
.empty-note{color:var(--muted);font-size:14px;padding:8px 0}

/* NOC: Health Score + quick-status + badges */
.health-card{border-radius:16px;padding:18px;margin-bottom:12px;text-align:center;box-shadow:var(--shadow)}
.health-card.hs-100{background:var(--good-bg);border:2px solid var(--good)}
.health-card.hs-warn{background:var(--warn-bg);border:2px solid var(--warn)}
.health-card.hs-bad{background:var(--bad-bg);border:2px solid var(--bad)}
.health-score-num{font-size:40px;font-weight:900;line-height:1}
.health-score-num.good{color:var(--good)} .health-score-num.warn{color:var(--warn)} .health-score-num.bad{color:var(--bad)}
.health-title{font-size:17px;font-weight:800;margin-top:6px}
.health-title.good{color:var(--good)} .health-title.warn{color:var(--warn)} .health-title.bad{color:var(--bad)}
.health-sub{font-size:13px;color:var(--muted);margin-top:4px}

.status-badge{display:inline-flex;align-items:center;gap:5px;padding:4px 11px;border-radius:20px;font-size:12.5px;font-weight:800}
.status-badge.healthy{background:var(--good-bg);color:var(--good)}
.status-badge.warning{background:var(--warn-bg);color:var(--warn)}
.status-badge.critical{background:var(--bad-bg);color:var(--bad)}
.status-badge.info{background:var(--wait-bg);color:var(--wait)}

.noc-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-bottom:14px}
@media (min-width:480px){.noc-grid{grid-template-columns:repeat(3,1fr)}}
@media (min-width:820px){.noc-grid{grid-template-columns:repeat(4,1fr)}}
.noc-cell{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:14px 12px;text-align:center;box-shadow:var(--shadow)}
.noc-icon{font-size:26px;line-height:1}
.noc-value{font-size:20px;font-weight:800;margin-top:4px}
.noc-label{font-size:12px;color:var(--muted);margin-top:2px}
</style></head><body>

{% set totals = namespace(eq=0, bal=0, pl=0) %}
{% for acc in accounts %}{% if not acc.error %}
{% set totals.eq = totals.eq + (acc.equity or 0) %}
{% set totals.bal = totals.bal + (acc.balance or 0) %}
{% set totals.pl = totals.pl + (acc.profit or 0) %}
{% endif %}{% endfor %}
{% set total_equity = totals.eq %}
{% set total_balance = totals.bal %}
{% set total_pl = totals.pl %}
{% set protected_pct = (total_protected / total_positions * 100) if total_positions > 0 else 100 %}

{# ===== NOC Health Score (2026-08-07, Ahmed's spec) -- computed ENTIRELY in
   the template from data already in this render context. No new backend
   call, no new field. "All required bots running" has no true expected-vs-
   actual list exposed by the API, so it's approximated as "at least one
   bot type currently detected running" -- the closest honest signal
   available without adding a backend endpoint (explicitly out of scope). #}
{% set mt5 = namespace(ea=none, em=none, ba=none, oracle=none) %}
{% for acc in accounts %}
{% set code = account_codes.get(acc.name) %}
{% if code == 'EA' %}{% set mt5.ea = not acc.error %}{% endif %}
{% if code == 'EM' %}{% set mt5.em = not acc.error %}{% endif %}
{% if code == 'BA' %}{% set mt5.ba = not acc.error %}{% endif %}
{% if not code %}{% set mt5.oracle = not acc.error %}{% endif %}
{% endfor %}
{% set mt5_all_ok = mt5.ea and mt5.em and mt5.ba %}
{% set oracle_ok = mt5.oracle and oracle_meta and oracle_meta.bybit_shield_alive and oracle_meta.ladder_guard_alive and oracle_meta.tg_signal_bot_alive %}
{% set bots_ok = bots|length > 0 %}
{% set unprotected_ok = total_unprotected == 0 %}
{% set alerts_ok = not alerts %}
{% set health_score = (20 if bots_ok else 0) + (20 if mt5_all_ok else 0) + (20 if oracle_ok else 0) + (20 if unprotected_ok else 0) + (20 if alerts_ok else 0) %}

<div class="sticky-header"><div class="sh-inner">
  <div class="sh-top">
    <span class="sh-equity">${{ '%.2f'|format(total_equity) }}</span>
    <span class="freshness f-{{ freshness.replace(' ','-') }}">{{ freshness }}</span>
  </div>
  <div class="sh-stats">
    <span class="sh-stat">Equity <b>${{ '%.2f'|format(total_equity) }}</b></span>
    <span class="sh-stat">Balance <b>${{ '%.2f'|format(total_balance) }}</b></span>
    <span class="sh-stat">P/L <b class="{{ 'good' if total_pl >= 0 else 'bad' }}">{{ '%+.2f'|format(total_pl) }}</b></span>
    <span class="sh-stat">Positions <b>{{ total_positions }}</b></span>
    <span class="sh-stat">Bots <b>{{ bots|length }}</b></span>
    <span class="sh-stat">Updated <b>{{ age }}s ago</b></span>
  </div>
  {% if is_owner %}<div style="margin-top:4px"><span id="control-status" class="ctrl-status off">Control Layer: checking…</span></div>{% endif %}
</div></div>

<div class="quick-nav">
<a href="/trades">📜 Trade History</a><a href="/analytics">📊 Analytics</a><a href="/risk">⚠️ Risk</a><a href="/reports">📄 Reports</a><a href="/alerts">🔔 Alerts</a><a href="/monitor">🖥️ Monitor</a>
</div>

<div class="wrap">

{% if alerts %}
<div class="alerts-bar"><h2>⚠️ Alerts</h2>{% for a in alerts %}<div class="alert-item">{{ a }}</div>{% endfor %}</div>
{% endif %}

<a href="/trades" class="th-card">
  <div><div class="th-title">📜 Trade History</div><div class="th-sub">{{ '{:,}'.format(trade_history_total) }} Closed Trades</div></div>
  <div class="th-arrow">View History →</div>
</a>

{% if health_score == 100 %}
<div class="health-card hs-100">
  <div class="health-score-num good">100</div>
  <div class="health-title good">✅ SYSTEM HEALTHY — NO ACTION REQUIRED</div>
</div>
{% else %}
<div class="health-card {{ 'hs-bad' if health_score < 60 else 'hs-warn' }}">
  <div class="health-score-num {{ 'bad' if health_score < 60 else 'warn' }}">{{ health_score }}</div>
  <div class="health-title {{ 'bad' if health_score < 60 else 'warn' }}">{{ '🔴 ATTENTION NEEDED' if health_score < 60 else '🟡 MINOR ISSUES' }}</div>
  <div class="health-sub">
    {{ '✅' if bots_ok else '❌' }} Bots ·
    {{ '✅' if mt5_all_ok else '❌' }} MT5 ·
    {{ '✅' if oracle_ok else '❌' }} Oracle ·
    {{ '✅' if unprotected_ok else '❌' }} Protection ·
    {{ '✅' if alerts_ok else '❌' }} Alerts
  </div>
</div>
{% endif %}

<div class="noc-grid">
  <div class="noc-cell">
    <div class="noc-icon">{{ '🟢' if bots_ok else '🔴' }}</div>
    <div class="noc-value">{{ bots|length }}</div>
    <div class="noc-label">Bots Running</div>
  </div>
  <div class="noc-cell">
    <div class="noc-icon">💼</div>
    <div class="noc-value">{{ total_positions }}</div>
    <div class="noc-label">Open Positions</div>
  </div>
  <div class="noc-cell">
    <div class="noc-icon">{{ '🟢' if unprotected_ok else '🔴' }}</div>
    <div class="noc-value {{ 'good' if unprotected_ok else 'bad' }}">{{ total_unprotected }}</div>
    <div class="noc-label">Unprotected</div>
  </div>
  <div class="noc-cell">
    <div class="noc-icon">{{ '🟢' if mt5_all_ok else '🔴' }}</div>
    <div class="noc-value" style="font-size:14px">
      EA {{ '🟢' if mt5.ea else '🔴' }} · EM {{ '🟢' if mt5.em else '🔴' }} · BA {{ '🟢' if mt5.ba else '🔴' }}
    </div>
    <div class="noc-label">MT5 Status</div>
  </div>
  <div class="noc-cell">
    <div class="noc-icon">{{ '🟢' if oracle_ok else '🔴' }}</div>
    <div class="noc-value" style="font-size:15px">{{ oracle_conn.label }}</div>
    <div class="noc-label">Oracle</div>
  </div>
  <div class="noc-cell">
    <div class="noc-icon">🕒</div>
    <div class="noc-value" style="font-size:15px">{{ age }}s ago</div>
    <div class="noc-label">Last Update</div>
  </div>
</div>

<div class="summary-grid">
<div class="summary-cell"><div class="sc-label">Total Balance</div><div class="sc-value">${{ '%.2f'|format(total_balance) }}</div></div>
<div class="summary-cell"><div class="sc-label">Total Equity</div><div class="sc-value">${{ '%.2f'|format(total_equity) }}</div></div>
<div class="summary-cell"><div class="sc-label">Floating P/L</div><div class="sc-value {{ 'good' if total_pl >= 0 else 'bad' }}">{{ '%+.2f'|format(total_pl) }}</div></div>
<div class="summary-cell"><div class="sc-label">Open Positions</div><div class="sc-value">{{ total_positions }}</div></div>
<div class="summary-cell"><div class="sc-label">Protected %</div><div class="sc-value {{ 'good' if total_unprotected == 0 else 'bad' }}">{{ '%.0f'|format(protected_pct) }}%</div></div>
<div class="summary-cell"><div class="sc-label">Running Bots</div><div class="sc-value">{{ bots|length }}</div></div>
</div>

{# 2026-08-19 (Ahmed's dashboard patch): small P/L summary, entirely from
   data already fetched for this same request (deduped trade history +
   account profit) -- no new backend pipeline, no new data source. #}
<div class="summary-grid">
<div class="summary-cell"><div class="sc-label">Today Realized P/L</div><div class="sc-value {{ 'good' if today_realized_pl >= 0 else 'bad' }}">{{ '%+.2f'|format(today_realized_pl) }}</div></div>
<div class="summary-cell"><div class="sc-label">Current Floating P/L</div><div class="sc-value {{ 'good' if current_floating_pl >= 0 else 'bad' }}">{{ '%+.2f'|format(current_floating_pl) }}</div></div>
<div class="summary-cell"><div class="sc-label">7D Net P/L</div><div class="sc-value {{ 'good' if week_net_pl >= 0 else 'bad' }}">{{ '%+.2f'|format(week_net_pl) }}</div></div>
<div class="summary-cell"><div class="sc-label">Fees/Costs</div><div class="sc-value">{{ fees_costs_summary }}</div></div>
<div class="summary-cell"><div class="sc-label">Top Losing Bot (7D)</div><div class="sc-value">{{ top_losing_bot }}</div></div>
</div>

<details class="section" open>
<summary>🏦 Accounts</summary>
<div class="section-body">
{% for acc in accounts %}
{% set acct_code = account_codes.get(acc.name, 'ORACLE') %}
<div class="acc-card acc-{{ acct_code }}">
<div class="acc-head">
  <span class="acc-name">{{ acc.name }}</span>
  <span class="acc-badge">{{ acct_code }}</span>
</div>
{% if acc.error %}<p style="color:var(--bad);font-size:14px">⚠️ {{ acc.error }}</p>{% else %}
<div class="acc-stats">
  <div><div class="acc-stat-label">Balance</div><div class="acc-stat-value">${{ acc.balance }}</div></div>
  <div><div class="acc-stat-label">Equity</div><div class="acc-stat-value">${{ acc.equity }}</div></div>
  <div><div class="acc-stat-label">Floating P/L</div><div class="acc-stat-value {{ 'good' if acc.profit >= 0 else 'bad' }}">{{ '%+.2f'|format(acc.profit) }}</div></div>
  <div><div class="acc-stat-label">Positions</div><div class="acc-stat-value">{{ acc.positions|length }}</div></div>
  <div><div class="acc-stat-label">Protected</div><div class="acc-stat-value">{{ acc.protected }}</div></div>
  <div><div class="acc-stat-label">Ladder Active</div><div class="acc-stat-value">{{ acc.ladder_active }}</div></div>
</div>
{% endif %}
</div>
{% endfor %}
</div>
</details>

<details class="section">
<summary>💼 Positions ({{ total_positions }})</summary>
<div class="section-body">
{% for acc in accounts %}
{% set acct_code = account_codes.get(acc.name, 'ORACLE') %}
{% if not acc.error and acc.positions %}
<div class="acc-card acc-{{ acct_code }}">
<div class="acc-head">
  <span class="acc-name">{{ acc.name }}</span>
  <span class="acc-badge">{{ acct_code }}</span>
</div>

{% if acct_code != 'ORACLE' and is_owner %}
<div class="ctrl-row">
<button type="button" class="ctrl-btn close-all-btn" data-account="{{ acct_code }}" disabled>Close ALL positions ({{ acct_code }})</button>
</div>
{% endif %}

{% for p in acc.positions %}
{% set pclass = 'protected' if p.protection=='PROTECTED' else ('partial' if p.protection=='PARTIAL PROTECTION' else 'unprotected') %}
<div class="pos-card {{ pclass }}">
<div class="pos-top">
  <div><span class="pos-sym">{{ p.symbol }}</span><span class="pos-side side-{{p.side}}">{{ p.side }}</span><div class="pos-lot">{{ p.volume }} lot</div></div>
  <div class="pos-pl {{ 'pos' if (p.profit or 0) >= 0 else 'neg' }}">{{ '%+.2f'|format(p.profit or 0) }}</div>
</div>
<div class="pos-bot">🤖 {{ p.strategy }}</div>
<div class="pos-prices">
  <span>Entry <b>{{ p.entry }}</b></span>
  <span>Now <b>{{ p.current }}</b></span>
</div>
<div style="margin-bottom:6px">
<span class="badge b-{{ 'PROTECTED' if p.protection=='PROTECTED' else ('PARTIAL' if p.protection=='PARTIAL PROTECTION' else 'UNPROTECTED') }}">{{ p.protection }}</span>
<span style="font-size:13px;color:var(--muted)">SL {{ p.sl if p.sl else 'NONE' }} · TP {{ p.tp if p.tp else 'NONE' }}</span>
</div>
{% set st = (p.ladder.stage or 'UNKNOWN') %}
{% set stclass = 'PROFIT' if 'PROFIT' in st else ('RUNNER' if 'RUNNER' in st else ('BREAKEVEN' if 'BREAKEVEN' in st else ('RISK' if 'RISK' in st else ('WAITING' if 'WAITING' in st else ('UNMANAGED' if st in ('UNMANAGED','UNKNOWN') else 'WAITING'))))) %}
<div class="stage-row">
  <span class="stage-pill stage-{{ stclass }}">{{ st }}</span>
  {% if p.r %} · Initial R: {{ p.r.original_r }} · <b>{{ '%+.2f'|format(p.r.current_gain_r) if p.r.current_gain_r is not none else '?' }}R</b>
  {% elif p.target %} · TP dist: {{ p.target.distance }}
  {% endif %}
</div>
{% set pct = namespace(v=None) %}
{% if p.target and p.target.progress is not none %}
  {% set pct.v = p.target.progress * 100 %}
{% elif p.r and p.r.current_gain_r is not none and p.ladder.next_threshold %}
  {% set pct.v = (p.r.current_gain_r / p.ladder.next_threshold) * 100 %}
{% elif st == 'RUNNER' %}
  {% set pct.v = 100 %}
{% endif %}
{% if pct.v is not none %}
{% set pctclamped = 0 if pct.v < 0 else (100 if pct.v > 100 else pct.v) %}
<div class="progress-track"><div class="progress-fill" style="width:{{ '%.0f'|format(pctclamped) }}%"></div></div>
{% endif %}
{% if p.ladder.next_label %}
<div class="next-action">➡ Next: {{ p.ladder.next_threshold }}{{ 'R' if p.r else '' }} → {{ p.ladder.next_label }}{% if p.ladder.distance_to_next is not none %} (Δ{{ '%.2f'|format(p.ladder.distance_to_next) }}){% endif %}</div>
{% elif st == 'RUNNER' %}
<div class="next-action">➡ Runner active{% if p.ladder.stage_note %} — {{ p.ladder.stage_note }}{% endif %}</div>
{% elif st in ('UNMANAGED','UNKNOWN') %}
<div class="next-action" style="color:var(--muted)">{{ 'No valid SL/reference available' if p.management=='unmanaged' else 'Not enough data yet' }}</div>
{% endif %}
{% if acct_code != 'ORACLE' and is_owner %}
<div class="ctrl-row">
<button type="button" class="ctrl-btn close-btn" data-account="{{ acct_code }}" data-ticket="{{ p.ticket }}" data-symbol="{{ p.symbol }}" disabled>Close</button>
<button type="button" class="ctrl-btn be-btn" data-account="{{ acct_code }}" data-ticket="{{ p.ticket }}" data-symbol="{{ p.symbol }}" disabled>Breakeven</button>
</div>
{% endif %}
</div>
{% endfor %}
</div>
{% endif %}
{% endfor %}
{% if total_positions == 0 %}<div class="empty-note">No open positions across any account</div>{% endif %}
</div>
</details>

<details class="section" open>
<summary>🤖 Bot Health ({{ bot_health|length }} instance(s))</summary>
<div class="section-body">
{% if risk_halt_detail.active %}
<div class="halt-banner">🛑 RISK_HALT: ACTIVE — drawdown {{ risk_halt_detail.drawdown_pct }}% (threshold {{ risk_halt_detail.threshold_pct }}%) · baseline ${{ risk_halt_detail.baseline }} · current ${{ risk_halt_detail.current_equity }} · resets {{ risk_halt_detail.next_reset }}</div>
{% else %}
<div class="chip-row"><span class="chip">🟢 RISK_HALT: INACTIVE</span></div>
{% endif %}
<div class="chip-row" style="margin-top:6px">
<span class="chip">Trading-capable: {{ bot_health_summary.trading_capable }}</span>
<span class="chip">Allowed to enter: {{ bot_health_summary.allowed_to_enter }}</span>
<span class="chip">Blocked by RISK_HALT: {{ bot_health_summary.blocked_by_halt }}</span>
<span class="chip">Blocked manual: {{ bot_health_summary.blocked_manual }}</span>
<span class="chip">Protection: {{ bot_health_summary.protection }}</span>
<span class="chip">Watch/Research: {{ bot_health_summary.watch_research }}</span>
<span class="chip">{{ '🟡' if bot_health_summary.stale else '' }} Stale: {{ bot_health_summary.stale }}</span>
<span class="chip">{{ '⚪' if bot_health_summary.unknown else '' }} Unknown: {{ bot_health_summary.unknown }}</span>
</div>
{% if manual_entry_blocks %}
<div class="chip-row" style="margin-top:6px">
{% for b in manual_entry_blocks %}
<span class="chip">🛑 {{ b.bot }} {{ b.account }}: {{ b.status }}</span>
{% endfor %}
</div>
{% endif %}
<div style="overflow-x:auto; margin-top:8px">
<table class="mono-table">
<thead><tr><th>Bot</th><th>Account</th><th>PID</th><th>State</th><th>Last Heartbeat</th><th>Role</th><th>Can Trade?</th><th>Manual Block</th><th>Execution Eligibility</th></tr></thead>
<tbody>
{% for b in bot_health %}
<tr>
<td>{{ b.bot }}</td>
<td>{{ b.account }}</td>
<td>{{ b.pid }}</td>
<td>{{ '🟢' if b.state == 'RUNNING' else ('🟡' if b.state == 'STALE' else ('🔴' if b.state == 'STOPPED' else '⚪')) }} {{ b.state }}</td>
<td>{{ b.heartbeat_age }}</td>
<td>{{ b.role }}</td>
<td>{{ b.can_trade }}</td>
<td>{{ b.manual_block_reason or '—' }}</td>
<td>{{ b.eligibility or '—' }}</td>
</tr>
{% endfor %}
{% if not bot_health %}<tr><td colspan="9">No registered bots detected</td></tr>{% endif %}
</tbody>
</table>
</div>
{% if oracle_meta %}
<div class="chip-row" style="margin-top:8px">
<span class="chip">{{ '🟢' if oracle_meta.bybit_shield_alive else ('⚪' if oracle_meta.bybit_shield_alive is none else '🔴') }} bybit_shield (Oracle)</span>
<span class="chip">{{ '🟢' if oracle_meta.ladder_guard_alive else ('⚪' if oracle_meta.ladder_guard_alive is none else '🔴') }} ladder_guard (Oracle)</span>
<span class="chip">{{ '🟢' if oracle_meta.tg_signal_bot_alive else ('⚪' if oracle_meta.tg_signal_bot_alive is none else '🔴') }} tg_signal_bot (Oracle)</span>
<span class="chip">⚪ liquidity_sweep (Oracle stopped by design)</span>
</div>
{% endif %}
<div class="footnote" style="padding:8px 0 0 0">Local process table only — Oracle bots shown above as simple alive/dead chips (their own read-only API doesn't expose per-instance PID/heartbeat the same way). PID = wmic process match; heartbeat = same heartbeat_*.json files used elsewhere in this dashboard; RISK_HALT current equity summed live from the accounts already fetched this cycle. Zero process control performed by this section.</div>
</div>
</details>

<details class="section">
<summary>🧠 Claude Monitoring</summary>
<div class="section-body">
<div class="chip-row">
<span class="chip">{{ '🟢' if claude_loop.heartbeat.ok else '🔴' }} Heartbeat: {{ claude_loop.heartbeat.text }}</span>
<span class="chip">{{ '🟢' if claude_loop.claude_awake.ok else '🔴' }} Awake: {{ claude_loop.claude_awake.text }}</span>
<span class="chip">{{ '🟢' if claude_loop.analysis.ok else ('🟡' if claude_loop.claude_awake.ok else '🔴') }} Analysis: {{ claude_loop.analysis.text }}</span>
<span class="chip">{{ '🟢' if claude_loop.next_tick.ok else '🔴' }} Next tick: {{ claude_loop.next_tick.text }}</span>
</div>
<div class="footnote" style="padding:8px 0 0 0">Heartbeat ≠ Claude awake ≠ Analysis completed — each is written by a different process.</div>
</div>
</details>

<details class="section">
<summary>🔬 ML Research (London Breakout)</summary>
<div class="section-body">
{% if not ml_research.available %}
<div class="empty-note">Not Available — {{ ml_research.error or 'research_ml module unreachable' }} (read-only status card, no backend change).</div>
{% else %}
<div class="chip-row">
<span class="chip">{{ '🟢' if ml_research.status == 'READY_FOR_EXPERIMENT' else ('🟡' if ml_research.status == 'REVIEW' else '🔴') }} Status: {{ ml_research.status }}</span>
<span class="chip">Validated Eligible: {{ ml_research.validated_eligible_signals }} (of {{ ml_research.total_eligible_signals }} total eligible)</span>
<span class="chip">Wins: {{ ml_research.wins }} / Losses: {{ ml_research.losses }}</span>
<span class="chip">Coverage: {{ ml_research.first_eligible_signal_date or '—' }} → {{ ml_research.latest_eligible_signal_date or '—' }}</span>
<span class="chip">ML Trading Impact: {{ ml_research.ml_trading_impact }}</span>
</div>
<div class="footnote" style="padding:8px 0 0 0">{{ ml_research.reason }}. Observe-only research layer — this card cannot open/close/modify a trade, change SL/TP/lot, disable a strategy, or touch RISK_HALT.</div>
{% endif %}
</div>
</details>

<details class="section">
<summary>🔒 Autonomous Research Loop</summary>
<div class="section-body">
{% if not research_loop.available %}
<div class="empty-note">Not Available — {{ research_loop.error or 'autonomous_research_loop files unreachable' }} (read-only status card, no backend change).</div>
{% else %}
<div class="chip-row">
<span class="chip">{{ '🟢' if research_loop.status == 'RUNNING' else ('🔴' if research_loop.status == 'ERROR' else '⚪') }} Status: {{ research_loop.status }}</span>
<span class="chip">Active Families: {{ research_loop.active_families }} (of {{ research_loop.total_families }} registered)</span>
<span class="chip">Registered Protocols: {{ research_loop.registered_protocols }}</span>
<span class="chip">Last Run: {{ research_loop.last_run or '—' }}</span>
<span class="chip">Last Result: {{ research_loop.last_experiment_id or '—' }}{{ ' (' + research_loop.last_experiment_decision + ')' if research_loop.last_experiment_decision else '' }}</span>
<span class="chip" style="{{ 'color:var(--bad);font-weight:700' if research_loop.protocol_errors else '' }}">Protocol Errors: {{ research_loop.protocol_errors }}</span>
</div>
<div class="footnote" style="padding:8px 0 0 0">DORMANT by design — both registries ship empty; no family or protocol is auto-registered. Read-only status card: cannot start a research cycle, register a family/protocol, or change anything here.</div>
{% endif %}
</div>
</details>

<details class="section">
<summary>☁️ Oracle</summary>
<div class="section-body">
<div class="chip-row">
<span class="chip">{{ oracle_conn.emoji }} {{ oracle_conn.label }}</span>
<span class="chip">Last update: {{ (oracle_conn.last_success_ago ~ 's ago') if oracle_conn.last_success_ago is not none else 'never' }}</span>
{% if oracle_conn.next_retry_in is not none %}<span class="chip">Retry in {{ oracle_conn.next_retry_in }}s</span>{% endif %}
<span class="chip">{{ ([oracle_meta.bybit_shield_alive, oracle_meta.ladder_guard_alive, oracle_meta.tg_signal_bot_alive]|select('equalto', true)|list|length) if oracle_meta else 0 }}/3 required bots alive</span>
</div>
</div>
</details>

<details class="section">
<summary>📜 Logs</summary>
<div class="section-body">
<div class="empty-note">Not Available — raw log streaming is not exposed by the current read-only API (frontend-only change, no backend touch made).</div>
</div>
</details>

</div>

<p class="footnote">Auto-refreshes every 30s. Strictly read-only — no trades placed, modified, or closed from this page.</p>
<script>setTimeout(()=>location.reload(), 30000)</script>
{% if is_owner %}
<script>
// 2026-07-28 (Ahmed's UI-only Control Layer request): this block never
// talks to port 5002 directly -- every call below goes to this same page's
// own /ui/control/* proxy routes (same-origin, port 5001), which in turn
// relay to control_api.py server-side. Buttons start `disabled` in the
// HTML above and stay disabled unless /ui/control/probe reports ENABLED
// (it will report DISABLED or OFFLINE against the shipped defaults, since
// CONTROL_LAYER_ENABLED is False and control_api.py isn't even running as
// a process yet). Nothing here can place, open, or modify an order --
// only close_position / move_sl_to_breakeven / close_all, matching
// control_api.py's own route set exactly.
(function () {
  function setControlStatus(text, cls) {
    var el = document.getElementById('control-status');
    if (!el) return;
    el.textContent = text;
    el.className = 'ctrl-status ' + (cls || 'off');
  }
  function setButtonsEnabled(enabled) {
    document.querySelectorAll('.ctrl-btn').forEach(function (b) { b.disabled = !enabled; });
  }
  function genCommandId() {
    if (window.crypto && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
    // Fallback only -- every modern browser Ahmed uses has randomUUID.
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0, v = c === 'x' ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }
  function postJSON(url, body) {
    return fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        return { status: r.status, body: d };
      });
    });
  }
  function probeControlLayer() {
    fetch('/ui/control/probe').then(function (r) { return r.json(); }).then(function (d) {
      if (d.probe === 'ENABLED') {
        setControlStatus('Control Layer: ENABLED', 'ok');
        setButtonsEnabled(true);
      } else if (d.probe === 'DISABLED') {
        setControlStatus('Control Layer: DISABLED — CONTROL_LAYER_ENABLED is False', 'off');
        setButtonsEnabled(false);
      } else {
        setControlStatus('Control Layer: OFFLINE — not running', 'off');
        setButtonsEnabled(false);
      }
    }).catch(function () {
      setControlStatus('Control Layer: OFFLINE — probe failed', 'off');
      setButtonsEnabled(false);
    });
  }

  document.addEventListener('click', function (ev) {
    var t = ev.target;
    if (!t || !t.classList) return;

    if (t.classList.contains('close-btn')) {
      if (t.disabled) return;
      var account = t.getAttribute('data-account'), ticket = t.getAttribute('data-ticket'), symbol = t.getAttribute('data-symbol');
      if (!confirm('Close ' + symbol + ' (ticket ' + ticket + ') on ' + account + '?\\n\\n' +
                   document.getElementById('control-status').textContent)) return;
      postJSON('/ui/control/close', { command_id: genCommandId(), account: account, ticket: parseInt(ticket, 10), symbol: symbol })
        .then(function (res) { alert('Close request (' + res.status + '): ' + JSON.stringify(res.body)); location.reload(); });

    } else if (t.classList.contains('be-btn')) {
      if (t.disabled) return;
      var account = t.getAttribute('data-account'), ticket = t.getAttribute('data-ticket'), symbol = t.getAttribute('data-symbol');
      if (!confirm('Move SL to breakeven for ' + symbol + ' (ticket ' + ticket + ') on ' + account + '?\\n\\n' +
                   document.getElementById('control-status').textContent)) return;
      postJSON('/ui/control/breakeven', { command_id: genCommandId(), account: account, ticket: parseInt(ticket, 10), symbol: symbol })
        .then(function (res) { alert('Breakeven request (' + res.status + '): ' + JSON.stringify(res.body)); location.reload(); });

    } else if (t.classList.contains('close-all-btn')) {
      if (t.disabled) return;
      var account = t.getAttribute('data-account');
      postJSON('/ui/control/close_all/prepare', { account: account }).then(function (res) {
        if (!res.body || res.body.ok !== true) {
          alert('Close-All prepare failed (' + res.status + '): ' + JSON.stringify(res.body));
          return;
        }
        var info = res.body;
        var typed = prompt(
          'Close ALL ' + info.count + ' position(s) on ' + account + ':\\n' + JSON.stringify(info.tickets) +
          '\\n\\nType the exact phrase below to confirm (anything else cancels):\\nCONFIRM CLOSE ALL', '');
        if (typed !== 'CONFIRM CLOSE ALL') {
          alert('Not confirmed — no action taken.');
          return;
        }
        postJSON('/ui/control/close_all/confirm', { snapshot_id: info.snapshot_id, confirm: typed })
          .then(function (res2) { alert('Close-All result (' + res2.status + '): ' + JSON.stringify(res2.body)); location.reload(); });
      });
    }
  });

  probeControlLayer();
})();
</script>
{% endif %}
</body></html>
"""


@app.route("/")
def index():
    with _cache_lock:
        accounts, bots, ts = _cache["accounts"], _cache["bots"], _cache["ts"]
        oracle_meta = _cache["oracle_meta"]
    age = round(time.time() - ts) if ts else None
    freshness = freshness_state(age)
    all_positions = [p for acc in accounts for p in acc.get("positions", [])]
    alerts = build_alerts(all_positions, oracle_meta, accounts)
    oracle_conn = oracle_connection_status(oracle_meta)
    owner = _is_owner(request)
    trade_history_ctx, _th_errors, _th_ts = _get_prepared_trade_history(days=TRADE_HISTORY_DAYS)
    deduped_trades = trade_history_ctx["deduped"]
    trade_history_total = trade_history_ctx["grand_total"]

    # 2026-08-19 (Ahmed's dashboard patch, small top summary): all derived
    # from data already fetched above (deduped_trades, accounts) -- no new
    # backend pipeline, no new MT5/Oracle call. Fees/Costs stays N/A: the
    # trade dicts this dashboard reads carry only the already-netted `pnl`
    # (commission+swap folded in by fetch_closed_trades), not those two
    # components separately, so a real figure isn't available here.
    today_realized_pl = trade_history_ctx["today_realized_pl"]
    week_net_pl = trade_history_ctx["week_net_pl"]
    current_floating_pl = sum((a.get("profit") or 0) for a in accounts if not a.get("error"))
    fees_costs_summary = pa.NOT_AVAILABLE
    top_losing_bot = trade_history_ctx["top_losing_bot"]
    risk_halt_detail = pa.risk_halt_detail(accounts)
    bot_health = bot_health_detail(accounts, risk_halt_detail.get("active") is not False)
    manual_entry_blocks = _manual_entry_block_rows()
    bot_health_summary = {
        "trading_capable": sum(1 for b in bot_health if b["role"] == "TRADING"),
        "allowed_to_enter": sum(1 for b in bot_health if b["role"] == "TRADING" and b["can_trade"] == "YES"),
        "blocked_by_halt": sum(1 for b in bot_health if b["role"] == "TRADING" and risk_halt_detail["active"]
                                and b["can_trade"] == "NO"),
        "blocked_manual": len(manual_entry_blocks),
        "protection": sum(1 for b in bot_health if b["role"] == "PROTECTION"),
        "watch_research": sum(1 for b in bot_health if b["role"] in ("SIGNAL/WATCH", "RESEARCH")),
        "stale": sum(1 for b in bot_health if b["state"] == "STALE"),
        "unknown": sum(1 for b in bot_health if b["state"] == "UNKNOWN"),
    }
    resp = make_response(render_template_string(
        PAGE, accounts=accounts, bots=bots, age=(age if age is not None else "..."),
        freshness=freshness, oracle_meta=oracle_meta, alerts=alerts, oracle_conn=oracle_conn,
        risk_halt_detail=risk_halt_detail, bot_health=bot_health, bot_health_summary=bot_health_summary,
        manual_entry_blocks=manual_entry_blocks,
        claude_loop=claude_loop_status(),
        ml_research=ml_research_status(),
        research_loop=autonomous_research_loop_status(),
        trade_history_total=trade_history_total,
        total_positions=len(all_positions),
        total_protected=sum(1 for p in all_positions if p["protection"] == "PROTECTED"),
        total_unprotected=sum(1 for p in all_positions if p["protection"] == "UNPROTECTED"),
        total_ladder_active=sum(1 for p in all_positions if p["ladder"]["stage"] not in ("UNMANAGED", "UNKNOWN")),
        account_codes=CONTROL_ACCOUNT_CODES,
        is_owner=owner,
        today_realized_pl=today_realized_pl, week_net_pl=week_net_pl,
        current_floating_pl=current_floating_pl, fees_costs_summary=fees_costs_summary,
        top_losing_bot=top_losing_bot,
    ))
    if owner and request.args.get("owner"):
        # only set/refresh the cookie on the explicit ?owner=... visit --
        # never on a plain cookie-based match, so a stolen cookie can't renew itself
        resp.set_cookie(OWNER_COOKIE_NAME, CONTROL_SECRET, max_age=OWNER_COOKIE_MAX_AGE,
                         httponly=True, samesite="Lax")
    return resp


@app.route("/api/status")
def api_status():
    with _cache_lock:
        return jsonify({"accounts": _cache["accounts"], "bots": _cache["bots"],
                         "oracle_meta": _cache["oracle_meta"], "updated": _cache["ts"]})


@app.route("/ping")
def ping():
    return "ok"


@app.route("/api/mt5")
def api_mt5():
    # ponytail: legacy BotAPI_Watchdog scheduled task (elevated, can't be disabled
    # without admin) force-kills this process every 5min when this route 404s.
    return jsonify({"mt5_connected": True})


# ---------------------------------------------------------------------------
# 2026-07-28 (Ahmed's UI-only Control Layer request): server-side proxy to
# control_api.py (port 5002). This dashboard process (port 5001) NEVER
# calls MT5 order-affecting functions and never will -- these routes only
# forward the browser's request to control_api.py's existing HTTP API using
# the stdlib (no `requests` dependency, matching this file's existing
# imports) and hand back whatever control_api.py returns. This exists
# solely to dodge a same-origin/CORS problem in the browser (the page is
# served from :5001, control_api.py listens on :5002) without editing
# control_api.py to add CORS headers -- control_api.py, mt5_control_state.py,
# mt5_executor.py and control_config.py are untouched by this change.
# CONTROL_LAYER_ENABLED (False) and DRY_RUN (True) are unaffected here --
# this file has no way to change either; it just relays HTTP calls and
# control_api.py's own before_request gate still runs first, every time.
# ---------------------------------------------------------------------------

CONTROL_API_BASE = "http://127.0.0.1:5002"  # 2026-08-14: "localhost" was stalling ~2s
# per request (full CONTROL_API_PROBE_TIMEOUT_SEC every time) -- classic Windows
# IPv6-loopback-first-then-fallback delay. 127.0.0.1 skips resolution entirely.
# shared secret gate (2026-08-01) -- control_api.py rejects any /api/control/*
# call missing this header, since ngrok tunnels this dashboard to the internet
# and its own routes have no auth of their own otherwise.
_CONTROL_SECRET_FILE = os.path.join(os.path.dirname(__file__), "control_secret.txt")
try:
    CONTROL_SECRET = open(_CONTROL_SECRET_FILE, encoding="utf-8").read().strip()
except FileNotFoundError:
    CONTROL_SECRET = None

# 2026-08-02 (Ahmed: shared this dashboard's ngrok link with someone else
# following his trading -- they must never see or reach the Close/Breakeven/
# Close-All controls, but the link itself can't change). Reuses the same
# CONTROL_SECRET already in control_secret.txt as an "owner key" -- no new
# secret to manage. Visit once with ?owner=<the control_secret.txt value> to
# set a long-lived cookie in that browser; every other visitor (including
# anyone who only ever had the plain ngrok link) gets a page with the whole
# control-status/buttons/JS block omitted entirely -- not just disabled, not
# present in the HTML at all -- and the /ui/control/* routes reject them
# server-side even if they somehow crafted a raw request.
OWNER_COOKIE_NAME = "dash_owner"
OWNER_COOKIE_MAX_AGE = 365 * 24 * 3600  # 1 year


def _is_owner(req) -> bool:
    if not CONTROL_SECRET:
        return False
    return req.args.get("owner") == CONTROL_SECRET or req.cookies.get(OWNER_COOKIE_NAME) == CONTROL_SECRET


CONTROL_API_TIMEOUT_SEC = 5       # mutating calls (close/breakeven/close_all)
CONTROL_API_PROBE_TIMEOUT_SEC = 2  # probe -- must never make the page feel slow

# Only these three MT5 accounts may ever reach control_api.py from this UI
# (see CONTROL_ACCOUNT_CODES above) -- the Oracle/Bybit-ccxt account is
# out of scope and enforced here server-side, not just hidden in the HTML.
UI_ALLOWED_CONTROL_ACCOUNTS = set(CONTROL_ACCOUNT_CODES.values())


def _proxy_control(method, path, body=None, timeout=CONTROL_API_TIMEOUT_SEC):
    """Forward one HTTP request to control_api.py and relay back exactly
    what it said. Returns (status_code, json_dict). status_code is None
    only when control_api.py could not be reached at all (connection
    refused / timeout -- i.e. it isn't running as a process, which is this
    backend's actual current shipped state); that is distinct from a 503
    response, which means the process IS running but CONTROL_LAYER_ENABLED
    is False. Never raises -- every failure mode is turned into a JSON
    dict so callers (and the browser) always get valid JSON, never a raw
    500 from this proxy itself."""
    url = CONTROL_API_BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if CONTROL_SECRET:
        headers["X-Control-Secret"] = CONTROL_SECRET
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, {"ok": False, "error": "non-JSON response from control_api"}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"ok": False, "error": f"control_api returned HTTP {e.code}"}
    except Exception as e:
        return None, {"ok": False, "error": f"control_api unreachable (OFFLINE): {e}"}


@app.route("/ui/control/probe")
def ui_control_probe():
    """GETs a command_id that can never exist, purely to classify
    control_api.py's current reachability -- no dedicated status/health
    route exists there (and none may be added per this task's constraints).
    503 -> DISABLED (process up, CONTROL_LAYER_ENABLED is False).
    unreachable -> OFFLINE (process not running at all -- the expected,
    current shipped state). anything else (e.g. 404 unknown command_id)
    -> ENABLED (the gate is open)."""
    status, body = _proxy_control(
        "GET", "/api/control/status/00000000-0000-0000-0000-000000000000",
        timeout=CONTROL_API_PROBE_TIMEOUT_SEC)
    if status is None:
        return jsonify({"probe": "OFFLINE", "detail": body.get("error")})
    if status == 503:
        return jsonify({"probe": "DISABLED", "detail": body.get("error")})
    return jsonify({"probe": "ENABLED", "detail": body})


def _require_owner():
    """None if the request's owner cookie matches -- otherwise the (response,
    status) to return immediately. Server-side twin of the template's
    {% if is_owner %} guard: someone who only ever had the plain ngrok link
    never gets this cookie, so a raw request to these routes is rejected the
    same as the buttons being absent from their page."""
    if not _is_owner(request):
        return jsonify({"ok": False, "error": "not authorized"}), 403
    return None


@app.route("/ui/control/close", methods=["POST"])
def ui_control_close():
    denied = _require_owner()
    if denied:
        return denied
    body = request.get_json(force=True, silent=True) or {}
    if body.get("account") not in UI_ALLOWED_CONTROL_ACCOUNTS:
        return jsonify({"ok": False, "error": "account not permitted from this UI"}), 400
    status, resp_body = _proxy_control("POST", "/api/control/close", body)
    return jsonify(resp_body), (status if status is not None else 502)


@app.route("/ui/control/breakeven", methods=["POST"])
def ui_control_breakeven():
    denied = _require_owner()
    if denied:
        return denied
    body = request.get_json(force=True, silent=True) or {}
    if body.get("account") not in UI_ALLOWED_CONTROL_ACCOUNTS:
        return jsonify({"ok": False, "error": "account not permitted from this UI"}), 400
    status, resp_body = _proxy_control("POST", "/api/control/breakeven", body)
    return jsonify(resp_body), (status if status is not None else 502)


@app.route("/ui/control/close_all/prepare", methods=["POST"])
def ui_control_close_all_prepare():
    denied = _require_owner()
    if denied:
        return denied
    body = request.get_json(force=True, silent=True) or {}
    if body.get("account") not in UI_ALLOWED_CONTROL_ACCOUNTS:
        return jsonify({"ok": False, "error": "account not permitted from this UI"}), 400
    status, resp_body = _proxy_control("POST", "/api/control/close_all/prepare", body)
    return jsonify(resp_body), (status if status is not None else 502)


@app.route("/ui/control/close_all/confirm", methods=["POST"])
def ui_control_close_all_confirm():
    # No account field in this call (control_api.py's confirm route takes
    # only snapshot_id + the literal confirm string -- the account was
    # already fixed server-side at prepare time and lives inside the
    # snapshot control_api.py holds), so there's nothing account-scoped to
    # validate here beyond what control_api.py itself already enforces.
    denied = _require_owner()
    if denied:
        return denied
    body = request.get_json(force=True, silent=True) or {}
    status, resp_body = _proxy_control("POST", "/api/control/close_all/confirm", body)
    return jsonify(resp_body), (status if status is not None else 502)


# ---------------------------------------------------------------------------
# 2026-08-07 (Ahmed's Portfolio Analytics/Risk/Reports request): read-only
# historical performance pages. NOT a new polling loop -- computed lazily
# on request, cached for ANALYTICS_TTL_SEC so repeated page loads/refreshes
# don't hammer MT5 history queries, same spirit as the existing refresh_loop
# cache but request-triggered, not background-scheduled. No existing route
# touched, no trading call anywhere in this block.
# ---------------------------------------------------------------------------
ANALYTICS_TTL_SEC = 300
# 2026-08-08: keyed by `days` -- a single unkeyed slot meant a days=90 call
# (risk_page) and a days=180 call (analytics/reports) could silently hand
# each other the wrong window's data whenever both landed inside the same
# TTL period. One slot per distinct `days` value fixes that and is what
# lets the new days=TRADE_HISTORY_DAYS trades-page pull coexist safely.
_analytics_cache = {}
_analytics_lock = threading.Lock()
_prepared_trade_cache = {}
_prepared_trade_lock = threading.Lock()


def _get_trades(days=180):
    with _analytics_lock:
        slot = _analytics_cache.setdefault(days, {"ts": 0, "trades": [], "errors": {}})
        if time.time() - slot["ts"] > ANALYTICS_TTL_SEC:
            trades, errors = pa.fetch_all_closed_trades(days=days)
            slot["trades"] = trades
            slot["errors"] = errors
            slot["ts"] = time.time()
        return slot["trades"], slot["errors"], slot["ts"]


# 2026-08-22 (Ahmed: dashboard cold-cache latency ~16s complaint): background
# pre-warm for the same fetch _get_trades() does lazily. Refreshes each
# already-used `days` slot shortly BEFORE its TTL expires so a real request
# hits warm cache. Same _analytics_lock as _get_trades() -- a request landing
# mid-refresh just waits for the lock, it never triggers a second concurrent
# fetch. Only warms slots a real request already created (a cold process
# start still pays the fetch once, on the first visit, same as before).
_ANALYTICS_PREWARM_MARGIN_SEC = 30


def _analytics_prewarm_tick():
    for days in list(_analytics_cache.keys()):
        with _analytics_lock:
            slot = _analytics_cache.get(days)
            if not slot or time.time() - slot["ts"] <= ANALYTICS_TTL_SEC - _ANALYTICS_PREWARM_MARGIN_SEC:
                continue
            try:
                trades, errors = pa.fetch_all_closed_trades(days=days)
                slot["trades"], slot["errors"], slot["ts"] = trades, errors, time.time()
            except Exception:
                pass  # keep serving the last valid cache; don't blank it on a failed refresh


def _analytics_prewarm_loop():
    while True:
        time.sleep(10)
        _analytics_prewarm_tick()


if "--test" not in sys.argv and "--preflight" not in sys.argv:
    threading.Thread(target=_analytics_prewarm_loop, daemon=True).start()


def _build_trade_history_snapshot(raw_trades):
    """Pure read-only transform of one closed-trade snapshot.

    This keeps the expensive fetch in `_get_trades()` separate from the
    display-only work used by `/` and `/trades`, so the same already-fetched
    snapshot can be reused without recomputing dedup/enrichment/summary
    fields on every request.
    """
    deduped, dup_count = _dedup_trades(raw_trades)
    enriched = [_enrich_trade(t) for t in deduped]
    now_utc = datetime.now(timezone.utc)
    today_realized_pl = sum(t["pnl"] for t in deduped if t["exit_time"].date() == now_utc.date())
    week_cutoff = now_utc - timedelta(days=7)
    week_trades = [t for t in deduped if t["exit_time"] >= week_cutoff]
    week_net_pl = sum(t["pnl"] for t in week_trades)
    week_by_magic = {}
    for t in week_trades:
        if t["magic"] in pa.ACTIVE_MAGICS:
            week_by_magic[t["magic"]] = week_by_magic.get(t["magic"], 0.0) + t["pnl"]
    losing_bots = [(m, v) for m, v in week_by_magic.items() if v < 0]
    top_losing_bot = (pa.bot_name(min(losing_bots, key=lambda x: x[1])[0])
                      if losing_bots else pa.NOT_AVAILABLE)
    return {
        "deduped": deduped,
        "enriched": enriched,
        "dup_count": dup_count,
        "grand_total": len(enriched),
        "accounts_list": sorted({t["account"] for t in enriched}),
        "strategies_list": sorted({t["strategy"] for t in enriched}),
        "symbols_list": sorted({t["symbol"] for t in enriched}),
        "today_realized_pl": today_realized_pl,
        "week_net_pl": week_net_pl,
        "top_losing_bot": top_losing_bot,
    }


def _get_prepared_trade_history(days=None):
    if days is None:
        days = TRADE_HISTORY_DAYS
    raw_trades, errors, cache_ts = _get_trades(days=days)
    with _prepared_trade_lock:
        slot = _prepared_trade_cache.get(days)
        if not slot or slot.get("source_ts") != cache_ts:
            slot = {"source_ts": cache_ts, **_build_trade_history_snapshot(raw_trades)}
            _prepared_trade_cache[days] = slot
        return slot, errors, cache_ts


# ---------------------------------------------------------------------------
# 2026-08-08 (Trade History Dashboard). Reuses _get_trades()'s existing
# TTL cache (300s) -- zero new polling. Reuses pa.fetch_all_closed_trades()/
# pa.fetch_closed_trades() as the single source of truth for P&L (no
# parallel calculation). CLOSED trades only -- open positions are already
# shown on the main dashboard's Positions section, deliberately not
# duplicated here (Ahmed's explicit "لا تخلط" instruction).
# ---------------------------------------------------------------------------
TRADE_HISTORY_MAX_ROWS = 500  # hard cap on PAGE SIZE (not on total reachable history -- pagination reaches the rest)
# 2026-08-08 (pagination fix): verified live against all 3 MT5 accounts --
# true oldest deal found was 179 days back (BA account). 400 gives headroom
# so "oldest trade" pagination doesn't start silently clipping as accounts
# age past the old 180-day window. Separate cache slot from the days=180/90
# pulls used by Analytics/Risk/Reports -- doesn't change their behavior.
TRADE_HISTORY_DAYS = 400
TRADE_PAGE_SIZES = (50, 100, 250, 500)
TRADE_PAGE_SIZE_DEFAULT = 100


def _dedup_trades(trades):
    """Defensive, display-side only -- never touches the MT5 source.
    Keyed on (account, position_id), the same identifier MT5 itself
    treats as unique per round-trip. If a genuine duplicate is found
    (would indicate an upstream data problem, not expected), it's kept
    out of the displayed list but the count is surfaced, never silently
    hidden."""
    seen = set()
    unique, dup_count = [], 0
    for t in trades:
        key = (t["account"], t["position_id"])
        if key in seen:
            dup_count += 1
            continue
        seen.add(key)
        unique.append(t)
    return unique, dup_count


def _enrich_trade(t):
    e = dict(t)
    e["strategy"] = pa.strategy_attribution(t["magic"], t.get("comment", ""))
    e["result"] = "WIN" if t["pnl"] > 0 else ("LOSS" if t["pnl"] < 0 else "FLAT")
    e["duration_min"] = round((t["exit_time"] - t["entry_time"]).total_seconds() / 60, 1)
    e["pnl_pct"] = None  # NOT AVAILABLE -- would need account equity AT ENTRY TIME, which MT5 deal history doesn't carry; never estimated
    return e


def _filter_trades(trades, period=None, account=None, strategy=None, symbol=None,
                    direction=None, result=None, date_from=None, date_to=None, limit=None,
                    sort="desc"):
    from datetime import datetime, timedelta, timezone  # local import -- see _period_bounds()/claude_loop_status() for this file's established convention
    now = datetime.now(timezone.utc)
    out = trades
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        out = [t for t in out if t["exit_time"] >= start]
    elif period == "week":
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        out = [t for t in out if t["exit_time"] >= start]
    if date_from:
        out = [t for t in out if t["exit_time"] >= date_from]
    if date_to:
        out = [t for t in out if t["exit_time"] <= date_to]
    if account:
        out = [t for t in out if t["account"] == account]
    if strategy:
        out = [t for t in out if t["strategy"] == strategy]
    if symbol:
        out = [t for t in out if t["symbol"] == symbol]
    if direction:
        out = [t for t in out if t["side"] == direction]
    if result:
        out = [t for t in out if t["result"] == result]
    # deterministic tiebreak on position_id -- two trades can share an
    # exit_time to the second, and without a secondary key page N could
    # show a different order/rowset on every re-request
    out = sorted(out, key=lambda t: (t["exit_time"], t["position_id"]), reverse=(sort != "asc"))
    if limit:
        out = out[:limit]
    return out


def _paginate(items, page, per_page):
    """Clamps both page and per_page rather than erroring -- an out-of-range
    page number is a normal consequence of filters/page-size changing
    between requests, not a client bug worth a 400/500 for."""
    per_page = per_page if per_page in TRADE_PAGE_SIZES else TRADE_PAGE_SIZE_DEFAULT
    total = len(items)
    total_pages = max(1, -(-total // per_page)) if total else 1  # ceil div
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    return items[start:start + per_page], dict(
        page=page, per_page=per_page, total_pages=total_pages, filtered_total=total,
        showing_from=(start + 1) if total else 0, showing_to=min(start + per_page, total),
    )


def _trade_summary(trades):
    """Closed trades only -- callers must not mix this with open-position
    figures. Returned dict is explicitly labeled to prevent confusion at
    the render site too."""
    n = len(trades)
    if n == 0:
        return dict(scope="Closed Trades", total=0, wins=0, losses=0, flat=0, win_rate=None,
                    net_pl=0.0, avg_pl=0.0, best=None, worst=None)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] < 0)
    flat = n - wins - losses
    net_pl = sum(t["pnl"] for t in trades)
    best = max(trades, key=lambda t: t["pnl"])
    worst = min(trades, key=lambda t: t["pnl"])
    return dict(
        scope="Closed Trades", total=n, wins=wins, losses=losses, flat=flat,
        win_rate=round(wins / n * 100, 1) if n else None,
        net_pl=round(net_pl, 2), avg_pl=round(net_pl / n, 4),
        best=dict(symbol=best["symbol"], pnl=best["pnl"], exit_time=str(best["exit_time"])),
        worst=dict(symbol=worst["symbol"], pnl=worst["pnl"], exit_time=str(worst["exit_time"])),
    )


ANALYTICS_PAGE = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Portfolio Analytics</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--card2:#f8f9fb;--text:#1a1d21;--muted:#6b7280;--border:#e3e5e8;
  --good:#15803d;--good-bg:#dcfce7;--bad:#b91c1c;--bad-bg:#fee2e2;--wait:#1d4ed8;--wait-bg:#dbeafe;
  --warn:#b45309;--warn-bg:#fef3c7;--shadow:0 1px 3px rgba(0,0,0,.08)}
@media (prefers-color-scheme: dark){:root{--bg:#0f1115;--card:#181b20;--card2:#1f2329;--text:#e8eaed;
  --muted:#9aa1ac;--border:#2a2e35;--good:#4ade80;--good-bg:#0f2f1c;--bad:#f87171;--bad-bg:#3a1414;
  --wait:#7db2ff;--wait-bg:#122240;--warn:#fbbf24;--warn-bg:#3a2a0a;--shadow:0 1px 3px rgba(0,0,0,.4)}}
*{box-sizing:border-box} html,body{max-width:100%;overflow-x:hidden}
body{font-family:-apple-system,sans-serif;background:var(--bg);color:var(--text);margin:0;
  padding:0 0 24px 0;font-size:16px;line-height:1.45}
.wrap{padding:12px;max-width:960px;margin:0 auto}
.nav{display:flex;gap:10px;padding:12px;flex-wrap:wrap;max-width:960px;margin:0 auto}
.nav a{color:var(--wait);text-decoration:none;font-weight:700;font-size:14px;background:var(--card2);
  padding:8px 14px;border-radius:20px}
h1{font-size:19px;padding:0 12px}
.summary-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:14px}
@media (min-width:600px){.summary-grid{grid-template-columns:repeat(4,1fr)}}
.summary-cell{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:10px 12px;box-shadow:var(--shadow)}
.sc-label{font-size:12px;color:var(--muted);margin-bottom:2px}
.sc-value{font-size:18px;font-weight:700}
.sc-value.good{color:var(--good)} .sc-value.bad{color:var(--bad)}
.bot-card{background:var(--card);border-radius:14px;padding:14px;margin-bottom:12px;box-shadow:var(--shadow);
  border-left:5px solid var(--muted)}
.bot-card.good{border-left-color:var(--good)} .bot-card.bad{border-left-color:var(--bad)}
.bot-title{font-size:17px;font-weight:800;display:flex;justify-content:space-between;align-items:center}
.bot-magic{font-size:12px;color:var(--muted);font-weight:400}
.metric-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-top:10px}
@media (min-width:500px){.metric-grid{grid-template-columns:repeat(3,1fr)}}
.metric{background:var(--card2);border-radius:8px;padding:8px 10px}
.metric-label{font-size:11px;color:var(--muted)}
.metric-value{font-size:15px;font-weight:700}
.metric-value.good{color:var(--good)} .metric-value.bad{color:var(--bad)}
.equity-spark{display:flex;align-items:flex-end;gap:1px;height:40px;margin-top:10px;background:var(--card2);
  border-radius:8px;padding:4px}
.equity-bar{flex:1;background:var(--wait);border-radius:1px;min-height:2px}
.last-trade{font-size:12.5px;color:var(--muted);margin-top:8px}
.errors-note{font-size:12px;color:var(--bad);margin-top:6px}
.na{color:var(--muted);font-style:italic}
.footnote{color:var(--muted);font-size:12px;text-align:center;padding:10px 16px}
</style></head><body>
<div class="nav"><a href="/">🏠 Dashboard</a><a href="/analytics">📊 Analytics</a><a href="/risk">⚠️ Risk</a><a href="/reports">📄 Reports</a><a href="/trades">📜 Trades</a><a href="/monitor">🖥️ Monitor</a></div>
<h1>📊 Portfolio Analytics <span style="font-size:12px;color:var(--muted)">(read-only, last {{ days }} days, cached {{ cache_age }}s)</span></h1>
<div class="wrap">

{% if mt5_errors %}
<div class="errors-note">⚠️ MT5 history unavailable for: {{ mt5_errors.keys()|join(', ') }}</div>
{% endif %}

<h2 style="font-size:15px;padding:0 2px">Portfolio Summary (active strategies combined)</h2>
<div class="summary-grid">
<div class="summary-cell"><div class="sc-label">Total Equity</div><div class="sc-value">${{ '%.2f'|format(total_equity) }}</div></div>
<div class="summary-cell"><div class="sc-label">Total Balance</div><div class="sc-value">${{ '%.2f'|format(total_balance) }}</div></div>
<div class="summary-cell"><div class="sc-label">Total Floating</div><div class="sc-value {{ 'good' if total_pl >= 0 else 'bad' }}">{{ '%+.2f'|format(total_pl) }}</div></div>
<div class="summary-cell"><div class="sc-label">Total Closed Profit</div><div class="sc-value {{ 'good' if portfolio.net_profit >= 0 else 'bad' }}">${{ portfolio.net_profit }}</div></div>
<div class="summary-cell"><div class="sc-label">Running Bots</div><div class="sc-value">{{ bots|length }}</div></div>
<div class="summary-cell"><div class="sc-label">Active Strategies</div><div class="sc-value">{{ active_strategy_count }}</div></div>
<div class="summary-cell"><div class="sc-label">Open Positions</div><div class="sc-value">{{ total_positions }}</div></div>
<div class="summary-cell"><div class="sc-label">Protected %</div><div class="sc-value {{ 'good' if total_unprotected == 0 else 'bad' }}">{{ '%.0f'|format(protected_pct) }}%</div></div>
<div class="summary-cell"><div class="sc-label">Max Portfolio DD</div><div class="sc-value bad">${{ portfolio.max_drawdown }}</div></div>
<div class="summary-cell"><div class="sc-label">Today's P/L</div><div class="sc-value {{ 'good' if today_pl >= 0 else 'bad' }}">{{ '%+.2f'|format(today_pl) }}</div></div>
<div class="summary-cell"><div class="sc-label">This Week</div><div class="sc-value {{ 'good' if week_pl >= 0 else 'bad' }}">{{ '%+.2f'|format(week_pl) }}</div></div>
<div class="summary-cell"><div class="sc-label">This Month</div><div class="sc-value {{ 'good' if month_pl >= 0 else 'bad' }}">{{ '%+.2f'|format(month_pl) }}</div></div>
</div>

<h2 style="font-size:15px;padding:0 2px">Per-Bot Performance</h2>
{% for row in bot_rows %}
<div class="bot-card {{ 'good' if row.m.net_profit >= 0 else 'bad' }}">
<div class="bot-title"><span>{{ row.name }} {% if not row.active %}<span class="bot-magic">(inactive)</span>{% endif %}</span><span class="bot-magic">magic {{ row.magic }}</span></div>
<div class="metric-grid">
<div class="metric"><div class="metric-label">Net Profit</div><div class="metric-value {{ 'good' if row.m.net_profit>=0 else 'bad' }}">${{ row.m.net_profit }}</div></div>
<div class="metric"><div class="metric-label">Gross Profit</div><div class="metric-value good">${{ row.m.gross_profit }}</div></div>
<div class="metric"><div class="metric-label">Gross Loss</div><div class="metric-value bad">${{ row.m.gross_loss }}</div></div>
<div class="metric"><div class="metric-label">Win Rate</div><div class="metric-value">{{ row.m.win_rate }}%</div></div>
<div class="metric"><div class="metric-label">Profit Factor</div><div class="metric-value">{{ row.m.profit_factor }}</div></div>
<div class="metric"><div class="metric-label">Expectancy</div><div class="metric-value">${{ row.m.expectancy }}</div></div>
<div class="metric"><div class="metric-label">Avg R</div><div class="metric-value na">{{ row.m.avg_r }}</div></div>
<div class="metric"><div class="metric-label">Avg Duration</div><div class="metric-value">{{ row.m.avg_duration_min if row.m.avg_duration_min != 'Not Available' else row.m.avg_duration_min }}{{ ' min' if row.m.avg_duration_min != 'Not Available' else '' }}</div></div>
<div class="metric"><div class="metric-label"># Trades (closed)</div><div class="metric-value">{{ row.m.num_trades }}</div></div>
<div class="metric"><div class="metric-label">Open Positions</div><div class="metric-value">{{ row.open_count }}</div></div>
<div class="metric"><div class="metric-label">Max Drawdown</div><div class="metric-value bad">${{ row.m.max_drawdown }}</div></div>
<div class="metric"><div class="metric-label">Last Heartbeat</div><div class="metric-value">{{ row.heartbeat }}</div></div>
</div>
{% if row.m.equity_curve %}
<div class="equity-spark">
{% set mx = (row.m.equity_curve|map('abs')|max) or 1 %}
{% for v in row.m.equity_curve[-60:] %}<div class="equity-bar" style="height:{{ (((v|abs)/mx)*100)|round(0,'floor')|int }}%;background:{{ 'var(--good)' if v>=0 else 'var(--bad)' }}"></div>{% endfor %}
</div>
{% endif %}
{% if row.m.last_trade %}
<div class="last-trade">Last trade: {{ row.m.last_trade.symbol }} {{ '%+.2f'|format(row.m.last_trade.pnl) }} on {{ row.m.last_trade.account }} — {{ row.m.last_trade.time[:16] }}</div>
{% else %}
<div class="last-trade na">No closed trades in the last {{ days }} days</div>
{% endif %}
{% if row.last_error %}<div class="errors-note">Last error: {{ row.last_error }}</div>{% endif %}
</div>
{% endfor %}

<h2 style="font-size:15px;padding:0 2px">🐢 BAA (Oracle/Bybit) bots</h2>
<div class="bot-card"><div class="na">Not Available — historical performance requires a new remote data pull from the Oracle server (out of scope: no new backend added). Current open positions ARE shown on the main dashboard.</div></div>

</div>
<p class="footnote">Read-only. No trades placed, modified, or closed from this page. Historical data cached {{ ANALYTICS_TTL_SEC }}s.</p>
</body></html>
""".replace("{{ ANALYTICS_TTL_SEC }}", str(ANALYTICS_TTL_SEC))


TRADES_PAGE = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Trade History</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--card2:#f8f9fb;--text:#1a1d21;--muted:#6b7280;--border:#e3e5e8;
  --good:#15803d;--good-bg:#dcfce7;--bad:#b91c1c;--bad-bg:#fee2e2;--wait:#1d4ed8;--wait-bg:#dbeafe;
  --warn:#b45309;--warn-bg:#fef3c7;--shadow:0 1px 3px rgba(0,0,0,.08)}
@media (prefers-color-scheme: dark){:root{--bg:#0f1115;--card:#181b20;--card2:#1f2329;--text:#e8eaed;
  --muted:#9aa1ac;--border:#2a2e35;--good:#4ade80;--good-bg:#0f2f1c;--bad:#f87171;--bad-bg:#3a1414;
  --wait:#7db2ff;--wait-bg:#122240;--warn:#fbbf24;--warn-bg:#3a2a0a;--shadow:0 1px 3px rgba(0,0,0,.4)}}
*{box-sizing:border-box} html,body{max-width:100%;overflow-x:hidden}
body{font-family:-apple-system,sans-serif;background:var(--bg);color:var(--text);margin:0;
  padding:0 0 24px 0;font-size:16px;line-height:1.45}
.wrap{padding:12px;max-width:960px;margin:0 auto}
.nav{display:flex;gap:10px;padding:12px;flex-wrap:wrap;max-width:960px;margin:0 auto}
.nav a{color:var(--wait);text-decoration:none;font-weight:700;font-size:14px;background:var(--card2);
  padding:8px 14px;border-radius:20px}
.nav a.active{background:var(--wait);color:#fff}
h1{font-size:19px;padding:0 12px}
.summary-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:14px}
@media (min-width:600px){.summary-grid{grid-template-columns:repeat(4,1fr)}}
.summary-cell{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:10px 12px;box-shadow:var(--shadow)}
.sc-label{font-size:12px;color:var(--muted);margin-bottom:2px}
.sc-value{font-size:18px;font-weight:700}
.sc-value.good{color:var(--good)} .sc-value.bad{color:var(--bad)}
.tabs{display:flex;gap:8px;margin:10px 0;flex-wrap:wrap}
.tabs a{padding:7px 14px;border-radius:16px;background:var(--card2);color:var(--text);text-decoration:none;
  font-size:13px;font-weight:600;border:1px solid var(--border)}
.tabs a.active{background:var(--wait);color:#fff;border-color:var(--wait)}
.filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px;background:var(--card2);padding:10px;border-radius:10px}
.filters select,.filters input{padding:5px 8px;border-radius:6px;border:1px solid var(--border);background:var(--card);color:var(--text);font-size:13px}
.filters button{padding:5px 12px;border-radius:6px;border:none;background:var(--wait);color:#fff;font-size:13px;font-weight:600}
.trade-row{background:var(--card);border-radius:10px;margin-bottom:6px;box-shadow:var(--shadow);border-left:4px solid var(--muted)}
.trade-row.win{border-left-color:var(--good)} .trade-row.loss{border-left-color:var(--bad)}
.trade-summary{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:10px 12px;cursor:pointer;font-size:13.5px}
.trade-summary .tsym{font-weight:700;min-width:80px}
.trade-summary .tpl{margin-left:auto;font-weight:700}
.trade-summary .tpl.good{color:var(--good)} .trade-summary .tpl.bad{color:var(--bad)}
.trade-detail{padding:0 12px 12px 12px;font-size:13px;color:var(--muted)}
.trade-detail .dgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:4px 12px}
@media (min-width:500px){.trade-detail .dgrid{grid-template-columns:repeat(3,1fr)}}
.chip{display:inline-block;background:var(--card2);border-radius:12px;padding:2px 8px;font-size:11px;margin-right:4px}
.na{color:var(--muted);font-style:italic}
.warn-note{background:var(--warn-bg);color:var(--warn);border-radius:10px;padding:8px 12px;font-size:13px;margin-bottom:10px}
.pager{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;
  background:var(--card);border-radius:10px;padding:10px 12px;margin:12px 0;box-shadow:var(--shadow)}
.pager-info{font-size:13px;color:var(--muted)}
.pager-links{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.pager-links a{font-size:13px;font-weight:700;color:var(--wait);text-decoration:none;background:var(--card2);
  padding:5px 10px;border-radius:8px}
.pager-links a.disabled{color:var(--muted);pointer-events:none;opacity:.5}
.pager-page{font-size:13px;color:var(--muted)}
.footnote{color:var(--muted);font-size:12px;text-align:center;padding:10px 16px}
</style></head><body>
<div class="nav"><a href="/">🏠 Dashboard</a><a href="/analytics">📊 Analytics</a><a href="/risk">⚠️ Risk</a><a href="/reports">📄 Reports</a><a href="/trades" class="active">📜 Trades</a><a href="/monitor">🖥️ Monitor</a></div>
<h1>📜 Trade History <span style="font-size:12px;color:var(--muted)">(read-only, closed trades, cached {{ cache_age }}s)</span></h1>
<div class="wrap">

<div class="warn-note" style="background:var(--wait-bg);color:var(--wait)">Total Closed Trades (all history): <b>{{ '{:,}'.format(grand_total) }}</b></div>

{% if mt5_errors %}
<div class="warn-note">⚠️ MT5 history unavailable for: {{ mt5_errors.keys()|join(', ') }}</div>
{% endif %}
{% if dup_count %}
<div class="warn-note">⚠️ {{ dup_count }} duplicate record(s) detected in source data and excluded from display (source untouched).</div>
{% endif %}

<h2 style="font-size:15px;padding:0 2px">Summary ({{ summary.scope }} — {{ view_label }}, current filtered view)</h2>
<div class="summary-grid">
<div class="summary-cell"><div class="sc-label">Total Trades</div><div class="sc-value">{{ summary.total }}</div></div>
<div class="summary-cell"><div class="sc-label">Win Rate</div><div class="sc-value">{{ (summary.win_rate ~ '%') if summary.win_rate is not none else 'N/A' }}</div></div>
<div class="summary-cell"><div class="sc-label">Wins / Losses</div><div class="sc-value">{{ summary.wins }} / {{ summary.losses }}</div></div>
<div class="summary-cell"><div class="sc-label">Net P/L</div><div class="sc-value {{ 'good' if summary.net_pl >= 0 else 'bad' }}">{{ '%+.2f'|format(summary.net_pl) }}</div></div>
<div class="summary-cell"><div class="sc-label">Avg P/L</div><div class="sc-value {{ 'good' if summary.avg_pl >= 0 else 'bad' }}">{{ '%+.4f'|format(summary.avg_pl) }}</div></div>
<div class="summary-cell"><div class="sc-label">Best Trade</div><div class="sc-value good">{{ ('%+.2f'|format(summary.best.pnl) ~ ' ' ~ summary.best.symbol) if summary.best else 'N/A' }}</div></div>
<div class="summary-cell"><div class="sc-label">Worst Trade</div><div class="sc-value bad">{{ ('%+.2f'|format(summary.worst.pnl) ~ ' ' ~ summary.worst.symbol) if summary.worst else 'N/A' }}</div></div>
<div class="summary-cell"><div class="sc-label">Open Positions</div><div class="sc-value">{{ open_positions_count }} <span style="font-size:10px;color:var(--muted)">(see Dashboard)</span></div></div>
</div>

<div class="tabs">
<a href="/trades?view=today" class="{{ 'active' if view=='today' else '' }}">Today</a>
<a href="/trades?view=week" class="{{ 'active' if view=='week' else '' }}">This Week</a>
<a href="/trades?view=recent" class="{{ 'active' if view=='recent' else '' }}">Recent</a>
<a href="/trades?view=all" class="{{ 'active' if view=='all' else '' }}">All History</a>
</div>

<form class="filters" method="get" action="/trades">
<input type="hidden" name="view" value="{{ view }}">
<select name="account"><option value="">All Accounts</option>{% for a in accounts_list %}<option value="{{ a }}" {{ 'selected' if a==account else '' }}>{{ a }}</option>{% endfor %}</select>
<select name="strategy"><option value="">All Strategies</option>{% for s in strategies_list %}<option value="{{ s }}" {{ 'selected' if s==strategy else '' }}>{{ s }}</option>{% endfor %}</select>
<select name="symbol"><option value="">All Symbols</option>{% for s in symbols_list %}<option value="{{ s }}" {{ 'selected' if s==symbol else '' }}>{{ s }}</option>{% endfor %}</select>
<select name="direction"><option value="">Buy/Sell</option><option value="buy" {{ 'selected' if direction=='buy' else '' }}>Buy</option><option value="sell" {{ 'selected' if direction=='sell' else '' }}>Sell</option></select>
<select name="result"><option value="">Win/Loss</option><option value="WIN" {{ 'selected' if result=='WIN' else '' }}>Win</option><option value="LOSS" {{ 'selected' if result=='LOSS' else '' }}>Loss</option></select>
{% if view != 'recent' %}
<input type="date" name="date_from" value="{{ date_from or '' }}" title="From date">
<input type="date" name="date_to" value="{{ date_to or '' }}" title="To date">
<select name="sort"><option value="desc" {{ 'selected' if sort=='desc' else '' }}>Newest First</option><option value="asc" {{ 'selected' if sort=='asc' else '' }}>Oldest First</option></select>
<select name="per_page">{% for ps in page_sizes %}<option value="{{ ps }}" {{ 'selected' if ps==per_page else '' }}>{{ ps }} / page</option>{% endfor %}</select>
{% endif %}
<button type="submit">Filter</button>
</form>

{% if trades|length == 0 %}
<div class="trade-row"><div class="trade-summary"><span class="na">No trades match this view/filter.</span></div></div>
{% endif %}
{% for t in trades %}
<div class="trade-row {{ 'win' if t.result=='WIN' else ('loss' if t.result=='LOSS' else '') }}">
<details>
<summary class="trade-summary">
<span class="tsym">{{ t.symbol }}</span>
<span class="chip">{{ t.side|upper }}</span>
<span class="chip">{{ t.strategy }}</span>
<span class="chip">{{ t.account }}</span>
<span style="font-size:12px;color:var(--muted)">{{ t.exit_time.strftime('%Y-%m-%d %H:%M') }} UTC</span>
<span class="tpl {{ 'good' if t.pnl>=0 else 'bad' }}">{{ '%+.2f'|format(t.pnl) }}</span>
</summary>
<div class="trade-detail">
<div class="dgrid">
<div>Entry: {{ t.entry_price }}</div>
<div>Exit: {{ t.exit_price }}</div>
<div>Volume: {{ t.volume }}</div>
<div>SL: {{ t.sl if t.sl is not none else 'N/A' }}</div>
<div>TP: {{ t.tp if t.tp is not none else 'N/A' }}</div>
<div>Duration: {{ t.duration_min }} min</div>
<div>Magic: {{ t.magic }}</div>
<div>Trade ID: {{ t.ticket if t.ticket is not none else 'N/A' }}</div>
<div>Position ID: {{ t.position_id }}</div>
<div>Exit Reason: {{ t.exit_reason_label }}</div>
<div>P/L %: {{ t.pnl_pct if t.pnl_pct is not none else 'N/A' }}</div>
<div>Comment: {{ t.comment if t.comment else 'N/A' }}</div>
<div>Result: {{ t.result }}</div>
</div>
</div>
</details>
</div>
{% endfor %}

{% if view != 'recent' and filtered_total > 0 %}
<div class="pager">
<span class="pager-info">Showing {{ showing_from }}–{{ showing_to }} of {{ filtered_total }}</span>
<span class="pager-links">
<a href="/trades?{{ base_qs }}&page=1" {{ 'class="disabled"' if page<=1 else '' }}>« First</a>
<a href="/trades?{{ base_qs }}&page={{ page-1 }}" {{ 'class="disabled"' if page<=1 else '' }}>‹ Previous</a>
<span class="pager-page">Page {{ page }} / {{ total_pages }}</span>
<a href="/trades?{{ base_qs }}&page={{ page+1 }}" {{ 'class="disabled"' if page>=total_pages else '' }}>Next ›</a>
<a href="/trades?{{ base_qs }}&page={{ total_pages }}" {{ 'class="disabled"' if page>=total_pages else '' }}>Last »</a>
</span>
</div>
{% endif %}

</div>
<p class="footnote">Read-only. No trades placed, modified, or closed from this page. Source: MT5 history via portfolio_analytics.py, cached {{ ANALYTICS_TTL_SEC }}s. Oracle/BAA trade history: Not Available (no new backend added).</p>
</body></html>
""".replace("{{ ANALYTICS_TTL_SEC }}", str(ANALYTICS_TTL_SEC))


RISK_PAGE = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Portfolio Risk</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--card2:#f8f9fb;--text:#1a1d21;--muted:#6b7280;--border:#e3e5e8;
  --good:#15803d;--good-bg:#dcfce7;--bad:#b91c1c;--bad-bg:#fee2e2;--wait:#1d4ed8;--wait-bg:#dbeafe;
  --warn:#b45309;--warn-bg:#fef3c7;--shadow:0 1px 3px rgba(0,0,0,.08)}
@media (prefers-color-scheme: dark){:root{--bg:#0f1115;--card:#181b20;--card2:#1f2329;--text:#e8eaed;
  --muted:#9aa1ac;--border:#2a2e35;--good:#4ade80;--good-bg:#0f2f1c;--bad:#f87171;--bad-bg:#3a1414;
  --wait:#7db2ff;--wait-bg:#122240;--warn:#fbbf24;--warn-bg:#3a2a0a;--shadow:0 1px 3px rgba(0,0,0,.4)}}
*{box-sizing:border-box} html,body{max-width:100%;overflow-x:hidden}
body{font-family:-apple-system,sans-serif;background:var(--bg);color:var(--text);margin:0;
  padding:0 0 24px 0;font-size:16px;line-height:1.45}
.wrap{padding:12px;max-width:960px;margin:0 auto}
.nav{display:flex;gap:10px;padding:12px;flex-wrap:wrap;max-width:960px;margin:0 auto}
.nav a{color:var(--wait);text-decoration:none;font-weight:700;font-size:14px;background:var(--card2);
  padding:8px 14px;border-radius:20px}
h1{font-size:19px;padding:0 12px} h2{font-size:15px;padding:0 2px}
.heat-row{display:flex;align-items:center;gap:10px;background:var(--card);border-radius:10px;
  padding:10px 12px;margin-bottom:8px;box-shadow:var(--shadow)}
.heat-name{flex:1;font-weight:700;font-size:14px}
.heat-bar-track{flex:2;background:var(--card2);border-radius:6px;height:14px;overflow:hidden}
.heat-bar-fill{height:100%;background:linear-gradient(90deg,var(--wait),var(--warn),var(--bad))}
.heat-val{width:70px;text-align:right;font-size:13px;color:var(--muted)}
.summary-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:14px}
@media (min-width:600px){.summary-grid{grid-template-columns:repeat(4,1fr)}}
.summary-cell{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:10px 12px;box-shadow:var(--shadow)}
.sc-label{font-size:12px;color:var(--muted);margin-bottom:2px}
.sc-value{font-size:18px;font-weight:700}
.corr-item{background:var(--card);border-radius:8px;padding:8px 12px;margin-bottom:6px;font-size:13px;
  display:flex;justify-content:space-between}
.na{color:var(--muted);font-style:italic}
.footnote{color:var(--muted);font-size:12px;text-align:center;padding:10px 16px}
</style></head><body>
<div class="nav"><a href="/">🏠 Dashboard</a><a href="/analytics">📊 Analytics</a><a href="/risk">⚠️ Risk</a><a href="/reports">📄 Reports</a><a href="/trades">📜 Trades</a><a href="/monitor">🖥️ Monitor</a></div>
<h1>⚠️ Portfolio Risk (Read-Only)</h1>
<div class="wrap">

<div class="summary-grid">
<div class="summary-cell"><div class="sc-label">Open Positions</div><div class="sc-value">{{ risk.total_positions }}</div></div>
<div class="summary-cell"><div class="sc-label">Total Lot Open</div><div class="sc-value">{{ risk.total_lot }}</div></div>
<div class="summary-cell"><div class="sc-label">Symbols</div><div class="sc-value">{{ risk.by_symbol|length }}</div></div>
<div class="summary-cell"><div class="sc-label">Markets</div><div class="sc-value">{{ risk.by_market|length }}</div></div>
</div>

<h2>Exposure by Market</h2>
{% set max_market_lot = (risk.by_market.values()|map(attribute='lot')|max) if risk.by_market else 1 %}
{% for market, v in risk.by_market.items()|sort(attribute='1.lot', reverse=true) %}
<div class="heat-row">
  <div class="heat-name">{{ market }}</div>
  <div class="heat-bar-track"><div class="heat-bar-fill" style="width:{{ ((v.lot/max_market_lot)*100)|round(0,'floor')|int if max_market_lot else 0 }}%"></div></div>
  <div class="heat-val">{{ v.lot }} lot / {{ v.count }}</div>
</div>
{% endfor %}
{% if not risk.by_market %}<div class="na">No open positions</div>{% endif %}

<h2>Exposure by Symbol (Heat Map)</h2>
{% set max_symbol_lot = (risk.by_symbol.values()|map(attribute='lot')|max) if risk.by_symbol else 1 %}
{% for symbol, v in risk.by_symbol.items()|sort(attribute='1.lot', reverse=true) %}
<div class="heat-row">
  <div class="heat-name">{{ symbol }}</div>
  <div class="heat-bar-track"><div class="heat-bar-fill" style="width:{{ ((v.lot/max_symbol_lot)*100)|round(0,'floor')|int if max_symbol_lot else 0 }}%"></div></div>
  <div class="heat-val">{{ v.lot }} lot ({{ '%+.2f'|format(v.net_pnl) }})</div>
</div>
{% endfor %}
{% if not risk.by_symbol %}<div class="na">No open positions</div>{% endif %}

<h2>Trades per Bot (open positions right now)</h2>
{% for bot, v in risk.by_bot.items()|sort(attribute='1.count', reverse=true) %}
<div class="corr-item"><span>{{ bot }}</span><span>{{ v.count }} position(s), {{ v.lot }} lot</span></div>
{% endfor %}

<h2>Bot Correlation (daily P/L, active strategies, {{ days }}d)</h2>
{% for pair, val in correlations.items() %}
<div class="corr-item"><span>{{ pair }}</span><span class="{{ '' if val == 'Not Available' else 'na' if false else '' }}">{{ val }}</span></div>
{% endfor %}
{% if not correlations %}<div class="na">Not enough active strategies with overlapping trade history yet</div>{% endif %}

</div>
<p class="footnote">Read-only. Exposure computed from currently-open positions (same data as the main dashboard) — no new MT5 calls for this page.</p>
</body></html>
"""


REPORTS_PAGE = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Portfolio Reports</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--card2:#f8f9fb;--text:#1a1d21;--muted:#6b7280;--border:#e3e5e8;
  --good:#15803d;--good-bg:#dcfce7;--bad:#b91c1c;--bad-bg:#fee2e2;--wait:#1d4ed8;--wait-bg:#dbeafe;
  --shadow:0 1px 3px rgba(0,0,0,.08)}
@media (prefers-color-scheme: dark){:root{--bg:#0f1115;--card:#181b20;--card2:#1f2329;--text:#e8eaed;
  --muted:#9aa1ac;--border:#2a2e35;--good:#4ade80;--good-bg:#0f2f1c;--bad:#f87171;--bad-bg:#3a1414;
  --wait:#7db2ff;--wait-bg:#122240;--shadow:0 1px 3px rgba(0,0,0,.4)}}
*{box-sizing:border-box} html,body{max-width:100%;overflow-x:hidden}
body{font-family:-apple-system,sans-serif;background:var(--bg);color:var(--text);margin:0;
  padding:0 0 24px 0;font-size:16px;line-height:1.45}
.wrap{padding:12px;max-width:960px;margin:0 auto}
.nav{display:flex;gap:10px;padding:12px;flex-wrap:wrap;max-width:960px;margin:0 auto}
.nav a{color:var(--wait);text-decoration:none;font-weight:700;font-size:14px;background:var(--card2);
  padding:8px 14px;border-radius:20px}
.tabs{display:flex;gap:8px;padding:0 12px 12px;max-width:960px;margin:0 auto}
.tab{padding:8px 16px;border-radius:20px;background:var(--card2);font-size:13px;font-weight:700;color:var(--muted);text-decoration:none}
.tab.active{background:var(--wait-bg);color:var(--wait)}
h1{font-size:19px;padding:0 12px}
table{width:100%;border-collapse:collapse;background:var(--card);border-radius:12px;overflow:hidden;box-shadow:var(--shadow);margin-bottom:14px}
th,td{padding:10px;text-align:left;font-size:13px;border-bottom:1px solid var(--border)}
th{background:var(--card2);color:var(--muted);font-size:11px;text-transform:uppercase}
.good{color:var(--good)} .bad{color:var(--bad)}
.footnote{color:var(--muted);font-size:12px;text-align:center;padding:10px 16px}
</style></head><body>
<div class="nav"><a href="/">🏠 Dashboard</a><a href="/analytics">📊 Analytics</a><a href="/risk">⚠️ Risk</a><a href="/reports">📄 Reports</a><a href="/trades">📜 Trades</a><a href="/monitor">🖥️ Monitor</a></div>
<h1>📄 Auto Report — {{ period_label }}</h1>
<div class="tabs">
<a class="tab {{ 'active' if period=='daily' }}" href="/reports?period=daily">Daily</a>
<a class="tab {{ 'active' if period=='weekly' }}" href="/reports?period=weekly">Weekly</a>
<a class="tab {{ 'active' if period=='monthly' }}" href="/reports?period=monthly">Monthly</a>
</div>
<div class="wrap">
<table>
<tr><th>Bot</th><th>Trades</th><th>WR</th><th>PF</th><th>Net</th><th>Best</th><th>Worst</th></tr>
{% for row in rows %}
<tr>
<td>{{ row.name }}</td><td>{{ row.trades }}</td><td>{{ row.wr }}%</td><td>{{ row.pf }}</td>
<td class="{{ 'good' if row.net >= 0 else 'bad' }}">${{ row.net }}</td>
<td class="good">{{ row.best }}</td><td class="bad">{{ row.worst }}</td>
</tr>
{% endfor %}
{% if not rows %}<tr><td colspan="7">No trades in this period</td></tr>{% endif %}
</table>

<table>
<tr><th>Account</th><th>Balance</th><th>Equity</th><th>Floating P/L</th><th>Positions</th><th>Protected</th></tr>
{% for acc in accounts %}
<tr><td>{{ acc.name }}</td>
{% if acc.error %}<td colspan="5">⚠️ {{ acc.error }}</td>{% else %}
<td>${{ acc.balance }}</td><td>${{ acc.equity }}</td>
<td class="{{ 'good' if acc.profit >= 0 else 'bad' }}">{{ '%+.2f'|format(acc.profit) }}</td>
<td>{{ acc.positions|length }}</td><td>{{ acc.protected }}/{{ acc.positions|length }}</td>
{% endif %}
</tr>
{% endfor %}
</table>

<h2 style="font-size:15px">Best / Worst Strategy This Period</h2>
<p style="font-size:14px">🏆 Best: <b class="good">{{ best_bot }}</b> &nbsp;&nbsp; 💀 Worst: <b class="bad">{{ worst_bot }}</b></p>

<h2 style="font-size:15px">Operational Status</h2>
<p style="font-size:14px">Errors found: {{ error_count }} · Heartbeat: {{ 'All fresh' if all_heartbeats_ok else 'Check dashboard' }}</p>
</div>
<p class="footnote">Auto-generated on-demand from existing trade history and cached account data — no new backend service, no scheduled job.</p>
</body></html>
"""


def _period_bounds(period):
    now = datetime.now(timezone.utc) if False else __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    if period == "daily":
        return now.strftime("%Y-%m-%d"), "daily_pl", "Today"
    if period == "weekly":
        return now.strftime("%G-W%V"), "weekly_pl", "This Week"
    return now.strftime("%Y-%m"), "monthly_pl", "This Month"


@app.route("/analytics")
def analytics_page():
    from datetime import datetime as _dt, timezone as _tz
    trades, errors, cache_ts = _get_trades(days=180)
    by_magic = pa.group_by_magic(trades)
    active_trades = [t for t in trades if t["magic"] in pa.ACTIVE_MAGICS]
    portfolio = pa.compute_metrics(active_trades)

    with _cache_lock:
        accounts, bots = _cache["accounts"], _cache["bots"]

    totals = dict(eq=0.0, bal=0.0, pl=0.0)
    for acc in accounts:
        if not acc.get("error"):
            totals["eq"] += acc.get("equity") or 0
            totals["bal"] += acc.get("balance") or 0
            totals["pl"] += acc.get("profit") or 0
    total_positions = sum(len(a.get("positions", [])) for a in accounts)
    total_protected = sum(a.get("protected", 0) for a in accounts)
    total_unprotected = sum(a.get("unprotected", 0) for a in accounts)
    protected_pct = (total_protected / total_positions * 100) if total_positions else 100

    open_counts = {}
    for acc in accounts:
        for p in acc.get("positions", []):
            open_counts[p.get("strategy")] = open_counts.get(p.get("strategy"), 0) + 1

    LOG_BY_MAGIC = {
        990099: "MT5Bridge_ba_placeholder", 990101: "MT5Bridge_e2_placeholder",
        884400: "fx_signal_exec.log", 995502: "participation_pilot_btc_range.log",
        992201: "orb_eth_exness.log", 996600: "gold_btc_bot_ea.log",
    }
    HB_BY_MAGIC = {
        884400: "fx_signal_exec", 995502: "participation_pilot_btc_range",
        992201: "orb_eth_exness_EM", 996600: "gold_btc_bot_EA",
    }

    bot_rows = []
    for magic in sorted(pa.ACTIVE_MAGICS, key=lambda m: -len(by_magic.get(m, []))):
        ts = by_magic.get(magic, [])
        m = pa.compute_metrics(ts)
        hb = pa.last_heartbeat(HB_BY_MAGIC.get(magic, "")) if magic in HB_BY_MAGIC else None
        hb_text = f"{hb['age_sec']}s ago" if hb else pa.NOT_AVAILABLE
        log_file = LOG_BY_MAGIC.get(magic, "")
        err = pa.last_error(log_file) if log_file and "placeholder" not in log_file else None
        bot_rows.append(dict(
            name=pa.bot_name(magic), magic=magic, m=m, active=(magic in pa.ACTIVE_MAGICS),
            open_count=open_counts.get(pa.bot_name(magic), 0), heartbeat=hb_text, last_error=err,
        ))

    today_key, _, _ = _period_bounds("daily")
    week_key, _, _ = _period_bounds("weekly")
    month_key, _, _ = _period_bounds("monthly")
    today_pl = sum(pa.compute_metrics(by_magic.get(m, []))["daily_pl"].get(today_key, 0) for m in pa.ACTIVE_MAGICS)
    week_pl = sum(pa.compute_metrics(by_magic.get(m, []))["weekly_pl"].get(week_key, 0) for m in pa.ACTIVE_MAGICS)
    month_pl = sum(pa.compute_metrics(by_magic.get(m, []))["monthly_pl"].get(month_key, 0) for m in pa.ACTIVE_MAGICS)
    active_strategy_count = sum(1 for m in pa.ACTIVE_MAGICS if by_magic.get(m))

    resp = make_response(render_template_string(
        ANALYTICS_PAGE, days=180, cache_age=round(time.time() - cache_ts), mt5_errors=errors,
        total_equity=totals["eq"], total_balance=totals["bal"], total_pl=totals["pl"],
        portfolio=portfolio, bots=bots, active_strategy_count=active_strategy_count,
        total_positions=total_positions, total_unprotected=total_unprotected, protected_pct=protected_pct,
        today_pl=today_pl, week_pl=week_pl, month_pl=month_pl, bot_rows=bot_rows,
    ))
    return resp


def _int_arg(name, default):
    """Never lets a bad ?page=abc / ?per_page=-3 turn into a 500 -- falls
    back to the default instead, same fail-soft spirit as _paginate()'s
    clamping."""
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _date_arg(name):
    """Parses ?date_from=YYYY-MM-DD / ?date_to=YYYY-MM-DD. Invalid/missing
    -> None (filter simply not applied), never a 500 -- same fail-soft
    convention as _int_arg()."""
    from datetime import datetime, timezone  # local import -- see _filter_trades()/claude_loop_status() for this file's convention
    raw = request.args.get(name)
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    if name == "date_to":
        from datetime import timedelta
        dt = dt + timedelta(days=1) - timedelta(microseconds=1)  # inclusive of the whole day
    return dt


def _trades_view_common():
    """Shared logic for /trades and /api/trades -- one implementation,
    two renderers, so the two can never silently drift apart."""
    view = request.args.get("view", "recent")
    account = request.args.get("account") or None
    strategy = request.args.get("strategy") or None
    symbol = request.args.get("symbol") or None
    direction = request.args.get("direction") or None
    result = request.args.get("result") or None
    date_from_raw = request.args.get("date_from") or None
    date_to_raw = request.args.get("date_to") or None
    date_from = _date_arg("date_from")
    date_to = _date_arg("date_to")
    sort = request.args.get("sort", "desc")
    sort = sort if sort in ("asc", "desc") else "desc"
    page = _int_arg("page", 1)
    per_page = _int_arg("per_page", TRADE_PAGE_SIZE_DEFAULT)

    trade_history_ctx, errors, cache_ts = _get_prepared_trade_history(days=TRADE_HISTORY_DAYS)
    deduped = trade_history_ctx["deduped"]
    enriched = trade_history_ctx["enriched"]
    dup_count = trade_history_ctx["dup_count"]
    grand_total = trade_history_ctx["grand_total"]  # true historical count, unfiltered -- the "16,714+" figure, never capped

    period = view if view in ("today", "week") else None
    if view == "recent":
        # fixed most-recent-100 snapshot, not paginated -- per spec, "Recent" and
        # "All History" are deliberately different views, not the same list at two page sizes
        sort, page, per_page = "desc", 1, 100
    filtered = _filter_trades(enriched, period=period, account=account, strategy=strategy,
                               symbol=symbol, direction=direction, result=result, sort=sort,
                               date_from=(None if view == "recent" else date_from),
                               date_to=(None if view == "recent" else date_to),
                               limit=100 if view == "recent" else None)

    summary = _trade_summary(filtered)
    display_trades, page_meta = _paginate(filtered, page, per_page)

    with _cache_lock:
        accounts_data = _cache["accounts"]
    open_positions_count = sum(len(a.get("positions", [])) for a in accounts_data)

    accounts_list = trade_history_ctx["accounts_list"]
    strategies_list = trade_history_ctx["strategies_list"]
    symbols_list = trade_history_ctx["symbols_list"]

    # query string for pagination/sort links, excluding `page` itself (each link sets its own)
    qs_params = {k: v for k, v in dict(view=view, account=account, strategy=strategy, symbol=symbol,
                                        direction=direction, result=result, sort=sort,
                                        date_from=date_from_raw, date_to=date_to_raw,
                                        per_page=per_page).items() if v}
    base_qs = urllib.parse.urlencode(qs_params)

    view_labels = {"today": "Today", "week": "This Week", "recent": "Recent (last 100)", "all": "All History"}
    return dict(
        view=view, view_label=view_labels.get(view, view), account=account, strategy=strategy,
        symbol=symbol, direction=direction, result=result, sort=sort, trades=display_trades,
        date_from=date_from_raw, date_to=date_to_raw,
        grand_total=grand_total, base_qs=base_qs, page_sizes=TRADE_PAGE_SIZES,
        summary=summary, mt5_errors=errors, dup_count=dup_count, cache_ts=cache_ts,
        open_positions_count=open_positions_count, accounts_list=accounts_list,
        strategies_list=strategies_list, symbols_list=symbols_list,
        **page_meta,
    )


@app.route("/trades")
def trades_page():
    ctx = _trades_view_common()
    return render_template_string(
        TRADES_PAGE, cache_age=round(time.time() - ctx["cache_ts"]),
        **{k: v for k, v in ctx.items() if k != "cache_ts"},
    )


@app.route("/api/trades")
def api_trades():
    ctx = _trades_view_common()
    trades_json = [
        {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in t.items()}
        for t in ctx["trades"]
    ]
    return jsonify({
        "view": ctx["view"], "summary": ctx["summary"], "trades": trades_json,
        "grand_total": ctx["grand_total"], "filtered_total": ctx["filtered_total"],
        "page": ctx["page"], "per_page": ctx["per_page"], "total_pages": ctx["total_pages"],
        "showing_from": ctx["showing_from"], "showing_to": ctx["showing_to"], "sort": ctx["sort"],
        "date_from": ctx["date_from"], "date_to": ctx["date_to"],
        "mt5_errors": ctx["mt5_errors"], "duplicate_count": ctx["dup_count"],
        "open_positions_count": ctx["open_positions_count"],
        "cache_age_sec": round(time.time() - ctx["cache_ts"]),
    })


@app.route("/risk")
def risk_page():
    with _cache_lock:
        accounts = _cache["accounts"]
    risk = pa.portfolio_risk_snapshot(accounts)
    trades, errors, _ = _get_trades(days=90)
    by_magic = {m: t for m, t in pa.group_by_magic(trades).items() if m in pa.ACTIVE_MAGICS}
    correlations = pa.simple_daily_correlation(by_magic)
    return render_template_string(RISK_PAGE, risk=risk, correlations=correlations, days=90)


@app.route("/reports")
def reports_page():
    period = request.args.get("period", "daily")
    if period not in ("daily", "weekly", "monthly"):
        period = "daily"
    key, bucket_field, label = _period_bounds(period)
    trades, errors, _ = _get_trades(days=180)
    by_magic = pa.group_by_magic(trades)

    with _cache_lock:
        accounts = _cache["accounts"]

    rows = []
    for magic in pa.ACTIVE_MAGICS:
        ts = by_magic.get(magic, [])
        m = pa.compute_metrics(ts)
        period_trades = [t for t in ts if t["exit_time"].strftime(
            "%Y-%m-%d" if period == "daily" else ("%G-W%V" if period == "weekly" else "%Y-%m")) == key]
        if not period_trades:
            continue
        pm = pa.compute_metrics(period_trades)
        best = max(period_trades, key=lambda t: t["pnl"])
        worst = min(period_trades, key=lambda t: t["pnl"])
        rows.append(dict(name=pa.bot_name(magic), trades=pm["num_trades"], wr=pm["win_rate"],
                          pf=pm["profit_factor"], net=pm["net_profit"],
                          best=f"{best['symbol']} {best['pnl']:+.2f}", worst=f"{worst['symbol']} {worst['pnl']:+.2f}"))

    rows.sort(key=lambda r: -r["net"])
    best_bot = rows[0]["name"] if rows else pa.NOT_AVAILABLE
    worst_bot = rows[-1]["name"] if rows else pa.NOT_AVAILABLE

    error_count = sum(1 for magic in pa.ACTIVE_MAGICS
                       if pa.last_error({990099: "", 990101: "", 884400: "fx_signal_exec.log",
                                         995502: "participation_pilot_btc_range.log",
                                         992201: "orb_eth_exness.log", 996600: "gold_btc_bot_ea.log"}.get(magic, "")))
    all_heartbeats_ok = all(pa.last_heartbeat(n) and pa.last_heartbeat(n)["age_sec"] < 600
                             for n in ["fx_signal_exec", "participation_pilot_btc_range", "gold_btc_bot_EA"])

    return render_template_string(REPORTS_PAGE, period=period, period_label=label, rows=rows,
                                   accounts=accounts, best_bot=best_bot, worst_bot=worst_bot,
                                   error_count=error_count, all_heartbeats_ok=all_heartbeats_ok)


@app.route("/api/analytics")
def api_analytics():
    trades, errors, cache_ts = _get_trades(days=180)
    by_magic = pa.group_by_magic(trades)
    out = {pa.bot_name(m): pa.compute_metrics(t) for m, t in by_magic.items() if m in pa.ACTIVE_MAGICS}
    return jsonify({"bots": out, "mt5_errors": errors, "cached_age_sec": round(time.time() - cache_ts)})


@app.route("/api/risk")
def api_risk():
    with _cache_lock:
        accounts = _cache["accounts"]
    return jsonify(pa.portfolio_risk_snapshot(accounts))


ALERTS_PAGE = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Alerts</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--card2:#f8f9fb;--text:#1a1d21;--muted:#6b7280;--border:#e3e5e8;
  --good:#15803d;--good-bg:#dcfce7;--bad:#b91c1c;--bad-bg:#fee2e2;--wait:#1d4ed8;--wait-bg:#dbeafe;
  --warn:#b45309;--warn-bg:#fef3c7;--shadow:0 1px 3px rgba(0,0,0,.08)}
@media (prefers-color-scheme: dark){:root{--bg:#0f1115;--card:#181b20;--card2:#1f2329;--text:#e8eaed;
  --muted:#9aa1ac;--border:#2a2e35;--good:#4ade80;--good-bg:#0f2f1c;--bad:#f87171;--bad-bg:#3a1414;
  --wait:#7db2ff;--wait-bg:#122240;--warn:#fbbf24;--warn-bg:#3a2a0a;--shadow:0 1px 3px rgba(0,0,0,.4)}}
*{box-sizing:border-box} html,body{max-width:100%;overflow-x:hidden}
body{font-family:-apple-system,sans-serif;background:var(--bg);color:var(--text);margin:0;
  padding:0 0 24px 0;font-size:16px;line-height:1.45}
.wrap{padding:12px;max-width:960px;margin:0 auto}
.nav{display:flex;gap:10px;padding:12px;flex-wrap:wrap;max-width:960px;margin:0 auto}
.nav a{color:var(--wait);text-decoration:none;font-weight:700;font-size:14px;background:var(--card2);
  padding:8px 14px;border-radius:20px}
h1{font-size:19px;padding:0 12px} h2{font-size:15px;padding:0 2px}
.alert-card{background:var(--card);border-radius:12px;padding:12px 14px;margin-bottom:10px;box-shadow:var(--shadow);
  border-left:5px solid var(--muted)}
.alert-card.critical{border-left-color:var(--bad)}
.alert-card.warning{border-left-color:var(--warn)}
.alert-card.resolved{border-left-color:var(--good);opacity:.75}
.alert-msg{font-weight:700;font-size:14.5px}
.alert-meta{font-size:12px;color:var(--muted);margin-top:4px}
.status-badge{display:inline-flex;align-items:center;gap:5px;padding:3px 10px;border-radius:20px;font-size:11.5px;font-weight:800}
.status-badge.active{background:var(--bad-bg);color:var(--bad)}
.status-badge.resolved{background:var(--good-bg);color:var(--good)}
.hist-item{background:var(--card2);border-radius:8px;padding:8px 12px;margin-bottom:6px;font-size:12.5px}
.hist-item .ev-FIRED{color:var(--bad);font-weight:700}
.hist-item .ev-RESOLVED{color:var(--good);font-weight:700}
.hist-item .ev-REMINDER{color:var(--warn);font-weight:700}
.na{color:var(--muted);font-style:italic}
.footnote{color:var(--muted);font-size:12px;text-align:center;padding:10px 16px}
.tg-note{background:var(--warn-bg);color:var(--warn);border-radius:10px;padding:10px 12px;font-size:13px;margin-bottom:12px}
</style></head><body>
<div class="nav"><a href="/">🏠 Dashboard</a><a href="/analytics">📊 Analytics</a><a href="/risk">⚠️ Risk</a><a href="/reports">📄 Reports</a><a href="/trades">📜 Trades</a><a href="/alerts">🔔 Alerts</a><a href="/monitor">🖥️ Monitor</a></div>
<h1>🔔 Alerts</h1>
<div class="wrap">
{% if not tg_configured %}
<div class="tg-note">⚠️ Telegram delivery not configured in this environment (TG_TOKEN/TG_CHAT unset) -- alerts are being detected and logged here, but not pushed to Telegram yet.</div>
{% endif %}

<h2>Current Alerts ({{ active|length }})</h2>
{% for key, a in active.items() %}
<div class="alert-card {{ a.severity }}">
<div class="alert-msg">{{ a.message }}</div>
<div class="alert-meta">
<span class="status-badge active">ACTIVE</span>
Started {{ a.first_seen[:16] }} · Duration {{ durations.get(key, '?') }}
</div>
</div>
{% endfor %}
{% if not active %}<div class="na">No active alerts — all clear.</div>{% endif %}

<h2>Alert History (last 100)</h2>
{% for h in history %}
<div class="hist-item"><span class="ev-{{ h.event }}">{{ h.event }}</span> — {{ h.message }} <span class="na">({{ h.ts[:16] }})</span></div>
{% endfor %}
{% if not history %}<div class="na">No alert history yet.</div>{% endif %}

</div>
<p class="footnote">Read-only monitoring layer. Deduplicated every {{ dedup_min }} min while an issue stays active. No trading action taken from this page.</p>
</body></html>
"""


@app.route("/alerts")
def alerts_page():
    active, resolved, history = am.current_and_history()
    now = time.time()
    durations = {}
    for key, a in active.items():
        try:
            started = __import__("datetime").datetime.fromisoformat(a["first_seen"]).timestamp()
            mins = round((now - started) / 60)
            durations[key] = f"{mins}m" if mins < 60 else f"{mins // 60}h{mins % 60}m"
        except Exception:
            durations[key] = "?"
    return render_template_string(ALERTS_PAGE, active=active, history=history, durations=durations,
                                   # 2026-08-08: Telegram now relays through Oracle (no local
                                   # TG_TOKEN/TG_CHAT to check) -- always wired, delivery itself
                                   # can still fail (network/SSH), but that's a runtime outcome,
                                   # not a missing-config state to render differently for.
                                   tg_configured=True,
                                   dedup_min=am.DEDUP_SECONDS // 60)


@app.route("/api/alerts")
def api_alerts():
    active, resolved, history = am.current_and_history()
    return jsonify({"active": active, "resolved": resolved, "history": history})


MONITOR_PAGE = """
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Monitor</title>
<style>
:root{--bg:#f4f5f7;--card:#fff;--card2:#f8f9fb;--text:#1a1d21;--muted:#6b7280;--border:#e3e5e8;
  --good:#15803d;--good-bg:#dcfce7;--bad:#b91c1c;--bad-bg:#fee2e2;--wait:#1d4ed8;--wait-bg:#dbeafe;
  --warn:#b45309;--warn-bg:#fef3c7;--shadow:0 1px 3px rgba(0,0,0,.08)}
@media (prefers-color-scheme: dark){:root{--bg:#0f1115;--card:#181b20;--card2:#1f2329;--text:#e8eaed;
  --muted:#9aa1ac;--border:#2a2e35;--good:#4ade80;--good-bg:#0f2f1c;--bad:#f87171;--bad-bg:#3a1414;
  --wait:#7db2ff;--wait-bg:#122240;--warn:#fbbf24;--warn-bg:#3a2a0a;--shadow:0 1px 3px rgba(0,0,0,.4)}}
*{box-sizing:border-box} html,body{max-width:100%;overflow-x:hidden}
body{font-family:-apple-system,sans-serif;background:var(--bg);color:var(--text);margin:0;
  padding:0 0 24px 0;font-size:16px;line-height:1.45}
.wrap{padding:12px;max-width:1100px;margin:0 auto}
.nav{display:flex;gap:10px;padding:12px;flex-wrap:wrap;max-width:1100px;margin:0 auto}
.nav a{color:var(--wait);text-decoration:none;font-weight:700;font-size:14px;background:var(--card2);
  padding:8px 14px;border-radius:20px}
h1{font-size:19px;padding:0 12px} h2{font-size:15px;padding:12px 2px 6px 2px}
.summary-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:10px}
@media (min-width:600px){.summary-grid{grid-template-columns:repeat(4,1fr)}}
.summary-cell{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:10px 12px;box-shadow:var(--shadow)}
.sc-label{font-size:12px;color:var(--muted);margin-bottom:2px}
.sc-value{font-size:18px;font-weight:700}
.card{background:var(--card);border-radius:10px;padding:10px 12px;margin-bottom:8px;box-shadow:var(--shadow)}
.badge{display:inline-block;padding:3px 9px;border-radius:20px;font-size:11px;font-weight:700}
.b-good{background:var(--good-bg);color:var(--good)} .b-bad{background:var(--bad-bg);color:var(--bad)}
.b-warn{background:var(--warn-bg);color:var(--warn)} .b-wait{background:var(--wait-bg);color:var(--wait)}
.b-muted{background:var(--card2);color:var(--muted)}
.row{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border);font-size:13px}
.row:last-child{border-bottom:none}
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;color:var(--muted);font-weight:600;padding:6px 8px;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:6px 8px;border-bottom:1px solid var(--border);white-space:nowrap}
.na{color:var(--muted);font-style:italic}
.footnote{color:var(--muted);font-size:12px;text-align:center;padding:10px 16px}
.halt-banner{background:var(--bad-bg);color:var(--bad);border-radius:10px;padding:12px;margin-bottom:10px;font-weight:700;text-align:center}
.health-score{font-size:42px;font-weight:900;line-height:1}
.health-box{border:1px solid var(--border);border-radius:16px;padding:14px;background:var(--card);box-shadow:var(--shadow);margin-bottom:10px}
.details-grid{display:grid;grid-template-columns:1fr;gap:10px}
@media (min-width:780px){.details-grid{grid-template-columns:1fr 1fr}}
.component-card{background:var(--card);border:1px solid var(--border);border-radius:12px;margin-bottom:10px;overflow:hidden}
.component-title{padding:10px 12px;background:var(--card2);font-weight:800}
.small{font-size:12px;color:var(--muted)}
</style></head><body>
<div class="nav"><a href="/">🏠 Dashboard</a><a href="/analytics">📊 Analytics</a><a href="/risk">⚠️ Risk</a><a href="/reports">📄 Reports</a><a href="/trades">📜 Trades</a><a href="/monitor" class="active">🖥️ Monitor</a></div>
<h1>🖥️ Monitor (Read-Only)</h1>
<div class="wrap">

{% if risk_halt.halted %}
<div class="halt-banner">🛑 RISK_HALT ACTIVE since {{ risk_halt.since }}</div>
{% endif %}

<h2>🧭 FULL-DETAIL OBSERVABILITY</h2>
<div class="health-box">
  <div class="row"><span><b>Health Score</b></span><span class="health-score" style="color:{{ 'var(--good)' if observability.score >= 90 else ('var(--warn)' if observability.score >= 70 else 'var(--bad)') }}">{{ observability.score }}</span></div>
  <div class="small">Score starts at 100. Deductions below are explicit; retired Oracle services are not deducted.</div>
</div>
<div class="details-grid">
  <div class="card">
    <b>Score deductions</b>
    {% for d in observability.deductions %}
    <div class="row"><span>{{ d.reason }}</span><span>{{ d.points }} pts</span></div>
    <div class="small">{{ d.evidence }}</div>
    {% endfor %}
  </div>
  <div class="card">
    <b>Current actionable problems</b>
    {% for p in observability.problems %}
    <div class="row"><span><span class="badge {{ 'b-bad' if p.severity == 'HIGH' else 'b-warn' }}">{{ p.severity }}</span> {{ p.component }}</span><span>{{ p.status }}</span></div>
    <div class="small">Evidence: {{ p.evidence }} · user action: {{ p.user_action }} · action: {{ p.recommended_action }}</div>
    {% endfor %}
    {% if not observability.problems %}<div class="na">No actionable current problems.</div>{% endif %}
  </div>
</div>

{% for group, rows in observability.components.items() %}
<div class="component-card">
  <div class="component-title">{{ group }} ({{ rows|length }})</div>
  <div class="tbl-wrap"><table>
  <tr><th>Name</th><th>Type</th><th>Mode</th><th>Status</th><th>Heartbeat</th><th>PID</th><th>Account(s)</th><th>Symbols</th><th>Issue/Error</th><th>Last event</th><th>Recommended action</th><th>Can execute?</th></tr>
  {% for c in rows %}
  <tr>
    <td><b>{{ c.name }}</b></td><td>{{ c.kind }}</td><td>{{ c.mode }}</td>
    <td><span class="badge {{ c.status_class }}">{{ c.status }}</span></td>
    <td>{{ c.heartbeat }}</td><td>{{ c.pid }}</td><td>{{ c.accounts }}</td><td>{{ c.symbols }}</td>
    <td>{{ c.issue }}</td><td>{{ c.event }}</td><td>{{ c.action }}</td><td>{{ c.can_execute }}</td>
  </tr>
  {% endfor %}
  </table></div>
</div>
{% endfor %}

{% if autonomy_challenge %}
<h2>🔥 7-DAY AUTONOMY CHALLENGE</h2>
<div class="card row">
  <span>Status</span>
  <span class="badge {{ 'b-good' if autonomy_challenge.status=='ACTIVE' else 'b-muted' }}">{{ autonomy_challenge.status }}</span>
</div>
<div class="summary-grid">
  <div class="summary-cell"><div class="sc-label">START</div><div class="sc-value">${{ '%.2f'|format(autonomy_challenge.starting_equity) }}</div></div>
  <div class="summary-cell"><div class="sc-label">CURRENT</div><div class="sc-value">${{ '%.2f'|format(autonomy_challenge.current_equity) }}</div></div>
  <div class="summary-cell"><div class="sc-label">TARGET</div><div class="sc-value">${{ '%.0f'|format(autonomy_challenge.target_equity) }}</div></div>
  <div class="summary-cell"><div class="sc-label">P/L</div><div class="sc-value" style="color:{{ 'var(--good)' if autonomy_challenge.net_pnl >= 0 else 'var(--bad)' }}">{{ '%+.2f'|format(autonomy_challenge.net_pnl) }}</div></div>
  <div class="summary-cell"><div class="sc-label">RETURN</div><div class="sc-value">{{ '%+.2f'|format(autonomy_challenge.return_pct) }}%</div></div>
  <div class="summary-cell"><div class="sc-label">PEAK</div><div class="sc-value">${{ '%.2f'|format(autonomy_challenge.peak_equity) }}</div></div>
  <div class="summary-cell"><div class="sc-label">MAX DD</div><div class="sc-value">{{ '%.2f'|format(autonomy_challenge.max_drawdown_pct) }}%</div></div>
  <div class="summary-cell"><div class="sc-label">TRADES</div><div class="sc-value">{{ autonomy_challenge.total_trades }} ({{ autonomy_challenge.wins }}W/{{ autonomy_challenge.losses }}L)</div></div>
  <div class="summary-cell"><div class="sc-label">BOT HEALTH</div><div class="sc-value">{{ autonomy_challenge.bot_health }}</div></div>
  <div class="summary-cell"><div class="sc-label">RISK_HALT</div><div class="sc-value">{{ autonomy_challenge.risk_halt_events }} event(s)</div></div>
  <div class="summary-cell"><div class="sc-label">TIME REMAINING</div><div class="sc-value">{{ autonomy_challenge.time_remaining }}</div></div>
  <div class="summary-cell"><div class="sc-label">OPEN POS</div><div class="sc-value">{{ open_positions|length }}</div></div>
  <!-- 2026-08-19 dashboard patch: was autonomy_challenge.open_positions (a frozen field
       persisted by the now-completed challenge state file) -- now reads the SAME live
       open_positions list the Trade Center table below already renders, so this number
       and that table can never disagree again. autonomy_challenge.py itself untouched. -->
</div>
<div class="card">
  <div class="sc-label" style="margin-bottom:6px">Progress toward $300 target (display/measurement only — zero trading logic tied to this)</div>
  <div style="background:var(--card2);border-radius:8px;height:14px;overflow:hidden">
    <div style="background:var(--wait);height:100%;width:{{ autonomy_challenge.progress_pct }}%"></div>
  </div>
  <div style="font-size:11px;color:var(--muted);margin-top:4px">{{ '%.1f'|format(autonomy_challenge.progress_pct) }}% · started {{ autonomy_challenge.start_timestamp_utc }} · ends {{ autonomy_challenge.end_timestamp_utc }}</div>
</div>
{% endif %}

{% if shadow %}
<h2>🌓 SHADOW TRADES <span style="font-size:12px;color:var(--muted)">(risk-free measurement — zero live execution)</span></h2>
<div class="summary-grid">
  <div class="summary-cell"><div class="sc-label">CALLS</div><div class="sc-value">{{ shadow.overall.total }}</div></div>
  <div class="summary-cell"><div class="sc-label">CLOSED</div><div class="sc-value">{{ shadow.overall.closed }}</div></div>
  <div class="summary-cell"><div class="sc-label">OPEN</div><div class="sc-value">{{ shadow.overall.open }}</div></div>
  <div class="summary-cell"><div class="sc-label">WIN RATE</div><div class="sc-value">{{ '%.1f'|format(shadow.overall.win_rate_pct) }}%</div></div>
  <div class="summary-cell"><div class="sc-label">EXPECTANCY</div><div class="sc-value" style="color:{{ 'var(--good)' if shadow.overall.expectancy_r >= 0 else 'var(--bad)' }}">{{ '%+.2f'|format(shadow.overall.expectancy_r) }}R</div></div>
  <div class="summary-cell"><div class="sc-label">SUM R</div><div class="sc-value" style="color:{{ 'var(--good)' if shadow.overall.sum_r >= 0 else 'var(--bad)' }}">{{ '%+.2f'|format(shadow.overall.sum_r) }}R</div></div>
</div>
{% for a, s in shadow.by_agent.items() %}
<div class="card row"><span><b>{{ a }}</b></span><span>{{ s.wins }}W/{{ s.losses }}L · {{ '%.0f'|format(s.win_rate_pct) }}% WR · {{ '%+.2f'|format(s.expectancy_r) }}R exp · {{ '%+.2f'|format(s.sum_r) }}R total</span></div>
{% endfor %}
{% for c in shadow.open_calls %}
<div class="card" style="font-size:12px;color:var(--muted)">🌓 <b>{{ c.agent }}</b> · {{ c.symbol }} {{ c.side|upper }} @ {{ c.entry }} · SL {{ c.sl }} · TP {{ c.tp }} · until {{ c.valid_until }}</div>
{% endfor %}
{% if not shadow.open_calls %}<div class="na">no open shadow calls right now</div>{% endif %}
{% endif %}

{% if pending_signal_shadow %}
<h2>🎯 PENDING SIGNAL SHADOW <span style="font-size:12px;color:var(--muted)">(SHADOW/OBSERVE ONLY — isolated research, zero live execution)</span></h2>
<div class="summary-grid">
  <div class="summary-cell"><div class="sc-label">SIGNALS</div><div class="sc-value">{{ pending_signal_shadow.signals_received }}</div></div>
  <div class="summary-cell"><div class="sc-label">OPEN</div><div class="sc-value">{{ pending_signal_shadow.open_orders_total }}</div></div>
  <div class="summary-cell"><div class="sc-label">TRIGGERED</div><div class="sc-value">{{ pending_signal_shadow.triggered }}</div></div>
  <div class="summary-cell"><div class="sc-label">EXPIRED</div><div class="sc-value">{{ pending_signal_shadow.expired }}</div></div>
  <div class="summary-cell"><div class="sc-label">INVALIDATED</div><div class="sc-value">{{ pending_signal_shadow.invalidated }}</div></div>
  <div class="summary-cell"><div class="sc-label">WIN RATE</div><div class="sc-value">{{ '%.1f'|format(pending_signal_shadow.win_rate_pct)+'%' if pending_signal_shadow.win_rate_pct is not none else 'N/A' }}</div></div>
  <div class="summary-cell"><div class="sc-label">EXPECTANCY</div><div class="sc-value">{{ '%+.2f'|format(pending_signal_shadow.expectancy_r)+'R' if pending_signal_shadow.expectancy_r is not none else 'N/A' }}</div></div>
  <div class="summary-cell"><div class="sc-label">TOP SOURCE</div><div class="sc-value">{{ pending_signal_shadow.top_source or 'N/A' }}</div></div>
  <div class="summary-cell"><div class="sc-label">BEST SYMBOL</div><div class="sc-value">{{ pending_signal_shadow.best_symbol or 'N/A' }}</div></div>
  <div class="summary-cell"><div class="sc-label">BEST ORDER TYPE</div><div class="sc-value">{{ pending_signal_shadow.best_order_type or 'N/A' }}</div></div>
</div>
{% for src, s in pending_signal_shadow.by_source.items() %}
<div class="card row"><span><b>{{ src }}</b></span><span>n={{ s.sample_size }} · {{ '%.0f'|format(s.win_rate_pct)+'%' if s.win_rate_pct is not none else 'N/A' }} WR · {{ '%+.2f'|format(s.expectancy_r)+'R' if s.expectancy_r is not none else 'N/A' }} exp · {{ s.readiness }}</span></div>
{% endfor %}
{% if not pending_signal_shadow.by_source %}<div class="na">no closed shadow orders yet</div>{% endif %}
{% endif %}

{% if pending_breakout_shadow %}
<h2>⚡ PendingBreakoutShadow</h2>
<div class="summary-grid">
  <div class="summary-cell"><div class="sc-label">PROCESS</div><div class="sc-value">{{ 'RUNNING' if pending_breakout_shadow.alive else 'STOPPED' }}</div></div>
  <div class="summary-cell"><div class="sc-label">PID</div><div class="sc-value">{{ pending_breakout_shadow.pid }}</div></div>
  <div class="summary-cell"><div class="sc-label">ENGINE</div><div class="sc-value">{{ pending_breakout_shadow.engine_state }}</div></div>
  <div class="summary-cell"><div class="sc-label">LAST TICK</div><div class="sc-value">{{ pending_breakout_shadow.last_tick_age_ms if pending_breakout_shadow.last_tick_age_ms is not none else 'N/A' }} ms</div></div>
  <div class="summary-cell"><div class="sc-label">SETUPS</div><div class="sc-value">{{ pending_breakout_shadow.total_setups or 0 }}</div></div>
  <div class="summary-cell"><div class="sc-label">TRADES</div><div class="sc-value">{{ pending_breakout_shadow.total_trades or 0 }}</div></div>
</div>
<div class="card">Active trade: {{ 'YES' if pending_breakout_shadow.has_active_trade else 'NO' }} · last error: {{ pending_breakout_shadow.last_error or '—' }} · started: {{ pending_breakout_shadow.process_start_time or '—' }}</div>
{% endif %}

{% if pending_signal_runner %}
<h2>🎯 Pending Signal Shadow Runner</h2>
<div class="summary-grid">
  <div class="summary-cell"><div class="sc-label">PROCESS</div><div class="sc-value">{{ 'RUNNING' if pending_signal_runner.alive else 'STOPPED' }}</div></div>
  <div class="summary-cell"><div class="sc-label">PID</div><div class="sc-value">{{ pending_signal_runner.pid }}</div></div>
  <div class="summary-cell"><div class="sc-label">ENGINE</div><div class="sc-value">{{ pending_signal_runner.engine_state }}</div></div>
  <div class="summary-cell"><div class="sc-label">LAST TICK</div><div class="sc-value">{{ pending_signal_runner.last_tick_age_ms if pending_signal_runner.last_tick_age_ms is not none else 'N/A' }} ms</div></div>
  <div class="summary-cell"><div class="sc-label">SETUPS</div><div class="sc-value">{{ pending_signal_runner.total_setups or 0 }}</div></div>
  <div class="summary-cell"><div class="sc-label">TRADES</div><div class="sc-value">{{ pending_signal_runner.total_trades or 0 }}</div></div>
</div>
<div class="card">Active trade: {{ 'YES' if pending_signal_runner.has_active_trade else 'NO' }} · last error: {{ pending_signal_runner.last_error or '—' }} · started: {{ pending_signal_runner.process_start_time or '—' }}</div>
{% endif %}

{% if pending_signal_bridge %}
<h2>🔗 Pending Signal Bridge</h2>
<div class="summary-grid">
  <div class="summary-cell"><div class="sc-label">PROCESS</div><div class="sc-value">{{ 'RUNNING' if pending_signal_bridge.alive else 'STOPPED' }}</div></div>
  <div class="summary-cell"><div class="sc-label">PID</div><div class="sc-value">{{ pending_signal_bridge.pid }}</div></div>
  <div class="summary-cell"><div class="sc-label">MT5</div><div class="sc-value">{{ 'YES' if pending_signal_bridge.mt5_connected else 'NO' }}</div></div>
  <div class="summary-cell"><div class="sc-label">GUARD</div><div class="sc-value">{{ 'ON' if pending_signal_bridge.guard_active else 'OFF' }}</div></div>
  <div class="summary-cell"><div class="sc-label">ENGINE</div><div class="sc-value">{{ pending_signal_bridge.engine_state }}</div></div>
  <div class="summary-cell"><div class="sc-label">LAST TICK</div><div class="sc-value">{{ pending_signal_bridge.last_tick_age_ms if pending_signal_bridge.last_tick_age_ms is not none else 'N/A' }} ms</div></div>
  <div class="summary-cell"><div class="sc-label">SIGNALS</div><div class="sc-value">{{ pending_signal_bridge.signals_received or 0 }}</div></div>
  <div class="summary-cell"><div class="sc-label">OPEN ORDERS</div><div class="sc-value">{{ pending_signal_bridge.open_orders_total or 0 }}</div></div>
</div>
<div class="card">Logged: {{ pending_signal_bridge.gold_signals_logged or 0 }} · closed: {{ pending_signal_bridge.closed_total or 0 }} · triggered: {{ pending_signal_bridge.triggered or 0 }} · expired: {{ pending_signal_bridge.expired or 0 }} · invalidated: {{ pending_signal_bridge.invalidated or 0 }} · last error: {{ pending_signal_bridge.last_error or '—' }}</div>
{% endif %}

{% if manual_exit_shadow %}
<h2>🧾 Manual Exit Shadow Tracker</h2>
<div class="summary-grid">
  <div class="summary-cell"><div class="sc-label">PROCESS</div><div class="sc-value">{{ 'RUNNING' if manual_exit_shadow.alive else 'STOPPED' }}</div></div>
  <div class="summary-cell"><div class="sc-label">PID</div><div class="sc-value">{{ manual_exit_shadow.pid or '—' }}</div></div>
  <div class="summary-cell"><div class="sc-label">POST_FIX_LIVE</div><div class="sc-value">{{ manual_exit_shadow.post_fix_live_samples }}</div></div>
  <div class="summary-cell"><div class="sc-label">VERDICT</div><div class="sc-value">{{ manual_exit_shadow.current_verdict }}</div></div>
  <div class="summary-cell"><div class="sc-label">LAST RUN</div><div class="sc-value">{{ manual_exit_shadow.last_run_utc or '—' }}</div></div>
</div>
{% endif %}

<h2>🟢 NO TRADE — Bot Status</h2>
{% for r in no_trade_status %}
<div class="card row">
  <span><b>{{ r.bot }}</b></span>
  <span>
    {% if r.status == 'NO_TRADE_NORMAL' %}<span class="badge b-good">🟢 NO TRADE — NORMAL</span>
    {% elif r.status == 'BOT_DOWN' %}<span class="badge b-bad">🔴 BOT DOWN</span>
    {% else %}<span class="badge b-warn">🟠 EXECUTION ERROR</span>{% endif %}
  </span>
</div>
<div class="card" style="margin-top:-6px;font-size:12px;color:var(--muted)">
  last trade: {{ r.last_trade_time or 'none in window' }}
  {% if r.time_since_last_trade_sec %} ({{ (r.time_since_last_trade_sec/60)|round|int }}m ago){% endif %}
  · heartbeat age: {{ r.last_heartbeat_age_sec if r.last_heartbeat_age_sec is not none else 'N/A' }}s
  {% if r.reason %}<br>reason: {{ r.reason }}{% endif %}
</div>
{% endfor %}
{% if not no_trade_status %}<div class="na">No watched bots configured</div>{% endif %}

<h2>🧭 Market Context</h2>
<div class="card">
<div class="chip-row">
{% for r in market_context.regimes %}
<span class="chip"><span class="badge {{ r.badge_class }}">{{ r.symbol }}</span> {{ r.base_regime }} · {{ r.vol_state }} · {{ r.age_text }}</span>
{% endfor %}
</div>
<div class="chip-row" style="margin-top:12px">
{% for m in market_context.monitors %}
<span class="chip"><span class="badge {{ m.badge_class }}">{{ m.name }}</span> {{ m.age_text }}</span>
{% endfor %}
</div>
</div>

<h2>📒 Trade Center — Open Positions</h2>
<div class="tbl-wrap"><table>
<tr><th>Account</th><th>Symbol</th><th>Side</th><th>Vol</th><th>P&L</th><th>SL</th><th>Source</th><th>Confidence</th></tr>
{% for p in open_positions %}
<tr>
  <td>{{ p.account }}</td><td>{{ p.symbol }}</td><td>{{ p.side }}</td><td>{{ p.volume }}</td>
  <td>{{ '%+.2f'|format(p.profit) if p.profit is not none else 'N/A' }}</td>
  <td>{% if p.sl_status == 'CONFIRMED' %}<span class="badge b-good">SL OK</span>{% else %}<span class="badge b-bad">NO SL</span>{% endif %}</td>
  <td>{{ p.source }}</td><td>{{ p.attribution_confidence }}</td>
</tr>
{% endfor %}
</table></div>
{% if not open_positions %}<div class="na">No open positions</div>{% endif %}

<h2>📒 Trade Center — Today's Trades ({{ trades_today|length }})</h2>
<div class="tbl-wrap"><table>
<tr><th>Time</th><th>Account</th><th>Symbol</th><th>Side</th><th>P&L</th><th>Source</th><th>Confidence</th></tr>
{% for t in trades_today[:20] %}
<tr>
  <td>{{ t.exit_time.strftime('%H:%M') }}</td><td>{{ t.account }}</td><td>{{ t.symbol }}</td><td>{{ t.side }}</td>
  <td>{{ '%+.2f'|format(t.pnl) }}</td><td>{{ t.source }}</td><td>{{ t.attribution_confidence }}</td>
</tr>
{% endfor %}
</table></div>
{% if not trades_today %}<div class="na">No trades today (this can be normal -- see NO TRADE status above)</div>{% endif %}

<h2>📊 Portfolio Risk</h2>
<div class="summary-grid">
{% for asset, v in exposure.items() %}
<div class="summary-cell">
  <div class="sc-label">{{ asset }} exposure</div>
  <div class="sc-value">{{ v.position_count }} pos / {{ v.total_lot }} lot</div>
  <div style="font-size:11px;color:var(--muted)">{{ v.bot_count }} bot(s) · protected {{ v.protected_pct if v.protected_pct is not none else 'N/A' }}%</div>
</div>
{% endfor %}
</div>
{% for asset, v in exposure.items() %}
{% if v.unprotected_count > 0 %}
<div class="card"><b>⚠️ {{ asset }}: {{ v.unprotected_count }} unprotected position(s)</b>
{% for u in v.unprotected_positions %}<div class="row"><span>{{ u.account }} {{ u.symbol }} {{ u.side }}</span><span>{{ u.volume }}</span></div>{% endfor %}
</div>
{% endif %}
{% endfor %}

<h2>🔗 Oracle Attribution Ledger (Phase 1/2, real data only)</h2>
{% if oracle_attr.available %}
<div class="card">Total reconciled fills: {{ oracle_attr.total_fills }}</div>
{% for bot, counts in oracle_attr.confidence_by_bot.items() %}
<div class="row"><span>{{ bot }}</span><span>TAG={{ counts.TAG }} LOG_MATCH={{ counts.LOG_MATCH }} AMBIGUOUS={{ counts.AMBIGUOUS }} UNMATCHED={{ counts.UNMATCHED }}</span></div>
{% endfor %}
{% else %}
<div class="na">Not Available ({{ oracle_attr.reason }}) -- this dashboard instance has no oracle_attribution ledger to read. Not fabricated.</div>
{% endif %}

</div>
<p class="footnote">Read-only. No trades placed, modified, or closed from this page. Positions/trades cached from the same data status_dashboard.py already refreshes; cache age {{ cache_age_sec }}s. Oracle open-position attribution_confidence is intentionally INSUFFICIENT_DATA until the ledger cross-reference lands -- not guessed.</p>
</body></html>
"""


# ---------------------------------------------------------------------------
# 2026-08-09 (Read-Only Monitoring Extension). Reuses _cache["accounts"]
# (already refreshed every REFRESH_SEC by refresh_loop()) and _get_trades()
# (already TTL-cached) -- zero new MT5/Oracle polling. Oracle attribution
# ledger read gets its own small TTL cache slot since it's a file read.
# STRICT READ-ONLY: this page and its API render data only -- no control
# buttons, no POST routes, nothing here can close/modify/place an order.
# ---------------------------------------------------------------------------
_oracle_attr_cache = {"ts": 0, "data": None}
_oracle_attr_lock = threading.Lock()


def _get_oracle_attribution():
    with _oracle_attr_lock:
        if time.time() - _oracle_attr_cache["ts"] > ANALYTICS_TTL_SEC:
            _oracle_attr_cache["data"] = pa.oracle_attribution_summary()
            _oracle_attr_cache["ts"] = time.time()
        return _oracle_attr_cache["data"]


def _monitor_context_view():
    """Read-only summary of the extra monitoring tools this dashboard
    already launches. Uses their own persisted state/heartbeat files."""
    base_dir = os.path.dirname(__file__)
    regime_path = os.path.join(base_dir, "regime_state.json")
    heartbeat_paths = {
        "regime_detector": os.path.join(base_dir, "heartbeat_regime_detector.json"),
        "outcome_calibration_log": os.path.join(base_dir, "heartbeat_outcome_calibration_log.json"),
    }

    def _read_json(path, default):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def _heartbeat_age_text(path):
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
            last = datetime.fromisoformat(payload["last_success_ts"])
            age = (datetime.now(timezone.utc) - last).total_seconds()
            return age, f"{int(age)}s ago"
        except Exception:
            return None, "N/A"

    regime_state = _read_json(regime_path, {})
    primary = ["BTC/USDT:USDT", "XAU/USDT:USDT", "SOL/USDT:USDT", "ETH/USDT:USDT"]
    regimes = []
    for sym in primary:
        entry = regime_state.get(sym) or {}
        base = entry.get("base_regime", "N/A")
        vol = entry.get("vol_state", "N/A")
        updated = entry.get("updated_ts")
        age_txt = "N/A"
        if updated:
            age_txt = f"{int(max(0, time.time() - updated))}s ago"
        regimes.append({
            "symbol": sym.split("/")[0],
            "base_regime": base,
            "vol_state": vol,
            "age_text": age_txt,
            "badge_class": "b-good" if base == "TRENDING_UP" else ("b-bad" if base == "TRENDING_DOWN" else "b-wait"),
        })

    monitors = []
    for name, path in heartbeat_paths.items():
        _, age_txt = _heartbeat_age_text(path)
        monitors.append({
            "name": name,
            "age_text": age_txt,
            "badge_class": "b-good" if age_txt != "N/A" else "b-bad",
        })

    return {"regimes": regimes, "monitors": monitors}


def _autonomy_challenge_view(no_trade_status_list):
    """Combines the persisted challenge state (financial figures, survives
    restarts -- see autonomy_challenge.py) with LIVE-only operational
    fields (bot_health/execution_errors/time_remaining/progress_pct)
    computed fresh from the same no_trade_status() this route already
    computes. Those live fields are deliberately NOT persisted into the
    state file -- see record_snapshot()'s docstring for why (avoids the
    same recurring-error double-count class of bug last_error()'s
    recency window fixed earlier)."""
    try:
        state = achall.read_state()
    except Exception as e:
        _dependency_failed("autonomy_challenge.runtime", e)
        return None
    if not state:
        return None
    from datetime import datetime as _dt, timezone as _tz
    down = [b for b in no_trade_status_list if b.get("status") in ("BOT_DOWN", "EXECUTION_ERROR")]
    view = dict(state)
    view["bot_health"] = "HEALTHY" if not down else f"DEGRADED ({len(down)} bot(s))"
    view["execution_errors"] = sum(1 for b in no_trade_status_list if b.get("status") == "EXECUTION_ERROR")
    view["rejected_orders"] = "NOT_AVAILABLE"  # no rejected-order tracking exists in this codebase -- not guessed
    span = view["target_equity"] - view["starting_equity"]
    progress = ((view["current_equity"] - view["starting_equity"]) / span * 100) if span else 0.0
    view["progress_pct"] = max(0.0, min(100.0, progress))
    end = _dt.fromisoformat(view["end_timestamp_utc"])
    now = _dt.now(_tz.utc)
    remaining = end - now
    if view["status"] != "ACTIVE":
        view["time_remaining"] = view["status"]
    elif remaining.total_seconds() <= 0:
        view["time_remaining"] = "0d 0h"
    else:
        days, rem = divmod(int(remaining.total_seconds()), 86400)
        view["time_remaining"] = f"{days}d {rem // 3600}h"
    return view


def _source_from_strategy_string(strategy):
    """Derives BOT/TELEGRAM/MANUAL/UNKNOWN from the strategy label
    normalize_mt5_position()/normalize_baa_position() already computed
    -- avoids touching either function (both used elsewhere) just to
    retain a raw magic number."""
    if strategy in ("Manual", "Manual/Other"):
        return "MANUAL"
    if strategy == "fx_signal_exec":
        return "TELEGRAM"
    if not strategy or strategy == "Unknown / Unattributed" or strategy.startswith("Unattributed") or strategy.startswith("magic "):
        return "UNKNOWN"
    return "BOT"


@app.route("/monitor")
def monitor_page():
    with _cache_lock:
        accounts = _cache["accounts"]
        oracle_meta = _cache["oracle_meta"]
    trades, mt5_errors, cache_ts = _get_trades(days=7)
    by_magic = _pa_call("group_by_magic", {}, trades)

    def _enrich_center(t):
        e = _enrich_trade(t)
        e["source"] = _pa_call("trade_source", "ERROR", t["magic"])
        e["attribution_confidence"] = "HIGH"  # MT5 magic -- deterministic, never ambiguous
        return e

    trades_today = [_enrich_center(t) for t in _filter_trades(trades, period="today")][:100]
    trades_week = [_enrich_center(t) for t in _filter_trades(trades, period="week")][:200]

    open_positions = []
    for acc in accounts:
        for p in acc.get("positions", []):
            row = dict(p)
            is_oracle = str(acc.get("name", "")).startswith("Oracle")
            row["source"] = "ORACLE_BOT_OR_MANUAL" if is_oracle else _source_from_strategy_string(p.get("strategy"))
            row["attribution_confidence"] = "INSUFFICIENT_DATA" if is_oracle else "HIGH"
            open_positions.append(row)

    nts = _pa_call("no_trade_status", [{"bot": "portfolio_analytics", "status": "EXECUTION_ERROR",
                                        "reason": "runtime dependency failure", "last_trade_time": None,
                                        "time_since_last_trade_sec": None, "last_heartbeat_age_sec": None}], by_magic)
    risk_halt = _pa_call("risk_halt_status", {"halted": None, "status": "ERROR",
                                               "error": "portfolio_analytics runtime failure"})
    shadow = _shadow_view()
    pending_breakout = _pending_breakout_shadow_view()
    pending_signal_runner = _pending_signal_runner_view()
    pending_signal_bridge = _pending_signal_bridge_view()
    pending_signal_shadow = _pending_signal_shadow_view()
    manual_exit_shadow = _manual_exit_shadow_view()
    market_context = _monitor_context_view()
    oracle_attr = _get_oracle_attribution()
    bot_health = bot_health_detail(accounts, risk_halt.get("halted") is not False)
    cache_age_sec = round(time.time() - cache_ts)
    observability = _observability_model(
        accounts, bot_health, oracle_meta, risk_halt, nts, shadow, pending_breakout,
        pending_signal_runner, pending_signal_bridge, pending_signal_shadow,
        manual_exit_shadow, market_context, oracle_attr, cache_age_sec,
    )
    return render_template_string(
        MONITOR_PAGE,
        trades_today=trades_today, trades_week=trades_week, open_positions=open_positions,
        no_trade_status=nts,
        risk_halt=risk_halt,
        exposure=_pa_call("asset_exposure_summary", {}, accounts),
        oracle_attr=oracle_attr,
        market_context=market_context,
        autonomy_challenge=_autonomy_challenge_view(nts),
        shadow=shadow,
        pending_breakout_shadow=pending_breakout,
        pending_signal_runner=pending_signal_runner,
        pending_signal_bridge=pending_signal_bridge,
        pending_signal_shadow=pending_signal_shadow,
        manual_exit_shadow=manual_exit_shadow,
        observability=observability,
        mt5_errors=mt5_errors, cache_age_sec=cache_age_sec,
    )


def _shadow_view():
    """Read-only shadow-trade scoreboard for the /monitor card. ZERO execution.
    Returns None when no shadow calls exist yet (card stays hidden)."""
    try:
        import shadow_eval
        events = shadow_eval._read_events()
        if not events:
            return None
        board = shadow_eval.scoreboard(events)
        closed_ids = {e["id"] for e in events if e.get("type") == "close"}
        open_calls = [
            {"id": o.get("id"), "agent": o.get("agent", "?"), "symbol": o.get("symbol"),
             "side": o.get("side"), "entry": o.get("entry"), "sl": o.get("sl"),
             "tp": o.get("tp"), "valid_until": o.get("valid_until")}
            for o in events
            if o.get("type") == "open" and o.get("id") not in closed_ids
        ]
        return {"overall": board["overall"], "by_agent": board["by_agent"],
                "open_calls": open_calls}
    except Exception:
        return None


PENDING_SIGNAL_SHADOW_SUMMARY_PATH = (
    r"C:\TradingBot\Research_PendingSignalShadow_2026\data\pending_signal_shadow_engine\dashboard_summary.json"
)
PENDING_BREAKOUT_SHADOW_HEARTBEAT_PATH = (
    r"C:\TradingBot\Research_PendingBreakoutShadow_2026\data\pending_breakout_shadow\heartbeat.json"
)
PENDING_BREAKOUT_SHADOW_ENGINE_STATE_PATH = (
    r"C:\TradingBot\Research_PendingBreakoutShadow_2026\data\pending_breakout_shadow\engine_state.json"
)
PENDING_SIGNAL_RUNNER_HEARTBEAT_PATH = (
    r"C:\TradingBot\Research_PendingSignalShadow_2026\data\pending_signal_shadow_engine\heartbeat.json"
)
PENDING_SIGNAL_RUNNER_ENGINE_STATE_PATH = (
    r"C:\TradingBot\Research_PendingSignalShadow_2026\data\pending_signal_shadow_engine\engine_state.json"
)
PENDING_SIGNAL_BRIDGE_STATE_PATH = (
    r"C:\TradingBot\Research_PendingSignalShadow_2026\data\pending_signal_shadow_engine\signal_bridge_state.json"
)
PENDING_SIGNAL_BRIDGE_HEARTBEAT_PATH = (
    r"C:\TradingBot\Research_PendingSignalShadow_2026\data\pending_signal_shadow_engine\heartbeat.json"
)
MANUAL_EXIT_SHADOW_STATE_PATH = (
    r"C:\TradingBot\Research_ManualExitShadow_2026\data\manual_exit_shadow\manual_exit_shadow_state.json"
)
MANUAL_EXIT_SHADOW_REPORT_PATH = (
    r"C:\TradingBot\Research_ManualExitShadow_2026\data\manual_exit_shadow\manual_exit_shadow_report.md"
)
MANUAL_EXIT_SHADOW_SNAPSHOT_LOG = (
    r"C:\TradingBot\Research_ManualExitShadow_2026\data\manual_exit_shadow\manual_exit_snapshots.jsonl"
)


def _manual_exit_shadow_view():
    """Read-only health card for the isolated manual-exit shadow tracker."""
    state = _read_json_file(MANUAL_EXIT_SHADOW_STATE_PATH, default={}) or {}
    if not state and not os.path.exists(MANUAL_EXIT_SHADOW_REPORT_PATH):
        return None
    rows = _wmic_pid_map()
    pids = [pid for cmd, pid in rows if "manual_exit_shadow_tracker.py" in cmd]
    verdict = "INCONCLUSIVE"
    try:
        with open(MANUAL_EXIT_SHADOW_REPORT_PATH, encoding="utf-8") as f:
            for line in f:
                if line.startswith("Verdict:"):
                    verdict = line.split("**", 2)[1] if "**" in line else line.split(":", 1)[1].strip()
                    break
    except Exception:
        pass
    post_fix_live_samples = 0
    try:
        with open(MANUAL_EXIT_SHADOW_SNAPSHOT_LOG, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("observation_phase") == "POST_FIX_LIVE":
                    post_fix_live_samples += 1
    except Exception:
        pass
    return {
        "available": True,
        "pid": pids[0] if pids else state.get("pid"),
        "alive": bool(pids),
        "last_run_utc": state.get("last_run_utc"),
        "post_fix_live_samples": post_fix_live_samples,
        "current_verdict": verdict,
    }


def _pending_breakout_shadow_view():
    hb = _read_json_file(PENDING_BREAKOUT_SHADOW_HEARTBEAT_PATH, default={}) or {}
    state = _read_json_file(PENDING_BREAKOUT_SHADOW_ENGINE_STATE_PATH, default={}) or {}
    if not hb and not state:
        return None
    pid = hb.get("pid") or state.get("pid")
    return {
        "available": True,
        "pid": pid or "—",
        "alive": _pid_alive(pid),
        "engine_state": hb.get("engine_state") or state.get("state") or "—",
        "last_tick_age_ms": hb.get("last_tick_age_ms") or state.get("last_tick_age_ms"),
        "total_setups": hb.get("total_setups") or state.get("total_setups"),
        "total_trades": hb.get("total_trades") or state.get("total_trades"),
        "has_active_trade": hb.get("has_active_trade") if hb.get("has_active_trade") is not None else state.get("has_active_trade"),
        "last_error": hb.get("last_error") or state.get("last_error"),
        "process_start_time": hb.get("process_start_time") or state.get("process_start_time"),
    }


def _pending_signal_runner_view():
    hb = _read_json_file(PENDING_SIGNAL_RUNNER_HEARTBEAT_PATH, default={}) or {}
    state = _read_json_file(PENDING_SIGNAL_RUNNER_ENGINE_STATE_PATH, default={}) or {}
    if not hb and not state:
        return None
    pid = hb.get("pid") or state.get("pid")
    return {
        "available": True,
        "pid": pid or "—",
        "alive": _pid_alive(pid),
        "engine_state": hb.get("engine_state") or state.get("state") or "—",
        "last_tick_age_ms": hb.get("last_tick_age_ms") or state.get("last_tick_age_ms"),
        "total_setups": hb.get("total_setups") or state.get("total_setups"),
        "total_trades": hb.get("total_trades") or state.get("total_trades"),
        "has_active_trade": hb.get("has_active_trade") if hb.get("has_active_trade") is not None else state.get("has_active_trade"),
        "last_error": hb.get("last_error") or state.get("last_error"),
        "process_start_time": hb.get("process_start_time") or state.get("process_start_time"),
    }


def _pending_signal_bridge_view():
    state = _read_json_file(PENDING_SIGNAL_BRIDGE_STATE_PATH, default={}) or {}
    hb = _read_json_file(PENDING_SIGNAL_BRIDGE_HEARTBEAT_PATH, default={}) or {}
    if not state and not hb:
        return None
    pid = state.get("pid") or hb.get("pid")
    return {
        "available": True,
        "pid": pid or "—",
        "alive": _pid_alive(pid),
        "mt5_connected": state.get("mt5_connected"),
        "guard_active": state.get("guard_active"),
        "engine_state": state.get("engine_state") or hb.get("engine_state") or "—",
        "last_tick_age_ms": state.get("last_tick_age_ms") or hb.get("last_tick_age_ms"),
        "signals_received": state.get("signals_received"),
        "gold_signals_logged": state.get("gold_signals_logged"),
        "open_orders_total": state.get("open_orders_total"),
        "closed_total": state.get("closed_total"),
        "triggered": state.get("triggered"),
        "expired": state.get("expired"),
        "invalidated": state.get("invalidated"),
        "last_error": state.get("last_error") or hb.get("last_error"),
    }


def _pending_signal_shadow_view():
    """Read-only card for the isolated pending_signal_shadow_engine
    SHADOW/OBSERVE-ONLY research package (own project, own data dir, zero
    execution). Reads that package's own atomically-written summary JSON
    file directly -- this dashboard has no other dependency on that
    package. Returns None (card stays hidden) if the file doesn't exist
    yet or fails to parse, same degrade-safe pattern as _shadow_view()."""
    try:
        with open(PENDING_SIGNAL_SHADOW_SUMMARY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _component_status(status):
    if status in ("RUNNING", "HEALTHY", "OK", "READY"):
        return "b-good"
    if status in ("STALE", "BLOCKED", "NOT_READY", "INCONCLUSIVE", "UNKNOWN"):
        return "b-warn"
    if status in ("DOWN", "ERROR"):
        return "b-bad"
    return "b-muted"


def _component(name, group, kind, status, heartbeat="n/a", pid="—", accounts="—",
               symbols="—", mode="READ_ONLY", issue="—", event="—",
               action="Monitor", can_execute="NO"):
    return {
        "name": name,
        "group": group,
        "kind": kind,
        "status": status,
        "status_class": _component_status(status),
        "heartbeat": heartbeat,
        "pid": pid,
        "accounts": accounts,
        "symbols": symbols,
        "mode": mode,
        "issue": issue,
        "event": event,
        "action": action,
        "can_execute": can_execute,
    }


def _observability_model(accounts, bot_health, oracle_meta, risk_halt, no_trade_status,
                         shadow, pending_breakout, pending_signal_runner,
                         pending_signal_bridge, pending_signal_shadow, manual_exit_shadow,
                         market_context, oracle_attr, cache_age_sec):
    """Builds the full-detail monitor view from already-fetched dashboard data.
    No SSH, MT5, exchange, process control, or trading calls are made here."""
    components = {"Local Bots": [], "Oracle Bots": [], "Shadows": [], "Infrastructure": []}
    problems = []

    for name, error in sorted(dict(DEPENDENCY_ERRORS).items()):
        components["Infrastructure"].append(_component(
            name, "Infrastructure", "DEPENDENCY", "ERROR", "startup/runtime check", "—",
            "local", "dashboard", "READ_ONLY", error, "dependency unavailable",
            "Restore dependency before trusting affected fields", "NO",
        ))

    for b in bot_health:
        group = "Infrastructure" if b["role"] == "INFRASTRUCTURE" else "Local Bots"
        issue = b["manual_block_reason"] or ("Heartbeat stale" if b["state"] == "STALE" else "—")
        action = "Investigate heartbeat" if b["state"] == "STALE" else ("Start only if approved-active" if b["state"] == "STOPPED" and b["can_trade"] != "NO" else "Monitor")
        components[group].append(_component(
            b["bot"], group, b["role"], b["state"], b["heartbeat_age"], b["pid"],
            b["account"], "see strategy config", "LIVE" if b["can_trade"] == "YES" else b["can_trade"],
            issue, b["eligibility"] or "—", action, "YES" if b["can_trade"] == "YES" else "NO",
        ))

    oracle_services = [
        ("bybit_shield", oracle_meta.get("bybit_shield_alive"), "PROTECTION"),
        ("ladder_guard_bybit", oracle_meta.get("ladder_guard_alive"), "PROTECTION"),
        ("tg_signal_bot", oracle_meta.get("tg_signal_bot_alive"), "SIGNAL"),
    ]
    for name, alive, role in oracle_services:
        status = "RUNNING" if alive else ("UNKNOWN" if alive is None else "DOWN")
        components["Oracle Bots"].append(_component(
            name, "Oracle Bots", role, status, "from cached Oracle fetch", "oracle", "Oracle",
            "Bybit/Oracle", "LIVE", "—" if alive else "missing from Oracle cache",
            "expected-active Oracle service", "Investigate Oracle process" if alive is False else "Monitor",
            "YES" if role == "SIGNAL" else "NO",
        ))
    components["Oracle Bots"].append(_component(
        "liquidity_sweep_bot", "Oracle Bots", "TRADING", "BLOCKED",
        "retired heartbeat ignored", "—", "Oracle", "BTC", "STOPPED_BY_DESIGN",
        "retired 2026-08-16, not expected-active", "excluded from health score",
        "No action unless explicitly re-approved", "NO",
    ))
    components["Oracle Bots"].append(_component(
        "orb_bot", "Oracle Bots", "TRADING", "BLOCKED",
        "retired heartbeat ignored", "—", "Oracle", "SOL/ETH/XRP/ADA/DOT", "STOPPED_BY_DESIGN",
        "retired 2026-08-16, not expected-active", "excluded from health score",
        "No action unless explicitly re-approved", "NO",
    ))
    components["Oracle Bots"].append(_component(
        "live_decay_watch", "Oracle Bots", "WATCH", "RUNNING",
        "cron every 4h", "cron", "Oracle", "live groups", "READ_ONLY",
        "cron-only, not a daemon", "uses existing Oracle cadence", "Monitor", "NO",
    ))

    if shadow:
        components["Shadows"].append(_component(
            "shadow_scoreboard", "Shadows", "SHADOW", "RUNNING",
            "event log", "—", "—", "multi", "SHADOW",
            f"open={shadow['overall'].get('open', 0)} expired={shadow['overall'].get('expired', 0)}",
            f"closed={shadow['overall'].get('closed', 0)} expectancy={shadow['overall'].get('expectancy_r', 'n/a')}R",
            "Monitor open shadow calls", "NO",
        ))
    for name, view, label in [
        ("pending_breakout_shadow", pending_breakout, "XAUUSDm"),
        ("pending_signal_runner", pending_signal_runner, "pending signals"),
        ("pending_signal_bridge", pending_signal_bridge, "pending signals"),
    ]:
        if not view:
            continue
        stale = (view.get("last_tick_age_ms") or 0) > 15 * 60 * 1000
        status = "STALE" if view.get("alive") and stale else ("RUNNING" if view.get("alive") else "DOWN")
        issue = view.get("last_error") or ("stale tick/context" if stale else "—")
        components["Shadows"].append(_component(
            name, "Shadows", "SHADOW", status,
            f"{view.get('last_tick_age_ms', 'n/a')} ms tick age", view.get("pid") or "—",
            "EA shadow", label, "SHADOW", issue,
            f"engine={view.get('engine_state', '—')} active_trade={view.get('has_active_trade')}",
            "Diagnose feed before restart" if status == "STALE" else "Monitor", "NO",
        ))
    if pending_signal_shadow:
        readiness = "READY" if pending_signal_shadow.get("triggered") else "NOT_READY"
        components["Shadows"].append(_component(
            "pending_signal_shadow", "Shadows", "SHADOW", readiness,
            pending_signal_shadow.get("generated_at", "n/a"), "—", "—", "gold signals", "SHADOW",
            f"readiness={pending_signal_shadow.get('top_source') or 'n/a'}",
            f"signals={pending_signal_shadow.get('signals_received', 0)} open={pending_signal_shadow.get('open_orders_total', 0)}",
            "Keep collecting samples", "NO",
        ))
    if manual_exit_shadow:
        components["Shadows"].append(_component(
            "manual_exit_shadow_tracker", "Shadows", "SHADOW", "RUNNING" if manual_exit_shadow.get("alive") else "DOWN",
            manual_exit_shadow.get("last_run_utc") or "never", manual_exit_shadow.get("pid") or "—",
            "BA/EA/EM", "manual exits", "SHADOW",
            f"verdict={manual_exit_shadow.get('current_verdict')}",
            f"post_fix_live={manual_exit_shadow.get('post_fix_live_samples')}", "Monitor", "NO",
        ))

    components["Infrastructure"].append(_component(
        "status_dashboard", "Infrastructure", "INFRA", "RUNNING",
        f"cache age {cache_age_sec}s", "self", "local", "dashboard", "READ_ONLY",
        "—", "serves / and /monitor", "Monitor", "NO",
    ))
    components["Infrastructure"].append(_component(
        "Oracle SSH/cache", "Infrastructure", "INFRA",
        "ERROR" if oracle_meta.get("config_error") else ("RUNNING" if (oracle_meta.get("consecutive_failures") or 0) == 0 else "STALE"),
        f"last success {oracle_meta.get('last_success_ts') or 'never'}", "cache", "Oracle", "ssh",
        "READ_ONLY", oracle_meta.get("config_error") or f"failures={oracle_meta.get('consecutive_failures') or 0}",
        f"next_retry={oracle_meta.get('next_retry_ts') or 'n/a'}", "Reuse cache; do not add polling", "NO",
    ))
    for m in (market_context or {}).get("monitors", []):
        components["Infrastructure"].append(_component(
            m["name"], "Infrastructure", "INFRA", "RUNNING" if m["age_text"] != "N/A" else "UNKNOWN",
            m["age_text"], "—", "local", "market context", "READ_ONLY", "—", "heartbeat file",
            "Monitor", "NO",
        ))

    deductions = []
    def deduct(points, reason, evidence):
        deductions.append({"points": points, "reason": reason, "evidence": evidence})

    if any(a.get("error") for a in accounts):
        deduct(20, "Account fetch error", "; ".join(f"{a.get('name')}: {a.get('error')}" for a in accounts if a.get("error")))
    if DEPENDENCY_ERRORS:
        deduct(20, "Dashboard dependency failure", "; ".join(sorted(DEPENDENCY_ERRORS)))
    if not all(v is True for _, v, _ in oracle_services):
        deduct(15, "Expected-active Oracle service missing", "Only retired services are excluded")
    if risk_halt.get("halted"):
        deduct(20, "RISK_HALT active", risk_halt.get("since") or "active")
    stale_components = [c for rows in components.values() for c in rows if c["status"] == "STALE"]
    if stale_components:
        deduct(10, "Stale component data", ", ".join(c["name"] for c in stale_components[:4]))
    down_components = [c for rows in components.values() for c in rows if c["status"] == "DOWN"]
    if down_components:
        deduct(20, "Down expected component", ", ".join(c["name"] for c in down_components[:4]))
    if shadow and shadow["overall"].get("open", 0):
        deduct(5, "Open shadow calls need review", f"open={shadow['overall'].get('open')}")

    score = max(0, 100 - sum(d["points"] for d in deductions))
    if not deductions:
        deductions.append({"points": 0, "reason": "No current health deductions", "evidence": "all expected-active checks OK"})

    for c in [c for rows in components.values() for c in rows if c["status"] in ("ERROR", "DOWN", "STALE", "BLOCKED", "NOT_READY", "INCONCLUSIVE")]:
        if c["status"] == "BLOCKED" and "retired" in c["issue"]:
            continue
        problems.append({
            "severity": "HIGH" if c["status"] in ("ERROR", "DOWN") else "MEDIUM",
            "component": c["name"],
            "status": c["status"],
            "evidence": c["issue"],
            "user_action": "YES" if c["action"].startswith("No action unless") else "NO",
            "recommended_action": c["action"],
        })

    return {"components": components, "score": score, "deductions": deductions,
            "problems": problems, "oracle_attr": oracle_attr}


@app.route("/api/monitor")
def api_monitor():
    with _cache_lock:
        accounts = _cache["accounts"]
        oracle_meta = _cache["oracle_meta"]
    trades, mt5_errors, cache_ts = _get_trades(days=7)
    by_magic = _pa_call("group_by_magic", {}, trades)
    nts = _pa_call("no_trade_status", [{"bot": "portfolio_analytics", "status": "EXECUTION_ERROR",
                                        "reason": "runtime dependency failure", "last_trade_time": None,
                                        "time_since_last_trade_sec": None, "last_heartbeat_age_sec": None}], by_magic)
    risk_halt = _pa_call("risk_halt_status", {"halted": None, "status": "ERROR",
                                               "error": "portfolio_analytics runtime failure"})
    shadow = _shadow_view()
    pending_breakout = _pending_breakout_shadow_view()
    pending_signal_runner = _pending_signal_runner_view()
    pending_signal_bridge = _pending_signal_bridge_view()
    pending_signal_shadow = _pending_signal_shadow_view()
    manual_exit_shadow = _manual_exit_shadow_view()
    market_context = _monitor_context_view()
    oracle_attr = _get_oracle_attribution()
    cache_age_sec = round(time.time() - cache_ts)
    bot_health = bot_health_detail(accounts, risk_halt.get("halted") is not False)
    return jsonify({
        "no_trade_status": nts,
        "risk_halt": risk_halt,
        "asset_exposure": _pa_call("asset_exposure_summary", {}, accounts),
        "oracle_attribution": oracle_attr,
        "autonomy_challenge": _autonomy_challenge_view(nts),
        "shadow": shadow,
        "pending_breakout_shadow": pending_breakout,
        "pending_signal_runner": pending_signal_runner,
        "pending_signal_bridge": pending_signal_bridge,
        "pending_signal_shadow": pending_signal_shadow,
        "manual_exit_shadow": manual_exit_shadow,
        "observability": _observability_model(
            accounts, bot_health, oracle_meta, risk_halt, nts, shadow, pending_breakout,
            pending_signal_runner, pending_signal_bridge, pending_signal_shadow,
            manual_exit_shadow, market_context, oracle_attr, cache_age_sec,
        ),
        "mt5_errors": mt5_errors,
        "cached_age_sec": cache_age_sec,
    })


if __name__ == "__main__":
    if "--test" in sys.argv:
        _demo()
    elif "--preflight" in sys.argv:
        deployment_preflight()
        print("status_dashboard deployment preflight OK")
    else:
        # 2026-08-22 (Ahmed reported slow dashboard): Flask's dev server
        # defaults to handling one request at a time. With no threading,
        # any concurrent request (page load + its own JS polling /api/*,
        # or two people/tabs at once) queues behind whichever request is
        # already running -- the single biggest cause of perceived
        # slowness here, independent of how fast any individual handler
        # is. threaded=True is safe: shared state (_cache, _analytics_cache,
        # _prepared_trade_cache) is already lock-protected for the
        # background refresh_loop thread, so request threads join the
        # same protection, not a new hazard.
        app.run(host="0.0.0.0", port=5001, threaded=True)
