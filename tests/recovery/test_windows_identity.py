"""Handle identity consistency without weakening stale-file protection."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess, WorkspaceFileIdentity
from nz_coder.protocol.public_error import PublicInputError


def test_windows_snapshot_uses_one_win32_observation_and_exact_integer_times(monkeypatch):
    import ctypes
    from nz_coder.foundation.project_control import _windows_handle_snapshot

    class Query:
        def __call__(self, _handle, pointer):
            info = pointer._obj
            info.file_attributes = 1
            info.volume_serial_number = 3606225537
            info.file_index_high, info.file_index_low = 9, 7
            info.file_size_high, info.file_size_low = 2, 3
            for field in (info.last_access_time, info.last_write_time):
                field.dwHighDateTime = 27111902
                field.dwLowDateTime = 3577655353  # 1601 epoch + 12345 100 ns ticks
            return True
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: SimpleNamespace(GetFileInformationByHandle=Query()), raising=False)
    info = _windows_handle_snapshot(123)
    assert (info.device, info.inode, info.size) == (3606225537, 38654705671, 8589934595)
    assert info.mtime_ns == info.atime_ns == 1234500
    assert info.mode & 0o777 == 0o444


def test_windows_overwrite_uses_handle_identity_not_python_stat(monkeypatch, tmp_path):
    """Exercise the real Windows write method with only WinAPI calls doubled."""
    from nz_coder.foundation import project_control as control

    target = tmp_path / "a.bin"
    target.write_bytes(b"before")
    info = target.stat()
    access = WorkspaceFileAccess(tmp_path)
    handles = {1000: tmp_path}
    def opened(path, **_kwargs):
        if not Path(path).exists():
            return None
        handle = max(handles) + 1
        handles[handle] = Path(path)
        return handle
    def snapshot(handle):
        current = handles[handle].stat()
        return SimpleNamespace(attributes=0, device=3606225537, inode=current.st_ino,
                               size=current.st_size, mtime_ns=current.st_mtime_ns,
                               atime_ns=current.st_atime_ns, mode=0o666)
    monkeypatch.setattr(access, "_open_windows_parent", lambda *_a, **_k: ([1000], 1000, tmp_path))
    monkeypatch.setattr(control, "_windows_open", opened)
    monkeypatch.setattr(control, "_windows_final_path", lambda handle: str(handles[handle]))
    monkeypatch.setattr(control, "_windows_close", lambda handle: handles.pop(handle))
    monkeypatch.setattr(control, "_windows_handle_snapshot", snapshot, raising=False)
    expected = WorkspaceFileIdentity(True, 3606225537, info.st_ino, info.st_size, info.st_mtime_ns)
    access._write_windows(Path("a.bin"), b"after", None, expected=expected, overwrite=True)
    assert target.read_bytes() == b"after"
    assert handles == {}, "the write must release all acquired handles"


@pytest.mark.parametrize("original", [b"", b"\xff\x00\r\n", b"existing"])
def test_actual_read_write_create_modify_delete_keep_one_identity_system(tmp_path, original):
    access = WorkspaceFileAccess(tmp_path)
    access.write_bytes("a.bin", original, expected=WorkspaceFileIdentity.missing(), overwrite=False)
    data, expected = access.read_bytes_with_identity("a.bin")
    assert data == original
    access.write_bytes("a.bin", b"edited", expected=expected)
    data, expected = access.read_bytes_with_identity("a.bin")
    assert data == b"edited"
    access.delete("a.bin", expected=expected)
    assert not (tmp_path / "a.bin").exists()


@pytest.mark.parametrize("replacement", [False, True])
def test_actual_external_change_or_same_metadata_replacement_is_rejected(tmp_path, replacement):
    target = tmp_path / "a.bin"
    target.write_bytes(b"before")
    access = WorkspaceFileAccess(tmp_path)
    _, expected = access.read_bytes_with_identity("a.bin")
    if replacement:
        other = tmp_path / "external.bin"
        other.write_bytes(b"before")
        os.utime(other, ns=(expected.mtime_ns, expected.mtime_ns))
        other.replace(target)
    else:
        target.write_bytes(b"external change")
    retained = target.read_bytes()
    with pytest.raises(PublicInputError, match="changed"):
        access.write_bytes("a.bin", b"agent", expected=expected)
    with pytest.raises(PublicInputError, match="changed"):
        access.delete("a.bin", expected=expected)
    assert target.read_bytes() == retained


@pytest.mark.parametrize("tool,args", [
    ("write_file", {"path": "a.txt", "content": "after"}),
    ("edit_file", {"path": "a.txt", "old_text": "before", "new_text": "after"}),
    ("write_files_batch", {"files": [{"path": "a.txt", "content": "after"}, {"path": "b.txt", "content": "new"}], "overwrite": True}),
    ("apply_patch", {"changes": [{"path": "a.txt", "old_text": "before", "new_text": "after"}]}),
])
def test_actual_executor_overwrite_records_before_and_after(tmp_path, tool, args):
    from nz_coder.runtime.execution.tool_executor import ToolExecutor
    from nz_coder.runtime.process.checkpoint_runtime import recovery_run, register_batch
    from nz_coder.state.workdir import scoped_workdir
    from nz_coder.tools import files  # noqa: F401

    (tmp_path / "a.txt").write_text("before", encoding="utf-8")
    call = {"id": "call-edit", "function": {"name": tool, "arguments": args}}
    messages = [{"role": "assistant", "_nz_message_id": "msg-edit"}]
    with scoped_workdir(tmp_path), recovery_run(tmp_path, "session-a") as run:
        run.attach("interaction-a", messages)
        register_batch(messages, [call])
        result = ToolExecutor(SimpleNamespace(check=lambda *_: {"behavior": "allow"})).execute_one(call, 0)
        assert result.executed and not result.dispatch_failed, result.output
        mutation = run.ledger.mutations("session-a")[0]
        assert run.ledger.state_bytes(mutation["before_ref"]) == b"before"
        assert run.ledger.state_bytes(mutation["after_ref"]) == b"after"
