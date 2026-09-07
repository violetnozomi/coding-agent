"""Step snapshot projection for tool completion; not file recovery authority."""

from __future__ import annotations

import threading
from nz_coder.foundation.async_utils import to_thread_settled as _to_thread_settled
from nz_coder.runtime.core.tool_contracts import ToolTrace
from nz_coder.runtime.process.workspace_snapshot import WorkspaceSnapshotStore
from nz_coder.runtime.session.session_processor import SessionProcessor
from nz_coder.protocol.message_schema import (
    MESSAGE_ID_KEY,
    PARTS_KEY,
    SUMMARY_KEY,
    SESSION_SUMMARY_KEY,
    is_synthetic_user_message,
)


class ToolStepSnapshots:
    """Best-effort snapshot enrichment with explicitly owned dependencies."""

    def __init__(self, store: WorkspaceSnapshotStore | None, trace: ToolTrace) -> None:
        self.store = store
        self.trace = trace

    def capture(
        self,
        boundary: str,
        message_id: str,
        cancel_event: threading.Event | None = None,
    ) -> str | None:
        """Best-effort workspace capture without making Agent execution depend on it."""
        store = self.store
        if not isinstance(store, WorkspaceSnapshotStore):
            return None
        try:
            snapshot = store.track(cancel_event=cancel_event)
        except Exception as exc:
            self.trace(
                "workspace_snapshot_failed",
                boundary=boundary,
                message_id=message_id,
                error=str(exc),
            )
            return None
        self.trace(
            "workspace_snapshot_created",
            boundary=boundary,
            message_id=message_id,
            snapshot=snapshot,
        )
        return snapshot

    async def capture_async(self, boundary: str, message_id: str) -> str | None:
        return await _to_thread_settled(self.capture, boundary, message_id)

    def record_patch(
        self,
        messages: list[dict],
        processor: SessionProcessor,
        finish_snapshot: str | None,
    ) -> None:
        """Create PatchPart plus turn/session summaries from snapshot truth."""
        start_snapshot = processor.step_snapshot
        store = self.store
        if (
            not start_snapshot
            or not finish_snapshot
            or not isinstance(store, WorkspaceSnapshotStore)
        ):
            return
        # Content-addressed snapshots are identical when the step did not
        # change the workspace. Avoid rebuilding the complete Session diff for
        # a guaranteed-empty PatchPart; this is especially expensive in large
        # repositories and delayed queued follow-up takeover after read steps.
        if start_snapshot == finish_snapshot:
            self.trace(
                "workspace_patch_unchanged",
                message_id=processor.message_id,
                snapshot=start_snapshot,
            )
            return
        try:
            files = store.changed_files(start_snapshot, finish_snapshot)
            if files:
                processor.add_patch(start_snapshot, files)
            self._refresh_snapshot_summaries(
                messages,
                processor.message_id,
                finish_snapshot,
            )
            self.trace(
                "workspace_patch_created",
                message_id=processor.message_id,
                snapshot=start_snapshot,
                files=len(files),
            )
        except Exception as exc:
            self.trace(
                "workspace_patch_failed",
                message_id=processor.message_id,
                error=str(exc),
            )

    def _refresh_snapshot_summaries(
        self,
        messages: list[dict],
        assistant_message_id: str,
        finish_snapshot: str,
    ) -> None:
        """Persist net file diffs for one user turn and the whole Session."""
        store = self.store
        assistant_index = next(
            (
                index
                for index, message in enumerate(messages)
                if isinstance(message, dict)
                and message.get(MESSAGE_ID_KEY) == assistant_message_id
            ),
            None,
        )
        if assistant_index is None:
            return
        user_index = next(
            (
                index
                for index in range(assistant_index - 1, -1, -1)
                if isinstance(messages[index], dict)
                and messages[index].get("role") == "user"
                and not is_synthetic_user_message(messages[index])
            ),
            None,
        )
        if user_index is not None:
            turn_start = _first_part_snapshot(
                messages[user_index : assistant_index + 1], "step-start"
            )
            if turn_start:
                messages[user_index][SUMMARY_KEY] = {
                    "diffs": _lightweight_diffs(
                        store.diff_full(turn_start, finish_snapshot)
                    ),
                }

        session_start = _first_part_snapshot(messages, "step-start")
        if not session_start:
            return
        full = _bounded_snapshot_diffs(store.diff_full(session_start, finish_snapshot))
        messages[assistant_index][SESSION_SUMMARY_KEY] = {
            "additions": sum(item["additions"] for item in full),
            "deletions": sum(item["deletions"] for item in full),
            "files": len(full),
            "diffs": full,
        }


def _first_part_snapshot(messages: list[dict], part_type: str) -> str | None:
    for message in messages:
        if not isinstance(message, dict):
            continue
        for part in message.get(PARTS_KEY, []):
            if isinstance(part, dict) and part.get("type") == part_type:
                snapshot = part.get("snapshot")
                if isinstance(snapshot, str) and snapshot:
                    return snapshot
    return None


def _lightweight_diffs(diffs) -> list[dict]:  # noqa: ANN001
    return [
        {
            "file": item.file,
            "additions": max(0, int(item.additions)),
            "deletions": max(0, int(item.deletions)),
            "status": item.status,
        }
        for item in diffs
    ]


def _bounded_snapshot_diffs(
    diffs, *, patch_budget: int = 2 * 1024 * 1024
) -> list[dict]:  # noqa: ANN001
    """Bound cumulative persisted patch text while retaining every file stat."""
    remaining = max(0, int(patch_budget))
    result = []
    for item in diffs:
        patch = str(item.patch or "")
        size = len(patch.encode("utf-8"))
        if size > remaining:
            patch = ""
        else:
            remaining -= size
        result.append(
            {
                "file": item.file,
                "patch": patch,
                "additions": max(0, int(item.additions)),
                "deletions": max(0, int(item.deletions)),
                "status": item.status,
            }
        )
    return result
