"""Lightweight local evaluation harness for NZ-Coder.

The runner reads JSON task specs, optionally runs AgentLoop in non-streaming mode,
executes configured verification commands, and writes JSON/Markdown summaries.
It is designed for local demos and internship-project evaluation, not as a
secure sandbox or distributed benchmark system.
"""
from __future__ import annotations

import asyncio
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
from nz_coder.runtime.execution_context import scoped_runtime_overrides
from nz_coder.runtime.workdir import scoped_workdir


Task = dict[str, Any]

_EXCLUDED_DIRS = {
    ".git", ".nz-coder", ".nz-coder-runs", "node_modules",
    "build", "dist", "__pycache__",
}
_EXCLUDED_PREFIXES = ("eval/results/",)


def _include_repo_file(path: str) -> bool:
    parts = Path(path).parts
    if any(part in _EXCLUDED_DIRS for part in parts):
        return False
    normalized = Path(path).as_posix()
    return not any(normalized.startswith(prefix) for prefix in _EXCLUDED_PREFIXES)


def _add_unique_file(items: list[str], path: str) -> None:
    path = path.strip()
    if path and _include_repo_file(path) and path not in items:
        items.append(path)


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


def _safe_subpath(base: Path, relative: str | Path, *, must_exist: bool = True) -> Path:
    target = (base / Path(relative)).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError as exc:
        raise ValueError(f"path escapes task repo: {relative}") from exc
    if must_exist and not target.exists():
        raise ValueError(f"path does not exist under task repo: {relative}")
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
    with scoped_workdir(repo), scoped_runtime_overrides(max_agent_turns=max_turns):
        yield


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
    changed: list[str] = []
    for line in names.stdout.splitlines():
        _add_unique_file(changed, line)
    return changed, len(diff.stdout or "")


def _repo_dirty(repo: Path) -> dict:
    changed_files: list[str] = []
    untracked_files: list[str] = []
    try:
        changed = subprocess.run(
            ["git", "diff", "--name-only", "--", "."],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if changed.returncode in (0, 1):
            for line in changed.stdout.splitlines():
                _add_unique_file(changed_files, line)

        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "--", "."],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if untracked.returncode in (0, 1):
            for line in untracked.stdout.splitlines():
                _add_unique_file(untracked_files, line)
    except (OSError, subprocess.TimeoutExpired):
        return {"dirty": False, "changed_files": [], "untracked_files": []}
    return {
        "dirty": bool(changed_files or untracked_files),
        "changed_files": changed_files,
        "untracked_files": untracked_files,
    }


def _change_tracker_summary(agent) -> tuple[list[str], int]:
    if not agent or not getattr(agent, "change_tracker", None):
        return [], 0
    payload = load_change_file(agent.change_tracker.path)
    changes = payload.get("changes", []) if isinstance(payload, dict) else []
    files = [item.get("path", "") for item in changes if item.get("path")]
    diff_text = agent.change_tracker.render_diff()
    return files, len(diff_text or "")


def _evidence_summary(agent) -> dict:
    if not agent or not getattr(agent, "run_evidence", None):
        return {
            "created_files_count": 0,
            "modified_files_count": 0,
            "expected_files_count": 0,
            "verification_results_count": 0,
            "tool_failures_count": 0,
            "limitations": [],
        }
    evidence = agent.run_evidence
    return {
        "created_files_count": len(getattr(evidence, "created_files", []) or []),
        "modified_files_count": len(getattr(evidence, "modified_files", []) or []),
        "expected_files_count": len(getattr(evidence, "expected_files", []) or []),
        "verification_results_count": len(getattr(evidence, "verification_results", []) or []),
        "tool_failures_count": len(getattr(evidence, "tool_failures", []) or []),
        "limitations": [str(item) for item in (getattr(evidence, "limitations", []) or [])[:3]],
    }


def _task_type(task: Task) -> str:
    return str(task.get("task_type") or "repo_repair")


def _project_root_dir(repo: Path, task: Task, *, must_exist: bool) -> Path:
    project_root = str(task.get("project_root") or ".")
    return _safe_subpath(repo, project_root, must_exist=must_exist)


def _expected_file_state(base_dir: Path, expected_files: list[str]) -> dict:
    found: list[str] = []
    missing: list[str] = []
    for raw_rel in [str(item) for item in expected_files if str(item).strip()]:
        normalized = raw_rel.strip().lstrip("./")
        prefix = f"{base_dir.name}/" if base_dir.name else ""
        rel = normalized[len(prefix):] if prefix and normalized.startswith(prefix) else normalized
        target = _safe_subpath(base_dir, rel, must_exist=False)
        if target.exists():
            found.append(raw_rel)
        else:
            missing.append(raw_rel)
    return {
        "expected": len(expected_files),
        "found": found,
        "missing": missing,
        "ok": bool(expected_files) and not missing,
    }


def _project_quality(task: Task, project_dir: Path) -> tuple[dict, dict, dict, dict]:
    from nz_coder.project_creation.requirement_analyzer import analyze_project_requirements
    from nz_coder.project_creation.blueprint import create_project_blueprint
    from nz_coder.project_creation.inspector import inspect_generated_project
    from nz_coder.project_creation.completeness import check_project_completeness

    project_spec = analyze_project_requirements(str(task.get("prompt", "")))
    if task.get("project_root"):
        project_spec["project_name"] = project_dir.name
    blueprint = create_project_blueprint(project_spec)
    inspection = inspect_generated_project(str(project_dir), str(project_spec.get("project_type", "")))
    completeness = check_project_completeness(project_spec, blueprint, str(project_dir))
    return project_spec, blueprint, inspection, completeness


def _classify_result(
    task: Task,
    status: dict,
    verification: list[dict],
    changed_files: list[str],
    diff_chars: int,
    expected_state: dict,
    project_completeness: dict | None = None,
) -> tuple[str, str]:
    final = status.get("status", "dry_run")
    task_type = _task_type(task)
    if final == "aborted":
        return "error", "agent aborted"
    if final == "max_turns":
        return "fail", "max turns reached"

    verification_all_passed = bool(verification) and all(item.get("passed") for item in verification)
    verification_any_failed = any(not item.get("passed") for item in verification)
    expected_found = len(expected_state.get("found", []))
    expected_missing = expected_state.get("missing", [])
    quality_status = str((project_completeness or {}).get("status") or "")
    quality_missing = [str(item) for item in (project_completeness or {}).get("missing", []) if str(item).strip()]

    if task_type == "project_creation":
        if expected_state.get("expected") and expected_state.get("ok") and (not verification or verification_all_passed):
            if quality_status and quality_status != "ok":
                return "partial", "; ".join(quality_missing[:2]) or "project completeness check not satisfied"
            return "success", "expected files created and verification passed"
        if not expected_state.get("expected") and verification_all_passed and (changed_files or diff_chars):
            if quality_status and quality_status != "ok":
                return "partial", "; ".join(quality_missing[:2]) or "project completeness check not satisfied"
            return "success", "verification passed"
        if expected_found or changed_files or diff_chars:
            reasons: list[str] = []
            if expected_missing:
                reasons.append("missing expected files")
            if verification_any_failed:
                reasons.append("verification failed")
            elif not verification:
                reasons.append("verification not configured")
            if quality_status and quality_status != "ok":
                reasons.extend(quality_missing[:2])
            return "partial", "; ".join(reasons) or "project scaffold incomplete"
        return "fail", "no project files created"

    if verification_all_passed:
        return "success", "verification passed"
    if verification_any_failed:
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


async def _run_agent(task: Task, repo: Path, mode: str) -> tuple[dict, Any | None]:
    if mode == "dry-run":
        return {
            "status": "dry_run",
            "runtime": {"turn_count": 0},
            "verification_needed": False,
            "last_verification": None,
        }, None

    from nz_coder.memory import memory_mgr
    from nz_coder.runtime.composition import build_product_environment

    memory_mgr.memory_dir = repo / ".nz-coder" / "memory"
    memory_mgr.load_all()
    agent = build_product_environment(
        build_prompt(), permission_mode="auto", trace_enabled=True,
    )
    messages = [{"role": "user", "content": task.get("prompt", "")}]
    try:
        status = await agent.run(messages, stream=False)
    except BaseException:
        agent.close()
        raise
    return status, agent


async def run_task(task: Task, mode: str) -> dict:
    repo_raw = task.get("repo", ".")
    repo = _safe_path(repo_raw)
    max_turns = int(task.get("max_turns") or config.MAX_AGENT_TURNS)
    started = time.time()
    status: dict = {"status": "not_started"}
    agent = None
    verification: list[dict] = []
    error_summary = ""
    revert_report = ""
    expected_state = {"expected": 0, "found": [], "missing": [], "ok": False}
    project_spec: dict = {}
    project_blueprint: dict = {}
    project_inspection: dict = {}
    project_completeness: dict = {}

    try:
        with _temporary_workdir(repo, max_turns):
            verify_root = _project_root_dir(repo, task, must_exist=False)
            try:
                status, agent = await _run_agent(task, repo, mode)
                expected_state = _expected_file_state(verify_root, [str(p) for p in task.get("expected_files", [])])
                verification = _run_verification([str(c) for c in task.get("verification", [])], verify_root)
                if _task_type(task) == "project_creation":
                    project_spec, project_blueprint, project_inspection, project_completeness = _project_quality(task, verify_root)
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
            dirty_state = _repo_dirty(repo)
    finally:
        if agent is not None:
            agent.close()

    result_status, note = _classify_result(
        task,
        status,
        verification,
        changed_files,
        diff_chars,
        expected_state,
        project_completeness,
    )
    if dirty_state.get("dirty"):
        note = f"{note}; repo still dirty after tracked revert"
    if error_summary:
        result_status = "error"
        note = error_summary
    elapsed = time.time() - started
    runtime = status.get("runtime", {}) if isinstance(status, dict) else {}
    evidence = _evidence_summary(agent)
    return {
        "id": task.get("id", "unknown"),
        "task_type": _task_type(task),
        "tags": task.get("tags", []),
        "repo": str(repo),
        "project_root": str(task.get("project_root") or "."),
        "status": result_status,
        "turns": runtime.get("turn_count", 0),
        "tool_calls": getattr(agent, "tool_calls_this_run", 0) if agent else 0,
        "elapsed_seconds": round(elapsed, 3),
        "changed_files": changed_files,
        "diff_chars": diff_chars,
        "expected_files_ok": bool(expected_state.get("ok")),
        "found_expected_files": expected_state.get("found", []),
        "missing_expected_files": expected_state.get("missing", []),
        "project_spec": project_spec,
        "project_blueprint": project_blueprint,
        "project_inspection": project_inspection,
        "project_completeness": project_completeness,
        "verification_needed": status.get("verification_needed", False),
        "last_verification": status.get("last_verification"),
        "configured_verification": verification,
        "final_status": status.get("status"),
        "error_summary": error_summary,
        "notes": note,
        "revert_report": revert_report,
        "dirty_after_revert": bool(dirty_state.get("dirty")),
        "dirty_files_after_revert": dirty_state.get("changed_files", []) + dirty_state.get("untracked_files", []),
        "evidence": evidence,
        "mode": mode,
    }


def _verification_label(result: dict) -> str:
    checks = result.get("configured_verification", [])
    if not checks:
        return "not configured"
    passed = sum(1 for item in checks if item.get("passed"))
    return f"{passed}/{len(checks)} passed"


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def summarize_results(results: list[dict]) -> dict:
    tasks = len(results)
    status_counts = {"success": 0, "partial": 0, "fail": 0, "error": 0}
    for item in results:
        status = item.get("status")
        if status in status_counts:
            status_counts[status] += 1

    total_checks = 0
    passed_checks = 0
    for item in results:
        checks = item.get("configured_verification", [])
        total_checks += len(checks)
        passed_checks += sum(1 for check in checks if check.get("passed"))

    def avg(key: str) -> float:
        if not tasks:
            return 0.0
        return round(sum(float(item.get(key, 0) or 0) for item in results) / tasks, 2)

    return {
        "tasks": tasks,
        "success_count": status_counts["success"],
        "partial_count": status_counts["partial"],
        "fail_count": status_counts["fail"],
        "error_count": status_counts["error"],
        "success_rate": round(status_counts["success"] / tasks, 4) if tasks else 0.0,
        "avg_turns": avg("turns"),
        "avg_tool_calls": avg("tool_calls"),
        "avg_diff_chars": avg("diff_chars"),
        "verification_pass_rate": round(passed_checks / total_checks, 4) if total_checks else 0.0,
        "max_turn_failures": sum(1 for item in results if item.get("final_status") == "max_turns"),
        "no_diff_failures": sum(
            1 for item in results
            if item.get("status") == "fail" and not item.get("changed_files") and not item.get("diff_chars")
        ),
    }


def write_results(results: list[dict], output_dir: str | Path = "eval/results") -> tuple[Path, Path]:
    out_dir = _safe_path(output_dir, must_exist=False)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"{stamp}.json"
    md_path = out_dir / f"{stamp}.md"
    summary = summarize_results(results)
    payload = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": summary,
        "results": results,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# NZ-Coder Eval Results",
        "",
        "## Summary",
        "",
        f"- Tasks: {summary['tasks']}",
        f"- Success: {summary['success_count']}",
        f"- Partial: {summary['partial_count']}",
        f"- Fail: {summary['fail_count']}",
        f"- Error: {summary['error_count']}",
        f"- Success rate: {_pct(summary['success_rate'])}",
        f"- Avg turns: {summary['avg_turns']}",
        f"- Avg tool calls: {summary['avg_tool_calls']}",
        f"- Avg diff chars: {summary['avg_diff_chars']}",
        f"- Verification pass rate: {_pct(summary['verification_pass_rate'])}",
        f"- Max-turn failures: {summary['max_turn_failures']}",
        f"- No-diff failures: {summary['no_diff_failures']}",
        "",
        "## Tasks",
        "",
        "| Task | Type | Tags | Status | Turns | Tool Calls | Diff Chars | Verification | Notes |",
        "|---|---|---|---:|---:|---:|---:|---|---|",
    ]
    for item in results:
        tags = ", ".join(item.get("tags", []))
        rendered = item
        quality = item.get("project_completeness", {})
        if item.get("task_type") == "project_creation" and quality:
            quality_status = quality.get("status", "")
            if quality_status and quality_status != "ok":
                missing = ", ".join(str(value) for value in quality.get("missing", [])[:2])
                if missing:
                    rendered = dict(item)
                    base_note = rendered.get("notes", "")
                    rendered["notes"] = f"{base_note}; quality: {missing}" if base_note else f"quality: {missing}"
        lines.append(
            f"| {rendered['id']} | {rendered.get('task_type', 'repo_repair')} | {tags} | {rendered['status']} | {rendered['turns']} | "
            f"{rendered['tool_calls']} | {rendered['diff_chars']} | {_verification_label(rendered)} | {rendered.get('notes', '')} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def _resolve_mode(mode: str) -> str:
    if mode != "auto":
        return mode
    return "live" if config.API_KEY else "dry-run"


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local NZ-Coder eval tasks.")
    parser.add_argument("--tasks", default="examples/eval_tasks", help="Task JSON file or directory.")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of tasks.")
    parser.add_argument("--mode", choices=("auto", "live", "dry-run"), default="auto", help="auto uses live only when API_KEY is set.")
    parser.add_argument("--output-dir", default="eval/results", help="Result output directory.")
    args = parser.parse_args(argv)

    mode = _resolve_mode(args.mode)
    tasks = load_tasks(args.tasks, limit=args.limit or None)
    results = [await run_task(task, mode=mode) for task in tasks]
    json_path, md_path = write_results(results, args.output_dir)
    print(f"mode: {mode}")
    print(f"tasks: {len(results)}")
    print(f"json: {json_path}")
    print(f"markdown: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
