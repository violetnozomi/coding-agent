"""Session persistence for conversation resume and per-session runtime artifacts."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Callable

from nz_coder import config
from nz_coder.message_schema import (
    MESSAGE_SCHEMA_VERSION,
    is_synthetic_user_message,
    session_diffs,
    session_summary,
)
from nz_coder.state.workdir import current_workdir
from nz_coder.private_paths import harden_private_path

_DEFAULT_SESSION_DIR = Path(config.SESSION_DIR)
_DEFAULT_SESSION_TITLE = "New Session"
_SESSION_TITLE_LIMIT = 100
_ACTIVE_SESSION: ContextVar[tuple[Path, str] | None] = ContextVar(
    "nz_coder_active_session",
    default=None,
)
_BACKGROUND_DISPOSER: Callable[[Path, str], None] | None = None
_WORKTREE_REMOVER: Callable[[Path, str, Path], None] | None = None


def configure_session_cleanup(
    *,
    background_disposer: Callable[[Path, str], None] | None = None,
    worktree_remover: Callable[[Path, str, Path], None] | None = None,
) -> None:
    """Install runtime-owned cleanup adapters without reversing dependencies."""
    global _BACKGROUND_DISPOSER, _WORKTREE_REMOVER
    _BACKGROUND_DISPOSER = background_disposer
    _WORKTREE_REMOVER = worktree_remover


def _active_model_id() -> str:
    """Resolve workspace model metadata without introducing import-time cycles."""
    from nz_coder.providers.models import active_model_selection

    return active_model_selection().model_id


def session_dir() -> Path:
    configured = Path(getattr(config, "SESSION_DIR", _DEFAULT_SESSION_DIR))
    default_current = current_workdir() / ".nz-coder" / "sessions"
    if configured == _DEFAULT_SESSION_DIR:
        return default_current
    # Treat a default-shaped path from another workspace as stale and re-root it.
    if configured.name == "sessions" and configured.parent.name == ".nz-coder":
        workspace_root = configured.parent.parent
        if workspace_root != current_workdir():
            return default_current
    return configured


def create_session_id(prefix: str = "session") -> str:
    safe_prefix = "".join(c for c in prefix if c.isalnum() or c in ("_", "-"))
    safe_prefix = safe_prefix.strip("-_") or "session"
    return f"{safe_prefix}-{time.strftime('%Y%m%d_%H%M%S')}-{uuid.uuid4().hex[:8]}"


def ensure_session(session_id: str | None = None) -> str:
    current = _safe_session_id(session_id or active_session_id() or create_session_id())
    activate_session(current)
    return current


def save_session(
    messages: list,
    mode: str = None,
    session_id: str = None,
    activate: bool = True,
    run_status: str | None = None,
    require_aliases: bool = True,
    title: str | None = None,
    parent_session_id: str | None = None,
    session_metadata: dict | None = None,
    model: str | None = None,
) -> Path:
    session_id = _safe_session_id(session_id or active_session_id() or create_session_id())
    base = session_dir()
    base.mkdir(parents=True, exist_ok=True)
    _harden_session_directory(base)
    session_artifact_dir(session_id).mkdir(parents=True, exist_ok=True)
    _harden_session_directory(session_artifact_dir(session_id))
    session_runtime_dir(session_id).mkdir(parents=True, exist_ok=True)
    _harden_session_directory(session_runtime_dir(session_id))
    path = _session_path(session_id)
    existing = _read_json(path)
    resolved_title = _normalize_title(
        title if title is not None else existing.get("title", _DEFAULT_SESSION_TITLE)
    ) or _DEFAULT_SESSION_TITLE
    if resolved_title == _DEFAULT_SESSION_TITLE:
        resolved_title = fallback_session_title(messages) or resolved_title
    payload = {
        "session_id": session_id,
        "message_schema_version": MESSAGE_SCHEMA_VERSION,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "workspace": str(current_workdir()),
        "model": str(model or _active_model_id()),
        "mode": mode or config.PERMISSION_MODE,
        "messages": messages,
        "artifacts_dir": str(session_artifact_dir(session_id)),
        "runtime_dir": str(session_runtime_dir(session_id)),
    }
    if resolved_title:
        payload["title"] = resolved_title
    if run_status is not None:
        payload["run_status"] = str(run_status)
    resolved_parent = (
        _safe_session_id(parent_session_id)
        if parent_session_id is not None
        else existing.get("parent_session_id")
    )
    if resolved_parent:
        if resolved_parent == session_id:
            raise ValueError("Session cannot be its own parent")
        payload["parent_session_id"] = resolved_parent
    resolved_metadata = (
        dict(session_metadata)
        if isinstance(session_metadata, dict)
        else existing.get("metadata")
    )
    if resolved_metadata:
        payload["metadata"] = resolved_metadata
    summary = session_summary(messages)
    diffs = session_diffs(messages)
    if summary:
        payload["summary"] = summary
    if diffs:
        diff_path = session_diff_path(session_id)
        try:
            write_session_runtime_json(diff_path, {"version": 1, "diffs": diffs})
        except OSError:
            pass
        else:
            payload["diff_path"] = str(diff_path)
    # The session-ID file is the authoritative commit point. HTTP callers opt
    # out of requiring convenience aliases because they restore strictly by
    # ID; interactive CLI callers retain the historical strict alias behavior.
    _write_json(path, payload)
    try:
        _write_json(_latest_path(), payload)
    except OSError:
        if require_aliases:
            raise
    if activate:
        try:
            _write_json(_active_path(), payload)
        except OSError:
            if require_aliases:
                raise
    return path


def rename_session(session_id: str, title: str) -> str:
    """Atomically update one Session title and matching convenience aliases."""
    safe = _safe_session_id(session_id)
    normalized = _normalize_title(title)
    if not normalized:
        raise ValueError("Session title cannot be empty")
    path = _session_path(safe)
    payload = _read_json(path)
    if not payload:
        active_payload = _read_json(_active_path())
        if active_payload.get("session_id") == safe:
            payload = active_payload
    if not payload:
        raise ValueError(f"Unknown session '{safe}'")
    payload["title"] = normalized
    _write_json(path, payload)
    for alias in (_latest_path(), _active_path()):
        alias_payload = _read_json(alias)
        if alias_payload.get("session_id") == safe:
            alias_payload["title"] = normalized
            _write_json(alias, alias_payload)
    return normalized


def fallback_session_title(messages: list) -> str | None:
    """Derive InfCode's bounded deterministic title from the first real User."""
    first = next(
        (
            message
            for message in messages
            if isinstance(message, dict)
            and message.get("role") == "user"
            and not is_synthetic_user_message(message)
        ),
        None,
    )
    if first is None:
        return None
    content = first.get("content", "")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    else:
        text = ""
    text = " ".join(text.split()).strip()
    if not text:
        return None
    if len(text) > _SESSION_TITLE_LIMIT:
        return f"{text[:_SESSION_TITLE_LIMIT - 3]}..."
    return text


def delete_session(session_id: str) -> bool:
    """Delete one persisted Session and all Session-owned runtime artifacts.

    Active Agent runs must be settled by their owner before this function is
    called.  Child worktrees are removed before their state records so a
    failed teardown remains diagnosable and retryable.
    """
    raw = str(session_id or "")
    safe = _safe_session_id(raw)
    if raw != safe or raw in {"active", "latest"}:
        raise ValueError("Session deletion requires an exact persisted Session ID")
    path = _session_path(safe)
    artifacts = session_artifact_dir(safe)
    if not path.exists() and not artifacts.exists():
        return False

    _dispose_session_background_manager(safe)
    _remove_session_worktrees(safe)
    _remove_owned_tree(artifacts, session_dir() / "_artifacts")
    path.unlink(missing_ok=True)
    session_plan_path(safe).unlink(missing_ok=True)

    contextual = _ACTIVE_SESSION.get()
    if contextual == (current_workdir(), safe):
        _ACTIVE_SESSION.set(None)
    _repair_alias_after_delete(_active_path(), safe)
    _repair_alias_after_delete(_latest_path(), safe)
    return True


def load_session(session_id: str = "latest") -> dict:
    if session_id == "active":
        payload = _read_json(_active_path()) or _read_json(_latest_path())
        if not payload:
            return {}
        real_id = payload.get("session_id") or ""
        return _read_json(_session_path(real_id)) or payload

    path = _session_path(session_id)
    if not path.exists():
        return {}
    return _read_json(path)


def list_sessions(limit: int = 10) -> list[Path]:
    base = session_dir()
    if not base.exists():
        return []
    files = [
        p for p in base.glob("*.json")
        if p.name not in {"latest.json", "active.json"}
    ]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]


def describe_sessions(limit: int = 10) -> str:
    sessions = list_sessions(limit)
    if not sessions:
        return "No saved sessions."
    active = active_session_id()
    lines = ["Saved sessions:"]
    for path in sessions:
        payload = load_session(path.stem)
        msg_count = len(payload.get("messages", []))
        marker = " (active)" if path.stem == active else ""
        lines.append(
            f"- {path.stem}{marker}: {payload.get('timestamp', '-')} ({msg_count} messages)"
        )
    return "\n".join(lines)


def active_session_id() -> str | None:
    contextual = _ACTIVE_SESSION.get()
    if contextual and contextual[0] == current_workdir():
        return contextual[1]
    payload = _read_json(_active_path()) or _read_json(_latest_path())
    if not isinstance(payload, dict):
        return None
    session_id = payload.get("session_id")
    return _safe_session_id(session_id) if session_id else None


def activate_session(session_id: str) -> str:
    safe = _safe_session_id(session_id)
    _ACTIVE_SESSION.set((current_workdir(), safe))
    session_dir().mkdir(parents=True, exist_ok=True)
    _harden_session_directory(session_dir())
    session_runtime_dir(safe).mkdir(parents=True, exist_ok=True)
    _harden_session_directory(session_runtime_dir(safe))
    payload = _read_json(_session_path(safe)) or {
        "session_id": safe,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "workspace": str(current_workdir()),
        "model": _active_model_id(),
        "mode": config.PERMISSION_MODE,
        "messages": [],
        "artifacts_dir": str(session_artifact_dir(safe)),
        "runtime_dir": str(session_runtime_dir(safe)),
    }
    _write_json(_active_path(), payload)
    return safe


@contextmanager
def scoped_session(session_id: str):
    """Bind an active session to the current thread or async task."""
    safe = _safe_session_id(session_id)
    token = _ACTIVE_SESSION.set((current_workdir(), safe))
    try:
        yield safe
    finally:
        _ACTIVE_SESSION.reset(token)


def session_artifact_dir(session_id: str) -> Path:
    safe = _safe_session_id(session_id)
    return session_dir() / "_artifacts" / safe


def session_runtime_dir(session_id: str | None = None) -> Path:
    current = session_id or active_session_id()
    if not current:
        return current_workdir() / ".nz-coder"
    return session_artifact_dir(current) / "runtime"


def session_runtime_state_path(session_id: str) -> Path:
    return session_artifact_dir(session_id) / "runtime_state.json"


def session_subagent_dir(session_id: str) -> Path:
    return session_artifact_dir(session_id) / "subagents"


def session_trace_dir(session_id: str | None = None) -> Path:
    return session_runtime_dir(session_id) / "runs"


def session_change_dir(session_id: str | None = None) -> Path:
    return session_runtime_dir(session_id) / "changes"


def session_snapshot_dir(session_id: str | None = None) -> Path:
    """Return the content-addressed workspace snapshot directory."""
    return session_runtime_dir(session_id) / "snapshots"


def session_diff_path(session_id: str | None = None) -> Path:
    """Return the bounded snapshot-derived full Session diff artifact."""
    return session_runtime_dir(session_id) / "session_diff.json"


def load_session_diff(session_id: str | None = None) -> list[dict]:
    payload = _read_json(session_diff_path(session_id))
    diffs = payload.get("diffs") if isinstance(payload, dict) else None
    return diffs if isinstance(diffs, list) else []


def session_tool_results_dir(session_id: str | None = None) -> Path:
    return session_runtime_dir(session_id) / "tool-results"


def session_transcript_dir(session_id: str | None = None) -> Path:
    return session_runtime_dir(session_id) / "transcripts"


def session_memory_state_path(session_id: str | None = None) -> Path:
    return session_runtime_dir(session_id) / "memory_state.json"


def session_scratchpad_path(session_id: str | None = None) -> Path:
    """Return the durable working-memory path for one session."""
    return session_runtime_dir(session_id) / "scratchpad.json"


def session_todo_path(session_id: str | None = None) -> Path:
    """Return the durable todo path for one session."""
    return session_runtime_dir(session_id) / "todo.json"


def session_plan_path(session_id: str | None = None) -> Path:
    """Return the project-local, user-reviewable plan path for one session."""
    current = _safe_session_id(session_id or active_session_id() or "session")
    return current_workdir() / ".nz-coder" / "plans" / f"{current}.md"


def write_session_runtime_json(path: Path, payload: dict) -> None:
    """Atomically replace a small JSON checkpoint with a unique temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _harden_session_directory(path.parent)
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
        harden_private_path(path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise


def _harden_session_directory(path: Path) -> None:
    """Protect the state root and one created Session-owned directory."""
    state_root = current_workdir() / ".nz-coder"
    try:
        path.resolve().relative_to(state_root.resolve())
    except (OSError, ValueError):
        state_root = None
    if state_root is not None and state_root.exists():
        harden_private_path(state_root)
    harden_private_path(path)


def list_session_ids() -> list[str]:
    ids: set[str] = set()
    base = session_dir()
    if base.exists():
        for path in base.glob("*.json"):
            if path.name in {"latest.json", "active.json"}:
                continue
            ids.add(path.stem)
        artifacts_root = base / "_artifacts"
        if artifacts_root.exists():
            for child in artifacts_root.iterdir():
                if child.is_dir():
                    ids.add(child.name)
    active = active_session_id()
    if active:
        ids.add(active)
    return sorted(ids)


def _session_path(session_id: str) -> Path:
    if not session_id or session_id == "latest":
        return _latest_path()
    if session_id == "active":
        return _active_path()
    safe = _safe_session_id(session_id)
    return session_dir() / f"{safe}.json"


def _latest_path() -> Path:
    return session_dir() / "latest.json"


def _active_path() -> Path:
    return session_dir() / "active.json"


def _safe_session_id(session_id: str) -> str:
    safe = "".join(c for c in str(session_id or "") if c.isalnum() or c in ("_", "-"))
    return safe or "session"


def _normalize_title(title: object) -> str:
    value = " ".join(str(title or "").split())
    if len(value) > 160:
        raise ValueError("Session title must be at most 160 characters")
    return value


def _dispose_session_background_manager(session_id: str) -> None:
    """Settle process-local child jobs before deleting their durable state."""
    if _BACKGROUND_DISPOSER is not None:
        _BACKGROUND_DISPOSER(current_workdir(), session_id)


def _remove_session_worktrees(session_id: str) -> None:
    root = session_subagent_dir(session_id)
    if _WORKTREE_REMOVER is not None:
        _WORKTREE_REMOVER(current_workdir(), session_id, root)


def _remove_owned_tree(path: Path, owner: Path) -> None:
    """Remove one exact child without following a symlink outside its owner."""
    owner = owner.resolve()
    lexical = path.absolute()
    try:
        relative = lexical.relative_to(owner)
    except ValueError as exc:
        raise ValueError("Session artifact path escapes its owner") from exc
    if len(relative.parts) != 1:
        raise ValueError("Session artifact path must be one owned child")
    if path.is_symlink():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _repair_alias_after_delete(alias: Path, deleted_id: str) -> None:
    payload = _read_json(alias)
    if payload.get("session_id") != deleted_id:
        return
    replacement = next(iter(list_sessions(limit=1)), None)
    if replacement is None:
        alias.unlink(missing_ok=True)
        return
    replacement_payload = _read_json(replacement)
    if replacement_payload:
        _write_json(alias, replacement_payload)
    else:
        alias.unlink(missing_ok=True)


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json(path: Path, payload: dict) -> None:
    write_session_runtime_json(path, payload)
