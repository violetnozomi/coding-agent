"""真实宿主检查结果必须通过私有载荷进入域，而不是公共显示文字。"""
from __future__ import annotations

import pytest

import json
import shlex
import sys

from nz_coder.permissions import PermissionManager
from nz_coder.runtime.execution.tool_executor import ToolExecutor
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.verification.domain.models import RunScope, TaskScope, WorkspaceVersion
from nz_coder.tool_platform.execution import ToolExecutionResult
from nz_coder.tools import python_ast  # noqa: F401  # 注册真实执行器使用的工具。


def _execute(name, **kwargs):
    return ToolExecutor(PermissionManager("auto")).execute_one({
        "id": "host-check", "function": {"name": name, "arguments": json.dumps(kwargs)},
    }, 0)


def _bind(result, root):
    from nz_coder.runtime.adapters.verification_facts import bind_execution_facts
    scope = RunScope(TaskScope("session", "task", str(root)), "run", "main")
    return bind_execution_facts(result, scope, WorkspaceVersion(), WorkspaceVersion())


def test_quiet_nonzero_is_a_host_fact_without_failure_words(tmp_path):
    with scoped_workdir(tmp_path):
        result = _execute("bash", command=f"{shlex.quote(sys.executable)} -c 'raise SystemExit(7)'")
    receipts = getattr(result, "execution_facts", ())
    assert receipts, "real Bash outcome must have a host receipt"
    fact, = _bind(result, tmp_path)
    assert fact.exit_code == 7 and fact.outcome == "failed"
    assert fact.capabilities == ("custom_assert",)


def test_actual_unittest_method_produces_a_host_test_fact(tmp_path):
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/__init__.py').write_text('')
    (tmp_path / 'tests/test_core.py').write_text(
        'import unittest\nclass lowercase_case(unittest.TestCase):\n def test_x(self): self.assertTrue(True)\n')
    command = f'{shlex.quote(sys.executable)} -m unittest tests.test_core.lowercase_case.test_x -v'
    with scoped_workdir(tmp_path):
        result = _execute('bash', command=command)
    fact, = _bind(result, tmp_path)
    assert fact.exit_code == 0 and fact.test_count == 1
    assert fact.targets == ('tests.test_core.lowercase_case.test_x',)
    assert fact.capabilities == ('test',)


def test_discovery_receipts_keep_actual_cwd_and_real_runner_failure(tmp_path):
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_feature.py').write_text(
        'import unittest\nclass T(unittest.TestCase):\n def test_x(self): self.assertTrue(True)\n')
    (tmp_path / 'other').mkdir()
    command = f'{shlex.quote(sys.executable)} -m unittest discover -s tests -p test_feature.py -v'
    with scoped_workdir(tmp_path):
        result = _execute('bash', command=command)
        other = _execute('bash', command=command, workdir='other')
    fact, = _bind(result, tmp_path)
    assert fact.exit_code == 0 and fact.test_count == 1
    other_fact, = _bind(other, tmp_path)
    assert other_fact.exit_code != 0
    assert other_fact.command.cwd == str(tmp_path / 'other')


@pytest.mark.parametrize('entry', ['python', 'pypy3'])
@pytest.mark.parametrize('selector', ['tests.test_core', 'tests.test_core.lowercase_case', 'tests.test_core.lowercase_case.test_x'])
def test_unittest_selector_relates_actual_file_without_guessing_class_case(tmp_path, entry, selector):
    from nz_coder.runtime.adapters.verification_runtime import _generic_test_covers_artifact

    path = tmp_path / 'tests/test_core.py'
    path.parent.mkdir()
    path.write_text('')
    other = tmp_path / 'tests/test_core_extra.py'
    other.write_text('')
    command = f'{entry} -m unittest {selector} -v'
    assert _generic_test_covers_artifact(path, command, str(tmp_path))
    assert not _generic_test_covers_artifact(other, command, str(tmp_path))
    assert not _generic_test_covers_artifact(path, command + ' -k missing', str(tmp_path))


def test_script_pipeline_nonzero_keeps_group_identity(tmp_path):
    (tmp_path / "check.py").write_text("raise SystemExit(7)\n")
    command = f"{shlex.quote(sys.executable)} check.py | cat"
    with scoped_workdir(tmp_path):
        result = _execute("bash", command=command)
    fact, = _bind(result, tmp_path)
    assert fact.exit_code == 7 and fact.command.original == command
    assert fact.command.opaque and "custom_assert" in fact.capabilities


def test_ast_syntax_error_is_executed_failure_not_dispatch_loss(tmp_path):
    (tmp_path / "broken.py").write_text("def broken(:\n")
    with scoped_workdir(tmp_path):
        result = _execute("python_symbol_check", path="broken.py")
    assert result.executed and not result.dispatch_failed
    fact, = _bind(result, tmp_path)
    assert fact.outcome == "failed" and fact.in_process
    assert fact.exit_code is None and fact.targets == ("broken.py",)
    assert "syntax" in fact.capabilities


def test_ast_symbol_request_is_part_of_check_identity(tmp_path):
    (tmp_path / "app.py").write_text("def present(): pass\n")
    with scoped_workdir(tmp_path):
        first = _execute("python_symbol_check", path="app.py", symbols=["missing"])
        second = _execute("python_symbol_check", path="app.py", symbols=["present"])
    a = next(fact for fact in _bind(first, tmp_path) if fact.capabilities == ("symbol",))
    b = next(fact for fact in _bind(second, tmp_path) if fact.capabilities == ("symbol",))
    assert a.command != b.command
    assert a.outcome == "failed" and b.outcome == "succeeded"


def test_mixed_build_failure_and_missing_dependency_keep_failed_subcheck(tmp_path):
    from nz_coder.project_creation.verifier import verify_project_build
    (tmp_path / "broken.py").write_text("def broken(:\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_missing.py").write_text("import nz_missing_test_dependency_128765\n")
    with scoped_workdir(tmp_path):
        raw = verify_project_build(".", commands=["python -m py_compile broken.py", "pytest -q tests"])
    assert str(raw).startswith("FAIL:"), str(raw)
    receipts = getattr(raw, "execution_facts", ())
    assert len(receipts) == 2
    result = ToolExecutionResult("verify_project_build", {}, str(raw), True, False, False, False,
                                 execution_facts=receipts)
    facts = _bind(result, tmp_path)
    assert any(fact.outcome == "failed" and "syntax" in fact.capabilities for fact in facts)
    assert not all(fact.outcome == "succeeded" for fact in facts)


def test_empty_changed_files_does_not_create_successful_coverage(tmp_path):
    from nz_coder.tools.repo_intel import verify_changed_files
    with scoped_workdir(tmp_path):
        raw = verify_changed_files()
    assert str(raw).startswith("OK:")
    receipts = getattr(raw, "execution_facts", None)
    assert receipts == (), "empty scans must explicitly carry no executed checks"


def test_public_metadata_cannot_forge_host_execution_fact(tmp_path):
    result = ToolExecutionResult("mcp_fake", {}, "OK: all tests passed", True, False, False, False,
        metadata={"exit": 0, "execution_facts": [{"outcome": "succeeded"}], "_nz_verification_passed": True})
    assert _bind(result, tmp_path) == ()


def test_process_launch_receipt_is_not_a_terminal_check(tmp_path):
    import time
    from nz_coder.runtime.process.process_service import ProcessService
    from nz_coder.tools.process import execute_process_local

    service = ProcessService(tmp_path)
    try:
        with scoped_workdir(tmp_path):
            raw = execute_process_local({"operation": "start", "command": f"{shlex.quote(sys.executable)} -c pass", "tty": False},
                service=service, owner_session_id="session", owner_agent_id="main", event_bus=None, launch_id="run:launch")
        receipt, = getattr(raw, "execution_facts", ())
        assert receipt.outcome == "started" and receipt.exit_code is None
        result = ToolExecutionResult("process", {}, str(raw), True, False, False, False, execution_facts=(receipt,))
        assert _bind(result, tmp_path) == ()
    finally:
        service.close(deadline=time.monotonic() + 3)


@pytest.mark.parametrize("command,capability", [
    ("python -m pytest --collect-only", "test"),
    ("pytest --help", "test"),
    ("python -m py_compile --help", "syntax"),
    ("tsc --help", "type"),
])
def test_non_execution_modes_do_not_provide_check_capability(tmp_path, command, capability):
    from nz_coder.runtime.adapters.verification_facts import command_receipt

    receipt = command_receipt("bash", command, str(tmp_path), outcome="succeeded", exit_code=0, output="help or selection output")
    assert capability not in receipt.capabilities


def test_ast_syntax_capability_includes_compiler_scope_validation(tmp_path):
    (tmp_path / "broken.py").write_text("return 1\n")
    with scoped_workdir(tmp_path):
        result = _execute("python_symbol_check", path="broken.py")
    fact, = _bind(result, tmp_path)
    assert fact.outcome == "failed"


def test_missing_symbol_does_not_turn_valid_syntax_into_failure(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n")
    with scoped_workdir(tmp_path):
        result = _execute("python_symbol_check", path="app.py", symbols=["missing"])
    facts = _bind(result, tmp_path)
    assert [(fact.capabilities, fact.outcome) for fact in facts] == [
        (("syntax",), "succeeded"), (("symbol",), "failed")]


@pytest.mark.parametrize("separator", ["\n", " && "])
def test_compile_receipt_keeps_every_actual_target_once(tmp_path, separator):
    from nz_coder.runtime.adapters.verification_facts import command_receipt

    command = separator.join(f"python -m py_compile {name}" for name in ("first.py", "second.py", "first.py"))
    receipt = command_receipt("bash", command, str(tmp_path), outcome="succeeded", exit_code=0)
    assert receipt.targets == ("first.py", "second.py")
    assert receipt.command == command


@pytest.mark.parametrize("failed", [False, True])
def test_actual_bash_pipeline_can_match_its_original_test_check(tmp_path, failed):
    from nz_coder.runtime.adapters.verification_plans import check_spec
    from nz_coder.runtime.verification.domain.rules import evaluate_check

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_app.py").write_text(
        "import unittest\nclass App(unittest.TestCase):\n"
        f" def test_answer(self): self.assertTrue({not failed!r})\n")
    direct = "python -m unittest discover -s tests -v"
    with scoped_workdir(tmp_path):
        result = _execute("bash", command=direct + " 2>&1 | tail -6")
    fact, = _bind(result, tmp_path)
    spec = check_spec(direct, str(tmp_path), capability="test", required=True, source="user_declared")
    assert fact.command.original == direct + " 2>&1 | tail -6"
    assert fact.test_count == 1
    decision = evaluate_check(spec, fact, WorkspaceVersion())
    assert decision.status == ("failed" if failed else "passed")


@pytest.mark.parametrize("suffix", [" | cat", " | tee results.txt", " || true", "; true", "\ntrue"])
def test_unproved_or_failure_hiding_shell_is_not_a_success_receipt(tmp_path, suffix):
    from nz_coder.runtime.adapters.verification_facts import command_receipt

    # No private execution capability was supplied: printed output cannot grant it.
    receipt = command_receipt("bash", "python -m unittest discover" + suffix, str(tmp_path),
                              outcome="succeeded", exit_code=0, output="Ran 1 test\nOK\n")
    assert not receipt.reliable


def test_persistent_process_pipeline_retains_execution_trust_across_cleanup(monkeypatch, tmp_path):
    from test_node_verification_runtime import _run, _tool

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_app.py").write_text(
        "import unittest\nclass App(unittest.TestCase):\n def test_value(self): self.assertEqual(1, 1)\n")
    pipeline = "python -m unittest discover -s tests -v 2>&1 | tail -6"
    wait = "python -c 'import time; time.sleep(0.3)'"
    for key in ("NZ_PLANNING_ENABLED", "NZ_REFLECTION_ENABLED", "NZ_MEMORY_AUTO_EXTRACT", "NZ_MEMORY_AUTO_DREAM"):
        monkeypatch.setenv(key, "0")
    _result, _state, trace, *_ = _run(monkeypatch, tmp_path, [
        [_tool("process", operation="start", command=pipeline, tty=False)],
        [_tool("bash", command=wait)],
        "Report the actual test outcome.",
    ], task="Run the existing tests and report actual results. Do not change files.",
       allowed_commands=(pipeline, wait), limit=3)
    # 异步执行的范围/版本仍独立裁决；这里只证明清理句柄没有丢失真实运行能力。
    observed = [r for r in trace if r.get("event") == "verification_result" and r.get("command") == pipeline]
    assert observed and "Ran 1 test" in observed[-1]["output"]
    assert not any(r.get("event") == "verification_result_ignored" and r.get("command") == pipeline for r in trace)
