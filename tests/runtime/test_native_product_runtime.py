"""Full Production capability contracts for the default Native runner."""
from __future__ import annotations

import inspect
import asyncio
import copy
import json
import threading
from contextvars import copy_context
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.runtime.core import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunOptions, RunRequest
from nz_coder.runtime.model_gateway import ResolvedModelRuntime


def _request(tmp_path):
    return RunRequest(
        agent=AgentDefinition(name="product", instructions="Use production services."),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "inspect"},),
        workspace=tmp_path,
        session_id="native-product",
        stream=False,
        provider="offline",
        model="offline-model",
        metadata={"permission_mode": "auto", "persist_session": False},
    )


def _runtime():
    return ResolvedModelRuntime(
        provider_id="offline", model_id="offline-model",
        request_model_id="offline-model", variant=None,
        provider=object(), client=object(), owns_client=False,
        capabilities=ModelCapabilities(
            provider="offline", model_id="offline-model", supports_streaming=False,
        ),
    )


def _completion(text="done", *, tool=None):
    calls = [] if tool is None else [SimpleNamespace(
        id="call-progress", type="function", function=SimpleNamespace(
            name=tool, arguments='{"path":".","depth":1}',
        ),
    )]
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content="" if calls else text, tool_calls=calls),
            finish_reason="tool_calls" if calls else "stop",
        )],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1, total_tokens=3),
    )


def _install_runtime(monkeypatch, provider):
    runtime = replace(_runtime(), provider=provider)
    used = False
    def resolve(_selection):
        nonlocal used
        # 每个产品环境独占 runtime；保留 provider、会话与磁盘，不复活已退休 owner。
        selected = replace(runtime) if used else runtime
        used = True
        return selected
    monkeypatch.setattr("nz_coder.runtime.execution.native_sdk.resolve_model_runtime", resolve)
    monkeypatch.setattr("nz_coder.runtime.execution.loop.resolve_model_runtime", resolve)
    return runtime


@pytest.mark.parametrize("persist_session", [False, True])
def test_native_running_tool_checkpoint_preserves_store_policy(
    monkeypatch, tmp_path, persist_session,
):
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state import sessions
    from nz_coder.tools import report_tool_metadata, scoped_tool_call

    class Provider:
        name = "offline"

        def __init__(self):
            self.calls = 0

        def create_completion(self, _client, **_kwargs):
            self.calls += 1
            return _completion(tool="list_directory") if self.calls == 1 else _completion()

    _install_runtime(monkeypatch, Provider())
    request = replace(_request(tmp_path), metadata={
        "permission_mode": "auto", "persist_session": persist_session,
    })
    options = RunOptions(permission_asker=lambda *_: True)
    environment = native_sdk.build_product_run_environment(request, options)
    entered, release = threading.Event(), threading.Event()
    original_execute = environment._execute_tool_call_with_hooks

    def execute(tool_call, index, *args):
        with scoped_tool_call("call-progress"):
            assert report_tool_metadata(title="working", metadata={"output": "RUNNING_MARKER"})
        entered.set()
        assert release.wait(5), "tool was not released"
        return original_execute(tool_call, index, *args)

    monkeypatch.setattr(environment, "_execute_tool_call_with_hooks", execute)

    async def scenario():
        task = asyncio.create_task(native_sdk.NativeSDKRunner(environment).run_result(request, options))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            with scoped_workdir(tmp_path):
                path = sessions.session_dir() / f"{request.session_id}.json"
            if persist_session:
                running = json.loads(path.read_text())
                assert running["run_status"] == "running"
                parts = running["messages"][-1]["_nz_parts"]
                assert any(p.get("state", {}).get("metadata", {}).get("output") == "RUNNING_MARKER" for p in parts)
            else:
                assert not path.exists()
        finally:
            release.set()
            result = await asyncio.wait_for(task, 5)
        assert result.status.value == "completed"
        context = environment.active_run_context
        restored = await environment.runtime_services.session_runtime.store.load(
            context.session.identity, tmp_path,
        )
        assert restored.status.value == "completed"
        assert [m["role"] for m in restored.transcript] == ["user", "assistant", "tool", "assistant"]
        if not persist_session:
            assert not path.exists()

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        environment.close()


@pytest.mark.parametrize("persist_session", [False, True])
def test_captured_retry_callback_cannot_write_after_unbind_or_retirement(
    monkeypatch, tmp_path, persist_session,
):
    from nz_coder.protocol.message_schema import attach_message_identity
    from nz_coder.runtime.core.result import RunStatus
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.runtime.session.session_processor import SessionProcessor
    from nz_coder.state import sessions

    _install_runtime(monkeypatch, object())
    request = replace(_request(tmp_path), metadata={
        "permission_mode": "auto", "persist_session": persist_session,
    })
    environment = native_sdk.build_product_run_environment(request, RunOptions())
    captured, release = threading.Event(), threading.Event()
    errors = []

    async def scenario():
        runtime = environment.runtime_services.session_runtime
        context = await runtime.open(request)
        assistant = {"role": "assistant", "content": ""}
        attach_message_identity(assistant, session_id=request.session_id)
        context.transcript.append(assistant)
        processor = SessionProcessor(assistant)
        original_add = processor.add_retry

        def paused_add(*args, **kwargs):
            captured.set()
            assert release.wait(5), "retry callback was not released"
            return original_add(*args, **kwargs)

        processor.add_retry = paused_add
        environment.active_run_context = context
        environment._active_session_processor = processor
        environment._active_processor_messages = context.transcript
        with scoped_workdir(tmp_path):
            sessions.save_session(
                [{"role": "user", "content": "PREVIOUS_DURABLE"}],
                session_id="previous", run_status="completed",
            )
            state = sessions.session_dir()
            saved_scope = copy_context()
        before_latest = (state / "latest.json").read_bytes()

        def callback():
            try:
                saved_scope.run(environment._model_gateway_observer, "model_call_retry", {
                    "purpose": "coding", "attempt": 1, "wait_seconds": 0,
                    "error": "offline retry",
                })
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=callback, name="captured-parent-reporter")
        worker.start()
        try:
            assert await asyncio.to_thread(captured.wait, 3)
            environment._active_session_processor = None
            environment._active_processor_messages = None
            environment.active_run_context = None
            if persist_session:
                closing = asyncio.create_task(asyncio.to_thread(environment.close))
                await asyncio.sleep(0)
                assert not closing.done()
                release.set()
                await closing
                await runtime.finalize(context, RunStatus.CANCELLED)
                before_latest = (state / "latest.json").read_bytes()
                before_session = (state / f"{request.session_id}.json").read_bytes()
        finally:
            release.set()
            await asyncio.to_thread(worker.join, 3)
        assert not worker.is_alive()
        assert not errors
        assert (state / "latest.json").read_bytes() == before_latest
        if persist_session:
            assert (state / f"{request.session_id}.json").read_bytes() == before_session
        else:
            assert not (state / f"{request.session_id}.json").exists()

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        environment.close()


@pytest.mark.parametrize("persist_session, should_write", [(False, False), (0, False), (True, True)])
def test_checkpoint_uses_current_request_truthiness(
    monkeypatch, tmp_path, persist_session, should_write,
):
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state import sessions

    _install_runtime(monkeypatch, object())
    durable_request = replace(_request(tmp_path), metadata={"persist_session": True})
    environment = native_sdk.build_product_run_environment(durable_request, RunOptions())

    async def scenario():
        request = replace(durable_request, metadata={"persist_session": persist_session})
        context = await environment.runtime_services.session_runtime.open(request)
        context.session.metadata["persist_session"] = not bool(persist_session)
        environment.active_run_context = context
        with scoped_workdir(tmp_path):
            environment._checkpoint_messages(context.transcript, "running")
            path = sessions.session_dir() / f"{request.session_id}.json"
        assert path.exists() is should_write
        if should_write:
            assert json.loads(path.read_text())["messages"][0]["content"] == "inspect"

    try:
        asyncio.run(scenario())
    finally:
        environment.close()


def test_native_ephemeral_partial_cancellation_does_not_create_session_json(monkeypatch, tmp_path):
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state import sessions

    partial, release, exited = threading.Event(), threading.Event(), threading.Event()

    class Provider:
        name = "offline"

        def create_completion(self, _client, **_kwargs):
            def response():
                try:
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                        content="partial", tool_calls=None, reasoning_content=None,
                    ))])
                    partial.set()
                    assert release.wait(5), "stream was not released"
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                        content="LATE_PROVIDER_TEXT", tool_calls=None, reasoning_content=None,
                    ))])
                finally:
                    exited.set()
            return response()

    runtime = _install_runtime(monkeypatch, Provider())
    runtime.capabilities = replace(runtime.capabilities, supports_streaming=True)
    request = replace(_request(tmp_path), stream=True)
    environment = native_sdk.build_product_run_environment(request, RunOptions())

    async def scenario():
        task = asyncio.create_task(native_sdk.NativeSDKRunner(environment).run_result(request))
        try:
            assert await asyncio.to_thread(partial.wait, 3)
            task.cancel()
            try:
                await asyncio.wait_for(task, 3)
            except asyncio.CancelledError:
                pass
        finally:
            release.set()
            assert await asyncio.to_thread(exited.wait, 3)
        context = environment.active_run_context
        restored = await environment.runtime_services.session_runtime.store.load(
            context.session.identity, tmp_path,
        )
        assert context.finalized
        assert restored.status.value == "cancelled"
        assert "LATE_PROVIDER_TEXT" not in json.dumps(restored.transcript)
        with scoped_workdir(tmp_path):
            assert list(sessions.session_dir().glob("*.json")) == []

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        environment.close()


def test_owned_native_close_settles_entered_writer_despite_repeated_cancel(monkeypatch, tmp_path):
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.execution.loop import ProductRunEnvironment
    from nz_coder.runtime.adapters.runner import _LifecycleService
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state import sessions

    entered, release = threading.Event(), threading.Event()
    close_started, close_finished = threading.Event(), threading.Event()
    settle_started = threading.Event()
    original_settle = _LifecycleService.settle_session_checkpoints
    cleanup_started = threading.Event()
    callback_finished = threading.Event()
    callback_captured, release_callback = threading.Event(), threading.Event()
    captured = {}
    original_build = native_sdk.build_product_run_environment
    original_close = ProductRunEnvironment.close
    original_write = sessions._publish_session_json
    request = replace(_request(tmp_path), metadata={
        "permission_mode": "auto", "persist_session": True,
    })

    def build(*args, **kwargs):
        environment = original_build(*args, **kwargs)
        captured["environment"] = environment
        original_cleanup = environment._close_environment_run_controls

        def cleanup():
            cleanup_started.set()
            return original_cleanup()

        environment._close_environment_run_controls = cleanup
        return environment

    def close(environment):
        close_started.set()
        try:
            return original_close(environment)
        finally:
            close_finished.set()

    async def settle(lifecycle):
        settle_started.set()
        release_callback.set()
        assert await asyncio.to_thread(entered.wait, 3), "late callback did not enter the writer"
        return await original_settle(lifecycle)

    def paused_write(prepared, path):
        payload = json.loads(prepared.read_text())
        if (threading.current_thread().name == "late-parent-reporter"
                and path.name == f"{request.session_id}.json"
                and payload.get("run_status") == "running"):
            assert payload["workspace"] == str(tmp_path)
            entered.set()
            assert release.wait(5), "writer was not released"
        return original_write(prepared, path)

    class Provider:
        name = "offline"

        def create_completion(self, _client, **_kwargs):
            environment = captured["environment"]
            observer = environment._model_gateway_observer_for_run()
            scope = copy_context()

            def late_callback():
                try:
                    callback_captured.set()
                    assert release_callback.wait(5), "callback was not released"
                    def report():
                        with scoped_workdir(tmp_path):
                            observer("model_call_retry", {
                                "purpose": "coding", "attempt": 1,
                                "wait_seconds": 0, "error": "offline retry",
                            })
                    scope.run(report)
                finally:
                    callback_finished.set()

            worker = threading.Thread(target=late_callback, name="late-parent-reporter")
            captured["worker"] = worker
            worker.start()
            assert callback_captured.wait(3)
            return _completion()

    _install_runtime(monkeypatch, Provider())
    monkeypatch.setattr(native_sdk, "build_product_run_environment", build)
    monkeypatch.setattr(ProductRunEnvironment, "close", close)
    monkeypatch.setattr(_LifecycleService, "settle_session_checkpoints", settle)
    monkeypatch.setattr(sessions, "_publish_session_json", paused_write)

    async def scenario():
        task = asyncio.create_task(native_sdk.NativeSDKRunner().run_result(request))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            assert settle_started.is_set()
            assert not close_started.is_set()
            task.cancel()
            assert not close_finished.is_set(), "close overtook an entered Session writer"
            await asyncio.sleep(0)
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=0.05)
            assert not cleanup_started.is_set(), "resource cleanup overtook an entered Session writer"
            assert not done, "repeated cancellation bypassed Run writer settlement"
        finally:
            release_callback.set()
            release.set()
            try:
                await asyncio.wait_for(task, 5)
            except asyncio.CancelledError:
                pass
            assert await asyncio.to_thread(callback_finished.wait, 3)
            assert await asyncio.to_thread(close_finished.wait, 3)
            await asyncio.to_thread(captured["worker"].join, 3)
        assert captured["environment"].active_run_context.finalized

    asyncio.run(scenario())


def test_native_environment_uses_real_production_service_graph(monkeypatch, tmp_path):
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.conversation.input_preflight import ProductionInputPreflight
    from nz_coder.runtime.execution.services import (
        ProductionCompletionVerifier,
        ProductionMemoryService,
        ProductionTurnModelRuntime,
    )
    from nz_coder.runtime.tool_runtime import ProductionToolRuntime
    from nz_coder.tool_platform.exposure import ToolExposureMiddleware

    monkeypatch.setattr(native_sdk, "resolve_model_runtime", lambda _request: _runtime())
    environment = native_sdk.build_product_run_environment(
        _request(tmp_path), RunOptions(permission_asker=lambda *_args: True),
    )
    try:
        services = environment.runtime_services
        assert isinstance(services.model, ProductionTurnModelRuntime)
        assert isinstance(services.tools, ProductionToolRuntime)
        assert isinstance(services.memory, ProductionMemoryService)
        assert isinstance(services.verifier, ProductionCompletionVerifier)
        assert isinstance(services.inputs, ProductionInputPreflight)
        assert any(isinstance(item, ToolExposureMiddleware) for item in services.middleware)
        assert environment.auto_mode_controller.enabled is False
        assert environment.auto_permission_asker is None
    finally:
        environment.close()


def test_semantic_configuration_failure_closes_built_environment(monkeypatch, tmp_path):
    from nz_coder.intelligence import semantic
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.execution.loop import ProductRunEnvironment

    monkeypatch.setattr(native_sdk, "resolve_model_runtime", lambda _request: _runtime())
    request = _request(tmp_path)
    request = type(request)(
        **{
            **request.__dict__,
            "metadata": {**request.metadata, "semantic_model": "fixture/missing"},
        }
    )
    closed = []

    class Provider:
        identity = "fixture/missing"

        def prepare(self):
            raise RuntimeError("semantic model unavailable")

    monkeypatch.setattr(semantic, "sentence_transformer_provider", lambda _model: Provider())
    original_close = ProductRunEnvironment.close

    def close(environment):
        closed.append(True)
        return original_close(environment)

    monkeypatch.setattr(ProductRunEnvironment, "close", close)
    try:
        native_sdk.build_product_run_environment(request, RunOptions())
    except RuntimeError as exc:
        assert "semantic model unavailable" in str(exc)
    else:
        raise AssertionError("semantic configuration failure must abort the run")
    assert closed == [True]


def test_native_sdk_contains_no_reduced_capability_stubs():
    from nz_coder.runtime.execution import native_sdk

    source = inspect.getsource(native_sdk)
    for name in ("_Memory", "_Verifier", "_Planning", "_Snapshots", "_Inputs"):
        assert f"class {name}" not in source


def test_run_options_accept_explicit_product_interaction_ports():
    marker = object()
    options = RunOptions(
        permission_asker=lambda *_args: True,
        question_asker=lambda *_args: "answer",
        workflow_approval_asker=lambda *_args: True,
        event_bus=marker,
    )
    assert options.event_bus is marker


def test_agent_client_forwards_product_interaction_ports(tmp_path):
    import asyncio
    from nz_coder.runtime.core.result import RunResult, RunStatus, TokenUsage
    from nz_coder.sdk import AgentClient

    captured = {}

    class Runner:
        async def run_result(self, request, options):
            captured["options"] = options
            return RunResult(
                status=RunStatus.COMPLETED,
                final_text="done",
                messages=request.messages,
                usage=TokenUsage(),
                session_id=request.session_id,
                active_agent=request.agent.name,
            )

    marker = object()

    def permission(*_args):
        return True

    def question(*_args):
        return "answer"

    def approval(*_args):
        return True
    asyncio.run(AgentClient(runner=Runner()).run(
        _request(tmp_path),
        permission_asker=permission,
        question_asker=question,
        workflow_approval_asker=approval,
        event_bus=marker,
    ))
    assert captured["options"].permission_asker is permission
    assert captured["options"].question_asker is question
    assert captured["options"].workflow_approval_asker is approval
    assert captured["options"].event_bus is marker


def test_agent_client_external_bus_returns_result_and_preserves_event_identity(monkeypatch, tmp_path):
    from nz_coder.protocol.session_events import SessionEventBus
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.sdk import AgentClient

    class Provider:
        name = "offline"

        def create_completion(self, _client, **_kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="OFFLINE_DONE", tool_calls=[]),
                    finish_reason="stop",
                )],
                usage=None,
            )

    runtime = replace(_runtime(), provider=Provider())
    monkeypatch.setattr(native_sdk, "resolve_model_runtime", lambda _: runtime)
    monkeypatch.setattr("nz_coder.runtime.execution.loop.resolve_model_runtime", lambda _: runtime)
    request = replace(
        _request(tmp_path),
        interaction_run_id="sdk-external-interaction",
        parent_interaction_run_id="parent-interaction",
        parent_agent_id="parent-agent",
    )
    bus = SessionEventBus(session_id=request.session_id)
    try:
        result = asyncio.run(AgentClient().run(
            request, event_bus=bus, permission_asker=lambda *_: True,
        ))
        assert result.status.value == "completed"
        assert result.final_text.startswith("OFFLINE_DONE\n\n")
        assert "未评估" in result.final_text
        assert result.messages[-1]["content"] == result.final_text
        assert json.loads(json.dumps(copy.deepcopy(result.metadata))) == result.metadata
        completed = [event for event in bus.recent(100) if event.type == "session.run.completed"]
        assert len(completed) == 1
        event = completed[0]
        assert event.session_id == request.session_id
        assert event.run_id == request.interaction_run_id
        assert event.agent_invocation_id
        assert event.parent_interaction_run_id == request.parent_interaction_run_id
        assert event.parent_agent_invocation_id == request.parent_agent_id
        # SDK 结束后，总线仍归调用方所有，必须能继续发布。
        assert bus.publish("caller.after_run", {}).sequence > event.sequence
    finally:
        bus.close()


@pytest.mark.parametrize("metadata,evidence", [
    ({}, True),
    ({"repo_retrieval_strategy": "guidance"}, False),
    ({"repo_intelligence_mode": "off"}, False),
    ({"repo_retrieval_strategy": "tool-only"}, False),
])
def test_product_default_retrieval_reaches_provider(monkeypatch, tmp_path, metadata, evidence):
    """An unset terminal strategy reaches the model through the production factory."""
    from nz_coder.runtime.execution import native_sdk

    (tmp_path / "billing").mkdir()
    (tmp_path / "billing/api.py").write_text("def charge(order):\n    return order\n")
    requests = []

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="The billing module defines charge.", tool_calls=[]),
                    finish_reason="stop",
                )],
                usage=None,
            )

    runtime = replace(_runtime(), provider=Provider())
    monkeypatch.setattr(native_sdk, "resolve_model_runtime", lambda _: runtime)
    monkeypatch.setattr("nz_coder.runtime.execution.loop.resolve_model_runtime", lambda _: runtime)
    request = replace(
        _request(tmp_path),
        messages=({"role": "user", "content": "Explain `charge`"},),
        metadata={**_request(tmp_path).metadata, **metadata},
    )
    options = RunOptions(permission_asker=lambda *_: True)
    environment = native_sdk.build_product_run_environment(request, options)
    try:
        assert environment.repo_intelligence.wait_ready(timeout=5).status == "ready"
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
        assert result.status.value == "completed"
        visible = "\n".join(str(m.get("content", "")) for m in requests[0]["messages"])
        assert ("High-confidence bounded" in visible) is evidence
        if evidence:
            assert "billing/api.py" in visible
            assert "Next read: read_file" in visible
        assert environment.repo_intelligence.semantic_available is False
    finally:
        environment.close()


def test_native_runner_carries_edit_recovery_into_next_model_request(monkeypatch, tmp_path):
    """A real AgentRunner turn receives anchors before choosing the next edit."""
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.core.result import RunStatus

    target = tmp_path / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_module.py").write_text(
        "from module import value\n\n"
        "def test_value_was_fixed():\n    assert value == 2\n",
        encoding="utf-8",
    )
    requests = []

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            call_count = len(requests)
            if call_count == 1:
                message = SimpleNamespace(
                    content="",
                    tool_calls=[SimpleNamespace(
                        id="edit-failure",
                        type="function",
                        function=SimpleNamespace(
                            name="edit_file",
                            arguments='{"path":"module.py","old_text":"value = 9","new_text":"value = 0"}',
                        ),
                    )],
                )
                finish = "tool_calls"
            elif call_count == 2:
                message = SimpleNamespace(
                    content="",
                    tool_calls=[SimpleNamespace(
                        id="edit-success",
                        type="function",
                        function=SimpleNamespace(
                            name="edit_file",
                            arguments='{"path":"module.py","old_text":"value = 1","new_text":"value = 2"}',
                        ),
                    )],
                )
                finish = "tool_calls"
            elif call_count == 3:
                message = SimpleNamespace(
                    content="",
                    tool_calls=[SimpleNamespace(
                        id="verify-fix",
                        type="function",
                        function=SimpleNamespace(
                            name="bash",
                            arguments='{"command":"python -m pytest -q tests/test_module.py"}',
                        ),
                    )],
                )
                finish = "tool_calls"
            else:
                message = SimpleNamespace(content="fixed", tool_calls=[])
                finish = "stop"
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message, finish_reason=finish)],
                usage=SimpleNamespace(prompt_tokens=4, completion_tokens=2, total_tokens=6),
            )

    runtime = replace(_runtime(), provider=Provider())
    monkeypatch.setattr(native_sdk, "resolve_model_runtime", lambda _: runtime)
    monkeypatch.setattr("nz_coder.runtime.execution.loop.resolve_model_runtime", lambda _: runtime)
    request = replace(
        _request(tmp_path),
        messages=({"role": "user", "content": "Set module.py value to 2"},),
    )
    options = RunOptions(permission_asker=lambda *_: True)
    environment = native_sdk.build_product_run_environment(request, options)
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
        # 原来 SDK 折叠了未完成的 static 验证；当前保留真实义务。
        assert result.status is RunStatus.COMPLETED_UNVERIFIED
        assert result.metadata["verification"]["verification_needed"] is True
        assert target.read_text(encoding="utf-8") == "value = 2\n"
        assert len(requests) >= 4
        second_request = "\n".join(
            str(message.get("content", "")) for message in requests[1]["messages"]
        )
        assert "<edit-recovery>" in second_request
        assert "candidate_lines:" in second_request
        assert "read_file: path=module.py" in second_request
        assert "1 passed" in "\n".join(
            str(message.get("content", "")) for message in requests[3]["messages"]
        )
    finally:
        environment.close()


@pytest.mark.parametrize('tail,after_static,expected,calls', [(True,False,'completed',2), (True,True,'completed',3), (False,False,'completed',2)])
def test_real_tool_verification_is_frozen_in_result_and_durable_session(monkeypatch, tmp_path, tail, after_static, expected, calls):
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.session.model import SessionIdentity
    target=tmp_path/'tests'
    target.mkdir()
    (target/'test_probe.py').write_text('import unittest\nclass Probe(unittest.TestCase):\n def test_ok(self): self.assertEqual(1+1,2)\n')
    (tmp_path/'app.py').write_text('VALUE = 1\n')
    command='python -m unittest discover -s tests -v'+(' 2>&1 | tail -8' if tail else '')
    class Provider:
        name='offline'
        calls=0
        def create_completion(self,_client,**kwargs):
            self.calls+=1
            result=_completion('Actual run finished')
            if self.calls==1 or (after_static and self.calls==2):
                result.choices[0].message=SimpleNamespace(content='',tool_calls=[SimpleNamespace(
                    id='call-real-test', type='function', function=SimpleNamespace(name='bash',arguments=json.dumps({'command':command if self.calls==1 else 'python -m py_compile app.py'})))])
                result.choices[0].finish_reason='tool_calls'
            return result
    provider=Provider()
    _install_runtime(monkeypatch,provider)
    request=replace(_request(tmp_path), metadata={'permission_mode':'auto','persist_session':True,'max_turns':20})
    options=RunOptions(permission_asker=lambda *_:True)
    environment=native_sdk.build_product_run_environment(request,options)
    try:
        result=asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request,options))
        assert result.status.value==expected
        assert provider.calls==calls
        verification=result.metadata['verification']
        assert verification['verification_needed'] is False
        if tail:
            assert verification['tests_passed'] is None
            assert verification['ignored_verifications']
        assert '运行时验证记录' in result.final_text
        assert result.messages[-1]['content']==result.final_text
        store=environment.runtime_services.session_runtime.store
        restored=asyncio.run(store.load(SessionIdentity(request.session_id),tmp_path))
        assert restored.status.value==expected
        assert restored.metadata['verification']==verification
        assert restored.transcript[-1]['content']==result.final_text
        assert provider.calls==calls
    finally:
        environment.close()


def test_interrupted_verification_obligation_survives_real_runtime_restart(monkeypatch,tmp_path):
    from nz_coder.runtime.execution import native_sdk
    class Provider:
        name='offline'
        calls=0
        def create_completion(self,_client,**kwargs):
            self.calls+=1
            result=_completion('finished')
            if self.calls==1:
                result.choices[0].message=SimpleNamespace(content='',tool_calls=[SimpleNamespace(
                    id='call-before-interrupt',type='function',function=SimpleNamespace(name='bash',
                        arguments=json.dumps({'command':'python -m unittest discover -s tests 2>&1 | tail -8'})))])
                result.choices[0].finish_reason='tool_calls'
            return result
    tests=tmp_path/'tests'
    tests.mkdir()
    (tests/'test_ok.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(False)\n')
    provider=Provider()
    _install_runtime(monkeypatch,provider)
    options=RunOptions(permission_asker=lambda *_:True)
    request=replace(_request(tmp_path),metadata={'permission_mode':'auto','persist_session':True,'max_turns':2})
    environment=native_sdk.build_product_run_environment(request,options)
    try:
        first=asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request,options))
        assert first.status.value=='max_turns'
        assert first.metadata['verification']['verification_needed'] is True
        original_failures = first.metadata['verification']['test_obligations'][0]['failure_execution_ids']
    finally:
        environment.close()
    resumed=replace(request,messages=(*first.messages,{'role':'user','content':'go on'}),
        metadata={**request.metadata,'max_turns':20})
    environment=native_sdk.build_product_run_environment(resumed,options)
    try:
        result=asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(resumed,options))
        assert result.status.value=='completed_unverified'
        assert result.metadata['verification']['verification_needed'] is True
        assert result.metadata['verification']['test_obligations'][0]['failure_execution_ids'] == original_failures
        assert environment.runtime_state.completion_gate_prompts==2
        assert provider.calls==5
    finally:
        environment.close()
