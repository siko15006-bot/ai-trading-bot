"""
EMA-ADX Bot — XAUUSD.s M15 on Bybit MT5
========================================
Backtested 2026-07-03 (backtest_candidates.py, 6 months M15):
  EMA20/50 cross + EMA200 filter + ADX>=25, SL=200pts TP=600pts (RR 1:3)
  -> 26 trades, 31% WR, +$10.18 on $50, MaxDD 30%
Runs alongside London Breakout (magic 990099) as the second proven system.

RSI divergence filter added 2026-07-10 (backtest_ema_adx_divergence.py,
6 months M15, same terminal/symbol): requiring the last two confirmed
5-bar-fractal price pivots + RSI(14) at those pivots to agree with the
trade direction (no divergence = momentum confirms the trend) improved
ADX25/SL200/TP600 from 27 trades/26% WR/+$0.11/MaxDD34% to 15 trades/27%
WR/+$0.95/MaxDD25% -- fewer, more selective trades with meaningfully
lower drawdown. Sample is thin (15 trades) so keep watching live results.

Entry (on last CLOSED M15 bar):
  BUY : EMA20 crosses above EMA50 AND close > EMA200 AND ADX >= 25 AND no bearish RSI divergence
  SELL: EMA20 crosses below EMA50 AND close < EMA200 AND ADX >= 25 AND no bullish RSI divergence
Management: one position at a time; SL -> breakeven at 50% of TP.
"""
import MetaTrader5 as mt5
import pandas as pd
import pandas_ta as ta
import time
import json
import threading
from datetime import datetime, timezone

import sys

# account-selectable (2026-07-07): suspended on BA (margin too thin after the
# rogue-EA bleed), redeployed on EA which can carry its backtested 30% MaxDD.
ACCOUNTS = {
    "BA": dict(init=dict(path=r"C:\Program Files\MetaTrader 5\terminal64.exe"),
               symbol="XAUUSD.s", magic=993399),
    "EA": dict(init=dict(path=r"C:\MT5_Portable_2\terminal64.exe",
                         login=<REDACTED_MT5_LOGIN_EA>, password="<REDACTED_MT5_PASSWORD_EA>",
                         server="Exness-MT5Real33"),
               symbol="XAUUSDm", magic=993400),
}
ACC = sys.argv[1] if len(sys.argv) > 1 else "BA"
CFG = ACCOUNTS[ACC]

MT5_INIT = CFG["init"]
SYMBOL   = CFG["symbol"]
LOT      = 0.02  # 2026-07-15: doubled with EA/BA lot bump, Ahmed's explicit approval
SL_PTS   = 200
TP_PTS   = 600
ADX_MIN  = 25
MAGIC    = CFG["magic"]
TICK     = 90
LOG      = rf"C:\TradingBot\Bot_Active\ema_adx_bot_{ACC}.log"

be_done_for = None  # position id already moved to breakeven


def log(msg):
    line = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC | {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# 2026-08-06 incident fix: mt5.initialize() has no internal timeout and can
# hang indefinitely if the terminal's IPC channel is wedged (root cause of
# the 8.5h silent outage -- process stayed "alive" forever, blocked inside
# this one call, with zero further log output and no way for a process-
# existence-only watchdog to tell). Run it on a daemon thread with a real
# join() timeout so a wedged IPC channel can only cost one tick, not the
# whole run.
INIT_TIMEOUT_SEC = 30


def init_with_timeout():
    result = {}
    def target():
        result["ok"] = mt5.initialize(**MT5_INIT)
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(INIT_TIMEOUT_SEC)
    if t.is_alive():
        return False, "timeout"
    return result.get("ok", False), None


HEARTBEAT_FILE = rf"C:\TradingBot\Bot_Active\ema_adx_heartbeat_{ACC}.json"
_cycle_count = 0


def write_heartbeat():
    """Functional heartbeat -- 'a process exists' proved nothing during the
    2026-08-06 incident; this proves the bot actually completed a real
    cycle (successful mt5.initialize + position check/signal eval +
    shutdown), which is what a health check should actually verify."""
    global _cycle_count
    _cycle_count += 1
    try:
        with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_success_ts": datetime.now(timezone.utc).isoformat(),
                       "cycle_count": _cycle_count, "account": ACC, "symbol": SYMBOL}, f)
    except Exception as e:
        log(f"WARNING: heartbeat write failed: {e}")


DIV_N = 2          # 5-bar fractal (same as backtest_ema_adx_divergence.py)
DIV_LOOKBACK = 50  # max bars between the two pivots being compared


def check_divergence(df, direction):
    """True if the last two confirmed price pivots (lows for direction=1,
    highs for direction=-1) and RSI at those pivots move the SAME direction
    -- momentum confirms the trend, no divergence warning."""
    lows, highs, rsi = df["low"].values, df["high"].values, df["RSI"].values
    n, N = DIV_N, len(df)
    pivots = []
    for i in range(n, N - n):
        if direction == 1 and lows[i] == min(lows[i - n:i + n + 1]):
            pivots.append((i, lows[i]))
        elif direction == -1 and highs[i] == max(highs[i - n:i + n + 1]):
            pivots.append((i, highs[i]))
    if len(pivots) < 2:
        return False
    (i1, p1), (i2, p2) = pivots[-2], pivots[-1]
    if i2 - i1 > DIV_LOOKBACK:
        return False
    if direction == 1:
        return p2 > p1 and rsi[i2] > rsi[i1]
    return p2 < p1 and rsi[i2] < rsi[i1]


def get_signal():
    rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M15, 0, 300)
    if rates is None or len(rates) < 250:
        return 0, None
    df = pd.DataFrame(rates)
    df["EMA20"]  = ta.ema(df["close"], length=20)
    df["EMA50"]  = ta.ema(df["close"], length=50)
    df["EMA200"] = ta.ema(df["close"], length=200)
    df["ADX"] = ta.adx(df["high"], df["low"], df["close"], length=14)["ADX_14"]
    df["RSI"] = ta.rsi(df["close"], length=14)
    # bar -1 is forming; -2 is the last closed bar, -3 the one before
    c, p = df.iloc[-2], df.iloc[-3]
    if pd.isna(c["ADX"]) or c["ADX"] < ADX_MIN:
        return 0, c
    if c["EMA20"] > c["EMA50"] and p["EMA20"] <= p["EMA50"] and c["close"] > c["EMA200"]:
        if not check_divergence(df, 1):
            return 0, c
        return 1, c
    if c["EMA20"] < c["EMA50"] and p["EMA20"] >= p["EMA50"] and c["close"] < c["EMA200"]:
        if not check_divergence(df, -1):
            return 0, c
        return -1, c
    return 0, c


def place(direction):
    tick = mt5.symbol_info_tick(SYMBOL)
    pt = mt5.symbol_info(SYMBOL).point
    if direction == 1:
        price, otype = tick.ask, mt5.ORDER_TYPE_BUY
        sl, tp = price - SL_PTS * pt, price + TP_PTS * pt
    else:
        price, otype = tick.bid, mt5.ORDER_TYPE_SELL
        sl, tp = price + SL_PTS * pt, price - TP_PTS * pt
    req = dict(action=mt5.TRADE_ACTION_DEAL, symbol=SYMBOL, volume=LOT, type=otype,
               price=price, sl=sl, tp=tp, magic=MAGIC, comment="EMA_ADX",
               type_time=mt5.ORDER_TIME_GTC, type_filling=mt5.ORDER_FILLING_IOC)
    r = mt5.order_send(req)
    log(f"ENTRY {'BUY' if direction == 1 else 'SELL'} @ {price} SL={sl:.2f} TP={tp:.2f} -> {r.retcode}")
    return r


def manage(pos):
    """Move SL to entry once profit reaches 50% of TP distance."""
    global be_done_for
    if be_done_for == pos.ticket:
        return
    pt = mt5.symbol_info(SYMBOL).point
    tick = mt5.symbol_info_tick(SYMBOL)
    half_tp = TP_PTS * pt * 0.5
    in_profit = (tick.bid - pos.price_open) if pos.type == 0 else (pos.price_open - tick.ask)
    if in_profit >= half_tp:
        r = mt5.order_send(dict(action=mt5.TRADE_ACTION_SLTP, position=pos.ticket,
                                symbol=SYMBOL, sl=pos.price_open, tp=pos.tp))
        log(f"BREAKEVEN ticket={pos.ticket} sl->{pos.price_open} retcode={r.retcode}")
        if r.retcode == mt5.TRADE_RETCODE_DONE:
            be_done_for = pos.ticket


log(f"EMA-ADX Bot start | {SYMBOL} SL={SL_PTS} TP={TP_PTS} ADX>={ADX_MIN} magic={MAGIC}")
while True:
    try:
        ok, err = init_with_timeout()
        if not ok:
            log(f"mt5 init failed {mt5.last_error() if err is None else err}")
            time.sleep(TICK)
            continue
        positions = [p for p in (mt5.positions_get(symbol=SYMBOL) or []) if p.magic == MAGIC]
        if positions:
            manage(positions[0])
        else:
            sig, bar = get_signal()
            if sig != 0:
                place(sig)
        mt5.shutdown()
        write_heartbeat()
    except Exception as e:
        log(f"ERROR {e}")
        try: mt5.shutdown()
        except Exception: pass
    time.sleep(TICK)
