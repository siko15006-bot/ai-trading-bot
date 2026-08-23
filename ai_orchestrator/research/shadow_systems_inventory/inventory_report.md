# Shadow Systems Inventory

| System | Purpose (from memory, marked as such) | RUNNING/STOPPED/UNKNOWN | PID(s) | Prior decision status (if known) | Follow-up needed |
|---|---|---|---|---|---|
| `hypergold_scalp_shadow` | Not provided | RUNNING | Launcher: 7216 | Unknown | Investigate purpose and prior decision status |
| `pending_signal_shadow_engine.runner` | BTC + Forex pending-order shadow *(from memory)* | RUNNING | 15456 | Already assessed; NEEDS MORE DATA | Continue data collection; do not re-audit |
| `participation_pilot_btc_range` | BTC range strategy, live on EA magic 995500 *(from memory)* | RUNNING | Launcher: 6468; worker: 4020 | ACTIVE; 30-day freeze through 2026-09-06 | Respect freeze; review after it ends |
| `gold_btc_momentum_shadow` | Momentum-only ablation *(from memory)* | RUNNING | Launcher: 16236; worker: 14188 | ACTIVE SHADOW; Phase-0 passed and decision settled | Do not touch until observation period ends |
| `pending_breakout_shadow.runner` | XAUUSD straddle pending-order shadow *(from memory)* | RUNNING | 13364 | KEEP; lifecycle/startup-recovery work DONE and verified 2026-08-21 | None; do not redo completed work |
| `C:\TradingBot\Research_ManualExitShado...` | Unknown; exact script not confirmed | UNKNOWN | 11100 | Unknown | Manual check required to identify purpose and status |
| `C:\TradingBot\research_ml\strategy_discovery\...` | Unknown; exact script not confirmed | UNKNOWN | 16960 | Unknown | Manual check required to identify purpose and status |

Settled: participation pilot is ACTIVE under freeze, momentum shadow is ACTIVE/already decided, and pending breakout is KEEP/DONE. Next investigation: identify both UNKNOWN processes and assess `hypergold_scalp_shadow`; `pending_signal_shadow_engine.runner` only needs more outcome data.
