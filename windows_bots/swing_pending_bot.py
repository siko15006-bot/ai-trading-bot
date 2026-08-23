"""
swing_pending_bot.py -- mechanical swing-high/low detector that places
resting limit orders at real structural levels, forex + crypto only
(Ahmed 2026-07-30: gold stays manual/Claude-judgment only until this bot
is tested and evaluated -- its losses have been too severe to hand to an
untested mechanical script yet).

Every CHECK_INTERVAL seconds, per symbol:
  1. Fetch M15 candles, find the most recent CONFIRMED 3-candle fractal
     swing high and swing low (same pivot definition as
     ladder_guard.find_swing_anchor -- a real level the market already
     respected, not an arbitrary fraction).
  2. Compute a tight ATR-based SL and an RR-based TP (Ahmed 2026-07-30:
     "قلل الستوب عشان تحكم الصفقات صح" -- SL_ATR_MULT deliberately small).
  3. Place (or replace, if the underlying pivot moved) a BUY_LIMIT at the
     swing low and a SELL_LIMIT at the swing high -- two-sided, no
     directional guess, purely "here is a real level worth reacting to."
  4. Never touches an order it didn't place itself (tracked by comment
     tag), never re-places at the same level twice, never places at a
     level the live price has already passed (would fill immediately at
     market, defeating the point of a resting limit order).

This is a mechanical LEVEL-MARKER, not a strategy verdict -- it does not
decide direction, size beyond the fixed default, or override anything
Claude places by hand. Deliberately excluded for now (needs its own
future review before inclusion): XAUUSD/XAG (Ahmed's explicit gold
exception above), USOIL/UKOIL (already volatile/manual today), any
index.

Account credentials are NOT duplicated in this file -- imported from
mt5_accounts.py (same dict ladder_guard.py/ea_shield.py already read from
that shared module) so a password change only ever needs editing in one
place.

Usage: python swing_pending_bot.py EA|EM|BA
"""
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone

import MetaTrader5 as mt5
from heartbeat import write_heartbeat  # 2026-08-06: functional heartbeat rollout, see NOTIFICATION_POLICY.md
                                        # (code-only patch -- this bot is currently halted per Ahmed's
                                        # 2026-08-04 decision, not tested live; ready for whenever it restarts)

try:
    from mt5_accounts import ACCOUNTS
except ImportError:
    # shared module not present yet on this machine -- fall back to the
    # same per-account dict ladder_guard.py defines inline, so this bot
    # still runs standalone rather than crashing at import time.
    ACCOUNTS = {
        "BA": dict(path=r"C:\Program Files\MetaTrader 5\terminal64.exe"),
        "EA": dict(path=r"C:\MT5_Portable_2\terminal64.exe",
                   login=<REDACTED_MT5_LOGIN_EA>, password="<REDACTED_MT5_PASSWORD_EA>", server="Exness-MT5Real33"),
        "EM": dict(path=r"C:\MT5_Portable_3\terminal64.exe",
                   login=<REDACTED_MT5_LOGIN_EM>, password="<REDACTED_MT5_PASSWORD_EM>", server="Exness-MT5Real35"),
    }

# forex + crypto only -- gold/oil/indices deliberately excluded, see module docstring
SYMBOLS_BY_ACCOUNT = {
    # USDJPY/USDCAD/USDCHF added 2026-07-31 (Ahmed: more liquid majors,
    # confirmed safe -- tiny 0.01 lots + ample free margin, dedupe in
    # ladder_guard.py already covers any overlap with other machines).
    "EA": ["EURUSDm", "GBPUSDm", "AUDUSDm", "NZDUSDm", "USDJPYm", "USDCADm", "USDCHFm", "BTCUSDm", "ETHUSDm"],
    "EM": ["EURUSDm", "GBPUSDm", "AUDUSDm", "NZDUSDm", "USDJPYm", "USDCADm", "USDCHFm", "BTCUSDm", "ETHUSDm"],
    "BA": ["EURUSD.s", "GBPUSD.s", "AUDUSD.s", "NZDUSD.s", "USDJPY.s", "USDCAD.s", "USDCHF.s"],  # no BTC/ETH symbols on BA
}
LOT_OVERRIDES = {"ETHUSDm": 0.1}
DEFAULT_LOT = 0.01

TIMEFRAME = mt5.TIMEFRAME_M15
ATR_PERIOD = 14
SL_ATR_MULT = 1.5   # widened 2026-08-01 (Ahmed: the 0.8x version whipsawed on normal
                     # noise -- hundreds of small SL hits, -52%/-44% on EA/BA in one day)
TP_RR = 2.5          # take-profit distance = TP_RR x SL distance
MIN_PIVOT_AGE_BARS = 2       # pivot must be at least this many candles old (confirmed, not the live bar)
LEVEL_MOVE_TOL_MULT = 0.5    # replace a resting order only if the new pivot moved >= this many ATRs from the old one
CHECK_INTERVAL = 900         # 15 minutes -- mechanical structure check, not a scalping cadence
COMMENT_TAG = "swing_pending"
INIT_RETRY_BASE = 5
INIT_RETRY_MAX = 60
# 2026-08-02 cleanup: every position this bot ever opened carried magic=0
# (never set explicitly -- MT5 defaults to 0). ladder_guard.py treats magic==0
# as an unverified MANUAL trade and applies its most aggressive protection:
# EARLY_PROTECT locks partial profit at just 25% progress toward TP, and the
# RUNNER logic can strip the TP entirely and trail once progress hits 90%.
# Audited 45 real closed trades from the 2026-08-01/02 overnight run: net
# -$10.41, and NOT ONE of the 45 closes carried MT5's own "[tp ...]" auto-
# annotation -- every single trade was cut short by ladder_guard's magic==0
# handling before ever reaching the bot's own designed 2.5:1 target. Giving
# this bot its own magic removes it from that manual-trade tier; it still
# gets ladder_guard's normal STEPS_BY_CLASS profit ladder like any other
# automated system (55%/75%/90% progressive locks for crypto), just not the
# premature 25%-trigger lock or TP-stripping meant for unverified clicks.
MAGIC = 995500

_placed_levels = {}  # (account, symbol, "high"|"low") -> (price, order_ticket)

# --- market-hours + news awareness (2026-08-01, Ahmed: "الاوامر المعلقه ممكن
# تعمل كارثه" -- a resting limit order left over the weekend close or sitting
# through a high-impact release can fill at a terrible gap/spike price) ---
CRYPTO_SYMBOLS = {"BTCUSDm", "ETHUSDm"}  # 24/7, no weekend/news gate applies
SYMBOL_CURRENCIES = {
    "EURUSDm": ("EUR", "USD"), "EURUSD.s": ("EUR", "USD"),
    "GBPUSDm": ("GBP", "USD"), "GBPUSD.s": ("GBP", "USD"),
    "AUDUSDm": ("AUD", "USD"), "AUDUSD.s": ("AUD", "USD"),
    "NZDUSDm": ("NZD", "USD"), "NZDUSD.s": ("NZD", "USD"),
    "USDJPYm": ("USD", "JPY"), "USDJPY.s": ("USD", "JPY"),
    "USDCADm": ("USD", "CAD"), "USDCAD.s": ("USD", "CAD"),
    "USDCHFm": ("USD", "CHF"), "USDCHF.s": ("USD", "CHF"),
}
MARKET_CLOSE_BUFFER_MIN = 45   # stop placing / start pulling forex orders this long before Friday 21:00 UTC close
MARKET_REOPEN_BUFFER_MIN = 20  # wait this long after Sunday 21:00 UTC reopen before placing again (gap/spread settle)
NEWS_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"  # free, no key, used read-only
NEWS_CACHE_TTL_SEC = 1800
NEWS_BLACKOUT_BEFORE_MIN = 60
NEWS_BLACKOUT_AFTER_MIN = 30

_news_cache = {"ts": 0.0, "events": []}


def _forex_market_open(now=None):
    """False across the weekend close (with buffers on both sides). Crypto
    symbols never call this -- they're 24/7."""
    now = now or datetime.now(timezone.utc)
    wd = now.weekday()  # Mon=0 .. Sun=6
    minute_of_day = now.hour * 60 + now.minute
    if wd == 4 and minute_of_day >= 21 * 60 - MARKET_CLOSE_BUFFER_MIN:
        return False
    if wd == 5:
        return False
    if wd == 6 and minute_of_day < 21 * 60 + MARKET_REOPEN_BUFFER_MIN:
        return False
    return True


def _refresh_news_cache():
    if time.time() - _news_cache["ts"] < NEWS_CACHE_TTL_SEC:
        return
    try:
        with urllib.request.urlopen(NEWS_CALENDAR_URL, timeout=10) as r:
            _news_cache["events"] = json.loads(r.read())
    except Exception as e:
        log(f"news calendar fetch failed: {str(e)[:150]} -- keeping last-known calendar")
    _news_cache["ts"] = time.time()


def _news_blackout(symbol, now=None):
    """True if a HIGH-impact event for this symbol's currencies falls
    inside the blackout window. Fails OPEN (no blackout) on a fetch error --
    Ahmed's own manual news-protocol oversight (feedback-news-event-protocol)
    is still the primary defense; this is a second layer, not the only one."""
    ccys = SYMBOL_CURRENCIES.get(symbol)
    if not ccys:
        return False
    _refresh_news_cache()
    now = now or datetime.now(timezone.utc)
    for ev in _news_cache["events"]:
        if ev.get("impact") != "High" or ev.get("country") not in ccys:
            continue
        try:
            ev_time = datetime.fromisoformat(ev["date"])
        except Exception:
            continue
        delta_min = (ev_time - now).total_seconds() / 60
        if -NEWS_BLACKOUT_AFTER_MIN <= delta_min <= NEWS_BLACKOUT_BEFORE_MIN:
            return True
    return False


def _cancel_resting(acc, symbol):
    """Pull any resting pending order this bot placed for (acc, symbol), both
    sides -- used before a weekend close or a news blackout so nothing is
    left resting to fill at a gap/spike price."""
    for side in ("low", "high"):
        key = (acc, symbol, side)
        prev = _placed_levels.get(key)
        if prev is None:
            continue
        _, ticket = prev
        if _existing_order_ok(ticket):
            try:
                mt5.order_send(dict(action=mt5.TRADE_ACTION_REMOVE, order=ticket))
                log(f"{acc} {symbol} {side} pending PULLED (market close/news window)")
            except Exception:
                pass
            _placed_levels.pop(key, None)
        # 2026-08-04: if the order is no longer pending (already filled into
        # a live position before the close/blackout hit), leave the tracking
        # entry in place instead of clearing it -- popping it here was a
        # second path to the same 2026-08-03 duplicate-entry bug: the moment
        # trading resumed, an unchanged pivot with no tracking would look
        # like a fresh level and get a brand-new duplicate order placed on
        # it, even though a position from it might still be open or was
        # only just closed. _place_or_replace's own unchanged-pivot check
        # (see its comment) is what actually prevents re-entry -- it only
        # works if this function doesn't erase its memory first.


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _connect_mt5(acc, cfg):
    delay = INIT_RETRY_BASE
    while True:
        mt5.shutdown()
        if mt5.initialize(**cfg):
            return
        log(f"{acc} mt5.initialize FAILED: {mt5.last_error()} -- retrying in {delay}s")
        time.sleep(delay)
        delay = min(INIT_RETRY_MAX, delay * 2)


def _atr(symbol):
    try:
        rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 0, ATR_PERIOD + 2)
        if rates is None or len(rates) < ATR_PERIOD + 1:
            return None
        trs = []
        prev_close = rates[0]["close"]
        for row in rates[1:]:
            h, l, c = row["high"], row["low"], row["close"]
            trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
            prev_close = c
        return sum(trs[-ATR_PERIOD:]) / ATR_PERIOD
    except Exception as e:
        log(f"{symbol} ATR error: {str(e)[:120]}")
        return None


def _find_pivots(symbol):
    """Most recent CONFIRMED 3-candle fractal swing high and swing low on
    M15, at least MIN_PIVOT_AGE_BARS old (never the still-forming bar)."""
    try:
        rates = mt5.copy_rates_from_pos(symbol, TIMEFRAME, 0, 60)
        if rates is None or len(rates) < 10:
            return None, None
        swing_high = swing_low = None
        for i in range(len(rates) - 1 - MIN_PIVOT_AGE_BARS, 1, -1):
            hi, lo = rates[i]["high"], rates[i]["low"]
            if swing_high is None and hi > rates[i - 1]["high"] and hi > rates[i + 1]["high"]:
                swing_high = float(hi)
            if swing_low is None and lo < rates[i - 1]["low"] and lo < rates[i + 1]["low"]:
                swing_low = float(lo)
            if swing_high is not None and swing_low is not None:
                break
        return swing_high, swing_low
    except Exception as e:
        log(f"{symbol} pivot scan error: {str(e)[:120]}")
        return None, None


def _round_price(symbol, price):
    info = mt5.symbol_info(symbol)
    digits = info.digits if info else 5
    return round(price, digits)


def _lot(symbol):
    return LOT_OVERRIDES.get(symbol, DEFAULT_LOT)


def _existing_order_ok(ticket):
    orders = mt5.orders_get(ticket=ticket)
    return bool(orders)


def _place_or_replace(acc, symbol, side, level, atr):
    """side: 'low' (BUY_LIMIT at swing low) or 'high' (SELL_LIMIT at swing high)."""
    key = (acc, symbol, side)
    price = _round_price(symbol, level)
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return
    live = tick.bid if side == "low" else tick.ask
    if side == "low" and price >= live:
        return
    if side == "high" and price <= live:
        return

    prev = _placed_levels.get(key)
    if prev is not None:
        prev_price, prev_ticket = prev
        if atr and abs(price - prev_price) < LEVEL_MOVE_TOL_MULT * atr:
            # 2026-08-04 fix: same pivot as last cycle. Previously this branch
            # only fired while the ORIGINAL resting order was still pending --
            # the moment it filled, _existing_order_ok() (which only checks
            # orders_get(), i.e. resting orders) returned False and execution
            # fell through to placing a brand-new duplicate order on the
            # identical level. Confirmed root cause of the 2026-08-03 EA
            # incident: 17 of 44 trades that night were duplicates of just 7
            # real setups, accounting for 68% of the night's loss (see
            # project_swing_pending_duplicate_bug memory). Fix: as long as the
            # pivot hasn't moved, never re-enter it -- whether the previous
            # attempt is still resting, has become a live position, or has
            # already closed. Only a genuine pivot move (below) may try this
            # side again.
            return
        # pivot moved past tolerance -- cancel the stale resting order (if
        # still pending) and clear tracking so a fresh entry at the NEW level
        # can be placed below.
        if _existing_order_ok(prev_ticket):
            try:
                mt5.order_send(dict(action=mt5.TRADE_ACTION_REMOVE, order=prev_ticket))
            except Exception:
                pass
        _placed_levels.pop(key, None)

    sl_dist = SL_ATR_MULT * atr if atr else None
    if sl_dist is None or sl_dist <= 0:
        return
    tp_dist = TP_RR * sl_dist
    if side == "low":
        otype, sl, tp = mt5.ORDER_TYPE_BUY_LIMIT, price - sl_dist, price + tp_dist
    else:
        otype, sl, tp = mt5.ORDER_TYPE_SELL_LIMIT, price + sl_dist, price - tp_dist

    req = dict(action=mt5.TRADE_ACTION_PENDING, symbol=symbol, volume=_lot(symbol),
               type=otype, price=price, sl=_round_price(symbol, sl), tp=_round_price(symbol, tp),
               type_time=mt5.ORDER_TIME_GTC, type_filling=mt5.ORDER_FILLING_RETURN,
               magic=MAGIC, comment=COMMENT_TAG)
    r = mt5.order_send(req)
    ok = r is not None and r.retcode == mt5.TRADE_RETCODE_DONE
    if ok:
        _placed_levels[key] = (price, r.order)
    log(f"{acc} {symbol} {'BUY' if side == 'low' else 'SELL'}_LIMIT @ {price} "
        f"SL {sl:.5f} TP {tp:.5f} ({'OK' if ok else f'FAIL {r.retcode if r else None}'})")


def check_symbol(acc, symbol):
    if symbol not in CRYPTO_SYMBOLS:
        if not _forex_market_open():
            _cancel_resting(acc, symbol)
            return
        if _news_blackout(symbol):
            log(f"{acc} {symbol} skipped -- high-impact news blackout window")
            _cancel_resting(acc, symbol)
            return
    high, low = _find_pivots(symbol)
    atr = _atr(symbol)
    if atr is None:
        log(f"{acc} {symbol} skipped -- no ATR available")
        return
    if low is not None:
        _place_or_replace(acc, symbol, "low", low, atr)
    if high is not None:
        _place_or_replace(acc, symbol, "high", high, atr)


def _demo():
    """python swing_pending_bot.py --test -- asserts pivot detection,
    ATR-floor SL/TP, no-place-through-live-price, and no-duplicate-replace
    logic, without touching real MT5."""
    from types import SimpleNamespace as NS
    import numpy as np
    sent = []

    class FakeOrderSend:
        def __call__(self, req):
            sent.append(req)
            return NS(retcode=mt5.TRADE_RETCODE_DONE, order=len(sent))

    _RATE_DTYPE = np.dtype([
        ("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"),
        ("close", "f8"), ("tick_volume", "i8"), ("spread", "i4"), ("real_volume", "i8"),
    ])

    def make_rates(rows):
        arr = np.zeros(len(rows), dtype=_RATE_DTYPE)
        for i, (h, l, c) in enumerate(rows):
            arr[i] = (i, c, h, l, c, 0, 0, 0)
        return arr

    mt5.order_send = FakeOrderSend()
    mt5.symbol_info = lambda symbol: NS(digits=5)
    mt5.orders_get = lambda ticket=None: (NS(ticket=ticket),)
    _placed_levels.clear()

    # --- pivot detection: a clean fractal high and low, confirmed (not the live bar) ---
    rows = [(1.10, 1.09, 1.095) for _ in range(20)]
    rows[10] = (1.15, 1.09, 1.10)   # swing high at index 10
    rows[15] = (1.10, 1.04, 1.08)   # swing low at index 15
    rates = make_rates(rows)
    mt5.copy_rates_from_pos = lambda symbol, tf, start, count: rates[-count:] if count <= len(rates) else rates
    high, low = _find_pivots("TEST")
    assert high == 1.15, f"expected swing high 1.15, got {high}"
    assert low == 1.04, f"expected swing low 1.04, got {low}"
    print("swing_pending_bot pivot detection self-check: OK")

    # --- ATR: flat-ish candles around 1.10, small known true range ---
    atr_rows = [(1.102 + 0.001 * (i % 3), 1.098 - 0.001 * (i % 3), 1.10) for i in range(ATR_PERIOD + 2)]
    atr_rates = make_rates(atr_rows)
    mt5.copy_rates_from_pos = lambda symbol, tf, start, count: atr_rates
    atr = _atr("TEST")
    assert atr is not None and atr > 0, "ATR must compute a positive value from valid candles"
    print(f"swing_pending_bot ATR self-check: OK (ATR={atr:.5f})")

    # --- placement: BUY_LIMIT at a swing low BELOW live price must be placed ---
    mt5.symbol_info_tick = lambda symbol: NS(bid=1.12, ask=1.1202)
    sent.clear()
    _place_or_replace("EA", "TEST", "low", 1.10, atr)
    assert len(sent) == 1 and sent[0]["type"] == mt5.ORDER_TYPE_BUY_LIMIT
    assert abs(sent[0]["sl"] - (1.10 - SL_ATR_MULT * atr)) < 1e-9
    assert abs(sent[0]["tp"] - (1.10 + TP_RR * SL_ATR_MULT * atr)) < 1e-9
    assert sent[0]["magic"] == MAGIC, "every order must carry the bot's own magic, never the default 0 -- " \
        "magic=0 gets ladder_guard's manual-trade EARLY_PROTECT/RUNNER handling, which cut every real " \
        "trade short of its TP in the 2026-08-01/02 audit (see MAGIC's docstring above)"
    print("swing_pending_bot BUY_LIMIT placement self-check: OK")

    # --- never place a resting limit through/at the live price ---
    sent.clear()
    _place_or_replace("EA", "TEST2", "low", 1.13, atr)  # 1.13 > bid 1.12 -- invalid for a BUY_LIMIT
    assert not sent, "a BUY_LIMIT level at/above live bid must never be sent"
    sent.clear()
    _place_or_replace("EA", "TEST2", "high", 1.11, atr)  # 1.11 < ask 1.1202 -- invalid for a SELL_LIMIT
    assert not sent, "a SELL_LIMIT level at/below live ask must never be sent"
    print("swing_pending_bot no-place-through-price self-check: OK")

    # --- no duplicate replace: same pivot again must NOT resend ---
    sent.clear()
    _place_or_replace("EA", "TEST", "low", 1.10, atr)  # identical level, already tracked
    assert not sent, "an unchanged pivot must not be replaced/resent every cycle"
    print("swing_pending_bot no-duplicate-replace self-check: OK")

    # --- pivot moved meaningfully: must cancel old + place new ---
    sent.clear()
    _place_or_replace("EA", "TEST", "low", 1.10 - 5 * atr, atr)  # far below old level
    assert any(o.get("action") == mt5.TRADE_ACTION_REMOVE for o in sent), "moved pivot must cancel the stale resting order"
    assert any(o.get("action") == mt5.TRADE_ACTION_PENDING for o in sent), "moved pivot must place a fresh resting order at the new level"
    print("swing_pending_bot pivot-moved replace self-check: OK")

    # --- regression 2026-08-04: pivot UNCHANGED but the original order is no
    # longer pending (filled into a live position, or gone for any other
    # reason) -- must NOT place a duplicate. This is exactly the 2026-08-03
    # EA incident: _existing_order_ok() only checks orders_get() (resting
    # orders), so a FILLED order looked identical to "nothing tracked" and
    # the old code re-entered the same failed level again (17 of 44 trades
    # that night were duplicates of 7 real setups, 68% of the loss).
    sent.clear()
    _place_or_replace("EA", "TEST3", "low", 1.05, atr)  # below live bid 1.12 -- valid BUY_LIMIT
    assert len(sent) == 1, "first entry at a fresh level must be placed"
    orig_orders_get = mt5.orders_get
    mt5.orders_get = lambda ticket=None: ()  # simulate: order no longer pending (filled or gone)
    try:
        sent.clear()
        _place_or_replace("EA", "TEST3", "low", 1.05, atr)  # identical level
        assert not sent, ("must NOT re-enter an unchanged pivot even after the previous "
                           "order stopped being pending (2026-08-03 duplicate-entry bug)")
    finally:
        mt5.orders_get = orig_orders_get
    print("swing_pending_bot no-reentry-after-fill self-check: OK")

    # --- continuation of the above: once a genuinely NEW pivot forms (price
    # structure actually moved, not just the same level re-detected), entry
    # must resume -- the fix blocks re-entry on the OLD pivot only, it does
    # not disable the bot for that symbol/side forever.
    sent.clear()
    _place_or_replace("EA", "TEST3", "low", 1.05 - 2 * atr, atr)  # a real new, lower swing low
    new_orders = [o for o in sent if o.get("action") == mt5.TRADE_ACTION_PENDING]
    assert len(new_orders) == 1, (
        "a genuinely new pivot must still be tradeable after the old one was blocked post-fill")
    assert abs(new_orders[0]["price"] - (1.05 - 2 * atr)) < 1e-9, "must place at the NEW pivot, not the stale one"
    print("swing_pending_bot reentry-on-new-pivot self-check: OK")

    # --- regression 2026-08-04 (second path to the same bug): a market-close
    # or news blackout hitting AFTER an order already filled must NOT erase
    # the tracking entry -- otherwise the instant trading resumes on an
    # unchanged pivot, it looks like a fresh level and gets duplicated again,
    # defeating the fix above through a different door.
    sent.clear()
    _place_or_replace("EA", "TEST4", "low", 1.08, atr)  # below live bid 1.12
    assert len(sent) == 1, "setup: fresh level must be placed"
    mt5.orders_get = lambda ticket=None: ()  # simulate: filled before the blackout hits
    try:
        _cancel_resting("EA", "TEST4")
        assert ("EA", "TEST4", "low") in _placed_levels, (
            "_cancel_resting must not erase tracking for an order that already filled -- "
            "doing so re-opens the 2026-08-03 duplicate-entry bug via the market-close/news path")
    finally:
        mt5.orders_get = orig_orders_get
    sent.clear()
    _place_or_replace("EA", "TEST4", "low", 1.08, atr)  # same pivot, trading "resumed"
    assert not sent, "pivot still unchanged after _cancel_resting -- must stay blocked"
    print("swing_pending_bot cancel-resting-preserves-fill-tracking self-check: OK")

    # --- complementary case: a genuinely still-PENDING order must still be
    # cancelled and its tracking cleared as before (existing defensive
    # behavior, unaffected by the fix above) ---
    sent.clear()
    _place_or_replace("EA", "TEST5", "low", 1.09, atr)
    assert len(sent) == 1
    _cancel_resting("EA", "TEST5")  # order is still "pending" per the default mock
    assert any(o.get("action") == mt5.TRADE_ACTION_REMOVE for o in sent), \
        "a still-pending order must be cancelled on market-close/news"
    assert ("EA", "TEST5", "low") not in _placed_levels, \
        "tracking must clear once the pending order is actually cancelled"
    print("swing_pending_bot cancel-resting-still-pending self-check: OK")

    # --- market-close/reopen window ---
    fri_evening = datetime(2026, 7, 31, 20, 30, tzinfo=timezone.utc)   # inside pre-close buffer
    assert not _forex_market_open(fri_evening), "must be closed inside the pre-close buffer"
    fri_afternoon = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)  # ordinary Friday
    assert _forex_market_open(fri_afternoon), "must be open on an ordinary Friday afternoon"
    saturday = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    assert not _forex_market_open(saturday), "must be closed all Saturday"
    sunday_early = datetime(2026, 8, 2, 21, 10, tzinfo=timezone.utc)   # just after reopen, inside buffer
    assert not _forex_market_open(sunday_early), "must stay closed through the reopen buffer"
    sunday_late = datetime(2026, 8, 2, 22, 0, tzinfo=timezone.utc)
    assert _forex_market_open(sunday_late), "must be open once past the reopen buffer"
    print("swing_pending_bot market-hours self-check: OK")

    # --- news blackout ---
    _news_cache["events"] = [{"country": "USD", "impact": "High",
                               "date": "2026-07-31T13:30:00+00:00"}]
    _news_cache["ts"] = time.time()
    just_before = datetime(2026, 7, 31, 13, 0, tzinfo=timezone.utc)   # 30min before -- inside window
    assert _news_blackout("EURUSDm", just_before), "must blackout inside the pre-event window"
    long_before = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)
    assert not _news_blackout("EURUSDm", long_before), "must not blackout hours before an event"
    assert not _news_blackout("BTCUSDm", just_before), "crypto has no forex-calendar exposure"
    print("swing_pending_bot news blackout self-check: OK")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        _demo()
        return
    acc = sys.argv[1] if len(sys.argv) > 1 else "EA"
    cfg = ACCOUNTS[acc]
    symbols = SYMBOLS_BY_ACCOUNT.get(acc, [])
    _connect_mt5(acc, cfg)
    log(f"swing_pending_bot started on {acc} -- symbols {symbols}, forex+crypto only "
        f"(gold/oil/indices excluded per Ahmed 2026-07-30), every {CHECK_INTERVAL}s")
    while True:
        try:
            if mt5.terminal_info() is None:
                _connect_mt5(acc, cfg)
                continue
            for symbol in symbols:
                try:
                    check_symbol(acc, symbol)
                except Exception as e:
                    log(f"{acc} {symbol} CHECK_FAILED: {str(e)[:150]} -- other symbols unaffected")
        except Exception as e:
            log(f"{acc} loop error: {str(e)[:150]}")
            _connect_mt5(acc, cfg)
        write_heartbeat(f"swing_pending_bot_{acc}", symbols_checked=len(symbols))
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
