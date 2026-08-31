"""Full Production capability contracts for the default Native runner."""
from __future__ import annotations

import inspect

from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.runtime.core import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunOptions, RunRequest
from nz_coder.runtime.model_gateway import ResolvedModelRuntime


def _request(tmp_path):
    return RunRequest(
        agent=AgentDefinition(name="product", instructions="Use production services."),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "inspect"},),
        workspace=tmp_path,
        session_id="native-product",
        stream=False,
        provider="offline",
        model="offline-model",
        metadata={"permission_mode": "auto", "persist_session": False},
    )


def _runtime():
    return ResolvedModelRuntime(
        provider_id="offline", model_id="offline-model",
        request_model_id="offline-model", variant=None,
        provider=object(), client=object(), owns_client=False,
        capabilities=ModelCapabilities(
            provider="offline", model_id="offline-model", supports_streaming=False,
        ),
    )


def test_native_environment_uses_real_production_service_graph(monkeypatch, tmp_path):
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.conversation.input_preflight import ProductionInputPreflight
    from nz_coder.runtime.execution.services import (
        ProductionCompletionVerifier,
        ProductionMemoryService,
        ProductionTurnModelRuntime,
    )
    from nz_coder.runtime.tool_runtime import ProductionToolRuntime
    from nz_coder.tool_platform.exposure import ToolExposureMiddleware

    monkeypatch.setattr(native_sdk, "resolve_model_runtime", lambda _request: _runtime())
    environment = native_sdk.build_product_run_environment(
        _request(tmp_path), RunOptions(permission_asker=lambda *_args: True),
    )
    try:
        services = environment.runtime_services
        assert isinstance(services.model, ProductionTurnModelRuntime)
        assert isinstance(services.tools, ProductionToolRuntime)
        assert isinstance(services.memory, ProductionMemoryService)
        assert isinstance(services.verifier, ProductionCompletionVerifier)
        assert isinstance(services.inputs, ProductionInputPreflight)
        assert any(isinstance(item, ToolExposureMiddleware) for item in services.middleware)
        assert environment.auto_mode_controller.enabled is False
        assert environment.auto_permission_asker is None
    finally:
        environment.close()


def test_semantic_configuration_failure_closes_built_environment(monkeypatch, tmp_path):
    from nz_coder.intelligence import semantic
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.execution.loop import ProductRunEnvironment

    monkeypatch.setattr(native_sdk, "resolve_model_runtime", lambda _request: _runtime())
    request = _request(tmp_path)
    request = type(request)(
        **{
            **request.__dict__,
            "metadata": {**request.metadata, "semantic_model": "fixture/missing"},
        }
    )
    closed = []

    class Provider:
        identity = "fixture/missing"

        def prepare(self):
            raise RuntimeError("semantic model unavailable")

    monkeypatch.setattr(semantic, "sentence_transformer_provider", lambda _model: Provider())
    original_close = ProductRunEnvironment.close

    def close(environment):
        closed.append(True)
        return original_close(environment)

    monkeypatch.setattr(ProductRunEnvironment, "close", close)
    try:
        native_sdk.build_product_run_environment(request, RunOptions())
    except RuntimeError as exc:
        assert "semantic model unavailable" in str(exc)
    else:
        raise AssertionError("semantic configuration failure must abort the run")
    assert closed == [True]


def test_native_sdk_contains_no_reduced_capability_stubs():
    from nz_coder.runtime.execution import native_sdk

    source = inspect.getsource(native_sdk)
    for name in ("_Memory", "_Verifier", "_Planning", "_Snapshots", "_Inputs"):
        assert f"class {name}" not in source


def test_run_options_accept_explicit_product_interaction_ports():
    marker = object()
    options = RunOptions(
        permission_asker=lambda *_args: True,
        question_asker=lambda *_args: "answer",
        workflow_approval_asker=lambda *_args: True,
        event_bus=marker,
    )
    assert options.event_bus is marker


def test_agent_client_forwards_product_interaction_ports(tmp_path):
    import asyncio
    from nz_coder.runtime.core.result import RunResult, RunStatus, TokenUsage
    from nz_coder.sdk import AgentClient

    captured = {}

    class Runner:
        async def run_result(self, request, options):
            captured["options"] = options
            return RunResult(
                status=RunStatus.COMPLETED,
                final_text="done",
                messages=request.messages,
                usage=TokenUsage(),
                session_id=request.session_id,
                active_agent=request.agent.name,
            )

    marker = object()

    def permission(*_args):
        return True

    def question(*_args):
        return "answer"

    def approval(*_args):
        return True
    asyncio.run(AgentClient(runner=Runner()).run(
        _request(tmp_path),
        permission_asker=permission,
        question_asker=question,
        workflow_approval_asker=approval,
        event_bus=marker,
    ))
    assert captured["options"].permission_asker is permission
    assert captured["options"].question_asker is question
    assert captured["options"].workflow_approval_asker is approval
    assert captured["options"].event_bus is marker
