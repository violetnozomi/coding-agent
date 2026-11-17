"""真实 Session 提交后唯一发布终态，覆盖 Native、legacy 和异常边界。"""
from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace

import pytest

from nz_coder.protocol.message_schema import attach_message_identity
from nz_coder.protocol.session_events import SessionEventBus
from nz_coder.runtime.core.request import RunOptions
from nz_coder.runtime.core.result import RunStatus
from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.execution.runner import AgentRunner
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state import sessions
from tests.runtime.test_native_product_runtime import _completion, _install_runtime, _request
from tests.runtime.test_run_writer_settlement import _payload


_TERMINAL_EVENTS = {"session.run.completed", "session.run.cancelled", "session.run.failed"}


class _OfflineProvider:
    name = "offline"

    def __init__(self, *, tool=False):
        self.tool = tool
        self.calls = 0

    def create_completion(self, _client, **_kwargs):
        self.calls += 1
        return _completion("FINAL_ANSWER", tool="list_directory" if self.tool and self.calls == 1 else None)


def _durable_request(tmp_path):
    return replace(_request(tmp_path), interaction_run_id="publication-run", parent_interaction_run_id="parent-run",
        parent_agent_id="parent-agent", metadata={"permission_mode": "auto", "persist_session": True})


def _observe_terminal(monkeypatch, environment, tmp_path):
    observed = []
    original = environment.event_bus._publish_locked

    def publish(kind, properties, **identity):
        if kind in _TERMINAL_EVENTS:
            # 读实际文件而非可变内存，保证订阅者收到终态时确实可以恢复会话。
            with scoped_workdir(tmp_path):
                durable = copy.deepcopy(sessions.load_session(environment.session_id))
            event = original(kind, properties, **identity)
            observed.append((event, durable))
            return event
        return original(kind, properties, **identity)

    monkeypatch.setattr(environment.event_bus, "_publish_locked", publish)
    return observed


def _assert_committed_event(observed, expected_status, expected_event, session_id, run_id):
    assert len(observed) == 1, "one run must publish one terminal event"
    event, durable = observed[0]
    assert event.type == expected_event
    assert event.session_id == session_id
    assert event.run_id == run_id
    assert event.agent_invocation_id
    assert durable.get("run_status") == expected_status, "terminal event preceded the durable Session commit"
    assert event.properties["message_count"] == len(durable["messages"])
    return event, durable


@pytest.mark.parametrize("external_bus", [False, True], ids=["owned-bus", "external-bus"])
def test_native_terminal_is_unique_and_observes_committed_session(monkeypatch, tmp_path, external_bus):
    """不能先完成再落盘；成熟消息/工具事件和 core 回调仍须送达。"""
    _install_runtime(monkeypatch, _OfflineProvider(tool=True))
    request = _durable_request(tmp_path)
    bus = SessionEventBus(session_id=request.session_id) if external_bus else None
    callbacks = []
    options = RunOptions(event_bus=bus, on_event=callbacks.append)
    environment = native_sdk.build_product_run_environment(request, options)
    observed = _observe_terminal(monkeypatch, environment, tmp_path)
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
        assert result.status is RunStatus.COMPLETED
        event, durable = _assert_committed_event(observed, "completed", "session.run.completed", request.session_id, request.interaction_run_id)
        assert event.parent_interaction_run_id == "parent-run"
        assert event.parent_agent_invocation_id == "parent-agent"
        assert event.properties["status"] == "completed"
        assert durable["messages"][-1]["content"] == result.final_text
        assert durable["metadata"]["verification"] == result.metadata["verification"]
        assert _payload(tmp_path, request) == durable
        kinds = {item.type for item in environment.event_bus.recent(1000)}
        assert {"session.run.started", "session.tool.started", "session.tool.completed", "message.part.updated"} <= kinds
        if not external_bus:
            assert any(item.name.value == "session.run.completed" for item in callbacks)
        if bus is not None:
            assert bus.publish("caller.after_run", {}).sequence > event.sequence
    finally:
        environment.close()
        if bus is not None:
            bus.close()


def test_legacy_terminal_uses_explicit_publisher_after_host_scope_exit(monkeypatch, tmp_path):
    """真实 legacy 入口的最终保存发生在 ProductionRuntimeHost 作用域退出后。"""
    _install_runtime(monkeypatch, _OfflineProvider())
    request = _durable_request(tmp_path)
    environment = native_sdk.build_product_run_environment(request, RunOptions())
    environment._requested_interaction_run_id = "legacy-publication-run"
    observed = _observe_terminal(monkeypatch, environment, tmp_path)

    async def legacy_body(messages, *_args):
        assistant = {"role": "assistant", "content": "LEGACY_ANSWER"}
        attach_message_identity(assistant, session_id=request.session_id)
        messages.append(assistant)
        return {"status": "completed", "content": "LEGACY_ANSWER"}

    environment._run = legacy_body
    messages = list(request.messages)
    try:
        result = asyncio.run(AgentRunner().run(environment, messages, stream=False))
        assert result["status"] == "completed"
        event, durable = _assert_committed_event(observed, "completed", "session.run.completed", request.session_id, "legacy-publication-run")
        assert event.properties["status"] == "completed"
        assert durable["messages"][-1]["content"] == "LEGACY_ANSWER"
        assert messages == durable["messages"]
    finally:
        environment.close()


@pytest.mark.parametrize("cancel", [False, True], ids=["host-error", "host-cancel"])
def test_legacy_host_exception_publishes_only_after_durable_finalization(monkeypatch, tmp_path, cancel):
    _install_runtime(monkeypatch, _OfflineProvider())
    request = _durable_request(tmp_path)
    environment = native_sdk.build_product_run_environment(request, RunOptions())
    environment._requested_interaction_run_id = "legacy-exception-run"
    observed = _observe_terminal(monkeypatch, environment, tmp_path)
    entered = asyncio.Event()

    async def legacy_body(_messages, *_args):
        if cancel:
            entered.set()
            await asyncio.Event().wait()
        raise ValueError("primary execution failure")

    environment._run = legacy_body

    async def scenario():
        task = asyncio.create_task(AgentRunner().run(environment, list(request.messages), stream=False))
        if cancel:
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else ValueError):
            await asyncio.wait_for(task, 3)

    try:
        asyncio.run(scenario())
        expected = "cancelled" if cancel else "error"
        event, _durable = _assert_committed_event(observed, expected, "session.run.cancelled" if cancel else "session.run.failed", request.session_id, "legacy-exception-run")
        assert event.properties["status"] == expected
        if not cancel:
            assert event.properties["error"]["message"]
            assert event.properties["error_type"]
    finally:
        environment.close()


@pytest.mark.parametrize("raw_status,durable_status,event_type", [
    ("cancelled", "cancelled", "session.run.cancelled"),
    ("interrupted", "interrupted", "session.run.cancelled"),
    ("blocked", "blocked", "session.run.completed"),
    ("error", "error", "session.run.completed"),
    ("aborted", "error", "session.run.completed"),
    ("max_turns", "max_turns", "session.run.completed"),
    ("completed_unverified", "completed_unverified", "session.run.completed"),
])
def test_frozen_terminal_status_publishes_after_common_finalize(monkeypatch, tmp_path, raw_status, durable_status, event_type):
    """终态选择固定，实际生命周期、冻结、存储和共同发布边界保持真实。"""
    _install_runtime(monkeypatch, _OfflineProvider())
    request = _durable_request(tmp_path)
    environment = native_sdk.build_product_run_environment(request, RunOptions())
    observed = _observe_terminal(monkeypatch, environment, tmp_path)

    async def scenario():
        services = environment.runtime_services
        context = await services.session_runtime.open(request)
        execution = environment._native_execution_context(context, services)
        with scoped_workdir(tmp_path):
            execution.lifecycle.initialize(context.transcript, False)
            message = {"role": "assistant", "content": "BOUNDARY_TEXT", "_nz_finish": "stop"}
            attach_message_identity(message, session_id=request.session_id)
            context.transcript.append(message)
            raw = await execution.lifecycle.finalize(context.transcript, raw_status, stream=False)
            await AgentRunner._finalize_session(services, context, RunStatus(durable_status), execution.lifecycle)
        assert raw["status"] == raw_status
        assert context.finalized
        assert context.terminal_status.value == durable_status
        return raw

    try:
        raw = asyncio.run(scenario())
        event, durable = _assert_committed_event(observed, durable_status, event_type, request.session_id, request.interaction_run_id)
        assert event.properties["status"] == raw_status
        assert durable["messages"][-1]["content"] == raw["content"]
    finally:
        environment.close()


@pytest.mark.parametrize("recover", [False, True], ids=["persistent-write-failure", "catch-save-recovers"])
def test_terminal_write_failure_never_publishes_uncommitted_success(monkeypatch, tmp_path, recover):
    """真实 I/O 失败不算保存成功；异常结算恢复后才可发布冻结的 completed。"""
    _install_runtime(monkeypatch, _OfflineProvider())
    request = _durable_request(tmp_path)
    bus = SessionEventBus(session_id=request.session_id)
    options = RunOptions(event_bus=bus)
    environment = native_sdk.build_product_run_environment(request, options)
    observed = _observe_terminal(monkeypatch, environment, tmp_path)
    original = sessions._publish_session_json
    terminal_attempts = []

    def write(prepared, path):
        payload = json.loads(prepared.read_text())
        if path.name == f"{request.session_id}.json" and payload.get("run_status") == "completed":
            terminal_attempts.append(copy.deepcopy(payload))
            if not recover or len(terminal_attempts) == 1:
                raise OSError("terminal storage unavailable")
        return original(prepared, path)

    monkeypatch.setattr(sessions, "_publish_session_json", write)
    try:
        with pytest.raises(OSError, match="terminal storage unavailable"):
            asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
        owner = environment._session_checkpoint_writer.owner
        assert environment._session_checkpoint_writer.retired
        assert owner.terminal_status is RunStatus.COMPLETED
        assert owner.usage.input_tokens == 2 and owner.usage.output_tokens == 1
        assert len(terminal_attempts) == 2
        if recover:
            event, durable = _assert_committed_event(observed, "completed", "session.run.completed", request.session_id, request.interaction_run_id)
            assert event.properties["status"] == "completed"
            assert owner.finalized
            assert durable["messages"][-1]["content"].startswith("FINAL_ANSWER")
            assert owner.session.usage == owner.usage
        else:
            assert observed == []
            assert not owner.finalized
            assert _payload(tmp_path, request)["run_status"] == "running"
            assert owner.session.usage.total_tokens == 0
    finally:
        environment.close()
        bus.close()
