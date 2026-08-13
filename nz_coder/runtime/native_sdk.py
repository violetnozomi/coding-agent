"""Canonical product-runtime composition used by the public Python SDK."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace

from nz_coder.runtime.composition import declared_runtime
from nz_coder.runtime.core.request import RunOptions, RunRequest
from nz_coder.runtime.model_gateway import ModelSelectionRequest, resolve_model_runtime
from nz_coder.runtime.services import ProductionRuntimeEventSink, build_runtime_services
from nz_coder.runtime.session.store import EphemeralSessionStore, LegacyJsonSessionStore
from nz_coder.runtime.workdir import scoped_workdir
from nz_coder.session_events import SessionEventBus


class _ProductEventSink:
    """Publish canonical events to product transports and the session protocol."""

    def __init__(self, callback=None) -> None:
        self._callback = callback
        self._session = ProductionRuntimeEventSink()

    def publish(self, event) -> None:
        self._session.publish(event)
        if callable(self._callback):
            self._callback(event)


def build_product_run_environment(request: RunRequest, options: RunOptions):
    """Build the complete production capability graph for one product run."""
    if not isinstance(request, RunRequest):
        raise TypeError("build_product_run_environment requires RunRequest")
    if not isinstance(options, RunOptions):
        raise TypeError("build_product_run_environment requires RunOptions")

    runtime = resolve_model_runtime(ModelSelectionRequest(
        provider_name=request.provider or request.agent.provider,
        model_id=request.model or request.agent.model,
        variant=request.reasoning_effort or request.agent.reasoning_effort,
        workspace=request.workspace,
    ))
    store = (
        LegacyJsonSessionStore()
        if request.metadata.get("persist_session", True)
        else EphemeralSessionStore()
    )
    services = build_runtime_services(
        session_store=store,
        events=_ProductEventSink(options.on_event),
    )
    event_bus = options.event_bus
    owns_event_bus = event_bus is None
    if event_bus is None:
        event_bus = SessionEventBus(session_id=request.session_id)

    # Importing these helpers here keeps the SDK's public declaration layer
    # independent from the product composition root during module import.
    from nz_coder.sdk import _agent_graph_from_definition, _tool_allowlist

    graph = _agent_graph_from_definition(
        request.agent,
        root_provider=request.provider,
        root_model=request.model,
        root_effort=request.reasoning_effort,
    )
    try:
        with scoped_workdir(request.workspace):
            environment = declared_runtime(graph).build(
                session_id=request.session_id,
                permission_mode=request.metadata.get("permission_mode"),
                permission_asker=options.permission_asker,
                question_asker=options.question_asker,
                workflow_approval_asker=options.workflow_approval_asker,
                event_bus=event_bus,
                event_bus_owned=owns_event_bus,
                tool_allowlist=_tool_allowlist(request),
                model_runtime=runtime,
                runtime_services=services,
            )
    except BaseException:
        if owns_event_bus:
            event_bus.close()
        runtime.close()
        raise
    environment.runtime_profile = request.profile.mode.value
    environment.model_capability_options = copy.deepcopy(
        request.metadata.get("model_capability_options") or {}
    )
    environment.repo_retrieval_strategy = str(
        request.metadata.get("repo_retrieval_strategy") or "guidance"
    )
    environment.repo_intelligence_mode = str(
        request.metadata.get("repo_intelligence_mode") or "lookup"
    )
    semantic_model = str(request.metadata.get("semantic_model") or "").strip()
    if semantic_model:
        from nz_coder.intelligence.semantic import sentence_transformer_provider

        try:
            environment.repo_intelligence.configure_semantic(
                sentence_transformer_provider(semantic_model),
            )
        except BaseException:
            environment.close()
            runtime.close()
            raise
    return environment


class NativeSDKRunner:
    """Run SDK requests through the same complete environment as product hosts."""

    def __init__(self, environment=None) -> None:
        self._environment = environment

    async def run_result(
        self, request: RunRequest, options: RunOptions | None = None,
    ):
        if not isinstance(request, RunRequest):
            raise TypeError("NativeSDKRunner requires RunRequest")
        selected = options or RunOptions()
        environment = self._environment or build_product_run_environment(request, selected)
        owns_environment = self._environment is None
        messages = copy.deepcopy(list(request.messages))
        effective_request = request
        if selected.event_bus is not None:
            effective_request = replace(request, metadata={
                **request.metadata,
                # The product SessionEventBus already receives the mature
                # lifecycle stream; do not duplicate core projection events.
                "suppress_runtime_events": True,
            })

        async def execute(_owner, _messages, *_callbacks):
            return await environment.runner.run_result(effective_request, selected)

        try:
            result = await environment.runtime_host.run(
                environment,
                messages,
                on_tool=selected.on_tool,
                on_text=selected.on_text,
                on_token=selected.on_token,
                stream=request.stream if selected.stream is None else selected.stream,
                execute=execute,
            )
            return replace(result, metadata={
                **result.metadata,
                "changed_files": environment.change_tracker.current_changed_paths(),
            })
        finally:
            if owns_environment:
                await asyncio.to_thread(environment.close)


def build_native_sdk_runner() -> NativeSDKRunner:
    """Return the canonical host-neutral product runner."""
    return NativeSDKRunner()


__all__ = [
    "NativeSDKRunner", "build_native_sdk_runner", "build_product_run_environment",
]
