"""真实工具批次在写后诊断期间取消，仍结清提交结果与会话检查点。"""
from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nz_coder.protocol.message_schema import PARTS_KEY, attach_message_identity
from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.runtime.adapters.verification_runtime import VerificationRuntime
from nz_coder.runtime.core import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.core.run_context import RunContext, scoped_run_context
from nz_coder.runtime.core.run_settings import current_run_settings, scoped_run_settings
from nz_coder.runtime.execution import loop
from nz_coder.runtime.model_gateway import ResolvedModelRuntime
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.session.model import Session
from nz_coder.runtime.session.runtime import SessionRuntime
from nz_coder.runtime.session.session_processor import SessionProcessor
from nz_coder.runtime.session.store import LegacyJsonSessionStore
from nz_coder.runtime.tool_runtime.pipeline import ProductionToolRuntime
from nz_coder.tools import scoped_tool_cancellation
from nz_coder.tools.files import bind_tool_state


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(loop, "update_code_index_after_write", lambda *_args: SimpleNamespace(indexed=0, removed=0))
    target = tmp_path / "created.py"
    calls = [{"id": "call-write", "type": "function", "function": {
        "name": "write_file", "arguments": {"path": "created.py", "content": "value = 2\n"},
    }}]
    assistant = {"role": "assistant", "content": "", "tool_calls": calls}
    attach_message_identity(assistant, "msg-write", session_id="lsp-runtime")
    messages = [assistant]
    model = ResolvedModelRuntime(
        provider_id="offline", model_id="offline", request_model_id="offline", variant=None,
        provider=SimpleNamespace(name="offline"), client=SimpleNamespace(close=lambda: None),
        capabilities=ModelCapabilities(provider="offline", model_id="offline"), owns_client=True,
    )
    settings = replace(current_run_settings(), lsp_enabled=True,
                       lsp_write_diagnostics_enabled=True, lsp_diagnostic_wait=1.0)
    with scoped_workdir(tmp_path), scoped_run_settings(settings):
        environment = loop.AgentLoop("local test", permission_mode="auto", model_runtime=model,
                                     trace_enabled=False, session_id="lsp-runtime")
        request = RunRequest(agent=AgentDefinition(name="test", instructions="test"),
                             profile=MAIN_PROFILE, workspace=tmp_path,
                             session_id="lsp-runtime", messages=())
        run = RunContext(request, Session.create("lsp-runtime", messages, workspace=tmp_path), "test")
        messages = run.transcript
        processor = SessionProcessor(messages[0])
        processor.register_tool_calls(calls)
        environment.runtime_state.bind_run_context(run)
        environment.verification_runtime = VerificationRuntime(environment, run)
        environment.verification_runtime.initialize(restored=False)
        processor.start_step(environment.workspace_snapshots.track())
        checkpoint_path = tmp_path.parent / f"{tmp_path.name}-checkpoint.json"
        session_runtime = SessionRuntime(LegacyJsonSessionStore())

        async def checkpoint(status):
            await session_runtime.checkpoint(run, status)
            saved = await session_runtime.store.load(run.session.identity, tmp_path)
            assert saved is not None
            checkpoint_path.write_text(json.dumps({"status": saved.status.value, "messages": saved.transcript}))

        with scoped_run_context(run), bind_tool_state(environment.txn, environment.change_tracker):
            try:
                yield SimpleNamespace(environment=environment, run=run, processor=processor,
                                      calls=calls, messages=messages, target=target,
                                      checkpoint=checkpoint, checkpoint_path=checkpoint_path)
            finally:
                environment.close()


def _batch():
    return {"enabled": True, "skipped": 0, "reports": [{
        "path": "created.py", "status": "ready", "freshness": "current_push",
        "items": [], "analysis_environment": "configured", "reason_code": "", "truncated": False,
    }]}


async def _wait_for(event):
    for _ in range(200):
        if event.is_set():
            return
        await asyncio.sleep(.005)
    pytest.fail("worker did not reach the controlled boundary")


@pytest.mark.parametrize(("cancel_source", "cancel_during_snapshot"), [
    ("task", False), ("task", True), ("run", False), ("tool", False),
])
def test_committed_write_cancel_keeps_parts_patch_and_checkpoint(
    runtime, monkeypatch, cancel_source, cancel_during_snapshot,
):
    """删除取消结算、或第二次取消打断快照，都会丢失已提交 patch 的检查点。"""
    entered = threading.Event()
    cancelled = threading.Event()
    release_worker = threading.Event()
    exited = threading.Event()
    snapshot_entered = threading.Event()
    release_snapshot = threading.Event()
    collected_calls = []
    tool_cancel = threading.Event()
    original_collect = runtime.environment._collect_lsp_write_diagnostics
    original_capture = runtime.environment._capture_step_snapshot

    def collect(dispatched, *, operation):
        collected_calls.extend(dispatched)
        return original_collect(dispatched, operation=operation)

    def diagnostics(paths, workspace, *, operation):
        assert paths == ["created.py"]
        assert workspace == runtime.target.parent
        entered.set()
        deadline = time.monotonic() + 2
        while not operation.cancelled() and time.monotonic() < deadline:
            time.sleep(.005)
        assert operation.cancelled()
        cancelled.set()
        assert release_worker.wait(2)
        exited.set()
        return _batch()

    def snapshot(*args, **kwargs):
        snapshot_entered.set()
        if cancel_during_snapshot:
            assert release_snapshot.wait(2)
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(loop, "collect_write_diagnostics", diagnostics)
    monkeypatch.setattr(runtime.environment, "_collect_lsp_write_diagnostics", collect)
    monkeypatch.setattr(runtime.environment, "_capture_step_snapshot", snapshot)

    async def scenario():
        task = asyncio.create_task(ProductionToolRuntime().execute_batch_async(
            runtime.environment, runtime.calls, runtime.messages,
            processor=runtime.processor, checkpoint=runtime.checkpoint))
        try:
            await _wait_for(entered)
            before_cancel = copy.deepcopy(runtime.messages)
            assert runtime.target.read_text() == "value = 2\n"
            assert runtime.environment.txn.active is False
            if cancel_source == "run":
                runtime.run.request_cancel()
            elif cancel_source == "tool":
                tool_cancel.set()
            else:
                task.cancel("diagnostic cancel")
            await _wait_for(cancelled)
            # worker 尚未结清时，owner 仍保持活动，事件循环继续响应。
            await asyncio.sleep(.02)
            assert not task.done()
            assert runtime.messages == before_cancel
            release_worker.set()
            if cancel_during_snapshot:
                await _wait_for(snapshot_entered)
                task.cancel("repeat cancel")
                release_snapshot.set()
            with pytest.raises(asyncio.CancelledError) as error:
                await task
            if cancel_source == "task":
                assert error.value.args == ("diagnostic cancel",)
            assert exited.is_set()
            after_cancel = copy.deepcopy(runtime.messages)
            await asyncio.sleep(.02)
            assert runtime.messages == after_cancel
        finally:
            release_worker.set()
            release_snapshot.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    with scoped_tool_cancellation(tool_cancel):
        asyncio.run(scenario())

    assert runtime.target.read_text() == "value = 2\n"
    assert runtime.environment.txn.active is False
    assert runtime.environment.runtime_state.mutation_generation == 1
    assert collected_calls[0][2].evidence_metadata == {
        "_nz_evidence_kind": "file_write", "_nz_mutated_resources": ["created.py"],
        "_nz_mutation_generation": 1,
    }
    checkpoint = json.loads(runtime.checkpoint_path.read_text())
    assert checkpoint["status"] == "interrupted"
    saved_write = next(message for message in checkpoint["messages"] if message.get("role") == "tool")
    assert saved_write["_nz_evidence_kind"] == "file_write"
    assert saved_write["_nz_mutated_resources"] == ["created.py"]
    assert saved_write["_nz_mutation_generation"] == 1
    parts = checkpoint["messages"][0][PARTS_KEY]
    tool = next(part for part in parts if part["type"] == "tool")
    assert tool["state"]["status"] == "completed"
    assert "interrupted" not in tool["state"]
    patch = next(part for part in parts if part["type"] == "patch")
    assert patch["files"] == ["created.py"]
    assert "<lsp-diagnostics>" not in str(checkpoint["messages"])


def test_sync_diagnostics_uses_same_batch_and_owner_application(runtime, monkeypatch):
    """同步入口必须保持收集元组接口，并使用当前 RunSettings 创建观察预算。"""
    owner = threading.get_ident()
    seen = []

    def diagnostics(paths, workspace, *, operation):
        seen.append(threading.get_ident())
        assert .8 < operation.remaining() <= 1
        return _batch()

    monkeypatch.setattr(loop, "collect_write_diagnostics", diagnostics)
    runtime.environment.tool_runtime.execute_batch_sync(
        runtime.environment, runtime.calls, runtime.messages, processor=runtime.processor)
    assert seen == [owner]
    tool = next(message for message in runtime.messages if message.get("role") == "tool")
    assert tool["tool_call_id"] == "call-write"
    assert "<lsp-diagnostics>" in tool["content"]
    assert runtime.target.read_text() == "value = 2\n"


def test_async_diagnostics_collects_off_thread_and_updates_existing_part(runtime, monkeypatch):
    """事实和 Part 附加留在 owner；诊断不会丢失标题、附件或另记一次写入。"""
    owner = threading.get_ident()
    entered = threading.Event()
    release = threading.Event()
    owner_threads = []
    before_apply = []
    events = []
    environment = runtime.environment
    execute = environment._execute_tool_call_with_hooks
    apply = environment._apply_lsp_write_diagnostics
    emit = environment._emit_session_event
    attachment = {"type": "file", "mime": "image/png", "url": "data:image/png;base64,aGk="}

    def execute_with_metadata(*args):
        result = execute(*args)
        result.title = "Saved source"
        result.metadata = {**result.metadata, "existing": True}
        result.attachments = [attachment]
        return result

    def diagnostics(paths, workspace, *, operation):
        assert threading.get_ident() != owner
        entered.set()
        assert release.wait(2)
        return _batch()

    def apply_on_owner(observation, messages):
        owner_threads.append(threading.get_ident())
        assert observation[1] == "call-write"
        before_apply.append(copy.deepcopy(messages[0][PARTS_KEY]))
        apply(observation, messages)

    def emit_event(name, payload):
        events.append(name)
        emit(name, payload)

    for name in ("_refresh_patch_risk", "_refresh_code_index"):
        original = getattr(environment, name)

        def observe_on_owner(*args, original=original):
            owner_threads.append(threading.get_ident())
            return original(*args)

        monkeypatch.setattr(environment, name, observe_on_owner)
    monkeypatch.setattr(environment, "_execute_tool_call_with_hooks", execute_with_metadata)
    monkeypatch.setattr(loop, "collect_write_diagnostics", diagnostics)
    monkeypatch.setattr(environment, "_apply_lsp_write_diagnostics", apply_on_owner)
    monkeypatch.setattr(environment, "_emit_session_event", emit_event)

    async def scenario():
        task = asyncio.create_task(ProductionToolRuntime().execute_batch_async(
            environment, runtime.calls, runtime.messages, processor=runtime.processor,
            checkpoint=runtime.checkpoint))
        try:
            await _wait_for(entered)
            # 只有事件循环仍运行，才能放行阻塞中的诊断 collector。
            await asyncio.sleep(.02)
            release.set()
            assert await task == "continue"
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert owner_threads == [owner, owner, owner]
    assert events.count("session.tool.completed") == 1
    assert environment.runtime_state.mutation_generation == 1
    checkpoint = json.loads(runtime.checkpoint_path.read_text())
    part = next(part for part in checkpoint["messages"][0][PARTS_KEY] if part["type"] == "tool")
    original_part = next(part for part in before_apply[0] if part["type"] == "tool")
    assert part["id"] == original_part["id"]
    for key in ("status", "title", "time", "attachments"):
        assert part["state"][key] == original_part["state"][key]
    assert part["state"]["title"] == "Saved source"
    assert part["state"]["attachments"] == [attachment]
    assert "<lsp-diagnostics>" in part["state"]["output"]
    metadata = part["state"]["metadata"]
    assert metadata["existing"] is True
    assert metadata["lsp_diagnostics"] == {"skipped": 0, "reports": [{
        "path": "created.py", "status": "ready", "freshness": "current_push",
        "analysis_environment": "configured", "reason_code": "", "truncated": False,
    }]}


def test_internal_diagnostics_update_does_not_publish_empty_public_part(runtime, monkeypatch):
    """内部 Part 保留诊断元数据，但不向界面发出空的公开 Part 事件。"""
    message = runtime.messages[0]
    message["_nz_internal"] = True
    for part in message[PARTS_KEY]:
        part.update(internal=True, visible=False)
    runtime.processor.complete_tool("call-write", "Created source")
    runtime.messages.append({"role": "tool", "tool_call_id": "call-write", "content": "Created source"})
    events = []
    monkeypatch.setattr(runtime.environment, "_emit_session_event", lambda *args: events.append(args))

    runtime.environment._apply_lsp_write_diagnostics((_batch(), "call-write"), runtime.messages)

    part = next(part for part in message[PARTS_KEY] if part["type"] == "tool")
    assert "lsp_diagnostics" in part["state"]["metadata"]
    assert events == []
