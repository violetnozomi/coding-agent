"""用真实请求/工具流水线证明阶段路线保存和下一轮投影。"""
from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace

import pytest

from nz_coder.runtime.execution import native_sdk
from test_native_task_intent import environment
from test_working_memory_reminders import _reply, _call
from test_working_plan_state import PLAN, TEXT


@pytest.mark.parametrize("stream", [False, True])
def test_route_published_before_next_request_without_extra_model_call(monkeypatch, tmp_path, stream):
    requests = []
    class Provider:
        name = "offline"
        def create_completion(self, _client, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            if len(requests) == 1:
                return _reply(kwargs, calls=[_call(1, "update_working_plan", {"plan": PLAN, "expected_revision": 0})])
            return _reply(kwargs, text="Read-only investigation complete.")
    env, request, options = environment(monkeypatch, tmp_path, Provider(), stream=stream)
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(env).run_result(request, options))
        assert len(requests) == 2
        assert env.runtime_state.plan_text == TEXT and env.runtime_state.plan_revision == 1
        assert not env.runtime_state.plan_generated and not env.runtime_state.has_diff
        assert "<working-plan-version>" in str(requests[0]["messages"])
        assert TEXT in str(requests[1]["messages"]).replace("\\n", "\n")
        rows = [m for m in result.messages if m.get("role") == "tool"]
        assert any("Working plan accepted" in m["content"] and "persisted=false" in m["content"] for m in rows)
        assert env._sp.completed_steps == 2
    finally:
        env.close()


@pytest.mark.parametrize("stream", [False, True])
def test_wire_unknown_field_is_rejected_without_installing_route(monkeypatch, tmp_path, stream):
    requests = []
    class Provider:
        name = "offline"
        def create_completion(self, _client, **kwargs):
            requests.append(kwargs)
            if len(requests) == 1:
                return _reply(kwargs, calls=[_call(1, "update_working_plan", {
                    "plan": PLAN, "expected_revision": 0, "permission": "allow"})])
            return _reply(kwargs, text="Read-only investigation complete.")
    env, request, options = environment(monkeypatch, tmp_path, Provider(), stream=stream)
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(env).run_result(request, options))
        assert not env.runtime_state.plan_text and env.runtime_state.plan_revision == 0
        rows = [m for m in result.messages if m.get("role") == "tool"]
        assert any("working plan rejected" in m["content"] for m in rows)
    finally:
        env.close()


def test_explicit_tool_allowlist_cannot_be_bypassed_by_plan_proposal(monkeypatch, tmp_path):
    requests = []
    class Provider:
        name = "offline"
        def create_completion(self, _client, **kwargs):
            requests.append(kwargs)
            if len(requests) == 1:
                return _reply(kwargs, calls=[_call(1, "update_working_plan", {"plan": PLAN, "expected_revision": 0})])
            return _reply(kwargs, text="Read-only investigation complete.")
    env, request, options = environment(monkeypatch, tmp_path, Provider(), tool_names=("read_file",))
    try:
        asyncio.run(native_sdk.NativeSDKRunner(env).run_result(request, options))
        assert not env.runtime_state.plan_text
        assert all(t["function"]["name"] != "update_working_plan" for t in requests[0]["tools"])
    finally:
        env.close()


@pytest.mark.parametrize("stream", [False, True])
def test_long_legitimate_investigation_keeps_one_initial_planner_only(monkeypatch, tmp_path, stream):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.runtime.core.request import RunOptions
    from test_native_product_runtime import _install_runtime, _request

    for index in range(6):
        (tmp_path / f"evidence-{index}.txt").write_text(f"different useful observation {index}")
    requests = []
    main_calls = []
    class Provider:
        name = "offline"
        def create_completion(self, _client, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            system = kwargs["messages"][0]["content"]
            if "concise coding task planner" in system or "re-planner" in system:
                return _reply(kwargs, text=json.dumps({"plan": PLAN}))
            index = len(main_calls)
            main_calls.append(True)
            if index < 6:
                return _reply(kwargs, calls=[_call(index + 1, "read_file", {"path": f"evidence-{index}.txt"})])
            return _reply(kwargs, text="Read-only investigation complete.")
    runtime = _install_runtime(monkeypatch, Provider())
    runtime.capabilities = replace(runtime.capabilities, supports_streaming=stream)
    snapshot = load_config_snapshot(tmp_path, environ={"NZ_PLANNING_ENABLED": "1",
        "NZ_WORKING_MEMORY_REMINDER_STEPS": "0", "RUNTIME_STATE_PERSIST": "0"})
    request = replace(_request(tmp_path), stream=stream)
    options = RunOptions(config_snapshot=snapshot, permission_asker=lambda *_: True)
    env = native_sdk.build_product_run_environment(request, options, config_snapshot=snapshot)
    try:
        asyncio.run(native_sdk.NativeSDKRunner(env).run_result(request, options))
        assert len(requests) == 8  # 一次初始规划、六次读文件、一轮最终回答。
        assert len(main_calls) == 7
        assert env.runtime_state.plan_text == TEXT
        assert env.runtime_state.plan_generated and env.runtime_state.plan_revision == 1
        assert env.runtime_state.edits_this_run == 0
    finally:
        env.close()
