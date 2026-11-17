"""完整范围证明；与允许省略文件的回退快照分开存储和校验。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path

from .workspace_snapshot import SnapshotError


_PRIVATE_NAMES = {".git", ".hg", ".svn", ".nz-coder", ".nz-coder-runs"}
_REFERENCE = re.compile(r"scope-[0-9a-f]{64}\Z")


def _identity(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "scope-" + hashlib.sha256(encoded).hexdigest()


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def capture(store, *, cancel_event=None, timeout_seconds: float = 1.0) -> str:
    from nz_coder.runtime.process.execution_environment import current_execution_environment
    environment = current_execution_environment()
    resource_exclusions = tuple(environment.excluded) if environment else ()
    deadline = time.monotonic() + timeout_seconds

    def check():
        if cancel_event is not None and cancel_event.is_set():
            raise SnapshotError("workspace scope capture cancelled")
        if time.monotonic() >= deadline:
            raise SnapshotError("workspace scope capture deadline exceeded")

    entries = {}
    signatures = {}
    total = 0
    store_root = store.root.resolve()
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
        file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    except AttributeError as exc:
        # 不支持无跟随打开的宿主仍可执行工具，但不能发布完整范围证明。
        raise SnapshotError("strict workspace scope capture is unavailable on this platform") from exc

    def walk(directory: Path):
        nonlocal total
        check()
        fd = os.open(directory, directory_flags)
        try:
            initial = os.fstat(fd)
            with os.scandir(fd) as children:
                for child in children:
                    check()
                    path = directory / child.name
                    if child.name in _PRIVATE_NAMES or path == store_root or any(path == Path(p) or path.is_relative_to(Path(p)) for p in resource_exclusions):
                        continue
                    rel = path.relative_to(store.workspace).as_posix()
                    if len(entries) >= store.max_files:
                        raise SnapshotError("workspace scope entry limit exceeded: " + rel)
                    info = child.stat(follow_symlinks=False)
                    mode = stat.S_IMODE(info.st_mode)
                    if stat.S_ISDIR(info.st_mode):
                        entries[rel] = {"kind": "directory", "mode": mode}
                        walk(path)
                    elif stat.S_ISREG(info.st_mode):
                        if info.st_size > store.max_file_bytes:
                            raise SnapshotError("workspace scope file byte limit exceeded: " + rel)
                        digest = hashlib.sha256()
                        size = 0
                        file_fd = os.open(child.name, file_flags, dir_fd=fd)
                        try:
                            if _signature(os.fstat(file_fd)) != _signature(info):
                                raise SnapshotError("workspace scope file changed before read: " + rel)
                            while True:
                                check()
                                chunk = os.read(file_fd, 65536)
                                if not chunk:
                                    break
                                size += len(chunk)
                                total += len(chunk)
                                if size > store.max_file_bytes or total > store.max_total_bytes:
                                    raise SnapshotError("workspace scope byte limit exceeded: " + rel)
                                digest.update(chunk)
                            if _signature(os.fstat(file_fd)) != _signature(info) or size != info.st_size:
                                raise SnapshotError("workspace scope file changed during read: " + rel)
                        finally:
                            os.close(file_fd)
                        entries[rel] = {"kind": "file", "mode": mode,
                                        "size": size, "digest": digest.hexdigest()}
                    elif stat.S_ISLNK(info.st_mode):
                        # 链接本身属于工作区；目标可在外部或不存在，不能跟随读取。
                        target = os.fsencode(os.readlink(child.name, dir_fd=fd))
                        if _signature(os.stat(child.name, dir_fd=fd, follow_symlinks=False)) != _signature(info):
                            raise SnapshotError("workspace scope link changed during read: " + rel)
                        entries[rel] = {"kind": "symlink", "mode": mode,
                                        "size": len(target), "digest": hashlib.sha256(target).hexdigest()}
                    else:
                        raise SnapshotError("workspace scope contains a special file: " + rel)
                    signatures[path] = _signature(info)
            if _signature(os.fstat(fd)) != _signature(initial):
                raise SnapshotError("workspace scope directory changed during scan: " + directory.relative_to(store.workspace).as_posix())
            signatures[directory] = _signature(initial)
        finally:
            os.close(fd)

    try:
        check()
        walk(store.workspace)
        # 检查早先读过的文件，不能把扫描期间的前后版本拼成一个证明。
        for path, signature in signatures.items():
            check()
            if _signature(path.lstat()) != signature:
                raise SnapshotError("workspace scope changed during scan: " + path.relative_to(store.workspace).as_posix())
        payload = {"purpose": "verification_scope", "version": 1,
                   "workspace": str(store.workspace), "files": entries,
                   "root_mode": stat.S_IMODE(signatures[store.workspace][2]),
                   "resource_exclusions": list(resource_exclusions)}
        reference = _identity(payload)
        check()
        store._atomic_json(store.root / "scope" / (reference + ".json"), payload)
        check()
        return reference
    except (OSError, NotImplementedError, RecursionError) as exc:
        # 系统异常可能带绝对路径；公开诊断只保留异常类型及工作区内路径。
        location = ""
        if isinstance(exc, OSError) and exc.filename:
            candidate = Path(exc.filename)
            if not candidate.is_absolute():
                location = ": " + candidate.as_posix()
            elif candidate.is_relative_to(store.workspace):
                location = ": " + candidate.relative_to(store.workspace).as_posix()
        raise SnapshotError("workspace scope capture incomplete (" + type(exc).__name__ + ")" + location) from exc


def load(store, reference: str) -> dict:
    if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference):
        raise SnapshotError("invalid workspace scope reference")
    try:
        payload = json.loads((store.root / "scope" / (reference + ".json")).read_text())
        if (not isinstance(payload, dict) or _identity(payload) != reference
                or payload.get("purpose") != "verification_scope" or payload.get("version") != 1
                or payload.get("workspace") != str(store.workspace)
                or not isinstance(payload.get("files"), dict)):
            raise SnapshotError("workspace scope identity mismatch")
        return {**payload["files"], ".": {"kind": "directory", "mode": payload["root_mode"]}}
    except (OSError, ValueError) as exc:
        raise SnapshotError("workspace scope reference unreadable") from exc


def changes(store, source: str, destination: str) -> dict[str, str]:
    left, right = load(store, source), load(store, destination)
    scope_dir = store.root / 'scope'
    a = json.loads((scope_dir / (source + '.json')).read_text())
    b = json.loads((scope_dir / (destination + '.json')).read_text())
    if a.get('resource_exclusions', []) != b.get('resource_exclusions', []):
        raise SnapshotError('workspace environment scope changed; fresh verification baseline required')
    return {path: "create" if path not in left else "delete" if path not in right else "replace"
            for path in sorted(left.keys() | right.keys()) if left.get(path) != right.get(path)}
