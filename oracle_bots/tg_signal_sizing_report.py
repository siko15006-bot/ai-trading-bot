"""
tg_signal_sizing_report.py -- READ-ONLY. Parses SIZING_DIAG lines from
/tmp/tg_signal.log (written by tg_signal_bot.py's gold_our_risk path) and
prints a summary: how many trades were risk-based vs margin-capped, average
dollar risk, average risk as % of equity at trade time. Does not touch the
live bot, does not place/modify/cancel any order.
"""
import re

LOG = "/tmp/tg_signal.log"

pattern = re.compile(
    r"SIZING_DIAG (?P<group>\S+): risk_qty=(?P<risk_qty>[\d.]+) "
    r"margin_cap_qty=(?P<margin_cap_qty>[\d.]+) chosen_qty=(?P<chosen_qty>[\d.]+) "
    r"source=(?P<source>[\w-]+) dollar_risk=\$(?P<dollar_risk>[\d.]+) "
    r"margin_used_est=\$(?P<margin_used>[\d.]+) margin_pct_est=(?P<margin_pct>[\d.]+)%"
)

rows = []
with open(LOG, errors="replace") as f:
    for line in f:
        m = pattern.search(line)
        if m:
            rows.append(m.groupdict())

if not rows:
    print("No SIZING_DIAG entries found yet (log may have rotated, or no gold_our_risk trades since restart).")
    raise SystemExit(0)

risk_based = [r for r in rows if r["source"] == "risk-based"]
margin_capped = [r for r in rows if r["source"] == "margin-capped"]
avg_dollar_risk = sum(float(r["dollar_risk"]) for r in rows) / len(rows)
avg_margin_pct = sum(float(r["margin_pct"]) for r in rows) / len(rows)

print(f"Total gold_our_risk trades logged: {len(rows)}")
print(f"  risk-based:     {len(risk_based)}")
print(f"  margin-capped:  {len(margin_capped)}")
print(f"Average dollar risk per trade:      ${avg_dollar_risk:.4f}")
print(f"Average margin used (% of equity):  {avg_margin_pct:.2f}%")
