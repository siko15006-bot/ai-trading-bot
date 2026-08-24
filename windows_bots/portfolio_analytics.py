"""
portfolio_analytics.py -- READ-ONLY historical performance analytics.

Pulls closed-trade history via mt5.history_deals_get() (a read-only MT5
API call -- no order_send, no trading_stop, no position modification
anywhere in this file) and computes standard performance metrics per bot
(grouped by magic number) and per account. Imported by status_dashboard.py
for the new /analytics, /risk, /reports routes -- does not touch any
existing route, any bot file, or any trading logic.

Data availability, honestly stated:
  - MT5-side bots (EA/EM/BA, magic-tagged): full history available via
    mt5.history_deals_get().
  - BAA (Oracle/Bybit ccxt) bots: NOT available here -- would require a
    new remote data pull (SSH + a new script on the Oracle server), which
    is out of scope ("no new backend for missing data" per spec). Always
    reported as unavailable, never estimated or guessed.
  - Average R-multiple: NOT available -- mt5.history_deals_get() does not
    carry the position's SL distance at open, so a true R-multiple can't
    be reconstructed reliably from deal history alone. Reported as
    unavailable rather than computed from a shaky assumption.
"""
import os
import json
import glob
import re
from datetime import datetime, timezone, timedelta

import MetaTrader5 as mt5

BASE_DIR = os.path.dirname(__file__)
NOT_AVAILABLE = "Not Available"


def _env_int(name):
    raw = os.environ.get(name)
    return int(raw) if raw else None

# Same account credentials already used read-only elsewhere in this
# codebase (status_dashboard.py's own ACCOUNTS dict) -- duplicated here on
# purpose so this module has zero import-time dependency on the dashboard
# process (importable/testable standalone).
ACCOUNTS = {
    "BA": dict(path=r"C:\Program Files\MetaTrader 5\terminal64.exe",
               login=None, password=None, server=None),
    "EM": dict(path=r"C:\MT5_Portable_3\terminal64.exe",
               login=_env_int("MT5_LOGIN_EM"), password=os.environ.get("MT5_PASSWORD_EM"), server="Exness-MT5Real35"),
    "EA": dict(path=r"C:\MT5_Portable_2\terminal64.exe",
               login=_env_int("MT5_LOGIN_EA"), password=os.environ.get("MT5_PASSWORD_EA"), server="Exness-MT5Real33"),
}

# Magic -> bot display name. Kept in sync with status_dashboard.py's own
# MAGIC_NAMES by hand (both are display-only maps, no shared runtime state).
BOT_NAMES = {
    990099: "London Breakout", 990101: "London Breakout",
    884400: "fx_signal_exec",
    995502: "participation_pilot_btc_range", 995501: "participation_pilot_btc (retired)",
    992200: "orb_eth_exness (retired)", 992201: "orb_eth_exness",
    996600: "gold_btc_bot",
    993399: "ema_adx_bot (retired)", 993400: "ema_adx_bot (retired)",
    995500: "swing_pending_bot (retired)",
    0: "Manual",
}

# Which magics are considered part of the CURRENT active baseline (per
# Production Mode / project_strategy_verdicts memory) -- used only to
# decide what to show under "Active Strategies", never to filter out data.
ACTIVE_MAGICS = {990099, 990101, 884400, 995502, 992201, 996600}


def bot_name(magic):
    return BOT_NAMES.get(magic, f"magic {magic}")


DEAL_REASON_LABELS = {0: "Manual (Client)", 1: "Manual (Mobile)", 2: "Manual (Web)", 3: "Bot (Expert)",
                       4: "Stop Loss", 5: "Take Profit", 6: "Stop Out", 7: "Rollover",
                       8: "Variation Margin", 9: "Split"}


def fetch_closed_trades(account_code, days=180):
    """Read-only. Returns a list of closed round-trip trades for one
    account over the last `days`, or None if MT5 connection failed.

    2026-08-08 (Trade History Dashboard): enriched with comment/sl/tp
    (from the position's opening order, via history_orders_get -- same
    read-only call already proven elsewhere in this project) and
    exit_reason (from the closing deal's own broker-assigned `reason`
    code, DEAL_REASON_LABELS above) -- both fetched in the SAME connected
    session as the deals pull, no new MT5 connection added. No new P&L
    calculation -- `pnl` is untouched from the original formula."""
    cfg = ACCOUNTS.get(account_code)
    if not cfg:
        return None
    try:
        kwargs = {"path": cfg["path"]}
        if cfg["login"]:
            kwargs.update(login=cfg["login"], password=cfg["password"], server=cfg["server"])
        if not mt5.initialize(**kwargs):
            return None
        utc_to = datetime.now(timezone.utc)
        utc_from = utc_to - timedelta(days=days)
        deals = mt5.history_deals_get(utc_from, utc_to)

        if deals is None:
            mt5.shutdown()
            return []

        by_position = {}
        for d in deals:
            by_position.setdefault(d.position_id, []).append(d)

        trades = []
        for pos_id, ds in by_position.items():
            ds_sorted = sorted(ds, key=lambda x: x.time)
            opens = [x for x in ds_sorted if x.entry == 0]
            closes = [x for x in ds_sorted if x.entry == 1]
            if not opens or not closes:
                continue  # position still open, or a balance/credit op (type=2, no symbol) -- skip
            o, c = opens[0], closes[-1]
            if not o.symbol:
                continue  # balance/credit operation, not a trade
            pnl = sum(x.profit + x.swap + x.commission for x in ds_sorted)

            orders = mt5.history_orders_get(position=pos_id)
            open_order = next((ord_ for ord_ in (orders or []) if ord_.ticket == o.order), None)
            comment = (open_order.comment if open_order else "") or ""
            sl = open_order.sl if (open_order and open_order.sl) else None
            tp = open_order.tp if (open_order and open_order.tp) else None

            trades.append(dict(
                account=account_code, position_id=pos_id, ticket=o.ticket, magic=o.magic, symbol=o.symbol,
                side="buy" if o.type == 0 else "sell",
                volume=o.volume,
                entry_time=datetime.fromtimestamp(o.time, tz=timezone.utc),
                exit_time=datetime.fromtimestamp(c.time, tz=timezone.utc),
                entry_price=o.price, exit_price=c.price,
                pnl=round(pnl, 4),
                comment=comment, sl=sl, tp=tp,
                exit_reason_code=c.reason,
                exit_reason_label=DEAL_REASON_LABELS.get(c.reason, f"Unknown ({c.reason})"),
            ))
        mt5.shutdown()
    except Exception:
        try:
            mt5.shutdown()
        except Exception:
            pass
        return None
    return sorted(trades, key=lambda t: t["exit_time"])


def strategy_attribution(magic, comment):
    """Best-available attribution, never a guess: magic first (the
    reliable, already-trusted mapping), comment text second (only if it
    names a bot we recognize), else explicit Unknown/Unattributed."""
    if magic in BOT_NAMES:
        return BOT_NAMES[magic]
    if comment:
        c = comment.strip().lower()
        for known_magic, name in BOT_NAMES.items():
            if name.lower() in c:
                return name
        if comment.strip():
            return f"Unattributed (comment: {comment.strip()[:40]})"
    return "Unknown / Unattributed"


def fetch_all_closed_trades(days=180):
    """Read-only. All 3 MT5 accounts combined. Returns (trades, errors)."""
    all_trades = []
    errors = {}
    for code in ACCOUNTS:
        t = fetch_closed_trades(code, days=days)
        if t is None:
            errors[code] = "MT5 connection failed"
        else:
            all_trades.extend(t)
    return all_trades, errors


def _dd_from_equity_curve(pnls):
    equity, peak, maxdd = 0.0, 0.0, 0.0
    curve = []
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        maxdd = max(maxdd, peak - equity)
        curve.append(round(equity, 4))
    return curve, round(maxdd, 4)


def compute_metrics(trades):
    """Standard metrics from a list of closed trades (as returned by
    fetch_closed_trades). Returns a dict; every field either a real
    computed number or explicitly NOT_AVAILABLE -- never a fabricated
    placeholder."""
    n = len(trades)
    if n == 0:
        return {
            "num_trades": 0, "net_profit": 0.0, "gross_profit": 0.0, "gross_loss": 0.0,
            "win_rate": 0.0, "profit_factor": 0.0, "expectancy": 0.0,
            "avg_r": NOT_AVAILABLE, "avg_duration_min": NOT_AVAILABLE,
            "max_drawdown": 0.0, "equity_curve": [], "last_trade": None,
            "daily_pl": {}, "weekly_pl": {}, "monthly_pl": {},
        }

    trades_sorted = sorted(trades, key=lambda t: t["exit_time"])
    pnls = [t["pnl"] for t in trades_sorted]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    net_profit = sum(pnls)
    win_rate = len(wins) / n * 100
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    expectancy = net_profit / n
    durations = [(t["exit_time"] - t["entry_time"]).total_seconds() / 60 for t in trades_sorted]
    avg_duration_min = sum(durations) / len(durations)
    equity_curve, max_dd = _dd_from_equity_curve(pnls)

    def bucket(fmt):
        d = {}
        for t in trades_sorted:
            key = t["exit_time"].strftime(fmt)
            d[key] = round(d.get(key, 0.0) + t["pnl"], 4)
        return d

    last = trades_sorted[-1]
    return {
        "num_trades": n,
        "net_profit": round(net_profit, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else "inf",
        "expectancy": round(expectancy, 4),
        "avg_r": NOT_AVAILABLE,  # see module docstring -- no reliable SL-at-open data
        "avg_duration_min": round(avg_duration_min, 1),
        "max_drawdown": max_dd,
        "equity_curve": equity_curve,
        "last_trade": {"time": last["exit_time"].isoformat(), "symbol": last["symbol"],
                        "pnl": last["pnl"], "account": last["account"]},
        "daily_pl": bucket("%Y-%m-%d"),
        "weekly_pl": bucket("%G-W%V"),
        "monthly_pl": bucket("%Y-%m"),
    }


def group_by_magic(trades):
    out = {}
    for t in trades:
        out.setdefault(t["magic"], []).append(t)
    return out


def last_heartbeat(name):
    """Read-only. Reuses the SAME heartbeat_*.json files already written
    by heartbeat.py -- no new data source, no new writer."""
    path = os.path.join(BASE_DIR, f"heartbeat_{name}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        ts_key = next((k for k in d if "ts" in k.lower()), None)
        ts = d.get(ts_key) if ts_key else None
        if not ts:
            return None
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds()
        return {"ts": ts, "age_sec": round(age)}
    except Exception:
        return None


# 2026-08-09 (monitoring-only, Ahmed-approved): a MONITORING recency
# window -- how recent a log ERROR/Exception/Traceback line must be to
# still count as a CURRENT problem. NOT a trading/risk parameter; changes
# nothing about how any bot trades. Chosen because bot log lines only
# carry HH:MM:SS (no date), so "recent" has to mean something -- 1h is
# generous enough that a real ongoing problem is never missed, while a
# transient error from hours/days ago (already self-healed) stops being
# reported as if it were still happening.
ERROR_RECENCY_WINDOW_SEC = 3600


def last_error(log_filename, max_lines_scanned=4000, recency_window_sec=ERROR_RECENCY_WINDOW_SEC):
    """Read-only tail-scan of an existing bot log file for the last
    ERROR/Traceback line -- but only if it's still within
    recency_window_sec of now (UTC). Reads at most the last
    max_lines_scanned lines (bounded, avoids loading multi-MB logs
    entirely into memory). The most recent error-like line is found
    first (scanning backward from end-of-file); if THAT one is already
    stale, nothing more recent exists, so the scan stops there rather
    than reporting an even older line."""
    path = os.path.join(BASE_DIR, log_filename)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-max_lines_scanned:]
        now = datetime.now(timezone.utc)
        # Two real timestamp conventions exist across this project's bot logs:
        # some (participation_pilot_btc_range.py) prefix full "[YYYY-MM-DD
        # HH:MM:SS]"; others (Oracle-side bots, orb_eth_exness.py, etc.) prefix
        # HH:MM:SS only. Full-date lines need no day-rollover guessing at all.
        full_dt_re = re.compile(r"^\[(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)\]")
        time_only_re = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\]")
        # 2026-08-20 (Ahmed-approved): tracks whether a dated line (error or
        # not) has already been passed while scanning backward from EOF. An
        # untimed error/traceback line hit AFTER that is provably older than
        # the dated line -- multi-line Python tracebacks have no per-line
        # timestamp, so the old fallback ("no timestamp -- report anyway")
        # was returning a day-old traceback as a CURRENT error whenever it
        # was still within the last 4000 lines, even with hours of clean
        # dated activity after it (participation_pilot_btc_range false
        # EXECUTION ERROR). Only report an untimed line when nothing dated
        # has been seen yet -- i.e. it's genuinely the newest thing on file.
        seen_dated_line = False
        for line in reversed(lines):
            low = line.lower()
            m_full = full_dt_re.match(line)
            m_time = None if m_full else time_only_re.match(line)
            if m_full or m_time:
                seen_dated_line = True
            if "traceback" in low or "error" in low or "exception" in low:
                if m_full:
                    y, mo, d, h, mi, s = (int(x) for x in m_full.groups())
                    candidate = datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)
                elif m_time:
                    h, mi, s = (int(x) for x in m_time.groups())
                    candidate = now.replace(hour=h, minute=mi, second=s, microsecond=0)
                    if candidate > now:
                        candidate -= timedelta(days=1)  # HH:MM:SS-only, scanning backward -> must be yesterday
                else:
                    if seen_dated_line:
                        continue  # provably older than a dated line already seen -- keep scanning, don't report
                    return line.strip()[:300]  # nothing dated seen yet -- report rather than hide
                if (now - candidate).total_seconds() <= recency_window_sec:
                    return line.strip()[:300]
                return None
        return None
    except Exception:
        return None


def portfolio_risk_snapshot(accounts):
    """Read-only. Computes symbol/market exposure and lot distribution
    from the SAME already-fetched `accounts` list status_dashboard.py's
    refresh_loop() already builds every 8s (the _cache["accounts"] list,
    passed in by the caller) -- no new data fetch, no new MT5 connection."""
    MARKET_MAP = {
        "XAU": "Gold", "GOLD": "Gold",
        "EUR": "Forex", "GBP": "Forex", "USD": "Forex", "AUD": "Forex",
        "NZD": "Forex", "CAD": "Forex", "CHF": "Forex", "JPY": "Forex",
        "BTC": "Crypto", "ETH": "Crypto", "XRP": "Crypto", "DOT": "Crypto",
        "SOL": "Crypto", "ADA": "Crypto", "DOGE": "Crypto", "LTC": "Crypto",
        "OIL": "Oil",
    }

    def classify(symbol):
        s = symbol.upper()
        for key, market in MARKET_MAP.items():
            if key in s:
                return market
        return "Other"

    by_symbol, by_market, by_bot = {}, {}, {}
    total_lot = 0.0
    all_positions = []
    for acc in accounts:
        for p in acc.get("positions", []):
            all_positions.append(p)
            sym = p["symbol"]
            vol = p.get("volume", 0) or 0
            total_lot += vol
            by_symbol.setdefault(sym, dict(count=0, lot=0.0, net_pnl=0.0))
            by_symbol[sym]["count"] += 1
            by_symbol[sym]["lot"] += vol
            by_symbol[sym]["net_pnl"] += (p.get("profit") or 0)

            market = classify(sym)
            by_market.setdefault(market, dict(count=0, lot=0.0, net_pnl=0.0))
            by_market[market]["count"] += 1
            by_market[market]["lot"] += vol
            by_market[market]["net_pnl"] += (p.get("profit") or 0)

            strat = p.get("strategy", "Manual")
            by_bot.setdefault(strat, dict(count=0, lot=0.0))
            by_bot[strat]["count"] += 1
            by_bot[strat]["lot"] += vol

    for d in (by_symbol, by_market, by_bot):
        for v in d.values():
            if "lot" in v:
                v["lot"] = round(v["lot"], 4)
            if "net_pnl" in v:
                v["net_pnl"] = round(v["net_pnl"], 2)

    return dict(by_symbol=by_symbol, by_market=by_market, by_bot=by_bot,
                total_lot=round(total_lot, 4), total_positions=len(all_positions))


def simple_daily_correlation(trades_by_magic, min_overlap_days=10):
    """Read-only, best-effort. Pearson correlation of daily P/L between
    each pair of bots, computed ONLY from days where BOTH bots have at
    least one closed trade. Returns 'Not Available' for a pair with fewer
    than min_overlap_days overlapping days -- an honest small-sample
    guard, not a fabricated number."""
    daily_series = {}
    for magic, trades in trades_by_magic.items():
        m = compute_metrics(trades)
        daily_series[magic] = m["daily_pl"]

    magics = list(daily_series.keys())
    result = {}
    for i in range(len(magics)):
        for j in range(i + 1, len(magics)):
            m1, m2 = magics[i], magics[j]
            days = set(daily_series[m1]) & set(daily_series[m2])
            key = f"{bot_name(m1)} vs {bot_name(m2)}"
            if len(days) < min_overlap_days:
                result[key] = NOT_AVAILABLE
                continue
            xs = [daily_series[m1][d] for d in days]
            ys = [daily_series[m2][d] for d in days]
            mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
            cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
            var_x = sum((x - mean_x) ** 2 for x in xs)
            var_y = sum((y - mean_y) ** 2 for y in ys)
            if var_x == 0 or var_y == 0:
                result[key] = NOT_AVAILABLE
            else:
                result[key] = round(cov / (var_x * var_y) ** 0.5, 3)
    return result


# ---------------------------------------------------------------------------
# 2026-08-09 (Dashboard Monitoring Extension, read-only). Everything below
# reuses data this module/status_dashboard.py already fetches -- no new
# MT5/Oracle connection, no new polling loop. Every value is either
# computed from real data or explicitly NOT_AVAILABLE/None -- never invented.
# ---------------------------------------------------------------------------

TELEGRAM_SIGNAL_MAGICS = {884400}  # fx_signal_exec -- executes external Telegram signals, not its own market view


def trade_source(magic):
    """BOT / TELEGRAM / MANUAL / UNKNOWN -- magic-based only, same
    reliable field strategy_attribution() already trusts. Never a guess."""
    if magic == 0:
        return "MANUAL"
    if magic in TELEGRAM_SIGNAL_MAGICS:
        return "TELEGRAM"
    if magic in BOT_NAMES:
        return "BOT"
    return "UNKNOWN"


RISK_HALT_FLAG_PATH = os.path.join(BASE_DIR, "RISK_HALT.flag")


def risk_halt_status():
    """Read-only existence+mtime check on the SAME flag file
    portfolio_risk_guard.py already writes/reads -- no other inference."""
    if not os.path.exists(RISK_HALT_FLAG_PATH):
        return {"halted": False, "since": None}
    try:
        mtime = os.path.getmtime(RISK_HALT_FLAG_PATH)
        return {"halted": True, "since": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()}
    except Exception:
        return {"halted": True, "since": NOT_AVAILABLE}


PORTFOLIO_RISK_STATE_PATH = os.path.join(BASE_DIR, "portfolio_risk_state.json")
PORTFOLIO_RISK_THRESHOLD_PCT = 15.0  # must match portfolio_risk_guard.py's DEFAULT_THRESHOLD_PCT


def risk_halt_detail(accounts):
    """Read-only. Dashboard badge (2026-08-18, Ahmed's Bot Health request):
    live drawdown/baseline/current, not just the flag's existence. The flag
    file's own drawdown/current numbers are a ONE-TIME snapshot from the
    moment it first tripped (portfolio_risk_guard.py never rewrites them on
    later HALTED_ACTIVE ticks) -- stale within minutes. This recomputes
    'current' live from the SAME accounts list the rest of the dashboard
    already fetched this cycle (no extra MT5/SSH calls), against the SAME
    baseline portfolio_risk_guard.py itself is using today (its own state
    file), so the number always matches what the real guard would compute
    right now."""
    status = risk_halt_status()
    baseline = None
    try:
        with open(PORTFOLIO_RISK_STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
        baseline = state.get("baseline_equity")
    except Exception:
        pass
    current_total = round(sum((a.get("equity") or 0) for a in accounts if not a.get("error")), 2) \
        if accounts else None
    drawdown_pct = None
    if baseline and current_total is not None and baseline > 0:
        drawdown_pct = round((baseline - current_total) / baseline * 100, 2)
    return {
        "active": status["halted"],
        "since": status["since"],
        "drawdown_pct": drawdown_pct,
        "threshold_pct": PORTFOLIO_RISK_THRESHOLD_PCT,
        "baseline": round(baseline, 2) if baseline is not None else None,
        "current_equity": current_total,
        "next_reset": "daily rollover (01:00 Africa/Cairo)",
    }


def gold_btc_bot_eligibility(acc, equity, risk_halted):
    """Read-only. Same equity-guard math as gold_btc_bot.py's own
    _risk_gates_ok() (EQUITY_FLOOR_PCT=70.0 of that account's recorded
    Pilot-start baseline) plus the RISK_HALT check -- reads its baseline
    file, never writes it (only gold_btc_bot.py itself creates that file,
    on first run)."""
    if risk_halted:
        return "BLOCKED BY RISK_HALT"
    path = os.path.join(BASE_DIR, f"gold_btc_bot_baseline_{acc}.json")
    if not os.path.exists(path):
        return "UNKNOWN"
    try:
        with open(path, encoding="utf-8") as f:
            baseline = json.load(f).get("baseline_equity")
        # 2026-08-19 (Ahmed's dashboard patch): the two collapse cases below
        # used to both return a bare "UNKNOWN" even though the reason is
        # already known from data already read above -- split them out,
        # without inferring any actual eligibility verdict.
        if not baseline:
            return "UNKNOWN (baseline_equity missing in file)"
        if equity is None:
            return "UNKNOWN (current equity unavailable)"
        floor = baseline * 0.70
        if equity < floor:
            return f"SKIPPED — EQUITY BELOW MINIMUM (${equity:.2f} < ${floor:.2f})"
        return "ELIGIBLE"
    except Exception:
        return "UNKNOWN"


def asset_exposure_summary(accounts, assets=("XAU", "BTC", "ETH", "OIL")):
    """Read-only. For each named asset, aggregates across every symbol-name
    variant (XAUUSD/XAUUSD.s/XAUUSDm/XAU-USDT etc, substring-matched like
    classify() inside portfolio_risk_snapshot already does): position
    count, total lot, distinct bots exposed, protected/unprotected split.
    Reuses the same `accounts` list portfolio_risk_snapshot() receives."""
    out = {}
    for asset in assets:
        positions = [p for acc in accounts for p in acc.get("positions", []) if asset in p["symbol"].upper()]
        bots = sorted(set(p.get("strategy", "Manual") for p in positions))
        protected = [p for p in positions if p.get("sl_status") == "CONFIRMED"]
        unprotected = [p for p in positions if p.get("sl_status") != "CONFIRMED"]
        out[asset] = {
            "position_count": len(positions),
            "total_lot": round(sum(p.get("volume", 0) or 0 for p in positions), 4),
            "distinct_bots": bots,
            "bot_count": len(bots),
            "protected_count": len(protected),
            "unprotected_count": len(unprotected),
            "protected_pct": round(100 * len(protected) / len(positions), 1) if positions else None,
            "unprotected_positions": [{"account": p["account"], "symbol": p["symbol"], "side": p["side"],
                                          "volume": p.get("volume")} for p in unprotected],
        }
    return out


# (display_name, heartbeat_name, log_filename, magic, stale_after_sec_override)
# -- the same heartbeat_*.json / *.log files last_heartbeat()/last_error()
# already read elsewhere in this module (reports_page's all_heartbeats_ok
# check). stale_after_sec_override is a MONITORING parameter (None = use
# no_trade_status()'s own default) -- it exists because each bot's real
# heartbeat-write cadence differs (matches that bot's own check_interval_sec
# in its source, e.g. gold_btc_bot.py's cfg["check_interval_sec"]=900), not
# because any bot's actual trading cycle changed here.
# 2026-08-09 (monitoring-only, Ahmed-approved) fixes:
#  - orb_eth_exness: was pointing at heartbeat_orb_eth_exness.json / orb_eth_exness.log,
#    neither of which the bot (which runs as account EM) ever writes -- it
#    writes heartbeat_orb_eth_exness_EM.json via write_heartbeat(f"orb_eth_exness_{ACC}")
#    (orb_eth_exness.py:131) and logs to orb_eth_em.log. Corrected to match.
#  - gold_btc_bot (EA): given its own 900s check_interval_sec, a 600s
#    monitoring threshold was tighter than its real cycle -- flapped
#    BOT_DOWN for ~5min out of every 15min even when perfectly healthy.
#    Override = 900 (its real cycle, confirmed in gold_btc_bot.py) + 300s
#    buffer, not an arbitrary number.
NO_TRADE_WATCHED_BOTS = [
    ("fx_signal_exec", "fx_signal_exec", "fx_signal_exec.log", 884400, None),
    ("participation_pilot_btc_range", "participation_pilot_btc_range", "participation_pilot_btc_range.log", 995502, None),
    ("orb_eth_exness", "orb_eth_exness_EM", "orb_eth_em.log", 992201, None),
    ("gold_btc_bot (EA)", "gold_btc_bot_EA", "gold_btc_bot_ea.log", 996600, 1200),
]


def no_trade_status(trades_by_magic, stale_after_sec=600):
    """Read-only. Per watched bot: NO_TRADE_NORMAL / BOT_DOWN /
    EXECUTION_ERROR -- Ahmed's explicit policy that an idle bot with a
    healthy heartbeat and no error is NORMAL, not a problem to chase.
    Never invents a reason string beyond what last_error() actually
    found in the real log. `stale_after_sec` is the DEFAULT monitoring
    threshold; a bot with a per-entry override in NO_TRADE_WATCHED_BOTS
    uses its own instead (see that list's comment for why)."""
    now = datetime.now(timezone.utc)
    rows = []
    for display_name, hb_name, log_name, magic, stale_override in NO_TRADE_WATCHED_BOTS:
        bot_stale_after_sec = stale_override if stale_override is not None else stale_after_sec
        hb = last_heartbeat(hb_name)
        err = last_error(log_name)
        closed = trades_by_magic.get(magic, [])
        last_trade_time = max((t["exit_time"] for t in closed), default=None)
        if hb is None:
            status, reason = "BOT_DOWN", "no heartbeat file found"
        elif hb["age_sec"] > bot_stale_after_sec:
            status, reason = "BOT_DOWN", f"heartbeat stale ({hb['age_sec']}s > {bot_stale_after_sec}s)"
        elif err:
            status, reason = "EXECUTION_ERROR", err
        else:
            status, reason = "NO_TRADE_NORMAL", None
        rows.append({
            "bot": display_name, "status": status, "reason": reason,
            "last_heartbeat_age_sec": hb["age_sec"] if hb else None,
            "last_trade_time": last_trade_time.isoformat() if last_trade_time else None,
            "time_since_last_trade_sec": round((now - last_trade_time).total_seconds()) if last_trade_time else None,
        })
    return rows


ORACLE_ATTRIBUTION_LEDGER_PATH = os.path.normpath(os.path.join(BASE_DIR, "..", "oracle_attribution", "ledger.jsonl"))


def oracle_attribution_summary(limit=20):
    """Read-only. Reads the real oracle_attribution/ledger.jsonl (the
    Phase 1/2 attribution ledger) if this dashboard instance has that
    sibling package -- returns available=False (never fabricated data)
    if it doesn't exist or can't be parsed."""
    if not os.path.exists(ORACLE_ATTRIBUTION_LEDGER_PATH):
        return {"available": False, "reason": NOT_AVAILABLE, "recent": [], "confidence_by_bot": {}}
    rows = []
    try:
        with open(ORACLE_ATTRIBUTION_LEDGER_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        return {"available": False, "reason": NOT_AVAILABLE, "recent": [], "confidence_by_bot": {}}

    fills = [r for r in rows if r.get("event") == "FILL_RECONCILED"]
    fills.sort(key=lambda r: r.get("timestamp", 0))
    confidence_by_bot = {}
    for f in fills:
        bot = f.get("matched_bot") or "UNMATCHED"
        method = f.get("match_method", "UNMATCHED")
        confidence_by_bot.setdefault(bot, {"TAG": 0, "LOG_MATCH": 0, "AMBIGUOUS": 0, "UNMATCHED": 0})
        confidence_by_bot[bot][method] = confidence_by_bot[bot].get(method, 0) + 1
    return {"available": True, "total_fills": len(fills), "recent": fills[-limit:],
             "confidence_by_bot": confidence_by_bot}


def _demo():
    """ponytail: smallest check that fails if the metrics math breaks."""
    from datetime import timedelta as td
    now = datetime.now(timezone.utc)
    trades = [
        dict(account="EA", position_id=1, magic=996600, symbol="XAUUSDm", side="buy", volume=0.01,
             entry_time=now - td(days=2, minutes=30), exit_time=now - td(days=2), entry_price=4300, exit_price=4310, pnl=10.0),
        dict(account="EA", position_id=2, magic=996600, symbol="XAUUSDm", side="sell", volume=0.01,
             entry_time=now - td(days=1, minutes=15), exit_time=now - td(days=1), entry_price=4310, exit_price=4315, pnl=-5.0),
    ]
    m = compute_metrics(trades)
    assert m["num_trades"] == 2
    assert m["net_profit"] == 5.0
    assert m["win_rate"] == 50.0
    assert m["gross_profit"] == 10.0 and m["gross_loss"] == 5.0
    assert m["profit_factor"] == 2.0
    assert m["max_drawdown"] == 5.0  # peak 10 -> trough 5
    assert m["avg_r"] == NOT_AVAILABLE
    empty = compute_metrics([])
    assert empty["num_trades"] == 0 and empty["profit_factor"] == 0.0
    grouped = group_by_magic(trades)
    assert set(grouped.keys()) == {996600}

    # 2026-08-09 Dashboard Monitoring Extension additions
    assert trade_source(0) == "MANUAL"
    assert trade_source(884400) == "TELEGRAM"
    assert trade_source(996600) == "BOT"
    assert trade_source(123456) == "UNKNOWN"

    halt = risk_halt_status()
    assert halt["halted"] in (True, False)  # real check against the actual flag file, either answer is valid here

    fake_accounts = [{"positions": [
        {"account": "EA", "symbol": "XAUUSDm", "volume": 0.01, "strategy": "gold_btc_bot", "sl_status": "CONFIRMED", "side": "buy"},
        {"account": "BA", "symbol": "BTC/USDT:USDT", "volume": 0.001, "strategy": "Manual/Other", "sl_status": "MISSING", "side": "long"},
    ]}]
    exposure = asset_exposure_summary(fake_accounts, assets=("XAU", "BTC"))
    assert exposure["XAU"]["position_count"] == 1 and exposure["XAU"]["protected_pct"] == 100.0
    assert exposure["BTC"]["unprotected_count"] == 1 and exposure["BTC"]["protected_pct"] == 0.0

    nts = no_trade_status({})  # no trades at all -- must not crash, must classify from heartbeat/log only
    assert len(nts) == len(NO_TRADE_WATCHED_BOTS)
    assert all(r["status"] in ("NO_TRADE_NORMAL", "BOT_DOWN", "EXECUTION_ERROR") for r in nts)
    # 2026-08-09 monitoring fix: gold_btc_bot (EA) must use its 1200s override,
    # not the 600s global default -- verify the override entry is actually wired.
    gold_ea_entry = next(e for e in NO_TRADE_WATCHED_BOTS if e[0] == "gold_btc_bot (EA)")
    assert gold_ea_entry[4] == 1200, "gold_btc_bot (EA) per-bot stale threshold override missing/changed"
    orb_entry = next(e for e in NO_TRADE_WATCHED_BOTS if e[0] == "orb_eth_exness")
    assert orb_entry[1] == "orb_eth_exness_EM" and orb_entry[2] == "orb_eth_em.log", \
        "orb_eth_exness heartbeat/log names must point at the real EM-account files"

    oa = oracle_attribution_summary()
    assert oa["available"] in (True, False)  # real check against the actual ledger file, either answer is valid here

    # 2026-08-09 monitoring fix: last_error() recency handling, synthetic log
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        orig_base = globals()["BASE_DIR"]
        globals()["BASE_DIR"] = d
        try:
            now = datetime.now(timezone.utc)
            stale_time = (now - timedelta(hours=5)).strftime("%H:%M:%S")
            recent_time = (now - timedelta(minutes=2)).strftime("%H:%M:%S")

            with open(os.path.join(d, "stale_only.log"), "w", encoding="utf-8") as f:
                f.write(f"[{stale_time}] ERROR: something broke a long time ago\n")
                f.write(f"[{now.strftime('%H:%M:%S')}] all fine now\n")
            assert last_error("stale_only.log") is None, "a 5h-old error must not be reported as current"

            with open(os.path.join(d, "recent.log"), "w", encoding="utf-8") as f:
                f.write(f"[{stale_time}] ERROR: old one\n")
                f.write(f"[{recent_time}] ERROR: this one is recent\n")
            found = last_error("recent.log")
            assert found and "this one is recent" in found, "a 2min-old error must still be reported"

            with open(os.path.join(d, "no_error.log"), "w", encoding="utf-8") as f:
                f.write(f"[{now.strftime('%H:%M:%S')}] all fine, nothing wrong here\n")
            assert last_error("no_error.log") is None

            # full "[YYYY-MM-DD HH:MM:SS]" convention (participation_pilot_btc_range.py's
            # real format) -- the exact bug this fix was for: a 26h-old full-date
            # error must not be reported, a 2min-old one must.
            stale_full = (now - timedelta(hours=26)).strftime("%Y-%m-%d %H:%M:%S")
            recent_full = (now - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
            with open(os.path.join(d, "full_date_stale.log"), "w", encoding="utf-8") as f:
                f.write(f"[{stale_full}] REGIME CLASSIFICATION ERROR: old connection reset\n")
            assert last_error("full_date_stale.log") is None, "a 26h-old full-date error must not be reported as current"
            with open(os.path.join(d, "full_date_recent.log"), "w", encoding="utf-8") as f:
                f.write(f"[{recent_full}] REGIME CLASSIFICATION ERROR: fresh one\n")
            found2 = last_error("full_date_recent.log")
            assert found2 and "fresh one" in found2, "a 2min-old full-date error must still be reported"

            # 2026-08-20: untimed traceback lines (no per-line [timestamp],
            # the real shape of a Python traceback) must not win over dated
            # clean activity that came after them -- root cause of the false
            # EXECUTION ERROR on participation_pilot_btc_range.
            old_full = (now - timedelta(hours=26)).strftime("%Y-%m-%d %H:%M:%S")
            with open(os.path.join(d, "untimed_traceback_then_clean.log"), "w", encoding="utf-8") as f:
                f.write("Traceback (most recent call last):\n")
                f.write("  File \"x.py\", line 1, in <module>\n")
                f.write("json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)\n")
                f.write(f"[{old_full}] load_state: STATE_FILE corrupted -- falling back to default state\n")
                f.write(f"[{recent_full}] bot started cleanly\n")
            assert last_error("untimed_traceback_then_clean.log") is None, \
                "an untimed traceback line must not be reported once a dated clean line proves it's stale"

            with open(os.path.join(d, "multiline_traceback_then_clean.log"), "w", encoding="utf-8") as f:
                f.write(f"[{old_full}] Bot started\n")
                f.write("Traceback (most recent call last):\n")
                f.write("  File \"x.py\", line 173, in main\n")
                f.write("    state = load_state()\n")
                f.write("  File \"x.py\", line 112, in load_state\n")
                f.write("    return json.load(f)\n")
                f.write("json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)\n")
                f.write(f"[{old_full}] load_state: STATE_FILE corrupted -- falling back to default state\n")
                f.write(f"[{recent_full}] bot started cleanly, all fine\n")
            assert last_error("multiline_traceback_then_clean.log") is None, \
                "a multi-line untimed traceback must not be reported once dated clean activity follows it"
        finally:
            globals()["BASE_DIR"] = orig_base

    # 2026-08-19 dashboard patch: UNKNOWN eligibility must expose the
    # already-known reason instead of collapsing to a bare "UNKNOWN",
    # without inventing an actual eligibility verdict.
    import tempfile as _tf
    with _tf.TemporaryDirectory() as d2:
        orig_base2 = globals()["BASE_DIR"]
        globals()["BASE_DIR"] = d2
        try:
            assert gold_btc_bot_eligibility("EA", 100.0, risk_halted=False) == "UNKNOWN"  # no baseline file at all
            with open(os.path.join(d2, "gold_btc_bot_baseline_EA.json"), "w", encoding="utf-8") as f:
                json.dump({"baseline_equity": None}, f)
            assert gold_btc_bot_eligibility("EA", 100.0, risk_halted=False) == "UNKNOWN (baseline_equity missing in file)"
            with open(os.path.join(d2, "gold_btc_bot_baseline_EA.json"), "w", encoding="utf-8") as f:
                json.dump({"baseline_equity": 150.0}, f)
            assert gold_btc_bot_eligibility("EA", None, risk_halted=False) == "UNKNOWN (current equity unavailable)"
            assert gold_btc_bot_eligibility("EA", 200.0, risk_halted=False) == "ELIGIBLE"  # unaffected control case (baseline=150, floor=105)
        finally:
            globals()["BASE_DIR"] = orig_base2

    print("portfolio_analytics self-check OK")


if __name__ == "__main__":
    _demo()
