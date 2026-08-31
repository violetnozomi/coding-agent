"""Tool Runtime checkpoint ownership at stable async processor boundaries."""
from __future__ import annotations

import asyncio

import pytest

from nz_coder.runtime.tool_runtime.pipeline import ProductionToolRuntime


class _Processor:
    message_id = "msg-tool"
    step_snapshot = None

    def __init__(self) -> None:
        self.interrupted = False
        self.finished = False

    def start_tools(self, _calls) -> None:
        return None

    def interrupt_unsettled(self) -> None:
        self.interrupted = True

    def finish_step(self, *_args, **_kwargs) -> None:
        self.finished = True

    def process_result(self) -> str:
        return "continue"


class _Hooks:
    def after_tool_batch(self, *_args, **_kwargs) -> None:
        return None


class _Tracer:
    def log(self, *_args, **_kwargs) -> None:
        return None


class _Harness:
    active_run_context = object()
    hooks = _Hooks()
    tracer = _Tracer()

    def __init__(self, *, interrupt: bool = False) -> None:
        self.interrupt = interrupt
        self.legacy_checkpoints: list[str] = []

    def _tool_batch_has_write(self, _calls) -> bool:
        return False

    async def _dispatch_tool_calls_async(self, _calls, _has_write, _messages):
        if self.interrupt:
            raise asyncio.CancelledError
        return []

    def _consume_dispatched_tools(self, _dispatched, _messages, **_kwargs):
        return {
            "all_succeeded": True,
            "blocked": False,
            "manual_compact": False,
            "used_todo": False,
            "write_total": 0,
            "write_denied": 0,
            "handoff_signal": None,
        }

    def _strict_verification_completed(self, _dispatched) -> bool:
        return False

    def _finish_tool_transaction(self, *_args) -> None:
        return None

    def _apply_pending_plan_mode(self) -> None:
        return None

    def _record_step_patch(self, *_args) -> None:
        return None

    def _checkpoint_messages(self, _messages, status: str) -> None:
        self.legacy_checkpoints.append(status)


def test_async_tool_boundaries_use_injected_session_checkpoint() -> None:
    """Removing callback precedence would leak production persistence to host."""
    harness = _Harness()
    processor = _Processor()
    statuses: list[str] = []

    async def checkpoint(status: str) -> None:
        statuses.append(status)

    result = asyncio.run(
        ProductionToolRuntime().execute_batch_async(
            harness,
            [],
            [],
            processor=processor,
            checkpoint=checkpoint,
        )
    )

    assert result == "continue"
    assert statuses == ["running", "running"]
    assert harness.legacy_checkpoints == []
    assert processor.finished is True


def test_async_tool_cancellation_checkpoints_interrupted_through_session_runtime() -> None:
    """Cancellation must not bypass the Session-owned interrupted checkpoint."""
    harness = _Harness(interrupt=True)
    processor = _Processor()
    statuses: list[str] = []

    async def checkpoint(status: str) -> None:
        statuses.append(status)

    async def scenario() -> None:
        with pytest.raises(asyncio.CancelledError):
            await ProductionToolRuntime().execute_batch_async(
                harness,
                [],
                [],
                processor=processor,
                checkpoint=checkpoint,
            )

    asyncio.run(scenario())

    assert statuses == ["running", "interrupted"]
    assert harness.legacy_checkpoints == []
    assert processor.interrupted is True


def test_active_run_rejects_missing_session_checkpoint_callback() -> None:
    """An active production run must not silently fall back to legacy storage."""
    harness = _Harness()

    with pytest.raises(RuntimeError, match="SessionRuntime checkpoint"):
        asyncio.run(
            ProductionToolRuntime().execute_batch_async(
                harness,
                [],
                [],
                processor=_Processor(),
            )
        )

    assert harness.legacy_checkpoints == []


def test_async_tool_batch_freezes_one_dynamic_provider_generation() -> None:
    """Scheduling, permission metadata, and dispatch share one MCP snapshot."""
    from nz_coder.tools import (
        dispatch,
        get_tool_side_effect,
        scoped_dynamic_tool_provider,
    )

    provider_calls = []

    def provider():
        provider_calls.append(len(provider_calls) + 1)
        return [{
            "name": "mcp_batch_snapshot",
            "description": "test",
            "parameters": {"type": "object", "properties": {}},
            "handler": lambda: "ok",
            "execution": "read",
            "side_effect": "reads-network",
        }]

    class SnapshotHarness(_Harness):
        async def _dispatch_tool_calls_async(self, _calls, _has_write, _messages):
            assert get_tool_side_effect("mcp_batch_snapshot") == "reads-network"
            assert get_tool_side_effect("mcp_batch_snapshot") == "reads-network"
            assert dispatch("mcp_batch_snapshot", {}) == "ok"
            return []

    async def checkpoint(_status: str) -> None:
        return None

    with scoped_dynamic_tool_provider(provider):
        provider_calls.clear()  # Ignore eager scope validation.
        result = asyncio.run(
            ProductionToolRuntime().execute_batch_async(
                SnapshotHarness(),
                [],
                [],
                processor=_Processor(),
                checkpoint=checkpoint,
            )
        )

    assert result == "continue"
    assert provider_calls == [1]
