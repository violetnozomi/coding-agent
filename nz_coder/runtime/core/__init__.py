"""Stable contracts for NZ-Coder's shared Agent execution kernel."""
from __future__ import annotations

from importlib import import_module


_EXPORTS = {
    "BACKGROUND_PROFILE": "profiles",
    "CompletionVerifier": "contracts",
    "ContextManager": "contracts",
    "MAIN_PROFILE": "profiles",
    "MemoryService": "contracts",
    "ModelGateway": "contracts",
    "READ_CHILD_PROFILE": "profiles",
    "WORKFLOW_PROFILE": "profiles",
    "WRITE_CHILD_PROFILE": "profiles",
    "RunMode": "profiles",
    "RunProfile": "profiles",
    "AgentDefinition": "request",
    "RunRequest": "request",
    "RunResult": "result",
    "RunState": "state",
    "RunStatus": "result",
    "RuntimeEvent": "events",
    "RuntimeEventSink": "events",
    "RuntimeEventMiddleware": "events",
    "RuntimeEventName": "events",
    "MiddlewarePipeline": "middleware",
    "RuntimeMiddleware": "middleware",
    "RuntimeServices": "contracts",
    "SessionRepository": "contracts",
    "TokenUsage": "result",
    "ToolRuntime": "contracts",
    "profile_for_mode": "profiles",
}

__all__ = [
    "BACKGROUND_PROFILE",
    "CompletionVerifier",
    "ContextManager",
    "MAIN_PROFILE",
    "MemoryService",
    "ModelGateway",
    "READ_CHILD_PROFILE",
    "WORKFLOW_PROFILE",
    "WRITE_CHILD_PROFILE",
    "RunMode",
    "RunProfile",
    "AgentDefinition",
    "RunRequest",
    "RunResult",
    "RunState",
    "RunStatus",
    "RuntimeEvent",
    "RuntimeEventSink",
    "RuntimeEventMiddleware",
    "RuntimeEventName",
    "MiddlewarePipeline",
    "RuntimeMiddleware",
    "RuntimeServices",
    "SessionRepository",
    "TokenUsage",
    "ToolRuntime",
    "profile_for_mode",
]


def __getattr__(name: str):  # noqa: ANN202
    """Resolve stable package exports without loading the full core surface."""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Include lazy public contracts in interactive discovery."""
    return sorted(set(globals()).union(__all__))
