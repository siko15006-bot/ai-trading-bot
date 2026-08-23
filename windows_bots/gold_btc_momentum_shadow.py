"""
gold_btc_momentum_shadow.py -- SHADOW/OBSERVE ONLY, zero live execution.

Watches the real gold_btc_bot.py trace (gold_btc_trace_{UTC date}.jsonl,
rotates daily, written by the live bot's own instrumentation) for three
counterfactual "what if this gate were different" opportunities, all on
symbols/accounts the real bot already evaluates (never invented):

  - btc_candidate1     : BTC, trend+momentum PASS regardless of atr_gate_passed.
                         The 2026-08-20 backtest winner (PF 1.09 vs live 0.89,
                         net +$57.45 vs -$73.21 on 180 days).
  - btc_momentum_nearmiss : BTC, trend PASS, momentum FAIL but RSI within
                         NEAR_MISS_MARGIN of the momentum boundary for the
                         hypothesis direction (2026-08-21 request, focus on
                         RSI 70-72 observed live today).
  - xau_atr_reject     : XAU (EA XAUUSDm or BA XAUUSD.s), trend+momentum PASS,
                         atr_gate_passed FALSE -- the live mirror of
                         btc_candidate1 for gold.

Zero order_send anywhere in this file. Zero import from gold_btc_bot.py
(kept fully separate on purpose -- this can never affect the live bot).
SL/TP math and config values (risk.sl_atr_mult per asset, risk.rr_min) are
read straight from gold_btc_bot_config.json, matching the live bot's real
formula exactly (see gold_btc_bot.py:567-575), never invented.

Entry price is recorded two ways: `trace_price` (the price the live bot's
own evaluation cycle logged) and `tick_price` (a fresh symbol_info_tick at
classification time, ask for buy / bid for sell) -- the gap between them is
the real spread cost of actually taking the trade, which `trace_price` alone
hides.

Usage: venv/bin/python gold_btc_momentum_shadow.py [--test] [--compare]
"""
import glob
import json
import os
import sys
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5

BASE_DIR = r"C:\TradingBot\Bot_Active"
CONFIG_PATH = os.path.join(BASE_DIR, "gold_btc_bot_config.json")
SHADOW_LOG = os.path.join(BASE_DIR, "gold_btc_momentum_shadow.jsonl")
POLL_SEC = 30
NEAR_MISS_MARGIN = 2.0  # RSI points beyond the momentum boundary, still "near"
MAX_HOLD_SEC = 4 * 3600  # force-close an open shadow position after 4h (bounds tracking, still yields MFE/MAE)

# Same read-only MT5 credentials already used elsewhere in this project for
# analytics (portfolio_analytics.py's ACCOUNTS dict) -- duplicated on purpose
# so this file has zero import-time dependency on any live bot or shared module.
ACCOUNTS = {
    "EA": dict(path=r"C:\MT5_Portable_2\terminal64.exe",
               login=<REDACTED_MT5_LOGIN_EA>, password="<REDACTED_MT5_PASSWORD_EA>", server="Exness-MT5Real33"),
    "EM": dict(path=r"C:\MT5_Portable_3\terminal64.exe",
               login=<REDACTED_MT5_LOGIN_EM>, password="<REDACTED_MT5_PASSWORD_EM>", server="Exness-MT5Real35"),
    "BA": dict(path=r"C:\Program Files\MetaTrader 5\terminal64.exe",
               login=None, password=None, server=None),
}

# Matches gold_btc_bot_config.json's symbols_by_account exactly (BA has no
# BTC symbol on this broker, EM has no XAU -- not arbitrary, confirmed
# constraints already documented in that file's own comments).
SYMBOL_ASSET = {"BTCUSDm": "BTC", "XAUUSDm": "XAU", "XAUUSD.s": "XAU"}

_current_conn_acc = None  # which account's terminal is currently connected


def now_utc():
    return datetime.now(timezone.utc)


def log(msg):
    print(f'[{now_utc().strftime("%Y-%m-%d %H:%M:%S")}] {msg}', flush=True)


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _truthy(v):
    """gold_btc_bot's trace writes gate booleans as native bool in some
    records and str(bool) in others (an existing quirk of its own
    instrumentation, not something to fix here) -- accept both."""
    return v is True or v == "True"


def ensure_connected(account):
    """Switch the single MT5 connection to `account` only if it isn't
    already the active one -- avoids re-initializing every poll when
    consecutive work is on the same account (ponytail: one connection,
    swap on demand, no connection-pool machinery for a 30s-cadence
    background script)."""
    global _current_conn_acc
    if _current_conn_acc == account:
        return True
    cfg = ACCOUNTS[account]
    kwargs = {"path": cfg["path"]}
    if cfg["login"]:  # BA connects via an already-logged-in terminal, path only (same pattern as portfolio_analytics.py)
        kwargs.update(login=cfg["login"], password=cfg["password"], server=cfg["server"])
    if not mt5.initialize(**kwargs):
        log(f"mt5 init failed for {account}: {mt5.last_error()}")
        return False
    _current_conn_acc = account
    return True


def live_tick_price(account, symbol, side):
    """Fresh tick at classification/poll time. Returns (price, spread) or
    (None, None) on failure -- never raises, callers treat None as
    'unavailable, fall back to trace price'."""
    if not ensure_connected(account):
        return None, None
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return None, None
    price = tick.ask if side == "buy" else tick.bid
    return price, tick.ask - tick.bid


def classify_opportunity(cfg, rec):
    """Returns (opportunity_type, asset_key) or (None, None). Trend gate is
    required PASS for every category -- these are all 'trend agrees, some
    other gate is the question' cases, not trend disagreements."""
    gates = rec.get("gates")
    symbol = rec.get("symbol")
    if not gates or symbol not in SYMBOL_ASSET:
        return None, None
    if not _truthy(gates.get("trend_gate_passed")):
        return None, None
    asset = SYMBOL_ASSET[symbol]
    momentum_passed = _truthy(gates.get("momentum_gate_passed"))
    atr_passed = _truthy(gates.get("atr_gate_passed"))

    if asset == "BTC" and momentum_passed:
        return "btc_candidate1", asset
    if asset == "XAU" and momentum_passed and not atr_passed:
        return "xau_atr_reject", asset
    if asset == "BTC" and not momentum_passed:
        rsi = gates.get("rsi")
        direction = rec.get("direction")
        mom_cfg = cfg["momentum"]
        if rsi is None or direction not in ("buy", "sell"):
            return None, None
        lo, hi = (mom_cfg["long_min"], mom_cfg["long_max"]) if direction == "buy" \
            else (mom_cfg["short_min"], mom_cfg["short_max"])
        if (hi < rsi <= hi + NEAR_MISS_MARGIN) or (lo - NEAR_MISS_MARGIN <= rsi < lo):
            return "btc_momentum_nearmiss", asset
    return None, None


def compute_sl_tp(cfg, asset, direction, price, raw_atr):
    sl_mult = cfg["risk"]["sl_atr_mult"].get(asset, cfg["risk"]["sl_atr_mult"]["default"])
    rr = cfg["risk"]["rr_min"]
    sl_dist = raw_atr * sl_mult
    tp_dist = sl_dist * rr
    if direction == "buy":
        sl, tp = price - sl_dist, price + tp_dist
    else:
        sl, tp = price + sl_dist, price - tp_dist
    return sl, tp, sl_dist


def entry_key(rec):
    return f"{rec.get('account')}|{rec.get('symbol')}|{rec.get('logged_at')}"


def current_trace_path():
    return os.path.join(BASE_DIR, f"gold_btc_trace_{now_utc().strftime('%Y-%m-%d')}.jsonl")


def load_shadow_state():
    """Rebuild seen-entry-keys and still-open trades from the shadow log
    itself -- restart-safe, no separate state file (ponytail: one file
    is the whole state, less to keep in sync)."""
    seen_keys = set()
    open_trades = {}
    if not os.path.exists(SHADOW_LOG):
        return seen_keys, open_trades
    with open(SHADOW_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") == "entry":
                seen_keys.add(rec["key"])
                open_trades[rec["id"]] = rec
            elif rec.get("type") == "outcome":
                open_trades.pop(rec["id"], None)
    return seen_keys, open_trades


def append_shadow(rec):
    with open(SHADOW_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def scan_new_entries(cfg, trace_path, offset, seen_keys):
    """Read new lines since `offset`, emit shadow entries for any
    classified opportunity. Returns new offset."""
    if not os.path.exists(trace_path):
        return offset
    with open(trace_path, encoding="utf-8") as f:
        f.seek(offset)
        while True:
            line = f.readline()
            if not line:
                break
            offset = f.tell()
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            opp_type, asset = classify_opportunity(cfg, rec)
            if opp_type is None:
                continue
            key = entry_key(rec)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            symbol = rec["symbol"]
            account = rec["account"]
            trace_price = rec.get("price")
            raw_atr = rec["gates"].get("atr")
            direction = rec.get("direction")
            if trace_price is None or raw_atr is None or direction not in ("buy", "sell"):
                log(f"skip candidate (incomplete data): key={key}")
                continue
            tick_price, spread = live_tick_price(account, symbol, direction)
            entry_price = tick_price if tick_price is not None else trace_price
            sl, tp, sl_dist = compute_sl_tp(cfg, asset, direction, entry_price, raw_atr)
            entry = {
                "type": "entry", "id": key, "key": key, "opportunity_type": opp_type, "asset": asset,
                "timestamp": rec["logged_at"], "account": account, "symbol": symbol,
                "side": direction, "trace_price": trace_price, "tick_price": tick_price,
                "entry_price": entry_price, "spread": spread,
                "rsi": rec["gates"].get("rsi"), "atr": raw_atr, "adx": rec["gates"].get("adx"),
                "trend_gate_passed": True,
                "momentum_gate_passed": _truthy(rec["gates"].get("momentum_gate_passed")),
                "atr_gate_passed": _truthy(rec["gates"].get("atr_gate_passed")),
                "live_decision": rec.get("decision"), "live_rejection_reason": rec.get("rejection_reason"),
                "risk_halt_active": rec.get("risk_halt_active"),
                "sl": sl, "tp": tp, "sl_dist": sl_dist,
                "mfe": 0.0, "mae": 0.0, "opened_at": now_utc().isoformat(),
            }
            append_shadow(entry)
            log(f"SHADOW {opp_type} {direction.upper()} {symbol}[{account}] @ {entry_price} "
                f"(trace={trace_price}, spread={spread}) SL={sl:.2f} TP={tp:.2f} (live={rec.get('decision')})")
    return offset


def update_open_trades(open_trades):
    """Poll live price per (account, symbol) of each open shadow position,
    track MFE/MAE, close on SL/TP touch or MAX_HOLD_SEC timeout.
    Read-only (symbol_info_tick), zero order_send."""
    for tid, trade in list(open_trades.items()):
        account, symbol = trade["account"], trade["symbol"]
        side, entry, sl, tp, sl_dist = trade["side"], trade["entry_price"], trade["sl"], trade["tp"], trade["sl_dist"]
        if not ensure_connected(account):
            continue
        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            continue
        price = tick.bid if side == "buy" else tick.ask
        excursion = (price - entry) if side == "buy" else (entry - price)
        r = excursion / sl_dist if sl_dist else 0.0
        trade["mfe"] = max(trade["mfe"], r)
        trade["mae"] = min(trade["mae"], r)
        hit_tp = (price >= tp) if side == "buy" else (price <= tp)
        hit_sl = (price <= sl) if side == "buy" else (price >= sl)
        age_sec = (now_utc() - datetime.fromisoformat(trade["opened_at"])).total_seconds()
        timed_out = age_sec >= MAX_HOLD_SEC
        if hit_tp or hit_sl or timed_out:
            outcome = "TP" if hit_tp else ("SL" if hit_sl else "TIMEOUT")
            if hit_tp:
                r_result = (tp - entry) / sl_dist * (1 if side == "buy" else -1)
            elif hit_sl:
                r_result = -1.0
            else:
                r_result = r  # timeout: whatever R currently stands, still informative
            append_shadow({
                "type": "outcome", "id": tid, "outcome": outcome,
                "exit_price": price, "exit_time": now_utc().isoformat(),
                "mfe": trade["mfe"], "mae": trade["mae"], "r_result": r_result,
            })
            log(f"SHADOW OUTCOME {outcome} {tid} r={r_result:.2f} mfe={trade['mfe']:.2f} mae={trade['mae']:.2f}")
            del open_trades[tid]


def write_heartbeat():
    path = os.path.join(BASE_DIR, "heartbeat_gold_btc_momentum_shadow.json")
    tmp = path + f".tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump({"last_success_ts": now_utc().isoformat()}, f)
    os.replace(tmp, path)


def run():
    cfg = load_config()
    seen_keys, open_trades = load_shadow_state()
    log(f"gold_btc_momentum_shadow started -- SHADOW ONLY, zero order_send. "
        f"{len(open_trades)} open shadow trade(s) resumed from log.")
    trace_path = current_trace_path()
    offset = 0
    while True:
        try:
            new_trace_path = current_trace_path()
            if new_trace_path != trace_path:
                trace_path, offset = new_trace_path, 0  # daily rotation
            offset = scan_new_entries(cfg, trace_path, offset, seen_keys)
            if open_trades:
                update_open_trades(open_trades)
            write_heartbeat()
        except Exception as e:
            log(f"loop error: {e}")
        time.sleep(POLL_SEC)


def _find_next_fill(records_by_key, account, symbol, direction, after_ts):
    """Offline helper: first later FILLED record for the same
    account+symbol+direction, or None. `records_by_key` is a flat
    time-sorted list of all trace records (already parsed)."""
    for rec in records_by_key:
        if rec.get("logged_at", "") <= after_ts:
            continue
        if rec.get("account") != account or rec.get("symbol") != symbol:
            continue
        if rec.get("decision") != "FILLED" or rec.get("direction") != direction:
            continue
        return rec
    return None


def compare():
    """Offline: LIVE BASELINE (real gold_btc_bot decisions, all trace files)
    vs SHADOW opportunities (this file's own log), split by opportunity
    type/asset/account, plus delay-to-real-fill for each shadow entry that
    was later actually taken live."""
    all_trace = []
    for path in sorted(glob.glob(os.path.join(BASE_DIR, "gold_btc_trace*.jsonl"))):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    all_trace.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    all_trace.sort(key=lambda r: r.get("logged_at", ""))

    live_counts = {}
    for rec in all_trace:
        sym = rec.get("symbol")
        if sym not in SYMBOL_ASSET:
            continue
        d = live_counts.setdefault(sym, {"evaluations": 0, "filled": 0})
        d["evaluations"] += 1
        if rec.get("decision") == "FILLED":
            d["filled"] += 1

    print("=== LIVE BASELINE (real gold_btc_bot) ===")
    for sym, d in live_counts.items():
        print(f"  {sym}: {d['evaluations']} evaluations, {d['filled']} filled")

    by_type = {}
    entries_by_key = {}
    if os.path.exists(SHADOW_LOG):
        with open(SHADOW_LOG) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") == "entry":
                    entries_by_key[rec["id"]] = rec
                    d = by_type.setdefault(rec["opportunity_type"], {"entries": 0, "outcomes": 0, "wins": 0, "r_sum": 0.0, "spread_sum": 0.0})
                    d["entries"] += 1
                    if rec.get("spread") is not None:
                        d["spread_sum"] += rec["spread"]
                elif rec.get("type") == "outcome":
                    parent = entries_by_key.get(rec["id"])
                    if not parent:
                        continue
                    d = by_type[parent["opportunity_type"]]
                    d["outcomes"] += 1
                    d["r_sum"] += rec.get("r_result", 0.0)
                    if rec.get("outcome") == "TP":
                        d["wins"] += 1

    print("\n=== SHADOW OPPORTUNITIES (by type) ===")
    if not by_type:
        print("  (none yet -- waiting for a qualifying evaluation cycle)")
    for opp_type, d in by_type.items():
        avg_spread = d["spread_sum"] / d["entries"] if d["entries"] else 0.0
        print(f"  {opp_type}: {d['entries']} entries, {d['outcomes']} closed "
              f"({d['wins']} TP / {d['outcomes'] - d['wins']} SL/timeout), "
              f"net R={d['r_sum']:.2f}, avg spread={avg_spread:.3f}")

    print("\n=== Delay-to-real-fill (shadow entries the live bot later actually took) ===")
    any_match = False
    for key, entry in entries_by_key.items():
        fill = _find_next_fill(all_trace, entry["account"], entry["symbol"], entry["side"], entry["timestamp"])
        if fill:
            any_match = True
            entry_t = datetime.fromisoformat(entry["timestamp"])
            fill_t = datetime.fromisoformat(fill["logged_at"])
            delay_min = (fill_t - entry_t).total_seconds() / 60
            price_delta = fill.get("price", 0) - entry["entry_price"]
            print(f"  {key}: rejected @ {entry['entry_price']}, live filled {delay_min:.0f} min later "
                  f"@ {fill.get('price')} (delta {price_delta:+.2f})")
    if not any_match:
        print("  (none yet)")

    print("\n=== ATR gate note (config, read-only) ===")
    cfg = load_config()
    for asset in ("XAU", "BTC"):
        print(f"  {asset}: risk.sl_atr_mult={cfg['risk']['sl_atr_mult'].get(asset)} "
              f"(same relative gate formula for both: atr_gate_passed = raw_atr*mult >= avg(TR,{cfg['atr']['avg_period']}) "
              f"-- identical threshold logic and identical mult for XAU and BTC, no per-asset differentiation despite different volatility profiles)")


def _demo():
    """python gold_btc_momentum_shadow.py --test -- pure-logic checks, zero
    MT5/file-system side effects on real paths."""
    cfg = {
        "momentum": {"long_min": 50, "long_max": 70, "short_min": 30, "short_max": 50},
        "risk": {"sl_atr_mult": {"BTC": 0.5, "XAU": 0.5, "default": 1.4}, "rr_min": 2.0},
        "atr": {"avg_period": 20},
    }

    rec_cand1 = {"symbol": "BTCUSDm", "direction": "buy",
                 "gates": {"trend_gate_passed": True, "momentum_gate_passed": "True",
                           "atr_gate_passed": False, "atr": 200.0, "rsi": 55}}
    assert classify_opportunity(cfg, rec_cand1) == ("btc_candidate1", "BTC")

    rec_xau_atr = {"symbol": "XAUUSD.s", "direction": "buy",
                   "gates": {"trend_gate_passed": True, "momentum_gate_passed": True,
                             "atr_gate_passed": False, "atr": 5.0, "rsi": 55}}
    assert classify_opportunity(cfg, rec_xau_atr) == ("xau_atr_reject", "XAU")

    rec_nearmiss = {"symbol": "BTCUSDm", "direction": "buy",
                     "gates": {"trend_gate_passed": True, "momentum_gate_passed": False,
                               "atr_gate_passed": True, "atr": 200.0, "rsi": 71.5}}
    assert classify_opportunity(cfg, rec_nearmiss) == ("btc_momentum_nearmiss", "BTC")

    rec_far_reject = {"symbol": "BTCUSDm", "direction": "buy",
                       "gates": {"trend_gate_passed": True, "momentum_gate_passed": False,
                                 "atr_gate_passed": True, "atr": 200.0, "rsi": 81.8}}
    assert classify_opportunity(cfg, rec_far_reject) == (None, None)

    rec_trend_fail = {"symbol": "BTCUSDm", "direction": "buy",
                       "gates": {"trend_gate_passed": False, "momentum_gate_passed": True}}
    assert classify_opportunity(cfg, rec_trend_fail) == (None, None)

    rec_no_gates = {"symbol": "BTCUSDm", "gates": None}
    assert classify_opportunity(cfg, rec_no_gates) == (None, None)
    print("classify_opportunity (candidate1/xau_atr/nearmiss/far-reject/trend-fail/no-gates): OK")

    sl, tp, sl_dist = compute_sl_tp(cfg, "BTC", "buy", 70000.0, 200.0)
    assert sl_dist == 100.0 and sl == 69900.0 and tp == 70200.0, (sl, tp, sl_dist)
    sl2, tp2, _ = compute_sl_tp(cfg, "XAU", "sell", 4500.0, 10.0)
    assert sl2 == 4505.0 and tp2 == 4490.0, (sl2, tp2)
    print("compute_sl_tp (per-asset mult, matches gold_btc_bot.py formula): OK")

    seen = set()
    tmp_trace = "test_trace_tmp.jsonl"
    rows = [
        dict(logged_at="t1", account="EA", symbol="BTCUSDm", direction="buy", price=70000.0, decision="REJECTED",
             gates={"trend_gate_passed": True, "momentum_gate_passed": True, "atr_gate_passed": False, "atr": 200.0, "rsi": 55}),
        dict(logged_at="t2", account="BA", symbol="XAUUSD.s", direction="buy", price=4500.0, decision="REJECTED",
             gates={"trend_gate_passed": True, "momentum_gate_passed": True, "atr_gate_passed": False, "atr": 10.0, "rsi": 55}),
    ]
    with open(tmp_trace, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    global SHADOW_LOG
    real_log, SHADOW_LOG = SHADOW_LOG, "test_shadow_tmp.jsonl"
    if os.path.exists(SHADOW_LOG):
        os.remove(SHADOW_LOG)
    # isolate from real MT5 connection state (--test must be pure-logic,
    # never depend on whether a terminal happens to be reachable right now)
    module = sys.modules[__name__]
    real_live_tick_price = module.live_tick_price
    module.live_tick_price = lambda account, symbol, side: (None, None)
    try:
        off = scan_new_entries(cfg, tmp_trace, 0, seen)
        assert off > 0
        with open(SHADOW_LOG) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        assert len(lines) == 2
        assert lines[0]["opportunity_type"] == "btc_candidate1" and lines[0]["entry_price"] == 70000.0  # no live tick -> falls back to trace_price
        assert lines[1]["opportunity_type"] == "xau_atr_reject" and lines[1]["account"] == "BA"
        print("scan_new_entries (multi-asset classify + tick-unavailable fallback to trace_price): OK")

        seen2, open2 = load_shadow_state()
        assert len(open2) == 2
        print("load_shadow_state restart-safe reload (multi-entry): OK")

        # simulate TP hit without a live tick connection: patch update_open_trades'
        # tick source is MT5-only, so exercise the R-math directly instead (kept
        # pure-logic, matching the rest of this self-check's zero-MT5-dependency style)
        trade = list(open2.values())[0]
        price = trade["tp"]
        excursion = (price - trade["entry_price"]) if trade["side"] == "buy" else (trade["entry_price"] - price)
        r = excursion / trade["sl_dist"]
        assert abs(r - 2.0) < 1e-9  # rr_min=2.0
        print("TP R-multiple math (independent of live tick source): OK")
    finally:
        module.live_tick_price = real_live_tick_price
        SHADOW_LOG = real_log
        os.remove(tmp_trace)
        if os.path.exists("test_shadow_tmp.jsonl"):
            os.remove("test_shadow_tmp.jsonl")

    print("=== ALL gold_btc_momentum_shadow self-checks PASSED ===")


if __name__ == "__main__":
    if "--test" in sys.argv:
        _demo()
    elif "--compare" in sys.argv:
        compare()
    else:
        run()
