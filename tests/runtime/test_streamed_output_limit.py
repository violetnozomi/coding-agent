"""流式异常终态不能新执行工具，未执行截断应沿用有界继续。"""
from __future__ import annotations

import asyncio
import copy
import json
import socket
from types import SimpleNamespace

import pytest

from nz_coder.protocol.message_schema import ASSISTANT_ERROR_KEY
from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.providers.normalized import chunk, completion, NormalizedFunction, NormalizedToolCall
from nz_coder.runtime.conversation.model_result import LLMResult
from nz_coder.runtime.core import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunOptions, RunRequest
from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.execution.provider_stream import _settle_stream_tools
from nz_coder.runtime.model_gateway import ModelCallOutcome, ResolvedModelRuntime


def _tool(name, arguments, call_id="limited-call", index=0):
    return NormalizedToolCall(index=index, id=call_id,
        function=NormalizedFunction(name=name, arguments=arguments))


def _run(monkeypatch, tmp_path, turns, *, stream=True, limit=5, capabilities=None,
         task="Inspect marker.txt and report what it contains. Do not modify files."):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "marker.txt").write_text("original")
    requests, tool_results, network_attempts = [], [], []
    monkeypatch.setenv("NZ_PROVIDER_MAX_RETRIES", "0")
    monkeypatch.setenv("NZ_CODER_USER_CONFIG", str(tmp_path / "user.env"))
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(tmp_path / "trust.json"))

    def deny_network(*_args, **_kwargs):
        network_attempts.append(True)
        raise AssertionError("offline output-limit test attempted external network")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            if any(tool.get("function", {}).get("name") == "emit_sidecar_verdict"
                   for tool in kwargs.get("tools", [])):
                # 此替身只提供编码轮次，不凭空制造交付评审证据。
                raise RuntimeError("Offline fixture supplies no delivery review evidence")
            requests.append(copy.deepcopy(kwargs))
            assert len(requests) <= len(turns), "unexpected provider continuation"
            return turns[len(requests) - 1]

    runtime = ResolvedModelRuntime(provider_id="offline", model_id="output-limit",
        request_model_id="output-limit", variant=None, provider=Provider(),
        client=SimpleNamespace(close=lambda: None), owns_client=True,
        capabilities=capabilities or ModelCapabilities(provider="offline", model_id="output-limit"))
    for target in ("nz_coder.runtime.execution.native_sdk.resolve_model_runtime",
                   "nz_coder.runtime.execution.loop.resolve_model_runtime",
                   "nz_coder.runtime.model_gateway.resolve_model_runtime"):
        monkeypatch.setattr(target, lambda *_a, **_k: runtime)
    request = RunRequest(
        agent=AgentDefinition(name="output-limit", instructions="Report actual tool outcomes."),
        profile=MAIN_PROFILE, workspace=workspace, session_id="output-limit",
        stream=stream, provider="offline", model="output-limit",
        messages=({"role": "user", "content": task},),
        metadata={"permission_mode": "default", "persist_session": True, "max_turns": limit})
    options = RunOptions(permission_asker=lambda *_a: True)
    env = native_sdk.build_product_run_environment(request, options)
    observe = env._record_tool_result

    def record(result):
        tool_results.append(result)
        return observe(result)

    monkeypatch.setattr(env, "_record_tool_result", record)
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(env).run_result(request, options))
        trace = [json.loads(line) for line in env.tracer.path.read_text().splitlines()]
        from nz_coder.runtime.session.model import SessionIdentity
        saved = asyncio.run(env.runtime_services.session_runtime.store.load(
            SessionIdentity("output-limit"), workspace))
        assert saved is not None
        assert not network_attempts
        return result, requests, trace, saved.transcript, tool_results, workspace
    finally:
        env.close()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("tool_name,arguments", [
    ("write_files_batch", '{"files":['),
    ("write_file", '{"path":"marker.txt","content":"must-not-run"}'),
])
def test_output_limit_retries_only_unexecuted_calls(monkeypatch, tmp_path, stream, tool_name, arguments):
    first = _tool(tool_name, arguments)
    read = _tool("read_file", '{"path":"marker.txt"}', "read-after-limit")
    usage = {"prompt_tokens": 41, "completion_tokens": 13, "total_tokens": 54}
    turns = ([iter([chunk(tool_calls=[first]), chunk(finish_reason="length", usage=usage)]),
              iter([chunk(tool_calls=[read]), chunk(finish_reason="tool_calls")]),
              iter([chunk(content="The file contains original.", finish_reason="stop")])]
             if stream else [completion(tool_calls=[first], finish_reason="length", usage=usage),
                             completion(tool_calls=[read], finish_reason="tool_calls"),
                             completion(content="The file contains original.", finish_reason="stop")])
    result, requests, trace, history, tools, workspace = _run(monkeypatch, tmp_path, turns, stream=stream)
    # 文件读取完成不提供行为验证或交付评审，不能把未验证结果写成已验证完成。
    assert result.status.value == "completed_unverified", result.final_text
    assert result.metadata["verification"]["verification_state"] == "unverified"
    assert result.metadata["verification"]["last_verification"] is None
    assert result.metadata["verification"]["delivery_review"] is None
    assert (workspace / "marker.txt").read_text() == "original"
    assert len(requests) == 3
    assert len([e for e in trace if e["event"] == "model_output_limit_continuation"]) == 1
    feedback = [m for m in requests[1]["messages"] if m.get("role") == "tool" and m.get("tool_call_id") == "limited-call"]
    assert len(feedback) == 1 and "not executed" in feedback[0]["content"]
    assert len([m for m in history if m.get("role") == "tool" and m.get("tool_call_id") == "limited-call"]) == 1
    assert not any(t.executed and t.name in {"write_file", "write_files_batch"} for t in tools)
    assert result.metadata["runtime"]["provider_usage"]["output"] == 13
    assert result.metadata["runtime"]["provider_usage"]["total"] == 54


def test_streamed_truncated_batch_can_retry_with_real_file_writes(monkeypatch, tmp_path):
    complete = json.dumps({"files": [{"path": "result.txt", "content": "complete"},
                                      {"path": "notes.md", "content": "recorded"}]})
    turns = [iter([chunk(tool_calls=[_tool("write_files_batch", '{"files":[')]),
                   chunk(finish_reason="length")]),
             iter([chunk(tool_calls=[_tool("write_files_batch", complete, "complete-batch")]),
                   chunk(finish_reason="tool_calls")])]
    _result, requests, _trace, history, tools, workspace = _run(
        monkeypatch, tmp_path, turns, limit=2,
        task="Create result.txt containing complete and notes.md containing recorded.")
    assert len(requests) == 2
    assert (workspace / "result.txt").read_text() == "complete"
    assert (workspace / "notes.md").read_text() == "recorded"
    assert (workspace / "marker.txt").read_text() == "original"
    assert len([t for t in tools if t.executed and t.name == "write_files_batch"]) == 1
    assert len([m for m in history if m.get("role") == "tool" and m.get("tool_call_id") == "complete-batch"]) == 1


@pytest.mark.parametrize("finish", ["length", "error"])
def test_terminal_fallback_does_not_dispatch_unexecuted_tools(tmp_path, finish):
    target = tmp_path / "must-not-exist"
    result = LLMResult(tool_calls=[{"id": "unexecuted"}], finish_reason=finish)
    def handler(_result):
        target.write_text("executed")
        return "continue"
    projected = _settle_stream_tools(SimpleNamespace(), result,
        ModelCallOutcome.completed(finish_reason=finish), handler, False, "", None, 0)
    assert not target.exists()
    assert not projected.tools_executed_in_stream


def test_streamed_error_does_not_execute_valid_tool_arguments(monkeypatch, tmp_path):
    first = _tool("write_file", '{"path":"marker.txt","content":"must-not-run"}')
    turns = [iter([chunk(tool_calls=[first]), chunk(finish_reason="error")])]
    result, requests, _trace, history, tools, workspace = _run(monkeypatch, tmp_path, turns)
    assert result.status.value == "error"
    assert len(requests) == 1
    assert (workspace / "marker.txt").read_text() == "original"
    assert not any(t.executed for t in tools)
    assert any(part.get("type") == "tool" and part.get("state", {}).get("status") == "error"
               for message in history for part in message.get("_nz_parts", []))


def _assert_stop_diagnostic(result, trace, history, code, count):
    stopped = [event for event in trace if event["event"] == "model_output_limit_stopped"]
    assert len(stopped) == 1
    assert stopped[0]["reason"] == code and stopped[0]["continuation_count"] == count
    errors = [message[ASSISTANT_ERROR_KEY] for message in history
              if message.get(ASSISTANT_ERROR_KEY, {}).get("name") == "ModelOutputLimitError"]
    assert len(errors) == 1
    public = errors[0]["data"]["public_error"]
    assert public["code"] == code
    assert public["message"] in result.final_text


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("evidence", ["reasoning_text", "reasoning_usage", "empty"])
def test_output_limit_without_visible_progress_does_not_repeat_unchanged_request(
    monkeypatch, tmp_path, stream, evidence,
):
    private_reasoning = "private reasoning must not appear in a public diagnostic"
    reasoning = private_reasoning if evidence == "reasoning_text" else ""
    usage = {"prompt_tokens": 41, "completion_tokens": 8192, "total_tokens": 8233}
    if evidence == "reasoning_usage":
        usage["completion_tokens_details"] = {"reasoning_tokens": 8192}
    monkeypatch.setenv("MAX_OUTPUT_TOKENS", "8192")
    turns = ([iter([chunk(content=" \n", reasoning_content=reasoning),
                    chunk(finish_reason="length", usage=usage)]) for _ in range(3)]
             if stream else [completion(content=" \n", reasoning_content=reasoning,
                                         finish_reason="length", usage=usage) for _ in range(3)])
    result, requests, trace, history, tools, workspace = _run(
        monkeypatch, tmp_path, turns, stream=stream,
        capabilities=ModelCapabilities(provider="offline", model_id="output-limit",
                                       output_tokens=8192),
    )
    assert result.status.value == "error"
    assert len(requests) == 1
    assert requests[0]["max_tokens"] == 8192
    assert not any(event["event"] == "model_output_limit_continuation" for event in trace)
    assert not any(tool.executed for tool in tools)
    assert (workspace / "marker.txt").read_text() == "original"
    _assert_stop_diagnostic(result, trace, history, "output_limit_no_visible_progress", 0)
    limits = [event for event in trace if event["event"] == "model_output_limit"]
    assert len(limits) == 1
    assert limits[0]["reasoning_only"] is (evidence != "empty")
    assert "no visible" in result.final_text.lower()
    assert "recovery was exhausted" not in result.final_text
    assert private_reasoning not in result.final_text
    errors = [message[ASSISTANT_ERROR_KEY] for message in history
              if message.get(ASSISTANT_ERROR_KEY, {}).get("name") == "ModelOutputLimitError"]
    assert private_reasoning not in json.dumps(errors)
    assert private_reasoning not in json.dumps(trace)
    assert result.metadata["runtime"]["provider_usage"]["total"] == 8233
    assert result.metadata["runtime"]["provider_usage"]["reasoning"] == (
        8192 if evidence == "reasoning_usage" else 0
    )


@pytest.mark.parametrize("stream", [False, True])
def test_visible_partial_answer_can_still_continue_after_output_limit(monkeypatch, tmp_path, stream):
    turns = ([iter([chunk(content="The file contains", finish_reason="length")]),
              iter([chunk(content="The file contains original.", finish_reason="length")])]
             if stream else [completion(content="The file contains", finish_reason="length"),
                             completion(content="The file contains original.", finish_reason="length")])
    result, requests, trace, history, tools, workspace = _run(
        monkeypatch, tmp_path, turns, stream=stream, limit=2,
    )
    assert result.status.value == "error"
    assert "original" in result.final_text
    assert len(requests) == 2
    assert requests[0]["max_tokens"] == requests[1]["max_tokens"]
    assert len([event for event in trace if event["event"] == "model_output_limit_continuation"]) == 1
    _assert_stop_diagnostic(result, trace, history, "output_limit_turn_budget_exhausted", 1)
    assert not any(tool.executed for tool in tools)
    assert (workspace / "marker.txt").read_text() == "original"


@pytest.mark.parametrize("stream", [False, True])
def test_empty_output_limit_on_last_turn_keeps_turn_boundary_reason(monkeypatch, tmp_path, stream):
    turns = ([iter([chunk(finish_reason="length")])] if stream
             else [completion(finish_reason="length")])
    result, requests, trace, history, tools, _workspace = _run(
        monkeypatch, tmp_path, turns, stream=stream, limit=1,
    )
    assert result.status.value == "error"
    assert len(requests) == 1
    assert not any(tool.executed for tool in tools)
    _assert_stop_diagnostic(result, trace, history, "output_limit_turn_budget_exhausted", 0)


@pytest.mark.parametrize("limit", [3, 5])
def test_repeated_streamed_limits_stop_after_bounded_recovery(monkeypatch, tmp_path, limit):
    turns = [iter([chunk(tool_calls=[_tool("write_files_batch", '{"files":[', f"cut-{i}")]),
                   chunk(finish_reason="length")]) for i in range(3)]
    result, requests, trace, history, tools, workspace = _run(monkeypatch, tmp_path, turns, limit=limit)
    assert result.status.value == "error"
    assert "recovery was exhausted" in result.final_text
    assert len(requests) == 3
    assert len([e for e in trace if e["event"] == "model_output_limit_continuation"]) == 2
    assert not any(t.executed for t in tools)
    assert (workspace / "marker.txt").read_text() == "original"
    for i in range(2):
        assert len([m for m in history if m.get("role") == "tool" and m.get("tool_call_id") == f"cut-{i}"]) == 1
    _assert_stop_diagnostic(result, trace, history, "output_limit_recovery_exhausted", 2)


@pytest.mark.parametrize("limit", [1, 5])
def test_executed_stream_prefix_is_not_replayed_after_later_limit(monkeypatch, tmp_path, limit):
    first = _tool("write_file", '{"path":"marker.txt","content":"executed-once"}', "prefix")
    tail = _tool("write_file", '{"path":"tail.txt","content":', "tail", index=1)
    turns = [iter([chunk(tool_calls=[first]), chunk(finish_reason="tool_calls"),
                   chunk(tool_calls=[tail]), chunk(finish_reason="length")])]
    result, requests, trace, history, tools, workspace = _run(monkeypatch, tmp_path, turns, limit=limit)
    assert result.status.value == "error"
    assert len(requests) == 1
    assert (workspace / "marker.txt").read_text() == "executed-once"
    assert not (workspace / "tail.txt").exists()
    assert len([t for t in tools if t.executed and t.name == "write_file"]) == 1
    assert "recovery was exhausted" not in result.final_text
    assert "execution" in result.final_text.lower()
    _assert_stop_diagnostic(result, trace, history, "output_limit_after_tool_execution", 0)


def test_last_turn_limit_reports_turn_boundary_not_exhausted_recovery(monkeypatch, tmp_path):
    turns = [iter([chunk(tool_calls=[_tool("write_files_batch", '{"files":[')]),
                   chunk(finish_reason="length")])]
    result, requests, trace, history, tools, _workspace = _run(monkeypatch, tmp_path, turns, limit=1)
    assert result.status.value == "error"
    assert len(requests) == 1
    assert not any(e["event"] == "model_output_limit_continuation" for e in trace)
    assert not any(t.executed for t in tools)
    assert "recovery was exhausted" not in result.final_text
    assert "turn" in result.final_text.lower()
    _assert_stop_diagnostic(result, trace, history, "output_limit_turn_budget_exhausted", 0)
