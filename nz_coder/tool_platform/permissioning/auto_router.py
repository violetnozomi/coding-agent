"""Deterministic admission routing for interactive Auto mode."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PureWindowsPath

from nz_coder.tools import (
    FILESYSTEM_MUTATION_TOOLS,
    collect_filesystem_mutation_paths,
    get_dynamic_tool_binding_identity,
    get_tool_side_effect,
    is_filesystem_mutation_tool,
    is_transactional_dynamic_tool,
)

from .tool_groups import READ_TOOLS


class AutoRouteKind(str, Enum):
    """Deterministic action selected before any classifier request."""

    HARD_DENY = "hard_deny"
    MANUAL = "manual"
    FAST_ALLOW = "fast_allow"
    CLASSIFY = "classify"


@dataclass(frozen=True)
class AutoRoute:
    """One stable admission route and its exact action identity."""

    kind: AutoRouteKind
    reason_code: str
    reason: str
    action_digest: str


AUTO_SAFE_STATE_TOOLS = frozenset({
    "compact",
    "load_optional_tools",
    "load_skill",
    "plan_enter",
    "plan_exit",
    "plan_verification",
    "question",
    "read_scratchpad",
    "recall_memory",
    "todo",
    "verify_changed_files",
    "verify_project_build",
    "write_plan",
})

_AGENT_EVENT_TOOLS = frozenset({
    "agent_manager",
    "background_task_apply",
    "background_task_start",
    "emit_handoff",
    "message_parent",
    "send_message",
    "subagent",
    "task",
    "workflow_run",
})

_LOCAL_PATH_TOOLS = frozenset(READ_TOOLS) | FILESYSTEM_MUTATION_TOOLS
_SAFE_PROCESS_OPERATIONS = frozenset({"read", "status", "list", "resize", "kill"})
_NETWORK_TOOLS = frozenset({"webfetch", "web_search"})


def canonical_action_digest(
    tool_name: str,
    tool_input: dict,
    workspace: Path,
) -> str:
    """Return a workspace-bound digest for one exact tool invocation."""
    normalized_name = str(tool_name or "").strip().lower()
    action = {
        "tool": normalized_name,
        "input": tool_input,
        "workspace": str(Path(workspace).resolve()),
    }
    binding_identity = get_dynamic_tool_binding_identity(normalized_name)
    if binding_identity:
        action["binding_identity"] = binding_identity
    payload = json.dumps(
        action,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def route_auto_action(
    tool_name: str,
    tool_input: dict,
    *,
    workspace: Path,
    permission_decision: dict,
    explicit_behavior: str | None,
    approved_digests: set[str] | frozenset[str],
) -> AutoRoute:
    """Select hard deny, manual, fast allow, or classifier admission."""
    name = str(tool_name or "").strip().lower()
    digest = canonical_action_digest(name, tool_input, workspace)
    escaped = _first_workspace_escape(name, tool_input, Path(workspace))
    if escaped:
        return AutoRoute(
            AutoRouteKind.HARD_DENY,
            "workspace_escape",
            f"Path escapes workspace: {escaped}",
            digest,
        )

    behavior = str(permission_decision.get("behavior") or "")
    reason = str(permission_decision.get("reason") or "Permission denied")
    if behavior == "deny":
        return AutoRoute(
            AutoRouteKind.HARD_DENY,
            "permission_deny",
            reason,
            digest,
        )
    if explicit_behavior == "ask" or behavior == "ask":
        return AutoRoute(
            AutoRouteKind.MANUAL,
            "explicit_ask",
            reason,
            digest,
        )
    if explicit_behavior == "allow":
        return AutoRoute(
            AutoRouteKind.FAST_ALLOW,
            "explicit_allow",
            "Allowed by explicit permission rule",
            digest,
        )
    if digest in approved_digests:
        return AutoRoute(
            AutoRouteKind.FAST_ALLOW,
            "session_approval",
            "Allowed by exact session approval",
            digest,
        )

    if name == "bash":
        return _classify("shell_command", "Shell command needs risk review", digest)
    if name == "process":
        operation = str(tool_input.get("operation") or "").strip().lower()
        if operation in _SAFE_PROCESS_OPERATIONS:
            return AutoRoute(
                AutoRouteKind.FAST_ALLOW,
                "session_process_control",
                "Session-owned process control",
                digest,
            )
        return _classify(
            "process_side_effect",
            "Persistent process action needs risk review",
            digest,
        )
    if name.startswith("mcp_"):
        return _classify(
            "external_mcp",
            "External MCP action needs risk review",
            digest,
        )
    if name in _NETWORK_TOOLS:
        return _classify(
            "network_access",
            "Network access needs risk review",
            digest,
        )
    if name in _AGENT_EVENT_TOOLS:
        return _classify(
            "agent_side_effect",
            "Agent orchestration action needs risk review",
            digest,
        )

    effect = get_tool_side_effect(name)
    if effect in {"reads-network", "mutates-network", "mutates-shell"}:
        return _classify(
            "external_side_effect",
            "External side effect needs risk review",
            digest,
        )
    if effect == "readonly" or name in READ_TOOLS:
        return AutoRoute(
            AutoRouteKind.FAST_ALLOW,
            "local_read",
            "Local read-only tool",
            digest,
        )
    if name in AUTO_SAFE_STATE_TOOLS:
        return AutoRoute(
            AutoRouteKind.FAST_ALLOW,
            "safe_state",
            "Explicit safe-state tool",
            digest,
        )
    if is_filesystem_mutation_tool(name) and is_transactional_dynamic_tool(name):
        return AutoRoute(
            AutoRouteKind.FAST_ALLOW,
            "transactional_edit",
            "Transaction-covered workspace edit",
            digest,
        )
    return _classify(
        "unknown_side_effect",
        "Unknown or stateful action needs risk review",
        digest,
    )


def _classify(reason_code: str, reason: str, digest: str) -> AutoRoute:
    return AutoRoute(AutoRouteKind.CLASSIFY, reason_code, reason, digest)


def _first_workspace_escape(
    tool_name: str,
    tool_input: dict,
    workspace: Path,
) -> str:
    if tool_name not in _LOCAL_PATH_TOOLS:
        return ""
    root = workspace.resolve()
    for raw in collect_filesystem_mutation_paths(tool_input):
        if _is_windows_absolute(raw):
            return raw
        candidate = Path(raw)
        if candidate.is_absolute():
            return raw
        resolved = (root / candidate).resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError:
            return raw
    return ""


def _is_windows_absolute(path: str) -> bool:
    candidate = PureWindowsPath(str(path or ""))
    return bool(candidate.drive or candidate.root)
