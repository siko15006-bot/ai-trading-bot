"""
retroactive_qualification_check.py -- ISOLATED, READ-ONLY: for the 4
current KEEP groups, retroactively checks (1) whether any of their
already-logged signals were edited/deleted after posting (mutation --
Telethon exposes edit_date on a fetched message even retroactively),
and (2) whether the signal's entry zone was already stale vs the real
market price at the moment of posting (late_signal), using ccxt candle
data the same way the live evaluator does.

Uses a COPY of the live session file (never the original), disconnects
immediately after, deletes the copy. Does NOT modify signals_all.json,
group_stats.json, or tg_signal_bot.py. Writes its own separate output
file only.
"""
import asyncio
import json
import os
import shutil
import time
from datetime import datetime, timezone, timedelta

import ccxt
from telethon import TelegramClient
from telethon.tl.types import PeerChannel

ENV_PATH = os.path.expanduser("~/.env")
SESSION_SRC = os.path.expanduser("~/trading-bot/tg_session.session")
SESSION_COPY = os.path.expanduser("~/trading-bot/retro_check_readonly_copy.session")

GROUPS = {
    # 2026-08-16 re-run: new promotion candidates from the fresh KEEP
    # re-classification (analyze_signal_performance.py), per Ahmed's
    # standing evidence-gated promotion policy. VIP/BINANCE_360 already
    # live, skipped here to save Telegram API calls -- FOREX_GOLD_PRIME
    # and FX_RIVER_ACADEMY re-included since their prior mutation check
    # is now 8 days stale and they're candidates again.
    "FOREX_GOLD_PRIME": -1004397421755,
    "FX_RIVER_ACADEMY": -1001476143548,
    "ROYAL_GOLD_SIGNALS": -1003710550706,
    "PROFESSIONAL_RISK_CONTROL": -1002216665319,
    "XAUUSD_SIGNAL_FINDER": -1002226409599,
}
MATCH_WINDOW_S = 90  # signal ts vs message date tolerance
LATE_THRESHOLD_PCT = 0.15

ex = ccxt.bybit({"enableRateLimit": True})


def _load_env():
    env = {}
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v
    return env


def _ccxt_symbol(sig_symbol):
    if sig_symbol == "XAUUSD":
        return "XAU/USDT:USDT"
    if ":USDT" in sig_symbol:
        return sig_symbol
    return None


def _late_check(sig, ts):
    ref = _ccxt_symbol(sig["symbol"])
    if ref is None:
        return "UNKNOWN"
    try:
        bars = ex.fetch_ohlcv(ref, "1m", since=ts * 1000, limit=1)
        if not bars:
            return "UNKNOWN"
        price = bars[0][4]  # close of the first 1m bar at/after ts
    except Exception:
        return "UNKNOWN"
    lo, hi = sig["entry_low"], sig["entry_high"]
    if lo <= price <= hi:
        return False
    dist = min(abs(price - lo), abs(price - hi))
    return (dist / price * 100) > LATE_THRESHOLD_PCT


async def main():
    with open("/home/ubuntu/trading-bot/signals_all.json") as f:
        all_signals = json.load(f)

    env = _load_env()
    shutil.copyfile(SESSION_SRC, SESSION_COPY)
    client = TelegramClient(SESSION_COPY.replace(".session", ""), int(env["TG_API_ID"]), env["TG_API_HASH"])
    await client.connect()
    assert await client.is_user_authorized()

    results = {}
    for group, chat_id in GROUPS.items():
        sigs = [s for s in all_signals if s["group"] == group]
        print(f"\n=== {group}: {len(sigs)} logged signals, fetching real message history ===")
        entity = await client.get_entity(PeerChannel(abs(chat_id) - 1000000000000))

        ts_min = min(s["ts"] for s in sigs) - 60
        ts_max = max(s["ts"] for s in sigs) + 60
        messages_by_time = []
        async for msg in client.iter_messages(entity, offset_date=datetime.fromtimestamp(ts_max + 3600, tz=timezone.utc), reverse=False):
            if msg.date.timestamp() < ts_min - 3600:
                break
            messages_by_time.append(msg)
        print(f"  fetched {len(messages_by_time)} real messages in range")

        mutated = []
        matched_count = 0
        late_signals = []
        unknown_late = 0
        for s in sigs:
            best = None
            best_delta = MATCH_WINDOW_S + 1
            for msg in messages_by_time:
                delta = abs(msg.date.timestamp() - s["ts"])
                if delta < best_delta:
                    best_delta = delta
                    best = msg
            if best is not None and best_delta <= MATCH_WINDOW_S:
                matched_count += 1
                if best.edit_date is not None:
                    mutated.append({"ts": s["ts"], "msg_id": best.id, "edit_date": best.edit_date.isoformat()})

            late = _late_check(s, s["ts"])
            if late == "UNKNOWN":
                unknown_late += 1
            elif late is True:
                late_signals.append(s["ts"])

        results[group] = {
            "total_signals": len(sigs), "matched_to_real_message": matched_count,
            "unmatched": len(sigs) - matched_count,
            "mutations_found": len(mutated), "mutation_detail": mutated,
            "late_signals_found": len(late_signals), "late_signal_ts_list": late_signals[:20],
            "late_check_unknown": unknown_late,
        }
        print(f"  matched={matched_count}/{len(sigs)}  mutations={len(mutated)}  "
              f"late_signals={len(late_signals)}  late_check_unknown={unknown_late}")

    await client.disconnect()
    os.remove(SESSION_COPY)
    for ext in ("-journal", "-wal", "-shm"):
        p = SESSION_COPY + ext
        if os.path.exists(p):
            os.remove(p)

    out_path = "/home/ubuntu/trading-bot/retroactive_qualification_check_result.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nWritten: {out_path}")


asyncio.run(main())
