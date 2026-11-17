"""Message-level workspace revert and unrevert for durable Agent sessions."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from nz_coder.protocol.message_schema import MESSAGE_ID_KEY, PARTS_KEY, is_synthetic_user_message
from nz_coder.state.sessions import write_session_runtime_json
from nz_coder.runtime.session.store import SessionHistoryUnconfirmed
from nz_coder.runtime.process.workspace_snapshot import SnapshotError, WorkspaceSnapshotStore


def copy_fork_snapshots(
    workspace: Path, parent_id: str, child_id: str, messages: list[dict],
    *, target_workspace: Path | None = None,
) -> None:
    """先迁入保留前缀引用的快照，再允许发布子会话。"""
    snapshot_ids = set()
    for message in messages:
        for part in message.get(PARTS_KEY, []):
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"step-start", "step-finish"}:
                snapshot = part.get("snapshot")
            elif part.get("type") == "patch":
                snapshot = part.get("hash")
            else:
                continue
            if isinstance(snapshot, str) and snapshot:
                snapshot_ids.add(snapshot)
    if not snapshot_ids:
        return
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.sessions import session_snapshot_dir

    with scoped_workdir(workspace):
        parent_root = session_snapshot_dir(parent_id)
    destination_workspace = target_workspace if target_workspace is not None else workspace
    with scoped_workdir(destination_workspace):
        child_root = session_snapshot_dir(child_id)
    source = WorkspaceSnapshotStore(workspace, parent_root)
    destination = WorkspaceSnapshotStore(destination_workspace, child_root)
    source.copy_referenced_to(destination, snapshot_ids)


@dataclass(frozen=True)
class SessionRevertResult:
    """Public result of a history plus workspace transition."""

    message_id: str
    files: tuple[str, ...]
    removed_messages: int


class SessionReverter:
    """Coordinate snapshot transitions with one mutable Session history list."""

    def __init__(self, store: WorkspaceSnapshotStore, state_path: Path, *,
                 before_transition: Callable[[], None] | None = None) -> None:
        self.store = store
        self.state_path = Path(state_path)
        self.before_transition = before_transition

    def _prepare_transition(self) -> None:
        if self.before_transition is None:
            return
        try:
            self.before_transition()
        except (OSError, ValueError, TypeError) as exc:
            raise SnapshotError("working plan invalidation failed; transition not started") from exc

    def revert(
        self,
        messages: list[dict],
        *,
        message_id: str | None = None,
        commit_history: Callable[[list[dict]], None] | None = None,
    ) -> SessionRevertResult:
        """Revert the target message and all later Agent workspace changes."""
        target = self._target_index(messages, message_id)
        if target is None:
            raise SnapshotError("no revertible message with step snapshots")
        start_snapshot = self._first_snapshot(messages[target:], "step-start")
        finish_snapshot = self._last_snapshot(messages[target:], "step-finish")
        if not start_snapshot or not finish_snapshot:
            raise SnapshotError("target message range has incomplete step snapshots")

        self._prepare_transition()
        previous_state = self._load_state() if self.state_path.exists() else None
        original = copy.deepcopy(messages)
        recovery_snapshot = self.store.track()
        transition = self.store.transition(finish_snapshot, start_snapshot)
        tail = copy.deepcopy(messages[target:])
        del messages[target:]
        target_id = str(tail[0].get(MESSAGE_ID_KEY) or message_id or "")
        payload = {
            "version": 1,
            "message_id": target_id,
            "start_snapshot": start_snapshot,
            "finish_snapshot": finish_snapshot,
            "recovery_snapshot": recovery_snapshot,
            "files": list(transition.files),
            "history_size": len(messages),
            "history_tail": tail,
        }
        def rollback():
            self.store.transition(start_snapshot, recovery_snapshot, paths=list(transition.files))
            self._restore_state(previous_state)
            messages[:] = copy.deepcopy(original)
        try:
            write_session_runtime_json(self.state_path, payload)
            if commit_history is not None:
                commit_history(messages)
        except SessionHistoryUnconfirmed as exc:
            exc.transition.rollback = rollback
            raise
        except BaseException as exc:
            try:
                rollback()
            except BaseException as rollback_error:
                raise SnapshotError(f"revert persistence failed and rollback failed: {rollback_error}") from exc
            raise SnapshotError("revert persistence failed; transition rolled back") from exc
        return SessionRevertResult(target_id, transition.files, len(tail))

    def unrevert(self, messages: list[dict], *,
                 commit_history: Callable[[list[dict]], None] | None = None) -> SessionRevertResult:
        """Reapply the most recent revert if conversation and files are unchanged."""
        payload = self._load_state()
        if not payload:
            raise SnapshotError("no reverted message range is available")
        expected_size = payload.get("history_size")
        if not isinstance(expected_size, int) or len(messages) != expected_size:
            raise SnapshotError("conversation advanced after revert")
        files = payload.get("files")
        if not isinstance(files, list) or not all(isinstance(path, str) for path in files):
            raise SnapshotError("invalid revert state")
        tail = payload.get("history_tail")
        if not isinstance(tail, list) or not all(isinstance(item, dict) for item in tail):
            raise SnapshotError("invalid reverted history")
        self._prepare_transition()
        original = copy.deepcopy(messages)
        before_snapshot = self.store.track()
        transition = self.store.transition(
            str(payload.get("start_snapshot") or ""),
            str(payload.get("recovery_snapshot") or ""),
            paths=files,
        )
        messages.extend(copy.deepcopy(tail))
        def rollback():
            self.store.transition(str(payload["recovery_snapshot"]), before_snapshot, paths=files)
            self._restore_state(payload)
            messages[:] = copy.deepcopy(original)
        try:
            self.state_path.unlink(missing_ok=True)
            if commit_history is not None:
                commit_history(messages)
        except SessionHistoryUnconfirmed as exc:
            exc.transition.rollback = rollback
            raise
        except BaseException as exc:
            try:
                rollback()
            except BaseException as rollback_error:
                raise SnapshotError(f"redo persistence failed and rollback failed: {rollback_error}") from exc
            raise SnapshotError("redo persistence failed; transition rolled back") from exc
        return SessionRevertResult(
            str(payload.get("message_id") or ""),
            transition.files,
            len(tail),
        )

    def _restore_state(self, payload: dict | None) -> None:
        if payload is None:
            self.state_path.unlink(missing_ok=True)
        else:
            write_session_runtime_json(self.state_path, payload)

    def clear(self) -> None:
        """Invalidate an obsolete unrevert record after new Agent work begins."""
        self.state_path.unlink(missing_ok=True)

    @staticmethod
    def _target_index(messages: list[dict], message_id: str | None) -> int | None:
        if message_id:
            for index, message in enumerate(messages):
                if isinstance(message, dict) and message.get(MESSAGE_ID_KEY) == message_id:
                    return index
            return None
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if (
                isinstance(message, dict)
                and message.get("role") == "user"
                and not is_synthetic_user_message(message)
                and SessionReverter._first_snapshot(messages[index:], "step-start")
            ):
                return index
        return None

    @staticmethod
    def _first_snapshot(messages: list[dict], part_type: str) -> str | None:
        for message in messages:
            for part in message.get(PARTS_KEY, []) if isinstance(message, dict) else []:
                if isinstance(part, dict) and part.get("type") == part_type:
                    snapshot = part.get("snapshot")
                    if isinstance(snapshot, str) and snapshot:
                        return snapshot
        return None

    @staticmethod
    def _last_snapshot(messages: list[dict], part_type: str) -> str | None:
        found = None
        for message in messages:
            for part in message.get(PARTS_KEY, []) if isinstance(message, dict) else []:
                if isinstance(part, dict) and part.get("type") == part_type:
                    snapshot = part.get("snapshot")
                    if isinstance(snapshot, str) and snapshot:
                        found = snapshot
        return found

    def _load_state(self) -> dict:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}
