"""Tests for Git-independent, conflict-safe Agent workspace snapshots."""
from __future__ import annotations

import pytest

from nz_coder.runtime.process.workspace_snapshot import SnapshotError, WorkspaceSnapshotStore


def _store(tmp_path):
    return WorkspaceSnapshotStore(tmp_path, tmp_path / ".nz-coder" / "snapshots")


def test_snapshot_transition_restores_modified_created_and_deleted_files(tmp_path):
    first = tmp_path / "first.py"
    removed = tmp_path / "removed.py"
    first.write_text("before\n", encoding="utf-8")
    removed.write_text("present\n", encoding="utf-8")
    store = _store(tmp_path)
    before = store.track()

    first.write_text("after\n", encoding="utf-8")
    removed.unlink()
    created = tmp_path / "created.py"
    created.write_text("new\n", encoding="utf-8")
    after = store.track()

    result = store.transition(after, before)

    assert result.files == ("created.py", "first.py", "removed.py")
    assert first.read_text(encoding="utf-8") == "before\n"
    assert removed.read_text(encoding="utf-8") == "present\n"
    assert not created.exists()


def test_snapshot_transition_refuses_all_changes_on_conflict(tmp_path):
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("one\n", encoding="utf-8")
    second.write_text("two\n", encoding="utf-8")
    store = _store(tmp_path)
    before = store.track()
    first.write_text("agent-one\n", encoding="utf-8")
    second.write_text("agent-two\n", encoding="utf-8")
    after = store.track()
    second.write_text("user-two\n", encoding="utf-8")

    with pytest.raises(SnapshotError, match="workspace changed"):
        store.transition(after, before)

    assert first.read_text(encoding="utf-8") == "agent-one\n"
    assert second.read_text(encoding="utf-8") == "user-two\n"


def test_snapshot_diff_full_reports_status_counts_and_bounded_patch(tmp_path):
    modified = tmp_path / "modified.py"
    deleted = tmp_path / "deleted.py"
    binary = tmp_path / "asset.bin"
    modified.write_text("one\ntwo\n", encoding="utf-8")
    deleted.write_text("gone\n", encoding="utf-8")
    binary.write_bytes(b"\0before")
    store = _store(tmp_path)
    before = store.track()

    modified.write_text("one\nthree\nfour\n", encoding="utf-8")
    deleted.unlink()
    (tmp_path / "added.py").write_text("new\n", encoding="utf-8")
    binary.write_bytes(b"\0after")
    after = store.track()

    diffs = {item.file: item for item in store.diff_full(before, after)}

    assert sorted(diffs) == ["added.py", "asset.bin", "deleted.py", "modified.py"]
    assert diffs["added.py"].status == "added"
    assert (diffs["added.py"].additions, diffs["added.py"].deletions) == (1, 0)
    assert diffs["deleted.py"].status == "deleted"
    assert (diffs["deleted.py"].additions, diffs["deleted.py"].deletions) == (0, 1)
    assert diffs["modified.py"].status == "modified"
    assert (diffs["modified.py"].additions, diffs["modified.py"].deletions) == (2, 1)
    assert "--- a/modified.py" in diffs["modified.py"].patch
    assert "+three" in diffs["modified.py"].patch
    assert diffs["asset.bin"].patch == ""
    assert (diffs["asset.bin"].additions, diffs["asset.bin"].deletions) == (0, 0)


def test_snapshot_excludes_internal_state_and_symlinks(tmp_path):
    (tmp_path / "app.py").write_text("ok\n", encoding="utf-8")
    workflow = tmp_path / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "ci.yml").write_text("name: CI\n", encoding="utf-8")
    internal = tmp_path / ".nz-coder"
    internal.mkdir()
    (internal / "state.json").write_text("secret\n", encoding="utf-8")
    runs = tmp_path / ".nz-coder-runs"
    runs.mkdir()
    trace = runs / "raw-trace.jsonl"
    trace.write_text('{"event":"tool"}\n', encoding="utf-8")
    (tmp_path / "linked.py").symlink_to(tmp_path / "app.py")
    store = WorkspaceSnapshotStore(tmp_path, internal / "snapshots")

    before = store.track()
    trace.write_text(
        '{"event":"tool"}\n{"event":"result"}\n',
        encoding="utf-8",
    )
    after = store.track()
    manifest = store._load(after)

    assert list(manifest["files"]) == [".github/workflows/ci.yml", "app.py"]
    assert after == before
    assert store.changed_files(before, after) == []


def test_snapshot_keeps_unmanaged_product_prefixed_directories(tmp_path):
    """Snapshot storage must not hide user files based on a test-only prefix."""
    source = tmp_path / ".product-catalog"
    source.mkdir()
    (source / "catalog.py").write_text("VALUE = 1\n", encoding="utf-8")
    state = tmp_path / ".nz-coder"
    store = WorkspaceSnapshotStore(tmp_path, state / "snapshots")

    snapshot = store.track()

    assert list(store._load(snapshot)["files"]) == [
        ".product-catalog/catalog.py",
    ]


def test_snapshot_limit_fails_instead_of_creating_ambiguous_manifest(tmp_path):
    (tmp_path / "a.py").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b\n", encoding="utf-8")
    store = WorkspaceSnapshotStore(
        tmp_path,
        tmp_path / ".nz-coder" / "snapshots",
        max_files=1,
    )

    with pytest.raises(SnapshotError, match="file limit"):
        store.track()


def test_snapshot_manifest_integrity_is_verified(tmp_path):
    (tmp_path / "app.py").write_text("ok\n", encoding="utf-8")
    store = _store(tmp_path)
    snapshot = store.track()
    manifest = store._manifest_path(snapshot)
    text = manifest.read_text(encoding="utf-8").replace(
        '"size": 3',
        '"size": 4',
    )
    manifest.write_text(text, encoding="utf-8")

    with pytest.raises(SnapshotError, match="integrity"):
        store.changed_files(snapshot, snapshot)


def test_track_detects_same_size_content_with_restored_mtime(tmp_path):
    import os
    path = tmp_path / 'app.py'
    path.write_bytes(b'AAAA')
    store = _store(tmp_path)
    before = store.track()
    info = path.stat()
    path.write_bytes(b'BBBB')
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    after = store.track()
    assert before != after
    assert store.changed_files(before, after) == ['app.py']


def test_transition_rejects_internal_leaf_symlink_without_touching_victim(tmp_path):
    path, victim = tmp_path / 'app.py', tmp_path / 'victim'
    path.write_text('before')
    victim.write_text('after')
    store = _store(tmp_path)
    before = store.track()
    path.write_text('after')
    after = store.track()
    path.unlink()
    path.symlink_to(victim)
    with pytest.raises(SnapshotError):
        store.transition(after, before)
    assert victim.read_text() == 'after'


def test_copy_referenced_snapshots_supports_child_revert_after_parent_deletion(tmp_path):
    import shutil
    parent, child = tmp_path / 'parent', tmp_path / 'child'
    parent.mkdir()
    child.mkdir()
    (parent / 'app.py').write_bytes(b'before')
    source = WorkspaceSnapshotStore(parent, tmp_path / 'source-store')
    before = source.track()
    (parent / 'app.py').write_bytes(b'after!')
    after = source.track()
    (child / 'app.py').write_bytes(b'after!')
    destination = WorkspaceSnapshotStore(child, tmp_path / 'dest-store')
    source.copy_referenced_to(destination, [before, after])
    shutil.rmtree(source.root)
    destination.transition(after, before)
    assert (child / 'app.py').read_bytes() == b'before'


def test_corrupt_or_missing_referenced_blob_prevents_child_publication(tmp_path):
    parent, child = tmp_path / 'parent', tmp_path / 'child'
    parent.mkdir()
    child.mkdir()
    (parent / 'app.py').write_text('before')
    source = WorkspaceSnapshotStore(parent, tmp_path / 'source-store')
    snapshot = source.track()
    blob = source._load(snapshot)['files']['app.py']['blob']
    source._blob_path(blob).write_text('corrupt')
    destination = WorkspaceSnapshotStore(child, tmp_path / 'dest-store')
    with pytest.raises(SnapshotError):
        source.copy_referenced_to(destination, [snapshot])
    assert not destination._manifest_path(snapshot).exists()


def test_compensation_rejects_external_same_size_restored_mtime_write(tmp_path):
    import os
    path = tmp_path / 'app.py'
    path.write_bytes(b'AAAA')
    store = _store(tmp_path)
    before = store.track()
    path.write_bytes(b'BBBB')
    after = store.track()
    applied = store.transition(after, before)
    published = path.stat()
    path.write_bytes(b'CCCC')
    os.utime(path, ns=(published.st_atime_ns, published.st_mtime_ns))
    with pytest.raises(SnapshotError):
        store.transition(before, after, paths=list(applied.files), expected=applied.published_identities)
    assert path.read_bytes() == b'CCCC'


def test_transition_tracks_publish_before_fsync_error(tmp_path, monkeypatch):
    import os
    import stat
    path = tmp_path / 'app.py'
    path.write_bytes(b'before')
    store = _store(tmp_path)
    before = store.track()
    path.write_bytes(b'after!')
    after = store.track()
    fsync = os.fsync
    def fail_once(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            monkeypatch.setattr(os, 'fsync', fsync)
            raise OSError('parent sync failed')
        fsync(fd)
    monkeypatch.setattr(os, 'fsync', fail_once)
    with pytest.raises(SnapshotError):
        store.transition(after, before)
    assert path.read_bytes() == b'after!'
    assert not store.has_pending_recovery


def test_partial_rollback_retains_single_recovery_owner(tmp_path, monkeypatch):
    import os
    import stat
    path = tmp_path / 'app.py'
    path.write_bytes(b'before')
    store = _store(tmp_path)
    before = store.track()
    path.write_bytes(b'after!')
    after = store.track()
    fsync = os.fsync
    def fail_after_external_edit(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            monkeypatch.setattr(os, 'fsync', fsync)
            path.write_bytes(b'EXTERN')
            raise OSError('parent sync failed after external edit')
        fsync(fd)
    monkeypatch.setattr(os, 'fsync', fail_after_external_edit)
    with pytest.raises(SnapshotError):
        store.transition(after, before)
    assert path.read_bytes() == b'EXTERN'
    assert store.has_pending_recovery
    with pytest.raises(SnapshotError, match='recovery is pending'):
        store.transition(after, before)
    assert not store.retry_pending_recovery()
    path.write_bytes(b'before')
    assert store.retry_pending_recovery()
    assert path.read_bytes() == b'after!'


def test_track_detects_atomic_replacement_with_equal_stat(tmp_path):
    import os
    path = tmp_path / 'app.py'
    path.write_bytes(b'AAAA')
    store = _store(tmp_path)
    before = store.track()
    info = path.stat()
    replacement = tmp_path / 'replacement'
    replacement.write_bytes(b'BBBB')
    replacement.chmod(info.st_mode & 0o777)
    os.utime(replacement, ns=(info.st_atime_ns, info.st_mtime_ns))
    replacement.replace(path)
    after = store.track()
    assert before != after
    assert store.changed_files(before, after) == ['app.py']


def test_transition_rejects_parent_symlink(tmp_path):
    directory = tmp_path / 'src'
    directory.mkdir()
    path = directory / 'app'
    path.write_bytes(b'before')
    store = _store(tmp_path)
    before = store.track()
    path.write_bytes(b'after!')
    after = store.track()
    directory.rename(tmp_path / 'moved')
    victim = tmp_path / 'victim'
    victim.mkdir()
    (victim / 'app').write_bytes(b'after!')
    directory.symlink_to(victim, target_is_directory=True)
    with pytest.raises(SnapshotError):
        store.transition(after, before)
    assert (victim / 'app').read_bytes() == b'after!'


def test_binary_mode_transition_and_compensation_are_exact(tmp_path):
    import stat
    path = tmp_path / 'asset'
    path.write_bytes(b'\x00\xff\r\n')
    path.chmod(0o640)
    store = _store(tmp_path)
    before = store.track()
    path.write_bytes(b'\x01\x80\x00\n')
    path.chmod(0o600)
    after = store.track()
    applied = store.transition(after, before)
    assert path.read_bytes() == b'\x00\xff\r\n'
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    store.transition(before, after, paths=list(applied.files), expected=applied.published_identities)
    assert path.read_bytes() == b'\x01\x80\x00\n'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_copy_rejects_symlinked_destination_storage_without_creating_outside(tmp_path):
    parent, child, outside = (tmp_path / name for name in ('parent', 'child', 'outside'))
    parent.mkdir()
    child.mkdir()
    outside.mkdir()
    (parent / 'app').write_text('content')
    source = WorkspaceSnapshotStore(parent, tmp_path / 'source-store')
    snapshot = source.track()
    (tmp_path / 'linked').symlink_to(outside, target_is_directory=True)
    destination = WorkspaceSnapshotStore(child, tmp_path / 'linked' / 'created')
    with pytest.raises((SnapshotError, ValueError, OSError)):
        source.copy_referenced_to(destination, [snapshot])
    assert not (outside / 'created').exists()


def test_external_chmod_after_publish_keeps_recovery_pending(tmp_path, monkeypatch):
    import os
    import stat
    path = tmp_path / 'app'
    path.write_bytes(b'before')
    path.chmod(0o640)
    store = _store(tmp_path)
    before = store.track()
    path.write_bytes(b'after!')
    path.chmod(0o600)
    after = store.track()
    fsync = os.fsync
    def fail_parent(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            monkeypatch.setattr(os, 'fsync', fsync)
            path.chmod(0o444)
            raise OSError('fsync failure after external chmod')
        fsync(fd)
    monkeypatch.setattr(os, 'fsync', fail_parent)
    with pytest.raises(SnapshotError):
        store.transition(after, before)
    assert path.read_bytes() == b'before'
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    assert store.has_pending_recovery
    assert not store.retry_pending_recovery()
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    path.chmod(0o640)
    assert store.retry_pending_recovery()
    assert path.read_bytes() == b'after!'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
