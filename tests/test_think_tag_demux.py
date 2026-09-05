"""Tests for InfCode-compatible tagged reasoning stream demultiplexing."""
from __future__ import annotations

import asyncio
import pytest

from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.providers.normalized import chunk, completion
from nz_coder.runtime.execution.loop import AgentLoop
from nz_coder.runtime.conversation.think_tags import ThinkTagDemux, demux_think_tags
from nz_coder.runtime.process.workdir import scoped_workdir


@pytest.mark.parametrize(
    ("parts", "visible", "reasoning"),
    [
        (["  <thi", "nk>inspect", " carefully</th", "ink>\n  Answer"], "Answer", "inspect carefully"),
        (["<thinking>", "deep", " thought"], "", "deep thought"),
        (["<thi"], "<thi", ""),
        (["ordinary </thi", "nk> text"], "ordinary  text", ""),
        (["prefix <think>not leading</think>"], "prefix <think>not leading", ""),
    ],
)
def test_incremental_demux_matches_leading_tag_contract(parts, visible, reasoning):
    state = ThinkTagDemux()
    events = []
    for part in parts:
        events.extend(state.push(part))
    events.extend(state.finish())

    assert "".join(item.text for item in events if item.type == "text-delta") == visible
    assert "".join(item.text for item in events if item.type == "reasoning-delta") == reasoning


def test_complete_demux_supports_long_tag_and_unclosed_reasoning():
    assert demux_think_tags("\n<thinking>plan\nstep") == ("", "plan\nstep")


class _Recovery:
    def record_success(self):
        return None


class _Provider:
    name = "openai-compatible"

    def __init__(self, response):
        self.response = response

    def create_completion(self, _client, **_kwargs):
        return self.response


def _agent(response):
    agent = AgentLoop.__new__(AgentLoop)
    agent.provider = _Provider(response)
    agent.client = object()
    agent.recovery = _Recovery()
    agent.model_id = "test-model"
    agent.model_capabilities = None
    return agent


def test_streaming_agent_routes_split_tags_to_reasoning():
    agent = _agent(iter([
        chunk(content=" <thi"),
        chunk(content="nk>private"),
        chunk(content=" chain</think>\nFinal"),
        chunk(finish_reason="stop", usage={
            "input_tokens": 8,
            "output_tokens": 4,
            "total_tokens": 12,
        }),
    ]))

    result = agent._call_streaming([{"role": "user", "content": "answer"}])

    assert result.content == "Final"
    assert result.extra["reasoning_content"] == "private chain"
    assert (result.input_tokens, result.output_tokens, result.total_tokens) == (8, 4, 12)


def test_non_streaming_agent_combines_native_and_tagged_reasoning():
    response = completion(
        content="<think>tagged</think> visible",
        reasoning_content="native; ",
        finish_reason="stop",
    )
    agent = _agent(response)

    result = agent._call_non_streaming([{"role": "user", "content": "answer"}])

    assert result.content == "visible"
    assert result.extra["reasoning_content"] == "native; tagged"


def test_full_agent_run_persists_demuxed_text_and_reasoning_parts(tmp_path):
    class Provider:
        name = "openai-compatible"

        def create_client(self):
            return object()

        def capabilities(self, model_id):
            return ModelCapabilities(provider=self.name, model_id=model_id)

        def create_completion(self, _client, **_kwargs):
            return iter([
                chunk(content="<think>inspect"),
                chunk(content=" state</think>\nReady"),
                chunk(
                    finish_reason="stop",
                    usage={
                        "input_tokens": 5,
                        "output_tokens": 3,
                        "total_tokens": 8,
                    },
                ),
            ])

    messages = [{"role": "user", "content": "answer"}]
    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "system",
            permission_mode="auto",
            provider=Provider(),
            trace_enabled=False,
        )
        result = asyncio.run(agent.run(messages, stream=True))
        agent.close()

    assistant = next(item for item in messages if item.get("role") == "assistant")
    text_parts = [part for part in assistant["_nz_parts"] if part["type"] == "text"]
    reasoning_parts = [
        part for part in assistant["_nz_parts"] if part["type"] == "reasoning"
    ]
    assert result["status"] == "completed"
    assert assistant["content"] == "Ready"
    private_reasoning = assistant["_nz_provider_reasoning_content"]
    assert private_reasoning["schema"] == "nz.provider_private_state.v2"
    assert private_reasoning["provider_instance_id"].startswith(
        "provider-instance-"
    )
    assert private_reasoning["provider_id"] == "openai-compatible"
    assert private_reasoning["payload"] == "inspect state"
    assert [part["text"] for part in text_parts] == ["Ready"]
    assert [part["text"] for part in reasoning_parts] == ["inspect state"]
    assert all(part["internal"] is True for part in reasoning_parts)
    assert all(part["visible"] is False for part in reasoning_parts)
    assert all("<think>" not in part.get("text", "") for part in assistant["_nz_parts"])
