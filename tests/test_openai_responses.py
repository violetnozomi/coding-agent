"""Offline contracts for the native OpenAI Responses provider."""
from __future__ import annotations

import json

import pytest

from nz_coder.foundation import config
from nz_coder.providers import OpenAIResponsesProvider, create_provider
from nz_coder.providers.capabilities import resolve_model_capabilities
from nz_coder.runtime.execution.loop import AgentLoop


class _FakeResponses:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.response


class _FakeClient:
    def __init__(self, response):
        self.responses = _FakeResponses(response)


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


def _history():
    return [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "Inspect README."},
        {
            "role": "assistant",
            "content": "",
            "provider_extra": {
                "openai_reasoning_items": [
                    {
                        "type": "reasoning",
                        "id": "rs-old",
                        "encrypted_content": "encrypted",
                        "summary": [],
                    }
                ]
            },
            "tool_calls": [
                {
                    "id": "call-old",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"README.md"}',
                    },
                    "provider_extra": {
                        "openai_response_item_id": "fc-old",
                        "openai_reasoning_items": [
                            {
                                "type": "reasoning",
                                "id": "rs-old",
                                "encrypted_content": "encrypted",
                                "summary": [],
                            }
                        ],
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-old", "content": "contents"},
    ]


def test_non_streaming_translates_history_tools_and_response():
    client = _FakeClient(
        {
            "status": "completed",
            "usage": {"input_tokens": 41, "output_tokens": 13, "total_tokens": 54},
            "output": [
                {
                    "type": "reasoning",
                    "id": "rs-new",
                    "encrypted_content": "next-encrypted",
                    "summary": [{"type": "summary_text", "text": "Inspect first."}],
                },
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Reading."}],
                },
                {
                    "type": "function_call",
                    "id": "fc-new",
                    "call_id": "call-new",
                    "name": "read_file",
                    "arguments": '{"path":"pyproject.toml"}',
                },
            ],
        }
    )
    provider = OpenAIResponsesProvider(
        api_key="secret",
        base_url="https://api.openai.test/v1",
    )
    response = provider.create_completion(
        client,
        model="gpt-5-codex",
        messages=_history(),
        tools=[_tool_spec()],
        tool_choice={"type": "function", "function": {"name": "read_file"}},
        max_tokens=123,
    )

    request = client.responses.requests[0]
    assert request["store"] is False
    assert request["include"] == ["reasoning.encrypted_content"]
    assert request["max_output_tokens"] == 123
    assert request["tools"][0] == {
        "type": "function",
        "name": "read_file",
        "description": "Read a file",
        "parameters": _tool_spec()["function"]["parameters"],
    }
    assert request["tool_choice"] == {"type": "function", "name": "read_file"}
    assert request["input"][2]["id"] == "rs-old"
    assert request["input"][3] == {
        "type": "function_call",
        "call_id": "call-old",
        "name": "read_file",
        "arguments": '{"path":"README.md"}',
        "id": "fc-old",
    }
    assert request["input"][4] == {
        "type": "function_call_output",
        "call_id": "call-old",
        "output": "contents",
    }

    message = response.choices[0].message
    assert message.content == "Reading."
    assert message.reasoning_content == "Inspect first."
    assert message.provider_extra["openai_reasoning_items"][0]["id"] == "rs-new"
    tool_call = message.tool_calls[0].model_dump()
    assert tool_call["id"] == "call-new"
    assert json.loads(tool_call["function"]["arguments"]) == {
        "path": "pyproject.toml",
    }
    assert tool_call["provider_extra"]["openai_response_item_id"] == "fc-new"
    assert tool_call["provider_extra"]["openai_reasoning_items"][0]["id"] == "rs-new"
    assert response.choices[0].finish_reason == "tool_calls"
    assert response.usage["total_tokens"] == 54


def test_incomplete_max_output_response_preserves_length_and_usage():
    client = _FakeClient({
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_tokens_details": {"cached_tokens": 4},
            "output_tokens_details": {"reasoning_tokens": 2},
        },
        "output": [{
            "type": "message",
            "content": [{"type": "output_text", "text": "partial"}],
        }],
    })
    provider = OpenAIResponsesProvider(api_key="key")

    response = provider.create_completion(
        client,
        model="gpt-5-codex",
        messages=[{"role": "user", "content": "answer"}],
    )

    assert response.choices[0].message.content == "partial"
    assert response.choices[0].finish_reason == "length"
    assert response.usage["total_tokens"] == 15
    assert response.usage["cache_read_input_tokens"] == 4
    assert response.usage["uncached_input_tokens"] == 6
    assert response.usage["reasoning_tokens"] == 2


def test_incomplete_max_output_stream_emits_terminal_chunk():
    events = iter([{
        "type": "response.incomplete",
        "response": {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "usage": {"input_tokens": 12, "output_tokens": 6, "total_tokens": 18},
        },
    }])
    provider = OpenAIResponsesProvider(api_key="key")

    chunks = list(provider.create_completion(
        _FakeClient(events),
        model="gpt-5-codex",
        messages=[{"role": "user", "content": "answer"}],
        stream=True,
    ))

    assert chunks[-1].choices[0].finish_reason == "length"
    assert chunks[-1].usage["total_tokens"] == 18


def test_stream_normalizes_text_reasoning_and_partial_function_call():
    events = iter(
        [
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "type": "reasoning",
                    "id": "rs-stream",
                    "encrypted_content": "ciphertext",
                    "summary": [],
                },
            },
            {
                "type": "response.reasoning_summary_text.delta",
                "output_index": 0,
                "delta": "Check.",
            },
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {
                    "type": "function_call",
                    "id": "fc-stream",
                    "call_id": "call-stream",
                    "name": "read_file",
                    "arguments": "",
                },
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 1,
                "delta": '{"path":',
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 1,
                "delta": '"README.md"}',
            },
            {
                "type": "response.output_item.done",
                "output_index": 1,
                "item": {
                    "type": "function_call",
                    "id": "fc-stream",
                    "call_id": "call-stream",
                    "name": "read_file",
                    "arguments": '{"path":"README.md"}',
                },
            },
            {"type": "response.output_text.delta", "output_index": 2, "delta": "Done"},
        ]
    )
    client = _FakeClient(events)
    provider = OpenAIResponsesProvider(api_key="key")

    chunks = list(
        provider.create_completion(
            client,
            model="gpt-5-codex",
            messages=[{"role": "user", "content": "read"}],
            tools=[_tool_spec()],
            stream=True,
        )
    )

    assert chunks[0].choices[0].delta.provider_extra[
        "openai_reasoning_items"
    ][0]["id"] == "rs-stream"
    assert chunks[1].choices[0].delta.reasoning_content == "Check."
    tool_chunks = [
        item.choices[0].delta.tool_calls[0]
        for item in chunks
        if item.choices[0].delta.tool_calls
    ]
    assert tool_chunks[0].id == "call-stream"
    assert tool_chunks[0].function.name == "read_file"
    assert tool_chunks[0].provider_extra["openai_reasoning_items"][0]["id"] == "rs-stream"
    assert "".join(call.function.arguments for call in tool_chunks) == '{"path":"README.md"}'
    assert chunks[-1].choices[0].delta.content == "Done"


def test_agent_loop_accumulates_responses_stream_tool_metadata(monkeypatch):
    monkeypatch.setattr(config, "MODEL_ID", "gpt-5-codex")
    events = iter(
        [
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "type": "reasoning",
                    "id": "rs-loop",
                    "encrypted_content": "ciphertext",
                    "summary": [],
                },
            },
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {
                    "type": "function_call",
                    "id": "fc-loop",
                    "call_id": "call-loop",
                    "name": "read_file",
                },
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 1,
                "delta": '{"path":"README.md"}',
            },
        ]
    )
    provider = OpenAIResponsesProvider(api_key="key")
    client = _FakeClient(events)

    class Recovery:
        def record_success(self):
            return None

    agent = AgentLoop.__new__(AgentLoop)
    agent.provider = provider
    agent.client = client
    agent.recovery = Recovery()
    agent.model_id = "gpt-5-codex"
    agent.model_capabilities = resolve_model_capabilities(
        "openai-responses",
        "gpt-5-codex",
    )
    result = agent._call_streaming([{"role": "user", "content": "read"}])

    assert result.extra["provider_extra"]["openai_reasoning_items"][0]["id"] == "rs-loop"
    assert result.tool_calls == [
        {
            "id": "call-loop",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": '{"path":"README.md"}',
            },
            "provider_extra": {
                "openai_response_item_id": "fc-loop",
                "openai_reasoning_items": [
                    {
                        "type": "reasoning",
                        "id": "rs-loop",
                        "encrypted_content": "ciphertext",
                        "summary": [],
                    }
                ],
            },
        }
    ]


@pytest.mark.parametrize("name", ["openai-responses", "openai_responses", "codex"])
def test_create_provider_accepts_responses_aliases(name):
    provider = create_provider(
        name,
        api_key="key",
        base_url="https://api.openai.test/v1",
        client_factory=lambda **kwargs: kwargs,
    )
    assert isinstance(provider, OpenAIResponsesProvider)
    expected = "openai-responses" if name == "openai_responses" else name
    assert provider.name == expected


def test_failed_response_and_unsupported_option_are_explicit():
    provider = OpenAIResponsesProvider(api_key="key")
    failed = _FakeClient(
        {
            "status": "failed",
            "error": {"message": "bad request"},
            "output": [],
        }
    )
    with pytest.raises(RuntimeError, match="bad request"):
        provider.create_completion(
            failed,
            model="gpt-5-codex",
            messages=[{"role": "user", "content": "hello"}],
        )

    with pytest.raises(ValueError, match="Unsupported OpenAI Responses option"):
        provider.create_completion(
            _FakeClient({"status": "completed", "output": []}),
            model="gpt-5-codex",
            messages=[{"role": "user", "content": "hello"}],
            unsupported=True,
        )
