"""Canonical product-runtime composition used by the public Python SDK."""
from __future__ import annotations

import copy
import threading
from pathlib import Path
import uuid
from dataclasses import replace

from nz_coder.foundation.async_utils import to_thread_settled
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
    activate_session_on_build: bool = True,
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
                activate_session_on_build=activate_session_on_build,
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
    # Absence must remain distinct from an explicit guidance/tool-only choice.
    # The scoped execution resolver owns defaults and benchmark overrides.
    environment.repo_retrieval_strategy = request.metadata.get("repo_retrieval_strategy")
    environment.repo_intelligence_mode = request.metadata.get("repo_intelligence_mode")
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
        self._cleanup_lock = threading.RLock()
        self._pending_environments = {}
        self._active_keys = set()
        self._cleanup_keys = set()

    @staticmethod
    def _key(workspace, session_id):
        return Path(workspace).resolve(), str(session_id)

    def request_retirement(self, workspace, session_id) -> None:
        with self._cleanup_lock:
            environment = self._pending_environments.get(self._key(workspace, session_id))
        if environment is not None:
            environment.request_retirement()

    def retry_cleanup(self, workspace, session_id, *, allow_active=False) -> bool:
        """由原 caller 重试同会话资源；锁只登记，不包住慢 close。"""
        key = self._key(workspace, session_id)
        with self._cleanup_lock:
            if key in self._active_keys and not allow_active:
                return False
            environment = self._pending_environments.get(key)
            if environment is None:
                return True
            if key in self._cleanup_keys:
                return False
            self._cleanup_keys.add(key)
        try:
            try:
                environment.close()
            except BaseException as exc:
                _record_cleanup_failure(environment, exc)
                return False
            complete = environment.environment_cleanup_complete
            if complete and not environment.has_pending_settlement:
                with self._cleanup_lock:
                    if self._pending_environments.get(key) is environment:
                        del self._pending_environments[key]
            return complete
        finally:
            with self._cleanup_lock:
                self._cleanup_keys.discard(key)

    def _settlement_environment(self, workspace, session_id):
        key = self._key(workspace, session_id)
        with self._cleanup_lock:
            environment = self._environment or self._pending_environments.get(key)
        if environment is not None and self._key(environment.workdir, environment.session_id) == key:
            return environment
        return None

    def _has_pending_settlement(self, workspace, session_id) -> bool:
        environment = self._settlement_environment(workspace, session_id)
        return environment is not None and environment.has_pending_settlement

    async def retry_settlement(self, workspace, session_id):
        from nz_coder.runtime.core.result import SessionSettlementResult
        key = self._key(workspace, session_id)
        environment = self._settlement_environment(*key)
        if environment is None:
            return SessionSettlementResult(str(session_id), None, "idle")
        result = await environment.retry_settlement()
        if self._environment is None and result.state == "committed":
            await to_thread_settled(self.retry_cleanup, *key)
        return result

    async def run_result(
        self, request: RunRequest, options: RunOptions | None = None,
    ):
        from nz_coder.protocol.public_error import PublicRuntimeError
        if not isinstance(request, RunRequest):
            raise TypeError("NativeSDKRunner requires RunRequest")
        key = self._key(request.workspace, request.session_id)
        with self._cleanup_lock:
            if key in self._active_keys:
                raise RuntimeError("Session execution or cleanup is in progress")
            self._active_keys.add(key)
        try:
            settlement = await self.retry_settlement(*key)
            if settlement.state in {"pending", "conflict"}:
                raise PublicRuntimeError(settlement.error)
            if self._environment is None and not await to_thread_settled(self.retry_cleanup, *key, allow_active=True):
                raise RuntimeError("Session environment cleanup is incomplete")
            return await self._run_result(request, options)
        finally:
            with self._cleanup_lock:
                self._active_keys.discard(key)

    async def _run_result(
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
        if owns_environment:
            with self._cleanup_lock:
                self._pending_environments[self._key(request.workspace, request.session_id)] = environment
        messages = copy.deepcopy(list(effective_request.messages))
        if selected.event_bus is not None:
            effective_request = replace(effective_request, metadata={
                **effective_request.metadata,
                # The product SessionEventBus already receives the mature
                # lifecycle stream; do not duplicate core projection events.
                "suppress_runtime_events": True,
            })

        async def execute(_owner, _messages, *_callbacks):
            try:
                return await environment.runner.run_result(effective_request, selected)
            finally:
                if owns_environment:
                    environment.request_retirement()

        run_control = None
        try:
            trusted_scope = environment.memory_scope.to_dict()
            supplied_scope = effective_request.metadata.get("memory_scope")
            if supplied_scope is not None and supplied_scope != trusted_scope:
                raise ValueError("Request memory scope differs from its trusted environment")
            effective_request = replace(effective_request, metadata={
                **effective_request.metadata,
                "memory_scope": trusted_scope,
                "memory_capability": environment.memory_capability,
            })
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
        except BaseException:
            if owns_environment:
                environment.request_retirement()
            if (
                run_control is not None
                and getattr(environment, "_active_run_control", None) is run_control
            ):
                environment.retire_run_control(run_control)
            raise
        finally:
            if owns_environment:
                try:
                    await to_thread_settled(self.retry_cleanup, request.workspace, request.session_id, allow_active=True)
                except BaseException as exc:
                    _record_cleanup_failure(environment, exc)
        return replace(result, metadata={
            **result.metadata,
            "changed_files": environment.change_tracker.current_changed_paths(),
            **({"environment_cleanup_complete": environment.environment_cleanup_complete,
                "environment_cleanup_failures": environment.environment_cleanup_failures}
               if owns_environment else {}),
        })


def build_native_sdk_runner() -> NativeSDKRunner:
    """Return the canonical host-neutral product runner."""
    return NativeSDKRunner()


__all__ = [
    "NativeSDKRunner", "build_native_sdk_runner", "build_product_run_environment",
]
