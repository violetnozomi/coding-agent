"""Type-check actual tool-side call shapes; not a runtime test substitute."""
from __future__ import annotations

from nz_coder.runtime.core.tool_context import ToolLifecycleContext


async def checkpoint_call(lifecycle: ToolLifecycleContext, messages: list[dict]) -> None:
    await lifecycle.checkpoint(messages, "running")
