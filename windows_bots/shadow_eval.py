"""shadow_eval.py -- risk-free scoreboard for shadow trade calls (Codex/Claude)
during the frozen 7-day challenge. ZERO execution: it only reads an append-only
event log and aggregates it. No orders, no MT5/ccxt writes, no strategy state.

Event log: shadow_trades.jsonl (append only, one JSON object per line)
  open : {"type":"open","id":"sh-001","ts":"<iso>","agent":"codex","symbol":"XAUUSD",
          "side":"buy","entry":4340,"sl":4327,"tp":4371,"valid_until":"<iso>","rationale":"..."}
  close: {"type":"close","id":"sh-001","ts":"<iso>","status":"HIT_TP","exit":4371,"r":2.38}

Claude records the `close` events each monitoring cycle from live prices (score()
computes status+R). An OPEN call with no close is still open. R for a loss = -1.0
by definition (SL distance = 1R). Expectancy = mean R over CLOSED calls.

# ponytail: cycle-sampled outcomes -- an intra-cycle wick between price samples can
# be missed. Fine for a shadow measure; upgrade to tick data only if we trade off these.
"""
import json
import os
from datetime import datetime, timezone

BASE = os.path.dirname(__file__)
TRADES_FILE = os.path.join(BASE, "shadow_trades.jsonl")
BOARD_FILE = os.path.join(BASE, "shadow_scoreboard.json")


def score(trade, price, now=None):
    """Pure. Given an open `trade` dict and current `price`, return (status, r).
    status in OPEN/HIT_TP/HIT_SL/EXPIRED/INVALID. r is None while OPEN."""
    side = str(trade.get("side", "")).lower()
    entry, sl, tp = trade.get("entry"), trade.get("sl"), trade.get("tp")
    if None in (entry, sl, tp) or entry == sl:
        return ("INVALID", 0.0)
    risk = abs(entry - sl)
    long = side in ("buy", "long")
    if long:
        if price >= tp:
            return ("HIT_TP", round((tp - entry) / risk, 2))
        if price <= sl:
            return ("HIT_SL", -1.0)
    else:
        if price <= tp:
            return ("HIT_TP", round((entry - tp) / risk, 2))
        if price >= sl:
            return ("HIT_SL", -1.0)
    vu = trade.get("valid_until")
    if now and vu and now >= datetime.fromisoformat(vu):
        r = ((price - entry) if long else (entry - price)) / risk
        return ("EXPIRED", round(r, 2))
    return ("OPEN", None)


def _read_events(path=TRADES_FILE):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def scoreboard(events):
    """Aggregate open/close events into per-agent + overall stats."""
    opens = {e["id"]: e for e in events if e.get("type") == "open"}
    closes = {e["id"]: e for e in events if e.get("type") == "close"}
    agents = {}
    overall = {"total": 0, "open": 0, "closed": 0, "wins": 0, "losses": 0,
               "expired": 0, "sum_r": 0.0}
    for tid, o in opens.items():
        a = agents.setdefault(o.get("agent", "?"),
                              {"total": 0, "open": 0, "closed": 0, "wins": 0,
                               "losses": 0, "expired": 0, "sum_r": 0.0})
        for bucket in (a, overall):
            bucket["total"] += 1
        c = closes.get(tid)
        if not c:
            a["open"] += 1; overall["open"] += 1
            continue
        st, r = c.get("status"), c.get("r", 0.0) or 0.0
        for bucket in (a, overall):
            bucket["closed"] += 1
            bucket["sum_r"] += r
            if st == "HIT_TP":
                bucket["wins"] += 1
            elif st == "HIT_SL":
                bucket["losses"] += 1
            elif st == "EXPIRED":
                bucket["expired"] += 1

    def finalize(b):
        b["win_rate_pct"] = round(b["wins"] / b["closed"] * 100, 1) if b["closed"] else 0.0
        b["expectancy_r"] = round(b["sum_r"] / b["closed"], 2) if b["closed"] else 0.0
        b["sum_r"] = round(b["sum_r"], 2)
        return b

    return {"overall": finalize(overall),
            "by_agent": {k: finalize(v) for k, v in agents.items()}}


def write_board(path=BOARD_FILE):
    board = scoreboard(_read_events())
    board["updated_utc"] = datetime.now(timezone.utc).isoformat()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(board, f, indent=2)
    os.replace(tmp, path)
    return board


def _selftest():
    long = {"side": "buy", "entry": 100.0, "sl": 90.0, "tp": 130.0}
    assert score(long, 131)[0] == "HIT_TP" and score(long, 131)[1] == 3.0
    assert score(long, 89) == ("HIT_SL", -1.0)
    assert score(long, 110) == ("OPEN", None)
    short = {"side": "sell", "entry": 100.0, "sl": 110.0, "tp": 80.0}
    assert score(short, 79)[0] == "HIT_TP" and score(short, 79)[1] == 2.0
    assert score(short, 111) == ("HIT_SL", -1.0)
    evts = [
        {"type": "open", "id": "1", "agent": "codex", "side": "buy", "entry": 100, "sl": 90, "tp": 130},
        {"type": "close", "id": "1", "status": "HIT_TP", "r": 3.0},
        {"type": "open", "id": "2", "agent": "codex", "side": "buy", "entry": 100, "sl": 90, "tp": 130},
        {"type": "close", "id": "2", "status": "HIT_SL", "r": -1.0},
        {"type": "open", "id": "3", "agent": "codex", "side": "buy", "entry": 100, "sl": 90, "tp": 130},
    ]
    b = scoreboard(evts)["overall"]
    assert b["total"] == 3 and b["closed"] == 2 and b["open"] == 1, b
    assert b["wins"] == 1 and b["losses"] == 1, b
    assert b["win_rate_pct"] == 50.0 and b["expectancy_r"] == 1.0, b
    print("shadow_eval selftest OK")


if __name__ == "__main__":
    import sys
    if "--board" in sys.argv:
        print(json.dumps(write_board(), indent=2, ensure_ascii=False))
    else:
        _selftest()
