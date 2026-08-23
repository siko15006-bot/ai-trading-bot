# Dashboard Gap Audit

## Current Coverage

- Dedicated result-display sections already exist for `pending_breakout_shadow`,
  `pending_signal_shadow`, and `manual_exit_shadow`; each reads its own state or
  summary JSON files and renders a panel.
- The process-liveness and heartbeat table covers many bots, including
  `hypergold_scalp_shadow` as RESEARCH with a 3600-second expected heartbeat and
  `participation_pilot_btc_range` as TRADING with a 300-second interval.
- `freshness_state(age_sec)` labels data LIVE or UNKNOWN based on recency. It
  measures data staleness, not whether a system trades real money or runs shadow.
- `/analytics`, `/trades`, `/risk`, `/reports`, `/alerts`, and `/monitor` routes
  exist with corresponding `/api/*` JSON endpoints.

## Confirmed Gaps

1. Rejected-opportunity tracking is absent. The dashboard explicitly sets
   `view['rejected_orders'] = 'NOT_AVAILABLE'` because no such tracking exists.
   Capturing why an opportunity was rejected and which gate rejected it would
   directly reduce time spent reconstructing decisions during future investigations.
2. `gold_btc_momentum_shadow` has no dashboard references, heartbeat entry, or
   results panel despite being an active, decision-relevant shadow experiment.
   Making its status and results visible would prevent investigators from searching
   elsewhere to establish whether it is running and how it is performing.
3. `hypergold_scalp_shadow` appears only in the liveness table and has no results
   panel for shadow trade count, win rate, or profit factor. Surfacing its available
   results, including zero-trade states, would shorten performance investigations.

## Partial Coverage

- LIVE real-money versus SHADOW observe-only status is distinguishable through
  section and panel placement, but no consistent explicit per-bot or per-item tag
  exists. Adding one would make classification immediate and less error-prone.

## Recommended Next Step

These are observability-only additions (read more existing state files, render
more panels) with zero impact on trading logic, strategy, risk, or thresholds.
`status_dashboard.py` is a production file serving a live monitoring dashboard;
implementing any of the gaps above requires Ahmed's separate explicit approval
before a real code change is made. This task is audit-only -- no implementation
is proposed or attempted here.

