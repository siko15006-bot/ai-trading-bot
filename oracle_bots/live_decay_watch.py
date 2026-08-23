#!/usr/bin/env python3
"""live_decay_watch.py — catches a live-promoted signal group OR the ORB bot
decaying below its proven expectancy, so it can't silently bleed real money
for days again (tv_rating_signals.py ran 07-08 -> 07-12 losing before anyone
noticed).

Three independent checks, all alert-only-once-per-breach (state in
live_decay_alerts.json):
  1. signals_all.json ledger (tg_signal_bot.py / tv_rating_signals.py groups)
     vs the breakeven implied by each signal's own SL/target R:R.
     tv_rating_signals.py additionally gets auto-demoted (EXECUTE=False) since
     it has a single clean kill switch; tg_signal_bot's GROUPS are alert-only,
     smaller probation size (1% risk) makes an immediate auto-edit overkill.
  2. orb_bot.py's live fills via Bybit's closed-pnl endpoint. orb_bot doesn't
     tag its orders, so trades are attributed by symbol-in-SYMBOLS +
     entry (createdTime) falling inside the 13:30-19:30 UTC session window —
     the only time orb_bot ever enters, so this is a reliable enough proxy
     without needing order tagging. Alerts (does not auto-disable — dropping
     a symbol from a multi-symbol live bot needs a look, not a blind edit)
     per the standing rule in project_orb_strategy_verdict.md: "drop a symbol
     if it goes red over a rolling ~60 trades" — checked at n>=20 for an
     earlier heads-up, not waiting for the full 60.
  3. BAA weekly drift (2026-08-16, Ahmed-approved): account-level (all bots
     combined) PF trend for BTC/ETH/XAU closed-pnl on the shared Bybit
     account. Per feedback-shared-position-measurement-policy, swing_pending_
     bybit and tg_signal_bot share the same net position on these symbols, so
     this is deliberately NOT attributed to one bot — it's a system-health
     signal only ("is BAA as a whole degrading"), same bar the policy allows.

Run via cron every few hours. No args.
"""
import json
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import ccxt
from dotenv import load_dotenv

from attribution_hooks import parse_tag, tag_matches_strategy

BOT_DIR = '/home/ubuntu/trading-bot'
LEDGER = f'{BOT_DIR}/signals_all.json'
ALERT_STATE = f'{BOT_DIR}/live_decay_alerts.json'
TV_RATING_SCRIPT = f'{BOT_DIR}/tv_rating_signals.py'
MIN_TRADES = 15
MARGIN = 0.03  # breach must clear breakeven by this much to filter noise

# Groups currently allowed to trade real money. Keep in sync with
# tg_signal_bot.py's GROUPS dict keys + whichever tv_rating symbols have
# EXECUTE=True (checked live below, not hardcoded, since that flag flips).
TG_LIVE_GROUPS = {'WHALES_CIRCLE', 'SPARTA_CRYPTO', 'LEVERAGE200X', 'BINANCE_360'}

# Keep in sync with orb_bot.py's SYMBOLS dict.
ORB_SYMBOLS = ['SOL/USDT:USDT', 'ETH/USDT:USDT', 'XRP/USDT:USDT',
               'ADA/USDT:USDT', 'DOT/USDT:USDT']
ORB_SESSION_START_MIN = 13 * 60 + 25   # 13:30 UTC open, 5min buffer
ORB_SESSION_END_MIN = 19 * 60 + 35     # 19:30 UTC close, 5min buffer
ORB_MIN_TRADES = 20
ORB_LOOKBACK_DAYS = 45

# Keep in sync with trama_trend_bot.py's SYMBOLS dict. Overlaps orb_bot on
# SOL/ADA, so trades falling inside orb's session window are excluded here
# (could be either bot's — conservative undercount beats misattribution).
TRAMA_SYMBOLS = ['SOL/USDT:USDT', 'ADA/USDT:USDT']
TRAMA_MIN_TRADES = 15
TRAMA_LOOKBACK_DAYS = 45

# Keep in sync with swing_pending_bybit.py's SYMBOLS — shared account-wide,
# see check_baa_weekly_drift() docstring for why this stays aggregate-only.
BAA_DRIFT_SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'XAU/USDT:USDT']
BAA_DRIFT_STATE = f'{BOT_DIR}/baa_weekly_drift.json'
BAA_DRIFT_LOOKBACK_DAYS = 90
BAA_DRIFT_MIN_TRADES = 10

TG_TOKEN = "8002641228:AAHAqcHwuI4h0MYNuH6MkY8iqDDSE4Vg03A"
TG_CHAT = "682191881"


def notify(msg):
    import requests
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": msg}, timeout=10,
        )
    except Exception as e:
        print("notify failed:", e)


def r_multiple(sig):
    entry = (sig['entry_low'] + sig['entry_high']) / 2
    sl = sig['sl']
    target = sig['targets'][0]
    risk = abs(entry - sl)
    reward = abs(target - entry)
    return reward / risk if risk else None


def tv_rating_is_live():
    if not os.path.exists(TV_RATING_SCRIPT):
        return False
    src = open(TV_RATING_SCRIPT).read()
    m = re.search(r'^EXECUTE\s*=\s*(True|False)', src, re.M)
    return bool(m and m.group(1) == 'True')


def demote_tv_rating():
    src = open(TV_RATING_SCRIPT).read()
    new_src = re.sub(
        r'^EXECUTE\s*=\s*True.*$',
        "EXECUTE = False      # auto-demoted by live_decay_watch.py — live WR fell below breakeven",
        src, count=1, flags=re.M,
    )
    open(TV_RATING_SCRIPT, 'w').write(new_src)


def evaluate(signals, live_group_names):
    by_group = defaultdict(list)
    for s in signals:
        if s.get('status') in ('tp1_hit', 'sl_hit'):
            by_group[s['group']].append(s)

    breaches = {}
    for group, sigs in by_group.items():
        if group not in live_group_names or len(sigs) < MIN_TRADES:
            continue
        wins = sum(1 for s in sigs if s['status'] == 'tp1_hit')
        wr = wins / len(sigs)
        rs = [r for r in (r_multiple(s) for s in sigs) if r]
        avg_r = sum(rs) / len(rs) if rs else 1.5
        breakeven = 1 / (1 + avg_r)
        if wr < breakeven - MARGIN:
            breaches[group] = {
                'trades': len(sigs), 'wr': round(wr, 3),
                'breakeven': round(breakeven, 3),
            }
    return breaches


def minute_of_day(ms):
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    return dt.hour * 60 + dt.minute


def _bybit_client():
    import ccxt
    from dotenv import load_dotenv
    load_dotenv('/home/ubuntu/.env')
    return ccxt.bybit({
        'apiKey': os.getenv('BYBIT_API_KEY'), 'secret': os.getenv('BYBIT_API_SECRET'),
        'options': {'defaultType': 'linear'}, 'enableRateLimit': True,
    })


def _fetch_closed_pnl_full(ex, market_symbol, since_ms):
    """Bybit v5 closed-pnl HARD ERRORS if startTime/endTime span >7 days
    ("time range ... cannot exceed 7 days"), and silently returns a partial/
    truncated window if you omit endTime on a lookback longer than that
    (found 2026-08-16: a 90-day startTime-only query returned 0 rows for a
    symbol with real trading history, while explicit endTime confirmed the
    7-day hard cap) -- so any lookback > 7 days needs chunking + cursor
    pagination within each chunk, not a single call. This was silently
    under-counting orb_bot/trama_trend_bot's 45-day decay lookback too."""
    all_rows = []
    window_ms = 7 * 24 * 3600 * 1000 - 1
    now_ms = int(time.time() * 1000)
    start = since_ms
    while start < now_ms:
        end = min(start + window_ms, now_ms)
        cursor = None
        for _ in range(20):  # hard cap on pages/chunk, avoid a runaway loop
            params = {'category': 'linear', 'symbol': market_symbol,
                      'startTime': start, 'endTime': end, 'limit': 100}
            if cursor:
                params['cursor'] = cursor
            res = ex.private_get_v5_position_closed_pnl(params)
            rows = res['result']['list']
            all_rows.extend(rows)
            cursor = res['result'].get('nextPageCursor')
            if not cursor or not rows:
                break
        start = end
    return all_rows


def check_orb_decay():
    """Bybit closed-pnl for ORB_SYMBOLS, filtered to entries inside the ORB
    session window (the only time orb_bot ever opens a position), grouped
    per symbol. Returns {symbol: {trades, wins, net_pnl}} for symbols with
    n >= ORB_MIN_TRADES and net_pnl < 0 (the standing "drop it" trigger)."""
    ex = _bybit_client()
    since = int((time.time() - ORB_LOOKBACK_DAYS * 24 * 3600) * 1000)

    breaches = {}
    for symbol in ORB_SYMBOLS:
        market_symbol = symbol.replace('/', '').replace(':USDT', '')
        try:
            rows = _fetch_closed_pnl_full(ex, market_symbol, since)
        except Exception as e:
            print(f'orb decay check {symbol} fetch failed: {e}')
            continue
        session_rows = [r for r in rows
                         if ORB_SESSION_START_MIN <= minute_of_day(r['createdTime']) < ORB_SESSION_END_MIN]
        n = len(session_rows)
        if n < ORB_MIN_TRADES:
            continue
        net = sum(float(r['closedPnl']) for r in session_rows)
        wins = sum(1 for r in session_rows if float(r['closedPnl']) > 0)
        if net < 0:
            breaches[symbol] = {'trades': n, 'wins': wins, 'net_pnl': round(net, 2)}
    return breaches


def check_trama_decay():
    """Same idea as check_orb_decay, but for trama_trend_bot's SOL/ADA trades
    — excludes anything inside orb_bot's session window since that overlap
    can't be cleanly attributed without order tagging."""
    ex = _bybit_client()
    since = int((time.time() - TRAMA_LOOKBACK_DAYS * 24 * 3600) * 1000)

    breaches = {}
    for symbol in TRAMA_SYMBOLS:
        market_symbol = symbol.replace('/', '').replace(':USDT', '')
        try:
            rows = _fetch_closed_pnl_full(ex, market_symbol, since)
        except Exception as e:
            print(f'trama decay check {symbol} fetch failed: {e}')
            continue
        outside_orb = [r for r in rows if not (
            ORB_SESSION_START_MIN <= minute_of_day(r['createdTime']) < ORB_SESSION_END_MIN)]
        n = len(outside_orb)
        if n < TRAMA_MIN_TRADES:
            continue
        net = sum(float(r['closedPnl']) for r in outside_orb)
        wins = sum(1 for r in outside_orb if float(r['closedPnl']) > 0)
        if net < 0:
            breaches[symbol] = {'trades': n, 'wins': wins, 'net_pnl': round(net, 2)}
    return breaches


def _iso_week(ts_ms):
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    y, w, _ = dt.isocalendar()
    return f'{y}-W{w:02d}'


def _week_stats(rows):
    """rows: list of Bybit closed-pnl records -> {trades, wins, net_pnl, pf, expectancy}."""
    n = len(rows)
    gp = sum(float(r['closedPnl']) for r in rows if float(r['closedPnl']) > 0)
    gl = sum(-float(r['closedPnl']) for r in rows if float(r['closedPnl']) < 0)
    wins = sum(1 for r in rows if float(r['closedPnl']) > 0)
    net = gp - gl
    pf = (gp / gl) if gl > 0 else (None if gp == 0 else float('inf'))
    return {
        'trades': n, 'wins': wins, 'net_pnl': round(net, 2),
        'pf': (round(pf, 3) if pf not in (None, float('inf')) else pf),
        'expectancy': round(net / n, 4) if n else 0.0,
    }


def check_baa_weekly_drift():
    """Account-level (ALL bots combined — swing_pending_bybit + tg_signal_bot
    both touch these symbols) weekly PF trend for BAA's shared BTC/ETH/XAU
    closed-pnl. Deliberately NOT attributed to one bot: per
    feedback-shared-position-measurement-policy, Bybit's closed-pnl reflects
    the whole net position per symbol, and a prior attempt to isolate
    swing_pending_bybit's own number by order-tag filtering produced a
    provably wrong result (+$13.69 vs the real -$9.47, see
    project-swing-pending-bybit-audit). This only answers "is BAA as a whole
    degrading", which the policy explicitly allows (rule 3: aggregate for
    system health, never for judging one bot).

    Flags when the latest COMPLETE ISO week's PF drops below 1.0 while the
    trailing 3-week average was >= 1.0 — a real trend, not one noisy week."""
    ex = _bybit_client()
    since = int((time.time() - BAA_DRIFT_LOOKBACK_DAYS * 24 * 3600) * 1000)

    rows_by_week = defaultdict(list)
    for symbol in BAA_DRIFT_SYMBOLS:
        market_symbol = symbol.replace('/', '').replace(':USDT', '')
        try:
            rows = _fetch_closed_pnl_full(ex, market_symbol, since)
        except Exception as e:
            print(f'baa drift check {symbol} fetch failed: {e}')
            continue
        for r in rows:
            rows_by_week[_iso_week(int(r['createdTime']))].append(r)

    weeks = {wk: _week_stats(rows) for wk, rows in rows_by_week.items()}
    json.dump(weeks, open(BAA_DRIFT_STATE, 'w'), indent=2, sort_keys=True)

    current_week = _iso_week(int(time.time() * 1000))
    complete_weeks = sorted(wk for wk in weeks if wk != current_week)
    if len(complete_weeks) < 4:
        return None  # not enough history yet to judge a trend
    latest = complete_weeks[-1]
    trailing = complete_weeks[-4:-1]
    if weeks[latest]['trades'] < BAA_DRIFT_MIN_TRADES:
        return None
    latest_pf = weeks[latest]['pf']
    trailing_pfs = [weeks[w]['pf'] for w in trailing
                     if weeks[w]['trades'] >= BAA_DRIFT_MIN_TRADES
                     and isinstance(weeks[w]['pf'], (int, float))]
    if not isinstance(latest_pf, (int, float)) or not trailing_pfs:
        return None
    trailing_avg = sum(trailing_pfs) / len(trailing_pfs)
    if latest_pf < 1.0 and trailing_avg >= 1.0:
        return {'week': latest, 'trailing_avg_pf': round(trailing_avg, 3), **weeks[latest]}
    return None



@dataclass(frozen=True)
class TaggedPnLSummary:
    prefix: str
    classification: str
    symbol: str | None
    matched_trades: int
    confirmed_pnl: float | None
    estimated_pnl: float | None
    details: dict[str, Any]


DEFAULT_SYMBOLS = ('BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'XAU/USDT:USDT')


def _exchange():
    load_dotenv('/home/ubuntu/.env')
    return ccxt.bybit({
        'apiKey': os.getenv('BYBIT_API_KEY'),
        'secret': os.getenv('BYBIT_API_SECRET'),
        'enableRateLimit': True,
        'options': {'defaultType': 'swap', 'recvWindow': 20000},
    })


def _now():
    return datetime.now(timezone.utc)


def _trade_prefix(trade):
    info = trade.get('info') or {}
    for key in ('orderLinkId', 'clientOrderId'):
        value = trade.get(key) or info.get(key)
        if value:
            return str(value)
    return None


def _trade_pnl(trade):
    for key in ('realizedPnl', 'closedPnl', 'execPnl'):
        value = trade.get(key)
        if value is None:
            value = (trade.get('info') or {}).get(key)
        if value is not None:
            try:
                return float(value)
            except Exception:
                continue
    return None


def _signed_cashflow(trades):
    total = 0.0
    for trade in trades:
        side = str(trade.get('side') or '').lower()
        price = float(trade.get('price') or 0.0)
        amount = float(trade.get('amount') or 0.0)
        if side == 'sell':
            total += price * amount
        elif side == 'buy':
            total -= price * amount
        fee = trade.get('fee') or {}
        if isinstance(fee, dict) and fee.get('cost') is not None:
            total -= abs(float(fee['cost']))
    return float(total)


def check_tagged_bot_pnl(prefixes, symbols=DEFAULT_SYMBOLS, lookback_days=14):
    ex = _exchange()
    since = int((_now() - timedelta(days=lookback_days)).timestamp() * 1000)
    prefix_list = [p for p in prefixes if p]
    out = []

    for prefix in prefix_list:
        matched_trades = []
        matched_confirmed = 0.0
        confirmed = False
        symbols_seen = set()
        approx_by_symbol = {}
        details = {'source': 'fetch_my_trades+fetch_closed_orders', 'lookback_days': lookback_days}

        for symbol in symbols:
            try:
                trades = ex.fetch_my_trades(symbol, since=since, limit=200) or []
            except Exception as exc:
                details.setdefault('errors', []).append(f'{symbol}:trades:{type(exc).__name__}')
                trades = []
            try:
                closed = ex.fetch_closed_orders(symbol, since=since, limit=200) or []
            except Exception as exc:
                details.setdefault('errors', []).append(f'{symbol}:closed:{type(exc).__name__}')
                closed = []

            current_position_flat = True
            try:
                positions = ex.fetch_positions([symbol]) or []
                current_position_flat = all(float(p.get('contracts', 0) or 0) == 0 for p in positions)
            except Exception:
                current_position_flat = False

            symbol_trades = []
            for row in list(trades) + list(closed):
                tag = _trade_prefix(row)
                if not tag_matches_strategy(tag, prefix) and not (tag and str(tag).startswith(prefix)):
                    continue
                symbols_seen.add(symbol)
                symbol_trades.append(row)
                pnl = _trade_pnl(row)
                if pnl is not None:
                    matched_confirmed += pnl
                    confirmed = True

            if symbol_trades and not confirmed:
                approx_by_symbol[symbol] = _signed_cashflow(symbol_trades)
                if current_position_flat:
                    details.setdefault('flat_symbols', []).append(symbol)
                else:
                    details.setdefault('open_symbols', []).append(symbol)
            matched_trades.extend(symbol_trades)

        if confirmed:
            classification = 'CONFIRMED/TAGGED DATA'
            confirmed_pnl = round(matched_confirmed, 8)
            estimated_pnl = confirmed_pnl
        elif matched_trades and details.get('flat_symbols'):
            classification = 'HIGH-CONFIDENCE ESTIMATE'
            confirmed_pnl = None
            estimated_pnl = round(sum(approx_by_symbol.values()), 8) if approx_by_symbol else None
        elif matched_trades:
            classification = 'APPROXIMATE'
            confirmed_pnl = None
            estimated_pnl = round(sum(approx_by_symbol.values()), 8) if approx_by_symbol else None
        else:
            classification = 'UNKNOWN'
            confirmed_pnl = None
            estimated_pnl = None

        out.append(TaggedPnLSummary(
            prefix=prefix,
            classification=classification,
            symbol=','.join(sorted(symbols_seen)) if symbols_seen else None,
            matched_trades=len(matched_trades),
            confirmed_pnl=confirmed_pnl,
            estimated_pnl=estimated_pnl,
            details=details,
        ))

    return out


def source_scan_ok():
    text = Path(__file__).read_text(encoding='utf-8').lower()
    forbidden = ('create_' + 'order', 'cancel_' + 'order', 'modify_' + 'order',
                 'close_' + 'order', 'order_' + 'send', 'with' + 'draw', 'trans' + 'fer')
    hits = [term for term in forbidden if term in text]
    return (len(hits) == 0, hits)


def main():
    signals = json.load(open(LEDGER))
    live_groups = set(TG_LIVE_GROUPS)
    if tv_rating_is_live():
        live_groups.add('TV_RATING_15M')

    breaches = evaluate(signals, live_groups)
    orb_breaches = check_orb_decay()
    trama_breaches = check_trama_decay()
    baa_drift = check_baa_weekly_drift()

    prev = {}
    if os.path.exists(ALERT_STATE):
        prev = json.load(open(ALERT_STATE))
    prev_orb = prev.get('_orb', {})
    prev_trama = prev.get('_trama', {})
    prev_baa_week = prev.get('_baa_drift_week')

    for group, info in breaches.items():
        if group in prev:
            continue  # already alerted, don't spam every cron run
        notify(
            f"LIVE DECAY: {group} live WR {info['wr']:.0%} is below its "
            f"breakeven {info['breakeven']:.0%} over {info['trades']} real "
            f"trades. " + (
                "Auto-demoted to paper (EXECUTE=False)."
                if group == 'TV_RATING_15M' else
                "Not auto-demoted (tg_signal group) — needs a manual look."
            )
        )
        if group == 'TV_RATING_15M':
            demote_tv_rating()

    for symbol, info in orb_breaches.items():
        if symbol in prev_orb:
            continue
        notify(
            f"ORB DECAY: {symbol} is net negative (${info['net_pnl']}) over "
            f"{info['trades']} live session trades ({info['wins']} wins). "
            f"Per the standing rule, consider dropping it from orb_bot.py's "
            f"SYMBOLS dict. Not auto-removed — needs a look."
        )

    for symbol, info in trama_breaches.items():
        if symbol in prev_trama:
            continue
        notify(
            f"TRAMA DECAY: {symbol} is net negative (${info['net_pnl']}) over "
            f"{info['trades']} live trades ({info['wins']} wins). "
            f"trama_trend_bot is new (deployed 2026-07-12) — worth a look, "
            f"not auto-removed."
        )

    if baa_drift and baa_drift['week'] != prev_baa_week:
        notify(
            f"BAA WEEKLY DRIFT: {baa_drift['week']} PF {baa_drift['pf']} vs "
            f"trailing-3wk avg {baa_drift['trailing_avg_pf']} (net "
            f"${baa_drift['net_pnl']} over {baa_drift['trades']} trades). "
            f"Account-level BTC+ETH+XAU on BAA, ALL bots combined — not "
            f"attributable to one bot (shared-position policy). Worth a look "
            f"if it repeats next week."
        )

    breaches['_orb'] = orb_breaches
    breaches['_trama'] = trama_breaches
    breaches['_baa_drift_week'] = baa_drift['week'] if baa_drift else prev_baa_week
    json.dump(breaches, open(ALERT_STATE, 'w'), indent=2)
    print(f"checked {len(live_groups)} live groups ({len(breaches) - 3} in breach) + "
          f"{len(ORB_SYMBOLS)} orb symbols ({len(orb_breaches)} in breach) + "
          f"{len(TRAMA_SYMBOLS)} trama symbols ({len(trama_breaches)} in breach) + "
          f"BAA weekly drift ({'BREACH ' + baa_drift['week'] if baa_drift else 'ok'})")


def _selftest():
    """ponytail: smallest check that fails if the math breaks."""
    sig_win = {'entry_low': 100, 'entry_high': 100, 'sl': 99, 'targets': [101.5]}
    assert abs(r_multiple(sig_win) - 1.5) < 1e-9
    signals = (
        [{'group': 'X', 'status': 'sl_hit', **sig_win} for _ in range(12)]
        + [{'group': 'X', 'status': 'tp1_hit', **sig_win} for _ in range(3)]
    )
    b = evaluate(signals, {'X'})
    assert 'X' in b, "should flag 20% WR against a 40% breakeven (RR1.5)"

    rows = ([{'closedPnl': '10'}] * 3) + ([{'closedPnl': '-5'}] * 2)
    stats = _week_stats(rows)
    assert stats['trades'] == 5 and stats['wins'] == 3
    assert abs(stats['net_pnl'] - 20.0) < 1e-9
    assert abs(stats['pf'] - 3.0) < 1e-9  # 30 gross profit / 10 gross loss
    assert abs(stats['expectancy'] - 4.0) < 1e-9
    print("selftest OK")


if __name__ == '__main__':
    import sys
    if '--test' in sys.argv:
        _selftest()
    else:
        main()
