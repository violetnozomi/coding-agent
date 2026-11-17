"""检查主体、真实进程结果与验证域必须使用同一份含义。"""
from __future__ import annotations

import shlex
import sys

import pytest

from nz_coder.runtime.adapters.verification_facts import command_capabilities, command_receipt
from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
from nz_coder.runtime.verification.domain.identity import command_identity, commands_equivalent
from nz_coder.runtime.verification.verification_scheduler import VerificationScheduler
from tests.verification_scenarios import VerificationScenario


@pytest.fixture(autouse=True)
def _ordinary_local_execution():
    with scoped_runtime_overrides(strict_local_tools=False):
        yield


def _project(root, *, expected=2, diagnostic="", compiler="python", require_targeted=False):
    (root / "pkg").mkdir()
    (root / "pkg/__init__.py").write_text("")
    (root / "pkg/module.py").write_text("value = 0\n")
    (root / "tests").mkdir()
    (root / "tests/__init__.py").write_text("")
    (root / "tests/test_value.py").write_text(
        "import unittest\nfrom pkg.module import value\n"
        "class ValueTest(unittest.TestCase):\n"
        " def test_value(self):\n"
        + (f"  print({diagnostic!r})\n" if diagnostic else "")
        + f"  self.assertEqual(value, {expected})\n"
    )
    compile_command = compiler + " -m py_compile pkg/module.py"
    scenario = VerificationScenario(workspace=root, require_targeted=require_targeted, plan={
        "stages": [{"name": "static", "required": True,
                    "commands": [{"command": compile_command, "required": True}]}],
    })
    scenario.settle_shell("printf 'value = 2\\n' > pkg/module.py")
    return scenario, compile_command


def test_real_redirected_failure_blocks_after_real_source_change(tmp_path):
    vm, compile_command = _project(tmp_path, expected=3)
    assert not vm.settle_shell(compile_command).command_failed
    assert vm.runtime.status()["completion_ready"]
    result = vm.settle_shell("python -m unittest tests.test_value -v > unit-output.txt 2>&1")
    assert result.command_failed
    assert "FAILED (failures=1)" in (tmp_path / "unit-output.txt").read_text()
    status = vm.runtime.status()
    assert status["completion_ready"] is False
    assert status["tests_passed"] is False
    assert len(vm.domain.snapshot().obligations) == 1


def test_redirected_failure_is_closed_only_by_fresh_matching_success(tmp_path):
    vm, compile_command = _project(tmp_path, expected=3)
    vm.settle_shell(compile_command)
    vm.settle_shell("python -m unittest tests.test_value -v > unit-output.txt 2>&1")
    assert vm.domain.snapshot().obligations
    vm.settle_shell("printf 'value = 3\\n' > pkg/module.py")
    vm.settle_shell(compile_command)
    result = vm.settle_shell("python -m unittest tests.test_value -v")
    assert not result.command_failed
    assert "Ran 1 test" in result.output
    assert all(item.resolved for item in vm.domain.snapshot().obligations)
    assert vm.runtime.status()["completion_ready"]


def test_stderr_merge_success_retains_test_selector_and_passes(tmp_path):
    vm, compile_command = _project(tmp_path)
    vm.settle_shell(compile_command)
    result = vm.settle_shell("python -m unittest tests.test_value.ValueTest.test_value -v 2>&1")
    assert not result.command_failed
    fact = next(item for item in vm.domain.snapshot().executions if "test" in item.capabilities)
    assert fact.targets == ("tests.test_value.ValueTest.test_value",)
    assert fact.test_count == 1 and fact.reliable
    assert vm.runtime.status()["tests_passed"] is True


def test_passing_native_test_may_print_expected_error_diagnostic(tmp_path):
    vm, compile_command = _project(tmp_path, diagnostic="No module named expected_fixture (expected diagnostic)")
    vm.settle_shell(compile_command)
    result = vm.settle_shell("python -m unittest tests.test_value -v")
    assert not result.command_failed
    assert "Ran 1 test" in result.output and "OK" in result.output
    assert vm.runtime.status()["tests_passed"] is True


@pytest.mark.parametrize("command", ["go vet ./...", "cargo clippy"])
def test_static_lint_success_cannot_satisfy_behavioral_test_requirement(tmp_path, command):
    vm, _ = _project(tmp_path, require_targeted=True)
    assert command_capabilities(command) == ("lint",)
    vm.command_result({"command": command}, "", False, False, exit_code=0)
    snapshot = vm.domain.snapshot()
    assert snapshot.tests_passed is not True
    assert snapshot.targeted_pending
    assert not snapshot.execution_ready


def test_absolute_python_cache_is_not_a_new_source_change(tmp_path):
    compiler = shlex.quote(sys.executable)
    vm, compile_command = _project(tmp_path, compiler=compiler)
    before = vm.state.source_mutation_generation
    assert not vm.settle_shell(compile_command).command_failed
    assert list((tmp_path / "pkg/__pycache__").glob("module.*.pyc"))
    assert vm.state.source_mutation_generation == before
    assert vm.runtime.status()["verification_needed"] is False


def test_explicit_python_versions_cannot_resolve_each_others_checks(tmp_path):
    old = command_identity("python3.12 -m unittest tests.test_value -v", str(tmp_path))
    new = command_identity("python3.13 -m unittest tests.test_value -v", str(tmp_path))
    assert not commands_equivalent(old, new)


def test_redirected_zero_exit_without_captured_test_body_is_not_positive_proof(tmp_path):
    vm, compile_command = _project(tmp_path, require_targeted=True)
    vm.settle_shell(compile_command)
    result = vm.settle_shell("python -m unittest tests.test_value -v > unit-output.txt 2>&1")
    assert not result.command_failed
    assert "Ran 1 test" in (tmp_path / "unit-output.txt").read_text()
    fact = next(item for item in vm.domain.snapshot().executions if "test" in item.capabilities)
    assert not fact.output_complete or not fact.reliable
    assert vm.runtime.status()["tests_passed"] is not True
    assert vm.domain.snapshot().targeted_pending


def test_output_filter_retry_keeps_original_command_and_is_not_auto_relaunched(tmp_path):
    from nz_coder.runtime.adapters.verification_plans import check_spec

    vm, compile_command = _project(tmp_path)
    command = "python -m unittest tests.test_value -v 2>&1 | tail -20"
    spec = check_spec(command, str(tmp_path), capability="test", stage="targeted",
                      native_runner_kind="observed_output", source="failure_evidence",
                      targets=("tests.test_value",))
    vm.domain.register_check(spec)
    vm.command_result({"command": command}, "Ran 1 test\nFAILED (failures=1)", False, True, exit_code=1)
    status = vm.runtime.status()
    item = next(item for stage in status["verification_pipeline"]["stages"] for item in stage["commands"]
                if item["id"] == spec.id)
    assert item["retry_command"] == command
    scheduler = VerificationScheduler()
    actions = scheduler.completion_actions(status, mutation_generation=vm.state.mutation_generation)
    assert tuple(action.command for action in actions) == (compile_command,)
    assert scheduler._pending_command(status, "targeted") == ("", "")


def test_early_command_failure_does_not_claim_later_test_execution(tmp_path):
    vm, _ = _project(tmp_path)
    vm.settle_shell("false && python -m unittest tests.test_value -v")
    assert not vm.domain.snapshot().obligations
    assert not any(item.capability == "test" for item in vm.domain.snapshot().specs)


def test_plain_empty_selection_cannot_satisfy_behavioral_requirement(tmp_path):
    vm, compile_command = _project(tmp_path, require_targeted=True)
    vm.settle_shell(compile_command)
    vm.settle_shell("python -m unittest tests.test_value -k not_a_real_test -v")
    assert vm.runtime.status()["tests_passed"] is not True


def test_real_user_fake_cache_file_remains_a_mutation(tmp_path):
    vm, _ = _project(tmp_path)
    generation = vm.state.mutation_generation
    vm.settle_shell("mkdir -p __pycache__; printf x > __pycache__/fake.pyc")
    assert vm.state.mutation_generation > generation


def test_receipt_preserves_failure_when_output_is_empty(tmp_path):
    receipt = command_receipt("bash", "python -m unittest tests.test_value > unit-output.txt 2>&1",
                              str(tmp_path), outcome="failed", exit_code=1)
    assert receipt.outcome == "failed" and receipt.exit_code == 1
    assert receipt.capabilities == ("test",)


@pytest.mark.parametrize("exit_code,expected", [(0, "passed"), (None, "failed")])
def test_observational_report_only_uses_error_words_without_an_exit_status(exit_code, expected):
    from nz_coder.runtime.observability.run_evidence import RunEvidence

    evidence = RunEvidence(run_id="diagnostic-status")
    metadata = {"exit": exit_code} if exit_code is not None else {}
    evidence.record_tool_result(
        "bash", {"command": "python -m unittest tests.test_value -v"},
        "Ran 1 test\nOK\nNo module named expected_fixture (expected diagnostic)",
        success=True, metadata=metadata,
    )
    assert evidence.verification_results[0]["status"] == expected


@pytest.mark.parametrize("command", [
    "go test lint", "cargo test build", "cargo test -- lint", "npm test -- build", "npm test -- lint",
])
def test_test_selection_named_like_a_static_action_keeps_test_capability(command):
    assert command_capabilities(command) == ("test",)


def test_python_interpreter_flag_keeps_native_compiler_capability():
    assert command_capabilities("python -u -m py_compile pkg/module.py") == ("syntax",)


@pytest.mark.parametrize("command,expected", [
    ("pytest --plugin-select opaque", False),
    ("pytest tests/test_value.py::ValueTest::test_value", True),
    ("python -u -m pytest tests/test_value.py", True),
    ("pytest", True),
    ("python -m unittest --plugin-select opaque", False),
])
def test_artifact_coverage_distinguishes_unknown_from_full_selection(tmp_path, command, expected):
    from nz_coder.runtime.adapters.verification_runtime import _generic_test_covers_artifact

    path = tmp_path / "tests/test_value.py"
    path.parent.mkdir()
    path.write_text("")
    assert _generic_test_covers_artifact(path, command, str(tmp_path)) is expected


@pytest.mark.parametrize("command", ["go build ./...", "cargo build", "npm run build", "yarn build"])
def test_actual_build_failure_creates_a_static_repair_obligation(tmp_path, command):
    vm, _ = _project(tmp_path)
    assert command_capabilities(command) == ("build",)
    vm.command_result({"command": command}, "build failed", False, True, exit_code=1)
    assert len(vm.domain.snapshot().obligations) == 1
    assert not vm.runtime.status()["completion_ready"]


def test_bare_stderr_native_script_is_scheduled_with_original_environment(tmp_path):
    from nz_coder.runtime.adapters.verification_plans import check_spec

    command = "VERIFY_LABEL='a  b' python tests/runtests.py test_index 2>&1"
    vm, _ = _project(tmp_path)
    spec = check_spec(command, str(tmp_path), capability="test", stage="targeted",
                      native_runner_kind="observed_output", source="failure_evidence",
                      targets=("test_index",))
    vm.domain.register_check(spec)
    vm.command_result({"command": command}, "FAILED (failures=1)", False, True, exit_code=1)
    scheduler = VerificationScheduler()
    assert scheduler._pending_command(vm.runtime.status(), "targeted") == (command, str(tmp_path))
    actions = scheduler.completion_actions(vm.runtime.status(), mutation_generation=vm.state.mutation_generation)
    assert any(item.command == command for item in actions)


def test_python_interpreter_flag_keeps_real_pytest_cache_out_of_source_generation(tmp_path):
    vm, _ = _project(tmp_path, require_targeted=True)
    before = vm.state.source_mutation_generation
    command = shlex.quote(sys.executable) + ' -u -m pytest tests/test_value.py -q'
    result = vm.settle_shell(command)
    assert not result.command_failed, result.output
    assert (tmp_path / '.pytest_cache').is_dir()
    assert vm.state.source_mutation_generation == before
    assert vm.runtime.status()['tests_passed'] is True
