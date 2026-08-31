"""Provider-facing tool schema adaptation and lint contracts."""
from __future__ import annotations

import copy

import pytest


def _spec(parameters):
    return [{
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "Apply patches",
            "parameters": parameters,
        },
    }]


def test_linter_reports_explicit_nested_required_field_missing_from_required():
    from nz_coder.providers.tool_schema import lint_tool_specs

    specs = _spec({
        "type": "object",
        "properties": {
            "changes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "x-nz-required": True},
                    },
                },
            },
        },
        "required": ["changes"],
    })

    issues = lint_tool_specs(specs)

    assert len(issues) == 1
    assert issues[0].tool_name == "apply_patch"
    assert issues[0].schema_path == "parameters.properties.changes.items"
    assert issues[0].field == "path"


def test_linter_accepts_nested_required_field_when_declared():
    from nz_coder.providers.tool_schema import lint_tool_specs

    specs = _spec({
        "type": "object",
        "properties": {
            "changes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "x-nz-required": True},
                    },
                    "required": ["path"],
                },
            },
        },
        "required": ["changes"],
    })

    assert lint_tool_specs(specs) == []


def test_deepseek_adapter_simplifies_combinators_and_preserves_nested_required():
    from nz_coder.providers.tool_schema import adapt_tool_specs

    specs = _spec({
        "type": "object",
        "properties": {
            "changes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "x-nz-required": True,
                        },
                    },
                    "required": ["path"],
                },
            },
        },
        "required": ["changes"],
    })

    adapted = adapt_tool_specs(
        specs,
        provider="openai-compatible",
        model="deepseek-v4-flash",
    )
    nested = adapted[0]["function"]["parameters"]["properties"]["changes"]["items"]

    assert nested["required"] == ["path"]
    assert nested["properties"]["path"] == {"type": "string"}
    assert "x-nz-required" not in str(adapted)


def test_deepseek_apply_patch_uses_flat_single_file_path_contract():
    """DeepSeek should not have to repeat path inside every array item."""
    from nz_coder.providers.tool_schema import adapt_tool_specs
    from nz_coder.tools import get_catalog_specs
    import nz_coder.tools.files  # noqa: F401

    canonical = next(
        spec for spec in get_catalog_specs()
        if spec.get("function", {}).get("name") == "apply_patch"
    )
    adapted = adapt_tool_specs(
        [canonical],
        provider="openai-compatible",
        model="deepseek-v4-flash",
    )[0]["function"]
    parameters = adapted["parameters"]
    item_schema = parameters["properties"]["changes"]["items"]

    assert parameters["required"] == ["path", "changes"]
    assert "path" not in item_schema["properties"]
    assert "path" not in item_schema.get("required", [])
    assert "single file" in adapted["description"].lower()
    assert "overlap" in adapted["description"].lower()
    assert "contiguous" in adapted["description"].lower()
    assert "intervening" in adapted["description"].lower()
    assert "op=append" in adapted["description"]


def test_adapter_never_mutates_canonical_tool_specs():
    from nz_coder.providers.tool_schema import adapt_tool_specs

    specs = _spec({
        "type": "object",
        "properties": {"value": {"type": "string", "examples": ["x"]}},
    })
    original = copy.deepcopy(specs)

    adapt_tool_specs(specs, provider="deepseek", model="deepseek-v4-flash")

    assert specs == original


def test_catalog_specs_are_detached_from_global_registry():
    """A Provider adapter cannot mutate schemas seen by another Session."""
    from nz_coder.tools import get_catalog_specs
    import nz_coder.tools.files  # noqa: F401

    first = get_catalog_specs()
    target = next(
        spec for spec in first
        if spec.get("function", {}).get("name") == "read_file"
    )
    target["function"]["description"] = "mutated by adapter"
    target["function"]["parameters"]["properties"].clear()

    fresh = next(
        spec for spec in get_catalog_specs()
        if spec.get("function", {}).get("name") == "read_file"
    )

    assert fresh["function"]["description"] != "mutated by adapter"
    assert fresh["function"]["parameters"]["properties"]


def test_registry_rejects_non_json_schema_values_before_exposure():
    from nz_coder.tools import register, scoped_dynamic_tools

    invalid_schema = {
        "type": "object",
        "properties": {"value": {"enum": {"not", "json"}}},
    }

    with pytest.raises(ValueError, match="JSON-serializable"):
        register(
            "_invalid_static_schema",
            "invalid",
            invalid_schema,
            lambda: "ok",
        )
    with pytest.raises(ValueError, match="JSON-serializable"):
        with scoped_dynamic_tools([{
            "name": "_invalid_dynamic_schema",
            "description": "invalid",
            "parameters": invalid_schema,
            "handler": lambda: "ok",
        }]):
            pass


def test_registered_apply_patch_schema_passes_recursive_lint():
    from nz_coder.providers.tool_schema import lint_tool_specs
    from nz_coder.tools import get_catalog_specs
    import nz_coder.tools.files  # noqa: F401

    apply_patch = [
        spec for spec in get_catalog_specs()
        if spec.get("function", {}).get("name") == "apply_patch"
    ]

    assert lint_tool_specs(apply_patch) == []
