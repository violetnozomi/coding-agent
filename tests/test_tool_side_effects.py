"""Tests for declarative tool side effects and filesystem attribution."""
from __future__ import annotations

import pytest


def test_plan_mode_policy_uses_effect_metadata_and_explicit_overrides():
    """Plan visibility must fail closed without a planning-loop override."""
    from nz_coder.tools import (
        TOOL_EXECUTION_MODES,
        TOOL_HANDLERS,
        TOOL_PLAN_MODE_ALLOWED,
        TOOL_SIDE_EFFECTS,
        TOOL_SPECS,
        get_tool_policy_snapshot,
        is_tool_plan_mode_allowed,
        register,
    )

    names = (
        "_test_plan_read",
        "_test_plan_state",
        "_test_plan_override",
    )
    try:
        register(
            names[0],
            "test",
            {"type": "object", "properties": {}},
            lambda: "ok",
            execution="read",
        )
        register(
            names[1],
            "test",
            {"type": "object", "properties": {}},
            lambda: "ok",
            side_effect="mutates-state",
        )
        register(
            names[2],
            "test",
            {"type": "object", "properties": {}},
            lambda: "ok",
            side_effect="mutates-state",
            plan_mode_allowed=True,
        )

        assert is_tool_plan_mode_allowed(names[0]) is True
        assert is_tool_plan_mode_allowed(names[1]) is False
        assert is_tool_plan_mode_allowed(names[2]) is True
        assert is_tool_plan_mode_allowed("_test_unknown_tool") is False
        snapshot = get_tool_policy_snapshot()
        assert snapshot[names[1]] == {
            "side_effect": "mutates-state",
            "plan_mode_allowed": False,
        }
        assert snapshot[names[2]]["plan_mode_allowed"] is True
    finally:
        for name in names:
            TOOL_HANDLERS.pop(name, None)
            TOOL_EXECUTION_MODES.pop(name, None)
            TOOL_SIDE_EFFECTS.pop(name, None)
            TOOL_PLAN_MODE_ALLOWED.pop(name, None)
        TOOL_SPECS[:] = [
            spec for spec in TOOL_SPECS
            if spec["function"]["name"] not in names
        ]


def test_builtin_filesystem_catalog_matches_registered_effects():
    """Every cycle-safe filesystem entry must register as an actual write."""
    import nz_coder.project_creation.templates  # noqa: F401
    import nz_coder.runtime.agent.agent_manager  # noqa: F401
    import nz_coder.tools.files  # noqa: F401
    import nz_coder.tools.python_ast  # noqa: F401
    from nz_coder.tools import (
        FILESYSTEM_MUTATION_TOOLS,
        get_execution_mode,
        get_tool_side_effect,
    )

    assert FILESYSTEM_MUTATION_TOOLS
    assert all(
        get_execution_mode(name) == "write"
        for name in FILESYSTEM_MUTATION_TOOLS
    )
    assert all(
        get_tool_side_effect(name) == "mutates-fs"
        for name in FILESYSTEM_MUTATION_TOOLS
    )


def test_unloaded_optional_pack_keeps_declared_effect_metadata():
    """Permission checks must not treat an unloaded read pack as unknown state."""
    import nz_coder.tools.optional_loader  # noqa: F401
    from nz_coder.tools import get_tool_side_effect

    assert get_tool_side_effect("lsp") == "readonly"
    assert get_tool_side_effect("python_symbol_check") == "readonly"
    assert get_tool_side_effect("python_structural_edit") == "mutates-fs"


def test_loaded_optional_registration_overrides_bootstrap_declaration():
    """Loaded handler metadata is authoritative over its pre-import hint."""
    from nz_coder.tools import (
        OPTIONAL_TOOL_PACKS,
        OPTIONAL_TOOL_TO_PACK,
        TOOL_EXECUTION_MODES,
        TOOL_HANDLERS,
        TOOL_PLAN_MODE_ALLOWED,
        TOOL_SIDE_EFFECTS,
        TOOL_SPECS,
        get_execution_mode,
        get_tool_side_effect,
        register,
        register_optional_pack,
    )

    pack_name = "_test_optional_policy_pack"
    tool_name = "_test_optional_policy_tool"
    try:
        register_optional_pack(
            pack_name,
            module="nz_coder.tools.optional_loader",
            tool_names=[tool_name],
            description="test",
            tool_effects={tool_name: "read"},
        )
        assert get_execution_mode(tool_name) == "read"
        assert get_tool_side_effect(tool_name) == "readonly"

        register(
            tool_name,
            "test",
            {"type": "object", "properties": {}},
            lambda: "ok",
            execution="serial",
            side_effect="mutates-state",
        )

        assert get_execution_mode(tool_name) == "serial"
        assert get_tool_side_effect(tool_name) == "mutates-state"
    finally:
        OPTIONAL_TOOL_PACKS.pop(pack_name, None)
        OPTIONAL_TOOL_TO_PACK.pop(tool_name, None)
        TOOL_HANDLERS.pop(tool_name, None)
        TOOL_EXECUTION_MODES.pop(tool_name, None)
        TOOL_SIDE_EFFECTS.pop(tool_name, None)
        TOOL_PLAN_MODE_ALLOWED.pop(tool_name, None)
        TOOL_SPECS[:] = [
            spec for spec in TOOL_SPECS
            if spec["function"]["name"] != tool_name
        ]


def test_dynamic_tool_cannot_claim_unloaded_optional_pack_name():
    """Reserved optional names must not become duplicate run-local schemas."""
    from nz_coder.tools import (
        OPTIONAL_TOOL_PACKS,
        OPTIONAL_TOOL_TO_PACK,
        register_optional_pack,
        scoped_dynamic_tools,
    )

    pack_name = "_test_reserved_dynamic_pack"
    tool_name = "_test_reserved_dynamic_tool"
    try:
        register_optional_pack(
            pack_name,
            module="nz_coder.tools.optional_loader",
            tool_names=[tool_name],
            description="test",
        )

        with pytest.raises(ValueError, match="name collision"):
            with scoped_dynamic_tools([{
                "name": tool_name,
                "handler": lambda: "dynamic",
                "execution": "read",
            }]):
                pass
    finally:
        OPTIONAL_TOOL_PACKS.pop(pack_name, None)
        OPTIONAL_TOOL_TO_PACK.pop(tool_name, None)


def test_optional_tool_name_has_one_declared_pack_owner():
    """Two lazy packs must not silently overwrite the reverse ownership index."""
    from nz_coder.tools import (
        OPTIONAL_TOOL_PACKS,
        OPTIONAL_TOOL_TO_PACK,
        register_optional_pack,
    )

    first = "_test_optional_owner_first"
    second = "_test_optional_owner_second"
    tool_name = "_test_optional_owned_tool"
    try:
        register_optional_pack(
            first,
            module="nz_coder.tools.optional_loader",
            tool_names=[tool_name],
            description="first",
        )

        with pytest.raises(ValueError, match="already belongs"):
            register_optional_pack(
                second,
                module="nz_coder.tools.optional_loader",
                tool_names=[tool_name],
                description="second",
            )

        assert OPTIONAL_TOOL_TO_PACK[tool_name] == first
        assert second not in OPTIONAL_TOOL_PACKS
    finally:
        OPTIONAL_TOOL_PACKS.pop(first, None)
        OPTIONAL_TOOL_PACKS.pop(second, None)
        OPTIONAL_TOOL_TO_PACK.pop(tool_name, None)


def test_optional_pack_cannot_relabel_an_active_non_optional_tool():
    """A lazy declaration must not claim an already active built-in name."""
    from nz_coder.tools import (
        OPTIONAL_TOOL_PACKS,
        OPTIONAL_TOOL_TO_PACK,
        TOOL_EXECUTION_MODES,
        TOOL_HANDLERS,
        TOOL_PLAN_MODE_ALLOWED,
        TOOL_SIDE_EFFECTS,
        TOOL_SPECS,
        register,
        register_optional_pack,
    )

    pack_name = "_test_optional_active_collision_pack"
    tool_name = "_test_optional_active_collision_tool"
    try:
        register(
            tool_name,
            "test",
            {"type": "object", "properties": {}},
            lambda: "ok",
            execution="read",
        )

        with pytest.raises(ValueError, match="already registered"):
            register_optional_pack(
                pack_name,
                module="nz_coder.tools.optional_loader",
                tool_names=[tool_name],
                description="test",
            )

        assert pack_name not in OPTIONAL_TOOL_PACKS
        assert tool_name not in OPTIONAL_TOOL_TO_PACK
    finally:
        OPTIONAL_TOOL_PACKS.pop(pack_name, None)
        OPTIONAL_TOOL_TO_PACK.pop(tool_name, None)
        TOOL_HANDLERS.pop(tool_name, None)
        TOOL_EXECUTION_MODES.pop(tool_name, None)
        TOOL_SIDE_EFFECTS.pop(tool_name, None)
        TOOL_PLAN_MODE_ALLOWED.pop(tool_name, None)
        TOOL_SPECS[:] = [
            spec for spec in TOOL_SPECS
            if spec["function"]["name"] != tool_name
        ]


def test_optional_pack_redefinition_cannot_leave_stale_tool_owners():
    """One pack name must not be replaced while its old reverse index survives."""
    from nz_coder.tools import (
        OPTIONAL_TOOL_PACKS,
        OPTIONAL_TOOL_TO_PACK,
        register_optional_pack,
    )

    pack_name = "_test_optional_redefinition_pack"
    first_tool = "_test_optional_redefinition_first"
    second_tool = "_test_optional_redefinition_second"
    try:
        register_optional_pack(
            pack_name,
            module="nz_coder.tools.optional_loader",
            tool_names=[first_tool],
            description="first",
            tool_effects={first_tool: "read"},
        )

        with pytest.raises(ValueError, match="already registered"):
            register_optional_pack(
                pack_name,
                module="nz_coder.tools.optional_loader",
                tool_names=[second_tool],
                description="second",
            )

        assert OPTIONAL_TOOL_PACKS[pack_name]["tool_names"] == [first_tool]
        assert OPTIONAL_TOOL_TO_PACK[first_tool] == pack_name
        assert second_tool not in OPTIONAL_TOOL_TO_PACK
    finally:
        OPTIONAL_TOOL_PACKS.pop(pack_name, None)
        OPTIONAL_TOOL_TO_PACK.pop(first_tool, None)
        OPTIONAL_TOOL_TO_PACK.pop(second_tool, None)


def test_optional_pack_load_failure_rolls_back_partial_registrations(monkeypatch):
    """A failed lazy import must not expose half of a declared tool pack."""
    from nz_coder.tools import (
        OPTIONAL_TOOL_PACKS,
        OPTIONAL_TOOL_TO_PACK,
        TOOL_EXECUTION_MODES,
        TOOL_HANDLERS,
        TOOL_PLAN_MODE_ALLOWED,
        TOOL_SIDE_EFFECTS,
        TOOL_SPECS,
        load_optional_pack,
        register,
        register_optional_pack,
    )
    import nz_coder.tools as registry

    pack_name = "_test_optional_failed_load_pack"
    first_tool = "_test_optional_failed_load_first"
    second_tool = "_test_optional_failed_load_second"

    def partial_import(_module):
        register(
            first_tool,
            "partial",
            {"type": "object", "properties": {}},
            lambda: "partial",
        )
        raise RuntimeError("import stopped")

    try:
        register_optional_pack(
            pack_name,
            module="nz_coder._test_partial_pack",
            tool_names=[first_tool, second_tool],
            description="test",
        )
        monkeypatch.setattr(registry.importlib, "import_module", partial_import)

        with pytest.raises(RuntimeError, match="import stopped"):
            load_optional_pack(pack_name)

        assert first_tool not in TOOL_HANDLERS
        assert first_tool not in TOOL_EXECUTION_MODES
        assert first_tool not in TOOL_SIDE_EFFECTS
        assert first_tool not in TOOL_PLAN_MODE_ALLOWED
        assert all(
            spec["function"]["name"] != first_tool for spec in TOOL_SPECS
        )
    finally:
        OPTIONAL_TOOL_PACKS.pop(pack_name, None)
        for tool_name in (first_tool, second_tool):
            OPTIONAL_TOOL_TO_PACK.pop(tool_name, None)
            TOOL_HANDLERS.pop(tool_name, None)
            TOOL_EXECUTION_MODES.pop(tool_name, None)
            TOOL_SIDE_EFFECTS.pop(tool_name, None)
            TOOL_PLAN_MODE_ALLOWED.pop(tool_name, None)
        TOOL_SPECS[:] = [
            spec for spec in TOOL_SPECS
            if spec["function"]["name"] not in {first_tool, second_tool}
        ]


def test_internal_product_writes_are_not_filesystem_task_mutations():
    """Workflow persistence must stay separate from task-workspace edits."""
    import nz_coder.runtime.workflows.workflow_features  # noqa: F401
    import nz_coder.runtime.workflows.workflow_library  # noqa: F401
    import nz_coder.runtime.workflows.workflow_lifecycle  # noqa: F401
    from nz_coder.tools import get_tool_side_effect

    for name in (
        "workflow_review_packet",
        "workflow_save",
        "workflow_library_mutate",
        "workflow_run_archive",
        "workflow_run_rename",
    ):
        assert get_tool_side_effect(name) == "mutates-state"


def test_dynamic_remote_write_does_not_claim_local_file_mutation():
    """A non-transactional MCP-style write is a remote, not workspace, effect."""
    from nz_coder.tools import (
        get_tool_side_effect,
        is_filesystem_mutation_tool,
        scoped_dynamic_tools,
    )

    with scoped_dynamic_tools([{
        "name": "mcp_demo_update",
        "handler": lambda: "ok",
        "execution": "write",
        "transactional": False,
    }]):
        assert get_tool_side_effect("mcp_demo_update") == "mutates-network"
        assert is_filesystem_mutation_tool("mcp_demo_update") is False


def test_filesystem_mutation_paths_cover_nested_batches_and_child_merges():
    """Path attribution covers every input shape used by real NZ write tools."""
    from nz_coder.tools import collect_filesystem_mutation_paths

    assert collect_filesystem_mutation_paths({
        "path": "./src/main.py",
        "changes": [{"path": "tests\\test_main.py"}],
        "files": [{"path": "README.md"}],
        "reviewed_files": ["src/child.py", "src/main.py"],
    }) == (
        "src/main.py",
        "tests/test_main.py",
        "README.md",
        "src/child.py",
    )


def test_dispatch_validates_postponed_scalar_annotations():
    """Future-style string annotations must enforce the handler contract."""
    from nz_coder.tools import (
        TOOL_EXECUTION_MODES,
        TOOL_HANDLERS,
        TOOL_PLAN_MODE_ALLOWED,
        TOOL_SIDE_EFFECTS,
        TOOL_SPECS,
        dispatch,
        register,
    )

    name = "_test_postponed_annotation"

    def handler(value):
        return f"value={value}"

    handler.__annotations__ = {"value": "str", "return": "str"}
    try:
        register(
            name,
            "test",
            {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
            handler,
        )

        output = dispatch(name, {"value": 123})

        assert "expected as `string`" in output
        assert "provided as `int`" in output
    finally:
        TOOL_HANDLERS.pop(name, None)
        TOOL_EXECUTION_MODES.pop(name, None)
        TOOL_SIDE_EFFECTS.pop(name, None)
        TOOL_PLAN_MODE_ALLOWED.pop(name, None)
        TOOL_SPECS[:] = [
            spec for spec in TOOL_SPECS
            if spec["function"]["name"] != name
        ]


def test_dispatch_does_not_treat_boolean_as_integer():
    """JSON booleans must not satisfy an integer handler parameter."""
    from nz_coder.tools import (
        TOOL_EXECUTION_MODES,
        TOOL_HANDLERS,
        TOOL_PLAN_MODE_ALLOWED,
        TOOL_SIDE_EFFECTS,
        TOOL_SPECS,
        dispatch,
        register,
    )

    name = "_test_boolean_integer_boundary"

    def handler(count: int) -> str:
        return f"count={count}"

    try:
        register(
            name,
            "test",
            {
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ["count"],
            },
            handler,
        )

        output = dispatch(name, {"count": True})

        assert "expected as `integer`" in output
        assert "provided as `bool`" in output
    finally:
        TOOL_HANDLERS.pop(name, None)
        TOOL_EXECUTION_MODES.pop(name, None)
        TOOL_SIDE_EFFECTS.pop(name, None)
        TOOL_PLAN_MODE_ALLOWED.pop(name, None)
        TOOL_SPECS[:] = [
            spec for spec in TOOL_SPECS
            if spec["function"]["name"] != name
        ]


def test_dispatch_survives_unresolvable_extension_annotations():
    """Bad third-party type metadata must not escape the string error boundary."""
    from nz_coder.tools import (
        TOOL_EXECUTION_MODES,
        TOOL_HANDLERS,
        TOOL_PLAN_MODE_ALLOWED,
        TOOL_SIDE_EFFECTS,
        TOOL_SPECS,
        dispatch,
        register,
    )

    name = "_test_invalid_annotation_metadata"

    def handler(value):
        return f"value={value}"

    handler.__annotations__ = {"value": "invalid["}
    try:
        register(
            name,
            "test",
            {"type": "object", "properties": {"value": {"type": "string"}}},
            handler,
        )

        assert dispatch(name, {"value": "ok"}) == "value=ok"
    finally:
        TOOL_HANDLERS.pop(name, None)
        TOOL_EXECUTION_MODES.pop(name, None)
        TOOL_SIDE_EFFECTS.pop(name, None)
        TOOL_PLAN_MODE_ALLOWED.pop(name, None)
        TOOL_SPECS[:] = [
            spec for spec in TOOL_SPECS
            if spec["function"]["name"] != name
        ]


@pytest.mark.parametrize(
    ("name", "description", "parameters", "handler", "message"),
    [
        ("", "test", {"type": "object"}, lambda: "ok", "name"),
        ("bad tool", "test", {"type": "object"}, lambda: "ok", "name"),
        ("_test_bad_handler", "test", {"type": "object"}, None, "handler"),
        ("_test_bad_description", None, {"type": "object"}, lambda: "ok", "description"),
        ("_test_bad_schema", "test", [], lambda: "ok", "parameters"),
    ],
)
def test_register_rejects_invalid_definition_without_partial_state(
    name, description, parameters, handler, message,
):
    """Bad extension metadata must fail before mutating the global registry."""
    from nz_coder.tools import (
        TOOL_EXECUTION_MODES,
        TOOL_HANDLERS,
        TOOL_PLAN_MODE_ALLOWED,
        TOOL_SIDE_EFFECTS,
        TOOL_SPECS,
        register,
    )

    with pytest.raises(ValueError, match=message):
        register(name, description, parameters, handler)

    assert name not in TOOL_HANDLERS
    assert name not in TOOL_EXECUTION_MODES
    assert name not in TOOL_SIDE_EFFECTS
    assert name not in TOOL_PLAN_MODE_ALLOWED
    assert all(spec["function"]["name"] != name for spec in TOOL_SPECS)


def test_dynamic_registry_rejects_non_object_definition_cleanly():
    """Malformed MCP/provider records must produce a stable validation error."""
    from nz_coder.tools import scoped_dynamic_tools

    with pytest.raises(ValueError, match="definition must be an object"):
        with scoped_dynamic_tools(["not-an-object"]):
            pass


def test_dispatch_accepts_falsey_callable_handlers():
    """Callable extension objects must not be mistaken for absent handlers."""
    from nz_coder.tools import (
        TOOL_EXECUTION_MODES,
        TOOL_HANDLERS,
        TOOL_PLAN_MODE_ALLOWED,
        TOOL_SIDE_EFFECTS,
        TOOL_SPECS,
        dispatch,
        register,
        scoped_dynamic_tools,
    )

    class FalseyHandler:
        def __bool__(self):
            return False

        def __call__(self):
            return "called"

    static_name = "_test_falsey_static_handler"
    dynamic_name = "_test_falsey_dynamic_handler"
    try:
        register(
            static_name,
            "test",
            {"type": "object", "properties": {}},
            FalseyHandler(),
        )
        assert dispatch(static_name, {}) == "called"
        with scoped_dynamic_tools([{
            "name": dynamic_name,
            "handler": FalseyHandler(),
            "execution": "read",
        }]):
            assert dispatch(dynamic_name, {}) == "called"
    finally:
        TOOL_HANDLERS.pop(static_name, None)
        TOOL_EXECUTION_MODES.pop(static_name, None)
        TOOL_SIDE_EFFECTS.pop(static_name, None)
        TOOL_PLAN_MODE_ALLOWED.pop(static_name, None)
        TOOL_SPECS[:] = [
            spec for spec in TOOL_SPECS
            if spec["function"]["name"] != static_name
        ]
