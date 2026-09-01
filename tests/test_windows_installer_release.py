"""Contracts for the self-contained Windows EXE distribution."""
from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys

import pytest

from nz_coder.evaluation.release_contracts import (
    InstallerContract,
    load_installer_contract,
)


ROOT = Path(__file__).resolve().parents[1]


def test_installer_contract_uses_project_version():
    contract = load_installer_contract(ROOT)

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
    (internal / "tree_sitter").mkdir()
    (internal / "tree_sitter" / "_binding.pyd").write_bytes(b"pyd")

    assert InstallerContract("0.1.0").validate_frozen_tree(tmp_path) == ()


def test_contract_module_cli_rejects_an_incomplete_frozen_tree(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "nz_coder.evaluation.release_contracts",
            "--root", str(ROOT),
            "--validate-frozen", str(tmp_path),
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert json.loads(result.stdout)["artifact_name"].endswith("-setup.exe")
    assert "nz-coder.exe" in json.loads(result.stdout)["missing"]


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


def test_inno_setup_is_per_user_upgrade_safe_and_owns_path():
    text = (ROOT / "packaging" / "windows" / "nz-coder.iss").read_text(
        encoding="utf-8",
    )

    assert "PrivilegesRequired=lowest" in text
    assert "ArchitecturesAllowed=x64compatible" in text
    assert "AppId={{" in text
    assert 'DefaultDirName={localappdata}\\Programs\\NZ-Coder' in text
    assert 'Flags: recursesubdirs createallsubdirs' in text
    assert 'Name: "userpath"' in text
    assert "ChangesEnvironment=yes" in text
    assert "UninstallDisplayIcon=" in text
    assert "[UninstallDelete]" not in text


def test_windows_installer_workflow_builds_tests_and_publishes_same_artifact():
    text = (ROOT / ".github" / "workflows" / "windows-installer.yml").read_text(
        encoding="utf-8",
    )

    for required in (
        "windows-latest",
        'python-version: "3.12"',
        "pyinstaller==6.16.0",
        "nz_coder.evaluation.release_contracts",
        "PyInstaller",
        "ISCC.exe",
        "/VERYSILENT",
        "unins000.exe",
        "actions/upload-artifact@v4",
        "windows-installer-evidence",
        "refs/tags/v",
        "softprops/action-gh-release@v2",
        "SHA256SUMS.txt",
    ):
        assert required in text
