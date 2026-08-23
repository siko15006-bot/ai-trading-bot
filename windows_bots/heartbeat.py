"""heartbeat.py -- shared functional-heartbeat writer for all bots.
Standard established 2026-08-06 after the ema_adx_bot 8.5h silent-hang
incident (see NOTIFICATION_POLICY.md "Functional Heartbeat" section).

Write ONLY after a real, successfully-completed unit of work (a full
poll cycle, a signal evaluated, an order processed) -- never on process
start alone, never on an independent timer. That's what makes this prove
the bot is actually doing its job, not just that the process exists.
"""
import json
import os
from datetime import datetime, timezone

BOT_ACTIVE_DIR = os.path.dirname(__file__)


def write_heartbeat(name, **extra):
    """name becomes the filename: heartbeat_<name>.json. Never raises --
    a heartbeat failure must not be allowed to break the calling bot."""
    path = os.path.join(BOT_ACTIVE_DIR, f"heartbeat_{name}.json")
    try:
        payload = {"last_success_ts": datetime.now(timezone.utc).isoformat()}
        payload.update(extra)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)  # atomic on Windows/NTFS -- a reader never sees a half-written file
    except Exception:
        pass


def read_heartbeat(name):
    path = os.path.join(BOT_ACTIVE_DIR, f"heartbeat_{name}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def classify(name, healthy_max_age_sec, degraded_max_age_sec, process_alive=True):
    """Returns (status, reason) -- the general-purpose version of the
    classifier proven against the ema_adx incident replay."""
    data = read_heartbeat(name)
    if data is None:
        return "Critical", f"{name}: no heartbeat file -- never completed a cycle"
    last = datetime.fromisoformat(data["last_success_ts"])
    age = (datetime.now(timezone.utc) - last).total_seconds()
    if not process_alive:
        return "Critical", f"{name}: process not running (last success {age:.0f}s ago)"
    if age <= healthy_max_age_sec:
        return "Healthy", f"{name}: last success {age:.0f}s ago"
    if age <= degraded_max_age_sec:
        return "Degraded", f"{name}: last success {age:.0f}s ago -- beyond normal cadence"
    return "Critical", f"{name}: last success {age:.0f}s ago -- process alive but not completing cycles"


def _demo():
    import tempfile
    global BOT_ACTIVE_DIR
    orig = BOT_ACTIVE_DIR
    BOT_ACTIVE_DIR = tempfile.gettempdir()

    write_heartbeat("demo_bot", cycle_count=1)
    status, reason = classify("demo_bot", healthy_max_age_sec=60, degraded_max_age_sec=300)
    assert status == "Healthy", reason
    print(f"fresh heartbeat -> {status}: {reason}")

    status, reason = classify("nonexistent_bot", healthy_max_age_sec=60, degraded_max_age_sec=300)
    assert status == "Critical", reason
    print(f"missing heartbeat -> {status}: {reason}")

    os.remove(os.path.join(BOT_ACTIVE_DIR, "heartbeat_demo_bot.json"))
    BOT_ACTIVE_DIR = orig
    print("heartbeat._demo(): all checks passed")


if __name__ == "__main__":
    _demo()
