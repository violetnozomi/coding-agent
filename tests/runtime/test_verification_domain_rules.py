"""验证身份和采信规则；反例对应全链路审计 F1、F5、F6。"""
from __future__ import annotations

from dataclasses import replace
import subprocess
import sys

import pytest

from nz_coder.runtime.verification.domain.identity import command_identity, commands_equivalent
from nz_coder.runtime.verification.domain.models import (
    CheckSpec, ExecutionFact, RunScope, TaskScope, WorkspaceVersion,
)
from nz_coder.runtime.verification.domain.rules import evaluate_check


def _check(tmp_path, *, capability="test", command="python -m pytest tests/test_a.py"):
    return CheckSpec("check-a", command_identity(command, str(tmp_path)), capability,
                     targets=("tests/test_a.py",))


def _fact(spec, **changes):
    version = WorkspaceVersion()
    scope = RunScope(TaskScope("session", "task", spec.command.cwd), "run", "main")
    fact = ExecutionFact("execution-a", scope, spec.command, "succeeded", version, version,
                         capabilities=(spec.capability,), targets=spec.targets,
                         exit_code=0, test_count=1)
    return replace(fact, **changes)


def test_argument_binding_distinguishes_pytest_selection(tmp_path):
    first = command_identity("python -m pytest -q tests/test_a.py --ignore tests/test_b.py", str(tmp_path))
    second = command_identity("python -m pytest -q tests/test_b.py --ignore tests/test_a.py", str(tmp_path))
    assert not commands_equivalent(first, second)
    assert commands_equivalent(first, first)


@pytest.mark.parametrize("other", [
    "python check.py --value 'a b' left right",
    "python check.py --value 'a  b' right left",
    "MODE=other python check.py --value 'a  b' left right",
])
def test_script_quoted_space_and_environment_are_identity(tmp_path, other):
    first = command_identity("MODE=expected python check.py --value 'a  b' left right", str(tmp_path))
    assert not commands_equivalent(first, command_identity(other, str(tmp_path)))
    assert not commands_equivalent(first, command_identity(first.original, str(tmp_path / "child")))


def test_pytest_module_alias_preserves_argument_bindings(tmp_path):
    assert commands_equivalent(
        command_identity("python3 -m pytest -q tests/test_a.py", str(tmp_path)),
        command_identity("pytest -q tests/test_a.py", str(tmp_path)),
    )
    assert not commands_equivalent(
        command_identity("python -m pytest -k alpha tests/test_a.py", str(tmp_path)),
        command_identity("pytest -k beta tests/test_a.py", str(tmp_path)),
    )


def test_shell_and_quoted_control_characters_are_distinct(tmp_path):
    direct = command_identity("python check.py 'a|b'", str(tmp_path))
    assert direct.opaque is False
    assert command_identity("python check.py | cat", str(tmp_path)).opaque is True
    assert not commands_equivalent(direct, command_identity("python check.py a | b", str(tmp_path)))


def test_django_alias_requires_verified_runner_and_same_selection(tmp_path):
    planned = command_identity("pytest tests/auth_tests/test_migrations.py -q", str(tmp_path))
    observed = command_identity("python3 tests/runtests.py auth_tests.test_migrations --parallel 1 -v1", str(tmp_path))
    assert not commands_equivalent(planned, observed)
    assert commands_equivalent(planned, observed, native_runner_kind="django")
    assert not commands_equivalent(
        planned, command_identity("python3 tests/runtests.py other_tests.test_case -v1", str(tmp_path)),
        native_runner_kind="django",
    )
    assert not commands_equivalent(
        command_identity("pytest tests/auth_tests/test_migrations.py --ignore tests/other.py", str(tmp_path)),
        observed, native_runner_kind="django",
    )


def test_capability_and_nonempty_coverage_are_required(tmp_path):
    spec = _check(tmp_path, capability="custom_assert", command="python check.py")
    assert evaluate_check(spec, _fact(spec, capabilities=("syntax",)), WorkspaceVersion()).status == "pending"
    assert evaluate_check(spec, _fact(spec, targets=()), WorkspaceVersion()).status == "pending"
    assert evaluate_check(spec, _fact(spec), WorkspaceVersion()).status == "passed"


@pytest.mark.parametrize("changes", [
    {"exit_code": None}, {"test_count": 0}, {"output_complete": False},
    {"scope_complete": False}, {"outcome": "cancelled"}, {"reliable": False},
    {"after": WorkspaceVersion(1, 1, 1)},
])
def test_missing_exit_zero_tests_and_version_drift_are_inconclusive(tmp_path, changes):
    spec = _check(tmp_path)
    assert evaluate_check(spec, _fact(spec, **changes), WorkspaceVersion()).status == "pending"


def test_cache_only_generation_does_not_relabel_execution(tmp_path):
    spec = _check(tmp_path)
    before, after = WorkspaceVersion(), WorkspaceVersion(1, 0, 0)
    decision = evaluate_check(spec, _fact(spec, before=before, after=after), after)
    assert decision.status == "passed"
    assert decision.evidence_generation == 1


def test_real_nonzero_survives_output_loss(tmp_path):
    spec = _check(tmp_path, command=f"{sys.executable} -c 'raise SystemExit(7)'")
    result = subprocess.run([sys.executable, "-c", "raise SystemExit(7)"], capture_output=True)
    assert result.returncode == 7 and not result.stdout and not result.stderr
    fact = _fact(spec, outcome="failed", exit_code=result.returncode, output_complete=False)
    assert evaluate_check(spec, fact, WorkspaceVersion()).status == "failed"


def test_empty_native_test_selection_remains_unconfirmed_without_a_behavior_failure(tmp_path):
    spec = _check(tmp_path, command="python -m unittest discover -k no_matching_test")
    # 纯域消费生产回执的结构化分类，不再从诊断摘要重新解析正文。
    fact = _fact(spec, outcome="failed", exit_code=5, test_count=0,
                 reason="Ran 0 tests in 0.000s\n\nNO TESTS RAN\n",
                 test_selection_failure="empty_selection")
    assert evaluate_check(spec, fact, WorkspaceVersion()).status == "pending"


@pytest.mark.parametrize("changes", [
    {"exit_code": 1},
    {"output_complete": False},
    {"reason": "Ran 0 tests\nERROR: collection failed\n"},
    {"reason": "Traceback: ImportError\nRan 0 tests\nNO TESTS RAN\n"},
])
def test_empty_selection_rule_cannot_hide_real_errors(tmp_path, changes):
    spec = _check(tmp_path, command="python -m unittest discover -k no_matching_test")
    fact = _fact(spec, outcome="failed", exit_code=5, test_count=0,
                 reason="Ran 0 tests\nNO TESTS RAN\n")
    assert evaluate_check(spec, replace(fact, **changes), WorkspaceVersion()).status == "failed"


def test_in_process_check_has_no_fabricated_exit_code(tmp_path):
    spec = _check(tmp_path, capability="symbol", command="python_symbol_check app.py")
    fact = _fact(spec, exit_code=None, in_process=True)
    assert evaluate_check(spec, fact, WorkspaceVersion()).status == "passed"


def test_aggregate_coverage_is_explicit_and_does_not_cover_other_targets(tmp_path):
    spec = CheckSpec("syntax", command_identity("verify_changed_files", str(tmp_path)), "syntax",
        targets=("app.py",), source="planner", coverage_mode="capability_targets")
    fact = _fact(spec, command=command_identity("python_symbol_check app.py", str(tmp_path)),
                 exit_code=None, in_process=True)
    assert evaluate_check(spec, fact, WorkspaceVersion()).status == "passed"
    assert evaluate_check(spec, replace(fact, targets=("other.py",), outcome="failed"), WorkspaceVersion()).status == "pending"
    assert evaluate_check(replace(spec, source="user_declared"), fact, WorkspaceVersion()).status == "pending"


def test_observational_pipeline_retry_preserves_original_selection_and_environment(tmp_path):
    from nz_coder.runtime.verification.domain.identity import commands_equivalent

    cwd = str(tmp_path)
    wrapped = command_identity("pytest test_a.py --ignore test_b.py 2>&1 | tail -4", cwd)
    exact = command_identity("pytest test_a.py --ignore test_b.py", cwd)
    assert not commands_equivalent(wrapped, exact)
    assert commands_equivalent(wrapped, exact, native_runner_kind="observed_output")
    assert not commands_equivalent(wrapped, command_identity("pytest test_b.py --ignore test_a.py", cwd), native_runner_kind="observed_output")
    assert not commands_equivalent(command_identity("MODE=one pytest test_a.py | tail -4", cwd),
                                   command_identity("MODE=two pytest test_a.py", cwd), native_runner_kind="observed_output")
    assert not commands_equivalent(command_identity("false && pytest test_a.py | tail -4", cwd),
                                   command_identity("pytest test_a.py", cwd), native_runner_kind="observed_output")


@pytest.mark.parametrize("entry", ["python", "python3.13", "/usr/bin/python3", "pypy3"])
def test_unittest_retry_ignores_only_presentation_flags(tmp_path, entry):
    from nz_coder.runtime.verification.domain.identity import commands_equivalent

    cwd = str(tmp_path)
    first = command_identity(f"{entry} -m unittest -v tests.test_core.lowercase_case.test_x", cwd)
    quiet = command_identity(f"{entry} -m unittest tests.test_core.lowercase_case.test_x --quiet", cwd)
    assert commands_equivalent(first, quiet)
    for command, directory in [
        ("python -m unittest tests.test_core.lowercase_case.test_other", cwd),
        ("python -m unittest tests.test_core.lowercase_case.test_x -k other", cwd),
        ("MODE=other python -m unittest tests.test_core.lowercase_case.test_x", cwd),
        ("python -m unittest tests.test_core.lowercase_case.test_x", str(tmp_path / "other")),
        ("rm cache && python -m unittest tests.test_core.lowercase_case.test_x", cwd),
    ]:
        assert not commands_equivalent(first, command_identity(command, directory))


def test_bare_stderr_display_wrapper_keeps_environment_and_test_identity(tmp_path):
    from nz_coder.runtime.verification.domain.identity import commands_equivalent, observed_check_subject, verified_check_subject

    cwd = str(tmp_path)
    command = "VERIFY_LABEL='a  b' go test ./oval -run Test_util"
    bare = command_identity(command, cwd)
    wrapped = command_identity(command + " 2>&1", cwd)
    assert observed_check_subject(wrapped) == bare
    assert commands_equivalent(wrapped, bare, native_runner_kind="observed_output")
    redirected = command_identity(command + ' > output.txt', cwd)
    assert commands_equivalent(wrapped, redirected, native_runner_kind="observed_output")
    assert verified_check_subject(redirected) is None
    for other in [command.replace('a  b', 'a b'), command.replace('Test_util', 'Test_other'),
                  'rm cache && ' + command]:
        assert not commands_equivalent(wrapped, command_identity(other, cwd), native_runner_kind="observed_output")


def test_new_unittest_interpreter_entries_keep_their_execution_environment(tmp_path):
    from nz_coder.runtime.verification.domain.identity import commands_equivalent

    suffix = ' -m unittest tests.test_core.Case.test_x'
    cwd = str(tmp_path)
    assert not commands_equivalent(command_identity('/venv-a/python' + suffix + ' -v', cwd),
                                   command_identity('/venv-b/python' + suffix + ' -q', cwd))
    assert not commands_equivalent(command_identity('python' + suffix + ' -v', cwd),
                                   command_identity('pypy3' + suffix + ' -q', cwd))


def test_unittest_duration_value_remains_in_the_check_identity(tmp_path):
    from nz_coder.runtime.verification.domain.identity import commands_equivalent

    command = 'python3.13 -m unittest tests.test_core.lowercase_case.test_x --durations 3'
    first = command_identity(command + ' -v', str(tmp_path))
    assert commands_equivalent(first, command_identity(command + ' -q', str(tmp_path)))
    assert not commands_equivalent(first, command_identity(command.replace('--durations 3', '--durations 4'), str(tmp_path)))
