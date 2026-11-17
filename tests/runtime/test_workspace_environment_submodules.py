"""子模块必须确认提交和私有 Git 身份，不能只复制源码。"""

import json
import subprocess
import time

import pytest
from pathlib import Path

from nz_coder.foundation.workspace_trust import load_config_snapshot
from nz_coder.runtime.worktree import WorktreeManager
from tests.fixtures.process_lifecycle import make_preparation_scope


def git(root, *args):
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "protocol.file.allow=always",
            *args,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def module_case(tmp_path):
    from nz_coder.runtime.worktree.environment import (
        WorkspacePreparationSpec,
    )

    lib = tmp_path / "lib"
    lib.mkdir()
    git(lib, "init", "-q")
    (lib / "lib.txt").write_text("dependency")
    git(lib, "add", ".")
    git(lib, "commit", "-qm", "lib")
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "submodule", "add", "-q", str(lib), "vendor/lib")
    git(root, "commit", "-qm", "module")
    expected = git(root / "vendor/lib", "rev-parse", "HEAD")
    snapshot = load_config_snapshot(
        root,
        environ={
            "NZ_SUBAGENT_WORKSPACE_JSON": json.dumps({"submodules": ["vendor/lib"]})
        },
    )
    spec = WorkspacePreparationSpec.from_snapshot(snapshot)
    scope = make_preparation_scope(root, timeout=10)
    original = scope.run_git
    # 仅本地 fixture 授权 file transport；生产逻辑不放宽 Git 传输权限。
    scope.run_git = lambda args, **kw: original(
        ("-c", "protocol.file.allow=always", *args), **kw
    )
    tree = WorktreeManager(root, preparation=scope).create("child")
    child = Path(tree.path)
    return root, child, spec, scope, tree, expected


def test_required_submodule_has_expected_sha_and_separate_gitdir(tmp_path):
    from nz_coder.runtime.worktree.environment import prepare_environment

    root, child, spec, scope, tree, expected = module_case(tmp_path)
    state = {}
    try:
        prepare_environment(
            spec, parent=root, child=child, state=state, preparation=scope
        )
        assert (child / "vendor/lib/lib.txt").read_text() == "dependency"
        assert git(child / "vendor/lib", "rev-parse", "HEAD") == expected
        assert git(child / "vendor/lib", "rev-parse", "--absolute-git-dir") != git(
            root / "vendor/lib", "rev-parse", "--absolute-git-dir"
        )
        assert state["environment_preparation"]["submodules"][0]["sha"] == expected
        WorktreeManager(root).remove(tree)
        assert (root / "vendor/lib/lib.txt").read_text() == "dependency"
    finally:
        scope.close(deadline=time.monotonic() + 2)


@pytest.mark.parametrize("fault", ["no_initialization", "parent_gitdir"])
def test_zero_exit_from_init_does_not_prove_correct_submodule(tmp_path, fault):
    from nz_coder.runtime.worktree.environment import prepare_environment

    root, child, spec, scope, tree, _ = module_case(tmp_path)
    original = scope.run_git
    saved_pointer = []

    def initialize(args, **kwargs):
        if args[:2] == ("submodule", "update"):
            if fault == "no_initialization":
                return subprocess.CompletedProcess(args, 0, "", "")
            result = original(args, **kwargs)
            pointer = child / "vendor/lib/.git"
            saved_pointer.append(pointer.read_bytes())
            pointer.write_text(
                "gitdir: "
                + git(root / "vendor/lib", "rev-parse", "--absolute-git-dir")
                + "\n"
            )
            return result
        return original(args, **kwargs)

    scope.run_git = initialize
    state = {}
    try:
        with pytest.raises(Exception, match="Submodule|submodule"):
            prepare_environment(
                spec, parent=root, child=child, state=state, preparation=scope
            )
        assert state["environment_preparation"]["status"] == "failed"
        assert (root / "vendor/lib/lib.txt").read_text() == "dependency"
    finally:
        if saved_pointer:
            (child / "vendor/lib/.git").write_bytes(saved_pointer[0])
        scope.close(deadline=time.monotonic() + 2)
