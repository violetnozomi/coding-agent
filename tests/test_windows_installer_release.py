"""Contracts for the self-contained Windows EXE distribution."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.windows_installer_contract import InstallerContract, load_contract


ROOT = Path(__file__).resolve().parents[1]


def test_installer_contract_uses_project_version():
    contract = load_contract(ROOT)

    assert contract.version == "0.1.0"
    assert contract.architecture == "x64"
    assert contract.artifact_name == "NZ-Coder-0.1.0-windows-x64-setup.exe"


def test_installer_contract_rejects_unsupported_architecture():
    with pytest.raises(ValueError, match="Unsupported Windows architecture"):
        InstallerContract("0.1.0", "arm64")


def test_frozen_tree_reports_required_runtime_assets(tmp_path):
    missing = InstallerContract("0.1.0").validate_frozen_tree(tmp_path)

    assert "nz-coder.exe" in missing
    assert "bundled_commands" in " ".join(missing)
    assert "bundled_skills" in " ".join(missing)
    assert "winpty" in " ".join(missing).lower()
    assert "tree_sitter" in " ".join(missing).lower()


def test_frozen_tree_accepts_complete_distribution(tmp_path):
    internal = tmp_path / "_internal"
    (internal / "nz_coder" / "bundled_commands").mkdir(parents=True)
    (internal / "nz_coder" / "bundled_skills").mkdir(parents=True)
    (tmp_path / "nz-coder.exe").write_bytes(b"exe")
    (internal / "winpty.dll").write_bytes(b"dll")
    (internal / "tree_sitter.pyd").write_bytes(b"pyd")

    assert InstallerContract("0.1.0").validate_frozen_tree(tmp_path) == ()


def test_pyinstaller_entrypoint_uses_existing_cli_runtime():
    entrypoint = ROOT / "packaging" / "windows" / "nz_coder_entry.py"

    text = entrypoint.read_text(encoding="utf-8")
    assert "from nz_coder.cli import main" in text
    assert "SystemExit(main())" in text


def test_pyinstaller_spec_builds_one_directory_with_package_resources():
    spec = ROOT / "packaging" / "windows" / "nz-coder.spec"

    text = spec.read_text(encoding="utf-8")
    assert 'collect_all("nz_coder")' in text
    assert 'copy_metadata("nz-coder")' in text
    assert 'name="nz-coder"' in text
    assert "console=True" in text
    assert "COLLECT(" in text
    assert 'target_arch="x86_64"' in text
