"""Tool registry: maps tool names to handlers and OpenAI function specs."""

import json
from typing import Callable

# All registered tool specs (OpenAI function calling format)
TOOL_SPECS: list[dict] = []

# Handler map: tool_name -> callable(**kwargs) -> str
TOOL_HANDLERS: dict[str, Callable] = {}


def register(name: str, description: str, parameters: dict, handler: Callable):
    """Register a tool with its spec and handler."""
    TOOL_SPECS.append({
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    })
    TOOL_HANDLERS[name] = handler


def dispatch(name: str, arguments) -> str:
    """Dispatch a tool call by name. Returns the output string."""
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return f"Error: Unknown tool '{name}'"
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return f"Error: Invalid JSON arguments for {name}"
    try:
        result = handler(**arguments)
        return str(result) if result is not None else "(no output)"
    except TypeError as e:
        return f"Error: Bad arguments for {name}: {e}"
    except Exception as e:
        return f"Error: {e}"


def get_specs() -> list[dict]:
    """Return all tool specs for the OpenAI API call."""
    return list(TOOL_SPECS)
