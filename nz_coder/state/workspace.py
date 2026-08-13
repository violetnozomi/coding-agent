"""Workspace and git status helpers for CLI/user visibility."""
from __future__ import annotations

import subprocess
from nz_coder import __version__, config
from nz_coder.state.workdir import current_workdir
from nz_coder.changes import latest_change_file
from nz_coder.sessions import active_session_id, session_runtime_state_path
from nz_coder.trace import latest_trace


def is_git_repo() -> bool:
    result = _git(["rev-parse", "--is-inside-work-tree"])
    return result.returncode == 0 and result.stdout.strip() == "true"


def git_status_short(limit: int = 20) -> list[str]:
    if not is_git_repo():
        return []
    result = _git(["status", "--short"])
    if result.returncode != 0:
        return []
    lines = result.stdout.splitlines()
    if len(lines) > limit:
        return lines[:limit] + [f"... ({len(lines) - limit} more)"]
    return lines


def git_file_status(path: str) -> str:
    if not is_git_repo():
        return ""
    result = _git(["status", "--short", "--", path])
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def project_profile() -> list[str]:
    root = current_workdir()
    facts = []
    if (root / "pyproject.toml").exists():
        facts.append("Python project (pyproject.toml)")
    if (root / "requirements.txt").exists():
        facts.append("Python dependencies (requirements.txt)")
    if (root / "package.json").exists():
        facts.append("Node project (package.json)")
    if (root / "Cargo.toml").exists():
        facts.append("Rust project (Cargo.toml)")
    if (root / "go.mod").exists():
        facts.append("Go module (go.mod)")
    return facts or ["Unknown project type"]


def status_report(agent=None, history: list = None) -> str:
    from nz_coder.providers.models import active_model_selection

    mode = agent.permissions.mode if agent else config.PERMISSION_MODE
    messages = len(history or [])
    session_id = getattr(agent, "session_id", None) or active_session_id()
    latest_run = getattr(getattr(agent, "tracer", None), "path", None)
    if latest_run is None:
        latest_run = latest_trace(session_id=session_id)
    latest_change = latest_change_file()
    runtime_state = (
        session_runtime_state_path(session_id)
        if session_id
        else current_workdir() / ".nz-coder" / "runtime_state.json"
    )
    lines = [
        "# NZ-Coder Status",
        "",
        f"- Version: {__version__}",
        f"- Model: {getattr(agent, 'model_id', None) or active_model_selection().model_id}",
        f"- Workspace: {current_workdir()}",
        f"- Session: {session_id or 'none'}",
        f"- Permission mode: {mode}",
        f"- Conversation messages: {messages}",
        f"- Current trace file: {latest_run if latest_run else 'none'}",
        f"- Runtime state file: {runtime_state}",
        f"- Latest change set: {latest_change if latest_change else 'none'}",
        "",
        "## Project",
    ]
    lines.extend(f"- {fact}" for fact in project_profile())
    lines.extend(["", "## Git"])
    if not is_git_repo():
        lines.append("- Not a git repository")
    else:
        dirty = git_status_short()
        if dirty:
            lines.extend(f"- `{line}`" for line in dirty)
        else:
            lines.append("- Clean working tree")
    return "\n".join(lines)


def _git(args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(current_workdir()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return subprocess.CompletedProcess(["git", *args], 1, "", str(e))
