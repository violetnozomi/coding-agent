"""真实测试产物逐项结算，空目录和其他输入仍保留版本归属。"""
from __future__ import annotations

from pathlib import Path
import shlex
import subprocess
import sys
import py_compile

import pytest

from nz_coder.runtime.adapters.verification_facts import command_receipt
from nz_coder.runtime.adapters.verification_workspace import collect_workspace_settlement
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.process.workspace_scope_execution import execute_observed
from nz_coder.tool_platform.execution import ToolExecutionResult
from tests.verification_scenarios import VerificationScenario


def _project(root, effect="Path('scratch-container').mkdir(exist_ok=True)"):
    (root / "tests").mkdir()
    (root / "app.py").write_text("VALUE = 2\n")
    (root / "tests/test_app.py").write_text(
        "from pathlib import Path\nimport unittest\nimport app\n"
        "class AppTest(unittest.TestCase):\n"
        "    def test_value(self):\n"
        "        self.assertEqual(app.VALUE, 2)\n"
        + "".join(f"        {line}\n" for line in effect.splitlines())
    )
    return f"{shlex.quote(sys.executable)} -m unittest discover -s tests -v"


def _execute(scenario, command):
    args = {"command": command, "workdir": str(scenario.root)}
    start = scenario.runtime.begin_tool({"function": {"name": "bash", "arguments": args}})

    def actual():
        completed = subprocess.run(shlex.split(command), cwd=scenario.root,
                                   text=True, capture_output=True, timeout=10)
        output = completed.stdout + completed.stderr
        receipt = command_receipt("bash", command, str(scenario.root),
            outcome="failed" if completed.returncode else "succeeded",
            exit_code=completed.returncode, output=output)
        return ToolExecutionResult("bash", args, output, True, False,
            completed.returncode != 0, False, execution_facts=(receipt,),
            execution_id=start.execution_id)

    with scoped_workdir(scenario.root):
        result = execute_observed(scenario.store, start.version.generation, actual,
            on_before=lambda ref: scenario.runtime.record_before(start, ref))
        scenario.runtime.record_result(result)
    assert not result.command_failed, result.output
    return result


def _scenario(root, command):
    return VerificationScenario(workspace=root, require_targeted=True, plan={"stages": [{
        "name": "targeted", "required": True, "commands": [{
            "command": command, "cwd": str(root), "required": True,
        }],
    }]})


def test_cold_test_and_cleanup_keep_only_real_directory_changes(tmp_path):
    command = _project(tmp_path)
    scenario = _scenario(tmp_path, command)
    try:
        _execute(scenario, command)
        assert list(tmp_path.rglob("*.pyc")), "必须实际生成缓存"
        assert scenario.state.last_mutation_paths == ("scratch-container",)
        assert not scenario.runtime.status()["tests_passed"]
        generation = scenario.state.mutation_generation

        _execute(scenario, command)
        assert scenario.state.mutation_generation == generation
        assert scenario.runtime.status()["completion_ready"]

        cleanup = shlex.join([sys.executable, "-c",
            "from pathlib import Path; import shutil; "
            "shutil.rmtree('scratch-container'); "
            "[shutil.rmtree(p) for p in Path('.').rglob('__pycache__')]"])
        _execute(scenario, cleanup)
        assert scenario.state.last_mutation_paths == ("scratch-container",)
        assert scenario.state.mutation_generation == generation + 1
        assert not scenario.runtime.status()["tests_passed"]

        _execute(scenario, command)
        assert not scenario.runtime.status()["tests_passed"]
        cold_generation = scenario.state.mutation_generation
        _execute(scenario, command)
        assert scenario.state.mutation_generation == cold_generation
        assert scenario.runtime.status()["completion_ready"]
    finally:
        scenario.runtime.close()


@pytest.mark.parametrize("path,content", [("app.py", "VALUE = 2\nCHANGED = True\n"),
                                          ("settings.json", '{"feature":true}')])
def test_cache_does_not_hide_real_source_or_configuration_write(tmp_path, path, content):
    command = _project(tmp_path, f"Path({path!r}).write_text({content!r})")
    scenario = _scenario(tmp_path, command)
    try:
        _execute(scenario, command)
        assert path in scenario.state.last_mutation_paths
        assert not scenario.runtime.status()["completion_ready"]
        assert not any(p.startswith("tests/__pycache__") for p in scenario.state.last_mutation_paths)
        if path == "settings.json":
            assert scenario.state.last_mutation_paths == ("settings.json",)
    finally:
        scenario.runtime.close()


def test_empty_directory_can_change_business_behavior_and_is_not_ignored(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "app.py").write_text(
        "from pathlib import Path\ndef enabled():\n    return Path('enabled').exists()\n")
    (tmp_path / "tests/test_app.py").write_text(
        "from pathlib import Path\nimport unittest\nimport app\n"
        "class AppTest(unittest.TestCase):\n"
        "    def test_initially_disabled(self):\n"
        "        self.assertFalse(app.enabled())\n"
        "        Path('enabled').mkdir()\n")
    command = f"{shlex.quote(sys.executable)} -m unittest discover -s tests -v"
    scenario = _scenario(tmp_path, command)
    try:
        _execute(scenario, command)
        assert "enabled" in scenario.state.last_mutation_paths
        assert not scenario.runtime.status()["completion_ready"]
    finally:
        scenario.runtime.close()


def test_cache_directory_permissions_remain_a_real_change(tmp_path):
    command = _project(tmp_path)
    scenario = _scenario(tmp_path, command)
    try:
        _execute(scenario, command)
        _execute(scenario, command)
        cache = tmp_path / "__pycache__"
        assert cache.exists()
        mode = cache.stat().st_mode & 0o777
        changed_mode = mode ^ 0o020
        mutate = shlex.join([sys.executable, "-c", f"from pathlib import Path; Path('__pycache__').chmod({changed_mode})"])
        _execute(scenario, mutate)
        assert "__pycache__" in scenario.state.last_mutation_paths
        assert not scenario.runtime.status()["completion_ready"]
    finally:
        scenario.runtime.close()


def test_unknown_file_inside_cache_stays_visible_with_its_directory(tmp_path):
    command = _project(tmp_path, "Path('__pycache__/settings.json').write_text('{\"feature\":true}')")
    scenario = _scenario(tmp_path, command)
    try:
        _execute(scenario, command)
        assert "__pycache__" in scenario.state.last_mutation_paths
        assert "__pycache__/settings.json" in scenario.state.last_mutation_paths
        assert not any(p.endswith(".pyc") for p in scenario.state.last_mutation_paths)
        assert not scenario.runtime.status()["completion_ready"]
    finally:
        scenario.runtime.close()


def test_external_cache_creation_before_a_check_is_not_its_runtime_artifact(tmp_path):
    command = _project(tmp_path, "")
    scenario = _scenario(tmp_path, command)
    try:
        cache = Path(py_compile.compile(str(tmp_path / "app.py"), doraise=True))
        _execute(scenario, command)
        assert cache.relative_to(tmp_path).as_posix() in scenario.state.last_mutation_paths
        assert not scenario.runtime.status()["completion_ready"]
        generation = scenario.state.mutation_generation
        _execute(scenario, command)
        assert scenario.state.mutation_generation == generation
        assert scenario.runtime.status()["completion_ready"]
    finally:
        scenario.runtime.close()


@pytest.mark.parametrize("proof", [{}, {"workspace": "/foreign"}, [], "bad-proof"])
def test_invalid_scope_remains_incomplete_instead_of_raising(tmp_path, proof):
    command = _project(tmp_path)
    scenario = _scenario(tmp_path, command)
    try:
        result = ToolExecutionResult("bash", {"command": command}, "", True, False, False, False,
                                     workspace_scope=proof)
        settled = collect_workspace_settlement(result, scenario.store, 0,
                                               dict(scenario.state.workspace_observation))
        assert settled.changes is None
        assert not settled.complete and not settled.version_matches
    finally:
        scenario.runtime.close()
