# CURRENT_TASK.md

Short handoff between Claude and Codex. Only the active task — no project
history here (see `PROJECT_GUARDRAILS.md` for standing rules).

Overwrite this file's content (not append) whenever a new task starts or an
owner handoff happens.

---

## task_id (= investigation_id, same identifier, kept for compatibility)
manual-exit-shadow-20260821

## owner
CODEX

## status
ACTIVE

## created_at
2026-08-21

## updated_at
2026-08-22

## Objective
Manual Exit vs Bot Exit Shadow Tracker — observe bot trades that were
closed manually, snapshot the manual exit, and shadow the counterfactual
outcome if the original plan had been left untouched.

## investigation_id
manual-exit-shadow-20260821

## Scope
- Read-only source inspection first.
- Build an isolated shadow tracker only.
- Keep it SHADOW / OBSERVABILITY ONLY.
- Verify deterministic attribution, counterfactual resolution, and safe
  skips for unknown/manual-only cases.

## Known Evidence (= evidence)
- MT5 deal reason codes can deterministically identify manual closes via
  client/mobile/web reasons.
- Existing read-only MT5 history utilities already show how to reconstruct
  closed trades without touching live bot logic.
- The tracker must not touch any live trading bot, order path, or SL/TP
  management path.

## Files likely involved (= exact_files)
`Research_ManualExitShadow_2026/manual_exit_shadow_tracker.py`,
`Research_ManualExitShadow_2026/test_manual_exit_shadow_tracker.py`,
`Research_ManualExitShadow_2026/data/manual_exit_shadow/manual_exit_shadow_state.json`,
`Research_ManualExitShadow_2026/data/manual_exit_shadow/manual_exit_snapshots.jsonl`,
`Research_ManualExitShadow_2026/data/manual_exit_shadow/manual_exit_resolutions.jsonl`,
`Research_ManualExitShadow_2026/data/manual_exit_shadow/manual_exit_shadow_report.md`.

## Exact requested change (= expected_output)
Create an isolated shadow tracker that records manual exits of bot trades,
then tracks the counterfactual TP/SL outcome after the exit.

## Forbidden changes (= constraints)
Any broker order, live trading execution, strategy/gate/threshold change,
SL/TP/lot change, any live bot change, or any execution path inside the
tracker.

## Required tests (= done_criteria)
- Fixture/sample tracker run.
- Deterministic manual-exit attribution.
- Counterfactual resolution for TP-first / SL-first / unresolved cases.
- Unknown attribution stays UNKNOWN instead of guessing.
- `py_compile` and no-exec-path check.

## Current status (= status: ACTIVE)
IN PROGRESS — owned by Codex.

## Next owner (= owner: CODEX)
CODEX (implement tracker only; no live trading work).

## Workflow rule
Before any new investigation:
1. Check `CURRENT_TASK.md`.
2. Check the relevant Work Queue item status.
3. If the same `investigation_id` or same topic is already covered, do not
   repeat the investigation unless there is new data or a clearly different
   scope.
