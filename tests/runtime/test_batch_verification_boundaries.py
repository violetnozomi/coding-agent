"""逐工具事实交接的并发历史标签及后台进程归属边界。"""
from __future__ import annotations

import json
import shlex
import socket
import sys
import threading
import time

import pytest

from nz_coder.runtime.process.process_service import ProcessService
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.process.workspace_scope_execution import execute_observed
from nz_coder.tool_platform.execution import ToolExecutionResult
from nz_coder.tools.files import read_file
from nz_coder.tools.process import execute_process_local
from tests.verification_scenarios import VerificationScenario


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("verification boundary test attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


def _process_call(command):
    return {"id": "background-start", "function": {"name": "process", "arguments": {
        "operation": "start", "command": command, "tty": False,
    }}}


def _local_launch(scenario, service, start, arguments):
    raw = execute_process_local(arguments, service=service, owner_session_id="boundary-session",
        owner_agent_id="main", event_bus=None, launch_id=start.execution_id)
    return ToolExecutionResult("process", arguments, str(raw), True, False, False, False,
        metadata=raw.metadata, execution_facts=raw.execution_facts, execution_id=start.execution_id)


class _InterleaveAfterCommit:
    """只在指定读取提交释放最外层锁后，放行真实进程并等待其事实提交。"""

    def __init__(self, lock, interleave):
        self._lock = lock
        self._local = threading.local()
        self._interleave = interleave
        self._armed_thread = None

    def arm(self):
        self._armed_thread = threading.get_ident()

    def __enter__(self):
        self._lock.acquire()
        self._local.depth = getattr(self._local, "depth", 0) + 1
        return self

    def __exit__(self, *_args):
        self._local.depth -= 1
        outermost = self._local.depth == 0
        self._lock.release()
        if outermost and self._armed_thread == threading.get_ident():
            self._armed_thread = None
            self._interleave()


def test_read_history_is_frozen_before_concurrent_process_terminal_commit(tmp_path, monkeypatch):
    # 把标签计算挪到提交锁外，会把真实旧读取贴成后台写入后的generation=1。
    (tmp_path / "note.txt").write_text("before the background write\n")
    (tmp_path / "worker.py").write_text(
        "import pathlib, sys\n"
        "sys.stdin.buffer.read(1)\n"
        "pathlib.Path('note.txt').write_text('after the background write\\n')\n"
        "print('PROCESS_TAG_BOUNDARY', flush=True)\n"
        "raise SystemExit(7)\n"
    )
    command = f"{shlex.quote(sys.executable)} worker.py"
    scenario = VerificationScenario(workspace=tmp_path, plan={"stages": []})
    scenario.state.verification_definition = {"command": command, "targets": []}
    service = ProcessService(tmp_path)
    terminal_done = threading.Event()
    terminal_errors = []
    receive = scenario.runtime._receive_local_terminal

    def receive_terminal(terminal):
        try:
            return receive(terminal)
        except BaseException as error:
            terminal_errors.append(error)
            raise
        finally:
            terminal_done.set()

    monkeypatch.setattr(scenario.runtime, "_receive_local_terminal", receive_terminal)
    monkeypatch.setattr("nz_coder.runtime.process.process_service.workspace_process_service", lambda _root: service)
    try:
        with scoped_workdir(tmp_path):
            call = _process_call(command)
            process_start = scenario.runtime.begin_tool(call)
            arguments = call["function"]["arguments"]
            result = execute_observed(scenario.store, process_start.version.generation,
                lambda: _local_launch(scenario, service, process_start, arguments),
                on_before=lambda reference: scenario.runtime.record_before(process_start, reference))
            scenario.runtime.record_result(result)
            process_id = json.loads(result.output)["process"]["process_id"]

            def interleave_terminal():
                service.write(process_id, "x", owner_session_id="boundary-session")
                assert terminal_done.wait(5), "real process terminal did not finish its state commit"

            lock = _InterleaveAfterCommit(scenario.context._state_lock, interleave_terminal)
            scenario.context._state_lock = lock
            scenario.domain._lock = lock
            read_start = scenario.runtime.begin_tool({"id": "read-before-terminal", "function": {
                "name": "read_file", "arguments": {"path": "note.txt"}}})
            result = ToolExecutionResult("read_file", {"path": "note.txt"}, str(read_file("note.txt")),
                True, False, False, False, execution_id=read_start.execution_id)
            observe_workspace = scenario.domain.observe_workspace

            def observe(observation):
                observe_workspace(observation)
                if observation.execution_id == read_start.execution_id:
                    lock.arm()

            monkeypatch.setattr(scenario.domain, "observe_workspace", observe)
            scenario.runtime.record_result(result)

        assert not terminal_errors
        assert "before the background write" in result.output
        assert (tmp_path / "note.txt").read_text() == "after the background write\n"
        assert scenario.state.mutation_generation == 1
        assert scenario.domain.execution_settled(process_start.execution_id)
        fact, = scenario.domain.snapshot().executions
        assert fact.exit_code == 7 and fact.outcome == "failed"
        assert "PROCESS_TAG_BOUNDARY" in fact.reason
        assert result.evidence_metadata == {
            "_nz_evidence_kind": "file_read", "_nz_resource": "note.txt", "_nz_mutation_generation": 0,
        }
    finally:
        service.close(deadline=time.monotonic() + 5)
        scenario.runtime.close()


def test_process_subscription_failure_settles_unlaunched_execution(tmp_path, monkeypatch):
    # begin_tool尚未返回时订阅失败，不能留下用户无法结清的inflight/rebuild状态。
    scenario = VerificationScenario(workspace=tmp_path, plan={"stages": []})
    service = ProcessService(tmp_path)

    def unavailable(_callback):
        raise OSError("controlled terminal subscription initialization failure")

    monkeypatch.setattr(service, "subscribe_terminal", unavailable)
    monkeypatch.setattr("nz_coder.runtime.process.process_service.workspace_process_service", lambda _root: service)
    try:
        with scoped_workdir(tmp_path), pytest.raises(OSError, match="subscription initialization failure"):
            scenario.runtime.begin_tool(_process_call("python -m unittest test_app.py"))
        assert service.list() == []
        assert scenario.domain._active == {}
        assert scenario.runtime._pending == {}
        assert scenario.domain.checkpoint()["rebuild_required"] is False
        assert scenario.domain.snapshot().executions == ()
    finally:
        service.close(deadline=time.monotonic() + 5)
        scenario.runtime.close()


@pytest.mark.parametrize("finish", ["terminal", "resource-close"])
def test_aborted_process_launch_without_started_receipt_stays_active_until_real_terminal(
    tmp_path, monkeypatch, finish,
):
    # 实际launch后丢started回执不等于未启动；普通abort不能吞掉稍后的真实终态。
    (tmp_path / "worker.py").write_text(
        "import sys\nsys.stdin.buffer.read(1)\n"
        "print('LAUNCHED_WITHOUT_RECEIPT', flush=True)\nraise SystemExit(7)\n"
    )
    command = f"{shlex.quote(sys.executable)} worker.py"
    scenario = VerificationScenario(workspace=tmp_path, plan={"stages": []})
    scenario.state.verification_definition = {"command": command, "targets": []}
    service = ProcessService(tmp_path)
    terminal_done = threading.Event()
    receive = scenario.runtime._receive_local_terminal
    launched = []

    def receive_terminal(terminal):
        try:
            return receive(terminal)
        finally:
            terminal_done.set()

    original_start = service.start

    def start_then_fail(*args, **kwargs):
        launched.append(original_start(*args, **kwargs))
        raise RuntimeError("controlled failure after actual process launch")

    monkeypatch.setattr(scenario.runtime, "_receive_local_terminal", receive_terminal)
    monkeypatch.setattr(service, "start", start_then_fail)
    monkeypatch.setattr("nz_coder.runtime.process.process_service.workspace_process_service", lambda _root: service)
    try:
        with scoped_workdir(tmp_path):
            call = _process_call(command)
            start = scenario.runtime.begin_tool(call)
            arguments = call["function"]["arguments"]
            with pytest.raises(RuntimeError, match="after actual process launch"):
                execute_observed(scenario.store, start.version.generation,
                    lambda: _local_launch(scenario, service, start, arguments),
                    on_before=lambda reference: scenario.runtime.record_before(start, reference))
            assert len(launched) == 1 and launched[0].pid is not None
            assert not terminal_done.is_set()
            scenario.runtime.abort_tool(start, "process", arguments)
            assert not scenario.domain.execution_settled(start.execution_id)
            assert start.execution_id in scenario.runtime._pending
            assert scenario.domain.snapshot().executions == ()
            assert not scenario.domain.snapshot().ready_for_completion

            if finish == "terminal":
                service.write(launched[0].process_id, "x", owner_session_id="boundary-session")
                assert terminal_done.wait(5), "launched process terminal was not delivered"
            else:
                service.close(deadline=time.monotonic() + 5)
                assert terminal_done.is_set()

        assert scenario.domain.execution_settled(start.execution_id)
        fact, = scenario.domain.snapshot().executions
        handle = service.get(launched[0].process_id)
        assert fact.exit_code == handle.exit_code
        assert fact.outcome != "succeeded"
        if finish == "terminal":
            assert fact.exit_code == 7 and fact.outcome == "failed"
            assert "LAUNCHED_WITHOUT_RECEIPT" in fact.reason
        else:
            assert handle.status == "killed"
            assert fact.outcome == "cancelled"
    finally:
        service.close(deadline=time.monotonic() + 5)
        scenario.runtime.close()
