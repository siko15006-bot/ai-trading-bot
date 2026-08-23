#!/usr/bin/env python3
"""Minimal local AI orchestrator for dry-run task routing.

Phase 1 rules:
- classify queued work
- route execution-only work to Codex
- call Claude only when review is actually needed
- never auto-run production-sensitive work
- capture stdout/stderr/outputs
- write one report per task
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
DEFAULT_CONFIG = ROOT / "config.json"
DEFAULT_QUEUE = ROOT / "task_queue.json"
DEFAULT_REPORTS = ROOT / "reports"
FINAL_STATUSES = {"PASS", "FAIL", "NEEDS_REVIEW", "NEEDS_APPROVAL"}
CLASSIFICATIONS = {"RESEARCH", "CODE_CHANGE", "REVIEW", "PRODUCTION_SENSITIVE"}

# Patch-gate: Codex stays read-only and only ever emits a diff; this is the one
# place allowed to touch disk on its behalf, and only inside these allowlisted
# prefixes (all still inside ai_orchestrator/, never Bot_Active/production).
PATCH_ALLOWLIST_PREFIXES = (
    "ai_orchestrator/test_workspace/",
    "ai_orchestrator/research/",
)
PATCH_MAX_FILES = 3
PATCH_MAX_CHARS = 20_000


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _save_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=False)
        fh.write("\n")
    tmp.replace(path)


def _slug(value: str) -> str:
    out = []
    for ch in value.lower():
        out.append(ch if ch.isalnum() else "-")
    slug = "".join(out).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug or "task"


def classify_task(task: Dict[str, Any], config: Dict[str, Any]) -> str:
    explicit = str(task.get("classification", "")).upper().strip()
    if explicit in CLASSIFICATIONS:
        return explicit

    text = " ".join(
        str(task.get(key, "")) for key in ("title", "request", "description", "notes")
    ).lower()
    rules = config.get("classification_rules", {})
    prod_words = [w.lower() for w in rules.get("production_sensitive_keywords", [])]
    review_words = [w.lower() for w in rules.get("review_keywords", [])]
    research_words = [w.lower() for w in rules.get("research_keywords", [])]

    if task.get("production_sensitive") or any(word in text for word in prod_words):
        return "PRODUCTION_SENSITIVE"
    if task.get("requires_review") or any(word in text for word in review_words):
        return "REVIEW"
    if task.get("research") or any(word in text for word in research_words):
        return "RESEARCH"
    return "CODE_CHANGE"


def _adapter_command(adapter: Dict[str, Any]) -> List[str]:
    command = adapter.get("command")
    if not isinstance(command, list) or not command:
        raise ValueError("adapter.command must be a non-empty JSON list")
    return [str(part) for part in command]


def run_adapter(
    adapter_name: str,
    prompt: str,
    config: Dict[str, Any],
    *,
    dry_run: bool,
) -> Dict[str, Any]:
    adapter = config["adapters"][adapter_name]
    command = _adapter_command(adapter)
    timeout_sec = int(adapter.get("timeout_sec", 120))
    use_stdin = bool(adapter.get("stdin", True))

    if dry_run or not adapter.get("enabled", False):
        return {
            "adapter": adapter_name,
            "command": command,
            "returncode": 0,
            "stdout": f"[dry-run] would run: {' '.join(command)}\n{prompt}\n",
            "stderr": "",
            "mode": "dry_run",
        }

    try:
        completed = subprocess.run(
            command,
            input=prompt if use_stdin else None,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_sec,
            check=False,
        )
        return {
            "adapter": adapter_name,
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "mode": "live",
        }
    except Exception as exc:
        return {
            "adapter": adapter_name,
            "command": command,
            "returncode": 1,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
            "mode": "error",
        }


def _extract_diff(text: str) -> str:
    if not text:
        return ""
    idx = text.find("diff --git")
    return text[idx:].strip() if idx != -1 else ""


def _diff_paths(diff_text: str) -> List[str]:
    paths: List[str] = []
    for line in diff_text.splitlines():
        m = re.match(r"^(?:\+\+\+|---) [ab]/(.+)$", line)
        if m and m.group(1) != "dev/null" and m.group(1) not in paths:
            paths.append(m.group(1))
    return paths


def validate_patch(diff_text: str) -> str:
    """Return '' if the diff is safe to apply, else a rejection reason."""
    if not diff_text or "diff --git" not in diff_text:
        return "no unified diff found in codex output"
    if len(diff_text) > PATCH_MAX_CHARS:
        return f"patch too large ({len(diff_text)} chars > {PATCH_MAX_CHARS})"
    paths = _diff_paths(diff_text)
    if not paths:
        return "no file paths found in diff"
    if len(paths) > PATCH_MAX_FILES:
        return f"too many files changed ({len(paths)} > {PATCH_MAX_FILES})"
    allow_roots = [(REPO_ROOT / prefix).resolve() for prefix in PATCH_ALLOWLIST_PREFIXES]
    for p in paths:
        norm = p.replace("\\", "/")
        if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm):
            return f"absolute path rejected: {p}"
        if ".." in Path(norm).parts:
            return f"path traversal rejected: {p}"
        if not any(norm.startswith(prefix) for prefix in PATCH_ALLOWLIST_PREFIXES):
            return f"path outside allowlist rejected: {p}"
        target = (REPO_ROOT / norm).resolve()
        if not any(_is_relative_to(target, root) for root in allow_roots):
            return f"path escapes allowlist after normalization: {p}"
        if target.exists() and target.is_symlink():
            return f"symlink target rejected: {p}"
    return ""


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _snapshot_allowlist() -> Dict[str, bytes]:
    """Repo-relative path -> bytes, for every file under any allowlisted prefix."""
    snap: Dict[str, bytes] = {}
    for prefix in PATCH_ALLOWLIST_PREFIXES:
        d = REPO_ROOT / prefix
        if d.exists():
            for f in d.rglob("*"):
                if f.is_file():
                    snap[str(f.relative_to(REPO_ROOT)).replace("\\", "/")] = f.read_bytes()
    return snap


def _restore_allowlist(snapshot: Dict[str, bytes]) -> None:
    current = _snapshot_allowlist()
    for rel, content in snapshot.items():
        target = REPO_ROOT / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    for rel in current.keys() - snapshot.keys():
        (REPO_ROOT / rel).unlink(missing_ok=True)


def apply_patch_gate(diff_text: str) -> Dict[str, Any]:
    """Validate, apply (via git apply, scoped to the allowlist), and roll back on
    any surprise. Never touches anything outside ai_orchestrator/test_workspace/."""
    rejection = validate_patch(diff_text)
    if rejection:
        return {"applied": False, "reason": rejection, "actual_diff": "", "changed_files": []}

    before = _snapshot_allowlist()

    fd, tmp_name = tempfile.mkstemp(suffix=".patch", dir=str(ROOT / "reports"))
    patch_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(diff_text if diff_text.endswith("\n") else diff_text + "\n")

        check = subprocess.run(
            ["git", "apply", "--check", str(patch_path)],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=30,
        )
        if check.returncode != 0:
            return {"applied": False, "reason": f"git apply --check failed: {check.stderr.strip()}",
                    "actual_diff": "", "changed_files": []}

        apply = subprocess.run(
            ["git", "apply", str(patch_path)],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=30,
        )
        if apply.returncode != 0:
            return {"applied": False, "reason": f"git apply failed: {apply.stderr.strip()}",
                    "actual_diff": "", "changed_files": []}

        after = _snapshot_allowlist()
        changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
        intended = set(_diff_paths(diff_text))

        if not changed or not set(changed) <= intended:
            _restore_allowlist(before)
            return {"applied": False,
                     "reason": f"unexpected changed files {changed} vs intended {sorted(intended)}; rolled back",
                     "actual_diff": "", "changed_files": []}

        actual_diff = "".join(
            "".join(difflib.unified_diff(
                before.get(rel, b"").decode("utf-8", "replace").splitlines(keepends=True),
                after.get(rel, b"").decode("utf-8", "replace").splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            ))
            for rel in changed
        )
        return {"applied": True, "reason": "", "actual_diff": actual_diff, "changed_files": changed,
                "_rollback_snapshot": before}
    finally:
        patch_path.unlink(missing_ok=True)


def process_patch_task(task: Dict[str, Any], config: Dict[str, Any], reports_dir: Path) -> Dict[str, Any]:
    """Codex (read-only) proposes a diff; this function is the only thing that
    ever writes to disk on its behalf, and only inside the patch-gate allowlist."""
    started = time.time()
    task_id = str(task["id"])
    report_path = reports_dir / f"{_slug(task_id)}.json"

    codex = run_adapter("codex", task.get("request", task.get("title", "")), config, dry_run=False)
    raw_diff = _extract_diff(codex["stdout"])

    gate = apply_patch_gate(raw_diff)
    results = [{"step": "codex", **codex}]

    if not gate["applied"]:
        run = {
            "classification": "CODE_CHANGE", "route": "codex(patch-gate)",
            "final_status": "FAIL", "attempts": 1,
            "started_at": started, "finished_at": time.time(),
            "stdout": codex["stdout"], "stderr": gate["reason"],
            "raw_diff": raw_diff, "results": results,
        }
        render_report(task, run, report_path)
        return run | {"report_path": str(report_path)}

    claude_prompt = (
        "Review this actual git diff for safety and correctness. "
        "Reply with exactly PASS or NEEDS_REVIEW on the first line, then a one-line rationale.\n\n"
        + gate["actual_diff"]
    )
    claude = run_adapter("claude", claude_prompt, config, dry_run=False)
    results.append({"step": "claude", **claude})
    first_line = claude["stdout"].strip().splitlines()[0].upper() if claude["stdout"].strip() else ""
    verdict = "PASS" if first_line.startswith("PASS") else "NEEDS_REVIEW"

    run = {
        "classification": "CODE_CHANGE", "route": "codex(patch)+claude",
        "final_status": verdict, "attempts": 1,
        "started_at": started, "finished_at": time.time(),
        "stdout": codex["stdout"], "stderr": "",
        "raw_diff": raw_diff, "actual_diff": gate["actual_diff"],
        "changed_files": gate["changed_files"], "results": results,
    }
    render_report(task, run, report_path)
    return run | {"report_path": str(report_path)}


def render_report(task: Dict[str, Any], run: Dict[str, Any], report_path: Path) -> None:
    report = {
        "task_id": task["id"],
        "title": task.get("title", ""),
        "classification": run["classification"],
        "route": run["route"],
        "final_status": run["final_status"],
        "attempts": run["attempts"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "stdout": run["stdout"],
        "stderr": run["stderr"],
        "results": run["results"],
    }
    for optional_key in ("raw_diff", "actual_diff", "changed_files"):
        if optional_key in run:
            report[optional_key] = run[optional_key]
    _save_json(report_path, report)


def process_task(task: Dict[str, Any], config: Dict[str, Any], reports_dir: Path) -> Dict[str, Any]:
    started = time.time()
    classification = classify_task(task, config)
    task_id = str(task["id"])
    report_path = reports_dir / f"{_slug(task_id)}.json"
    max_retries = max(0, int(config.get("max_retries", 1)))
    dry_run = bool(task.get("dry_run", config.get("dry_run_default", True)))

    if classification == "PRODUCTION_SENSITIVE":
        run = {
            "classification": classification,
            "route": "blocked",
            "final_status": "NEEDS_APPROVAL",
            "attempts": 0,
            "started_at": started,
            "finished_at": time.time(),
            "stdout": "",
            "stderr": "production-sensitive task blocked before execution",
            "results": [],
        }
        render_report(task, run, report_path)
        return run | {"report_path": str(report_path)}

    if classification == "RESEARCH":
        run = {
            "classification": classification,
            "route": "none",
            "final_status": "PASS",
            "attempts": 0,
            "started_at": started,
            "finished_at": time.time(),
            "stdout": "",
            "stderr": "",
            "results": [],
        }
        render_report(task, run, report_path)
        return run | {"report_path": str(report_path)}

    if classification == "CODE_CHANGE" and task.get("patch_mode"):
        return process_patch_task(task, config, reports_dir)

    if classification == "REVIEW":
        claude = run_adapter(
            "claude",
            task.get("review_prompt", task.get("request", task.get("title", ""))),
            config,
            dry_run=dry_run,
        )
        run = {
            "classification": classification,
            "route": "claude",
            "final_status": "PASS" if claude["returncode"] == 0 else "NEEDS_REVIEW",
            "attempts": 1,
            "started_at": started,
            "finished_at": time.time(),
            "stdout": claude["stdout"],
            "stderr": claude["stderr"],
            "results": [{"step": "claude", **claude}],
        }
        render_report(task, run, report_path)
        return run | {"report_path": str(report_path)}

    attempts = 0
    results: List[Dict[str, Any]] = []
    stdout_chunks: List[str] = []
    stderr_chunks: List[str] = []
    route = "codex"
    final_status = "PASS"

    while attempts <= max_retries:
        attempts += 1
        codex = run_adapter("codex", task.get("request", task.get("title", "")), config, dry_run=dry_run)
        results.append({"step": "codex", **codex})
        stdout_chunks.append(codex["stdout"])
        stderr_chunks.append(codex["stderr"])

        if codex["returncode"] != 0:
            final_status = "FAIL"
            if attempts <= max_retries:
                continue
            break

        needs_review = classification == "REVIEW" or bool(task.get("review_after"))
        if needs_review:
            route = "codex+claude"
            claude = run_adapter(
                "claude",
                task.get("review_prompt", task.get("request", task.get("title", ""))),
                config,
                dry_run=dry_run,
            )
            results.append({"step": "claude", **claude})
            stdout_chunks.append(claude["stdout"])
            stderr_chunks.append(claude["stderr"])
            if claude["returncode"] != 0:
                final_status = "NEEDS_REVIEW"
            else:
                final_status = "PASS"
        break

    finished = time.time()
    run = {
        "classification": classification,
        "route": route,
        "final_status": final_status if final_status in FINAL_STATUSES else "FAIL",
        "attempts": attempts,
        "started_at": started,
        "finished_at": finished,
        "stdout": "".join(stdout_chunks),
        "stderr": "".join(stderr_chunks),
        "results": results,
    }
    render_report(task, run, report_path)
    return run | {"report_path": str(report_path)}


def process_queue(queue_path: Path, config_path: Path, *, write_queue: bool = True) -> Dict[str, Any]:
    config = _load_json(config_path)
    queue = _load_json(queue_path)
    tasks = list(queue.get("tasks", []))
    base_dir = config_path.parent
    reports_setting = Path(str(config.get("reports_dir", "reports")))
    reports_dir = reports_setting if reports_setting.is_absolute() else base_dir / reports_setting
    reports_dir.mkdir(parents=True, exist_ok=True)

    max_tasks = max(1, int(config.get("max_tasks_per_run", 1)))
    processed = []
    for task in tasks[:max_tasks]:
        if str(task.get("status", "PENDING")).upper() in FINAL_STATUSES:
            continue
        result = process_task(task, config, reports_dir)
        task["classification"] = result["classification"]
        task["status"] = result["final_status"]
        task["report_path"] = result["report_path"]
        processed.append({"id": task["id"], "status": task["status"], "classification": task["classification"]})

    if write_queue:
        queue["tasks"] = tasks
        _save_json(queue_path, queue)

    return {"processed": processed, "queue_path": str(queue_path), "config_path": str(config_path)}


def self_test() -> None:
    sample_config = {
        "dry_run_default": True,
        "max_tasks_per_run": 10,
        "max_retries": 1,
        "classification_rules": {
            "production_sensitive_keywords": ["production", "broker", "live order", "risk"],
            "review_keywords": ["review", "debug failing checks", "triage"],
            "research_keywords": ["research", "investigate"],
        },
        "adapters": {
            "codex": {"enabled": False, "command": ["codex"], "stdin": True, "timeout_sec": 1},
            "claude": {"enabled": False, "command": ["claude"], "stdin": True, "timeout_sec": 1},
        },
    }

    assert classify_task({"classification": "CODE_CHANGE"}, sample_config) == "CODE_CHANGE"
    assert classify_task({"request": "please review this"}, sample_config) == "REVIEW"
    assert classify_task({"request": "touch live broker config"}, sample_config) == "PRODUCTION_SENSITIVE"
    assert classify_task({"request": "do research on the queue"}, sample_config) == "RESEARCH"

    valid_diff = (
        "diff --git a/ai_orchestrator/test_workspace/x.txt b/ai_orchestrator/test_workspace/x.txt\n"
        "index 0000000..1111111 100644\n"
        "--- a/ai_orchestrator/test_workspace/x.txt\n"
        "+++ b/ai_orchestrator/test_workspace/x.txt\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    assert validate_patch(valid_diff) == ""
    assert "traversal" in validate_patch(valid_diff.replace(
        "ai_orchestrator/test_workspace/x.txt", "ai_orchestrator/test_workspace/../../secrets.txt"))
    assert "absolute" in validate_patch(valid_diff.replace(
        "a/ai_orchestrator/test_workspace/x.txt", "a/C:/Windows/system.ini").replace(
        "b/ai_orchestrator/test_workspace/x.txt", "b/C:/Windows/system.ini"))
    assert "allowlist" in validate_patch(valid_diff.replace(
        "ai_orchestrator/test_workspace/x.txt", "TradingBot/Bot_Active/config.py"))
    assert "no unified diff" in validate_patch("not a diff, just prose")

    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        cfg = tmp / "config.json"
        queue = tmp / "task_queue.json"
        reports = tmp / "reports"
        _save_json(cfg, sample_config)
        _save_json(
            queue,
            {
                "tasks": [
                    {
                        "id": "codex-only",
                        "title": "Codex-only dry run",
                        "request": "Generate a tiny helper in ai_orchestrator only.",
                        "classification": "CODE_CHANGE",
                        "dry_run": True,
                    },
                    {
                        "id": "codex-claude-review",
                        "title": "Codex plus Claude review",
                        "request": "Implement a small local orchestration helper and review it.",
                        "classification": "CODE_CHANGE",
                        "review_after": True,
                        "dry_run": True,
                    },
                    {
                        "id": "prod-sensitive",
                        "title": "Production-sensitive task",
                        "request": "Change live broker execution settings.",
                        "classification": "PRODUCTION_SENSITIVE",
                        "dry_run": True,
                    },
                ]
            },
        )
        result = process_queue(queue, cfg)
        processed = result["processed"]
        assert processed[0]["status"] == "PASS"
        assert processed[1]["status"] == "PASS"
        assert processed[2]["status"] == "NEEDS_APPROVAL"
        assert (reports / "codex-only.json").exists()
        assert (reports / "codex-claude-review.json").exists()
        assert (reports / "prod-sensitive.json").exists()


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 1 local AI orchestrator")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--queue", default=str(DEFAULT_QUEUE))
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        print("SELF_TEST_PASS")
        return 0

    result = process_queue(Path(args.queue), Path(args.config))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
