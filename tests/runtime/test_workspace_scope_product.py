"""离线模型边界配合真实生产 Runner、文件工具和子进程验收范围结清。"""
from __future__ import annotations

import pytest

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
    assert 'changed_python_syntax' not in verification
    assert '没有本代通过的语法检查记录' not in result.final_text
    assert 'Ran 1 test' in str(requests[-1]['messages'])
    assert not (tmp_path / 'data').exists() and not (tmp_path / 'verify_http.py').exists()
    assert len(requests) == len(actions)
    assert all(allowed for _, _, allowed in permissions)


@pytest.mark.parametrize("explicit", [False, True], ids=["general-verification", "explicit-b-check"])
def test_native_untracked_bad_b_cannot_be_hidden_by_checking_only_a(monkeypatch, tmp_path, explicit):
    _settings(monkeypatch)
    (tmp_path / 'a.py').write_text('value = 1\n')
    (tmp_path / 'template.txt').write_text('def broken(:\n')
    mutate = 'cp template.txt b.py'
    check = 'python -m py_compile a.py'
    check_b = 'python -m py_compile b.py'
    request = 'Run the supplied local operation and verify the changed code.'
    actions = [[_tool('bash', command=mutate)], [_tool('bash', command=check)]]
    if explicit:
        request += f' Acceptance command: `{check_b}`.'
        actions.append([_tool('bash', command=check_b)])
    actions.extend(['The operation finished.'] * 3)
    result, state, trace, requests, *_ = _run(
        monkeypatch, tmp_path, actions, task=request,
        allowed_commands=(mutate, check, check_b) if explicit else (mutate, check), limit=len(actions),
    )
    verification = result.metadata['verification']
    assert verification['mutation_scope_unknown'] is False
    assert (tmp_path / 'b.py').read_text() == 'def broken(:\n'
    with pytest.raises(SyntaxError):
        compile((tmp_path / 'b.py').read_bytes(), 'b.py', 'exec')
    gap = {'status': 'missing_current_pass', 'count': 1, 'paths': ['b.py']}
    assert verification['changed_python_syntax'] == gap
    assert next(e for e in reversed(trace) if e['event'] == 'run_end')['changed_python_syntax'] == gap
    if explicit:
        assert result.status.value not in {'completed', 'completed_unverified'}
        declared, = verification['acceptance_checks']
        assert declared['command'] == check_b and declared['status'] == 'failed'
        assert verification['verification_needed']
    else:
        assert result.status.value == 'completed'
        assert verification['acceptance_checks'] == [] and verification['delivery_review']['accepted']
        assert "变更的 Python 文件 `b.py` 没有本代通过的语法检查记录" in result.final_text
        assert "已有通过的检查记录" in result.final_text and "验证状态：passed" not in result.final_text
        assert check in result.final_text


def test_frozen_manifest_excludes_a_deleted_source_even_if_it_reappears(tmp_path):
    from nz_coder.runtime.core.result import RunStatus
    from tests.verification_scenarios import VerificationScenario

    old = tmp_path / 'removed.py'
    old.write_text('value = 1')
    scenario = VerificationScenario(workspace=tmp_path, plan={'stages': []})
    old.unlink()
    (tmp_path / 'notes.md').write_text('Updated')
    scenario.runtime.prepare_freeze()
    assert {'removed.py', 'notes.md'} <= set(scenario.state.changed_files)
    old.write_text('return 0')
    with scenario.runtime.terminal_python_sources() as sources:
        assert sources == ()
        scenario.context.freeze_execution(RunStatus.MAX_TURNS, changed_python_sources=sources)
    assert 'changed_python_syntax' not in scenario.runtime.status()


@pytest.mark.parametrize('filename', ['pending.py', 'notes.md'])
def test_incomplete_scope_reports_known_python_candidates_without_guessing_paths(tmp_path, filename, monkeypatch):
    from nz_coder.runtime.core.result import RunStatus
    from nz_coder.runtime.process.workspace_snapshot import SnapshotError
    from nz_coder.runtime.verification.completion_gate import verification_appendix
    from tests.verification_scenarios import VerificationScenario

    scenario = VerificationScenario(workspace=tmp_path, plan={'stages': []})
    (tmp_path / filename).write_text('value = 1')
    scenario.runtime.reconcile_workspace()

    def unavailable():
        raise SnapshotError('controlled unavailable capture')

    monkeypatch.setattr(scenario.store, 'verification_snapshot', unavailable)
    scenario.runtime.prepare_freeze()
    with scenario.runtime.terminal_python_sources() as sources:
        scenario.context.freeze_execution(RunStatus.MAX_TURNS, changed_python_sources=sources)
    frozen = scenario.runtime.status()
    assert frozen['mutation_scope_unknown'] and '工作区范围尚未确认' in verification_appendix(frozen)
    if filename.endswith('.py'):
        assert frozen['changed_python_syntax'] == {'status': 'unknown'}
    else:
        assert 'changed_python_syntax' not in frozen


def test_manifest_read_allows_a_writer_and_never_freezes_coverage_from_two_versions(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import current_thread
    from nz_coder.runtime.core.result import RunStatus
    from nz_coder.runtime.process import workspace_scope
    from tests.verification_scenarios import VerificationScenario

    scenario = VerificationScenario(workspace=tmp_path, plan={'stages': []})
    source = tmp_path / 'changed.py'
    source.write_text('value = 1')
    scenario.runtime.prepare_freeze()
    original_ref = scenario.state.workspace_observation['reference']
    original_load, reader = workspace_scope.load, current_thread()

    def later_write():
        lock = scenario.context._state_lock
        assert lock.acquire(blocking=False), 'manifest I/O held the run lock'
        lock.release()
        source.write_text('value = 22')
        scenario.runtime.reconcile_workspace()

    with ThreadPoolExecutor(max_workers=1) as writers:
        def interleaved_load(store, reference):
            entries = original_load(store, reference)
            if current_thread() is reader:
                writers.submit(later_write).result(timeout=5)
            return entries

        monkeypatch.setattr(workspace_scope, 'load', interleaved_load)
        with scenario.runtime.terminal_python_sources() as sources:
            assert sources is None
            scenario.context.freeze_execution(RunStatus.MAX_TURNS, changed_python_sources=sources)
    assert scenario.state.workspace_observation['reference'] != original_ref
    assert scenario.runtime.status()['changed_python_syntax'] == {'status': 'unknown'}


def test_sealed_syntax_gap_bounds_examples_and_displays_literal_markdown_paths(tmp_path):
    from markdown_it import MarkdownIt
    from nz_coder.runtime.core.result import RunStatus
    from nz_coder.runtime.verification.completion_gate import verification_appendix
    from tests.verification_scenarios import VerificationScenario

    scenario = VerificationScenario(workspace=tmp_path, plan={'stages': []})
    paths = ('a.py', '01tick`here.py', 'x``y\nz.py', 'another.py')
    scenario.context.freeze_execution(RunStatus.MAX_TURNS, changed_python_sources=paths)
    frozen = scenario.runtime.status()
    assert frozen['changed_python_syntax'] == {'status': 'missing_current_pass', 'count': 4, 'paths': list(paths[:3])}
    text = verification_appendix(frozen)
    spans = [child.content for node in MarkdownIt().parse(text) for child in node.children or ()
             if child.type == 'code_inline']
    assert spans == ['a.py', '01tick`here.py', r'x``y\nz.py']
    assert '等 4 个文件' in text and 'another.py' not in text
