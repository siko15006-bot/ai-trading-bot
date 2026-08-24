# Issue #11 Closure Report

Generated: 2026-08-24

## AI Decision Layer

- Root cause found: `windows_bots/alert_decision_gate.py` and `windows_bots/alert_watch_loop.py` were hard-wired to the EA terminal (`C:\MT5_Portable_2\terminal64.exe`), so any BA/EM symbol/account path was analyzed through the wrong MT5 session.
- Fix implemented in repo branch only: gate and watcher now accept/store account scope and switch MT5 sessions per account before fetching read-only M15/H1 bars.
- Follow-up fix: removed the temporary `MT5_*_LOGIN/PASSWORD` ENV dependency and reused the live centralized `ladder_guard.ACCOUNTS` loader used by dashboard/guards. Missing account config now fails closed for that account only instead of blocking the whole watcher loop.
- Live watcher recovery check: local runtime currently has `alert_watch_loop.py` process running, heartbeat fresh, and `active_watches=0`.
- Pending event result: `alert_watches.json` contains no `WATCHING` rows at audit time, so there are no pending events to process. Existing terminal states are `SETUP_CONFIRMED`, `INVALIDATED`, or `EXPIRED`.
- Live restart: not performed.

## gold_btc_momentum_shadow Audit

- Runtime sample: 197 entries, 29 closed outcomes.
- Closed outcomes: 29 wins, 0 losses.
- `mae`: exactly `0.0` for every closed outcome.
- `r_result`: effectively exactly `2.0` for every closed outcome, matching fixed RR TP math rather than proving realized edge.
- Regime coverage: entries observed only from 2026-08-21 to 2026-08-24, dominated by one strong-trend window.
- Conclusion: label as `SINGLE_REGIME / SUSPICIOUS_METRICS / NEED_MORE_DATA`; do not promote or treat as proven edge.

## pending_signal_shadow

- Runtime sample: `signals_received=621`, `closed_total=1`, `triggered=0`, `expired=1`.
- Dashboard readiness remains `NOT_READY`.
- Feed proof failure: `signal_bridge_state.json` does not expose a clear recent `last_signal_ts`/`last_message_ts`; heartbeat proves the engine loop is alive, not that the upstream signal timestamp path is healthy.
- Conclusion: keep `pending_signal_shadow` as `NOT_READY/timestamp-feed path not proven`.

## Scoreboard Labels

- Added `windows_bots/orbit_shadow_scoreboard.py` with conservative labels:
- `gold_btc_momentum_shadow`: `SINGLE_REGIME / SUSPICIOUS_METRICS / NEED_MORE_DATA`.
- `pending_signal_shadow`: `NOT_READY` until timestamp/feed path proof exists.

## Tests

- `python windows_bots\alert_decision_gate.py --self-test`
- `python windows_bots\alert_watch.py --self-test`
- `python windows_bots\alert_watch_loop.py --self-test`
- `python windows_bots\orbit_shadow_scoreboard.py --test`
- `python -m py_compile windows_bots\alert_decision_gate.py windows_bots\alert_watch.py windows_bots\alert_watch_loop.py windows_bots\orbit_shadow_scoreboard.py`
- Read-only MT5 proof after config-loader follow-up: EA `BTCUSDm`, EM `BTCUSDm`, BA `EURUSD.s`; each returned >=50 M15 bars and >=48 H1 bars. Secrets were not printed.

## Boundaries

- No live orders.
- No `gold_btc_bot` SELL logic changes.
- No strategy/risk/threshold/lot/SL/TP changes.
- No BAA freeze/halt or EM execution policy changes.
- No live watcher restart.
- Spec Kit pilot deferred.
