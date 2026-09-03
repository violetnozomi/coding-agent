"""Discover repository-owned control files shared by trust and runtimes."""
from __future__ import annotations

from pathlib import Path
import stat


CONTROL_FILE_KINDS = ("fixed", "skill", "command", "workflow")
_FIXED_CONTROL_PATHS = (
    Path(".nz-coder/settings.json"),
    Path(".nz-coder/mcp.json"),
)
_CONTROL_DIRECTORIES = {
    "skill": Path(".nz-coder/skills"),
    "command": Path(".nz-coder/commands"),
    "workflow": Path(".nz-coder/workflows"),
}


class UnsafeProjectControl(ValueError):
    """Raised when a repository control path is not a regular local file."""


def discover_project_control_files(
    workspace: Path | str,
    *,
    kinds: tuple[str, ...] = CONTROL_FILE_KINDS,
) -> tuple[Path, ...]:
    """Return exactly the regular files that can influence product authority."""
    selected = set(kinds)
    unknown = selected - set(CONTROL_FILE_KINDS)
    if unknown:
        raise ValueError(f"unknown project control kind: {sorted(unknown)[0]}")
    root = Path(workspace).expanduser().resolve(strict=True)
    found: list[Path] = []
    if "fixed" in selected:
        for relative in _FIXED_CONTROL_PATHS:
            path = root / relative
            if path.exists() or path.is_symlink():
                _require_regular(path)
                found.append(path)
    if "skill" in selected:
        found.extend(_discover_skills(root / _CONTROL_DIRECTORIES["skill"]))
    if "command" in selected:
        found.extend(_discover_flat(
            root / _CONTROL_DIRECTORIES["command"], suffix=".md"
        ))
    if "workflow" in selected:
        found.extend(_discover_flat(
            root / _CONTROL_DIRECTORIES["workflow"], suffix=".workflow.json"
        ))
    return tuple(sorted(set(found), key=lambda path: path.relative_to(root).as_posix()))


def has_project_control_files(workspace: Path | str) -> bool:
    """Return whether the workspace contains an active repository control file."""
    return bool(discover_project_control_files(workspace))


def _discover_skills(directory: Path) -> list[Path]:
    if not _safe_directory(directory):
        return []
    found: list[Path] = []
    try:
        entries = tuple(directory.iterdir())
    except OSError as exc:
        raise UnsafeProjectControl("project control directory cannot be read") from exc
    for entry in entries:
        info = _lstat(entry)
        if stat.S_ISLNK(info.st_mode):
            continue
        if not stat.S_ISDIR(info.st_mode):
            continue
        path = entry / "SKILL.md"
        if path.exists() or path.is_symlink():
            _require_regular(path)
            found.append(path)
    return found


def _discover_flat(directory: Path, *, suffix: str) -> list[Path]:
    if not _safe_directory(directory):
        return []
    found: list[Path] = []
    try:
        entries = tuple(directory.iterdir())
    except OSError as exc:
        raise UnsafeProjectControl("project control directory cannot be read") from exc
    for path in entries:
        info = _lstat(path)
        if stat.S_ISLNK(info.st_mode):
            raise UnsafeProjectControl("project control path must not be a symlink")
        if path.name.endswith(suffix):
            if not stat.S_ISREG(info.st_mode):
                raise UnsafeProjectControl("project control path must be a regular file")
            found.append(path)
    return found


def _safe_directory(directory: Path) -> bool:
    if not directory.exists() and not directory.is_symlink():
        return False
    info = _lstat(directory)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UnsafeProjectControl("project control directory is unsafe")
    return True


def _require_regular(path: Path) -> None:
    info = _lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise UnsafeProjectControl("project control file must be a regular file")


def _lstat(path: Path):  # noqa: ANN202
    try:
        return path.lstat()
    except OSError as exc:
        raise UnsafeProjectControl("project control path cannot be inspected") from exc


__all__ = [
    "CONTROL_FILE_KINDS",
    "UnsafeProjectControl",
    "discover_project_control_files",
    "has_project_control_files",
]
