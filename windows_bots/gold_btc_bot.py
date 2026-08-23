"""
gold_btc_bot.py -- conservative, quality-over-quantity trend-following bot for
XAU and BTC only (Ahmed 2026-08-04 design, see project_gold_btc_bot_design
memory for the full write-up and rationale).

Every CHECK_INTERVAL seconds, per (account, symbol):
  1. Determine a directional hypothesis from EMA(fast) vs EMA(slow) on H1 --
     this always runs, it's the trade's basic premise, not a togglable gate.
  2. Run whichever gates are enabled in the config on top of that hypothesis:
     trend strength (ADX), momentum (RSI), volatility floor (ATR), and a
     high-impact news blackout. ALL enabled gates must pass -- one market
     order in the hypothesis direction, RR-based TP off an ATR-based SL.
  3. Skip entirely if a position already exists for (account, symbol, magic)
     -- one position per symbol per account, ever. No grid, no martingale,
     no pyramiding.
  4. Exit management is NOT this file's job: give the position its own
     magic and ladder_guard.py (STEPS_BY_CLASS["METAL"]/["CRYPTO"], already
     live and already proven on swing_pending_bot/London Breakout) takes it
     from there -- same reuse principle as swing_pending_bot.

All thresholds (EMA/ADX/RSI/ATR/RR) live in gold_btc_bot_config.json, NOT
hardcoded here -- Ahmed 2026-08-04: today's defaults are initial hypotheses,
not proven values; gold_btc_bot_backtest.py is what determines the final
numbers, by testing each gate's contribution independently before any of
this ever touches a live account. active_accounts in the config gates the
pilot rollout (EA only until reviewed) without needing a code change.

Usage:
    python gold_btc_bot.py --test              (self-test, no MT5 needed)
    python gold_btc_bot.py <ACCOUNT>            (live loop, e.g. "EA")
"""
import atexit
import copy
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

import MetaTrader5 as mt5
from ladder_guard import ACCOUNTS  # reuse the one place credentials live
from heartbeat import write_heartbeat  # 2026-08-06: functional heartbeat rollout, see NOTIFICATION_POLICY.md
from bot_period_guard import manual_block_reason  # 2026-08-22: per-account manual entry block (EM low-equity)
                                        # (code-only patch -- this bot is not currently launched, status
                                        # unresolved per [[project-swing-pending-bot-halt]]; not tested live)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gold_btc_bot_config.json")
TRACE_LOG_DIR = os.path.dirname(os.path.abspath(__file__))

TIMEFRAME_MAP = {"M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1}

NEWS_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NEWS_CACHE_TTL_SEC = 1800
_news_cache = {"ts": 0.0, "events": []}
INIT_RETRY_BASE = 5
INIT_RETRY_MAX = 60

# --- capital-protection gates (Ahmed 2026-08-03) ---------------------------
# Read-only, operational-only: these only decide whether NEW entries are
# allowed THIS cycle. They never touch evaluate_signal/check_symbol's own
# entry/exit logic, filters, or thresholds -- an existing open position is
# always managed normally by its own SL/TP and by ladder_guard.py regardless
# of what these gates decide.
RISK_HALT_FLAG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "RISK_HALT.flag")
KILL_SWITCH_PATH_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gold_btc_bot_HALT_{acc}.flag")
BASELINE_PATH_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gold_btc_bot_baseline_{acc}.json")
EQUITY_FLOOR_PCT = 70.0  # halt new entries if equity < 70% of this account's recorded Pilot-start
                         # baseline -- wider than the ~5-6 loss streak that's normal variance at this
                         # win rate, so it only trips on genuinely abnormal drawdown (Ahmed's choice).


REQUIRED_TOP_KEYS = ["magic", "active_accounts", "symbols_by_account", "lot",
                     "check_interval_sec", "filters_enabled", "trend", "momentum", "atr", "risk"]


class ConfigError(ValueError):
    """Raised on a structurally invalid config -- a clear, single error at
    load time instead of a random KeyError deep inside evaluate_signal."""


def validate_config(cfg):
    """2026-08-04 code review (Ahmed): every threshold lives in the JSON
    config by design, which means a missing/malformed file must fail loudly
    and specifically at load time, not as an obscure KeyError three calls
    deep. Also fills in safe defaults for genuinely optional keys."""
    missing = [k for k in REQUIRED_TOP_KEYS if k not in cfg]
    if missing:
        raise ConfigError(f"missing required top-level key(s): {missing}")
    for k in ("timeframe", "ema_fast", "ema_slow", "adx_period", "adx_min"):
        if k not in cfg["trend"]:
            raise ConfigError(f"config['trend'] missing required key: {k!r}")
    if not isinstance(cfg["trend"]["adx_min"], dict) or "default" not in cfg["trend"]["adx_min"]:
        raise ConfigError("config['trend']['adx_min'] must be a dict with a 'default' fallback "
                           "value (per-symbol ADX thresholds, same pattern as risk.sl_atr_mult)")
    for k in ("timeframe", "rsi_period", "long_min", "long_max", "short_min", "short_max"):
        if k not in cfg["momentum"]:
            raise ConfigError(f"config['momentum'] missing required key: {k!r}")
    for k in ("timeframe", "period", "avg_period"):
        if k not in cfg["atr"]:
            raise ConfigError(f"config['atr'] missing required key: {k!r}")
    for k in ("sl_atr_mult", "rr_min"):
        if k not in cfg["risk"]:
            raise ConfigError(f"config['risk'] missing required key: {k!r}")
    if "default" not in cfg["risk"]["sl_atr_mult"]:
        raise ConfigError("config['risk']['sl_atr_mult'] must include a 'default' fallback value")
    for tf in (cfg["trend"]["timeframe"], cfg["momentum"]["timeframe"], cfg["atr"]["timeframe"]):
        if tf not in TIMEFRAME_MAP:
            raise ConfigError(f"unknown timeframe {tf!r} -- must be one of {list(TIMEFRAME_MAP)}")
    # safe defaults for genuinely optional keys -- absence is fine, garbage isn't
    cfg.setdefault("news_symbols", {})
    cfg.setdefault("news_blackout_before_min", 60)
    cfg.setdefault("news_blackout_after_min", 30)
    for gate in ("trend", "momentum", "atr", "news_blackout"):
        cfg["filters_enabled"].setdefault(gate, True)
    return cfg


def load_config(path=CONFIG_PATH):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    return validate_config(cfg)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _connect_mt5(account_cfg, acc):
    delay = INIT_RETRY_BASE
    while True:
        mt5.shutdown()
        if mt5.initialize(**account_cfg):
            return
        log(f"{acc} mt5.initialize FAILED: {mt5.last_error()} -- retrying in {delay}s")
        time.sleep(delay)
        delay = min(INIT_RETRY_MAX, delay * 2)


def _trace_base(acc, symbol, price=None):
    """Shared fields for every funnel trace record (2026-08-20, Ahmed-approved,
    instrumentation-only helper -- no decision logic here). risk_halt_active
    is always False here: every call site is inside check_symbol(), which
    only runs after this cycle's per-account RISK_HALT check already passed."""
    return {"logged_at": datetime.now(timezone.utc).isoformat(), "account": acc, "symbol": symbol,
            "price": price, "risk_halt_active": False}


def _first_failed_gate(gates):
    """Pure helper (2026-08-20, instrumentation-only): names the first gate
    that failed, in the same order evaluate_signal() already checks them.
    Never used in any trading decision -- purely descriptive for the trace."""
    if "data" in gates:
        return gates["data"]
    if gates.get("direction_hypothesis") is None:
        return "no_direction (ema_fast == ema_slow)"
    if gates.get("trend_gate_passed") is False:
        return "trend_gate_failed"
    if gates.get("momentum_gate_passed") is False:
        return "momentum_gate_failed"
    if gates.get("atr_gate_passed") is False:
        return "atr_gate_failed"
    return None


def _test_first_failed_gate():
    assert _first_failed_gate({"data": "insufficient input rates"}) == "insufficient input rates"
    assert _first_failed_gate({"direction_hypothesis": None}) == "no_direction (ema_fast == ema_slow)"
    assert _first_failed_gate({"direction_hypothesis": "buy", "trend_gate_passed": False}) == "trend_gate_failed"
    assert _first_failed_gate({"direction_hypothesis": "buy", "trend_gate_passed": True,
                                "momentum_gate_passed": False}) == "momentum_gate_failed"
    assert _first_failed_gate({"direction_hypothesis": "buy", "trend_gate_passed": True,
                                "momentum_gate_passed": True, "atr_gate_passed": False}) == "atr_gate_failed"
    assert _first_failed_gate({"direction_hypothesis": "buy", "trend_gate_passed": True,
                                "momentum_gate_passed": True, "atr_gate_passed": True}) is None
    print("gold_btc_bot _first_failed_gate self-check: OK")


def _trace_path():
    # 2026-08-20 (Ahmed-approved, observability-only): one file per UTC day --
    # keeps a single file from growing unbounded while never losing history
    # across a restart (same day resumes the same file; a new day starts a
    # fresh one automatically, nothing to delete/expire).
    return os.path.join(TRACE_LOG_DIR, f"gold_btc_trace_{datetime.now(timezone.utc):%Y-%m-%d}.jsonl")


def _write_trace(record):
    """Durable, log-independent per-signal record -- same rationale/pattern
    as fx_signal_exec.py's trace file (2026-08-04): entry reason, every
    gate's pass/fail value, and exit are never solely dependent on the
    regular print log surviving."""
    try:
        with open(_trace_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass


# --- asset classification (mirrors ladder_guard.classify_asset) ------------

def asset_class(symbol):
    s = symbol.upper()
    if s.startswith("BTC"):
        return "BTC"
    if s.startswith("XAU"):
        return "XAU"
    if s.startswith(("USOIL", "UKOIL", "USOUSD", "UKOUSD")):
        return "OIL"
    return "OTHER"


# --- indicators (hand-rolled, no new dependency -- numpy already used
# elsewhere in this codebase e.g. swing_pending_bot.py's self-test) --------

def _ema_series(closes, period):
    """Standard EMA: seeded with the SMA of the first `period` closes, then
    smoothed forward. Returns a list the same length as `closes` (the first
    `period`-1 entries are None -- not enough data yet)."""
    n = len(closes)
    out = [None] * n
    if n < period:
        return out
    k = 2.0 / (period + 1)
    seed = sum(closes[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = closes[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _true_ranges(rates):
    trs = []
    prev_close = rates[0]["close"]
    for row in rates[1:]:
        h, l, c = row["high"], row["low"], row["close"]
        trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
        prev_close = c
    return trs


def rsi(closes, period):
    """Simple (non-Wilder) RSI -- consistent in style with this codebase's
    existing ATR helpers (plain averages, not exponential smoothing).
    Returns None if there isn't enough data."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[-i] - closes[-i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def adx(rates, period):
    """Standard Wilder ADX/+DI/-DI off a closed-bar rate window. `rates`
    must have at least 2*period+1 bars (period to seed the Wilder smoothing,
    period more to stabilize ADX itself). Returns (adx, plus_di, minus_di)
    or (None, None, None) if there isn't enough data."""
    n = len(rates)
    if n < 2 * period + 1:
        return None, None, None

    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, n):
        up = rates[i]["high"] - rates[i - 1]["high"]
        down = rates[i - 1]["low"] - rates[i]["low"]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        trs.append(max(
            rates[i]["high"] - rates[i]["low"],
            abs(rates[i]["high"] - rates[i - 1]["close"]),
            abs(rates[i]["low"] - rates[i - 1]["close"]),
        ))

    def wilder_smooth(values, period):
        smoothed = [sum(values[:period])]
        for v in values[period:]:
            smoothed.append(smoothed[-1] - smoothed[-1] / period + v)
        return smoothed

    tr_s = wilder_smooth(trs, period)
    pdm_s = wilder_smooth(plus_dm, period)
    mdm_s = wilder_smooth(minus_dm, period)

    dx_values = []
    for tr_v, pdm_v, mdm_v in zip(tr_s, pdm_s, mdm_s):
        if tr_v == 0:
            dx_values.append(0.0)
            continue
        pdi = 100 * pdm_v / tr_v
        mdi = 100 * mdm_v / tr_v
        denom = pdi + mdi
        dx_values.append(0.0 if denom == 0 else 100 * abs(pdi - mdi) / denom)

    if len(dx_values) < period:
        return None, None, None
    adx_val = sum(dx_values[:period]) / period
    for dx_v in dx_values[period:]:
        adx_val = (adx_val * (period - 1) + dx_v) / period

    last_tr, last_pdm, last_mdm = tr_s[-1], pdm_s[-1], mdm_s[-1]
    plus_di = 100 * last_pdm / last_tr if last_tr else 0.0
    minus_di = 100 * last_mdm / last_tr if last_tr else 0.0
    return adx_val, plus_di, minus_di


# --- news blackout (same pattern as swing_pending_bot._news_blackout) -----

def _refresh_news_cache():
    if time.time() - _news_cache["ts"] < NEWS_CACHE_TTL_SEC:
        return
    try:
        # 2026-08-15: bare urlopen got 403 Forbidden every cycle for hours --
        # the CDN in front of this feed blocks Python's default User-Agent.
        # A normal browser-like UA is enough; no auth/key involved.
        req = urllib.request.Request(NEWS_CALENDAR_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            _news_cache["events"] = json.loads(r.read())
    except Exception as e:
        log(f"news calendar fetch failed: {str(e)[:150]} -- keeping last-known calendar")
    _news_cache["ts"] = time.time()


def news_blackout(symbol, cfg, now=None):
    ccys = cfg.get("news_symbols", {}).get(symbol)
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
        if -cfg["news_blackout_after_min"] <= delta_min <= cfg["news_blackout_before_min"]:
            return True
    return False


# --- gate evaluation (pure function: bars in, decision + reasons out) -----

def evaluate_signal(symbol, h1_rates, m15_rates, cfg):
    """Pure decision function -- no MT5 calls, no I/O, so it's directly
    reusable by both the live bot and the backtester with identical logic.
    Returns a dict: {"direction": "buy"/"sell"/None, "passed": bool,
    "gates": {...per-gate pass/fail + raw values...}, "sl_dist": float|None}
    """
    # 2026-08-04 code review: defensive against a future/different caller
    # that doesn't already length-guard its input the way check_symbol() and
    # the backtester both do -- an empty/near-empty window must return a
    # clean "not enough data" result, never an IndexError from _true_ranges.
    if h1_rates is None or m15_rates is None or len(h1_rates) == 0 or len(m15_rates) < 2:
        return {"direction": None, "passed": False, "gates": {"data": "insufficient input rates"}, "sl_dist": None}

    closes_h1 = [r["close"] for r in h1_rates]
    fe = cfg["filters_enabled"]
    gates = {}

    ema_fast = _ema_series(closes_h1, cfg["trend"]["ema_fast"])
    ema_slow = _ema_series(closes_h1, cfg["trend"]["ema_slow"])
    if ema_fast[-1] is None or ema_slow[-1] is None:
        return {"direction": None, "passed": False, "gates": {"data": "insufficient H1 history"}, "sl_dist": None}

    direction = "buy" if ema_fast[-1] > ema_slow[-1] else ("sell" if ema_fast[-1] < ema_slow[-1] else None)
    gates["ema_fast"] = ema_fast[-1]
    gates["ema_slow"] = ema_slow[-1]
    gates["direction_hypothesis"] = direction
    if direction is None:
        return {"direction": None, "passed": False, "gates": gates, "sl_dist": None}

    passed = True

    # 2026-08-03 (Ahmed, backtest evidence): ADX min is per-symbol now, not one
    # shared value -- 180-day backtest showed 25 leaves real XAU profit on the
    # table (every lower threshold tested beat it) while BTC stayed flat
    # regardless of threshold, so only XAU's value actually changed (25->22).
    adx_min_cfg = cfg["trend"]["adx_min"]
    cls = asset_class(symbol)
    adx_min = adx_min_cfg.get(cls, adx_min_cfg["default"])
    gates["adx_min_used"] = adx_min

    adx_val, plus_di, minus_di = adx(h1_rates, cfg["trend"]["adx_period"])
    gates["adx"] = adx_val
    if fe.get("trend", True):
        if adx_val is None or adx_val < adx_min:
            passed = False
        gates["trend_gate_passed"] = bool(adx_val is not None and adx_val >= adx_min)
    else:
        gates["trend_gate_passed"] = "disabled"

    rsi_val = rsi(closes_h1, cfg["momentum"]["rsi_period"])
    gates["rsi"] = rsi_val
    if fe.get("momentum", True):
        mom_cfg = cfg["momentum"]
        if rsi_val is None:
            mom_ok = False
        elif direction == "buy":
            mom_ok = mom_cfg["long_min"] <= rsi_val <= mom_cfg["long_max"]
        else:
            mom_ok = mom_cfg["short_min"] <= rsi_val <= mom_cfg["short_max"]
        gates["momentum_gate_passed"] = mom_ok
        passed = passed and mom_ok
    else:
        gates["momentum_gate_passed"] = "disabled"

    closes_m15 = [r["close"] for r in m15_rates]
    atr_period = cfg["atr"]["period"]
    avg_period = cfg["atr"]["avg_period"]
    trs = _true_ranges(m15_rates)
    sl_dist = None
    if len(trs) >= atr_period:
        sl_dist = sum(trs[-atr_period:]) / atr_period
        gates["atr"] = sl_dist
        if fe.get("atr", True):
            if len(trs) >= avg_period:
                atr_avg = sum(trs[-avg_period:]) / avg_period
                atr_ok = sl_dist >= atr_avg
            else:
                atr_ok = False
            gates["atr_gate_passed"] = atr_ok
            passed = passed and atr_ok
        else:
            gates["atr_gate_passed"] = "disabled"
    else:
        gates["atr"] = None
        gates["atr_gate_passed"] = False
        passed = False

    return {"direction": direction, "passed": bool(passed), "gates": gates, "sl_dist": sl_dist}


# --- capital-protection gates -----------------------------------------------

def _load_or_init_baseline(acc, current_equity):
    """First call for an account snapshots current equity as the Pilot-start
    baseline and persists it; every later call returns that SAME stored value
    (never overwritten automatically) -- so the equity floor is always
    measured against one fixed reference point, not a moving target. Delete
    the file manually to intentionally reset the baseline (e.g. a new Pilot
    period)."""
    path = BASELINE_PATH_TEMPLATE.format(acc=acc)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)["baseline_equity"]
    data = {"account": acc, "baseline_equity": current_equity,
            "recorded_at": datetime.now(timezone.utc).isoformat()}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)
    return current_equity


def _risk_gates_ok(acc):
    """Read-only gate: may only return (False, reason) to block NEW entries
    this cycle. Never raises, never modifies any position, never touches
    filters/thresholds -- an existing open position keeps being managed by
    its own SL/TP and by ladder_guard.py exactly as if this function didn't
    exist."""
    block_reason = manual_block_reason("gold_btc_bot", acc)
    if block_reason:
        return False, block_reason
    try:
        _flag_dir = os.path.dirname(RISK_HALT_FLAG_PATH)
        if _flag_dir and not os.path.isdir(_flag_dir):
            return False, f"RISK_HALT flag dir unreadable ({_flag_dir}) -- failing closed"
        if os.path.exists(RISK_HALT_FLAG_PATH):
            return False, "portfolio-wide RISK_HALT.flag active (portfolio_risk_guard.py)"
    except OSError as _e:
        return False, f"RISK_HALT flag check error ({_e}) -- failing closed"
    if os.path.exists(KILL_SWITCH_PATH_TEMPLATE.format(acc=acc)):
        return False, f"manual kill switch active for {acc} (gold_btc_bot_HALT_{acc}.flag present)"
    info = mt5.account_info()
    if info is None:
        return False, f"account_info() failed ({mt5.last_error()}) -- failing safe, not trading this cycle"
    baseline = _load_or_init_baseline(acc, info.equity)
    floor = baseline * (EQUITY_FLOOR_PCT / 100.0)
    if info.equity < floor:
        return False, (f"equity guard: equity {info.equity:.2f} < floor {floor:.2f} "
                        f"({EQUITY_FLOOR_PCT:.0f}% of Pilot-start baseline {baseline:.2f})")
    return True, None


# --- order placement --------------------------------------------------------

class PositionCheckFailed(Exception):
    """Raised when positions_get() itself failed (returned None -- a real
    MT5/connection error) as opposed to genuinely returning zero positions
    (empty tuple). 2026-08-04 code review (Ahmed): collapsing these two
    states into 'no position' would let a transient connection hiccup during
    the dedup check open a SECOND position on top of one that already
    existed but couldn't be verified. Fail-safe means treating 'can't tell'
    as 'don't trade this cycle', never as 'assume it's clear.'"""


def _has_open_position(symbol, magic):
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        raise PositionCheckFailed(f"positions_get({symbol}) returned None: {mt5.last_error()}")
    return any(p.magic == magic for p in positions)


def check_symbol(acc, symbol, cfg):
    magic = cfg["magic"]
    try:
        if _has_open_position(symbol, magic):
            return  # one position per (account, symbol), ever -- no grid/pyramiding
    except PositionCheckFailed as e:
        log(f"{acc} {symbol} skipped -- can't verify existing position ({e}), failing safe (not trading this cycle)")
        return

    if cfg["filters_enabled"].get("news_blackout", True) and news_blackout(symbol, cfg):
        log(f"{acc} {symbol} skipped -- high-impact news blackout window")
        _write_trace({**_trace_base(acc, symbol), "decision": "REJECTED",
                      "rejection_reason": "news_blackout", "gates": None})
        return

    h1_tf = TIMEFRAME_MAP[cfg["trend"]["timeframe"]]
    m15_tf = TIMEFRAME_MAP[cfg["atr"]["timeframe"]]
    need_h1 = max(cfg["trend"]["ema_slow"], 2 * cfg["trend"]["adx_period"] + 1, cfg["momentum"]["rsi_period"] + 1) + 5
    need_m15 = max(cfg["atr"]["period"], cfg["atr"]["avg_period"]) + 5

    h1_rates = mt5.copy_rates_from_pos(symbol, h1_tf, 1, need_h1)
    m15_rates = mt5.copy_rates_from_pos(symbol, m15_tf, 1, need_m15)
    if h1_rates is None or len(h1_rates) < need_h1 or m15_rates is None or len(m15_rates) < need_m15:
        log(f"{acc} {symbol} skipped -- insufficient bar history")
        _write_trace({**_trace_base(acc, symbol), "decision": "NO_CANDIDATE",
                      "rejection_reason": "insufficient_bar_history", "gates": None})
        return

    decision = evaluate_signal(symbol, h1_rates, m15_rates, cfg)
    receive_time = datetime.now(timezone.utc)
    if not decision["passed"] or decision["sl_dist"] is None:
        # --- funnel observability (2026-08-20, Ahmed-approved, instrumentation-only):
        # evaluate_signal() already computed every gate above -- this just records
        # what was silently discarded before, it changes no gate/threshold/timing. ---
        _has_direction = decision["gates"].get("direction_hypothesis") is not None or decision.get("direction") is not None
        # 2026-08-20 (Ahmed-approved, observability-only): best-effort price
        # snapshot for the trace record only -- never used in any decision,
        # a failed/unavailable tick just leaves price=None (same fail-quiet
        # pattern _write_trace itself already uses).
        try:
            _rej_tick = mt5.symbol_info_tick(symbol)
            _rej_price = _rej_tick.bid if _rej_tick else None
        except Exception:
            _rej_price = None
        _write_trace({**_trace_base(acc, symbol, price=_rej_price),
                      "decision": "REJECTED" if _has_direction else "NO_CANDIDATE",
                      "direction": decision["direction"], "gates": decision["gates"],
                      "rejection_reason": _first_failed_gate(decision["gates"])})
        return  # gates not aligned this cycle -- quality over quantity, just wait

    direction = decision["direction"]
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        _write_trace({**_trace_base(acc, symbol), "decision": "REJECTED",
                      "rejection_reason": "tick_unavailable", "gates": decision["gates"]})
        return
    price = tick.ask if direction == "buy" else tick.bid

    cls = asset_class(symbol)
    sl_mult = cfg["risk"]["sl_atr_mult"].get(cls, cfg["risk"]["sl_atr_mult"]["default"])
    sl_dist = decision["sl_dist"] * sl_mult
    rr = cfg["risk"]["rr_min"]
    tp_dist = sl_dist * rr
    sl = price - sl_dist if direction == "buy" else price + sl_dist
    tp = price + tp_dist if direction == "buy" else price - tp_dist

    lot = cfg["lot"].get(acc)
    if lot is None:
        log(f"{acc} {symbol} skipped -- no lot size configured for account {acc!r} in cfg['lot']")
        return

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": magic,
        "comment": "gold_btc_bot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
    _write_trace({
        "receive_time": receive_time.isoformat(),
        "account": acc,
        "symbol": symbol,
        "price": price,
        # 2026-08-20 (Ahmed-approved, instrumentation-only): standardized
        # funnel fields added alongside the original ones below -- nothing
        # removed, nothing changed in what actually got submitted.
        "decision": "FILLED" if ok else "ORDER_FAILED",
        "risk_eligible": True, "risk_halt_active": False,
        "direction": direction,
        "gates": decision["gates"],
        "sl": sl, "tp": tp, "rr": rr,
        "retcode": result.retcode if result else None,
        "order_ticket": result.order if ok else None,
    })
    log(f"{acc} {symbol} {direction.upper()} @ {price} SL={sl:.5f} TP={tp:.5f} "
        f"(ADX={decision['gates'].get('adx')}, RSI={decision['gates'].get('rsi')}) "
        f"({'OK' if ok else f'FAIL {result.retcode if result else None}'})")


LOCK_PATH_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gold_btc_bot_{acc}.lock")
LOCK_STALE_MULTIPLIER = 3  # a lock older than this many check-intervals is presumed dead


def _touch_lock(acc):
    tmp = LOCK_PATH_TEMPLATE.format(acc=acc) + ".tmp"
    path = LOCK_PATH_TEMPLATE.format(acc=acc)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "ts": time.time()}, f)
    os.replace(tmp, path)


def _pid_alive(pid):
    """Stdlib-only Windows PID liveness check (no psutil dependency).

    2026-08-07 bug fix: a plain 'does this PID exist' check (tasklist /FI
    "PID eq N") is NOT enough -- Windows recycles PIDs aggressively on a
    busy machine, and a Restart Simulation just proved it: PID 16624
    belonged to a just-killed gold_btc_bot, but within seconds Windows had
    already reassigned that exact PID to an unrelated cmd.exe/
    ladder_guard_ba.bat process, so the naive check reported "alive" and
    the real bot was refused a restart. Fix: also require the PID's OWN
    command line to actually mention gold_btc_bot.py -- confirms it's
    genuinely the same kind of process, not a coincidental PID reuse."""
    try:
        out = subprocess.run(
            ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
        ).stdout
        return "gold_btc_bot.py" in out
    except Exception:
        return None  # unknown -- don't claim it's dead on a tooling failure


def _release_lock(acc):
    """Graceful-shutdown cleanup (2026-08-07 op-fix, Ahmed-approved): remove
    OUR OWN lock on clean exit so a deliberate restart doesn't have to wait
    out the stale-timeout at all. Only removes it if it still holds our
    pid -- never touch a lock a newer instance has since legitimately
    acquired (e.g. if this process was merely slow to exit after losing
    the race)."""
    path = LOCK_PATH_TEMPLATE.format(acc=acc)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("pid") == os.getpid():
            os.remove(path)
    except Exception:
        pass  # nothing to clean up, or already gone -- fine either way


def _acquire_lock(acc, check_interval_sec):
    """Heartbeat-based instance lock (same atomic-write philosophy as
    claude_loop_heartbeat.py) -- 2026-08-04 code review: prevents two
    processes for the SAME account ever running blind to each other's
    positions, exactly the accident this session already had once with
    manual ea_shield/ladder_guard restarts. A stale timestamp (older than a
    few check cycles) is treated as a dead process and taken over
    automatically -- a hard crash can never permanently block a legitimate
    restart, unlike a plain PID-file lock.

    2026-08-07 op-fix (Ahmed-approved, operational only -- no entry/exit/
    risk/threshold/lot/SL/TP logic touched): a force-killed process (e.g. a
    deliberate Restart Simulation, not a crash) left the time-based check
    waiting out the full stale window even though nothing was actually
    running. Now also checks whether the lock's owning PID is still alive;
    a dead PID is stale immediately regardless of timestamp age."""
    path = LOCK_PATH_TEMPLATE.format(acc=acc)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            owner_alive = _pid_alive(data.get("pid"))
            if owner_alive is False:
                pass  # confirmed dead -- fall through and take over immediately
            else:
                age = time.time() - data["ts"]
                if age < check_interval_sec * LOCK_STALE_MULTIPLIER:
                    return False, data
        except Exception:
            pass  # unreadable/corrupt lock file -- treat as stale, take over
    _touch_lock(acc)
    return True, None


def main():
    acc = sys.argv[1] if len(sys.argv) > 1 else "EA"
    cfg = load_config()
    if acc not in cfg["active_accounts"]:
        log(f"{acc} is not in active_accounts {cfg['active_accounts']} -- exiting, "
            f"nothing to do (pilot gating, see gold_btc_bot_config.json)")
        return
    ok, existing = _acquire_lock(acc, cfg["check_interval_sec"])
    if not ok:
        log(f"{acc} ABORT -- another gold_btc_bot instance for this account looks alive "
            f"(lock heartbeat {time.time() - existing['ts']:.0f}s old, pid={existing.get('pid')}). "
            f"Refusing to start a second one.")
        return
    atexit.register(_release_lock, acc)  # graceful-shutdown cleanup, see _release_lock docstring
    account_cfg = ACCOUNTS[acc]
    symbols = cfg["symbols_by_account"].get(acc, [])
    _connect_mt5(account_cfg, acc)
    log(f"gold_btc_bot started on {acc} -- symbols {symbols}, magic={cfg['magic']}, "
        f"every {cfg['check_interval_sec']}s")
    log(f"resolved paths (absolute): flag={RISK_HALT_FLAG_PATH} lock={LOCK_PATH_TEMPLATE.format(acc=acc)}")
    while True:
        try:
            cfg = load_config()  # re-read each cycle -- thresholds tunable without a restart
        except Exception as e:
            # 2026-08-04 code review (Ahmed): a transient read (e.g. mid-edit)
            # or a corrupted file must NOT crash the process or trade on a
            # half-parsed config -- keep running on the last-known-good cfg.
            log(f"config reload FAILED ({str(e)[:150]}) -- keeping last-known-good config this cycle")
        _touch_lock(acc)
        # 2026-08-07: heartbeat must reflect process liveness, not trading
        # activity -- writing it only after a successful (non-halted) cycle
        # meant any portfolio-wide RISK_HALT correctly blocking entries also
        # silently starved this bot's heartbeat, making a healthy, correctly
        # -behaving bot look "down" to alert_manager.py. Write it every loop
        # iteration regardless of the risk-gate outcome.
        write_heartbeat(f"gold_btc_bot_{acc}", symbols_checked=len(symbols))
        risk_ok, risk_reason = _risk_gates_ok(acc)
        if not risk_ok:
            log(f"{acc} risk gate BLOCKED new entries this cycle -- {risk_reason} "
                f"(any open position still managed normally by its own SL/TP and ladder_guard.py)")
            # 2026-08-20 (Ahmed-approved, instrumentation-only): one record per
            # blocked cycle -- not per symbol, since no symbol was even evaluated.
            _write_trace({**_trace_base(acc, None), "decision": "REJECTED",
                          "rejection_reason": risk_reason, "gates": None,
                          "risk_halt_active": os.path.exists(RISK_HALT_FLAG_PATH),
                          "risk_eligible": False})
            if risk_reason and risk_reason.startswith("account_info() failed"):
                # 2026-08-16: _connect_mt5() only ran once at startup -- if the
                # MT5 terminal restarts later (as it did this morning), the
                # bot's IPC session goes stale forever and it silently idles
                # (fail-safe, but never recovers) until a manual restart.
                # ladder_guard.py already self-heals this way; mirror it here.
                log(f"{acc} attempting MT5 reconnect (terminal likely restarted)")
                _connect_mt5(account_cfg, acc)
            time.sleep(cfg["check_interval_sec"])
            continue
        for symbol in symbols:
            try:
                check_symbol(acc, symbol, cfg)
            except Exception as e:
                log(f"{acc} {symbol} CHECK_FAILED: {str(e)[:150]} -- other symbols unaffected")
        time.sleep(cfg["check_interval_sec"])


def _demo():
    """python gold_btc_bot.py --test -- asserts indicator correctness and
    gate modularity (each gate can independently block/allow a signal),
    without touching real MT5."""
    from types import SimpleNamespace as NS
    import numpy as np

    _RATE_DTYPE = np.dtype([
        ("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"),
        ("close", "f8"), ("tick_volume", "i8"), ("spread", "i4"), ("real_volume", "i8"),
    ])

    def make_rates(rows):
        arr = np.zeros(len(rows), dtype=_RATE_DTYPE)
        for i, (h, l, c) in enumerate(rows):
            arr[i] = (i, c, h, l, c, 0, 0, 0)
        return arr

    # --- EMA: simple uptrend must give fast > slow ---
    closes_up = [100 + i * 0.5 for i in range(210)]
    ema_fast = _ema_series(closes_up, 50)
    ema_slow = _ema_series(closes_up, 200)
    assert ema_fast[-1] > ema_slow[-1], "steady uptrend must give EMA50 > EMA200"
    closes_down = [300 - i * 0.5 for i in range(210)]
    ema_fast_d = _ema_series(closes_down, 50)
    ema_slow_d = _ema_series(closes_down, 200)
    assert ema_fast_d[-1] < ema_slow_d[-1], "steady downtrend must give EMA50 < EMA200"
    print("gold_btc_bot EMA selftest OK")

    # --- RSI: monotonic up = 100 (no losses), monotonic down = 0 (no gains) ---
    assert abs(rsi(closes_up[-20:], 14) - 100.0) < 1e-6, "pure uptrend RSI must be 100"
    assert abs(rsi(closes_down[-20:], 14) - 0.0) < 1e-6, "pure downtrend RSI must be 0"
    print("gold_btc_bot RSI selftest OK")

    # --- ADX: a strong clean trend must score high and correctly on direction ---
    trend_rows = [(100 + i * 2 + 1, 100 + i * 2 - 1, 100 + i * 2) for i in range(60)]
    trend_rates = make_rates(trend_rows)
    adx_val, pdi, mdi = adx(trend_rates, 14)
    assert adx_val is not None and adx_val > 25, f"a clean strong uptrend must score ADX>25, got {adx_val}"
    assert pdi > mdi, "a clean uptrend must show +DI > -DI"
    choppy_rows = [(105, 95, 100 + (1 if i % 2 == 0 else -1)) for i in range(60)]
    choppy_rates = make_rates(choppy_rows)
    adx_choppy, _, _ = adx(choppy_rates, 14)
    assert adx_choppy is not None and adx_choppy < 20, f"pure chop must score a low ADX, got {adx_choppy}"
    print(f"gold_btc_bot ADX selftest OK (trend={adx_val:.1f}, choppy={adx_choppy:.1f})")

    # --- gate modularity: disabling a gate must remove its veto power ---
    cfg = {
        "filters_enabled": {"trend": True, "momentum": True, "atr": True, "news_blackout": False},
        "trend": {"timeframe": "H1", "ema_fast": 50, "ema_slow": 200, "adx_period": 14,
                  "adx_min": {"XAU": 22, "BTC": 25, "default": 25}},
        "momentum": {"timeframe": "H1", "rsi_period": 14, "long_min": 50, "long_max": 70, "short_min": 30, "short_max": 50},
        "atr": {"timeframe": "M15", "period": 14, "avg_period": 20},
        "risk": {"sl_atr_mult": {"XAU": 1.3, "BTC": 1.5, "default": 1.4}, "rr_min": 2.5},
        "news_blackout_before_min": 60, "news_blackout_after_min": 30, "news_symbols": {},
    }
    # uptrend on H1 with occasional small pullback ticks mixed in -- a pure
    # monotonic climb gives RSI=100 (maximally overbought), which the
    # momentum gate correctly REJECTS by design (not "healthy momentum,
    # room to run", but "already fully extended"); realistic bull markets
    # breathe, so the test data should too.
    up_closes = []
    price = 100.0
    for i in range(210):
        price += -0.6 if i % 3 == 2 else 0.6  # 2-up/1-down repeating -> net uptrend, moderate RSI
        up_closes.append(price)
    h1_up = make_rates([(c + 0.3, c - 0.3, c) for c in up_closes])
    # M15 with rising true range (ATR climbing above its own recent average)
    m15_rows = [(100 + i * 0.05, 100 - i * 0.05, 100) for i in range(15)] + \
               [(100 + i * 0.3, 100 - i * 0.3, 100) for i in range(30)]
    m15_active = make_rates(m15_rows)

    d_all_on = evaluate_signal("XAUUSDm", h1_up, m15_active, cfg)
    assert d_all_on["direction"] == "buy"
    assert d_all_on["passed"] is True, f"all gates should pass on a clean qualifying uptrend: {d_all_on['gates']}"

    cfg_no_atr = json.loads(json.dumps(cfg))
    cfg_no_atr["filters_enabled"]["atr"] = False
    # was volatile (TR=1.0), has gone quiet recently (TR=0.02) -- the last 14
    # bars (current ATR) must read lower than the last 20 (rolling average),
    # so the gate has something real to reject when enabled.
    m15_quiet = make_rates([(100.5, 99.5, 100) for _ in range(31)] + [(100.01, 99.99, 100) for _ in range(14)])
    d_atr_off = evaluate_signal("XAUUSDm", h1_up, m15_quiet, cfg_no_atr)
    assert d_atr_off["gates"]["atr_gate_passed"] == "disabled"
    d_atr_on = evaluate_signal("XAUUSDm", h1_up, m15_quiet, cfg)
    assert d_atr_on["passed"] is False, "flat M15 volatility must fail the ATR gate when enabled"
    print("gold_btc_bot gate-modularity selftest OK")

    # --- per-symbol ADX threshold: XAU must use 22, BTC must use 25, same cfg ---
    # 2026-08-03 (Ahmed): confirms the lookup picks the right threshold per
    # symbol's asset class, independent of whatever ADX value the bars
    # produce -- checks the SELECTION logic directly via gates["adx_min_used"]
    # rather than needing to engineer bars that sit exactly between 22 and 25.
    d_xau = evaluate_signal("XAUUSDm", h1_up, m15_active, cfg)
    d_btc = evaluate_signal("BTCUSDm", h1_up, m15_active, cfg)
    assert asset_class("USOILm") == "OIL", f"USOILm must classify as OIL, got {asset_class('USOILm')}"
    assert d_xau["gates"]["adx_min_used"] == 22, f"XAU must use adx_min=22, got {d_xau['gates']['adx_min_used']}"
    assert d_btc["gates"]["adx_min_used"] == 25, f"BTC must use adx_min=25, got {d_btc['gates']['adx_min_used']}"
    d_other = evaluate_signal("EURUSDm", h1_up, m15_active, cfg)
    assert d_other["gates"]["adx_min_used"] == 25, \
        f"an unmapped symbol must fall back to adx_min['default'], got {d_other['gates']['adx_min_used']}"
    print("gold_btc_bot per-symbol-adx selftest OK "
          f"(XAU={d_xau['gates']['adx_min_used']}, BTC={d_btc['gates']['adx_min_used']})")

    # --- one-position-per-symbol dedup ---
    mt5.positions_get = lambda symbol=None: (NS(magic=996600),)
    assert _has_open_position("XAUUSDm", 996600) is True
    mt5.positions_get = lambda symbol=None: ()
    assert _has_open_position("XAUUSDm", 996600) is False
    print("gold_btc_bot dedup selftest OK")

    # --- 2026-08-04 code review: positions_get()==None must fail SAFE, not
    # silently look like "no position exists" ---
    mt5.positions_get = lambda symbol=None: None
    try:
        _has_open_position("XAUUSDm", 996600)
        assert False, "positions_get() returning None must raise PositionCheckFailed, not swallow it"
    except PositionCheckFailed:
        pass
    # check_symbol must catch that and return WITHOUT ever reaching order_send
    order_calls = []
    mt5.order_send = lambda req: order_calls.append(req)
    check_symbol("EA", "XAUUSDm", {
        "magic": 996600, "filters_enabled": {"news_blackout": False},
        "trend": {"timeframe": "H1", "ema_fast": 50, "ema_slow": 200, "adx_period": 14,
                  "adx_min": {"XAU": 22, "BTC": 25, "default": 25}},
        "momentum": {"rsi_period": 14, "long_min": 50, "long_max": 70, "short_min": 30, "short_max": 50},
        "atr": {"timeframe": "M15", "period": 14, "avg_period": 20},
        "risk": {"sl_atr_mult": {"default": 1.4}, "rr_min": 2.5}, "lot": {"EA": 0.01},
    })
    assert not order_calls, "an unverifiable position check must never fall through to placing an order"
    print("gold_btc_bot positions-get-failsafe selftest OK")

    # --- config validation: required keys enforced, optional keys defaulted ---
    good_cfg = {
        "magic": 996600, "active_accounts": ["EA"], "symbols_by_account": {"EA": ["XAUUSDm"]},
        "lot": {"EA": 0.01}, "check_interval_sec": 900,
        "filters_enabled": {"trend": True, "momentum": True, "atr": True, "news_blackout": True},
        "trend": {"timeframe": "H1", "ema_fast": 50, "ema_slow": 200, "adx_period": 14,
                  "adx_min": {"XAU": 22, "BTC": 25, "default": 25}},
        "momentum": {"timeframe": "H1", "rsi_period": 14, "long_min": 50, "long_max": 70, "short_min": 30, "short_max": 50},
        "atr": {"timeframe": "M15", "period": 14, "avg_period": 20},
        "risk": {"sl_atr_mult": {"XAU": 1.3, "default": 1.4}, "rr_min": 2.5},
    }
    validate_config(copy.deepcopy(good_cfg))  # must not raise
    for missing_key in ("magic", "trend", "risk"):
        broken = copy.deepcopy(good_cfg)
        del broken[missing_key]
        try:
            validate_config(broken)
            assert False, f"missing top-level key {missing_key!r} must raise ConfigError"
        except ConfigError:
            pass
    broken_risk = copy.deepcopy(good_cfg)
    del broken_risk["risk"]["sl_atr_mult"]["default"]
    try:
        validate_config(broken_risk)
        assert False, "sl_atr_mult without a 'default' fallback must raise ConfigError"
    except ConfigError:
        pass
    broken_adx = copy.deepcopy(good_cfg)
    del broken_adx["trend"]["adx_min"]["default"]
    try:
        validate_config(broken_adx)
        assert False, "adx_min without a 'default' fallback must raise ConfigError"
    except ConfigError:
        pass
    broken_adx_type = copy.deepcopy(good_cfg)
    broken_adx_type["trend"]["adx_min"] = 25  # old shared-value shape, no longer accepted
    try:
        validate_config(broken_adx_type)
        assert False, "a plain-number adx_min (old shared shape) must raise ConfigError"
    except ConfigError:
        pass
    no_news_cfg = copy.deepcopy(good_cfg)
    del no_news_cfg["filters_enabled"]["news_blackout"]
    validated = validate_config(no_news_cfg)
    assert validated["filters_enabled"]["news_blackout"] is True, "missing optional gate flag must default to True (safe: gate stays active)"
    assert validated["news_blackout_before_min"] == 60, "missing optional news timing must get a safe default"
    print("gold_btc_bot config-validation selftest OK")

    # --- instance lock: fresh lock blocks a second start; stale lock (crash) doesn't ---
    import tempfile
    global LOCK_PATH_TEMPLATE
    real_template = LOCK_PATH_TEMPLATE
    tmp_dir = tempfile.mkdtemp()
    LOCK_PATH_TEMPLATE = os.path.join(tmp_dir, "test_{acc}.lock")
    try:
        ok1, _ = _acquire_lock("EA", check_interval_sec=10)
        assert ok1 is True, "first acquire on a clean start must succeed"
        ok2, existing = _acquire_lock("EA", check_interval_sec=10)
        assert ok2 is False, "a second acquire while the first lock is still fresh must be refused"
        assert existing is not None and "pid" in existing
        # simulate a crashed process: back-date the lock file past the staleness window
        lock_path = LOCK_PATH_TEMPLATE.format(acc="EA")
        with open(lock_path, "w", encoding="utf-8") as f:
            json.dump({"pid": 999999, "ts": time.time() - 10 * 30}, f)
        ok3, _ = _acquire_lock("EA", check_interval_sec=10)
        assert ok3 is True, "a stale (crashed-process) lock must be taken over automatically"
        # different account must never contend for the same lock
        ok4, _ = _acquire_lock("EM", check_interval_sec=10)
        assert ok4 is True, "different accounts must have independent locks"

        # 2026-08-07 op-fix: a FRESH-timestamp lock whose owning PID no longer
        # exists (e.g. force-killed during a deliberate restart, not a crash)
        # must be taken over immediately -- must NOT wait out the staleness
        # window just because the timestamp still looks recent.
        lock_path_ba = LOCK_PATH_TEMPLATE.format(acc="BA")
        with open(lock_path_ba, "w", encoding="utf-8") as f:
            json.dump({"pid": 999999, "ts": time.time()}, f)  # fresh ts, dead pid
        ok5, _ = _acquire_lock("BA", check_interval_sec=10)
        assert ok5 is True, "a fresh-timestamp lock with a dead owning PID must be taken over immediately"

        # 2026-08-07 regression test for the EXACT bug a live Restart
        # Simulation just caught: a PID Windows has already recycled for a
        # completely unrelated process must NOT be treated as "our bot is
        # still alive" just because a process with that PID number exists.
        # Spawn a short-lived, definitely-not-gold_btc_bot process and
        # confirm its PID is correctly reported as NOT alive-as-gold_btc_bot.
        decoy = subprocess.Popen(["cmd.exe", "/c", "timeout", "/t", "5"],
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            assert _pid_alive(decoy.pid) is False, \
                "a real running PID belonging to an unrelated process must not read as alive-as-gold_btc_bot"
        finally:
            decoy.kill()
            decoy.wait()

        # graceful shutdown: _release_lock must remove our own still-held lock...
        ok6, _ = _acquire_lock("EM", check_interval_sec=10)  # EM's lock from ok4 above is ours
        _release_lock("EM")
        assert not os.path.exists(LOCK_PATH_TEMPLATE.format(acc="EM")), \
            "graceful release must remove a lock we still own"
        # ...but must NEVER remove a lock a newer instance has since acquired.
        ok7, _ = _acquire_lock("EM", check_interval_sec=10)
        assert ok7 is True
        with open(LOCK_PATH_TEMPLATE.format(acc="EM"), "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid() + 1, "ts": time.time()}, f)  # simulate a newer owner
        _release_lock("EM")
        assert os.path.exists(LOCK_PATH_TEMPLATE.format(acc="EM")), \
            "release must never remove a lock now owned by a different pid"
    finally:
        LOCK_PATH_TEMPLATE = real_template
    print("gold_btc_bot instance-lock selftest OK")

    # --- capital-protection gates: RISK_HALT.flag, kill switch, equity guard ---
    global RISK_HALT_FLAG_PATH, KILL_SWITCH_PATH_TEMPLATE, BASELINE_PATH_TEMPLATE
    real_risk_flag, real_kill_tmpl, real_baseline_tmpl = (
        RISK_HALT_FLAG_PATH, KILL_SWITCH_PATH_TEMPLATE, BASELINE_PATH_TEMPLATE)
    tmp_dir2 = tempfile.mkdtemp()
    RISK_HALT_FLAG_PATH = os.path.join(tmp_dir2, "RISK_HALT.flag")
    KILL_SWITCH_PATH_TEMPLATE = os.path.join(tmp_dir2, "HALT_{acc}.flag")
    BASELINE_PATH_TEMPLATE = os.path.join(tmp_dir2, "baseline_{acc}.json")
    real_account_info = mt5.account_info
    try:
        mt5.account_info = lambda: NS(equity=100.0)
        ok, reason = _risk_gates_ok("EA")
        assert ok is True, f"clean state (no flags, equity at baseline) must pass: {reason}"

        # baseline auto-created at first equity seen (100.0), then must NOT move even
        # if account_info() reports a different equity on a later call
        mt5.account_info = lambda: NS(equity=250.0)
        ok, reason = _risk_gates_ok("EA")
        assert ok is True, "equity above its own recorded baseline must still pass"
        with open(BASELINE_PATH_TEMPLATE.format(acc="EA"), encoding="utf-8") as f:
            assert json.load(f)["baseline_equity"] == 100.0, \
                "baseline must stay pinned to the FIRST equity seen, never overwritten"

        # equity guard: below 70% of the 100.0 baseline must block
        mt5.account_info = lambda: NS(equity=65.0)
        ok, reason = _risk_gates_ok("EA")
        assert ok is False and "equity guard" in reason, f"equity below floor must block: {reason}"
        mt5.account_info = lambda: NS(equity=75.0)
        ok, reason = _risk_gates_ok("EA")
        assert ok is True, f"equity still above the floor must pass: {reason}"

        # portfolio-wide RISK_HALT.flag must block regardless of equity
        mt5.account_info = lambda: NS(equity=250.0)
        with open(RISK_HALT_FLAG_PATH, "w") as f:
            f.write("HALT test\n")
        ok, reason = _risk_gates_ok("EA")
        assert ok is False and "RISK_HALT" in reason, f"RISK_HALT.flag must block new entries: {reason}"
        os.remove(RISK_HALT_FLAG_PATH)
        ok, _ = _risk_gates_ok("EA")
        assert ok is True, "removing RISK_HALT.flag must let the gate pass again (auto-resume)"

        # per-account manual kill switch must block only ITS OWN account
        kill_path = KILL_SWITCH_PATH_TEMPLATE.format(acc="EA")
        with open(kill_path, "w") as f:
            f.write("halt\n")
        ok, reason = _risk_gates_ok("EA")
        assert ok is False and "kill switch" in reason, f"kill switch file must block EA: {reason}"
        ok, _ = _risk_gates_ok("EM")
        assert ok is True, "EA's kill switch file must NOT block a different account (EM)"
        os.remove(kill_path)
        ok, _ = _risk_gates_ok("EA")
        assert ok is True, "removing the kill switch file must let the gate pass again"

        # account_info() failure must fail SAFE (block), not silently pass
        mt5.account_info = lambda: None
        ok, reason = _risk_gates_ok("BA")
        assert ok is False, f"account_info() returning None must fail safe (block), got: {reason}"
    finally:
        mt5.account_info = real_account_info
        RISK_HALT_FLAG_PATH, KILL_SWITCH_PATH_TEMPLATE, BASELINE_PATH_TEMPLATE = (
            real_risk_flag, real_kill_tmpl, real_baseline_tmpl)
    print("gold_btc_bot risk-gates selftest OK")



def _selftest_pathfix():
    """Proves the RISK_HALT fix: paths are absolute & launch-invariant, all three
    accounts block when the flag is present, and the gate fails closed."""
    global RISK_HALT_FLAG_PATH
    import tempfile
    # (1) paths are absolute regardless of CWD (the bug was a relative dirname(__file__))
    expected = os.path.join(os.path.dirname(os.path.abspath(__file__)), "RISK_HALT.flag")
    assert RISK_HALT_FLAG_PATH == expected, (RISK_HALT_FLAG_PATH, expected)
    assert os.path.isabs(RISK_HALT_FLAG_PATH), "flag path must be absolute"
    assert os.path.isabs(LOCK_PATH_TEMPLATE), "lock path must be absolute"
    cwd0 = os.getcwd()
    try:
        os.chdir(tempfile.gettempdir())  # simulate a different-CWD / relative launch
        assert RISK_HALT_FLAG_PATH == expected, "flag path must stay correct from any CWD"
    finally:
        os.chdir(cwd0)
    # (2) flag present -> EA, EM, BA all blocked (mock account_info so eval never runs)
    real = RISK_HALT_FLAG_PATH
    tmpd = tempfile.mkdtemp()
    RISK_HALT_FLAG_PATH = os.path.join(tmpd, "RISK_HALT.flag")
    real_ai = mt5.account_info
    try:
        mt5.account_info = lambda: NS(equity=100.0)
        with open(RISK_HALT_FLAG_PATH, "w") as f:
            f.write("HALT test\n")
        for acc in ("EA", "EM", "BA"):
            ok, reason = _risk_gates_ok(acc)
            assert ok is False and "RISK_HALT.flag active" in reason, f"{acc} must block on flag: {reason}"
        os.remove(RISK_HALT_FLAG_PATH)
        # (3) fail-closed: point the flag at a non-existent directory -> blocked
        RISK_HALT_FLAG_PATH = os.path.join(tmpd, "does_not_exist_dir", "RISK_HALT.flag")
        for acc in ("EA", "EM", "BA"):
            ok, reason = _risk_gates_ok(acc)
            assert ok is False and "failing closed" in reason, f"{acc} must fail closed: {reason}"
    finally:
        mt5.account_info = real_ai
        RISK_HALT_FLAG_PATH = real
    print("gold_btc_bot pathfix selftest OK (absolute paths, 3-account halt, fail-closed)")

if __name__ == "__main__":
    if "--test" in sys.argv:
        _demo()
        _selftest_pathfix()
        _test_first_failed_gate()
    else:
        main()
