# Shared GitHub Workflow

This repository is the official project reference for ChatGPT, Codex, and Claude.

## Source of truth
- Official repository: `siko15006-bot/ai-trading-bot`
- Default branch: `main`
- `chat-gpt-connector-test` is connectivity testing only.
- Do not use the old GitHub account/repository unless Ahmed explicitly requests recovery of a specific historical file.

## Roles
- Codex: implementation, code patches, tests, repository maintenance.
- Claude: architecture, root-cause analysis, design/risk review.
- ChatGPT: coordination, independent review of commits/diffs, status summaries, and cross-agent consistency checks.

## Change flow
1. Read `docs/CURRENT_TASK.md` and `docs/PROJECT_GUARDRAILS.md` before starting.
2. Do not duplicate an active investigation.
3. Important code changes should use a dedicated branch rather than editing `main` directly.
4. Run relevant tests and document evidence before proposing merge.
5. ChatGPT or another reviewer should inspect the diff before an important merge when practical.
6. Keep commits focused and descriptive.

## Trading safety boundary
Research/Shadow -> Approved Candidate -> Production.

- New ideas start in Research/Shadow unless Ahmed explicitly approves otherwise.
- GitHub changes do not imply deployment to live machines.
- No production strategy, thresholds, gates, SL/TP, lot sizing, execution settings, credentials, or live deployment changes without Ahmed's explicit approval.
- Operational bug fixes still require backup/diff/test discipline before any live deployment.

## Secrets and runtime data
Never commit live credentials, API keys, passwords, account secrets, Telegram sessions, private notification topics, or other bearer credentials.
Avoid committing transient runtime state/logs/databases unless intentionally sanitized and needed as a test fixture.

## Status discipline
`docs/CURRENT_TASK.md` is the short active handoff. Update it only when ownership/task status genuinely changes; do not use it as long-term history.
Use focused docs/issues for durable decisions and completed investigations.
