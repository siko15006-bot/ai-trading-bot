# Agent Handoff Template

Use this when Codex, Claude, or ChatGPT hands work to another reviewer/agent.
Keep it short and evidence-based.

## Task
- task_id:
- owner:
- status: TODO | IN_PROGRESS | REVIEW | DONE | BLOCKED
- branch:
- commit:

## What changed
- files changed:
- behavior changed:
- production touched: YES/NO

## Safety
- strategy/threshold/SL/TP/lot changed: YES/NO
- credentials/runtime state touched: YES/NO
- live execution path changed: YES/NO
- if any answer above is YES, explicit Ahmed approval reference:

## Verification
- tests run:
- result:
- known limitations:

## Review request
- exact question for reviewer:
- merge recommendation: MERGE | HOLD | REDESIGN

## Evidence
- relevant logs/results/diffs:
