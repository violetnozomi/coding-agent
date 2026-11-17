"""任务差异基于首次源码副本，生成物和借用资源不交付。"""

from pathlib import Path
from types import SimpleNamespace

from nz_coder.runtime.worktree import Worktree
from nz_coder.runtime.agent.subagent import _collect_changed_files
from nz_coder.runtime.worktree.environment import source_manifest


def test_delivery_does_not_include_parent_dirty_source_or_owned_outputs(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    (child / "app.py").write_text("parent dirty source")
    state = {
        "delivery_baseline": {"version": 1, "files": source_manifest(child)},
        "environment_preparation": {"owned": ["build"], "borrowed": []},
    }
    (child / "build").mkdir()
    (child / "build/output").write_text("generated")
    tracker = SimpleNamespace(changed_paths=lambda: [])
    tree = Worktree("child", str(child), "", "", "", mode="copy")
    assert _collect_changed_files(tmp_path, tree, tracker, state) == []
    (child / "app.py").write_text("child edit")
    assert _collect_changed_files(tmp_path, tree, tracker, state) == ["app.py"]


def test_borrowed_symlink_is_not_delivery_and_cleanup_keeps_target(tmp_path):
    from nz_coder.runtime.worktree import WorktreeManager

    parent = tmp_path / "repo"
    parent.mkdir()
    shared = parent / "node_modules"
    shared.mkdir()
    (shared / "keep").write_text("dependency")
    manager = WorktreeManager(parent, worktree_dir=tmp_path / "trees")
    tree = manager.create("child")
    child = Path(tree.path)
    state = {
        "delivery_baseline": {"version": 1, "files": source_manifest(child)},
        "environment_preparation": {"owned": [], "borrowed": ["node_modules"]},
    }
    (child / "node_modules").symlink_to(shared, target_is_directory=True)
    assert (
        _collect_changed_files(
            parent, tree, SimpleNamespace(changed_paths=lambda: []), state
        )
        == []
    )
    manager.remove(tree)
    assert (shared / "keep").read_text() == "dependency"


def test_snapshot_keeps_tracked_source_names_but_excludes_declared_outputs(tmp_path):
    from nz_coder.runtime.process.workspace_snapshot import WorkspaceSnapshotStore
    from nz_coder.runtime.process.execution_environment import (
        ExecutionEnvironment,
        scoped_execution_environment,
    )

    child = tmp_path / "child"
    child.mkdir()
    (child / "build").mkdir()
    (child / "build/source.txt").write_text("source")
    (child / "out").mkdir()
    (child / "out/generated.txt").write_text("generated")
    env = ExecutionEnvironment.capture(
        {}, excluded=(str(child / "out"),), source_paths=("build/source.txt",)
    )
    store = WorkspaceSnapshotStore(child, tmp_path / "snapshots")
    with scoped_execution_environment(env):
        files = {p.relative_to(child).as_posix() for p in store._iter_files()}
        assert "build/source.txt" in files
        assert "out/generated.txt" not in files
        left = store.verification_snapshot()
        (child / "out/generated.txt").write_text("new build")
        right = store.verification_snapshot()
        assert store.verification_changes(left, right) == {}


def test_resume_and_fork_keep_unapplied_changes_and_original_parent_baseline(tmp_path):
    import time
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.core.run_settings import RunSettings
    from tests.fixtures.process_lifecycle import make_preparation_scope

    (tmp_path / "app.py").write_text("parent at copy")
    state = subagent._new_subagent_state("parent", "general-purpose", None)
    settings = RunSettings.from_legacy_globals()

    def prepare(value):
        scope = make_preparation_scope(tmp_path, child=value["session_id"])
        try:
            return subagent.prepare_subagent_workspace(
                value,
                workspace=tmp_path,
                config_snapshot=None,
                settings=settings,
                preparation=scope,
            )
        finally:
            scope.close(deadline=time.monotonic() + 2)

    original = prepare(state)
    baseline = dict(state["baseline_hashes"])
    (original.execution_root / "app.py").write_text("unapplied child edit")
    (tmp_path / "app.py").write_text("parent changed later")
    resumed = prepare(state)
    tracker = SimpleNamespace(changed_paths=lambda: [])
    assert state["baseline_hashes"] == baseline
    assert _collect_changed_files(tmp_path, resumed.worktree, tracker, state) == [
        "app.py"
    ]
    forked = subagent._new_subagent_state("parent", "general-purpose", None)
    subagent._clone_child_worktree(tmp_path, state, forked)
    prepared_fork = prepare(forked)
    assert (
        prepared_fork.execution_root / "app.py"
    ).read_text() == "unapplied child edit"
    assert forked["baseline_hashes"] == baseline
    assert _collect_changed_files(
        tmp_path, prepared_fork.worktree, tracker, forked
    ) == ["app.py"]
