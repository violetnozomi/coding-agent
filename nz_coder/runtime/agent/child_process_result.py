"""Bounded private JSON files for one isolated child invocation's result."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import uuid

from nz_coder.foundation.json_safety import reject_nonstandard_json_constant
from nz_coder.foundation.private_paths import harden_private_path, inspect_private_path
from nz_coder.protocol.public_error import PublicError, public_error_from_wire
from nz_coder.runtime.agent.child_result import ChildAgentResult


CHILD_PROCESS_RESULT_SCHEMA = "nz.child_result.v1"
MAX_RESULT_CHARACTERS = 2_000_000
MAX_ENVELOPE_BYTES = 16 * 1024 * 1024


def _directory_identity(handle: int) -> tuple[int, int]:
    if os.name == "nt":
        from nz_coder.foundation.project_control import _windows_handle_info

        return _windows_handle_info(handle)
    info = os.fstat(handle)
    return info.st_dev, info.st_ino


@dataclass(frozen=True)
class ChildProcessResultFile:
    """本次启动的路径和分配时的目录身份，一起传给子方，不能只凭路径重认归属。"""

    path: Path
    directory_identity: tuple[int, int]

    @classmethod
    def capture(cls, path: Path) -> ChildProcessResultFile:
        path = Path(path).absolute()
        with _result_parent(path) as parent:
            return cls(path, _directory_identity(parent))


def allocate_child_process_result(workspace: Path) -> ChildProcessResultFile:
    """Allocate a fresh parent-owned invocation; task input never chooses its path."""
    from nz_coder.foundation.user_paths import prepare_user_storage

    root = prepare_user_storage(workspace).workspace_state / "child-process-results"
    try:
        info = root.lstat()
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Unsafe child process result directory")
    name = uuid.uuid4().hex
    with _result_parent(root) as parent:
        if os.name == "nt":
            from nz_coder.foundation.project_control import _windows_close, _windows_open

            root.mkdir(mode=0o700, exist_ok=True)
            root_handle = _windows_open(root, directory=True, parent=parent)
            try:
                if not harden_private_path(root).hardened:
                    raise ValueError("Child process result directory cannot be made private")
                invocation = root / name
                invocation.mkdir(mode=0o700)
                handle = _windows_open(invocation, directory=True, parent=root_handle)
                try:
                    if not harden_private_path(invocation).hardened:
                        raise ValueError("Child invocation directory cannot be made private")
                    identity = _directory_identity(handle)
                finally:
                    _windows_close(handle)
            finally:
                _windows_close(root_handle)
        else:
            try:
                os.mkdir(root.name, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            descriptor = os.open(root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            try:
                info = os.fstat(descriptor)
                if info.st_mode & 0o077 or info.st_uid != os.geteuid():
                    raise ValueError("Child process result directory is not private")
                os.mkdir(name, 0o700, dir_fd=descriptor)
                invocation = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
                try:
                    identity = _directory_identity(invocation)
                finally:
                    os.close(invocation)
            finally:
                os.close(descriptor)
    return ChildProcessResultFile(root / name / "result.json", identity)


def discard_child_process_result(result_file: ChildProcessResultFile) -> None:
    """Remove only flat transport files, preserving unrelated directory contents."""
    path = result_file.path
    with _result_parent(path, result_file.directory_identity) as parent:
        names = os.listdir(path.parent if os.name == "nt" else parent)
        for name in names:
            if name != path.name and not (name.startswith(f".{path.name}.") and name.endswith(".tmp")):
                continue
            if os.name == "nt":
                (path.parent / name).unlink()
            else:
                os.unlink(name, dir_fd=parent)
    with _result_parent(path.parent) as parent:
        if os.name == "nt":
            from nz_coder.foundation.project_control import _windows_close, _windows_open

            handle = _windows_open(path.parent, directory=True, parent=parent)
            try:
                if _directory_identity(handle) != result_file.directory_identity:
                    raise ValueError("Child invocation directory identity changed")
            finally:
                _windows_close(handle)
            path.parent.rmdir()
        else:
            info = os.stat(path.parent.name, dir_fd=parent, follow_symlinks=False)
            if (info.st_dev, info.st_ino) != result_file.directory_identity:
                raise ValueError("Child invocation directory identity changed")
            os.rmdir(path.parent.name, dir_fd=parent)


def _validate_envelope(envelope: object) -> dict:
    if (
        not isinstance(envelope, dict)
        or envelope.get("schema") != CHILD_PROCESS_RESULT_SCHEMA
        or not isinstance(envelope.get("ok"), bool)
    ):
        raise ValueError("Invalid child process result envelope")
    if envelope["ok"]:
        result = envelope.get("result")
        if not isinstance(result, str) or len(result) > MAX_RESULT_CHARACTERS:
            raise ValueError("Invalid or oversized child process result")
    elif public_error_from_wire(envelope.get("public_error")) is None:
        raise ValueError("Invalid child process public error")
    if "child_result" in envelope:
        if (
            not envelope["ok"]
            or not isinstance(envelope["child_result"], dict)
            or not isinstance(envelope.get("child_result_settlement_id"), str)
            or not envelope["child_result_settlement_id"]
        ):
            raise ValueError("Invalid canonical child process result")
        envelope = {**envelope, "child_result": ChildAgentResult.from_dict(envelope["child_result"]).to_dict()}
    return envelope


def decode_child_process_result(envelope: object) -> tuple[str, PublicError | None]:
    """Decode the existing child contract without exposing private failures."""
    try:
        validated = _validate_envelope(envelope)
    except ValueError:
        error = PublicError("child_process_protocol_error", "The child process returned an invalid result.")
        return error.message, error
    if validated["ok"]:
        return validated["result"], None
    error = public_error_from_wire(validated["public_error"])
    assert error is not None
    return error.message, error


@contextmanager
def _result_parent(path: Path, expected_identity: tuple[int, int] | None = None):
    """父方绑定路径；逐级固定目录，打开时拒绝跟随目录别名。"""
    if ".." in path.parts:
        raise ValueError("Unsafe child process result path")
    parent = path.parent
    if os.name == "nt":
        from nz_coder.foundation.project_control import _windows_close, _windows_open

        handles = []
        try:
            cursor = Path(parent.anchor)
            handle = _windows_open(cursor, directory=True)
            handles.append(handle)
            for part in parent.parts[1:]:
                cursor /= part
                handle = _windows_open(cursor, directory=True, parent=handle)
                handles.append(handle)
            if not inspect_private_path(parent).hardened:
                raise ValueError("Child process result directory is not private")
            if expected_identity is not None and _directory_identity(handle) != expected_identity:
                raise ValueError("Child invocation directory identity changed")
            yield handle
        finally:
            for handle in reversed(handles):
                _windows_close(handle)
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(parent.anchor, flags)
    try:
        for part in parent.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise ValueError("Child process result directory is not private")
        if expected_identity is not None and (info.st_dev, info.st_ino) != expected_identity:
            raise ValueError("Child invocation directory identity changed")
        yield descriptor
    finally:
        os.close(descriptor)


def _validate_file(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Child process result is not an unaliased regular file")
    if os.name != "nt" and (info.st_mode & 0o077 or info.st_uid != os.geteuid()):
        raise ValueError("Child process result file is not private")
    if info.st_size > MAX_ENVELOPE_BYTES:
        raise ValueError("Child process result exceeds the maximum envelope size")


def _open_result(path: Path, parent: int) -> int:
    if os.name == "nt":
        import msvcrt
        from nz_coder.foundation.project_control import _windows_close, _windows_open

        handle = _windows_open(path, directory=False, parent=parent, missing_ok=True)
        if handle is None:
            raise FileNotFoundError("Child process result does not exist")
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            _windows_close(handle)
            raise
        try:
            if not inspect_private_path(path).hardened:
                raise ValueError("Child process result file is not private")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    # 非阻塞只影响特殊文件；普通文件照常读取，FIFO 则可立即由 fstat 拒绝。
    return os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)


def read_child_process_result(result_file: ChildProcessResultFile) -> dict:
    """Read one fixed parent-owned path using the same validated file handle."""
    path = result_file.path
    with _result_parent(path, result_file.directory_identity) as parent:
        descriptor = _open_result(path, parent)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            _validate_file(before)
            encoded = stream.read(MAX_ENVELOPE_BYTES + 1)
            after = os.fstat(stream.fileno())
            _validate_file(after)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError("Child process result changed during reading")
    if len(encoded) > MAX_ENVELOPE_BYTES:
        raise ValueError("Child process result exceeds the maximum envelope size")
    try:
        envelope = json.loads(encoded.decode("utf-8"), parse_constant=reject_nonstandard_json_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError("Invalid child process result JSON") from exc
    return _validate_envelope(envelope)


def write_child_process_result(result_file: ChildProcessResultFile, envelope: dict) -> None:
    """Atomically replace a bounded JSON envelope inside its private directory."""
    envelope = _validate_envelope(envelope)
    encoded = json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_ENVELOPE_BYTES:
        raise ValueError("Child process result exceeds the maximum envelope size")
    path = result_file.path
    temporary = f".{path.name}.{uuid.uuid4().hex}.tmp"
    with _result_parent(path, result_file.directory_identity) as parent:
        # 拒绝已有别名；原子替换本身只操作目录项，不跟随目标链接。
        try:
            existing = _open_result(path, parent)
        except FileNotFoundError:
            pass
        else:
            try:
                _validate_file(os.fstat(existing))
            finally:
                os.close(existing)
        try:
            if os.name == "nt":
                descriptor = os.open(path.parent / temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY, 0o600)
            else:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
            with os.fdopen(descriptor, "wb") as stream:
                if os.name == "nt" and not harden_private_path(path.parent / temporary).hardened:
                    raise ValueError("Child process result file cannot be made private")
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            if os.name == "nt":
                os.replace(path.parent / temporary, path)
            else:
                os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                if os.name == "nt":
                    (path.parent / temporary).unlink()
                else:
                    os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
