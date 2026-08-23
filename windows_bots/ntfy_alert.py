"""
ntfy_alert.py -- fire-and-forget push notifications via ntfy.sh.

Why this exists: PushNotification only works through a live Claude Code
conversation. Bots/guards run 24/7 independent of any Claude session, so a
critical event (margin call, set_stop failure, portfolio risk cutoff) that
happens while no session is open currently reaches nobody. alert() lets
those scripts reach Ahmed's phone directly.

Every call is wrapped so a slow/dead network or an ntfy.sh outage can NEVER
affect the calling bot -- alert() never raises and never blocks longer than
the timeout. This must stay true even if the module is edited later.

Usage:
    from ntfy_alert import alert
    alert("set_stop failed 3x on XAU/USDT", key="ladder_bybit_setstop_fail")

`key` identifies the alert for throttling (same key = same cooldown window,
default 15 min). Two different problems must use two different keys or the
second one gets silently throttled by the first.
"""
import json
import os
import time
import urllib.request

# 2026-08-03 SHADOW MODE: pointed at a private test topic while the
# integration is being verified. Ahmed must confirm delivery to his phone
# before this is switched to the real topic and wired into any live bot.
TOPIC = "<REDACTED_NTFY_TOPIC>"

THROTTLE_FILE = os.path.join(os.path.dirname(__file__), "ntfy_throttle.json")
THROTTLE_SECONDS = 900  # 15 min -- matches claude_loop_heartbeat's NOTIFY_SECONDS cadence


def _load_throttle():
    try:
        with open(THROTTLE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_throttle(data):
    tmp = THROTTLE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, THROTTLE_FILE)  # atomic -- never a half-written throttle file


def alert(msg, key=None, topic=None, throttle_seconds=THROTTLE_SECONDS):
    """Send a push notification. Returns True/False, never raises.
    key: throttle bucket -- repeated calls with the same key inside
    throttle_seconds are silently dropped so a stuck bot can't spam."""
    try:
        key = key or msg
        data = _load_throttle()
        now = time.time()
        last = data.get(key, 0)
        if now - last < throttle_seconds:
            return False
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic or TOPIC}",
            data=msg.encode("utf-8"),
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
        data[key] = now
        _save_throttle(data)
        return True
    except Exception:
        return False


def _demo():
    """ponytail: smallest check that fails if throttling or the
    never-raises guarantee breaks. Mocks the network call."""
    import urllib.request as _u

    global THROTTLE_FILE
    real_file = THROTTLE_FILE
    THROTTLE_FILE = THROTTLE_FILE + ".selftest"
    calls = {"n": 0}

    class FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        return FakeResp()

    orig = _u.urlopen
    _u.urlopen = fake_urlopen
    try:
        if os.path.exists(THROTTLE_FILE):
            os.remove(THROTTLE_FILE)
        assert alert("test message", key="demo") is True
        assert calls["n"] == 1
        # second call with same key inside the window must be throttled --
        # no network call, no exception
        assert alert("test message", key="demo") is False
        assert calls["n"] == 1, "throttled call must not hit the network"
        # different key must go through immediately
        assert alert("other message", key="demo2") is True
        assert calls["n"] == 2

        # never-raises guarantee: a broken urlopen must not propagate
        def broken_urlopen(req, timeout=None):
            raise OSError("network down")
        _u.urlopen = broken_urlopen
        assert alert("test message", key="demo3") is False, "network failure must return False, not raise"
        print("ntfy_alert self-check OK")
    finally:
        _u.urlopen = orig
        if os.path.exists(THROTTLE_FILE):
            os.remove(THROTTLE_FILE)
        THROTTLE_FILE = real_file


if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        _demo()
    elif len(sys.argv) > 1:
        ok = alert(" ".join(sys.argv[1:]), key="manual_cli")
        print("sent" if ok else "throttled or failed")
    else:
        print("usage: python ntfy_alert.py <message>   |   --test")
