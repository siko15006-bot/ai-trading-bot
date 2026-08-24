#!/usr/bin/env python3
"""Deterministic preflight validator for Spec Kit pilot task contracts.

Sandbox governance only. This module does not invoke agents, brokers, bots, or execution adapters.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REQUIRED_FIELDS = {
    "id",
    "objective",
    "classification",
    "allowed_paths",
    "forbidden_paths",
    "acceptance_criteria",
    "review_policy",
    "expected_output",
    "metrics",
}

PRODUCTION_MARKERS = (
    "windows_bots/",
    "oracle_bots/",
    "production",
    "live trading",
    "risk settings",
    "trade strategy thresholds",
)


def validate(contract: dict) -> list[str]:
    errors: list[str] = []

    missing = sorted(REQUIRED_FIELDS - set(contract))
    if missing:
        errors.append("missing required fields: " + ", ".join(missing))

    objective = contract.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        errors.append("objective must be a non-empty string")

    allowed = contract.get("allowed_paths")
    if not isinstance(allowed, list) or not allowed or not all(isinstance(p, str) and p.strip() for p in allowed):
        errors.append("allowed_paths must be a non-empty list of strings")
    else:
        unsafe = [p for p in allowed if not p.startswith("ai_orchestrator/")]
        if unsafe:
            errors.append("allowed_paths must stay inside ai_orchestrator/: " + ", ".join(unsafe))

    forbidden = contract.get("forbidden_paths")
    if not isinstance(forbidden, list) or not forbidden:
        errors.append("forbidden_paths must be a non-empty list")
    else:
        joined = " ".join(str(x).lower() for x in forbidden)
        missing_boundaries = [m for m in PRODUCTION_MARKERS if m.lower() not in joined]
        if missing_boundaries:
            errors.append("forbidden_paths missing production boundaries: " + ", ".join(missing_boundaries))

    criteria = contract.get("acceptance_criteria")
    if not isinstance(criteria, list) or not criteria or not all(isinstance(x, str) and x.strip() for x in criteria):
        errors.append("acceptance_criteria must be a non-empty list of strings")

    review = contract.get("review_policy")
    if not isinstance(review, str) or not review.strip():
        errors.append("review_policy must be a non-empty string")

    expected = contract.get("expected_output")
    if not isinstance(expected, str) or not expected.strip():
        errors.append("expected_output must be a non-empty string")

    metrics = contract.get("metrics")
    required_metrics = {
        "codex_calls",
        "claude_calls",
        "retries",
        "review_cycles",
        "out_of_scope_diff",
        "scope_misunderstanding",
    }
    if not isinstance(metrics, dict):
        errors.append("metrics must be an object")
    else:
        metric_missing = sorted(required_metrics - set(metrics))
        if metric_missing:
            errors.append("metrics missing fields: " + ", ".join(metric_missing))

    return errors


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate_task_contract.py <contract.json>", file=sys.stderr)
        return 2

    path = Path(sys.argv[1])
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"INVALID: cannot read contract: {exc}")
        return 2

    errors = validate(contract)
    if errors:
        print("REJECTED")
        for error in errors:
            print(f"- {error}")
        return 1

    print("READY_FOR_SANDBOX_AGENT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
