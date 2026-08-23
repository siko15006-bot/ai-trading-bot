#!/usr/bin/env python3
"""Auto worker -- polls task_queue.json and drains PENDING tasks so the
queue processes itself without a manual `python orchestrator.py` call each
time. Bounded (not an unattended infinite loop): stops itself after
MAX_IDLE_CYCLES with nothing to do, and honors a stop file for a clean
manual stop. Status (current task, call counters, last activity) is
written to worker_status.json after every cycle -- that file is the
dashboard.
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import orchestrator as orc

STATUS_PATH = orc.ROOT / "worker_status.json"
STOP_PATH = orc.ROOT / "auto_worker.stop"
POLL_SECONDS = 15
MAX_IDLE_CYCLES = 40  # ~10 minutes of nothing PENDING, then exit


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_status(state: str, current_task: str, codex_calls: int, claude_calls: int, note: str) -> None:
    orc._save_json(STATUS_PATH, {
        "state": state,
        "current_task": current_task,
        "codex_calls_total": codex_calls,
        "claude_calls_total": claude_calls,
        "last_activity_at": _now(),
        "last_activity": note,
    })


def run(queue_path: Path, config_path: Path) -> None:
    config = orc._load_json(config_path)
    reports_dir = orc.ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    codex_calls = 0
    claude_calls = 0
    idle_cycles = 0
    _write_status("IDLE", "", codex_calls, claude_calls, "worker started")

    while True:
        if STOP_PATH.exists():
            _write_status("IDLE", "", codex_calls, claude_calls, "stopped via auto_worker.stop")
            STOP_PATH.unlink(missing_ok=True)
            return

        queue = orc._load_json(queue_path)
        tasks = queue.get("tasks", [])
        pending = next((t for t in tasks if str(t.get("status", "PENDING")).upper() not in orc.FINAL_STATUSES), None)

        if pending is None:
            idle_cycles += 1
            _write_status("IDLE", "", codex_calls, claude_calls, "no PENDING tasks")
            if idle_cycles >= MAX_IDLE_CYCLES:
                _write_status("IDLE", "", codex_calls, claude_calls,
                               f"exiting after {MAX_IDLE_CYCLES} idle cycles -- rerun when new tasks are queued")
                return
            time.sleep(POLL_SECONDS)
            continue

        idle_cycles = 0
        _write_status("RUNNING", pending["id"], codex_calls, claude_calls, "processing")
        result = orc.process_task(pending, config, reports_dir)
        for step in result.get("results", []):
            if step.get("adapter") == "codex":
                codex_calls += 1
            elif step.get("adapter") == "claude":
                claude_calls += 1

        pending["classification"] = result["classification"]
        pending["status"] = result["final_status"]
        pending["report_path"] = result["report_path"]
        orc._save_json(queue_path, queue)

        _write_status("IDLE", pending["id"], codex_calls, claude_calls,
                       f"{pending['id']} -> {result['final_status']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded auto worker for the orchestrator queue")
    parser.add_argument("--queue", default=str(orc.DEFAULT_QUEUE))
    parser.add_argument("--config", default=str(orc.DEFAULT_CONFIG))
    args = parser.parse_args()
    run(Path(args.queue), Path(args.config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
