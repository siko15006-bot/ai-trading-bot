# Issue #7 Dashboard V2 P0 Proof

Generated: 2026-08-24

## Scope

- Dashboard/observability only.
- No bot, strategy, risk, order, threshold, lot, SL, or TP changes.
- No deploy performed.
- No extra Oracle SSH polling added; P0 uses existing dashboard caches.

## Branch

- `issue-7-dashboard-v2-p0`

## Local UI Proof

- `/monitor`: HTTP 200, 38994 bytes, 13494.9 ms cold/warm mixed test-client request.
- `/api/monitor`: HTTP 200, 23292 bytes, 726.6 ms warm test-client request.
- P0 command strip payload keys:
  `ai`, `correlated_warnings`, `counts`, `data_source_map`, `exposure`, `feed`, `health`, `portfolio`, `risk`.

## Mobile Proof

- Monitor page includes viewport meta:
  `width=device-width, initial-scale=1, viewport-fit=cover`.
- Command strip grid is mobile-first and expands with media query at 760px.

## Endpoint Tests

- `python -m py_compile windows_bots\status_dashboard.py windows_bots\portfolio_analytics.py`
- `python windows_bots\portfolio_risk_guard.py --self-test`
- `python windows_bots\status_dashboard.py --test`
- Flask test client: `/monitor` 200, `/api/monitor` 200.

## Cairo Reset

- Shared operational day comes from `portfolio_risk_guard._reset_day_id`.
- Proof:
  `2026-08-23T21:59:00Z -> 2026-08-23`
  `2026-08-23T22:00:00Z -> 2026-08-24`
- Risk halt detail now renders `daily rollover (01:00 Africa/Cairo)`.

## OIL Exposure

- `portfolio_analytics.asset_exposure_summary()` default assets now include `OIL`.
- Dashboard bucket proof:
  `USOILm -> OIL`
  `UKOIL.cash -> OIL`
  `XTIUSD -> OIL`
- `/api/monitor.asset_exposure` includes `OIL`.

## Data-Source Map

- `portfolio_balance_equity_floating`: `_cache.accounts` from existing refresh loop.
- `realized_today_7d_30d`: `portfolio_analytics.fetch_all_closed_trades()` via TTL-cached `_get_trades(days=30)`.
- `health_score_deductions_root_cause`: `_observability_model()` from existing dashboard data.
- `bot_counts`: `_observability_model().components`; no process control.
- `exposure_by_account_asset`: cached open positions.
- `correlated_exposure_warning`: derived from current open positions only.
- `risk_halt_protection`: `portfolio_analytics.risk_halt_status()` plus cached protection labels.
- `ai_status`: official CLI status only; quota remains `UNAVAILABLE`.
- `market_sessions_feed_freshness`: deterministic UTC session windows plus cache/component ages.
- `oracle_pressure`: reuses `_cache.oracle_meta` and `_oracle_attr_cache`; no extra Oracle SSH polling.

## Secrets Safety

- Removed invalid redacted Python placeholders from `portfolio_analytics.py`.
- EA/EM optional account credentials now come only from environment variables:
  `MT5_LOGIN_EA`, `MT5_PASSWORD_EA`, `MT5_LOGIN_EM`, `MT5_PASSWORD_EM`.
- No secrets added.

## Deploy Status

- Not deployed.
- ChatGPT review required before deploy.

## Road To $500 Addendum

- Added `/monitor` ROAD TO `$500` card and `/api/monitor.road_to_500`.
- `$300` is rendered as first milestone/completed when eligible live equity is at least `$300`.
- `$500` is rendered as the active target.
- Current eligible equity is summed from live dashboard `accounts` cache only.
- Included accounts in live proof: Bybit MT5 `$133.02`, E2/EA `$120.57`, Oracle/BAA `$69.24`.
- Excluded accounts in live proof: E1/EM, reason `EM/E1 read-only execution policy`.
- Live proof values with runtime challenge baseline `198.62`: eligible equity `$322.83`, remaining `$177.17`, progress `41.2%`, profit from baseline `$124.21`, peak `$322.83`, current drawdown `0.0%`.
- Visualization includes `START -> $300 -> $350 -> $400 -> $450 -> $500`.
- Removed the old visible 7-day challenge/deadline block from `/monitor`; legacy API field remains for compatibility.
- No deploy performed.
