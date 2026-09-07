"""Pure ToolRuntime contracts for run isolation and cancelled mixed batches."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import replace
import threading

import pytest

from nz_coder.protocol.message_schema import PARTS_KEY
from nz_coder.runtime.core.profiles import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.core.run_context import RunContext
from nz_coder.runtime.session.model import Session
from nz_coder.runtime.session.session_processor import SessionProcessor
from nz_coder.runtime.tool_runtime.pipeline import ProductionToolRuntime
from nz_coder.tool_platform.execution import ToolExecutionResult
from nz_coder.tools import current_tool_cancel_event
from .support import Dependencies, call


class MemoryTransaction:
    """The transaction port owns only these test-local in-memory writes."""

    def __init__(self, messages):
        self.messages = messages
        self.values = {"existing": "unchanged"}
        self.active = False
        self.events = []

    def begin(self):
        assert not self.active
        self.before = dict(self.values)
        self.active = True
        self.events.append(("begin",))

    def finish(self, has_write, all_succeeded, messages):
        assert messages is self.messages
        assert has_write and self.active
        self.events.append(("finish", all_succeeded))
        if not all_succeeded:
            self.values.clear()
            self.values.update(self.before)
        self.active = False


def memory_run(tmp_path, agent_name):
    """Bind a real SessionProcessor to an in-memory run, without a Store."""
    dependencies = Dependencies()
    session = Session.create(
        f"session-{agent_name}",
        [
            {
                "role": "user",
                "content": agent_name,
                "_nz_message_id": f"msg-user-{agent_name}",
            },
            {
                "role": "assistant",
                "content": "",
                "_nz_message_id": f"msg-step-{agent_name}",
            },
        ],
        workspace=tmp_path,
    )
    dependencies.messages = session.transcript
    dependencies.processor = SessionProcessor(session.transcript[-1])
    dependencies.processor.start_step()
    dependencies.transactions = MemoryTransaction(dependencies.messages)
    request = RunRequest(
        agent=AgentDefinition(agent_name, "Exercise only declared tool ports"),
        profile=MAIN_PROFILE,
        messages=(),
        workspace=tmp_path,
        session_id=session.session_id,
        interaction_run_id=f"interaction-{agent_name}",
    )
    return dependencies, RunContext(request, session, agent_name)


def traced_context(dependencies, run, allowed_tools, **lifecycle):
    context = dependencies.context(**lifecycle)

    def trace_result(result, output, *, tool_call_id, index):
        dependencies.trace(
            "admitted_result",
            call_id=tool_call_id,
            index=index,
            output=output,
            executed=result.executed,
            denied=result.permission_denied,
        )

    return replace(
        context,
        run=run,
        policy=replace(
            context.policy,
            agent_name=run.active_agent,
            tool_allowlist=frozenset(allowed_tools),
        ),
        projection=replace(context.projection, trace_result=trace_result),
    )


def test_concurrent_run_contexts_keep_admission_results_counters_and_transactions_local(
    tmp_path,
):
    """Sharing any run's policy/recording/transaction state must fail this test."""
    release = threading.Event()
    started = {name: threading.Event() for name in ("writer", "reader")}
    pairs = {name: memory_run(tmp_path, name) for name in started}
    executions = {name: Counter() for name in started}

    class Executor:
        def __init__(self, owner, transaction):
            self.owner = owner
            self.transaction = transaction

        def execute_one(self, selected, index):
            name = selected["function"]["name"]
            assert name in {"write_file", "read_file"}
            executions[self.owner][selected["id"]] += 1
            if not started[self.owner].is_set():
                started[self.owner].set()
                assert release.wait(5), "test did not release both independent workers"
            arguments = selected["function"]["arguments"]
            if name == "write_file":
                assert self.transaction.active
                self.transaction.values[arguments["path"]] = arguments["content"]
            return ToolExecutionResult(
                name,
                arguments,
                f"{self.owner}:{selected['id']}",
                True,
                False,
                False,
                name == "write_file",
            )

    contexts = {}
    calls_by_run = {}
    for owner, (dependencies, run) in pairs.items():
        dependencies.executor = Executor(owner, dependencies.transactions)
        contexts[owner] = traced_context(
            dependencies,
            run,
            {"write_file", "read_file"} if owner == "writer" else {"read_file"},
        )
        calls_by_run[owner] = [
            call("write_file", "same-call", path="same.txt", content=owner),
            call("read_file", "same-probe", path="same.txt"),
        ]
        dependencies.prepare(calls_by_run[owner])

    async def execute():
        # The same runtime instance services both contexts concurrently.
        runtime = ProductionToolRuntime()
        tasks = {
            owner: asyncio.create_task(
                runtime.execute_batch_async(
                    contexts[owner],
                    calls_by_run[owner],
                    dependencies.messages,
                )
            )
            for owner, (dependencies, _run) in pairs.items()
        }
        try:
            for event in started.values():
                assert await asyncio.wait_for(asyncio.to_thread(event.wait, 3), 4)
            assert all(not task.done() for task in tasks.values())
            assert all(
                dependencies.transactions.active
                for dependencies, _run in pairs.values()
            )
        finally:
            release.set()
            await asyncio.wait_for(
                asyncio.gather(*tasks.values(), return_exceptions=True), 5
            )
        assert [await task for task in tasks.values()] == ["continue", "continue"]

    asyncio.run(execute())
    writer, reader = pairs["writer"][0], pairs["reader"][0]
    assert executions == {
        "writer": Counter({"same-call": 1, "same-probe": 1}),
        "reader": Counter({"same-probe": 1}),
    }
    assert writer.transactions.values == {"existing": "unchanged", "same.txt": "writer"}
    assert reader.transactions.values == {"existing": "unchanged"}
    assert writer.transactions.events == [("begin",), ("finish", True)]
    assert reader.transactions.events == [("begin",), ("finish", False)]
    assert [result.output for result in writer.recorded] == [
        "writer:same-call",
        "writer:same-probe",
    ]
    denial, read = reader.recorded
    assert not denial.executed and denial.permission_denied and denial.dispatch_failed
    assert denial.metadata["agent"] == "reader"
    assert "'reader'" in denial.output and "'writer'" not in denial.output
    assert read.output == "reader:same-probe"

    for owner, (dependencies, run) in pairs.items():
        assert dependencies.messages is run.transcript
        assert not dependencies.transactions.active
        assert contexts[owner].policy.observability["batches"] == 1
        assert contexts[owner].policy.observability["calls"] == 2
        begins = [
            data for event, data in dependencies.traces if event == "tool_batch_started"
        ]
        ends = [
            data
            for event, data in dependencies.traces
            if event == "tool_batch_completed"
        ]
        assert [data["batch_id"] for data in begins] == ["batch-1"]
        assert [data["batch_id"] for data in ends] == ["batch-1"]
        assert ends[0]["error"] is None
        assert ends[0]["mode"] == (
            "scheduled" if owner == "writer" else "sequential_guarded"
        )
        traces = [
            data for event, data in dependencies.traces if event == "admitted_result"
        ]
        assert [data["call_id"] for data in traces] == ["same-call", "same-probe"]
        assert [data["output"] for data in traces] == [
            result.output for result in dependencies.recorded
        ]
        parts = [
            part
            for part in dependencies.processor.message[PARTS_KEY]
            if part["type"] == "tool"
        ]
        assert [part["call_id"] for part in parts] == ["same-call", "same-probe"]
        assert [part["state"]["status"] for part in parts] == (
            ["completed", "completed"] if owner == "writer" else ["error", "completed"]
        )
    assert not list(tmp_path.iterdir()), (
        "Pure module runs must not create a Store or ledger"
    )


def test_cancelled_mixed_batch_drains_started_worker_compensates_and_never_publishes_success(
    tmp_path,
):
    """Premature cleanup, executing rejected/tail calls, or publishing partial success must fail."""
    dependencies, run = memory_run(tmp_path, "mixed")
    transaction = dependencies.transactions
    started, release = threading.Event(), threading.Event()
    calls = [
        call("write_file", "success", path="first.txt", content="first"),
        call("read_file", "denied", path="forbidden.txt"),
        call("write_file", "started", path="started.txt", content="started"),
        call("write_file", "unstarted", path="tail.txt", content="must not exist"),
    ]
    executed, finished = [], []
    cancelled = []
    checkpoints = []

    class Executor:
        def execute_one(self, selected, index):
            assert selected["function"]["name"] == "write_file"
            assert transaction.active
            identity = selected["id"]
            executed.append(identity)
            arguments = selected["function"]["arguments"]
            transaction.values[arguments["path"]] = arguments["content"]
            if identity == "started":
                started.set()
                assert release.wait(5), "test did not release the cancelled worker"
                event = current_tool_cancel_event()
                cancelled.append(event is not None and event.is_set())
            finished.append(identity)
            transaction.events.append(("worker-finished", identity))
            return ToolExecutionResult(
                "write_file",
                arguments,
                f"private-{identity}-output",
                True,
                False,
                False,
                True,
            )

    async def checkpoint(messages, status):
        assert messages is run.transcript
        checkpoints.append((status, transaction.active, dict(transaction.values)))

    dependencies.executor = Executor()
    context = traced_context(dependencies, run, {"write_file"}, checkpoint=checkpoint)

    async def execute():
        runtime = ProductionToolRuntime()
        approved = await runtime.approve_tool_calls_async(
            context, calls, dependencies.messages
        )
        assert set(approved.blocked) == {1}
        rejected = approved.blocked[1]
        assert (
            not rejected.executed
            and rejected.permission_denied
            and rejected.dispatch_failed
        )
        assert rejected.metadata["agent"] == "mixed"
        dependencies.prepare(calls)
        task = asyncio.create_task(
            runtime.execute_batch_async(
                context,
                calls,
                dependencies.messages,
                approved_batch=approved,
            )
        )
        try:
            assert await asyncio.wait_for(asyncio.to_thread(started.wait, 3), 4)
            assert executed == ["success", "started"]
            assert finished == ["success"]
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            for _ in range(5):
                await asyncio.sleep(0)
            assert not task.done(), "Cancellation must not abandon the started worker"
            assert transaction.active
            assert transaction.values == {
                "existing": "unchanged",
                "first.txt": "first",
                "started.txt": "started",
            }
            assert dependencies.recorded == []
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(execute())
    assert executed == finished == ["success", "started"]
    assert cancelled == [True]
    assert transaction.events == [
        ("begin",),
        ("worker-finished", "success"),
        ("worker-finished", "started"),
        ("finish", False),
    ]
    assert transaction.values == {"existing": "unchanged"}
    assert not transaction.active
    assert checkpoints == [
        ("running", False, {"existing": "unchanged"}),
        ("interrupted", False, {"existing": "unchanged"}),
    ]
    assert dependencies.recorded == []
    assert not any(event == "admitted_result" for event, _data in dependencies.traces)
    assert not any(message["role"] == "tool" for message in dependencies.messages)
    parts = [
        part
        for part in dependencies.processor.message[PARTS_KEY]
        if part["type"] == "tool"
    ]
    assert [part["call_id"] for part in parts] == [
        "success",
        "denied",
        "started",
        "unstarted",
    ]
    assert all(part["state"]["status"] == "error" for part in parts)
    assert all(part["state"]["interrupted"] for part in parts)
    assert all("output" not in part["state"] for part in parts)
    failures = [
        data for event, data in dependencies.traces if event == "tool_batch_failed"
    ]
    assert len(failures) == 1
    assert failures[0]["session_id"] == "session-mixed"
    assert failures[0]["interaction_id"] == "interaction-mixed"
    assert failures[0]["assistant_step_id"] == "msg-step-mixed"
    assert failures[0]["call_ids"] == ["success", "denied", "started", "unstarted"]
    assert failures[0]["primary_error_type"] == "CancelledError"
    assert failures[0]["transaction_active"] is True
    assert not any(
        event == "tool_batch_cleanup_failed" for event, _data in dependencies.traces
    )
    assert "private-" not in repr(dependencies.traces)
    assert not list(tmp_path.iterdir()), (
        "Pure module cancellation must not use disk recovery"
    )
