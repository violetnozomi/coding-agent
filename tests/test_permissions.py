"""Focused tests for the split permission system."""
from __future__ import annotations

import json
import logging

from nz_coder.foundation import config
from nz_coder.permissions import PermissionManager
from nz_coder.foundation.private_paths import inspect_private_path
from nz_coder.runtime.execution.tool_executor import ToolExecutor
from nz_coder.tool_platform.permissions import PermissionRule
from nz_coder.tool_platform.permissioning.rules import scoped_allow_rule
from nz_coder.tools import (
    TOOL_EXECUTION_MODES,
    TOOL_HANDLERS,
    TOOL_PLAN_MODE_ALLOWED,
    TOOL_SIDE_EFFECTS,
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


def test_permission_manager_reports_matching_explicit_rule_source():
    """Auto routing can distinguish user authority from mode fallback."""
    pm = PermissionManager("auto")
    pm._allow_rules = [PermissionRule("bash", "allow", "prefix:git ")]
    pm._ask_rules = [PermissionRule("read_file", "ask")]

    assert pm.explicit_rule_behavior("bash", {"command": "git status"}) == "allow"
    assert pm.explicit_rule_behavior("read_file", {"path": "app.py"}) == "ask"
    assert pm.explicit_rule_behavior("todo", {}) is None


def test_argv_prefix_rule_rejects_shell_composition():
    """A reusable command prefix cannot authorize a composed second command."""
    rule = scoped_allow_rule("bash", {"command": "git status"})

    assert rule.matches("bash", {"command": "git status --short"}) is True
    for command in (
        "git status ; rm app.py",
        "git status && rm app.py",
        "git status | tee leaked.txt",
        "git status\nrm app.py",
        "git status $(touch changed.txt)",
        "git status `touch changed.txt`",
    ):
        assert rule.matches("bash", {"command": command}) is False


def test_legacy_prefix_rule_rejects_shell_composition():
    """Legacy settings prefixes cannot authorize a composed second command."""
    rule = PermissionRule("bash", "allow", "prefix:git ")

    assert rule.matches("bash", {"command": "git status --short"}) is True
    for command in (
        "git status ; rm app.py",
        "git status && rm app.py",
        "git status | tee leaked.txt",
        "git status\nrm app.py",
        "git status $(touch changed.txt)",
        "git status `touch changed.txt`",
    ):
        assert rule.matches("bash", {"command": command}) is False


def test_pytest_family_rule_rejects_shell_composition():
    """A reusable pytest family cannot authorize a composed side effect."""
    rule = scoped_allow_rule("bash", {"command": "pytest tests"})

    assert rule.matches("bash", {"command": "pytest tests -q"}) is True
    for command in (
        "pytest && rm -rf .",
        "pytest | tee leaked.txt",
        "python -m pytest; curl https://example.test",
        "python -m pytest $(touch changed.txt)",
    ):
        assert rule.matches("bash", {"command": command}) is False


def test_permission_manager_supports_http_once_reject_and_scoped_always(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    replies = iter(["once", "reject", "always"])
    pm = PermissionManager("default", asker=lambda _name, _input: next(replies))

    assert pm.ask_user("edit_file", {"path": "first.py"}) is True
    assert pm.ask_user("edit_file", {"path": "second.py"}) is False
    assert pm.ask_user("edit_file", {"path": "third.py"}) is True
    assert pm.check("edit_file", {"path": "later.py"}) == {
        "behavior": "allow",
        "reason": "Rule: edit_file",
    }


def test_permission_manager_persists_always_rule_without_clobbering_settings(
    tmp_path,
    monkeypatch,
):
    """Always survives restart while preserving settings owned by other features."""
    settings_dir = tmp_path / ".nz-coder"
    settings_dir.mkdir()
    settings_path = settings_dir / "settings.json"
    settings_path.write_text(
        json.dumps({"disabled_skills": ["review"], "permissions": {"deny": []}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    manager = PermissionManager("default", asker=lambda _name, _input: "always")

    assert manager.ask_user("edit_file", {"path": "first.py"}) is True

    reloaded = PermissionManager("default")
    assert reloaded.check("edit_file", {"path": "later.py"})["behavior"] == "allow"
    assert json.loads(settings_path.read_text(encoding="utf-8"))["disabled_skills"] == [
        "review"
    ]
    assert inspect_private_path(settings_path).hardened is True


def test_permission_manager_persists_token_scoped_bash_rule(tmp_path, monkeypatch):
    """Approving one git mutation must never authorize another subcommand."""
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    manager = PermissionManager("default", asker=lambda _name, _input: "always")

    assert manager.ask_user("bash", {"command": "git checkout -- app.py"}) is True

    reloaded = PermissionManager("default")
    assert reloaded.check("bash", {"command": "git checkout -- app.py"})["behavior"] == "allow"
    assert reloaded.check("bash", {"command": "git commit -m unsafe"})["behavior"] == "ask"


def test_permission_manager_persistence_failure_allows_once_only(
    tmp_path,
    monkeypatch,
    caplog,
):
    """A broken settings file cannot silently turn an approval into session-wide access."""
    settings_dir = tmp_path / ".nz-coder"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    manager = PermissionManager("default", asker=lambda _name, _input: "always")

    with caplog.at_level(logging.WARNING):
        assert manager.ask_user("edit_file", {"path": "first.py"}) is True

    assert manager.check("edit_file", {"path": "later.py"})["behavior"] == "ask"
    assert "could not persist permission rule" in caplog.text.lower()


def test_permission_manager_refuses_symlinked_settings_directory(
    tmp_path,
    monkeypatch,
    caplog,
):
    """Persistent approval cannot follow a workspace settings symlink."""
    real_directory = tmp_path / "real-settings"
    real_directory.mkdir()
    (tmp_path / ".nz-coder").symlink_to(real_directory, target_is_directory=True)
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    manager = PermissionManager("default", asker=lambda _name, _input: "always")

    with caplog.at_level(logging.WARNING):
        assert manager.ask_user("edit_file", {"path": "first.py"}) is True

    assert manager.check("edit_file", {"path": "later.py"})["behavior"] == "ask"
    assert not (real_directory / "settings.json").exists()
    assert "symbolic link" in caplog.text.lower()


def test_special_doom_loop_permission_supports_once_always_and_fail_closed():
    answers = iter(["once", "always"])
    pm = PermissionManager("default", asker=lambda _name, _input: next(answers))
    metadata = {"tool": "read_file", "input": {"path": "app.py"}}

    assert PermissionManager("default").ask_special("doom_loop", metadata) is False
    assert pm.ask_special("doom_loop", metadata) is True
    assert pm.ask_special("doom_loop", metadata) is True
    assert pm.ask_special("doom_loop", metadata) is True


def test_permission_manager_scopes_http_bash_always_to_command_prefix(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    pm = PermissionManager("default", asker=lambda _name, _input: "always")

    assert pm.ask_user("bash", {"command": "git status"}) is True
    assert pm.check("bash", {"command": "git status --short"})["behavior"] == "allow"
    assert pm.check("bash", {"command": "git commit -m unsafe"})["behavior"] == "ask"
    assert pm.check("bash", {"command": "python build.py"})["behavior"] == "ask"


def test_permission_manager_scopes_pytest_approval_without_allowing_arbitrary_python(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    pm = PermissionManager("acceptEdits", asker=lambda _name, _input: "always")

    command = "python -m pytest tests/test_parser.py -q"
    assert pm.ask_user("bash", {"command": command}) is True

    assert pm.check("bash", {
        "command": "python -m pytest tests/test_other.py -q",
    })["behavior"] == "allow"
    assert pm.check("bash", {
        "command": "python -c \"from pathlib import Path; Path('x').unlink()\"",
    })["behavior"] == "ask"


def test_permission_manager_denies_model_invented_external_workspace_path(
    tmp_path, monkeypatch,
):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    monkeypatch.setattr(config, "WORKDIR", workspace)
    pm = PermissionManager("auto")

    decision = pm.check("bash", {
        "command": f"diff -rq {outside} {workspace}",
    })

    assert decision["behavior"] == "deny"
    assert "outside workspace" in decision["reason"].lower()


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
        TOOL_SIDE_EFFECTS.pop(name, None)
        TOOL_PLAN_MODE_ALLOWED.pop(name, None)
        TOOL_SPECS[:] = [
            spec for spec in TOOL_SPECS
            if spec["function"]["name"] != name
        ]


def test_permission_modes_use_side_effect_instead_of_scheduler_mode():
    """Serial FS writes and scheduled state writes keep distinct authority."""
    fs_name = "_test_serial_fs_write"
    state_name = "_test_scheduled_state_write"

    try:
        register(
            fs_name,
            "test",
            {"type": "object", "properties": {}},
            lambda: "ok",
            execution="serial",
            side_effect="mutates-fs",
        )
        register(
            state_name,
            "test",
            {"type": "object", "properties": {}},
            lambda: "ok",
            execution="write",
            side_effect="mutates-state",
        )

        assert PermissionManager("default").check(fs_name, {})["behavior"] == "ask"
        assert PermissionManager("acceptEdits").check(fs_name, {})["behavior"] == "allow"
        assert PermissionManager("plan").check(fs_name, {})["behavior"] == "deny"

        assert PermissionManager("default").check(state_name, {})["behavior"] == "ask"
        assert PermissionManager("acceptEdits").check(state_name, {})["behavior"] == "ask"
        assert PermissionManager("plan").check(state_name, {})["behavior"] == "deny"
    finally:
        for name in (fs_name, state_name):
            TOOL_HANDLERS.pop(name, None)
            TOOL_EXECUTION_MODES.pop(name, None)
            TOOL_SIDE_EFFECTS.pop(name, None)
            TOOL_PLAN_MODE_ALLOWED.pop(name, None)
        TOOL_SPECS[:] = [
            spec for spec in TOOL_SPECS
            if spec["function"]["name"] not in {fs_name, state_name}
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


def test_explicit_ask_rule_overrides_safe_state_tool_default():
    """Project/user ask rules retain authority over built-in safe exceptions."""
    pm = PermissionManager("default")
    pm._ask_rules = [PermissionRule("task", "ask")]

    assert pm.check("task", {"prompt": "inspect"}) == {
        "behavior": "ask",
        "reason": "Ask rule: task",
    }


def test_explicit_ask_rule_applies_to_read_only_bash_command():
    """Bash's specialized policy must not bypass configured ask rules."""
    pm = PermissionManager("default")
    pm._ask_rules = [PermissionRule("bash", "ask", "prefix:git ")]

    assert pm.check("bash", {"command": "git status"}) == {
        "behavior": "ask",
        "reason": "Ask rule: prefix:git ",
    }


def test_process_allow_rule_cannot_bypass_hard_shell_safety():
    """Persistent-process rules must not authorize commands Bash hard-denies."""
    pm = PermissionManager("default")
    pm._allow_rules = [PermissionRule("process", "allow")]

    dangerous = pm.check("process", {
        "operation": "start",
        "command": "rm -rf /",
    })
    external = pm.check("process", {
        "operation": "start",
        "command": "cat /etc/passwd",
    })

    assert dangerous["behavior"] == "deny"
    assert external["behavior"] == "deny"


def test_process_rules_cannot_bypass_plan_mode():
    """Plan mode remains authoritative over process start and stdin writes."""
    pm = PermissionManager("plan")
    pm._allow_rules = [PermissionRule("process", "allow")]

    start = pm.check("process", {
        "operation": "start",
        "command": "touch changed.txt",
    })
    write = pm.check("process", {
        "operation": "write",
        "process_id": "proc_test",
        "data": "mutate",
    })

    assert start["behavior"] == "deny"
    assert write == {
        "behavior": "deny",
        "reason": "Plan mode: process stdin blocked",
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


def test_tool_executor_rejects_nonfinite_arguments_before_dispatch(
    monkeypatch,
):
    """NaN and infinities are invalid JSON arguments, never tool input."""
    from nz_coder.runtime.execution import tool_executor as tool_executor_module

    dispatched = []
    monkeypatch.setattr(
        tool_executor_module,
        "dispatch",
        lambda name, tool_input: dispatched.append((name, tool_input)) or "ok",
    )
    executor = ToolExecutor(PermissionManager("auto"))
    calls = [
        {
            "id": "raw-nan",
            "function": {"name": "bash", "arguments": '{"command":NaN}'},
        },
        {
            "id": "dict-inf",
            "function": {
                "name": "bash",
                "arguments": {"command": float("inf")},
            },
        },
    ]

    results = [executor.execute_one(call, 0) for call in calls]

    assert dispatched == []
    assert all(result.executed is False for result in results)
    assert all(result.dispatch_failed is True for result in results)
    assert all(result.permission_denied is False for result in results)
    assert all("Invalid JSON arguments" in result.output for result in results)


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
