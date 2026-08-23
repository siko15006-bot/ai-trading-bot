# AI Orchestrator Phase 1

Minimal local routing for queued tasks.

## What it does

- Reads `ai_orchestrator/task_queue.json`
- Classifies each task as `RESEARCH`, `CODE_CHANGE`, `REVIEW`, or `PRODUCTION_SENSITIVE`
- Routes execution-oriented tasks to Codex
- Calls Claude only when review is requested or needed
- Blocks production-sensitive work at `NEEDS_APPROVAL`
- Writes one report per task in `ai_orchestrator/reports/`

## Run

```powershell
python ai_orchestrator/orchestrator.py --queue ai_orchestrator/task_queue.json --config ai_orchestrator/config.json
```

Self-test:

```powershell
python ai_orchestrator/orchestrator.py --self-test
```

## Config

The adapter commands are config-driven, not hardcoded.

- Codex default command:
  - `C:\Program Files\WindowsApps\OpenAI.Codex_26.814.5167.0_x64__2p2nqsd0c76g0\app\resources\codex.exe`
- Claude default command:
  - `powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\ahmed\AppData\Roaming\npm\claude.ps1`

Both adapters read the task prompt from stdin when enabled.

## Safety gates

- No production changes are executed automatically
- No broker or order calls live here
- No restart logic
- Max retries and max tasks per run are capped in config
- Claude is only called when a review step is actually needed

## Reports

Each task writes a JSON report in `ai_orchestrator/reports/` with:

- classification
- route
- final status
- stdout
- stderr
- step results
