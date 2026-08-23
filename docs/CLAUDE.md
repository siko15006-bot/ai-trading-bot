# TradingBot — Structure Reference

Live trading system: 4 broker accounts, local Windows bots + oracle cloud bots. This file
covers code/file structure only. Trading rules, risk preferences, and decision history live
in a private persistent memory system, not in this repo.

## Account map (MCP server name → nickname → broker)
- `metatrader` → **BA** (Bybit MT5, currency "UST", leverage 500)
- `metatrader-exness` → **EM** (Exness, leverage 2000) — gold trading permanently banned on this account
- `metatrader-ea` → **EA** (leverage 2000)
- ccxt/Bybit via oracle SSH → **BAA** (crypto + XAU/USDT perpetual, `~/trading-bot/` on the Oracle cloud host)

Symbol names differ per broker for the same instrument:
- NAS100: EA/EM = `USTECm`, BA = `NAS100.s` (min lot 0.1 on BA, 0.01 elsewhere)
- USOIL: EA/EM = `USOILm`, BA = `USOUSD.s` (do not confuse with `GASOIL-C.s` on BA — different commodity)
- XAU: EA/EM = `XAUUSDm`, BA = `XAUUSD.s`, BAA = `XAU/USDT:USDT`
- BA's candle history for NAS100.s/USOUSD.s can be stale/broken — live price is fine, use EA candles for structure analysis on those symbols.

## Local bots (`windows_bots/`)
Each `<name>.bat` wraps the matching `.py` in a `:loop` that restarts it after a 15s pause if
the process exits (crash-loop protection, not a scheduler). Logs: `<name>.log` or `<name>_out.log`
depending on age — check `ls -la --time-style=full-iso *.log` for the actually-active one, this
directory has stale abandoned logs from prior naming schemes.

- `ea_shield.py` (arg: BA/EA/EM) — per-account position guard
- `ladder_guard.py` (arg: BA/EA/EM) — step trailing: BE@50%, lock50%@75%, lock75%@90%, runs every 15s
- `ema_adx_bot.py` (arg: BA/EA) — validated EMA+ADX strategy, XAUUSD, SL=200/TP=600, magic 993399(BA)/993400(EA). Supervised separately by `ema_adx_watchdog.ps1` (Startup folder `run_ema_adx_watchdog.vbs`), not by the plain `.bat` loop.
- `fx_signal_exec.py` — Learn2Trade + gold Telegram groups → executes on EA+BA only (magic 884400). Telegram session file `fx_session_new.session` — NEVER run from two devices at once (AuthKeyDuplicatedError kills it); separate from oracle's tg_signal_bot session on purpose.
- `fast_move_watch.py` / `grind_watch.py` — tripwire pollers (30s/90s), log-only, no execution. Watched symbols/thresholds are in the script headers.
- `orb_eth_exness.py` — ORB pullback bot, validated on SOL/ETH (not BTC)
- `portfolio_risk_guard.py` — run manually with `--ea --em --ba --baa` (current equities) each cycle; returns OK/HALT based on daily drawdown vs day-start baseline
- `status_dashboard.py`, `e2_ctl.py`, `em_ctl.py`, `check_bot_errors.py` — utility/monitoring, not trading logic
- `claude_gold_agent_skeleton.py` — standalone experiment, not live

## Startup (how everything comes back up after reboot/logon)
Real launcher: `TradingBot_Startup_Master.bat` in the Windows Startup folder (hidden). A
Scheduled Task also fires on logon but its target is deliberately neutered (no-op) — it used
to double-launch things. `run_ema_adx_watchdog.vbs` and `TradingView_CDP_AutoStart.vbs` are
separate Startup entries.

## Oracle cloud bots (`oracle_bots/`, systemd/screen-independent of the Windows machine)
`liquidity_sweep_bot.py` (BTC short-only, Sweep+Break, no FVG), `orb_bot.py`, `bybit_shield.py`,
`crypto_watch.py`, `tg_signal_bot.py` (gold/crypto Telegram groups → BAA only, has its own
daily circuit breaker at -5% equity that resets each UTC day).

## MCP / bridge
`MT5Bridge/server.py` and `MT5Bridge_E2/server.py` are the local API servers the `metatrader`/
`metatrader-ea` MCP tools talk to. MCP server credentials (logins/passwords) are never stored
in this repo. Oracle-side scripts read Bybit/Telegram credentials from environment variables
(`os.environ`/`os.getenv`) at runtime; Windows-side MT5 scripts originally held them as local
literals and any occurrence has been replaced in this public copy with an `<REDACTED_...>`
placeholder — the private, credentialed copies stay on the account owner's own machines only.
