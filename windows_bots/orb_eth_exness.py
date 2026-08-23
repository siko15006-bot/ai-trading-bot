"""
ORB ETH Exness v1.0 — port of oracle orb_bot.py to MT5 (EA account <REDACTED_MT5_LOGIN_EA>)
Backtested on Exness M3 data 180d (2026-07-05): ETHUSDm +45% sum, WR 50%,
both halves positive, survives 2x spread stress. SOL rejected (1.47% spread),
BTC rejected (walk-forward failure) — ETH ONLY. Do not tweak without re-backtest.

Rules (identical to the proven backtest):
  - Session 13:30 UTC. OR = first 30 min (10 x 3m bars). Scan closed 3m bars to 19:30.
  - LONG:  close > OR_high AND close > daily VWAP AND EMA9 > EMA20
           AND close between EMA9/EMA20 (pullback).  SHORT mirrored.
  - SL = opposite OR side (skip if OR > 5% of price). TP = 3 x SL dist.
  - One trade/day. Force-close 19:30 UTC. No trailing/BE (not modeled in backtest).
"""
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

import MetaTrader5 as mt5
from heartbeat import write_heartbeat  # 2026-08-06: functional heartbeat rollout, see NOTIFICATION_POLICY.md
from bot_period_guard import manual_block_reason  # 2026-08-22: per-account manual entry block (EM low-equity)

# 2026-08-07 (Ahmed-approved): same portfolio-wide circuit breaker
# gold_btc_bot.py already respects, wired in here too. Blocks NEW entries
# only -- force_close() (session-end exit) and MT5's own SL/TP are
# completely untouched, so an open position stays managed normally even
# while halted.
RISK_HALT_FLAG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "RISK_HALT.flag")


def _risk_halt_block_reason():
    """Return a reason string if NEW entries must be blocked (RISK_HALT active, or
    the flag dir is unreadable -> fail closed), else None. Read-only, never raises."""
    block_reason = manual_block_reason("orb_eth_exness", ACC)
    if block_reason:
        return block_reason
    try:
        _flag_dir = os.path.dirname(RISK_HALT_FLAG_PATH)
        if _flag_dir and not os.path.isdir(_flag_dir):
            return "RISK_HALT flag dir unreadable -- failing closed"
        if os.path.exists(RISK_HALT_FLAG_PATH):
            return "portfolio-wide RISK_HALT.flag active"
    except OSError as _e:
        return f"RISK_HALT flag check error ({_e}) -- failing closed"
    return None

ACCOUNTS = {
    "EA": dict(path=r"C:\MT5_Portable_2\terminal64.exe",
               login=<REDACTED_MT5_LOGIN_EA>, password="<REDACTED_MT5_PASSWORD_EA>", server="Exness-MT5Real33"),
    # EM: gold banned forever on this account — this bot is ETH-only so OK.
    "EM": dict(path=r"C:\MT5_Portable_3\terminal64.exe",
               login=<REDACTED_MT5_LOGIN_EM>, password="<REDACTED_MT5_PASSWORD_EM>", server="Exness-MT5Real35"),
}
ACC = sys.argv[1].upper() if len(sys.argv) > 1 else "EA"
EA = ACCOUNTS[ACC]

SYMBOL = "ETHUSDm"
MAGIC = 992200 if ACC == "EA" else 992201
RR = 3.0
RISK_PCT = 0.015
MAX_OR_PCT = 0.05
MIN_LOT = 0.01
SESSION_OPEN = 13 * 60 + 30
OR_END = SESSION_OPEN + 30
SESSION_CLOSE = SESSION_OPEN + 6 * 60
TICK_SEC = 35
STATE_FILE = rf"C:\TradingBot\Bot_Active\orb_eth_state_{ACC}.json"
# Exness server time == UTC (verified 2026-07-05 by matching bars vs Bybit klines)


def now_utc():
    return datetime.now(timezone.utc)


def log(msg):
    print(f'[{now_utc().strftime("%H:%M:%S")}] {msg}', flush=True)


def utc_minute():
    n = now_utc()
    return n.hour * 60 + n.minute


def utc_date():
    return now_utc().strftime("%Y-%m-%d")


def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        return s if s.get("date") == utc_date() else {"date": utc_date(), "traded": False}
    except Exception:
        return {"date": utc_date(), "traded": False}


def save_state(s):
    try:
        tmp = STATE_FILE + f".tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump(s, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log(f"state save error: {e}")


def calc_ema(closes, period):
    k = 2 / (period + 1)
    e = closes[0]
    for c in closes[1:]:
        e = c * k + e * (1 - k)
    return e


def my_position():
    for p in mt5.positions_get(symbol=SYMBOL) or []:
        if p.magic == MAGIC:
            return p
    return None


def normalize_volume(info, requested):
    step = info.volume_step or 0.01
    minimum = info.volume_min or step
    maximum = info.volume_max or requested
    if step <= 0:
        step = 0.01
    raw = max(minimum, min(requested, maximum))
    lot = math.floor(raw / step) * step
    if lot < minimum:
        lot = minimum
    return round(lot, 8)


def force_close(p):
    tick = mt5.symbol_info_tick(SYMBOL)
    price = tick.bid if p.type == 0 else tick.ask
    otype = mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY
    r = mt5.order_send(dict(action=mt5.TRADE_ACTION_DEAL, position=p.ticket,
                            symbol=SYMBOL, volume=p.volume, type=otype, price=price,
                            magic=MAGIC, comment="orb_session_end",
                            type_filling=mt5.ORDER_FILLING_IOC))
    log(f"SESSION-END CLOSE ticket={p.ticket} pnl={p.profit:+.2f} retcode={r.retcode}")


def run():
    if not mt5.initialize(**EA):
        log(f"mt5 init failed {mt5.last_error()}")
        return
    mt5.symbol_select(SYMBOL, True)
    log(f"ORB ETH Exness v1.0 started — account {ACC} {SYMBOL} magic={MAGIC} session 13:30-19:30 UTC RR 1:{RR:.0f}")
    state = load_state()

    while True:
        try:
            # written at the top of each iteration, not just on a trade --
            # this bot spends most of the day outside its 13:30-19:30 session
            # window doing time checks only, so "a full trade cycle" isn't a
            # fair health definition here. What matters is the loop itself
            # never getting stuck inside an MT5 call (the ema_adx_bot failure
            # class) -- a hang anywhere below means this line stops updating.
            write_heartbeat(f"orb_eth_exness_{ACC}")
            m = utc_minute()

            if state.get("date") != utc_date():
                state = {"date": utc_date(), "traded": False}
                save_state(state)
                log("new day — state reset")

            if m < OR_END or m >= SESSION_CLOSE + 10:
                time.sleep(60)
                continue

            if m >= SESSION_CLOSE:
                p = my_position()
                if p:
                    force_close(p)
                time.sleep(60)
                continue

            if state["traded"] or my_position():
                if my_position():
                    state["traded"] = True
                    save_state(state)
                time.sleep(TICK_SEC)
                continue

            rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M3, 0, 500)
            if rates is None or len(rates) < 30:
                time.sleep(TICK_SEC)
                continue
            # stale feed guard (weekend/holiday): newest bar must be < 10 min old
            if now_utc().timestamp() - int(rates[-1][0]) > 600:
                time.sleep(300)
                continue

            bars = [(int(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in rates]  # t,o,h,l,c,tickvol
            n = now_utc()
            or_start = int(n.replace(hour=13, minute=30, second=0, microsecond=0).timestamp())
            or_bars = [b for b in bars if or_start <= b[0] < or_start + 1800]
            if len(or_bars) < 10:
                time.sleep(TICK_SEC)
                continue
            or_high = max(b[2] for b in or_bars)
            or_low = min(b[3] for b in or_bars)

            price = bars[-2][4]  # last CLOSED 3m bar
            closed = [b[4] for b in bars[:-1]]
            ema9 = calc_ema(closed, 9)
            ema20 = calc_ema(closed, 20)
            midnight = int(n.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            today = [b for b in bars if b[0] >= midnight] or bars[-20:]
            vol = sum(b[5] for b in today)
            vwap = sum(((b[2] + b[3] + b[4]) / 3) * b[5] for b in today) / vol if vol else 0

            lo_e, hi_e = min(ema9, ema20), max(ema9, ema20)
            pullback_ok = lo_e <= price <= hi_e
            sig = 0
            if price > or_high and price > vwap and ema9 > ema20 and pullback_ok:
                sig = 1
            elif price < or_low and price < vwap and ema9 < ema20 and pullback_ok:
                sig = -1
            if not sig:
                time.sleep(TICK_SEC)
                continue

            sl = or_low if sig == 1 else or_high
            dist = abs(price - sl)
            if dist <= 0 or dist / price > MAX_OR_PCT:
                log(f"OR too wide ({dist / price:.1%}) — skip today")
                state["traded"] = True
                save_state(state)
                continue
            tp = price + dist * RR if sig == 1 else price - dist * RR

            _halt = _risk_halt_block_reason()
            if _halt:
                log(f"{SYMBOL} ENTRY BLOCKED -- {_halt} "
                    f"(open positions still managed normally by SL/TP and force_close())")
                time.sleep(TICK_SEC)
                continue

            acc = mt5.account_info()
            risk_usd = acc.balance * RISK_PCT
            si = mt5.symbol_info(SYMBOL)
            if si is None:
                log("symbol_info unavailable -- skip")
                time.sleep(TICK_SEC)
                continue
            # broker limits change live; normalize to the current min/step/max instead of
            # assuming a fixed 0.1 cap.
            step = si.volume_step or 0.01
            requested_lot = math.floor(risk_usd / dist / step) * step
            lot = normalize_volume(si, requested_lot)
            if lot < (si.volume_min or MIN_LOT):
                log(f"invalid volume candidate {lot} (min={si.volume_min}, step={si.volume_step}, max={si.volume_max}) -- skip")
                state["traded"] = True
                save_state(state)
                continue

            tick = mt5.symbol_info_tick(SYMBOL)
            px = tick.ask if sig == 1 else tick.bid
            r = mt5.order_send(dict(
                action=mt5.TRADE_ACTION_DEAL, symbol=SYMBOL, volume=lot,
                type=mt5.ORDER_TYPE_BUY if sig == 1 else mt5.ORDER_TYPE_SELL,
                price=px, sl=float(sl), tp=float(tp), magic=MAGIC,
                comment="orb_eth", type_filling=mt5.ORDER_FILLING_IOC))
            if r.retcode == mt5.TRADE_RETCODE_DONE:
                log(f"ENTRY {'BUY' if sig == 1 else 'SELL'} {lot} @ {px} SL={sl:.2f} TP={tp:.2f} ticket={r.order}")
            else:
                log(f"order failed retcode={r.retcode} {r.comment}")
            state["traded"] = True
            save_state(state)

        except Exception as e:
            log(f"tick error: {e}")
            try:
                if not mt5.terminal_info():
                    mt5.initialize(**EA)
            except Exception:
                pass
        time.sleep(TICK_SEC)


if __name__ == "__main__":
    run()
