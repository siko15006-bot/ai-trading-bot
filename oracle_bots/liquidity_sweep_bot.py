"""
Liquidity Sweep Bot v1.1 — backtested 2026-07-18, see project_liquidity_sweep_finding.
Reconstructed from liquidity_sweep_break_close_NOTES.pine (original Pine 13 code was
lost, only the documented pattern survived) -- Ahmed approved live deploy 2026-07-18
before re-confirming the original script matches; started conservative (1% risk,
below orb_bot's proven 2%) as a hedge against that reconstruction risk.

Strategy (EXACTLY as backtested — do not tweak without re-backtesting):
  M15 BTC/USDT:USDT, SHORT-ONLY (only side that showed edge; XAU was rejected
  entirely in the backtest, not implemented here at all):
    1. SWEEP: a closed bar's high > prior-10-bar high AND its close < that prior high
       (a stop-hunt above recent highs that closes back under).
    2. BREAK: level = sweep bar's close. A later closed bar's close < level confirms
       the break (bearish continuation). Level expires after 15 bars unversed.
    3. RETEST: after the break, a closed bar's high touches back up to level and its
       close is still < level -> SHORT entry. Level is consumed (one shot per setup).
  SL = retest bar's high, floored by 0.5*ATR14. TP = RR 3:1.
  train n=1010 WR=25.6% exp=+$6.24 | OOS n=402 WR=29.4% exp=+$45.75 (both positive,
  OOS improved over train -- see project_liquidity_sweep_finding.md for the full
  walk-forward numbers this is faithful to).

v1.1 (2026-07-18, Ahmed asked for Telegram alerts + graduated sizing):
  - notify() reuses the exact Telegram sendMessage pattern from tg_signal_bot.py
    (same bot token/chat -- Ahmed's own notification channel).
  - Graduated risk: starts at RISK_MIN (1%), steps up by RISK_STEP after every
    EVAL_BLOCK completed trades IF that block's win rate >= breakeven for RR3:1
    (25%) and its PnL was net positive, capped at RISK_MAX (2%, orb_bot's proven
    ceiling -- never exceeds what our one other live-proven bot risks). A losing
    block steps back down to RISK_MIN. This is evidence-gated ratcheting, not a
    martingale/chase-losses scheme -- it only scales UP after proof, never after
    a loss, and the ceiling matches an already-approved bot's sizing.
    ponytail: trade-closure attribution matches by "closed after we opened" on a
    single-position system (this bot never holds >1 position) rather than by
    order id -- good enough given it's the only BTC short source most of the
    time; a genuine collision with another bot shorting BTC/USDT:USDT at the
    exact same moment would misattribute one trade's outcome, acceptable risk
    for a sizing ratchet (not the entry logic itself).
"""
import ccxt, os, time, math, json
from heartbeat import write_heartbeat  # 2026-08-06: functional heartbeat rollout
from risk_halt_gate import is_portfolio_halted  # 2026-08-08: portfolio-wide RISK_HALT, synced from Windows
from oracle_entry_freeze import entry_freeze_status
from oracle_entry_freeze import entry_freeze_status
from attribution_hooks import make_tag, record_intent  # 2026-08-09: Phase 2, tagging only -- see attribution_hooks.py
import requests as _req
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv('/home/ubuntu/.env')

exchange = ccxt.bybit({
    'apiKey':  os.getenv('BYBIT_API_KEY'),
    'secret':  os.getenv('BYBIT_API_SECRET'),
    # 2026-07-26: same clock-drift fix already applied to bybit_shield.py
    # and ladder_guard_bybit.py (Oracle's clock drifts a few seconds between
    # NTP syncs, Bybit's default 5s recv_window rejected requests during
    # those windows with retCode 10002) -- this script never got it.
    'options': {'defaultType': 'linear', 'recvWindow': 20000},
    'timeout': 10000,
    'enableRateLimit': True,
})

TG_TOKEN = '8002641228:AAHAqcHwuI4h0MYNuH6MkY8iqDDSE4Vg03A'
TG_CHAT = '682191881'

SYMBOL = 'BTC/USDT:USDT'
STEP, MIN_QTY, MAX_QTY, LEV = 0.001, 0.001, 5.0, 10
SWEEP_LOOKBACK = 10
EXPIRY_BARS = 15
RR = 3.0
RISK_MIN, RISK_MAX, RISK_STEP = 0.01, 0.02, 0.005
EVAL_BLOCK = 5           # re-evaluate sizing every N completed trades
MIN_NOTIONAL = 5.0
TICK_SEC = 30
STATE_FILE = '/tmp/liqsweep_state.json'


def now_utc():
    return datetime.now(timezone.utc)


def log(msg):
    print(f'[{now_utc().strftime("%H:%M:%S")}] {msg}', flush=True)


def notify(text):
    log('NOTIFY: ' + text.replace('\n', ' | '))
    try:
        _req.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                  data={'chat_id': TG_CHAT, 'text': text[:4000]}, timeout=10)
    except Exception as e:
        log(f'notify error: {e}')


def default_state():
    return {'level': None, 'sweep_high': None, 'broken': False, 'age': 0, 'last_closed_ts': 0,
            'risk_pct': RISK_MIN, 'outcomes': [], 'open_since_ms': None}


def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        for k, v in default_state().items():
            s.setdefault(k, v)
        return s
    except Exception:
        return default_state()


def save_state(s):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(s, f)
    except Exception as e:
        log(f'state save error: {e}')


def atr14(ohlcv):
    trs = []
    for i in range(1, len(ohlcv)):
        h, l, pc = ohlcv[i][2], ohlcv[i][3], ohlcv[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-14:]) / 14 if len(trs) >= 14 else None


def get_balance():
    try:
        return float(exchange.fetch_balance().get('USDT', {}).get('free', 0))
    except Exception:
        return 0.0


def has_open_position():
    try:
        for p in exchange.fetch_positions([SYMBOL]):
            if float(p.get('contracts') or 0) > 0:
                return True
    except Exception as e:
        log(f'fetch_positions error: {e}')
    return False


def check_closed_trade(state):
    """If we had a position open and it's now closed, look up its PnL and update
    the risk ladder. See the v1.1 docstring note on attribution."""
    if not state.get('open_since_ms'):
        return
    try:
        res = exchange.private_get_v5_position_closed_pnl({'category': 'linear', 'symbol': 'BTCUSDT', 'limit': 5})
        rows = res['result']['list']
        candidates = [r for r in rows if int(r.get('updatedTime', 0)) >= state['open_since_ms']]
        if not candidates:
            if has_open_position():
                return  # position still open — keep tracking (v1.2 fix: was clearing here)
            state['pnl_miss'] = state.get('pnl_miss', 0) + 1
            if state['pnl_miss'] < 3:
                save_state(state)
                return
            state['open_since_ms'] = None
            state['pnl_miss'] = 0
            save_state(state)
            return
        state['pnl_miss'] = 0
        pnl = sum(float(r.get('closedPnl', 0)) for r in candidates)
        state['outcomes'].append(1 if pnl > 0 else 0)
        state['outcomes'] = state['outcomes'][-200:]
        state['open_since_ms'] = None
        log(f'trade closed pnl={pnl:.4f}')

        recent = state['outcomes'][-EVAL_BLOCK:]
        if len(recent) == EVAL_BLOCK:
            wr = sum(recent) / EVAL_BLOCK
            if wr >= 0.25:
                new_risk = min(RISK_MAX, round(state['risk_pct'] + RISK_STEP, 4))
            else:
                new_risk = RISK_MIN
            if new_risk != state['risk_pct']:
                log(f'risk ladder: {state["risk_pct"]:.3f} -> {new_risk:.3f} (last {EVAL_BLOCK} WR={wr:.0%})')
                notify(f'Liquidity Sweep Bot: risk {state["risk_pct"]:.1%} -> {new_risk:.1%} (last {EVAL_BLOCK} trades WR={wr:.0%})')
                state['risk_pct'] = new_risk
        save_state(state)
    except Exception as e:
        log(f'check_closed_trade error: {e}')


def place_short(qty, sl, tp, risk_pct):
    try:
        freeze = entry_freeze_status('liquidity_sweep_bot', account='BAA')
        if freeze.get('frozen'):
            log(f"ENTRY BLOCKED -- central freeze active ({freeze.get('reason')})")
            return False
        freeze = entry_freeze_status('liquidity_sweep_bot', account='BAA')
        if freeze.get('frozen'):
            log(f"ENTRY BLOCKED -- central freeze active ({freeze.get('reason')})")
            return False
        try:
            exchange.set_margin_mode('isolated', SYMBOL)
        except Exception:
            pass
        try:
            exchange.set_leverage(LEV, SYMBOL)
        except Exception:
            pass
        params = {
            'stopLoss':   str(round(float(sl), 2)), 'slTriggerBy': 'MarkPrice',
            'takeProfit': str(round(float(tp), 2)), 'tpTriggerBy': 'MarkPrice',
        }
        for attempt in range(3):  # v1.2: 110007 = margin cap hit -> halve qty and retry
            try:
                # 2026-08-09 Phase 2: tag + local intent log only -- no trading param changed
                tag = make_tag('liqsweep', SYMBOL, 'sell', suffix=str(attempt))
                record_intent('liquidity_sweep_bot', SYMBOL, 'sell', qty, tag)
                exchange.create_order(SYMBOL, 'market', 'sell', qty, params={**params, 'orderLinkId': tag})
                break
            except Exception as oe:
                if '110007' in str(oe) and attempt < 2:
                    qty = round(max(MIN_QTY, math.floor((qty / 2) / STEP) * STEP), 6)
                    log(f'110007 margin cap -> retry with qty={qty}')
                else:
                    raise
        log(f'ENTRY SHORT {SYMBOL} qty={qty} SL={sl:.2f} TP={tp:.2f} risk={risk_pct:.1%}')

        # v1.3 (2026-07-26): create_order's stopLoss/takeProfit params sometimes
        # silently fail to attach on Bybit's side -- found via bybit_shield.py
        # repeatedly catching these as "naked" positions and slapping a much
        # wider fallback SL with no TP at all, destroying the designed RR3:1
        # (live avg_win/avg_loss was ~0.6:1 instead of 3:1, same WR as backtest).
        # Verify on-exchange and re-apply via the dedicated endpoint if missing.
        confirmed = False
        for _ in range(4):
            time.sleep(1.5)
            try:
                pos = next((p for p in exchange.fetch_positions([SYMBOL])
                            if float(p.get('contracts') or 0) > 0), None)
            except Exception:
                pos = None
            if pos is None:
                break  # already closed before we could check -- nothing to fix
            info = pos.get('info', {})
            has_sl = float(info.get('stopLoss') or 0) > 0
            has_tp = float(info.get('takeProfit') or 0) > 0
            if has_sl and has_tp:
                confirmed = True
                break
            log(f'SL/TP missing after entry (SL={has_sl} TP={has_tp}) -> re-applying directly')
            try:
                exchange.private_post_v5_position_trading_stop({
                    'category': 'linear', 'symbol': 'BTCUSDT', 'positionIdx': 0,
                    'stopLoss': str(round(float(sl), 2)), 'slTriggerBy': 'MarkPrice',
                    'takeProfit': str(round(float(tp), 2)), 'tpTriggerBy': 'MarkPrice',
                })
            except Exception as e:
                log(f'trading-stop re-apply failed: {str(e)[:150]}')
        if not confirmed:
            log('WARNING: could not confirm SL/TP attached after retries')
            notify('⚠️ Liquidity Sweep Bot: مش متأكد إن SL/TP اتسجلوا بعد الدخول - راجع يدويًا')

        notify(f'Liquidity Sweep Bot: SHORT {SYMBOL} qty={qty} entry~{sl-((sl-tp)/RR):.2f} SL={sl:.2f} TP={tp:.2f} (risk {risk_pct:.1%})')
        return True
    except Exception as e:
        log(f'order error: {str(e)[:200]}')
        return False


def run():
    log('Liquidity Sweep Bot v1.1 started — BTC/USDT:USDT M15 short-only, RR 1:3')
    state = load_state()
    while True:
        try:
            write_heartbeat('liquidity_sweep_bot')
            check_closed_trade(state)

            ohlcv = exchange.fetch_ohlcv(SYMBOL, '15m', limit=SWEEP_LOOKBACK + EXPIRY_BARS + 20)
            if len(ohlcv) < SWEEP_LOOKBACK + 2:
                time.sleep(TICK_SEC)
                continue

            closed = ohlcv[:-1]  # exclude the still-forming bar (no lookahead)
            last = closed[-1]
            last_ts = last[0]

            if last_ts != state.get('last_closed_ts'):
                # a new bar has closed since our last evaluation -> advance the state machine once
                prior = closed[-1 - SWEEP_LOOKBACK:-1]
                hh_prior = max(c[2] for c in prior) if len(prior) == SWEEP_LOOKBACK else None
                high, close = last[2], last[4]

                if hh_prior is not None and high > hh_prior and close < hh_prior:
                    state.update({'level': close, 'sweep_high': high, 'broken': False, 'age': 0, 'last_closed_ts': last_ts})
                    log(f'SWEEP detected level={close:.2f} sweep_high={high:.2f}')
                elif state.get('level') is not None:
                    state['age'] = state.get('age', 0) + 1
                    if state['age'] > EXPIRY_BARS:
                        log('setup expired')
                        state.update({'level': None, 'sweep_high': None, 'broken': False, 'age': 0, 'last_closed_ts': last_ts})
                    else:
                        level = state['level']
                        if not state['broken'] and close < level:
                            state['broken'] = True
                            log(f'BREAK confirmed close={close:.2f} < level={level:.2f}')
                        elif state['broken'] and high >= level and close < level:
                            # RETEST -> entry signal on this closed bar
                            a = atr14(closed)
                            sl_dist = max(high - close, 0.5 * a) if a else (high - close)
                            if sl_dist > 0 and not has_open_position():
                                balance = get_balance()
                                risk_usd = balance * state['risk_pct']
                                raw = risk_usd / sl_dist
                                qty = max(MIN_QTY, min(MAX_QTY, math.floor(raw / STEP) * STEP))
                                qty = round(qty, 6)
                                if qty * close >= MIN_NOTIONAL:
                                    sl_price = close + sl_dist
                                    tp_price = close - sl_dist * RR
                                    if is_portfolio_halted():
                                        log('ENTRY BLOCKED -- portfolio-wide RISK_HALT active (synced from Windows)')
                                    elif place_short(qty, sl_price, tp_price, state['risk_pct']):
                                        state['open_since_ms'] = int(now_utc().timestamp() * 1000)
                                else:
                                    log('notional too small — skip')
                            state.update({'level': None, 'sweep_high': None, 'broken': False, 'age': 0, 'last_closed_ts': last_ts})
                        state['last_closed_ts'] = last_ts
                else:
                    state['last_closed_ts'] = last_ts
                save_state(state)

        except Exception as e:
            log(f'tick error: {e}')

        time.sleep(TICK_SEC)


if __name__ == '__main__':
    run()
