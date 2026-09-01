"""Tool Runtime checkpoint ownership at stable async processor boundaries."""
from __future__ import annotations

import asyncio

import pytest

from nz_coder.runtime.agent.guardrails import GuardrailEscalateError
from nz_coder.runtime.core.tool_context import (
    ToolExecutionContext,
    ToolLifecycleContext,
    ToolProjectionContext,
)
from nz_coder.tool_platform.execution import ToolExecutionResult
from nz_coder.runtime.tool_runtime.pipeline import ProductionToolRuntime
from tests.runtime.tool_runtime.test_focused_policy import _context


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


def test_tool_guardrail_escalation_defers_settlement_to_run_boundary() -> None:
    """The tool pipeline must not checkpoint before the atomic run settler."""
    processor = _Processor()
    statuses: list[str] = []

    async def checkpoint(status: str) -> None:
        statuses.append(status)

    async def noop_async(*_args):
        return None

    class Observer:
        post_write = staticmethod(lambda *_args: None)
        after_batch = staticmethod(lambda *_args: None)
        apply_plan_mode = staticmethod(lambda *_args: None)
        capture_snapshot = staticmethod(lambda *_args: None)
        record_patch = staticmethod(lambda *_args: None)

    class Executor:
        @staticmethod
        def execute_one(_tool_call, _index, _messages):
            return ToolExecutionResult(
                name="read_file",
                tool_input={"path": "private.py"},
                output="PRIVATE-TOOL-RESULT",
                executed=True,
                dispatch_failed=False,
                command_failed=False,
                is_write=False,
            )

    async def after_tool(_tool_call, _result, _messages):
        raise GuardrailEscalateError(
            "reviewer",
            "tool",
            "Authorization=Bearer PRIVATE",
        )

    lifecycle = ToolLifecycleContext(
        checkpoint=checkpoint,
        processor_for_messages=lambda _messages: processor,
        write_override=None,
        begin_transaction=lambda: None,
        transaction_active=lambda: False,
        finish_transaction=lambda *_args: None,
        metadata_reporter=lambda *_args: (lambda *_a, **_k: None),
        question_reporter=lambda *_args: (lambda *_a, **_k: None),
        dispatch_override_async=None,
        consume_override=None,
        model_capabilities=None,
        describe_read_results=noop_async,
        strict_completed=lambda _results: False,
        apply_transition=noop_async,
        observer=Observer(),
        has_pre_tool_hooks=lambda: False,
        executor=Executor(),
        execute_one=Executor.execute_one,
        before_tool=lambda call, _messages: _async_pair(call, None),
        after_tool=after_tool,
        trace=lambda *_args, **_kwargs: None,
    )
    projection = ToolProjectionContext(
        signal_from_metadata=lambda _metadata: None,
        record_result=lambda _result: False,
        trace_result=lambda *_args, **_kwargs: None,
        stall_orchestrator=None,
        after_result=lambda *_args: None,
    )
    context = ToolExecutionContext(
        run=None,
        policy=_context(),
        lifecycle=lifecycle,
        projection=projection,
    )
    calls = [{
        "id": "call-after-tool",
        "function": {
            "name": "read_file",
            "arguments": {"path": "private.py"},
        },
    }]

    async def scenario() -> None:
        with pytest.raises(GuardrailEscalateError):
            await ProductionToolRuntime().execute_batch_async(
                context,
                calls,
                [],
                processor=processor,
                checkpoint=checkpoint,
            )

    asyncio.run(scenario())

    assert statuses == ["running"]
    assert processor.interrupted is False


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


async def _async_pair(first, second):
    return first, second
