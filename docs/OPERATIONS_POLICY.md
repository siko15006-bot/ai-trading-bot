# Operations Authority Policy

## Purpose
Give the project agents enough standing authority to keep infrastructure healthy without repeatedly interrupting Ahmed, while keeping capital-changing trading decisions behind an explicit approval boundary.

## Standing authority — no new approval required
Codex/operations agents may diagnose and perform reversible operational maintenance when evidence shows it is needed, including:

- Dashboard, monitoring, observability and reporting fixes.
- Logs, heartbeat, stale-state and bookkeeping repair.
- Backup, tests, validation and rollback preparation.
- Restarting a service that is confirmed to be part of the currently approved active inventory and has stopped/crashed.
- Restoring feeds, tunnels, caches and connectivity without changing trading logic.
- Fixing clear implementation/runtime bugs where intended existing behavior is unambiguous and trading parameters are unchanged.
- Read-only audits of MT5, Oracle/Bybit and project state.
- Shadow/research work that cannot place, modify or close real orders.

Operational changes must be minimal, evidenced, backed up when applicable, tested, and reported with before/action/after evidence. Never resurrect retired/paused services merely because their code exists.

## Explicit Ahmed approval still required
A fresh explicit approval is required before any action that intentionally changes financial exposure or the approved live trading policy, including:

- Opening, closing, modifying or cancelling a discretionary real order/position outside already-approved automated logic.
- Adding a new live trading bot/strategy or promoting Shadow/Candidate logic to Production.
- Changing strategy logic, entry/exit rules, thresholds or filters in Production.
- Changing lot/position sizing, leverage, risk limits, SL/TP, portfolio guards or account allocation.
- Disabling an intentionally active protection/risk control.
- Retiring/disabling an intentionally active live trading strategy for strategic rather than emergency operational reasons.

Trust or broad project-success intent is not treated as blanket authorization for discretionary live trading. Capital-changing decisions stay explicit so the exact action and risk are known before execution.

## Emergency safety exception
If a clear technical malfunction is creating unintended duplicate orders, runaway execution, credential/security exposure, or behavior that contradicts already-approved trading logic, agents may take the minimum reversible containment action needed to stop the malfunction (for example blocking new unintended entries or stopping the malfunctioning process). Do not create a new market view or discretionary trade as an emergency action. Report immediately with evidence and rollback state.

## Agent allocation / limit efficiency
- Codex: implementation, operational diagnosis, patches, tests, deployment/rollback under this policy.
- ChatGPT: coordination, independent GitHub review, safety gate, prioritization and approval-boundary enforcement.
- Claude: reserve for architecture, difficult root-cause analysis or independent high-value review when Codex/ChatGPT cannot resolve efficiently.

## Production deployment discipline
For an authorized operational Production change: diagnose first; take backup; apply the smallest change; restart only the affected service when required; run post-deploy health checks; rollback on regression; record branch/commit/evidence. Trading bots must not be restarted merely because a dashboard or monitoring component changed.
