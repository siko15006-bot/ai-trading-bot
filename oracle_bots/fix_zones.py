with open('/home/ubuntu/trading-bot/crypto_bot.py', 'r') as f:
    code = f.read()

INSERT_AFTER = 'LIMIT_OFFSET  = 0.001       # 0.1% offset from current price for limit entry\n'
ZONES_BLOCK = 'BTC_BUY_ZONES = [\n    (49000, 51000, \'Zone1_PrevATH\',      47500, 65000),\n    (39000, 41500, \'Zone2_GoldenPocket\', 37000, 55000),\n]\nZONE_RISK_PCT = 0.015\n'

if INSERT_AFTER in code:
    code = code.replace(INSERT_AFTER, INSERT_AFTER + ZONES_BLOCK)
    print('BTC_BUY_ZONES inserted')
else:
    print('ANCHOR NOT FOUND')
    idx = code.find('LIMIT_OFF')
    print(repr(code[idx:idx+80]))

OLD_ENTRY = "                    if sig == 'BUY':\n                        sl = round(price - cfg['sl'], 6)\n                        tp = round(price + cfg['tp'], 6)\n                        zone_risk = None\n                        if symbol == 'BTC/USDT:USDT':\n                            for z_low, z_high, z_name, z_sl, z_tp in BTC_BUY_ZONES:\n                                if z_low <= price <= z_high:\n                                    zone_risk = ZONE_RISK_PCT\n                                    print(f'BTC ZONE ENTRY: {z_name} — risk={ZONE_RISK_PCT*100}%')\n                                    break\n                        qty = calc_qty(cfg, balance, price, risk=zone_risk)\n                        if place_order(symbol, 'buy', qty, sl, tp, price=price):\n                            open_count += 1"
NEW_ENTRY = "                    if sig == 'BUY':\n                        sl = round(price - cfg['sl'], 6)\n                        tp = round(price + cfg['tp'], 6)\n                        qty = calc_qty(cfg, balance, price)\n                        if symbol == 'BTC/USDT:USDT':\n                            for z_low, z_high, z_name, z_sl, z_tp in BTC_BUY_ZONES:\n                                if z_low <= price <= z_high:\n                                    sl = z_sl\n                                    tp = z_tp\n                                    sl_dist = max(price - z_sl, 100)\n                                    import math as _m\n                                    qty = max(cfg['min'], min(cfg['max'], _m.floor((balance*ZONE_RISK_PCT/sl_dist)/cfg['step'])*cfg['step']))\n                                    print(f'BTC ZONE ENTRY: {z_name} @ {price:.0f} SL={z_sl} TP={z_tp} qty={qty}')\n                                    break\n                        if place_order(symbol, 'buy', qty, sl, tp, price=price):\n                            open_count += 1"

if OLD_ENTRY in code:
    code = code.replace(OLD_ENTRY, NEW_ENTRY)
    print('entry block fixed')
else:
    print('ENTRY BLOCK NOT FOUND - searching...')
    idx = code.find("zone_risk = None")
    print(repr(code[max(0,idx-200):idx+300]))

with open('/home/ubuntu/trading-bot/crypto_bot.py', 'w') as f:
    f.write(code)

ok = 'BTC_BUY_ZONES = [' in code and '47500' in code and 'sl = z_sl' in code
print('RESULT:', 'OK' if ok else 'INCOMPLETE')
