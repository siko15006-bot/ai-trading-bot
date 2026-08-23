"""
e2_ctl.py — direct control tool for E2 (Exness <REDACTED_MT5_LOGIN_EA>) — no MCP needed
==========================================================================
Usage:
  python e2_ctl.py list
  python e2_ctl.py close <ticket>
  python e2_ctl.py close_symbol <SYMBOL>
  python e2_ctl.py sltp <ticket> <sl> <tp>      (0 keeps current value)
  python e2_ctl.py breakeven <ticket>
  python e2_ctl.py open <SYMBOL> <buy|sell> <lot> <sl_dist> <tp_dist>   (distances in price units)
"""
import os
import sys
import MetaTrader5 as mt5

E2 = dict(path=r"C:\MT5_Portable_2\terminal64.exe",
          login=<REDACTED_MT5_LOGIN_EA>, password="<REDACTED_MT5_PASSWORD_EA>", server="Exness-MT5Real33")

# 2026-08-08 (Ahmed, explicit approval): manual `open` respects the same
# portfolio-wide RISK_HALT as every autonomous bot -- close/sltp/breakeven/
# close_symbol are untouched (management/protection of existing positions,
# not new risk). No automatic bypass/emergency override exists by design.
RISK_HALT_FLAG_PATH = r"C:\TradingBot\Bot_Active\RISK_HALT.flag"


def die(msg):
    print(f"ERROR: {msg}")
    mt5.shutdown()
    sys.exit(1)


if not mt5.initialize(**E2):
    print(f"ERROR: init failed {mt5.last_error()}")
    sys.exit(1)

cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

if cmd == "balance":
    info = mt5.account_info()
    print(f"balance={info.balance:.2f} equity={info.equity:.2f} profit={info.profit:+.2f}")

elif cmd == "list":
    for p in mt5.positions_get() or []:
        side = "BUY" if p.type == 0 else "SELL"
        print(f"{p.ticket} {p.symbol} {side} {p.volume} @ {p.price_open} SL={p.sl} TP={p.tp} PnL={p.profit:+.2f} magic={p.magic}")

elif cmd in ("close", "close_symbol"):
    if cmd == "close":
        targets = [p for p in (mt5.positions_get() or []) if p.ticket == int(sys.argv[2])]
    else:
        targets = list(mt5.positions_get(symbol=sys.argv[2]) or [])
    if not targets:
        die("position not found")
    for p in targets:
        tick = mt5.symbol_info_tick(p.symbol)
        price = tick.bid if p.type == 0 else tick.ask
        otype = mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY
        r = mt5.order_send(dict(action=mt5.TRADE_ACTION_DEAL, position=p.ticket,
                                symbol=p.symbol, volume=p.volume, type=otype, price=price,
                                comment="MCP_close", type_filling=mt5.ORDER_FILLING_IOC))
        print(f"close {p.ticket} {p.symbol} -> retcode={r.retcode}")

elif cmd == "sltp":
    ticket, sl, tp = int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
    pos = [p for p in (mt5.positions_get() or []) if p.ticket == ticket]
    if not pos:
        die("position not found")
    p = pos[0]
    r = mt5.order_send(dict(action=mt5.TRADE_ACTION_SLTP, position=ticket, symbol=p.symbol,
                            sl=sl or p.sl, tp=tp or p.tp))
    print(f"sltp {ticket} sl={sl or p.sl} tp={tp or p.tp} -> retcode={r.retcode}")

elif cmd == "breakeven":
    ticket = int(sys.argv[2])
    pos = [p for p in (mt5.positions_get() or []) if p.ticket == ticket]
    if not pos:
        die("position not found")
    p = pos[0]
    r = mt5.order_send(dict(action=mt5.TRADE_ACTION_SLTP, position=ticket, symbol=p.symbol,
                            sl=p.price_open, tp=p.tp))
    print(f"breakeven {ticket} sl->{p.price_open} -> retcode={r.retcode}")

elif cmd == "pending":
    # pending <SYMBOL> <buy|sell> <stop|limit> <lot> <price> <sl_dist> <tp_dist>
    sym, side, kind, lot = sys.argv[2], sys.argv[3].lower(), sys.argv[4].lower(), float(sys.argv[5])
    price, sl_dist, tp_dist = float(sys.argv[6]), float(sys.argv[7]), float(sys.argv[8])
    if not mt5.symbol_select(sym, True):
        die(f"symbol {sym} not available")
    otype = {("buy", "stop"): mt5.ORDER_TYPE_BUY_STOP, ("buy", "limit"): mt5.ORDER_TYPE_BUY_LIMIT,
             ("sell", "stop"): mt5.ORDER_TYPE_SELL_STOP, ("sell", "limit"): mt5.ORDER_TYPE_SELL_LIMIT}[(side, kind)]
    sl = price - sl_dist if side == "buy" else price + sl_dist
    tp = price + tp_dist if side == "buy" else price - tp_dist
    r = mt5.order_send(dict(action=mt5.TRADE_ACTION_PENDING, symbol=sym, volume=lot,
                            type=otype, price=price, sl=sl, tp=tp,
                            comment="claude_pending", type_time=mt5.ORDER_TIME_GTC,
                            type_filling=mt5.ORDER_FILLING_RETURN))
    print(f"pending {side} {kind} {sym} {lot} @ {price} SL={sl} TP={tp} -> retcode={r.retcode}"
          + (f" order={r.order}" if r.retcode == mt5.TRADE_RETCODE_DONE else f" comment={r.comment}"))

elif cmd == "open":
    if os.path.exists(RISK_HALT_FLAG_PATH):
        die("ENTRY BLOCKED -- portfolio-wide RISK_HALT.flag active (portfolio_risk_guard.py)")
    sym, side, lot = sys.argv[2], sys.argv[3].lower(), float(sys.argv[4])
    sl_dist, tp_dist = float(sys.argv[5]), float(sys.argv[6])
    if not mt5.symbol_select(sym, True):
        die(f"symbol {sym} not available")
    tick = mt5.symbol_info_tick(sym)
    if tick is None or tick.ask == 0:
        die(f"no quotes for {sym} (market closed?)")
    if side == "buy":
        price, otype = tick.ask, mt5.ORDER_TYPE_BUY
        sl, tp = price - sl_dist, price + tp_dist
    else:
        price, otype = tick.bid, mt5.ORDER_TYPE_SELL
        sl, tp = price + sl_dist, price - tp_dist
    r = mt5.order_send(dict(action=mt5.TRADE_ACTION_DEAL, symbol=sym, volume=lot,
                            type=otype, price=price, sl=sl, tp=tp,
                            comment="claude_open", type_filling=mt5.ORDER_FILLING_IOC))
    print(f"open {side} {sym} {lot} @ {price} SL={sl} TP={tp} -> retcode={r.retcode}"
          + (f" ticket={r.order}" if r.retcode == mt5.TRADE_RETCODE_DONE else f" comment={r.comment}"))

else:
    die(f"unknown command {cmd}")

mt5.shutdown()
