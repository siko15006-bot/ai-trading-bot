# Spec Kit Pilot Evaluation Plan

Status: SANDBOX / ISOLATED BRANCH ONLY

## Goal
Measure whether the task-contract + constitution workflow reduces agent errors, rework, and token/limit waste versus the prior ad-hoc workflow without weakening safety.

## Test design
Use matched, non-production `ai_orchestrator/` sandbox tasks only. For each task, record a baseline/ad-hoc run when evidence exists and a contract-governed run using `task_contract.template.json`.

Minimum sample before any conclusion: 6 comparable tasks, ideally 3 small and 3 medium.

## Per-task metrics
- Codex calls
- Claude calls
- retries
- review cycles
- out-of-scope diff: yes/no
- scope misunderstanding: yes/no
- deterministic validation failures
- approximate prompt/context size when available
- production-boundary violation attempt: yes/no

## Pass rule
Do not call the pilot successful unless at least two of the following improve by >=20% across the matched sample, with zero increase in production-boundary violations:
- total agent calls per task
- retries/rework
- review cycles
- out-of-scope diffs / scope misunderstandings
- prompt/context size

## Execution rule
1. Reject/clarify incomplete task contracts before any model call.
2. Run deterministic checks before Claude review.
3. One Codex implementation attempt by default.
4. Claude review only for high-risk, ambiguous, or explicitly review-required diffs.
5. No writes outside `ai_orchestrator/` pilot/test areas.
6. No merge to `main` and no changes to trading strategies, thresholds, risk settings, production bots, or production execution.

## Current conclusion
INSUFFICIENT_DATA. The governance files are in place, but there is not yet a large enough matched task sample to claim lower token use or lower error/rework rates.
