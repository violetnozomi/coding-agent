"""Track agent-authored file changes for diff review and rollback."""
from __future__ import annotations

import difflib
import json
import time
import uuid
from pathlib import Path

from nz_coder.protocol.message_schema import is_synthetic_user_message
from nz_coder.state.workdir import current_derived_path, current_workdir
from nz_coder.state.sessions import session_change_dir, write_session_runtime_json


class ChangeTracker:
    """Persist before/after text snapshots for files changed by the agent."""

    def __init__(self, run_id: str = None, change_dir: Path = None, enabled: bool = True):
        self.run_id = run_id or time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
        self.change_dir = change_dir or current_derived_path("CHANGE_DIR")
        self.enabled = enabled
        self.path = self.change_dir / f"{self.run_id}.json"
        self.history_start: int | None = None
        self._changes: dict[str, dict] = {}
        if self.enabled:
            self.change_dir.mkdir(parents=True, exist_ok=True)

    def record_before(self, rel_path: str, exists: bool, content: str = "") -> None:
        """Snapshot the original state of a file. First snapshot wins.

        FIX: 旧实现里 ``setdefault`` 之后的 ``if "before" not in item`` 永远为
        False（setdefault 的默认 dict 已含 "before" 键），是一段死代码；并且
        如果 ``record_after`` 因异常顺序先被调用，before 快照会被永久卡在
        空字符串。现在用显式的 ``before_recorded`` 标记：
        - 同一文件多次编辑时，保留**第一次**的 before（原始状态）；
        - 若条目由 record_after 先创建，之后到达的 record_before 仍能补上
          真实的原始内容。
        """
        if not self.enabled:
            return
        if not self._changes:
            _clear_undo_state(self.change_dir)
        key = str(rel_path)
        item = self._changes.get(key)
        if item is None:
            self._changes[key] = {
                "path": key,
                "before_exists": bool(exists),
                "before": content or "",
                "before_recorded": True,
                # after_exists/after 初始化为 None，而非复制 before 的值。
                # 若 record_after 未被调用（工具异常中断），None 明确表示
                # 「after 状态未知」，而不是错误地沿用 before 的值。
                "after_exists": None,
                "after": None,
            }
        elif not item.get("before_recorded"):
            item["before_exists"] = bool(exists)
            item["before"] = content or ""
            item["before_recorded"] = True
        self._save()

    def record_after(self, rel_path: str, exists: bool, content: str = "") -> None:
        if not self.enabled:
            return
        item = self._changes.setdefault(str(rel_path), {
            "path": str(rel_path),
            "before_exists": False,
            "before": "",
            "before_recorded": False,
        })
        item["after_exists"] = bool(exists)
        item["after"] = content or ""
        self._save()

    def render_diff(self) -> str:
        return render_change_diff(load_change_file(self.path))

    def render_current_diff(self) -> str:
        """Render the original before-state against the current workspace state."""
        return render_change_diff(current_change_payload(load_change_file(self.path)))

    def changed_paths(self) -> list[str]:
        """Return repo-relative paths whose before/after snapshots differ."""
        payload = {
            "changes": list(self._changes.values()),
        }
        return changed_files_from_payload(payload)

    def current_changed_paths(self) -> list[str]:
        """Return tracked paths that still differ on disk after commit or rollback."""
        return changed_files_from_payload(current_change_payload(load_change_file(self.path)))

    def current_deleted_paths(self) -> list[str]:
        """Return tracked files that existed before this run and are now absent."""
        return deleted_files_from_payload(current_change_payload(load_change_file(self.path)))

    def revert(self) -> str:
        return revert_change_file(self.path)

    def undo(self, history: list | None = None) -> str:
        """Undo the latest non-undone change set in this session."""
        return undo_latest(self.change_dir, history=history)

    def redo(self, history: list | None = None) -> str:
        """Reapply the most recently undone change set in this session."""
        return redo_latest(self.change_dir, history=history)

    def _save(self) -> None:
        payload = {
            "run_id": self.run_id,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "workspace": str(current_workdir()),
            "history_start": self.history_start,
            "changes": list(self._changes.values()),
        }
        self.change_dir.mkdir(parents=True, exist_ok=True)
        write_session_runtime_json(self.path, payload)


def latest_change_file(change_dir: Path = None) -> Path | None:
    base = change_dir or session_change_dir()
    if not base.exists():
        return None
    files = sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def load_change_file(path: Path | None = None) -> dict:
    target = path or latest_change_file()
    if not target or not target.exists():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def render_latest_diff() -> str:
    return render_change_diff(load_change_file())


def current_change_payload(payload: dict | None = None) -> dict:
    """Refresh tracked after-states from disk while preserving first before-states."""
    source = payload if isinstance(payload, dict) else load_change_file()
    result = dict(source or {})
    refreshed: list[dict] = []
    for raw in source.get("changes", []) if isinstance(source, dict) else []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        rel_path = str(item.get("path") or "").strip()
        try:
            target = _safe_path(rel_path)
        except ValueError:
            item["after_exists"] = None
            item["after"] = None
            refreshed.append(item)
            continue
        exists = target.exists() and target.is_file()
        item["after_exists"] = exists
        item["after"] = (
            target.read_text(encoding="utf-8", errors="replace")
            if exists
            else ""
        )
        refreshed.append(item)
    result["changes"] = refreshed
    return result


def render_current_change_diff(payload: dict | None = None) -> str:
    """Render agent-tracked before-states against current files without Git."""
    return render_change_diff(current_change_payload(payload))


def current_changed_files(payload: dict | None = None) -> list[str]:
    """Return current agent-authored changes without trusting stale after snapshots."""
    return changed_files_from_payload(current_change_payload(payload))


def current_deleted_files(payload: dict | None = None) -> list[str]:
    """Return current agent-authored file deletions without consulting Git."""
    return deleted_files_from_payload(current_change_payload(payload))


def changed_files_from_payload(payload: dict) -> list[str]:
    """Extract changed file paths from a tracked change payload."""
    if not payload or not payload.get("changes"):
        return []
    changed: list[str] = []
    for change in payload.get("changes", []):
        if not isinstance(change, dict):
            continue
        before_exists = bool(change.get("before_exists"))
        after_exists = change.get("after_exists")
        before = change.get("before") or ""
        after = change.get("after") or ""
        if after_exists is None:
            continue
        if before_exists != bool(after_exists) or before != after:
            rel_path = str(change.get("path") or "").strip()
            if rel_path:
                changed.append(rel_path)
    return sorted(dict.fromkeys(changed))


def deleted_files_from_payload(payload: dict) -> list[str]:
    """Extract files whose recorded before-state exists and current after-state does not."""
    if not payload or not payload.get("changes"):
        return []
    deleted = [
        str(change.get("path") or "").strip()
        for change in payload.get("changes", [])
        if isinstance(change, dict)
        and bool(change.get("before_exists"))
        and change.get("after_exists") is False
    ]
    return sorted(dict.fromkeys(path for path in deleted if path))


def render_change_diff(payload: dict) -> str:
    if not payload or not payload.get("changes"):
        return "No agent file changes recorded."
    sections = [f"Change set: {payload.get('run_id', '-')}", f"Workspace: {payload.get('workspace', '-')}", ""]
    for change in payload["changes"]:
        before = change.get("before", "") if change.get("before_exists") else ""
        # after 为 None 表示工具中断、after 状态未记录，渲染为空字符串。
        after_exists = change.get("after_exists")
        after = (change.get("after") or "") if after_exists else ""
        diff = "".join(difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{change['path']}",
            tofile=f"b/{change['path']}",
        ))
        sections.append(f"## {change['path']}")
        sections.append(diff or "(no changes)")
    return "\n".join(sections).rstrip()


def revert_latest() -> str:
    """Backward-compatible one-way revert of the latest change set."""
    return revert_change_file(latest_change_file())


def undo_latest(change_dir: Path | None = None, history: list | None = None) -> str:
    """Undo the latest active change set and persist enough state for redo."""
    base = change_dir or session_change_dir()
    state = _load_undo_state(base)
    undone_names = {
        str(item.get("change_file") or "")
        for item in state.get("undone", [])
        if isinstance(item, dict)
    }
    target = next(
        (path for path in _change_files(base) if path.name not in undone_names),
        None,
    )
    if target is None:
        return "No agent change set available to undo."

    result = revert_change_file(target)
    if not result.startswith("Reverted agent changes:"):
        return result

    history_tail: list = []
    history_size_after_undo: int | None = None
    if history is not None:
        payload = load_change_file(target)
        recorded_start = payload.get("history_start")
        start = (
            recorded_start
            if isinstance(recorded_start, int)
            and 0 <= recorded_start <= len(history)
            else _last_user_message_index(history)
        )
        history_tail = list(history[start:])
        del history[start:]
        history_size_after_undo = len(history)

    state.setdefault("undone", []).append({
        "change_file": target.name,
        "history_tail": history_tail,
        "history_size_after_undo": history_size_after_undo,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    _save_undo_state(base, state)
    return result.replace("Reverted agent changes:", "Undid agent changes:", 1)


def redo_latest(change_dir: Path | None = None, history: list | None = None) -> str:
    """Reapply the most recently undone change set."""
    base = change_dir or session_change_dir()
    state = _load_undo_state(base)
    undone = state.get("undone", [])
    if not undone:
        return "No agent change set available to redo."
    entry = undone[-1]
    if not isinstance(entry, dict):
        return "No agent change set available to redo."

    expected_history_size = entry.get("history_size_after_undo")
    if (
        history is not None
        and expected_history_size is not None
        and len(history) != expected_history_size
    ):
        return (
            "Refused to redo: conversation advanced after undo. "
            "Start a new edit instead."
        )

    change_file = str(entry.get("change_file") or "")
    if Path(change_file).name != change_file or not change_file.endswith(".json"):
        return "Refused to redo: invalid change-set reference."
    target = base / change_file
    result = redo_change_file(target)
    if not result.startswith("Redid agent changes:"):
        return result

    if history is not None:
        history.extend(entry.get("history_tail") or [])
    undone.pop()
    _save_undo_state(base, state)
    return result


def revert_change_file(path: Path | None) -> str:
    """Atomically restore a change set's tracked before-state."""
    return _transition_change_file(
        path,
        source="after",
        destination="before",
        success_heading="Reverted agent changes:",
        refusal_heading="Refused to revert:",
    )


def redo_change_file(path: Path | None) -> str:
    """Atomically restore a previously reverted change set's after-state."""
    return _transition_change_file(
        path,
        source="before",
        destination="after",
        success_heading="Redid agent changes:",
        refusal_heading="Refused to redo:",
    )


def _transition_change_file(
    path: Path | None,
    *,
    source: str,
    destination: str,
    success_heading: str,
    refusal_heading: str,
) -> str:
    payload = load_change_file(path)
    if not payload or not payload.get("changes"):
        return "No agent file changes recorded."

    prepared: list[tuple[str, Path, bool, str, bool, str]] = []
    refused: list[str] = []
    for change in payload["changes"]:
        rel_path = str(change.get("path") or "")
        source_exists_raw = change.get(f"{source}_exists")
        destination_exists_raw = change.get(f"{destination}_exists")
        if source_exists_raw is None:
            refused.append(
                f"{rel_path} ({source} state was never recorded — tool may have been interrupted)"
            )
            continue
        if destination_exists_raw is None:
            refused.append(
                f"{rel_path} ({destination} state was never recorded — tool may have been interrupted)"
            )
            continue
        try:
            target = _safe_path(rel_path)
        except ValueError as exc:
            refused.append(f"{rel_path} ({exc})")
            continue
        current_exists = target.exists()
        current = (
            target.read_text(encoding="utf-8", errors="replace")
            if current_exists
            else ""
        )
        expected_exists = bool(source_exists_raw)
        expected = (change.get(source) or "") if expected_exists else ""
        if current_exists != expected_exists or current != expected:
            refused.append(
                f"{rel_path} (current content no longer matches tracked {source}-state)"
            )
            continue
        destination_exists = bool(destination_exists_raw)
        destination_content = (
            change.get(destination) or ""
            if destination_exists
            else ""
        )
        prepared.append((
            rel_path,
            target,
            destination_exists,
            destination_content,
            current_exists,
            current,
        ))

    if refused:
        return refusal_heading + "\n" + "\n".join(f"- {item}" for item in refused)

    applied: list[tuple[Path, bool, str]] = []
    reports: list[str] = []
    try:
        for rel_path, target, exists, content, original_exists, original in prepared:
            if exists:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                reports.append(f"restored {rel_path}")
            else:
                if target.exists():
                    target.unlink()
                reports.append(f"deleted {rel_path}")
            applied.append((target, original_exists, original))
    except OSError as exc:
        for target, existed, content in reversed(applied):
            try:
                if existed:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
                elif target.exists():
                    target.unlink()
            except OSError:
                pass
        return f"Error: change-set transition failed and was rolled back: {exc}"

    return success_heading + "\n" + "\n".join(f"- {item}" for item in reports)


def _change_files(change_dir: Path) -> list[Path]:
    if not change_dir.exists():
        return []
    candidates = sorted(
        change_dir.glob("*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return [
        path
        for path in candidates
        if load_change_file(path).get("changes")
    ]


def _undo_state_path(change_dir: Path) -> Path:
    return change_dir.parent / "undo_state.json"


def _load_undo_state(change_dir: Path) -> dict:
    path = _undo_state_path(change_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if payload.get("workspace") != str(current_workdir()):
        return {"workspace": str(current_workdir()), "undone": []}
    if not isinstance(payload.get("undone"), list):
        payload["undone"] = []
    return payload


def _save_undo_state(change_dir: Path, state: dict) -> None:
    path = _undo_state_path(change_dir)
    undone = state.get("undone") or []
    if not undone:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "workspace": str(current_workdir()),
        "undone": undone,
    }
    write_session_runtime_json(path, payload)


def _clear_undo_state(change_dir: Path) -> None:
    path = _undo_state_path(change_dir)
    if path.exists():
        path.unlink()


def _last_user_message_index(history: list) -> int:
    for index in range(len(history) - 1, -1, -1):
        message = history[index]
        if (
            isinstance(message, dict)
            and message.get("role") == "user"
            and not is_synthetic_user_message(message)
        ):
            return index
    return len(history)


def _safe_path(p: str) -> Path:
    path = (current_workdir() / p).resolve()
    try:
        path.relative_to(current_workdir().resolve())
    except ValueError:
        raise ValueError(f"Path escapes workspace: {p}")
    return path
