"""User-owned state and cache roots, isolated by opaque workspace identity."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Mapping

from nz_coder.foundation.private_paths import harden_private_path
from nz_coder.foundation.workspace_trust import workspace_identity_key


class UnsafeUserStorage(ValueError):
    """Raised when a private state/cache root crosses a filesystem alias."""


@dataclass(frozen=True)
class UserStorageLayout:
    """Platform-standard roots for one canonical workspace identity."""

    state_root: Path
    cache_root: Path
    workspace_state: Path
    workspace_cache: Path
    workspace_key: str


_ATTACHMENT_PREFIX = "user-state://attachments/"


def user_storage_layout(
    workspace: Path | str,
    *,
    environ: Mapping[str, str] | None = None,
) -> UserStorageLayout:
    """Resolve private roots without using a repository-controlled path segment."""
    environment = os.environ if environ is None else environ
    root = Path(workspace).expanduser().absolute()
    key = workspace_identity_key(root)
    if os.name == "nt":
        local = str(environment.get("LOCALAPPDATA", "")).strip()
        base = Path(local).expanduser().absolute() if local else (
            Path.home() / "AppData" / "Local"
        )
        state_root = base / "nz-coder" / "state"
        cache_root = base / "nz-coder" / "cache"
    else:
        state_value = str(environment.get("XDG_STATE_HOME", "")).strip()
        cache_value = str(environment.get("XDG_CACHE_HOME", "")).strip()
        state_base = (
            Path(state_value).expanduser().absolute()
            if state_value else Path.home() / ".local" / "state"
        )
        cache_base = (
            Path(cache_value).expanduser().absolute()
            if cache_value else Path.home() / ".cache"
        )
        state_root = state_base / "nz-coder"
        cache_root = cache_base / "nz-coder"
    _reject_workspace_root(state_root, root)
    _reject_workspace_root(cache_root, root)
    return UserStorageLayout(
        state_root=state_root,
        cache_root=cache_root,
        workspace_state=state_root / "workspaces" / key,
        workspace_cache=cache_root / "workspaces" / key,
        workspace_key=key,
    )


def prepare_user_storage(
    workspace: Path | str,
    *,
    environ: Mapping[str, str] | None = None,
) -> UserStorageLayout:
    """Create and verify owner-private roots without accepting aliases."""
    layout = user_storage_layout(workspace, environ=environ)
    for target in (
        layout.state_root,
        layout.cache_root,
        layout.state_root / "workspaces",
        layout.cache_root / "workspaces",
        layout.workspace_state,
        layout.workspace_cache,
    ):
        _secure_directory(target)
    return layout


def private_attachment_reference(filename: str) -> str:
    """Return an opaque, non-filesystem reference for one private attachment."""
    name = Path(str(filename)).name
    if not name or name != str(filename) or name in {".", ".."}:
        raise ValueError("private attachment name is invalid")
    return _ATTACHMENT_PREFIX + name


def resolve_private_attachment(
    workspace: Path | str,
    reference: str,
) -> Path:
    """Resolve only an opaque attachment reference in this workspace's user state."""
    value = str(reference)
    if not value.startswith(_ATTACHMENT_PREFIX):
        raise ValueError("not a private attachment reference")
    name = value[len(_ATTACHMENT_PREFIX):]
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("private attachment reference is invalid")
    root = prepare_user_storage(workspace).workspace_state / "attachments"
    target = root / name
    if target.is_symlink():
        raise ValueError("private attachment must not be an alias")
    return target


def _reject_workspace_root(candidate: Path, workspace: Path) -> None:
    try:
        candidate.resolve(strict=False).relative_to(workspace.resolve(strict=True))
    except ValueError:
        return
    except OSError as exc:
        raise UnsafeUserStorage("workspace identity cannot be verified") from exc
    raise UnsafeUserStorage("user storage root must be outside the workspace")


def _is_alias(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    return bool(attributes & 0x00000400)


def _secure_directory(path: Path) -> None:
    path = path.expanduser().absolute()
    chain = list(reversed((path, *path.parents)))
    for current in chain:
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise UnsafeUserStorage("user storage path cannot be inspected") from exc
        if _is_alias(info):
            raise UnsafeUserStorage("user storage path contains an alias")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise UnsafeUserStorage("user storage directory cannot be created") from exc
    for current in chain:
        try:
            info = current.lstat()
        except OSError as exc:
            raise UnsafeUserStorage("user storage path cannot be verified") from exc
        if _is_alias(info) or (current == path and not stat.S_ISDIR(info.st_mode)):
            raise UnsafeUserStorage("user storage path is unsafe")
    security = harden_private_path(path)
    if not security.hardened:
        raise UnsafeUserStorage("user storage directory is not owner-private")


__all__ = [
    "UnsafeUserStorage",
    "UserStorageLayout",
    "prepare_user_storage",
    "private_attachment_reference",
    "resolve_private_attachment",
    "user_storage_layout",
]
