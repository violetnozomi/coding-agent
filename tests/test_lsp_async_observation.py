"""写后可选诊断不阻塞事件循环，也不在取消返回后改写历史。"""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest


def test_post_write_diagnostics_cancel_settles_worker_without_attaching(monkeypatch):
    async def scenario():
        from nz_coder.runtime.tool_runtime import observers

        monkeypatch.setattr(
            observers, "observe_execution_handoff", lambda *a, **k: None
        )
        entered = threading.Event()
        exited = threading.Event()
        events = []

        class Host:
            runtime_state = SimpleNamespace(_verification_context=None)
            recovery = SimpleNamespace(
                reset_tool_call_history=lambda **k: events.append("committed")
            )

            def _refresh_patch_risk(self, messages):
                pass

            def _refresh_code_index(self, dispatched):
                pass

            def _collect_lsp_write_diagnostics(self, dispatched, *, operation):
                entered.set()
                while not operation.cancelled():
                    time.sleep(0.005)
                exited.set()
                return ({"enabled": True, "reports": [], "skipped": 0}, "call-write")

            def _apply_lsp_write_diagnostics(self, result, messages):
                events.append("attached")

        observer = observers.LegacyCodingToolObserver(Host())
        task = asyncio.create_task(observer.post_write_async([], []))
        for _ in range(50):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert exited.is_set()
        assert events == ["committed"]

    asyncio.run(scenario())
