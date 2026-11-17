"""工作区变更、验证范围和补测接口的真实行为回归。"""
from __future__ import annotations

import copy
import subprocess
from types import MethodType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tests.verification_scenarios import VerificationScenario
from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.runtime.adapters.verification_facts import command_receipt
from nz_coder.runtime.process.workspace_scope_execution import execute_observed
from nz_coder.runtime.execution.loop import AgentLoop
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.verification.completion_gate import ordinary_verification_guidance
from nz_coder.tool_platform.execution import ToolExecutionResult, is_transactional_write_tool


COMPILE = 'python -m py_compile pkg/module.py'


def test_failed_command_keeps_execution_evidence_without_writing_model_notes(tmp_path):
    with scoped_workdir(tmp_path):
        host = _host()
        _record(host, "bash", {"command": COMPILE},
                "Traceback (most recent call last):\n  file.py\nAssertionError: broken", code=1)
        host._sp.update.assert_not_called()
        call = host.run_evidence.record_tool_result.call_args
        assert call.kwargs["command_failed"] is True
        assert call.kwargs["success"] is False




def _host():
    scenario = VerificationScenario(workspace=current_workdir())
    # 此组检查版本/范围约束，明确声明检查，避免依赖已裁掉的普通失败硬义务。
    from nz_coder.runtime.verification.domain.requirements import TaskContract
    scenario.state.set_task_contract(TaskContract(acceptance_commands=(COMPILE,)))
    scenario.runtime.synchronize_task()
    host = SimpleNamespace(
        scenario=scenario, verification_runtime=scenario.runtime,
        runtime_state=scenario.state, workdir=scenario.root, tracer=MagicMock(),
        workspace_snapshots=scenario.store, _sp=MagicMock(),
        tool_calls_this_run=0, used_save_memory=False, _sidecar_risky_shell_ops=0,
        _sidecar_unattributed_write_ops=0, _skill_loader=MagicMock(),
        _admission_session=None, run_evidence=MagicMock(), lineage=MagicMock(),
    )
    scenario.runtime.host = host
    host._skill_loader.activate_for_paths.return_value = []
    for name in ('_observe_write_tool', '_observe_run_evidence',
                 '_best_effort_tool_input', '_infer_hook_file_path'):
        setattr(host, name, MethodType(getattr(AgentLoop, name), host))
    return host


def _record(host, name, args, output, code=0, **kwargs):
    from uuid import uuid4
    start = host.verification_runtime.begin_tool({'id': uuid4().hex, 'function': {'name': name, 'arguments': args}})
    facts = (command_receipt(name, args['command'], str(args.get('workdir') or host.workdir),
        outcome='failed' if code else 'succeeded', exit_code=code, output=output),) if name == 'bash' else ()
    result = ToolExecutionResult(name, args, output, True, False, code != 0,
        is_transactional_write_tool(name), metadata=kwargs, execution_facts=facts, execution_id=start.execution_id)
    host.verification_runtime.record_result(result)
    AgentLoop._record_tool_result(host, result)
    host.verification_runtime.refresh_plan()


def _run(host, command, cwd):
    from uuid import uuid4
    args = {'command': command, 'workdir': str(cwd)}
    start = host.verification_runtime.begin_tool({'id': uuid4().hex, 'function': {'name': 'bash', 'arguments': args}})
    completed = []
    def execute():
        result = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True)
        completed.append(result)
        output = result.stdout + result.stderr
        receipt = command_receipt('bash', command, str(cwd), outcome='failed' if result.returncode else 'succeeded',
                                  exit_code=result.returncode, output=output)
        return ToolExecutionResult('bash', args, output, True, False, result.returncode != 0, False,
            execution_facts=(receipt,), metadata={'exit': result.returncode})
    result = execute_observed(host.workspace_snapshots, start.version.generation, execute,
        on_before=lambda reference: host.verification_runtime.record_before(start, reference))
    result.execution_id = start.execution_id
    host.verification_runtime.record_result(result)
    AgentLoop._record_tool_result(host, result)
    return completed[0]


def _source(root, content='def value(): return 1\n'):
    (root / 'pkg').mkdir(parents=True, exist_ok=True)
    (root / 'pkg/module.py').write_text(content)


def test_shell_mutation_invalidates_prior_pass_and_keeps_evidence_version(tmp_path):
    _source(tmp_path)
    (tmp_path / 'replacement.py').write_text('def value(: broken\n')
    with scoped_workdir(tmp_path):
        host = _host()
        _record(host, 'write_file', {'path': 'pkg/module.py'}, 'Created pkg/module.py')
        assert _run(host, COMPILE, tmp_path).returncode == 0
        before = host.verification_runtime.status()
        assert before['verification_needed'] is False
        assert _run(host, 'cp replacement.py pkg/module.py', tmp_path).returncode == 0
        assert host.runtime_state.mutation_generation == 2
        assert subprocess.run(COMPILE, shell=True, cwd=tmp_path, capture_output=True).returncode != 0
        after = host.verification_runtime.status()
        assert after['verification_needed'] is True
        assert after['verification_state'] != 'passed'
        assert any(item.after.generation == before['mutation_generation'] for item in host.scenario.domain.snapshot().executions)
        assert all(item.status != 'passed' for item in host.scenario.domain.snapshot().checks)
        assert after['mutation_generation'] == 2


def test_known_other_source_change_requires_review_without_inventing_checks(tmp_path):
    _source(tmp_path)
    (tmp_path / 'broken.py').write_text('def broken(:\n')
    with scoped_workdir(tmp_path):
        host = _host()
        _record(host, 'write_file', {'path': 'pkg/module.py'}, 'Created')
        assert _run(host, COMPILE, tmp_path).returncode == 0
        assert _run(host, 'cp broken.py pkg/second.py', tmp_path).returncode == 0
        assert _run(host, COMPILE, tmp_path).returncode == 0
        status = host.verification_runtime.status()
        assert status['completion_ready'] is False
        assert status['mutation_scope_unknown'] is False
        assert 'pkg/second.py' in host.runtime_state.changed_files
        assert status['last_verification']['status'] == 'passed'
        assert status['last_verification']['evidence_generation'] == 2
        assert 'pkg/second.py' not in ordinary_verification_guidance(status, tmp_path)

        # 该聚合工具在非 Git 项目只知道文件工具登记的路径，空 OK 也不证明 shell 范围。
        from nz_coder.tools.repo_intel import verify_changed_files
        _record(host, "verify_changed_files", {}, str(verify_changed_files()))
        assert host.verification_runtime.status()['completion_ready'] is False
        _record(host, 'edit_file', {'path': 'pkg/module.py'}, 'Edited')
        assert _run(host, COMPILE, tmp_path).returncode == 0
        assert not host.verification_runtime.status()['completion_ready']
        checkpoint = host.scenario.domain.checkpoint()
        host.scenario.restore(checkpoint, workspace=tmp_path, generation=host.runtime_state.mutation_generation)
        recovered = host.scenario.runtime.status()
        assert recovered['verification_needed'] is True


def test_readonly_command_does_not_invalidate_pass(tmp_path):
    _source(tmp_path)
    with scoped_workdir(tmp_path):
        host = _host()
        _record(host, 'write_file', {'path': 'pkg/module.py'}, 'Created pkg/module.py')
        assert _run(host, COMPILE, tmp_path).returncode == 0
        before = copy.deepcopy(host.verification_runtime.status())
        assert _run(host, 'cat pkg/module.py', tmp_path).returncode == 0
        after = host.verification_runtime.status()
        assert after['verification_needed'] == before['verification_needed']
        assert after['mutation_generation'] == before['mutation_generation']
        assert any(item.status == 'passed' for item in host.scenario.domain.snapshot().checks)


def test_documentation_and_dry_run_do_not_invalidate_code_pass(tmp_path):
    _source(tmp_path)
    with scoped_workdir(tmp_path):
        host = _host()
        _record(host, 'write_file', {'path': 'pkg/module.py'}, 'Created pkg/module.py')
        assert _run(host, COMPILE, tmp_path).returncode == 0
        _record(host, 'write_file', {'path': 'README.md'}, 'Created README.md')
        assert host.verification_runtime.status()['verification_needed'] is False
        _record(host, 'edit_file', {'path': 'pkg/module.py', 'dry_run': True}, 'Preview')
        assert host.verification_runtime.status()['verification_needed'] is False


def test_existing_test_mutation_invalidates_verification(tmp_path):
    _source(tmp_path)
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_existing.py').write_text('old = True\n')
    with scoped_workdir(tmp_path):
        host = _host()
        _record(host, 'write_file', {'path': 'pkg/module.py'}, 'Created pkg/module.py')
        assert _run(host, COMPILE, tmp_path).returncode == 0
        _record(host, 'edit_file', {'path': 'tests/test_existing.py'}, 'Edited tests/test_existing.py')
        assert host.verification_runtime.status()['verification_needed'] is True


def test_remembered_target_retains_scope_after_mutation(tmp_path):
    _source(tmp_path)
    other = tmp_path / 'other'
    (other / 'tests').mkdir(parents=True)
    (other / 'tests/test_a.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_ok(self): pass\n')
    with scoped_workdir(tmp_path):
        host = _host()
        _record(host, 'write_file', {'path': 'pkg/module.py'}, 'Created pkg/module.py')
        assert _run(host, 'python -m unittest discover -s tests', other).returncode == 0
        _record(host, 'edit_file', {'path': 'pkg/module.py'}, 'Edited pkg/module.py')
        regression = next(stage for stage in host.verification_runtime.status()['verification_pipeline']['stages'] if stage['name'] == 'regression')
        assert regression['commands'][0]['scope'] == str(other)


def test_same_shell_mutation_and_check_cannot_claim_final_version(tmp_path):
    _source(tmp_path)
    (tmp_path / 'replacement.py').write_text('def value(: broken\n')
    with scoped_workdir(tmp_path):
        host = _host()
        _record(host, 'write_file', {'path': 'pkg/module.py'}, 'Created pkg/module.py')
        assert _run(host, COMPILE, tmp_path).returncode == 0
        _run(host, COMPILE + ' && cp replacement.py pkg/module.py', tmp_path)
        assert host.verification_runtime.status()['verification_needed'] is True


def test_pipeline_scope_cannot_be_satisfied_by_other_directory(tmp_path):
    _source(tmp_path, 'def value(: broken\n')
    _source(tmp_path / 'other')
    with scoped_workdir(tmp_path):
        host = _host()
        _record(host, 'edit_file', {'path': 'pkg/module.py'}, 'Edited pkg/module.py')
        assert _run(host, COMPILE, tmp_path / 'other').returncode == 0
        status = host.verification_runtime.status()
        assert status['verification_needed'] is True
        required = next(item for stage in status['verification_pipeline']['stages'] for item in stage['commands'] if item['command'] == COMPILE and item['cwd'] == str(tmp_path))
        assert required['status'] == 'pending'
        assert required['scope'] == str(tmp_path)
        assert status['last_verification']['scope'] == str(tmp_path / 'other')


def test_pipeline_scope_normalizes_same_directory(tmp_path):
    _source(tmp_path)
    (tmp_path / 'other').mkdir()
    with scoped_workdir(tmp_path):
        host = _host()
        _record(host, 'write_file', {'path': 'pkg/module.py'}, 'Created pkg/module.py')
        assert _run(host, COMPILE, tmp_path / 'other/..').returncode == 0
        assert host.verification_runtime.status()['verification_needed'] is False
        assert host.verification_runtime.status()['last_verification']['scope'] == str(tmp_path)


def test_aggregate_evidence_retains_observed_scope_and_version(tmp_path):
    _source(tmp_path)
    with scoped_workdir(tmp_path):
        host = _host()
        _record(host, 'write_file', {'path': 'pkg/module.py'}, 'Created pkg/module.py')
        host.scenario.syntax_result('OK: changed Python files compile', passed=True)
        last = host.scenario.runtime.status()['last_verification']
        assert last['scope'] == str(tmp_path)
        assert last['evidence_generation'] == host.runtime_state.mutation_generation


def test_plan_scope_is_not_rebound_by_status_reader(tmp_path):
    _source(tmp_path)
    other = tmp_path / 'other'
    other.mkdir()
    with scoped_workdir(tmp_path):
        host = _host()
        _record(host, 'write_file', {'path': 'pkg/module.py'}, 'Created pkg/module.py')
    with scoped_workdir(other):
        command = host.verification_runtime.status()['verification_pipeline']['stages'][0]['commands'][0]
        assert command['scope'] == str(tmp_path)


def test_acceptance_contract_cannot_use_other_directory(tmp_path):
    from tests.test_runtime_state import _state_with_current_verification_contract
    from nz_coder.runtime.verification.verification_contract import VerificationContract
    other = tmp_path / 'other'
    (other / 'cron_engine/tests').mkdir(parents=True)
    (other / 'cron_engine/tests/test_a.py').write_text('def test_ok(): pass\n')
    with scoped_workdir(tmp_path):
        state = _state_with_current_verification_contract()
        command = 'python -m pytest -q cron_engine/tests'
        result = subprocess.run(command, shell=True, cwd=other, capture_output=True, text=True)
        assert result.returncode == 0
        observed = state.observe_tool('bash', {'command': command, 'workdir': str(other)},
            result.stdout, succeeded=True, metadata={'workdir': str(other)})
        assert observed is None
        assert VerificationContract.from_dict(state.verification_contract).passed is not True


@pytest.mark.parametrize('invalid_count', [1, 7])
def test_guidance_keeps_original_commands_without_guessing_paths(tmp_path, invalid_count):
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_live.py').write_text('def test_live(): pass\n')
    snapshot = {
        'verification_pipeline': {'stages': [{'commands': [
            {'command': f'python -m py_compile .verify/missing_{index}.py', 'required': True, 'status': 'pending'}
            for index in range(invalid_count)
        ]}]},
        'test_obligations': [{'command': 'pytest tests/test_live.py', 'cwd': '.', 'status': 'pending'}],
    }
    original = copy.deepcopy(snapshot)
    guidance = ordinary_verification_guidance(snapshot, tmp_path)
    assert '.verify/missing_0.py' in guidance
    assert ('pytest tests/test_live.py' in guidance) is (invalid_count < 6)
    assert snapshot == original


@pytest.mark.parametrize('blocker_key', ['verification_unavailable', 'environment_blocker'])
def test_guidance_is_not_globally_suppressed_by_one_command_blocker(tmp_path, blocker_key):
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_live.py').write_text('def test_live(): pass\n')
    snapshot = {blocker_key: {'command': 'node --test missing.cjs', 'cwd': '.', 'reason': 'unavailable'},
                'test_obligations': [{'command': 'pytest tests/test_live.py', 'cwd': '.', 'status': 'pending'}]}
    assert 'pytest tests/test_live.py' in ordinary_verification_guidance(snapshot, tmp_path)
