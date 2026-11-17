"""运行取消和真实 Provider owner 的离线回归。"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nz_coder.runtime.core.run_context import RunContext
from nz_coder.runtime.execution.loop import ProductRunEnvironment
from nz_coder.runtime.execution.services import ProductionTurnModelRuntime
from nz_coder.runtime.model_gateway import ModelCallStatus, ProductionModelGateway
from nz_coder.runtime.session.model import Session
from tests.runtime.model_gateway.test_buffered_gateway import _Provider, _runtime, _response, _call
from tests.runtime.model_gateway.test_focused_turn_runtime import _context
from tests.runtime.test_native_product_runtime import _request


def _run_context(tmp_path, external=None):
    request = _request(tmp_path)
    return RunContext(request, Session.create(request.session_id, request.messages, workspace=tmp_path),
                      request.agent.name, cancellation=external)


@pytest.mark.parametrize("close_fails", [False, True])
def test_runtime_retirement_rejects_new_provider_calls(close_fails):
    provider = _Provider([_response()])
    runtime = _runtime(provider)
    class Client:
        def close(self):
            if close_fails:
                raise RuntimeError("held client")
    runtime.client = Client()
    runtime.owns_client = True
    if close_fails:
        with pytest.raises(RuntimeError, match="held client"):
            runtime.close()
    else:
        runtime.close()
    outcome = ProductionModelGateway(runtime).complete_sync(_call())
    assert outcome.status is ModelCallStatus.ABORTED
    assert provider.calls == []


@pytest.mark.parametrize("stream", [False, True])
def test_gateway_captures_original_run_cancellation(tmp_path, stream):
    from nz_coder.runtime.core import run_context as module
    first = _run_context(tmp_path, threading.Event())
    second = _run_context(tmp_path)
    provider = _Provider([_response()])
    with module.scoped_run_context(first):
        gateway = ProductionModelGateway(_runtime(provider))
    first.cancellation.set()
    with module.scoped_run_context(second):
        outcome = (gateway.complete_stream_sync(replace(_call(), streaming=True)) if stream
                   else gateway.complete_sync(_call()))
    assert outcome.status is ModelCallStatus.CANCELLED
    assert provider.calls == []
    assert not second.cancel_scope.is_set()


def test_async_gateway_cancellation_does_not_set_external_event(tmp_path):
    external = threading.Event()
    entered, release = threading.Event(), threading.Event()
    provider = _Provider([lambda: (entered.set(), release.wait(3), _response())[-1]])
    async def exercise():
        task = asyncio.create_task(ProductionModelGateway(_runtime(provider)).complete(_call(), external))
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            assert not external.is_set()
        finally:
            release.set()
    asyncio.run(exercise())


def test_complete_turn_notifies_internal_scope_before_wait(tmp_path):
    external = threading.Event()
    run = _run_context(tmp_path, external)
    entered = threading.Event()
    context, _ = _context(streaming=False)
    def complete(*_args, **_kwargs):
        entered.set()
        assert run.cancel_scope.wait(1)
        return SimpleNamespace(content="late")
    context = replace(context, run_context=run, complete_override=complete)
    async def exercise():
        task = asyncio.create_task(ProductionTurnModelRuntime().complete_turn(
            context, [], stream=False, on_token=None, message_part=None, stream_tool_handler=None))
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert run.cancel_scope.is_set()
        assert not external.is_set()
    asyncio.run(exercise())


def test_planning_cancel_notifies_scope_before_settling(tmp_path):
    from nz_coder.runtime.core import run_context as module
    run = _run_context(tmp_path)
    entered = threading.Event()
    host = ProductRunEnvironment.__new__(ProductRunEnvironment)
    def complete(*_args):
        entered.set()
        assert run.cancel_scope.wait(1), "planning worker waited without cancellation"
        return "done"
    host._call_planning_llm = complete
    async def exercise():
        with module.scoped_run_context(run):
            task = asyncio.create_task(host._call_planning_llm_async("task"))
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert run.cancel_scope.is_set()
    asyncio.run(exercise())


def test_compaction_registers_worker_in_original_model_runtime(tmp_path):
    from nz_coder.state.context import _create_compaction_completion
    entered, release, cancel = threading.Event(), threading.Event(), threading.Event()
    provider = _Provider([lambda: (entered.set(), release.wait(3), _response())[-1]])
    owner = _runtime(provider)
    errors = []
    def invoke():
        try:
            _create_compaction_completion(owner.client, provider, owner.capabilities,
                {"model": owner.model_id, "messages": [], "max_tokens": 40},
                cancel_event=cancel, model_runtime=owner)
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=invoke)
    try:
        thread.start()
        assert entered.wait(1)
        cancel.set()
        thread.join(1)
        assert not thread.is_alive()
        assert owner.inflight_status()["worker_still_running"] == 1
        with pytest.raises(RuntimeError, match="incomplete"):
            owner.close()
    finally:
        release.set()
        thread.join(2)
        owner.cancel_inflight(grace_seconds=1)
        owner.close()


def test_manager_retirement_signals_all_children_before_diagnostic_io(tmp_path, monkeypatch):
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager, _LiveJob
    manager = BackgroundAgentManager(tmp_path, "retiring-parent")
    first, second, frozen = threading.Event(), threading.Event(), threading.Event()
    for key, event in (("first", first), ("second", second), ("frozen", frozen)):
        manager._jobs[key] = _LiveJob(event, threading.Event(), threading.Thread(), frozen=key == "frozen")
    monkeypatch.setattr(manager, "_save", lambda _state: pytest.fail("notification must not write diagnostics"))
    try:
        manager.request_retirement()
        manager.request_retirement()
        assert first.is_set() and second.is_set()
        assert not frozen.is_set()
        assert manager.start([{"prompt": "new", "target_paths": ["x"]}]) == "Error: background Agent manager is closed"
        # 通知不是 close 调用本身，后续 close 不会等待自己。
        assert manager._closing is False
    finally:
        for job in manager._jobs.values():
            job.done_event.set()
        manager.close()


def test_default_sdk_retains_incomplete_environment_and_retries_same_session(tmp_path, monkeypatch):
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.sdk import AgentClient
    from tests.runtime.test_native_product_runtime import _install_runtime, _completion
    entered, release = threading.Event(), threading.Event()
    provider = _Provider([lambda: (entered.set(), release.wait(5), _completion())[-1], _completion()])
    _install_runtime(monkeypatch, provider)
    environments = []
    original = native_sdk.build_product_run_environment
    def build(*args, **kwargs):
        env = original(*args, **kwargs)
        environments.append(env)
        return env
    monkeypatch.setattr(native_sdk, "build_product_run_environment", build)
    client = AgentClient()
    async def exercise():
        request = _request(tmp_path)
        task = asyncio.create_task(client.run(request))
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        result = await task
        assert result.status.value == "cancelled"
        assert not environments[0].environment_cleanup_complete
        with pytest.raises(RuntimeError, match="cleanup"):
            await client.run(request)
        assert len(environments) == 1
        release.set()
        await asyncio.to_thread(environments[0].model_runtime.cancel_inflight, grace_seconds=1)
        # 首个 runtime 已退休；第二轮用另一份真实 runtime。
        runtime = _install_runtime(monkeypatch, provider)
        await client.run(request)
        assert len(environments) == 2
        assert environments[0].environment_cleanup_complete
        assert runtime.inflight_status()["inflight_calls"] == 0
    try:
        asyncio.run(exercise())
    finally:
        release.set()
        for env in environments:
            env.model_runtime.cancel_inflight(grace_seconds=1)
            env.close()


def test_owned_sdk_notifies_children_before_first_run_control_retirement(tmp_path, monkeypatch):
    from nz_coder.runtime.execution import native_sdk
    from tests.runtime.test_native_product_runtime import _install_runtime, _completion
    provider = _Provider([_completion()])
    _install_runtime(monkeypatch, provider)
    original = native_sdk.build_product_run_environment
    notifications = []
    def build(*args, **kwargs):
        env = original(*args, **kwargs)
        notify = getattr(env, "request_retirement", lambda: None)
        def request():
            notifications.append("stop")
            notify()
        env.request_retirement = request
        retire = env.retire_run_control
        def retire_control(control):
            assert notifications, "host retired provider before notifying children"
            return retire(control)
        env.retire_run_control = retire_control
        return env
    monkeypatch.setattr(native_sdk, "build_product_run_environment", build)
    asyncio.run(native_sdk.NativeSDKRunner().run_result(_request(tmp_path)))


@pytest.mark.parametrize("purpose", ["coding", "compaction", "memory"])
def test_real_manager_thread_retains_environment_and_lease_until_provider_exit(tmp_path, monkeypatch, purpose):
    import time
    from nz_coder.foundation import config
    from nz_coder.foundation.capability_lease import capability_leases
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    from nz_coder.runtime.process.workdir import scoped_workdir
    from tests.runtime.test_native_product_runtime import _completion
    entered, release = threading.Event(), threading.Event()
    def held():
        entered.set()
        assert release.wait(10)
        return _completion()
    provider = _Provider([held])
    runtime = _runtime(provider)
    monkeypatch.setattr(config, "SUBAGENT_WORKTREE_ENABLED", False)
    monkeypatch.setattr(subagent, "create_provider", lambda **_kwargs: provider)
    monkeypatch.setattr(subagent, "resolve_model_runtime", lambda *_args, **_kwargs: runtime)
    monkeypatch.setenv("MEMORY_LLM_RERANK", "1" if purpose == "memory" else "0")
    if purpose == "memory":
        from nz_coder.state.memory import workspace_memory_manager
        from nz_coder.state.memory_scope import MemoryScope
        from tests.test_memory import create
        with scoped_workdir(tmp_path):
            memory = workspace_memory_manager(scope=MemoryScope.for_workspace(tmp_path))
            assert create(memory, name="inspect", description="inspect repository", content="Inspect repository reusable facts")["status"] == "saved"
    if purpose != "compaction":
        runtime.capabilities = replace(runtime.capabilities, context_tokens=128000)
    manager = BackgroundAgentManager(tmp_path, "real-thread-parent")
    try:
        with scoped_workdir(tmp_path), subagent.scoped_parent_context(session_id=manager.parent_session_id, model_id="offline-model"):
            started = manager.start([{"prompt": "inspect", "agent_type": "explore", "read_only": True, "target_paths": []}])
        assert hasattr(started, "metadata"), started
        task = started.metadata["task_ids"][0]
        job = manager._jobs[task]
        assert entered.wait(4), manager._load(task)
        stopped = manager.stop([task], reason="test stop", timeout_ms=20)
        assert task in stopped.metadata["unsettled_task_ids"]
        deadline = time.monotonic() + 4
        while not job.frozen and time.monotonic() < deadline:
            time.sleep(.01)
        assert job.frozen
        assert not job.done_event.is_set()
        assert manager._active_jobs == 1
        assert capability_leases().get(job.capability_lease_id) is not None
        assert job.environment.model_runtime is runtime
        assert runtime.inflight_status()["worker_still_running"] == 1
        original_candidate = manager._load(task)["status"]
        manager.request_retirement()
        release.set()
        assert job.done_event.wait(4)
        assert manager._active_jobs == 0
        assert capability_leases().get(job.capability_lease_id) is None
        assert job.environment.environment_cleanup_complete
        assert manager._load(task)["status"] in {"cancelled", "aborted"}, original_candidate
        assert len(provider.calls) == 1
        if purpose == "memory":
            assert "Rerank these" in str(provider.calls[0])
    finally:
        release.set()
        for job in manager._jobs.values():
            job.thread.join(4)
        manager.close(timeout=4)


def test_default_http_retains_session_owner_until_cleanup_retries(tmp_path, monkeypatch):
    from nz_coder.http_service.manager import SessionManager, SessionBusyError
    from tests.runtime.test_native_product_runtime import _install_runtime, _completion
    entered, release = threading.Event(), threading.Event()
    provider = _Provider([lambda: (entered.set(), release.wait(10), _completion())[-1]])
    runtime = _install_runtime(monkeypatch, provider)
    manager = SessionManager(workspace_roots=[tmp_path], restore_saved=False)
    try:
        session = manager.get(manager.create(permission_mode="auto", workspace_id=manager.workspaces.id_for(tmp_path))["id"])
        session.start_run("inspect", model="offline/offline-model")
        assert entered.wait(4)
        session.abort()
        assert session.wait(4)
        with pytest.raises(SessionBusyError, match="cleanup"):
            session.start_run("again", model="offline/offline-model")
        with pytest.raises(SessionBusyError, match="cleanup"):
            session.dispose()
        assert not session._disposed
        assert manager.get(session.session_id) is session
        release.set()
        runtime.cancel_inflight(grace_seconds=1)
        session.dispose()
        assert session._disposed
        assert len(provider.calls) == 1
    finally:
        release.set()
        manager.close()


def _sidecar_context():
    from nz_coder.runtime.verification.hooks import StopHookContext
    return StopHookContext(transcript=({"role": "user", "content": "Explain how to run it"},),
                           last_assistant_text="run it", runtime_state={})


@pytest.mark.parametrize("hold_supervisor", [False, True])
def test_borrowed_sidecar_close_confirms_provider_and_supervisor_exit(tmp_path, monkeypatch, hold_supervisor):
    import json
    import time
    from nz_coder.runtime.verification import sidecar_verifier as module
    entered, release = threading.Event(), threading.Event()
    report = {"verdict": "accept", "reason": "ok", "delivery_assessment": {"status": "not_applicable", "issues": []}}
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[
        SimpleNamespace(function=SimpleNamespace(name="emit_sidecar_verdict", arguments=json.dumps(report)))
    ]), finish_reason="tool_calls")])
    def completion():
        if not hold_supervisor:
            entered.set()
            release.wait(5)
        return response
    provider = _Provider([completion])
    owner = _runtime(provider)
    owner.capabilities = replace(owner.capabilities, context_tokens=128000)
    if hold_supervisor:
        parse = module._parse_tool_arguments
        def held_parse(value):
            entered.set()
            assert release.wait(5)
            return parse(value)
        monkeypatch.setattr(module, "_parse_tool_arguments", held_parse)
    host = SimpleNamespace(_model_gateway_observer_for_run=lambda: None, change_tracker=None,
        runtime_state=None, tracer=None, _sidecar_risky_shell_ops=0, _sidecar_unattributed_write_ops=0)
    hook = module.create_sidecar_verifier_hook(host,
        module.ResolvedVerifierProvider(provider, owner.client, owner.model_id, "fake", "inherit-main"),
        model_runtime=owner, env={"KODAX_VERIFIER_ALWAYS": "1"})
    async def exercise():
        task = asyncio.create_task(hook(_sidecar_context()))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(RuntimeError, match="incomplete"):
            await asyncio.to_thread(hook.close)
        if not hold_supervisor:
            assert owner.inflight_status()["worker_still_running"] == 1
        release.set()
        deadline = time.monotonic() + 2
        while True:
            try:
                await asyncio.to_thread(hook.close)
                break
            except RuntimeError:
                assert time.monotonic() < deadline
                await asyncio.sleep(.01)
        assert owner.inflight_status()["inflight_calls"] == 0
        assert hook.stats["verdict_counts"]["accept"] == 0
        # Hook 借用主 runtime，成功清理也不关闭主 client 或退休主准入。
        assert not owner._closed
    try:
        asyncio.run(exercise())
    finally:
        release.set()
        owner.cancel_inflight(grace_seconds=1)
        owner.close()


def test_http_start_busy_does_not_retire_active_run(tmp_path, monkeypatch):
    from nz_coder.http_service.manager import SessionManager, SessionBusyError
    from tests.runtime.test_native_product_runtime import _install_runtime, _completion
    entered, release = threading.Event(), threading.Event()
    provider = _Provider([lambda: (entered.set(), release.wait(5), _completion())[-1]])
    _install_runtime(monkeypatch, provider)
    manager = SessionManager(workspace_roots=[tmp_path], restore_saved=False)
    try:
        session = manager.get(manager.create(permission_mode="auto", workspace_id=manager.workspaces.id_for(tmp_path))["id"])
        session.start_run("inspect", model="offline/offline-model")
        assert entered.wait(3)
        runner = session.client._native_runner
        environment = next(iter(runner._pending_environments.values()))
        with pytest.raises(SessionBusyError):
            session.start_run("second", model="offline/offline-model")
        assert not environment.background_agents._retiring
        assert not environment.active_run_context.cancel_scope.is_set()
        release.set()
        assert session.wait(3)
        assert session.status in {"completed", "completed_unverified"}
    finally:
        release.set()
        manager.close()


def test_memory_rejects_mismatched_runtime_identity_before_provider_call():
    from nz_coder.state.memory import _create_chat_completion
    provider = _Provider([_response()])
    runtime = _runtime(provider)
    with pytest.raises(ValueError, match="identity"):
        _create_chat_completion(runtime.client, provider=provider, model_runtime=runtime,
                                model="different-memory-model", messages=[])
    assert provider.calls == []


@pytest.mark.parametrize("entry", ["buffered", "text", "stream_fallback", "gateway"])
def test_focused_model_uses_original_context_after_host_switch(tmp_path, monkeypatch, entry):
    from nz_coder.runtime.adapters.model import model_context_from_legacy_host
    from nz_coder.runtime.execution.native_sdk import build_product_run_environment
    from nz_coder.runtime.core.request import RunOptions
    from tests.runtime.test_native_product_runtime import _install_runtime, _completion
    external = threading.Event()
    original = _run_context(tmp_path, external)
    provider = _Provider([_completion()])
    _install_runtime(monkeypatch, provider)
    environment = build_product_run_environment(_request(tmp_path), RunOptions())
    context = model_context_from_legacy_host(environment, original)
    environment.active_run_context = _run_context(tmp_path)
    external.set()
    try:
        service = ProductionTurnModelRuntime()
        if entry == "buffered":
            result = service.complete_buffered(context, [])
        elif entry == "text":
            result = service.complete_text_result(context, "system", "prompt")
        elif entry == "gateway":
            result = context.gateway().complete_sync(_call())
        else:
            result = service.complete_turn_sync(context, [], stream=True, on_token=None,
                                                message_part=None, stream_tool_handler=None)
        assert provider.calls == []
        assert (result.status is ModelCallStatus.CANCELLED) if entry == "gateway" else result.aborted
    finally:
        environment.close()


def test_active_stream_combines_original_run_scope_and_part_retirement(tmp_path):
    from nz_coder.providers.normalized import chunk
    from nz_coder.runtime.core.run_context import scoped_run_context
    entered, release = threading.Event(), threading.Event()
    run = _run_context(tmp_path)
    part = threading.Event()
    def stream():
        entered.set()
        assert release.wait(3)
        yield chunk(content="late", finish_reason="stop")
    provider = _Provider([stream()])
    runtime = _runtime(provider)
    with scoped_run_context(run):
        gateway = ProductionModelGateway(runtime)
    outcomes = []
    worker = threading.Thread(target=lambda: outcomes.append(gateway.complete_stream_sync(
        replace(_call(), streaming=True), cancel_event=part)))
    try:
        worker.start()
        assert entered.wait(1)
        run.request_cancel()
        worker.join(1)
        assert not worker.is_alive()
        assert outcomes[0].status is ModelCallStatus.CANCELLED
        assert not part.is_set()
        assert runtime.inflight_status()["worker_still_running"] == 1
    finally:
        release.set()
        worker.join(2)
        runtime.cancel_inflight(grace_seconds=1)
        runtime.close()


def test_foreground_child_keeps_cleanup_owner_until_provider_exit(tmp_path, monkeypatch):
    import time
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.process.workdir import scoped_workdir
    from tests.runtime.test_native_product_runtime import _completion
    entered, release, cancel = threading.Event(), threading.Event(), threading.Event()
    provider = _Provider([lambda: (entered.set(), release.wait(6), _completion())[-1]])
    runtime = _runtime(provider)
    runtime.capabilities = replace(runtime.capabilities, context_tokens=128000)
    monkeypatch.setattr(config, "SUBAGENT_WORKTREE_ENABLED", False)
    monkeypatch.setattr(subagent, "create_provider", lambda **_kwargs: provider)
    monkeypatch.setattr(subagent, "resolve_model_runtime", lambda *_args, **_kwargs: runtime)
    results = []
    def run():
        with scoped_workdir(tmp_path), subagent.scoped_parent_context(session_id="foreground-parent", model_id="offline-model"):
            results.append(subagent.run_subagent("inspect", agent_type="explore", cancel_event=cancel))
    worker = threading.Thread(target=run)
    try:
        worker.start()
        assert entered.wait(3)
        cancel.set()
        deadline = time.monotonic() + 2
        while not runtime._retiring and time.monotonic() < deadline:
            time.sleep(.01)
        assert runtime._retiring
        assert worker.is_alive() and results == []
        release.set()
        worker.join(3)
        assert not worker.is_alive()
        assert runtime.inflight_status()["inflight_calls"] == 0
        assert len(results) == 1
    finally:
        release.set()
        worker.join(3)
        runtime.close()


@pytest.mark.parametrize("other_workspace", [False, True])
def test_sdk_other_key_runs_while_original_cleanup_is_slow(tmp_path, monkeypatch, other_workspace):
    from nz_coder.sdk import AgentClient
    from nz_coder.runtime.execution import native_sdk
    from tests.runtime.test_native_product_runtime import _runtime as native_runtime, _completion
    provider_entered, provider_release = threading.Event(), threading.Event()
    close_entered, close_release = threading.Event(), threading.Event()
    provider = _Provider([lambda: (provider_entered.set(), provider_release.wait(8), _completion())[-1], _completion()])
    environments = []
    def resolve(_request):
        return replace(native_runtime(), provider=provider)
    monkeypatch.setattr(native_sdk, "resolve_model_runtime", resolve)
    monkeypatch.setattr("nz_coder.runtime.execution.loop.resolve_model_runtime", resolve)
    build = native_sdk.build_product_run_environment
    def capture(*args, **kwargs):
        env = build(*args, **kwargs)
        environments.append(env)
        return env
    monkeypatch.setattr(native_sdk, "build_product_run_environment", capture)
    client = AgentClient()
    async def exercise():
        first = _request(tmp_path)
        task = asyncio.create_task(client.run(first))
        assert await asyncio.to_thread(provider_entered.wait, 3)
        task.cancel()
        await task
        assert not environments[0].environment_cleanup_complete
        close = environments[0].close
        def slow_close():
            close_entered.set()
            assert close_release.wait(4)
            close()
        environments[0].close = slow_close
        retry = asyncio.create_task(client.run(first))
        assert await asyncio.to_thread(close_entered.wait, 2)
        second_workspace = tmp_path / "other" if other_workspace else tmp_path
        second_workspace.mkdir(exist_ok=True)
        second = replace(first, workspace=second_workspace,
                         session_id=first.session_id if other_workspace else "other-session")
        try:
            result = await asyncio.wait_for(client.run(second), 2)
            assert result.status.value in {"completed", "completed_unverified"}
            assert len(provider.calls) == 2
            assert len(environments) == 2
        finally:
            close_release.set()
        with pytest.raises(RuntimeError, match="cleanup"):
            await retry
    try:
        asyncio.run(exercise())
    finally:
        provider_release.set()
        close_release.set()
        for env in environments:
            env.model_runtime.cancel_inflight(grace_seconds=1)
            env.close()


def test_default_sdk_sidecar_worker_remains_in_original_environment(tmp_path, monkeypatch):
    from nz_coder.sdk import AgentClient
    from tests.runtime.test_native_product_runtime import _install_runtime, _completion
    entered, release = threading.Event(), threading.Event()
    class Client:
        closes = 0
        def close(self):
            self.closes += 1
    class Provider:
        name = "offline"
        calls = 0
        def create_completion(self, _client, **kwargs):
            self.calls += 1
            choice = kwargs.get("tool_choice") or {}
            if isinstance(choice, dict) and choice.get("function", {}).get("name") == "emit_sidecar_verdict":
                entered.set()
                assert release.wait(8)
            return _completion("run it")
    provider = Provider()
    owner = _install_runtime(monkeypatch, provider)
    owner.owns_client = True
    owner.client = Client()
    monkeypatch.setenv("KODAX_VERIFIER_ALWAYS", "1")
    client = AgentClient()
    async def exercise():
        task = asyncio.create_task(client.run(_request(tmp_path)))
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        result = await task
        assert result.status.value == "cancelled"
        assert result.metadata["environment_cleanup_complete"] is False
        assert owner.inflight_status()["worker_still_running"] == 1
        assert owner.client.closes == 0
        runner = client._native_runner
        environment = next(iter(runner._pending_environments.values()))
        release.set()
        owner.cancel_inflight(grace_seconds=1)
        assert await asyncio.to_thread(runner.retry_cleanup, tmp_path, _request(tmp_path).session_id)
        assert environment.environment_cleanup_complete
        assert owner.client.closes == 1
        assert provider.calls == 2
    try:
        asyncio.run(exercise())
    finally:
        release.set()
        owner.cancel_inflight(grace_seconds=1)
        owner.close()


@pytest.mark.parametrize("host_kind", ["sdk", "http"])
def test_default_buffered_rerank_cancel_reaches_scope_before_worker_released(tmp_path, monkeypatch, host_kind):
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.sdk import AgentClient
    from nz_coder.http_service.manager import SessionManager
    from tests.runtime.test_native_product_runtime import _install_runtime, _completion
    entered, release, freeze_exited = threading.Event(), threading.Event(), threading.Event()
    external = threading.Event()
    environments, observed = [], []
    class Provider:
        name = "offline"
        calls = 0
        def create_completion(self, _client, **kwargs):
            self.calls += 1
            assert "Rerank these" in str(kwargs["messages"])
            entered.set()
            assert release.wait(3)
            return _completion('[]')
    provider = Provider()
    _install_runtime(monkeypatch, provider)
    monkeypatch.setenv("MEMORY_LLM_RERANK", "1")
    build = native_sdk.build_product_run_environment
    def capture(*args, **kwargs):
        env = build(*args, **kwargs)
        environments.append(env)
        from tests.test_memory import create
        assert create(env._mm, name="inspect", description="inspect the repository",
                      content="inspect reusable repository facts")["status"] == "saved"
        freeze = env._freeze_api_request
        def freeze_request(messages):
            try:
                return freeze(messages)
            finally:
                freeze_exited.set()
        env._freeze_api_request = freeze_request
        close = env.close
        def close_after_freeze():
            assert freeze_exited.is_set(), "cleanup overtook prompt worker writes"
            close()
        env.close = close_after_freeze
        return env
    monkeypatch.setattr(native_sdk, "build_product_run_environment", capture)
    if host_kind == "sdk":
        async def exercise():
            client = AgentClient()
            task = asyncio.create_task(client.run(_request(tmp_path), cancel_event=external))
            loop = asyncio.get_running_loop()
            def cancel():
                assert entered.wait(2)
                loop.call_soon_threadsafe(task.cancel)
                observed.append(environments[0].active_run_context.cancel_scope.wait(.6))
                release.set()
            controller = threading.Thread(target=cancel)
            controller.start()
            try:
                result = await task
                assert result.status.value == "cancelled"
                assert observed == [True]
                assert not external.is_set()
            finally:
                release.set()
                controller.join(2)
        asyncio.run(exercise())
    else:
        manager = SessionManager(workspace_roots=[tmp_path], restore_saved=False)
        try:
            session = manager.get(manager.create(permission_mode="auto", workspace_id=manager.workspaces.id_for(tmp_path))["id"])
            session.start_run("inspect", model="offline/offline-model")
            assert entered.wait(3)
            session.abort()
            assert environments[0].active_run_context.cancel_scope.wait(.6)
            release.set()
            assert session.wait(3)
            assert session.status == "cancelled"
        finally:
            release.set()
            manager.close()
    assert freeze_exited.is_set()
    assert provider.calls == 1


@pytest.mark.parametrize("action", ["start", "dispose"])
def test_http_cleanup_retry_allows_session_callback_outside_session_lock(tmp_path, monkeypatch, action):
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.http_service.manager import SessionManager
    from tests.runtime.test_native_product_runtime import _runtime as native_runtime, _completion
    entered, release = threading.Event(), threading.Event()
    provider = _Provider([lambda: (entered.set(), release.wait(5), _completion())[-1], _completion()])
    def resolve(_request):
        return replace(native_runtime(), provider=provider)
    monkeypatch.setattr(native_sdk, "resolve_model_runtime", resolve)
    monkeypatch.setattr("nz_coder.runtime.execution.loop.resolve_model_runtime", resolve)
    manager = SessionManager(workspace_roots=[tmp_path], restore_saved=False)
    try:
        session = manager.get(manager.create(permission_mode="auto", workspace_id=manager.workspaces.id_for(tmp_path))["id"])
        session.start_run("inspect", model="offline/offline-model")
        assert entered.wait(3)
        session.abort()
        assert session.wait(3)
        environment = next(iter(session.client._native_runner._pending_environments.values()))
        close = environment.close
        callbacks = []
        def close_with_callback():
            callback = threading.Thread(target=lambda: callbacks.append(session.info()), daemon=True)
            callback.start()
            callback.join(.5)
            assert not callback.is_alive(), "cleanup callback blocked on the Session lock"
            close()
        environment.close = close_with_callback
        release.set()
        environment.model_runtime.cancel_inflight(grace_seconds=1)
        if action == "dispose":
            session.dispose()
            assert session._disposed
        else:
            session.start_run("second", model="offline/offline-model")
            assert session.wait(3)
        assert callbacks
        assert environment.environment_cleanup_complete
    finally:
        release.set()
        manager.close()



def test_explicit_environment_run_end_keeps_background_admission(tmp_path, monkeypatch):
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.core.request import RunOptions
    from tests.runtime.test_native_product_runtime import _install_runtime, _completion
    _install_runtime(monkeypatch, _Provider([_completion()]))
    request = _request(tmp_path)
    environment = native_sdk.build_product_run_environment(request, RunOptions())
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request))
        assert result.status.value in {"completed", "completed_unverified"}
        assert not environment.background_agents._retiring
        assert environment.background_agents.start([]) == "Error: tasks must be a non-empty list"
    finally:
        environment.close()
