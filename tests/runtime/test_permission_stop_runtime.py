"""真实执行链：拒绝立即结清，不执行同批后续操作，不再请求模型。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from nz_coder.runtime.tool_runtime.scheduler import _execute_scheduled, _execute_scheduled_async
from nz_coder.tool_platform.execution import ToolExecutionResult


@pytest.mark.parametrize("asynchronous", [False, True])
def test_scheduler_settles_remaining_ids_without_executing_after_rejection(asynchronous):
    executed = []
    calls = [{"id": str(i), "function": {"name": "write_file", "arguments": json.dumps({"path": f"{i}.py"})}} for i in range(3)]

    def execute(call, index):
        executed.append(index)
        return ToolExecutionResult("write_file", {}, "Permission approval rejected.", False, True, False, True,
            permission_denied=True, metadata={"permission_source": "approval_reply", "permission_reason": "rejected"})

    executor = SimpleNamespace(execute_one=execute)
    if asynchronous:
        results = asyncio.run(_execute_scheduled_async(executor, calls, lambda _: False))
    else:
        results = _execute_scheduled(executor, calls, lambda _: False)
    assert executed == [0]
    assert [call["id"] for _, call, _ in results] == ["0", "1", "2"]
    assert all(not result.executed for _, _, result in results)
    assert results[1][2].metadata["permission_source"] == "batch_cancelled"


def test_explicit_denial_stops_native_run_and_later_write(monkeypatch, tmp_path):
    from test_node_verification_runtime import _run, _tool

    for key in ("NZ_PLANNING_ENABLED", "NZ_REFLECTION_ENABLED", "NZ_MEMORY_AUTO_EXTRACT", "NZ_MEMORY_AUTO_DREAM"):
        monkeypatch.setenv(key, "0")
    result, _, _, requests, reviews, permissions = _run(monkeypatch, tmp_path,
        [[_tool("bash", command="python denied.py"), _tool("write_file", path="should_not_exist.py", content="bad=1\n")]],
        task="请执行这个本地操作", limit=5, permission=False)
    assert result.status.value == "blocked", (result.error, result.final_text)
    assert not (tmp_path / "should_not_exist.py").exists()
    assert len(requests) == 1 and not reviews
    assert len(permissions) == 1
    assert "approval" in result.final_text.lower() and "rejected" in result.final_text.lower()
    assert "completed" not in result.final_text.lower()


def test_synthetic_completion_denial_stops_without_extra_model_or_approval(monkeypatch, tmp_path):
    from test_node_verification_runtime import _run, _tool

    for key in ("NZ_PLANNING_ENABLED", "NZ_REFLECTION_ENABLED", "NZ_MEMORY_AUTO_EXTRACT", "NZ_MEMORY_AUTO_DREAM"):
        monkeypatch.setenv(key, "0")
    result, _, _, requests, reviews, permissions = _run(monkeypatch, tmp_path,
        [[_tool("write_files_batch", files=[{"path": "a.py", "content": "a=1\n"}, {"path": "b.py", "content": "b=1\n"}])], "All work completed."],
        task="创建并验证 a.py、b.py", limit=20, permission=False)
    assert result.status.value == "blocked", (result.error, result.final_text)
    assert len(requests) == 2 and not reviews
    assert len(permissions) == 1
    assert "rejected" in result.final_text.lower()
    assert "All work completed" not in result.final_text


@pytest.mark.parametrize('source', ['approval_reply', 'interaction_failure'])
def test_preflight_stop_prevents_earlier_approved_but_unstarted_call(monkeypatch, source):
    from nz_coder.runtime.tool_runtime.pipeline import ProductionToolRuntime
    from tests.runtime.tool_runtime.test_focused_policy import _context

    events = []
    calls = [{'id': str(i), 'function': {'name': 'write_file', 'arguments': json.dumps({'path': f'{i}.py', 'content': 'x'})}} for i in range(3)]

    async def before(call, _messages):
        events.append('admit:' + call['id'])
        if call['id'] == '1':
            return call, ToolExecutionResult('write_file', {}, 'stopped', False, True, False, True,
                permission_denied=True, metadata={'permission_source': source, 'permission_reason': 'unavailable' if source == 'interaction_failure' else 'rejected'})
        return call, None

    def execute(call, *_args):
        events.append('execute:' + call['id'])
        return ToolExecutionResult('write_file', {}, 'unexpected', True, False, False, True)

    async def after(_call, result, _messages):
        return result

    async def scenario():
        runtime = ProductionToolRuntime()
        focused = SimpleNamespace(assessment_scope=None, working_plan_scope=None, run=None, runtime_state_path=None, request_superseded=lambda: False, policy=_context(), lifecycle=SimpleNamespace(
            trace=lambda *_args, **_kwargs: None, before_tool=before, after_tool=after,
            has_pre_tool_hooks=lambda: False, execute_one=execute))
        results = await runtime.dispatch_async(focused, calls, has_write=True, messages=[])
        assert events == ['admit:0', 'admit:1']
        assert len(results) == 3 and all(not result.executed for _, _, result in results)
        assert results[1][2].metadata['permission_source'] == source
        from nz_coder.runtime.core.tool_context import ToolProjectionContext
        from nz_coder.runtime.session.session_processor import SessionProcessor
        from nz_coder.runtime.tool_runtime.result_projection import ProductionToolResultProjector
        from nz_coder.protocol.message_schema import attach_message_identity
        assistant = {'role': 'assistant', 'content': 'All work completed.'}
        attach_message_identity(assistant, session_id='permission-test')
        processor = SessionProcessor(assistant)
        processor.start_step()
        processor.register_tool_calls(calls)
        projection = ToolProjectionContext(signal_from_metadata=lambda _: None, record_result=lambda _: False,
            trace_result=lambda *_args, **_kwargs: None, after_result=lambda *_args: None)
        ProductionToolResultProjector().consume(projection, results, [], processor=processor)
        assert processor.process_result() == 'stop'
        assert ('rejected' if source == 'approval_reply' else 'not available') in processor.blocked_reason
    asyncio.run(scenario())


@pytest.mark.parametrize('reply', ['reject', 'unavailable'])
def test_repeated_tool_approval_stops_before_more_prompts_or_execution(reply):
    from nz_coder.runtime.tool_runtime.policy import ProductionToolPolicy
    from nz_coder.tool_platform.permissioning.interaction import PermissionInteractionError
    from tests.runtime.tool_runtime.test_focused_policy import _context

    events = []
    context = _context()
    def ask(*_args):
        events.append('ask')
        if reply == 'unavailable':
            raise PermissionInteractionError('unavailable')
        return False
    context.permissions.ask_special = ask
    blocked = {i: ToolExecutionResult('write_file', {}, 'repeat', False, True, False, True,
               permission_denied=True, metadata={'stall_kind': 'consecutive'}) for i in range(2)}
    calls = [{'id': str(i), 'function': {'name': 'write_file', 'arguments': '{}'}} for i in range(2)]
    result = asyncio.run(ProductionToolPolicy().resolve_doom_loop_permissions_async(context, blocked, calls))
    assert events == ['ask']
    assert result[0].approval_stopped


def test_repeated_tool_permission_cancellation_waits_for_selector_cleanup():
    from dataclasses import replace
    from nz_coder.interface.interactions import TerminalInteractionBridge
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.tool_runtime.policy import ProductionToolPolicy
    from tests.runtime.tool_runtime.test_focused_policy import _context

    async def scenario():
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        resumed = []
        class Terminal:
            async def select_async(self, **_kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release.wait()
        bridge = TerminalInteractionBridge(Terminal(), SimpleNamespace(pause=lambda: None, resume=lambda: resumed.append(True)), asyncio.get_running_loop())
        context = replace(_context(), permissions=PermissionManager('default', asker=bridge.ask_permission))
        result = ToolExecutionResult('write_file', {}, 'repeat', False, True, False, True,
            permission_denied=True, metadata={'stall_kind': 'consecutive'})
        task = asyncio.create_task(ProductionToolPolicy().resolve_doom_loop_permissions_async(
            context, {0: result}, [{'id': 'repeat', 'function': {'name': 'write_file'}}]))
        try:
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            await asyncio.wait_for(cleaning.wait(), 1)
            assert not task.done() and not resumed
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        assert resumed == [True]
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['reject', 'unavailable'])
def test_streamed_native_stop_does_not_publish_model_success_as_final(monkeypatch, tmp_path, failure):
    from dataclasses import replace
    from nz_coder.foundation import config
    from nz_coder.runtime.core import MAIN_PROFILE
    from nz_coder.runtime.core.request import AgentDefinition, RunOptions, RunRequest
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.tool_platform.permissioning.interaction import PermissionInteractionError
    from tests.runtime.test_native_product_runtime import _install_runtime

    class Provider:
        name = 'offline'
        calls = 0
        def create_completion(self, _client, **_kwargs):
            self.calls += 1
            assert self.calls == 1, 'approval stop requested another model call'
            return iter([SimpleNamespace(choices=[SimpleNamespace(finish_reason='tool_calls', delta=SimpleNamespace(
                content='All work completed.', reasoning_content=None,
                tool_calls=[SimpleNamespace(index=0, id='denied-write', type='function', provider_extra=None,
                    function=SimpleNamespace(name='write_file', arguments='{"path":"never.py","content":"x"}'))],
            ))])])
    provider = Provider()
    runtime = _install_runtime(monkeypatch, provider)
    runtime.capabilities = replace(runtime.capabilities, supports_streaming=True)
    for name in ('PLANNING_ENABLED',):
        monkeypatch.setattr(config, name, False)
    def ask(*_args):
        if failure == 'unavailable':
            raise PermissionInteractionError('unavailable')
        return 'reject'
    request = RunRequest(agent=AgentDefinition(name='approval-stream', instructions='Execute the requested action.'),
        profile=MAIN_PROFILE, workspace=tmp_path, session_id='approval-stream', stream=True,
        provider='offline', model='offline-model', messages=({'role': 'user', 'content': 'Perform the supplied local action.'},),
        metadata={'permission_mode': 'default', 'persist_session': False, 'max_turns': 8})
    options = RunOptions(permission_asker=ask)
    environment = native_sdk.build_product_run_environment(request, options)
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
        assert result.status.value == 'blocked', (result.error, result.final_text)
        assert ('rejected' if failure == 'reject' else 'not available') in result.final_text
        assert 'All work completed' not in result.final_text
        assert not (tmp_path / 'never.py').exists()
        assert provider.calls == 1
    finally:
        environment.close()


def test_static_rejection_does_not_request_repeated_tool_approval():
    from nz_coder.runtime.tool_runtime.pipeline import ProductionToolRuntime, ApprovedToolBatch
    from tests.runtime.tool_runtime.test_focused_policy import _context

    events = []
    context = _context()
    def ask(*_args):
        events.append('unexpected approval')
        return False
    context.permissions.ask_special = ask
    calls = [{'id': str(i), 'function': {'name': 'write_file', 'arguments': '{}'}} for i in range(2)]
    static = ToolExecutionResult('write_file', {}, 'policy denied', False, True, False, True,
        permission_denied=True, metadata={'guardrail': 'agent_allowlist'})
    repeated = ToolExecutionResult('write_file', {}, 'repeat', False, True, False, True,
        permission_denied=True, metadata={'stall_kind': 'consecutive'})
    def execute(call, *_args):
        events.append('execute:' + call['id'])
        return ToolExecutionResult('write_file', {}, 'ok', True, False, False, True)
    async def after(_call, result, _messages):
        return result
    async def scenario():
        runtime = ProductionToolRuntime()
        runtime.policy.find_repeated_tool_calls = lambda *_args: {0: repeated}
        focused = SimpleNamespace(assessment_scope=None, working_plan_scope=None, run=None, runtime_state_path=None, request_superseded=lambda: False, policy=context, lifecycle=SimpleNamespace(
            after_tool=after, has_pre_tool_hooks=lambda: False, execute_one=execute))
        results = await runtime.dispatch_async(focused, calls, has_write=True, messages=[],
                                              approved_batch=ApprovedToolBatch(calls, {0: static}))
        assert events == ['execute:1']
        assert results[0][2].metadata['guardrail'] == 'agent_allowlist'
    asyncio.run(scenario())
