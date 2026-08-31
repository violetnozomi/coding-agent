"""Provider presentation adapters and recursive linting for tool schemas."""
from __future__ import annotations

import copy
from dataclasses import dataclass


@dataclass(frozen=True)
class SchemaIssue:
    """One actionable canonical tool-schema defect."""

    tool_name: str
    schema_path: str
    field: str
    code: str
    message: str


def lint_tool_specs(specs: list[dict]) -> list[SchemaIssue]:
    """Recursively validate required declarations, including array item objects."""
    issues: list[SchemaIssue] = []
    for spec in specs or []:
        function = spec.get("function") if isinstance(spec, dict) else None
        if not isinstance(function, dict):
            continue
        tool_name = str(function.get("name") or "<unnamed>")
        parameters = function.get("parameters")
        if isinstance(parameters, dict):
            _lint_schema(
                parameters,
                tool_name=tool_name,
                schema_path="parameters",
                issues=issues,
            )
    return issues


def adapt_tool_specs(
    specs: list[dict],
    *,
    provider: str,
    model: str,
) -> list[dict]:
    """Return provider-facing copies while canonical definitions stay unchanged."""
    flavor = _provider_flavor(provider, model)
    adapted = copy.deepcopy(list(specs or []))
    for spec in adapted:
        function = spec.get("function") if isinstance(spec, dict) else None
        if not isinstance(function, dict):
            continue
        tool_name = str(function.get("name") or "")
        description = function.get("description")
        if flavor == "deepseek" and isinstance(description, str):
            function["description"] = description[:600]
        parameters = function.get("parameters")
        if isinstance(parameters, dict):
            function["parameters"] = _adapt_schema(parameters, flavor=flavor)
        if flavor == "deepseek" and tool_name == "apply_patch":
            _adapt_deepseek_apply_patch(function)
    return adapted


def _adapt_deepseek_apply_patch(function: dict) -> None:
    """Present DeepSeek with a flat single-file batch edit contract.

    DeepSeek-family models are materially less reliable at satisfying a
    required field nested inside every object of an array.  The canonical
    handler already supports a top-level path for single-file batches, so the
    provider projection can use the same shape as InfCodeX ``multi_edit``
    without changing the registered tool contract used by other providers.
    """
    parameters = function.get("parameters")
    if not isinstance(parameters, dict):
        return
    properties = parameters.get("properties")
    if not isinstance(properties, dict) or not {
        "path", "changes",
    }.issubset(properties):
        return
    changes = properties.get("changes")
    items = changes.get("items") if isinstance(changes, dict) else None
    item_properties = items.get("properties") if isinstance(items, dict) else None
    if not isinstance(item_properties, dict):
        return

    item_properties.pop("path", None)
    nested_required = items.get("required")
    if isinstance(nested_required, list):
        remaining = [field for field in nested_required if field != "path"]
        if remaining:
            items["required"] = remaining
        else:
            items.pop("required", None)
    properties["path"]["description"] = (
        "Relative path for every change in this single-file patch."
    )
    parameters["required"] = ["path", *(
        field for field in parameters.get("required", []) if field != "path"
    )]
    function["description"] = (
        "Apply one or more exact changes to a single file atomically. Set the "
        "top-level path once; each changes item contains only its operation and "
        "text payload. Each old_text must be one exact contiguous excerpt from "
        "the file; include every intervening line rather than skipping imports or "
        "statements. Changes run sequentially, so their old_text regions must not "
        "overlap. To add content at end of file, use op=append with new_text and "
        "omit old_text instead of guessing an anchor. Use separate calls for "
        "different files."
    )


def _lint_schema(
    schema: dict,
    *,
    tool_name: str,
    schema_path: str,
    issues: list[SchemaIssue],
) -> None:
    properties = schema.get("properties")
    if isinstance(properties, dict):
        raw_required = schema.get("required") or []
        required = set(raw_required) if isinstance(raw_required, list) else set()
        for field in required - set(properties):
            issues.append(SchemaIssue(
                tool_name=tool_name,
                schema_path=schema_path,
                field=str(field),
                code="required_field_unknown",
                message=f"required field {field!r} is absent from properties",
            ))
        for field, child in properties.items():
            if not isinstance(child, dict):
                continue
            if child.get("x-nz-required") is True and field not in required:
                issues.append(SchemaIssue(
                    tool_name=tool_name,
                    schema_path=schema_path,
                    field=str(field),
                    code="required_field_missing",
                    message=(
                        f"handler-required field {field!r} is missing from the "
                        "object's JSON Schema required list"
                    ),
                ))
            _lint_schema(
                child,
                tool_name=tool_name,
                schema_path=f"{schema_path}.properties.{field}",
                issues=issues,
            )
    items = schema.get("items")
    if isinstance(items, dict):
        _lint_schema(
            items,
            tool_name=tool_name,
            schema_path=f"{schema_path}.items",
            issues=issues,
        )
    for key in ("anyOf", "oneOf", "allOf"):
        for index, child in enumerate(schema.get(key) or []):
            if isinstance(child, dict):
                _lint_schema(
                    child,
                    tool_name=tool_name,
                    schema_path=f"{schema_path}.{key}[{index}]",
                    issues=issues,
                )


def _adapt_schema(schema: dict, *, flavor: str) -> dict:
    value = copy.deepcopy(schema)
    if flavor == "deepseek":
        value = _flatten_combinators(value)
    result: dict = {}
    for key, item in value.items():
        if key.startswith("x-nz-"):
            continue
        if flavor == "deepseek" and key in {
            "$defs", "definitions", "examples", "title", "$comment",
        }:
            continue
        if key == "description" and isinstance(item, str) and flavor == "deepseek":
            result[key] = item[:300]
        elif key == "properties" and isinstance(item, dict):
            result[key] = {
                name: _adapt_schema(child, flavor=flavor)
                if isinstance(child, dict) else child
                for name, child in item.items()
            }
        elif key == "items" and isinstance(item, dict):
            result[key] = _adapt_schema(item, flavor=flavor)
        elif key in {"anyOf", "oneOf", "allOf"} and isinstance(item, list):
            result[key] = [
                _adapt_schema(child, flavor=flavor)
                if isinstance(child, dict) else child
                for child in item
            ]
        elif key == "required" and isinstance(item, list):
            properties = value.get("properties")
            allowed = set(properties) if isinstance(properties, dict) else None
            result[key] = list(dict.fromkeys(
                str(field) for field in item
                if allowed is None or str(field) in allowed
            ))
        else:
            result[key] = item
    return result


def _flatten_combinators(schema: dict) -> dict:
    value = copy.deepcopy(schema)
    any_of = value.pop("anyOf", None)
    one_of = value.pop("oneOf", None)
    variants = any_of if isinstance(any_of, list) else one_of
    if isinstance(variants, list) and variants:
        selected = next(
            (
                item for item in variants
                if isinstance(item, dict) and item.get("type") != "null"
            ),
            variants[0],
        )
        if isinstance(selected, dict):
            merged = copy.deepcopy(selected)
            merged.update(value)
            value = merged
    all_of = value.pop("allOf", None)
    if isinstance(all_of, list):
        for part in all_of:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("properties"), dict):
                value.setdefault("type", "object")
                value.setdefault("properties", {}).update(part["properties"])
            if isinstance(part.get("required"), list):
                value["required"] = list(dict.fromkeys([
                    *(value.get("required") or []),
                    *part["required"],
                ]))
    return value


def _provider_flavor(provider: str, model: str) -> str:
    identity = f"{provider} {model}".casefold()
    if "deepseek" in identity:
        return "deepseek"
    if "anthropic" in identity or "claude" in identity:
        return "anthropic"
    if "gemini" in identity or "google" in identity:
        return "gemini"
    return "openai"


__all__ = ["SchemaIssue", "adapt_tool_specs", "lint_tool_specs"]
