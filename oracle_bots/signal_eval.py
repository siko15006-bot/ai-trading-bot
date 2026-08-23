"""
signal_eval.py — evaluates every recorded telegram signal against real price data
and produces per-group performance stats, so Ahmed can decide which groups to trust.

Reads signals_all.json (written by tg_signal_bot for EVERY parsed signal, executed
or not), walks 15m Bybit candles from the signal time and decides the outcome:
  tp1_hit / sl_hit  — first level touched after the entry zone was filled
  no_fill           — price never entered the entry zone within 24h
  open              — filled but neither level hit yet (re-checked next run)
  expired           — open for >7 days with no resolution
Gold (XAUUSD) is evaluated via the XAUT/USDT proxy (Tether Gold tracks spot oz).
Non-gold forex has no data source on this server -> marked fx_untracked.

Run hourly via cron. Writes stats to group_stats.json and prints a table.
"""
import json
import os
import time

import ccxt
from dotenv import load_dotenv

load_dotenv('/home/ubuntu/trading-bot/.env')

SIGNALS = '/home/ubuntu/trading-bot/signals_all.json'
STATS = '/home/ubuntu/trading-bot/group_stats.json'
NO_FILL_H = 24
EXPIRE_D = 7

ex = ccxt.bybit({'enableRateLimit': True})


def eval_symbol(sig):
    s = sig['symbol']
    if s.endswith(':USDT'):
        return s
    if s == 'XAUUSD':
        return 'XAUT/USDT'  # spot gold proxy
    return None


def resolve(sig):
    sym = eval_symbol(sig)
    if sym is None:
        return {'status': 'fx_untracked'}
    since = sig['ts'] * 1000
    try:
        bars = ex.fetch_ohlcv(sym, '15m', since=since, limit=1000)
    except Exception as e:
        print(f"fetch error {sym}: {e}")
        return None
    if not bars:
        return None
    buy = sig['side'] == 'buy'
    sl, tp = sig['sl'], sig['targets'][0]
    filled = False
    for ts, o, h, l, c, v in bars:
        if not filled:
            if l <= sig['entry_high'] and h >= sig['entry_low']:
                filled = True
            elif ts - since > NO_FILL_H * 3600 * 1000:
                return {'status': 'no_fill'}
            else:
                continue
        # filled: check levels (conservative — SL first if both in one bar)
        if sl is not None and (l <= sl if buy else h >= sl):
            return {'status': 'sl_hit', 'resolved_ts': ts // 1000}
        if h >= tp if buy else l <= tp:
            return {'status': 'tp1_hit', 'resolved_ts': ts // 1000}
    if time.time() - sig['ts'] > EXPIRE_D * 86400:
        return {'status': 'expired'}
    return {'status': 'open'} if filled else None


def main():
    if not os.path.exists(SIGNALS):
        print('no signals recorded yet')
        return
    with open(SIGNALS) as f:
        signals = json.load(f)
    changed = False
    for sig in signals:
        if sig.get('status') in ('pending', 'open'):
            r = resolve(sig)
            if r and r['status'] != sig.get('status'):
                sig.update(r)
                changed = True
    if changed:
        with open(SIGNALS, 'w') as f:
            json.dump(signals, f)

    stats = {}
    for sig in signals:
        g = stats.setdefault(sig['group'], {'signals': 0, 'tp1': 0, 'sl': 0,
                                            'no_fill': 0, 'open': 0, 'other': 0})
        g['signals'] += 1
        st = sig.get('status', 'pending')
        if st == 'tp1_hit':
            g['tp1'] += 1
        elif st == 'sl_hit':
            g['sl'] += 1
        elif st == 'no_fill':
            g['no_fill'] += 1
        elif st in ('open', 'pending'):
            g['open'] += 1
        else:
            g['other'] += 1
    for g, d in stats.items():
        done = d['tp1'] + d['sl']
        d['win_rate'] = round(d['tp1'] / done, 3) if done else None
    with open(STATS, 'w') as f:
        json.dump({'updated': int(time.time()), 'groups': stats}, f, indent=1)
    for g, d in sorted(stats.items()):
        wr = f"{d['win_rate']:.0%}" if d['win_rate'] is not None else '—'
        print(f"{g:16} signals={d['signals']:3} tp1={d['tp1']:3} sl={d['sl']:3} "
              f"no_fill={d['no_fill']:3} open={d['open']:3} WR={wr}")


if __name__ == '__main__':
    main()
