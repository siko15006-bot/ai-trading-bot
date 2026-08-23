"""30-day live monitoring report for participation_pilot_btc_range (Phase 1.1,
dynamic P99/14d threshold, deployed 2026-08-07). Reads the existing
signals/trades logs -- no new logging added, no strategy logic touched.

Reports exactly the 7 items Ahmed asked to track during the freeze period
(2026-08-07 -> 2026-09-06, no strategic changes until then):
  1. anomalies detected
  2. trades executed
  3. signals rejected for H1 disagreement
  4. signals rejected for regime (not Range)
  5. daily dynamic threshold (min/mean/max)
  6. PF / WR / Net / MaxDD
  7. execution errors + heartbeat freshness

Only counts data from the dynamic-threshold deployment onward (rows without
a "dynamic_threshold" key are the old static-threshold era and are skipped).
"""
import json
import os
from datetime import datetime, timezone

BASE_DIR = r"C:\TradingBot\Bot_Active"
SIGNALS_LOG = os.path.join(BASE_DIR, "participation_pilot_range_signals.jsonl")
TRADES_LOG = os.path.join(BASE_DIR, "participation_pilot_range_trades.json")
BOT_LOG = os.path.join(BASE_DIR, "participation_pilot_btc_range.log")
HEARTBEAT = os.path.join(BASE_DIR, "heartbeat_participation_pilot_btc_range.json")
DEPLOY_TS = datetime(2026, 8, 7, 10, 58, 3, tzinfo=timezone.utc)
CHECKPOINT_DUE = datetime(2026, 9, 6, tzinfo=timezone.utc)

# Locked reference numbers from the decisive backtest that got this version
# approved (rolling_threshold_design.py, Round 6, dynamic P99/14d design).
# Never edited -- this is the fixed yardstick live results get compared to.
BACKTEST_REF = {
    "IS":      dict(n=108, wr=45.4, pf=1.72, net=229.76, maxdd=31.25, rate_mo=7.77),
    "OOS":     dict(n=37,  wr=40.5, pf=1.56, net=55.77,  maxdd=19.02, rate_mo=7.99),
    "Holdout": dict(n=36,  wr=44.4, pf=1.49, net=37.01,  maxdd=13.51, rate_mo=7.77),
}
PASS_PF, PASS_NET, PASS_MIN_TRADES, PASS_MAX_DD = 1.20, 0.0, 30, 500.0


def pct_dev(live_val, ref_val):
    """% deviation of live vs a backtest reference value. None if ref is 0 (undefined)."""
    if ref_val == 0:
        return None
    return (live_val - ref_val) / abs(ref_val) * 100


def fmt_dev(d):
    return f"{d:+7.1f}%" if d is not None else "    n/a"


def compute_verdict(n, pf, net, maxdd):
    """Numbers-only verdict. Per Ahmed's explicit rule: PASS/FAIL is NEVER
    issued below the n>=30 sample bar -- a small sample cannot statistically
    support either verdict, so it's always INCONCLUSIVE instead, regardless
    of how good or bad the available-sample numbers look."""
    if n < PASS_MIN_TRADES:
        reasons = [f"n={n} < {PASS_MIN_TRADES} required trades -- PASS/FAIL not "
                   f"statistically supportable at this sample size"]
        if n > 0:
            reasons.append(f"available-sample numbers (informational only, NOT a verdict basis): "
                            f"PF={pf:.2f}, Net=${net:.2f}, MaxDD=${maxdd:.2f}")
        else:
            reasons.append("0 closed trades so far -- no evidence yet")
        return "INCONCLUSIVE -- Insufficient Live Sample", reasons
    hard_fail_reasons = []
    if net <= PASS_NET:
        hard_fail_reasons.append(f"Net=${net:.2f} <= ${PASS_NET} (required strictly positive)")
    if pf <= PASS_PF:
        hard_fail_reasons.append(f"PF={pf:.2f} <= {PASS_PF} (required strictly above)")
    if maxdd > PASS_MAX_DD:
        hard_fail_reasons.append(f"MaxDD=${maxdd:.2f} > ${PASS_MAX_DD} (risk limit breached)")
    if hard_fail_reasons:
        return "FAIL", hard_fail_reasons
    return "PASS", [f"n={n}>={PASS_MIN_TRADES}, PF={pf:.2f}>{PASS_PF}, "
                     f"Net=${net:.2f}>${PASS_NET}, MaxDD=${maxdd:.2f}<=${PASS_MAX_DD}"]


def extension_estimate(n, live_rate_mo):
    """How many more trades / how much more time to reach the n>=30 bar,
    at the current baseline (no parameter changes) -- reporting only."""
    needed = max(PASS_MIN_TRADES - n, 0)
    if needed == 0:
        return None
    backtest_avg_rate = (BACKTEST_REF["OOS"]["rate_mo"] + BACKTEST_REF["Holdout"]["rate_mo"]) / 2
    rate = live_rate_mo if live_rate_mo and live_rate_mo > 0 else backtest_avg_rate
    rate_source = "live-observed rate" if live_rate_mo and live_rate_mo > 0 else \
        f"backtest OOS/Holdout average ({backtest_avg_rate:.2f}/mo, live rate not yet established)"
    est_days = needed / rate * 30.44
    return needed, est_days, rate, rate_source

rows = []
with open(SIGNALS_LOG, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if "dynamic_threshold" not in r:
            continue  # pre-deployment (static-threshold era) row, skip
        rows.append(r)

n_anomaly = sum(1 for r in rows if r["is_anomaly"])
n_h1_reject = sum(1 for r in rows if r["is_anomaly"] and not r["base_signal"] and not r["h1_agree"])
n_regime_reject = sum(1 for r in rows if r["base_signal"] and r["regime"] != "Range")
n_signal = sum(1 for r in rows if r["signal"])

# Reporting-only proximity metrics (2026-08-07 addition, per Ahmed) -- how
# often the market approaches the dynamic threshold without crossing it.
# Pure read of already-logged ratio/dynamic_threshold; zero effect on
# execution, not fed back into is_anomaly/signal anywhere.
rt_rows = [r for r in rows if r.get("ratio") is not None and r.get("dynamic_threshold") is not None]
n_near_90pct = sum(1 for r in rt_rows if r["ratio"] >= 0.90 * r["dynamic_threshold"])
n_confirmed_anomaly = sum(1 for r in rt_rows if r["ratio"] >= r["dynamic_threshold"])

print("=" * 70)
print(f"participation_pilot_btc_range -- 30-day monitoring report")
print(f"window: {DEPLOY_TS.isoformat()} -> {datetime.now(timezone.utc).isoformat()}")
print(f"checkpoint due: 2026-09-06 (30 days from deployment)")
print("=" * 70)

print(f"\n1) Anomalies detected: {n_anomaly}  (of {len(rows)} bars observed)")
print(f"2) Signals fired (signal=True): {n_signal}")
print(f"3) Rejected -- H1 disagreement: {n_h1_reject}")
print(f"4) Rejected -- not Range regime: {n_regime_reject}")

print(f"\n[reporting-only, no execution impact]")
print(f"8) Bars with ratio >= 0.90 x dynamic_threshold (near-miss): {n_near_90pct} (of {len(rt_rows)})")
print(f"9) Bars with ratio >= dynamic_threshold (confirmed anomaly): {n_confirmed_anomaly} "
      f"(cross-check vs item 1: {'match' if n_confirmed_anomaly == n_anomaly else 'MISMATCH -- investigate'})")

print(f"\n5) Dynamic threshold, daily (min / mean / max):")
by_day = {}
for r in rows:
    if r["dynamic_threshold"] is None:
        continue
    day = r["bar_time"][:10]
    by_day.setdefault(day, []).append(r["dynamic_threshold"])
for day in sorted(by_day):
    vals = by_day[day]
    print(f"   {day}: min={min(vals):.3f}  mean={sum(vals)/len(vals):.3f}  max={max(vals):.3f}  (n={len(vals)})")

trades = []
if os.path.exists(TRADES_LOG):
    with open(TRADES_LOG, encoding="utf-8") as f:
        all_trades = json.load(f)
    for t in all_trades:
        et = t.get("exit_time")
        if et and datetime.fromisoformat(et.replace("Z", "+00:00")) >= DEPLOY_TS:
            trades.append(t)

n = len(trades)
wins = [t["pnl"] for t in trades if t["pnl"] > 0]
losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
net = sum(t["pnl"] for t in trades)
pf = (sum(wins) or 0) / (abs(sum(losses)) or 1) if trades else 0.0
wr = len(wins) / n * 100 if n else 0.0
exp = net / n if n else 0.0
equity, peak, maxdd = 0.0, 0.0, 0.0
for t in trades:
    equity += t["pnl"]; peak = max(peak, equity); maxdd = max(maxdd, peak - equity)
days_elapsed = max((datetime.now(timezone.utc) - DEPLOY_TS).total_seconds() / 86400, 0.01)
live_rate_mo = n / (days_elapsed / 30.44)

print(f"\n6) Closed trades since deployment: {n}")
if trades:
    print(f"   WR={wr:.1f}%  PF={pf:.2f}  Net=${net:+.2f}  MaxDD=${maxdd:.2f}  Exp=${exp:+.3f}/trade")
else:
    print("   (no closed trades yet -- insufficient sample, expected early in the window)")

print(f"\n{'='*70}\nBACKTEST vs LIVE COMPARISON\n{'='*70}")
print(f"   {'stage':<10}{'n':>5}{'WR%':>8}{'PF':>8}{'Net$':>10}{'MaxDD$':>9}{'Exp$/tr':>10}{'tr/mo':>8}")
print(f"   {'LIVE':<10}{n:>5}{wr:>8.1f}{pf:>8.2f}{net:>10.2f}{maxdd:>9.2f}{exp:>10.3f}{live_rate_mo:>8.2f}")
live_metrics = dict(wr=wr, pf=pf, net=net, maxdd=maxdd, exp=exp)
for stage, ref in BACKTEST_REF.items():
    ref_exp = ref["net"] / ref["n"]
    print(f"   {stage:<10}{ref['n']:>5}{ref['wr']:>8.1f}{ref['pf']:>8.2f}{ref['net']:>10.2f}"
          f"{ref['maxdd']:>9.2f}{ref_exp:>10.3f}{ref['rate_mo']:>8.2f}")

print(f"\n   Deviation, Live vs each backtest stage (%):")
print(f"   {'vs stage':<10}{'WR':>9}{'PF':>9}{'Net':>9}{'MaxDD':>9}{'Exp':>9}")
for stage, ref in BACKTEST_REF.items():
    ref_exp = ref["net"] / ref["n"]
    d_wr = pct_dev(wr, ref["wr"]); d_pf = pct_dev(pf, ref["pf"]); d_net = pct_dev(net, ref["net"])
    d_dd = pct_dev(maxdd, ref["maxdd"]); d_exp = pct_dev(exp, ref_exp)
    print(f"   vs {stage:<7}{fmt_dev(d_wr):>9}{fmt_dev(d_pf):>9}{fmt_dev(d_net):>9}"
          f"{fmt_dev(d_dd):>9}{fmt_dev(d_exp):>9}")

print(f"\n{'='*70}\nAUTOMATIC VERDICT\n{'='*70}")
verdict, reasons = compute_verdict(n, pf, net, maxdd)
print(f"   {verdict}")
for r in reasons:
    print(f"    - {r}")

if verdict.startswith("INCONCLUSIVE"):
    ext = extension_estimate(n, live_rate_mo)
    if ext:
        needed, est_days, rate, rate_source = ext
        print(f"\n   Recommendation: extend monitoring, SAME baseline, zero changes.")
        print(f"    - trades needed to reach n={PASS_MIN_TRADES}: {needed}")
        print(f"    - estimated additional time: ~{est_days:.0f} days "
              f"(at {rate:.2f} trades/mo, source: {rate_source})")
        print(f"    - during extension: no threshold/parameter changes, no retraining, "
              f"no strategy edits -- same Baseline continues until n>={PASS_MIN_TRADES} "
              f"or an operational fault requires a fix")

print(f"\n7) Execution errors / heartbeat:")
error_lines = []
if os.path.exists(BOT_LOG):
    with open(BOT_LOG, encoding="utf-8") as f:
        for line in f:
            if "ERROR" in line or "loop error" in line or "order failed" in line.lower():
                if "10:58:03" not in line:  # exclude the startup banner match noise
                    error_lines.append(line.strip())
if error_lines:
    print(f"   {len(error_lines)} error line(s) found:")
    for e in error_lines[-10:]:
        print(f"   {e}")
else:
    print("   0 errors/exceptions in bot log since deployment")

if os.path.exists(HEARTBEAT):
    with open(HEARTBEAT, encoding="utf-8") as f:
        hb = json.load(f)
    hb_ts = datetime.fromisoformat(hb["last_success_ts"])
    age_min = (datetime.now(timezone.utc) - hb_ts).total_seconds() / 60
    print(f"   last heartbeat: {hb['last_success_ts']}  (age: {age_min:.1f} min)")
    if age_min > 5:
        print(f"   *** WARNING: heartbeat stale (>5 min) ***")
else:
    print("   *** WARNING: heartbeat file not found ***")

print("\nDONE.")
