"""真实文件与 shell 副作用必须按实际范围结算，而非从命令文本猜路径。"""
from __future__ import annotations

import copy
import subprocess
import sys
from types import MethodType
from unittest.mock import MagicMock

import pytest

from nz_coder.runtime.execution.loop import AgentLoop
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.process.workspace_snapshot import WorkspaceSnapshotStore
from nz_coder.runtime.process.workspace_snapshot import SnapshotError
from nz_coder.tool_platform.execution import ToolExecutionResult
from nz_coder.runtime.verification.domain.requirements import TaskContract
from test_verification_interfaces import COMPILE, _host, _source


def _scope_host(root, **limits):
    host = _host()
    host._sp = MagicMock()
    host.workspace_snapshots = WorkspaceSnapshotStore(root, root.parent / 'scope-store', **limits)
    host.verification_runtime.store = host.workspace_snapshots
    host.hooks = MagicMock()
    host.hooks.before_tool_use.return_value = None
    for name in ('_best_effort_tool_input', '_infer_hook_file_path'):
        setattr(host, name, MethodType(getattr(AgentLoop, name), host))
    host.executor = MagicMock()

    def execute(call, _):
        args = call['function']['arguments']
        result = subprocess.run(args['command'], shell=True, cwd=root, text=True, capture_output=True)
        from nz_coder.runtime.adapters.verification_facts import command_receipt
        receipt = command_receipt('bash', args['command'], str(root), outcome='failed' if result.returncode else 'succeeded', exit_code=result.returncode, output=result.stdout + result.stderr)
        return ToolExecutionResult('bash', args, result.stdout + result.stderr,
                                   True, False, result.returncode != 0, False,
                                   metadata={'exit': result.returncode, 'workdir': str(root)}, execution_facts=(receipt,))

    host.executor.execute_one.side_effect = execute
    return host


def _execute(host, command, *, settle=True):
    from uuid import uuid4
    call = {'id': uuid4().hex, 'function': {'name': 'bash', 'arguments': {'command': command}}}
    if settle:
        result = AgentLoop._execute_tool_call_with_hooks(host, call, 0, [])
        AgentLoop._record_tool_result(host, result)
    else:
        # 在真实下层执行边界保留尚未交接的回执，模拟晚到事实；不延迟正常宿主交接。
        from nz_coder.runtime.adapters.verification_runtime import scoped_tool_verification
        from nz_coder.runtime.process.workspace_scope_execution import execute_observed
        start = host.verification_runtime.begin_tool(call)
        with scoped_tool_verification(start):
            result = execute_observed(host.workspace_snapshots, start.version.generation,
                lambda: host.executor.execute_one(call, 0),
                on_before=lambda reference: host.verification_runtime.record_before(start, reference))
        result.execution_id = start.execution_id
    return result


def test_proved_noop_shell_does_not_create_unknown_mutation(tmp_path):
    with scoped_workdir(tmp_path):
        host = _scope_host(tmp_path)
        _execute(host, 'rm -f never-created.tmp')
        assert host.runtime_state.mutation_generation == 0
        assert host.verification_runtime.status()['mutation_scope_unknown'] is False


def test_shell_actual_changes_cover_untracked_files_and_failed_partial_writes(tmp_path):
    _source(tmp_path)
    with scoped_workdir(tmp_path):
        host = _scope_host(tmp_path)
        result = _execute(host, "printf 'def bad(:' > pkg/new.py; exit 1")
        assert result.command_failed is True
        assert host.runtime_state.last_mutation_paths == ('pkg/new.py',)
        assert host.runtime_state.mutation_generation == 1
        assert host.verification_runtime.status()['mutation_scope_unknown'] is False
        assert host.verification_runtime.status()['verification_needed'] is True


def test_standalone_python_verification_cache_does_not_invalidate_itself(tmp_path):
    _source(tmp_path)
    with scoped_workdir(tmp_path):
        host = _scope_host(tmp_path)
        _execute(host, "printf '\nvalue = 2\n' >> pkg/module.py")
        generation = host.runtime_state.mutation_generation
        _execute(host, 'python -m py_compile pkg/module.py')
        assert host.runtime_state.mutation_generation == generation
        assert host.verification_runtime.status()['verification_needed'] is False


def test_arbitrary_shell_cache_write_is_a_real_mutation(tmp_path):
    with scoped_workdir(tmp_path):
        host = _scope_host(tmp_path)
        _execute(host, "mkdir __pycache__; printf x > __pycache__/fake.pyc")
        assert host.runtime_state.mutation_generation == 1
        assert '__pycache__/fake.pyc' in host.runtime_state.last_mutation_paths


def test_incomplete_scope_cannot_promote_a_verification_pass(tmp_path):
    _source(tmp_path)
    with scoped_workdir(tmp_path):
        host = _scope_host(tmp_path, max_file_bytes=1)
        _execute(host, 'python -m py_compile pkg/module.py')
        assert host.verification_runtime.status()['verification_needed'] is True
        assert host.verification_runtime.status()['mutation_scope_unknown'] is True


def test_denied_shell_does_not_record_a_mutation(tmp_path):
    with scoped_workdir(tmp_path):
        host = _scope_host(tmp_path)
        host.hooks.before_tool_use.return_value = type('Decision', (), {
            'rejected': True, 'message': 'denied', 'hook_id': 'test'})()
        _execute(host, 'rm -rf data')
        host.executor.execute_one.assert_not_called()
        assert host.runtime_state.mutation_generation == 0


def test_stale_execution_generation_cannot_relabel_old_positive(tmp_path):
    _source(tmp_path)
    with scoped_workdir(tmp_path):
        host = _scope_host(tmp_path)
        _execute(host, "printf '\nvalue = 2\n' >> pkg/module.py")
        old = _execute(host, 'python -m py_compile pkg/module.py', settle=False)
        _execute(host, "printf 'def broken(:' > pkg/module.py")
        host.verification_runtime.record_result(copy.deepcopy(old))
        AgentLoop._record_tool_result(host, copy.deepcopy(old))
        assert host.verification_runtime.status()['verification_needed'] is True
        assert host.verification_runtime.status()['last_verification']['status'] == 'pending'


def _dynamic_host(root):
    host = _scope_host(root)

    def plan(paths):
        return {'stages': [{'name': 'static', 'required': True, 'commands': [
            {'command': f'python -m py_compile {path}', 'required': True}
            for path in paths if path.endswith('.py') and (root / path).is_file()
        ]}]}

    from nz_coder.runtime.adapters.verification_plans import register_verification_plan
    host.verification_runtime.refresh_plan = lambda: register_verification_plan(host.scenario.domain, plan(host.runtime_state.changed_files))
    return host


def test_cleanup_then_current_checks_closes_the_original_unverified_pattern(tmp_path):
    _source(tmp_path)
    with scoped_workdir(tmp_path):
        host = _dynamic_host(tmp_path)
        _execute(host, "printf 'temporary = True\n' > verify_http.py")
        _execute(host, 'rm -rf data verify_http.py __pycache__ && echo cleaned')
        assert host.verification_runtime.status()['mutation_scope_unknown'] is False
        _execute(host, 'python -m py_compile pkg/module.py')
        assert host.verification_runtime.status()['verification_needed'] is False


def test_full_unknown_interval_adds_bad_b_and_only_fresh_complete_checks_can_close(tmp_path, monkeypatch):
    _source(tmp_path)
    with scoped_workdir(tmp_path):
        host = _dynamic_host(tmp_path)
        _execute(host, "printf '\nvalue = 2\n' >> pkg/module.py")
        real_capture = host.workspace_snapshots.verification_snapshot
        monkeypatch.setattr(host.workspace_snapshots, 'verification_snapshot',
                            lambda **_: (_ for _ in ()).throw(SnapshotError('incomplete')))
        _execute(host, "printf 'def bad(:' > pkg/new.py")
        unknown = host.runtime_state.mutation_generation
        assert host.verification_runtime.status()['mutation_scope_unknown']
        # 中间的已知变更不能忘掉此前未知；最新一次未知也不能覆盖较早锚点。
        monkeypatch.setattr(host.workspace_snapshots, 'verification_snapshot', real_capture)
        _execute(host, "printf '\nvalue = 3\n' >> pkg/module.py")
        monkeypatch.setattr(host.workspace_snapshots, 'verification_snapshot',
                            lambda **_: (_ for _ in ()).throw(SnapshotError('incomplete')))
        _execute(host, 'rm -f no-such-file')
        monkeypatch.setattr(host.workspace_snapshots, 'verification_snapshot', real_capture)
        _execute(host, 'python -m py_compile pkg/module.py')
        status = host.verification_runtime.status()
        assert status['mutation_generation'] >= unknown
        assert status['mutation_scope_unknown'] is False
        assert 'pkg/new.py' in host.runtime_state.changed_files
        new_command = 'python -m py_compile pkg/new.py'
        snap = host.scenario.domain.snapshot()
        assert any(s.command.original == new_command and not s.required for s in snap.specs)
        # 扫描先找回新文件。用户随后要求检查它，这条检查才构成硬验收。
        host.runtime_state.set_task_contract(TaskContract(acceptance_commands=(COMPILE, new_command)))
        host.verification_runtime.synchronize_task()
        assert host.verification_runtime.status()['verification_needed']
        assert _execute(host, 'python -m py_compile pkg/new.py').command_failed is True
        _execute(host, "printf 'value = 1\n' > pkg/new.py")
        _execute(host, 'python -m py_compile pkg/module.py')
        assert host.verification_runtime.status()['verification_needed'] is True
        _execute(host, 'python -m py_compile pkg/new.py')
        assert host.verification_runtime.status()['mutation_scope_unknown'] is False, host.verification_runtime.status()
        assert host.verification_runtime.status()['verification_needed'] is False, host.verification_runtime.status()


def test_missing_pre_unknown_anchor_cannot_be_reconstructed_from_current_files(tmp_path, monkeypatch):
    _source(tmp_path)
    with scoped_workdir(tmp_path):
        host = _dynamic_host(tmp_path)
        real_capture = host.workspace_snapshots.verification_snapshot
        monkeypatch.setattr(host.workspace_snapshots, 'verification_snapshot',
                            lambda **_: (_ for _ in ()).throw(SnapshotError('incomplete')))
        _execute(host, "printf 'value = 2\n' > pkg/module.py")
        monkeypatch.setattr(host.workspace_snapshots, 'verification_snapshot', real_capture)
        _execute(host, 'python -m py_compile pkg/module.py')
        assert host.verification_runtime.status()['verification_needed'] is True
        assert host.verification_runtime.status()['mutation_scope_unknown'] is True


def test_unknown_checkpoint_restores_anchor_but_requires_fresh_checks(tmp_path, monkeypatch):

    _source(tmp_path)
    with scoped_workdir(tmp_path):
        host = _dynamic_host(tmp_path)
        _execute(host, "printf '\nvalue = 2\n' >> pkg/module.py")
        real_capture = host.workspace_snapshots.verification_snapshot
        monkeypatch.setattr(host.workspace_snapshots, 'verification_snapshot',
                            lambda **_: (_ for _ in ()).throw(SnapshotError('incomplete')))
        _execute(host, 'rm -f missing.tmp')
        monkeypatch.setattr(host.workspace_snapshots, 'verification_snapshot', real_capture)
        generation = host.runtime_state.mutation_generation
        saved = host.scenario.domain.checkpoint()
        refresh = host.verification_runtime.refresh_plan
        observation = dict(host.runtime_state.workspace_observation)
        host.scenario.restore(saved, workspace=tmp_path, generation=generation)
        host.runtime_state = host.scenario.state
        host.runtime_state.workspace_observation = observation
        host.runtime_state.changed_files = ['pkg/module.py']
        host.verification_runtime = host.scenario.runtime
        host.verification_runtime.host = host
        host.verification_runtime.store = host.workspace_snapshots
        host.verification_runtime.refresh_plan = refresh
        assert host.verification_runtime.status()['verification_needed'] is True
        assert host.verification_runtime.status()['mutation_scope_unknown'] is True
        _execute(host, 'python -m py_compile pkg/module.py')
        assert host.verification_runtime.status()['verification_needed'] is False
        assert host.verification_runtime.status()['mutation_scope_unknown'] is False


def test_before_anchor_survives_first_incomplete_after_scan(tmp_path, monkeypatch):
    _source(tmp_path)
    with scoped_workdir(tmp_path):
        host = _dynamic_host(tmp_path)
        real_capture = host.workspace_snapshots.verification_snapshot
        calls = 0

        def capture(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise SnapshotError('after failed')
            return real_capture(**kwargs)

        monkeypatch.setattr(host.workspace_snapshots, 'verification_snapshot', capture)
        _execute(host, "printf '\nvalue = 2\n' >> pkg/module.py")
        assert host.verification_runtime.status()['mutation_scope_unknown'] is True
        # 验证范围恢复后的既有义务，避免动态规划晚注册影响本次事实交接。
        from nz_coder.runtime.adapters.verification_plans import register_verification_plan
        register_verification_plan(host.scenario.domain, {'stages': [{
            'name': 'static', 'required': True, 'commands': [
                {'command': 'python -m py_compile pkg/module.py', 'required': True},
            ],
        }]})
        _execute(host, 'python -m py_compile pkg/module.py')
        assert host.verification_runtime.status()['verification_needed'] is False
        assert host.verification_runtime.status()['mutation_scope_unknown'] is False
        assert 'pkg/module.py' in host.runtime_state.changed_files


def test_late_execution_cannot_cross_unknown_scope_with_unchanged_generation(tmp_path, monkeypatch):
    from nz_coder.runtime.verification.domain.requirements import TaskContract

    _source(tmp_path)
    with scoped_workdir(tmp_path):
        host = _scope_host(tmp_path)
        command = 'python -c "print(1)"'
        host.runtime_state.set_task_contract(TaskContract(acceptance_commands=(command,)))
        old = _execute(host, command, settle=False)
        capture = host.workspace_snapshots.verification_snapshot
        monkeypatch.setattr(host.workspace_snapshots, 'verification_snapshot',
                            lambda **_: (_ for _ in ()).throw(SnapshotError('incomplete')))
        _execute(host, 'cat pkg/module.py')
        monkeypatch.setattr(host.workspace_snapshots, 'verification_snapshot', capture)
        _execute(host, 'cat pkg/module.py')
        assert host.runtime_state.mutation_generation == 0
        host.verification_runtime.record_result(old)
        snapshot = host.scenario.domain.snapshot()
        spec = next(spec for spec in snapshot.specs if spec.command.original == command)
        check = snapshot.decision(spec.id)
        assert check.status == 'pending'
        assert check.reason == 'execution started before workspace scope loss'
        assert host.verification_runtime.status()['verification_needed']
        _execute(host, command)
        assert not host.verification_runtime.status()['verification_needed']


def test_external_drift_before_a_check_is_added_to_verification_scope(tmp_path):
    _source(tmp_path)
    with scoped_workdir(tmp_path):
        host = _dynamic_host(tmp_path)
        _execute(host, "printf '\nvalue = 2\n' >> pkg/module.py")
        _execute(host, 'python -m py_compile pkg/module.py')
        (tmp_path / 'pkg/second.py').write_text('def broken(:\n')
        _execute(host, 'python -m py_compile pkg/module.py')
        assert host.verification_runtime.status()['verification_needed'] is True
        assert 'pkg/second.py' in host.runtime_state.changed_files


def test_dispatched_tool_error_still_records_observed_partial_write(tmp_path):
    with scoped_workdir(tmp_path):
        host = _scope_host(tmp_path)

        def fail_after_write(*_):
            (tmp_path / 'leftover.py').write_text('def broken(:')
            return ToolExecutionResult('bash', {'command': 'opaque-script'}, 'Error: failed',
                                       True, True, False, False)

        host.executor.execute_one.side_effect = fail_after_write
        _execute(host, 'opaque-script')
        assert host.runtime_state.last_mutation_paths == ('leftover.py',)
        assert host.verification_runtime.status()['verification_needed'] is True


def test_incomplete_command_output_cannot_publish_positive_scope(tmp_path):
    _source(tmp_path)
    with scoped_workdir(tmp_path):
        host = _dynamic_host(tmp_path)
        _execute(host, "printf '\nvalue = 2\n' >> pkg/module.py")
        host.executor.execute_one.side_effect = lambda *_: ToolExecutionResult(
            'bash', {'command': 'python -m py_compile pkg/module.py'}, 'OK',
            True, False, False, False, metadata={'exit': 0, 'output_incomplete': True})
        _execute(host, 'python -m py_compile pkg/module.py')
        assert host.verification_runtime.status()['verification_needed'] is True


def test_denied_targeted_test_cannot_be_replaced_with_static_compile(tmp_path):
    _source(tmp_path)
    with scoped_workdir(tmp_path):
        host = _dynamic_host(tmp_path)
        _execute(host, "printf '\nvalue = 2\n' >> pkg/module.py")
        command = 'python -m unittest discover -s tests -p test_app.py -v'
        # 用户声明的测试未获授权，静态通过不能代替它；不靠缓存副作用维持 pending。
        host.runtime_state.set_task_contract(TaskContract(acceptance_commands=(COMPILE, command)))
        host.verification_runtime.synchronize_task()
        host.executor.execute_one.side_effect = lambda *_: ToolExecutionResult(
            'bash', {'command': command}, 'Denied: user rejected the test',
            False, True, False, False, permission_denied=True)
        _execute(host, command)
        host = _restore_executor(host, tmp_path)
        _execute(host, 'python -m py_compile pkg/module.py')
        assert host.verification_runtime.status()['verification_needed'] is True
        assert not host.scenario.domain.snapshot().obligations
        assert any(item.status == 'pending' for item in host.scenario.domain.snapshot().checks)


def _restore_executor(host, root):
    normal = _scope_host(root)
    host.executor = normal.executor
    return host


@pytest.mark.parametrize('damage', ['foreign-workspace', 'corrupt-reference'])
def test_foreign_or_corrupt_tool_proof_cannot_promote_positive(tmp_path, damage):
    _source(tmp_path)
    with scoped_workdir(tmp_path):
        host = _dynamic_host(tmp_path)
        _execute(host, "printf '\nvalue = 2\n' >> pkg/module.py")
        result = _execute(host, 'python -m py_compile pkg/module.py', settle=False)
        if damage == 'foreign-workspace':
            result.workspace_scope['workspace'] = str(tmp_path.parent)
        else:
            reference = result.workspace_scope['before']
            (host.workspace_snapshots.root / 'scope' / (reference + '.json')).write_text('{}')
        host.verification_runtime.record_result(result)
        AgentLoop._record_tool_result(host, result)
        assert host.verification_runtime.status()['verification_needed'] is True
        assert host.verification_runtime.status()['mutation_scope_unknown'] is True


def test_compile_and_arbitrary_cache_write_is_not_a_verification_artifact(tmp_path):
    _source(tmp_path)
    with scoped_workdir(tmp_path):
        host = _dynamic_host(tmp_path)
        _execute(host, "printf '\nvalue = 2\n' >> pkg/module.py")
        generation = host.runtime_state.mutation_generation
        cache = f'pkg/__pycache__/module.{sys.implementation.cache_tag}.pyc'
        result = _execute(host, f'python -m py_compile pkg/module.py && printf arbitrary > {cache}')
        assert result.command_failed is False
        assert (tmp_path / cache).read_text() == 'arbitrary'
        assert host.runtime_state.mutation_generation > generation
        assert cache in host.runtime_state.last_mutation_paths
        assert host.verification_runtime.status()['verification_needed'] is True


def test_pytest_source_write_inside_cache_is_a_real_mutation(tmp_path):
    _source(tmp_path)
    (tmp_path / '.pytest_cache').mkdir()
    (tmp_path / '.pytest_cache/source.py').write_text('value = 1\n')
    (tmp_path / 'pkg/test_cache.py').write_text(
        'from pathlib import Path\n'
        'def test_cache():\n'
        ' Path(".pytest_cache/source.py").write_text("def broken(:\\n")\n'
        ' assert True\n')
    with scoped_workdir(tmp_path):
        host = _dynamic_host(tmp_path)
        _execute(host, "printf '\nvalue = 2\n' >> pkg/module.py")
        _execute(host, 'python -m py_compile pkg/module.py')
        generation = host.runtime_state.mutation_generation
        result = _execute(host, 'python -m pytest pkg/test_cache.py -q')
        assert result.command_failed is False and '1 passed' in result.output
        assert (tmp_path / '.pytest_cache/source.py').read_text() == 'def broken(:\n'
        assert host.runtime_state.mutation_generation > generation
        assert '.pytest_cache/source.py' in host.runtime_state.last_mutation_paths
        assert host.verification_runtime.status()['verification_needed'] is True


def test_unsupported_strict_scan_does_not_prevent_tool_execution(tmp_path, monkeypatch):
    _source(tmp_path)
    with scoped_workdir(tmp_path):
        host = _dynamic_host(tmp_path)
        monkeypatch.delattr(__import__('os'), 'O_DIRECTORY')
        result = _execute(host, "printf '\nvalue = 2\n' >> pkg/module.py")
        assert result.executed and not result.command_failed
        assert 'value = 2' in (tmp_path / 'pkg/module.py').read_text()
        assert host.verification_runtime.status()['mutation_scope_unknown'] is True
        assert host.verification_runtime.status()['verification_needed'] is True
