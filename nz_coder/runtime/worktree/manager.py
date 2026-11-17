"""Git worktree manager for isolated child-agent workspaces."""
from __future__ import annotations

import os
import hashlib
import inspect
import stat
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from nz_coder.foundation.user_paths import prepare_user_storage
from nz_coder.foundation.private_paths import harden_private_path
from nz_coder.foundation.subprocess_env import build_sanitized_subprocess_env
from nz_coder.protocol.public_error import to_public_error
from nz_coder.runtime.worktree.models import Worktree
from nz_coder.runtime.worktree.setup import perform_post_creation_setup

if TYPE_CHECKING:
    from nz_coder.runtime.process.preparation import PreparationScope

GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""}
_SNAPSHOT_EXCLUDED_DIRS = frozenset({
    ".git", ".nz-coder", ".nz-coder-runs", ".hg", ".svn",
    ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", "build", "dist",
})


class WorktreeError(Exception):
    """Raised when a child-agent worktree cannot be prepared."""


class WorktreeManager:
    """Create or reuse child worktrees in a private user cache."""

    def __init__(
        self,
        repo_root: str | Path,
        symlink_directories: list[str] | None = None,
        worktree_dir: str | Path | None = None,
        *,
        preparation: PreparationScope | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.symlink_directories = symlink_directories or []
        self.preparation = preparation
        self.snapshot_source = self.repo_root
        self.source_manifest: dict[str, dict] = {}
        self.snapshot_exclusions: tuple[str, ...] = ()
        default = prepare_user_storage(self.repo_root).workspace_cache / "worktrees"
        self.worktree_dir = Path(worktree_dir or default).resolve()
        if self.worktree_dir == self.repo_root:
            raise WorktreeError("Managed worktree directory must not be the repository root")

    def create(self, worktree_id: str, base_ref: str = "HEAD", *, snapshot_source: Path | None = None) -> Worktree:
        self.snapshot_source = Path(snapshot_source or self.repo_root).resolve()
        self._checkpoint()
        safe_id = _safe_slug(worktree_id)
        base_ref = _validated_base_ref(base_ref)
        wt_path = self.worktree_dir / safe_id
        if wt_path.is_symlink():
            raise WorktreeError("Managed worktree target must not be a symbolic link")
        if wt_path.exists() and not wt_path.is_dir():
            raise WorktreeError("Managed worktree target must be a directory")
        if self.preparation is not None and wt_path.exists():
            raise WorktreeError("Existing preparation target has no completed ownership record")
        is_git = self.is_git_repo()
        self._checkpoint()
        if self.preparation is not None and (wt_path.exists() or wt_path.is_symlink()):
            raise WorktreeError("Existing preparation target has no completed ownership record")
        if not is_git:
            return self._copy_worktree(safe_id, base_ref)

        branch_name = f"subagent-{safe_id}"
        # 受控准备的完整工作树只在上游按持久化记录复用，此处不能认领未知目录。
        head_sha = self.read_worktree_head_sha(wt_path) if self.preparation is None else None
        if head_sha is not None:
            return Worktree(
                id=safe_id,
                path=str(wt_path),
                branch=branch_name,
                based_on=base_ref,
                head_commit=head_sha,
                mode="git",
            )

        resolved = self._run_git(["rev-parse", "--verify", base_ref + "^{commit}"])
        if resolved.returncode or not resolved.stdout.strip():
            raise WorktreeError("Worktree base commit is unavailable")
        base_commit = resolved.stdout.strip()

        if self.preparation is not None:
            self.preparation.record_target(Worktree(safe_id, str(wt_path), branch_name, base_ref, "", mode="git"))
        self._checkpoint()
        self.worktree_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        harden_private_path(self.worktree_dir)
        result = self._run_git([
            "worktree", "add", "-b", branch_name, "--", str(wt_path), base_commit,
        ])
        self._checkpoint()
        if result.returncode != 0:
            if self.preparation is not None:
                from nz_coder.protocol.public_error import PublicError
                from nz_coder.runtime.process.preparation import PreparationFailure
                raise PreparationFailure(PublicError(
                    "preparation_git_add_failed", "Worktree preparation did not complete; explicit cleanup is required.",
                ))
            raise WorktreeError("Git worktree creation failed; retain its ownership record for cleanup")

        self._sync_snapshot(wt_path, preserve_git=True)
        perform_post_creation_setup(
            self.snapshot_source,
            wt_path,
            symlink_directories=self.symlink_directories,
            preparation=self.preparation,
        )
        self._checkpoint()
        return Worktree(
            id=safe_id,
            path=str(wt_path),
            branch=branch_name,
            based_on=base_ref,
            head_commit=self._run_git(["rev-parse", "--verify", "HEAD"], cwd=wt_path).stdout.strip(),
            mode="git",
        )

    def _copy_worktree(self, safe_id: str, base_ref: str) -> Worktree:
        """Create a filesystem snapshot when Git worktrees are unavailable."""
        wt_path = self.worktree_dir / safe_id
        if wt_path.is_symlink():
            raise WorktreeError("Managed worktree target must not be a symbolic link")
        if wt_path.exists() and not wt_path.is_dir():
            raise WorktreeError("Managed worktree target must be a directory")
        if self.preparation is not None and wt_path.exists():
            raise WorktreeError("Existing preparation target has no completed ownership record")
        if not wt_path.exists():
            if self.preparation is not None:
                self.preparation.record_target(Worktree(safe_id, str(wt_path), "", base_ref, "", mode="copy"))
            self._checkpoint()
            self.worktree_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            harden_private_path(self.worktree_dir)
            wt_path.mkdir(mode=0o700, parents=True, exist_ok=False)
            self._sync_snapshot(wt_path, preserve_git=False)
            perform_post_creation_setup(
                self.snapshot_source,
                wt_path,
                symlink_directories=self.symlink_directories,
                preparation=self.preparation,
                include_ignored=False,
            )
        self._checkpoint()
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
        self._checkpoint()
        try:
            destination.relative_to(self.worktree_dir)
        except ValueError as exc:
            raise WorktreeError("Worktree destination escapes managed directory") from exc

        source = self.snapshot_source
        before, signature = self._inventory(source, git=preserve_git)
        # 任意旧 base/fork 可能多出已提交删除的文件，不能只覆盖源端仍存在的路径。
        if preserve_git:
            checked_out, _ = self._inventory(destination, git=True)
            for relative in sorted(set(checked_out) - set(before), key=lambda p: len(Path(p).parts), reverse=True):
                self._checkpoint()
                target = destination / relative
                if target.is_file() or target.is_symlink():
                    target.unlink()
        for relative, entry in before.items():
            self._checkpoint()
            target = destination / relative
            # checkout 的目录可能是源码软链；先断开目标祖先链接，绝不沿它写回父方。
            cursor = destination
            for part in Path(relative).parts[:-1]:
                cursor /= part
                if cursor.is_symlink() or (cursor.exists() and not cursor.is_dir()):
                    cursor.unlink()
                cursor.mkdir(exist_ok=True)
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            if entry["kind"] == "missing":
                continue
            src = source / relative
            if entry["kind"] == "symlink":
                target.symlink_to(entry["target"])
            else:
                shutil.copy2(src, target)
        after, final_signature = self._inventory(source, git=preserve_git)
        if before != after or signature != final_signature:
            raise WorktreeError("Parent source changed during workspace preparation")
        self.source_manifest = {key: value for key, value in before.items() if value["kind"] != "missing"}
        self._checkpoint()

    def _inventory(self, source: Path, *, git: bool) -> tuple[dict, str]:
        tracked: set[str] = set()
        gitlinks: set[str] = set()
        signature = ""
        if git:
            def checked(args):
                result = self._run_git(args, cwd=source)
                if result.returncode:
                    raise WorktreeError("Unable to inspect source workspace: " + result.stderr.strip())
                return result.stdout
            head = checked(["rev-parse", "--verify", "HEAD"])
            index = checked(["ls-files", "--stage", "-z"])
            tree = checked(["ls-tree", "-r", "-z", "HEAD"])
            signature = head + index
            for record in index.split("\0"):
                if not record:
                    continue
                metadata, path = record.split("\t", 1)
                mode, _sha, stage = metadata.split()
                if stage != "0":
                    raise WorktreeError("Source workspace has an unmerged index")
                tracked.add(path)
                if mode == "160000":
                    gitlinks.add(path)
            for record in tree.split("\0"):
                if not record:
                    continue
                metadata, path = record.split("\t", 1)
                tracked.add(path)
                if metadata.startswith("160000 "):
                    gitlinks.add(path)
            untracked = set(filter(None, checked(["ls-files", "--others", "--exclude-standard", "-z"]).split("\0")))
            names = tracked | {p for p in untracked if not any(part in _SNAPSHOT_EXCLUDED_DIRS for part in Path(p).parts)}
        else:
            names = set()
            for current, dirs, files in os.walk(source, followlinks=False):
                self._checkpoint()
                current = Path(current)
                for name in dirs + files:
                    path = current / name
                    if path.is_symlink() and name not in _SNAPSHOT_EXCLUDED_DIRS:
                        names.add(path.relative_to(source).as_posix())
                dirs[:] = [name for name in dirs if name not in _SNAPSHOT_EXCLUDED_DIRS and not (current / name).is_symlink()]
                names.update((current / name).relative_to(source).as_posix() for name in files if name not in _SNAPSHOT_EXCLUDED_DIRS)
        manifest = {}
        for name in sorted(names):
            self._checkpoint()
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
                continue
            if any(name == link or name.startswith(link + "/") for link in gitlinks):
                continue
            if name not in tracked and any(name == link or name.startswith(link + "/") for link in (*self.symlink_directories, *self.snapshot_exclusions)):
                continue
            path = source / relative
            if path.is_symlink():
                raw_target = os.readlink(path)
                resolved = path.resolve()
                if Path(raw_target).is_absolute() or not resolved.is_relative_to(source):
                    raise WorktreeError(f"Source symlink requires an explicit safe mapping: {name}")
                manifest[name] = {"kind": "symlink", "target": raw_target,
                                  "sha256": hashlib.sha256(os.fsencode(raw_target)).hexdigest()}
            elif path.is_file():
                if not path.resolve().is_relative_to(source):
                    raise WorktreeError(f"Source path escapes workspace: {name}")
                manifest[name] = {"kind": "file", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            elif name in tracked:
                manifest[name] = {"kind": "missing"}
        return manifest, signature

    def _checkpoint(self) -> None:
        if self.preparation is not None:
            self.preparation.checkpoint()

    def is_git_repo(self) -> bool:
        result = self._run_git(["rev-parse", "--is-inside-work-tree"])
        return result.returncode == 0 and result.stdout.strip() == "true"

    def gitlinks(self, root: Path | None = None, *, ref: str | None = None) -> dict[str, str]:
        """模块边界来自真实 Git 清单，配置只选择需要初始化的模块。"""
        args = ["ls-tree", "-r", "-z", ref] if ref is not None else ["ls-files", "--stage", "-z"]
        result = self._run_git(["-C", str(root or self.repo_root), *args])
        if result.returncode:
            raise WorktreeError("Unable to inspect Git submodule boundaries")
        links = {}
        for row in filter(None, result.stdout.split("\0")):
            fields, name = row.split("\t", 1)
            metadata = fields.split()
            if metadata[0] != "160000":
                continue
            if ref is None and metadata[2] != "0":
                raise WorktreeError("Conflicted submodule index is unsupported")
            links[name] = metadata[2] if ref is not None else metadata[1]
        return links

    def changed_files(self, worktree_path: str | Path) -> list[str]:
        """Return repo-relative files currently changed inside a worktree."""
        result = self._run_git(["status", "--porcelain", "-z", "--untracked-files=all"], cwd=worktree_path)
        if result.returncode != 0:
            raise WorktreeError("Unable to determine worktree cleanliness")
        fields = iter(result.stdout.split("\0"))
        changed = []
        for field in fields:
            if not field:
                continue
            changed.append(field[3:])
            if 'R' in field[:2] or 'C' in field[:2]:
                next(fields, None)
        return sorted(set(changed))

    def remove(self, worktree: Worktree | str | Path, *, preparing: bool = False) -> bool:
        """Remove one managed child worktree and its private Git branch.

        The target must be an immediate child of this manager's worktree
        directory.  This mirrors InfCode's explicit worktree teardown while
        keeping NZ-Coder's copy-mode fallback safe when Git is unavailable.
        Missing targets are treated as already removed.
        """
        metadata = worktree if isinstance(worktree, Worktree) else None
        raw_path = Path(metadata.path if metadata is not None else worktree)
        target = Path(os.path.abspath(raw_path))
        try:
            relative = target.relative_to(self.worktree_dir)
        except ValueError as exc:
            raise WorktreeError("Worktree removal target escapes managed directory") from exc
        if target == self.worktree_dir or len(relative.parts) != 1:
            raise WorktreeError("Worktree removal target must be one managed child")
        if metadata is not None and metadata.id != target.name:
            raise WorktreeError("Worktree identity does not match managed directory")
        try:
            info = target.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                                 or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise WorktreeError("Worktree removal target is not an owned directory")

        mode = metadata.mode if metadata is not None else "git"
        branch = metadata.branch if metadata is not None else ""
        if mode == "git" and metadata is not None and branch and branch != f"subagent-{metadata.id}":
            raise WorktreeError("Git worktree branch identity does not match its owner")
        if preparing and mode == "git":
            if metadata is None or metadata.id != target.name or metadata.branch != f"subagent-{metadata.id}":
                raise WorktreeError("Pending Git worktree cleanup could not be confirmed; retain the Session and retry.")
            self._remove_preparing_git(metadata, target, info)
            return True
        if mode == "git" and not self.is_git_repo():
            raise WorktreeError("Git worktree ownership could not be confirmed")
        if mode == "git":
            listed = self._run_git(["worktree", "list", "--porcelain", "-z"])
            if listed.returncode != 0:
                raise WorktreeError("Git worktree registration cleanup is unconfirmed")
            registered = any(
                Path(os.path.abspath(field[9:])) == target
                for field in listed.stdout.split("\0") if field.startswith("worktree ")
            )
            if info is None and registered:
                raise WorktreeError("Git worktree registration cleanup is unconfirmed")
            if info is not None:
                if not registered:
                    raise WorktreeError("Git worktree ownership could not be confirmed")
                try:
                    current = target.lstat()
                except FileNotFoundError as exc:
                    raise WorktreeError("Git worktree disappeared before removal") from exc
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino) or not stat.S_ISDIR(current.st_mode):
                    raise WorktreeError("Git worktree identity changed before removal")
                removed = self._run_git(["worktree", "remove", "--force", str(target)])
                if removed.returncode != 0 or target.exists() or target.is_symlink():
                    raise WorktreeError("Git worktree removal could not be confirmed")
        else:
            self._remove_directory(target, info)
        if branch and branch.startswith("subagent-") and mode == "git":
            deleted = self._run_git(["branch", "-D", branch])
            if deleted.returncode != 0 and "not found" not in deleted.stderr.lower():
                raise WorktreeError(
                    "Worktree directory was removed but its branch could not be deleted: "
                    + (deleted.stderr.strip() or branch)
                )
        return True

    def _remove_preparing_git(self, metadata: Worktree | None, target: Path, info) -> None:
        # 目录缺失不能证明Git注册/分支已清理；不确定时保留Session原指向。
        message = "Pending Git worktree cleanup could not be confirmed; retain the Session and retry."
        if (metadata is None or metadata.id != target.name
                or metadata.branch != f"subagent-{metadata.id}"):
            raise WorktreeError(message)

        def checked(args: list[str]) -> subprocess.CompletedProcess[str]:
            result = self._run_git(args)
            if result.returncode != 0:
                raise WorktreeError(message)
            return result

        def registered() -> bool:
            result = checked(["worktree", "list", "--porcelain", "-z"])
            return any(
                Path(field[9:]).resolve() == target
                for field in result.stdout.split("\0") if field.startswith("worktree ")
            )

        reference = f"refs/heads/{metadata.branch}"

        def branch_exists() -> bool:
            result = checked(["for-each-ref", "--format=%(refname)", "--", reference])
            return reference in result.stdout.splitlines()

        if checked(["rev-parse", "--is-inside-work-tree"]).stdout.strip() != "true":
            raise WorktreeError(message)
        if registered():
            if info is None:
                raise WorktreeError(message)
            current = target.lstat()
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino) or not stat.S_ISDIR(current.st_mode):
                raise WorktreeError(message)
            result = self._run_git(["worktree", "remove", "--force", str(target)])
            if result.returncode != 0 or target.exists() or target.is_symlink():
                raise WorktreeError(message)
            checked(["worktree", "prune"])
        elif info is not None:
            raise WorktreeError(message)
        if registered():
            raise WorktreeError(message)
        if branch_exists():
            checked(["branch", "-D", metadata.branch])
        if branch_exists():
            raise WorktreeError(message)

    @staticmethod
    def _remove_directory(target: Path, expected) -> None:
        """Remove a managed directory with bounded retries for transient locks."""
        if expected is None:
            if target.exists() or target.is_symlink():
                raise WorktreeError("Worktree target appeared during cleanup")
            return
        if os.name == 'nt' or not shutil.rmtree.avoids_symlink_attacks or 'dir_fd' not in inspect.signature(shutil.rmtree).parameters:
            raise WorktreeError("Safe worktree directory removal is unavailable")
        parent = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            current = os.stat(target.name, dir_fd=parent, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
                raise WorktreeError("Worktree directory identity changed")
            shutil.rmtree(target.name, dir_fd=parent)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise WorktreeError(f"Unable to remove worktree directory: {exc}") from exc
        finally:
            os.close(parent)

    def _run_git(self, args: list[str], cwd: str | Path | None = None) -> subprocess.CompletedProcess[str]:
        env = {**build_sanitized_subprocess_env(), **GIT_ENV}
        try:
            if self.preparation is not None:
                return self.preparation.run_git(tuple(args), cwd=Path(cwd or self.repo_root), timeout=60)
            result = subprocess.run(
                ["git", *args],
                cwd=str(cwd or self.repo_root),
                capture_output=True,
                timeout=60,
                stdin=subprocess.DEVNULL,
                env=env,
                check=False,
            )
            # NUL分隔的Git路径必须保留回车和任意文件名字节；文本模式会改写它们。
            return subprocess.CompletedProcess(
                result.args, result.returncode, os.fsdecode(result.stdout), os.fsdecode(result.stderr),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._checkpoint()
            return subprocess.CompletedProcess(
                ["git", *args], 1, "", to_public_error(exc).message,
            )

    @staticmethod
    def read_worktree_head_sha(wt_path: str | Path) -> str | None:
        wt = Path(wt_path)
        if not (wt / ".git").exists():
            return None
        result = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=wt,
                                capture_output=True, stdin=subprocess.DEVNULL,
                                env={**build_sanitized_subprocess_env(), **GIT_ENV}, timeout=10, check=False)
        return os.fsdecode(result.stdout).strip() if result.returncode == 0 else None


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
