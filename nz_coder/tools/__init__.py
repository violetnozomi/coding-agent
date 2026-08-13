"""Tool registry: maps tool names to handlers and OpenAI function specs."""
from __future__ import annotations

import importlib
import inspect
import json
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable

from nz_coder.attachments import normalize_attachments


class ToolOutput(str):
    """String-compatible tool output with internal durable result metadata.

    Tool handlers keep the public ``str`` return contract while the executor
    can persist title/metadata without encoding control facts into visible
    output text.
    """

    def __new__(
        cls,
        value: str,
        *,
        title: str = "",
        metadata: dict | None = None,
        attachments: list[dict] | None = None,
    ):
        instance = super().__new__(cls, str(value))
        instance.title = str(title)
        instance.metadata = dict(metadata or {})
        instance.attachments = normalize_attachments(attachments)
        return instance


# All registered tool specs (OpenAI function calling format)
TOOL_SPECS: list[dict] = []

# Handler map: tool_name -> callable(**kwargs) -> str
TOOL_HANDLERS: dict[str, Callable] = {}

# Execution effect used by the agent-loop scheduler. Unknown and legacy tools
# default to "serial": parallelism must be explicitly opted into.
TOOL_EXECUTION_MODES: dict[str, str] = {}
_VALID_EXECUTION_MODES = frozenset({"read", "serial", "write"})

# Optional tool packs: pack_name -> metadata.
OPTIONAL_TOOL_PACKS: dict[str, dict] = {}

# Reverse index: tool_name -> optional pack name.
OPTIONAL_TOOL_TO_PACK: dict[str, str] = {}

# Per-execution dynamic tools (for example MCP). Keeping these out of the
# module-level registry prevents concurrent workspaces from seeing each
# other's handlers or schemas.
_DYNAMIC_TOOLS: ContextVar[dict[str, dict] | None] = ContextVar(
    "nz_coder_dynamic_tools",
    default=None,
)
_DYNAMIC_TOOL_PROVIDER: ContextVar[Callable[[], list[dict]] | None] = ContextVar(
    "nz_coder_dynamic_tool_provider",
    default=None,
)
_TOOL_METADATA_REPORTER: ContextVar[
    Callable[[str, dict], None] | None
] = ContextVar(
    "nz_coder_tool_metadata_reporter",
    default=None,
)
_TOOL_CALL_ID: ContextVar[str] = ContextVar(
    "nz_coder_tool_call_id",
    default="",
)
_TOOL_CANCEL_EVENT: ContextVar[threading.Event | None] = ContextVar(
    "nz_coder_tool_cancel_event",
    default=None,
)


@contextmanager
def scoped_tool_metadata_reporter(reporter: Callable[[str, dict], None]):
    """Bind the running-tool metadata sink for one Agent execution scope."""
    if not callable(reporter):
        raise ValueError("Tool metadata reporter must be callable")
    token = _TOOL_METADATA_REPORTER.set(reporter)
    try:
        yield
    finally:
        _TOOL_METADATA_REPORTER.reset(token)


@contextmanager
def scoped_tool_call(call_id: str):
    """Bind one provider tool-call identity around handler dispatch."""
    token = _TOOL_CALL_ID.set(str(call_id))
    try:
        yield
    finally:
        _TOOL_CALL_ID.reset(token)


def current_tool_call_id() -> str:
    """Return the tool call currently executing in this context."""
    return _TOOL_CALL_ID.get()


@contextmanager
def scoped_tool_cancellation(cancel_event: threading.Event):
    """Bind one cooperative cancellation signal to a tool worker context."""
    if not isinstance(cancel_event, threading.Event):
        raise ValueError("Tool cancellation requires a threading.Event")
    token = _TOOL_CANCEL_EVENT.set(cancel_event)
    try:
        yield
    finally:
        _TOOL_CANCEL_EVENT.reset(token)


def current_tool_cancel_event() -> threading.Event | None:
    """Return the current tool call's cooperative cancellation event."""
    return _TOOL_CANCEL_EVENT.get()


def report_tool_metadata(*, title: str = "", metadata: dict | None = None) -> bool:
    """Publish best-effort progress without changing the handler return API.

    This mirrors InfCode's ``Tool.Context.metadata``.  Both the sink and call
    identity are execution-local, preventing concurrent Agent sessions and
    parallel tool calls from crossing their progress streams.
    """
    reporter = _TOOL_METADATA_REPORTER.get()
    if reporter is None:
        return False
    try:
        reporter(str(title), dict(metadata or {}))
    except Exception:
        return False
    return True


def _dynamic_tools() -> dict[str, dict]:
    current = dict(_DYNAMIC_TOOLS.get() or {})
    provider = _DYNAMIC_TOOL_PROVIDER.get()
    if provider is None:
        return current
    return _normalize_dynamic_tools(provider(), current)


def register(
    name: str,
    description: str,
    parameters: dict,
    handler: Callable,
    *,
    execution: str = "serial",
):
    """Register a tool and its scheduler execution effect.

    ``read`` calls may run concurrently. ``write`` calls remain transaction-aware
    and serial. ``serial`` is the safe default for tools with process, session,
    registry, or otherwise unknown side effects. The metadata is internal and is
    not exposed in the provider tool schema.
    """
    if execution not in _VALID_EXECUTION_MODES:
        choices = ", ".join(sorted(_VALID_EXECUTION_MODES))
        raise ValueError(f"Invalid execution mode '{execution}'; expected one of: {choices}")
    TOOL_HANDLERS[name] = handler
    TOOL_EXECUTION_MODES[name] = execution
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


def get_execution_mode(name: str) -> str:
    """Return a tool's scheduler effect, defaulting unknown tools to serial."""
    dynamic = _dynamic_tools()
    if name in dynamic:
        return dynamic[name]["execution"]
    return TOOL_EXECUTION_MODES.get(name, "serial")


def is_transactional_dynamic_tool(name: str) -> bool:
    """Return whether a dynamic write participates in the local file transaction."""
    dynamic = _dynamic_tools()
    item = dynamic.get(name)
    if item is None:
        return True
    return bool(item.get("transactional", True))


@contextmanager
def scoped_dynamic_tools(definitions: list[dict]):
    """Bind validated dynamic tools to the current execution context.

    Each definition uses the same public fields as ``register``: ``name``,
    ``description``, ``parameters``, ``handler``, and optional ``execution``.
    Dynamic tools may not shadow built-ins or an enclosing dynamic scope.
    """
    current = dict(_DYNAMIC_TOOLS.get() or {})
    combined = _normalize_dynamic_tools(definitions, current)
    additions = {
        name: item for name, item in combined.items() if name not in current
    }
    token = _DYNAMIC_TOOLS.set(combined)
    try:
        yield list(additions)
    finally:
        _DYNAMIC_TOOLS.reset(token)


def _normalize_dynamic_tools(
    definitions: list[dict],
    current: dict[str, dict],
) -> dict[str, dict]:
    """Validate definitions and merge them into a new dynamic registry."""
    result = dict(current)
    additions: dict[str, dict] = {}
    for definition in definitions:
        name = str(definition.get("name") or "").strip()
        if not name:
            raise ValueError("Dynamic tool name cannot be empty")
        if name in TOOL_HANDLERS or name in current or name in additions:
            raise ValueError(f"Dynamic tool name collision: {name}")
        handler = definition.get("handler")
        if not callable(handler):
            raise ValueError(f"Dynamic tool '{name}' handler must be callable")
        execution = str(definition.get("execution") or "serial")
        if execution not in _VALID_EXECUTION_MODES:
            choices = ", ".join(sorted(_VALID_EXECUTION_MODES))
            raise ValueError(
                f"Invalid execution mode '{execution}'; expected one of: {choices}"
            )
        parameters = definition.get("parameters")
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}
        transactional = definition.get("transactional", execution == "write")
        if not isinstance(transactional, bool):
            raise ValueError(f"Dynamic tool '{name}' transactional must be boolean")
        additions[name] = {
            "handler": handler,
            "execution": execution,
            "transactional": transactional,
            "spec": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(definition.get("description") or ""),
                    "parameters": parameters,
                },
            },
        }

    result.update(additions)
    return result


@contextmanager
def scoped_dynamic_tool_provider(provider: Callable[[], list[dict]]):
    """Bind a live source whose definitions may change between model turns."""
    if not callable(provider):
        raise ValueError("Dynamic tool provider must be callable")
    # Validate the initial snapshot before exposing the provider.
    _normalize_dynamic_tools(provider(), dict(_DYNAMIC_TOOLS.get() or {}))
    token = _DYNAMIC_TOOL_PROVIDER.set(provider)
    try:
        yield
    finally:
        _DYNAMIC_TOOL_PROVIDER.reset(token)


@contextmanager
def scoped_dynamic_tools_disabled():
    """Hide inherited dynamic tools for a child execution scope."""
    token = _DYNAMIC_TOOLS.set({})
    provider_token = _DYNAMIC_TOOL_PROVIDER.set(None)
    try:
        yield
    finally:
        _DYNAMIC_TOOL_PROVIDER.reset(provider_token)
        _DYNAMIC_TOOLS.reset(token)


def register_optional_pack(
    name: str,
    *,
    module: str,
    tool_names: list[str],
    description: str,
    tool_effects: dict[str, str] | None = None,
) -> None:
    """Register an optional tool pack without importing its module yet."""
    declared_effects = dict(tool_effects or {})
    unknown_tools = sorted(set(declared_effects) - set(tool_names))
    if unknown_tools:
        raise ValueError(
            f"Optional pack '{name}' declares effects for unknown tools: "
            + ", ".join(unknown_tools)
        )
    invalid_effects = sorted(set(declared_effects.values()) - _VALID_EXECUTION_MODES)
    if invalid_effects:
        raise ValueError(
            f"Optional pack '{name}' has invalid effects: "
            + ", ".join(invalid_effects)
        )
    OPTIONAL_TOOL_PACKS[name] = {
        "module": module,
        "tool_names": list(tool_names),
        "description": description,
        "tool_effects": {
            tool_name: declared_effects.get(tool_name, "serial")
            for tool_name in tool_names
        },
    }
    for tool_name in tool_names:
        OPTIONAL_TOOL_TO_PACK[tool_name] = name


def _optional_pack_loaded(name: str) -> bool:
    pack = OPTIONAL_TOOL_PACKS.get(name)
    if not pack:
        return False
    return all(tool_name in TOOL_HANDLERS for tool_name in pack["tool_names"])


def load_optional_pack(name: str) -> dict:
    """Import and register an optional tool pack."""
    pack = OPTIONAL_TOOL_PACKS.get(name)
    if pack is None:
        choices = ", ".join(sorted(OPTIONAL_TOOL_PACKS))
        raise ValueError(f"Unknown optional tool pack '{name}'. Available packs: {choices or '(none)'}")

    importlib.import_module(pack["module"])
    missing = [tool_name for tool_name in pack["tool_names"] if tool_name not in TOOL_HANDLERS]
    if missing:
        raise RuntimeError(
            f"Optional tool pack '{name}' did not register expected tools: {', '.join(missing)}"
        )
    return {
        "name": name,
        "module": pack["module"],
        "description": pack["description"],
        "tool_names": list(pack["tool_names"]),
        "loaded": True,
    }


def list_optional_packs() -> list[dict]:
    """Return optional tool pack metadata with current loaded status."""
    items: list[dict] = []
    for name in sorted(OPTIONAL_TOOL_PACKS):
        pack = OPTIONAL_TOOL_PACKS[name]
        items.append({
            "name": name,
            "module": pack["module"],
            "description": pack["description"],
            "tool_names": list(pack["tool_names"]),
            "tool_effects": {
                tool_name: TOOL_EXECUTION_MODES.get(
                    tool_name,
                    pack["tool_effects"].get(tool_name, "serial"),
                )
                for tool_name in pack["tool_names"]
            },
            "loaded": _optional_pack_loaded(name),
        })
    return items


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
    cancel_event = _TOOL_CANCEL_EVENT.get()
    if cancel_event is not None and cancel_event.is_set():
        return "Error: Tool execution cancelled"
    dynamic = _dynamic_tools()
    handler = dynamic.get(name, {}).get("handler") or TOOL_HANDLERS.get(name)
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
        if isinstance(result, str):
            return result
        return str(result) if result is not None else "(no output)"
    except TypeError as e:
        return f"Error: Bad arguments for {name}: {e}"
    except Exception as e:
        return f"Error: {e}"


def get_specs() -> list[dict]:
    """Return all tool specs for the OpenAI API call."""
    return get_catalog_specs()


def get_catalog_specs() -> list[dict]:
    """Return the complete run-local catalog before progressive exposure."""
    dynamic = _dynamic_tools()
    return list(TOOL_SPECS) + [item["spec"] for item in dynamic.values()]


from . import optional_loader  # noqa: F401,E402
from . import tool_search  # noqa: F401,E402
from . import repo_context  # noqa: F401,E402
from . import semantic_search  # noqa: F401,E402
from . import mcp_catalog  # noqa: F401,E402
