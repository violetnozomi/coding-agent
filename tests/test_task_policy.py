"""Tests for language/task policy helpers."""
from __future__ import annotations

import pytest


def test_is_test_file_handles_common_language_conventions():
    from nz_coder.runtime.agent.task_policy import is_test_file

    assert is_test_file("tests/test_app.py")
    assert is_test_file("src/__tests__/widget.test.tsx")
    assert is_test_file("pkg/server_test.go")
    assert is_test_file("spec/models/user_spec.rb")
    assert is_test_file("testing/python/fixtures.py")
    assert not is_test_file("src/app.ts")


def test_broad_test_detection_is_language_aware():
    from nz_coder.runtime.agent.task_policy import is_broad_test_command

    assert is_broad_test_command("pytest tests/")
    assert not is_broad_test_command("pytest tests/test_app.py::test_ok")
    assert is_broad_test_command("npm test")
    assert is_broad_test_command("go test ./...")
    assert not is_broad_test_command("go test ./pkg -run TestParser")


def test_exact_test_detection_requires_a_test_runner():
    """A source filename alone must not turn shell reads/writes into test runs."""
    from nz_coder.runtime.agent.task_policy import is_exact_test_command

    assert is_exact_test_command("pytest -q tests/test_app.py")
    assert is_exact_test_command("go test ./pkg -run TestParser")
    assert not is_exact_test_command("touch src/generated.py")
    assert not is_exact_test_command("cat src/app.py")


@pytest.mark.parametrize("command", [
    "ls node_modules/.bin | grep -i jest",
    "grep -n '\"test\"\\|jest' package.json | head -40",
    "ls node_modules/.bin/ | grep -iE 'jest|tsc'",
    "find packages/shared -maxdepth 2 -name 'jest*' | head",
    "printf '%s\\n' 'pytest tests/'",
    "cat jest.config.js",
    "rg 'pytest; jest' README.md",
])
def test_querying_test_runner_names_is_not_test_execution(command):
    """搜索参数和文件名不能让只读查询消耗验证准入或验证次数。"""
    from nz_coder.runtime.agent.task_policy import is_broad_test_command, is_exact_test_command

    assert is_broad_test_command(command) is False
    assert is_exact_test_command(command) is False


@pytest.mark.parametrize("command", ["", None])
def test_empty_command_is_not_test_execution(command):
    from nz_coder.runtime.agent.task_policy import is_broad_test_command, is_exact_test_command

    assert is_broad_test_command(command) is False
    assert is_exact_test_command(command) is False


@pytest.mark.parametrize("command", [
    "ls node_modules/.bin | grep jest && pytest",
    "printf '%s\\n' 'pytest tests/fake_test.py'; pytest",
    "cat tests/test_app.py && pytest tests/",
    "env MODE=test pytest tests/",
    "bash -c 'ls node_modules/.bin | grep jest; pytest'",
])
def test_observation_segments_cannot_hide_a_real_broad_test(command):
    from nz_coder.runtime.agent.task_policy import is_broad_test_command

    assert is_broad_test_command(command) is True


@pytest.mark.parametrize("command", [
    "npx jest", "command pytest", "uv run pytest tests/",
    "find . -exec pytest {} +", "printf '%s\\n' \"$(pytest)\"",
    "echo pytest | sh", "printf '%s\\n' pytest | xargs",
    "custom-runner jest", "pytest tests/test_ok.py && pytest",
])
def test_real_or_unresolved_execution_keeps_broad_test_observation(command):
    from nz_coder.runtime.agent.task_policy import is_broad_test_command, is_exact_test_command

    assert is_broad_test_command(command) is True
    assert is_exact_test_command(command) is False


def test_declared_test_scopes_extract_directory_suite_from_user_request():
    from nz_coder.runtime.agent.task_policy import declared_test_scopes

    assert declared_test_scopes(
        "实现别名，然后运行 `python -m pytest -q cron_engine/tests`。"
    ) == ("cron_engine/tests",)


@pytest.mark.parametrize("entry", ["python", "python3", "python3.13", "pypy3", "/usr/bin/python3"])
@pytest.mark.parametrize("selector", [
    "tests.test_core", "tests.test_core.lowercase_case",
    "tests.test_core.lowercase_case.test_next_id_is_lifted_above_existing_ids",
])
def test_unittest_dotted_selection_is_exact_and_preserves_targets(entry, selector):
    from nz_coder.runtime.agent.task_policy import (
        is_broad_test_command, is_exact_test_command, test_command_targets,
    )

    command = f"{entry} -m unittest -v {selector} -k next_id"
    assert is_exact_test_command(command)
    assert not is_broad_test_command(command)
    assert test_command_targets(command) == (selector,)


@pytest.mark.parametrize("arguments", [
    "", "-v", "-k next_id", "-k", "discover -s tests -p test_core.py -t .",
    "-s tests tests.test_core", "--unknown tests.test_core",
])
def test_unittest_discovery_and_option_values_are_not_exact_selectors(arguments):
    from nz_coder.runtime.agent.task_policy import (
        is_broad_test_command, is_exact_test_command, test_command_targets,
    )

    command = "python -m unittest " + arguments
    assert not is_exact_test_command(command)
    assert is_broad_test_command(command)
    assert test_command_targets(command) == ()


def test_declared_unittest_scope_preserves_method_without_widening():
    from nz_coder.runtime.agent.task_policy import declared_test_scopes

    selector = "tests.test_core.StorageTests.test_next_id"
    assert declared_test_scopes(f"修复后运行 `python3.13 -m unittest {selector} -v`。") == (selector,)


def test_unittest_file_and_multiple_selectors_keep_actual_selection():
    from nz_coder.runtime.agent.task_policy import is_exact_test_command, test_command_targets

    command = "env MODE=test python3 -m unittest ./tests/test_core.py tests.test_other.Case.test_x -q"
    assert is_exact_test_command(command)
    assert test_command_targets(command) == ("tests/test_core.py", "tests.test_other.Case.test_x")


def test_pytest_filter_named_unittest_does_not_change_the_runner():
    from nz_coder.runtime.agent.task_policy import test_command_targets

    assert test_command_targets("pytest tests/test_core.py -k unittest") == ("tests/test_core.py",)


def test_declared_discovery_keeps_the_complete_selection():
    from nz_coder.runtime.agent.task_policy import (
        declared_test_scopes, is_broad_test_command,
        is_exact_test_command, test_command_targets,
    )

    command = 'python -m unittest discover -s tests -t . -p test_feature.py -v'
    assert declared_test_scopes(f'Acceptance command: `{command}`.') == (command.removesuffix(' -v'),)
    assert is_broad_test_command(command) and not is_exact_test_command(command)
    assert test_command_targets(command) == ()


@pytest.mark.parametrize('command', [
    'env MODE=test python -m unittest discover -s tests',
    'MODE=test python -m unittest discover -s tests',
    'false && python -m unittest discover -s tests',
    'python -m unittest discover -s tests | cat',
    'python -m unittest discover -s $TESTS',
    'python -m unittest discover -p test*.py',
    'python -m unittest discover --unknown tests',
    'python -m unittest discover -p -v',
])
def test_unproved_discovery_declaration_does_not_claim_selection(command):
    from nz_coder.runtime.agent.task_policy import declared_test_scopes

    assert declared_test_scopes(f'Run `{command}`.') == ()


def test_discovery_pattern_named_like_display_flag_is_preserved():
    from nz_coder.runtime.agent.task_policy import declared_test_scopes

    first = 'python -m unittest discover --pattern=-v -q'
    second = 'python -m unittest discover --pattern=-q -v'
    assert declared_test_scopes(f'Run `{first}`') == ('python -m unittest discover --pattern=-v',)
    assert declared_test_scopes(f'Run `{second}`') == ('python -m unittest discover --pattern=-q',)


@pytest.mark.parametrize('selector', [
    'tests.test_core', 'tests.test_core.lowercase_case', 'tests.test_core.lowercase_case.test_x',
])
@pytest.mark.parametrize('option', ['--durations 3', '--durations=3'])
def test_unittest_durations_keeps_the_explicit_selector(selector, option):
    from nz_coder.intelligence.verification_planner import classify_verification_command
    from nz_coder.runtime.agent.task_policy import is_broad_test_command, is_exact_test_command, test_command_targets

    command = f'python3.13 -m unittest {selector} {option} -v'
    assert is_exact_test_command(command)
    assert not is_broad_test_command(command)
    assert test_command_targets(command) == (selector,)
    assert classify_verification_command(command) == 'targeted'


@pytest.mark.parametrize('option', ['--durations', '--durations=bad', '--durations nope'])
def test_invalid_unittest_durations_does_not_grant_exact_scope(option):
    from nz_coder.runtime.agent.task_policy import is_exact_test_command, test_command_targets

    command = f'python3.13 -m unittest tests.test_core {option}'
    assert not is_exact_test_command(command)
    assert test_command_targets(command) == ()


def test_explicit_test_changes_remain_distinct_from_running_tests():
    from nz_coder.runtime.agent.task_policy import task_forbids_test_changes, task_wants_tests

    assert task_wants_tests("please add unit tests") is True
    assert task_wants_tests("Fix parser.py, do not modify tests, then run pytest -q tests/test_parser.py") is False
    assert task_wants_tests("修复 parser.py，不修改测试，完成后运行 pytest -q tests/test_parser.py") is False
    assert task_forbids_test_changes("do not modify the tests") is True
    assert task_forbids_test_changes("不修改测试文件") is True


def test_native_bin_test_counts_as_exact_without_requiring_a_py_suffix():
    from nz_coder.runtime.agent.task_policy import is_broad_test_command, is_exact_test_command
    assert is_exact_test_command("python3 bin/test test_sympify --no-colors")
    assert not is_broad_test_command("python3 bin/test test_sympify --no-colors")
    assert is_broad_test_command("python3 bin/test")


def test_read_then_implement_document_is_task_reference():
    from nz_coder.runtime.agent.task_policy import classify_instruction_paths
    prompt = ("请阅读当前目录 requirements.md，从零完成其中的 TaskBoard Lite 项目。"
              "先创建 taskboard/__init__.py，然后创建 taskboard/__main__.py。不要修改需求文件。")
    roles = classify_instruction_paths(prompt)
    assert [(r.path, r.role, r.operation) for r in roles] == [
        ("requirements.md", "task_reference", ""),
        ("taskboard/__init__.py", "mutation", "create"),
        ("taskboard/__main__.py", "mutation", "create"),
    ]
    for prompt in ("阅读 requirements.md 并总结其中的要求", "阅读 requirements.md，然后修改 api.py", "不要阅读 requirements.md 并完成其中的项目"):
        assert all(r.role != "task_reference" for r in classify_instruction_paths(prompt))
    roles = classify_instruction_paths("阅读 README.md 了解背景，阅读 requirements.md 并完成其中的项目。修改 README.md，修改 api.py。")
    assert [r.path for r in roles if r.role == "task_reference"] == ["requirements.md"]
    assert {r.path for r in roles if r.role == "mutation"} == {"README.md", "api.py"}


@pytest.mark.parametrize("text", [
    "修复 minitasks.py，使 test_minitasks.py 的15项验收全部通过。不要修改验收文件。",
    "修复 minitasks.py，让 test_minitasks.py 的测试通过。不要修改测试文件。",
    "Fix minitasks.py and make test_minitasks.py pass. Do not modify tests.",
    "修复 minitasks.py 和 test_minitasks.py。不要修改 test_minitasks.py。",
])
def test_acceptance_file_is_not_promoted_to_a_write_target(text):
    from nz_coder.runtime.agent.task_policy import classify_instruction_paths

    assert [r.path for r in classify_instruction_paths(text) if r.role == "mutation"] == ["minitasks.py"]


def test_explicit_changes_to_both_files_still_allow_test_work():
    from nz_coder.runtime.agent.task_policy import classify_instruction_paths

    assert [r.path for r in classify_instruction_paths("修改 minitasks.py 和 test_minitasks.py，补充测试。") if r.role == "mutation"] == [
        "minitasks.py", "test_minitasks.py",
    ]
