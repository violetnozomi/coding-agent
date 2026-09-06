"""Windows lock contention must keep the shared live-session refusal contract."""
from __future__ import annotations

import errno
import os
import sys
from types import SimpleNamespace

import pytest

from nz_coder.foundation import file_lock


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EAGAIN, errno.EDEADLK])
def test_windows_nonblocking_contention_is_portable_and_releases_descriptor(tmp_path, monkeypatch, error_number):
    attempts = []

    def locking(fd, mode, size):
        attempts.append((fd, mode, size))
        if len(attempts) == 1:
            raise OSError(error_number, "test lock busy")

    monkeypatch.setattr(file_lock, "os", SimpleNamespace(name="nt", SEEK_END=os.SEEK_END))
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(locking=locking, LK_LOCK=1, LK_NBLCK=2, LK_UNLCK=0))
    target = tmp_path / "owner.lock"
    with pytest.raises(BlockingIOError):
        with file_lock.exclusive_file_lock(target, blocking=False):
            pytest.fail("a contended lock must not admit recovery")
    with pytest.raises(OSError):
        os.fstat(attempts[0][0])
    assert [mode for _fd, mode, _size in attempts] == [2]  # Never unlock an unowned byte.
    with file_lock.exclusive_file_lock(target, blocking=False):
        pass
    assert [mode for _fd, mode, _size in attempts] == [2, 2, 0]


@pytest.mark.parametrize("blocking,error_number", [(True, errno.EACCES), (False, errno.EBADF)])
def test_windows_other_lock_errors_are_not_mislabelled_busy(tmp_path, monkeypatch, blocking, error_number):
    def locking(*_args):
        raise OSError(error_number, "test other failure")

    monkeypatch.setattr(file_lock, "os", SimpleNamespace(name="nt", SEEK_END=os.SEEK_END))
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(locking=locking, LK_LOCK=1, LK_NBLCK=2, LK_UNLCK=0))
    with pytest.raises(OSError) as error:
        with file_lock.exclusive_file_lock(tmp_path / "owner.lock", blocking=blocking):
            pytest.fail("failed lock acquisition must not enter")
    assert error.value.errno == error_number
    assert not isinstance(error.value, BlockingIOError)
