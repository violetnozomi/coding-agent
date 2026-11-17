"""通过真实NativeSDK、工具注册与结算验证任务判断；Provider仅为离线传输替身。"""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace

import pytest

from nz_coder.foundation.workspace_trust import load_config_snapshot
from nz_coder.runtime.core.request import RunOptions
from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.agent.task_intent import current_task_assessment
from nz_coder.runtime.verification.sidecar_verifier import (
    VerifierGateMetrics, compose_gate_decision, _transcript_has_tool_use,
)
from test_native_product_runtime import _install_runtime, _request
from test_working_memory_reminders import _reply, _call
from test_task_intent_state import VALUE


def environment(monkeypatch, tmp_path, provider, *, stream=False, tool_names=(), persist=False, turns=500, supports_tools=True):
    runtime = _install_runtime(monkeypatch, provider)
    runtime.capabilities = replace(runtime.capabilities, supports_streaming=stream, supports_tools=supports_tools)
    snapshot = load_config_snapshot(tmp_path, environ={"RUNTIME_STATE_PERSIST": "1" if persist else "0", "NZ_PLANNING_ENABLED": "0", "NZ_WORKING_MEMORY_REMINDER_STEPS": "0", "MAX_AGENT_TURNS": str(turns)})
    request = replace(_request(tmp_path), stream=stream, tool_names=tool_names, session_id=tmp_path.name)
    options = RunOptions(config_snapshot=snapshot, permission_asker=lambda *_: True)
    env = native_sdk.build_product_run_environment(request, options, config_snapshot=snapshot)
    return env, request, options


@pytest.mark.parametrize("stream", [False, True])
def test_native_assessment_publishes_before_next_request_without_extra_classifier(monkeypatch, tmp_path, stream):
    (tmp_path / "evidence.txt").write_text("A read-only observation")
    requests = []

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            if len(requests) == 1:
                return _reply(kwargs, calls=[
                    _call(1, "update_task_assessment", {**VALUE, "expected_revision": 0}),
                    _call(2, "read_file", {"path": "evidence.txt"}),
                ])
            return _reply(kwargs, text="Read-only review complete.")

    env, request, options = environment(monkeypatch, tmp_path, Provider(), stream=stream)
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(env).run_result(request, options))
        assert len(requests) == 2
        assert "expected_revision=0" in str(requests[0]["messages"])
        assert "expected_revision=1" in str(requests[1]["messages"])
        assert VALUE["reason"] in str(requests[1]["messages"])
        assert current_task_assessment(env.runtime_state) == VALUE
        assert env.runtime_state.task_assessment_state["revision"] == 1
        assert env._sp.completed_steps == 2
        assert not env.runtime_state.has_diff and not env.runtime_state.plan_generated
        tool_rows = [m for m in result.messages if m.get("role") == "tool" and m.get("tool_call_id") == "memory-1"]
        assert len(tool_rows) == 1 and "persisted=false" in tool_rows[0]["content"]
    finally:
        env.close()


@pytest.mark.parametrize("supports_tools", [True, False])
def test_native_direct_answer_does_not_require_an_assessment_call(monkeypatch, tmp_path, supports_tools):
    requests = []

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            requests.append(kwargs)
            return _reply(kwargs, text="A coroutine suspends until its awaited event completes.")

    env, request, options = environment(monkeypatch, tmp_path, Provider(), supports_tools=supports_tools)
    try:
        asyncio.run(native_sdk.NativeSDKRunner(env).run_result(request, options))
        assert len(requests) == 1
        assert current_task_assessment(env.runtime_state) is None
        if not supports_tools:
            assert not requests[0].get("tools")
    finally:
        env.close()


def test_metadata_does_not_become_current_business_evidence():
    transcript = ({"role": "user", "content": "Explain the issue."},
                  {"role": "assistant", "content": "", "tool_calls": [_call(1, "update_task_assessment", {**VALUE, "expected_revision": 0})]})
    assert _transcript_has_tool_use(transcript) is False
    assert compose_gate_decision(transcript, VerifierGateMetrics(), env={}) == (True, "default-fire")


@pytest.mark.parametrize("real_read", [False, True])
def test_metadata_does_not_become_historical_work_evidence(real_read):
    calls = [_call(1, "update_task_assessment", {**VALUE, "expected_revision": 0})]
    if real_read:
        calls.append(_call(2, "read_file", {"path": "evidence.txt"}))
    transcript = ({"role": "user", "content": "Inspect."},
                  {"role": "assistant", "content": "", "tool_calls": calls},
                  {"role": "user", "content": "Do not call tools and do not modify files. Report the verification result already obtained in the previous turn."})
    decision = compose_gate_decision(transcript, VerifierGateMetrics(), env={})
    assert decision == ((False, "grounded-history-report") if real_read else (True, "default-fire"))


def test_native_assessment_persists_continuation_and_resets_on_real_instruction(monkeypatch, tmp_path):
    import json
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.sessions import session_runtime_state_path

    requests = []

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            if len(requests) == 1:
                return _reply(kwargs, calls=[_call(1, "update_task_assessment", {**VALUE, "expected_revision": 0})])
            return _reply(kwargs, text="Read-only explanation complete.")

    def activate(messages=None, *, turns=500):
        env, request, options = environment(monkeypatch, tmp_path, Provider(), persist=True, turns=turns)
        if messages is not None:
            request = replace(request, messages=tuple(messages))
        try:
            result = asyncio.run(native_sdk.NativeSDKRunner(env).run_result(request, options))
            return result, copy.deepcopy(env.runtime_state.task_assessment_state), request
        finally:
            env.close()

    first, published, request = activate(turns=1)
    assert first.status.value == "max_turns"
    assert published["value"] == VALUE
    with scoped_workdir(tmp_path):
        saved = json.loads(session_runtime_state_path(request.session_id).read_text())
    assert saved["task_assessment_state"]["value"] == VALUE
    previous_id = published["binding"]["instruction_message_id"]
    _, resumed, _ = activate(first.messages)
    assert resumed["value"] == VALUE, (published, resumed)
    assert "expected_revision=1" in str(requests[-1]["messages"])
    _, changed, _ = activate([{"role": "user", "content": "Explain a separate question."}])
    assert changed["value"] is None
    assert "expected_revision=0" in str(requests[-1]["messages"])
    assert changed["binding"]["instruction_message_id"] != previous_id


def test_explicit_tool_allowlist_cannot_be_bypassed_by_assessment(monkeypatch, tmp_path):
    requests = []

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            if len(requests) == 1:
                return _reply(kwargs, calls=[_call(1, "update_task_assessment", {**VALUE, "expected_revision": 0})])
            return _reply(kwargs, text="Read-only explanation complete.")

    env, request, options = environment(monkeypatch, tmp_path, Provider(), tool_names=("read_file",))
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(env).run_result(request, options))
        assert current_task_assessment(env.runtime_state) is None
        assert all(tool["function"]["name"] != "update_task_assessment" for tool in requests[0]["tools"])
        receipts = [row for row in result.messages if row.get("role") == "tool"]
        assert any("undeclared tool" in str(row.get("content", "")).lower() for row in receipts)
    finally:
        env.close()


def test_system_only_caller_can_answer_with_unknown_assessment(monkeypatch, tmp_path):
    requests = []

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            requests.append(kwargs)
            return _reply(kwargs, text="Ready.")

    env, request, options = environment(monkeypatch, tmp_path, Provider(), supports_tools=False)
    request = replace(request, messages=({"role": "system", "content": "Reply Ready."},))
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(env).run_result(request, options))
        assert result.status.value == "completed"
        assert len(requests) == 1
        assert current_task_assessment(env.runtime_state) is None
        assert env.runtime_state.task_assessment_state["revision"] == 0
    finally:
        env.close()
