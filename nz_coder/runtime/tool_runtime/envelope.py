"""Private Provider tool envelopes and approved public tool calls."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from nz_coder.runtime.agent.agent_resilience import (
    repair_tool_call_envelopes,
    repair_tool_call_ids,
    repair_tool_call_names,
)


@dataclass(frozen=True)
class RawToolEnvelope:
    """Complete untrusted Provider envelope retained outside Session state."""

    call_id: str
    tool_name: str
    raw_arguments: str
    parsed_arguments: dict[str, Any] | None
    attempt_id: str = ""
    generation_id: str = ""


@dataclass(frozen=True)
class ApprovedToolCall:
    """Canonical tool call safe for public state and execution."""

    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    audit_metadata: dict[str, Any] = field(default_factory=dict)
    provider_extra: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict:
        """Return the canonical runtime call.

        The runtime deliberately keeps approved arguments structured. Provider
        adapters serialize them at their own wire boundary; Session Parts and
        the executor therefore consume the exact same approved mapping.
        """
        return {
            "id": self.call_id,
            "type": "function",
            "function": {
                "name": self.tool_name,
                "arguments": copy.deepcopy(self.arguments),
            },
            **(
                {"provider_extra": copy.deepcopy(self.provider_extra)}
                if self.provider_extra
                else {}
            ),
        }


def normalize_raw_tool_calls(
    calls: list,
    candidate_names: list[str],
) -> tuple[list[dict], tuple[dict, ...]]:
    """Repair structural/name/id damage before policy inspects a call."""
    normalized, envelope_repairs = repair_tool_call_envelopes(list(calls))
    normalized, name_repairs = repair_tool_call_names(normalized, candidate_names)
    normalized, id_repairs = repair_tool_call_ids(normalized)
    return normalized, tuple([*envelope_repairs, *name_repairs, *id_repairs])


def approved_tool_call(call: dict, arguments: dict[str, Any]) -> ApprovedToolCall:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    return ApprovedToolCall(
        call_id=str(call.get("id") or call.get("tool_call_id") or ""),
        tool_name=str(function.get("name") or "unknown"),
        arguments=copy.deepcopy(arguments),
        provider_extra=(
            copy.deepcopy(call["provider_extra"])
            if isinstance(call.get("provider_extra"), dict)
            else {}
        ),
    )
