"""Coding-specific observations injected into the generic Tool Runtime."""
from __future__ import annotations


class LegacyCodingToolObserver:
    """Own index, diagnostics, patch, hook, and plan effects after tool work."""

    def __init__(self, host) -> None:
        self._host = host

    def post_write(self, dispatched: list, messages: list[dict]) -> None:
        admission = getattr(self._host, "_admission_session", None)
        if admission is not None:
            for _index, _tool_call, result in dispatched:
                admission.record_committed_mutation(result)
        self._host.recovery.reset_tool_call_history(reason="workspace_changed")
        self._required("_refresh_patch_risk")(messages)
        self._required("_refresh_code_index")(dispatched)
        self._required("_attach_lsp_write_diagnostics")(dispatched, messages)

    def after_batch(self, messages: list[dict], batch_state: dict, on_text) -> None:
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
