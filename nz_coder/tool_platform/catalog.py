"""Immutable logical catalog adapted from NZ-Coder's legacy tool registry."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass


def estimate_schema_tokens(spec: dict) -> int:
    """Return a deterministic coarse token estimate without requiring tiktoken."""
    encoded = json.dumps(spec, ensure_ascii=False, separators=(",", ":"))
    return max(1, (len(encoded) + 3) // 4)


@dataclass(frozen=True)
class ToolDefinition:
    """Provider-neutral immutable view of one registered tool schema."""

    name: str
    description: str
    parameters: dict
    schema_tokens: int
    dynamic: bool = False

    @classmethod
    def from_spec(cls, spec: dict) -> ToolDefinition:
        function = spec.get("function") if isinstance(spec, dict) else None
        if not isinstance(function, dict):
            raise ValueError("Tool spec must contain a function object")
        name = str(function.get("name") or "").strip()
        if not name:
            raise ValueError("Tool definition name must be non-empty")
        parameters = function.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}
        normalized = {
            "type": "function",
            "function": {
                "name": name,
                "description": str(function.get("description") or ""),
                "parameters": copy.deepcopy(parameters),
            },
        }
        return cls(
            name=name,
            description=normalized["function"]["description"],
            parameters=normalized["function"]["parameters"],
            schema_tokens=estimate_schema_tokens(normalized),
        )

    def spec(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": copy.deepcopy(self.parameters),
            },
        }


class ToolCatalog:
    """Deterministic name-indexed collection of ToolDefinition values."""

    def __init__(self, definitions) -> None:
        items = tuple(definitions)
        by_name = {}
        for definition in items:
            if not isinstance(definition, ToolDefinition):
                raise TypeError("ToolCatalog requires ToolDefinition values")
            if definition.name in by_name:
                raise ValueError(f"Duplicate tool definition: {definition.name}")
            by_name[definition.name] = definition
        self._definitions = tuple(sorted(items, key=lambda item: item.name))
        self._by_name = by_name

    @classmethod
    def from_specs(cls, specs: list[dict]) -> ToolCatalog:
        return cls(ToolDefinition.from_spec(spec) for spec in specs)

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._definitions

    def names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self._definitions)

    def get(self, name: str) -> ToolDefinition | None:
        return self._by_name.get(str(name))

    def require(self, name: str) -> ToolDefinition:
        result = self.get(name)
        if result is None:
            raise KeyError(name)
        return result

    @property
    def schema_tokens(self) -> int:
        return sum(item.schema_tokens for item in self._definitions)


__all__ = ["ToolCatalog", "ToolDefinition", "estimate_schema_tokens"]
