"""Reader-facing regression checks for the tracked public release boundary."""
from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
STRUCTURE = (ROOT / "docs" / "struct.md").read_text(encoding="utf-8")
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_readme_relative_markdown_links_exist():
    """A fresh checkout must not advertise excluded local documentation."""
    links = re.findall(r"\[[^]]+\]\(([^)]+)\)", README)
    assert links
    for target in links:
        if "://" in target or target.startswith("#"):
            continue
        path_text = target.split("#", 1)[0]
        assert (ROOT / path_text).exists(), f"broken README link: {target}"


def test_readme_names_the_single_tracked_architecture_document():
    assert "docs/struct.md" in README
    assert "docs/architecture.md" not in README
    assert "docs/release-checklist.md" not in README


def test_structure_document_describes_the_canonical_runtime_boundary():
    for phrase in (
        "AgentRunner",
        "ProductionRuntimeHost",
        "ToolExecutor",
        "TransactionManager",
        "MemoryManager",
        "TraceRecorder",
    ):
        assert phrase in STRUCTURE


def test_readme_release_commands_match_the_core_workflow():
    workflow = (
        ROOT / ".github" / "workflows" / "core-runtime.yml"
    ).read_text(encoding="utf-8")
    for command in (
        "python -m compileall -q nz_coder",
        "python -m pytest -q",
        "ruff check .",
        "python -m build --wheel --sdist",
    ):
        assert command in README
        assert command in workflow


def test_release_linter_and_distribution_data_are_reproducible():
    assert 'ruff==0.15.10' in PYPROJECT
    assert '"bundled_commands/*.md"' in PYPROJECT
    assert '"bundled_skills/*/SKILL.md"' in PYPROJECT


def test_windows_installer_boundary_is_tracked_and_executable():
    workflow = (
        ROOT / ".github" / "workflows" / "windows-installer.yml"
    ).read_text(encoding="utf-8")
    for required in (
        "python -m PyInstaller",
        "ISCC.exe",
        "/VERYSILENT",
        "unins000.exe",
        "SHA256SUMS.txt",
    ):
        assert required in workflow
