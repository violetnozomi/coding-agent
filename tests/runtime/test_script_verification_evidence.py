"""独立 Python 验证脚本的真实失败、精确复测和生产收尾回归。"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from tests.verification_scenarios import VerificationScenario
from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.runtime.verification.domain.identity import command_identity, commands_equivalent
from nz_coder.runtime.process.workdir import scoped_workdir


SUITE = 'python -m unittest discover -s tests -t .'
SCRIPT = 'python checks/interaction.py'


def _project(root):
    root.mkdir(exist_ok=True)
    (root / 'tests').mkdir()
    (root / 'tests/__init__.py').touch()
    (root / 'tests/test_api.py').write_text(
        'import unittest\nclass ApiTest(unittest.TestCase):\n'
        ' def test_shape(self): self.assertEqual({"items": []}["items"], [])\n')
    (root / 'checks').mkdir()
    (root / 'checks/interaction.py').write_text(
        'from pathlib import Path\nimport sys\n'
        'if len(sys.argv) == 1:\n'
        ' assert Path("current.txt").read_text() == "Beta", "stale interaction result"\n'
        'print("interaction check passed")\n')
    (root / 'current.txt').write_text('Alpha')


def _vm(*, declare_script=True):
    vm = VerificationScenario(workspace=current_workdir(), plan={'stages': []})
    if declare_script:
        from nz_coder.runtime.verification.domain.requirements import TaskContract
        # 精确脚本复测约束只适用于明确选定的硬验收，普通探测留作评审材料。
        vm.state.set_task_contract(TaskContract(acceptance_commands=(SCRIPT,)))
        vm.runtime.synchronize_task()
    return vm


def _observe(vm, root, command):
    result = subprocess.run(command.replace('python ', sys.executable + ' ', 1),
                            cwd=root, shell=True, capture_output=True, text=True)
    output = result.stdout + result.stderr
    vm.command_result({'command': command, 'workdir': str(root)}, output,
                    False, result.returncode != 0, exit_code=result.returncode)
    return result


def _obligation(vm, command=SCRIPT):
    matches = [item for item in vm.runtime.status()['test_obligations'] if item['command'] == command]
    assert len(matches) == 1, vm.runtime.status()
    return matches[0]


def test_script_assertion_overrides_prior_suite_pass(tmp_path):
    _project(tmp_path)
    with scoped_workdir(tmp_path):
        vm = _vm()
        assert _observe(vm, tmp_path, SUITE).returncode == 0
        assert vm.runtime.status()['verification_needed'] is True  # 明确脚本验收尚未执行。
        failed = _observe(vm, tmp_path, SCRIPT)
        assert failed.returncode == 1 and 'AssertionError' in failed.stderr
        status = vm.runtime.status()
        assert status['verification_needed'] is True
        assert status['verification_state'] == 'failed_repairable'
        assert status['last_verification']['command'] == SCRIPT
        assert _obligation(vm)['status'] == 'pending'


def test_same_script_failure_deduplicates_and_exact_success_settles(tmp_path):
    _project(tmp_path)
    with scoped_workdir(tmp_path):
        vm = _vm()
        _observe(vm, tmp_path, SCRIPT)
        _observe(vm, tmp_path, SCRIPT)
        assert _obligation(vm)['status'] == 'pending'
        (tmp_path / 'current.txt').write_text('Beta')
        assert _observe(vm, tmp_path, SCRIPT).returncode == 0
        assert _obligation(vm)['status'] == 'passed'
        assert vm.runtime.status()['verification_needed'] is False
        observed = vm.runtime.status()['verification_pipeline']['stages'][0]['commands']
        assert len(observed) == 1 and observed[0]['status'] == 'passed'


def test_other_suite_pass_cannot_clear_failed_script_after_mutation(tmp_path):
    _project(tmp_path)
    with scoped_workdir(tmp_path):
        vm = _vm()
        _observe(vm, tmp_path, SCRIPT)
        vm.mutate(1, paths=('current.txt',))
        (tmp_path / 'current.txt').write_text('Beta')
        _observe(vm, tmp_path, SUITE)
        assert vm.runtime.status()['verification_needed'] is True
        assert _obligation(vm)['status'] == 'pending'
        _observe(vm, tmp_path, SCRIPT)
        assert vm.runtime.status()['verification_needed'] is False
        assert _obligation(vm)['evidence_generation'] == 1


@pytest.mark.parametrize('changed', [SCRIPT + ' -q', SCRIPT + ' --value "a  b"'])
def test_changed_script_arguments_cannot_clear_original_failure(tmp_path, changed):
    _project(tmp_path)
    with scoped_workdir(tmp_path):
        vm = _vm()
        _observe(vm, tmp_path, SCRIPT)
        vm.mutate(1, paths=('current.txt',))
        assert _observe(vm, tmp_path, changed).returncode == 0
        assert vm.runtime.status()['verification_needed'] is True
        assert _obligation(vm)['status'] == 'pending'


def test_quoted_spaces_and_argument_order_remain_distinct(tmp_path):
    _project(tmp_path)
    with scoped_workdir(tmp_path):
        _vm()
        first = SCRIPT + ' --value "a  b" left right'
        compacted = SCRIPT + ' --value "a b" left right'
        reordered = SCRIPT + ' --value "a  b" right left'
        cwd = str(tmp_path)
        assert commands_equivalent(command_identity(first, cwd), command_identity(first, cwd))
        assert not commands_equivalent(command_identity(first, cwd), command_identity(compacted, cwd))
        assert not commands_equivalent(command_identity(first, cwd), command_identity(reordered, cwd))


def test_identical_script_in_other_directory_cannot_clear_failure(tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    _project(first)
    _project(second)
    (second / 'current.txt').write_text('Beta')
    with scoped_workdir(first):
        vm = _vm()
        _observe(vm, first, SCRIPT)
        assert _observe(vm, second, SCRIPT).returncode == 0
        obligation = _obligation(vm)
        assert obligation['scope'] == str(first) and obligation['status'] == 'pending'
        assert vm.runtime.status()['verification_needed'] is True


def test_checkpoint_preserves_failed_script_until_current_recheck(tmp_path):
    _project(tmp_path)
    with scoped_workdir(tmp_path):
        vm = _vm()
        _observe(vm, tmp_path, SCRIPT)
        frozen = json.loads(json.dumps(vm.domain.checkpoint()))
        restored = _vm()
        restored.restore(frozen, workspace=tmp_path, generation=0)
        _observe(restored, tmp_path, SUITE)
        assert _obligation(restored)['status'] == 'pending'
        assert restored.runtime.status()['verification_needed'] is True
        (tmp_path / 'current.txt').write_text('Beta')
        _observe(restored, tmp_path, SCRIPT)
        assert restored.runtime.status()['verification_needed'] is False


def test_first_successful_application_script_is_not_test_evidence(tmp_path):
    _project(tmp_path)
    (tmp_path / 'current.txt').write_text('Beta')
    with scoped_workdir(tmp_path):
        vm = _vm(declare_script=False)
        _observe(vm, tmp_path, SCRIPT)
        assert vm.runtime.status()['test_obligations'] == []
        assert vm.runtime.status()['last_verification'] is None


@pytest.mark.parametrize('command', [
    'echo "python checks/interaction.py Traceback (most recent call last):"',
    'python -c "print(\'Traceback (most recent call last):\')" --help',
    'python -m http.server --help',
    'bash -c "python checks/interaction.py"',
])
def test_non_script_execution_does_not_create_script_obligation(tmp_path, command):
    _project(tmp_path)
    with scoped_workdir(tmp_path):
        vm = _vm(declare_script=False)
        vm.command_result({'command': command}, 'Traceback (most recent call last):\nAssertionError',
                        False, True, exit_code=1)
        obligations = vm.runtime.status()['test_obligations']
        assert obligations == []


def test_native_failure_cannot_be_overridden_by_model_and_semantic_accept(monkeypatch, tmp_path):
    from test_node_verification_runtime import _run, _tool
    _project(tmp_path)
    for key in ('NZ_PLANNING_ENABLED', 'NZ_REFLECTION_ENABLED', 'NZ_MEMORY_AUTO_EXTRACT', 'NZ_MEMORY_AUTO_DREAM'):
        monkeypatch.setenv(key, '0')
    actions = [[_tool('bash', command=SUITE)], [_tool('bash', command=SCRIPT)],
               'All checks passed.', 'All checks passed.', 'All checks passed.', 'All checks passed.']
    result, state, trace, requests, *_ = _run(monkeypatch, tmp_path, actions,
        task=f'Run the existing automated suite and checks/interaction.py, then report actual results. Do not modify files. Acceptance command: `{SCRIPT}`.',
        allowed_commands=(SUITE, SCRIPT), limit=len(actions), semantic_response='accept')
    assert result.status.value != 'completed', result.final_text
    assert result.metadata['verification']['verification_needed'] is True
    assert any(e.get('event') == 'verification_result' and e.get('command') == SCRIPT and e['status'] == 'failed' for e in trace)
    assert any(SCRIPT in str(request['messages']) and 'Declared verification: status=failed' in str(request['messages']) for request in requests[3:])


def test_native_recheck_settles_script_and_produces_completed(monkeypatch, tmp_path):
    from test_node_verification_runtime import _run, _tool
    _project(tmp_path)
    for key in ('NZ_PLANNING_ENABLED', 'NZ_REFLECTION_ENABLED', 'NZ_MEMORY_AUTO_EXTRACT', 'NZ_MEMORY_AUTO_DREAM'):
        monkeypatch.setenv(key, '0')
    repair = [_tool('edit_file', path='current.txt', old_text='Alpha', new_text='Beta')]
    actions = [[_tool('bash', command=SUITE)], [_tool('bash', command=SCRIPT)],
               repair, [_tool('bash', command=SUITE)], [_tool('bash', command=SCRIPT)],
               'The suite and independent interaction check now pass.']
    result, state, trace, *_ = _run(monkeypatch, tmp_path, actions,
        task=f'Check the suite and checks/interaction.py. Repair current.txt if the interaction check fails, then rerun both checks. Acceptance command: `{SCRIPT}`.',
        allowed_commands=(SUITE, SCRIPT), limit=len(actions), semantic_response='accept')
    assert result.status.value == 'completed', (result.final_text, result.error)
    assert result.metadata['verification']['verification_needed'] is False
    results = [e['status'] for e in trace if e.get('event') == 'verification_result' and e.get('command') == SCRIPT]
    assert results == ['failed', 'passed'], results
    assert SCRIPT in '\n'.join(line for line in result.final_text.splitlines() if '已采信检查' in line)
