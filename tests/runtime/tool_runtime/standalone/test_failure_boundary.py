"""Failure/cancellation contracts exercised through the real batch pipeline."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from nz_coder.runtime.tool_runtime.pipeline import ProductionToolRuntime
from .support import Dependencies, call


def test_sync_entry_rejects_a_running_loop_before_any_persistence():
    dependencies = Dependencies()

    async def run():
        with pytest.raises(RuntimeError, match="async"):
            ProductionToolRuntime().execute_batch_sync(
                dependencies.context(), [], dependencies.messages
            )

    asyncio.run(run())
    assert dependencies.statuses == []
    assert dependencies.executor.calls == []


@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
def test_checkpoint_failure_cannot_skip_compensation_or_mask_primary(sync, cancel):
    dependencies = Dependencies()
    original = asyncio.CancelledError() if cancel else RuntimeError("PRIVATE_ORIGINAL")

    async def after(call, result, messages):
        raise original

    async def checkpoint(messages, status):
        dependencies.statuses.append(status)
        if status == "interrupted":
            raise OSError("PRIVATE_CHECKPOINT")

    context = dependencies.context(after_tool=after, checkpoint=checkpoint)
    dependencies.prepare([call()])
    if sync:
        with pytest.raises(type(original)) as caught:
            ProductionToolRuntime().execute_batch_sync(
                context, [call()], dependencies.messages
            )
        observed = caught.value
        # Python 3.10's Task creates a new CancelledError at asyncio.run's
        # boundary and retains the original in __context__. Accept only that
        # cancellation chain, never a replacement cleanup error or lost cause.
        seen = set()
        while (
            observed is not original
            and isinstance(observed, asyncio.CancelledError)
            and id(observed) not in seen
        ):
            seen.add(id(observed))
            observed = observed.__context__
    else:
        async def run():
            # Assert at the actual async API boundary, before asyncio.run can
            # recreate a cancelled Task's exception on supported Python 3.10.
            with pytest.raises(type(original)) as caught:
                await ProductionToolRuntime().execute_batch_async(
                    context, [call()], dependencies.messages
                )
            return caught.value

        observed = asyncio.run(run())
    assert observed is original
    assert dependencies.transactions.finishes == [(True, False)]
    assert not dependencies.transactions.active
    assert dependencies.executor.calls == ["call-write"]
    failures = [
        payload
        for event, payload in dependencies.traces
        if event == "tool_batch_cleanup_failed"
    ]
    assert failures and failures[0]["stage"] == "checkpoint"
    assert failures[0]["error_type"] == "OSError"
    assert failures[0]["primary_error_type"] == type(original).__name__
    assert "PRIVATE" not in repr(failures)


@pytest.mark.parametrize("sync", [False, True])
def test_legacy_facade_preserves_injected_runtime(sync):
    from nz_coder.runtime.execution.loop import ProductRunEnvironment
    from nz_coder.runtime.core.tool_context import ToolExecutionContext

    dependencies = Dependencies()
    received = []

    class SelectedRuntime:
        def execute_batch_sync(self, context, calls, messages, **kwargs):
            assert isinstance(context, ToolExecutionContext)
            received.append(context)
            return "selected"

        async def execute_batch_async(self, context, calls, messages, **kwargs):
            return self.execute_batch_sync(context, calls, messages, **kwargs)

    class CompatibilityCaller:
        tool_runtime = SelectedRuntime()

        def tool_execution_context(self, run=None, services=None, *, sync=False):
            return dependencies.context()

    caller = CompatibilityCaller()
    result = (
        ProductRunEnvironment._execute_tools(caller, [], dependencies.messages)
        if sync
        else asyncio.run(
            ProductRunEnvironment._execute_tools_async(
                caller, [], dependencies.messages
            )
        )
    )
    assert result == "selected" and len(received) == 1


@pytest.mark.parametrize(
    "missing", ["checkpoint", "executor", "transaction", "drain_progress"]
)
def test_required_capabilities_fail_at_construction(missing):
    context = Dependencies().context()
    with pytest.raises(TypeError):
        replace(context.lifecycle, **{missing: None})


def test_transaction_begin_failure_still_settles_registered_work():
    dependencies = Dependencies()
    original = OSError("begin fault")

    def begin():
        dependencies.transactions.active = True
        raise original

    dependencies.transactions.begin = begin
    dependencies.prepare([call()])
    with pytest.raises(OSError) as caught:
        asyncio.run(
            ProductionToolRuntime().execute_batch_async(
                dependencies.context(),
                [call()],
                dependencies.messages,
            )
        )
    assert caught.value is original
    assert dependencies.transactions.finishes == [(True, False)]
    assert dependencies.executor.calls == []


@pytest.mark.parametrize("sync", [False, True])
def test_execution_exception_retains_primary_when_compensation_also_fails(sync):
    dependencies = Dependencies()
    original = ValueError("PRIVATE_EXECUTOR")

    def execute(call, index, messages):
        raise original

    def finish(has_write, all_succeeded, messages):
        raise OSError("PRIVATE_ROLLBACK")

    dependencies.transactions.finish = finish
    context = dependencies.context(execute_one=execute)
    dependencies.prepare([call()])
    with pytest.raises(ValueError) as caught:
        if sync:
            ProductionToolRuntime().execute_batch_sync(
                context, [call()], dependencies.messages
            )
        else:
            asyncio.run(
                ProductionToolRuntime().execute_batch_async(
                    context, [call()], dependencies.messages
                )
            )
    assert caught.value is original
    assert dependencies.transactions.active
    failures = [
        payload
        for event, payload in dependencies.traces
        if event == "tool_batch_cleanup_failed"
    ]
    assert [
        (f["stage"], f["error_type"], f["primary_error_type"]) for f in failures
    ] == [
        ("compensation", "OSError", "ValueError"),
    ]
    assert failures[0]["call_ids"] == ["call-write"]
    assert "PRIVATE" not in repr(failures)
