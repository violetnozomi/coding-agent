"""Built-in-first resolver for saved and nested data-only workflows."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from nz_coder.runtime.workflow_builtins import get_builtin_workflow
from nz_coder.runtime.workflow_capsule import preflight_workflow_capsule
from nz_coder.runtime.workflow_library import (
    capsule_environment,
    load_workflow_capsule,
)


def _substitute(value: Any, args: dict) -> Any:
    if isinstance(value, list):
        return [_substitute(item, args) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, args) for key, item in value.items()}
    if not isinstance(value, str):
        return copy.deepcopy(value)
    result = value
    for key, item in args.items():
        result = result.replace(
            "{args." + str(key) + "}",
            json.dumps(item, ensure_ascii=False, default=str)[:20_000],
        )
    return result


def resolve_workflow_capsule(
    name: str,
    args: dict | None,
    *,
    workspace: Path,
    source: str = "",
) -> dict:
    """Resolve trusted built-ins before saved capsules and preflight once."""
    arguments = dict(args or {})
    builtin = get_builtin_workflow(name, arguments)
    if builtin is not None:
        capsule = builtin
        reference = {
            "name": str(name),
            "source": "builtin",
            "execution": "trusted-data",
        }
    else:
        capsule, reference = load_workflow_capsule(
            name,
            workspace=workspace,
            source=source,
        )
        capsule = copy.deepcopy(capsule)
        capsule["plan"] = _substitute(capsule["plan"], arguments)
    preflight = preflight_workflow_capsule(
        capsule,
        capsule_environment(workspace),
    )
    if not preflight["ok"]:
        errors = "; ".join(
            f"[{item['requirement']}] {item['message']}"
            for item in preflight["issues"]
            if item["severity"] == "error"
        )
        raise ValueError(f"workflow capsule preflight failed: {errors}")
    return {
        "capsule": preflight["capsule"],
        "ref": reference,
        "preflight": preflight,
    }


def resolve_nested_workflows(
    plan: dict,
    *,
    workspace: Path,
    depth: int = 0,
) -> dict:
    """Resolve exactly one nested level before any Agent effect is admitted."""
    prepared = copy.deepcopy(plan)
    for phase in prepared.get("phases", []):
        if not isinstance(phase, dict) or phase.get("mode") != "workflow":
            continue
        if depth >= 1:
            raise ValueError("nested workflows are limited to one level")
        name = str(phase.get("workflow") or "").strip()
        if not name:
            raise ValueError("nested workflow phase requires workflow name")
        resolved = resolve_workflow_capsule(
            name,
            phase.get("args") if isinstance(phase.get("args"), dict) else {},
            workspace=workspace,
            source=str(phase.get("source") or ""),
        )
        nested = resolve_nested_workflows(
            resolved["capsule"]["plan"],
            workspace=workspace,
            depth=depth + 1,
        )
        phase["_nested_plan"] = nested
        phase["_nested_ref"] = resolved["ref"]
        phase["_nested_preflight"] = resolved["preflight"]
    return prepared
