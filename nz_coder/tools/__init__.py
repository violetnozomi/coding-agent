"""Tool registry: maps tool names to handlers and OpenAI function specs."""
from __future__ import annotations

import copy
import importlib
import inspect
import json
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, get_type_hints

from nz_coder.protocol.attachments import normalize_attachments


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

# Dominant side effect is separate from scheduler execution. In particular,
# product-state writes must not masquerade as task-workspace mutations.
TOOL_SIDE_EFFECTS: dict[str, str] = {}
_VALID_SIDE_EFFECTS = frozenset({
    "readonly",
    "reads-network",
    "mutates-fs",
    "mutates-shell",
    "mutates-network",
    "mutates-state",
})
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Explicit planning-loop exceptions. Missing means the default policy applies:
# registered readonly tools are allowed and every other effect is blocked.
TOOL_PLAN_MODE_ALLOWED: dict[str, bool] = {}

# RuntimeState is intentionally usable without importing every handler module.
# Keep this one bootstrap set aligned with registrations; registry-parity tests
# catch drift. This mirrors InfCodeX's cycle-safe MUTATES_FS_TOOL_NAMES catalog.
FILESYSTEM_MUTATION_TOOLS = frozenset({
    "apply_agent_changes",
    "apply_patch",
    "edit_file",
    "python_structural_edit",
    "replace_lines",
    "scaffold_project",
    "write_file",
    "write_files_batch",
})

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

# Static tools are normally registered during import, but optional packs can
# load while HTTP sessions are already serving catalog reads.  Keep each
# registry generation coherent for those readers.
_STATIC_REGISTRY_LOCK = threading.RLock()


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


def _validate_json_schema(parameters: dict, *, label: str) -> None:
    try:
        json.dumps(parameters, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} parameters must be JSON-serializable") from exc


def register(
    name: str,
    description: str,
    parameters: dict,
    handler: Callable,
    *,
    execution: str = "serial",
    side_effect: str | None = None,
    plan_mode_allowed: bool | None = None,
):
    """Register a tool with independent scheduling and side-effect metadata.

    ``read`` calls may run concurrently. ``write`` calls remain transaction-aware
    and serial. ``serial`` is the safe default for tools with process, session,
    registry, or otherwise unknown side effects. ``side_effect`` distinguishes
    task-filesystem mutations from internal state and remote effects. The
    ``plan_mode_allowed`` is an explicit override for planning-loop tools whose
    state mutation is itself part of planning. Metadata stays internal and is
    not exposed in the provider tool schema.
    """
    if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name):
        raise ValueError(
            "Tool name must contain 1-64 ASCII letters, digits, underscores, "
            "or hyphens"
        )
    if not isinstance(description, str):
        raise ValueError("Tool description must be a string")
    if not isinstance(parameters, dict):
        raise ValueError("Tool parameters must be a JSON schema object")
    _validate_json_schema(parameters, label=f"Tool '{name}'")
    if not callable(handler):
        raise ValueError(f"Tool '{name}' handler must be callable")
    if execution not in _VALID_EXECUTION_MODES:
        choices = ", ".join(sorted(_VALID_EXECUTION_MODES))
        raise ValueError(f"Invalid execution mode '{execution}'; expected one of: {choices}")
    selected_effect = side_effect or _default_side_effect(
        execution,
        transactional=True,
    )
    if selected_effect not in _VALID_SIDE_EFFECTS:
        choices = ", ".join(sorted(_VALID_SIDE_EFFECTS))
        raise ValueError(
            f"Invalid side effect '{selected_effect}'; expected one of: {choices}"
        )
    if plan_mode_allowed is not None and not isinstance(plan_mode_allowed, bool):
        raise ValueError("plan_mode_allowed must be boolean or None")
    spec = {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }
    with _STATIC_REGISTRY_LOCK:
        TOOL_HANDLERS[name] = handler
        TOOL_EXECUTION_MODES[name] = execution
        TOOL_SIDE_EFFECTS[name] = selected_effect
        if plan_mode_allowed is None:
            TOOL_PLAN_MODE_ALLOWED.pop(name, None)
        else:
            TOOL_PLAN_MODE_ALLOWED[name] = plan_mode_allowed
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
    with _STATIC_REGISTRY_LOCK:
        if name in TOOL_EXECUTION_MODES:
            return TOOL_EXECUTION_MODES[name]
        pack_name = OPTIONAL_TOOL_TO_PACK.get(name)
        pack = OPTIONAL_TOOL_PACKS.get(pack_name or "")
        if pack is not None:
            return str(pack.get("tool_effects", {}).get(name, "serial"))
    return "serial"


def _default_side_effect(execution: str, *, transactional: bool) -> str:
    if execution == "read":
        return "readonly"
    if execution == "write":
        return "mutates-fs" if transactional else "mutates-network"
    return "mutates-state"


def get_tool_side_effect(name: str) -> str:
    """Return one tool's dominant effect, failing closed as state mutation."""
    dynamic = _dynamic_tools()
    if name in dynamic:
        return str(dynamic[name]["side_effect"])
    with _STATIC_REGISTRY_LOCK:
        if name in FILESYSTEM_MUTATION_TOOLS:
            return "mutates-fs"
        if name in TOOL_SIDE_EFFECTS:
            return TOOL_SIDE_EFFECTS[name]
        pack_name = OPTIONAL_TOOL_TO_PACK.get(name)
        pack = OPTIONAL_TOOL_PACKS.get(pack_name or "")
        if pack is not None:
            execution = str(pack.get("tool_effects", {}).get(name, "serial"))
            return _default_side_effect(execution, transactional=True)
    return "mutates-state"


def get_dynamic_tool_binding_identity(name: str) -> str | None:
    """Return the active dynamic binding's opaque authorization identity."""
    identity = (_dynamic_tools().get(name) or {}).get("binding_identity")
    return identity if isinstance(identity, str) and identity else None


def get_tool_policy_snapshot() -> dict[str, dict]:
    """Return a run-local declarative policy snapshot for every active tool."""
    dynamic = _dynamic_tools()
    snapshot: dict[str, dict] = {}
    with _STATIC_REGISTRY_LOCK:
        for name in TOOL_HANDLERS:
            effect = (
                "mutates-fs"
                if name in FILESYSTEM_MUTATION_TOOLS
                else TOOL_SIDE_EFFECTS.get(name, "mutates-state")
            )
            override = TOOL_PLAN_MODE_ALLOWED.get(name)
            snapshot[name] = {
                "side_effect": effect,
                "plan_mode_allowed": (
                    override if isinstance(override, bool) else effect == "readonly"
                ),
            }
    for name, definition in dynamic.items():
        effect = str(definition["side_effect"])
        override = definition.get("plan_mode_allowed")
        snapshot[name] = {
            "side_effect": effect,
            "plan_mode_allowed": (
                override if isinstance(override, bool) else effect == "readonly"
            ),
        }
    return snapshot


def is_filesystem_mutation_tool(name: str) -> bool:
    """Return whether a successful call mutates task-workspace files."""
    return get_tool_side_effect(str(name or "")) == "mutates-fs"


def is_tool_plan_mode_allowed(name: str) -> bool:
    """Return declarative Plan-mode eligibility, failing closed for unknowns."""
    metadata = get_tool_policy_snapshot().get(str(name or ""))
    return bool(metadata and metadata["plan_mode_allowed"])


def is_tool_read_capability(name: str) -> bool:
    """Return whether a read-only worker may use the tool by effect class."""
    metadata = get_tool_policy_snapshot().get(str(name or ""))
    return bool(
        metadata
        and metadata["side_effect"] in {"readonly", "reads-network"}
    )


def collect_filesystem_mutation_paths(tool_input: dict | None) -> tuple[str, ...]:
    """Collect explicit mutation paths from nested tool input.

    Only path-shaped fields are considered; computed destinations remain
    unattributed so acceptance invalidation stays conservative.
    """
    singular_keys = frozenset({
        "destination_path",
        "file_path",
        "path",
        "source_path",
    })
    plural_keys = frozenset({"files", "paths", "reviewed_files"})
    paths: list[str] = []
    seen_objects: set[int] = set()

    def add(value: object) -> None:
        if not isinstance(value, str):
            return
        normalized = value.strip().replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized and normalized not in paths:
            paths.append(normalized)

    def visit(value: object, key: str = "") -> None:
        if isinstance(value, str):
            if key in singular_keys or key in plural_keys:
                add(value)
            return
        if isinstance(value, list):
            for item in value:
                visit(item, key)
            return
        if not isinstance(value, dict):
            return
        identity = id(value)
        if identity in seen_objects:
            return
        seen_objects.add(identity)
        for child_key, child_value in value.items():
            visit(child_value, str(child_key))

    visit(tool_input or {})
    return tuple(paths)


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
        if not isinstance(definition, dict):
            raise ValueError("Dynamic tool definition must be an object")
        name = str(definition.get("name") or "").strip()
        if not _TOOL_NAME_RE.fullmatch(name):
            raise ValueError(
                "Dynamic tool name must contain 1-64 ASCII letters, digits, "
                "underscores, or hyphens"
            )
        with _STATIC_REGISTRY_LOCK:
            static_collision = (
                name in TOOL_HANDLERS or name in OPTIONAL_TOOL_TO_PACK
            )
        if static_collision or name in current or name in additions:
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
        _validate_json_schema(parameters, label=f"Dynamic tool '{name}'")
        transactional = definition.get("transactional", execution == "write")
        if not isinstance(transactional, bool):
            raise ValueError(f"Dynamic tool '{name}' transactional must be boolean")
        side_effect = str(
            definition.get("side_effect")
            or _default_side_effect(execution, transactional=transactional)
        )
        if side_effect not in _VALID_SIDE_EFFECTS:
            choices = ", ".join(sorted(_VALID_SIDE_EFFECTS))
            raise ValueError(
                f"Dynamic tool '{name}' side_effect must be one of: {choices}"
            )
        plan_mode_allowed = definition.get("plan_mode_allowed")
        if plan_mode_allowed is not None and not isinstance(
            plan_mode_allowed, bool
        ):
            raise ValueError(
                f"Dynamic tool '{name}' plan_mode_allowed must be boolean or None"
            )
        binding_identity = definition.get("binding_identity")
        if binding_identity is not None and (
            not isinstance(binding_identity, str)
            or re.fullmatch(r"[0-9a-f]{64}", binding_identity) is None
        ):
            raise ValueError(
                f"Dynamic tool '{name}' binding_identity must be a SHA-256 digest"
            )
        additions[name] = {
            "handler": handler,
            "execution": execution,
            "transactional": transactional,
            "side_effect": side_effect,
            "plan_mode_allowed": plan_mode_allowed,
            "binding_identity": binding_identity,
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
def scoped_dynamic_tool_snapshot():
    """Freeze the live dynamic registry for one authorization/dispatch scope.

    A provider is intentionally live between model turns, but permission,
    scheduling metadata, and handler dispatch for one concrete call must see
    the same generation. The resolved snapshot is inherited by worker threads
    through their copied context.
    """
    provider = _DYNAMIC_TOOL_PROVIDER.get()
    if provider is None:
        yield
        return
    resolved = _normalize_dynamic_tools(
        provider(),
        dict(_DYNAMIC_TOOLS.get() or {}),
    )
    tools_token = _DYNAMIC_TOOLS.set(resolved)
    provider_token = _DYNAMIC_TOOL_PROVIDER.set(None)
    try:
        yield
    finally:
        _DYNAMIC_TOOL_PROVIDER.reset(provider_token)
        _DYNAMIC_TOOLS.reset(tools_token)


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
    candidate = {
        "module": module,
        "tool_names": list(tool_names),
        "description": description,
        "tool_effects": {
            tool_name: declared_effects.get(tool_name, "serial")
            for tool_name in tool_names
        },
    }
    with _STATIC_REGISTRY_LOCK:
        existing = OPTIONAL_TOOL_PACKS.get(name)
        if existing is not None:
            if existing == candidate:
                return
            raise ValueError(
                f"Optional pack '{name}' is already registered with a different definition"
            )
        active_collisions = sorted(
            tool_name
            for tool_name in tool_names
            if tool_name in TOOL_HANDLERS
            and OPTIONAL_TOOL_TO_PACK.get(tool_name) != name
        )
        if active_collisions:
            raise ValueError(
                f"Optional pack '{name}' cannot claim already registered tools: "
                + ", ".join(active_collisions)
            )
        owned_elsewhere = {
            tool_name: OPTIONAL_TOOL_TO_PACK[tool_name]
            for tool_name in tool_names
            if tool_name in OPTIONAL_TOOL_TO_PACK
            and OPTIONAL_TOOL_TO_PACK[tool_name] != name
        }
        if owned_elsewhere:
            detail = ", ".join(
                f"{tool_name} (already belongs to {owner})"
                for tool_name, owner in sorted(owned_elsewhere.items())
            )
            raise ValueError(f"Optional pack '{name}' tool collision: {detail}")
        OPTIONAL_TOOL_PACKS[name] = candidate
        for tool_name in tool_names:
            OPTIONAL_TOOL_TO_PACK[tool_name] = name


def _optional_pack_loaded(name: str) -> bool:
    with _STATIC_REGISTRY_LOCK:
        pack = OPTIONAL_TOOL_PACKS.get(name)
        if not pack:
            return False
        return all(tool_name in TOOL_HANDLERS for tool_name in pack["tool_names"])


def load_optional_pack(name: str) -> dict:
    """Import and register an optional tool pack."""
    with _STATIC_REGISTRY_LOCK:
        pack = OPTIONAL_TOOL_PACKS.get(name)
        if pack is None:
            choices = ", ".join(sorted(OPTIONAL_TOOL_PACKS))
            raise ValueError(f"Unknown optional tool pack '{name}'. Available packs: {choices or '(none)'}")
        snapshots = (
            list(TOOL_SPECS),
            dict(TOOL_HANDLERS),
            dict(TOOL_EXECUTION_MODES),
            dict(TOOL_SIDE_EFFECTS),
            dict(TOOL_PLAN_MODE_ALLOWED),
        )
        try:
            importlib.import_module(pack["module"])
            missing = [
                tool_name
                for tool_name in pack["tool_names"]
                if tool_name not in TOOL_HANDLERS
            ]
            if missing:
                raise RuntimeError(
                    f"Optional tool pack '{name}' did not register expected tools: "
                    + ", ".join(missing)
                )
        except BaseException:
            TOOL_SPECS[:] = snapshots[0]
            TOOL_HANDLERS.clear()
            TOOL_HANDLERS.update(snapshots[1])
            TOOL_EXECUTION_MODES.clear()
            TOOL_EXECUTION_MODES.update(snapshots[2])
            TOOL_SIDE_EFFECTS.clear()
            TOOL_SIDE_EFFECTS.update(snapshots[3])
            TOOL_PLAN_MODE_ALLOWED.clear()
            TOOL_PLAN_MODE_ALLOWED.update(snapshots[4])
            raise
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
    with _STATIC_REGISTRY_LOCK:
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
    try:
        resolved_annotations = get_type_hints(handler)
    except Exception:
        resolved_annotations = {}

    issues: list[str] = []

    # Missing required parameters
    for pname, param in params.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if param.default is inspect.Parameter.empty and pname not in arguments:
            issues.append(f"The required parameter `{pname}` is missing")

    # Type mismatches for annotated scalar parameters
    _SCALAR_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}

    def scalar_matches(annotation: type, value: object) -> bool:
        if annotation is bool:
            return type(value) is bool
        if annotation is int:
            return type(value) is int
        if annotation is float:
            return type(value) in {int, float}
        return isinstance(value, annotation)

    for pname, param in params.items():
        if pname not in arguments:
            continue
        ann = resolved_annotations.get(pname, param.annotation)
        if ann is inspect.Parameter.empty or ann not in _SCALAR_TYPES:
            continue
        val = arguments[pname]
        if not scalar_matches(ann, val):
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
    dynamic_definition = dynamic.get(name)
    if dynamic_definition is not None:
        handler = dynamic_definition.get("handler")
    else:
        with _STATIC_REGISTRY_LOCK:
            handler = TOOL_HANDLERS.get(name)
    if handler is None:
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
    with _STATIC_REGISTRY_LOCK:
        static_specs = copy.deepcopy(TOOL_SPECS)
    return static_specs + copy.deepcopy([
        item["spec"] for item in dynamic.values()
    ])


from . import optional_loader  # noqa: F401,E402
from . import tool_search  # noqa: F401,E402
from . import repo_context  # noqa: F401,E402
from . import semantic_search  # noqa: F401,E402
from . import mcp_catalog  # noqa: F401,E402
