"""正式循环：事实提醒走主请求，真实重复授权继续独立生效。"""
import asyncio
import copy
import threading
from dataclasses import replace

import pytest

from nz_coder.runtime.execution import native_sdk
from test_native_task_intent import environment
from test_working_memory_reminders import _reply, _call


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("boundary", ["failed", "cached"])
def test_batch_settlement_preserves_nonexecution_event_boundary(monkeypatch, tmp_path, stream, boundary):
    requests = []
    (tmp_path / "cached.txt").write_text("stable cached evidence")
    repeated = {"command": "printf repeated"}
    class Provider:
        name = "offline"
        def create_completion(self, _client, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            if boundary == "cached" and len(requests) == 1:
                return _reply(kwargs, calls=[_call(1, "read_file", {"path": "cached.txt"})])
            if len(requests) == (1 if boundary == "failed" else 2):
                calls = ([_call(2, "bash", repeated), _call(3, "bash", repeated),
                          _call(4, "read_file", {"path": "missing.txt"})]
                         if boundary == "failed" else
                         [_call(2, "bash", repeated), _call(3, "read_file", {"path": "cached.txt"}),
                          _call(4, "bash", repeated)])
                return _reply(kwargs, calls=calls)
            return _reply(kwargs, text="Read-only observation complete.")
    env, request, options = environment(monkeypatch, tmp_path, Provider(), stream=stream, tool_names=("bash", "read_file"))
    observed = []
    original = env.recovery.record_tool_result_evidence
    def record(name, args, output, **kwargs):
        observed.append((name, args, kwargs))
        return original(name, args, output, **kwargs)
    monkeypatch.setattr(env.recovery, "record_tool_result_evidence", record)
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(env).run_result(request, options))
        assert len(requests) == (2 if boundary == "failed" else 3)
        assert "<repeat-observation>" not in str(requests[-1]["messages"])
        assert len([m for m in result.messages if m.get("role") == "tool"]) == (3 if boundary == "failed" else 4)
        assert any(name == "read_file" and flags["cache_hit" if boundary == "cached" else "dispatch_failed"]
                   for name, _, flags in observed)
    finally:
        env.close()


@pytest.mark.parametrize("stream", [False, True])
def test_same_call_cache_preserves_event_without_counting_as_execution(monkeypatch, tmp_path, stream):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    requests = []
    (tmp_path / "source.txt").write_text("same source")
    class Provider:
        name = "offline"
        def create_completion(self, _client, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            if len(requests) == 3:
                env.executor.clear_read_cache()
            if len(requests) <= 3:
                return _reply(kwargs, calls=[_call(len(requests), "read_file", {"path": "source.txt"})])
            return _reply(kwargs, text="Read-only observation complete.")
    env, request, options = environment(monkeypatch, tmp_path, Provider(), stream=stream, tool_names=("read_file",))
    # 使用已有阈值配置隔离缓存语义；默认3次授权由另一组真实交互测试覆盖。
    options = replace(options, config_snapshot=load_config_snapshot(tmp_path, environ={
        "NZ_DOOM_LOOP_THRESHOLD": "100", "RUNTIME_STATE_PERSIST": "0", "NZ_PLANNING_ENABLED": "0",
        "NZ_WORKING_MEMORY_REMINDER_STEPS": "0", "MAX_AGENT_TURNS": "500",
    }))
    try:
        asyncio.run(native_sdk.NativeSDKRunner(env).run_result(request, options))
        assert len(requests) == 4
        assert "<repeat-observation>" not in str(requests[2]["messages"])
        assert "executed 2 times" in str(requests[3]["messages"])
    finally:
        env.close()


@pytest.mark.parametrize("profile", ["main", "read_child", "write_child"])
def test_real_compaction_retracts_old_notice_and_refreshes_normal_results(monkeypatch, tmp_path, profile):
    from nz_coder.runtime.conversation.context_manager import ProductionContextManager, accept_compaction
    from nz_coder.runtime.conversation.request_snapshot import message_version
    from nz_coder.runtime.core.profiles import profile_for_mode
    from nz_coder.protocol.message_schema import SYNTHETIC_USER_KEY

    requests = []
    prepared = []
    class Provider:
        name = "offline"
        def create_completion(self, _client, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            if len(requests) <= 4:
                return _reply(kwargs, calls=[_call(len(requests), "bash", {"command": "pwd"})])
            return _reply(kwargs, text="Read-only observation complete.")
    env, request, options = environment(monkeypatch, tmp_path, Provider(), tool_names=("bash",))
    request = replace(request, profile=profile_for_mode(profile))
    original = ProductionContextManager.prepare_async
    async def prepare(self, context, messages, **kwargs):
        frame = kwargs["request_snapshot"]
        if len(requests) == 2:
            assert frame.repeat_notice is not None
            source = frame.repeat_notice.source
            accepted = accept_compaction(context, messages,
                [{"role": "user", "content": "Compacted read-only observations.",
                  SYNTHETIC_USER_KEY: True, "_nz_compaction": {"operation_id": "repeat-test"}}],
                snapshot=frame, source_version=message_version(messages), trigger="test", physical=False)
            assert accepted
            prepared.append(source)
            return True
        return await original(self, context, messages, **kwargs)
    monkeypatch.setattr(ProductionContextManager, "prepare_async", prepare)
    try:
        asyncio.run(native_sdk.NativeSDKRunner(env).run_result(request, options))
        assert len(prepared) == 1 and len(requests) == 5
        assert "<repeat-observation>" not in str(requests[2]["messages"])
        # 压缩后的两次真实结果仍能形成新事件，证明新结果没有被旧epoch误丢弃。
        assert "<repeat-observation>" in str(requests[4]["messages"])
    finally:
        env.close()


def test_stream_tool_committed_before_tail_error_acknowledges_without_replay(monkeypatch, tmp_path):
    requests = []
    acknowledgements = []
    from nz_coder.runtime.verification.recovery import RecoveryState
    original_ack = RecoveryState.acknowledge_repeat_notice
    def ack(self, notice_id, source):
        acknowledgements.append((notice_id, source))
        return original_ack(self, notice_id, source)
    monkeypatch.setattr(RecoveryState, "acknowledge_repeat_notice", ack)
    class Provider:
        name = "offline"
        def create_completion(self, _client, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            n = len(requests)
            if n <= 3:
                stream = _reply(kwargs, calls=[_call(n, "bash", {"command": "printf repeat-evidence"})])
                if n == 3:
                    def broken():
                        yield from stream
                        raise ConnectionError("tail disconnected after full tool commit")
                    return broken()
                return stream
            return _reply(kwargs, text="Read-only observation complete.")
    env, request, options = environment(monkeypatch, tmp_path, Provider(), stream=True, tool_names=("bash",))
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(env).run_result(request, options))
        assert len(requests) == 3
        assert "<repeat-observation>" in str(requests[2]["messages"])
        rows = [m for m in result.messages if m.get("role") == "tool"]
        assert len(rows) == 3 and len({m["tool_call_id"] for m in rows}) == 3
        assert len(acknowledgements) == 1
        assert result.status.value == "error"
    finally:
        env.close()


def test_graph_handoff_return_uses_actual_persona_and_invalidates_old_batch(tmp_path):
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec, HandoffSpec
    from nz_coder.runtime.process.workdir import scoped_workdir
    from tests.test_loop_fake import FakeClient, FakeResponse, FakeMessage, FakeToolCall

    graph = AgentGraph([
        AgentSpec("caller", "CALLER", allowed_tools=(), handoffs=(HandoffSpec("helper", kind="as-tool"),)),
        AgentSpec("helper", "HELPER", allowed_tools=()),
    ], start="caller")
    client = FakeClient([
        FakeResponse(FakeMessage(tool_calls=[FakeToolCall("emit_handoff", {"target": "helper", "summary": "Inspect."})])),
        FakeResponse(FakeMessage("Read-only helper result.")),
        FakeResponse(FakeMessage("Read-only task complete.")),
    ])
    with scoped_workdir(tmp_path):
        env = AgentLoop("unused", permission_mode="auto", client=client, trace_enabled=False, agent_graph=graph)
        captured = []
        capture = env.recovery.capture_repeat_source
        def capture_source(state, run, persona, messages):
            source = capture(state, run, persona, messages)
            captured.append((persona, source, env.recovery._last_tool_result_signature))
            return source
        env.recovery.capture_repeat_source = capture_source
        try:
            result = asyncio.run(env.run([{"role": "user", "content": "Inspect using the helper; explain only."}], stream=False))
            assert result["status"] == "completed"
            sources = [(name, source) for name, source, _ in captured if source is not None]
            by_name = [item for i, item in enumerate(sources) if i == 0 or item != sources[i - 1]]
            assert [name for name, _ in by_name] == ["caller", "helper", "caller"]
            assert [source.observer_epoch for _, source in by_name] == sorted({source.observer_epoch for _, source in by_name})
            assert len({source.instruction_message_id for _, source in by_name}) == 1
            assert all(signature is None for _, _, signature in captured)
        finally:
            env.close()


@pytest.mark.parametrize("stream", [False, True])
def test_eight_real_repeats_have_one_notice_no_side_request_or_worker(monkeypatch, tmp_path, stream):
    requests = []
    threads = []
    original = threading.Thread.start
    def start(thread):
        threads.append(thread.name)
        return original(thread)
    monkeypatch.setattr(threading.Thread, "start", start)
    class Provider:
        name = "offline"
        def create_completion(self, _client, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            if len(requests) <= 8:
                return _reply(kwargs, calls=[_call(len(requests), "bash", {"command": "printf repeat-evidence"})])
            return _reply(kwargs, text="Read-only observation complete.")
    env, request, options = environment(monkeypatch, tmp_path, Provider(), stream=stream, tool_names=("bash",))
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(env).run_result(request, options))
        assert len(requests) == 9
        notice_requests = [i for i, request in enumerate(requests) if "<repeat-observation>" in str(request["messages"])]
        assert notice_requests[0] == 2
        assert len(notice_requests) < 4  # 授权清零会开启新事件；同一事件不逐轮发送。
        assert not any("stall" in name for name in threads)
        assert "<repeat-observation>" not in str(result.messages)
        rows = [m for m in result.messages if m.get("role") == "tool"]
        assert len(rows) == 8 and all("repeat-evidence" in m["content"] for m in rows)
    finally:
        env.close()
