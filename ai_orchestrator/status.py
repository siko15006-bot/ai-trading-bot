#!/usr/bin/env python3
"""Dashboard status -- prints ORCHESTRATOR/CURRENT TASK/CODEX/CLAUDE/CALLS/
LAST ACTIVITY from worker_status.json. Read-only, no side effects."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATUS_PATH = ROOT / "worker_status.json"


def main() -> int:
    if not STATUS_PATH.exists():
        print("ORCHESTRATOR: IDLE (auto_worker never run)")
        return 0

    s = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    state = s.get("state", "IDLE")
    print(f"ORCHESTRATOR: {state}")
    print(f"CURRENT TASK: {s.get('current_task') or '(none)'}")
    print(f"CODEX CALLS: {s.get('codex_calls_total', 0)}")
    print(f"CLAUDE CALLS: {s.get('claude_calls_total', 0)}")
    print(f"LAST ACTIVITY: {s.get('last_activity_at', '?')} -- {s.get('last_activity', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
