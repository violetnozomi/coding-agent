"""Tests for model capability resolution and runtime request policy."""
from __future__ import annotations

import os
from dataclasses import replace

import pytest

from nz_coder.providers import (
    OpenAICompatibleProvider,
    configured_model_capabilities,
    load_model_catalog_file,
    prepare_openai_request,
    resolve_model_capabilities,
)
from nz_coder.runtime.execution.loop import AgentLoop, LLMResult
from nz_coder.runtime.conversation.prompt import build


class _FakeCompletions:
    def __init__(self):
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return kwargs


class _FakeClient:
    def __init__(self):
        self.chat = type("Chat", (), {"completions": _FakeCompletions()})()


def test_registry_resolves_family_limits_and_request_semantics():
    qwen = resolve_model_capabilities("openai-compatible", "qwen-plus")
    codex = resolve_model_capabilities("openai-compatible", "gpt-5.3-codex")
    gpt_chat = resolve_model_capabilities(
        "openai-compatible",
        "gpt-5.2-chat-latest",
    )
    gemini_image = resolve_model_capabilities("gemini", "gemini-2.5-flash-image")
    gpt_vision = resolve_model_capabilities("openai", "gpt-4o-mini")
    qwen_vision = resolve_model_capabilities("dashscope", "qwen2.5-vl-72b")

    assert (qwen.family, qwen.prompt_family) == ("qwen", "qwen")
    assert (qwen.context_tokens, qwen.output_tokens) == (1_000_000, 32_768)
    assert qwen.preserve_reasoning_content is True
    assert codex.prompt_family == "codex"
    assert codex.supports_reasoning is True
    assert codex.supports_image_input is True
    assert codex.supports_temperature is False
    assert codex.max_tokens_parameter == "max_completion_tokens"
    assert gpt_chat.supports_temperature is True
    assert gpt_chat.max_tokens_parameter == "max_tokens"
    assert gemini_image.prompt_family == "gemini"
    assert gemini_image.supports_tools is False
    assert gemini_image.supports_image_input is True
    assert gpt_vision.supports_image_input is True
    assert qwen_vision.supports_image_input is True


def test_registry_explicit_limits_and_json_overrides_take_precedence():
    capability = resolve_model_capabilities(
        "private-gateway",
        "custom-coder",
        context_tokens=64_000,
        output_tokens=12_000,
        overrides={
            "family": "private",
            "prompt_family": "codex",
            "supports_reasoning": True,
            "supports_streaming": False,
        },
    )

    assert capability.context_tokens == 64_000
    assert capability.output_tokens == 12_000
    assert capability.family == "private"
    assert capability.prompt_family == "codex"
    assert capability.supports_reasoning is True
    assert capability.supports_streaming is False
    assert capability.source == "fallback+override"


def test_exact_local_catalog_record_precedes_fallback_and_selects_variant():
    capability = resolve_model_capabilities(
        "openai-compatible",
        "private-coder-v2",
        catalog={
            "models": {
                "openai-compatible/private-coder-v2": {
                    "family": "private-coder",
                    "prompt_family": "codex",
                    "context_tokens": 196_000,
                    "output_tokens": 48_000,
                    "supports_reasoning": True,
                    "variants": {
                        "low": {"reasoning_effort": "low"},
                        "high": {"reasoning_effort": "high"},
                    },
                }
            }
        },
        variant="high",
    )

    assert capability.family == "private-coder"
    assert capability.prompt_family == "codex"
    assert capability.context_tokens == 196_000
    assert capability.output_tokens == 48_000
    assert capability.source == (
        "catalog:openai-compatible/private-coder-v2"
    )
    assert capability.available_variants == ("high", "low")
    assert capability.selected_variant == "high"
    request = prepare_openai_request(
        capability,
        {"model": capability.model_id, "messages": []},
    )
    assert request["reasoning_effort"] == "high"


def test_builtin_qwen_variant_maps_to_openai_extra_body():
    capability = resolve_model_capabilities(
        "openai-compatible",
        "qwen-plus",
        variant="thinking",
    )

    request = prepare_openai_request(
        capability,
        {
            "model": capability.model_id,
            "messages": [],
            "extra_body": {"trace": False},
        },
    )

    assert capability.available_variants == ("instant", "thinking")
    assert request["extra_body"] == {
        "trace": False,
        "enable_thinking": True,
    }


def test_named_openai_compatible_provider_uses_its_exact_catalog_key():
    capability = resolve_model_capabilities(
        "deepseek",
        "private-reasoner",
        catalog={
            "models": {
                "deepseek/private-reasoner": {
                    "supports_reasoning": True,
                    "variants": {
                        "high": {"reasoning_effort": "high"},
                    },
                }
            }
        },
        variant="high",
    )

    assert capability.provider == "deepseek"
    assert capability.source == "catalog:deepseek/private-reasoner"
    assert prepare_openai_request(capability, {})["reasoning_effort"] == "high"


def test_openai_wire_adds_empty_reasoning_to_runtime_assistant_for_deepseek():
    """Late Runtime messages must satisfy DeepSeek's replay wire contract."""
    capabilities = resolve_model_capabilities(
        "openai-compatible",
        "deepseek-v4-flash",
    )
    provider = OpenAICompatibleProvider(
        api_key="test-key",
        base_url="https://example.invalid",
    )
    client = _FakeClient()
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "last-call guidance"},
    ]

    provider.create_completion(
        client,
        model="deepseek-v4-flash",
        messages=messages,
        _capabilities=capabilities,
    )

    wire = client.chat.completions.requests[0]["messages"]
    assert wire[-1]["reasoning_content"] == ""
    assert "reasoning_content" not in messages[-1]


def test_openai_wire_does_not_add_reasoning_to_non_replay_model():
    """Strict OpenAI-compatible endpoints must not receive unknown fields."""
    capabilities = resolve_model_capabilities(
        "openai-compatible",
        "gpt-4o",
    )
    provider = OpenAICompatibleProvider(
        api_key="test-key",
        base_url="https://example.invalid",
    )
    client = _FakeClient()

    provider.create_completion(
        client,
        model="gpt-4o",
        messages=[
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "last-call guidance"},
        ],
        _capabilities=capabilities,
    )

    wire = client.chat.completions.requests[0]["messages"]
    assert "reasoning_content" not in wire[-1]


def test_registry_rejects_unknown_variant_and_unsafe_variant_fields():
    with pytest.raises(ValueError, match="Unknown model variant"):
        resolve_model_capabilities(
            "openai-compatible",
            "qwen-plus",
            variant="maximum",
        )
    with pytest.raises(ValueError, match="Unknown option"):
        resolve_model_capabilities(
            "openai-compatible",
            "private",
            catalog={
                "models": {
                    "openai-compatible/private": {
                        "variants": {"bad": {"model": "other"}},
                    }
                }
            },
            variant="bad",
        )


@pytest.mark.parametrize(
    ("provider", "options", "message"),
    [
        (
            "openai-compatible",
            {"extra_body": {"model": "evil"}},
            "Unknown extra_body",
        ),
        ("openai-compatible", {"extra_body": []}, "must be an object"),
        ("openai-compatible", {"reasoning_effort": {}}, "must be a string"),
        ("openai-compatible", {"top_p": "high"}, "between 0 and 1"),
        ("anthropic", {"thinking": []}, "must be an object"),
        ("anthropic", {"effort": "extreme"}, "effort is invalid"),
        ("gemini", {"thinking_config": []}, "must be an object"),
    ],
)
def test_registry_rejects_unsafe_or_mistyped_variant_options(
    provider,
    options,
    message,
):
    with pytest.raises(ValueError, match=message):
        resolve_model_capabilities(
            provider,
            "private",
            catalog={
                "models": {
                    f"{provider}/private": {
                        "supports_reasoning": True,
                        "variants": {"bad": options},
                    }
                }
            },
            variant="bad",
        )


def test_final_override_disables_builtin_variants_and_explicit_empty_is_kept():
    with pytest.raises(ValueError, match="does not support reasoning variants"):
        resolve_model_capabilities(
            "openai-compatible",
            "qwen-plus",
            overrides={"supports_reasoning": False},
            variant="thinking",
        )
    capability = resolve_model_capabilities(
        "openai-compatible",
        "qwen-plus",
        catalog={
            "models": {
                "openai-compatible/qwen-plus": {"variants": {}},
            }
        },
    )
    assert capability.available_variants == ()


def test_local_catalog_file_is_workspace_bounded(tmp_path):
    catalog_path = tmp_path / ".nz-coder" / "models.json"
    catalog_path.parent.mkdir()
    catalog_path.write_text(
        '{"models":{"openai-compatible/local":{"context_tokens":64000}}}',
        encoding="utf-8",
    )

    catalog = load_model_catalog_file(
        ".nz-coder/models.json",
        workspace=tmp_path,
    )

    assert catalog["models"]["openai-compatible/local"][
        "context_tokens"
    ] == 64_000
    with pytest.raises(ValueError, match="escapes workspace"):
        load_model_catalog_file("../outside.json", workspace=tmp_path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_local_catalog_rejects_non_regular_file_without_blocking(tmp_path):
    fifo = tmp_path / "catalog.pipe"
    os.mkfifo(fifo)

    with pytest.raises(ValueError, match="regular file"):
        load_model_catalog_file(fifo, workspace=tmp_path)


def test_configured_registry_loads_catalog_path(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.process.workdir import scoped_workdir

    catalog_path = tmp_path / "models.json"
    catalog_path.write_text(
        '{"models":{"openai-compatible/local":{"context_tokens":75000}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "MODEL_CATALOG_JSON", "")
    monkeypatch.setattr(config, "MODEL_CATALOG_PATH", "models.json")
    monkeypatch.setattr(config, "MODEL_VARIANT", "")

    with scoped_workdir(tmp_path):
        capability = configured_model_capabilities(
            "openai-compatible",
            "local",
        )

    assert capability.context_tokens == 75_000
    assert capability.source == "catalog:openai-compatible/local"


def test_configured_variant_only_applies_to_active_model(monkeypatch):
    from nz_coder.foundation import config

    monkeypatch.setattr(config, "MODEL_ID", "qwen-plus")
    monkeypatch.setattr(config, "MODEL_VARIANT", "thinking")
    monkeypatch.setattr(config, "MODEL_CATALOG_JSON", "")
    monkeypatch.setattr(config, "MODEL_CATALOG_PATH", "")

    active = configured_model_capabilities(
        "openai-compatible",
        "qwen-plus",
    )
    child = configured_model_capabilities(
        "openai-compatible",
        "gpt-4o",
    )

    assert active.selected_variant == "thinking"
    assert child.selected_variant is None


def test_configured_registry_prefers_explicit_environment_limits(monkeypatch):
    from nz_coder.foundation import config

    monkeypatch.setenv("MAX_CONTEXT_TOKENS", "64000")
    monkeypatch.setenv("MAX_OUTPUT_TOKENS", "12000")
    monkeypatch.setattr(config, "MAX_CONTEXT_TOKENS", 64_000)
    monkeypatch.setattr(config, "MAX_OUTPUT_TOKENS", 12_000)
    monkeypatch.setattr(
        config,
        "MODEL_CAPABILITIES_JSON",
        '{"supports_streaming": false}',
    )

    capability = configured_model_capabilities(
        "openai-compatible",
        "qwen-plus",
    )

    assert capability.context_tokens == 64_000
    assert capability.output_tokens == 12_000
    assert capability.supports_streaming is False
    assert capability.source == "builtin:qwen-plus+override"


@pytest.mark.parametrize(
    "overrides, message",
    [
        ('{"supports_tools": "yes"}', "must be boolean"),
        ('{"unknown": true}', "Unknown model capability"),
        ('[]', "must decode to an object"),
    ],
)
def test_registry_rejects_invalid_overrides(overrides, message):
    with pytest.raises(ValueError, match=message):
        resolve_model_capabilities("test", "model", overrides=overrides)


def test_openai_request_policy_maps_gpt5_tokens_and_drops_temperature():
    capability = resolve_model_capabilities(
        "openai-compatible",
        "gpt-5.3-codex",
    )
    request = prepare_openai_request(
        capability,
        {
            "model": capability.model_id,
            "messages": [],
            "max_tokens": 12_345,
            "temperature": 0.7,
            "tools": [{"type": "function"}],
        },
    )

    assert request["max_completion_tokens"] == 12_345
    assert "max_tokens" not in request
    assert "temperature" not in request
    assert request["tools"] == [{"type": "function"}]


def test_openai_provider_applies_capabilities_before_delegation():
    client = _FakeClient()
    provider = OpenAICompatibleProvider(
        api_key="key",
        base_url="https://example.test/v1",
        client_factory=lambda **kwargs: client,
    )

    provider.create_completion(
        client,
        model="gpt-5-codex",
        messages=[{"role": "user", "content": "fix"}],
        max_tokens=9_000,
        temperature=0.5,
    )

    request = client.chat.completions.requests[0]
    assert request["max_completion_tokens"] == 9_000
    assert "max_tokens" not in request
    assert "temperature" not in request


def test_openai_provider_uses_agent_capability_snapshot():
    client = _FakeClient()
    provider = OpenAICompatibleProvider(
        api_key="key",
        base_url="https://example.test/v1",
        client_factory=lambda **kwargs: client,
    )
    snapshot = resolve_model_capabilities(
        "openai-compatible",
        "qwen-plus",
        variant="thinking",
    )

    provider.create_completion(
        client,
        _capabilities=snapshot,
        model="qwen-plus",
        messages=[],
    )

    assert client.chat.completions.requests[0]["extra_body"] == {
        "enable_thinking": True,
    }
    assert "_capabilities" not in client.chat.completions.requests[0]


def test_prompt_builder_selects_model_family_appendix(tmp_path):
    capability = resolve_model_capabilities(
        "openai-compatible",
        "gpt-5-codex",
    )

    prompt = build(capabilities=capability)

    assert "## Model-family guidance" in prompt
    assert "Family: codex" in prompt
    assert "small verified patches" in prompt


def test_kimi_uses_infcode_kimi_prompt_contract():
    capability = resolve_model_capabilities(
        "openai-compatible",
        "kimi-k2.5",
    )

    prompt = build(capabilities=capability)

    assert capability.prompt_family == "kimi"
    assert "Family: kimi" in prompt
    assert "same language as the user" in prompt
    assert "Parallelize independent tool calls" in prompt


def test_agent_appends_guidance_from_its_own_capability_snapshot(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir

    capability = resolve_model_capabilities(
        "openai-compatible",
        "qwen-plus",
        variant="thinking",
    )

    class Provider:
        name = "snapshot"

        def capabilities(self, _model_id):
            return capability

        def create_client(self):
            return _FakeClient()

    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "base prompt",
            provider=Provider(),
            trace_enabled=False,
        )
    try:
        assert "Family: qwen" in agent.system_prompt
        assert agent.model_capabilities is capability
    finally:
        agent.close()


def test_agent_budget_uses_model_capability_limits():
    agent = AgentLoop.__new__(AgentLoop)
    agent.model_capabilities = resolve_model_capabilities(
        "test",
        "custom",
        context_tokens=64_000,
        output_tokens=32_000,
    )

    budget = agent._prompt_budget()

    assert budget.context_tokens == 64_000
    assert budget.output_reserve_tokens == 16_000
    assert budget.usable_input_tokens == 48_000


def test_agent_falls_back_to_non_streaming_when_model_declares_no_stream():
    class Tracer:
        def __init__(self):
            self.events = []

        def log(self, event, **payload):
            self.events.append((event, payload))

    agent = AgentLoop.__new__(AgentLoop)
    agent.model_id = "custom-no-stream"
    capability = resolve_model_capabilities("test", agent.model_id)
    agent.model_capabilities = replace(capability, supports_streaming=False)
    agent.tracer = Tracer()
    agent._call_streaming = lambda *args, **kwargs: LLMResult(content="stream")
    agent._call_non_streaming = lambda *args, **kwargs: LLMResult(content="complete")

    result = agent._call_llm([], stream=True)

    assert result.content == "complete"
    assert agent.tracer.events[0][0] == "provider_capability_fallback"


def test_reasoning_history_policy_is_model_aware():
    qwen_agent = AgentLoop.__new__(AgentLoop)
    qwen_agent.model_capabilities = resolve_model_capabilities(
        "openai-compatible",
        "qwen-plus",
    )
    gpt_agent = AgentLoop.__new__(AgentLoop)
    gpt_agent.model_capabilities = resolve_model_capabilities(
        "openai-compatible",
        "gpt-4o",
    )
    message = {
        "role": "assistant",
        "content": "tooling",
        "_nz_provider_id": "openai-compatible",
        "_nz_model_id": "qwen-plus",
        "reasoning_content": "provider-state",
    }

    assert qwen_agent._sanitize_messages([message])[0]["reasoning_content"] == (
        "provider-state"
    )
    assert "reasoning_content" not in gpt_agent._sanitize_messages([message])[0]

    synthetic_tool_turn = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "synthetic-call",
            "type": "function",
            "function": {"name": "bash", "arguments": "{}"},
        }],
    }
    assert qwen_agent._sanitize_messages(
        [synthetic_tool_turn],
    )[0]["reasoning_content"] == ""
    assert "reasoning_content" not in gpt_agent._sanitize_messages(
        [synthetic_tool_turn],
    )[0]
