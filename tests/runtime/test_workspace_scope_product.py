"""离线模型边界配合真实生产 Runner、文件工具和子进程验收范围结清。"""
from __future__ import annotations

from test_node_verification_runtime import _run, _tool


def _settings(monkeypatch):
    for key in ('NZ_PLANNING_ENABLED', 'NZ_REFLECTION_ENABLED',
                'NZ_MEMORY_AUTO_EXTRACT', 'NZ_MEMORY_AUTO_DREAM'):
        monkeypatch.setenv(key, '0')


def test_native_actual_cleanup_then_tests_and_compile_can_complete(monkeypatch, tmp_path):
    _settings(monkeypatch)
    (tmp_path / 'app.py').write_text('value = 1\n')
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_app.py').write_text(
        'import unittest\nfrom app import value\n'
        'class TestApp(unittest.TestCase):\n def test_value(self): self.assertEqual(value, 2)\n')
    setup = "mkdir data; printf x > data/state; printf 'temporary = True\n' > verify_http.py"
    cleanup = 'rm -rf data verify_http.py __pycache__ && echo cleaned && ls -la'
    test = 'python -m unittest discover -s tests -p test_app.py -v'
    compile_check = 'python -m py_compile app.py'
    actions = [
        [_tool('edit_file', path='app.py', old_text='value = 1', new_text='value = 2')],
        [_tool('bash', command=setup)], [_tool('bash', command=cleanup)],
        [_tool('bash', command=test)], [_tool('bash', command=compile_check)],
        'The application has been changed and local checks passed.',
    ]
    result, state, trace, requests, auxiliary, permissions = _run(
        monkeypatch, tmp_path, actions, task='Change the application value to 2 and run local verification.',
        allowed_commands=(setup, cleanup, test, compile_check), limit=len(actions),
    )
    assert result.status.value == 'completed', (result.error, result.final_text)
    verification = result.metadata['verification']
    assert verification['verification_needed'] is False
    assert verification['mutation_scope_unknown'] is False
    assert verification['last_verification']['evidence_generation'] == state['mutation_generation']
    assert 'Ran 1 test' in str(requests[-1]['messages'])
    assert not (tmp_path / 'data').exists() and not (tmp_path / 'verify_http.py').exists()
    assert len(requests) == len(actions)
    assert all(allowed for _, _, allowed in permissions)


def test_native_untracked_bad_b_cannot_be_hidden_by_checking_only_a(monkeypatch, tmp_path):
    _settings(monkeypatch)
    (tmp_path / 'a.py').write_text('value = 1\n')
    (tmp_path / 'template.txt').write_text('def broken(:\n')
    mutate = 'cp template.txt b.py'
    check = 'python -m py_compile a.py'
    actions = [[_tool('bash', command=mutate)], [_tool('bash', command=check)],
               'The operation finished.', 'The operation finished.', 'The operation finished.']
    result, state, trace, requests, *_ = _run(
        monkeypatch, tmp_path, actions, task='Run the supplied local operation and verify the changed code.',
        allowed_commands=(mutate, check), limit=len(actions),
    )
    assert result.status.value != 'completed'
    verification = result.metadata['verification']
    assert verification['verification_needed'] is True
    assert verification['mutation_scope_unknown'] is False
    required = [item for stage in verification['verification_pipeline']['stages']
                for item in stage['commands'] if item.get('required')]
    assert any('b.py' in item['command'] and item['status'] != 'passed' for item in required)
    assert (tmp_path / 'b.py').read_text() == 'def broken(:\n'
