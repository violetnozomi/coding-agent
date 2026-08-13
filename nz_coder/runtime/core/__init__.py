"""Stable contracts for NZ-Coder's shared Agent execution kernel."""
from __future__ import annotations

from nz_coder.runtime.core.contracts import (
    CompletionVerifier,
    ContextManager,
    MemoryService,
    ModelGateway,
    RuntimeServices,
    SessionRepository,
    ToolRuntime,
)
from nz_coder.runtime.core.events import (
    RuntimeEvent,
    RuntimeEventMiddleware,
    RuntimeEventName,
    RuntimeEventSink,
)
from nz_coder.runtime.core.middleware import MiddlewarePipeline, RuntimeMiddleware
from nz_coder.runtime.core.profiles import (
    BACKGROUND_PROFILE,
    MAIN_PROFILE,
    READ_CHILD_PROFILE,
    WORKFLOW_PROFILE,
    WRITE_CHILD_PROFILE,
    RunMode,
    RunProfile,
    profile_for_mode,
)
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.core.result import RunResult, RunStatus, TokenUsage
from nz_coder.runtime.core.state import RunState

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
