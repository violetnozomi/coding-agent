"""Runtime-owned cleanup boundary for persisted Session deletion."""
from __future__ import annotations

from pathlib import Path

from nz_coder.state import sessions as session_state


def dispose_background_manager(workspace: Path, session_id: str) -> None:
    """Settle Session-owned Agents and persistent processes."""
    from nz_coder.runtime.agent.agent_manager import dispose_background_agent_manager
    from nz_coder.runtime.process.process_service import dispose_session_processes

    dispose_background_agent_manager(workspace, session_id)
    dispose_session_processes(workspace, session_id)


def remove_session_worktrees(
    workspace: Path,
    session_id: str,
    root: Path,
) -> None:
    """Remove worktrees recorded beneath one Session's owned artifact root."""
    del session_id
    from nz_coder.runtime.worktree import Worktree, WorktreeManager

    if not root.is_dir() or root.is_symlink():
        return
    manager = WorktreeManager(workspace)
    removed: set[Path] = set()
    for state_path in sorted(root.glob("*/state.json")):
        state = session_state._read_json(state_path)
        payload = state.get("worktree") if isinstance(state, dict) else None
        if not isinstance(payload, dict):
            continue
        mode = str(payload.get("mode") or "")
        raw_path = str(payload.get("path") or "").strip()
        if mode not in {"git", "copy"} or not raw_path:
            continue
        target = Path(raw_path).resolve()
        if target in removed:
            continue
        manager.remove(Worktree(
            id=str(payload.get("id") or state.get("session_id") or target.name),
            path=str(target),
            branch=str(payload.get("branch") or ""),
            based_on=str(payload.get("based_on") or "HEAD"),
            head_commit=str(payload.get("head_commit") or ""),
            mode=mode,
            created_at=str(payload.get("created_at") or ""),
        ))
        removed.add(target)


def install_session_cleanup() -> None:
    """Wire runtime cleanup capabilities into the state lifecycle ports."""
    session_state.configure_session_cleanup(
        background_disposer=dispose_background_manager,
        worktree_remover=remove_session_worktrees,
    )


def delete_session(session_id: str) -> bool:
    """Delete persisted state after installing runtime-owned cleanup ports."""
    install_session_cleanup()
    return session_state.delete_session(session_id)
