# Manual Exit Shadow: Dedup and Status Report

## Dedup Determination

This is **not a duplicate** of `ManualTradeTracking`. The older project studies whether the bot's own trade-management exits manual/MCP trades (`magic=0`) too early, using post-exit MFE observation. This later project studies whether Ahmed's manual closure of bot-originated positions helped or hurt versus leaving their original plans intact, using counterfactual continuation tracking. These are distinct research questions and mechanisms.

**Decision: CONTINUE, not SKIP.**

## Safety

Direct code review confirmed that the tracker is read-only: it places no orders, modifies no SL/TP levels, and changes no bot logic. It uses MetaTrader5 for read access and the shared `credentials.get_account` helper; no order-placement call was found in the reviewed code.

## Current Status

- `HISTORICAL_BACKFILL`: 313 snapshots and 313 resolutions. This phase predates a fix, is preserved for audit only, and cannot contribute to the final judgment.
- `POST_FIX_LIVE`: began 2026-08-21T15:52 UTC and had 27 snapshots and 24 resolutions after approximately 1.5 days.
- Three post-fix-live snapshots remain pending counterfactual resolution.

The current verdict is **INCONCLUSIVE** because there are 0 eligible post-fix resolved rows. A verdict requires both high-confidence manual-exit attribution and a reliable original SL/TP; those conditions have not yet co-occurred sufficiently in the short live window. This means there is not enough eligible post-fix-live data, not that the mechanism has failed.

## Counterfactual Resolution Note

Across both phases, outcomes are TP-first=0, SL-first=0, timeout=193, and unresolved=144. The timeout- and unresolved-heavy distribution is an open question about whether the 24-hour lookahead window adequately captures planned outcomes under observed market behavior; it is not established as a bug.

## Recommendation

**CONTINUE.** Keep the tracker running and re-check once `POST_FIX_LIVE` accumulates enough eligible rows for a real verdict. No code change, threshold change, or production change is proposed.
