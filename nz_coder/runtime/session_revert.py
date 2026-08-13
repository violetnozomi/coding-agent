"""Message-level workspace revert and unrevert for durable Agent sessions."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path

from nz_coder.message_schema import MESSAGE_ID_KEY, PARTS_KEY, is_synthetic_user_message
from nz_coder.state.sessions import write_session_runtime_json
from nz_coder.runtime.workspace_snapshot import SnapshotError, WorkspaceSnapshotStore


@dataclass(frozen=True)
class SessionRevertResult:
    """Public result of a history plus workspace transition."""

    message_id: str
    files: tuple[str, ...]
    removed_messages: int


class SessionReverter:
    """Coordinate snapshot transitions with one mutable Session history list."""

    def __init__(self, store: WorkspaceSnapshotStore, state_path: Path) -> None:
        self.store = store
        self.state_path = Path(state_path)

    def revert(
        self,
        messages: list[dict],
        *,
        message_id: str | None = None,
    ) -> SessionRevertResult:
        """Revert the target message and all later Agent workspace changes."""
        target = self._target_index(messages, message_id)
        if target is None:
            raise SnapshotError("no revertible message with step snapshots")
        start_snapshot = self._first_snapshot(messages[target:], "step-start")
        finish_snapshot = self._last_snapshot(messages[target:], "step-finish")
        if not start_snapshot or not finish_snapshot:
            raise SnapshotError("target message range has incomplete step snapshots")

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
        try:
            write_session_runtime_json(self.state_path, payload)
        except Exception as exc:
            messages.extend(tail)
            try:
                self.store.transition(
                    start_snapshot,
                    recovery_snapshot,
                    paths=list(transition.files),
                )
            except SnapshotError as rollback_error:
                raise SnapshotError(
                    "revert state persistence failed and workspace rollback failed: "
                    f"{rollback_error}"
                ) from exc
            raise SnapshotError("revert state persistence failed; transition rolled back") from exc
        return SessionRevertResult(target_id, transition.files, len(tail))

    def unrevert(self, messages: list[dict]) -> SessionRevertResult:
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
        transition = self.store.transition(
            str(payload.get("start_snapshot") or ""),
            str(payload.get("recovery_snapshot") or ""),
            paths=files,
        )
        messages.extend(copy.deepcopy(tail))
        self.state_path.unlink(missing_ok=True)
        return SessionRevertResult(
            str(payload.get("message_id") or ""),
            transition.files,
            len(tail),
        )

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
