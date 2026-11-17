"""Coding-specific observations injected into the generic Tool Runtime."""
from __future__ import annotations

from contextlib import nullcontext
import asyncio
import threading
import time
from nz_coder.runtime.agent.planning_runtime import observe_execution_handoff


class LegacyCodingToolObserver:
    """Own index, diagnostics, patch, hook, and plan effects after tool work."""

    def __init__(self, host) -> None:
        self._host = host

    def _post_write_facts(self, dispatched: list, messages: list[dict]) -> None:
        observe_execution_handoff(self._host.runtime_state, dispatched, messages, committed_writes=True)
        admission = getattr(self._host, "_admission_session", None)
        if admission is not None:
            for _index, _tool_call, result in dispatched:
                admission.record_committed_mutation(result)
        run = self._host.runtime_state._verification_context
        with run._state_lock if run is not None else nullcontext():
            self._host.recovery.reset_tool_call_history(reason="workspace_changed")
        self._required("_refresh_patch_risk")(messages)
        self._required("_refresh_code_index")(dispatched)

    def post_write(self, dispatched: list, messages: list[dict]) -> None:
        self._post_write_facts(dispatched, messages)
        self._required("_attach_lsp_write_diagnostics")(dispatched, messages)

    async def post_write_async(self, dispatched: list, messages: list[dict]) -> None:
        from nz_coder.foundation.async_utils import to_thread_settled
        from nz_coder.foundation.cancellation import CancellationView
        from nz_coder.lsp.types import LspOperation
        from nz_coder.runtime.core.run_context import current_run_context
        from nz_coder.runtime.core.run_settings import current_run_settings
        from nz_coder.tools import current_tool_cancel_event
        self._post_write_facts(dispatched, messages)
        local = threading.Event()
        run = current_run_context()
        operation = LspOperation(
            CancellationView(local, current_tool_cancel_event(), run.cancel_scope if run else None),
            time.monotonic() + current_run_settings().lsp_diagnostic_wait,
        )
        result = await to_thread_settled(
            self._required("_collect_lsp_write_diagnostics"),
            dispatched, operation=operation, cancel_callback=local.set,
        )
        if operation.cancelled():
            raise asyncio.CancelledError
        # 工作线程仅收集数据，历史只能由仍持有本轮调用的owner追加。
        self._required("_apply_lsp_write_diagnostics")(result, messages)

    def after_batch(self, messages: list[dict], batch_state: dict, on_text) -> None:
        state = getattr(self._host, "runtime_state", None)
        if state is not None:
            observe_execution_handoff(state, batch_state.get("dispatched", []), messages, committed_writes=False)
        self._host.hooks.after_tool_batch(
            self._host,
            messages,
            manual_compact=batch_state["manual_compact"],
            used_todo=batch_state["used_todo"],
            on_text=on_text,
            write_total=batch_state["write_total"],
            write_denied=batch_state["write_denied"],
        )

    def apply_plan_mode(self) -> None:
        self._required("_apply_pending_plan_mode")()

    async def capture_snapshot(self, processor):
        if not processor.step_snapshot:
            return None
        return await self._required("_capture_step_snapshot_async")(
            "step-finish", processor.message_id,
        )

    def record_patch(self, messages, processor, finish_snapshot) -> None:
        self._required("_record_step_patch")(
            messages, processor, finish_snapshot,
        )

    def _required(self, name: str):
        value = getattr(self._host, name, None)
        if not callable(value):
            raise RuntimeError(f"Tool observer is missing required capability {name}")
        return value
