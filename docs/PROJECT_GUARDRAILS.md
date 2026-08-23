# PROJECT_GUARDRAILS.md

Permanent workflow rules for Claude and Codex on this repo. Read before any
task. These rules do not expire and are not restated per-task — `CURRENT_TASK.md`
holds the task-specific handoff; this file holds the standing constraints.

## Division of responsibility (routing)

**LOCAL** — deterministic work only: grep, diffs, tests, scripted
backtests/analysis, log parsing. No model judgment needed, no LLM call
required.

**Codex** — implementation/debugging on an already-approved design: code
execution, clear operational fixes, tests, diffs, logging, dashboard,
heartbeat, startup/restart, state corruption, duplicate-process issues.

**Claude** — architecture, root-cause analysis, design, review of
sensitive changes.

**BOTH** — only for high-risk decisions or a genuine disagreement between
Claude and Codex on the same task. Never for routine work.

Do not re-analyze the same problem from scratch in both agents. If the other
agent already diagnosed root cause, reuse it — don't re-derive. No open
model-to-model conversation — coordination happens only through
`CURRENT_TASK.md`'s task packet fields, not free-form chat between agents.

## Dedup gate (before starting any new task)

1. Read `MEMORY.md` as an index only (one line per topic).
2. Open only the specific memory file the task relates to.
3. Check `CURRENT_TASK.md`'s `task_id` and the relevant Work Queue item.
4. If the same investigation/fix already exists with a trustworthy
   result, mark it `DO_NOT_REPEAT` and stop — do not re-run an
   audit/backtest/investigation without a documented new reason (new
   data, or a clearly different scope).

## Conflict rule

If Claude and Codex disagree on the same task: neither overrides the
other's position. Record each position separately, then write one short
conflict summary containing only: `task_id`, disputed point, Claude's
position + evidence, Codex's position + evidence, safety impact,
recommended options. Set `status: BLOCKED_AWAITING_AHMED`. Neither agent
resolves a strategic or production disagreement automatically — Ahmed
decides.

## Token-saving rule

Do not auto-load the full project history for a routine task. Path:
`MEMORY.md` index → the one relevant memory file → `CURRENT_TASK.md`
packet → the exact files named in it. When Claude reviews Codex's work
(or vice versa), the reviewer should receive objective + diff + tests +
evidence + constraints — not the full authoring session/context.

## Core loop

**Think once → Execute once → Review only on failure.**

## Production change rules

- Never change strategy logic, thresholds, SL/TP, lot sizing, risk settings,
  or production trading behavior without Ahmed's explicit approval.
- Any production change, when needed: backup → minimal change → diff →
  tests → verification. No step skipped, no step skipped even under time
  pressure.
- Do not expand task scope without a proven reason found during the task
  itself.
- Do not read large files/logs without need. Start with the smallest
  context that could answer the question, widen only when actually
  insufficient.

## Reporting

- Do not repeat information already known/established in the conversation
  or in `CURRENT_TASK.md`.
- Final report format, kept short:
  `Root Cause / Files Changed / Tests / Result / Strategy Logic Changed? YES|NO`

## When blocked

If there is ambiguity or strategic risk: stop and ask Ahmed for approval.
Never guess on a strategy-affecting decision.

## Standing state

- Current systems, Shadow bots, and monitoring continue running as normal —
  this file does not pause anything.
- Do not start any new large project or the Work Queue until Ahmed gives an
  explicit go-ahead for that specific item.
