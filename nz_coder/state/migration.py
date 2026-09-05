"""Explicit, bounded migration of legacy repository-local runtime state."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import tempfile
from typing import Iterable

from nz_coder.foundation.file_lock import exclusive_file_lock
from nz_coder.foundation.private_paths import harden_private_path
from nz_coder.foundation.user_paths import prepare_user_storage


_MAX_FILES = 10_000
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_SAFE_CATEGORIES = (
    "runs",
    "changes",
    "tool-results",
    "artifacts",
    "attachments",
    "terminal",
    "review-packets",
    "plans",
)
_SENSITIVE_CATEGORIES = ("sessions", "memory", "models")
_ALL_CATEGORIES = _SAFE_CATEGORIES + _SENSITIVE_CATEGORIES


@dataclass(frozen=True)
class MigrationItem:
    """One legacy regular file accepted for an explicit migration."""

    category: str
    source: Path
    destination: Path
    size: int


@dataclass(frozen=True)
class MigrationResult:
    """Bounded migration outcome without implicit source deletion."""

    planned: tuple[MigrationItem, ...]
    copied: tuple[MigrationItem, ...]
    skipped: tuple[str, ...]
    total_bytes: int


def migrate_legacy_state(
    workspace: str | Path,
    *,
    include: Iterable[str] = (),
    apply: bool = False,
    delete_source: bool = False,
) -> MigrationResult:
    """Inspect and optionally copy selected legacy state into private user roots."""
    root = Path(workspace).resolve(strict=True)
    requested = tuple(dict.fromkeys(str(item).strip() for item in include if str(item).strip()))
    unknown = sorted(set(requested) - set(_ALL_CATEGORIES))
    if unknown:
        raise ValueError("Unknown legacy state categories: " + ", ".join(unknown))
    categories = requested or _SAFE_CATEGORIES
    if delete_source and not apply:
        raise ValueError("--delete-source requires --apply")
    legacy = root / ".nz-coder"
    if legacy.is_symlink() or _is_reparse(legacy):
        raise ValueError("Legacy .nz-coder must not be a symlink or reparse point")
    if not legacy.exists():
        return MigrationResult((), (), (), 0)
    if not legacy.is_dir():
        raise ValueError("Legacy .nz-coder must be a directory")
    _require_owner(legacy)

    layout = prepare_user_storage(root)
    planned: list[MigrationItem] = []
    skipped: list[str] = []
    total = 0
    for category in categories:
        source_root = legacy / category
        if not source_root.exists():
            continue
        if source_root.is_symlink() or _is_reparse(source_root) or not source_root.is_dir():
            raise ValueError(f"Legacy category is not a safe directory: {category}")
        _require_owner(source_root)
        for source in _walk_regular_files(source_root):
            relative = source.relative_to(source_root)
            size = source.stat(follow_symlinks=False).st_size
            if size > _MAX_FILE_BYTES:
                raise ValueError(f"Legacy state file exceeds 8 MiB: {category}/{relative}")
            total += size
            if len(planned) >= _MAX_FILES or total > _MAX_TOTAL_BYTES:
                raise ValueError("Legacy state migration exceeds the bounded quota")
            destination = _destination_path(layout, category, relative)
            if destination.exists():
                skipped.append(f"{category}/{relative}: destination exists")
                continue
            planned.append(MigrationItem(category, source, destination, size))

    copied: list[MigrationItem] = []
    if apply:
        lock = layout.state_root / "migration.lock"
        with exclusive_file_lock(lock):
            for item in planned:
                _copy_regular_file(item.source, item.destination)
                copied.append(item)
            if delete_source:
                for item in copied:
                    item.source.unlink()
    return MigrationResult(tuple(planned), tuple(copied), tuple(skipped), total)


def migration_main(argv: list[str] | None = None) -> int:
    """CLI for inspect-first legacy runtime-state migration."""
    parser = argparse.ArgumentParser(prog="nz-coder migrate-state")
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--include", action="append", choices=_ALL_CATEGORIES, default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--delete-source", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = migrate_legacy_state(
            args.cwd,
            include=args.include,
            apply=args.apply,
            delete_source=args.delete_source,
        )
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1
    action = "Copied" if args.apply else "Planned"
    print(f"{action} {len(result.copied) if args.apply else len(result.planned)} file(s), {result.total_bytes} bytes.")
    if not args.apply and result.planned:
        print("Review the categories above, then rerun with --apply. Sources are preserved by default.")
    if not args.include:
        print("Sensitive categories sessions, memory, and models were excluded; use --include explicitly.")
    for item in result.planned[:100]:
        print(f"- {item.category}: {item.source.name} ({item.size} bytes)")
    return 0


def _destination_path(layout, category: str, relative: Path) -> Path:
    if category == "models":
        base = (
            layout.workspace_state
            if relative.as_posix() == "selection.json"
            else layout.workspace_cache
        )
        return base / "models" / relative
    if category == "plans":
        return layout.workspace_state / "sessions" / "_plans" / relative
    return layout.workspace_state / category / relative


def _walk_regular_files(root: Path):
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in tuple(directory_names):
            child = base / name
            if child.is_symlink() or _is_reparse(child):
                raise ValueError(f"Legacy state contains an aliased directory: {child}")
            _require_owner(child)
        for name in sorted(file_names):
            source = base / name
            info = source.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or _is_reparse(source):
                raise ValueError(f"Legacy state contains an aliased file: {source}")
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"Legacy state contains a non-regular file: {source}")
            _require_owner(source)
            yield source


def _copy_regular_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    harden_private_path(destination.parent)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(source, flags)
    temporary = ""
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE_BYTES:
            raise ValueError(f"Legacy state changed during migration: {source}")
        output, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        with os.fdopen(output, "wb") as target:
            remaining = info.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 1024 * 1024))
                if not chunk:
                    raise ValueError(f"Legacy state changed during migration: {source}")
                target.write(chunk)
                remaining -= len(chunk)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        harden_private_path(destination)
    finally:
        os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _require_owner(path: Path) -> None:
    info = path.stat(follow_symlinks=False)
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValueError(f"Legacy state is not owned by the current user: {path}")


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return bool(int(getattr(info, "st_file_attributes", 0) or 0) & 0x00000400)


__all__ = ["MigrationItem", "MigrationResult", "migrate_legacy_state", "migration_main"]
