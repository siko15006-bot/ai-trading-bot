"""
control_api.py — backend for the Status Dashboard's Control Layer (port 5002).

Ahmed's 2026-07-28 UI-only Control Layer request left status_dashboard.py's
proxy fully built (see its /ui/control/* routes) pointing at this file, which
never existed. Contract matched exactly to what that proxy already expects:
  GET  /api/control/status/<command_id>  -> 404 unknown id (= gate open),
                                             503 while CONTROL_LAYER_ENABLED
                                             is False (checked in before_request)
  POST /api/control/close                {command_id, account, ticket, symbol}
  POST /api/control/breakeven            {command_id, account, ticket, symbol}
  POST /api/control/close_all/prepare    {account} -> {ok, count, tickets, snapshot_id}
  POST /api/control/close_all/confirm    {snapshot_id, confirm} (confirm must be
                                          the literal string "CONFIRM CLOSE ALL")

Reuses the exact MT5 connect/close/breakeven pattern already proven in
e2_ctl.py / em_ctl.py / ladder_guard.py's ACCOUNTS -- one mt5.initialize()
per request scoped by explicit terminal path, mt5.shutdown() when done, same
as those scripts. No new abstraction: this is that same pattern wearing an
HTTP face for the dashboard's buttons.

Security (2026-08-01, added here rather than skipped -- ngrok is live-tunneling
status_dashboard.py's port 5001 to the internet right now, and its proxy has
no auth of its own): every /api/control/* call must carry the header
X-Control-Secret matching control_secret.txt (also read by status_dashboard.py
so the proxy can forward it). Wrong/missing secret -> 401, checked before the
CONTROL_LAYER_ENABLED gate so a real attacker without the file can never even
learn whether the layer is enabled.

DRY_RUN=False, CONTROL_LAYER_ENABLED=True (Ahmed's explicit go-ahead
2026-08-01): buttons clicked from the dashboard now place real close/SLTP
orders. Flip DRY_RUN back to True to simulate without touching MT5 if that's
ever needed again.
"""
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime

import MetaTrader5 as mt5
from flask import Flask, jsonify, request

BOT_DIR = os.path.dirname(__file__)
ACTION_LOG_PATH = os.path.join(BOT_DIR, "control_api_actions.log")


def log_action(msg):
    """2026-08-01: werkzeug's access log only ever showed method+path+status
    (e.g. "POST /api/control/close 200"), never which account/ticket or what
    the result actually was -- useless for debugging "the button didn't work"
    after the fact. This writes the full request+result for every mutating
    call."""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(ACTION_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
SECRET_FILE = os.path.join(BOT_DIR, "control_secret.txt")
CONTROL_SECRET = open(SECRET_FILE, encoding="utf-8").read().strip()

CONTROL_LAYER_ENABLED = True
DRY_RUN = False

# same three accounts/paths as ladder_guard.py's ACCOUNTS / e2_ctl.py / em_ctl.py --
# explicit path always, bare initialize() attaches to the last-active terminal
# and silently watches the wrong account (ea_shield incident 2026-07-06).
ACCOUNTS = {
    "BA": dict(path=r"C:\Program Files\MetaTrader 5\terminal64.exe"),
    "EA": dict(path=r"C:\MT5_Portable_2\terminal64.exe",
               login=<REDACTED_MT5_LOGIN_EA>, password="<REDACTED_MT5_PASSWORD_EA>", server="Exness-MT5Real33"),
    "EM": dict(path=r"C:\MT5_Portable_3\terminal64.exe",
               login=<REDACTED_MT5_LOGIN_EM>, password="<REDACTED_MT5_PASSWORD_EM>", server="Exness-MT5Real35"),
}

SNAPSHOT_TTL_SEC = 120  # close_all/confirm must follow prepare within this window

_snapshots = {}   # snapshot_id -> {account, tickets, ts}
_commands = {}    # command_id -> result dict, for the /status probe route
_lock = threading.Lock()  # serializes all MT5 access (one terminal connection at a time per process)

app = Flask(__name__)


def _with_account(account, fn):
    """mt5.initialize -> fn(mt5) -> mt5.shutdown, mirroring e2_ctl.py exactly."""
    cfg = ACCOUNTS[account]
    with _lock:
        if not mt5.initialize(**cfg):
            return {"ok": False, "error": f"mt5 init failed: {mt5.last_error()}"}
        try:
            return fn()
        finally:
            mt5.shutdown()


def _find_position(ticket):
    positions = mt5.positions_get()
    for p in (positions or []):
        if p.ticket == ticket:
            return p
    return None


def _do_close(p):
    if DRY_RUN:
        return {"ok": True, "dry_run": True, "ticket": p.ticket, "symbol": p.symbol}
    tick = mt5.symbol_info_tick(p.symbol)
    if tick is None:
        return {"ok": False, "error": f"no quotes for {p.symbol} (market closed?)"}
    otype = mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY
    price = tick.bid if p.type == 0 else tick.ask
    r = mt5.order_send(dict(action=mt5.TRADE_ACTION_DEAL, position=p.ticket,
                             symbol=p.symbol, volume=p.volume, type=otype, price=price,
                             comment="dashboard_close", type_filling=mt5.ORDER_FILLING_IOC))
    return {"ok": r.retcode == mt5.TRADE_RETCODE_DONE, "retcode": r.retcode,
            "ticket": p.ticket, "symbol": p.symbol}


def _do_breakeven(p):
    if DRY_RUN:
        return {"ok": True, "dry_run": True, "ticket": p.ticket, "sl": p.price_open}
    r = mt5.order_send(dict(action=mt5.TRADE_ACTION_SLTP, position=p.ticket,
                             symbol=p.symbol, sl=p.price_open, tp=p.tp))
    return {"ok": r.retcode == mt5.TRADE_RETCODE_DONE, "retcode": r.retcode,
            "ticket": p.ticket, "sl": p.price_open}


@app.before_request
def _gate():
    if not request.path.startswith("/api/control/"):
        return None
    if request.headers.get("X-Control-Secret") != CONTROL_SECRET:
        return jsonify({"ok": False, "error": "bad or missing X-Control-Secret"}), 401
    if not CONTROL_LAYER_ENABLED:
        return jsonify({"ok": False, "error": "CONTROL_LAYER_ENABLED is False"}), 503
    return None


@app.route("/api/control/status/<command_id>")
def control_status(command_id):
    result = _commands.get(command_id)
    if result is None:
        return jsonify({"ok": False, "error": "unknown command_id"}), 404
    return jsonify(result)


@app.route("/api/control/close", methods=["POST"])
def control_close():
    body = request.get_json(force=True, silent=True) or {}
    account, ticket, symbol = body.get("account"), body.get("ticket"), body.get("symbol")
    command_id = body.get("command_id") or str(uuid.uuid4())
    if account not in ACCOUNTS:
        return jsonify({"ok": False, "error": f"unknown account {account}"}), 400

    def op():
        p = _find_position(ticket)
        if p is None:
            return {"ok": False, "error": f"ticket {ticket} not found on {account}"}
        if symbol and p.symbol != symbol:
            return {"ok": False, "error": f"symbol mismatch: expected {symbol}, position is {p.symbol}"}
        return _do_close(p)

    result = _with_account(account, op)
    _commands[command_id] = result
    log_action(f"CLOSE account={account} ticket={ticket} symbol={symbol} -> {result}")
    return jsonify(result), (200 if result.get("ok") else 409)


@app.route("/api/control/breakeven", methods=["POST"])
def control_breakeven():
    body = request.get_json(force=True, silent=True) or {}
    account, ticket, symbol = body.get("account"), body.get("ticket"), body.get("symbol")
    command_id = body.get("command_id") or str(uuid.uuid4())
    if account not in ACCOUNTS:
        return jsonify({"ok": False, "error": f"unknown account {account}"}), 400

    def op():
        p = _find_position(ticket)
        if p is None:
            return {"ok": False, "error": f"ticket {ticket} not found on {account}"}
        if symbol and p.symbol != symbol:
            return {"ok": False, "error": f"symbol mismatch: expected {symbol}, position is {p.symbol}"}
        return _do_breakeven(p)

    result = _with_account(account, op)
    _commands[command_id] = result
    log_action(f"BREAKEVEN account={account} ticket={ticket} symbol={symbol} -> {result}")
    return jsonify(result), (200 if result.get("ok") else 409)


@app.route("/api/control/close_all/prepare", methods=["POST"])
def control_close_all_prepare():
    body = request.get_json(force=True, silent=True) or {}
    account = body.get("account")
    if account not in ACCOUNTS:
        return jsonify({"ok": False, "error": f"unknown account {account}"}), 400

    def op():
        positions = mt5.positions_get() or []
        return {"ok": True, "tickets": [p.ticket for p in positions]}

    result = _with_account(account, op)
    if not result.get("ok"):
        log_action(f"CLOSE_ALL_PREPARE account={account} -> FAILED {result}")
        return jsonify(result), 502
    snapshot_id = str(uuid.uuid4())
    _snapshots[snapshot_id] = {"account": account, "tickets": result["tickets"], "ts": time.time()}
    log_action(f"CLOSE_ALL_PREPARE account={account} tickets={result['tickets']} snapshot={snapshot_id}")
    return jsonify({"ok": True, "count": len(result["tickets"]), "tickets": result["tickets"],
                     "snapshot_id": snapshot_id})


@app.route("/api/control/close_all/confirm", methods=["POST"])
def control_close_all_confirm():
    body = request.get_json(force=True, silent=True) or {}
    snapshot_id, confirm = body.get("snapshot_id"), body.get("confirm")
    if confirm != "CONFIRM CLOSE ALL":
        return jsonify({"ok": False, "error": "confirm phrase mismatch"}), 400
    snap = _snapshots.pop(snapshot_id, None)
    if snap is None:
        return jsonify({"ok": False, "error": "unknown or already-used snapshot_id"}), 400
    if time.time() - snap["ts"] > SNAPSHOT_TTL_SEC:
        return jsonify({"ok": False, "error": "snapshot expired, prepare again"}), 400

    def op():
        results = []
        for ticket in snap["tickets"]:
            p = _find_position(ticket)
            if p is None:
                results.append({"ok": True, "ticket": ticket, "note": "already closed"})
            else:
                results.append(_do_close(p))
        return {"ok": all(r.get("ok") for r in results), "results": results}

    result = _with_account(snap["account"], op)
    log_action(f"CLOSE_ALL_CONFIRM snapshot={snapshot_id} account={snap['account']} -> {result}")
    return jsonify(result), (200 if result.get("ok") else 409)


def _demo():
    """Self-check for the money-affecting logic that doesn't need real MT5:
    the secret gate, the enabled gate, and the close_all snapshot lifecycle
    (expiry, one-time use, wrong-phrase rejection)."""
    app.testing = True
    client = app.test_client()
    H = {"X-Control-Secret": CONTROL_SECRET}

    r = client.get("/api/control/status/x")
    assert r.status_code == 401, "missing secret must be rejected before anything else"

    r = client.get("/api/control/status/x", headers=H)
    assert r.status_code == 404, "unknown command_id with correct secret should be 404 (gate open)"

    global CONTROL_LAYER_ENABLED
    CONTROL_LAYER_ENABLED = False
    r = client.get("/api/control/status/x", headers=H)
    assert r.status_code == 503, "disabled layer must 503 even with correct secret"
    CONTROL_LAYER_ENABLED = True

    r = client.post("/api/control/close_all/prepare", headers=H, json={"account": "NOPE"})
    assert r.status_code == 400, "unknown account must be rejected"

    _snapshots["fake-1"] = {"account": "EA", "tickets": [111, 222], "ts": time.time() - 999}
    r = client.post("/api/control/close_all/confirm", headers=H,
                     json={"snapshot_id": "fake-1", "confirm": "CONFIRM CLOSE ALL"})
    assert r.status_code == 400 and "expired" in r.get_json()["error"]
    assert "fake-1" not in _snapshots, "expired snapshot must still be consumed, not retried"

    _snapshots["fake-2"] = {"account": "EA", "tickets": [111], "ts": time.time()}
    r = client.post("/api/control/close_all/confirm", headers=H,
                     json={"snapshot_id": "fake-2", "confirm": "wrong phrase"})
    assert r.status_code == 400
    assert "fake-2" in _snapshots, "wrong confirm phrase must not consume the snapshot"

    print("control_api self-check OK")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _demo()
    else:
        app.run(host="127.0.0.1", port=5002, threaded=False)
