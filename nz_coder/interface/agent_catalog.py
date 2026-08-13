"""Read-only product projection of primary and child Agent definitions."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nz_coder import config


_CHILD_DESCRIPTIONS = {
    "explore": "Fast read-only repository exploration",
    "plan": "Read-only implementation planning",
    "general-purpose": "Isolated write-capable coding Agent",
    "reflection": "Read-only completion and verification critic",
}


def agent_catalog(agent: Any, workspace: Path) -> list[dict[str, Any]]:
    """Project live Session identity plus the Runtime's supported child roles.

    This is presentation-only data. It does not create Agents or own routing
    state; child names are read from the canonical subagent runtime.
    """
    from nz_coder.runtime.subagent import (
        _CANONICAL_SUBAGENT_TYPES,
        _READ_ONLY_TYPES,
    )

    del workspace  # Reserved for future workspace-owned Agent definitions.
    parent_model = str(getattr(agent, "model_id", None) or config.MODEL_ID)
    effort = getattr(agent, "model_variant", None)
    permission_mode = str(
        getattr(getattr(agent, "permissions", None), "mode", "default")
    )
    values: list[dict[str, Any]] = [{
        "name": "worker",
        "description": "Primary coding Agent for this Session",
        "model": parent_model,
        "reasoning_effort": effort,
        "tools": "session tool policy",
        "permissions": permission_mode,
        "role": "primary",
    }]
    for name in sorted(_CANONICAL_SUBAGENT_TYPES):
        model = (
            str(config.SUBAGENT_EXPLORE_MODEL or parent_model)
            if name == "explore"
            else parent_model
        )
        values.append({
            "name": name,
            "description": _CHILD_DESCRIPTIONS[name],
            "model": model,
            "reasoning_effort": effort,
            "tools": "read-only child policy" if name in _READ_ONLY_TYPES else "write child policy",
            "permissions": "read-only" if name in _READ_ONLY_TYPES else "isolated write worktree",
            "role": "child",
        })
    return values


__all__ = ["agent_catalog"]
