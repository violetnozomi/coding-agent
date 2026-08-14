"""Version and frozen-tree contracts for the Windows EXE distribution."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


_VERSION = re.compile(
    r"(?ms)^\[project\]\s*.*?^version\s*=\s*[\"']([^\"']+)[\"']"
)
_SAFE_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){2}(?:[A-Za-z0-9._-]*)?$")


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
    def app_version(self) -> str:
        return self.version

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
        missing.extend(label for candidate, label in required if not candidate.exists())
        names = tuple(item.name.lower() for item in internal.rglob("*") if item.is_file())
        if not any("winpty" in name for name in names):
            missing.append("_internal/**/winpty native runtime")
        if not any("tree_sitter" in name for name in names):
            missing.append("_internal/**/tree_sitter native runtime")
        return tuple(missing)


def load_contract(root: Path, architecture: str = "x64") -> InstallerContract:
    """Load the installer identity from project metadata without extra packages."""
    project = Path(root) / "pyproject.toml"
    text = project.read_text(encoding="utf-8")
    match = _VERSION.search(text)
    if match is None:
        raise ValueError(f"Could not read [project].version from {project}")
    return InstallerContract(match.group(1), architecture)
