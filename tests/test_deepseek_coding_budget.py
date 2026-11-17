"""当前 DeepSeek 模型的主调用额度必须抵达真实 Provider 请求边界。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nz_coder.providers import OpenAICompatibleProvider, resolve_model_capabilities
from nz_coder.providers.normalized import chunk, completion
from nz_coder.runtime.execution.loop import AgentLoop
from nz_coder.runtime.model_gateway import ResolvedModelRuntime
from nz_coder.runtime.process.workdir import scoped_workdir


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(("model_id", "expected_context"), [
    ("deepseek-flash", 1_048_576),
    ("deepseek-v4-flash", 1_000_000),
    ("deepseek-v4-pro", 1_000_000),
])
@pytest.mark.parametrize(
    ("selection", "expected_output"),
    [("default", 256_000), ("explicit", 8192), ("catalog", 128_000)],
)
def test_deepseek_flash_main_request_uses_model_budget(
    tmp_path, stream, selection, expected_output, model_id, expected_context,
):
    requests = []

    def create(**request):
        requests.append(request)
        if request.get("stream"):
            return iter([chunk(content="done", finish_reason="stop")])
        return completion(content="done", finish_reason="stop")

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create),
    ))
    provider = OpenAICompatibleProvider(
        api_key="test-only", base_url="https://example.test/v1",
        client_factory=lambda **_kwargs: client,
    )
    options = {}
    if selection == "explicit":
        options["output_tokens"] = 8192
    elif selection == "catalog":
        options["catalog"] = {"models": {f"openai-compatible/{model_id}": {
            "context_tokens": expected_context, "output_tokens": 128_000,
        }}}
    capability = resolve_model_capabilities(
        "openai-compatible", model_id, **options,
    )
    runtime = ResolvedModelRuntime(
        provider_id="openai-compatible", model_id=model_id,
        request_model_id=model_id, variant=None,
        provider=provider, client=client, capabilities=capability,
    )
    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "Implement the requested project.", model_runtime=runtime,
            trace_enabled=False, tool_allowlist=[],
        )
        try:
            result = agent._call_llm(
                [{"role": "user", "content": "Build the project."}], stream=stream,
            )
            budget = agent._prompt_budget()
        finally:
            agent.close()

    assert result.content == "done"
    assert len(requests) == 1
    assert requests[0]["model"] == model_id
    assert requests[0]["max_tokens"] == expected_output
    assert "max_completion_tokens" not in requests[0]
    assert budget.context_tokens == expected_context
    assert budget.usable_input_tokens == expected_context - expected_output


@pytest.mark.parametrize("model_id", [
    "private-deepseek-flash", "deepseek-flash-custom",
    "custom/deepseek-flash", "deepseek-chat",
])
def test_unknown_deepseek_ids_keep_existing_conservative_budget(model_id):
    capability = resolve_model_capabilities("openai-compatible", model_id)

    assert capability.output_tokens == 8192
    assert capability.context_tokens == 128_000


def test_explicit_environment_output_budget_remains_authoritative(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.providers import configured_model_capabilities

    monkeypatch.setenv("MAX_OUTPUT_TOKENS", "8192")
    monkeypatch.setattr(config, "MAX_OUTPUT_TOKENS", 8192)
    monkeypatch.setattr(config, "MODEL_CATALOG_JSON", "")
    monkeypatch.setattr(config, "MODEL_CATALOG_PATH", "")
    capability = configured_model_capabilities("openai-compatible", "deepseek-flash")

    assert capability.output_tokens == 8192
