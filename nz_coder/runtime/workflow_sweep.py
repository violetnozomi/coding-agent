"""Fail-soft cleanup for clean terminal workflow child worktrees."""
from __future__ import annotations

import time
from pathlib import Path

from nz_coder.runtime.worktree import Worktree, WorktreeManager


_TERMINAL = frozenset({
    "completed", "completed_unverified", "cancelled", "timeout", "error",
    "max_turns", "verification_failed", "verification_failed_rolled_back",
    "tool_error_rolled_back", "interrupted",
})


def _worktree(state: dict) -> Worktree | None:
    value = state.get("worktree")
    if not isinstance(value, dict) or value.get("mode") not in {"git", "copy"}:
        return None
    path = str(value.get("path") or "")
    if not path:
        return None
    return Worktree(
        id=str(value.get("id") or state.get("session_id") or "workflow"),
        path=path,
        branch=str(value.get("branch") or ""),
        based_on=str(value.get("based_on") or "HEAD"),
        head_commit=str(value.get("head_commit") or ""),
        mode=str(value.get("mode")),
        created_at=str(value.get("created_at") or ""),
    )


def sweep_workflow_worktrees(
    states: list[dict],
    workspace: Path,
    *,
    run_id: str = "",
    older_than_seconds: float | None = None,
    now: float | None = None,
) -> dict:
    """Remove only clean terminal worktrees; retain uncertainty and mutations."""
    manager = WorktreeManager(workspace)
    removed = []
    warnings = []
    current = float(now if now is not None else time.time())
    for state in states:
        if run_id and state.get("workflow_run_id") != run_id:
            continue
        if not state.get("workflow_run_id") or state.get("status") not in _TERMINAL:
            continue
        if older_than_seconds is not None:
            ended = float(state.get("finished_at") or state.get("updated_at") or 0)
            if ended <= 0 or current - ended < older_than_seconds:
                continue
        worktree = _worktree(state)
        if worktree is None:
            continue
        if state.get("changed_files"):
            warnings.append(f"retain {worktree.path}: child reported changed files")
            continue
        if worktree.mode == "git" and manager.changed_files(worktree.path):
            warnings.append(f"retain {worktree.path}: worktree has unmerged changes")
            continue
        if worktree.mode == "copy" and state.get("read_only") is not True:
            warnings.append(
                f"retain {worktree.path}: copy-mode write child cleanliness is unknown"
            )
            continue
        try:
            manager.remove(worktree)
            removed.append(worktree.path)
        except Exception as exc:
            warnings.append(f"retain {worktree.path}: cleanup failed: {exc}")
    return {"removed": removed, "warnings": warnings}
