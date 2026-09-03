"""Security contracts for immutable, handle-anchored project control."""
from __future__ import annotations

import hashlib
from pathlib import Path

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
