"""原生临时 Git 验证工作树源码、身份与本地资源。"""

from pathlib import Path
import subprocess

from nz_coder.runtime.worktree import WorktreeManager
from nz_coder.runtime.worktree.setup import perform_post_creation_setup


def git(root, *args):
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            *args,
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "app.py").write_text("BASE=1\n")
    (root / "build").mkdir()
    (root / "build/source.txt").write_text("tracked source")
    (root / ".gitignore").write_text("config/\n.venv/\nnode_modules/\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "baseline")
    return root


def test_linked_parent_keeps_child_git_identity_and_real_sha(tmp_path):
    root = repo(tmp_path)
    parent = tmp_path / "parent"
    git(root, "worktree", "add", "-b", "parent", str(parent), "HEAD")
    child = WorktreeManager(parent, worktree_dir=tmp_path / "children").create("child")
    path = Path(child.path)
    assert git(path, "branch", "--show-current") == child.branch
    assert child.head_commit == git(parent, "rev-parse", "HEAD")
    assert (path / ".git").read_bytes() != (parent / ".git").read_bytes()


def test_snapshot_keeps_tracked_build_links_dirty_and_deletions(tmp_path):
    root = repo(tmp_path)
    (root / "app.py").unlink()
    (root / "added.py").write_text("NEW=2")
    (root / "alias.py").symlink_to("added.py")
    (root / ".venv").mkdir()
    (root / ".venv/skip").write_text("dependency")
    child = WorktreeManager(root, worktree_dir=tmp_path / "children").create("child")
    path = Path(child.path)
    assert (path / "build/source.txt").read_text() == "tracked source"
    assert not (path / "app.py").exists()
    assert (path / "alias.py").is_symlink()
    assert (path / "alias.py").resolve() == path / "added.py"
    assert not (path / ".venv").exists()


def test_include_wholly_ignored_directory_and_negation(tmp_path):
    root = repo(tmp_path)
    (root / "config").mkdir()
    (root / "config/a.env").write_text("included")
    (root / "config/no.env").write_text("excluded")
    (root / ".worktreeinclude").write_text("config/**\n!config/no.env\n")
    dest = tmp_path / "target"
    dest.mkdir()
    perform_post_creation_setup(root, dest)
    assert (dest / "config/a.env").read_text() == "included"
    assert not (dest / "config/no.env").exists()


def test_creation_does_not_change_parent_hooks_config(tmp_path):
    root = repo(tmp_path)
    before = git(root, "config", "--local", "--list")
    WorktreeManager(root, worktree_dir=tmp_path / "children").create("child")
    assert git(root, "config", "--local", "--list") == before


def test_fork_snapshot_source_is_old_child_not_current_parent(tmp_path):
    root = repo(tmp_path)
    manager = WorktreeManager(root, worktree_dir=tmp_path / "children")
    old = manager.create("old")
    (Path(old.path) / "app.py").write_text("OLD_CHILD=1")
    (root / "app.py").write_text("PARENT_LATER=2")
    new = manager.create("new", snapshot_source=Path(old.path))
    assert (Path(new.path) / "app.py").read_text() == "OLD_CHILD=1"


def test_include_uses_the_trusted_captured_content(tmp_path):
    from dataclasses import replace
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from tests.fixtures.process_lifecycle import make_preparation_scope
    import time

    root = repo(tmp_path)
    (root / "config").mkdir()
    (root / "config/a.env").write_text("captured")
    (root / "config/b.env").write_text("later")
    (root / ".worktreeinclude").write_text("config/a.env\n")
    snapshot = load_config_snapshot(root, environ={})
    snapshot = replace(
        snapshot, project_control=replace(snapshot.project_control, trusted=True)
    )
    scope = make_preparation_scope(root)
    scope.config_snapshot = snapshot
    (root / ".worktreeinclude").write_text("config/b.env\n")
    try:
        tree = WorktreeManager(root, preparation=scope).create("child")
        assert (Path(tree.path) / "config/a.env").read_text() == "captured"
        assert not (Path(tree.path) / "config/b.env").exists()
    finally:
        scope.close(deadline=time.monotonic() + 2)


def test_git_paths_preserve_carriage_returns(tmp_path):
    root = repo(tmp_path)
    (root / "odd\r\nname.py").write_text("VALUE=1")
    tree = WorktreeManager(root, worktree_dir=tmp_path / "children").create("child")
    assert (Path(tree.path) / "odd\r\nname.py").read_text() == "VALUE=1"


def test_snapshot_removes_files_only_present_in_the_selected_old_commit(tmp_path):
    root = repo(tmp_path)
    old = git(root, "rev-parse", "HEAD")
    git(root, "rm", "app.py")
    git(root, "commit", "-qm", "delete")
    tree = WorktreeManager(root, worktree_dir=tmp_path / "children").create(
        "child", old
    )
    assert not (Path(tree.path) / "app.py").exists()
