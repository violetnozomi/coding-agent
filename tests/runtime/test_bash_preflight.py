"""无效 shell 超时在权限审批和资源创建之前失败。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import Mock

import pytest

from nz_coder.permissions import PermissionManager
from nz_coder.runtime.core.run_settings import current_run_settings, scoped_run_settings
from nz_coder.runtime.execution.tool_executor import ToolExecutor
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.tools import bash


def _call(timeout):
    return {"id": "timeout-test", "type": "function", "function": {
        "name": "bash", "arguments": {"command": "python build.py", "timeout": timeout}}}


@pytest.mark.parametrize("timeout", [180, 0, -1, "1", True, 1.5])
def test_invalid_timeout_never_asks_or_spawns(timeout, monkeypatch):
    permissions = PermissionManager("default")
    check = Mock(side_effect=AssertionError("must not check permission"))
    monkeypatch.setattr(permissions, "check", check)
    spawn = Mock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(bash, "BashExecution", spawn)
    with scoped_run_settings(replace(current_run_settings(), bash_timeout=120)):
        result = ToolExecutor(permissions).execute_one(_call(timeout), 0)
    assert not result.executed and result.dispatch_failed and not result.permission_denied
    assert result.output.startswith("Error: timeout")
    check.assert_not_called()
    spawn.assert_not_called()


@pytest.mark.parametrize("maximum", [30, 240])
def test_handler_preflight_uses_run_limit_before_parent_channel(maximum, monkeypatch, tmp_path):
    client = Mock()
    monkeypatch.setattr(bash, "current_command_client", lambda: client)
    with scoped_workdir(tmp_path), scoped_run_settings(replace(current_run_settings(), bash_timeout=maximum)):
        assert str(bash.run_bash("echo ok", timeout=maximum+1)).startswith("Error: timeout")
        client.call.assert_not_called()
        create = Mock(side_effect=AssertionError("must not create BashExecution"))
        assert str(bash.execute_bash_local("echo ok", timeout=True, create_execution=create)).startswith("Error: timeout")
        create.assert_not_called()
        for timeout in (None, 1, maximum):
            bash.run_bash("echo ok", timeout=timeout)
        assert client.call.call_count == 3


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("rewrite", [None, 1, 180])
def test_auto_preflight_precedes_classifier_and_manual_approval(tmp_path, monkeypatch, asynchronous, rewrite):
    from tests.test_linux_local_interactions import _environment
    from nz_coder.runtime.agent.auto_mode import AutoModeContext, AutoModeController
    from nz_coder.runtime.agent.guardrails import ToolGuardrail
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec
    from nz_coder.runtime.adapters.tool import tool_context_from_legacy_host
    from nz_coder.runtime.tool_runtime.pipeline import ProductionToolRuntime
    from nz_coder.runtime.model_gateway import ModelCallOutcome

    observed = []
    async def classify(_):
        observed.append("classifier")
        return ModelCallOutcome.completed(content='{"decision":"block","reason":"review","reason_code":"review"}')
    async def ask(*_):
        observed.append("asker")
        return "reject"
    async def guard(call, _):
        return {"action": "rewrite", "payload": {**call, "function": {
            **call["function"], "arguments": {"command": "python build.py", "timeout": rewrite}}}}
    with _environment(tmp_path, monkeypatch) as host, scoped_run_settings(replace(current_run_settings(), bash_timeout=120)):
        host.agent_graph = AgentGraph([AgentSpec("worker", "worker", guardrails=(ToolGuardrail("rewrite", before_tool=guard),) if rewrite is not None else ())], start="worker")
        host.current_agent_name = "worker"
        host.auto_mode_controller = AutoModeController(True)
        context = AutoModeContext(PermissionManager("auto"), tmp_path, classify, ask, lambda *a, **kw: None)
        monkeypatch.setattr(host, "_auto_mode_context", lambda: context)
        runtime = ProductionToolRuntime()
        call = _call(1 if rewrite == 180 else 180)
        if asynchronous:
            batch = asyncio.run(runtime.approve_tool_calls_async(tool_context_from_legacy_host(host), [call], []))
        else:
            batch = runtime.approve_tool_calls_sync(host, [call], [])
        if rewrite == 1:
            if asynchronous:
                assert observed == ["classifier", "asker"]
                assert batch.blocked[0].permission_denied
            else:
                assert batch.calls[0]["function"]["arguments"]["timeout"] == 1
        else:
            assert observed == []
            result = batch.blocked[0]
            assert not result.executed and result.dispatch_failed and not result.permission_denied
            assert result.output.startswith("Error: timeout")


def test_valid_timeout_still_requires_permission(tmp_path, monkeypatch):
    permissions = PermissionManager("default")
    monkeypatch.setattr(permissions, "ask_user", lambda *_: False)
    with scoped_workdir(tmp_path):
        result = ToolExecutor(permissions).execute_one(_call(1), 0)
    assert result.permission_denied and not result.executed


def test_run_settings_reach_actual_tool_worker(tmp_path):
    from nz_coder.foundation.async_utils import to_thread_settled
    async def run():
        with scoped_workdir(tmp_path), scoped_run_settings(replace(current_run_settings(), bash_timeout=30)):
            result = await to_thread_settled(ToolExecutor(PermissionManager("auto")).execute_one, _call(31), 0)
        assert "between 1 and 30s" in result.output and not result.executed
    asyncio.run(run())
