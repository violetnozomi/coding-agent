"""Provider-neutral structured Agent output extraction and validation."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re


SUPPORTED_SCHEMA_KEYWORDS = frozenset({
    "type",
    "enum",
    "required",
    "properties",
    "items",
    "additionalProperties",
    "description",
    "title",
    "default",
    "examples",
    "$comment",
    "$schema",
    "format",
})
UNSUPPORTED_SCHEMA_KEYWORDS = frozenset({
    "$ref", "$defs", "definitions", "oneOf", "allOf", "anyOf", "not",
    "if", "then", "else", "const", "patternProperties", "propertyNames",
    "dependencies", "dependentSchemas", "dependentRequired", "contains",
    "unevaluatedProperties", "unevaluatedItems", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength",
    "maxLength", "pattern", "minItems", "maxItems", "uniqueItems",
    "minProperties", "maxProperties",
})
STRUCTURED_OUTPUT_REPAIR_SYSTEM_PROMPT = (
    "You are re-formatting your own just-completed report into the JSON its "
    "caller requires. Do not use tools. Do not continue investigation. "
    "Output only the JSON block."
)
STRUCTURED_OUTPUT_KEY = "_nz_structured_output"


@dataclass(frozen=True)
class StructuredOutputEvaluation:
    """Non-throwing parse and schema-validation result."""

    ok: bool
    value: object = None
    errors: tuple[str, ...] = ()


def assert_supported_output_schema(schema: object) -> None:
    """Reject declarations whose constraints this validator cannot enforce."""
    if not isinstance(schema, dict):
        raise ValueError("output_schema must be a JSON-Schema object")
    try:
        json.dumps(schema, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("output_schema must be JSON serializable") from exc
    found: dict[str, str] = {}

    def visit(node: object, path: str) -> None:
        if not isinstance(node, dict):
            return
        for keyword in UNSUPPORTED_SCHEMA_KEYWORDS:
            if keyword in node and keyword not in found:
                found[keyword] = path or "(root)"
        additional = node.get("additionalProperties")
        if isinstance(additional, dict):
            found.setdefault("additionalProperties (schema form)", path or "(root)")
        properties = node.get("properties")
        if isinstance(properties, dict):
            for key, child in properties.items():
                visit(child, _join_path(path, str(key)))
        items = node.get("items")
        if isinstance(items, dict):
            visit(items, f"{path or '(root)'}[]")

    visit(schema, "")
    if found:
        detail = ", ".join(
            f"'{keyword}' (at {path})" for keyword, path in found.items()
        )
        raise ValueError(
            "output_schema uses unsupported JSON-Schema keyword(s): " + detail
        )


def validate_against_schema(
    value: object,
    schema: object,
    path: str = "",
) -> tuple[str, ...]:
    """Validate the focused InfCodeX schema subset without external packages."""
    if not isinstance(schema, dict):
        return ()
    location = path or "(root)"
    declared_type = schema.get("type")
    if isinstance(declared_type, str) and not _matches_type(value, declared_type):
        return (f"{location}: expected type {declared_type}",)
    if isinstance(declared_type, list) and not any(
        isinstance(candidate, str) and _matches_type(value, candidate)
        for candidate in declared_type
    ):
        expected = "|".join(str(item) for item in declared_type)
        return (f"{location}: expected one of types {expected}",)

    errors: list[str] = []
    candidates = schema.get("enum")
    if isinstance(candidates, list) and not any(
        _json_equal(candidate, value) for candidate in candidates
    ):
        errors.append(f"{location}: value is not one of the allowed enum values")

    if isinstance(value, dict):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else None
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if isinstance(key, str) and key not in value:
                    errors.append(
                        f"{_join_path(path, key)}: required field is missing"
                    )
        if properties is not None:
            for key, child in value.items():
                if key in properties:
                    errors.extend(validate_against_schema(
                        child,
                        properties[key],
                        _join_path(path, str(key)),
                    ))
            if schema.get("additionalProperties") is False:
                for key in value:
                    if key not in properties:
                        errors.append(
                            f"{_join_path(path, str(key))}: unexpected property "
                            "(additionalProperties is false)"
                        )

    items = schema.get("items")
    if isinstance(value, list) and isinstance(items, dict):
        for index, element in enumerate(value):
            errors.extend(validate_against_schema(
                element,
                items,
                f"{location}[{index}]",
            ))
    return tuple(errors)


def extract_json_candidate(text: str) -> str | None:
    """Prefer the last JSON fence, then the first decodable object or array."""
    if not isinstance(text, str) or not text.strip():
        return None
    fenced = [
        match.group(1).strip()
        for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text, re.I)
        if match.group(1).strip()
    ]
    source = fenced[-1] if fenced else text
    decoder = json.JSONDecoder()
    starts = [index for index, char in enumerate(source) if char in "{["]
    for index in starts:
        try:
            _value, end = decoder.raw_decode(source[index:])
        except json.JSONDecodeError:
            continue
        return source[index:index + end]
    return source.strip() if fenced else None


def evaluate_structured_output(
    final_text: str,
    schema: object,
) -> StructuredOutputEvaluation:
    """Extract, parse, and validate one final Agent response; never raise."""
    candidate = extract_json_candidate(final_text)
    if candidate is None:
        return StructuredOutputEvaluation(
            False,
            errors=("no JSON value was found in the output",),
        )
    try:
        value = json.loads(candidate)
    except (json.JSONDecodeError, TypeError) as exc:
        return StructuredOutputEvaluation(
            False,
            errors=(f"output was not valid JSON: {exc}",),
        )
    errors = validate_against_schema(value, schema)
    return StructuredOutputEvaluation(not errors, value=value, errors=errors)


def build_structured_output_instruction(schema: object) -> str:
    """Append a stable provider-neutral output contract to an Agent prompt."""
    return "\n".join((
        "## Required Output Format",
        "After your analysis, end your response with a single fenced ```json "
        "code block containing ONLY a JSON value that matches the JSON Schema "
        "below. Put nothing after the closing fence.",
        "Schema:",
        "```json",
        _safe_json(schema),
        "```",
    ))


def build_structured_output_repair_prompt(
    errors: tuple[str, ...] | list[str],
    schema: object,
) -> str:
    """Build the sole no-tool repair request for an invalid final value."""
    return "\n".join((
        "Your previous response did not produce a valid result object.",
        "Problems:",
        *(f"- {error}" for error in errors),
        "Re-emit ONLY a single fenced ```json code block containing a JSON "
        "value that matches this schema. No prose, nothing after the closing fence.",
        "Schema:",
        "```json",
        _safe_json(schema),
        "```",
    ))


def _matches_type(value: object, declared: str) -> bool:
    if declared == "string":
        return isinstance(value, str)
    if declared == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if declared == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared == "boolean":
        return isinstance(value, bool)
    if declared == "object":
        return isinstance(value, dict)
    if declared == "array":
        return isinstance(value, list)
    if declared == "null":
        return value is None
    return True


def _json_equal(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    try:
        return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)
    except (TypeError, ValueError):
        return left == right


def _join_path(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _safe_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)
