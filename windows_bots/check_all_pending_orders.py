"""
check_all_pending_orders.py -- READ-ONLY. Checks pending orders across ALL
THREE MT5 accounts (EA, EM, BA), not just EA (the only one exposed via the
currently-active MCP tool in this session). Built 2026-08-05 after a Buy
Stop sat unnoticed on BA with no SL/TP -- direct evidence this account was
a blind spot, not a hypothetical one.

Flags any pending order missing SL or TP so it doesn't go unnoticed again.
No trading calls, no modifications.
"""
import MetaTrader5 as mt5

ACCOUNTS = {
    "EA": dict(path=r"C:\MT5_Portable_2\terminal64.exe",
               login=<REDACTED_MT5_LOGIN_EA>, password="<REDACTED_MT5_PASSWORD_EA>", server="Exness-MT5Real33"),
    "EM": dict(path=r"C:\MT5_Portable_3\terminal64.exe",
               login=<REDACTED_MT5_LOGIN_EM>, password="<REDACTED_MT5_PASSWORD_EM>", server="Exness-MT5Real35"),
    "BA": dict(path=r"C:\Program Files\MetaTrader 5\terminal64.exe"),
}

ORDER_TYPE_NAMES = {0: "BUY", 1: "SELL", 2: "BUY_LIMIT", 3: "SELL_LIMIT",
                     4: "BUY_STOP", 5: "SELL_STOP", 6: "BUY_STOP_LIMIT", 7: "SELL_STOP_LIMIT"}


def check_account(name, cfg):
    if not mt5.initialize(**cfg):
        print(f"{name}: MT5 init failed {mt5.last_error()}")
        return
    try:
        orders = mt5.orders_get() or []
        if not orders:
            print(f"{name}: no pending orders")
            return
        for o in orders:
            type_name = ORDER_TYPE_NAMES.get(o.type, f"type={o.type}")
            missing = []
            if not o.sl:
                missing.append("SL")
            if not o.tp:
                missing.append("TP")
            flag = f" *** MISSING {'/'.join(missing)} ***" if missing else ""
            print(f"{name}: ticket={o.ticket} {type_name} {o.symbol} vol={o.volume_current} "
                  f"price={o.price_open} sl={o.sl} tp={o.tp} magic={o.magic}{flag}")
    finally:
        mt5.shutdown()


def main():
    for name, cfg in ACCOUNTS.items():
        check_account(name, cfg)


if __name__ == "__main__":
    main()
