# Hypergold Scalp Shadow Assessment

## Safety

The 277-line `run_shadow_check.py` has no `order_send` call or other confirmed
real-order-call path. Its MetaTrader 5 usage is limited to initialization,
symbol inspection/selection, bar retrieval, and shutdown. Each five-minute
cycle attaches by IPC to the continuously running EA terminal and then
disconnects; it does not create a duplicate-terminal risk.

The shadow is pinned to the EA account and `XAUUSDm`. It raises `RuntimeError`
if persisted state contains a different account or symbol, guarding against
silent drift. Its scope is only the scalp-entry branch; Regular, SMART_AI, and
XAURush are explicitly excluded.

## Sample Size

The shadow began at `2026-08-17T17:50:00 UTC`, approximately 5.5 days before
the checked snapshots. Recent history reports zero shadow trades and therefore
`n=0`, with WR, PF, net, and maximum drawdown represented as zeros. These are
empty-sample values, not measured performance. Zero trades are insufficient
for any conclusion about win rate, profit factor, expectancy, or drawdown.

## Signal Selectivity

An entry must pass five combined gates: M5 fast-EMA momentum alignment; matching
M15 fast/slow EMA trend alignment; direction-specific RSI(9) bounds; acceptable
candle geometry, including configured body-size and wick limits; and two
consecutive candles in the same direction. This is a narrow multi-condition
filter, so no signals across the observed period is plausible. The evidence
cannot yet distinguish a rare but functioning setup from thresholds that are
overly tight, and neither interpretation should be treated as established.

## Data Feed Note

Recent snapshots were checked through `2026-08-23 06:05 UTC`, while
`data_through` remained at Friday `2026-08-21 21:00 UTC`. This matches the
normal weekend gold/FX closure through the Sunday 21:00 UTC reopen and is not
flagged as a data-feed bug. Advancement should resume after reopening.

## Open Questions

- Whether backup files named `shadow_history.jsonl.bak_broker_mix_20260818_125727` indicate the current active history file is a continuous stream since 2026-08-17 or was reset by that 2026-08-18 broker-mix fix was not independently verified.
- The exact `CFG.scalping_min_points`/`scalping_max_points`/`max_candle_wick` threshold constants were not read, so whether the signal filter is "rare but working" versus "overly tight" remains open.

## Recommendation

NEEDS MORE DATA

Zero trades in ~5.5 days is not a usable sample for any performance judgment; safety and account/symbol guards are sound; recommend continuing to observe past the weekend reopen and revisiting once a real trade sample exists, or investigating the threshold constants if it remains at zero for a materially longer period.
