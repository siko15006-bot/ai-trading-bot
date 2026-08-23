# Pending Signal Shadow Audit

## Safety

The engine is confirmed SHADOW/OBSERVE-only and structurally incapable of placing real orders:

- `ShadowBroker.place_order`, `modify_order`, and `close_order` unconditionally raise `RuntimeError` and do not import MT5.
- `install_mt5_execution_guard()` monkey-patches `MetaTrader5.order_send`; startup verifies the patch by function name and aborts before connecting to MT5 if verification fails.
- `mt5_source.py`, the only MT5 importer, makes only read-only calls and contains no `order_send` call.
- The inspected engine path therefore contains zero real-order calls.
- Startup acquires a single-instance lock and aborts if another instance owns it, preventing concurrent duplicate runners.
- PID 15456 is live, with a recent heartbeat, `mt5_connected=true`, `guard_active=true`, zero open orders, and 621 processed signals since 2026-08-19.

## Lifecycle

The validated state machine is `CREATED -> WAITING -> {TRIGGERED, EXPIRED, INVALIDATED} -> {TP, SL}`; invalid transitions raise `RuntimeError`.

Trigger and exit handling correctly use executable prices:

- A `BUY_STOP` triggers when ask is at or above entry; a `SELL_STOP` triggers when bid is at or below entry.
- BUY exits use bid, while SELL exits use ask.
- Cost modeling uses the live bid-ask spread at signal time: `cost_r = spread / risk_distance`, then `result_r_after_cost = result_r - cost_r`.

These lifecycle, fill, exit, and cost conventions are confirmed correct and executable-price-based.

## Sample Data

- 621 signals were received: 574 gold signals were logged separately and excluded from BTC+Forex execution simulation.
- The 47 evaluated non-gold setups comprised 1 `BUY_STOP` order and 46 `SKIP` decisions.
- Symbols were BTCUSDm 11, GBPJPYm 10, EURUSDm 8, GBPUSDm 9, ETHUSDm 7, USDCADm 1, and EURGBPm 1.
- The sole order was BTCUSDm BUY_STOP order 1 from `manual_live_verification_20260819`: entry 70449.84, SL 69785.61, TP levels 71114.08/71778.31/72442.54.
- It expired without triggering, with exit price 68671.55 and no R result.
- Dashboard totals are: closed 1, triggered 0, expired 1, invalidated 0; win rate, expectancy R, and profit factor are null.

The BTC+Forex outcome sample is effectively zero: one expired order, never triggered, and no closed trade with an outcome.

## Bottleneck Finding

Of 47 evaluated setups, 46 were skipped; 45 were `STALE_SIGNAL_AGE` and one was `SYMBOL_NOT_IN_EXECUTION_ALLOWLIST`. Thus 45/47, approximately 96%, were already older than the configured 900-second limit when evaluated. With a five-second runner poll interval, this points to a timestamping or delivery-latency issue upstream rather than runner-loop speed. Follow up on how the signal source stamps `signal_timestamp` and when signals reach `incoming_signals.jsonl`; no more specific root cause is established here.

## Code Findings

1. **Low severity â€” replay/duplicate setup risk:** `processed_count` advances in memory after ingestion but is persisted only at the end of the loop, after open-order advancement. A crash in that window can replay a batch after restart and duplicate setup records. This cannot create real orders because the engine is shadow-only.
2. **Low severity â€” order ID collision risk:** the module-level `itertools.count(1)` resets on restart while restored open orders retain prior IDs. A newly created order can numerically collide with a restored open order if both coexist. No real money is at risk in shadow mode.

## Recommendation: NEEDS MORE DATA

Safety controls and executable-price lifecycle modeling are sound, while the two code risks are low-severity shadow-data integrity issues. However, there is no triggered BTC+Forex outcome sample from which to assess performance: the only order expired untriggered, and approximately 96% of evaluated setups were stale. Keep the engine observing, investigate upstream timestamping and delivery latency, address the restart integrity findings, and defer any performance conclusion until a meaningful triggered-and-closed sample exists.
