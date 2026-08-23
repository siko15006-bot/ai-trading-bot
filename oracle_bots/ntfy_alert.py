"""
ntfy_alert.py -- fire-and-forget push notifications via ntfy.sh (Oracle/BAA side).
Mirror of the Windows-side C:\\TradingBot\\Bot_Active\\ntfy_alert.py -- same topic,
same throttle/never-raises contract, ported here because Oracle bots run in a
separate process/machine and can't import the Windows file directly.

Every call is wrapped so a slow/dead network or an ntfy.sh outage can NEVER
affect the calling bot -- alert() never raises and never blocks longer than
the timeout.

Usage:
    from ntfy_alert import alert
    alert("bybit_shield: naked position protected", key="bybit_shield_naked")
"""
import json
import os
import time
import urllib.request

TOPIC = "<REDACTED_NTFY_TOPIC>"

THROTTLE_FILE = os.path.join(os.path.dirname(__file__), "ntfy_throttle.json")
THROTTLE_SECONDS = 900  # 15 min


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
    os.replace(tmp, THROTTLE_FILE)


def alert(msg, key=None, topic=None, throttle_seconds=THROTTLE_SECONDS):
    """Send a push notification. Returns True/False, never raises."""
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
        assert alert("test", key="demo") is True
        assert calls["n"] == 1
        assert alert("test", key="demo") is False
        assert calls["n"] == 1
        assert alert("other", key="demo2") is True
        assert calls["n"] == 2

        def broken_urlopen(req, timeout=None):
            raise OSError("network down")
        _u.urlopen = broken_urlopen
        assert alert("test", key="demo3") is False
        print("ntfy_alert (oracle) self-check OK")
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
