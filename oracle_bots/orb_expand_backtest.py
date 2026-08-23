"""
Extends the validated ORB pullback methodology (orb_bot.py / orb_risk_backtest.py,
identical entry/exit/SL/TP logic, untouched) to more liquid Bybit pairs, to see if
the edge that worked on SOL/ETH generalizes. Reuses fetch_all/simulate_symbol/
run_equity from orb_risk_backtest.py verbatim via import - no logic duplicated.
ponytail: single flat script, no framework, run once and read the printout.
"""
import sys
sys.path.insert(0, '/home/ubuntu/trading-bot')
from orb_risk_backtest import fetch_all, simulate_symbol, run_equity, DAYS

CANDIDATES = [
    'BNB/USDT:USDT', 'XRP/USDT:USDT', 'DOGE/USDT:USDT', 'ADA/USDT:USDT',
    'LINK/USDT:USDT', 'AVAX/USDT:USDT', 'LTC/USDT:USDT', 'ARB/USDT:USDT',
    'DOT/USDT:USDT', 'TRX/USDT:USDT',
]

if __name__ == '__main__':
    print(f'Fetching {DAYS}d of 3m bars for {len(CANDIDATES)} candidates...')
    results = []
    for sym in CANDIDATES:
        try:
            bars = fetch_all(sym, DAYS)
        except Exception as e:
            print(f'{sym}: FETCH FAILED {e}')
            continue
        if len(bars) < 1000:
            print(f'{sym}: too little data ({len(bars)} bars), skip')
            continue
        trades = simulate_symbol(bars, {}, None)
        if len(trades) < 20:
            print(f'{sym}: only {len(trades)} trades, too thin, skip')
            continue
        res = run_equity({sym: trades}, risk_pct=0.02, start_balance=100)
        results.append((sym, res))
        print(f"{sym:22s} n={res['n_trades']:>3} WR={res['wr']:5.1f}% "
              f"total={res['total_return_pct']:7.1f}% maxDD={res['max_dd_pct']:5.1f}% "
              f"half1={res['first_half_pct']:7.1f}% half2={res['second_half_pct']:7.1f}%")

    print('\n--- verdict (needs: positive BOTH halves, WR>=40%, n>=30) ---')
    for sym, res in results:
        both_halves_positive = res['first_half_pct'] > 0 and res['second_half_pct'] > 0
        passes = both_halves_positive and res['wr'] >= 40 and res['n_trades'] >= 30
        print(f"{sym:22s} {'PASS' if passes else 'reject'}")
