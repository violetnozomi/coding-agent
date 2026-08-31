"""InfCode-style cooperative cancellation across scheduler and tool workers."""
from __future__ import annotations

import asyncio
import os
import shlex
import sys
import threading
import time

import pytest

from nz_coder.capabilities.documents import _DocumentInterrupted, _pdf_page_count
from nz_coder.protocol.message_schema import PARTS_KEY, attach_message_identity
from nz_coder.foundation.async_utils import to_thread_settled
from nz_coder.runtime.execution.loop import AgentLoop, _execute_concurrent_async
from nz_coder.runtime.session.session_processor import SessionProcessor
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.tools import dispatch, register, scoped_tool_cancellation
from nz_coder.tools.bash import run_bash
from nz_coder.tools.files import read_file


def test_thread_bridge_signals_callback_before_waiting_for_worker():
    started = threading.Event()
    stop = threading.Event()
    settled = threading.Event()

    def worker():
        started.set()
        stop.wait(2)
        settled.set()

    async def scenario():
        task = asyncio.create_task(
            to_thread_settled(worker, cancel_callback=stop.set)
        )
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert stop.is_set()
    assert settled.is_set()


def test_dispatch_does_not_start_handler_after_cancel_signal():
    cancel_event = threading.Event()
    cancel_event.set()
    calls = []
    name = "test_cancelled_before_dispatch"
    register(
        name,
        "test cancellation gate",
        {"type": "object", "properties": {}},
        lambda: calls.append(True) or "unexpected",
    )

    with scoped_tool_cancellation(cancel_event):
        result = dispatch(name, {})

    assert result == "Error: Tool execution cancelled"
    assert calls == []


def test_cancelled_pdf_read_settles_worker_without_cache_or_completed_toolpart(
    tmp_path,
    monkeypatch,
):
    pdf = tmp_path / "slow.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF")
    started = threading.Event()
    observed_cancel = threading.Event()

    monkeypatch.setattr("nz_coder.capabilities.documents._pdf_page_count", lambda *_args: 1)

    def slow_extract(_path, _cache, cancel_event, _start, _end):
        started.set()
        if cancel_event.wait(2):
            observed_cancel.set()
        raise RuntimeError("conversion stopped")

    monkeypatch.setattr("nz_coder.capabilities.documents._extract_pdf", slow_extract)

    tool_call = {
        "id": "call-cancel-pdf",
        "type": "function",
        "function": {
            "name": "read_file",
            "arguments": '{"path":"slow.pdf"}',
        },
    }
    assistant = {"role": "assistant", "content": "", "tool_calls": [tool_call]}
    attach_message_identity(assistant, "msg-cancel-pdf", session_id="session-cancel")
    processor = SessionProcessor(assistant)
    processor.register_tool_calls([tool_call])

    class Executor:
        def execute_one(self, _tool_call, _index):
            return read_file("slow.pdf")

    class Harness:
        executor = Executor()

        def _tool_batch_has_write(self, _calls):
            return False

        async def _dispatch_tool_calls_async(self, calls, _has_write, _messages):
            return await _execute_concurrent_async(self.executor, calls)

        def _checkpoint_messages(self, _messages, _status):
            return None

    async def scenario():
        task = asyncio.create_task(AgentLoop._execute_tools_async(
            Harness(),
            [tool_call],
            [assistant],
            processor=processor,
        ))
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with scoped_workdir(tmp_path):
        asyncio.run(scenario())

    assert observed_cancel.is_set()
    cache = (
        tmp_path / ".nz-coder" / "sessions" / "default"
        / "documents" / ".cache"
    )
    assert not cache.exists() or not list(cache.glob("*.md"))
    tool_part = next(part for part in assistant[PARTS_KEY] if part["type"] == "tool")
    assert tool_part["state"]["status"] == "error"
    assert tool_part["state"]["interrupted"] is True
    assert "output" not in tool_part["state"]


def test_pdfinfo_process_is_terminated_by_cooperative_cancel(
    tmp_path,
    monkeypatch,
):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF")
    started = threading.Event()
    process_holder = []

    class Process:
        def __init__(self, *_args, **_kwargs):
            self.returncode = None
            self.terminated = False
            self.killed = False
            process_holder.append(self)
            started.set()

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            self.killed = True
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr("nz_coder.capabilities.documents.shutil.which", lambda _name: "/fake/pdfinfo")
    monkeypatch.setattr("nz_coder.capabilities.documents.subprocess.Popen", Process)
    cancel_event = threading.Event()
    interrupted = []

    def worker():
        try:
            _pdf_page_count(pdf, cancel_event)
        except _DocumentInterrupted:
            interrupted.append(True)

    thread = threading.Thread(target=worker)
    thread.start()
    assert started.wait(1)
    cancel_event.set()
    thread.join(1)

    assert thread.is_alive() is False
    assert interrupted == [True]
    assert process_holder[0].terminated is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cancellation smoke")
def test_bash_consumes_current_tool_cancel_event(tmp_path):
    cancel_event = threading.Event()
    result = []
    command = (
        f"{shlex.quote(sys.executable)} -c "
        '"import time; time.sleep(5)"'
    )

    def worker():
        with scoped_workdir(tmp_path), scoped_tool_cancellation(cancel_event):
            result.append(run_bash(command, read_only=False, timeout=10))

    started = time.monotonic()
    thread = threading.Thread(target=worker)
    thread.start()
    time.sleep(0.15)
    cancel_event.set()
    thread.join(2)

    assert thread.is_alive() is False
    assert result == ["Error: Command cancelled"]
    assert time.monotonic() - started < 3
