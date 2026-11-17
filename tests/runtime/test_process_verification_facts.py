"""无 read 也交付真实进程终态，通知不能提前于资源线程退出。"""
from __future__ import annotations

import shlex
import sys
import threading
import time

import pytest

from nz_coder.runtime.process.process_service import ProcessService, ProcessStateError


def _command(body):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(body)}"


def test_terminal_arrives_without_read_and_after_worker_exit(tmp_path):
    service = ProcessService(tmp_path)
    arrived, facts = threading.Event(), []

    def observe(fact):
        record = service._records[fact.handle.process_id]
        assert not record.waiter.is_alive()
        assert record.reader is None or not record.reader.is_alive()
        facts.append(fact)
        arrived.set()

    unsubscribe = service.subscribe_terminal(observe)
    try:
        handle = service.start(_command("raise SystemExit(7)"), cwd=tmp_path,
                               owner_session_id="session", tty=False, launch_id="run/launch")
        assert arrived.wait(3), "terminal must be delivered without read/status"
        assert len(facts) == 1
        assert facts[0].handle.process_id == handle.process_id
        assert facts[0].handle.exit_code == 7
        assert facts[0].output_complete
        assert facts[0].launch_id == "run/launch"
        service.get(handle.process_id)
        service.list()
        assert len(facts) == 1
    finally:
        unsubscribe()
        service.close(deadline=time.monotonic() + 3)


def test_cleanup_event_does_not_publish_before_waiter_really_returns(tmp_path):
    service = ProcessService(tmp_path)
    cleanup_entered, release_waiter, arrived = threading.Event(), threading.Event(), threading.Event()

    class Bus:
        def publish(self, kind, _properties):
            if kind == "process.exited":
                cleanup_entered.set()
                assert release_waiter.wait(3)

    bus = Bus()
    unsubscribe = service.subscribe_terminal(lambda fact: arrived.set())
    try:
        service.start(_command("pass"), cwd=tmp_path, owner_session_id="session", tty=False, event_bus=bus)
        assert cleanup_entered.wait(3)
        assert not arrived.is_set(), "cleanup_done alone is not an exited worker"
        release_waiter.set()
        assert arrived.wait(3)
    finally:
        release_waiter.set()
        unsubscribe()
        service.close(deadline=time.monotonic() + 3)


def test_unacknowledged_terminal_keeps_slot_until_ack(tmp_path):
    service = ProcessService(tmp_path, max_processes=1)
    arrived = threading.Event()

    def hold(_fact):
        arrived.set()
        return False

    unsubscribe = service.subscribe_terminal(hold)
    try:
        first = service.start(_command("pass"), cwd=tmp_path, owner_session_id="session", tty=False)
        assert arrived.wait(3)
        with pytest.raises(ProcessStateError, match="limit"):
            service.start(_command("pass"), cwd=tmp_path, owner_session_id="session", tty=False)
        service.acknowledge_terminal(first.process_id, hold)
        unsubscribe()
        second = service.start(_command("pass"), cwd=tmp_path, owner_session_id="session", tty=False)
        assert second.process_id != first.process_id
    finally:
        unsubscribe()
        service.close(deadline=time.monotonic() + 3)


def test_delivery_thread_start_failure_allows_clean_retry(tmp_path, monkeypatch):
    actual_start = threading.Thread.start
    fail_once = [True]

    def start(worker):
        if worker.name == "nz-process-terminal-delivery" and fail_once[0]:
            fail_once[0] = False
            raise RuntimeError("controlled delivery thread start failure")
        return actual_start(worker)

    monkeypatch.setattr(threading.Thread, "start", start)
    service, arrived = ProcessService(tmp_path), threading.Event()
    with pytest.raises(RuntimeError, match="controlled"):
        service.subscribe_terminal(lambda fact: None)
    unsubscribe = service.subscribe_terminal(lambda fact: arrived.set())
    try:
        service.start(_command("pass"), cwd=tmp_path, owner_session_id="session", tty=False)
        assert arrived.wait(2), "failed setup must not prevent the next subscriber from receiving facts"
    finally:
        unsubscribe()
        service.close(deadline=time.monotonic() + 3)


def test_terminal_barrier_waits_for_fact_receiver_before_freeze(tmp_path):
    service = ProcessService(tmp_path)
    entered, release, barrier_done = threading.Event(), threading.Event(), threading.Event()

    def observe(_fact):
        entered.set()
        assert release.wait(3)

    unsubscribe = service.subscribe_terminal(observe)
    worker = None
    try:
        service.start(_command("pass"), cwd=tmp_path, owner_session_id="session", tty=False)
        assert entered.wait(3)
        assert service.wait_terminal_delivery(deadline=time.monotonic()) is False
        result = []
        def wait():
            result.append(service.wait_terminal_delivery(deadline=time.monotonic() + 3))
            barrier_done.set()
        worker = threading.Thread(target=wait)
        worker.start()
        assert not barrier_done.is_set()
        release.set()
        assert barrier_done.wait(3)
        assert result == [True]
    finally:
        release.set()
        if worker is not None:
            worker.join(3)
        unsubscribe()
        service.close(deadline=time.monotonic() + 3)


def test_reader_error_with_zero_exit_cannot_pass_verification(tmp_path, monkeypatch):
    from nz_coder.runtime.adapters.verification_facts import command_receipt
    from nz_coder.runtime.adapters.verification_plans import check_spec
    from nz_coder.runtime.verification.domain.identity import command_identity
    from nz_coder.runtime.verification.domain.models import ExecutionFact, RunScope, TaskScope, WorkspaceVersion
    from nz_coder.runtime.verification.domain.service import VerificationDomain
    import nz_coder.runtime.process.process_service as process_module

    class ReadErrorBackend:
        pid, tty, pipefail = 999999999, False, True
        def read_bytes(self, size):
            raise OSError('output transport failed')
        def wait(self, timeout=None): return 0
        def poll(self): return 0
        def terminate_tree(self, grace_seconds): pass
        def close(self): pass

    monkeypatch.setattr(process_module, 'create_process_backend', lambda *a, **kw: ReadErrorBackend())
    service, arrived, facts = ProcessService(tmp_path), threading.Event(), []
    unsubscribe = service.subscribe_terminal(lambda fact: (facts.append(fact), arrived.set()))
    try:
        service.start('python -m unittest discover', cwd=tmp_path, owner_session_id='s', tty=False, launch_id='launch')
        assert arrived.wait(3)
        terminal = facts[0]
        assert terminal.handle.exit_code == 0
        assert terminal.output_complete is False
        receipt = command_receipt('process', terminal.handle.command, terminal.handle.cwd,
            outcome='succeeded', exit_code=0, output=terminal.output, output_complete=terminal.output_complete,
            pipefail=terminal.pipefail)
        scope = RunScope(TaskScope('s', 'task', str(tmp_path)), 'run', 'agent')
        version = WorkspaceVersion()
        fact = ExecutionFact(receipt.execution_id, scope, command_identity(receipt.command, receipt.cwd),
            receipt.outcome, version, version, receipt.capabilities, receipt.targets, receipt.exit_code,
            test_count=receipt.test_count, output_complete=receipt.output_complete,
            reliable=receipt.reliable, reason=receipt.reason)
        spec = check_spec(receipt.command, str(tmp_path), capability='test', required=True, source='user_declared')
        domain = VerificationDomain(lock=threading.RLock(), scope=scope, version=version)
        domain.register_check(spec)
        domain.begin_execution(fact.execution_id, (spec.id,))
        domain.observe_execution(fact)
        assert domain.snapshot().decision(spec.id).status == 'pending'
        assert not domain.snapshot().ready_for_completion
    finally:
        service.wait_terminal_delivery(deadline=time.monotonic() + 3)
        unsubscribe()
        service.close(deadline=time.monotonic() + 3)
