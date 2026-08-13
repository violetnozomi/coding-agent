"""Validated JSON-only reusable workflow capsules and requirement preflight."""
from __future__ import annotations

import copy
import re
from typing import Any

from nz_coder.runtime.workflow_manifest import validate_workflow_manifest


WORKFLOW_CAPSULE_FORMAT = "nzcoder.workflow"
WORKFLOW_CAPSULE_VERSION = 1
WORKFLOW_CAPSULE_API_VERSION = 1
_ENVIRONMENT_REQUIREMENTS = frozenset({"git-repo", "worktree-capable"})
_MODEL_TIERS = frozenset({"fast", "balanced", "deep"})
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


def _record(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"workflow capsule {label} must be an object")
    return value


def _text(value: dict, key: str, *, optional: bool = False) -> str | None:
    item = value.get(key)
    if optional and item is None:
        return None
    if not isinstance(item, str) or not item.strip():
        suffix = " when provided" if optional else ""
        raise ValueError(
            f"workflow capsule {key} must be a non-empty string{suffix}"
        )
    return item.strip()


def _strings(value: dict, key: str) -> list[str] | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, list) or not all(
        isinstance(entry, str) and entry.strip() for entry in item
    ):
        raise ValueError(f"workflow capsule {key} must be a string array")
    return [entry.strip() for entry in item]


def validate_workflow_capsule(value: Any) -> dict:
    """Normalize one inert capsule; no field can contain executable code."""
    capsule = _record(value, "")
    allowed = {
        "format", "version", "workflow_api_version", "min_nzcoder_version",
        "manifest", "plan", "intent", "requires", "provenance",
    }
    unknown = sorted(set(capsule) - allowed)
    if unknown:
        raise ValueError(f"workflow capsule contains unsupported field: {unknown[0]}")
    if capsule.get("format") != WORKFLOW_CAPSULE_FORMAT:
        raise ValueError(
            f"workflow capsule format must be {WORKFLOW_CAPSULE_FORMAT}"
        )
    if capsule.get("version") != WORKFLOW_CAPSULE_VERSION:
        raise ValueError(f"workflow capsule version must be {WORKFLOW_CAPSULE_VERSION}")
    if capsule.get("workflow_api_version") != WORKFLOW_CAPSULE_API_VERSION:
        raise ValueError(
            f"workflow capsule workflow_api_version must be {WORKFLOW_CAPSULE_API_VERSION}"
        )
    minimum = _text(capsule, "min_nzcoder_version")
    manifest = validate_workflow_manifest(capsule.get("manifest"))
    plan = copy.deepcopy(_record(capsule.get("plan"), "plan"))
    if not isinstance(plan.get("phases"), list) or not plan["phases"]:
        raise ValueError("workflow capsule plan requires non-empty phases")
    declared = plan.get("manifest")
    if declared is not None and validate_workflow_manifest(declared) != manifest:
        raise ValueError("workflow capsule plan manifest does not match capsule manifest")
    plan["manifest"] = copy.deepcopy(manifest)

    intent = capsule.get("intent")
    normalized_intent = None
    if intent is not None:
        intent = _record(intent, "intent")
        normalized_intent = {
            "task_class": _text(intent, "task_class"),
            **({"patterns": _strings(intent, "patterns")} if intent.get("patterns") is not None else {}),
            **({"original_request": _text(intent, "original_request")} if intent.get("original_request") is not None else {}),
            **({"reusable_for": _strings(intent, "reusable_for")} if intent.get("reusable_for") is not None else {}),
            **({"not_for": _strings(intent, "not_for")} if intent.get("not_for") is not None else {}),
        }

    requires = capsule.get("requires")
    normalized_requires = None
    if requires is not None:
        requires = _record(requires, "requires")
        environment = _strings(requires, "environment")
        if environment is not None:
            unsupported = [item for item in environment if item not in _ENVIRONMENT_REQUIREMENTS]
            if unsupported:
                raise ValueError(
                    f"workflow capsule environment requirement is unsupported: {unsupported[0]}"
                )
        model_tiers = _strings(requires, "model_tiers")
        if model_tiers is not None:
            unsupported = [item for item in model_tiers if item not in _MODEL_TIERS]
            if unsupported:
                raise ValueError(
                    f"workflow capsule model tier is unsupported: {unsupported[0]}"
                )
        interaction = requires.get("user_interaction")
        if interaction is not None and not isinstance(interaction, bool):
            raise ValueError(
                "workflow capsule requires.user_interaction must be boolean"
            )
        normalized_requires = {
            **({"environment": environment} if environment is not None else {}),
            **({"tools": _strings(requires, "tools")} if requires.get("tools") is not None else {}),
            **({"mcp": _strings(requires, "mcp")} if requires.get("mcp") is not None else {}),
            **({"skills": _strings(requires, "skills")} if requires.get("skills") is not None else {}),
            **({"model_tiers": model_tiers} if model_tiers is not None else {}),
            **({"user_interaction": interaction} if interaction is not None else {}),
        }

    provenance = capsule.get("provenance")
    normalized_provenance = None
    if provenance is not None:
        provenance = _record(provenance, "provenance")
        normalized_provenance = {
            "created_at": _text(provenance, "created_at"),
            "nzcoder_version": _text(provenance, "nzcoder_version"),
        }
        for key in (
            "from_run_id", "from_workflow_name", "revision_of",
            "replaces_workflow_name",
        ):
            if provenance.get(key) is not None:
                normalized_provenance[key] = _text(provenance, key)

    return {
        "format": WORKFLOW_CAPSULE_FORMAT,
        "version": WORKFLOW_CAPSULE_VERSION,
        "workflow_api_version": WORKFLOW_CAPSULE_API_VERSION,
        "min_nzcoder_version": minimum,
        "manifest": manifest,
        "plan": plan,
        **({"intent": normalized_intent} if normalized_intent is not None else {}),
        **({"requires": normalized_requires} if normalized_requires is not None else {}),
        **({"provenance": normalized_provenance} if normalized_provenance is not None else {}),
    }


def create_workflow_capsule(
    *,
    manifest: dict,
    plan: dict,
    min_nzcoder_version: str = "0.1.0",
    intent: dict | None = None,
    requires: dict | None = None,
    provenance: dict | None = None,
) -> dict:
    return validate_workflow_capsule({
        "format": WORKFLOW_CAPSULE_FORMAT,
        "version": WORKFLOW_CAPSULE_VERSION,
        "workflow_api_version": WORKFLOW_CAPSULE_API_VERSION,
        "min_nzcoder_version": min_nzcoder_version,
        "manifest": manifest,
        "plan": plan,
        **({"intent": intent} if intent is not None else {}),
        **({"requires": requires} if requires is not None else {}),
        **({"provenance": provenance} if provenance is not None else {}),
    })


def _version(value: str) -> tuple[int, int, int] | None:
    match = _SEMVER.fullmatch(str(value).strip())
    return tuple(int(match.group(index)) for index in range(1, 4)) if match else None


def preflight_workflow_capsule(capsule: dict, environment: dict | None = None) -> dict:
    """Check requirements without loading tools, MCP servers, or executable code."""
    validated = validate_workflow_capsule(capsule)
    env = dict(environment or {})
    issues: list[dict] = []

    def issue(severity: str, requirement: str, message: str) -> None:
        issues.append({
            "severity": severity,
            "requirement": requirement,
            "message": message,
        })

    minimum = _version(validated["min_nzcoder_version"])
    current_text = str(env.get("nzcoder_version") or "")
    current = _version(current_text)
    if minimum is None:
        issue("error", "nzcoder:min-version", "capsule minimum version is invalid")
    elif current is None:
        issue("warning", "nzcoder:min-version", "current NZ-Coder version is unknown")
    elif current < minimum:
        issue(
            "error",
            "nzcoder:min-version",
            f"workflow requires NZ-Coder >= {validated['min_nzcoder_version']}; current is {current_text}",
        )

    requirements = validated.get("requires") or {}
    requested_environment = requirements.get("environment") or []
    if "git-repo" in requested_environment and env.get("is_git_repo") is False:
        issue("error", "environment:git-repo", "workflow requires a git repository")
    if (
        "worktree-capable" in requested_environment
        and env.get("worktree_capable") is False
    ):
        issue(
            "error",
            "environment:worktree-capable",
            "workflow requires worktree-capable isolation",
        )
    for kind in ("tools", "mcp", "skills", "model_tiers"):
        required = requirements.get(kind) or []
        available = env.get(f"available_{kind}")
        if available is None:
            for item in required:
                issue(
                    "warning",
                    f"{kind}:{item}",
                    f"cannot verify required workflow {kind}: {item}",
                )
            continue
        available_set = set(available)
        for item in required:
            if item not in available_set:
                issue("error", f"{kind}:{item}", f"missing required workflow {kind}: {item}")
    return {
        "ok": not any(item["severity"] == "error" for item in issues),
        "issues": issues,
        "capsule": validated,
    }
