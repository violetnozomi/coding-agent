"""无需下载依赖的真实 Python 复用和 CMake 构建验收。"""

import json
import os
import shutil
import subprocess
import time
import venv
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nz_coder.foundation.workspace_trust import load_config_snapshot
from nz_coder.runtime.process.execution_environment import scoped_execution_environment
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.worktree import WorktreeManager
from nz_coder.runtime.worktree.environment import (
    WorkspacePreparationSpec,
    prepare_environment,
)
from nz_coder.tools.bash import execute_bash_local
from tests.fixtures.process_lifecycle import make_preparation_scope


def test_two_python_children_borrow_interpreter_and_import_their_own_source(tmp_path):
    parent = tmp_path / "repo"
    (parent / "src/project").mkdir(parents=True)
    (parent / "src/project/__init__.py").write_text('VALUE="parent"')
    # 标准库创建无 pip 的测试解释器，未安装或下载任何项目依赖。
    venv.EnvBuilder(with_pip=False, symlinks=True).create(parent / ".venv")
    python = parent / ".venv/bin/python"
    site = Path(
        subprocess.check_output(
            [
                str(python),
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            text=True,
        ).strip()
    )
    (site / "borrowed_dependency.py").write_text("VALUE=41")
    (site / "parent_editable.pth").write_text(str(parent / "src"))
    config = {
        "environment": {
            "VIRTUAL_ENV": "${parent}/.venv",
            "PATH": "${parent}/.venv/bin:${env:PATH}",
            "PYTHONPATH": "${worktree}/src",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        "checks": [
            [
                str(python),
                "-c",
                "import project, pathlib; assert pathlib.Path(project.__file__).resolve().is_relative_to(pathlib.Path.cwd())",
            ]
        ],
    }
    snapshot = load_config_snapshot(
        parent, environ={"NZ_SUBAGENT_WORKSPACE_JSON": json.dumps(config)}
    )
    spec = WorkspacePreparationSpec.from_snapshot(snapshot)
    before = {
        p.relative_to(site): p.read_bytes() for p in site.rglob("*") if p.is_file()
    }

    def child_task(name):
        scope = make_preparation_scope(parent, child=name)
        try:
            tree = WorktreeManager(parent, preparation=scope).create(name)
            child = Path(tree.path)
            (child / "src/project/__init__.py").write_text("VALUE=" + repr(name))
            state = {}
            receipt = prepare_environment(
                spec, parent=parent, child=child, state=state, preparation=scope
            )
            command = 'python -c "import project, borrowed_dependency, sys; print(project.VALUE, borrowed_dependency.VALUE, sys.prefix)"'
            with (
                scoped_workdir(child),
                scoped_execution_environment(receipt.environment),
            ):
                result = execute_bash_local(command)
            assert f"{name} 41 {parent / '.venv'}" in str(result)
            assert not (child / ".venv").exists()
            assert state["environment_preparation"]["status"] == "ready"
        finally:
            scope.close(deadline=time.monotonic() + 2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(child_task, ("one", "two")))
    assert {
        p.relative_to(site): p.read_bytes() for p in site.rglob("*") if p.is_file()
    } == before


def test_cmake_configures_and_builds_from_child_in_its_private_build_directory(
    tmp_path,
):
    cmake, compiler = shutil.which("cmake"), shutil.which("c++")
    if not cmake or not compiler:
        pytest.skip("CMake/C++ toolchain is unavailable; no tool installation")
    parent = tmp_path / "repo"
    parent.mkdir()
    (parent / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.16)\nproject(fixture LANGUAGES CXX)\nadd_executable(app main.cpp)\n"
    )
    (parent / "main.cpp").write_text(
        '#include <iostream>\nint main(){std::cout << "parent";}'
    )
    (parent / "build").mkdir()
    (parent / "build/parent-sentinel").write_text("keep")
    config = {
        "owned_directories": ["build"],
        "environment": {"CXX": compiler},
        "setup": [[cmake, "-S", "${worktree}", "-B", "${worktree}/build"]],
        "checks": [
            [
                os.sys.executable,
                "-c",
                "from pathlib import Path; assert ('CMAKE_HOME_DIRECTORY:INTERNAL='+str(Path.cwd())) in Path('build/CMakeCache.txt').read_text()",
            ]
        ],
    }
    snapshot = load_config_snapshot(
        parent, environ={"NZ_SUBAGENT_WORKSPACE_JSON": json.dumps(config)}
    )
    scope = make_preparation_scope(parent, timeout=15)
    try:
        tree = WorktreeManager(parent, preparation=scope).create("child")
        child = Path(tree.path)
        (child / "main.cpp").write_text(
            '#include <iostream>\nint main(){std::cout << "child";}'
        )
        receipt = prepare_environment(
            WorkspacePreparationSpec.from_snapshot(snapshot),
            parent=parent,
            child=child,
            state={},
            preparation=scope,
        )
        built = scope.run(
            (cmake, "--build", "build"),
            cwd=child,
            timeout=10,
            environment=receipt.environment.as_dict(),
        )
        assert built.returncode == 0, built.stderr
        run = scope.run(
            (str(child / "build/app"),),
            cwd=child,
            timeout=2,
            environment=receipt.environment.as_dict(),
        )
        assert run.stdout == "child"
        assert (parent / "build/parent-sentinel").read_text() == "keep"
        assert not (parent / "build/CMakeCache.txt").exists()
    finally:
        scope.close(deadline=time.monotonic() + 2)
