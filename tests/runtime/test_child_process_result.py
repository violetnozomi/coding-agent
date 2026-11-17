"""Child-process result files preserve data without following filesystem aliases."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import stat
import sys
import threading
from types import SimpleNamespace

import pytest


def _codec():
    return importlib.import_module("nz_coder.runtime.agent.child_process_result")


def _path(tmp_path):
    directory = tmp_path / "invocation"
    directory.mkdir(mode=0o700)
    if os.name == "nt":
        from nz_coder.foundation.private_paths import harden_private_path

        assert harden_private_path(directory).hardened
    return directory / "result.json"


def _success(text):
    return {"schema": "nz.child_result.v1", "ok": True, "result": text}


def _bounded_read(codec, path):
    """A broken FIFO reader must fail the test without leaving a blocked thread."""
    values = []
    errors = []
    keeper = os.open(path, os.O_RDWR | os.O_NONBLOCK) if stat.S_ISFIFO(path.lstat().st_mode) else None

    def read():
        try:
            values.append(codec.read_child_process_result(codec.ChildProcessResultFile.capture(path)))
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    try:
        thread.join(0.5)
        assert not thread.is_alive(), "result file reader blocked on a non-regular file"
    finally:
        if keeper is not None:
            os.close(keeper)
        # Even a blocking open introduced by a regression can be released here.
        if thread.is_alive() and path.exists() and stat.S_ISFIFO(path.stat().st_mode):
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
                os.close(descriptor)
            except OSError:
                pass
        thread.join(1)
        assert not thread.is_alive(), "test reader was not cleaned up"
    return values, errors


@pytest.mark.parametrize("text", ["x" * (512 * 1024), "中😀" * 1_000_000, "\x00" * 2_000_000], ids=["ascii-512k", "unicode-2m", "controls-2m"])
def test_large_unicode_result_round_trips(tmp_path, text):
    codec = _codec()
    path = _path(tmp_path)
    codec.write_child_process_result(codec.ChildProcessResultFile.capture(path), _success(text))
    assert codec.read_child_process_result(codec.ChildProcessResultFile.capture(path)) == _success(text)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_result_limits_bound_characters_and_whole_envelope(tmp_path):
    codec = _codec()
    path = _path(tmp_path)
    with pytest.raises(ValueError):
        codec.write_child_process_result(codec.ChildProcessResultFile.capture(path), _success("x" * 2_000_001))
    with pytest.raises(ValueError):
        codec.write_child_process_result(codec.ChildProcessResultFile.capture(path), {**_success("ok"), "padding": "x" * (16 * 1024 * 1024)})
    path.write_bytes(b" " * (16 * 1024 * 1024 + 1))
    path.chmod(0o600)
    with pytest.raises(ValueError):
        codec.read_child_process_result(codec.ChildProcessResultFile.capture(path))


@pytest.mark.parametrize("payload", [b'{"schema":', b'{"schema":"nz.child_result.v1","ok":true,"result":NaN}', b'{"schema":"wrong","ok":true,"result":"ok"}', b'{"schema":"nz.child_result.v1","ok":true,"result":1}'])
def test_rejects_invalid_result_json(tmp_path, payload):
    codec = _codec()
    path = _path(tmp_path)
    path.write_bytes(payload)
    path.chmod(0o600)
    with pytest.raises(ValueError):
        codec.read_child_process_result(codec.ChildProcessResultFile.capture(path))


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo"])
def test_rejects_invalid_and_aliased_result_without_blocking(tmp_path, kind):
    codec = _codec()
    path = _path(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_success("outside")))
    outside.chmod(0o600)
    if kind == "symlink":
        try:
            path.symlink_to(outside)
        except OSError:
            pytest.skip("symlink creation unavailable")
    elif kind == "hardlink":
        os.link(outside, path)
    elif kind == "directory":
        path.mkdir(mode=0o700)
    elif hasattr(os, "mkfifo"):
        os.mkfifo(path, 0o600)
    else:
        pytest.skip("POSIX FIFO")
    if kind == "fifo":
        values, errors = _bounded_read(codec, path)
        assert values == [] and len(errors) == 1
    else:
        with pytest.raises((ValueError, OSError)):
            codec.read_child_process_result(codec.ChildProcessResultFile.capture(path))
    assert json.loads(outside.read_text())["result"] == "outside"


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor-relative race")
@pytest.mark.parametrize("replacement", ["fifo", "symlink"])
def test_file_replacement_after_check_cannot_redirect_or_block_read(tmp_path, monkeypatch, replacement):
    codec = _codec()
    path = _path(tmp_path)
    codec.write_child_process_result(codec.ChildProcessResultFile.capture(path), _success("original"))
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_success("outside")))
    original_open = os.open
    changed = False

    def replace_before_open(name, flags, mode=0o777, *, dir_fd=None):
        nonlocal changed
        if Path(name).name == "result.json" and not changed:
            changed = True
            path.unlink()
            if replacement == "fifo":
                os.mkfifo(path, 0o600)
            else:
                path.symlink_to(outside)
        return original_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replace_before_open)
    values, errors = _bounded_read(codec, path)
    assert changed and not values and len(errors) == 1
    assert json.loads(outside.read_text())["result"] == "outside"


def test_atomic_write_failure_keeps_previous_result(tmp_path, monkeypatch):
    codec = _codec()
    path = _path(tmp_path)
    codec.write_child_process_result(codec.ChildProcessResultFile.capture(path), _success("previous"))

    def fail_replace(*args, **kwargs):
        raise OSError("injected atomic replacement failure")

    with monkeypatch.context() as fault:
        fault.setattr(os, "replace", fail_replace)
        with pytest.raises(OSError):
            codec.write_child_process_result(codec.ChildProcessResultFile.capture(path), _success("next"))
    assert codec.read_child_process_result(codec.ChildProcessResultFile.capture(path)) == _success("previous")
    assert sorted(p.name for p in path.parent.iterdir()) == ["result.json"]


def test_parent_directory_alias_is_rejected(tmp_path):
    codec = _codec()
    path = _path(tmp_path)
    codec.write_child_process_result(codec.ChildProcessResultFile.capture(path), _success("private"))
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(path.parent, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    with pytest.raises((ValueError, OSError)):
        codec.read_child_process_result(codec.ChildProcessResultFile.capture(alias / path.name))


def test_public_error_envelope_round_trips(tmp_path):
    codec = _codec()
    from nz_coder.protocol.public_error import to_public_error

    envelope = {"schema": "nz.child_result.v1", "ok": False, "public_error": to_public_error(RuntimeError("sentinel-secret")).to_dict()}
    path = _path(tmp_path)
    codec.write_child_process_result(codec.ChildProcessResultFile.capture(path), envelope)
    read = codec.read_child_process_result(codec.ChildProcessResultFile.capture(path))
    assert read["public_error"]["code"] == "internal_error"
    assert "sentinel-secret" not in repr(read)


def test_windows_absent_result_is_reported_as_missing(tmp_path, monkeypatch):
    """首次写入时的目标不存在，不能被 Windows 边界误判为不安全路径。"""
    codec = _codec()
    from nz_coder.foundation import project_control

    def open_missing(path, *, directory, missing_ok=False, parent=None):
        if missing_ok:
            return None
        raise ValueError("path cannot be opened")

    monkeypatch.setattr(project_control, "_windows_open", open_missing)
    monkeypatch.setattr(codec, "os", SimpleNamespace(name="nt"))
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace())
    with pytest.raises(FileNotFoundError):
        codec._open_result(tmp_path / "result.json", 42)


@pytest.mark.skipif(os.name != "nt", reason="Windows native reparse-point boundary")
def test_windows_result_reparse_point_is_rejected(tmp_path):
    import subprocess

    codec = _codec()
    path = _path(tmp_path)
    codec.write_child_process_result(codec.ChildProcessResultFile.capture(path), _success("private"))
    alias = tmp_path / "junction"
    created = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(path.parent)], capture_output=True, timeout=5)
    assert created.returncode == 0
    try:
        with pytest.raises((OSError, ValueError)):
            codec.read_child_process_result(codec.ChildProcessResultFile.capture(alias / "result.json"))
    finally:
        alias.rmdir()


def test_result_allocation_rejects_reparse_root_before_hardening(tmp_path, monkeypatch):
    """Windows junction 不是 symlink，父方不能先给其目标修改权限。"""
    codec = _codec()
    from nz_coder.foundation.user_paths import prepare_user_storage

    root = prepare_user_storage(tmp_path).workspace_state / "child-process-results"
    root.mkdir(mode=0o700)
    original = Path.lstat

    def tagged_lstat(path, *args, **kwargs):
        if path == root:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_file_attributes=0x400)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", tagged_lstat)
    with pytest.raises(ValueError):
        codec.allocate_child_process_result(tmp_path)
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("operation", ["read", "write", "discard", "discard-after-read"])
def test_allocated_invocation_replacement_cannot_redirect_operations(tmp_path, operation):
    codec = _codec()
    first = codec.allocate_child_process_result(tmp_path)
    second = codec.allocate_child_process_result(tmp_path)
    codec.write_child_process_result(first, _success("first-task"))
    codec.write_child_process_result(second, _success("second-task"))
    if operation == "discard-after-read":
        assert codec.read_child_process_result(first) == _success("first-task")
    moved = first.path.parent.with_name(first.path.parent.name + "-moved")
    first.path.parent.rename(moved)
    second.path.parent.rename(first.path.parent)
    with pytest.raises((ValueError, OSError)):
        if operation == "read":
            codec.read_child_process_result(first)
        elif operation == "write":
            codec.write_child_process_result(first, _success("wrong-task"))
        else:
            codec.discard_child_process_result(first)
    assert json.loads(first.path.read_text())["result"] == "second-task"
    assert json.loads((moved / "result.json").read_text())["result"] == "first-task"


@pytest.mark.parametrize("canonical", [[], "not-an-object", 42])
def test_canonical_requires_an_object_and_invocation_binding(tmp_path, canonical):
    codec = _codec()
    result_file = codec.allocate_child_process_result(tmp_path)
    with pytest.raises(ValueError):
        codec.write_child_process_result(result_file, {**_success("done"), "child_result": canonical, "child_result_settlement_id": "round"})
