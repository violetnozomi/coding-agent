"""真实执行事实和批事务必须独立于后续工具、展示及存盘故障结清。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from test_node_verification_runtime import _run, _tool
from test_workspace_scope_execution import _execute, _scope_host
from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
from nz_coder.runtime.adapters.verification_runtime import VerificationRuntime, current_tool_verification, scoped_tool_verification
from nz_coder.runtime.agent.guardrail_runtime import ProductionGuardrailRuntime
from nz_coder.runtime.agent.guardrails import GuardrailEscalateError
from nz_coder.runtime.execution.loop import AgentLoop, ProductRunEnvironment
from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
from nz_coder.runtime.execution.tool_executor import ToolExecutor
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.process.workspace_scope_execution import execute_observed
from nz_coder.runtime.process.workspace_snapshot import SnapshotError
from nz_coder.runtime.tool_runtime.pipeline import ApprovedToolBatch, ProductionToolRuntime
from nz_coder.runtime.tool_runtime.result_projection import ProductionToolResultProjector
from nz_coder.runtime.verification.recovery import RecoveryState
from nz_coder.state.transaction import TransactionManager
from nz_coder.tool_platform.execution import ToolExecutionResult
from nz_coder.tools.files import bind_tool_state, edit_file


COMMAND = 'python -m unittest tests.test_value.ValueTest.test_value -v'


def _project(root: Path) -> None:
    (root / 'app.py').write_text('def value(): return 0\n')
    (root / 'tests').mkdir()
    (root / 'tests/__init__.py').write_text('')
    (root / 'tests/test_value.py').write_text(
        'import unittest\nfrom app import value\n'
        'class ValueTest(unittest.TestCase):\n'
        '    def test_value(self): self.assertEqual(value(), 2)\n'
    )


@pytest.mark.parametrize('fault', ['write_cancel', 'read_cancel', 'read_error', 'guardrail', 'projection'])
def test_native_completed_failed_test_survives_later_batch_fault(tmp_path, monkeypatch, fault):
    """已退出的失败单测不能因后项异常消失，普通工具归属也必须结清。"""
    _project(tmp_path)
    for key in ('NZ_PLANNING_ENABLED', 'NZ_REFLECTION_ENABLED', 'NZ_MEMORY_AUTO_EXTRACT', 'NZ_MEMORY_AUTO_DREAM'):
        monkeypatch.setenv(key, '0')
    starts, actual_checks = [], []
    original_begin = VerificationRuntime.begin_tool
    original_execute = ToolExecutor.execute_one
    original_write = WorkspaceFileAccess.write_text
    original_after = ProductionGuardrailRuntime.after_tool
    original_consume = ProductionToolResultProjector.consume

    def begin(runtime, call):
        start = original_begin(runtime, call)
        starts.append(start)
        return start

    def execute(executor, call, index):
        result = original_execute(executor, call, index)
        if result.name == 'bash':
            actual_checks.append((current_tool_verification().execution_id, result.output, result.command_failed))
        if fault in {'read_cancel', 'read_error'} and result.name == 'read_file' and result.tool_input.get('path') == 'tests/test_value.py':
            if fault == 'read_cancel':
                raise asyncio.CancelledError('read interrupted after the failed check')
            raise RuntimeError('read failed after the failed check')
        return result

    def cancel_after_write(access, path, content, **kwargs):
        original_write(access, path, content, **kwargs)
        raise asyncio.CancelledError('write interrupted after application')

    async def after(guardrails, host, call, result, messages):
        if result.name == 'edit_file':
            raise GuardrailEscalateError('batch-test', 'tool', 'controlled review')
        return await original_after(guardrails, host, call, result, messages)

    def consume(projector, context, dispatched, messages, **kwargs):
        if any(result.name == 'edit_file' for _, _, result in dispatched):
            def unavailable_observer(_name, _output):
                raise RuntimeError('tool observer unavailable')
            kwargs['on_tool'] = unavailable_observer
        return original_consume(projector, context, dispatched, messages, **kwargs)

    monkeypatch.setattr(VerificationRuntime, 'begin_tool', begin)
    monkeypatch.setattr(ToolExecutor, 'execute_one', execute)
    edit = _tool('edit_file', path='app.py', old_text='return 0', new_text='return 2')
    if fault == 'write_cancel':
        monkeypatch.setattr(WorkspaceFileAccess, 'write_text', cancel_after_write)
    elif fault == 'guardrail':
        monkeypatch.setattr(ProductionGuardrailRuntime, 'after_tool', after)
    elif fault == 'projection':
        monkeypatch.setattr(ProductionToolResultProjector, 'consume', consume)
        edit = _tool('edit_file', path='app.py', old_text='return 0', new_text='return 11')
    later = _tool('read_file', path='tests/test_value.py') if fault.startswith('read_') else edit
    batch = [edit, _tool('bash', command=COMMAND)] if fault == 'projection' else [_tool('bash', command=COMMAND), later]
    caught = None
    try:
        _run(monkeypatch, tmp_path, [[_tool('read_file', path='app.py')], batch],
             task=f'Change app.py value to 2. Run {COMMAND}.', allowed_commands=(COMMAND,), limit=2)
    except Exception as error:
        caught = error
    if fault in {'read_error', 'guardrail', 'projection'}:
        assert caught is not None
    assert len(actual_checks) == 1
    failed_id, output, command_failed = actual_checks[0]
    assert command_failed and 'Ran 1 test' in output and 'FAILED (failures=1)' in output
    runtime = starts[0].runtime
    snapshot = runtime.domain.snapshot()
    failed = [fact for fact in snapshot.executions if fact.execution_id == failed_id]
    assert len(failed) == 1, '已完成的真实失败回执必须保留在验证域'
    assert failed[0].exit_code == 1 and failed[0].outcome == 'failed'
    assert any(failed_id in item.failure_execution_ids and not item.resolved for item in snapshot.obligations)
    assert all(runtime.domain.execution_settled(start.execution_id) for start in starts), '普通工具的取消/异常归属必须结清'
    assert (tmp_path / 'app.py').read_text() == 'def value(): return 0\n'


def _causes(error):
    seen, pending = set(), [error]
    while pending:
        item = pending.pop()
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        yield item
        pending.extend([item.__cause__, item.__context__])
        pending.extend(getattr(item, 'exceptions', ()))


@pytest.mark.parametrize('asynchronous', [False, True])
def test_checkpoint_failure_cannot_skip_real_write_rollback(tmp_path, asynchronous):
    """中断存盘失败后仍恢复真实文件，执行取消保持为主异常。"""
    source = tmp_path / 'app.py'
    source.write_text('value = 0\n')
    calls = [{'id': 'edit', 'function': {'name': 'edit_file', 'arguments': {
        'path': 'app.py', 'old_text': 'value = 0', 'new_text': 'value = 2'}}}]
    checkpoint_contents = []
    with scoped_workdir(tmp_path):
        host = _scope_host(tmp_path)
        host.txn = TransactionManager()
        host._tool_batch_has_write = lambda _: True
        host._finish_tool_transaction = MethodType(AgentLoop._finish_tool_transaction, host)

        def dispatch(_calls, _has_write, _messages):
            assert not edit_file(**calls[0]['function']['arguments']).startswith('Error:')
            raise asyncio.CancelledError('original execution cancelled')

        async def dispatch_async(*args):
            return dispatch(*args)

        def checkpoint(_messages, status):
            if status == 'interrupted':
                checkpoint_contents.append(source.read_text())
                raise OSError('interrupted checkpoint unavailable')

        host._dispatch_tool_calls = dispatch
        host._dispatch_tool_calls_async = dispatch_async
        host._checkpoint_messages = checkpoint
        processor = SimpleNamespace(start_tools=lambda _: None, interrupt_unsettled=lambda: None)
        runtime = ProductionToolRuntime()
        approved = ApprovedToolBatch(calls)
        with bind_tool_state(txn=host.txn):
            try:
                if asynchronous:
                    asyncio.run(runtime.execute_batch_async(host, calls, [], processor=processor, approved_batch=approved))
                else:
                    runtime._execute_batch_sync_snapshot(host, calls, [], processor=processor, approved_batch=approved)
            except BaseException as error:
                caught = error
            else:
                pytest.fail('执行取消不能变为成功返回')
        assert source.read_text() == 'value = 0\n', 'checkpoint 故障不能跳过事务恢复'
        assert host.txn.active is False
        assert checkpoint_contents == ['value = 0\n'], '中断存盘应看到回滚后的文件'
        assert isinstance(caught, asyncio.CancelledError)
        assert any(isinstance(item, OSError) and 'checkpoint unavailable' in str(item) for item in _causes(caught))


@pytest.mark.parametrize('snapshot_fails', [False, True])
def test_rollback_immediately_invalidates_pass_for_restored_bad_source(tmp_path, monkeypatch, snapshot_fails):
    """无需等待最终freeze，回滚返回时旧通过已失效且负事实仍在。"""
    (tmp_path / 'pkg').mkdir()
    source = tmp_path / 'pkg/module.py'
    source.write_text('def broken(:\n')
    with scoped_workdir(tmp_path):
        host = _scope_host(tmp_path)
        first_failure = _execute(host, 'python -m py_compile pkg/module.py')
        assert first_failure.command_failed
        original_failed_ids = {fact.execution_id for fact in host.verification_runtime.domain.snapshot().executions if fact.exit_code == 1}
        assert original_failed_ids
        host.txn = TransactionManager()
        host.txn.begin()
        call = {'id': 'repair', 'function': {'name': 'edit_file', 'arguments': {
            'path': 'pkg/module.py', 'old_text': 'def broken(:', 'new_text': 'value = 2'}}}
        start = host.verification_runtime.begin_tool(call)
        with bind_tool_state(txn=host.txn), scoped_tool_verification(start):
            result = execute_observed(host.workspace_snapshots, start.version.generation,
                lambda: ToolExecutionResult('edit_file', call['function']['arguments'], edit_file(**call['function']['arguments']),
                                             True, False, False, True),
                on_before=lambda reference: host.verification_runtime.record_before(start, reference))
        result.execution_id = start.execution_id
        host.verification_runtime.record_result(result)
        assert not _execute(host, 'python -m py_compile pkg/module.py').command_failed
        assert host.verification_runtime.status()['verification_needed'] is False
        if snapshot_fails:
            def unavailable_snapshot(**_kwargs):
                raise SnapshotError('rollback observation unavailable')
            monkeypatch.setattr(host.workspace_snapshots, 'verification_snapshot', unavailable_snapshot)
        AgentLoop._finish_tool_transaction(host, True, False, [])
        assert source.read_text() == 'def broken(:\n'
        status = host.verification_runtime.status()
        assert status['verification_needed'] is True, '回滚后不得继续使用好源码的通过'
        assert status['mutation_scope_unknown'] is snapshot_fails
        snapshot = host.verification_runtime.domain.snapshot()
        assert original_failed_ids <= {fact.execution_id for fact in snapshot.executions}
        assert any(not item.resolved for item in snapshot.obligations)


def _settlement_host(root):
    host = _scope_host(root)
    host.txn = TransactionManager()
    host.recovery = RecoveryState()
    host._record_tool_result = MethodType(AgentLoop._record_tool_result, host)
    host._finish_tool_transaction = MethodType(AgentLoop._finish_tool_transaction, host)
    host._tool_batch_has_write = lambda _: True
    host._apply_pending_plan_mode = lambda: None
    return host


def _observed_edit(host, *, path='pkg/module.py', old_text='def broken(:', new_text='value = 2', call_id='repair'):
    args = {'path': path, 'old_text': old_text, 'new_text': new_text}
    call = {'id': call_id, 'function': {'name': 'edit_file', 'arguments': args}}
    start = host.verification_runtime.begin_tool(call)

    def execute():
        output = edit_file(**args)
        return ToolExecutionResult('edit_file', args, output, True, output.startswith('Error:'), False, True)

    with scoped_tool_verification(start):
        result = execute_observed(host.workspace_snapshots, start.version.generation, execute,
            on_before=lambda reference: host.verification_runtime.record_before(start, reference))
    result.execution_id = start.execution_id
    host.verification_runtime.record_result(result)
    return call, result


@pytest.mark.parametrize('asynchronous', [False, True])
def test_normal_failed_batch_reconciles_before_strict_callback(tmp_path, asynchronous):
    """普通consume发现后项失败时，严格完成回调必须看到回滚后的未验证状态。"""
    (tmp_path / 'pkg').mkdir()
    source = tmp_path / 'pkg/module.py'
    source.write_text('def broken(:\n')
    observed = []
    with scoped_workdir(tmp_path):
        host = _settlement_host(tmp_path)
        calls = [{'id': 'batch', 'function': {'name': 'edit_file', 'arguments': {'path': 'pkg/module.py'}}}]

        def dispatch(_calls, _has_write, _messages):
            edit_call, edit_result = _observed_edit(host)
            check = _execute(host, 'python -m py_compile pkg/module.py')
            assert not check.command_failed and not host.verification_runtime.status()['verification_needed']
            bad_call, bad_result = _observed_edit(host, old_text='missing anchor', new_text='wrong', call_id='bad-edit')
            assert bad_result.dispatch_failed
            check_call = {'id': 'check', 'function': {'name': 'bash', 'arguments': check.tool_input}}
            return [(0, edit_call, edit_result), (1, check_call, check), (2, bad_call, bad_result)]

        async def dispatch_async(*args):
            return dispatch(*args)

        def strict_completed(_dispatched):
            needed = host.verification_runtime.status()['verification_needed']
            observed.append((needed, source.read_text(), host.txn.active))
            return not needed

        host._dispatch_tool_calls = dispatch
        host._dispatch_tool_calls_async = dispatch_async
        host._strict_verification_completed = strict_completed
        runtime = ProductionToolRuntime()
        with bind_tool_state(txn=host.txn):
            if asynchronous:
                action = asyncio.run(runtime.execute_batch_async(host, calls, [], approved_batch=ApprovedToolBatch(calls)))
            else:
                action = runtime._execute_batch_sync_snapshot(host, calls, [], approved_batch=ApprovedToolBatch(calls))
        assert observed == [(True, 'def broken(:\n', False)]
        assert action == 'continue', '回滚前的通过不能缓存为terminal返回'


def test_active_partial_rollback_blocks_product_strict_terminal(tmp_path, monkeypatch):
    """真实回滚部分失败，即使现存源码仍通过，产品严格检查也不能发terminal。"""
    (tmp_path / 'pkg').mkdir()
    source = tmp_path / 'pkg/module.py'
    source.write_text('def broken(:\n')
    with scoped_workdir(tmp_path):
        host = _settlement_host(tmp_path)
        host.txn.begin()
        with bind_tool_state(txn=host.txn):
            _observed_edit(host)
            assert not _execute(host, 'python -m py_compile pkg/module.py').command_failed
        assert not host.verification_runtime.status()['verification_needed']

        def unavailable_restore(*_args):
            raise OSError('controlled recovery storage failure')

        try:
            with monkeypatch.context() as fault:
                fault.setattr(host.txn, '_restore_backup', unavailable_restore)
                AgentLoop._finish_tool_transaction(host, True, False, [])
                assert host.txn.active and host.txn.state == 'rollback_partial'
                assert source.read_text() == 'value = 2\n'
                assert not host.verification_runtime.status()['verification_needed']
                host.runtime_state.observe_tool('diff_status', {},
                    'has_non_empty_diff: true\ndiff_chars: 20\ntests_modified: false\nsource_only: true\nChanged files:\n  pkg/module.py')
                assert host.runtime_state.strict_generation_terminal_ready()
                with scoped_runtime_overrides(strict_local_tools=True):
                    assert ProductRunEnvironment._strict_verification_completed(host, []) is False
        finally:
            host.txn.rollback()


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('factory_name', ['_tool_metadata_callback', '_question_lifecycle_callback'])
def test_reporter_factory_failure_after_begin_closes_transaction(tmp_path, asynchronous, factory_name):
    """begin之后的两类factory异常都必须走真实事务清理。"""
    source = tmp_path / 'app.py'
    source.write_text('value = 0\n')
    calls = [{'id': 'edit', 'function': {'name': 'edit_file', 'arguments': {'path': 'app.py'}}}]
    with scoped_workdir(tmp_path):
        host = _settlement_host(tmp_path)

        def unavailable_factory(_processor, _messages):
            assert host.txn.active, '故障必须发生在真实事务已经begin之后'
            raise RuntimeError('reporter factory unavailable')

        setattr(host, factory_name, unavailable_factory)
        runtime = ProductionToolRuntime()
        with pytest.raises(RuntimeError, match='reporter factory unavailable'):
            if asynchronous:
                asyncio.run(runtime.execute_batch_async(host, calls, [], approved_batch=ApprovedToolBatch(calls)))
            else:
                runtime._execute_batch_sync_snapshot(host, calls, [], approved_batch=ApprovedToolBatch(calls))
        assert not host.txn.active
        assert host.txn.state == 'rolled_back'
        assert source.read_text() == 'value = 0\n'


def test_rollback_error_still_reconciles_actual_partial_restore(tmp_path, monkeypatch):
    """真实恢复第一份坏源码后rollback抛错，也必须立即使旧通过失效。"""
    (tmp_path / 'pkg').mkdir()
    first = tmp_path / 'pkg/module.py'
    second = tmp_path / 'pkg/second.py'
    first.write_text('def broken(:\n')
    second.write_text('def broken(:\n')
    with scoped_workdir(tmp_path):
        host = _settlement_host(tmp_path)
        host.txn.begin()
        with bind_tool_state(txn=host.txn):
            _observed_edit(host)
            _observed_edit(host, path='pkg/second.py', call_id='repair-second')
            assert not _execute(host, 'python -m py_compile pkg/module.py').command_failed
            assert not _execute(host, 'python -m py_compile pkg/second.py').command_failed
        assert not host.verification_runtime.status()['verification_needed']
        original_restore = host.txn._restore_backup
        original_rollback = host.txn.rollback

        def restore_first_only(record, backup, parent):
            if record.relative == 'pkg/second.py':
                raise OSError('second restore unavailable')
            return original_restore(record, backup, parent)

        def rollback_then_error():
            original_rollback()
            raise OSError('rollback report unavailable after partial recovery')

        try:
            with monkeypatch.context() as fault:
                fault.setattr(host.txn, '_restore_backup', restore_first_only)
                fault.setattr(host.txn, 'rollback', rollback_then_error)
                with pytest.raises(OSError, match='rollback report unavailable'):
                    AgentLoop._finish_tool_transaction(host, True, False, [])
                assert first.read_text() == 'def broken(:\n'
                assert second.read_text() == 'value = 2\n'
                assert host.txn.active
                status = host.verification_runtime.status()
                assert status['verification_needed'] is True
                assert not status['mutation_scope_unknown'], '真实完整扫描应确认部分恢复后的范围'
        finally:
            host.txn.rollback()
