"""Session processor/progress conversion used by declared tool capabilities."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from typing import Callable
from nz_coder.foundation.async_utils import await_settled
from nz_coder.protocol.message_schema import MESSAGE_ID_KEY
from nz_coder.runtime.core.tool_contracts import ToolCheckpoint
from nz_coder.runtime.session.session_processor import SessionProcessor


class ToolSessionBoundary:
    """Explicit transcript checkpoint and existing synchronous progress-store adapter."""

    def __init__(
        self,
        checkpoint: ToolCheckpoint,
        progress_checkpoint: Callable[[list[dict], str], None] | None,
        publish: Callable[[str, dict], None],
    ) -> None:
        if not callable(checkpoint):
            raise TypeError("ToolSessionBoundary requires a checkpoint callback")
        self.persist = checkpoint
        self._sync_progress = progress_checkpoint
        self.publish = publish
        self._lock = threading.RLock()
        self._pending: list[Future[None]] = []
        self._save_lock = asyncio.Lock()
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    async def checkpoint(self, messages: list[dict], status: str) -> None:
        await self.drain_progress()
        await self._persist_serial(messages, status)

    async def _persist_serial(self, messages: list[dict], status: str) -> None:
        async with self._save_lock:
            # A Store may use to_thread internally. Keep the lock until that
            # operation finishes, including cancellation of a direct checkpoint.
            await await_settled(self.persist(messages, status))

    async def drain_progress(self) -> None:
        """Observe every started save, even after failure or repeated cancellation."""
        errors: list[BaseException] = []
        while True:
            with self._lock:
                pending = list(self._pending)
            if not pending:
                break
            for future in pending:
                task = asyncio.wrap_future(future)
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError as error:
                        errors.append(error)
                    except BaseException:
                        break
                try:
                    task.result()
                except BaseException as error:
                    errors.append(error)
                with self._lock:
                    self._pending.remove(future)
        if errors:
            # Retain the actual first exception; attach only safe type names.
            add_note = getattr(errors[0], "add_note", None)
            if callable(add_note):
                for error in errors[1:]:
                    add_note(
                        f"Additional tool progress failure: {type(error).__name__}"
                    )
            raise errors[0]

    def progress_checkpoint(self, messages: list[dict], status: str) -> None:
        if self._sync_progress is not None:
            self._sync_progress(messages, status)
            return
        if self._loop is None:
            raise RuntimeError("Native tool progress requires its Session event loop")
        # Workers must never block on a save that needs the same executor.
        # Submission and ownership registration are atomic; all saves are
        # drained before this capability is retired or a terminal state saved.
        with self._lock:
            self._pending.append(
                asyncio.run_coroutine_threadsafe(
                    self._persist_serial(messages, status),
                    self._loop,
                )
            )

    def processor_for_messages(
        self,
        messages: list,
    ) -> SessionProcessor | None:
        """Restore the lifecycle handle for the newest durable assistant step."""
        message = next(
            (
                item
                for item in reversed(messages)
                if isinstance(item, dict)
                and item.get("role") == "assistant"
                and isinstance(item.get(MESSAGE_ID_KEY), str)
            ),
            None,
        )
        if message is None:
            return None
        return SessionProcessor(message, publish=self.publish)

    def metadata_reporter(
        self,
        processor: SessionProcessor | None,
        messages: list,
    ):
        """Bridge execution-local tool progress into durable Session parts."""

        def report(title: str, metadata: dict) -> None:
            if processor is None:
                return
            from nz_coder.tools import current_tool_call_id

            call_id = current_tool_call_id()
            if not call_id:
                return
            with self._lock:
                updated = processor.update_tool_metadata(
                    call_id,
                    # Tool-provided titles/metadata can contain private output.
                    # Before after_tool admission, publish only a structural
                    # heartbeat. The settled result retains admitted details.
                    metadata={},
                )
                if updated is not None:
                    self.progress_checkpoint(messages, "running")

        return report

    def question_reporter(
        self,
        processor: SessionProcessor | None,
        messages: list,
    ):
        """Bridge the question tool and UI service into durable display parts."""

        def report(action: str, payload: dict) -> None:
            if processor is None:
                return
            call_id = str(payload.get("tool_call_id") or "")
            if not call_id:
                return
            with self._lock:
                updated = None
                if action == "pending":
                    updated = processor.start_question(
                        call_id,
                        str(payload.get("request_id") or ""),
                        list(payload.get("questions") or []),
                    )
                elif action == "completed":
                    updated = processor.complete_question(
                        call_id,
                        list(payload.get("answers") or []),
                    )
                elif action == "terminated":
                    updated = processor.terminate_question(call_id)
                elif action == "error":
                    updated = processor.fail_question(
                        call_id,
                        str(payload.get("error") or "Question failed"),
                    )
                if updated is not None:
                    self.progress_checkpoint(messages, "running")

        return report
