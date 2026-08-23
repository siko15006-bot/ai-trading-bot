"""
portfolio_risk_guard.py -- بند 8ل: قاطع دائرة خسارة يومي على مستوى المحفظة (4 حسابات).
مش بوت مستقل بيتصل بـMT5 لوحده (اتعلمنا الليلة إن الاتصالات المباشرة المتكررة بتعارض
مع البوتات الحية) -- ياخد أرقام equity الأربعة كـargs من نفس الفحص اللي بيحصل كل دورة
مراقبة أصلاً عبر MCP، ويحسب/يخزن baseline اليوم + نسبة الهبوط.

الاستخدام (من دورة المراقبة):
    python portfolio_risk_guard.py --ea 167.16 --em 45.11 --ba 124.34 --baa 98.47

المخرج: HALT إذا اتخطينا حد الخسارة اليومي (افتراضي 15%)، وإلا OK.
عند HALT: بيتكتب RISK_HALT.flag (الملف ده المفروض تفحصه أي وظيفة تفتح صفقة جديدة --
التوصيل الفعلي لبوتات ema_adx/orb/fx_signal_exec مؤجل لمراجعة الأحد بإذن أحمد).
الإشعار الفعلي لأحمد مش من هنا -- الدورة اللي بتنده السكريبت ده هي اللي تبعت
PushNotification لما تشوف HALT في الناتج (نفس نمط قراءة تريب واير fast_move/grind).
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

STATE_FILE = os.path.join(os.path.dirname(__file__), "portfolio_risk_state.json")
FLAG_FILE = os.path.join(os.path.dirname(__file__), "RISK_HALT.flag")
DEFAULT_THRESHOLD_PCT = 15.0

# 2026-08-19 (Ahmed-approved, reviewed in C:\TradingBot\Review_RiskResetMarketOpen_2026):
# day-rollover boundary re-keyed from UTC midnight to 01:00 Africa/Cairo
# (DST-safe via zoneinfo -- Egypt currently observes DST, confirmed by
# direct tzdata scan, not assumed). This is the ONLY behavioral change --
# same "new day -> fresh baseline, halted=False, remove flag" policy as
# before, just a different definition of "new day". Threshold, drawdown
# formula, EM exclusion, and every other line are untouched.
CAIRO_TZ = ZoneInfo("Africa/Cairo")
RESET_HOUR = 1
RESET_MINUTE = 0


def _reset_day_id(utc_dt):
    """Which daily reset-cycle a UTC timestamp belongs to, using 01:00
    Africa/Cairo as the once-daily boundary. Before that boundary, a
    timestamp still belongs to the PREVIOUS calendar date's cycle."""
    cairo = utc_dt.astimezone(CAIRO_TZ)
    boundary_today = cairo.replace(hour=RESET_HOUR, minute=RESET_MINUTE, second=0, microsecond=0)
    if cairo >= boundary_today:
        return cairo.date()
    return cairo.date() - timedelta(days=1)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                # empty/corrupted state file (e.g. a write got interrupted) --
                # treat exactly like "no state yet" so a fresh baseline gets
                # written below, instead of crashing the whole guard.
                return None
    return None


def save_state(state):
    # atomic write (temp file + os.replace) -- a plain open("w") left the
    # state file empty at least once (likely a write interrupted mid-flight,
    # possibly by risk_guard_autorun.py's scheduled task and a manual
    # monitoring-cycle call landing at the same time), which then crashed
    # every subsequent run via load_state()'s json.load.
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ea", type=float, required=True)
    p.add_argument("--em", type=float, required=True)
    p.add_argument("--ba", type=float, required=True)
    p.add_argument("--baa", type=float, required=True)
    p.add_argument("--threshold-pct", type=float, default=DEFAULT_THRESHOLD_PCT)
    args = p.parse_args()

    # 2026-08-17 (Ahmed-approved, explicit): EM excluded from the portfolio
    # total. EM is a third-party's account Ahmed manages, but EM also runs
    # its own independent copy-trading/signal subscription outside our
    # infrastructure (confirmed 2026-08-17: real trades with foreign magic
    # numbers/comments -- "AutoTrailBuy", "YUVI FX TRADER NEXORA" -- with
    # zero local EA files or Experts-log trace on MT5_Portable_3, i.e. not
    # anything of ours). Counting that activity against OUR risk budget
    # isn't meaningful. --em is still required and still logged for
    # visibility, just never summed into the halt-triggering total.
    total = args.ea + args.ba + args.baa
    now_utc = datetime.now(timezone.utc)
    current_reset_day = _reset_day_id(now_utc).isoformat()

    state = load_state()
    if state is None or state.get("last_reset_date") != current_reset_day:
        state = {"last_reset_date": current_reset_day, "baseline_equity": total, "halted": False}
        if os.path.exists(FLAG_FILE):
            os.remove(FLAG_FILE)
        print(f"NEW_DAY baseline={total:.2f} reset_day={current_reset_day}")

    baseline = state["baseline_equity"]
    drawdown_pct = (baseline - total) / baseline * 100 if baseline > 0 else 0.0

    if drawdown_pct >= args.threshold_pct and not state["halted"]:
        state["halted"] = True
        with open(FLAG_FILE, "w") as f:
            f.write(f"HALT {current_reset_day} drawdown={drawdown_pct:.2f}% baseline={baseline:.2f} current={total:.2f}\n")
        print(f"HALT drawdown={drawdown_pct:.2f}% threshold={args.threshold_pct}% baseline={baseline:.2f} current={total:.2f}")
    elif state["halted"]:
        print(f"HALTED_ACTIVE drawdown={drawdown_pct:.2f}% baseline={baseline:.2f} current={total:.2f}")
    else:
        print(f"OK drawdown={drawdown_pct:.2f}% baseline={baseline:.2f} current={total:.2f}")

    save_state(state)


def _self_test():
    """ponytail: smallest check that fails if EM leaks back into the total."""
    import subprocess
    import sys
    import tempfile
    global STATE_FILE, FLAG_FILE
    orig_state, orig_flag = STATE_FILE, FLAG_FILE
    d = tempfile.mkdtemp()
    STATE_FILE = os.path.join(d, "state.json")
    FLAG_FILE = os.path.join(d, "flag")
    try:
        # first call of a "day" sets baseline -- em must not count even here
        sys.argv = ["x", "--ea", "100", "--em", "999", "--ba", "100", "--baa", "100"]
        main()
        with open(STATE_FILE) as f:
            st = json.load(f)
        assert st["baseline_equity"] == 300.0, f"em leaked into baseline: {st['baseline_equity']}"

        # a huge em swing alone must never trigger halt
        sys.argv = ["x", "--ea", "100", "--em", "0.01", "--ba", "100", "--baa", "100"]
        main()
        with open(STATE_FILE) as f:
            st = json.load(f)
        assert st["halted"] is False, "em-only crash incorrectly triggered halt"

        # a real ea/ba/baa drawdown must still trigger halt normally
        sys.argv = ["x", "--ea", "80", "--em", "999999", "--ba", "80", "--baa", "80"]
        main()
        with open(STATE_FILE) as f:
            st = json.load(f)
        assert st["halted"] is True, "real drawdown on ea/ba/baa failed to trigger halt"
        assert os.path.exists(FLAG_FILE)
    finally:
        STATE_FILE, FLAG_FILE = orig_state, orig_flag
    print("self-test OK (em excluded from baseline + total, halt logic intact)")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _self_test()
    else:
        main()
