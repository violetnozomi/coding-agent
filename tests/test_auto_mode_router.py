"""Deterministic admission routes used before the Auto classifier."""
from __future__ import annotations

import os

from nz_coder.permissions import PermissionManager
from nz_coder.tool_platform.permissions import PermissionRule
from nz_coder.tool_platform.permissioning.auto_router import (
    AutoRouteKind,
    canonical_action_digest,
    route_auto_action,
)
from nz_coder.tools import scoped_dynamic_tools


def _route(
    tmp_path,
    name: str,
    tool_input: dict,
    *,
    decision: dict | None = None,
    explicit: str | None = None,
    approved: set[str] | None = None,
):
    return route_auto_action(
        name,
        tool_input,
        workspace=tmp_path,
        permission_decision=(
            decision or {"behavior": "allow", "reason": "Auto mode"}
        ),
        explicit_behavior=explicit,
        approved_digests=set(approved or ()),
    )


def test_router_hard_denies_before_explicit_or_session_allow(tmp_path) -> None:
    """A saved approval must never override workspace or shell hard safety."""
    escaped = {"path": "../outside.py", "content": "x"}
    digest = canonical_action_digest("write_file", escaped, tmp_path)

    route = _route(
        tmp_path,
        "write_file",
        escaped,
        explicit="allow",
        approved={digest},
    )
    shell = _route(
        tmp_path,
        "bash",
        {"command": "rm -rf /"},
        decision={
            "behavior": "deny",
            "reason": "Blocked: recursive root delete",
        },
        explicit="allow",
    )

    assert route.kind is AutoRouteKind.HARD_DENY
    assert route.reason_code == "workspace_escape"
    assert shell.kind is AutoRouteKind.HARD_DENY


def test_router_fast_allows_local_read_and_transactional_edit(tmp_path) -> None:
    """Local observation and transaction-covered edits make no model call."""
    read = _route(tmp_path, "read_file", {"path": "app.py"})
    edit = _route(
        tmp_path,
        "edit_file",
        {"path": "app.py", "old_text": "a", "new_text": "b"},
    )

    assert read.kind is AutoRouteKind.FAST_ALLOW
    assert edit.kind is AutoRouteKind.FAST_ALLOW


def test_router_classifies_shell_network_mcp_agent_and_unknown(tmp_path) -> None:
    """External or untrusted effects cannot inherit the local read fast path."""
    cases = [
        ("bash", {"command": "git status"}),
        ("process", {"operation": "write", "process_id": "p1", "data": "x"}),
        ("mcp_demo_read", {"query": "status"}),
        ("webfetch", {"url": "https://example.test"}),
        ("task", {"prompt": "inspect"}),
        ("unregistered_tool", {}),
    ]

    assert [
        _route(tmp_path, name, payload).kind for name, payload in cases
    ] == [AutoRouteKind.CLASSIFY] * len(cases)


def test_router_honors_explicit_ask_before_fast_path(tmp_path) -> None:
    """A user ask rule remains authoritative for an otherwise safe read."""
    route = _route(
        tmp_path,
        "read_file",
        {"path": "app.py"},
        explicit="ask",
    )

    assert route.kind is AutoRouteKind.MANUAL


def test_legacy_bash_prefix_cannot_bypass_auto_classifier(tmp_path) -> None:
    """Composed commands must not inherit a legacy explicit allow prefix."""
    manager = PermissionManager("auto")
    manager._allow_rules = [PermissionRule("bash", "allow", "prefix:git ")]
    manager._deny_rules = []
    manager._ask_rules = []
    tool_input = {"command": "git status && touch pwned"}

    route = _route(
        tmp_path,
        "bash",
        tool_input,
        decision=manager.check("bash", tool_input),
        explicit=manager.explicit_rule_behavior("bash", tool_input),
    )

    assert route.kind is AutoRouteKind.CLASSIFY


def test_pytest_family_rule_cannot_bypass_auto_classifier(tmp_path) -> None:
    """A composed pytest command remains residual after an always rule."""
    manager = PermissionManager("auto")
    manager._allow_rules = [
        PermissionRule("bash", "allow", "family:pytest"),
    ]
    manager._deny_rules = []
    manager._ask_rules = []
    tool_input = {"command": "pytest && rm -rf ."}

    route = _route(
        tmp_path,
        "bash",
        tool_input,
        decision=manager.check("bash", tool_input),
        explicit=manager.explicit_rule_behavior("bash", tool_input),
    )

    assert route.kind is AutoRouteKind.CLASSIFY


def test_session_approval_is_exact_to_action_and_workspace(tmp_path) -> None:
    """Auto always approval cannot expand to new flags or another workspace."""
    action = {"command": "git status"}
    digest = canonical_action_digest("bash", action, tmp_path)

    exact = _route(tmp_path, "bash", action, approved={digest})
    changed = _route(
        tmp_path,
        "bash",
        {"command": "git status --short"},
        approved={digest},
    )
    other_workspace = _route(
        tmp_path / "other",
        "bash",
        action,
        approved={digest},
    )

    assert exact.kind is AutoRouteKind.FAST_ALLOW
    assert changed.kind is AutoRouteKind.CLASSIFY
    assert other_workspace.kind is AutoRouteKind.CLASSIFY


def test_mcp_session_approval_is_exact_to_binding_generation(tmp_path) -> None:
    """A replacement MCP implementation cannot inherit an old approval."""
    action = {"query": "status"}

    def definition(identity: str) -> dict:
        return {
            "name": "mcp_generation_status",
            "description": "status",
            "parameters": {"type": "object", "properties": {}},
            "handler": lambda **_kwargs: "ok",
            "execution": "read",
            "side_effect": "reads-network",
            "binding_identity": identity,
        }

    with scoped_dynamic_tools([definition("a" * 64)]):
        approved_digest = canonical_action_digest(
            "mcp_generation_status",
            action,
            tmp_path,
        )
        exact = _route(
            tmp_path,
            "mcp_generation_status",
            action,
            approved={approved_digest},
        )

    with scoped_dynamic_tools([definition("b" * 64)]):
        replacement_digest = canonical_action_digest(
            "mcp_generation_status",
            action,
            tmp_path,
        )
        replacement = _route(
            tmp_path,
            "mcp_generation_status",
            action,
            approved={approved_digest},
        )

    assert exact.kind is AutoRouteKind.FAST_ALLOW
    assert replacement.kind is AutoRouteKind.CLASSIFY
    assert replacement_digest != approved_digest


def test_router_rejects_nested_batch_and_patch_escape(tmp_path) -> None:
    """Every nested file target is checked before the transaction fast path."""
    batch = _route(
        tmp_path,
        "write_files_batch",
        {
            "files": [
                {"path": "ok.py", "content": "ok"},
                {"path": "../bad.py", "content": "bad"},
            ],
        },
    )
    patch = _route(
        tmp_path,
        "apply_patch",
        {
            "changes": [
                {"op": "create", "path": "../bad.py", "content": "bad"},
            ],
        },
    )

    assert batch.kind is AutoRouteKind.HARD_DENY
    assert patch.kind is AutoRouteKind.HARD_DENY


def test_router_rejects_symlink_windows_drive_and_unc_escape(tmp_path) -> None:
    """Cross-platform absolute forms and resolved symlinks cannot escape."""
    inputs = [
        {"path": "C:\\outside\\escaped.py", "content": "x"},
        {"path": "\\\\server\\share\\escaped.py", "content": "x"},
    ]
    if os.name != "nt":
        outside = tmp_path.parent / "outside-auto-router"
        outside.mkdir(exist_ok=True)
        (tmp_path / "link").symlink_to(outside, target_is_directory=True)
        inputs.append({"path": "link/escaped.py", "content": "x"})

    routes = [_route(tmp_path, "write_file", item) for item in inputs]

    assert all(route.kind is AutoRouteKind.HARD_DENY for route in routes)
