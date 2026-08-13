"""Host-side workflow identity, invocation, limits, and approval contracts."""
from __future__ import annotations

import copy
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Callable

from nz_coder.runtime.workflow_builtins import list_builtin_workflows
from nz_coder.runtime.workflow_library import discover_workflow_capsules
from nz_coder.runtime.workflow_run_store import list_workflow_run_records


SYSTEM_WORKFLOW_MAX_AGENTS = 64
SYSTEM_WORKFLOW_MAX_CONCURRENCY = 8
SYSTEM_WORKFLOW_TOKEN_BUDGET = 200_000
_RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,200}")
SCOUT_THEN_AUTHOR_PROMPT_LINES = (
    "Set up and run a multi-agent workflow for this task.",
    "First investigate the relevant files and sub-problems with your own tools, "
    "then author and run it with workflow_run. Bake concrete findings, exact paths, "
    "specific comparison dimensions, and real output schemas into child prompts "
    "instead of re-delegating the scouting.",
)

WorkflowApprovalAsker = Callable[[dict], str]
_WORKFLOW_APPROVAL_ASKER: ContextVar[WorkflowApprovalAsker | None] = ContextVar(
    "nz_coder_workflow_approval_asker", default=None
)


@contextmanager
def scoped_workflow_approval_asker(asker: WorkflowApprovalAsker | None):
    """Bind one host-owned approval channel to the active execution context."""
    token = _WORKFLOW_APPROVAL_ASKER.set(asker)
    try:
        yield asker
    finally:
        _WORKFLOW_APPROVAL_ASKER.reset(token)


def current_workflow_approval_asker() -> WorkflowApprovalAsker | None:
    return _WORKFLOW_APPROVAL_ASKER.get()


def workflow_invocation_decision(source: str) -> dict:
    """Only an explicit command may trigger a host suggestion before Agent work."""
    normalized = str(source or "").strip().lower()
    if normalized not in {"command", "natural-language"}:
        raise ValueError("workflow invocation source must be command or natural-language")
    return {"source": normalized, "action": "suggest" if normalized == "command" else "none"}


def workflow_start_outcome_consumes_turn(outcome: str) -> bool:
    normalized = str(outcome or "").strip().lower()
    if normalized not in {"started", "declined", "cancelled", "failed"}:
        raise ValueError("invalid workflow start outcome")
    return normalized in {"started", "cancelled"}


def build_scout_then_author_prompt(request: str) -> str:
    text = str(request or "").strip()
    if not text:
        raise ValueError("workflow authoring request must be non-empty")
    return "\n".join((*SCOUT_THEN_AUTHOR_PROMPT_LINES, "", text))


def validate_workflow_display_name(value: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 200 or any(ord(character) < 32 for character in text):
        raise ValueError("workflow display_name must contain 1 to 200 printable characters")
    return text


def _positive_limit(value: Any, hard_cap: int) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return 1
    return min(value, hard_cap)


def _token_limit(value: Any, hard_cap: int) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None
    return min(int(value), hard_cap)


def clamp_workflow_limits(
    manifest: dict | None,
    host_policy: dict | None = None,
    *,
    system_max_agents: int = SYSTEM_WORKFLOW_MAX_AGENTS,
    system_max_concurrency: int = SYSTEM_WORKFLOW_MAX_CONCURRENCY,
    system_token_budget: int = SYSTEM_WORKFLOW_TOKEN_BUDGET,
) -> dict:
    """Apply min-wins manifest, host, and system ceilings."""
    meta = dict(manifest or {})
    host = dict(host_policy or {})
    allowed = {"max_agents", "max_concurrency", "token_budget"}
    unknown = sorted(set(host) - allowed)
    if unknown:
        raise ValueError(f"workflow host policy contains unsupported field: {unknown[0]}")

    def minimum(key: str, hard_cap: int) -> int | None:
        values = [
            item for item in (
                _positive_limit(meta.get(key), hard_cap),
                _positive_limit(host.get(key), hard_cap),
            )
            if item is not None
        ]
        return min(values) if values else None

    max_agents = minimum("max_agents", max(1, int(system_max_agents)))
    max_concurrency = minimum(
        "max_concurrency", max(1, int(system_max_concurrency))
    ) or max(1, int(system_max_concurrency))
    token_values = [
        item for item in (
            _token_limit(meta.get("token_budget"), system_token_budget),
            _token_limit(host.get("token_budget"), system_token_budget),
        )
        if item is not None
    ]
    token_budget = min(token_values) if token_values else None
    return {
        "max_agents": max_agents,
        "max_concurrency": max_concurrency,
        "token_budget": token_budget,
    }


def build_workflow_approval_summary(
    manifest: dict | None,
    host_policy: dict | None = None,
    **system_limits: int,
) -> dict:
    meta = dict(manifest or {})
    limits = clamp_workflow_limits(meta, host_policy, **system_limits)
    return {
        "name": str(meta.get("name") or "workflow")[:200],
        "description": str(meta.get("description") or "")[:1000],
        "phases": [str(item)[:200] for item in (meta.get("phases") or [])],
        **(
            {"planned_agents": int(meta["planned_agents"])}
            if isinstance(meta.get("planned_agents"), int)
            and not isinstance(meta.get("planned_agents"), bool)
            else {}
        ),
        **limits,
        "writes_files": meta.get("read_only") is not True,
    }


def workflow_approval_digest(summary: dict) -> str:
    """Bind a decision to the exact effective limits and write-risk summary."""
    encoded = json.dumps(
        summary,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evaluate_workflow_approval(
    summary: dict,
    *,
    decision: str = "",
    expected_digest: str = "",
    headless: bool = False,
) -> dict:
    """Resolve approval without letting a stale UI decision authorize new limits."""
    digest = workflow_approval_digest(summary)
    if expected_digest and expected_digest != digest:
        return {
            "outcome": "failed",
            "reason": "stale approval summary",
            "digest": digest,
            "consumes_turn": False,
        }
    normalized = str(decision or "").strip().lower()
    if not normalized and headless:
        normalized = "approve"
        mode = "headless-auto"
    else:
        mode = "explicit"
    outcomes = {
        "approve": ("started", "approved"),
        "deny": ("declined", "approval denied"),
        "cancel": ("cancelled", "approval cancelled"),
        "": ("pending", "approval required"),
    }
    if normalized not in outcomes:
        raise ValueError("workflow approval decision must be approve, deny, or cancel")
    outcome, reason = outcomes[normalized]
    return {
        "outcome": outcome,
        "reason": reason,
        "decision": normalized or None,
        "mode": mode,
        "digest": digest,
        "consumes_turn": outcome in {"started", "cancelled"},
    }


def resolve_workflow_identity(
    target: str,
    *,
    workspace: Path,
    runs_root: Path,
    personal_dir: Path | None = None,
) -> dict:
    """Resolve run IDs/display aliases and reusable names, failing on ambiguity."""
    requested = str(target or "")
    name = requested.strip()
    if not name:
        return {"kind": "missing", "target": requested}
    records = list_workflow_run_records(runs_root, 1000)
    direct = next(
        (
            item for item in records
            if item.get("run_id") == name and _RUN_ID.fullmatch(name) and ".." not in name
        ),
        None,
    )
    aliases = [] if direct is not None else [
        item for item in records if str(item.get("display_name") or "").strip() == name
    ]
    run_matches = [direct] if direct is not None else aliases
    saved = next(
        (
            item for item in discover_workflow_capsules(workspace, personal_dir)
            if item.get("name") == name
        ),
        None,
    )
    builtin = name if name in list_builtin_workflows() else None
    reusable_matches = int(saved is not None) + int(builtin is not None)
    if len(run_matches) > 1 or (run_matches and reusable_matches) or reusable_matches > 1:
        return {
            "kind": "ambiguous",
            "target": name,
            "matches": [
                *("run" for _ in [0] if run_matches),
                *("saved" for _ in [0] if saved is not None),
                *("builtin" for _ in [0] if builtin is not None),
            ],
            **(
                {"run": _run_identity(run_matches[0], runs_root, target=name)}
                if len(run_matches) == 1 else {}
            ),
            **({"saved_workflow": copy.deepcopy(saved)} if saved is not None else {}),
            **({"builtin_workflow": builtin} if builtin is not None else {}),
        }
    if len(run_matches) == 1:
        return _run_identity(run_matches[0], runs_root, target=name)
    if saved is not None:
        return {"kind": "saved", "target": name, "saved_workflow": copy.deepcopy(saved)}
    if builtin is not None:
        return {"kind": "builtin", "target": name, "builtin_workflow": builtin}
    return {"kind": "missing", "target": name}


def _run_identity(record: dict, runs_root: Path, *, target: str) -> dict:
    run_id = str(record.get("run_id") or "")
    return {
        "kind": "run",
        "target": target,
        "run_id": run_id,
        "run_dir": str((Path(runs_root).resolve() / run_id).resolve()),
        **(
            {"workflow_name": str(record["workflow_name"])}
            if record.get("workflow_name") else {}
        ),
        **(
            {"display_name": str(record["display_name"])}
            if record.get("display_name") else {}
        ),
    }
