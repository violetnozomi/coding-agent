"""Executable declarations at the tool coordination boundary."""
from __future__ import annotations

from typing import Awaitable, get_args, get_type_hints

from nz_coder.runtime.core.tool_context import ToolLifecycleContext


def test_checkpoint_contract_accepts_transcript_and_status() -> None:
    """A status-only declaration rejects the production two-argument call."""
    parameters, returned = get_args(
        get_type_hints(ToolLifecycleContext)["checkpoint"],
    )
    assert parameters == [list[dict], str]
    assert returned == Awaitable[None]
