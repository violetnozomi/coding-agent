"""Typed Agent input, output, and tool-call guardrail contracts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable


GuardrailCheck = Callable[..., dict | Awaitable[dict]]


class GuardrailBlockedError(RuntimeError):
    """Raised when an input or output guardrail rejects a run."""

    def __init__(self, name: str, hook_point: str, reason: str):
        self.guardrail_name = name
        self.hook_point = hook_point
        super().__init__(f'Guardrail "{name}" blocked at {hook_point}: {reason}')


class GuardrailEscalateError(RuntimeError):
    """Raised when a guardrail delegates its decision to the host/user."""

    def __init__(self, name: str, hook_point: str, reason: str):
        self.guardrail_name = name
        self.hook_point = hook_point
        super().__init__(f'Guardrail "{name}" escalated at {hook_point}: {reason}')


@dataclass(frozen=True)
class InputGuardrail:
    """Inspect or rewrite the run transcript before its first model call."""

    name: str
    check: GuardrailCheck
    kind: str = "input"


@dataclass(frozen=True)
class OutputGuardrail:
    """Inspect or rewrite the final assistant message before publication."""

    name: str
    check: GuardrailCheck
    kind: str = "output"


@dataclass(frozen=True)
class ToolGuardrail:
    """Inspect, rewrite, or reject individual tool calls and results."""

    name: str
    before_tool: GuardrailCheck | None = None
    after_tool: GuardrailCheck | None = None
    kind: str = "tool"


def validate_verdict(verdict: object, name: str) -> dict:
    """Validate the common four-action verdict envelope."""
    if not isinstance(verdict, dict) or verdict.get("action") not in {
        "allow", "rewrite", "block", "escalate",
    }:
        raise ValueError(f'Guardrail "{name}" returned an invalid verdict')
    action = verdict["action"]
    if action in {"block", "escalate"} and not isinstance(verdict.get("reason"), str):
        raise ValueError(f'Guardrail "{name}" {action} verdict requires reason')
    if action == "rewrite" and "payload" not in verdict:
        raise ValueError(f'Guardrail "{name}" rewrite verdict requires payload')
    return verdict
