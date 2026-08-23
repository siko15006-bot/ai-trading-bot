"""
participation_pilot_btc.py -- SMALL LIVE PILOT, not production. Per Ahmed's
explicit approval: verify the Participation-Anomaly BTC edge (Baseline +
H1-trend filter, validated in Research_ParticipationAnomaly_2026) continues
on current live data before any production decision. Zero logic changes
from the validated research: same anomaly definition, same H1 filter, same
SL/TP. No grid search, no new filters, no trailing/BE (not modeled in the
backtest, so not added here).

Account: EA (Exness-MT5Real33) -- the same account the research data (M15
BTCUSDm) was fetched from, so spread/slippage behavior matches what was
backtested. Min lot size (0.01) only -- pilot, not sized for production.

Tracks everything Ahmed asked to see during the pilot:
  - every signal detected (anomaly + H1-agreement), traded or not
  - every executed trade, with reference price vs actual fill (slippage)
  - a local trades log from which PF/Expectancy can be computed any time
    (see pilot_status.py in the research project)
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
from participation_strategy import DEFAULT_PARAMS  # roll_window, anomaly_mult, sl_atr_mult, rr_mult -- single source of truth

# Event Store Phase 1 pilot (EVENT_STORE_DESIGN.md v3, Ahmed-approved
# 2026-08-06). Additive only -- every existing print()/append_jsonl() call
# below is untouched, these are extra log_event() calls alongside them.
from event_log import log_event
from heartbeat import write_heartbeat  # 2026-08-06: functional heartbeat rollout, see NOTIFICATION_POLICY.md
                                        # (separate from event_log -- that's the trade-lifecycle
                                        # store, this is the plain liveness signal every bot gets)

ACCOUNT = "EA"
STRATEGY = "participation_pilot_btc"

EA = dict(path=r"C:\MT5_Portable_2\terminal64.exe",
          login=<REDACTED_MT5_LOGIN_EA>, password="<REDACTED_MT5_PASSWORD_EA>", server="Exness-MT5Real33")

SYMBOL = "BTCUSDm"
MAGIC = 995501  # unique to this bot -- was 995500 until 2026-08-06, which collided
                # with swing_pending_bot.py's own MAGIC (same number, two different
                # strategies, broke position attribution). Never share a magic again.
LOT = 0.01
POLL_SEC = 30
INIT_RETRY_BASE = 5
INIT_RETRY_MAX = 60

BASE_DIR = r"C:\TradingBot\Bot_Active"
SIGNALS_LOG = os.path.join(BASE_DIR, "participation_pilot_signals.jsonl")
TRADES_LOG = os.path.join(BASE_DIR, "participation_pilot_trades.json")
STATE_FILE = os.path.join(BASE_DIR, "participation_pilot_state.json")


def now_utc():
    return datetime.now(timezone.utc)


def log(msg):
    print(f'[{now_utc().strftime("%Y-%m-%d %H:%M:%S")}] {msg}', flush=True)


def append_jsonl(path, row):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def load_trades():
    if os.path.exists(TRADES_LOG):
        with open(TRADES_LOG) as f:
            return json.load(f)
    return []


def save_trades(trades):
    with open(TRADES_LOG, "w", encoding="utf-8") as f:
        json.dump(trades, f, indent=2, default=str)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_bar_time": None, "open_ticket": None, "open_ref_price": None}


def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, default=str)


def my_position():
    for p in mt5.positions_get(symbol=SYMBOL) or []:
        if p.magic == MAGIC:
            return p
    return None


def record_closed_trade(ticket, ref_price, entry_price, side, sl, tp):
    deals = mt5.history_deals_get(position=ticket)
    if not deals:
        return None
    exit_deal = max(deals, key=lambda d: d.time)
    if exit_deal.entry != 1:  # 1 = DEAL_ENTRY_OUT (closing deal)
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
    }
    return trade


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
    log(f"Participation Anomaly PILOT started -- {SYMBOL} magic={MAGIC} lot={LOT} "
        f"params={DEFAULT_PARAMS} -- SMALL PILOT, NOT PRODUCTION")
    log_event("BOT_STARTED", account=ACCOUNT, strategy=STRATEGY, symbol=SYMBOL,
              details={"pid": os.getpid(), "magic": MAGIC, "lot": LOT, "params": DEFAULT_PARAMS})

    state = load_state()

    while True:
        try:
            write_heartbeat("participation_pilot_btc")  # loop-alive: many early
                                                          # `continue` branches below skip
                                                          # a fixed end-of-loop point
            if not mt5.terminal_info():
                _connect_mt5()
                continue

            # --- did our open position close since last loop? ---
            if state.get("open_ticket") and not my_position():
                t = record_closed_trade(state["open_ticket"], state.get("open_ref_price"),
                                          state.get("open_entry"), state.get("open_side"),
                                          state.get("open_sl"), state.get("open_tp"))
                if t:
                    trades = load_trades()
                    trades.append(t)
                    save_trades(trades)
                    log(f"CLOSED ticket={state['open_ticket']} pnl={t['pnl']:+.2f} "
                        f"entry_slippage={t['entry_slippage']}")
                    # tp/sl inferred from proximity to the saved order levels --
                    # this bot has no other source of truth for why it closed
                    close_reason = "unknown"
                    saved_sl, saved_tp = state.get("open_sl"), state.get("open_tp")
                    if saved_sl is not None and abs(t["exit"] - saved_sl) <= abs(t["exit"] - (saved_tp or t["exit"])):
                        close_reason = "sl" if saved_sl is not None else close_reason
                    if saved_tp is not None and abs(t["exit"] - saved_tp) < abs(t["exit"] - (saved_sl or t["exit"])):
                        close_reason = "tp"
                    log_event("POSITION_CLOSED", ticket=state["open_ticket"], account=ACCOUNT,
                              strategy=STRATEGY, symbol=SYMBOL, reason=close_reason,
                              details={"pnl": t["pnl"], "exit_price": t["exit"],
                                       "entry_slippage": t["entry_slippage"]})
                state["open_ticket"] = None
                save_state(state)

            m15 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M15, 0, 100)
            h1 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H1, 0, 500)
            if m15 is None or len(m15) < 30 or h1 is None or len(h1) < 60:
                time.sleep(POLL_SEC)
                continue

            df15 = pd.DataFrame(m15)
            last_closed = df15.iloc[-2]  # -1 is the still-forming bar
            bar_time = int(last_closed["time"])

            if state.get("last_bar_time") == bar_time:
                time.sleep(POLL_SEC)
                continue  # already evaluated this closed bar

            df15_closed = df15.iloc[:-1]  # exclude the forming bar for indicator calc
            roll_avg = df15_closed["tick_volume"].rolling(DEFAULT_PARAMS["roll_window"]).mean().iloc[-1]
            atr14 = ta.atr(df15_closed["high"], df15_closed["low"], df15_closed["close"], length=14).iloc[-1]
            is_anomaly = bool(last_closed["tick_volume"] >= DEFAULT_PARAMS["anomaly_mult"] * roll_avg) if pd.notna(roll_avg) else False
            bar_dir_up = last_closed["close"] > last_closed["open"]

            dfh1 = pd.DataFrame(h1)
            h1_closed = dfh1.iloc[:-1]
            h1_ema50 = ta.ema(h1_closed["close"], length=50).iloc[-1]
            h1_trend_up = bool(h1_closed["close"].iloc[-1] > h1_ema50) if pd.notna(h1_ema50) else None

            h1_agree = (h1_trend_up is not None) and (bar_dir_up == h1_trend_up)
            signal = is_anomaly and h1_agree
            side = "buy" if bar_dir_up else "sell"

            append_jsonl(SIGNALS_LOG, {
                "bar_time": datetime.fromtimestamp(bar_time, tz=timezone.utc).isoformat(),
                "tick_volume": int(last_closed["tick_volume"]), "roll_avg": round(float(roll_avg), 1) if pd.notna(roll_avg) else None,
                "is_anomaly": is_anomaly, "bar_dir": side, "h1_trend_up": h1_trend_up,
                "h1_agree": h1_agree, "signal": signal, "atr14": round(float(atr14), 2) if pd.notna(atr14) else None,
            })

            temp_id = f"{SYMBOL}-{bar_time}"
            if is_anomaly:
                if signal:
                    log_event("SIGNAL_DETECTED", correlation_id=temp_id, account=ACCOUNT,
                              strategy=STRATEGY, symbol=SYMBOL, reason="anomaly+h1_agree",
                              details={"side": side, "tick_volume": int(last_closed["tick_volume"])})
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

            tick = mt5.symbol_info_tick(SYMBOL)
            ref_price = tick.ask if side == "buy" else tick.bid
            sl_dist = DEFAULT_PARAMS["sl_atr_mult"] * atr14
            sl = ref_price - sl_dist if side == "buy" else ref_price + sl_dist
            tp = ref_price + DEFAULT_PARAMS["rr_mult"] * sl_dist if side == "buy" else ref_price - DEFAULT_PARAMS["rr_mult"] * sl_dist

            log_event("ORDER_SUBMITTED", correlation_id=temp_id, account=ACCOUNT,
                      strategy=STRATEGY, symbol=SYMBOL,
                      details={"type": "market", "side": side, "volume": LOT,
                               "sl": float(sl), "tp": float(tp), "ref_price": ref_price})

            r = mt5.order_send(dict(
                action=mt5.TRADE_ACTION_DEAL, symbol=SYMBOL, volume=LOT,
                type=mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
                price=ref_price, sl=float(sl), tp=float(tp), magic=MAGIC,
                comment="participation_pilot", type_filling=mt5.ORDER_FILLING_IOC))

            if r.retcode == mt5.TRADE_RETCODE_DONE:
                actual_fill = r.price
                log(f"ENTRY {side.upper()} {LOT} ref={ref_price} fill={actual_fill} "
                    f"slippage={abs(actual_fill - ref_price):.2f} SL={sl:.2f} TP={tp:.2f} ticket={r.order}")
                state.update({"open_ticket": r.order, "open_ref_price": ref_price, "open_entry": actual_fill,
                              "open_side": side, "open_sl": float(sl), "open_tp": float(tp)})
                save_state(state)
                log_event("ORDER_FILLED", correlation_id=temp_id, ticket=r.order, account=ACCOUNT,
                          strategy=STRATEGY, symbol=SYMBOL,
                          details={"temp_id": temp_id, "fill_price": actual_fill,
                                   "slippage": abs(actual_fill - ref_price)})
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
    main()
