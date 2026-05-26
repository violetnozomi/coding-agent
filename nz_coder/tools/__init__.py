"""Tool registry: maps tool names to handlers and OpenAI function specs."""
from __future__ import annotations


import json
import inspect
from typing import Callable

# All registered tool specs (OpenAI function calling format)
TOOL_SPECS: list[dict] = []

# Handler map: tool_name -> callable(**kwargs) -> str
TOOL_HANDLERS: dict[str, Callable] = {}


def register(name: str, description: str, parameters: dict, handler: Callable):
    """注册工具。幂等：重复注册同名工具时替换已有条目，而非追加。"""
    TOOL_HANDLERS[name] = handler
    spec = {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }
    for i, existing in enumerate(TOOL_SPECS):
        if existing["function"]["name"] == name:
            TOOL_SPECS[i] = spec
            return
    TOOL_SPECS.append(spec)


def _format_param_error(tool_name: str, arguments: dict, handler: Callable) -> str | None:
    """Validate arguments against the handler signature and return a
    LLM-friendly error message, or None if everything looks fine.

    Checks for:
    - missing required parameters (no default, not VAR_KEYWORD/VAR_POSITIONAL)
    - type mismatches for simple scalar types (str, int, float, bool)

    Intentionally does NOT flag unexpected/extra parameters — the dispatch
    function silently drops them, and LLMs occasionally send extra fields
    like `description` or `explanation` which should not cause failures.
    """
    sig = inspect.signature(handler)
    params = sig.parameters

    issues: list[str] = []

    # Missing required parameters
    for pname, param in params.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if param.default is inspect.Parameter.empty and pname not in arguments:
            issues.append(f"The required parameter `{pname}` is missing")

    # Type mismatches for annotated scalar parameters
    _SCALAR_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}
    for pname, param in params.items():
        if pname not in arguments:
            continue
        ann = param.annotation
        if ann is inspect.Parameter.empty or ann not in _SCALAR_TYPES:
            continue
        val = arguments[pname]
        if not isinstance(val, ann):
            actual = type(val).__name__
            issues.append(
                f"The parameter `{pname}` type is expected as "
                f"`{_SCALAR_TYPES[ann]}` but provided as `{actual}`"
            )

    if not issues:
        return None
    count = len(issues)
    return (
        f"{tool_name} failed due to the following "
        f"{'issue' if count == 1 else 'issues'}:\n"
        + "\n".join(issues)
    )


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
    if not isinstance(arguments, dict):
        return f"Error: Invalid arguments for {name}: expected object"

    # Structured parameter validation before dispatch
    param_error = _format_param_error(name, arguments, handler)
    if param_error:
        return f"Error: {param_error}"

    sig = inspect.signature(handler)
    if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        arguments = {k: v for k, v in arguments.items() if k in sig.parameters}
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
