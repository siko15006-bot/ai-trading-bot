# Phase 3 — Live MT5 Symbol Telemetry: BLOCKED_PENDING_SAFE_MT5_TELEMETRY

Investigated (read-only, 2026-08-23) for a safe way to fetch BTCUSDm
contract_size/tick_size/tick_value/volume_min/volume_step/spread/trade_mode
on EA and BA without risking a second MT5 terminal instance:

- Checked running `terminal64.exe` processes (read-only `Get-Process`):
  MT5_Portable_2 (EA) and MT5_Portable_3 (EM) are already running; a third
  untitled-path instance (likely BA) is also up. No new-instance action taken.
- Found two already-running local HTTP bridges (`MT5Bridge` port 5001/5002,
  `MT5Bridge_E2`) with a live `get_symbol_info()` path in
  `app/mt5_utils.py`, but: (a) the only field set it fetches is
  point/tick_size/tick_value/volume_min/volume_max/volume_step/digits --
  contract_size, spread, and trade_mode are not fetched at all; (b) none of
  the existing HTTP routes (`/risk/lot-size` etc.) return those raw fields --
  only a derived lot-size calculation.
- Getting the full field set would require either (a) adding a new route to
  a production bridge file (forbidden -- production trading file), or
  (b) a fresh `mt5.initialize()` call from a new process (forbidden -- second
  MT5 instance risk).

**Conclusion: no safe existing channel exposes the needed fields. Blocked,
not guessed.** Unblocks when either an approved read-only MT5 MCP tool
becomes available in-session, or Ahmed approves adding a narrowly-scoped
read-only `/symbol-info/{symbol}` route to one bridge (explicit approval
needed first -- it's a production file).
