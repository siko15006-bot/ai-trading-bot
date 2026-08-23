"""
bybit_shield.py — naked-position guard for BAA (Bybit), mirror of ea_shield on MT5.
Every 30s: any open position with no stop-loss gets one at 1.5*ATR(15m) from entry
(floored at 0.5% of price). SL only — TP left to the owner. Telegram-notifies on action.

2026-07-26: briefly tried having this also invent a bootstrap TP (RR 2:1 off the
SL distance) so ladder_guard_bybit's existing TP-gated logic would pick these
positions up. Ahmed rejected that -- inventing a target the trader never set is
wrong even with good intentions. The real fix belongs in ladder_guard_bybit.py:
it now manages SL-only positions directly off the real SL distance (R), no TP
ever assumed or set. This file stays exactly as originally designed.
"""
import os
import time
from heartbeat import write_heartbeat  # 2026-08-06: functional heartbeat rollout
import ccxt
from dotenv import load_dotenv
from ntfy_alert import alert  # 2026-08-03: replaces the dead Telegram path below

load_dotenv("/home/ubuntu/.env")

POLL_SECONDS = 30
ATR_MULT = 1.5
MIN_SL_PCT = 0.005

exchange = ccxt.bybit({
    "apiKey": os.environ["BYBIT_API_KEY"],
    "secret": os.environ["BYBIT_API_SECRET"],
    "options": {"recvWindow": 20000},
})


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def notify(text):
    # 2026-08-03: was Telegram (TG_TOKEN/TG_CHAT never set in .env, silently
    # no-op'd since first deploy) -- same function name, same call site,
    # same silent-failure contract, now routed through ntfy.sh instead.
    try:
        alert(text, key="bybit_shield_notify")
    except Exception as e:
        log(f"notify failed: {e}")


def atr_15m(symbol, n=14):
    try:
        bars = exchange.fetch_ohlcv(symbol, "15m", limit=n + 1)
        trs = []
        for i in range(1, len(bars)):
            h, l, pc = bars[i][2], bars[i][3], bars[i - 1][4]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        return sum(trs) / len(trs) if trs else None
    except Exception as e:
        log(f"atr fetch failed {symbol}: {e}")
        return None


def main():
    log(f"bybit_shield started — guarding naked BAA positions (SL={ATR_MULT}xATR15m, floor {MIN_SL_PCT:.1%})")
    while True:
        try:
            for p in exchange.fetch_positions():
                qty = float(p.get("contracts") or 0)
                if qty == 0:
                    continue
                sl = p.get("stopLossPrice")
                if sl and float(sl) > 0:
                    continue
                symbol = p["symbol"]
                entry = float(p["entryPrice"])
                side = p["side"]
                atr = atr_15m(symbol)
                dist = max(ATR_MULT * atr if atr else 0, entry * MIN_SL_PCT)
                sl_price = entry - dist if side == "long" else entry + dist
                exchange.private_post_v5_position_trading_stop({
                    "category": "linear",
                    "symbol": symbol.replace("/", "").replace(":USDT", ""),
                    "stopLoss": str(round(sl_price, 6)),
                    "slTriggerBy": "MarkPrice",
                    "positionIdx": 0,
                })
                log(f"SHIELD: naked {side} {symbol} qty={qty} entry={entry} -> SL {sl_price:.6f}")
                notify(f"🛡 Bybit Shield: صفقة {symbol} {side} كانت من غير ستوب — اتأمّنت SL={sl_price:.6f}")
            write_heartbeat('bybit_shield')
        except Exception as e:
            log(f"loop error: {str(e)[:150]}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
