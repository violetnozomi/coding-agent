"""并行只读段取消必须真实排空后，才允许批事务和持久化收尾。"""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from tests.runtime.tool_runtime.test_focused_policy import _context
from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
from nz_coder.runtime.core.tool_context import ToolExecutionContext, ToolLifecycleContext, ToolProjectionContext
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.tool_runtime import scheduler
from nz_coder.runtime.tool_runtime.pipeline import ApprovedToolBatch, ProductionToolRuntime
from nz_coder.state.transaction import TransactionManager
from nz_coder.tool_platform.execution import ToolExecutionResult
from nz_coder.tools import current_tool_cancel_event
from nz_coder.tools.files import read_file


async def _loop_barrier():
    """让已排入事件循环的取消/完成回调前进，不靠耗时猜测线程状态。"""
    ready = asyncio.Event()
    asyncio.get_running_loop().call_soon(ready.set)
    await ready.wait()


@pytest.mark.parametrize('repeat_cancel', [0, 2])
def test_parallel_cancel_waits_for_slow_worker_before_rollback_and_checkpoint(tmp_path, monkeypatch, repeat_cancel):
    """一个worker快速取消、另一个未退出时，管线不能提前返回或存盘。"""
    (tmp_path / 'fast.py').write_text('fast = 1\n')
    (tmp_path / 'slow.py').write_text('slow = 1\n')
    calls = [
        {'id': 'fast', 'function': {'name': 'read_file', 'arguments': {'path': 'fast.py'}}},
        {'id': 'slow', 'function': {'name': 'read_file', 'arguments': {'path': 'slow.py'}}},
        {'id': 'later-write', 'function': {'name': 'write_file', 'arguments': {'path': 'later.py', 'content': 'must not run'}}},
    ]

    async def scenario():
        loop = asyncio.get_running_loop()
        started = [asyncio.Event(), asyncio.Event()]
        ended = [asyncio.Event(), asyncio.Event()]
        drained = [asyncio.Event(), asyncio.Event()]
        slow_saw_cancel = asyncio.Event()
        release_slow = threading.Event()
        cleanup = []
        txn = TransactionManager()
        original_settled = scheduler._to_thread_settled

        async def observe_worker_exit(function, *args, **kwargs):
            call = next(item for item in args if isinstance(item, dict) and item.get('id') in {'fast', 'slow'})
            index = 0 if call['id'] == 'fast' else 1
            try:
                return await original_settled(function, *args, **kwargs)
            finally:
                # 原助手已等待真实线程future结束；观察其返回，消除线程发信号后被抢占的窗口。
                drained[index].set()

        monkeypatch.setattr(scheduler, '_to_thread_settled', observe_worker_exit)

        def execute(call, index, _messages):
            if index == 2:
                (tmp_path / 'later.py').write_text('must not run')
                return ToolExecutionResult('write_file', call['function']['arguments'], 'written', True, False, False, True)
            path = call['function']['arguments']['path']
            content = read_file(path=path)
            loop.call_soon_threadsafe(started[index].set)
            cancel = current_tool_cancel_event()
            assert cancel is not None and cancel.wait(5), 'worker没有收到合作取消'
            if index == 1:
                loop.call_soon_threadsafe(slow_saw_cancel.set)
                assert release_slow.wait(5), '测试未释放慢worker'
            loop.call_soon_threadsafe(ended[index].set)
            return ToolExecutionResult('read_file', {'path': path}, content, True, False, False, False)

        async def checkpoint(_messages, status):
            cleanup.append((status, all(item.is_set() for item in drained), txn.active))

        def finish_transaction(has_write, all_succeeded, _messages):
            assert has_write and not all_succeeded
            cleanup.append(('rollback', all(item.is_set() for item in drained), txn.active))
            txn.rollback()

        async def before(call, _messages):
            return call, None

        async def after(_call, result, _messages):
            return result

        async def noop_async(*_args):
            return None

        observer = SimpleNamespace(post_write=lambda *_: None, after_batch=lambda *_: None,
            apply_plan_mode=lambda: None, capture_snapshot=noop_async, record_patch=lambda *_: None)
        context = ToolExecutionContext(run=None, policy=_context(), lifecycle=ToolLifecycleContext(
            checkpoint=checkpoint, processor_for_messages=lambda _: None, write_override=None,
            begin_transaction=txn.begin, transaction_active=lambda: txn.active,
            finish_transaction=finish_transaction, metadata_reporter=lambda *_: lambda *_a: None,
            question_reporter=lambda *_: lambda *_a: None, dispatch_override_async=None,
            consume_override=None, model_capabilities=None, describe_read_results=noop_async,
            strict_completed=lambda _: False, apply_transition=noop_async, observer=observer,
            has_pre_tool_hooks=lambda: False, executor=None, execute_one=execute,
            before_tool=before, after_tool=after, trace=lambda *_a, **_k: None),
            projection=ToolProjectionContext(signal_from_metadata=lambda _: None,
                record_result=lambda _: False, trace_result=lambda *_a, **_k: None,
                after_result=lambda *_: None))
        processor = SimpleNamespace(start_tools=lambda _: None, interrupt_unsettled=lambda: None)
        with scoped_runtime_overrides(max_parallel_tasks=2):
            task = asyncio.create_task(ProductionToolRuntime().execute_batch_async(
                context, calls, [], processor=processor, approved_batch=ApprovedToolBatch(calls)))
            try:
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), 5)
                task.cancel()
                await asyncio.wait_for(drained[0].wait(), 5)
                await asyncio.wait_for(slow_saw_cancel.wait(), 5)
                for _ in range(repeat_cancel):
                    task.cancel()
                    await _loop_barrier()
                # 两个真实线程都已到达指定屏障；排空回调队列后慢线程仍被门锁住。
                for _ in range(3):
                    await _loop_barrier()
                returned_while_blocked = task.done()
                early_cleanup = [item for item in cleanup if item[0] != 'running']
            finally:
                release_slow.set()
                if not all(event.is_set() for event in started):
                    task.cancel()
                outcomes = await asyncio.gather(task, return_exceptions=True)
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in ended)), 5)
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in drained)), 5)
        assert isinstance(outcomes[0], asyncio.CancelledError)
        assert not returned_while_blocked, '慢worker真实退出之前scheduler不能返回取消'
        assert not early_cleanup, '排空之前不能开始回滚或中断存盘'
        settled_cleanup = [item for item in cleanup if item[0] != 'running']
        assert [item[0] for item in settled_cleanup] == ['rollback', 'interrupted']
        assert all(item[1] for item in settled_cleanup)
        assert settled_cleanup[-1][2] is False
        assert not txn.active
        assert not (tmp_path / 'later.py').exists(), '取消后不能启动后续写屏障'

    with scoped_workdir(tmp_path):
        asyncio.run(scenario())
