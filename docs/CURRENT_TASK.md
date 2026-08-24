# CURRENT_TASK.md

Short handoff between Claude and Codex. Only the active task — no project
history here (see `PROJECT_GUARDRAILS.md` for standing rules).

Overwrite this file's content (not append) whenever a new task starts or an
owner handoff happens.

---

## task_id (= investigation_id, same identifier, kept for compatibility)
orbit-final-closure-20260824

## owner
CODEX

## status
REVIEW

## created_at
2026-08-24

## updated_at
2026-08-24

## Objective
ORBIT final closure sprint: close current Dashboard V2 evidence, auto-exec
shadow proof, pending-signal readiness evidence, shadow scoreboard status,
swing_pending_bybit read-only reconciliation, and backlog dedup.

## investigation_id
orbit-final-closure-20260824

## Scope
- No live orders.
- No deploy before ChatGPT review.
- No strategy, risk, threshold, lot, SL, or TP changes.
- No new projects.
- Use existing GitHub issues for milestone documentation.

## Known Evidence (= evidence)
- Dashboard V2 proof is on branch `issue-7-dashboard-v2-p0`.
- Auto-exec shadow is parsing real BTC/XAU candidates but has not yet seen an
  A-grade eligible setup in the latest 24h proof window.
- pending_signal_shadow process is alive but upstream signal source has no new
  unique records; it remains NOT_READY.
- swing_pending_bybit live process is stopped; Oracle read-only reconciliation
  is CLEAN/idempotent with zero matching orders and zero positions.

## Files likely involved (= exact_files)
`docs/ISSUE7_DASHBOARD_V2_P0_PROOF.md`,
`docs/BACKLOG_CLEANUP_20260824.md`,
`docs/ORBIT_FINAL_CLOSURE_20260824.md`.

## Exact requested change (= expected_output)
Final readiness report and GitHub issue comments only.

## Forbidden changes (= constraints)
Any broker order, live trading execution, strategy/gate/threshold change,
SL/TP/lot change, production deploy, BAA freeze/halt change, EM execution
policy change, or new project branch.

## Required tests (= done_criteria)
- Dashboard V2 py_compile/preflight/self-test proof.
- Auto-exec shadow functional candidate parse proof.
- pending_signal_shadow feed/timestamp proof.
- swing_pending_bybit Oracle read-only repeated reconciliation proof.
- GitHub comments on active issues.

## Current status (= status: ACTIVE)
REVIEW — closure evidence gathered; no deploy performed.

## Next owner (= owner: AHMED/CHATGPT)
Review Dashboard V2 evidence and auto-exec shadow evidence before any live
activation or deploy.

## Workflow rule
Before any new investigation:
1. Check `CURRENT_TASK.md`.
2. Check the relevant Work Queue item status.
3. If the same `investigation_id` or same topic is already covered, do not
   repeat the investigation unless there is new data or a clearly different
   scope.
