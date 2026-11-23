"""真实复合命令的退出、检查归属与缓存必须在同一结算中保持一致。"""
from __future__ import annotations

import shlex
import sys

import pytest

from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
from nz_coder.runtime.verification.domain.requirements import TaskContract
from tests.verification_scenarios import VerificationScenario


@pytest.fixture(autouse=True)
def _local_checks(monkeypatch):
    # 父 pytest 禁写仓库缓存；子进程仍真实产生被审查的 Python 缓存。
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    with scoped_runtime_overrides(strict_local_tools=False):
        yield


def _project(root, *, failed=False, effect=""):
    (root / "tests").mkdir()
    (root / "tests/test_sample.py").write_text(
        "import unittest\nfrom pathlib import Path\n"
        "class Sample(unittest.TestCase):\n"
        " def test_example(self):\n"
        f"  self.assertTrue({not failed!r})\n"
        + (f"  {effect}\n" if effect else ""))
    return f"{shlex.quote(sys.executable)} -m unittest discover -s tests -v"


def _scenario(root, command=""):
    return VerificationScenario(workspace=root, plan={"stages": []},
        contract=TaskContract(acceptance_commands=(command,) if command else ()))


def _settle(vm, command):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tool_platform.permissioning.command_authorization import scoped_command_authorization

    # 测试已有授权后的回执链；审批仍绑定原始完整命令、cwd 和超时。
    arguments = {"command": command, "workdir": str(vm.root), "timeout": 3}
    with scoped_workdir(vm.root), scoped_command_authorization("bash", arguments):
        result = vm.settle_shell(command)
    assert result.execution_facts, result.output
    return result


def _required(snapshot):
    return [snapshot.decision(spec.id) for spec in snapshot.specs if spec.required]


@pytest.mark.parametrize("wrapper", ["direct", "echo", "cd-echo", "cd-filter", "absolute-filter", "cd-filter-echo"])
def test_real_single_check_wrappers_preserve_acceptance_and_cache(tmp_path, wrapper):
    bare = _project(tmp_path)
    command = {
        "direct": bare,
        "echo": bare + " && echo done",
        "cd-echo": "cd . && " + bare + " && echo done",
        "cd-filter": "cd . && " + bare + " 2>&1 | tail -4",
        "absolute-filter": f"cd {shlex.quote(str(tmp_path))} && {bare} 2>&1 | tail -4",
        "cd-filter-echo": "cd . && " + bare + " 2>&1 | tail -4 && echo done",
    }[wrapper]
    vm = _scenario(tmp_path, bare)
    result = _settle(vm, command)
    snapshot = vm.domain.snapshot()
    fact, = snapshot.executions
    assert not result.command_failed, result.output
    assert fact.exit_code == 0 and fact.reliable and fact.scope_complete
    assert list((tmp_path / "tests/__pycache__").glob("*.pyc"))
    assert fact.before.acceptance_generation == fact.after.acceptance_generation == 0
    assert [decision.status for decision in _required(snapshot)] == ["passed"]
    assert fact.command.original == command


def test_exact_compound_acceptance_does_not_expire_on_its_own_cache(tmp_path):
    bare = _project(tmp_path)
    command = bare + " && echo done"
    vm = _scenario(tmp_path, command)
    _settle(vm, command)
    snapshot = vm.domain.snapshot()
    assert [decision.status for decision in _required(snapshot)] == ["passed"]
    assert snapshot.version.acceptance_generation == 0


@pytest.mark.parametrize("suffix", [
    " && echo 'Ran 900 tests'", " && printf '900 passed\\n'",
    " && echo '--- Test completed ---'", " && printf '%s\\n' '900 passed' finished",
])
def test_display_text_does_not_invent_the_subject_test_count(tmp_path, suffix):
    bare = _project(tmp_path)
    vm = _scenario(tmp_path, bare)
    result = _settle(vm, bare + suffix)
    fact, = vm.domain.snapshot().executions
    assert not result.command_failed, result.output
    assert fact.test_count == 1
    assert [decision.status for decision in _required(vm.domain.snapshot())] == ["passed"]


@pytest.mark.parametrize("filtered", [False, True])
def test_display_zero_tests_text_does_not_turn_a_pass_into_empty_selection(tmp_path, filtered):
    bare = _project(tmp_path)
    vm = _scenario(tmp_path, bare)
    command = bare + (" 2>&1 | tail -1" if filtered else "") + " && echo 'Ran 0 tests'"
    result = _settle(vm, command)
    fact, = vm.domain.snapshot().executions
    assert not result.command_failed and fact.exit_code == 0
    # tail -1 只留下 OK；echo 后的数字不能补足被裁去的运行数。
    assert fact.test_count == (None if filtered else 1)
    assert not fact.test_selection_failure
    assert [decision.status for decision in _required(vm.domain.snapshot())] == ["pending" if filtered else "passed"]


def test_real_empty_selection_stops_before_the_display_tail(tmp_path):
    bare = _project(tmp_path) + " -k no_such_test"
    vm = _scenario(tmp_path, bare)
    result = _settle(vm, bare + " && echo '900 passed'")
    fact, = vm.domain.snapshot().executions
    assert result.command_failed and fact.exit_code == 5
    assert "900 passed" not in result.output
    assert fact.test_count == 0 and fact.test_selection_failure == "empty_selection"
    assert [decision.status for decision in _required(vm.domain.snapshot())] == ["pending"]


def test_failed_display_tail_is_not_a_failed_subject_test(tmp_path):
    bare = _project(tmp_path)
    command = bare + " && printf '%d' invalid"
    vm = _scenario(tmp_path, bare)
    result = _settle(vm, command)
    snapshot = vm.domain.snapshot()
    fact, = snapshot.executions
    assert result.command_failed and fact.exit_code != 0
    assert "Ran 1 test" in result.output and "OK" in result.output
    assert [decision.status for decision in _required(snapshot)] == ["pending"]
    assert not snapshot.ready_for_completion


@pytest.mark.parametrize("declared_whole", [False, True])
def test_multiple_checks_keep_one_aggregate_fact_without_child_counts(tmp_path, declared_whole):
    bare = _project(tmp_path)
    command = bare + " && " + bare
    vm = _scenario(tmp_path, command if declared_whole else bare)
    result = _settle(vm, command)
    snapshot = vm.domain.snapshot()
    fact, = snapshot.executions
    assert not result.command_failed and fact.exit_code == 0 and fact.reliable
    assert fact.test_count is None
    assert [decision.status for decision in _required(snapshot)] == ["passed" if declared_whole else "pending"]


@pytest.mark.parametrize("wrapper", ["echo", "filter", "hidden", "newline", "nested-shell"])
def test_failed_check_never_passes_through_a_wrapper(tmp_path, wrapper):
    bare = _project(tmp_path, failed=True)
    command = {
        "echo": bare + " && echo done",
        "filter": "cd . && " + bare + " 2>&1 | tail -4",
        "hidden": bare + " || true",
        "newline": bare + "\necho done",
        "nested-shell": "sh -c " + shlex.quote(bare + " 2>&1 | tail -4"),
    }[wrapper]
    vm = _scenario(tmp_path, bare)
    result = _settle(vm, command)
    snapshot = vm.domain.snapshot()
    fact, = snapshot.executions
    assert "FAIL" in result.output
    assert [decision.status for decision in _required(snapshot)] != ["passed"]
    assert not snapshot.ready_for_completion
    assert fact.command.original == command
    if wrapper in {"echo", "filter"}:
        assert fact.exit_code != 0
    else:
        assert fact.exit_code == 0 and not fact.reliable


def test_failed_prefix_does_not_claim_the_unexecuted_check(tmp_path):
    bare = _project(tmp_path)
    vm = _scenario(tmp_path, bare)
    result = _settle(vm, "false && " + bare)
    snapshot = vm.domain.snapshot()
    assert result.command_failed and "Ran 1 test" not in result.output
    assert [decision.status for decision in _required(snapshot)] == ["pending"]
    assert snapshot.obligations == ()


@pytest.mark.parametrize("prefix", ["different", "dynamic"])
def test_directory_wrappers_do_not_borrow_the_declared_check_cwd(tmp_path, prefix):
    bare = _project(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    _project(other)
    vm = _scenario(tmp_path, bare)
    directory = shlex.quote(str(other)) if prefix == "different" else '"$PWD"'
    result = _settle(vm, f"cd {directory} && {bare} 2>&1 | tail -4")
    snapshot = vm.domain.snapshot()
    assert not result.command_failed
    assert [decision.status for decision in _required(snapshot)] == ["pending"]


@pytest.mark.parametrize("effect", ["source", "ordinary-cache-file"])
def test_compound_cache_classification_keeps_actual_input_changes(tmp_path, effect):
    code = ("Path('tests/test_sample.py').write_text('# replaced source\\n')" if effect == "source"
            else "Path('tests/__pycache__/business.json').write_text('{}')")
    bare = _project(tmp_path, effect=code)
    vm = _scenario(tmp_path, bare)
    result = _settle(vm, bare + " && echo done")
    snapshot = vm.domain.snapshot()
    assert not result.command_failed
    assert snapshot.version.acceptance_generation > 0
    assert [decision.status for decision in _required(snapshot)] == ["pending"]


@pytest.mark.parametrize("command", ["pwd; echo done", "python -c 'print(1)' ; echo done"])
def test_ordinary_probes_keep_facts_without_ignored_test_projection(tmp_path, command):
    vm = _scenario(tmp_path)
    _settle(vm, command)
    snapshot = vm.domain.snapshot()
    assert len(snapshot.executions) == 1
    assert snapshot.to_dict()["ignored_verifications"] == []


def test_explicit_unknown_check_retains_ignored_evidence_diagnostic(tmp_path):
    command = "printf 'ready\\n' | cat"
    vm = _scenario(tmp_path, command)
    _settle(vm, command)
    snapshot = vm.domain.snapshot()
    assert len(snapshot.executions) == 1
    assert snapshot.to_dict()["ignored_verifications"]
    assert [decision.status for decision in _required(snapshot)] == ["pending"]


def test_unexecuted_pytest_cannot_explain_written_pytest_cache(tmp_path):
    vm = _scenario(tmp_path)
    command = "mkdir -p .pytest_cache/v/cache && printf '{}' > .pytest_cache/v/cache/lastfailed && false && pytest"
    result = _settle(vm, command)
    snapshot = vm.domain.snapshot()
    assert result.command_failed
    assert (tmp_path / ".pytest_cache/v/cache/lastfailed").read_text() == "{}"
    assert snapshot.version.acceptance_generation > 0
    assert ".pytest_cache/v/cache/lastfailed" in vm.state.changed_files


@pytest.mark.parametrize("relative", [False, True])
def test_foreign_cpython_timestamp_cache_uses_actual_shell_interpreter(tmp_path, relative):
    import os
    import shutil

    # 在多套独立 Conda 并存的机器上，PATH 的第一个外部 Python 不一定是受支持的系统解释器。
    candidates = [os.environ.get("NZ_TEST_PROJECT_PYTHON")]
    candidates.extend(shutil.which(name, path=os.defpath) for name in ("python3", "python3.13", "python3.12"))
    other = next((path for path in candidates if path and not os.path.samefile(path, sys.executable)), None)
    if not other or not sys.platform.startswith("linux"):
        pytest.skip("requires another installed CPython on Linux")
    _project(tmp_path)
    if relative:
        link = tmp_path / ".venv/bin/python"
        link.parent.mkdir(parents=True)
        link.symlink_to(other)
        other = ".venv/bin/python"
    command = f"{shlex.quote(other)} -m unittest discover -s tests -v"
    vm = _scenario(tmp_path, command)
    result = _settle(vm, command)
    snapshot = vm.domain.snapshot()
    assert not result.command_failed and snapshot.executions[0].test_count == 1
    from nz_coder.state.sessions import _validate_json_value
    _validate_json_value(result.metadata)
    assert isinstance(result.metadata.get("python_bytecode_identity"), list)
    assert snapshot.version.acceptance_generation == 0
    assert [item.status for item in _required(snapshot)] == ["passed"]


def test_required_failure_is_visible_after_later_unregistered_scripts(tmp_path):
    from nz_coder.runtime.conversation.continuation_context import build_current_facts
    from nz_coder.runtime.adapters.verification_plans import check_spec
    from nz_coder.runtime.verification.domain.models import ExecutionFact
    from nz_coder.runtime.verification.domain.identity import command_identity

    vm = _scenario(tmp_path)
    command = "python -m pytest"
    spec = check_spec(command, str(tmp_path), capability="test", source="planner", required=True)
    vm.domain.register_check(spec)
    version = vm.domain.snapshot().version
    vm.domain.observe_execution(ExecutionFact("required", vm.domain.scope, spec.command, "failed", version, version,
        ("test",), exit_code=127, environment_error=True, output_complete=False))
    for i in range(4):
        identity = command_identity(f"bash smoke{i}.sh", str(tmp_path))
        vm.domain.observe_execution(ExecutionFact(str(i), vm.domain.scope, identity, "succeeded", version, version,
            ("custom_assert",), exit_code=0))
    block = build_current_facts(vm.state, workspace=tmp_path)
    assert 'blocked_environment' in block
    assert block.index('blocked_environment') < block.index('no matching check decision')


def test_projected_pass_survives_readme_but_expires_on_source_change(tmp_path):
    from nz_coder.runtime.conversation.continuation_context import build_current_facts
    suite = _project(tmp_path)
    vm = _scenario(tmp_path)
    _settle(vm, suite)
    vm.mutate(1, ["README.md"])
    assert "Verified check" in build_current_facts(vm.state, workspace=tmp_path)
    vm.mutate(2, ["source.py"])
    assert "Verified check" not in build_current_facts(vm.state, workspace=tmp_path)


def test_empty_discovery_and_foreign_workspace_cannot_appear_verified(tmp_path):
    from nz_coder.runtime.conversation.continuation_context import build_current_facts

    (tmp_path / "empty").mkdir()
    command = f"{shlex.quote(sys.executable)} -m unittest discover -s empty"
    vm = _scenario(tmp_path, command)
    _settle(vm, command)
    rendered = build_current_facts(vm.state, workspace=tmp_path)
    assert "no tests executed" in rendered and "Verified check (" not in rendered
    _project(tmp_path)
    _settle(vm, command.replace("-s empty", "-s tests"))
    assert "Verified check (" not in build_current_facts(vm.state, workspace=tmp_path / "empty")


def test_foreign_python_hash_cache_reports_why_it_cannot_be_exempt(tmp_path):
    import os
    import shutil
    from nz_coder.runtime.process.python_bytecode import _installed_identity

    candidates = [os.environ.get("NZ_TEST_PROJECT_PYTHON"), shutil.which("python3.13"), shutil.which("python3.12")]
    other = next((path for path in candidates if path and (identity := _installed_identity(path, tmp_path))
                  and identity[-2] != sys.implementation.cache_tag), None)
    if not other or not sys.platform.startswith("linux"):
        pytest.skip("requires a second managed CPython version on Linux")
    (tmp_path / "helper.py").write_text("answer = 42\n")
    _project(tmp_path, effect="import py_compile; py_compile.compile('helper.py', invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)")
    command = f"{shlex.quote(other)} -m unittest discover -s tests -v"
    vm = _scenario(tmp_path, command)
    result = _settle(vm, command)
    assert not result.command_failed
    [(cache_path, reason)] = result.metadata["unconfirmed_python_caches"].items()
    assert cache_path.startswith("__pycache__/helper.") and "source hash cannot be verified" in reason
    assert vm.domain.snapshot().version.acceptance_generation > 0
    assert _required(vm.domain.snapshot())[0].status != "passed"
