"""Lightweight local evaluation harness for NZ-Coder.

The runner reads JSON task specs, optionally runs AgentLoop in non-streaming mode,
executes configured verification commands, and writes JSON/Markdown summaries.
It is designed for local demos and internship-project evaluation, not as a
secure sandbox or distributed benchmark system.
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from nz_coder import config
from nz_coder.changes import load_change_file
from nz_coder.command_policy import classify_bash
from nz_coder.prompt import build as build_prompt


Task = dict[str, Any]


def _safe_path(path: str | Path, *, must_exist: bool = True) -> Path:
    root = Path.cwd().resolve()
    target = (root / Path(path)).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes current workspace: {path}") from exc
    if must_exist and not target.exists():
        raise ValueError(f"path does not exist: {path}")
    return target


def _load_task_file(path: Path) -> list[Task]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [dict(item, _source=str(path)) for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [dict(data, _source=str(path))]
    return []


def load_tasks(tasks_path: str | Path, limit: int | None = None) -> list[Task]:
    """Load eval tasks from a JSON file or directory of JSON files."""
    base = _safe_path(tasks_path)
    files = sorted(base.glob("*.json")) if base.is_dir() else [base]
    tasks: list[Task] = []
    for fp in files:
        tasks.extend(_load_task_file(fp))
    if limit and limit > 0:
        tasks = tasks[:limit]
    return tasks


@contextmanager
def _temporary_workdir(repo: Path, max_turns: int):
    old_workdir = config.WORKDIR
    old_turns = config.MAX_AGENT_TURNS
    config.WORKDIR = repo
    config.MAX_AGENT_TURNS = max_turns
    try:
        yield
    finally:
        config.WORKDIR = old_workdir
        config.MAX_AGENT_TURNS = old_turns


def _run_command(command: str, repo: Path, timeout: int = 120) -> dict:
    classification = classify_bash(command)
    if classification.get("dangerous"):
        return {
            "command": command,
            "passed": False,
            "returncode": None,
            "output": f"Blocked dangerous verification command: {classification['reason']}",
        }
    try:
        result = subprocess.run(
            shlex.split(command),
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return {"command": command, "passed": False, "returncode": None, "output": str(exc)}
    except subprocess.TimeoutExpired:
        return {"command": command, "passed": False, "returncode": None, "output": f"timed out after {timeout}s"}
    output = (result.stdout + result.stderr).strip()
    return {
        "command": command,
        "passed": result.returncode == 0,
        "returncode": result.returncode,
        "output": output[-4000:],
    }


def _run_verification(commands: list[str], repo: Path) -> list[dict]:
    return [_run_command(cmd, repo) for cmd in commands]


def _git_diff(repo: Path) -> tuple[list[str], int]:
    try:
        names = subprocess.run(
            ["git", "diff", "--name-only", "--", ".", ":!.nz-coder"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=5,
        )
        diff = subprocess.run(
            ["git", "diff", "--", ".", ":!.nz-coder"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return [], 0
    if names.returncode not in (0, 1):
        return [], 0
    changed = [line.strip() for line in names.stdout.splitlines() if line.strip()]
    return changed, len(diff.stdout or "")


def _change_tracker_summary(agent) -> tuple[list[str], int]:
    if not agent or not getattr(agent, "change_tracker", None):
        return [], 0
    payload = load_change_file(agent.change_tracker.path)
    changes = payload.get("changes", []) if isinstance(payload, dict) else []
    files = [item.get("path", "") for item in changes if item.get("path")]
    diff_text = agent.change_tracker.render_diff()
    return files, len(diff_text or "")


def _classify_result(status: dict, verification: list[dict], changed_files: list[str], diff_chars: int) -> tuple[str, str]:
    final = status.get("status", "dry_run")
    if final == "aborted":
        return "error", "agent aborted"
    if final == "max_turns":
        return "fail", "max turns reached"
    if verification and all(item.get("passed") for item in verification):
        return "success", "verification passed"
    if verification and any(not item.get("passed") for item in verification):
        if changed_files or diff_chars:
            if status.get("verification_needed") or status.get("last_verification"):
                return "partial", "patch exists but verification failed"
            return "partial", "diff exists but configured verification failed"
        return "fail", "verification failed and no diff detected"
    if changed_files or diff_chars:
        if status.get("verification_needed"):
            return "partial", "diff exists but verification still needed"
        return "partial", "diff exists; no verification configured"
    return "fail", "no diff detected"


def _run_agent(task: Task, repo: Path, mode: str) -> tuple[dict, Any | None]:
    if mode == "dry-run":
        return {"status": "dry_run", "runtime": {"turn_count": 0}, "verification_needed": False, "last_verification": None}, None

    from nz_coder.loop import AgentLoop
    from nz_coder.memory import memory_mgr

    memory_mgr.memory_dir = repo / ".nz-coder" / "memory"
    memory_mgr.load_all()
    agent = AgentLoop(build_prompt(), permission_mode="auto", trace_enabled=True)
    messages = [{"role": "user", "content": task.get("prompt", "")}]
    status = agent.run(messages, stream=False)
    return status, agent


def run_task(task: Task, mode: str) -> dict:
    repo_raw = task.get("repo", ".")
    repo = _safe_path(repo_raw)
    max_turns = int(task.get("max_turns") or config.MAX_AGENT_TURNS)
    started = time.time()
    status: dict = {"status": "not_started"}
    agent = None
    verification: list[dict] = []
    error_summary = ""
    revert_report = ""

    with _temporary_workdir(repo, max_turns):
        try:
            status, agent = _run_agent(task, repo, mode)
            verification = _run_verification([str(c) for c in task.get("verification", [])], repo)
        except Exception as exc:
            status = {"status": "error", "runtime": {"turn_count": 0}}
            error_summary = str(exc)
        tracker_files, tracker_diff = _change_tracker_summary(agent)
        git_files, git_diff_chars = _git_diff(repo)
        changed_files = tracker_files or git_files
        diff_chars = tracker_diff or git_diff_chars
        if agent and getattr(agent, "change_tracker", None):
            try:
                revert_report = agent.change_tracker.revert()
            except Exception as exc:
                error_summary = error_summary or f"revert failed: {exc}"

    result_status, note = _classify_result(status, verification, changed_files, diff_chars)
    if error_summary:
        result_status = "error"
        note = error_summary
    elapsed = time.time() - started
    runtime = status.get("runtime", {}) if isinstance(status, dict) else {}
    return {
        "id": task.get("id", "unknown"),
        "tags": task.get("tags", []),
        "repo": str(repo),
        "status": result_status,
        "turns": runtime.get("turn_count", 0),
        "tool_calls": getattr(agent, "tool_calls_this_run", 0) if agent else 0,
        "elapsed_seconds": round(elapsed, 3),
        "changed_files": changed_files,
        "diff_chars": diff_chars,
        "verification_needed": status.get("verification_needed", False),
        "last_verification": status.get("last_verification"),
        "configured_verification": verification,
        "final_status": status.get("status"),
        "error_summary": error_summary,
        "notes": note,
        "revert_report": revert_report,
        "mode": mode,
    }


def _verification_label(result: dict) -> str:
    checks = result.get("configured_verification", [])
    if not checks:
        return "not configured"
    passed = sum(1 for item in checks if item.get("passed"))
    return f"{passed}/{len(checks)} passed"


def write_results(results: list[dict], output_dir: str | Path = "eval/results") -> tuple[Path, Path]:
    out_dir = _safe_path(output_dir, must_exist=False)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"{stamp}.json"
    md_path = out_dir / f"{stamp}.md"
    payload = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "results": results,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# NZ-Coder Eval Results", "", "| Task | Tags | Status | Turns | Tool Calls | Diff Chars | Verification | Notes |", "|---|---|---:|---:|---:|---:|---|---|"]
    for item in results:
        tags = ", ".join(item.get("tags", []))
        lines.append(
            f"| {item['id']} | {tags} | {item['status']} | {item['turns']} | "
            f"{item['tool_calls']} | {item['diff_chars']} | {_verification_label(item)} | {item.get('notes', '')} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def _resolve_mode(mode: str) -> str:
    if mode != "auto":
        return mode
    return "live" if config.API_KEY else "dry-run"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local NZ-Coder eval tasks.")
    parser.add_argument("--tasks", default="examples/eval_tasks", help="Task JSON file or directory.")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of tasks.")
    parser.add_argument("--mode", choices=("auto", "live", "dry-run"), default="auto", help="auto uses live only when API_KEY is set.")
    parser.add_argument("--output-dir", default="eval/results", help="Result output directory.")
    args = parser.parse_args(argv)

    mode = _resolve_mode(args.mode)
    tasks = load_tasks(args.tasks, limit=args.limit or None)
    results = [run_task(task, mode=mode) for task in tasks]
    json_path, md_path = write_results(results, args.output_dir)
    print(f"mode: {mode}")
    print(f"tasks: {len(results)}")
    print(f"json: {json_path}")
    print(f"markdown: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
