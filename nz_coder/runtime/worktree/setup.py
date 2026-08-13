"""Post-creation setup for git worktrees."""
from __future__ import annotations

import fnmatch
import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

LOCAL_CONFIG_FILES = [
    ".env",
    "settings.local.json",
]


def perform_post_creation_setup(
    repo_root: str | Path,
    wt_path: str | Path,
    symlink_directories: list[str] | None = None,
) -> None:
    root = Path(repo_root)
    wt = Path(wt_path)
    _copy_local_configs(root, wt)
    _setup_git_hooks(root, wt)
    _create_symlinks(root, wt, symlink_directories or [])
    _copy_ignored_files(root, wt)


def _copy_local_configs(root: Path, wt: Path) -> None:
    for name in LOCAL_CONFIG_FILES:
        src = root / name
        if not src.exists():
            continue
        try:
            shutil.copy2(str(src), str(wt / name))
        except OSError as exc:
            log.warning("Failed to copy %s into worktree: %s", name, exc)


def _setup_git_hooks(root: Path, wt: Path) -> None:
    hooks_path: str | None = None
    if (root / ".husky").is_dir():
        hooks_path = str(root / ".husky")
    elif (root / ".git" / "hooks").is_dir():
        hooks_path = str(root / ".git" / "hooks")
    if hooks_path is None:
        return
    try:
        subprocess.run(
            ["git", "config", "core.hooksPath", hooks_path],
            cwd=str(wt),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Failed to configure worktree hooks path: %s", exc)


def _create_symlinks(root: Path, wt: Path, directories: list[str]) -> None:
    for dirname in directories:
        src = root / dirname
        dst = wt / dirname
        if not src.exists() or dst.exists() or dst.is_symlink():
            continue
        try:
            os.symlink(str(src), str(dst))
        except OSError as exc:
            log.warning("Failed to symlink %s into worktree: %s", dirname, exc)


def _copy_ignored_files(root: Path, wt: Path) -> None:
    include_file = root / ".worktreeinclude"
    if not include_file.exists():
        return
    try:
        patterns = [
            line.strip()
            for line in include_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    except OSError:
        return
    if not patterns:
        return
    try:
        result = subprocess.run(
            ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if result.returncode != 0:
        return
    for rel_path in (line.rstrip("/") for line in result.stdout.splitlines() if line.strip()):
        if not any(fnmatch.fnmatch(rel_path, pattern) for pattern in patterns):
            continue
        src = root / rel_path
        if not src.is_file():
            continue
        dst = wt / rel_path
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
        except OSError as exc:
            log.warning("Failed to copy ignored file %s into worktree: %s", rel_path, exc)
