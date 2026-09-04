"""Project and user instructions are immutable inputs to one run epoch."""
from __future__ import annotations

from dataclasses import replace
import os

import pytest


SECRET = "API_KEY=INSTRUCTION-SECRET-MUST-NOT-LEAK"


def test_agents_symlink_to_env_never_enters_prompt(tmp_path):
    from nz_coder.foundation.project_control import UnsafeProjectControl
    from nz_coder.foundation.workspace_trust import load_config_snapshot

    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".env").write_text(SECRET, encoding="utf-8")
    try:
        (workspace / "AGENTS.md").symlink_to(".env")
    except OSError:
        pytest.skip("file symlinks are unavailable")

    snapshot = load_config_snapshot(
        workspace, environ={}, user_config_path=tmp_path / "user" / "config.env"
    )

    assert snapshot.project_control.files == {}
    assert any("unsafe workspace control" in item.message for item in snapshot.issues)
    assert SECRET not in snapshot.public_json()
    with pytest.raises(UnsafeProjectControl):
        from nz_coder.foundation.project_control import capture_project_control_snapshot
        capture_project_control_snapshot(workspace)


def test_agents_symlink_outside_workspace_is_rejected(tmp_path):
    from nz_coder.foundation.project_control import (
        UnsafeProjectControl,
        capture_project_control_snapshot,
    )

    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text(SECRET, encoding="utf-8")
    try:
        (workspace / "AGENTS.md").symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are unavailable")

    with pytest.raises(UnsafeProjectControl) as error:
        capture_project_control_snapshot(workspace)
    assert SECRET not in str(error.value)


def test_project_rule_symlink_is_rejected(tmp_path):
    from nz_coder.foundation.project_control import (
        UnsafeProjectControl,
        capture_project_control_snapshot,
    )

    rules = tmp_path / "repo" / ".nz-coder" / "rules"
    rules.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text(SECRET, encoding="utf-8")
    try:
        (rules / "unsafe.md").symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are unavailable")

    with pytest.raises(UnsafeProjectControl) as error:
        capture_project_control_snapshot(tmp_path / "repo")
    assert SECRET not in str(error.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor race seam")
def test_instruction_parent_swap_during_capture_fails_closed(tmp_path, monkeypatch):
    from nz_coder.foundation import project_control

    workspace = tmp_path / "repo"
    rules = workspace / ".nz-coder" / "rules"
    outside = tmp_path / "outside"
    rules.mkdir(parents=True)
    outside.mkdir()
    (rules / "safe.md").write_text("SAFE", encoding="utf-8")
    (outside / "safe.md").write_text(SECRET, encoding="utf-8")
    original_open = project_control.os.open
    swapped = False

    def swap_after_control_open(path, flags, *args, **kwargs):
        nonlocal swapped
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == ".nz-coder" and not swapped:
            swapped = True
            rules.rename(rules.with_name("rules-opened"))
            rules.symlink_to(outside, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(project_control.os, "open", swap_after_control_open)
    with pytest.raises(project_control.UnsafeProjectControl) as error:
        project_control.capture_project_control_snapshot(workspace)
    assert SECRET not in str(error.value)


def test_instruction_edit_does_not_affect_inflight_run(tmp_path):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.state.instructions import load_instruction_context

    instruction = tmp_path / "AGENTS.md"
    instruction.write_text("PINNED-A", encoding="utf-8")
    snapshot = load_config_snapshot(
        tmp_path, environ={}, user_config_path=tmp_path.parent / "user" / "config.env"
    )
    snapshot = replace(
        snapshot,
        control_plane_trusted=True,
        project_control=replace(snapshot.project_control, trusted=True),
    )
    instruction.write_text("CHANGED-B", encoding="utf-8")

    bundle = load_instruction_context(tmp_path, config_snapshot=snapshot)

    assert "PINNED-A" in bundle.reminder
    assert "CHANGED-B" not in bundle.reminder


def test_instruction_edit_requires_retrust_next_run(tmp_path):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot

    instruction = tmp_path / "AGENTS.md"
    instruction.write_text("PINNED-A", encoding="utf-8")
    store = WorkspaceTrustStore(tmp_path.parent / "trust.json")
    first = load_config_snapshot(
        tmp_path, environ={}, user_config_path=tmp_path.parent / "user" / "config.env",
        trust_store=store,
    )
    store.trust(tmp_path, "workspace-control", first.control_fingerprint)
    trusted = load_config_snapshot(
        tmp_path, environ={}, user_config_path=tmp_path.parent / "user" / "config.env",
        trust_store=store,
    )
    instruction.write_text("CHANGED-B", encoding="utf-8")
    changed = load_config_snapshot(
        tmp_path, environ={}, user_config_path=tmp_path.parent / "user" / "config.env",
        trust_store=store,
    )

    assert trusted.control_plane_trusted is True
    assert changed.control_fingerprint != trusted.control_fingerprint
    assert changed.control_plane_trusted is False


def test_user_global_instruction_is_pinned_per_run(tmp_path):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.state.instructions import load_instruction_context

    workspace = tmp_path / "repo"
    config_root = tmp_path / "user-config"
    workspace.mkdir()
    config_root.mkdir()
    instruction = config_root / "AGENTS.md"
    instruction.write_text("USER-PINNED-A", encoding="utf-8")
    snapshot = load_config_snapshot(
        workspace, environ={}, user_config_path=config_root / "config.env"
    )
    instruction.write_text("USER-CHANGED-B", encoding="utf-8")

    bundle = load_instruction_context(workspace, config_snapshot=snapshot)

    assert "USER-PINNED-A" in bundle.reminder
    assert "USER-CHANGED-B" not in bundle.reminder


@pytest.mark.parametrize(
    "tool_input",
    [
        {"path": "AGENTS.md", "content": "changed"},
        {"path": "CLAUDE.md", "content": "changed"},
        {"path": ".nz-coder/rules/review.md", "content": "changed"},
        {"files": [{"path": "AGENTS.md", "content": "changed"}]},
    ],
)
def test_model_instruction_write_requires_control_plane_permission(tool_input):
    from nz_coder.tool_platform.permissioning.checker import PermissionChecker

    decision = PermissionChecker(
        "auto", workspace_trusted=True,
    ).check("write_file", tool_input, [], [], [])

    assert decision["behavior"] == "ask"
    assert "control" in decision["reason"].lower()


def test_model_cannot_refresh_instruction_trust():
    from nz_coder.tools import get_specs

    names = {
        str(item.get("function", {}).get("name") or "")
        for item in get_specs()
    }
    assert not {name for name in names if "trust" in name.casefold()}


def test_http_instruction_change_invalidates_pending_run_digest(tmp_path):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.http_service.manager import ManagedSession
    from nz_coder.state.instructions import create_instruction_file

    workspace = tmp_path / "repo"
    workspace.mkdir()
    before = load_config_snapshot(
        workspace,
        environ={},
        user_config_path=tmp_path / "user" / "config.env",
    )
    pending = ManagedSession._command_execution_digest(
        before, "review", ("read_file",), None,
    )

    create_instruction_file(workspace, "project")
    after = load_config_snapshot(
        workspace,
        environ={},
        user_config_path=tmp_path / "user" / "config.env",
    )
    current = ManagedSession._command_execution_digest(
        after, "review", ("read_file",), None,
    )

    assert after.control_fingerprint != before.control_fingerprint
    assert current != pending
