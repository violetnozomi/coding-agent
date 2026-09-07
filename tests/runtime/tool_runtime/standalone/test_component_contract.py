"""The same batch contract on explicit test ports and real file/Session/SQLite owners."""

from __future__ import annotations

import asyncio
import copy
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import replace

import pytest

from nz_coder.runtime.core.profiles import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.core.run_context import RunContext
from nz_coder.runtime.execution.tool_effects import ToolTransaction
from nz_coder.runtime.execution.tool_executor import ToolExecutor
from nz_coder.runtime.process.checkpoint_runtime import recovery_run
from nz_coder.runtime.session.model import Session
from nz_coder.runtime.session.runtime import SessionRuntime
from nz_coder.runtime.session.store import LegacyJsonSessionStore
from nz_coder.runtime.session.session_processor import SessionProcessor
from nz_coder.runtime.tool_runtime.pipeline import ProductionToolRuntime
from nz_coder.state.transaction import TransactionManager
from nz_coder.state.tool_ledger import ToolLedger
from nz_coder.state.workdir import scoped_workdir
from nz_coder.tools.files import bind_tool_state

from .support import Dependencies, Permissions, call


@contextmanager
def components(tmp_path, real):
    dependencies = Dependencies()
    with ExitStack() as scopes:
        scopes.enter_context(scoped_workdir(tmp_path))
        context = dependencies.context()
        if real:
            manager = TransactionManager()
            dependencies.executor = ToolExecutor(Permissions())
            dependencies.transactions = ToolTransaction(manager, dependencies.trace)
            session = Session.create("session-contract", [], workspace=tmp_path)
            session.transcript[:] = dependencies.messages
            dependencies.messages = session.transcript
            request = RunRequest(
                agent=AgentDefinition("coder", "Test the boundary"),
                profile=MAIN_PROFILE,
                workspace=tmp_path,
                session_id=session.session_id,
                messages=(),
            )
            run = RunContext(request, session, "coder")
            runtime = SessionRuntime(LegacyJsonSessionStore())

            async def checkpoint(messages, status):
                assert messages is run.transcript
                dependencies.statuses.append(status)
                await runtime.checkpoint(run, status)

            scopes.enter_context(bind_tool_state(manager))
            recovery = scopes.enter_context(recovery_run(tmp_path, session.session_id))
            recovery.attach(run.interaction_run_id, session.transcript)
            context = replace(dependencies.context(checkpoint=checkpoint), run=run)
        yield dependencies, context


@pytest.mark.parametrize(
    "real", [False, True], ids=["declared-fake", "real-executor-store-ledger"]
)
@pytest.mark.parametrize("sync", [False, True], ids=["async", "sync"])
@pytest.mark.parametrize(
    "scenario", ["read", "write", "denial", "checkpoint", "rewrite", "output_block"]
)
def test_shared_batch_contract(tmp_path, real, sync, scenario):
    with components(tmp_path, real) as (dependencies, context):
        selected = call()
        original = context.lifecycle.checkpoint
        if scenario == "read":
            selected = call("read_file", path="a.txt")
            (tmp_path / "a.txt").write_text("visible output", encoding="utf-8")
        if scenario == "denial":
            context.policy.tool_allowlist = frozenset({"read_file"})
        if scenario == "checkpoint":

            async def fail_checkpoint(messages, status):
                raise OSError("contract checkpoint")

            context = replace(
                context,
                lifecycle=replace(context.lifecycle, checkpoint=fail_checkpoint),
            )
        if scenario in {"rewrite", "output_block"}:

            async def after(call, result, messages):
                return replace(
                    result,
                    output="ADMITTED",
                    dispatch_failed=scenario == "output_block",
                )

            context = replace(
                context, lifecycle=replace(context.lifecycle, after_tool=after)
            )

        def execute():
            dependencies.prepare([selected])
            runtime = ProductionToolRuntime()
            if sync:
                return runtime.execute_batch_sync(
                    context, [selected], dependencies.messages
                )
            return asyncio.run(
                runtime.execute_batch_async(context, [selected], dependencies.messages)
            )

        if scenario == "checkpoint":
            with pytest.raises(OSError, match="contract checkpoint"):
                execute()
            assert dependencies.recorded == []
            assert not (tmp_path / "a.txt").exists()
        else:
            assert execute() == "continue"
            (result,) = dependencies.recorded
            assert result.executed == (scenario != "denial")
            assert result.permission_denied == (scenario == "denial")
            assert result.dispatch_failed == (scenario in {"denial", "output_block"})
            messages = [m for m in dependencies.messages if m.get("role") == "tool"]
            assert [m["tool_call_id"] for m in messages] == [selected["id"]]
            if scenario in {"rewrite", "output_block"}:
                assert messages[0]["content"] == "ADMITTED"
            assert not dependencies.transactions.active
            parts = [
                p
                for p in dependencies.processor.message["_nz_parts"]
                if p["type"] == "tool"
            ]
            assert len(parts) == 1 and parts[0]["call_id"] == selected["id"]
            assert parts[0]["state"]["status"] == (
                "error" if scenario in {"denial", "output_block"} else "completed"
            )

        if real:
            ledger = ToolLedger(tmp_path)
            (row,) = ledger.executions("session-contract")
            if scenario in {"denial", "checkpoint"}:
                assert row["execution_state"] == "not_executed"
                assert ledger.mutations("session-contract") == []
            else:
                assert row["execution_state"] == "succeeded"
                assert row["preview_admitted"] == 1
                if scenario in {"rewrite", "output_block"}:
                    assert row["result_preview"] == (
                        "" if scenario == "output_block" else "ADMITTED"
                    )
                if scenario != "read":
                    (mutation,) = ledger.mutations("session-contract")
                    assert mutation["disposition"] == (
                        "compensated" if scenario == "output_block" else "committed"
                    )
                    assert (tmp_path / "a.txt").exists() == (scenario != "output_block")
            if scenario != "checkpoint":
                durable = asyncio.run(
                    LegacyJsonSessionStore().load(
                        context.run.session.identity, tmp_path
                    )
                )
                assert durable is not None
                assert durable.transcript == context.run.transcript
                # Repeating a stable checkpoint is safe; it cannot replay a tool.
                before = copy.deepcopy(ledger.executions("session-contract"))
                asyncio.run(original(dependencies.messages, "running"))
                assert ledger.executions("session-contract") == before


def test_real_cancel_drains_started_write_and_distinguishes_mixed_execution_facts(
    tmp_path,
):
    started, release = threading.Event(), threading.Event()
    with components(tmp_path, True) as (dependencies, context):

        class SelectivePermissions(Permissions):
            def check(self, name, arguments):
                return {
                    "behavior": "deny"
                    if arguments.get("path") == "denied.txt"
                    else "allow",
                    "reason": "contract decision",
                }

        executor = ToolExecutor(SelectivePermissions())

        def execute(selected, index, messages):
            result = executor.execute_one(selected, index)
            if selected["id"] == "call-started":
                started.set()
                assert release.wait(5), (
                    "test failed to release the already-started worker"
                )
            return result

        context = replace(
            context,
            lifecycle=replace(
                context.lifecycle,
                execute_one=execute,
                has_pre_tool_hooks=lambda: True,
            ),
        )
        calls = [
            call("write_file", "call-denied", path="denied.txt", content="denied"),
            call("write_file", "call-success", path="success.txt", content="first"),
            call("write_file", "call-started", path="started.txt", content="second"),
            call(
                "write_file",
                "call-unstarted",
                path="unstarted.txt",
                content="must not run",
            ),
        ]

        async def run():
            dependencies.prepare(calls)
            task = asyncio.create_task(
                ProductionToolRuntime().execute_batch_async(
                    context, calls, dependencies.messages
                )
            )
            try:
                assert await asyncio.wait_for(asyncio.to_thread(started.wait, 3), 4)
                assert (tmp_path / "started.txt").exists()
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(run())
        assert not any(
            (tmp_path / name).exists()
            for name in ("denied.txt", "success.txt", "started.txt", "unstarted.txt")
        )
        assert not dependencies.transactions.active
        rows = {
            row["call_id"]: row
            for row in ToolLedger(tmp_path).executions("session-contract")
        }
        assert rows["call-denied"]["execution_state"] == "not_executed"
        assert rows["call-denied"]["terminal_cause"] == "permission_denied"
        assert rows["call-success"]["execution_state"] == "succeeded"
        assert rows["call-started"]["execution_state"] == "succeeded"
        assert rows["call-unstarted"]["execution_state"] == "not_executed"
        assert rows["call-unstarted"]["terminal_cause"] == "user_cancelled"
        mutations = ToolLedger(tmp_path).mutations("session-contract")
        assert len(mutations) == 2 and all(
            m["disposition"] == "compensated" for m in mutations
        )
        assert all(row["preview_admitted"] == 0 for row in rows.values())
        parts = [
            p
            for p in dependencies.processor.message["_nz_parts"]
            if p["type"] == "tool"
        ]
        assert len(parts) == 4
        assert all(p["state"]["status"] == "error" for p in parts)
        assert all(p["state"].get("interrupted") for p in parts)


def test_real_compensation_failure_keeps_file_and_partial_transaction(
    tmp_path, monkeypatch
):
    (tmp_path / "a.txt").write_text("before", encoding="utf-8")
    with components(tmp_path, True) as (dependencies, context):

        def cannot_restore(*args):
            raise OSError("injected restoration failure")

        async def after(selected, result, messages):
            raise RuntimeError("after execution failure")

        monkeypatch.setattr(
            dependencies.transactions.txn, "_restore_backup", cannot_restore
        )
        context = replace(
            context, lifecycle=replace(context.lifecycle, after_tool=after)
        )
        with pytest.raises(RuntimeError, match="after execution"):
            dependencies.prepare([call()])
            asyncio.run(
                ProductionToolRuntime().execute_batch_async(
                    context, [call()], dependencies.messages
                )
            )
        assert dependencies.transactions.active
        assert dependencies.transactions.txn.state == "rollback_partial"
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "written"
        (row,) = ToolLedger(tmp_path).executions("session-contract")
        (mutation,) = ToolLedger(tmp_path).mutations("session-contract")
        assert row["execution_state"] == "succeeded" and row["preview_admitted"] == 0
        assert mutation["disposition"] != "compensated"
        assert any(event == "transaction_rollback" for event, _ in dependencies.traces)


def test_post_write_observer_failure_cannot_relabel_committed_write(tmp_path):
    from nz_coder.runtime.tool_runtime.observers import NoOpToolObserver

    class FailingObserver(NoOpToolObserver):
        def post_write(self, dispatched, messages):
            raise OSError("post-write observer failed")

    with components(tmp_path, True) as (dependencies, context):
        context = replace(
            context, lifecycle=replace(context.lifecycle, observer=FailingObserver())
        )
        with pytest.raises(OSError, match="observer"):
            dependencies.prepare([call()])
            asyncio.run(
                ProductionToolRuntime().execute_batch_async(
                    context, [call()], dependencies.messages
                )
            )
        assert not dependencies.transactions.active
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "written"
        (row,) = ToolLedger(tmp_path).executions("session-contract")
        (mutation,) = ToolLedger(tmp_path).mutations("session-contract")
        assert row["execution_state"] == "succeeded" and row["preview_admitted"] == 1
        assert mutation["disposition"] == "committed"


def test_real_write_read_edit_share_the_declared_transaction_and_session(tmp_path):
    with components(tmp_path, True) as (dependencies, context):
        selected = [
            call("write_file", "create", path="a.txt", content="before"),
            call("read_file", "read", path="a.txt"),
            call(
                "edit_file", "edit", path="a.txt", old_text="before", new_text="after"
            ),
        ]
        runtime = ProductionToolRuntime()
        for index, tool_call in enumerate(selected):
            if index:
                dependencies.messages.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "_nz_message_id": f"msg-{index}",
                    }
                )
                dependencies.processor = SessionProcessor(dependencies.messages[-1])
                dependencies.processor.start_step()
            dependencies.prepare([tool_call])
            assert (
                asyncio.run(
                    runtime.execute_batch_async(
                        context, [tool_call], dependencies.messages
                    )
                )
                == "continue"
            )
            assert not dependencies.recorded[-1].dispatch_failed
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "after"
        ledger = ToolLedger(tmp_path)
        rows = ledger.executions("session-contract")
        assert len(rows) == 3 and all(
            row["execution_state"] == "succeeded" for row in rows
        )
        mutations = ledger.mutations("session-contract")
        assert len(mutations) == 2 and all(
            row["disposition"] == "committed" for row in mutations
        )
        stored = asyncio.run(
            LegacyJsonSessionStore().load(context.run.session.identity, tmp_path)
        )
        assert stored is not None and stored.transcript == dependencies.messages
        parts = [
            p
            for m in stored.transcript
            for p in m.get("_nz_parts", [])
            if p["type"] == "tool"
        ]
        assert len(parts) == 3 and all(
            p["state"]["status"] == "completed" for p in parts
        )
