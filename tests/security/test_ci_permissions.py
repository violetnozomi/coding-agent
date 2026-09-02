"""Static contracts for least-privilege build and release workflows."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_all_build_workflows_default_to_read_only_contents() -> None:
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        workflow = path.read_text(encoding="utf-8")
        assert "permissions:\n  contents: read" in workflow, path.name


def test_windows_build_defaults_to_read_only_and_release_is_tag_scoped() -> None:
    workflow = (ROOT / ".github/workflows/windows-installer.yml").read_text(
        encoding="utf-8",
    )

    assert "permissions:\n  contents: read" in workflow
    build, release = workflow.split("\n  release:\n", 1)
    assert "contents: write" not in build
    assert "needs: windows-installer" in release
    assert "github.event_name == 'push'" in release
    assert "startsWith(github.ref, 'refs/tags/v')" in release
    assert "permissions:\n      contents: write" in release
    assert "actions/download-artifact@v4" in release
    assert "softprops/action-gh-release@v2" in release


def test_linux_ci_installs_and_probes_the_built_wheel_outside_source() -> None:
    workflow = (ROOT / ".github/workflows/core-runtime.yml").read_text(
        encoding="utf-8",
    )

    assert "installed-wheel-contract" in workflow
    assert "python -m build --wheel --sdist" in workflow
    assert "python -m venv" in workflow
    assert "dist/nz_coder-*.whl" in workflow
    assert "cd \"$RUNNER_TEMP\"" in workflow
    assert 'bin/nz-coder" --help' in workflow
    assert 'bin/nz-coder" doctor --repo-intelligence-only' in workflow
    assert 'bin/nz-coder" config show --json' in workflow
