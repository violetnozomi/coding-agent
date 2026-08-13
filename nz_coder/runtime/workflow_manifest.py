"""Strict manifest validation for declarative Agent workflows."""
from __future__ import annotations

from typing import Any


WORKFLOW_PATTERN_IDS = frozenset({
    "classify-and-act",
    "fan-out-and-synthesize",
    "adversarial-verification",
    "generate-and-filter",
    "tournament",
    "loop-until-done",
})


def validate_workflow_manifest(value: Any) -> dict:
    """Return one normalized manifest or raise a declaration error."""
    if not isinstance(value, dict):
        raise ValueError("workflow manifest must be an object")

    def text(key: str) -> str:
        item = value.get(key)
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"workflow manifest {key} must be a non-empty string")
        return item.strip()

    def positive(key: str, *, optional: bool = False) -> int | None:
        item = value.get(key)
        if optional and item is None:
            return None
        if not isinstance(item, int) or isinstance(item, bool) or item <= 0:
            suffix = " when provided" if optional else ""
            raise ValueError(
                f"workflow manifest {key} must be a positive integer{suffix}"
            )
        return item

    def strings(key: str) -> list[str]:
        item = value.get(key)
        if (
            not isinstance(item, list)
            or not item
            or not all(isinstance(entry, str) and entry.strip() for entry in item)
        ):
            raise ValueError(
                f"workflow manifest {key} must be a non-empty string array"
            )
        return [entry.strip() for entry in item]

    read_only = value.get("read_only")
    if not isinstance(read_only, bool):
        raise ValueError("workflow manifest read_only must be a boolean")
    phases = strings("phases")
    if len(set(phases)) != len(phases):
        raise ValueError("workflow manifest phases must be unique")
    patterns = strings("patterns")
    unsupported = [item for item in patterns if item not in WORKFLOW_PATTERN_IDS]
    if unsupported:
        raise ValueError(
            f"workflow manifest patterns contains unsupported id: {unsupported[0]}"
        )
    planned = positive("planned_agents", optional=True)
    maximum = positive("max_agents")
    concurrency = positive("max_concurrency")
    token_budget = positive("token_budget", optional=True)
    assert maximum is not None and concurrency is not None
    if planned is not None and planned > maximum:
        raise ValueError(
            "workflow manifest planned_agents must be less than or equal to max_agents"
        )
    if concurrency > maximum:
        raise ValueError(
            "workflow manifest max_concurrency must not exceed max_agents"
        )
    may_use_worktree = value.get("may_use_worktree")
    if may_use_worktree is not None and not isinstance(may_use_worktree, bool):
        raise ValueError(
            "workflow manifest may_use_worktree must be a boolean when provided"
        )
    return {
        "name": text("name"),
        "description": text("description"),
        "phases": phases,
        "read_only": read_only,
        **({"planned_agents": planned} if planned is not None else {}),
        "max_agents": maximum,
        "max_concurrency": concurrency,
        **({"token_budget": token_budget} if token_budget is not None else {}),
        **(
            {"may_use_worktree": may_use_worktree}
            if may_use_worktree is not None else {}
        ),
        "patterns": patterns,
    }
