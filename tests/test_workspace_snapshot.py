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
