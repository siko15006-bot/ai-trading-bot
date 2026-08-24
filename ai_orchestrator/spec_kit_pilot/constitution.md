# AI Orchestrator Spec Kit Pilot Constitution

Status: SANDBOX / ISOLATED BRANCH ONLY

## Non-negotiable boundaries

1. No live trading strategy, threshold, risk-setting, broker, order-routing, production bot, or production execution change may be made without Ahmed's explicit approval.
2. Spec-driven changes in this pilot are limited to `ai_orchestrator/` governance, prompts, tests, documentation, and isolated test workspaces.
3. Production-sensitive requests must terminate as `NEEDS_APPROVAL` before any execution adapter is called.
4. Codex should be used for bounded implementation work; Claude should be invoked only for genuinely necessary review, architecture, or ambiguous-risk judgment.
5. Every code-change task must have explicit scope, allowed paths, acceptance criteria, and a review policy before execution.
6. Prefer deterministic checks and self-tests before consuming model tokens.
7. No automatic merge to `main` from this pilot branch.

## Workflow contract

A task is ready for agent execution only when it contains:

- objective
- classification
- allowed paths
- forbidden paths or production boundary
- acceptance criteria
- whether review is required
- expected output type

If any required field is missing, the task should be clarified or rejected rather than sent repeatedly to an agent.

## Token / rework policy

- One implementation attempt by default; retries require new evidence from a failed deterministic check.
- Do not call Claude after a successful deterministic check unless the task is marked high-risk or review-required.
- Review actual diffs, not the original broad request.
- Keep agent context to the smallest relevant files/spec sections.
- Record whether a failure came from unclear scope, incorrect implementation, failed validation, or review disagreement.

## Pilot success criteria

The pilot is useful only if, versus the existing ad-hoc flow, it measurably reduces at least two of these without weakening safety:

- repeated Claude/Codex calls per task
- corrections caused by misunderstood scope
- out-of-scope diffs
- review cycles
- prompt/context size
- production-boundary violations

No claim of improvement should be made until enough comparable sandbox tasks are recorded.
