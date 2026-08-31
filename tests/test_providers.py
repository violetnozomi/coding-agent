"""Tests for model provider selection and delegation."""
from __future__ import annotations

import pytest

from nz_coder.providers import OpenAICompatibleProvider, create_provider
from nz_coder.runtime.execution.loop import AgentLoop
from nz_coder.runtime.agent.subagent import _completion_with_timeout
from nz_coder.runtime.process.workdir import scoped_workdir


class _FakeCompletions:
    def __init__(self):
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return "response"


class _FakeClient:
    def __init__(self):
        self.chat = type("Chat", (), {"completions": _FakeCompletions()})()


def test_openai_compatible_provider_creates_client_and_delegates_completion():
    created = []

    def factory(**kwargs):
        created.append(kwargs)
        return _FakeClient()

    provider = OpenAICompatibleProvider(
        api_key="secret",
        base_url="https://example.test/v1",
        client_factory=factory,
    )
    client = provider.create_client()
    response = provider.create_completion(
        client,
        model="test-model",
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
    )

    assert created == [
        {"api_key": "secret", "base_url": "https://example.test/v1"},
    ]
    assert response == "response"
    assert client.chat.completions.requests == [
        {
            "model": "test-model",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        },
    ]


@pytest.mark.parametrize(
    "name",
    [
        "openai",
        "openai-compatible",
        "openai_compatible",
        "dashscope",
        "deepseek",
        "kimi",
        "openrouter",
    ],
)
def test_create_provider_accepts_openai_compatible_aliases(name):
    provider = create_provider(
        name,
        api_key="key",
        base_url="https://example.test/v1",
        client_factory=lambda **kwargs: kwargs,
    )
    assert isinstance(provider, OpenAICompatibleProvider)
    expected = "openai-compatible" if name == "openai_compatible" else name
    assert provider.name == expected


def test_create_provider_rejects_unknown_provider():
    with pytest.raises(ValueError, match="Unknown model provider"):
        create_provider("unsupported-provider")


def test_agent_loop_uses_injected_provider_to_create_client(tmp_path):
    client = _FakeClient()

    class Provider:
        name = "test"

        def __init__(self):
            self.create_count = 0

        def create_client(self):
            self.create_count += 1
            return client

        def create_completion(self, client, **kwargs):
            return client.chat.completions.create(**kwargs)

    provider = Provider()
    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "test",
            permission_mode="auto",
            provider=provider,
            trace_enabled=False,
        )

    assert provider.create_count == 1
    assert agent.provider is provider
    assert agent.client is client


def test_subagent_timeout_helper_delegates_to_provider():
    calls = []

    class Provider:
        def create_completion(self, client, **kwargs):
            calls.append((client, kwargs))
            return "response"

    client = object()
    response = _completion_with_timeout(
        client,
        timeout_seconds=0,
        provider=Provider(),
        model="test-model",
    )

    assert response == "response"
    assert calls == [(client, {"model": "test-model"})]
