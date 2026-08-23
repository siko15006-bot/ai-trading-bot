"""
outcome_calibration_log.py — closes the loop Ahmed asked for: not "did a
detector fire" (detection_miss_monitor already checks that) but "was firing
actually *right*, in hindsight." For every FAST MOVE / GRIND alert on
local_crypto_watch.log: record the detector's implied direction, the
regime/vol_state regime_detector had for that symbol at that moment, and
Ollama's structured interpretation -- then check back at +5m/+15m/+30m
against real price and grade each of those three claims independently as
CORRECT / WRONG / UNCERTAIN. Read-only against the market (only fetches
public tickers); never places or touches any order.

Why grade the LLM claim mechanically instead of asking a second model to
judge it: tripwire_explainer's prompt already forces Ollama into a fixed
template with an enumerated field --
    "طبيعة الحركة الحالية: [كسر حقيقي / امتداد ترند / ارتداد]"
"كسر حقيقي" or "امتداد ترند" claims the move continues (same direction as
the alert); "ارتداد" claims it reverses. That's parsed directly (string
match against the three known choices) rather than asking another LLM call
to interpret free text -- deterministic, auditable, no new judgment layer
that would itself need calibrating.

Noise threshold: reuses local_crypto_watch.py's own per-symbol FAST
threshold (halved) as the "did this actually move enough to count, or is
this noise" bar -- not a new invented number, half of an already-approved
per-symbol threshold.
"""
import json
import os
import re
import time
import urllib.request
from heartbeat import write_heartbeat  # 2026-08-06: functional heartbeat rollout (not currently
                                        # running -- code-only patch, not tested live)

import ccxt

BA = r"C:\TradingBot\Bot_Active"
WATCHED_LOG = os.path.join(BA, "local_crypto_watch_out.log")
# 2026-07-27: was pointed at "local_crypto_watch.log", a file that never
# actually gets created -- local_crypto_watch.py's real alert output goes to
# "local_crypto_watch_out.log" (supervisor's stdout redirect). This script
# silently returned every single cycle all night (os.path.exists() was
# always False) -- zero events captured despite real alerts firing
# constantly. Ahmed caught it in the morning. That night's data is
# unrecoverable; this only fixes it going forward.
REGIME_STATE_FILE = os.path.join(BA, "regime_state.json")
POSITIONS_FILE = os.path.join(BA, "outcome_log.pos")
PENDING_FILE = os.path.join(BA, "outcome_pending.json")
COMPLETED_LOG = os.path.join(BA, "outcome_calibration_log.jsonl")

POLL_SECONDS = 20
HORIZONS_SEC = {"5m": 5 * 60, "15m": 15 * 60, "30m": 30 * 60}
REGIME_STALE_SEC = 180  # don't attach a regime reading older than this to a new event

# halved version of local_crypto_watch.py's own FAST_THRESHOLDS -- same
# values, not reinvented (see module docstring)
NOISE_THRESHOLDS = {"BTC/USDT:USDT": 0.0025, "ETH/USDT:USDT": 0.003, "XAU/USDT:USDT": 0.002,
                     "SOL/USDT:USDT": 0.004, "DOGE/USDT:USDT": 0.006, "XRP/USDT:USDT": 0.004,
                     "ADA/USDT:USDT": 0.004, "AAVE/USDT:USDT": 0.004}

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "qwen2.5:3b"

exchange = ccxt.bybit()

ALERT_RE = re.compile(
    r"(FAST MOVE|GRIND)\s+(\S+)\s+(UP|DOWN)\s+[+-]?([\d.]+)%\s+(?:in|over)\s+\d+s\s+\(([\d.]+)\s*->\s*([\d.]+)\)"
)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_alert_line(line):
    """Pure. Returns dict or None. Handles both FAST MOVE and GRIND formats."""
    m = ALERT_RE.search(line)
    if not m:
        return None
    detector, symbol, direction, pct, price_from, price_to = m.groups()
    return {
        "detector": "FAST_MOVE" if detector == "FAST MOVE" else "GRIND",
        "symbol": symbol, "direction": direction,
        "pct": float(pct), "price_from": float(price_from), "price_to": float(price_to),
    }


LLM_DIRECTION_MAP = {
    "كسر حقيقي": "CONTINUATION",
    "امتداد ترند": "CONTINUATION",
    "ارتداد": "REVERSAL",
}


def parse_llm_predicted_label(llm_text):
    """Pure. Looks for the fixed enumerated field tripwire_explainer's
    prompt forces Ollama into; returns CONTINUATION / REVERSAL / UNKNOWN."""
    if not llm_text:
        return "UNKNOWN"
    m = re.search(r"طبيعة الحركة الحالية[:\s]*([^\n]+)", llm_text)
    if not m:
        return "UNKNOWN"
    field = m.group(1)
    for phrase, label in LLM_DIRECTION_MAP.items():
        if phrase in field:
            return label
    return "UNKNOWN"


def opposite(direction):
    return "DOWN" if direction == "UP" else "UP"


def classify_directional_outcome(predicted_direction, price_before, price_after, noise_threshold):
    """Pure. predicted_direction must be 'UP' or 'DOWN' (or None/anything
    else -> UNCERTAIN, can't grade a claim that was never actually UP/DOWN)."""
    if predicted_direction not in ("UP", "DOWN") or not price_before:
        return "UNCERTAIN"
    pct_change = (price_after - price_before) / price_before
    if abs(pct_change) < noise_threshold:
        return "UNCERTAIN"
    actual_direction = "UP" if pct_change > 0 else "DOWN"
    return "CORRECT" if actual_direction == predicted_direction else "WRONG"


def llm_predicted_direction(event_direction, llm_label):
    if llm_label == "CONTINUATION":
        return event_direction
    if llm_label == "REVERSAL":
        return opposite(event_direction)
    return None  # UNKNOWN -- can't grade


def regime_predicted_direction(base_regime):
    if base_regime == "TRENDING_UP":
        return "UP"
    if base_regime == "TRENDING_DOWN":
        return "DOWN"
    return None  # RANGE/UNKNOWN never made a directional claim -- not scored, not forced to UNCERTAIN


def explain(alert_line):
    prompt = (
        "ده سطر تنبيه من أداة مراقبة سوق آلية (فوليوم/سرعة حركة):\n\n"
        f"{alert_line}\n\n"
        "رد بالتنسيق ده بالظبط (عربي، مختصر جدًا كل بند):\n"
        "الهيكل قبل التنبيه: [تماسك / صعود مستمر / هبوط مستمر / تذبذب]\n"
        "طبيعة الحركة الحالية: [كسر حقيقي / امتداد ترند / ارتداد]\n"
        "الفوليوم: [استثنائي / عادي]\n"
        "خلاصة سطر واحد للمتابعة السريعة"
    )
    try:
        payload = json.dumps({"model": MODEL, "prompt": prompt, "stream": False}).encode("utf-8")
        req = urllib.request.Request(OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data.get("response") or "").strip()
    except Exception as e:
        return ""


def read_regime(symbol):
    try:
        with open(REGIME_STATE_FILE) as f:
            state = json.load(f)
        entry = state.get(symbol)
        if not entry:
            return None, None
        if time.time() - entry.get("updated_ts", 0) > REGIME_STALE_SEC:
            return None, None
        return entry.get("base_regime"), entry.get("vol_state")
    except Exception:
        return None, None


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def load_positions():
    return load_json(POSITIONS_FILE, {})


def check_new_alerts(pending):
    positions = load_positions()
    if not os.path.exists(WATCHED_LOG):
        return
    last_pos = positions.get(WATCHED_LOG, os.path.getsize(WATCHED_LOG))
    with open(WATCHED_LOG, encoding="utf-8", errors="ignore") as f:
        f.seek(last_pos)
        new_lines = f.readlines()
        positions[WATCHED_LOG] = f.tell()
    save_json(POSITIONS_FILE, positions)

    for line in new_lines:
        parsed = parse_alert_line(line)
        if not parsed:
            continue
        base_regime, vol_state = read_regime(parsed["symbol"])
        llm_text = explain(line.strip())
        llm_label = parse_llm_predicted_label(llm_text)
        now = time.time()
        record = {
            "time": now, "symbol": parsed["symbol"], "detector": parsed["detector"],
            "event_direction": parsed["direction"], "price_at_event": parsed["price_to"],
            "regime": base_regime, "vol_state": vol_state,
            "llm_interpretation": llm_text, "llm_label": llm_label,
            "outcomes": {},
        }
        key = f"{parsed['symbol']}|{now}"
        pending[key] = record
        log(f"NEW EVENT {parsed['detector']} {parsed['symbol']} {parsed['direction']} "
            f"regime={base_regime} llm={llm_label}")


def resolve_due(pending):
    now = time.time()
    for key in list(pending.keys()):
        record = pending[key]
        symbol = record["symbol"]
        for horizon, seconds in HORIZONS_SEC.items():
            if horizon in record["outcomes"]:
                continue
            if now - record["time"] < seconds:
                continue
            try:
                current_price = exchange.fetch_ticker(symbol)["last"]
            except Exception as e:
                log(f"fetch error {symbol}: {str(e)[:100]}")
                continue
            noise = NOISE_THRESHOLDS.get(symbol, 0.003)
            detector_outcome = classify_directional_outcome(
                record["event_direction"], record["price_at_event"], current_price, noise)
            llm_pred = llm_predicted_direction(record["event_direction"], record["llm_label"])
            llm_outcome = classify_directional_outcome(llm_pred, record["price_at_event"], current_price, noise) \
                if llm_pred else "UNCERTAIN"
            regime_pred = regime_predicted_direction(record["regime"])
            regime_outcome = classify_directional_outcome(regime_pred, record["price_at_event"], current_price, noise) \
                if regime_pred else None
            record["outcomes"][horizon] = {
                "price": current_price, "detector_outcome": detector_outcome,
                "llm_outcome": llm_outcome, "regime_outcome": regime_outcome,
            }
            log(f"OUTCOME {horizon} {symbol} detector={detector_outcome} llm={llm_outcome} regime={regime_outcome}")

        if len(record["outcomes"]) == len(HORIZONS_SEC):
            with open(COMPLETED_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
            del pending[key]


def generate_report(horizon="30m"):
    """Reads the completed log and returns per-detector/regime/llm accuracy,
    matching Ahmed's example format. Callable standalone, not just from the
    polling loop -- also usable by a future digest/dashboard consumer."""
    if not os.path.exists(COMPLETED_LOG):
        return {}
    from collections import defaultdict
    tally = defaultdict(lambda: {"CORRECT": 0, "WRONG": 0, "UNCERTAIN": 0})
    with open(COMPLETED_LOG, encoding="utf-8") as f:
        for line in f:
            try:
                record = json.loads(line)
            except Exception:
                continue
            outcome = record.get("outcomes", {}).get(horizon)
            if not outcome:
                continue
            tally[f"detector:{record['detector']}"][outcome["detector_outcome"]] += 1
            tally["llm_interpretation"][outcome["llm_outcome"]] += 1
            if record.get("regime") in ("TRENDING_UP", "TRENDING_DOWN") and outcome["regime_outcome"]:
                tally[f"regime:{record['regime']}"][outcome["regime_outcome"]] += 1
    report = {}
    for key, counts in tally.items():
        total = sum(counts.values())
        graded = counts["CORRECT"] + counts["WRONG"]
        report[key] = {
            **counts, "total": total,
            "accuracy_pct": round(100 * counts["CORRECT"] / graded, 1) if graded else None,
        }
    return report


def main():
    log(f"outcome_calibration_log started — watching {WATCHED_LOG}, "
        f"horizons={list(HORIZONS_SEC.keys())}")
    pending = load_json(PENDING_FILE, {})
    tick = 0
    while True:
        try:
            check_new_alerts(pending)
            resolve_due(pending)
            save_json(PENDING_FILE, pending)
            write_heartbeat("outcome_calibration_log")
        except Exception as e:
            log(f"loop error: {str(e)[:150]}")
        tick += 1
        if tick % 15 == 0:  # ~every 5 min at POLL_SECONDS=20
            log(f"report(30m): {generate_report('30m')}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
