"""供应商报出的终止原因决定工具是否可执行，完整的参数本身不能证明成功。"""
import pytest

from nz_coder.providers import AnthropicProvider, GeminiProvider
from nz_coder.providers.openai_responses import OpenAIResponsesProvider
from nz_coder.providers.normalized import NormalizedFunction, NormalizedToolCall, chunk, completion
from tests.runtime.model_gateway.test_provider_protocol_regressions import Provider, Transport, call, gateway
from tests.test_openai_responses import _FakeClient


def _response(vendor, reason, streaming):
    if vendor == "anthropic":
        block = {"type": "tool_use", "id": "one", "name": "read_file", "input": {"path": "marker"}}
        body = {"content": [block], "stop_reason": reason}
        events = [
            {"type": "content_block_start", "index": 0, "content_block": block},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": '{"path":"marker"}'}},
            {"type": "message_delta", "delta": {"stop_reason": reason}},
            {"type": "message_stop"},
        ]
        return AnthropicProvider(api_key="offline", transport=Transport(response=body, events=events))
    if vendor == "gemini":
        function = {"functionCall": {"name": "read_file", "args": {"path": "marker"}}}
        body = {"candidates": [{"content": {"parts": [function]}, "finishReason": reason}]}
        return GeminiProvider(api_key="offline", transport=Transport(response=body, events=[body]))
    tool = NormalizedToolCall(id="one", function=NormalizedFunction("read_file", '{"path":"marker"}'))
    response = (iter([chunk(tool_calls=[tool]), chunk(finish_reason=reason), chunk(usage={"total_tokens": 9})])
                if streaming else completion(tool_calls=[tool], finish_reason=reason))
    return Provider([response])


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("vendor, reason, expected", [
    ("openai", "tool_calls", "tool_calls"), ("openai", "content_filter", "error"),
    ("anthropic", "tool_use", "tool_calls"), ("anthropic", "max_tokens", "length"),
    ("anthropic", "refusal", "error"), ("gemini", "STOP", "tool_calls"),
    ("gemini", "MAX_TOKENS", "length"), ("gemini", "SAFETY", "error"),
])
def test_filtered_or_shortened_tool_call_keeps_its_actual_finish(vendor, reason, expected, streaming):
    model = gateway(_response(vendor, reason, streaming), max_retries=0)
    if streaming:
        events = []
        result = model.complete_stream_sync(call(), on_event=events.append)
        assert [e.data["reason"] for e in events if e.kind == "finish"] == [expected]
        if vendor == "openai":
            assert result.usage.total_tokens == 9
    else:
        result = model.complete_sync(call(stream=False))
    assert result.finish_reason == expected


def test_late_finish_cannot_make_a_filtered_call_executable():
    tool = NormalizedToolCall(id="one", function=NormalizedFunction("read_file", '{"path":"marker"}'))
    provider = Provider([iter([chunk(tool_calls=[tool]), chunk(finish_reason="content_filter"),
                               chunk(finish_reason="stop"), chunk(usage={"total_tokens": 11})])])
    events = []
    result = gateway(provider).complete_stream_sync(call(), on_event=events.append)
    assert [e.data["reason"] for e in events if e.kind == "finish"] == ["error"]
    assert result.finish_reason == "error" and result.usage.total_tokens == 11


@pytest.mark.parametrize("tail, expected", [([], "aborted"), ([{"finishReason": "STOP"}], "completed")])
def test_gemini_unspecified_finish_waits_for_the_terminal_frame(tail, expected):
    tool = {"functionCall": {"name": "read_file", "args": {"path": "marker"}}}
    first = {"content": {"parts": [tool]}, "finishReason": "FINISH_REASON_UNSPECIFIED"}
    provider = GeminiProvider(api_key="offline", transport=Transport(
        events=[{"candidates": [candidate]} for candidate in [first, *tail]],
    ))
    events = []
    result = gateway(provider, max_retries=0).complete_stream_sync(call(), on_event=events.append)
    assert result.status.value == expected
    assert [e.data["reason"] for e in events if e.kind == "finish"] == (["tool_calls"] if tail else [])


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("status, reason, item_status, expected", [
    ("completed", "", "completed", "tool_calls"),
    ("incomplete", "content_filter", "incomplete", "error"),
    ("incomplete", "max_output_tokens", "incomplete", "length"),
    ("failed", "", "incomplete", None),
    ("cancelled", "", "incomplete", None),
    ("queued", "", "in_progress", "pending"),
    ("in_progress", "", "in_progress", "pending"),
    ("completed", "", "incomplete", "error"),
])
def test_responses_status_gates_tools_and_preserves_reported_usage(
    streaming, status, reason, item_status, expected,
):
    item = {"type": "function_call", "name": "read_file", "call_id": "one",
            "arguments": '{"path":"marker"}', "status": item_status}
    body = {"status": status, "output": [item], "incomplete_details": {"reason": reason},
            "usage": {"input_tokens": 7, "output_tokens": 4, "total_tokens": 11,
                      "input_tokens_details": {"cached_tokens": 2}, "cost": 0.05}}
    terminal = "failed" if status == "cancelled" else status
    events = [{"type": "response.output_item.added", "item": {**item, "status": "in_progress"}},
              {"type": "response.function_call_arguments.delta", "delta": item["arguments"]},
              {"type": f"response.{terminal}", "response": body}]
    raw = iter(events) if streaming else body
    provider = OpenAIResponsesProvider(api_key="offline", client_factory=lambda **_: _FakeClient(raw))
    host = gateway(provider, max_retries=0)
    observed = []
    if streaming:
        result = host.complete_stream_sync(call(), on_event=observed.append)
    else:
        result = host.complete_sync(call(stream=False))
    if expected is None or (expected == "pending" and streaming):
        assert result.status.value == "aborted" and result.finish_reason == ""
    else:
        assert result.finish_reason == ("error" if expected == "pending" else expected)
    if streaming:
        assert [e.data["reason"] for e in observed if e.kind == "finish"] == (
            [expected] if expected not in {None, "pending"} else []
        )
    assert result.usage.total_tokens == 11 and result.usage.cache_read_tokens == 2
    assert result.cost == 0.05 and result.cost_source == "provider"


def test_responses_incomplete_tool_done_without_final_output_is_not_executable():
    item = {"type": "function_call", "id": "one", "name": "read_file",
            "arguments": '{"path":"marker"}', "status": "incomplete"}
    events = [{"type": "response.output_item.done", "item": item},
              {"type": "response.completed", "response": {"status": "completed", "output": []}}]
    provider = OpenAIResponsesProvider(api_key="offline", client_factory=lambda **_: _FakeClient(iter(events)))
    result = gateway(provider, max_retries=0).complete_stream_sync(call())
    assert result.finish_reason == "error"


def test_buffered_responses_retry_counts_each_settled_usage_once():
    failed = {"status": "failed", "error": {"message": "server unavailable"},
              "usage": {"input_tokens": 3, "total_tokens": 3, "cost": 0.04}, "output": []}
    complete = {"status": "completed", "output": [],
                "usage": {"input_tokens": 7, "total_tokens": 7, "cost": 0.08}}
    payloads = iter([failed, complete])
    client = _FakeClient(None)
    client.responses.create = lambda **_: next(payloads)
    provider = OpenAIResponsesProvider(api_key="offline", client_factory=lambda **_: client)
    result = gateway(provider, max_retries=1, backoff_base=0).complete_sync(call(stream=False))
    assert result.finish_reason == "stop" and result.attempts == 2
    assert result.usage.total_tokens == 10 and result.cost == 0.12 and result.cost_source == "provider"


@pytest.mark.parametrize("item, reason", [
    ({"type": "message", "content": [{"type": "output_text", "text": "ready"}]}, "stop"),
    ({"type": "function_call", "name": "read_file", "call_id": "one", "arguments": "{}"}, "error"),
])
def test_buffered_responses_optional_status_preserves_plain_text_and_requires_completed_tools(item, reason):
    client = _FakeClient({"output": [item]})
    provider = OpenAIResponsesProvider(api_key="offline", client_factory=lambda **_: client)
    result = gateway(provider).complete_sync(call(stream=False))
    assert result.finish_reason == reason
