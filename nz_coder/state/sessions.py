"""Session persistence for conversation resume and per-session runtime artifacts."""
from __future__ import annotations

import json
import heapq
import math
import os
import shutil
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Callable

from nz_coder.foundation import config
from nz_coder.foundation.json_safety import json_safe_value
from nz_coder.protocol.message_schema import (
    MESSAGE_SCHEMA_VERSION, MESSAGE_ID_KEY, PARTS_KEY, _MESSAGE_ID_RE, _PART_ID_RE,
    is_synthetic_user_message,
    session_diffs,
    session_summary,
)
from nz_coder.state.workdir import current_derived_path, current_workdir
from nz_coder.foundation.private_paths import harden_private_path
from nz_coder.protocol.public_error import PublicInputError

class SessionReadError(PublicInputError):
    """会话存在但无法安全恢复；kind 用于跨界面传递稳定原因。"""
    def __init__(self, kind: str, path: Path, *, size_bytes=None, limit_bytes=None, detail=None):
        self.kind = kind
        self.path = path
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes
        safe_detail = " (invalid message parts)" if detail == "invalid persisted message parts" else ""
        super().__init__(f"Session {kind}{safe_detail}", code=kind)


def _session_byte_limit() -> int:
    return 64 * 1024 * 1024


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
    default_current = current_derived_path("SESSION_DIR")
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
    current = _exact_session_id(
        session_id or active_session_id() or create_session_id()
    )
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
    session_id = _exact_session_id(
        session_id or active_session_id() or create_session_id()
    )
    base = session_dir()
    base.mkdir(parents=True, exist_ok=True)
    _harden_session_directory(base)
    session_artifact_dir(session_id).mkdir(parents=True, exist_ok=True)
    _harden_session_directory(session_artifact_dir(session_id))
    session_runtime_dir(session_id).mkdir(parents=True, exist_ok=True)
    _harden_session_directory(session_runtime_dir(session_id))
    path = _session_path(session_id)
    existing = _read_saved_session(path, expected_id=session_id) or {}
    payload, diffs = _session_write_payload(
        messages, mode=mode, session_id=session_id, run_status=run_status,
        title=title, parent_session_id=parent_session_id,
        session_metadata=session_metadata, model=model, existing=existing,
    )
    return _save_session_payload(path, payload, diffs, activate=activate, require_aliases=require_aliases)


def _session_write_payload(
    messages, *, mode=None, session_id, run_status=None, title=None,
    parent_session_id=None, session_metadata=None, model=None, existing=None,
):
    """普通保存与终态重试使用同一持久投影，不另造摘要字段规则。"""
    existing = existing or {}
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
        _exact_session_id(parent_session_id)
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
        payload["diff_path"] = str(diff_path)
    return payload, diffs


def _save_session_payload(path, payload, diffs, *, activate=False, require_aliases=False, strict_diffs=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    _harden_session_directory(path.parent)
    session_id = payload["session_id"]
    for directory in (session_artifact_dir(session_id), session_runtime_dir(session_id)):
        directory.mkdir(parents=True, exist_ok=True)
        _harden_session_directory(directory)
    diff_path = session_diff_path(session_id)
    # 在任何持久文件替换前，先验证最终 UTF-8 字节数和可表示性。
    prepared = _prepare_session_json(path, payload)
    try:
        if diffs:
            try:
                write_session_runtime_json(diff_path, {"version": 1, "diffs": diffs})
            except OSError:
                if strict_diffs:
                    raise
                payload.pop("diff_path")
                prepared.unlink(missing_ok=True)
                prepared = _prepare_session_json(path, payload)
        _publish_session_json(prepared, path)
        for alias in ([_latest_path(), _active_path()] if activate else [_latest_path()]):
            try:
                _publish_session_json(prepared, alias)
            except OSError:
                if require_aliases:
                    raise
    finally:
        prepared.unlink(missing_ok=True)
    return path


def rename_session(session_id: str, title: str) -> str:
    """Atomically update one Session title and matching convenience aliases."""
    safe = _exact_session_id(session_id)
    normalized = _normalize_title(title)
    if not normalized:
        raise ValueError("Session title cannot be empty")
    path = _session_path(safe)
    payload = _read_saved_session(path, expected_id=safe)
    if payload is None:
        active_payload = _read_saved_session(_active_path()) or {}
        if active_payload.get("session_id") == safe:
            payload = active_payload
    if not payload:
        raise ValueError(f"Unknown session '{safe}'")
    payload["title"] = normalized
    aliases = [alias for alias in (_latest_path(), _active_path()) if (_read_saved_session(alias) or {}).get("session_id") == safe]
    prepared = _prepare_session_json(path, payload)
    try:
        _publish_session_json(prepared, path)
        for alias in aliases:
            _publish_session_json(prepared, alias)
    finally:
        prepared.unlink(missing_ok=True)
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
        payload = _read_saved_session(_active_path()) or _read_saved_session(_latest_path())
        if not payload:
            return {}
        real_id = payload.get("session_id") or ""
        return _read_saved_session(_session_path(real_id), expected_id=real_id) or payload

    path = _session_path(session_id)
    return _read_saved_session(path, expected_id=None if session_id == "latest" else session_id) or {}


def list_sessions(limit: int = 10) -> list[Path]:
    bounded = max(0, int(limit))
    if bounded == 0:
        return []
    base = session_dir()
    if not base.exists():
        return []
    dated: list[tuple[float, str, Path]] = []
    for path in base.glob("*.json"):
        if path.name in {"latest.json", "active.json"}:
            continue
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        item = (modified, path.name, path)
        if len(dated) < bounded:
            heapq.heappush(dated, item)
        elif item > dated[0]:
            heapq.heapreplace(dated, item)
    return [path for _modified, _name, path in sorted(dated, reverse=True)]


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
    payload = _read_saved_session(_active_path()) or _read_saved_session(_latest_path())
    if not isinstance(payload, dict):
        return None
    session_id = payload.get("session_id")
    if not session_id:
        return None
    try:
        return _exact_session_id(session_id)
    except ValueError:
        return None


def activate_session(session_id: str) -> str:
    safe = _exact_session_id(session_id)
    session_dir().mkdir(parents=True, exist_ok=True)
    _harden_session_directory(session_dir())
    session_runtime_dir(safe).mkdir(parents=True, exist_ok=True)
    _harden_session_directory(session_runtime_dir(safe))
    payload = _read_saved_session(_session_path(safe), expected_id=safe) or {
        "session_id": safe,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "workspace": str(current_workdir()),
        "model": _active_model_id(),
        "mode": config.PERMISSION_MODE,
        "messages": [],
        "artifacts_dir": str(session_artifact_dir(safe)),
        "runtime_dir": str(session_runtime_dir(safe)),
    }
    prepared = _prepare_session_json(_active_path(), payload)
    try:
        try:
            _publish_session_json(prepared, _active_path())
        except Exception:
            # replace 后的 harden 也可能抛错；只有精确读回候选才发布内存身份。
            if _read_session_payload(_active_path()) != payload:
                raise
    finally:
        prepared.unlink(missing_ok=True)
    # Publish the process-local identity only after the durable alias reaches
    # its commit point; otherwise a disk failure creates a phantom Session.
    _ACTIVE_SESSION.set((current_workdir(), safe))
    return safe


@contextmanager
def scoped_session(session_id: str):
    """Bind an active session to the current thread or async task."""
    safe = _exact_session_id(session_id)
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
        return current_derived_path("SESSION_DIR") / "_runtime"
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
    """Return the private user-owned plan path for one session."""
    current = _safe_session_id(session_id or active_session_id() or "session")
    return current_derived_path("SESSION_DIR") / "_plans" / f"{current}.md"


def write_session_runtime_json(path: Path, payload: dict) -> None:
    """Atomically replace a small JSON checkpoint with a unique temp file."""
    normalized = json_safe_value(payload)
    if not isinstance(normalized, dict):
        raise ValueError("Session runtime checkpoint must be a JSON object")
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
            json.dump(
                normalized,
                handle,
                ensure_ascii=False,
                indent=2,
                default=str,
                allow_nan=False,
            )
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
    state_root = session_dir().parent
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
    safe = _exact_session_id(session_id)
    return session_dir() / f"{safe}.json"


def _latest_path() -> Path:
    return session_dir() / "latest.json"


def _active_path() -> Path:
    return session_dir() / "active.json"


def _safe_session_id(session_id: str) -> str:
    safe = "".join(c for c in str(session_id or "") if c.isalnum() or c in ("_", "-"))
    return safe or "session"


def _filename_component_limit(directory: Path) -> int:
    # 会话目录可能尚未创建；卷查询需要已有的祖先目录。
    while not directory.exists():
        directory = directory.parent
    if os.name != "nt":
        return os.pathconf(directory, "PC_NAME_MAX")
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    root = ctypes.create_unicode_buffer(32768)
    if not kernel.GetVolumePathNameW(str(directory), root, len(root)):
        raise ctypes.WinError(ctypes.get_last_error())
    limit = wintypes.DWORD()
    if not kernel.GetVolumeInformationW(root, None, 0, None, ctypes.byref(limit), None, None, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return limit.value


def _exact_session_id(session_id: str) -> str:
    """Validate one owned identity without aliasing or lossy sanitization."""
    raw = str(session_id or "")
    safe = _safe_session_id(raw)
    try:
        limit = _filename_component_limit(session_dir())
        filename = raw + ".json"
        units = len(filename.encode("utf-16-le")) // 2 if os.name == "nt" else len(os.fsencode(filename))
        representable = units <= limit
    except (OSError, ValueError, UnicodeError):
        representable = False
    if not raw or raw != safe or safe in {"active", "latest"} or not representable:
        raise ValueError("Session ID must be an exact non-reserved identifier")
    return safe


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
    payload = _read_saved_session(alias) or {}
    if payload.get("session_id") != deleted_id:
        return
    replacement = next(iter(list_sessions(limit=1)), None)
    if replacement is None:
        alias.unlink(missing_ok=True)
        return
    replacement_payload = _read_saved_session(replacement, expected_id=replacement.stem) or {}
    if replacement_payload:
        prepared = _prepare_session_json(alias, replacement_payload)
        try:
            _publish_session_json(prepared, alias)
        finally:
            prepared.unlink(missing_ok=True)
    else:
        alias.unlink(missing_ok=True)


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    normalized = json_safe_value(payload)
    return normalized if isinstance(normalized, dict) else {}


def _file_identity(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _read_session_payload(path: Path, *, maximum_bytes: int | None = None, expected_identity=None) -> dict | None:
    limit = _session_byte_limit() if maximum_bytes is None else maximum_bytes
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError as exc:
        if expected_identity is not None:
            raise SessionReadError("changed", path) from exc
        return None
    except OSError as exc:
        raise SessionReadError("unreadable", path) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SessionReadError("unreadable", path)
        identity = _file_identity(before)
        if expected_identity is not None and identity != expected_identity:
            raise SessionReadError("changed", path)
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        try:
            entry = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise SessionReadError("changed", path) from exc
        if identity != _file_identity(after) or identity != _file_identity(entry):
            raise SessionReadError("changed", path)
        if len(raw) > limit:
            raise SessionReadError("resource_limit", path, size_bytes=before.st_size, limit_bytes=limit)
        try:
            payload = json.loads(raw.decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise SessionReadError("corrupt", path) from exc
        if not isinstance(payload, dict):
            raise SessionReadError("corrupt", path)
        try:
            _validate_json_value(payload)
        except (ValueError, RecursionError) as exc:
            raise SessionReadError("corrupt", path) from exc
        return payload
    except OSError as exc:
        raise SessionReadError("unreadable", path) from exc
    finally:
        os.close(descriptor)


def _valid_persisted_parts(message: dict) -> bool:
    """拒绝无法安全恢复的 Part，同时保留原始文件供诊断。"""
    parts = message.get(PARTS_KEY, [])
    if not isinstance(parts, list):
        return False
    if not parts:
        return True
    message_id = message.get(MESSAGE_ID_KEY)
    if not isinstance(message_id, str) or not _MESSAGE_ID_RE.fullmatch(message_id):
        return False
    seen = set()
    for part in parts:
        if not isinstance(part, dict):
            return False
        part_id = part.get("id")
        if (not isinstance(part_id, str) or not _PART_ID_RE.fullmatch(part_id)
                or part_id in seen or part.get("message_id", message_id) != message_id
                or not isinstance(part.get("type"), str)):
            return False
        seen.add(part_id)
        if "status" in part and not isinstance(part["status"], str):
            return False
        state = part.get("state")
        if state is not None:
            if not isinstance(state, dict):
                return False
            if "status" in state and not isinstance(state["status"], str):
                return False
            if "time" in state and not isinstance(state["time"], dict):
                return False
    return True


def _read_saved_session(path: Path, *, expected_id: str | None = None, expected_identity=None) -> dict | None:
    """校验持久文件身份及 Part，旧外层消息修复仍由 Store 负责。"""
    payload = _read_session_payload(path, expected_identity=expected_identity)
    if payload is None:
        return None
    sid = payload.get("session_id")
    try:
        if not isinstance(sid, str) or _exact_session_id(sid) != sid:
            raise ValueError()
        if expected_id is not None and sid != expected_id:
            raise ValueError()
        workspace = payload.get("workspace")
        if not isinstance(workspace, str) or not workspace or Path(workspace).resolve() != current_workdir():
            raise ValueError()
        parent = payload.get("parent_session_id")
        if parent not in (None, ""):
            if not isinstance(parent, str) or _exact_session_id(parent) != parent or parent == sid:
                raise ValueError()
        metadata = payload.get("metadata")
        if "metadata" in payload and not isinstance(metadata, dict):
            raise ValueError()
        messages = payload.get("messages")
        if isinstance(messages, list) and any(isinstance(item, dict) and not _valid_persisted_parts(item) for item in messages):
            raise SessionReadError("corrupt", path, detail="invalid persisted message parts")
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, SessionReadError):
            raise
        raise SessionReadError("corrupt", path) from exc
    return payload

def _validate_json_value(value, depth=0, *, allow_nonfinite=False):
    if depth > 200:
        raise ValueError("Session JSON is too deeply nested")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and (allow_nonfinite or math.isfinite(value)):
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth + 1, allow_nonfinite=allow_nonfinite)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _validate_json_value(item, depth + 1, allow_nonfinite=allow_nonfinite)
        return
    raise ValueError("Session JSON contains an unsupported value")


def _canonical_session_payload(payload: dict) -> dict:
    _validate_json_value(payload, allow_nonfinite=True)
    # 深度先严格检查，避免通用规范化函数静默替换合法历史。
    payload = json_safe_value(payload, max_depth=201)
    _validate_json_value(payload)
    return payload


def _prepare_session_json(path: Path, payload: dict) -> Path:
    payload = _canonical_session_payload(payload)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".session-", suffix=".tmp")
    temp = Path(name)
    limit = _session_byte_limit()
    size = 0
    try:
        with os.fdopen(fd, "wb") as handle:
            encoder = json.JSONEncoder(ensure_ascii=False, indent=2, allow_nan=False)
            for part in encoder.iterencode(payload):
                data = part.encode("utf-8")
                size += len(data)
                if size > limit:
                    raise SessionReadError("resource_limit", path, size_bytes=size, limit_bytes=limit)
                handle.write(data)
            size += 1
            if size > limit:
                raise SessionReadError("resource_limit", path, size_bytes=size, limit_bytes=limit)
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        return temp
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def _publish_session_json(prepared: Path, path: Path) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".session-", suffix=".tmp")
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as target, prepared.open("rb") as source:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        temp.replace(path)
        harden_private_path(path)
    finally:
        temp.unlink(missing_ok=True)


def _write_json(path: Path, payload: dict) -> None:
    write_session_runtime_json(path, payload)
