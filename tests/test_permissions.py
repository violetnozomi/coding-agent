"""Focused tests for the split permission system."""
from __future__ import annotations

import json

from nz_coder import config
from nz_coder.permissions import PermissionManager
from nz_coder.runtime.tool_executor import ToolExecutor
from nz_coder.tool_platform.permissions import PermissionRule
from nz_coder.tools import (
    TOOL_EXECUTION_MODES,
    TOOL_HANDLERS,
    TOOL_SPECS,
    register,
)


def test_permission_manager_normalizes_unknown_mode():
    pm = PermissionManager("weird-mode")

    assert pm.mode == "default"


def test_permission_manager_uses_injected_headless_asker():
    calls = []
    pm = PermissionManager(
        "default",
        asker=lambda name, tool_input: calls.append((name, tool_input)) or False,
    )

    assert pm.ask_user("write_file", {"path": "app.py"}) is False
    assert calls == [("write_file", {"path": "app.py"})]


def test_permission_manager_supports_http_once_reject_and_scoped_always():
    replies = iter(["once", "reject", "always"])
    pm = PermissionManager("default", asker=lambda _name, _input: next(replies))

    assert pm.ask_user("edit_file", {"path": "first.py"}) is True
    assert pm.ask_user("edit_file", {"path": "second.py"}) is False
    assert pm.ask_user("edit_file", {"path": "third.py"}) is True
    assert pm.check("edit_file", {"path": "later.py"}) == {
        "behavior": "allow",
        "reason": "Rule: edit_file",
    }


def test_special_doom_loop_permission_supports_once_always_and_fail_closed():
    answers = iter(["once", "always"])
    pm = PermissionManager("default", asker=lambda _name, _input: next(answers))
    metadata = {"tool": "read_file", "input": {"path": "app.py"}}

    assert PermissionManager("default").ask_special("doom_loop", metadata) is False
    assert pm.ask_special("doom_loop", metadata) is True
    assert pm.ask_special("doom_loop", metadata) is True
    assert pm.ask_special("doom_loop", metadata) is True


def test_permission_manager_scopes_http_bash_always_to_command_prefix():
    pm = PermissionManager("default", asker=lambda _name, _input: "always")

    assert pm.ask_user("bash", {"command": "git status"}) is True
    assert pm.check("bash", {"command": "git diff"}) == {
        "behavior": "allow",
        "reason": "Rule: prefix:git ",
    }
    assert pm.check("bash", {"command": "python build.py"})["behavior"] == "ask"


def test_permission_manager_accept_edits_allows_write_tools():
    pm = PermissionManager("acceptEdits")

    decision = pm.check("write_file", {"path": "demo.py", "content": "print('ok')\n"})

    assert decision == {"behavior": "allow", "reason": "acceptEdits mode"}


def test_dynamic_write_effect_is_enforced_by_permission_modes():
    name = "_test_permission_write"

    def handler():
        return "ok"

    try:
        register(
            name,
            "test",
            {"type": "object", "properties": {}},
            handler,
            execution="write",
        )

        default = PermissionManager("default").check(name, {})
        plan = PermissionManager("plan").check(name, {})
        accept_edits = PermissionManager("acceptEdits").check(name, {})

        assert default == {"behavior": "ask", "reason": f"Write operation: {name}"}
        assert plan == {
            "behavior": "deny",
            "reason": "Plan mode: write operations blocked",
        }
        assert accept_edits == {"behavior": "allow", "reason": "acceptEdits mode"}
    finally:
        TOOL_HANDLERS.pop(name, None)
        TOOL_EXECUTION_MODES.pop(name, None)
        TOOL_SPECS[:] = [
            spec for spec in TOOL_SPECS
            if spec["function"]["name"] != name
        ]


def test_permission_manager_plan_blocks_unknown_bash():
    pm = PermissionManager("plan")

    decision = pm.check("bash", {"command": "python -m pytest -q"})

    assert decision["behavior"] == "deny"
    assert "Plan mode: shell blocked" in decision["reason"]


def test_webfetch_defaults_to_read_only_allow_but_honors_ask_rule():
    default = PermissionManager("acceptEdits")
    configured = PermissionManager("default")
    configured._ask_rules = [PermissionRule("webfetch", "ask")]
    tool_input = {"url": "https://example.test/docs"}

    assert default.check("webfetch", tool_input) == {
        "behavior": "allow",
        "reason": "Read-only web fetch",
    }
    assert configured.check("webfetch", tool_input) == {
        "behavior": "ask",
        "reason": "Ask rule: webfetch",
    }


def test_tool_executor_distinguishes_permission_denial_from_tool_error():
    executor = ToolExecutor(PermissionManager("plan"))
    denied = executor.execute_one({
        "id": "call-denied",
        "function": {
            "name": "write_file",
            "arguments": json.dumps({"path": "app.py", "content": "x"}),
        },
    }, 0)
    invalid = executor.execute_one({
        "id": "call-invalid",
        "function": {"name": "read_file", "arguments": "{"},
    }, 0)

    assert denied.dispatch_failed is True
    assert denied.permission_denied is True
    assert invalid.dispatch_failed is True
    assert invalid.permission_denied is False


def test_permission_manager_session_allow_rule_applies_to_bash():
    pm = PermissionManager("default")
    pm.add_allow("bash(prefix:git )")

    decision = pm.check("bash", {"command": "git commit -m test"})

    assert decision == {"behavior": "allow", "reason": "Rule: prefix:git "}


def test_permission_manager_loads_project_rules(tmp_path, monkeypatch):
    settings_dir = tmp_path / ".nz-coder"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(
        json.dumps(
            {
                "permissions": {
                    "allow": ["bash(prefix:git )"],
                    "deny": ["write_file"],
                    "ask": ["edit_file"],
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "WORKDIR", tmp_path)

    pm = PermissionManager("default")

    assert pm._allow_rules == [PermissionRule("bash", "allow", "prefix:git ")]
    assert pm._deny_rules == [PermissionRule("write_file", "deny")]
    assert pm._ask_rules == [PermissionRule("edit_file", "ask")]
