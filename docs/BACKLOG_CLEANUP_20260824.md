# Backlog Cleanup 2026-08-24

Scope: dedup/status only. No trading, strategy, risk, execution, deploy, or production runtime changes.

## GitHub Inventory

| Item | Title | Decision | Evidence |
| --- | --- | --- | --- |
| #9 | ORBIT P0/P1 progress: Auto-Execution Shadow + Shadow Scoreboard | KEEP SHADOW | P0/P1 milestone comment exists; P0 still waiting for first real A-grade candidate evidence. |
| #8 | ORBIT Control Room — agents + trading runtime status | KEEP | Canonical coordination/status surface; no complete implementation evidence yet. |
| #7 | Dashboard V2 P0 implementation | KEEP REVIEW | Branch `issue-7-dashboard-v2-p0` at `365249c`; not deployed; ChatGPT review required. |
| #6 | Dashboard V2: AI command center, charts and decision intelligence | MERGE/PARK | P0 covered by #7; P1/P2 remain broader future scope. Do not start until #7 review/deploy decision. |
| #5 | Dashboard: Codex + Claude Usage / Limit Telemetry | MERGE INTO #7/#6 | Exact quota unavailable by safe sources; current dashboard renders unavailable instead of estimates. Keep open until #7 review confirms enough. |
| #4 | Shadow research: Long/Short Entry Quality Layer | KEEP SHADOW | Distinct research scope; no enough evidence to retire or promote. |
| #3 | Dashboard full-detail observability view | COMPLETE | Implemented in `issue-3-dashboard-observability` commits and consumed by #7 command strip. |
| #2 | Operational recovery: Oracle services + stale shadows | COMPLETE | `issue-2-operational-recovery` fixed Oracle false-positive health; pending breakout stale heartbeat/feed issue fixed during #9 runtime milestone. |
| PR #1 | Add shared agent handoff and review templates | KEEP REVIEW | Workflow/handoff PR still open; unrelated to ORBIT P0/P1 execution and Dashboard V2. |

## Branch Inventory

| Branch | Decision | Evidence |
| --- | --- | --- |
| `issue-7-dashboard-v2-p0` | KEEP | Active Dashboard V2 P0 review branch; pushed at `365249c`. |
| `issue-6-dashboard-v2-ai-status` | MERGE/PARK | Superseded by #7 lineage; do not delete until #7 reviewed. |
| `issue-3-dashboard-observability` | COMPLETE/PARK | Functionality consumed by #7; safe to close issue, keep branch until merge cleanup. |
| `issue-2-operational-recovery` | COMPLETE/PARK | Operational recovery complete; keep branch until merge cleanup. |
| `workflow/agent-handoff` | KEEP REVIEW | Open PR #1. |

## Local Task Files

| File | Decision | Evidence |
| --- | --- | --- |
| `docs/CURRENT_TASK.md` | UPDATE NEEDED LATER | Still points to manual-exit-shadow 2026-08-21; do not overwrite during ORBIT/#7 cleanup without a dedicated handoff update. |
| `ai_orchestrator/research/manual_exit_shadow_status/dedup_and_status_report.md` | KEEP SHADOW | Report says CONTINUE and INCONCLUSIVE; not a duplicate. |

## Result

- Close #2 and #3 as completed.
- Keep #4, #5, #6, #7, #8, #9 open.
- No issue is retired for failed strategy evidence in this pass.
- No branch deletion in this pass.
