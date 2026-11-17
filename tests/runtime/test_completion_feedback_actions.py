"""收尾计数只防连续无工具反馈，不能限制长任务的真实修复循环。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nz_coder.runtime.adapters.tool import (
    projection_context_from_legacy_host,
    tool_context_from_legacy_host,
)
from nz_coder.runtime.adapters.verification_runtime import scoped_verification_invocation
from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
from nz_coder.runtime.execution.runtime_state import RuntimeState
from nz_coder.runtime.tool_runtime.result_projection import ProductionToolResultProjector
from nz_coder.runtime.verification.hooks import AgentHooks, StopHookDecision
from nz_coder.tool_platform.execution import ToolExecutionResult


def _host():
    events = []
    host = SimpleNamespace(
        hooks=AgentHooks(stop_hooks=[lambda _ctx: StopHookDecision("reanimate", "Inspect the evidence.")]),
        runtime_state=RuntimeState(), current_agent_name="worker", active_run_context=None,
        tracer=SimpleNamespace(log=lambda event, **fields: events.append({"event": event, **fields})),
        _infer_hook_file_path=lambda args: str(args.get("path") or ""),
    )
    return host, events


def _feedback(host, messages):
    return asyncio.run(host.hooks.handle_delivery_boundary_async(
        host, messages, message="Candidate", review=False,
    ))


@pytest.mark.parametrize("focused", [False, True])
@pytest.mark.parametrize("name,changes,origin,strict,resets", [
    ("read_file", {}, "model", False, True),
    ("read_file", {"metadata": {"read_cache_hit": True}}, "model", False, True),
    ("bash", {"command_failed": True, "output": "Command exited with code 1"}, "model", False, True),
    ("todo", {}, "model", False, True),
    ("compact", {}, "model", False, True),
    ("bash", {"executed": False}, "model", False, False),
    ("bash", {"dispatch_failed": True}, "model", False, False),
    ("bash", {"permission_denied": True}, "model", False, False),
    ("bash", {"metadata": {"origin": "model"}}, "runtime", False, False),
    ("bash", {}, "model", True, False),
])
def test_result_projection_reopens_only_model_action_feedback(focused, name, changes, origin, strict, resets):
    host, events = _host()
    messages = []
    with scoped_runtime_overrides(strict_local_tools=strict):
        projection = (tool_context_from_legacy_host(host).projection if focused
                      else projection_context_from_legacy_host(host))
        assert _feedback(host, messages) == _feedback(host, messages) == "continue"
        result = replace(ToolExecutionResult(
            name, {"path": "example.py"}, "Observed output", True, False, False, False,
            execution_id="actual-model-tool",
        ), **changes)
        call = {"id": "actual-model-tool", "function": {"name": name, "arguments": {}}}
        with scoped_verification_invocation(origin, "scheduled" if origin == "runtime" else ""):
            ProductionToolResultProjector().consume(projection, [(0, call, result)], messages)
        assert _feedback(host, messages) == ("continue" if resets else "blocked")
    resets_seen = [e for e in events if e["event"] == "stop_hook_feedback_reset"]
    assert len(resets_seen) == int(resets)
    if resets:
        assert resets_seen[0]["execution_id"] == result.execution_id
        assert resets_seen[0]["previous_count"] == 2


@pytest.mark.parametrize("focused", [False, True])
def test_result_from_previous_run_cannot_reopen_current_run_feedback(focused):
    host, events = _host()
    host.active_run_context = SimpleNamespace(finalized=False)
    projection = (tool_context_from_legacy_host(host).projection if focused
                  else projection_context_from_legacy_host(host))
    host.active_run_context = SimpleNamespace(finalized=False)
    messages = []
    assert _feedback(host, messages) == _feedback(host, messages) == "continue"
    result = ToolExecutionResult("read_file", {}, "old result", True, False, False, False)
    projection.after_result(messages, result, result.output)
    assert _feedback(host, messages) == "blocked"
    assert not any(e["event"] == "stop_hook_feedback_reset" for e in events)


def test_diagnostic_proxy_does_not_count_as_model_tool_result():
    host, _events = _host()
    messages = []
    assert _feedback(host, messages) == _feedback(host, messages) == "continue"
    host.hooks.after_tool_result(host, messages, SimpleNamespace(name="bash"), "diagnostic")
    assert _feedback(host, messages) == "blocked"


def test_real_runner_can_act_between_more_than_two_completion_feedbacks(monkeypatch, tmp_path):
    from tests.runtime.test_native_product_runtime import _completion, _install_runtime, _request
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk

    class Provider:
        name = "offline"
        calls = 0

        def create_completion(self, _client, **_kwargs):
            self.calls += 1
            assert self.calls <= 7, "Unexpected model request"
            return _completion(tool="list_directory") if self.calls % 2 == 0 else _completion("Candidate")

    provider = Provider()
    _install_runtime(monkeypatch, provider)
    request = replace(_request(tmp_path), metadata={"max_turns": 10, "persist_session": False})
    options = RunOptions(permission_asker=lambda *_: True)
    environment = native_sdk.build_product_run_environment(request, options)
    candidates = []

    def review(context):
        candidates.append(context.last_assistant_text)
        return (StopHookDecision("reanimate", "Inspect the workspace before delivery.")
                if len(candidates) <= 3 else StopHookDecision())

    environment.hooks.register_stop_hook(review)
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
        assert result.status.value in {"completed", "completed_unverified"}
        assert provider.calls == 7
        assert len(candidates) == 4
        assert sum(m.get("role") == "tool" for m in result.messages) == 3
    finally:
        environment.close()
