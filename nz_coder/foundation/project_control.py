"""Immutable, handle-anchored capture of repository-owned control files."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
from types import MappingProxyType
from typing import Iterable, Mapping


CONTROL_FILE_KINDS = ("fixed", "skill", "command", "workflow")
_SNAPSHOT_KINDS = ("settings", "mcp", "skill", "command", "workflow")
_FIXED_CONTROL_PATHS = (
    ("settings", ".nz-coder/settings.json"),
    ("mcp", ".nz-coder/mcp.json"),
)
MAX_CONTROL_FILE_BYTES = 1024 * 1024
MAX_CONTROL_TOTAL_BYTES = 4 * 1024 * 1024
MAX_CONTROL_FILES = 1024
MAX_CONTROL_DIRECTORY_ENTRIES = 4096


class UnsafeProjectControl(ValueError):
    """Raised when Project Authority cannot be captured without path races."""


@dataclass(frozen=True)
class TrustedControlFile:
    """One immutable Project Control file read from a verified open handle."""

    kind: str
    relative_path: str
    content: bytes
    sha256: str
    device: int | None
    inode: int | None
    size: int


@dataclass(frozen=True)
class ProjectControlSnapshot:
    """Run-pinnable Project Authority whose trust is bound to captured bytes."""

    workspace_identity: Mapping[str, object]
    fingerprint: str
    files: Mapping[str, TrustedControlFile]
    total_bytes: int
    trusted: bool = False

    def files_for_kind(self, kind: str) -> tuple[TrustedControlFile, ...]:
        return tuple(item for item in self.files.values() if item.kind == str(kind))

    def get(self, relative_path: str | Path) -> TrustedControlFile | None:
        key = Path(relative_path).as_posix()
        if key.startswith("./"):
            key = key[2:]
        return self.files.get(key)

    def public(self) -> dict[str, object]:
        """Return metadata only; captured bytes never cross public boundaries."""
        return {
            "workspace_identity": dict(self.workspace_identity),
            "fingerprint": self.fingerprint,
            "trusted": self.trusted,
            "total_bytes": self.total_bytes,
            "files": [
                {
                    "kind": item.kind,
                    "relative_path": item.relative_path,
                    "sha256": item.sha256,
                    "size": item.size,
                }
                for item in self.files.values()
            ],
        }


def capture_project_control_snapshot(
    workspace: Path | str,
    *,
    workspace_config_fingerprint: str | None = None,
    kinds: Iterable[str] = CONTROL_FILE_KINDS,
) -> ProjectControlSnapshot:
    """Capture selected Project Control bytes from verified open handles."""
    root = Path(workspace).expanduser().absolute()
    selected = _normalize_kinds(kinds)
    config_fingerprint = (
        workspace_config_fingerprint
        if workspace_config_fingerprint is not None
        else hashlib.sha256(b"{}").hexdigest()
    )
    if os.name == "nt":
        identity, records = _capture_windows(root, selected)
    else:
        identity, records = _capture_posix(root, selected)
    ordered = dict(sorted(records.items()))
    total = sum(item.size for item in ordered.values())
    if len(ordered) > MAX_CONTROL_FILES or total > MAX_CONTROL_TOTAL_BYTES:
        raise UnsafeProjectControl("project control plane exceeds safety limits")
    digest = hashlib.sha256()
    digest.update(b"workspace-config\0")
    digest.update(config_fingerprint.encode("ascii"))
    for relative, item in ordered.items():
        digest.update(b"\0path\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0content\0")
        digest.update(item.content)
    return ProjectControlSnapshot(
        workspace_identity=MappingProxyType(dict(identity)),
        fingerprint=digest.hexdigest(),
        files=MappingProxyType(ordered),
        total_bytes=total,
    )


def discover_project_control_files(
    workspace: Path | str,
    *,
    kinds: tuple[str, ...] = CONTROL_FILE_KINDS,
) -> tuple[Path, ...]:
    """Return metadata paths represented by a safe capture."""
    root = Path(workspace).expanduser().absolute()
    snapshot = capture_project_control_snapshot(root, kinds=kinds)
    return tuple(root / relative for relative in snapshot.files)


def has_project_control_files(workspace: Path | str) -> bool:
    """Return whether a safe capture contains active Project Control."""
    return bool(capture_project_control_snapshot(workspace).files)


def _normalize_kinds(kinds: Iterable[str]) -> frozenset[str]:
    selected = set(str(kind) for kind in kinds)
    allowed = set(CONTROL_FILE_KINDS) | set(_SNAPSHOT_KINDS)
    unknown = selected - allowed
    if unknown:
        raise ValueError(f"unknown project control kind: {sorted(unknown)[0]}")
    if "fixed" in selected:
        selected.update(("settings", "mcp"))
        selected.remove("fixed")
    return frozenset(selected)


def _record(
    records: dict[str, TrustedControlFile],
    *,
    kind: str,
    relative: str,
    payload: bytes,
    device: int | None,
    inode: int | None,
) -> None:
    if len(records) >= MAX_CONTROL_FILES:
        raise UnsafeProjectControl("project control plane exceeds file limit")
    total = sum(item.size for item in records.values()) + len(payload)
    if total > MAX_CONTROL_TOTAL_BYTES:
        raise UnsafeProjectControl("project control plane exceeds byte limit")
    records[relative] = TrustedControlFile(
        kind=kind,
        relative_path=relative,
        content=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        device=device,
        inode=inode,
        size=len(payload),
    )


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_posix_directory(
    name: str | Path,
    *,
    dir_fd: int | None = None,
    missing_ok: bool = False,
) -> int | None:
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=dir_fd)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    except OSError as exc:
        raise UnsafeProjectControl("project control directory is unsafe") from exc
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        os.close(descriptor)
        raise UnsafeProjectControl("project control directory is unsafe")
    return descriptor


def _read_posix_file(
    parent_fd: int,
    name: str,
    *,
    missing_ok: bool = False,
) -> tuple[bytes, os.stat_result] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    except OSError as exc:
        raise UnsafeProjectControl("project control file is unsafe") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise UnsafeProjectControl("project control file must be regular")
        if before.st_size > MAX_CONTROL_FILE_BYTES:
            raise UnsafeProjectControl("project control file exceeds byte limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = MAX_CONTROL_FILE_BYTES + 1 - total
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_CONTROL_FILE_BYTES:
                raise UnsafeProjectControl("project control file exceeds byte limit")
        after = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or total != before.st_size
        ):
            raise UnsafeProjectControl("project control file changed during capture")
        return b"".join(chunks), before
    finally:
        os.close(descriptor)


def _bounded_scandir(descriptor: int):  # noqa: ANN202
    try:
        iterator = os.scandir(descriptor)
    except OSError as exc:
        raise UnsafeProjectControl("project control directory cannot be read") from exc
    with iterator:
        for index, entry in enumerate(iterator, start=1):
            if index > MAX_CONTROL_DIRECTORY_ENTRIES:
                raise UnsafeProjectControl("project control directory exceeds entry limit")
            yield entry.name


def _capture_posix(
    root: Path,
    selected: frozenset[str],
) -> tuple[dict[str, object], dict[str, TrustedControlFile]]:
    root_fd = _open_posix_directory(root)
    assert root_fd is not None
    control_fd: int | None = None
    try:
        root_info = os.fstat(root_fd)
        try:
            current = root.lstat()
        except OSError as exc:
            raise UnsafeProjectControl("workspace identity cannot be verified") from exc
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (root_info.st_dev, root_info.st_ino)
        ):
            raise UnsafeProjectControl("workspace identity changed during capture")
        identity = {
            "lexical": os.path.normcase(os.path.normpath(str(root))),
            "resolved": os.path.normcase(os.path.normpath(str(root.resolve(strict=True)))),
            "device": int(root_info.st_dev),
            "inode": int(root_info.st_ino),
        }
        records: dict[str, TrustedControlFile] = {}
        control_fd = _open_posix_directory(
            ".nz-coder", dir_fd=root_fd, missing_ok=True
        )
        if control_fd is None:
            return identity, records
        for kind, relative in _FIXED_CONTROL_PATHS:
            if kind not in selected:
                continue
            result = _read_posix_file(
                control_fd, Path(relative).name, missing_ok=True
            )
            if result is not None:
                payload, info = result
                _record(records, kind=kind, relative=relative, payload=payload,
                        device=int(info.st_dev), inode=int(info.st_ino))
        if "command" in selected:
            _capture_posix_flat(control_fd, records, "command", "commands", ".md")
        if "workflow" in selected:
            _capture_posix_flat(
                control_fd, records, "workflow", "workflows", ".workflow.json"
            )
        if "skill" in selected:
            _capture_posix_skills(control_fd, records)
        return identity, records
    finally:
        if control_fd is not None:
            os.close(control_fd)
        os.close(root_fd)


def _capture_posix_flat(
    control_fd: int,
    records: dict[str, TrustedControlFile],
    kind: str,
    directory_name: str,
    suffix: str,
) -> None:
    directory_fd = _open_posix_directory(
        directory_name, dir_fd=control_fd, missing_ok=True
    )
    if directory_fd is None:
        return
    try:
        for name in _bounded_scandir(directory_fd):
            if not name.endswith(suffix):
                continue
            result = _read_posix_file(directory_fd, name)
            assert result is not None
            payload, info = result
            _record(records, kind=kind,
                    relative=f".nz-coder/{directory_name}/{name}", payload=payload,
                    device=int(info.st_dev), inode=int(info.st_ino))
    finally:
        os.close(directory_fd)


def _capture_posix_skills(
    control_fd: int,
    records: dict[str, TrustedControlFile],
) -> None:
    skills_fd = _open_posix_directory("skills", dir_fd=control_fd, missing_ok=True)
    if skills_fd is None:
        return
    try:
        for name in _bounded_scandir(skills_fd):
            skill_fd = _open_posix_directory(name, dir_fd=skills_fd)
            assert skill_fd is not None
            try:
                result = _read_posix_file(skill_fd, "SKILL.md", missing_ok=True)
                if result is None:
                    continue
                payload, info = result
                _record(records, kind="skill",
                        relative=f".nz-coder/skills/{name}/SKILL.md", payload=payload,
                        device=int(info.st_dev), inode=int(info.st_ino))
            finally:
                os.close(skill_fd)
    finally:
        os.close(skills_fd)


def _capture_windows(
    root: Path,
    selected: frozenset[str],
) -> tuple[dict[str, object], dict[str, TrustedControlFile]]:
    """Capture on Windows while directory handles deny rename/delete sharing."""
    import ntpath

    root_handle = _windows_open(root, directory=True)
    assert root_handle is not None
    control_handle: int | None = None
    try:
        root_attributes, device, inode, _size = _windows_handle_info(
            root_handle, full=True
        )
        if root_attributes & 0x00000400:
            raise UnsafeProjectControl("workspace path is unsafe")
        root_final = _windows_final_path(root_handle)
        identity = {
            "lexical": os.path.normcase(os.path.normpath(str(root))),
            "resolved": os.path.normcase(os.path.normpath(root_final)),
            "device": device,
            "inode": inode,
        }
        records: dict[str, TrustedControlFile] = {}
        control_path = root / ".nz-coder"
        control_handle = _windows_open(
            control_path, directory=True, missing_ok=True, parent=root_handle
        )
        if control_handle is None:
            return identity, records
        for kind, relative in _FIXED_CONTROL_PATHS:
            if kind not in selected:
                continue
            result = _windows_read_file(
                control_path / Path(relative).name,
                parent=control_handle,
                missing_ok=True,
            )
            if result is not None:
                payload, file_device, file_inode = result
                _record(records, kind=kind, relative=relative, payload=payload,
                        device=file_device, inode=file_inode)
        if "command" in selected:
            _capture_windows_flat(
                control_path, control_handle, records, "command", "commands", ".md"
            )
        if "workflow" in selected:
            _capture_windows_flat(
                control_path, control_handle, records, "workflow", "workflows",
                ".workflow.json",
            )
        if "skill" in selected:
            _capture_windows_skills(control_path, control_handle, records)
        if ntpath.normcase(_windows_final_path(root_handle)) != ntpath.normcase(root_final):
            raise UnsafeProjectControl("workspace identity changed during capture")
        return identity, records
    finally:
        if control_handle is not None:
            _windows_close(control_handle)
        _windows_close(root_handle)


def _capture_windows_flat(
    control_path: Path,
    control_handle: int,
    records: dict[str, TrustedControlFile],
    kind: str,
    directory_name: str,
    suffix: str,
) -> None:
    directory_path = control_path / directory_name
    directory_handle = _windows_open(
        directory_path, directory=True, missing_ok=True, parent=control_handle
    )
    if directory_handle is None:
        return
    try:
        for name in _bounded_windows_names(directory_path):
            if not name.endswith(suffix):
                continue
            result = _windows_read_file(directory_path / name, parent=directory_handle)
            assert result is not None
            payload, device, inode = result
            _record(records, kind=kind,
                    relative=f".nz-coder/{directory_name}/{name}", payload=payload,
                    device=device, inode=inode)
    finally:
        _windows_close(directory_handle)


def _capture_windows_skills(
    control_path: Path,
    control_handle: int,
    records: dict[str, TrustedControlFile],
) -> None:
    skills_path = control_path / "skills"
    skills_handle = _windows_open(
        skills_path, directory=True, missing_ok=True, parent=control_handle
    )
    if skills_handle is None:
        return
    try:
        for name in _bounded_windows_names(skills_path):
            skill_path = skills_path / name
            skill_handle = _windows_open(
                skill_path, directory=True, parent=skills_handle
            )
            assert skill_handle is not None
            try:
                result = _windows_read_file(
                    skill_path / "SKILL.md", parent=skill_handle, missing_ok=True
                )
                if result is None:
                    continue
                payload, device, inode = result
                _record(records, kind="skill",
                        relative=f".nz-coder/skills/{name}/SKILL.md", payload=payload,
                        device=device, inode=inode)
            finally:
                _windows_close(skill_handle)
    finally:
        _windows_close(skills_handle)


def _bounded_windows_names(directory: Path):  # noqa: ANN202
    try:
        iterator = os.scandir(directory)
    except OSError as exc:
        raise UnsafeProjectControl("project control directory cannot be read") from exc
    with iterator:
        for index, entry in enumerate(iterator, start=1):
            if index > MAX_CONTROL_DIRECTORY_ENTRIES:
                raise UnsafeProjectControl("project control directory exceeds entry limit")
            yield entry.name


def _windows_open(
    path: Path,
    *,
    directory: bool,
    missing_ok: bool = False,
    parent: int | None = None,
) -> int | None:
    import ctypes
    from ctypes import wintypes
    import ntpath

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    access = 0x00000080 | (0x00000001 if directory else 0x80000000)
    flags = 0x00200000 | (0x02000000 if directory else 0x00000080)
    handle = create_file(
        str(path), access, 0x00000001 | 0x00000002, None, 3, flags, None
    )
    value = int(getattr(handle, "value", handle)) if handle is not None else -1
    invalid = ctypes.c_void_p(-1).value
    if value == invalid:
        error = ctypes.get_last_error()
        if missing_ok and error in {2, 3}:
            return None
        raise UnsafeProjectControl("project control path cannot be opened") from OSError(error)
    try:
        attributes, _device, _inode, _size = _windows_handle_info(value, full=True)
        if attributes & 0x00000400:
            raise UnsafeProjectControl("project control path is unsafe")
        if bool(attributes & 0x00000010) != directory:
            raise UnsafeProjectControl("project control path has an invalid type")
        if parent is not None:
            child_path = _windows_final_path(value)
            parent_path = _windows_final_path(parent)
            if ntpath.normcase(ntpath.dirname(child_path)) != ntpath.normcase(parent_path):
                raise UnsafeProjectControl("project control parent identity changed")
        return value
    except Exception:
        _windows_close(value)
        raise


def _windows_handle_info(
    handle: int,
    *,
    full: bool = False,
) -> tuple[int, int] | tuple[int, int, int, int]:
    import ctypes
    from ctypes import wintypes

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = (
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        )

    info = _ByHandleFileInformation()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    query = kernel32.GetFileInformationByHandle
    query.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation))
    query.restype = wintypes.BOOL
    if not query(wintypes.HANDLE(handle), ctypes.byref(info)):
        raise UnsafeProjectControl("project control handle cannot be inspected")
    device = int(info.volume_serial_number)
    inode = (int(info.file_index_high) << 32) | int(info.file_index_low)
    size = (int(info.file_size_high) << 32) | int(info.file_size_low)
    if full:
        return int(info.file_attributes), device, inode, size
    return device, inode


def _windows_read_file(
    path: Path,
    *,
    parent: int,
    missing_ok: bool = False,
) -> tuple[bytes, int, int] | None:
    import ctypes
    from ctypes import wintypes

    handle = _windows_open(path, directory=False, missing_ok=missing_ok, parent=parent)
    if handle is None:
        return None
    try:
        attributes, device, inode, size = _windows_handle_info(handle, full=True)
        if attributes & 0x00000400 or size > MAX_CONTROL_FILE_BYTES:
            raise UnsafeProjectControl("project control file is unsafe")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        read_file = kernel32.ReadFile
        read_file.argtypes = (
            wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
        )
        read_file.restype = wintypes.BOOL
        chunks: list[bytes] = []
        total = 0
        while total < size:
            count = min(64 * 1024, size - total)
            buffer = ctypes.create_string_buffer(count)
            read = wintypes.DWORD()
            if not read_file(
                wintypes.HANDLE(handle), buffer, count, ctypes.byref(read), None
            ):
                raise UnsafeProjectControl("project control file cannot be read")
            if not read.value:
                break
            chunks.append(buffer.raw[:read.value])
            total += int(read.value)
        if total != size:
            raise UnsafeProjectControl("project control file changed during capture")
        after = _windows_handle_info(handle, full=True)
        if after[1:] != (device, inode, size):
            raise UnsafeProjectControl("project control file changed during capture")
        return b"".join(chunks), device, inode
    finally:
        _windows_close(handle)


def _windows_final_path(handle: int) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    final_path = kernel32.GetFinalPathNameByHandleW
    final_path.argtypes = (
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
    )
    final_path.restype = wintypes.DWORD
    required = final_path(wintypes.HANDLE(handle), None, 0, 0)
    if not required:
        raise UnsafeProjectControl("project control handle path cannot be verified")
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = final_path(wintypes.HANDLE(handle), buffer, len(buffer), 0)
    if not written or written >= len(buffer):
        raise UnsafeProjectControl("project control handle path cannot be verified")
    value = buffer.value
    return value[4:] if value.startswith("\\\\?\\") else value


def _windows_close(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    close_handle(wintypes.HANDLE(handle))


__all__ = [
    "CONTROL_FILE_KINDS",
    "MAX_CONTROL_DIRECTORY_ENTRIES",
    "MAX_CONTROL_FILE_BYTES",
    "MAX_CONTROL_FILES",
    "MAX_CONTROL_TOTAL_BYTES",
    "ProjectControlSnapshot",
    "TrustedControlFile",
    "UnsafeProjectControl",
    "capture_project_control_snapshot",
    "discover_project_control_files",
    "has_project_control_files",
]
