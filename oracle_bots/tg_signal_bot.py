"""
tg_signal_bot.py — Telegram signal group monitor + Bybit executor (Oracle server)
Listens (as Ahmed's account via Telethon) to configured signal groups, parses
crypto signals, executes on Bybit with mandatory protections, and tracks
per-group realized performance so bad groups get dropped on evidence.

Protection rules (non-negotiable):
  - No parsed STOP-LOSS  -> no trade, notify only.
  - Risk 2% of balance per trade (sized by SL distance), leverage 3x isolated.
  - TP = Target 2 (house rule), or Target 1 if only one given.
  - Only enter if current price within entry zone (±0.5%) — no chasing.
  - Max 3 open signal-trades per group (raised from 1 on 2026-07-14, Ahmed's
    explicit call — high-WR groups like BINANCE_360 were losing real expectancy
    to an overly strict cap), skip symbol if any position already open.
  - Every action (trade or skip) -> Telegram notification to Ahmed.

Run test: python3 tg_signal_bot.py --test   (parses a real sample, asserts)
"""
import ccxt, os, re, json, math, time, asyncio
from heartbeat import write_heartbeat  # 2026-08-06: functional heartbeat rollout
from risk_halt_gate import is_portfolio_halted  # 2026-08-08: portfolio-wide RISK_HALT, synced from Windows
from attribution_hooks import make_tag, record_intent  # 2026-08-09: Phase 2, tagging only -- see attribution_hooks.py
from oracle_entry_freeze import entry_freeze_status  # 2026-08-22: central BAA freeze
import requests as _req
from datetime import datetime, timezone
from dotenv import load_dotenv
from telethon import TelegramClient, events

load_dotenv('/home/ubuntu/.env')

GROUPS = {
    -1002097370390: 'SPARTA_CRYPTO',
    -1001553551852: 'BINANCE_360',       # promoted 2026-07-05 by Ahmed (2 observed wins); probation 1% risk
    # Gold groups promoted to live BAA execution 2026-07-15 (Ahmed: "ذهب التليجرام
    # على BAA فعله") — mirrors the fx_signal_exec live list on EA/BA. Direction
    # from the group, OUR risk numbers (gold_our_risk flag in execute()).
    -1002110392082: 'XAUUSD_ANALYSIS',
    -1001759495587: 'NEXA_TRADE',
    -1003021391947: 'GOLD98_SURE',
    -1003597716166: 'FOREX_GOLD_SIGNALS',
    # Promoted 2026-07-18 per standing evidence policy (bullet 6, project-tg-signal-bot):
    # WR>=60% on >=15 evaluated signals -> auto-promote, no new confirmation needed.
    # GOLD98_PERCENT hit 71.4% WR on 17 signals in group_stats.json.
    -1001920682501: 'GOLD98_PERCENT',
    # Promoted 2026-07-18, same standing policy — group_stats.json this cycle:
    -1001252363647: 'VIP_RESULTS_B360',  # 83.3% WR on 74 signals, crypto — probation 1% risk like BINANCE_360
    -1002494978108: 'GOLD_GBP_KILLER',   # 76.9% WR on 15 signals, pure XAUUSD -> gold_our_risk path
    # Promoted 2026-07-20, same standing policy — group_stats.json this cycle:
    -1001627669928: 'FOREX_MASTER_TREDING',  # 82.4% WR on 17 evaluated signals, pure XAUUSD -> gold_our_risk path
}
# Demoted back to record-only 2026-07-15 (Ahmed: "شوف الخساير ووقفها"):
#   WHALES_CIRCLE  — eval WR 50% (11 signals), realized -2.21 over 6 trades/7d
#   LEVERAGE200X   — eval WR 0% (3 signals), never earned promotion evidence
GOLD_ALLOWED_GROUPS = {'NEXA_TRADE', 'GOLD98_SURE', 'GOLD98_PERCENT', 'GOLD_GBP_KILLER'}
# XAUUSD_ANALYSIS + FOREX_GOLD_SIGNALS + FOREX_MASTER_TREDING removed 2026-08-16
# (Ahmed-approved, evidence-gated demotion): fresh signal_eval.log data shows
# all three flipped/stayed net-negative since promotion (exp_R -0.11/n22,
# -0.13/n27, -0.32/n18) -- same symmetric evidence policy used to promote
# them, applied in reverse. Still recognized in GROUPS (parsing continues),
# just excluded here so no new real gold order gets placed for any of them.

# Groups whose signals are forwarded to Ahmed but NOT traded (e.g. forex — no Bybit market)
NOTIFY_ONLY_GROUPS = {
    -1001175463265: 'FOREX_GDP',
    -1002253964840: 'SMC_TRADER',
    -1001263225860: 'LEARN2TRADE',
}

RISK_PCT_PROBATION = 0.01   # unproven group: 1% risk until it earns more
RISK_PCT_PROVEN    = 0.02   # after ~20 trades with positive realized PnL
MAX_OPEN_PER_GROUP = 3      # raised from 1 2026-07-14 (Ahmed) — cap concentration without giving up proven expectancy
MIN_RR = 2.0                # reject signals that do not pay at least 2R
# Paper-tracked only (found by account scan 2026-07-06): parse + record for
# group evaluation, NO execution, NO notify. Promote only on group_stats evidence.
RECORD_ONLY_GROUPS = {
    # Tier-A Discovery Engine access check (2026-08-08, Ahmed-approved) --
    # 5 public free-tier channels confirmed joinable via Telethon, IDs
    # resolved for real (get_entity), record-only, zero execution, same
    # promotion policy as every other group here.
    -1001005437351: 'XAUHQ',
    -1001412082646: 'WOLVES_TRADING',
    -1001785197109: 'ANABELSIGNALS',
    -1001359054597: 'DEGRAM_FOREX_SIGNALS',
    -1001215049720: 'UNITEDSIGNALS',
    -1001727857237: 'CRYPTO_MUSK',
    -1002193292133: 'BINANCE_FUT_66',
    -1001594522150: 'WHALES_PUMPS',
    -1002046317788: 'AYAN_FOREX',
    -1001700314832: 'WHALES_CIRCLE',     # demoted 2026-07-15 — 50% WR + realized loss
    -1003520136927: 'LEVERAGE200X',      # demoted 2026-07-15 — 0% WR, no evidence
    -1002326322546: 'JOANTYP_7',         # Joan USA Gold Fx, Ahmed's tip 2026-07-07
    # added 2026-07-08 from account rescan (Ahmed: "جروبات جديده عامله شغل عالي") —
    # highest parse-hit-rate new groups; the paper ledger decides who earns promotion
    -1003820016064: 'GOLD_ANALYSIS_SIG',  # 10/30 msgs parsed — highest new hit rate
    -1002720327480: 'XAU_ROYAL_KING',     # 6/30
    -1001468571729: 'NAS100_US30',        # 6/30 (indices — paper only, no Bybit market)
    -1003638913183: 'RAJU_GOLD',          # 5/30
    # added 2026-07-09 from full account rescan (Ahmed: عندي جروبات كثير فيها
    # اشارات فوركس/ذهب ناجحين بس ما شفتهاش) — kept only groups with hit-rate
    # >=4/~45 msgs sampled, all gold/XAUUSD-flavored per parse_fx_gold_signal;
    # paper-track only, promote on group_stats evidence like everyone else.
    -1002324770442: 'TRADE_WITH_ROMEE',      # 7/49
    -1003210478272: 'GOLD_SNIPER',           # 7/48
    -1003307897378: 'XAUUSD_FOREX_TRADING',  # 7/50
    -1001425255914: 'FOREX_XAUUSD_BINARY',   # 6/50
    -1001566319279: 'GOLDINFINITY',          # 6/46
    -1001975523010: 'ELIZ_FX_ACADEMY',       # 6/49
    -1002451714298: 'GOLD_SCALPING_PRO',     # 6/45
    -1002533196532: 'ROYAL_PIPS_CLUB',       # 6/50
    -1002226409599: 'XAUUSD_SIGNAL_FINDER',  # 5/49
    -1001476143548: 'FX_RIVER_ACADEMY',      # 4/50
    -1001768818138: 'GOLD_SCALPER',          # 4/49
    -1002216665319: 'PROFESSIONAL_RISK_CONTROL',  # 4/50
    -1003811607244: 'XAUUSD_GOLD_TRADING',   # 4/42
    # added 2026-07-13 from account rescan (Ahmed: جروبات جديده لسه ما اتضافتش) —
    # paper-track only, promote on group_stats evidence like everyone else.
    -1003674772206: 'XAUUSD_GOLD_MARKET',     # 4/36
    -1003710550706: 'ROYAL_GOLD_SIGNALS',     # 3/48
    -1003995972761: 'XAUUSD_SIGNALS',         # 3/37
    -1001443366294: 'RAHMAN_PRO_FX',          # 2/49
    -1001580107051: 'GPT_PRO_TRADER',         # 2/41
    -1003864644782: 'PARWEEZ_TRDER',          # 2/43
    -1001932070473: 'FLASH_GOLD',             # 1/45
    -1002951939930: 'QAHER_ELGOLD',           # 1/47
    -1003900580386: 'PIPS_HUNTER',            # 1/42
    # added 2026-07-16 from account rescan (Ahmed: "جروبات كتير جديده ضيفها
    # عشان تعرف اشاراتها وتقييمها وشوف هتدخل معانا ولا لأ") — paper-track only,
    # promote on group_stats evidence like everyone else. 1-hit-only candidates
    # (Trade With Malik Hussain, GOLD TRADER) excluded, same bar as FLASH before.
    -1003138894647: 'MAJD_FX',                # 23/50 fx/gold
    -1001754095061: 'CRYPTO_RADAR',           # 20/50 crypto
    -1001703588804: 'AFRA_ALQASIMI_FX_GOLD',  # 8/48 fx/gold
    -1001833946395: 'SMART_FX_TRADER',        # 7/49 fx/gold
    -1001511003843: 'GOLD_VIP_SIGNALS',       # 6/47 fx/gold
    -1002067177406: 'MAGIC_TRADER_FX',        # 4/43 fx/gold
    -1002164876109: 'SMC_VIP',                # 4/48 fx/gold
    -1001654705239: 'WILLIAM_FX_ACADEMY',     # 3/48 fx/gold
    -1002745275908: 'MALIK_ALDAHAB',          # 2/44 fx/gold (ملك الذهب)
    -1004397421755: 'FOREX_GOLD_PRIME',       # 2/44 fx/gold
    # added 2026-07-16 evening at Ahmed's explicit request ("عايزك تضمها") —
    # these two were below the usual 1-hit exclusion bar; his call overrides,
    # record-only costs nothing and the ledger will judge them like everyone.
    -1001591904294: 'MALIK_HUSSAIN',          # 1/31 fx/gold
    -1003813809289: 'GOLD_TRADER_CH',         # 1/30 fx/gold
    # VIP_RESULTS_B360 and GOLD_GBP_KILLER promoted out of here 2026-07-18 (see GROUPS above).
    # FOREX_MASTER_TREDING promoted out of here 2026-07-20 (see GROUPS above).
}


PROVEN_GROUPS      = {'BINANCE_360'}  # promoted 2026-07-08: 7 signals, 80% win rate (4 tp1, 1 sl, 1 no_fill, 1 open) — 2x risk
LEVERAGE   = 3
EXCHANGE_LEVERAGE = 10  # 2026-07-15: real exchange margin setting, decoupled from LEVERAGE above (that stays 3x as the gold sizing-cap multiplier only)
ENTRY_TOL  = 0.005          # 0.5% tolerance around entry zone
DAILY_HALT_PCT = 0.05       # stop signal-trading for the UTC day at -5% equity
TRADES_LOG = '/home/ubuntu/trading-bot/signal_trades.json'
SIGNALS_ALL = '/home/ubuntu/trading-bot/signals_all.json'  # paper log of EVERY parsed signal for group evaluation
DAY_FILE   = '/tmp/tg_signal_day.json'
TG_TOKEN   = '8002641228:AAHAqcHwuI4h0MYNuH6MkY8iqDDSE4Vg03A'
TG_CHAT    = '682191881'
MIN_NOTIONAL = 5.0

ex = ccxt.bybit({
    'apiKey': os.getenv('BYBIT_API_KEY'),
    'secret': os.getenv('BYBIT_API_SECRET'),
    'options': {'defaultType': 'linear'},
    'enableRateLimit': True,
})


def log(msg):
    print(f'[{datetime.now(timezone.utc).strftime("%H:%M:%S")}] {msg}', flush=True)


def notify(text):
    log('NOTIFY: ' + text.replace('\n', ' | '))
    try:
        _req.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                  data={'chat_id': TG_CHAT, 'text': text[:4000]}, timeout=10)
    except Exception as e:
        log(f'notify error: {e}')


NUM = r'([0-9]*\.?[0-9]+)'

def parse_signal(text):
    """Extract symbol/side/entry-zone/targets/SL from a signal message.
    Returns dict or None if it's not a signal (results posts, ads, etc.)."""
    if not text:
        return None
    t = text.upper()

    m_sym = re.search(r'([A-Z0-9]{2,15})\s*/\s*USDT', t)
    if not m_sym:
        return None
    side = None
    if re.search(r'#?\bLONG\b', t):
        side = 'buy'
    elif re.search(r'#?\bSHORT\b', t):
        side = 'sell'

    m_entry = re.search(r'ENTRY(?:\s*(?:ZONE|PRICE))?\s*[:\-]*\s*' + NUM + r'(?:\s*(?:[-–~]|TO)\s*' + NUM + r')?', t)
    m_sl = re.search(r'(?:STOP(?:[\s\-]?LOSS)?|\bSL\b)\s*[:\-]*\s*' + NUM, t)
    targets = [float(x) for x in re.findall(r'^\s*\d\s*[:\-]\s*' + NUM, t, re.MULTILINE)]
    if not targets:
        m_tg = re.search(r'TARGET[S]?\s*[:\-]*\s*' + NUM, t)
        if m_tg:
            targets = [float(m_tg.group(1))]
    if not targets:
        # 'Take Profit: X' repeated lines (BINANCE_360 EPIC format)
        targets = [float(x) for x in re.findall(r'TAKE\s*PROFITS?\s*[:\-]*\s*' + NUM, t)]
    if not targets and 'TAKE PROFIT' in t:
        # bare numbers on their own lines after a 'Take Profits :' header (MTC format)
        block = re.split(r'STOP', t.split('TAKE PROFIT', 1)[1])[0]
        targets = [float(x) for x in re.findall(r'^\s*' + NUM + r'\s*$', block, re.MULTILINE)]

    # Arabic format (e.g. LEVERAGE200X): سعر الدخول 0.145 - 0.137 / الأهداف list / ستوب 0.132
    if not (m_entry and targets):
        orig = text  # Arabic keywords survive .upper() unchanged, but match on original too
        m_entry_ar = re.search(r'(?:سعر\s*الدخول|الدخول)\s*[:\-]?\s*' + NUM + r'(?:\s*[-–/\\]\s*' + NUM + r')?', orig)
        m_sl_ar = re.search(r'(?:ستوب|وقف)[^0-9]*' + NUM, orig)
        if m_entry_ar and 'هداف' in orig:
            after_targets = orig.split('هداف', 1)[1]
            before_stop = re.split(r'ستوب|وقف', after_targets)[0]
            targets_ar = [float(x) for x in re.findall(r'^\s*' + NUM + r'\s*✅?\s*$', before_stop, re.MULTILINE)]
            if targets_ar:
                m_entry = m_entry_ar
                targets = targets_ar
                if m_sl_ar and not m_sl:
                    m_sl = m_sl_ar
                if side is None:
                    side = 'buy'   # Arabic spot/futures groups post long-only unless شورت stated
        if re.search(r'شورت', orig):
            side = 'sell'

    if not (m_entry and targets) or side is None:
        return None

    e1 = float(m_entry.group(1))
    e2 = float(m_entry.group(2)) if m_entry.group(2) else e1
    return {
        'symbol': m_sym.group(1) + '/USDT:USDT',
        'side': side,
        'entry_low': min(e1, e2),
        'entry_high': max(e1, e2),
        'targets': targets,
        'sl': float(m_sl.group(1)) if m_sl else None,
    }


def record_trade(group, sig, qty, price):
    try:
        data = []
        if os.path.exists(TRADES_LOG):
            with open(TRADES_LOG) as f:
                data = json.load(f)
        data.append({'ts': int(time.time()), 'group': group, 'symbol': sig['symbol'],
                     'side': sig['side'], 'entry': price, 'qty': qty,
                     'sl': sig['sl'], 'tp': sig['targets'][1] if len(sig['targets']) > 1 else sig['targets'][0]})
        with open(TRADES_LOG, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        log(f'record error: {e}')




def record_signal(group, sig, message_id=None, late_signal='UNKNOWN'):
    """Paper-log every parsed signal (executed or not) for per-group
    evaluation. message_id/late_signal added 2026-08-08 (Qualification
    Safety layer, Ahmed-approved): message_id enables mutation tracking
    (see edit_handler/delete_handler below) and future dedup;
    late_signal flags whether the entry zone was already stale vs the
    real market price at the moment we received the message."""
    try:
        data = []
        if os.path.exists(SIGNALS_ALL):
            with open(SIGNALS_ALL) as f:
                data = json.load(f)
        data.append({'ts': int(time.time()), 'group': group, 'symbol': sig['symbol'],
                     'side': sig['side'], 'entry_low': sig['entry_low'], 'entry_high': sig['entry_high'],
                     'sl': sig['sl'], 'targets': sig['targets'], 'status': 'pending',
                     'message_id': message_id, 'late_signal': late_signal})
        with open(SIGNALS_ALL, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        log(f'record_signal error: {e}')


def _signal_rr(price, sl, tp):
    risk = abs(price - sl)
    reward = abs(tp - price)
    return (reward / risk) if risk > 0 else 0.0


SIGNAL_MUTATIONS = '/home/ubuntu/trading-bot/signal_mutations.json'


def record_mutation(group, message_id, kind, new_text=None):
    """Append-only mutation log -- 2026-08-08 Qualification Safety
    layer. Never overwrites signals_all.json; a mutated signal's
    ORIGINAL first-seen values stay exactly as first recorded there --
    this is a separate flag file the promotion gate consults
    separately, so a source that edits/deletes signals after posting
    is visible in the audit trail rather than silently blended in."""
    try:
        data = []
        if os.path.exists(SIGNAL_MUTATIONS):
            with open(SIGNAL_MUTATIONS) as f:
                data = json.load(f)
        data.append({'ts': int(time.time()), 'group': group, 'message_id': message_id,
                     'kind': kind, 'new_text': (new_text[:300] if new_text else None)})
        with open(SIGNAL_MUTATIONS, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        log(f'record_mutation error: {e}')


def _check_late_signal(sig, late_threshold_pct=0.15):
    """Compares the signal's own stated entry zone against the CURRENT
    market price at the moment we received it -- flags True if price
    is already beyond late_threshold_pct away from the nearest edge of
    the entry zone. Proposed default 0.15% (2026-08-08, stated
    explicitly, not silently invented) -- roughly half the smallest
    plausible risk distance observed in this project's own signal data
    (see afra_parser_correction_test.py's 0.01%-3% sanity band) --
    subject to real calibration once late_signal data accumulates.
    Returns True/False, or 'UNKNOWN' if no price reference exists for
    this symbol (plain forex pairs other than gold have no ccxt
    market -- never guessed as False)."""
    sym = sig['symbol']
    if sym == 'XAUUSD':
        ref = 'XAU/USDT:USDT'
    elif ':USDT' in sym:
        ref = sym
    else:
        return 'UNKNOWN'
    try:
        price = ex.fetch_ticker(ref)['last']
    except Exception:
        return 'UNKNOWN'
    lo, hi = sig['entry_low'], sig['entry_high']
    if lo <= price <= hi:
        return False
    dist = min(abs(price - lo), abs(price - hi))
    return (dist / price * 100) > late_threshold_pct


FXG_PAIR = re.compile(r'\b(XAU|EUR|GBP|AUD|NZD|USD|CAD|CHF|JPY)\s*/?\s*(USD|JPY|CHF|CAD|AUD|NZD|GBP)\b')
FXG_GOLD = re.compile(r'\bGOLD\b')
FXG_NUM = r'([0-9]*\.?[0-9]+)'

def parse_fx_gold_signal(text):
    """Parse forex/gold signals from notify-only groups (Learn2Trade / FOREX GDP style)."""
    if not text:
        return None
    t = text.upper()
    m_pair = FXG_PAIR.search(t)
    m_side = re.search(r'\b(BUY|LONG|SELL|SHORT)\b', t)
    m_entry = re.search(r'ENTRY(?:\s*(?:ZONE|PRICE))?\s*[:\-]*\s*\**\$?' + FXG_NUM + r'(?:\s*[-–]\s*\$?' + FXG_NUM + r')?', t)
    if not m_entry and m_side:
        # 'GOLD SELL 4139 - 4144' style: zone right after the side keyword
        m_entry = re.search(m_side.group(1) + r'\s+\$?' + FXG_NUM + r'(?:\s*[-\u2013]\s*\$?' + FXG_NUM + r')?', t)
    m_sl = re.search(r'(?:STOP\s*LOSS|\bSL\b)\s*[.:\-]*\s*\**\$?' + FXG_NUM, t)
    m_tp = re.search(r'TP1?\s*[.:\-]*\s*\**\$?' + FXG_NUM, t)
    if not m_pair and FXG_GOLD.search(t):
        class _G: pass
        m_pair = _G(); m_pair.group = lambda i: {1: 'XAU', 2: 'USD'}[i]
    if not (m_pair and m_side and m_entry and m_sl and m_tp):
        return None
    e1 = float(m_entry.group(1))
    e2 = float(m_entry.group(2)) if m_entry.group(2) else e1
    return {'symbol': m_pair.group(1) + m_pair.group(2),
            'side': 'buy' if m_side.group(1) in ('BUY', 'LONG') else 'sell',
            'entry_low': min(e1, e2), 'entry_high': max(e1, e2),
            'sl': float(m_sl.group(1)), 'targets': [float(m_tp.group(1))]}


def _correct_afra_decimal_shift(sig):
    """AFRA_ALQASIMI_FX_GOLD-specific fix (2026-08-08, Ahmed-approved,
    isolated-tested first in TelegramGroupAudit/afra_parser_correction_test.py).
    This channel types TP/SL (and sometimes entry) with a decimal point
    inserted after the first digit -- e.g. '4.178' meaning 4178 -- a
    channel-specific typing habit confirmed from real raw message text,
    not a general parser defect. SCOPED: only called for this one group
    (see call site in handler()) -- does not change parsing for any
    other channel. A value already in the expected [1000,10000) gold
    range is left untouched, never blindly multiplied. Returns
    (corrected_sig, ambiguous) -- ambiguous=True means one or more
    fields could not be confidently corrected, or the corrected values
    don't make directional sense (buy: sl<entry<tp, sell: tp<entry<sl)
    -- caller must skip the signal entirely, never guess."""
    def fix(value, lo=1000, hi=10000):
        if lo <= value < hi:
            return value, False
        if 1.0 <= value < 10.0:
            corrected = value * 1000
            if lo <= corrected < hi:
                return corrected, True
        return value, None  # None = ambiguous

    corrected = dict(sig)
    ambiguous = False
    for field in ('entry_low', 'entry_high', 'sl'):
        v, flag = fix(sig[field])
        corrected[field] = v
        if flag is None:
            ambiguous = True
    new_targets = []
    for t in sig['targets']:
        v, flag = fix(t)
        new_targets.append(v)
        if flag is None:
            ambiguous = True
    corrected['targets'] = new_targets

    if not ambiguous:
        entry = (corrected['entry_low'] + corrected['entry_high']) / 2
        tp, sl = corrected['targets'][0], corrected['sl']
        if corrected['side'] == 'buy':
            ambiguous = not (sl < entry < tp)
        else:
            ambiguous = not (tp < entry < sl)

    return corrected, ambiguous


def open_positions():
    try:
        return {p['symbol']: p for p in ex.fetch_positions()
                if float(p.get('contracts') or 0) > 0}
    except Exception as e:
        log(f'positions error: {e}')
        return {}


def group_open_count(group):
    try:
        if not os.path.exists(TRADES_LOG):
            return 0
        with open(TRADES_LOG) as f:
            data = json.load(f)
        open_syms = set(open_positions().keys())
        return sum(1 for t in data if t['group'] == group and t['symbol'] in open_syms)
    except Exception:
        return 0


def total_equity():
    try:
        b = ex.fetch_balance()
        return float(b['USDT'].get('total') or 0)
    except Exception:
        return 0.0


MANUAL_BLOCK_FILE = '/home/ubuntu/trading-bot/manual_block_tg_signal_bot.json'


def manual_entry_block():
    """Entry-only freeze switch (Ahmed 2026-08-22, Oracle bots review) -- reads
    the central freeze first, then the legacy manual JSON flag file. Blocks
    execute() from opening NEW trades only; never touches open positions,
    protective orders, or exit logic."""
    try:
        freeze = entry_freeze_status('tg_signal_bot', account='BAA')
        if freeze.get('frozen'):
            return True, freeze.get('reason', 'central entry freeze')
    except Exception as e:
        return True, f'central freeze unreadable: {e}'
    try:
        if os.path.exists(MANUAL_BLOCK_FILE):
            with open(MANUAL_BLOCK_FILE) as f:
                d = json.load(f)
            return d.get('blocked', False), d.get('reason', 'manual block')
    except Exception:
        pass
    return False, None


def circuit_breaker_tripped():
    """Halt signal-trading for the rest of the UTC day if equity fell DAILY_HALT_PCT
    below the day's starting equity. Day-start equity recorded on first check of the day."""
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    try:
        d = {}
        if os.path.exists(DAY_FILE):
            with open(DAY_FILE) as f:
                d = json.load(f)
        eq = total_equity()
        if d.get('date') != today:
            d = {'date': today, 'start_eq': eq, 'halted': False}
            with open(DAY_FILE, 'w') as f:
                json.dump(d, f)
            return False
        if d.get('halted'):
            return True
        if d['start_eq'] > 0 and eq < d['start_eq'] * (1 - DAILY_HALT_PCT):
            d['halted'] = True
            with open(DAY_FILE, 'w') as f:
                json.dump(d, f)
            notify(f'CIRCUIT BREAKER: equity {eq:.2f} is >{DAILY_HALT_PCT:.0%} below day start '
                   f'({d["start_eq"]:.2f}) — signal trading HALTED until tomorrow UTC. '
                   f'Open positions keep their SL/TP.')
            return True
        return False
    except Exception as e:
        log(f'breaker error: {e}')
        return False


BLOCKED_BASES = {'XAU', 'XAG', 'GOLD'}   # Ahmed 2026-07-04: no gold from telegram signals
                                          # 2026-07-15: GOLD_ALLOWED_GROUPS exempted (Ahmed: gold on BAA)


def _gold_atr_dist():
    """SL distance for gold: ATR(14) on H1 x 0.5, bounded $3-$10 — same numbers
    as fx_signal_exec on EA/BA. Fallback to the old fixed $4 if the fetch fails."""
    try:
        bars = ex.fetch_ohlcv('XAU/USDT:USDT', '1h', limit=15)
        trs = [max(c[2] - c[3], abs(c[2] - p[4]), abs(c[3] - p[4]))
               for p, c in zip(bars, bars[1:])]
        return min(10.0, max(3.0, (sum(trs) / len(trs)) * 0.5))
    except Exception as e:
        log(f'gold atr error: {e} — fallback $4')
        return 4.0


def _crypto_atr_dist(sym):
    """SL distance for majors (BTC/ETH/SOL): ATR(14) on H1 x 0.5 -- same
    proportional approach as gold. No fixed $ bound (price scales differ too
    much across BTC vs SOL); falls back to 1% of price if the fetch fails.
    2026-07-16: added after gold-on-our-risk (fx_signal_exec) made +$41.81/wk
    while BAA crypto groups using their own far SL/TP sat flat/negative despite
    good paper win rates -- same fix, applied to crypto."""
    try:
        bars = ex.fetch_ohlcv(sym, '1h', limit=15)
        trs = [max(c[2] - c[3], abs(c[2] - p[4]), abs(c[3] - p[4]))
               for p, c in zip(bars, bars[1:])]
        price = bars[-1][4]
        return max((sum(trs) / len(trs)) * 0.5, price * 0.003)
    except Exception as e:
        log(f'crypto atr error {sym}: {e} — fallback 1% of price')
        return ex.fetch_ticker(sym)['last'] * 0.01


# Majors-only execution filter (Ahmed 2026-07-15 "نفذ فلتر BAA"): random alts from
# signal groups churned 50+ trades/week to a flat PnL while their held margin
# blocked real gold/BTC entries (two InsufficientFunds incidents same day).
# Anything else still gets recorded for evaluation — just never traded.
EXEC_BASES = {'BTC', 'ETH', 'SOL', 'XAU'}


def execute(group, sig):
    sym = sig['symbol']
    base = sym.split('/')[0]
    if base in BLOCKED_BASES or base.startswith('XAU'):
        if group in GOLD_ALLOWED_GROUPS and (base.startswith('XAU') or base == 'GOLD'):
            # direction from the group, OUR risk numbers (feedback-signal-direction-our-risk)
            sym = 'XAU/USDT:USDT'
            sig['symbol'] = sym
            sig['gold_our_risk'] = True
        else:
            notify(f'SIGNAL SKIPPED ({group}): {sym} — gold/metals blocked from telegram signals by Ahmed’s rule.')
            return
    base = sig['symbol'].split('/')[0]
    if base not in EXEC_BASES:
        log(f'skip {sig["symbol"]} ({group}) — outside majors whitelist, recorded only')
        return
    if sig['sl'] is None:
        notify(f'SIGNAL SKIPPED ({group}): {sym} {sig["side"].upper()} — no stop-loss in signal, not trading it.')
        return
    if circuit_breaker_tripped():
        log(f'skip {sym} — daily circuit breaker active')
        return
    blocked, block_reason = manual_entry_block()
    if blocked:
        log(f'skip {sym} — manual entry block active: {block_reason}')
        return
    if is_portfolio_halted():
        log(f'skip {sym} — portfolio-wide RISK_HALT active (synced from Windows)')
        return

    try:
        ex.load_markets()
        if sym not in ex.markets:
            base = sym.split('/')[0]
            cands = [m for m in ex.markets if m.endswith('/USDT:USDT')
                     and m.split('/')[0].startswith(base)]
            if len(cands) == 1:
                log(f'symbol {sym} resolved to {cands[0]}')
                sym = cands[0]
                sig['symbol'] = sym
            else:
                notify(f'SIGNAL SKIPPED ({group}): {sym} not listed on Bybit linear.')
                return
        pos = open_positions()
        if sym in pos:
            notify(f'SIGNAL SKIPPED ({group}): {sym} already has an open position.')
            return
        open_count = group_open_count(group)
        if open_count >= MAX_OPEN_PER_GROUP:
            notify(f'SIGNAL SKIPPED ({group}): group already has {open_count} open trades (max {MAX_OPEN_PER_GROUP}).')
            return

        price = ex.fetch_ticker(sym)['last']
        lo = sig['entry_low'] * (1 - ENTRY_TOL)
        hi = sig['entry_high'] * (1 + ENTRY_TOL)
        if not (lo <= price <= hi):
            notify(f'SIGNAL SKIPPED ({group}): {sym} price {price} outside entry zone '
                   f'{sig["entry_low"]}-{sig["entry_high"]} — not chasing.')
            return

        sl = sig['sl']
        # BINANCE_360 takes T1 (2026-07-15, Ahmed): eval showed 97% of its signals
        # touch T1 but realized PnL was flat targeting T2 — bank T1, and the
        # reopen-on-close rule covers continuation toward T2/T3 when it's real.
        if group == 'BINANCE_360':
            tp = sig['targets'][0]
        else:
            tp = sig['targets'][1] if len(sig['targets']) > 1 else sig['targets'][0]
        if sig.get('gold_our_risk'):
            # replace the group's wide SL/TP with our scalp numbers around the live price
            d = _gold_atr_dist()
            sl = price - d if sig['side'] == 'buy' else price + d
            tp = price + 3 * d if sig['side'] == 'buy' else price - 3 * d
        elif base in ('BTC', 'ETH', 'SOL'):
            # 2026-07-16 (Ahmed approved): same our-risk fix as gold -- the
            # group's own wide SL/TP left BINANCE_360/SPARTA_CRYPTO flat despite
            # good paper win rates, because the group's distant TP2 rarely got
            # reached even when TP1 did. ATR-based stop, fixed 3:1 RR instead.
            d = _crypto_atr_dist(sym)
            sl = price - d if sig['side'] == 'buy' else price + d
            tp = price + 3 * d if sig['side'] == 'buy' else price - 3 * d
        # sanity: SL must be on the correct side
        if (sig['side'] == 'buy' and sl >= price) or (sig['side'] == 'sell' and sl <= price):
            notify(f'SIGNAL SKIPPED ({group}): {sym} SL {sl} on wrong side of price {price}.')
            return

        rr = _signal_rr(price, sl, tp)
        if rr < MIN_RR:
            notify(f'SIGNAL SKIPPED ({group}): {sym} RR {rr:.2f} below minimum {MIN_RR:.2f}.')
            return

        balance = float(ex.fetch_balance()['USDT']['total'])
        risk_pct = RISK_PCT_PROVEN if group in PROVEN_GROUPS else RISK_PCT_PROBATION
        dist = abs(price - sl)
        m = ex.market(sym)
        step = m['precision']['amount'] or 1
        risk_qty = math.floor((balance * risk_pct / dist) / step) * step
        # margin cap: risk-based sizing explodes when SL distance is tiny relative
        # to price (gold: ~$4 on ~$4000) — cap at 50% of balance as isolated margin
        # so the order can never bounce with InsufficientFunds (weekend XAU incident)
        margin_cap_qty = math.floor((balance * 0.5 * LEVERAGE / price) / step) * step
        qty = min(risk_qty, margin_cap_qty)
        # diagnostic only (Ahmed 2026-08-04): does not affect qty/execution above
        sizing_source = 'risk-based' if risk_qty <= margin_cap_qty else 'margin-capped'
        dollar_risk = qty * dist
        margin_used_est = qty * price / EXCHANGE_LEVERAGE
        margin_pct_est = (margin_used_est / balance * 100) if balance > 0 else 0.0
        cap_ratio = (margin_cap_qty / risk_qty) if risk_qty > 0 else float('inf')
        effective_risk_pct = (dollar_risk / balance * 100) if balance > 0 else 0.0
        log(f'SIZING_DIAG {group}: risk_qty={risk_qty:.6f} margin_cap_qty={margin_cap_qty:.6f} '
            f'chosen_qty={qty:.6f} source={sizing_source} dollar_risk=${dollar_risk:.4f} '
            f'margin_used_est=${margin_used_est:.2f} margin_pct_est={margin_pct_est:.2f}% '
            f'cap_ratio={cap_ratio:.4f} effective_risk_pct={effective_risk_pct:.4f}%')
        if qty <= 0 or qty * price < MIN_NOTIONAL:
            notify(f'SIGNAL SKIPPED ({group}): {sym} size too small (balance {balance:.2f}).')
            return

        try:
            ex.set_margin_mode('isolated', sym)
        except Exception:
            pass
        try:
            ex.set_leverage(EXCHANGE_LEVERAGE, sym)
        except Exception:
            pass

        # 2026-08-09 Phase 2: tag + local intent log only -- no trading param changed
        tag = make_tag('tgsig', sym, sig['side'])
        record_intent('tg_signal_bot', sym, sig['side'], qty, tag, extra={'source_group': group})
        ex.create_order(sym, 'market', sig['side'], qty, params={
            'stopLoss': str(sl), 'slTriggerBy': 'MarkPrice',
            'takeProfit': str(tp), 'tpTriggerBy': 'MarkPrice',
            'orderLinkId': tag,
        })
        record_trade(group, sig, qty, price)
        log(f'EXECUTED {group}: {sig["side"]} {sym} qty={qty} SL={sl} TP={tp}')
        notify(f'SIGNAL EXECUTED ({group})\n{sig["side"].upper()} {sym}\n'
               f'entry ~{price} qty {qty}\nSL {sl} | TP {tp} (target 2)\nrisk {risk_pct:.0%}')
    except Exception as e:
        log(f'execute error {sym}: {e}')
        notify(f'SIGNAL ERROR ({group}) {sym}: {str(e)[:200]}')


async def main():
    client = TelegramClient('/home/ubuntu/trading-bot/tg_session',
                            int(os.getenv('TG_API_ID')), os.getenv('TG_API_HASH'))
    await client.connect()
    if not await client.is_user_authorized():
        log('NOT AUTHORIZED — session missing')
        return

    FX_PAIR = re.compile(r'\b(EUR|GBP|USD|JPY|AUD|NZD|CAD|CHF)(USD|JPY|CHF|CAD|AUD|NZD|GBP)\b')

    @client.on(events.NewMessage(chats=list(GROUPS.keys()) + list(NOTIFY_ONLY_GROUPS.keys()) + list(RECORD_ONLY_GROUPS.keys())))
    async def handler(event):
        text = event.raw_text or ''
        write_heartbeat('tg_signal_bot')
        if event.chat_id in RECORD_ONLY_GROUPS:
            group = RECORD_ONLY_GROUPS[event.chat_id]
            sig = parse_signal(text) or parse_fx_gold_signal(text)
            if sig and group == 'AFRA_ALQASIMI_FX_GOLD':
                sig, ambiguous = _correct_afra_decimal_shift(sig)
                if ambiguous:
                    log(f'AFRA_ALQASIMI_FX_GOLD signal SKIPPED -- ambiguous decimal-shift correction, not recorded: {sig}')
                    return
            if sig:
                late = _check_late_signal(sig)
                record_signal(group, sig, message_id=event.id, late_signal=late)
                log(f'paper signal recorded from {group}: {sig["symbol"]} {sig["side"]} '
                    f'(msg_id={event.id}, late_signal={late})')
            return
        if event.chat_id in NOTIFY_ONLY_GROUPS:
            group = NOTIFY_ONLY_GROUPS[event.chat_id]
            if FX_PAIR.search(text.upper()) and re.search(r'\b(BUY|SELL)\b', text, re.I):
                log(f'fx signal forwarded from {group}')
                notify(f'FX SIGNAL ({group}) — notify only, no auto-trade:\n\n{text[:800]}')
            return
        group = GROUPS.get(event.chat_id, str(event.chat_id))
        # gold groups post fx-style signals — try both parsers (2026-07-15)
        sig = parse_signal(text) or (parse_fx_gold_signal(text) if group in GOLD_ALLOWED_GROUPS else None)
        if sig:
            log(f'signal from {group}: {sig}')
            record_signal(group, sig)
            execute(group, sig)

    @client.on(events.MessageEdited(chats=list(RECORD_ONLY_GROUPS.keys())))
    async def edit_handler(event):
        """Qualification Safety layer (2026-08-08, Ahmed-approved) --
        detects a source editing a signal after posting it. Scoped to
        RECORD_ONLY_GROUPS only (same population the promotion gate
        cares about) -- does not touch GROUPS/NOTIFY_ONLY_GROUPS
        behavior at all."""
        group = RECORD_ONLY_GROUPS.get(event.chat_id, str(event.chat_id))
        log(f'SIGNAL_MUTATION (edited): group={group} msg_id={event.id}')
        record_mutation(group, event.id, 'edited', event.raw_text)

    @client.on(events.MessageDeleted(chats=list(RECORD_ONLY_GROUPS.keys())))
    async def delete_handler(event):
        """Same scope as edit_handler. Telethon's deleted-message event
        does not always carry chat_id reliably for every chat type --
        handled defensively, logs 'UNKNOWN' group rather than guessing."""
        group = RECORD_ONLY_GROUPS.get(event.chat_id, 'UNKNOWN') if event.chat_id else 'UNKNOWN'
        for mid in event.deleted_ids:
            log(f'SIGNAL_MUTATION (deleted): group={group} msg_id={mid}')
            record_mutation(group, mid, 'deleted')

    log(f'tg_signal_bot started — watching {list(GROUPS.values())} + notify-only {list(NOTIFY_ONLY_GROUPS.values())}')
    await client.run_until_disconnected()


SAMPLE = """🐳 NAORIS/USDT #LONG 🐳
✳️ ENTRY :- 0.045 - 0.044
💢 LEVERAGE :- 20X
🔖TARGET :-
1 : 0.046
2 : 0.049
3 : 0.054
⛔️ STOP-LOSS :- 0.039
"""

SAMPLE_AR = """ADA/USDT

فيوتشر وسبوت
سعر الدخول : 0.1455 - 0.1378

الأهداف:

0.14770
0.14918
0.15065

ستوب/ إغلاق شمعة اربع ساعات أسفل  0.13293
"""

if __name__ == '__main__':
    import sys
    if '--test' in sys.argv:
        s = parse_signal(SAMPLE)
        assert s, 'parse failed'
        assert s['symbol'] == 'NAORIS/USDT:USDT' and s['side'] == 'buy'
        assert s['entry_low'] == 0.044 and s['entry_high'] == 0.045
        assert s['targets'] == [0.046, 0.049, 0.054] and s['sl'] == 0.039
        a = parse_signal(SAMPLE_AR)
        assert a, 'arabic parse failed'
        assert a['symbol'] == 'ADA/USDT:USDT' and a['side'] == 'buy'
        assert a['entry_low'] == 0.1378 and a['entry_high'] == 0.1455
        assert a['targets'][0] == 0.1477 and a['sl'] == 0.13293
        assert parse_signal('PROFIT +83% TARGET ACHIEVED') is None
        assert parse_signal('NAORIS/USDT LONG no numbers here') is None
        assert parse_signal('BTC/USDT ENTRY: 100\n1 : 110\nno side given') is None
        print('parse tests OK (EN + AR)')
    else:
        asyncio.run(main())
