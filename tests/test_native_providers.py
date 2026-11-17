"""Offline contract tests for native Anthropic and Gemini providers."""
from __future__ import annotations

import json

import pytest

from nz_coder.foundation import config
from nz_coder.providers import (
    AnthropicProvider,
    GeminiProvider,
    create_provider,
)
from nz_coder.providers.http import UrllibTransport
from nz_coder.runtime.execution.loop import AgentLoop


class _FakeTransport:
    def __init__(self, *, response=None, events=None):
        self.response = response or {}
        self.events = events or []
        self.calls = []

    def post_json(self, url, headers, payload):
        self.calls.append(("json", url, headers, payload))
        return self.response

    def post_sse(self, url, headers, payload):
        self.calls.append(("sse", url, headers, payload))
        return iter(self.events)


def _tool_spec():
    return {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }


def _history_with_tool_call(provider_extra=None):
    tool_call = {
        "id": "call-1",
        "type": "function",
        "function": {
            "name": "read_file",
            "arguments": json.dumps({"path": "README.md"}),
        },
    }
    if provider_extra:
        tool_call["provider_extra"] = provider_extra
    return [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "Inspect the readme."},
        {"role": "assistant", "content": "", "tool_calls": [tool_call]},
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "README contents",
        },
    ]


def test_anthropic_non_streaming_translates_messages_tools_and_response():
    transport = _FakeTransport(
        response={
            "type": "message",
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 17, "output_tokens": 9},
            "content": [
                {"type": "thinking", "thinking": "check"},
                {"type": "text", "text": "Reading."},
                {
                    "type": "tool_use",
                    "id": "call-2",
                    "name": "read_file",
                    "input": {"path": "pyproject.toml"},
                },
            ],
        },
    )
    provider = AnthropicProvider(
        api_key="anthropic-key",
        base_url="https://anthropic.test",
        transport=transport,
    )
    client = provider.create_client()
    response = client.chat.completions.create(
        model="claude-test",
        messages=_history_with_tool_call(),
        tools=[_tool_spec()],
        tool_choice={
            "type": "function",
            "function": {"name": "read_file"},
        },
        max_tokens=123,
    )

    kind, url, headers, payload = transport.calls[0]
    assert kind == "json"
    assert url == "https://anthropic.test/v1/messages"
    assert headers["x-api-key"] == "anthropic-key"
    assert payload["system"] == "You are a coding agent."
    assert payload["max_tokens"] == 123
    assert payload["tools"][0]["input_schema"]["required"] == ["path"]
    assert payload["tool_choice"] == {"type": "tool", "name": "read_file"}
    assert [message["role"] for message in payload["messages"]] == [
        "user",
        "assistant",
        "user",
    ]
    assert payload["messages"][1]["content"][0]["type"] == "tool_use"
    assert payload["messages"][2]["content"][0]["type"] == "tool_result"

    message = response.choices[0].message
    assert message.content == "Reading."
    assert message.reasoning_content == "check"
    assert message.tool_calls[0].model_dump() == {
        "id": "call-2",
        "type": "function",
        "function": {
            "name": "read_file",
            "arguments": '{"path": "pyproject.toml"}',
        },
    }
    assert response.choices[0].finish_reason == "tool_calls"
    assert response.usage == {
        "input_tokens": 17,
        "uncached_input_tokens": 17,
        "output_tokens": 9,
        "total_tokens": 26,
    }


def test_anthropic_stream_normalizes_text_reasoning_and_partial_tool_json():
    transport = _FakeTransport(
        events=[
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "call-stream",
                    "name": "read_file",
                },
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"path":',
                },
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '"README.md"}',
                },
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Done"},
            },
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "thinking_delta", "thinking": "verify"},
            },
        ],
    )
    provider = AnthropicProvider(api_key="key", transport=transport)
    chunks = list(
        provider.create_completion(
            provider.create_client(),
            model="claude-test",
            messages=[{"role": "user", "content": "read"}],
            stream=True,
        )
    )

    assert chunks[0].choices[0].delta.tool_calls[0].function.name == "read_file"
    arguments = "".join(
        item.choices[0].delta.tool_calls[0].function.arguments
        for item in chunks[1:3]
    )
    assert json.loads(arguments) == {"path": "README.md"}
    assert chunks[3].choices[0].delta.content == "Done"
    assert chunks[4].choices[0].delta.reasoning_content == "verify"


def test_anthropic_exact_catalog_variant_maps_to_native_payload(monkeypatch):
    monkeypatch.setattr(config, "MODEL_ID", "claude-test")
    monkeypatch.setattr(config, "MODEL_CAPABILITIES_JSON", "")
    monkeypatch.setattr(
        config,
        "MODEL_CATALOG_JSON",
        json.dumps(
            {
                "models": {
                    "anthropic/claude-test": {
                        "supports_reasoning": True,
                        "variants": {
                            "high": {
                                "thinking": {"type": "adaptive"},
                                "effort": "high",
                            }
                        },
                    }
                }
            }
        ),
    )
    monkeypatch.setattr(config, "MODEL_VARIANT", "high")
    transport = _FakeTransport(response={"type": "message", "content": []})
    provider = AnthropicProvider(api_key="key", transport=transport)

    provider.create_completion(
        provider.create_client(),
        model="claude-test",
        messages=[{"role": "user", "content": "think"}],
    )

    payload = transport.calls[0][3]
    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "high"}


def test_gemini_non_streaming_preserves_thought_signature_round_trip():
    signature = "signed-thought"
    transport = _FakeTransport(
        response={
            "usageMetadata": {
                "promptTokenCount": 21,
                "candidatesTokenCount": 8,
                "totalTokenCount": 29,
            },
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "role": "model",
                        "parts": [
                            {"text": "Checking."},
                            {"text": "internal", "thought": True},
                            {
                                "functionCall": {
                                    "id": "call-2",
                                    "name": "read_file",
                                    "args": {"path": "pyproject.toml"},
                                },
                                "thoughtSignature": signature,
                            },
                        ],
                    }
                }
            ]
        },
    )
    provider = GeminiProvider(
        api_key="gemini-key",
        base_url="https://gemini.test/v1beta",
        transport=transport,
    )
    response = provider.create_completion(
        provider.create_client(),
        model="gemini-test",
        messages=_history_with_tool_call(
            {"thoughtSignature": "previous-signature"},
        ),
        tools=[_tool_spec()],
        tool_choice={
            "type": "function",
            "function": {"name": "read_file"},
        },
        max_tokens=456,
    )

    kind, url, headers, payload = transport.calls[0]
    assert kind == "json"
    assert url == "https://gemini.test/v1beta/models/gemini-test:generateContent"
    assert headers["x-goog-api-key"] == "gemini-key"
    assert payload["systemInstruction"]["parts"][0]["text"] == "You are a coding agent."
    assert payload["generationConfig"]["maxOutputTokens"] == 456
    assert payload["toolConfig"] == {
        "functionCallingConfig": {
            "mode": "ANY",
            "allowedFunctionNames": ["read_file"],
        }
    }
    model_part = payload["contents"][1]["parts"][0]
    assert model_part["thoughtSignature"] == "previous-signature"
    function_response = payload["contents"][2]["parts"][0]["functionResponse"]
    assert function_response["name"] == "read_file"
    assert function_response["id"] == "call-1"

    message = response.choices[0].message
    assert message.content == "Checking."
    assert message.reasoning_content == "internal"
    assert message.tool_calls[0].model_dump()["provider_extra"] == {
        "thoughtSignature": signature,
    }
    dumped_message = message.model_dump()
    assert dumped_message["role"] == "assistant"
    assert dumped_message["tool_calls"][0]["provider_extra"] == {
        "thoughtSignature": signature,
    }
    assert response.choices[0].finish_reason == "tool_calls"
    assert response.usage["total_tokens"] == 29


def test_native_streams_emit_terminal_finish_and_cumulative_usage():
    anthropic = AnthropicProvider(
        api_key="key",
        transport=_FakeTransport(events=[
            {
                "type": "message_start",
                "message": {"usage": {
                    "input_tokens": 31,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 11,
                    "cache_creation_input_tokens": 4,
                }},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "long thought"},
            },
            {
                "type": "message_delta",
                "delta": {"stop_reason": "max_tokens"},
                "usage": {"output_tokens": 12},
            },
        ]),
    )
    anthropic_chunks = list(anthropic.create_completion(
        anthropic.create_client(),
        model="claude-test",
        messages=[{"role": "user", "content": "think"}],
        stream=True,
    ))

    assert anthropic_chunks[-1].choices[0].finish_reason == "length"
    assert anthropic_chunks[-1].usage == {
        "input_tokens": 31,
        "uncached_input_tokens": 31,
        "output_tokens": 12,
        "total_tokens": 58,
        "cache_read_input_tokens": 11,
        "cache_creation_input_tokens": 4,
    }

    gemini = GeminiProvider(
        api_key="key",
        transport=_FakeTransport(events=[{
            "usageMetadata": {
                "promptTokenCount": 18,
                "candidatesTokenCount": 7,
                "totalTokenCount": 28,
                "thoughtsTokenCount": 3,
                "cachedContentTokenCount": 6,
            },
            "candidates": [{
                "finishReason": "MAX_TOKENS",
                "content": {"parts": [{"text": "partial"}]},
            }],
        }]),
    )
    gemini_chunks = list(gemini.create_completion(
        gemini.create_client(),
        model="gemini-test",
        messages=[{"role": "user", "content": "answer"}],
        stream=True,
    ))

    assert gemini_chunks[-1].choices[0].finish_reason == "length"
    assert gemini_chunks[-1].usage["total_tokens"] == 28
    assert gemini_chunks[-1].usage["reasoning_tokens"] == 3
    assert gemini_chunks[-1].usage["cache_read_input_tokens"] == 6
    assert gemini_chunks[-1].usage["uncached_input_tokens"] == 12


def test_gemini_stream_and_agent_loop_keep_provider_metadata(monkeypatch, tmp_path):
    signature = "stream-signature"
    transport = _FakeTransport(
        events=[
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "Calling."},
                                {
                                    "functionCall": {
                                        "id": "call-stream",
                                        "name": "read_file",
                                        "args": {"path": "README.md"},
                                    },
                                    "thoughtSignature": signature,
                                },
                            ]
                        },
                        "finishReason": "STOP"
                    }
                ]
            }
        ],
    )
    provider = GeminiProvider(api_key="key", transport=transport)
    client = provider.create_client()

    from nz_coder.runtime.process.workdir import scoped_workdir

    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    with scoped_workdir(tmp_path):
        from nz_coder.providers.capabilities import ModelCapabilities
        from nz_coder.runtime.model_gateway import ResolvedModelRuntime

        runtime = ResolvedModelRuntime(
            provider_id="gemini", model_id="gemini-test", request_model_id="gemini-test",
            variant=None, provider=provider, client=client,
            capabilities=ModelCapabilities(provider="gemini", model_id="gemini-test"),
        )
        agent = AgentLoop("offline", model_runtime=runtime,
                          trace_enabled=False, session_id="gemini-stream-metadata")
        try:
            result = agent._call_streaming(
                [{"role": "user", "content": "read"}],
            )
        finally:
            agent.close()

    assert result.content == "Calling."
    assert result.tool_calls[0]["function"]["name"] == "read_file"
    assert result.tool_calls[0]["provider_extra"] == {
        "thoughtSignature": signature,
    }
    assert transport.calls[0][1].endswith(
        "/models/gemini-test:streamGenerateContent?alt=sse",
    )


def test_gemini_exact_catalog_variant_maps_to_generation_config(monkeypatch):
    monkeypatch.setattr(config, "MODEL_ID", "gemini-test")
    monkeypatch.setattr(config, "MODEL_CAPABILITIES_JSON", "")
    monkeypatch.setattr(
        config,
        "MODEL_CATALOG_JSON",
        json.dumps(
            {
                "models": {
                    "gemini/gemini-test": {
                        "supports_reasoning": True,
                        "variants": {
                            "high": {
                                "thinking_config": {
                                    "includeThoughts": True,
                                    "thinkingLevel": "high",
                                }
                            }
                        },
                    }
                }
            }
        ),
    )
    monkeypatch.setattr(config, "MODEL_VARIANT", "high")
    transport = _FakeTransport(response={"candidates": []})
    provider = GeminiProvider(api_key="key", transport=transport)

    provider.create_completion(
        provider.create_client(),
        model="gemini-test",
        messages=[{"role": "user", "content": "think"}],
    )

    assert transport.calls[0][3]["generationConfig"]["thinkingConfig"] == {
        "includeThoughts": True,
        "thinkingLevel": "high",
    }


@pytest.mark.parametrize(
    ("name", "provider_type"),
    [
        ("anthropic", AnthropicProvider),
        ("claude", AnthropicProvider),
        ("gemini", GeminiProvider),
        ("google", GeminiProvider),
    ],
)
def test_create_provider_accepts_native_aliases(name, provider_type):
    provider = create_provider(
        name,
        api_key="key",
        base_url="https://provider.test",
    )
    assert isinstance(provider, provider_type)


def test_sse_parser_handles_events_and_done_marker():
    class Response:
        def __init__(self):
            self.closed = False

        def __iter__(self):
            return iter(
                [
                    b"event: message\n",
                    b'data: {"type":"text","value":"ok"}\n',
                    b"\n",
                    b"data: [DONE]\n",
                    b"\n",
                ]
            )

        def close(self):
            self.closed = True

    response = Response()
    assert list(UrllibTransport._iter_sse(response)) == [
        {"type": "text", "value": "ok"},
    ]
    assert response.closed


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan")])
def test_native_transport_rejects_invalid_timeout(timeout):
    with pytest.raises(ValueError, match="timeout"):
        UrllibTransport(timeout)


def test_native_transport_rejects_nonfinite_outgoing_payload(monkeypatch):
    import nz_coder.providers.http as provider_http

    monkeypatch.setattr(
        provider_http,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid JSON must fail before network")
        ),
    )

    with pytest.raises(ValueError, match="strict JSON"):
        UrllibTransport().post_json(
            "https://provider.test/v1/messages",
            {},
            {"temperature": float("nan")},
        )


def test_native_transport_rejects_nonstandard_json_responses(monkeypatch):
    import nz_coder.providers.http as provider_http

    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit=-1):
            return b'{"usage":NaN}'

    monkeypatch.setattr(provider_http, "urlopen", lambda *_args, **_kwargs: Response())

    with pytest.raises(RuntimeError, match="invalid JSON"):
        UrllibTransport().post_json("https://provider.test/v1/messages", {}, {})
    with pytest.raises(RuntimeError, match="invalid JSON"):
        list(UrllibTransport._decode_sse_data(['{"usage":Infinity}']))


def test_native_provider_usage_repairs_malformed_numeric_fields():
    anthropic = AnthropicProvider(
        api_key="key",
        transport=_FakeTransport(response={
            "type": "message",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": "Infinity", "output_tokens": 2},
        }),
    )
    gemini = GeminiProvider(
        api_key="key",
        transport=_FakeTransport(response={
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": "ok"}]},
            }],
            "usageMetadata": {
                "promptTokenCount": "NaN",
                "candidatesTokenCount": 3,
                "totalTokenCount": "invalid",
            },
        }),
    )

    anthropic_result = anthropic.create_completion(
        anthropic.create_client(),
        model="claude-test",
        messages=[{"role": "user", "content": "hi"}],
    )
    gemini_result = gemini.create_completion(
        gemini.create_client(),
        model="gemini-test",
        messages=[{"role": "user", "content": "hi"}],
    )

    assert anthropic_result.usage["total_tokens"] == 2
    assert gemini_result.usage["total_tokens"] == 3


def test_provider_owner_retains_failed_close_for_retry():
    from nz_coder.providers import http

    class Raw:
        def __init__(self):
            self.attempts = 0
        def close(self):
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("close failed")

    raw = Raw()
    stream = http.OwnedStream(iter(()), close_owner=raw.close)
    with pytest.raises(OSError, match="close failed"):
        stream.close()
    stream.close()
    stream.close()
    assert raw.attempts == 2


def test_native_stream_close_before_first_next_releases_or_avoids_response():
    class Response:
        def __init__(self):
            self.closed = False
        def __iter__(self):
            return iter([b'data: {"value": 1}\n', b'\n'])
        def close(self):
            self.closed = True

    class Transport(UrllibTransport):
        def __init__(self):
            super().__init__()
            self.opened = []
        def _open(self, *args):
            response = Response()
            self.opened.append(response)
            return response

    transport = Transport()
    stream = transport.post_sse("url", {}, {})
    stream.close()
    assert not transport.opened
    stream = transport.post_sse("url", {}, {})
    assert next(stream) == {"value": 1}
    stream.close()
    assert transport.opened[0].closed


@pytest.mark.parametrize("kind", ["anthropic", "gemini", "responses"])
def test_normalized_provider_stream_close_propagates_to_raw_owner(kind):
    from nz_coder.providers.openai_responses import _normalize_stream as responses_stream

    class Raw:
        def __init__(self):
            self.closed = 0
        def __iter__(self):
            return self
        def __next__(self):
            raise StopIteration
        def close(self):
            self.closed += 1

    raw = Raw()
    normalizer = {"anthropic": AnthropicProvider._normalize_stream,
                  "gemini": GeminiProvider._normalize_stream,
                  "responses": responses_stream}[kind]
    stream = normalizer(raw)
    stream.close()
    stream.close()
    assert raw.closed == 1


def test_gemini_stream_preserves_distinct_calls_and_deduplicates_snapshots():
    def event(call_id, path):
        return {"candidates": [{"content": {"parts": [{"functionCall": {
            "id": call_id, "name": "read_file", "args": {"path": path}
        }}]}}]}

    events = [event("one", "a.py"), event("one", "a.py"), event("two", "b.py")]
    calls = [call for item in GeminiProvider._normalize_stream(events)
             for call in item.choices[0].delta.tool_calls]
    assert [(call.index, call.id, call.function.name, json.loads(call.function.arguments))
            for call in calls] == [
                (0, "one", "read_file", {"path": "a.py"}),
                (1, "two", "read_file", {"path": "b.py"}),
            ]
    with pytest.raises(ValueError):
        list(GeminiProvider._normalize_stream([event("one", "a.py"), event("one", "b.py")]))


def test_provider_owner_closes_raw_when_adapter_is_executing():
    import threading
    from nz_coder.providers.http import OwnedStream

    entered, release = threading.Event(), threading.Event()
    closed = []
    def adapter():
        entered.set()
        release.wait(2)
        yield 1
    stream = OwnedStream(adapter(), close_owner=lambda: (closed.append(True), release.set()))
    worker = threading.Thread(target=lambda: next(stream))
    worker.start()
    try:
        assert entered.wait(2)
        with pytest.raises(ValueError, match="already executing"):
            stream.close()
        worker.join(2)
        assert not worker.is_alive()
        stream.close()
        assert closed == [True]
    finally:
        release.set()
        worker.join(2)


def test_native_owner_retries_close_after_open_finishes():
    import threading
    entered, release = threading.Event(), threading.Event()
    class Response:
        closed = False
        def __iter__(self):
            return iter([b'data: {"value": 1}\n', b'\n'])
        def close(self):
            self.closed = True
    response = Response()
    class Transport(UrllibTransport):
        def _open(self, *args):
            entered.set()
            release.wait(2)
            return response
    stream = Transport().post_sse("url", {}, {})
    worker = threading.Thread(target=lambda: next(stream, None))
    worker.start()
    try:
        assert entered.wait(2)
        with pytest.raises(BaseException):
            stream.close()
        release.set()
        worker.join(2)
        assert not worker.is_alive()
        stream.close()
        assert response.closed
    finally:
        release.set()
        worker.join(2)


def test_provider_iteration_does_not_retry_close_before_worker_cleanup():
    from nz_coder.providers.http import OwnedStream
    attempts = []
    def close():
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("close failed")
    stream = OwnedStream(iter(()), close_owner=close)
    assert list(stream) == []
    assert attempts == []
    with pytest.raises(OSError, match="close failed"):
        stream.close()
    assert len(attempts) == 1
    stream.close()
    assert len(attempts) == 2


def test_gemini_late_thought_signature_survives_gateway_aggregation():
    from tests.runtime.model_gateway.test_streaming_gateway import _call, _gateway
    def event(signature=None):
        part = {"functionCall": {"id": "call-one", "name": "read_file", "args": {"path": "a.py"}}}
        if signature:
            part["thoughtSignature"] = signature
        return {"candidates": [{"content": {"parts": [part]}}]}
    stream = GeminiProvider._normalize_stream([event(), event("signed"), event("signed"),
        {"candidates": [{"finishReason": "STOP"}]}])
    gateway, _ = _gateway([stream])
    events = []
    outcome = gateway.complete_stream_sync(_call(), on_event=events.append)
    assert len(outcome.tool_calls) == 1
    call = outcome.tool_calls[0]
    assert call["id"] == "call-one"
    assert call["function"]["name"] == "read_file"
    assert json.loads(call["function"]["arguments"]) == {"path": "a.py"}
    assert call["provider_extra"]["thoughtSignature"] == "signed"
    assert sum(event.kind == "tool_delta" for event in events) == 2
