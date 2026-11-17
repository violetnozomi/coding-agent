"""真实准备命令、复用、私有模式及失败门槛。"""

import json
import sys
import time
from pathlib import Path

import pytest

from tests.fixtures.process_lifecycle import make_preparation_scope
from nz_coder.runtime.worktree import WorktreeManager


def case(tmp_path, config):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.runtime.worktree.environment import WorkspacePreparationSpec

    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text('VALUE="child"')
    snapshot = load_config_snapshot(
        root, environ={"NZ_SUBAGENT_WORKSPACE_JSON": json.dumps(config)}
    )
    spec = WorkspacePreparationSpec.from_snapshot(snapshot)
    scope = make_preparation_scope(root)
    worktree = WorktreeManager(root, preparation=scope).create("child")
    return root, Path(worktree.path), spec, scope


def test_same_environment_reuses_setup_but_runs_checks(tmp_path):
    from nz_coder.runtime.worktree.environment import prepare_environment

    config = {
        "owned_directories": ["build"],
        "setup": [
            [
                sys.executable,
                "-c",
                'from pathlib import Path; p=Path("build"); p.mkdir(exist_ok=True); f=p/"count"; f.write_text(str(int(f.read_text())+1) if f.exists() else "1")',
            ]
        ],
        "checks": [
            [
                sys.executable,
                "-c",
                'from pathlib import Path; assert Path("build/count").exists()',
            ]
        ],
    }
    root, child, spec, scope = case(tmp_path, config)
    state = {}
    try:
        first = prepare_environment(
            spec, parent=root, child=child, state=state, preparation=scope
        )
        second = prepare_environment(
            spec, parent=root, child=child, state=state, preparation=scope
        )
        assert first.environment.fingerprint == second.environment.fingerprint
        assert (child / "build/count").read_text() == "1"
        assert state["environment_preparation"]["status"] == "ready"
    finally:
        scope.close(deadline=time.monotonic() + 2)


def test_failed_check_never_records_ready(tmp_path):
    from nz_coder.runtime.worktree.environment import prepare_environment

    root, child, spec, scope = case(
        tmp_path, {"checks": [[sys.executable, "-c", "raise SystemExit(7)"]]}
    )
    state = {}
    try:
        with pytest.raises(Exception, match="checks|check"):
            prepare_environment(
                spec, parent=root, child=child, state=state, preparation=scope
            )
        assert state["environment_preparation"]["status"] == "failed"
        assert state["environment_preparation"]["exit_code"] == 7
    finally:
        scope.close(deadline=time.monotonic() + 2)


def test_private_profile_never_inherits_shared_links(tmp_path):
    from nz_coder.runtime.worktree.environment import prepare_environment

    root, child, spec, scope = case(
        tmp_path,
        {
            "symlink_directories": [".venv"],
            "environment": {"VIRTUAL_ENV": "${parent}/.venv"},
            "private": {
                "environment": {"VIRTUAL_ENV": "${worktree}/.venv"},
                "owned_directories": [".venv"],
            },
        },
    )
    (root / ".venv").mkdir()
    try:
        result = prepare_environment(
            spec, parent=root, child=child, state={}, preparation=scope, mode="private"
        )
        assert result.environment.as_dict()["VIRTUAL_ENV"] == str(child / ".venv")
        assert not (child / ".venv").is_symlink()
        assert (root / ".venv").exists()
    finally:
        scope.close(deadline=time.monotonic() + 2)


def test_owned_directory_cannot_hide_source(tmp_path):
    from nz_coder.runtime.worktree.environment import prepare_environment

    root, child, spec, scope = case(tmp_path, {"owned_directories": ["app.py"]})
    try:
        with pytest.raises(Exception, match="source|directory"):
            prepare_environment(
                spec, parent=root, child=child, state={}, preparation=scope
            )
        assert (child / "app.py").exists()
    finally:
        scope.close(deadline=time.monotonic() + 2)


def test_resume_can_add_an_empty_owned_directory(tmp_path):
    from nz_coder.runtime.worktree.environment import (
        prepare_environment,
        WorkspacePreparationSpec,
    )

    root, child, spec, scope = case(tmp_path, {"owned_directories": ["build"]})
    state = {}
    try:
        prepare_environment(
            spec, parent=root, child=child, state=state, preparation=scope
        )
        (child / "build").mkdir()
        (child / "build/object").write_text("generated")
        prepare_environment(
            WorkspacePreparationSpec({"owned_directories": ["build", "cache"]}),
            parent=root,
            child=child,
            state=state,
            preparation=scope,
        )
        assert state["environment_preparation"]["status"] == "ready"
    finally:
        scope.close(deadline=time.monotonic() + 2)


def test_copy_cannot_mutate_a_shared_dependency(tmp_path):
    from nz_coder.runtime.worktree.environment import prepare_environment

    root, child, spec, scope = case(
        tmp_path, {"symlink_directories": ["deps"], "copy_files": ["deps/config"]}
    )
    (root / "deps").mkdir()
    (root / "deps/config").write_text("original")
    (child / "deps").symlink_to(root / "deps", target_is_directory=True)
    try:
        with pytest.raises(Exception, match="overlap|shared"):
            prepare_environment(
                spec, parent=root, child=child, state={}, preparation=scope
            )
        assert (root / "deps/config").read_text() == "original"
    finally:
        scope.close(deadline=time.monotonic() + 2)


def test_reuse_rejects_child_dependency_input_different_from_parent(tmp_path):
    from nz_coder.runtime.worktree.environment import prepare_environment

    root, child, spec, scope = case(tmp_path, {"inputs": ["requirements.txt"]})
    (root / "requirements.txt").write_text("dependency==1")
    (child / "requirements.txt").write_text("dependency==2")
    try:
        with pytest.raises(Exception, match="private|dependenc"):
            prepare_environment(
                spec, parent=root, child=child, state={}, preparation=scope
            )
    finally:
        scope.close(deadline=time.monotonic() + 2)


def test_cancelled_setup_can_retry_same_tree_without_publishing_ready(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from nz_coder.runtime.process.preparation import PreparationInterrupted
    from nz_coder.runtime.worktree.environment import prepare_environment
    from tests.fixtures.process_lifecycle import wait_until

    config = {
        "owned_directories": ["build"],
        "setup": [
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import time; p=Path('build'); p.mkdir(exist_ok=True); (p/'started').touch();\nwhile not (p/'release').exists(): time.sleep(.01)",
            ]
        ],
    }
    root, child, spec, scope = case(tmp_path, config)
    state = {}
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                prepare_environment,
                spec,
                parent=root,
                child=child,
                state=state,
                preparation=scope,
            )
            wait_until(lambda: (child / "build/started").exists())
            scope.request_stop()
            with pytest.raises(PreparationInterrupted):
                future.result(timeout=3)
        assert state["environment_preparation"]["status"] == "cancelled"
        assert scope.close(deadline=time.monotonic() + 2)
        (child / "build/release").touch()
        retry = make_preparation_scope(root)
        retry.record_target(
            __import__("nz_coder.runtime.worktree", fromlist=["Worktree"]).Worktree(
                "child", str(child), "", "HEAD", "", mode="copy"
            )
        )
        try:
            prepare_environment(
                spec, parent=root, child=child, state=state, preparation=retry
            )
            assert state["environment_preparation"]["status"] == "ready"
        finally:
            retry.close(deadline=time.monotonic() + 2)
    finally:
        scope.close(deadline=time.monotonic() + 2)


def test_relative_setup_script_changes_invalidate_cached_preparation(tmp_path):
    from nz_coder.runtime.worktree.environment import prepare_environment

    root, child, spec, scope = case(
        tmp_path,
        {"owned_directories": ["build"], "setup": [[sys.executable, "setup_env.py"]]},
    )
    script = child / "setup_env.py"
    script.write_text(
        "from pathlib import Path; Path('build').mkdir(exist_ok=True); Path('build/result').write_text('v1')"
    )
    state = {}
    try:
        first = prepare_environment(
            spec, parent=root, child=child, state=state, preparation=scope
        )
        script.write_text(script.read_text().replace("v1", "v2"))
        with pytest.raises(Exception, match="inputs changed"):
            first.environment.validate()
        prepare_environment(
            spec, parent=root, child=child, state=state, preparation=scope
        )
        assert (child / "build/result").read_text() == "v2"
    finally:
        scope.close(deadline=time.monotonic() + 2)
