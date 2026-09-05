"""Canonical product-runtime composition used by the public Python SDK."""
from __future__ import annotations

import asyncio
import copy
import uuid
from dataclasses import replace

from nz_coder.runtime.execution.composition import declared_runtime
from nz_coder.runtime.core.request import RunOptions, RunRequest
from nz_coder.runtime.model_gateway import ModelSelectionRequest, resolve_model_runtime
from nz_coder.runtime.execution.services import ProductionRuntimeEventSink, build_runtime_services
from nz_coder.runtime.session.store import EphemeralSessionStore, LegacyJsonSessionStore
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.protocol.session_events import SessionEventBus


class _ProductEventSink:
    """Publish canonical events to product transports and the session protocol."""

    def __init__(self, callback=None) -> None:
        self._callback = callback
        self._session = ProductionRuntimeEventSink()

    def publish(self, event) -> None:
        self._session.publish(event)
        if callable(self._callback):
            self._callback(event)


def _record_cleanup_failure(environment, exc: BaseException) -> None:
    """Attach a secret-free cleanup diagnostic without replacing Run outcome."""
    tracer = getattr(environment, "tracer", None)
    log = getattr(tracer, "log", None)
    if not callable(log):
        return
    try:
        log(
            "product_environment_cleanup_failed",
            failure_type=type(exc).__name__,
        )
    except BaseException:
        # Diagnostics are not part of resource ownership and may not replace
        # either the business result or its primary exception.
        return


def _close_after_build_failure(primary: BaseException, *resources) -> None:
    """Best-effort construction rollback while retaining the primary error."""
    failures: list[str] = []
    for resource in resources:
        close = getattr(resource, "close", None)
        if not callable(close):
            continue
        try:
            close()
        except BaseException as exc:
            failures.append(type(exc).__name__)
    if failures and hasattr(primary, "add_note"):
        primary.add_note(
            "Product environment rollback also reported: "
            + ", ".join(failures)
        )


def build_product_run_environment(
    request: RunRequest,
    options: RunOptions,
    *,
    config_snapshot=None,
):
    """Build the complete production capability graph for one product run."""
    if not isinstance(request, RunRequest):
        raise TypeError("build_product_run_environment requires RunRequest")
    if not isinstance(options, RunOptions):
        raise TypeError("build_product_run_environment requires RunOptions")

    from nz_coder.foundation.workspace_trust import load_config_snapshot

    run_snapshot = config_snapshot or load_config_snapshot(request.workspace)
    runtime = resolve_model_runtime(ModelSelectionRequest(
        provider_name=request.provider or request.agent.provider,
        model_id=request.model or request.agent.model,
        variant=request.reasoning_effort or request.agent.reasoning_effort,
        workspace=request.workspace,
        config_snapshot=run_snapshot,
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
                manage_model_runtime=True,
                config_snapshot=run_snapshot,
                runtime_services=services,
            )
            if request.interaction_run_id:
                environment.event_publisher = event_bus.for_interaction(
                    request.interaction_run_id,
                    agent_invocation_id=environment.agent_id,
                    parent_interaction_run_id=str(
                        request.parent_interaction_run_id or ""
                    ),
                    parent_agent_invocation_id=str(request.parent_agent_id or ""),
                )
                environment.background_agents.bind_event_publisher(
                    environment.event_publisher
                )
    except BaseException as primary:
        _close_after_build_failure(
            primary,
            event_bus if owns_event_bus else None,
            runtime,
        )
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
        except BaseException as primary:
            _close_after_build_failure(primary, environment, runtime)
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
        from nz_coder.foundation.workspace_trust import load_config_snapshot

        run_snapshot = selected.config_snapshot or load_config_snapshot(request.workspace)
        effective_request = request
        if request.interaction_run_id is None:
            effective_request = replace(
                request,
                interaction_run_id=f"interaction-{uuid.uuid4().hex}",
            )
        environment = self._environment or build_product_run_environment(
            effective_request,
            selected,
            config_snapshot=run_snapshot,
        )
        owns_environment = self._environment is None
        messages = copy.deepcopy(list(effective_request.messages))
        if selected.event_bus is not None:
            effective_request = replace(effective_request, metadata={
                **effective_request.metadata,
                # The product SessionEventBus already receives the mature
                # lifecycle stream; do not duplicate core projection events.
                "suppress_runtime_events": True,
            })

        async def execute(_owner, _messages, *_callbacks):
            return await environment.runner.run_result(effective_request, selected)

        run_control = None
        try:
            run_control = environment.prepare_run_control(
                run_snapshot,
                provider_name=(
                    effective_request.provider
                    or effective_request.agent.provider
                    or environment.model_runtime.provider_id
                ),
                model_id=(
                    effective_request.model
                    or effective_request.agent.model
                    or environment.model_runtime.model_id
                ),
                variant=(
                    effective_request.reasoning_effort
                    or effective_request.agent.reasoning_effort
                    or environment.model_runtime.variant
                ),
            )
            result = await environment.runtime_host.run(
                environment,
                messages,
                on_tool=selected.on_tool,
                on_text=selected.on_text,
                on_token=selected.on_token,
                stream=(
                    effective_request.stream
                    if selected.stream is None
                    else selected.stream
                ),
                execute=execute,
                run_control=run_control,
            )
            return replace(result, metadata={
                **result.metadata,
                "changed_files": environment.change_tracker.current_changed_paths(),
            })
        except BaseException:
            if (
                run_control is not None
                and getattr(environment, "_active_run_control", None) is run_control
            ):
                environment.retire_run_control(run_control)
            raise
        finally:
            if owns_environment:
                try:
                    await asyncio.to_thread(environment.close)
                except BaseException as exc:
                    _record_cleanup_failure(environment, exc)


def build_native_sdk_runner() -> NativeSDKRunner:
    """Return the canonical host-neutral product runner."""
    return NativeSDKRunner()


__all__ = [
    "NativeSDKRunner", "build_native_sdk_runner", "build_product_run_environment",
]
