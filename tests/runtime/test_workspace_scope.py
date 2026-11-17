"""严格范围证据不能遗漏文件、复用mtime缓存或接受不完整扫描。"""
from __future__ import annotations

import json
import os
import threading

import pytest

from nz_coder.runtime.process.workspace_snapshot import SnapshotError, WorkspaceSnapshotStore


def _store(tmp_path, **limits):
    workspace = tmp_path / 'project'
    workspace.mkdir()
    return workspace, WorkspaceSnapshotStore(workspace, tmp_path / 'snapshots', **limits)


def test_strict_scope_covers_untracked_added_deleted_and_same_mtime_changes(tmp_path):
    workspace, store = _store(tmp_path)
    source = workspace / 'module.py'
    source.write_text('value = 1\n')
    (workspace / 'old.py').write_text('old = 1\n')
    old_stat = source.stat()
    before = store.verification_snapshot()
    source.write_text('value = 2\n')
    os.utime(source, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    (workspace / 'old.py').unlink()
    (workspace / 'new').mkdir()
    (workspace / 'new/untracked.py').write_text('broken syntax\n')
    after = store.verification_snapshot()
    assert store.verification_changes(before, after) == {
        'module.py': 'replace', 'old.py': 'delete',
        'new': 'create', 'new/untracked.py': 'create',
    }


def test_scope_noop_has_no_changes(tmp_path):
    workspace, store = _store(tmp_path)
    before = store.verification_snapshot()
    (workspace / 'temporary').write_text('discarded')
    (workspace / 'temporary').unlink()
    after = store.verification_snapshot()
    assert store.verification_changes(before, after) == {}


def test_scope_does_not_reuse_revert_exclusions(tmp_path):
    workspace, store = _store(tmp_path)
    (workspace / 'node_modules').mkdir()
    before = store.verification_snapshot()
    (workspace / 'node_modules/entry.js').write_text('broken!')
    after = store.verification_snapshot()
    assert store.verification_changes(before, after) == {'node_modules/entry.js': 'create'}


@pytest.mark.parametrize('limit', ['max_files', 'max_file_bytes', 'max_total_bytes'])
def test_scope_limits_fail_instead_of_skipping(tmp_path, limit):
    workspace, store = _store(tmp_path, **{limit: 1})
    (workspace / 'a.py').write_text('value = 1\n')
    (workspace / 'b.py').write_text('value = 2\n')
    with pytest.raises(SnapshotError):
        store.verification_snapshot()


def test_scope_cancellation_and_deadline_fail_closed(tmp_path):
    _, store = _store(tmp_path)
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(SnapshotError):
        store.verification_snapshot(cancel_event=cancelled)
    with pytest.raises(SnapshotError):
        store.verification_snapshot(timeout_seconds=0)


def test_scope_records_links_without_following_targets(tmp_path):
    from nz_coder.runtime.process.workspace_scope import load

    workspace, store = _store(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret').write_text('outside data')
    before = store.verification_snapshot()
    (workspace / 'file-link').symlink_to('missing-file')
    (workspace / 'dir-link').symlink_to(outside, target_is_directory=True)
    after = store.verification_snapshot()
    entries = load(store, after)
    assert store.verification_changes(before, after) == {'dir-link': 'create', 'file-link': 'create'}
    assert entries['file-link']['kind'] == entries['dir-link']['kind'] == 'symlink'
    assert set(entries) == {'.', 'file-link', 'dir-link'}
    (workspace / 'file-link').unlink()
    (workspace / 'file-link').symlink_to('another-missing-file')
    replacement = store.verification_snapshot()
    assert store.verification_changes(after, replacement) == {'file-link': 'replace'}
    (workspace / 'dir-link').unlink()
    assert store.verification_changes(replacement, store.verification_snapshot()) == {'dir-link': 'delete'}
    assert (outside / 'secret').read_text() == 'outside data'


def test_scope_rejects_link_changed_during_target_read(tmp_path, monkeypatch):
    workspace, store = _store(tmp_path)
    link = workspace / 'alias'
    link.symlink_to('first')
    original = os.readlink

    def replace_link(*args, **kwargs):
        target = original(*args, **kwargs)
        link.unlink()
        link.symlink_to('second')
        return target

    monkeypatch.setattr(os, 'readlink', replace_link)
    with pytest.raises(SnapshotError, match='changed'):
        store.verification_snapshot()


def test_scope_foreign_or_corrupt_reference_is_rejected(tmp_path):
    workspace, store = _store(tmp_path)
    (workspace / 'a.py').write_text('a = 1\n')
    reference = store.verification_snapshot()
    other = tmp_path / 'other'
    other.mkdir()
    foreign = WorkspaceSnapshotStore(other, store.root)
    with pytest.raises(SnapshotError):
        foreign.verification_changes(reference, reference)
    with pytest.raises(SnapshotError):
        store.verification_changes('../escape', reference)
    manifest = store.root / 'scope' / (reference + '.json')
    payload = json.loads(manifest.read_text())
    payload['files']['a.py']['digest'] = '0' * 64
    manifest.write_text(json.dumps(payload))
    with pytest.raises(SnapshotError):
        store.verification_changes(reference, reference)


def test_scope_read_error_or_mid_read_change_cannot_produce_proof(tmp_path, monkeypatch):
    workspace, store = _store(tmp_path)
    source = workspace / 'a.py'
    source.write_text('a = 1\n')
    real_read = os.read

    def racing_read(fd, size):
        chunk = real_read(fd, size)
        if chunk:
            source.write_text('a = 2\n')
        return chunk

    monkeypatch.setattr(os, 'read', racing_read)
    with pytest.raises(SnapshotError, match='changed'):
        store.verification_snapshot()
    monkeypatch.setattr(os, 'read', lambda *_: (_ for _ in ()).throw(PermissionError('unreadable')))
    with pytest.raises(SnapshotError, match='incomplete'):
        store.verification_snapshot()


def test_scope_fifo_and_revert_reference_are_not_accepted(tmp_path):
    workspace, store = _store(tmp_path)
    loose = store.track()
    os.mkfifo(workspace / 'pipe')
    with pytest.raises(SnapshotError, match='special file'):
        store.verification_snapshot()
    with pytest.raises(SnapshotError):
        store.verification_changes(loose, loose)


def test_scope_covers_workspace_root_and_directory_modes(tmp_path):
    workspace, store = _store(tmp_path)
    (workspace / 'pkg').mkdir()
    before = store.verification_snapshot()
    workspace.chmod(0o700)
    (workspace / 'pkg').chmod(0o700)
    after = store.verification_snapshot()
    assert store.verification_changes(before, after) == {'.': 'replace', 'pkg': 'replace'}
