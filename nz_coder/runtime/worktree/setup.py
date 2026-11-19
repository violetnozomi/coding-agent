"""Post-creation setup for git worktrees."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from nz_coder.foundation.subprocess_env import build_sanitized_subprocess_env

if TYPE_CHECKING:
    from nz_coder.runtime.process.preparation import PreparationScope

log = logging.getLogger(__name__)

LOCAL_CONFIG_FILES = [
    ".env",
    "settings.local.json",
]


def perform_post_creation_setup(
    repo_root: str | Path,
    wt_path: str | Path,
    symlink_directories: list[str] | None = None,
    *,
    preparation: PreparationScope | None = None,
    include_ignored: bool = True,
) -> None:
    root = Path(repo_root)
    wt = Path(wt_path)
    _checkpoint(preparation)
    _copy_local_configs(root, wt, preparation=preparation)
    _checkpoint(preparation)
    if include_ignored:
        _copy_ignored_files(root, wt, preparation=preparation, shared=symlink_directories or [])
    elif (root / ".worktreeinclude").exists():
        log.warning(".worktreeinclude requires Git; use copy_files for a non-Git project")
    _checkpoint(preparation)
    _create_symlinks(root, wt, symlink_directories or [], preparation=preparation)
    _checkpoint(preparation)


def _checkpoint(preparation: PreparationScope | None) -> None:
    if preparation is not None:
        preparation.checkpoint()


def _copy_local_configs(root: Path, wt: Path, *, preparation: PreparationScope | None = None) -> None:
    for name in LOCAL_CONFIG_FILES:
        _checkpoint(preparation)
        src = root / name
        dst = wt / name
        if not src.is_file() or src.is_symlink() or dst.is_symlink():
            continue
        try:
            shutil.copy2(str(src), str(dst))
        except OSError as exc:
            log.warning("Failed to copy %s into worktree: %s", name, exc)


def _create_symlinks(root: Path, wt: Path, directories: list[str], *, preparation: PreparationScope | None = None) -> None:
    for dirname in directories:
        _checkpoint(preparation)
        relative = Path(dirname)
        if relative == Path(".") or relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
            raise ValueError("Shared directory must be a relative workspace path")
        src, dst = root / relative, wt / relative
        if not src.is_dir() or not src.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"Shared directory is unavailable: {dirname}")
        if dst.is_symlink() and dst.resolve() == src.resolve():
            continue
        if dst.exists() or dst.is_symlink():
            raise ValueError(f"Shared directory conflicts with child source: {dirname}")
        if not dst.parent.resolve().is_relative_to(wt.resolve()):
            raise ValueError("Shared directory target escapes child workspace")
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(src.resolve()), str(dst))


def _copy_ignored_files(root: Path, wt: Path, *, preparation: PreparationScope | None = None, shared=()) -> None:
    _checkpoint(preparation)
    snapshot = preparation.config_snapshot if preparation is not None else None
    if snapshot is not None:
        control = snapshot.project_control.get(".worktreeinclude")
        if control is None or not snapshot.project_control.trusted:
            return
        content = control.content
    else:
        include = root / ".worktreeinclude"
        if not include.is_file() or include.is_symlink():
            return
        content = include.read_bytes()
    def git(args):
        if preparation is not None:
            result = preparation.run_git(tuple(args), cwd=root, timeout=30)
        else:
            env = build_sanitized_subprocess_env()
            env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="")
            raw = subprocess.run(
                ["git", *args], cwd=root, capture_output=True, timeout=30, check=False,
                stdin=subprocess.DEVNULL, env=env,
            )
            result = subprocess.CompletedProcess(raw.args, raw.returncode, os.fsdecode(raw.stdout), os.fsdecode(raw.stderr))
        _checkpoint(preparation)
        if result.returncode != 0:
            raise OSError("Unable to select ignored workspace files: " + result.stderr.strip())
        return list(filter(None, result.stdout.split("\0")))
    # Git 从本次激活已信任的字节选取，不能在排队后重新读取控制文件。
    with tempfile.NamedTemporaryFile(prefix="nz-worktreeinclude-") as captured:
        captured.write(content)
        captured.flush()
        candidates = git(["ls-files", "--others", "--ignored", "--exclude-from=" + captured.name, "-z"])
    for offset in range(0, len(candidates), 128):
        paths = git(["--literal-pathspecs", "ls-files", "--others", "--ignored", "--exclude-standard", "-z", "--", *candidates[offset:offset + 128]])
        for name in paths:
            _checkpoint(preparation)
            if any(name == p or name.startswith(p + "/") for p in shared):
                raise ValueError("Included file overlaps shared dependency directory: " + name)
            src, dst = root / name, wt / name
            if src.is_symlink() or not src.is_file() or not src.resolve().is_relative_to(root.resolve()):
                raise ValueError("Included file is not a safe local file: " + name)
            if dst.is_symlink() or not dst.parent.resolve().is_relative_to(wt.resolve()):
                raise ValueError("Included file target escapes workspace")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
