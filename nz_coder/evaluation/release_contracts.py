"""Tracked release and Windows frozen-distribution validation contracts."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Callable


_VERSION = re.compile(
    r"(?ms)^\[project\]\s*.*?^version\s*=\s*[\"']([^\"']+)[\"']"
)
_SAFE_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){2}(?:[A-Za-z0-9._-]*)?$")
_CREDENTIAL_NAMES = (
    "API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "DEEPSEEK_API_KEY",
    "NZ_IMAGE_DESCRIBE_API_KEY",
)


@dataclass(frozen=True)
class InstallerContract:
    """Immutable names and required files for one Windows installer build."""

    version: str
    architecture: str = "x64"

    def __post_init__(self) -> None:
        if not _SAFE_VERSION.fullmatch(self.version):
            raise ValueError(f"Invalid project version: {self.version!r}")
        if self.architecture != "x64":
            raise ValueError(
                f"Unsupported Windows architecture: {self.architecture}; expected x64"
            )

    @property
    def artifact_name(self) -> str:
        return f"NZ-Coder-{self.version}-windows-x64-setup.exe"

    def validate_frozen_tree(self, path: Path) -> tuple[str, ...]:
        """Return required frozen-runtime assets missing below *path*."""
        root = Path(path)
        internal = root / "_internal"
        missing: list[str] = []
        required = (
            (root / "nz-coder.exe", "nz-coder.exe"),
            (
                internal / "nz_coder" / "bundled_commands",
                "_internal/nz_coder/bundled_commands",
            ),
            (
                internal / "nz_coder" / "bundled_skills",
                "_internal/nz_coder/bundled_skills",
            ),
        )
        missing.extend(
            label for candidate, label in required if not candidate.exists()
        )
        assets = tuple(
            item.relative_to(internal).as_posix().lower()
            for item in internal.rglob("*")
            if item.is_file()
        )
        if not any("winpty" in asset for asset in assets):
            missing.append("_internal/**/winpty native runtime")
        if not any("tree_sitter" in asset for asset in assets):
            missing.append("_internal/**/tree_sitter native runtime")
        return tuple(missing)


def load_installer_contract(
    root: Path,
    architecture: str = "x64",
) -> InstallerContract:
    """Load installer identity from tracked project metadata."""
    project = Path(root) / "pyproject.toml"
    match = _VERSION.search(project.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError(f"Could not read [project].version from {project}")
    return InstallerContract(match.group(1), architecture)


def installed_environment() -> dict[str, str]:
    """Return an installed-product environment without developer credentials."""
    environment = os.environ.copy()
    for name in (*_CREDENTIAL_NAMES, "PYTHONPATH", "PYTHONHOME"):
        environment.pop(name, None)
    return environment


def measure_cli_startup(
    python: Path,
    workspace: Path,
    environment: dict[str, str],
    *,
    run: Callable[..., object] | None = None,
    clock: Callable[[], float] | None = None,
) -> float:
    """Measure a source-external installed CLI version command."""
    runner = run or subprocess.run
    timer = clock or time.perf_counter
    started = timer()
    runner(
        [str(python), "-m", "nz_coder", "--version"],
        cwd=workspace,
        env=environment,
    )
    return round((timer() - started) * 1000, 3)


def main(argv: list[str] | None = None) -> int:
    """Print installer identity and optionally validate a frozen tree."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--validate-frozen", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    contract = load_installer_contract(args.root)
    missing = (
        contract.validate_frozen_tree(args.validate_frozen)
        if args.validate_frozen is not None
        else ()
    )
    payload = {
        "version": contract.version,
        "architecture": contract.architecture,
        "artifact_name": contract.artifact_name,
        "missing": list(missing),
    }
    if args.json:
        print(json.dumps(payload, separators=(",", ":")))
    elif missing:
        print("Missing frozen assets: " + ", ".join(missing))
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
