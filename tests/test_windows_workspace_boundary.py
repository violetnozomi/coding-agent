"""Resolved workspace security contracts shared by Windows file operations."""
from __future__ import annotations

from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.tools.files import read_file, write_file


def test_file_tools_reject_existing_symlink_escape(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = workspace / "external"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        import pytest

        pytest.skip(f"host cannot create directory symlinks: {exc}")

    with scoped_workdir(workspace):
        assert str(read_file("external/secret.txt")).startswith("Error: Path escapes workspace")


def test_file_tools_reject_new_file_below_symlinked_parent(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    link = workspace / "external"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        import pytest

        pytest.skip(f"host cannot create directory symlinks: {exc}")

    with scoped_workdir(workspace):
        assert str(write_file("external/new.txt", "blocked")).startswith(
            "Error: Path escapes workspace"
        )
    assert not (outside / "new.txt").exists()
