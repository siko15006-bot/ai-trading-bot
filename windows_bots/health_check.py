"""health_check.py -- system-wide bot health, heartbeat-first.
2026-08-06 rollout: process existence is checked ONLY as a secondary
signal (to distinguish "no heartbeat because never started" from "no
heartbeat because hung/crashed"). The heartbeat age is what actually
decides Healthy / Degraded / Critical -- this is the fix for the
ema_adx_bot incident, where "the process exists" was the only check and
it said nothing about whether the bot was actually working.

Usage: python health_check.py
"""
import subprocess
import sys

sys.path.insert(0, r"C:\TradingBot\Bot_Active")
from heartbeat import classify

# (heartbeat_name, healthy_max_sec, degraded_max_sec, process_match_substring)
# Thresholds = 3x / 10x the bot's own poll interval (the rule established
# for ema_adx_bot). process_match_substring=None means "not currently
# launched" -- reported as N/A, not scored, per Ahmed's rollout scope
# (code-patched but not live-tested for stopped bots).
REGISTRY = [
    # process_running() matches by script filename only (no per-account argv
    # disambiguation -- multi-instance bots' account suffixes like `" EA"`
    # kept getting corrupted crossing subprocess/PowerShell string-quoting
    # layers). Coarse but sufficient: heartbeat freshness is what actually
    # decides the per-instance status; this only tells us "is at least one
    # process of this bot type alive," used solely to distinguish a stale
    # heartbeat from a fully-stopped bot.
    ("ea_shield_EA", 15, 50, "ea_shield.py"),
    ("ea_shield_BA", 15, 50, "ea_shield.py"),
    ("ea_shield_EM", 15, 50, "ea_shield.py"),
    ("ladder_guard_EA", 45, 150, "ladder_guard.py"),
    ("ladder_guard_BA", 45, 150, "ladder_guard.py"),
    ("ladder_guard_EM", 45, 150, "ladder_guard.py"),
    ("ladder_guard_EA_loop", 45, 150, "ladder_guard.py"),
    ("ladder_guard_BA_loop", 45, 150, "ladder_guard.py"),
    ("ladder_guard_EM_loop", 45, 150, "ladder_guard.py"),
    ("orb_eth_exness_EA", 105, 350, "orb_eth_exness.py"),
    ("orb_eth_exness_EM", 105, 350, "orb_eth_exness.py"),
    ("fast_move_watch", 90, 300, "fast_move_watch.py"),
    ("grind_watch", 270, 900, "grind_watch.py"),
    ("participation_pilot_btc_range", 90, 300, "participation_pilot_btc_range.py"),
    ("fx_signal_exec", 3600, 21600, "fx_signal_exec.py"),  # event-driven, generous
    ("fx_signal_exec_loop", 180, 600, "fx_signal_exec.py"),
    ("swing_pending_bot_EA", None, None, None),  # halted per Ahmed 2026-08-04
    ("swing_pending_bot_BA", None, None, None),
    ("gold_btc_bot_EA", None, None, None),  # status unresolved, not launched
    ("local_crypto_watch", None, None, None),
    ("regime_detector", 180, 600, "regime_detector.py"),
    ("detection_miss_monitor", None, None, None),
    ("outcome_calibration_log", 60, 180, "outcome_calibration_log.py"),
    ("hypergold_scalp_shadow", 900, 3600, "hypergold_scalp_shadow"),
]

# ema_adx_bot predates the shared heartbeat.py module and keeps its own
# filename pattern (ema_adx_heartbeat_{ACC}.json, not heartbeat_{name}.json)
# -- documented difference, not an oversight.
EMA_ADX_FILES = {"ema_adx_EA": (r"C:\TradingBot\Bot_Active\ema_adx_heartbeat_EA.json", 270, 900, "ema_adx_bot.py"),
                  "ema_adx_BA": (r"C:\TradingBot\Bot_Active\ema_adx_heartbeat_BA.json", 270, 900, "ema_adx_bot.py")}


_PS_SCRIPT = r"""
param([string]$Pattern)
# @(...) forces array-wrapping -- without it, Windows PowerShell 5.1's
# Where-Object returns a single bare object (not an array) when exactly
# one match exists, and a single non-array object has no .Count property
# at all (returns $null, not 1) -- silently misreported every single-
# instance bot as "not running" until this was caught.
@(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'python.exe' -and $_.CommandLine -like "*$Pattern*"
}).Count
"""
import os as _os
_PS_SCRIPT_PATH = _os.path.join(_os.path.dirname(__file__), "_health_check_proc_match.ps1")
with open(_PS_SCRIPT_PATH, "w", encoding="utf-8") as _f:
    _f.write(_PS_SCRIPT)


def process_running(substr):
    """Runs a real .ps1 FILE rather than an inline -Command string --
    inline strings kept getting mangled by nested shell/tool escaping
    layers ($_ and $args stripped before PowerShell ever saw them). A
    script file sidesteps all of that: plain PowerShell syntax, no
    escaping concerns."""
    if substr is None:
        return None
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-File", _PS_SCRIPT_PATH, "-Pattern", substr],
            capture_output=True, text=True, timeout=15)
        return int(out.stdout.strip() or "0") > 0
    except Exception:
        return None


def main():
    results = []
    for name, healthy, degraded, proc_substr in REGISTRY:
        if healthy is None:
            results.append((name, "N/A", "not currently launched -- code patched, not tested live"))
            continue
        alive = process_running(proc_substr)
        status, reason = classify(name, healthy, degraded, process_alive=bool(alive))
        results.append((name, status, reason))

    import json
    for name, (path, healthy, degraded, proc_substr) in EMA_ADX_FILES.items():
        alive = process_running(proc_substr)
        try:
            with open(path) as f:
                data = json.load(f)
            from datetime import datetime, timezone
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(data["last_success_ts"])).total_seconds()
            if not alive:
                status, reason = "Critical", f"process not running ({age:.0f}s since last success)"
            elif age <= healthy:
                status, reason = "Healthy", f"last success {age:.0f}s ago"
            elif age <= degraded:
                status, reason = "Degraded", f"last success {age:.0f}s ago"
            else:
                status, reason = "Critical", f"last success {age:.0f}s ago -- stale"
        except FileNotFoundError:
            status, reason = "Critical", "no heartbeat file"
        results.append((name, status, reason))

    print(f"{'BOT':<28} {'STATUS':<10} REASON")
    print("-" * 90)
    for name, status, reason in sorted(results, key=lambda r: (r[1] != "Critical", r[1] != "Degraded", r[0])):
        print(f"{name:<28} {status:<10} {reason}")

    counts = {}
    for _, status, _ in results:
        counts[status] = counts.get(status, 0) + 1
    print("-" * 90)
    print("Summary:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
