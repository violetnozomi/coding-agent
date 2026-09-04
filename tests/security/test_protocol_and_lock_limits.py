"""Adversarial contracts for local protocol frames and private-state locks."""
from __future__ import annotations

from io import BytesIO, StringIO
import os
from types import SimpleNamespace

import pytest


def test_lsp_oversized_frame_is_rejected(tmp_path):
    """An LSP peer cannot make the client allocate an arbitrary body."""
    from nz_coder.lsp.client import LSPClient, LSPError, _MAX_FRAME_BYTES

    client = object.__new__(LSPClient)
    client.process = SimpleNamespace(
        stdout=BytesIO(
            f"Content-Length: {_MAX_FRAME_BYTES + 1}\r\n\r\n".encode("ascii")
        )
    )

    with pytest.raises(LSPError, match="frame exceeds"):
        client._read_message()


def test_lsp_oversized_header_is_rejected(tmp_path):
    """Header lines are bounded before a terminator is received."""
    from nz_coder.lsp.client import LSPClient, LSPError, _MAX_HEADER_BYTES

    client = object.__new__(LSPClient)
    client.process = SimpleNamespace(
        stdout=BytesIO(b"X-Test: " + b"x" * (_MAX_HEADER_BYTES + 1) + b"\r\n")
    )

    with pytest.raises(LSPError, match="header exceeds"):
        client._read_message()


def test_mcp_oversized_frame_is_rejected():
    """A newline-delimited MCP peer cannot submit an unbounded JSON line."""
    from nz_coder.mcp.client import MCPError, _MAX_FRAME_BYTES, _read_json_frame

    stream = StringIO("{" + "x" * _MAX_FRAME_BYTES + "}\n")
    with pytest.raises(MCPError, match="frame exceeds"):
        _read_json_frame(stream, server_name="oversized")


def test_private_lock_rejects_symlink(tmp_path):
    """A lock path cannot be redirected to an attacker-selected file."""
    from nz_coder.foundation.file_lock import UnsafeFileLock, exclusive_file_lock

    outside = tmp_path / "outside.lock"
    outside.write_bytes(b"sentinel")
    link = tmp_path / "state.lock"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    with pytest.raises(UnsafeFileLock, match="symbolic link"):
        with exclusive_file_lock(link):
            pass
    assert outside.read_bytes() == b"sentinel"


def test_private_lock_rejects_parent_symlink(tmp_path):
    """Every existing parent component is checked before opening a lock."""
    from nz_coder.foundation.file_lock import UnsafeFileLock, exclusive_file_lock

    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    with pytest.raises(UnsafeFileLock, match="parent"):
        with exclusive_file_lock(alias / "state.lock"):
            pass
    assert not (outside / "state.lock").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point contract")
def test_private_lock_rejects_windows_reparse_parent(tmp_path, monkeypatch):
    """Windows private locks consult the reparse-point guard."""
    import nz_coder.foundation.file_lock as locks

    parent = tmp_path / "state"
    parent.mkdir()
    monkeypatch.setattr(locks, "_is_windows_reparse_point", lambda path: path == parent)
    with pytest.raises(locks.UnsafeFileLock, match="parent"):
        with locks.exclusive_file_lock(parent / "state.lock"):
            pass
