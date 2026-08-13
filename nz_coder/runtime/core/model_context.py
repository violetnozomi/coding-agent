"""Focused capabilities consumed by the production model-turn runtime."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


ModelCallback = Callable[..., object]


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
    complete_override: ModelCallback | None = None

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
        ):
            if not callable(getattr(self, name)):
                raise TypeError(f"ModelExecutionContext {name} must be callable")
        if self.complete_override is not None and not callable(self.complete_override):
            raise TypeError(
                "ModelExecutionContext complete_override must be callable or None"
            )
