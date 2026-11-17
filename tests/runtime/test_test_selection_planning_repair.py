"""测试选择、指令合同与实际 unittest 规划共享真实选择语义。"""
from __future__ import annotations

import shlex
import subprocess
import sys

import pytest

from nz_coder.runtime.agent import task_policy as policy
from nz_coder.runtime.verification.verification_contract import extract_verification_contract


@pytest.mark.parametrize("command", [
    "pytest --junitxml results.py",
    "pytest --ignore tests/test_skip.py",
    "pytest -c tests/config.py",
    "python tests/runtests.py",
    "pytest --plugin-option tests/test_report.py",
])
def test_runner_and_option_values_do_not_prove_exact_selection(command):
    assert policy.test_command_targets(command) == ()
    assert not policy.is_exact_test_command(command)


@pytest.mark.parametrize(("command", "targets"), [
    ("py.test -q tests/test_core.py::Case::test_ok", ("tests/test_core.py::Case::test_ok",)),
    ("python -B -u -m pytest --junitxml results.py tests/test_core.py::test_ok", ("tests/test_core.py::test_ok",)),
    ("python -B -u -m unittest -v tests.test_core.Case.test_ok", ("tests.test_core.Case.test_ok",)),
    ("python -m unittest 'tests/test odd-name.py'", ("tests/test odd-name.py",)),
    ("node --test tests/core.test.cjs", ("tests/core.test.cjs",)),
    ("pytest tests/test_core.py 2>&1", ("tests/test_core.py",)),
    ("pytest tests/test_core.py 2>&1 | tail -10", ("tests/test_core.py",)),
])
def test_supported_entrypoints_keep_complete_real_selectors(command, targets):
    assert policy.test_command_targets(command) == targets
    assert policy.is_exact_test_command(command)


@pytest.mark.parametrize("command", [
    "pytest tests/test_core.py && pytest",
    "python -m unittest tests.test_core.Case.test_ok && pytest",
    "pytest tests/test_core.py && npm --prefix web test",
    "pytest tests/test_core.py && python tests/runtests.py",
])
def test_partial_compound_selection_cannot_claim_complete_targets(command):
    assert policy.test_command_targets(command) == ()
    assert not policy.is_exact_test_command(command)
    assert policy.is_broad_test_command(command)


def test_package_manager_prefix_options_still_identify_unknown_test_execution():
    assert policy.is_broad_test_command("npm --prefix web test")
    assert not policy.is_exact_test_command("npm --prefix web test -- tests/core.test.js")
    assert policy.test_command_targets("npm --prefix web test -- tests/core.test.js") == ()


def test_a_file_does_not_make_an_additional_directory_selection_exact():
    command = "pytest tests/test_core.py tests/integration"
    assert policy.test_command_targets(command) == ("tests/test_core.py", "tests/integration")
    assert not policy.is_exact_test_command(command)
    assert policy.is_broad_test_command(command)


def test_a_script_named_unittest_is_not_the_native_module_entrypoint():
    assert policy.unittest_selectors(["python", "unittest", "tests.test_core"]) is None
    assert policy.test_command_targets("python unittest tests.test_core") == ()


@pytest.mark.parametrize("command", [
    "python wrapper.py -m unittest tests.test_core",
    "python wrapper.py tests/runtests.py",
])
def test_script_parameters_do_not_upgrade_verification_kind(command):
    from nz_coder.intelligence.verification_planner import classify_verification_command

    assert classify_verification_command(command) is None


def test_native_test_entrypoints_with_unknown_or_full_selection_still_classify():
    from nz_coder.intelligence.verification_planner import classify_verification_command

    assert classify_verification_command("npm --prefix web test") == "regression"
    assert classify_verification_command("node --test") == "regression"


@pytest.mark.parametrize("command", ["npm test -- lint", "npm test -- build", "npm --prefix web test -- typecheck"])
def test_package_script_test_selectors_do_not_become_static_checks(command):
    from nz_coder.intelligence.verification_planner import classify_verification_command

    assert classify_verification_command(command) == "regression"


def test_unittest_discovery_declaration_keeps_interpreter_flags():
    command = "python -B -u -m unittest discover -s tests -p test_core.py -v"
    assert policy.declared_test_scopes(f"Run `{command}`.") == (command.removesuffix(" -v"),)


@pytest.mark.parametrize("text", [
    "不要运行 `pytest tests/test_core.py`。",
    "Do not run `python -m unittest tests.test_core.Case.test_ok`.",
    "Previously ran `pytest tests/test_core.py`; this is historical output.",
    "历史命令：`python -m unittest tests.test_core.Case.test_ok`，仅供参考。",
    "示例：`pytest tests/test_core.py`。",
    "不要使用验收命令：`pytest tests/test_core.py`。",
])
def test_negative_and_historical_commands_do_not_create_declarations(text):
    assert policy.declared_test_scopes(text) == ()
    assert extract_verification_contract(text) is None


def test_positive_unittest_instruction_creates_full_selector_contract():
    command = "python -B -m unittest -v tests.test_core.Case.test_ok"
    contract = extract_verification_contract(f"完成后运行 `{command}`。")
    assert contract is not None
    assert shlex.split(contract.command) == shlex.split(command)
    assert contract.targets == ("tests.test_core.Case.test_ok",)
    assert policy.declared_test_scopes(f"完成后运行 `{command}`。") == contract.targets


@pytest.mark.parametrize("verb", ["Run", "run"])
def test_previous_sentence_assignment_does_not_hide_positive_test_declaration(verb):
    text = f"Fix payment.py: permit refunds only when allow_refund=True. {verb} python -m pytest -q tests."
    assert policy.declared_test_scopes(text) == ("tests",)
    contract = extract_verification_contract(text)
    assert contract is not None
    assert contract.command == "python -m pytest -q tests"


@pytest.mark.parametrize("text", [
    "Run PYTHONPATH=. python -m unittest tests.test_x",
    "Run FOO=. python -m pytest tests/test_x.py",
    "Run env -C . python -m unittest tests.test_x",
    "Run env -C foo. python -m unittest tests.test_x",
    "TEST_MODE=run python -m unittest tests.test_x",
    "uv run python -m unittest tests.test_x",
    "env FOO=run python -m unittest tests.test_x",
    "Run cd nested; python -m unittest tests.test_x",
    "Run python -m unittest tests.test_a; python -m unittest tests.test_b",
])
def test_assignment_dot_is_not_a_sentence_boundary_authorizing_bare_command(text):
    assert extract_verification_contract(text) is None


@pytest.mark.parametrize("text", [
    "Run `MODE=ci python -m unittest tests.test_core.Case.test_ok`.",
    "Run `MODE=ci pytest tests/test_core.py`.",
    "Run `echo start && pytest tests/test_core.py`.",
])
def test_ordinary_declaration_does_not_authorize_shortened_command(text):
    assert extract_verification_contract(text) is None


def test_explicit_contract_preserves_environment_and_composition():
    command = "MODE=ci python -B -m unittest tests.test_core.Case.test_ok && echo checked"
    contract = extract_verification_contract(f"验收命令：`{command}`。")
    assert contract is not None
    assert contract.command == command


@pytest.mark.parametrize("environment_prefix", ["", "REPAIR_LABEL='a  b' "])
def test_configured_unittest_plans_runnable_discovery_without_package_marker(tmp_path, environment_prefix):
    from nz_coder.intelligence.verification_planner import plan_verification_commands
    from nz_coder.runtime.process.workdir import scoped_workdir

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "core.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_core.py").write_text(
        "import os, unittest\nclass CoreTests(unittest.TestCase):\n"
        "    def test_marker(self):\n        print('ACTUAL_UNPACKAGED_TEST', repr(os.environ.get('REPAIR_LABEL')))\n",
        encoding="utf-8",
    )
    configured = environment_prefix + f"{shlex.quote(sys.executable)} -B -m unittest discover -s tests -p 'test*.py' -v -k marker"
    with scoped_workdir(tmp_path):
        plan = plan_verification_commands(
            changed_files=["src/core.py"], related_tests=["tests/test_core.py"],
            project_profile={"test_roots": ["tests"], "test_commands": [configured]},
            use_repo_intelligence=False,
        )
    targeted = next(stage for stage in plan["stages"] if stage["name"] == "targeted")["commands"]
    assert targeted
    command = targeted[0]["command"]
    tokens = shlex.split(command)
    assert tokens[tokens.index(sys.executable):][:4] == [sys.executable, "-B", "-m", "unittest"]
    assert "-k marker" in command
    result = subprocess.run(command, shell=True, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ACTUAL_UNPACKAGED_TEST" in result.stdout
    if environment_prefix:
        assert "'a  b'" in result.stdout
    assert "Ran 1 test" in result.stderr
    assert not (tmp_path / "tests" / "__init__.py").exists()
    assert configured in [item["command"] for item in plan["fallback"]]


def test_explicit_unittest_selector_takes_priority_over_profile_filter(tmp_path):
    from nz_coder.intelligence.verification_planner import plan_verification_commands
    from nz_coder.runtime.process.workdir import scoped_workdir

    selector = "checks.test_core.Case.test_marker"
    configured = "python -B -m unittest discover -s checks -k unrelated -v"
    with scoped_workdir(tmp_path):
        plan = plan_verification_commands(
            changed_files=[], failing_tests=[selector],
            project_profile={"test_commands": [configured]}, use_repo_intelligence=False,
        )
    targeted = next(stage for stage in plan["stages"] if stage["name"] == "targeted")["commands"]
    assert targeted and targeted[0]["required"]
    assert shlex.split(targeted[0]["command"]) == ["python", "-B", "-m", "unittest", "-v", selector]
    assert configured in [item["command"] for item in plan["fallback"]]


@pytest.mark.parametrize("discovery_args", [
    "-s tests -p 'test*.py' -t .",
    "--start-directory tests --pattern 'test*.py' --top-level-directory .",
    "--start-directory=tests --pattern='test*.py' --top-level-directory=.",
    "tests 'test*.py' .",
])
def test_configured_unittest_preserves_top_level_for_relative_imports(tmp_path, discovery_args):
    from nz_coder.intelligence.verification_planner import plan_verification_commands
    from nz_coder.runtime.process.workdir import scoped_workdir

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "core.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "helper.py").write_text("VALUE = 7\n", encoding="utf-8")
    (tmp_path / "tests" / "test_core.py").write_text(
        "import unittest\nfrom .helper import VALUE\n"
        "class CoreTests(unittest.TestCase):\n"
        "    def test_relative_import(self):\n"
        "        self.assertEqual(VALUE, 7)\n"
        "        print('ACTUAL_PACKAGE_TEST')\n",
        encoding="utf-8",
    )
    configured = f"{shlex.quote(sys.executable)} -B -m unittest discover {discovery_args} -v"
    original = subprocess.run(configured, shell=True, cwd=tmp_path, capture_output=True, text=True)
    assert original.returncode == 0, original.stdout + original.stderr
    with scoped_workdir(tmp_path):
        plan = plan_verification_commands(
            changed_files=["src/core.py"], related_tests=["tests/test_core.py"],
            project_profile={"test_roots": ["tests"], "test_commands": [configured]},
            use_repo_intelligence=False,
        )
    targeted = next(stage for stage in plan["stages"] if stage["name"] == "targeted")["commands"]
    assert targeted
    result = subprocess.run(targeted[0]["command"], shell=True, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ACTUAL_PACKAGE_TEST" in result.stdout
    assert "Ran 1 test" in result.stderr
    assert configured in [item["command"] for item in plan["fallback"]]
