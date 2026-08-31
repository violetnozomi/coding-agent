"""Git worktree manager for isolated child-agent workspaces."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from nz_coder.runtime.worktree.models import Worktree
from nz_coder.runtime.worktree.setup import perform_post_creation_setup

GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}
_SNAPSHOT_EXCLUDED_DIRS = frozenset({
    ".git", ".nz-coder", ".nz-coder-runs", ".hg", ".svn",
    ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", "build", "dist",
})


class WorktreeError(Exception):
    """Raised when a child-agent worktree cannot be prepared."""


class WorktreeManager:
    """Create or reuse git worktrees under ``.nz-coder/worktrees``."""

    def __init__(
        self,
        repo_root: str | Path,
        symlink_directories: list[str] | None = None,
        worktree_dir: str | Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.symlink_directories = symlink_directories or []
        self.worktree_dir = Path(worktree_dir or (self.repo_root / ".nz-coder" / "worktrees")).resolve()
        try:
            self.worktree_dir.relative_to(self.repo_root)
        except ValueError as exc:
            raise WorktreeError("Managed worktree directory escapes repository root") from exc

    def create(self, worktree_id: str, base_ref: str = "HEAD") -> Worktree:
        safe_id = _safe_slug(worktree_id)
        base_ref = _validated_base_ref(base_ref)
        wt_path = self.worktree_dir / safe_id
        if wt_path.is_symlink():
            raise WorktreeError("Managed worktree target must not be a symbolic link")
        if wt_path.exists() and not wt_path.is_dir():
            raise WorktreeError("Managed worktree target must be a directory")
        if not self.is_git_repo():
            return self._copy_worktree(safe_id, base_ref)

        branch_name = f"subagent-{safe_id}"
        head_sha = self.read_worktree_head_sha(wt_path)
        if head_sha is not None:
            return Worktree(
                id=safe_id,
                path=str(wt_path),
                branch=branch_name,
                based_on=base_ref,
                head_commit=head_sha,
                mode="git",
            )

        self.worktree_dir.mkdir(parents=True, exist_ok=True)
        result = self._run_git([
            "worktree", "add", "-B", branch_name, "--", str(wt_path), base_ref,
        ])
        if result.returncode != 0:
            return self._copy_worktree(safe_id, base_ref)

        self._sync_snapshot(wt_path, preserve_git=True)
        perform_post_creation_setup(
            self.repo_root,
            wt_path,
            symlink_directories=self.symlink_directories,
        )
        return Worktree(
            id=safe_id,
            path=str(wt_path),
            branch=branch_name,
            based_on=base_ref,
            head_commit=self.read_worktree_head_sha(wt_path) or "",
            mode="git",
        )

    def _copy_worktree(self, safe_id: str, base_ref: str) -> Worktree:
        """Create a filesystem snapshot when Git worktrees are unavailable."""
        wt_path = self.worktree_dir / safe_id
        if wt_path.is_symlink():
            raise WorktreeError("Managed worktree target must not be a symbolic link")
        if wt_path.exists() and not wt_path.is_dir():
            raise WorktreeError("Managed worktree target must be a directory")
        if not wt_path.exists():
            wt_path.mkdir(parents=True, exist_ok=False)
            self._sync_snapshot(wt_path, preserve_git=False)
            perform_post_creation_setup(
                self.repo_root,
                wt_path,
                symlink_directories=self.symlink_directories,
            )
        return Worktree(
            id=safe_id,
            path=str(wt_path),
            branch="",
            based_on=base_ref,
            head_commit="",
            mode="copy",
        )

    def _sync_snapshot(self, destination: Path, *, preserve_git: bool) -> None:
        """Make an isolated workspace reflect the parent's current file state."""
        destination = destination.resolve()
        try:
            destination.relative_to(self.worktree_dir)
        except ValueError as exc:
            raise WorktreeError("Worktree destination escapes managed directory") from exc

        for child in destination.iterdir():
            if preserve_git and child.name == ".git":
                continue
            if child.is_symlink() or child.is_file():
                child.unlink()
            elif child.is_dir():
                shutil.rmtree(child)

        for root, dir_names, file_names in os.walk(self.repo_root, followlinks=False):
            root_path = Path(root)
            try:
                relative_root = root_path.relative_to(self.repo_root)
            except ValueError:
                continue
            dir_names[:] = sorted(
                name
                for name in dir_names
                if name not in _SNAPSHOT_EXCLUDED_DIRS
                and not (root_path / name).is_symlink()
            )
            target_root = destination / relative_root
            target_root.mkdir(parents=True, exist_ok=True)
            for name in sorted(file_names):
                source = root_path / name
                if source.is_symlink():
                    continue
                target = target_root / name
                try:
                    shutil.copy2(source, target)
                except OSError as exc:
                    raise WorktreeError(f"Failed to snapshot {source}: {exc}") from exc

    def is_git_repo(self) -> bool:
        result = self._run_git(["rev-parse", "--is-inside-work-tree"])
        return result.returncode == 0 and result.stdout.strip() == "true"

    def changed_files(self, worktree_path: str | Path) -> list[str]:
        """Return repo-relative files currently changed inside a worktree."""
        result = self._run_git(["status", "--porcelain", "--untracked-files=all"], cwd=worktree_path)
        if result.returncode != 0:
            return []
        changed: list[str] = []
        for raw_line in result.stdout.splitlines():
            line = raw_line.rstrip()
            if len(line) < 4:
                continue
            rel_path = line[3:]
            if " -> " in rel_path:
                rel_path = rel_path.split(" -> ", 1)[1]
            rel_path = rel_path.strip().replace('\\', '/')
            if rel_path:
                changed.append(rel_path)
        return sorted(dict.fromkeys(changed))

    def remove(self, worktree: Worktree | str | Path) -> bool:
        """Remove one managed child worktree and its private Git branch.

        The target must be an immediate child of this manager's worktree
        directory.  This mirrors InfCode's explicit worktree teardown while
        keeping NZ-Coder's copy-mode fallback safe when Git is unavailable.
        Missing targets are treated as already removed.
        """
        metadata = worktree if isinstance(worktree, Worktree) else None
        raw_path = Path(metadata.path if metadata is not None else worktree)
        target = raw_path.resolve()
        try:
            relative = target.relative_to(self.worktree_dir)
        except ValueError as exc:
            raise WorktreeError("Worktree removal target escapes managed directory") from exc
        if target == self.worktree_dir or len(relative.parts) != 1:
            raise WorktreeError("Worktree removal target must be one managed child")

        mode = metadata.mode if metadata is not None else "git"
        branch = metadata.branch if metadata is not None else ""
        if mode == "git" and self.is_git_repo():
            removed = self._run_git(["worktree", "remove", "--force", str(target)])
            if removed.returncode != 0 and target.exists():
                raise WorktreeError(
                    "Failed to remove git worktree: "
                    + (removed.stderr.strip() or removed.stdout.strip() or str(target))
                )
            self._run_git(["worktree", "prune"])

        self._remove_directory(target)
        if branch and branch.startswith("subagent-") and self.is_git_repo():
            deleted = self._run_git(["branch", "-D", branch])
            if deleted.returncode != 0 and "not found" not in deleted.stderr.lower():
                raise WorktreeError(
                    "Worktree directory was removed but its branch could not be deleted: "
                    + (deleted.stderr.strip() or branch)
                )
        return True

    @staticmethod
    def _remove_directory(target: Path) -> None:
        """Remove a managed directory with bounded retries for transient locks."""
        if target.is_symlink():
            target.unlink(missing_ok=True)
            return
        for attempt in range(5):
            try:
                shutil.rmtree(target)
                return
            except FileNotFoundError:
                return
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(0.1)

    def _run_git(self, args: list[str], cwd: str | Path | None = None) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, **GIT_ENV}
        try:
            return subprocess.run(
                ["git", *args],
                cwd=str(cwd or self.repo_root),
                capture_output=True,
                text=True,
                timeout=60,
                stdin=subprocess.DEVNULL,
                env=env,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return subprocess.CompletedProcess(["git", *args], 1, "", str(exc))

    @staticmethod
    def read_worktree_head_sha(wt_path: str | Path) -> str | None:
        wt = Path(wt_path)
        git_file = wt / ".git"
        if not git_file.exists():
            return None
        try:
            content = git_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not content.startswith("gitdir:"):
            return None
        gitdir = Path(content.split(":", 1)[1].strip())
        if not gitdir.is_absolute():
            gitdir = (wt / gitdir).resolve()
        head_file = gitdir / "HEAD"
        if not head_file.exists():
            return None
        try:
            head_content = head_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not head_content.startswith("ref:"):
            return head_content or None
        ref_path = head_content.split(":", 1)[1].strip()
        ref_file = gitdir / ref_path
        if ref_file.exists():
            try:
                return ref_file.read_text(encoding="utf-8").strip() or None
            except OSError:
                return None
        return None

def _safe_slug(value: str) -> str:
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(value or "subagent"))
    while "--" in safe:
        safe = safe.replace("--", "-")
    safe = safe.strip("-") or "subagent"
    return safe


def _validated_base_ref(value: str) -> str:
    if not isinstance(value, str):
        raise WorktreeError("Worktree base ref must be a string")
    if (
        not value
        or len(value) > 500
        or value.startswith("-")
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise WorktreeError("Worktree base ref is invalid")
    return value
