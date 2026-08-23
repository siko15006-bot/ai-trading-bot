"""attribution_hooks.py -- tag helpers for Oracle Bybit bots.
Backward-compatible with the existing short orderLinkId format.
"""
import json
import os
import time

INTENT_LEDGER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "attribution_intent.jsonl")

# Bybit's orderLinkId has a 36-character limit.
_MAX_TAG_LEN = 36


def make_tag(bot_short, symbol, side, suffix=""):
    """symbol like 'BTC/USDT:USDT' -> base asset 'BTC' for a short tag.
    Deterministic, human-readable, and backward-compatible."""
    base = symbol.split("/")[0]
    tag = f"{bot_short}-{base}-{side}-{int(time.time())}"
    if suffix:
        tag = f"{tag}-{suffix}"
    return tag[:_MAX_TAG_LEN]


def parse_tag(tag):
    if not tag:
        return None
    raw = str(tag)
    if "|" in raw:
        parts = raw.split("|")
        strategy = parts[0] or None
        payload = {}
        for piece in parts[1:]:
            if "=" in piece:
                key, value = piece.split("=", 1)
                payload[key.strip().lower()] = value.strip()
        ts = payload.get("ts")
        return {
            "raw": raw,
            "format": "pipe",
            "strategy": strategy,
            "bot": payload.get("bot"),
            "symbol": payload.get("symbol"),
            "direction": payload.get("direction"),
            "event_id": payload.get("event"),
            "timestamp": int(ts) if ts and ts.isdigit() else None,
            "parent": payload.get("parent"),
            "payload": payload,
        }
    if "-" in raw:
        parts = raw.split("-")
        strategy = parts[0] if parts else None
        symbol = parts[1] if len(parts) > 1 else None
        direction = parts[2] if len(parts) > 2 else None
        event_id = parts[3] if len(parts) > 3 else None
        ts_piece = parts[-1] if len(parts) >= 4 else None
        try:
            timestamp = int(ts_piece) if ts_piece is not None else None
        except Exception:
            timestamp = None
        return {
            "raw": raw,
            "format": "legacy",
            "strategy": strategy,
            "bot": strategy,
            "symbol": symbol,
            "direction": direction,
            "event_id": event_id,
            "timestamp": timestamp,
            "parent": None,
            "payload": {},
        }
    return {
        "raw": raw,
        "format": "unknown",
        "strategy": raw,
        "bot": raw,
        "symbol": None,
        "direction": None,
        "event_id": None,
        "timestamp": None,
        "parent": None,
        "payload": {},
    }


def tag_matches_strategy(tag, strategy):
    parsed = parse_tag(tag)
    if not parsed:
        return False
    return parsed.get("strategy") == strategy or str(tag).startswith(strategy)


def record_intent(bot, symbol, side, qty, tag, extra=None):
    """Best-effort local append -- swallows every exception."""
    try:
        row = {"ts_utc": time.time(), "bot": bot, "symbol": symbol, "side": side,
               "qty": qty, "order_link_id": tag}
        if extra:
            row["extra"] = extra
        with open(INTENT_LEDGER_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        pass


def _demo():
    """ponytail: smallest check that fails if tagging/logging breaks."""
    import tempfile
    global INTENT_LEDGER_PATH
    orig = INTENT_LEDGER_PATH
    d = tempfile.mkdtemp()
    INTENT_LEDGER_PATH = os.path.join(d, "intent.jsonl")
    try:
        tag = make_tag("liqsweep", "BTC/USDT:USDT", "sell")
        assert tag.startswith("liqsweep-BTC-sell-") and len(tag) <= _MAX_TAG_LEN
        assert parse_tag(tag)["strategy"] == "liqsweep"
        assert tag_matches_strategy(tag, "liqsweep")
        record_intent("liquidity_sweep_bot", "BTC/USDT:USDT", "sell", 0.005, tag)
        with open(INTENT_LEDGER_PATH, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
        assert len(rows) == 1 and rows[0]["order_link_id"] == tag
        INTENT_LEDGER_PATH = "/nonexistent_dir_xyz/cannot_write.jsonl"
        record_intent("x", "BTC/USDT:USDT", "buy", 1, "tag")
        print("attribution_hooks self-check: OK")
    finally:
        INTENT_LEDGER_PATH = orig


if __name__ == "__main__":
    _demo()
