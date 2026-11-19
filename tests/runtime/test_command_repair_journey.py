"""离线生产Runner保留真实编辑、检查、负事实与完成门槛。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from tests.runtime.test_node_verification_runtime import _run, _tool


SUBJECT = 'python -m unittest tests.test_value.ValueTest.test_value -v'
STDERR_COMMAND = SUBJECT + ' 2>&1'
REDIRECT_COMMAND = SUBJECT + ' > unit-output.txt 2>&1'
READ = [_tool('read_file', path='app.py')]
EDIT_WRONG = _tool('edit_file', path='app.py', old_text='return 0', new_text='return 11')
EDIT_CORRECT = _tool('edit_file', path='app.py', old_text='return 11', new_text='return 2')
TASK = 'Fix app.py so value() returns 2 and report the actual check result.'


def _project(root):
    (root / 'app.py').write_text('def value():\n    return 0\n')
    tests = root / 'tests'
    tests.mkdir()
    (tests / '__init__.py').write_text('')
    (tests / 'test_value.py').write_text(
        'import unittest\nfrom app import value\n'
        'class ValueTest(unittest.TestCase):\n'
        ' def test_value(self):\n'
        '  print("REAL_UNITTEST_SENTINEL")\n'
        '  self.assertEqual(value(), 2)\n'
    )


def _journey(monkeypatch, workspace, name, actions, *, task=TASK):
    result, state, trace, requests, auxiliary, permissions = _run(
        monkeypatch, workspace, actions, task=task, limit=len(actions),
        allowed_commands=(SUBJECT, STDERR_COMMAND, REDIRECT_COMMAND, 'python -m py_compile app.py'),
    )
    destination = os.environ.get('NZ_COMMAND_REPAIR_EVIDENCE_DIR')
    if destination:
        root = Path(destination) / name
        root.mkdir(parents=True, exist_ok=True)
        for label, value in [('state', state), ('trace', trace), ('requests', requests),
                             ('auxiliary', auxiliary), ('permissions', permissions)]:
            (root / f'{label}.json').write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        (root / 'result.json').write_text(json.dumps({
            'status': result.status.value, 'error': str(result.error), 'final_text': result.final_text,
        }, ensure_ascii=False, indent=2) + '\n')
    return result, state, trace, requests, auxiliary, permissions


def _bash(trace, command):
    return [item for item in trace if item.get('event') == 'tool_call'
            and item.get('name') == 'bash' and item.get('input', {}).get('command') == command]


def test_explicit_unittest_stderr_success_completes_real_runner(monkeypatch, tmp_path):
    _project(tmp_path)
    result, state, trace, requests, auxiliary, permissions = _journey(
        monkeypatch, tmp_path, 'stderr-success',
        [READ, [_tool('edit_file', path='app.py', old_text='return 0', new_text='return 2')],
         [_tool('bash', command=STDERR_COMMAND)], 'The required check passes.'],
        task=TASK + f' Acceptance command: `{STDERR_COMMAND}`.',
    )
    assert (tmp_path / 'app.py').read_text() == 'def value():\n    return 2\n'
    calls = _bash(trace, STDERR_COMMAND)
    assert calls and all(item['executed'] and not item['command_failed'] for item in calls)
    assert any('REAL_UNITTEST_SENTINEL' in item['output'] and 'Ran 1 test' in item['output'] for item in calls)
    assert state['verification_contract']['passed'] is True
    assert state['verification_contract']['attempted_generation'] == state['acceptance_mutation_generation']
    assert result.status.value == 'completed', (result.error, result.final_text, state)
    boundary = [item for item in trace if item.get('event') == 'terminal_boundary_settled'][-1]
    assert boundary['contract_required'] and boundary['contract_passed']
    assert boundary['unresolved_requirements'] == []
    assert len(requests) == 4
    assert permissions and all(item[2] for item in permissions)


def test_redirected_real_failure_after_edit_prevents_runner_completion(monkeypatch, tmp_path):
    _project(tmp_path)
    result, state, trace, requests, *_ = _journey(
        monkeypatch, tmp_path, 'redirected-failure',
        [READ, [EDIT_WRONG, _tool('bash', command=REDIRECT_COMMAND)], 'The task is done.'],
        task=TASK + f' Acceptance command: `{REDIRECT_COMMAND}`.',
    )
    assert (tmp_path / 'app.py').read_text() == 'def value():\n    return 11\n'
    body = (tmp_path / 'unit-output.txt').read_text()
    assert 'Ran 1 test' in body and 'FAILED (failures=1)' in body
    calls = _bash(trace, REDIRECT_COMMAND)
    assert len(calls) == 1 and calls[0]['executed'] and calls[0]['command_failed']
    assert result.status.value != 'completed', (result.final_text, state)
    boundaries = [item for item in trace if item.get('event') == 'terminal_boundary_settled']
    assert boundaries and all(item['status'] != 'completed' for item in boundaries)
    final = [item for item in trace if item.get('event') == 'run_end'][-1]
    assert final['completion_ready'] is False and final['tests_passed'] is False
    assert len(final['test_obligations']) == 1
    assert final['test_obligations'][0]['command'] == REDIRECT_COMMAND
    assert final['test_obligations'][0]['retry_command'] == REDIRECT_COMMAND
    assert final['test_obligations'][0]['status'] == 'pending'
    assert len(requests) == 3


def test_repair_and_fresh_clean_subject_settle_redirected_failure(monkeypatch, tmp_path):
    _project(tmp_path)
    result, state, trace, requests, *_ = _journey(
        monkeypatch, tmp_path, 'redirected-repair',
        [READ, [EDIT_WRONG, _tool('bash', command=REDIRECT_COMMAND)],
         [EDIT_CORRECT], [_tool('bash', command=SUBJECT)], 'The unit test passes after the fix.'],
    )
    assert (tmp_path / 'app.py').read_text() == 'def value():\n    return 2\n'
    assert 'FAILED (failures=1)' in (tmp_path / 'unit-output.txt').read_text()
    failed = _bash(trace, REDIRECT_COMMAND)
    assert len(failed) == 1 and failed[0]['command_failed']
    passed = _bash(trace, SUBJECT)
    assert passed and all(item['executed'] and not item['command_failed'] for item in passed)
    assert any('Ran 1 test' in item['output'] and 'REAL_UNITTEST_SENTINEL' in item['output'] for item in passed)
    assert result.status.value == 'completed', (result.error, result.final_text, state)
    boundaries = [item for item in trace if item.get('event') == 'terminal_boundary_settled']
    assert boundaries[-1]['status'] == 'completed'
    assert boundaries[-1]['unresolved_requirements'] == []
    final = [item for item in trace if item.get('event') == 'run_end'][-1]
    assert final['completion_ready'] is True and final['tests_passed'] is None
    assert result.metadata['verification']['last_verification']['status'] == 'passed'
    assert not any(item['status'] == 'pending' for item in final['test_obligations'])
    assert len(requests) >= 3
