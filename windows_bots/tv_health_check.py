"""
tv_health_check.py -- keeps TradingView Desktop's CDP endpoint (port 9222)
reachable AND actually connectable.

2026-07-30 incident: the app was running with --remote-debugging-port=9222
(port open, /json/version answering) while still rejecting every real
WebSocket handshake with 403 Forbidden -- the --remote-allow-origins flag
was missing, so port-only checks would have reported healthy the whole
time. is_healthy() below performs a REAL WebSocket handshake against a live
page target, not just an HTTP GET, so it can't repeat that false-positive.

Every CHECK_INTERVAL seconds: confirm the handshake succeeds. If not, kill
every TradingView.exe process and relaunch with both required flags.
RESTART_COOLDOWN prevents hammering the app with repeated relaunches if it
keeps failing for an unrelated reason (e.g. Windows blocking the launch).
"""
import subprocess
import time

import requests
import websocket

TV_EXE = (r"C:\Program Files\WindowsApps\TradingView.Desktop_3.3.0.7992_x64__n534cwy3pjxzj"
          r"\TradingView.exe")
CHECK_INTERVAL = 60
RESTART_COOLDOWN = 120
HEARTBEAT_PATH = r"C:\TradingBot\Bot_Active\tv_health_check.heartbeat"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def is_healthy():
    try:
        r = requests.get("http://localhost:9222/json", timeout=5)
        r.raise_for_status()
        targets = r.json()
        pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        if not pages:
            return False, "CDP up but no page targets"
        ws = websocket.create_connection(pages[0]["webSocketDebuggerUrl"], timeout=5)
        ws.close()
        return True, "ok"
    except Exception as e:
        return False, str(e)[:150]


def restart_tv():
    log("TV_UNHEALTHY -- restarting TradingView Desktop with CDP flags")
    try:
        subprocess.run(["taskkill", "/IM", "TradingView.exe", "/F"], capture_output=True)
    except Exception as e:
        log(f"taskkill error (continuing): {e}")
    time.sleep(3)
    try:
        subprocess.Popen([TV_EXE, "--remote-debugging-port=9222", "--remote-allow-origins=*"])
        log("TV relaunched")
    except Exception as e:
        log(f"relaunch FAILED: {e}")


def main():
    log(f"tv_health_check started -- verifying a real CDP WebSocket handshake every {CHECK_INTERVAL}s")
    last_restart = 0.0
    while True:
        try:
            with open(HEARTBEAT_PATH, "w") as f:
                f.write(str(time.time()))
        except Exception:
            pass
        ok, detail = is_healthy()
        if ok:
            log("TV_HEALTHY")
        else:
            log(f"TV_UNHEALTHY: {detail}")
            if time.time() - last_restart > RESTART_COOLDOWN:
                restart_tv()
                last_restart = time.time()
            else:
                log("restart skipped -- within cooldown, avoiding a crash-loop against the app")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
