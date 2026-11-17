"""Focused capabilities consumed by the production model-turn runtime."""
from __future__ import annotations

from dataclasses import dataclass
import copy
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable


ModelCallback = Callable[..., object]
_request_tools: ContextVar[list | None] = ContextVar("frozen_request_tools", default=None)


@contextmanager
def scoped_request_tools(tools: list):
    """Keep one outbound schema snapshot across nested host/runtime wrappers."""
    token = _request_tools.set(copy.deepcopy(tools))
    try:
        yield
    finally:
        _request_tools.reset(token)


def current_request_tools() -> list | None:
    tools = _request_tools.get()
    return copy.deepcopy(tools) if tools is not None else None


@dataclass(frozen=True)
class ModelExecutionContext:
    """Dynamic model selection and narrow invocation operations for one run."""

    capabilities: ModelCallback
    active_model_id: ModelCallback
    active_tool_specs: ModelCallback
    prompt_budget: ModelCallback
    call_streaming: ModelCallback
    call_non_streaming: ModelCallback
    gateway: ModelCallback
    project_outcome: ModelCallback
    record_success: ModelCallback
    trace: ModelCallback
    retire_message_part: ModelCallback
    provider_instance_id: ModelCallback | None = None
    complete_override: ModelCallback | None = None
    working_memory_allowed: ModelCallback = lambda: True
    run_context: object | None = None

    def __post_init__(self) -> None:
        for name in (
            "capabilities",
            "active_model_id",
            "active_tool_specs",
            "prompt_budget",
            "call_streaming",
            "call_non_streaming",
            "gateway",
            "project_outcome",
            "record_success",
            "trace",
            "retire_message_part",
            "working_memory_allowed",
        ):
            if not callable(getattr(self, name)):
                raise TypeError(f"ModelExecutionContext {name} must be callable")
        if self.provider_instance_id is not None and not callable(
            self.provider_instance_id
        ):
            raise TypeError(
                "ModelExecutionContext provider_instance_id must be callable or None"
            )
        if self.complete_override is not None and not callable(self.complete_override):
            raise TypeError(
                "ModelExecutionContext complete_override must be callable or None"
            )
