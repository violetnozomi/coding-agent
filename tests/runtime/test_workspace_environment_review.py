"""独立评审发现的实际交付、恢复和子模块边界回归。"""

import shutil
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from nz_coder.runtime.agent import subagent
from nz_coder.runtime.worktree import WorktreeManager
from nz_coder.runtime.worktree.environment import prepare_environment, source_manifest
from tests.fixtures.process_lifecycle import make_preparation_scope
from tests.runtime.test_workspace_environment_git import repo, git
from tests.runtime.test_workspace_environment_prepare import case
from tests.runtime.test_workspace_environment_submodules import module_case


@pytest.mark.parametrize("mode", ["git", "copy"])
def test_test_cache_is_not_delivered_but_new_source_is(tmp_path, mode):
    if mode == "git":
        root = repo(tmp_path)
        with (root / ".gitignore").open("a") as stream:
            stream.write(".pytest_cache/\n")
    else:
        root = tmp_path / "repo"
        root.mkdir()
        (root / "app.py").write_text("source")
    tree = WorktreeManager(root, worktree_dir=tmp_path / "trees").create("child")
    child = Path(tree.path)
    state = {"delivery_baseline": {"version": 1, "files": source_manifest(child)}}
    (child / ".pytest_cache").mkdir()
    (child / ".pytest_cache/nodeids").write_text("test cache")
    tracker = SimpleNamespace(changed_paths=lambda: [])
    assert subagent._collect_changed_files(root, tree, tracker, state) == []
    (child / "new.py").write_text("new source")
    expected = ["new.py"]
    if mode == "git":
        (child / "build/source.txt").write_text("changed tracked source")
        expected = ["build/source.txt", "new.py"]
    assert subagent._collect_changed_files(root, tree, tracker, state) == expected


def test_missing_owned_output_invalidates_success_and_reruns_setup(tmp_path):
    import sys

    root, child, spec, scope = case(
        tmp_path,
        {
            "owned_directories": ["out"],
            "setup": [
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; p=Path('out'); p.mkdir(exist_ok=True); (p/'ready').write_text('ready')",
                ]
            ],
        },
    )
    state = {}
    try:
        first = prepare_environment(
            spec, parent=root, child=child, state=state, preparation=scope
        )
        shutil.rmtree(child / "out")
        with pytest.raises(Exception, match="resource|Resource"):
            first.environment.validate()
        prepare_environment(
            spec, parent=root, child=child, state=state, preparation=scope
        )
        assert (child / "out/ready").read_text() == "ready"
    finally:
        scope.close(deadline=time.monotonic() + 2)


def test_registered_missing_worktree_is_not_silently_recreated(tmp_path):
    from nz_coder.runtime.core.run_settings import RunSettings

    (tmp_path / "app.py").write_text("parent")
    state = subagent._new_subagent_state("parent", "general-purpose", None)

    def prepare():
        scope = make_preparation_scope(tmp_path, child=state["session_id"])
        try:
            return subagent.prepare_subagent_workspace(
                state,
                workspace=tmp_path,
                config_snapshot=None,
                settings=RunSettings.from_legacy_globals(),
                preparation=scope,
            )
        finally:
            scope.close(deadline=time.monotonic() + 2)

    prepared = prepare()
    (prepared.execution_root / "app.py").write_text("unapplied edit")
    original = state["delivery_baseline"]
    shutil.rmtree(prepared.execution_root)
    with pytest.raises(Exception, match="missing|unavailable|disappeared"):
        prepare()
    assert state["delivery_baseline"] == original
    assert not prepared.execution_root.exists()


def test_application_refuses_a_source_link_before_resolving_it(tmp_path):
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    from tests.test_agent_manager import _save_committed_apply_fixture

    (tmp_path / "app.py").write_text("parent")
    manager = BackgroundAgentManager(tmp_path, "parent")
    state = subagent._new_subagent_state("parent", "general-purpose", None)
    child = WorktreeManager(tmp_path).worktree_dir / state["session_id"]
    child.mkdir(parents=True)
    (child / "other.py").write_text("other content")
    (child / "app.py").symlink_to("other.py")
    state.update(
        background=True,
        status="completed",
        claimed_paths=["app.py"],
        changed_files=["app.py"],
        baseline_hashes=manager._baseline(["app.py"]),
        worktree={"id": state["session_id"], "path": str(child), "mode": "copy"},
    )
    _save_committed_apply_fixture(manager, state)
    try:
        writes, deletes, error = manager.application_changes(
            state["session_id"], ["app.py"]
        )
        assert writes == deletes == []
        assert "link" in error
    finally:
        manager.close()


def test_undeclared_real_submodule_is_not_a_normal_application_directory(tmp_path):
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    from tests.test_agent_manager import _save_committed_apply_fixture

    root, child, _, scope, tree, _ = module_case(tmp_path)
    (child / "vendor/lib").mkdir(parents=True, exist_ok=True)
    (child / "vendor/lib/new.py").write_text("module edit")
    manager = BackgroundAgentManager(root, "parent")
    state = subagent._new_subagent_state("parent", "general-purpose", None)
    state.update(
        session_id="child",
        background=True,
        status="completed",
        claimed_paths=["vendor/lib"],
        changed_files=["vendor/lib/new.py"],
        baseline_hashes=manager._baseline(["vendor/lib"]),
        worktree=asdict(tree),
    )
    _save_committed_apply_fixture(manager, state)
    try:
        writes, deletes, error = manager.application_changes(
            "child", ["vendor/lib/new.py"]
        )
        assert writes == deletes == []
        assert "submodule" in error
    finally:
        manager.close()
        scope.close(deadline=time.monotonic() + 2)


def test_undeclared_real_submodule_write_is_rejected_before_preparation(tmp_path):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.runtime.core.run_settings import RunSettings

    root, _, _, original, _, _ = module_case(tmp_path)
    original.close(deadline=time.monotonic() + 2)
    state = subagent._new_subagent_state("parent", "general-purpose", None)
    state["claimed_paths"] = ["vendor/lib/new.py"]
    snapshot = load_config_snapshot(root, environ={})
    scope = make_preparation_scope(root, child=state["session_id"])
    try:
        with pytest.raises(Exception, match="submodule"):
            subagent.prepare_subagent_workspace(
                state,
                workspace=root,
                config_snapshot=snapshot,
                settings=RunSettings.from_snapshot(snapshot),
                preparation=scope,
            )
        assert not state.get("worktree")
    finally:
        scope.close(deadline=time.monotonic() + 2)


def test_changed_gitlink_is_reported_even_when_module_head_stays_the_same(tmp_path):
    root, child, spec, scope, tree, expected = module_case(tmp_path)
    state = {}
    try:
        receipt = prepare_environment(
            spec, parent=root, child=child, state=state, preparation=scope
        )
        state["delivery_baseline"] = {
            "version": 1,
            "files": source_manifest(child, receipt.excluded),
        }
        git(child, "update-index", "--cacheinfo", "160000," + "1" * 40 + ",vendor/lib")
        assert git(child / "vendor/lib", "rev-parse", "HEAD") == expected
        assert subagent._collect_changed_files(
            root, tree, SimpleNamespace(changed_paths=lambda: []), state
        ) == ["vendor/lib"]
    finally:
        scope.close(deadline=time.monotonic() + 2)
