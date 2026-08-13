"""Legacy Agent host adapter for focused Runner orchestration."""
from __future__ import annotations

from pathlib import Path

from nz_coder.runtime.adapters.context import context_from_legacy_host
from nz_coder.runtime.adapters.lifecycle import lifecycle_context_from_legacy_host
from nz_coder.runtime.adapters.model import model_context_from_legacy_host
from nz_coder.runtime.adapters.tool import tool_context_from_legacy_host
from nz_coder.runtime.core.runner_context import RunnerExecutionContext
from nz_coder.runtime.core.profiles import MAIN_PROFILE, profile_for_mode
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.message_runtime import LegacyMessageRuntime
from nz_coder.runtime.planning_runtime import LegacyPlanningRuntime
from nz_coder.runtime.snapshot_runtime import LegacySnapshotRuntime
from nz_coder.runtime.adapters.verification import verification_context_from_legacy_host


def run_request_from_legacy_host(
    host,
    messages: list,
    stream: bool,
    *,
    allowed_tools: tuple[str, ...] | list[str] | None = None,
    provider_override: str | None = None,
    model_override: str | None = None,
) -> RunRequest:
    """Project a legacy coding facade onto the immutable native contract."""
    profile_name = str(getattr(host, "runtime_profile", "main") or "main")
    try:
        profile = profile_for_mode(profile_name)
    except ValueError:
        profile = MAIN_PROFILE
    agent_name = str(
        getattr(host, "current_agent_name", "")
        or getattr(host, "agent_id", "")
        or "worker"
    )
    allowlist = getattr(host, "tool_allowlist", None)
    host_tools = tuple(allowlist) if isinstance(allowlist, (set, tuple, list)) else ()
    if allowed_tools is None:
        tool_names = host_tools
    else:
        requested = tuple(dict.fromkeys(str(name).strip() for name in allowed_tools))
        admitted = set(host_tools)
        tool_names = (
            tuple(name for name in requested if name in admitted)
            if host_tools
            else requested
        )
    permissions = getattr(host, "permissions", None)
    permission_mode = getattr(permissions, "mode", None)
    parent_session_id = getattr(host, "parent_session_id", None)
    metadata = {
        "suppress_runtime_events": True,
        **(
            {"permission_mode": str(permission_mode)}
            if isinstance(permission_mode, str) and permission_mode
            else {}
        ),
        **(
            {"parent_session_id": str(parent_session_id)}
            if isinstance(parent_session_id, str) and parent_session_id
            else {}
        ),
    }
    provider_id = _optional_text(provider_override) or _optional_text(
        getattr(host, "provider_id", None)
    )
    model_id = _optional_text(model_override) or _optional_text(
        getattr(host, "model_id", None)
    )
    return RunRequest(
        agent=AgentDefinition(
            name=agent_name,
            instructions=str(getattr(host, "system_prompt", "") or "Coding agent"),
            allowed_tools=tool_names or None,
            provider=provider_id,
            model=model_id,
            reasoning_effort=_optional_text(getattr(host, "model_variant", None)),
        ),
        profile=profile,
        messages=messages,
        workspace=Path(getattr(host, "workdir", Path.cwd())),
        session_id=str(getattr(host, "session_id", "") or "session"),
        tool_names=tool_names,
        stream=stream,
        provider=provider_id,
        model=model_id,
        reasoning_effort=_optional_text(getattr(host, "model_variant", None)),
        metadata=metadata,
    )


def _optional_text(value: object) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def runner_context_from_legacy_host(host, services, run_context) -> RunnerExecutionContext:
    """Bind focused service owners once at the legacy composition boundary."""
    return RunnerExecutionContext(
        session_id=str(getattr(host, "session_id", "") or run_context.session.session_id),
        runtime_state=getattr(host, "runtime_state", None),
        execution=_ExecutionServices(host, services, run_context),
        lifecycle=_LifecycleService(host, services),
        policy=_PolicyService(host, services),
        planning=LegacyPlanningRuntime(host),
        control=_TurnControlService(host),
        hooks=_HookService(host),
        messages=LegacyMessageRuntime(host),
        snapshots=LegacySnapshotRuntime(host),
    )


class _ExecutionServices:
    def __init__(self, host, services, run_context) -> None:
        self.host = host
        self.services = services
        self.run_context = run_context

    def context(self):
        return context_from_legacy_host(self.host)

    def model(self):
        return model_context_from_legacy_host(self.host)

    def tools(self):
        return tool_context_from_legacy_host(
            self.host, self.run_context, self.services,
        )


class _LifecycleService:
    def __init__(self, host, services) -> None:
        self.host = host
        self.services = services

    def initialize(self, messages, stream):
        return self.services.lifecycle.initialize(
            _lifecycle_owner(self.host, self.services.lifecycle), messages, stream,
        )

    async def finalize(self, messages, status, *args, **kwargs):
        return await self.services.lifecycle.finalize(
            _lifecycle_owner(self.host, self.services.lifecycle),
            messages, status, *args, **kwargs,
        )


class _PolicyService:
    def __init__(self, host, services) -> None:
        self.host = host
        self.services = services

    async def run_input_guardrails(self, messages):
        return await self.services.guardrails.run_input(self.host, messages)

    def has_output_guardrail(self):
        return self.services.guardrails.has(self.host, "output")

    async def run_output_guardrail(self, content, messages):
        return await self.services.guardrails.run_output(self.host, content, messages)

    async def prepare_user_images(self, messages, owner):
        return await self.services.inputs.prepare_user_images(self.host, messages, owner)

    async def prepare_user_documents(self, messages, owner):
        return await self.services.inputs.prepare_user_documents(self.host, messages, owner)

    def resolve_structured_output(self, content, messages):
        return self.services.transitions.resolve_structured_output(
            self.host, content, messages,
        )

    def return_from_as_tool(self, messages, content):
        return self.services.transitions.return_from_as_tool(
            self.host, messages, content,
        )

    async def terminal_content(self, fallback, messages):
        return await self.services.transitions.terminal_content(
            self.host, fallback, messages,
        )

    async def verify_completion(self, messages, status, content):
        return await self.services.verifier.verify(
            verification_context_from_legacy_host(self.host),
            messages,
            status,
            content,
        )


class _TurnControlService:
    def __init__(self, host) -> None:
        self.host = host

    def has_queued_followup(self):
        callback = getattr(self.host, "_has_queued_followup", None)
        return bool(callback()) if callable(callback) else False

    def drain_background_messages(self, messages):
        callback = getattr(self.host, "_drain_background_agent_messages", None)
        return callback(messages) if callable(callback) else None

    def has_agent_call_stack(self):
        return bool(getattr(self.host, "_agent_call_stack", []))

    async def notify_agent_switched(self, transition):
        return await _required(self.host, "_notify_agent_switched_async")(transition)

    def persist_runtime_state(self, **kwargs):
        return _required(self.host, "_persist_runtime_state")(**kwargs)

    def stop_hook_reason(self):
        hooks = getattr(self.host, "hooks", None)
        return str(getattr(hooks, "stop_hook_reason", "") or "")


class _HookService:
    def __init__(self, host) -> None:
        self.host = host
        self.hooks = getattr(host, "hooks", None)
        self.tracer = getattr(host, "tracer", None)

    def on_turn_start(self, messages):
        return _call_hook(self.hooks, "on_turn_start", self.host, messages)

    def on_pre_send(self, messages):
        return _call_hook(self.hooks, "on_pre_send", self.host, messages)

    def on_turn_end(self, messages, status):
        return _call_hook(
            self.hooks, "on_turn_end", self.host, messages, status=status,
        )

    def trace(self, *args, **kwargs):
        callback = getattr(self.tracer, "log", None)
        return callback(*args, **kwargs) if callable(callback) else None


def _required(host, name: str):
    value = getattr(host, name, None)
    return value if callable(value) else _missing(name)


def _missing(name: str):
    def missing(*_args, **_kwargs):
        raise RuntimeError(f"Runner adapter is missing required capability {name}")
    return missing


def _discard(*_args, **_kwargs) -> None:
    return None


def _call_hook(hooks, name: str, *args, **kwargs) -> None:
    callback = getattr(hooks, name, None)
    if callable(callback):
        callback(*args, **kwargs)


def _lifecycle_owner(host, lifecycle):
    from nz_coder.runtime.run_lifecycle import ProductionRunLifecycle

    if isinstance(lifecycle, ProductionRunLifecycle):
        return lifecycle_context_from_legacy_host(host)
    return host
