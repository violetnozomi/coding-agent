"""Security contracts for immutable, handle-anchored project control."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess

import pytest


def _write_control_tree(workspace: Path) -> dict[str, bytes]:
    files = {
        ".nz-coder/settings.json": b'{"permissions":{"deny":["bash"]}}',
        ".nz-coder/mcp.json": b'{"servers":{}}',
        ".nz-coder/skills/review/SKILL.md": (
            b"---\nname: review\ndescription: Review code\n---\n\nORIGINAL-SKILL"
        ),
        ".nz-coder/commands/review.md": b"Review $ARGUMENTS",
        ".nz-coder/workflows/review.workflow.json": (
            b'{"manifest":{"name":"review","description":"Review"},'
            b'"plan":{"steps":[]}}'
        ),
    }
    for relative, payload in files.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return files


def _expected_fingerprint(files: dict[str, bytes]) -> str:
    from nz_coder.foundation.workspace_trust import workspace_config_fingerprint

    digest = hashlib.sha256()
    digest.update(b"workspace-config\0")
    digest.update(workspace_config_fingerprint({}).encode("ascii"))
    for relative, payload in sorted(files.items()):
        digest.update(b"\0path\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0content\0")
        digest.update(payload)
    return digest.hexdigest()


def test_control_snapshot_content_matches_fingerprint(tmp_path):
    from nz_coder.foundation.project_control import (
        capture_project_control_snapshot,
    )

    workspace = tmp_path / "repo"
    workspace.mkdir()
    files = _write_control_tree(workspace)

    snapshot = capture_project_control_snapshot(workspace)

    assert snapshot.fingerprint == _expected_fingerprint(files)
    assert snapshot.total_bytes == sum(map(len, files.values()))
    assert set(snapshot.files) == set(files)
    for relative, payload in files.items():
        control_file = snapshot.get(relative)
        assert control_file is not None
        assert control_file.content == payload
        assert control_file.sha256 == hashlib.sha256(payload).hexdigest()
        assert control_file.size == len(payload)


def test_control_plane_rejects_symlinked_nz_coder_ancestor(tmp_path):
    from nz_coder.foundation.project_control import (
        UnsafeProjectControl,
        capture_project_control_snapshot,
    )

    workspace = tmp_path / "repo"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "settings.json").write_text(
        '{"permissions":{"allow":["bash"]},"sentinel":"OUTSIDE-SECRET"}',
        encoding="utf-8",
    )
    try:
        (workspace / ".nz-coder").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(UnsafeProjectControl, match="unsafe"):
        capture_project_control_snapshot(workspace)


@pytest.mark.parametrize("directory", ["commands", "workflows", "skills"])
def test_control_plane_rejects_symlinked_control_parent(tmp_path, directory):
    from nz_coder.foundation.project_control import (
        UnsafeProjectControl,
        capture_project_control_snapshot,
    )

    workspace = tmp_path / "repo"
    outside = tmp_path / "outside"
    control = workspace / ".nz-coder"
    control.mkdir(parents=True)
    outside.mkdir()
    if directory == "commands":
        (outside / "escape.md").write_text("OUTSIDE-SECRET", encoding="utf-8")
    elif directory == "workflows":
        (outside / "escape.workflow.json").write_text(
            '{"sentinel":"OUTSIDE-SECRET"}', encoding="utf-8"
        )
    else:
        skill = outside / "escape"
        skill.mkdir()
        (skill / "SKILL.md").write_text("OUTSIDE-SECRET", encoding="utf-8")
    try:
        (control / directory).symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(UnsafeProjectControl, match="unsafe"):
        capture_project_control_snapshot(workspace)


def test_control_snapshot_is_immutable_and_keeps_opened_bytes(tmp_path):
    from dataclasses import FrozenInstanceError

    from nz_coder.foundation.project_control import (
        capture_project_control_snapshot,
    )

    workspace = tmp_path / "repo"
    workspace.mkdir()
    command = workspace / ".nz-coder" / "commands" / "review.md"
    command.parent.mkdir(parents=True)
    command.write_text("ORIGINAL", encoding="utf-8")
    snapshot = capture_project_control_snapshot(workspace)

    command.write_text("REPLACEMENT", encoding="utf-8")

    assert snapshot.get(".nz-coder/commands/review.md").content == b"ORIGINAL"
    with pytest.raises(TypeError):
        snapshot.files["replacement"] = snapshot.get(
            ".nz-coder/commands/review.md"
        )
    with pytest.raises(FrozenInstanceError):
        snapshot.trusted = True


def test_config_snapshot_binds_trust_to_captured_control_bytes(tmp_path):
    from nz_coder.foundation.workspace_trust import (
        WorkspaceTrustStore,
        load_config_snapshot,
    )

    workspace = tmp_path / "repo"
    workspace.mkdir()
    files = _write_control_tree(workspace)
    store = WorkspaceTrustStore(tmp_path / "trust.json")
    initial = load_config_snapshot(
        workspace,
        environ={},
        user_config_path=tmp_path / "user.env",
        trust_store=store,
    )
    store.trust(workspace, "workspace-control", initial.control_fingerprint)

    trusted = load_config_snapshot(
        workspace,
        environ={},
        user_config_path=tmp_path / "user.env",
        trust_store=store,
    )

    assert trusted.project_control.fingerprint == _expected_fingerprint(files)
    assert trusted.project_control.fingerprint == trusted.control_fingerprint
    assert trusted.project_control.trusted is True
    assert trusted.control_plane_trusted is True


def test_unsafe_control_capture_exposes_no_project_authority(tmp_path):
    from nz_coder.foundation.workspace_trust import (
        WorkspaceTrustStore,
        load_config_snapshot,
    )

    workspace = tmp_path / "repo"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "settings.json").write_text("OUTSIDE-SECRET", encoding="utf-8")
    try:
        (workspace / ".nz-coder").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    snapshot = load_config_snapshot(
        workspace,
        environ={},
        user_config_path=tmp_path / "user.env",
        trust_store=WorkspaceTrustStore(tmp_path / "trust.json"),
    )

    assert snapshot.control_plane_trusted is False
    assert snapshot.project_control.trusted is False
    assert snapshot.project_control.fingerprint == ""
    assert not snapshot.project_control.files
    assert "OUTSIDE-SECRET" not in snapshot.public_json()


def test_permission_rules_are_parsed_from_snapshot_bytes(tmp_path):
    from dataclasses import replace

    from nz_coder.foundation.project_control import capture_project_control_snapshot
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tool_platform.permissioning.manager import PermissionManager

    settings = tmp_path / ".nz-coder" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(
        '{"permissions":{"allow":["bash"],"deny":[],"ask":[]}}',
        encoding="utf-8",
    )
    snapshot = replace(capture_project_control_snapshot(tmp_path), trusted=True)
    settings.write_text(
        '{"permissions":{"allow":[],"deny":["bash"],"ask":[]}}',
        encoding="utf-8",
    )

    with scoped_workdir(tmp_path):
        manager = PermissionManager(
            "auto",
            workspace_trusted=True,
            project_control_snapshot=snapshot,
        )

    assert manager.check("bash", {"command": "git status"})["behavior"] == "allow"


def test_stale_trust_boolean_cannot_authorize_current_project_settings(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tool_platform.permissioning.manager import PermissionManager

    settings = tmp_path / ".nz-coder" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(
        '{"permissions":{"allow":["bash"]}}', encoding="utf-8"
    )

    with scoped_workdir(tmp_path):
        manager = PermissionManager("auto", workspace_trusted=True)

    assert manager.check("bash", {"command": "git status"})["behavior"] == "ask"


def test_project_skill_body_is_loaded_from_snapshot_bytes(tmp_path):
    from dataclasses import replace

    from nz_coder.foundation.project_control import capture_project_control_snapshot
    from nz_coder.state.skills import SkillLoader

    skill_file = tmp_path / ".nz-coder" / "skills" / "review" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(
        "---\nname: review\ndescription: Review\n---\n\nORIGINAL-SKILL",
        encoding="utf-8",
    )
    snapshot = replace(capture_project_control_snapshot(tmp_path), trusted=True)
    skill_file.write_text(
        "---\nname: review\ndescription: Replaced\n---\n\nREPLACEMENT-SKILL",
        encoding="utf-8",
    )

    loader = SkillLoader(
        bundled_dir=tmp_path / "bundled",
        user_dir=tmp_path / "user",
        project_dir=tmp_path / ".nz-coder" / "skills",
        project_control_snapshot=snapshot,
    )

    assert "ORIGINAL-SKILL" in loader.load("review")
    assert "REPLACEMENT-SKILL" not in loader.load("review")
    assert loader.get_skill_info("review").description == "Review"


def test_skill_reload_cannot_reuse_stale_trust_boolean(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_trust import (
        WorkspaceTrustStore,
        load_config_snapshot,
    )
    from nz_coder.state.skills import SkillLoader

    workspace = tmp_path / "repo"
    skill_file = workspace / ".nz-coder" / "skills" / "review" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(
        "---\nname: review\ndescription: Review\n---\n\nORIGINAL-SKILL",
        encoding="utf-8",
    )
    trust_path = tmp_path / "trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))
    initial = load_config_snapshot(workspace)
    WorkspaceTrustStore(trust_path).trust(
        workspace, "workspace-control", initial.control_fingerprint
    )
    trusted = load_config_snapshot(workspace)
    loader = SkillLoader(
        bundled_dir=tmp_path / "bundled",
        user_dir=tmp_path / "user",
        project_dir=skill_file.parent.parent,
        workspace_trusted=True,
        project_control_snapshot=trusted.project_control,
    )
    assert loader.get_skill_info("review") is not None

    skill_file.write_text(
        "---\nname: review\ndescription: Changed\n---\n\nCHANGED-SKILL",
        encoding="utf-8",
    )
    loader.reload()

    assert loader.get_skill_info("review") is None


def test_command_aba_replacement_cannot_use_old_trust(tmp_path):
    from dataclasses import replace

    from nz_coder.foundation.project_control import capture_project_control_snapshot
    from nz_coder.interface.custom_commands import CommandCatalog

    command = tmp_path / ".nz-coder" / "commands" / "review.md"
    command.parent.mkdir(parents=True)
    command.write_text("TRUSTED-A", encoding="utf-8")
    snapshot = replace(capture_project_control_snapshot(tmp_path), trusted=True)
    command.write_text("UNTRUSTED-B", encoding="utf-8")

    catalog = CommandCatalog.discover(
        project_dir=command.parent,
        project_trusted=True,
        project_control_snapshot=snapshot,
    )

    assert catalog.expand("review").prompt == "TRUSTED-A"


def test_command_stale_trust_boolean_does_not_read_project_path(tmp_path):
    from nz_coder.interface.custom_commands import CommandCatalog

    command = tmp_path / ".nz-coder" / "commands" / "review.md"
    command.parent.mkdir(parents=True)
    command.write_text("UNTRUSTED", encoding="utf-8")

    catalog = CommandCatalog.discover(
        project_dir=command.parent,
        project_trusted=True,
    )

    assert catalog.get("review") is None


def _workflow_capsule(description: str) -> dict:
    from nz_coder.runtime.workflows.workflow_capsule import create_workflow_capsule

    return create_workflow_capsule(
        manifest={
            "name": "review",
            "description": description,
            "phases": ["final"],
            "read_only": True,
            "planned_agents": 1,
            "max_agents": 1,
            "max_concurrency": 1,
            "patterns": ["fan-out-and-synthesize"],
        },
        plan={
            "phases": [{
                "name": "final",
                "mode": "synthesize",
                "from_phases": [],
                "rubric": "Return evidence.",
                "artifact": "review",
            }],
        },
    )


def test_workflow_aba_replacement_cannot_use_old_trust(tmp_path):
    from dataclasses import replace
    import json

    from nz_coder.foundation.project_control import capture_project_control_snapshot
    from nz_coder.runtime.workflows.workflow_library import load_workflow_capsule

    workflow = tmp_path / ".nz-coder" / "workflows" / "review.workflow.json"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(json.dumps(_workflow_capsule("TRUSTED-A")), encoding="utf-8")
    snapshot = replace(capture_project_control_snapshot(tmp_path), trusted=True)
    workflow.write_text(json.dumps(_workflow_capsule("UNTRUSTED-B")), encoding="utf-8")

    capsule, reference = load_workflow_capsule(
        "review",
        workspace=tmp_path,
        source="project",
        project_control_snapshot=snapshot,
    )

    assert capsule["manifest"]["description"] == "TRUSTED-A"
    assert reference["source"] == "project"


def test_mcp_project_config_uses_snapshot_bytes(tmp_path, monkeypatch):
    from dataclasses import replace

    from nz_coder.foundation import config
    from nz_coder.foundation.project_control import capture_project_control_snapshot
    from nz_coder.mcp.config import load_mcp_server_configs

    project = tmp_path / ".nz-coder" / "mcp.json"
    project.parent.mkdir()
    project.write_text(
        '{"servers":{"demo":{"command":["trusted-a"]}}}', encoding="utf-8"
    )
    snapshot = replace(capture_project_control_snapshot(tmp_path), trusted=True)
    project.write_text(
        '{"servers":{"demo":{"command":["untrusted-b"]}}}', encoding="utf-8"
    )
    monkeypatch.setattr(config, "MCP_USER_CONFIG", str(tmp_path / "user.json"))
    monkeypatch.setattr(config, "MCP_TRUST_STORE", str(tmp_path / "mcp-trust.json"))
    monkeypatch.setattr(config, "MCP_SERVERS_JSON", "")

    server = load_mcp_server_configs(
        workspace=tmp_path,
        project_control_snapshot=snapshot,
    )[0]

    assert server.command == ("trusted-a",)
    assert server.source == "project"


def test_project_hooks_are_parsed_from_snapshot_bytes(tmp_path):
    from dataclasses import replace

    from nz_coder.foundation.project_control import capture_project_control_snapshot
    from nz_coder.runtime.verification.hooks import build_default_hooks

    settings = tmp_path / ".nz-coder" / "settings.json"
    settings.parent.mkdir()
    settings.write_text(
        '{"hooks":[{"id":"trusted-a","event":"pre_tool_use",'
        '"action":{"type":"prompt","message":"A"}}]}',
        encoding="utf-8",
    )
    snapshot = replace(capture_project_control_snapshot(tmp_path), trusted=True)
    settings.write_text(
        '{"hooks":[{"id":"untrusted-b","event":"pre_tool_use",'
        '"action":{"type":"prompt","message":"B"}}]}',
        encoding="utf-8",
    )
    hooks = build_default_hooks(snapshot)
    assert [item.id for item in hooks.configured_hooks] == ["trusted-a"]


def test_control_plane_rejects_symlinked_commands_parent(tmp_path):
    _assert_control_parent_symlink_rejected(tmp_path, "commands", "entry.md")


def test_control_plane_rejects_symlinked_workflows_parent(tmp_path):
    _assert_control_parent_symlink_rejected(
        tmp_path, "workflows", "entry.workflow.json"
    )


def test_control_plane_rejects_symlinked_skills_parent(tmp_path):
    from nz_coder.foundation.project_control import (
        UnsafeProjectControl,
        capture_project_control_snapshot,
    )

    workspace = tmp_path / "repo"
    outside = tmp_path / "outside"
    (workspace / ".nz-coder").mkdir(parents=True)
    (outside / "entry").mkdir(parents=True)
    (outside / "entry" / "SKILL.md").write_text(
        "OUTSIDE-SECRET", encoding="utf-8"
    )
    (workspace / ".nz-coder" / "skills").symlink_to(
        outside, target_is_directory=True
    )
    with pytest.raises(UnsafeProjectControl, match="unsafe") as error:
        capture_project_control_snapshot(workspace)
    assert "OUTSIDE-SECRET" not in str(error.value)
    assert str(outside) not in str(error.value)


def test_control_plane_rejects_symlinked_mcp_parent(tmp_path):
    """The .nz-coder directory is the direct parent of mcp.json."""
    from nz_coder.foundation.project_control import (
        UnsafeProjectControl,
        capture_project_control_snapshot,
    )

    workspace = tmp_path / "repo"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "mcp.json").write_text("OUTSIDE-SECRET", encoding="utf-8")
    (workspace / ".nz-coder").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafeProjectControl, match="unsafe") as error:
        capture_project_control_snapshot(workspace)
    assert "OUTSIDE-SECRET" not in str(error.value)
    assert str(outside) not in str(error.value)


def _assert_control_parent_symlink_rejected(
    tmp_path: Path, directory: str, filename: str
) -> None:
    from nz_coder.foundation.project_control import (
        UnsafeProjectControl,
        capture_project_control_snapshot,
    )

    workspace = tmp_path / "repo"
    outside = tmp_path / "outside"
    (workspace / ".nz-coder").mkdir(parents=True)
    outside.mkdir()
    (outside / filename).write_text("OUTSIDE-SECRET", encoding="utf-8")
    (workspace / ".nz-coder" / directory).symlink_to(
        outside, target_is_directory=True
    )
    with pytest.raises(UnsafeProjectControl, match="unsafe") as error:
        capture_project_control_snapshot(workspace)
    assert "OUTSIDE-SECRET" not in str(error.value)
    assert str(outside) not in str(error.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor race seam")
def test_control_file_parent_swap_during_capture_fails_closed(tmp_path, monkeypatch):
    from nz_coder.foundation import project_control

    workspace = tmp_path / "repo"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "settings.json").write_text("OUTSIDE-SECRET", encoding="utf-8")
    control = workspace / ".nz-coder"
    control.mkdir()
    original_open = project_control.os.open
    swapped = False

    def swap_after_root_open(path, flags, *args, **kwargs):
        nonlocal swapped
        descriptor = original_open(path, flags, *args, **kwargs)
        if Path(path) == workspace and not swapped:
            swapped = True
            control.rename(workspace / ".nz-coder-opened")
            control.symlink_to(outside, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(project_control.os, "open", swap_after_root_open)
    with pytest.raises(project_control.UnsafeProjectControl, match="unsafe") as error:
        project_control.capture_project_control_snapshot(workspace)
    assert "OUTSIDE-SECRET" not in str(error.value)
    assert str(outside) not in str(error.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor race seam")
def test_control_file_replacement_after_open_does_not_change_snapshot(
    tmp_path, monkeypatch,
):
    from nz_coder.foundation import project_control

    workspace = tmp_path / "repo"
    command = workspace / ".nz-coder" / "commands" / "review.md"
    command.parent.mkdir(parents=True)
    command.write_text("OPENED-CONTENT", encoding="utf-8")
    original_read = project_control.os.read
    replaced = False

    def replace_path_after_open(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        if not replaced:
            replaced = True
            command.rename(command.with_suffix(".opened"))
            command.write_text("REPLACEMENT", encoding="utf-8")
        return original_read(descriptor, size)

    monkeypatch.setattr(project_control.os, "read", replace_path_after_open)
    snapshot = project_control.capture_project_control_snapshot(workspace)
    assert snapshot.get(".nz-coder/commands/review.md").content == b"OPENED-CONTENT"


def test_control_discovery_stops_at_file_limit_without_full_materialization(
    tmp_path, monkeypatch,
):
    from nz_coder.foundation import project_control

    commands = tmp_path / ".nz-coder" / "commands"
    commands.mkdir(parents=True)
    for index in range(3):
        (commands / f"{index}.md").write_text(str(index), encoding="utf-8")
    monkeypatch.setattr(project_control, "MAX_CONTROL_FILES", 2)
    with pytest.raises(project_control.UnsafeProjectControl, match="file limit"):
        project_control.capture_project_control_snapshot(tmp_path)


def test_control_discovery_stops_at_byte_limit(tmp_path, monkeypatch):
    from nz_coder.foundation import project_control

    settings = tmp_path / ".nz-coder" / "settings.json"
    settings.parent.mkdir()
    settings.write_bytes(b"12345")
    monkeypatch.setattr(project_control, "MAX_CONTROL_TOTAL_BYTES", 4)
    with pytest.raises(project_control.UnsafeProjectControl, match="byte limit"):
        project_control.capture_project_control_snapshot(tmp_path)


def test_current_run_keeps_pinned_control_snapshot(tmp_path):
    from nz_coder.foundation.workspace_trust import (
        current_config_snapshot,
        load_config_snapshot,
        scoped_config_snapshot,
    )

    command = tmp_path / ".nz-coder" / "commands" / "review.md"
    command.parent.mkdir(parents=True)
    command.write_text("A", encoding="utf-8")
    current = load_config_snapshot(tmp_path, environ={}, user_config_path=tmp_path / "user")
    command.write_text("B", encoding="utf-8")
    with scoped_config_snapshot(current):
        pinned = current_config_snapshot(tmp_path)
    assert pinned.project_control.get(".nz-coder/commands/review.md").content == b"A"


def test_next_run_rejects_changed_untrusted_control_plane(tmp_path):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot

    command = tmp_path / ".nz-coder" / "commands" / "review.md"
    command.parent.mkdir(parents=True)
    command.write_text("A", encoding="utf-8")
    store = WorkspaceTrustStore(tmp_path.parent / f"{tmp_path.name}-trust.json")
    first = load_config_snapshot(
        tmp_path, environ={}, user_config_path=tmp_path.parent / "missing", trust_store=store
    )
    store.trust(tmp_path, "workspace-control", first.control_fingerprint)
    trusted = load_config_snapshot(
        tmp_path, environ={}, user_config_path=tmp_path.parent / "missing", trust_store=store
    )
    assert trusted.control_plane_trusted is True
    command.write_text("B", encoding="utf-8")
    next_run = load_config_snapshot(
        tmp_path, environ={}, user_config_path=tmp_path.parent / "missing", trust_store=store
    )
    assert next_run.control_plane_trusted is False
    assert next_run.project_control.trusted is False


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_windows_control_plane_rejects_junction_ancestor(tmp_path):
    from nz_coder.foundation.project_control import (
        UnsafeProjectControl,
        capture_project_control_snapshot,
    )

    workspace = tmp_path / "repo"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "settings.json").write_text("OUTSIDE-SECRET", encoding="utf-8")
    junction = workspace / ".nz-coder"
    process = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr
    with pytest.raises(UnsafeProjectControl, match="unsafe"):
        capture_project_control_snapshot(workspace)


@pytest.mark.skipif(os.name != "nt", reason="Windows handle semantics")
def test_windows_control_parent_swap_during_capture_is_blocked_or_fails_closed(
    tmp_path, monkeypatch,
):
    from nz_coder.foundation import project_control

    workspace = tmp_path / "repo"
    command = workspace / ".nz-coder" / "commands" / "review.md"
    command.parent.mkdir(parents=True)
    command.write_text("OPENED-CONTENT", encoding="utf-8")
    original_open = project_control._windows_open
    attempted = False

    def attempt_swap(path: Path, **kwargs):
        nonlocal attempted
        handle = original_open(path, **kwargs)
        if Path(path) == command.parent and handle is not None and not attempted:
            attempted = True
            with pytest.raises(OSError):
                command.parent.rename(command.parent.with_name("commands-moved"))
        return handle

    monkeypatch.setattr(project_control, "_windows_open", attempt_swap)
    snapshot = project_control.capture_project_control_snapshot(workspace)
    assert attempted is True
    assert snapshot.get(".nz-coder/commands/review.md").content == b"OPENED-CONTENT"
