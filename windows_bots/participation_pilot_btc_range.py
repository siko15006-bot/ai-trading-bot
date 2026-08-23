"""
participation_pilot_btc_range.py -- PHASE 1 PILOT of the Range-Regime-gated
variant, approved 2026-08-07 per Research_Protocol_v3_regime.md (IS/OOS/
Holdout all passed: OOS PF=1.54, Holdout PF=1.36, both net positive).

This is a near-exact copy of participation_pilot_btc.py (the original,
ungated pilot -- now stopped, to avoid double execution on the same
signals during this Phase 1 test). ONLY addition: an entry gate requiring
regime_live.get_current_trend_regime() == "Range" (Regime Classifier v1,
computed live from Binance BTCUSDT H1 -- the same feed/logic the protocol
was validated on, not re-derived from MT5). No other logic, threshold, or
parameter differs from the original bot or from the backtest.

Account: EA (Exness-MT5Real33), min lot (0.01), single account -- Phase 1
of a staged rollout. Per Ahmed's explicit instruction: no further accounts,
no size increase, no threshold changes during this phase. Stop conditions:
any execution error, any backtest/live logic mismatch, any regime
classification problem -- document and resolve AFTER the phase ends, not
by patching mid-flight.

PHASE 1.1 (2026-08-07): the fixed anomaly_mult=2.0x threshold is replaced
with a dynamic one -- the P99 rolling percentile of the tick_volume/
roll_avg ratio over a trailing 14-day M15 window (bar-shifted by 1, no
look-ahead). Reason: the static 2.0x threshold hit the target 5-10
trades/month band on IS but blew out to 40-55/month on OOS+Holdout because
the ratio distribution drifted over time (see rolling_threshold_design.py:
decisive backtest IS 7.77/mo, OOS 7.99/mo, Holdout 7.77/mo, PF 1.72/1.56/
1.49, all passed -- rate-stability max/min ratio 1.03x vs ~8x for the
static version). Nothing else changed: same roll_window=20, same H1
agreement, same Range gate, same SL/TP/ATR/RR, same lot/magic/logs/
heartbeat. Rollback: restore participation_pilot_btc_range_STATIC_BACKUP_2.0x.py
over this file and restart if anything looks wrong.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5
import pandas as pd
import pandas_ta as ta

sys.path.insert(0, r"C:\TradingBot\Research_ParticipationAnomaly_2026\strategies")
from participation_strategy import DEFAULT_PARAMS

from event_log import log_event
from heartbeat import write_heartbeat
from regime_live import get_current_trend_regime

ACCOUNT = "EA"
STRATEGY = "participation_pilot_btc_range"

# 2026-08-21 (Ahmed-approved, Security Phase 0 -- first live-bot pilot):
# credentials moved out of source into C:\TradingBot\.env via
# credentials.py. sys.path insert makes the root-level helper importable
# regardless of cwd this bot is launched from.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from credentials import get_account

EA = get_account("EA")

SYMBOL = "BTCUSDm"
MAGIC = 995502  # NEW, distinct from 995501 (the now-stopped ungated pilot) --
                # never share a magic (see the 995500 collision lesson in the original bot).
LOT = 0.01
POLL_SEC = 30
INIT_RETRY_BASE = 5
INIT_RETRY_MAX = 60

# PHASE 1.1: dynamic anomaly threshold -- rolling P99 of tick_volume/roll_avg
# over a trailing 14-day M15 window, replacing the fixed anomaly_mult=2.0x.
DYNAMIC_PERCENTILE = 99
DYNAMIC_WINDOW_DAYS = 14
BARS_PER_DAY_M15 = 96
DYNAMIC_WINDOW_BARS = DYNAMIC_WINDOW_DAYS * BARS_PER_DAY_M15  # 1344
M15_PULL_COUNT = DYNAMIC_WINDOW_BARS + 100  # + warmup/buffer

BASE_DIR = r"C:\TradingBot\Bot_Active"
SIGNALS_LOG = os.path.join(BASE_DIR, "participation_pilot_range_signals.jsonl")
TRADES_LOG = os.path.join(BASE_DIR, "participation_pilot_range_trades.json")
STATE_FILE = os.path.join(BASE_DIR, "participation_pilot_range_state.json")
# 2026-08-07 (Ahmed-approved): same portfolio-wide circuit breaker
# gold_btc_bot.py already respects. Blocks NEW entries only -- the open-
# position check/close path (state["open_ticket"] handling above) and
# ladder_guard.py/ea_shield.py are completely untouched.
RISK_HALT_FLAG_PATH = os.path.join(BASE_DIR, "RISK_HALT.flag")


def now_utc():
    return datetime.now(timezone.utc)


def log(msg):
    print(f'[{now_utc().strftime("%Y-%m-%d %H:%M:%S")}] {msg}', flush=True)


def append_jsonl(path, row):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def load_trades():
    if os.path.exists(TRADES_LOG):
        try:
            with open(TRADES_LOG) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log(f"load_trades: TRADES_LOG corrupted/unreadable ({e}) -- falling back to empty list")
            return []
    return []


def save_trades(trades):
    tmp = TRADES_LOG + f".tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(trades, f, indent=2, default=str)
    os.replace(tmp, TRADES_LOG)


def load_state():
    default = {"last_bar_time": None, "open_ticket": None, "open_ref_price": None}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log(f"load_state: STATE_FILE corrupted/unreadable ({e}) -- falling back to default state")
            return default
    return default


def save_state(s):
    # 2026-08-19 (Ahmed-approved operational fix): atomic write -- a kill mid-write
    # previously left this file null-byte-corrupted, which crashed load_state()
    # on every subsequent start with no recovery (see participation_pilot_btc_range.py.bak_20260819_statefix).
    tmp = STATE_FILE + f".tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


def my_position():
    for p in mt5.positions_get(symbol=SYMBOL) or []:
        if p.magic == MAGIC:
            return p
    return None


def record_closed_trade(ticket, ref_price, entry_price, side, sl, tp, entry_regime):
    deals = mt5.history_deals_get(position=ticket)
    if not deals:
        return None
    exit_deal = max(deals, key=lambda d: d.time)
    if exit_deal.entry != 1:
        exit_deal = next((d for d in deals if d.entry == 1), None)
    if exit_deal is None:
        return None
    pnl = sum(d.profit + d.swap + d.commission for d in deals)
    trade = {
        "ticket": ticket, "side": side, "entry_time": None,
        "exit_time": datetime.fromtimestamp(exit_deal.time, tz=timezone.utc).isoformat(),
        "entry": entry_price, "exit": exit_deal.price, "sl": sl, "tp": tp,
        "pnl": round(pnl, 4),
        "ref_price": ref_price,
        "entry_slippage": round(abs(entry_price - ref_price), 2) if ref_price else None,
        "entry_regime": entry_regime,
    }
    return trade


def _classify_decision(is_anomaly, h1_agree, regime_ok, regime):
    """Pure mapping from the existing gate booleans to the standardized
    funnel vocabulary (2026-08-20, Ahmed-approved, instrumentation-only).
    Mirrors the exact same if/elif chain already used below for the
    log_event() SIGNAL_REJECTED reasons -- adds no new condition, changes
    no control flow, purely a label for what the code already decided."""
    if not is_anomaly:
        return "NO_CANDIDATE", None
    if not h1_agree:
        return "REJECTED", "h1_disagree"
    if not regime_ok:
        return "REJECTED", "regime_classification_failed"
    if regime != "Range":
        return "REJECTED", f"not_range_regime:{regime}"
    return "ELIGIBLE", None


def _test_classify_decision():
    assert _classify_decision(False, True, True, "Range") == ("NO_CANDIDATE", None)
    assert _classify_decision(True, False, True, "Range") == ("REJECTED", "h1_disagree")
    assert _classify_decision(True, True, False, None) == ("REJECTED", "regime_classification_failed")
    assert _classify_decision(True, True, True, "Trend") == ("REJECTED", "not_range_regime:Trend")
    assert _classify_decision(True, True, True, "Range") == ("ELIGIBLE", None)
    print("participation_pilot_btc_range _classify_decision self-check: OK")


def _connect_mt5():
    delay = INIT_RETRY_BASE
    while True:
        mt5.shutdown()
        if mt5.initialize(**EA):
            return
        log(f"mt5 init failed {mt5.last_error()} -- retrying in {delay}s")
        time.sleep(delay)
        delay = min(INIT_RETRY_MAX, delay * 2)


def main():
    _connect_mt5()
    mt5.symbol_select(SYMBOL, True)
    log(f"Participation Anomaly + Range PILOT started -- {SYMBOL} magic={MAGIC} lot={LOT} "
        f"params={DEFAULT_PARAMS} -- PHASE 1.1: dynamic anomaly threshold "
        f"(rolling P{DYNAMIC_PERCENTILE} over {DYNAMIC_WINDOW_DAYS}d, replaces fixed anomaly_mult) "
        f"-- PHASE 1 PILOT (single account, min lot)")
    log_event("BOT_STARTED", account=ACCOUNT, strategy=STRATEGY, symbol=SYMBOL,
              details={"pid": os.getpid(), "magic": MAGIC, "lot": LOT, "params": DEFAULT_PARAMS,
                       "regime_gate": "Range (Regime Classifier v1, live Binance BTCUSDT H1)",
                       "anomaly_threshold": f"dynamic rolling P{DYNAMIC_PERCENTILE} / {DYNAMIC_WINDOW_DAYS}d window"})

    state = load_state()

    while True:
        try:
            write_heartbeat("participation_pilot_btc_range")
            if not mt5.terminal_info():
                _connect_mt5()
                continue

            # --- did our open position close since last loop? ---
            if state.get("open_ticket") and not my_position():
                t = record_closed_trade(state["open_ticket"], state.get("open_ref_price"),
                                          state.get("open_entry"), state.get("open_side"),
                                          state.get("open_sl"), state.get("open_tp"),
                                          state.get("open_regime"))
                if t:
                    trades = load_trades()
                    trades.append(t)
                    save_trades(trades)
                    log(f"CLOSED ticket={state['open_ticket']} pnl={t['pnl']:+.2f} "
                        f"entry_slippage={t['entry_slippage']} entry_regime={t['entry_regime']}")
                    close_reason = "unknown"
                    saved_sl, saved_tp = state.get("open_sl"), state.get("open_tp")
                    if saved_sl is not None and abs(t["exit"] - saved_sl) <= abs(t["exit"] - (saved_tp or t["exit"])):
                        close_reason = "sl" if saved_sl is not None else close_reason
                    if saved_tp is not None and abs(t["exit"] - saved_tp) < abs(t["exit"] - (saved_sl or t["exit"])):
                        close_reason = "tp"
                    log_event("POSITION_CLOSED", ticket=state["open_ticket"], account=ACCOUNT,
                              strategy=STRATEGY, symbol=SYMBOL, reason=close_reason,
                              details={"pnl": t["pnl"], "exit_price": t["exit"],
                                       "entry_slippage": t["entry_slippage"], "entry_regime": t["entry_regime"]})
                state["open_ticket"] = None
                save_state(state)

            m15 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M15, 0, M15_PULL_COUNT)
            h1 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H1, 0, 500)
            if m15 is None or len(m15) < 30 or h1 is None or len(h1) < 60:
                time.sleep(POLL_SEC)
                continue

            df15 = pd.DataFrame(m15)
            last_closed = df15.iloc[-2]
            bar_time = int(last_closed["time"])

            if state.get("last_bar_time") == bar_time:
                time.sleep(POLL_SEC)
                continue

            df15_closed = df15.iloc[:-1].copy()
            df15_closed["roll_avg"] = df15_closed["tick_volume"].rolling(DEFAULT_PARAMS["roll_window"]).mean()
            df15_closed["ratio"] = df15_closed["tick_volume"] / df15_closed["roll_avg"]
            # causal: shift(1) so the current bar's own ratio never enters its own
            # reference window -- no look-ahead.
            dyn_threshold_series = df15_closed["ratio"].shift(1).rolling(
                DYNAMIC_WINDOW_BARS, min_periods=DYNAMIC_WINDOW_BARS).quantile(DYNAMIC_PERCENTILE / 100)
            roll_avg = df15_closed["roll_avg"].iloc[-1]
            ratio_now = df15_closed["ratio"].iloc[-1]
            dyn_threshold = dyn_threshold_series.iloc[-1]
            atr14 = ta.atr(df15_closed["high"], df15_closed["low"], df15_closed["close"], length=14).iloc[-1]
            is_anomaly = bool(ratio_now >= dyn_threshold) if pd.notna(ratio_now) and pd.notna(dyn_threshold) else False
            bar_dir_up = last_closed["close"] > last_closed["open"]

            dfh1 = pd.DataFrame(h1)
            h1_closed = dfh1.iloc[:-1]
            h1_ema50 = ta.ema(h1_closed["close"], length=50).iloc[-1]
            h1_trend_up = bool(h1_closed["close"].iloc[-1] > h1_ema50) if pd.notna(h1_ema50) else None

            h1_agree = (h1_trend_up is not None) and (bar_dir_up == h1_trend_up)
            base_signal = is_anomaly and h1_agree
            side = "buy" if bar_dir_up else "sell"

            # --- Range Regime gate (the ONLY addition vs the original pilot) ---
            try:
                regime, regime_bar_time, adx_val = get_current_trend_regime()
                regime_ok = True
            except Exception as e:
                regime, regime_bar_time, adx_val = None, None, None
                regime_ok = False
                log(f"REGIME CLASSIFICATION ERROR: {e} -- treating as non-Range (fail-safe: no trade)")
                log_event("ERROR", account=ACCOUNT, strategy=STRATEGY, symbol=SYMBOL,
                          reason="regime_classification_failed", details={"message": str(e)})

            signal = base_signal and regime_ok and (regime == "Range")

            # --- funnel observability (2026-08-20, Ahmed-approved, instrumentation
            # only -- no gate/threshold touched, these are pure additional reads for
            # logging). Wrapped so a failure here can never break the trading loop. ---
            decision, rejection_reason = _classify_decision(is_anomaly, h1_agree, regime_ok, regime)
            try:
                _tick_now = mt5.symbol_info_tick(SYMBOL)
                spread = round(_tick_now.ask - _tick_now.bid, 2) if _tick_now else None
            except Exception:
                spread = None
            try:
                risk_halt_active = os.path.exists(RISK_HALT_FLAG_PATH)
                position_open = bool(my_position())
            except Exception:
                risk_halt_active, position_open = None, None

            append_jsonl(SIGNALS_LOG, {
                "logged_at": now_utc().isoformat(),
                "account": ACCOUNT, "symbol": SYMBOL,
                "bar_time": datetime.fromtimestamp(bar_time, tz=timezone.utc).isoformat(),
                "price": float(last_closed["close"]),
                "tick_volume": int(last_closed["tick_volume"]), "roll_avg": round(float(roll_avg), 1) if pd.notna(roll_avg) else None,
                "ratio": round(float(ratio_now), 4) if pd.notna(ratio_now) else None,
                "dynamic_threshold": round(float(dyn_threshold), 4) if pd.notna(dyn_threshold) else None,
                "is_anomaly": is_anomaly, "bar_dir": side, "h1_trend_up": h1_trend_up,
                "h1_ema50": round(float(h1_ema50), 2) if pd.notna(h1_ema50) else None,
                "h1_agree": h1_agree, "base_signal": base_signal,
                "regime": regime, "regime_h1_bar": str(regime_bar_time), "adx": adx_val,
                "rsi": None,  # not part of this strategy's logic -- null for schema parity with gold_btc_bot
                "spread": spread,
                "atr14": round(float(atr14), 2) if pd.notna(atr14) else None,
                "gates": {"anomaly": is_anomaly, "h1_agreement": h1_agree,
                          "regime_classification_ok": regime_ok, "regime_is_range": (regime == "Range")},
                "signal": signal,
                "decision": decision, "rejection_reason": rejection_reason,
                "cooldown_state": "N/A (no time-based cooldown in this strategy; see position_open)",
                "position_open": position_open,
                "risk_halt_active": risk_halt_active,
                "risk_eligible": (position_open is False) and (risk_halt_active is False),
            })

            temp_id = f"{SYMBOL}-{bar_time}"
            if is_anomaly:
                if signal:
                    log_event("SIGNAL_DETECTED", correlation_id=temp_id, account=ACCOUNT,
                              strategy=STRATEGY, symbol=SYMBOL, reason="anomaly+h1_agree+range_regime",
                              details={"side": side, "tick_volume": int(last_closed["tick_volume"]), "regime": regime})
                elif base_signal and not regime_ok:
                    log_event("SIGNAL_REJECTED", correlation_id=temp_id, account=ACCOUNT,
                              strategy=STRATEGY, symbol=SYMBOL, reason="regime_classification_failed",
                              details={"side": side})
                elif base_signal:
                    log_event("SIGNAL_REJECTED", correlation_id=temp_id, account=ACCOUNT,
                              strategy=STRATEGY, symbol=SYMBOL, reason="not_range_regime",
                              details={"side": side, "regime": regime})
                else:
                    log_event("SIGNAL_REJECTED", correlation_id=temp_id, account=ACCOUNT,
                              strategy=STRATEGY, symbol=SYMBOL, reason="h1_disagree",
                              details={"side": side, "h1_trend_up": h1_trend_up})

            state["last_bar_time"] = bar_time
            save_state(state)

            if not signal:
                time.sleep(POLL_SEC)
                continue

            if my_position():
                log("signal fired but a pilot position is already open -- skip (one at a time, per backtest)")
                log_event("SIGNAL_REJECTED", correlation_id=temp_id, account=ACCOUNT,
                          strategy=STRATEGY, symbol=SYMBOL, reason="position_already_open")
                time.sleep(POLL_SEC)
                continue

            if os.path.exists(RISK_HALT_FLAG_PATH):
                log(f"{SYMBOL} ENTRY BLOCKED -- portfolio-wide RISK_HALT.flag active "
                    f"(open positions still managed normally by ladder_guard.py/ea_shield.py)")
                log_event("SIGNAL_REJECTED", correlation_id=temp_id, account=ACCOUNT,
                          strategy=STRATEGY, symbol=SYMBOL, reason="portfolio_risk_halt")
                time.sleep(POLL_SEC)
                continue

            tick = mt5.symbol_info_tick(SYMBOL)
            ref_price = tick.ask if side == "buy" else tick.bid
            sl_dist = DEFAULT_PARAMS["sl_atr_mult"] * atr14
            sl = ref_price - sl_dist if side == "buy" else ref_price + sl_dist
            tp = ref_price + DEFAULT_PARAMS["rr_mult"] * sl_dist if side == "buy" else ref_price - DEFAULT_PARAMS["rr_mult"] * sl_dist

            log_event("ORDER_SUBMITTED", correlation_id=temp_id, account=ACCOUNT,
                      strategy=STRATEGY, symbol=SYMBOL,
                      details={"type": "market", "side": side, "volume": LOT,
                               "sl": float(sl), "tp": float(tp), "ref_price": ref_price, "regime": regime})

            r = mt5.order_send(dict(
                action=mt5.TRADE_ACTION_DEAL, symbol=SYMBOL, volume=LOT,
                type=mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
                price=ref_price, sl=float(sl), tp=float(tp), magic=MAGIC,
                comment="participation_pilot_range", type_filling=mt5.ORDER_FILLING_IOC))

            if r.retcode == mt5.TRADE_RETCODE_DONE:
                actual_fill = r.price
                log(f"ENTRY {side.upper()} {LOT} ref={ref_price} fill={actual_fill} "
                    f"slippage={abs(actual_fill - ref_price):.2f} SL={sl:.2f} TP={tp:.2f} "
                    f"ticket={r.order} regime={regime}")
                state.update({"open_ticket": r.order, "open_ref_price": ref_price, "open_entry": actual_fill,
                              "open_side": side, "open_sl": float(sl), "open_tp": float(tp),
                              "open_regime": regime})
                save_state(state)
                log_event("ORDER_FILLED", correlation_id=temp_id, ticket=r.order, account=ACCOUNT,
                          strategy=STRATEGY, symbol=SYMBOL,
                          details={"temp_id": temp_id, "fill_price": actual_fill,
                                   "slippage": abs(actual_fill - ref_price), "regime": regime})
            else:
                log(f"order failed retcode={r.retcode} {r.comment}")
                log_event("ORDER_FAILED", correlation_id=temp_id, account=ACCOUNT,
                          strategy=STRATEGY, symbol=SYMBOL, retcode=r.retcode,
                          reason=str(r.comment), details={"side": side})

        except Exception as e:
            log(f"loop error: {e}")
            log_event("ERROR", account=ACCOUNT, strategy=STRATEGY, symbol=SYMBOL,
                      reason=type(e).__name__, details={"message": str(e)})
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    if "--test" in sys.argv:
        _test_classify_decision()
    else:
        main()
