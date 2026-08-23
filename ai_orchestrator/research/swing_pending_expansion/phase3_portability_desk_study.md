> **STATUS: UNVERIFIED (2026-08-23).** This report's "in-repo" claims about
> `gold_btc_bot.py` / `backtest_swing_pending_bybit.py` were produced by
> Codex under a sandbox later found to block all shell/file-read access
> (see `pending_signal_shadow_audit/audit_report.md` incident). Whether
> Codex genuinely read those files or inferred plausible-sounding content
> could not be confirmed after the fact (original stderr transcript lost).
> Treat every claim below as unconfirmed until re-verified with facts
> gathered directly (not by Codex file access). Not re-investigated per
> Ahmed's explicit instruction (2026-08-23) -- left as-is, marked only.

# Phase 3 Portability Desk Study: `swing_pending` on MT5 BTCUSDm

## Scope and evidence

This study considers an observe-only, zero-order shadow deployment on EA and BA. XAU is out of scope. Oracle/Bybit TAG-attributed results through 2026-08-19 show BTC PF 3.89 and ETH PF 2.67; XAU PF 0.21 was rejected and paused.

The separate `gold_btc_bot` already trades `BTCUSDm` on EA, BA, and EM using EMA/ADX and momentum gates. Its latest figures are blended across Gold and BTC: 84 trades, PF 1.089, and net +$14.07, with `XAUUSD.s` the best leg at +$23.34 and `XAUUSDm` the worst at -$16.23. These figures do **not** isolate BTC performance and therefore provide no BTC-only profitability evidence for either `gold_btc_bot` or `swing_pending`.

## Strategy and execution comparison

The Bybit `swing_pending` logic identifies confirmed swing pivots, places buy-stop and sell-stop entries at the applicable pivot levels, sizes the stop loss from ATR, and derives take profit from the configured reward-to-risk ratio. Its price distances therefore adapt to volatility rather than depending on a fixed monetary or point distance.

The in-repository MT5 implementation establishes that `BTCUSDm` is already recognized and traded on the target broker accounts. Its execution path uses broker symbol metadata for price/volume handling, permits a configured order-deviation/slippage allowance, and applies ATR-derived stop sizing. This is useful structural evidence: the accounts and existing code path can accommodate a volatile crypto CFD and volatility-scaled protective levels.

That evidence is not enough to declare execution equivalence. A pending stop can be triggered by the broker's executable side of the market while pivots are commonly calculated from chart bars; spread can therefore advance or delay a trigger relative to a Bybit simulation. Broker stop-level and freeze-level restrictions, price normalization, gaps, slippage, and account-specific spread widening can also change whether an order would have been admissible and where it would have filled. The blended `gold_btc_bot` results neither quantify these effects for BTC alone nor validate the pivot-entry strategy.

### Compatibility assessment

`BTCUSDm` is **provisionally compatible** with the pivot-plus-ATR design. Existing live use on EA/BA/EM and ATR-scaled distances support technical portability, while volatility scaling should be more robust to changing BTC price levels than fixed-distance stops. Compatibility remains unconfirmed until shadow telemetry compares pivot level, relevant bid/ask trigger price, spread, broker constraints, hypothetical fill/slippage, ATR stop distance, and RR target on EA and BA separately. No live orders should be submitted during that validation.

## Open items requiring a live read-only MT5 symbol-spec query

The following are explicitly not knowable from the present evidence and must not be guessed:

- Contract size for `BTCUSDm` on EA and BA.
- Tick size and tick value, including whether tick value differs by account or profit/loss direction.
- Minimum lot, maximum lot, and volume step.
- Current live spread and its time-varying distribution on each account.

The same query should also capture digits/point size, trade mode, stops level, freeze level, and permitted order/filling modes before any later execution design is considered.

