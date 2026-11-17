"""正式入口与离线 probe 的停止、排空和异常收尾。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import sys
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]


def execution_fixture(monkeypatch, tmp_path, *, budget=False, fail=None, pending=False):
    spec = importlib.util.spec_from_file_location('review_cleanup', ROOT / 'scripts/review_effects_execution.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    directory = tmp_path / 'runtime'
    directory.mkdir()
    (directory / 'runtime.json').write_text('{"image":"offline-image"}')
    for version in ('old', 'new'):
        (directory / ('code-' + version + '-hashes.json')).write_text('{"files":{}}')
    frozen = tmp_path / 'frozen'
    source = frozen / 'prepared/autonomous/initial'
    source.mkdir(parents=True)
    (source / 'seed.txt').write_text('seed')
    (frozen / 'manifest.json').write_text(json.dumps({
        'order': [], 'autonomous': {'task': 'offline', 'initial_hashes': {
            'seed.txt': hashlib.sha256(b'seed').hexdigest()}},
    }))
    monkeypatch.setattr(module, 'FROZEN', frozen)
    monkeypatch.setattr(module, 'runtime_identity', lambda _d: {'image': 'offline-image'})
    calls, relays, ledgers, fakes, processes = [], [], [], [], []
    containers = {}
    primary = ValueError('primary ' + str(fail))

    class Ledger:
        def __init__(self, *_args, **kwargs):
            self.policy = kwargs.get('stop_on_rejection')
            self.failed_contracts = {}
            self.journal_fault = False
            self.admission_closed_reason = None
            self.attempts = {}
            self.input_occupied = self.output_occupied = 0
            self.cost_occupied_cny = 0
            self.cancelled = set()
            self.closed = False
            ledgers.append(self)

        def close_admissions(self, reason):
            self.admission_closed_reason = self.admission_closed_reason or reason

        def cancel(self, unit):
            calls.append('cancel')
            if budget:
                assert not any(r.active_handlers for r in relays), 'soft stop cut an unsettled handler'
            self.cancelled.add(unit)

        def close(self):
            assert not any(r.active_handlers for r in relays), 'ledger closed while handler may settle'
            calls.append('ledger-close')
            self.closed = True

    class Service:
        def __init__(self):
            self.stopped = threading.Event()
            self.serving = threading.Event()
            self.thread = None

        def serve_forever(self):
            self.thread = threading.current_thread()
            self.serving.set()
            self.stopped.wait(5)

        def shutdown(self):
            calls.append('fake-shutdown' if self in fakes else 'relay-shutdown')
            self.stopped.set()

        def server_close(self):
            calls.append('fake-close' if self in fakes else 'relay-close')

    class Relay(Service):
        def __init__(self, _path, **kwargs):
            super().__init__()
            self.binding = kwargs['binding']
            self.ledger = kwargs['ledger']
            self.active_handlers = int(pending or budget)
            relays.append(self)

        def wait_handlers(self, deadline):
            assert deadline == self.binding.deadline
            calls.append('handler-wait')
            if pending:
                return False
            self.active_handlers = 0
            return True

    class Fake(Service):
        def __init__(self, **_kwargs):
            super().__init__()
            self.server_port = 1
            self.requests = []
            fakes.append(self)

    original_spec = module.importlib.util.spec_from_file_location

    def fixture_spec(name, path, *args, **kwargs):
        if Path(path).name == 'review_execution_fake.py':
            return SimpleNamespace(loader=SimpleNamespace(exec_module=lambda m: m.__dict__.update(
                ExecutionFake=Fake, FormalFake=Fake)))
        return original_spec(name, path, *args, **kwargs)

    original_module = module.importlib.util.module_from_spec
    monkeypatch.setattr(module.importlib.util, 'spec_from_file_location', fixture_spec)
    monkeypatch.setattr(module.importlib.util, 'module_from_spec',
                        lambda s: SimpleNamespace() if isinstance(s, SimpleNamespace) else original_module(s))
    monkeypatch.setattr(module, 'RelayLedger', Ledger)
    monkeypatch.setattr(module, 'RelayServer', Relay)

    def inspect(name):
        case = containers[name]
        mounts = [(directory / 'code-new', '/runtime', False)] + [
            (case / src, dst, writable) for src, dst, writable in (
                ('driver', '/driver', False), ('workspace', '/workspace', True),
                ('home', '/home/agent', True), ('tmp', '/tmp', True), ('input', '/input', False),
                ('sockets', '/model', False), ('result', '/result', True), ('control', '/control', False))]
        return {'Id': name, 'Image': 'offline-image', 'Config': {'User': '1001:1001', 'Labels': {'nz-review-case': case.name}},
                'HostConfig': {'NetworkMode': 'none', 'ReadonlyRootfs': True, 'CapDrop': ['ALL'],
                               'Privileged': False, 'SecurityOpt': ['no-new-privileges'], 'PidMode': '',
                               'IpcMode': 'private', 'CgroupnsMode': 'private'},
                'Mounts': [{'Source': str(src), 'Destination': dst, 'RW': rw} for src, dst, rw in mounts],
                'State': {'Running': any(p.poll() is None for p in processes), 'ExitCode': 0}}

    def docker(*args, **_kwargs):
        calls.append(('docker', args[0]))
        if args[0] == 'ps':
            return SimpleNamespace(stdout='\n'.join(containers))
        if args[0] == 'create':
            name = args[args.index('--name') + 1]
            containers[name] = tmp_path / 'formal' / name.removeprefix('nz-review-')
            return SimpleNamespace(stdout=name)
        if args[0] == 'inspect':
            if fail == 'inspect':
                raise primary
            return SimpleNamespace(stdout=json.dumps([inspect(args[1])]))
        if args[0] == 'kill':
            for p in processes:
                p.returncode = -9
        if args[0] == 'rm':
            containers.pop(args[1])
        return SimpleNamespace(stdout='')

    class Process:
        returncode = None

        def __init__(self, argv, **_kwargs):
            if fail == 'popen':
                raise primary
            if argv[1] == 'run':
                name = argv[argv.index('--name') + 1]
                containers[name] = directory / name.removeprefix('nz-review-')
            processes.append(self)
            self.polls = 0
            self.returncode = None if budget or fail == 'poll' else 0
            if budget:
                ledgers[-1].admission_closed_reason = 'output_reservation_exhausted'

        def poll(self):
            nonlocal fail
            self.polls += 1
            if fail == 'poll':
                # 首次抛出主异常，清理仍需收掉已启动的进程。
                fail = None
                raise primary
            if budget and (tmp_path / 'formal/a01/control/cancel').exists():
                self.returncode = 0
            if budget and self.polls > 10:
                self.returncode = 0
            return self.returncode

        def wait(self, **_kwargs):
            calls.append('process-wait')
            return self.returncode

        def kill(self):
            calls.append('process-kill')
            self.returncode = -9

    original_save = module.save

    def save(path, value):
        if fail == 'summary' and path.name == 'formal-results.json':
            raise primary
        if fail == 'probe-save' and path.name == 'container-inspect.json':
            raise primary
        if fail == 'probe-preledger' and path.name == 'probe-config.json':
            raise primary
        return original_save(path, value)

    monkeypatch.setattr(module, 'docker', docker)
    monkeypatch.setattr(module.subprocess, 'Popen', Process)
    monkeypatch.setattr(module, 'save', save)
    return SimpleNamespace(module=module, directory=directory, calls=calls, relays=relays, ledgers=ledgers,
                           fakes=fakes, processes=processes, primary=primary, containers=containers)


def run_formal(fx, tmp_path):
    fx.module.run_formal(fx.directory, tmp_path / 'formal', counter=fx.module.local_counter,
                         upstream='http://127.0.0.1:1/v1/chat/completions')


def test_formal_budget_stop_signals_worker_then_drains_before_ledger_close(monkeypatch, tmp_path):
    fx = execution_fixture(monkeypatch, tmp_path, budget=True)
    run_formal(fx, tmp_path)
    assert fx.ledgers[0].policy is True
    assert (tmp_path / 'formal/a01/control/cancel').exists()
    assert fx.calls.index('handler-wait') < fx.calls.index('ledger-close')
    assert fx.ledgers[0].closed and not fx.containers


@pytest.mark.parametrize('failure', ['popen', 'inspect', 'poll'])
def test_formal_primary_failure_reaps_owned_resources(monkeypatch, tmp_path, failure):
    fx = execution_fixture(monkeypatch, tmp_path, fail=failure)
    with pytest.raises(ValueError) as caught:
        run_formal(fx, tmp_path)
    assert caught.value is fx.primary
    assert all(r.stopped.is_set() for r in fx.relays)
    assert 'handler-wait' in fx.calls and fx.ledgers[0].closed
    if failure != 'inspect':
        assert not fx.containers
    if fx.processes:
        assert all(p.returncode is not None for p in fx.processes)
        assert 'process-wait' in fx.calls


def test_formal_summary_failure_still_closes_ledger(monkeypatch, tmp_path):
    fx = execution_fixture(monkeypatch, tmp_path, budget=True, fail='summary')
    with pytest.raises(ValueError) as caught:
        run_formal(fx, tmp_path)
    assert caught.value is fx.primary
    assert fx.ledgers[0].closed


def test_formal_summary_hash_failure_still_closes_ledger(monkeypatch, tmp_path):
    fx = execution_fixture(monkeypatch, tmp_path, budget=True)
    original_sha = fx.module.sha
    failure = OSError('summary hash unavailable')

    def sha(path):
        if path == fx.directory / 'runtime.json':
            raise failure
        return original_sha(path)

    monkeypatch.setattr(fx.module, 'sha', sha)
    with pytest.raises(OSError) as caught:
        run_formal(fx, tmp_path)
    assert caught.value is failure
    assert fx.ledgers[0].closed


def test_formal_summary_failure_does_not_cover_primary(monkeypatch, tmp_path):
    fx = execution_fixture(monkeypatch, tmp_path, fail='popen')
    original_save = fx.module.save

    def save(path, value):
        if path.name == 'formal-results.json':
            raise OSError('secondary summary failure')
        return original_save(path, value)

    monkeypatch.setattr(fx.module, 'save', save)
    with pytest.raises(ValueError) as caught:
        run_formal(fx, tmp_path)
    assert caught.value is fx.primary
    assert fx.ledgers[0].closed
    assert json.loads((tmp_path / 'formal/cleanup-failures.json').read_text())[0]['stage'] == 'formal-summary'


def test_formal_listener_start_failure_closes_without_shutdown_deadlock(monkeypatch, tmp_path):
    fx = execution_fixture(monkeypatch, tmp_path)
    failure = RuntimeError('listener cannot start')

    def start(_thread):
        raise failure

    monkeypatch.setattr(threading.Thread, 'start', start)
    with pytest.raises(RuntimeError) as caught:
        run_formal(fx, tmp_path)
    assert caught.value is failure
    assert 'relay-shutdown' not in fx.calls
    assert 'relay-close' in fx.calls and fx.ledgers[0].closed


def test_formal_keyboard_interrupt_hard_cancels_owned_run(monkeypatch, tmp_path):
    fx = execution_fixture(monkeypatch, tmp_path)
    failure = KeyboardInterrupt('manual host interrupt')

    def launch(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(fx.module.subprocess, 'Popen', launch)
    with pytest.raises(KeyboardInterrupt) as caught:
        run_formal(fx, tmp_path)
    assert caught.value is failure
    assert 'a01' in fx.ledgers[0].cancelled
    assert fx.calls.index('cancel') < fx.calls.index('handler-wait')
    assert fx.ledgers[0].closed


def test_formal_pending_handler_keeps_ledger_open(monkeypatch, tmp_path):
    fx = execution_fixture(monkeypatch, tmp_path, budget=True, pending=True)
    with pytest.raises(RuntimeError, match='handler'):
        run_formal(fx, tmp_path)
    assert not fx.ledgers[0].closed


@pytest.mark.parametrize('stage', ['process-wait', 'shutdown', 'server-close', 'listener-join', 'handler-wait'])
def test_cleanup_interrupt_cancels_real_ledger_before_next_owner_wait(monkeypatch, tmp_path, stage):
    from nz_coder.evaluation.model_relay import RelayLedger, RelayLimits

    fx = execution_fixture(monkeypatch, tmp_path)
    ledger = RelayLedger(tmp_path / 'interrupt-ledger.jsonl', RelayLimits())
    interrupted = KeyboardInterrupt('manual interrupt during ' + stage)
    cancelled = threading.Event()
    original_cancel = ledger.cancel
    handler_threads, listener_threads, stops = [], [], []
    observed = []

    def cancel(run):
        original_cancel(run)
        cancelled.set()

    monkeypatch.setattr(ledger, 'cancel', cancel)

    class Relay:
        def __init__(self, first):
            self.first = first
            self.stop = threading.Event()
            stops.append(self.stop)
            self.binding = SimpleNamespace(deadline=time.monotonic() + 2)
            self.handler = threading.Thread(target=cancelled.wait, daemon=True)
            handler_threads.append(self.handler)
            self.handler.start()

        @property
        def active_handlers(self):
            return int(self.handler.is_alive())

        def serve_forever(self):
            self.stop.wait(5)

        def shutdown(self):
            self.stop.set()
            if self.first and stage == 'shutdown':
                raise interrupted

        def server_close(self):
            if self.first and stage == 'server-close':
                raise interrupted

        def wait_handlers(self, deadline):
            if self.first and stage == 'handler-wait':
                raise interrupted
            observed.append(self.first)
            assert 'owned-run' in ledger.cancelled, 'next owner wait preceded hard cancellation'
            self.handler.join(max(0, deadline - time.monotonic()))
            return not self.handler.is_alive()

    class Process:
        waits = 0

        def wait(self, **_kwargs):
            self.waits += 1
            if self.waits == 1 and stage == 'process-wait':
                raise interrupted
            return 0

        def kill(self):
            pass

    relays = []
    try:
        for first in (True, False):
            relay = Relay(first)
            thread = threading.Thread(target=relay.serve_forever, daemon=True)
            listener_threads.append(thread)
            relays.append((relay, thread))
            thread.start()
        if stage == 'listener-join':
            original_join = listener_threads[0].join

            def join(_timeout=None):
                raise interrupted

            monkeypatch.setattr(listener_threads[0], 'join', join)
        failures = []
        fx.module._cleanup_case(tmp_path / 'case', 'owned-run', ledger, relays, Process(), False, failures)
        if stage == 'listener-join':
            monkeypatch.setattr(listener_threads[0], 'join', original_join)
            original_join(2)
        assert 'owned-run' in ledger.cancelled
        assert False in observed, 'remaining relay was not drained'
        assert all(not thread.is_alive() for thread in handler_threads + listener_threads)
        assert any(exc is interrupted for _name, exc in failures)
        fx.module._close_ledger(ledger, relays, failures)
        assert ledger._closed
        with pytest.raises(KeyboardInterrupt) as caught:
            fx.module._report_cleanup(tmp_path / 'interrupt-cleanup.json', failures)
        assert caught.value is interrupted
    finally:
        cancelled.set()
        for stop in stops:
            stop.set()
        if stage == 'listener-join':
            monkeypatch.setattr(listener_threads[0], 'join', original_join)
        for thread in handler_threads + listener_threads:
            thread.join(2)
        if not ledger._closed:
            ledger.close()


def test_alive_listener_keeps_real_ledger_fd_open_until_listener_exits(monkeypatch, tmp_path):
    from nz_coder.evaluation.model_relay import RelayLedger, RelayLimits

    fx = execution_fixture(monkeypatch, tmp_path)
    ledger = RelayLedger(tmp_path / 'listener-ledger.jsonl', RelayLimits())
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    original_join = thread.join
    thread.start()

    def shutdown():
        raise OSError('listener shutdown failed')

    relay = SimpleNamespace(shutdown=shutdown, server_close=lambda: None, active_handlers=0)
    monkeypatch.setattr(thread, 'join', lambda _timeout: original_join(0))
    try:
        failures = []
        fx.module._close_services([(relay, thread)], failures)
        assert thread.is_alive()
        fx.module._close_ledger(ledger, [(relay, thread)], failures)
        assert not ledger._closed
        os.fstat(ledger.fd)
        assert any(stage == 'ledger-close' for stage, _exc in failures)
        release.set()
        original_join(2)
        fx.module._close_ledger(ledger, [(relay, thread)], [])
        assert ledger._closed
        with pytest.raises(OSError):
            os.fstat(ledger.fd)
    finally:
        release.set()
        original_join(2)
        if not ledger._closed:
            ledger.close()


def test_interrupted_handler_drain_retries_current_owner_before_ledger_close(monkeypatch, tmp_path):
    from nz_coder.evaluation.model_relay import RelayLedger, RelayLimits

    fx = execution_fixture(monkeypatch, tmp_path)
    ledger = RelayLedger(tmp_path / 'retry-ledger.jsonl', RelayLimits())
    cancelled, retry_entered, listener_stop = threading.Event(), threading.Event(), threading.Event()
    deadline = time.monotonic() + 2
    interrupted = KeyboardInterrupt('first drain interrupted')
    original_cancel = ledger.cancel
    waits = []

    def cancel(run):
        original_cancel(run)
        cancelled.set()

    def handle():
        cancelled.wait(5)
        # 取消只是通知；实际退出还需控制器继续等待当前 owner。
        retry_entered.wait(5)

    monkeypatch.setattr(ledger, 'cancel', cancel)
    handler = threading.Thread(target=handle, daemon=True)
    listener = threading.Thread(target=listener_stop.wait, daemon=True)
    handler.start()
    listener.start()

    class Relay:
        binding = SimpleNamespace(deadline=deadline)

        @property
        def active_handlers(self):
            return int(handler.is_alive())

        def shutdown(self):
            listener_stop.set()

        def server_close(self):
            pass

        def wait_handlers(self, original_deadline):
            waits.append(original_deadline)
            if len(waits) == 1:
                raise interrupted
            assert 'owned-run' in ledger.cancelled
            retry_entered.set()
            handler.join(max(0, original_deadline - time.monotonic()))
            return not handler.is_alive()

    relays = [(Relay(), listener)]
    try:
        failures = []
        fx.module._cleanup_case(tmp_path / 'case', 'owned-run', ledger, relays, None, False, failures)
        fx.module._close_ledger(ledger, relays, failures)
        assert ledger._closed, 'interrupted owner never received its final drain wait'
        assert not handler.is_alive() and not listener.is_alive()
        assert waits == [deadline, deadline]
        assert [(stage, exc) for stage, exc in failures] == [('handler-wait', interrupted)]
        with pytest.raises(OSError):
            os.fstat(ledger.fd)
        with pytest.raises(KeyboardInterrupt) as caught:
            fx.module._report_cleanup(tmp_path / 'retry-cleanup.json', failures)
        assert caught.value is interrupted
    finally:
        cancelled.set()
        retry_entered.set()
        listener_stop.set()
        handler.join(2)
        listener.join(2)
        if not ledger._closed:
            ledger.close()


def test_cleanup_interrupt_keeps_original_run_error_and_cleans_remaining_relays(monkeypatch, tmp_path):
    fx = execution_fixture(monkeypatch, tmp_path, fail='popen')
    relay_type = fx.module.RelayServer
    interrupted = KeyboardInterrupt('manual interrupt during failed run cleanup')

    def relay(*args, **kwargs):
        result = relay_type(*args, **kwargs)
        if len(fx.relays) == 1:
            shutdown = result.shutdown

            def stop():
                shutdown()
                raise interrupted

            monkeypatch.setattr(result, 'shutdown', stop)
        return result

    monkeypatch.setattr(fx.module, 'RelayServer', relay)
    with pytest.raises(ValueError) as caught:
        run_formal(fx, tmp_path)
    assert caught.value is fx.primary
    assert 'a01' in fx.ledgers[0].cancelled
    assert all(r.stopped.is_set() for r in fx.relays)
    assert fx.ledgers[0].closed
    failures = json.loads((tmp_path / 'formal/a01/cleanup-failures.json').read_text())
    assert failures[0]['type'] == 'KeyboardInterrupt'


@pytest.mark.parametrize('failure', ['popen', 'probe-save'])
def test_probe_failure_closes_all_owned_services_and_ledger(monkeypatch, tmp_path, failure):
    fx = execution_fixture(monkeypatch, tmp_path, fail=failure)
    with pytest.raises(ValueError) as caught:
        fx.module.probe(fx.directory, 'offline', ['positive'])
    assert caught.value is fx.primary
    assert len(fx.relays) == 2 and fx.fakes
    assert all(r.stopped.is_set() for r in fx.relays + fx.fakes)
    assert fx.calls.index('handler-wait') < fx.calls.index('ledger-close')
    assert fx.ledgers[0].closed and not fx.containers


def test_probe_preledger_failure_still_closes_fake(monkeypatch, tmp_path):
    fx = execution_fixture(monkeypatch, tmp_path, fail='probe-preledger')
    with pytest.raises(ValueError) as caught:
        fx.module.probe(fx.directory, 'offline', ['positive'])
    assert caught.value is fx.primary
    assert not fx.ledgers and not fx.relays
    assert fx.fakes[0].stopped.is_set()
    assert not fx.fakes[0].thread.is_alive()


def test_probe_written_heartbeat_is_executable(monkeypatch, tmp_path):
    fx = execution_fixture(monkeypatch, tmp_path, fail='popen')
    with pytest.raises(ValueError):
        fx.module.probe(fx.directory, 'offline', ['positive'])
    path = fx.directory / 'probe-positive-offline/workspace/heartbeat.py'
    compile(path.read_text(), str(path), 'exec')


def test_formal_local_fake_cleanup_preserves_status_error(monkeypatch, tmp_path):
    fx = execution_fixture(monkeypatch, tmp_path)
    original_save = fx.module.save

    def status(*_args, **_kwargs):
        raise fx.primary

    def save(path, value):
        if path.name.endswith('-fake-requests.json'):
            raise OSError('fake requests write failure')
        return original_save(path, value)

    monkeypatch.setattr(fx.module, 'execution_status', status)
    monkeypatch.setattr(fx.module, 'save', save)
    monkeypatch.setattr(sys, 'argv', ['review_effects_execution.py', 'formal-local',
        '--artifact-root', str(fx.directory), '--output-root', str(tmp_path / 'formal-local')])
    with pytest.raises(ValueError) as caught:
        fx.module.main()
    assert caught.value is fx.primary
    assert fx.fakes[0].stopped.is_set()
    assert not fx.fakes[0].thread.is_alive()
    assert 'fake-close' in fx.calls


@pytest.mark.parametrize('reason', ['journal', 'contract', 'manual', 'cancelled'])
def test_probe_hard_stop_kills_container_before_handler_drain(monkeypatch, tmp_path, reason):
    fx = execution_fixture(monkeypatch, tmp_path, fail='probe-save')
    base = fx.module.subprocess.Popen

    class LiveProcess(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.returncode = None
            if reason == 'journal':
                fx.ledgers[0].journal_fault = True
            elif reason == 'contract':
                fx.ledgers[0].failed_contracts['positive'] = {'reason': 'billing failed'}
            elif reason == 'manual':
                (fx.directory / 'probe-positive-offline/control/cancel').write_text('manual')
            else:
                fx.ledgers[0].cancelled.add('positive')

        def poll(self):
            result = super().poll()
            if self.polls > 10:
                self.returncode = 0
                return 0
            return result

    monkeypatch.setattr(fx.module.subprocess, 'Popen', LiveProcess)
    with pytest.raises(ValueError) as caught:
        fx.module.probe(fx.directory, 'offline', ['positive'])
    assert caught.value is fx.primary
    assert 'positive' in fx.ledgers[0].cancelled
    assert ('docker', 'kill') in fx.calls
    assert fx.calls.index('cancel') < fx.calls.index('handler-wait')


@pytest.mark.parametrize('disconnect', [False, True])
def test_formal_budget_stop_collects_real_inflight_usage_after_worker_exit(monkeypatch, disconnect):
    import httpx
    from nz_coder.evaluation.model_relay import RelayLedger, RelayServer

    entered, release = threading.Event(), threading.Event()
    upstream_requests, client_responses, errors = [], [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            request = json.loads(raw)
            upstream_requests.append(request)
            entered.set()
            if not release.wait(5):
                return
            body = json.dumps({'model': request['model'],
                'choices': [{'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': len(raw), 'completion_tokens': 7, 'total_tokens': len(raw) + 7}}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    upstream_thread = threading.Thread(target=upstream.serve_forever)
    upstream_thread.start()
    clients = []
    try:
        # 保持 Unix socket 路径低于系统长度限制。
        with TemporaryDirectory(prefix='nz-formal-', dir='/tmp') as root:
            tmp_path = Path(root)
            fx = execution_fixture(monkeypatch, tmp_path)
            actual_ledgers, actual_relays, waits = [], [], []

            def ledger(*args, **kwargs):
                result = RelayLedger(*args, **kwargs)
                actual_ledgers.append(result)
                return result

            def relay(*args, **kwargs):
                result = RelayServer(*args, **kwargs)
                actual_relays.append(result)
                original_wait = result.wait_handlers

                def wait_handlers(deadline):
                    assert deadline == result.binding.deadline
                    assert (tmp_path / 'formal/a01/control/cancel').exists()
                    assert not result.ledger.cancelled
                    assert not result.ledger._closed
                    waits.append(result.binding.role)
                    release.set()
                    return original_wait(deadline)

                result.wait_handlers = wait_handlers
                return result

            def request(role):
                with httpx.Client(transport=httpx.HTTPTransport(
                        uds=str(tmp_path / 'formal/a01/sockets' / (role + '.sock'))),
                        trust_env=False, timeout=5) as client:
                    return client.post('http://localhost/v1/chat/completions',
                        headers={'X-NZ-Call-Id': role}, json={'model': 'deepseek-v4-flash',
                            'max_tokens': 64000, 'messages': [{'role': 'user', 'content': 'offline'}], 'stream': False})

            def main_request():
                try:
                    client_responses.append(request('main'))
                except BaseException as exc:
                    errors.append(exc)

            class Process:
                returncode = None
                main_socket = None

                def __init__(self, *_args, **_kwargs):
                    fx.processes.append(self)
                    if disconnect:
                        self.main_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        self.main_socket.connect(str(tmp_path / 'formal/a01/sockets/main.sock'))
                        body = json.dumps({'model': 'deepseek-v4-flash', 'max_tokens': 64000,
                            'messages': [{'role': 'user', 'content': 'offline'}], 'stream': False}).encode()
                        headers = ('POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n'
                                   'X-NZ-Call-Id: main\r\nContent-Type: application/json\r\n'
                                   f'Content-Length: {len(body)}\r\n\r\n').encode()
                        self.main_socket.sendall(headers + body)
                    else:
                        thread = threading.Thread(target=main_request)
                        clients.append(thread)
                        thread.start()
                    assert entered.wait(3), 'main request never reached local upstream'
                    denied = request('auxiliary')
                    assert denied.status_code == 429

                def poll(self):
                    if (tmp_path / 'formal/a01/control/cancel').exists():
                        self.returncode = 0
                        if self.main_socket is not None:
                            self.main_socket.close()
                            self.main_socket = None
                    return self.returncode

                def wait(self, **_kwargs):
                    return self.returncode

                def kill(self):
                    self.returncode = -9

            monkeypatch.setattr(fx.module, 'RelayLedger', ledger)
            monkeypatch.setattr(fx.module, 'RelayServer', relay)
            monkeypatch.setattr(fx.module.subprocess, 'Popen', Process)
            fx.module.run_formal(fx.directory, tmp_path / 'formal', counter=fx.module.local_counter,
                upstream=f'http://127.0.0.1:{upstream.server_port}/v1/chat/completions')
            for thread in clients:
                thread.join(3)
                assert not thread.is_alive()
            assert not errors
            if not disconnect:
                assert client_responses[0].status_code == 200
            assert len(upstream_requests) == 1
            assert actual_ledgers[0].stop_on_rejection is True
            assert actual_ledgers[0]._closed
            assert len(actual_ledgers[0].attempts) == 1
            settled = [e for e in actual_ledgers[0].events if e['event'] == 'settled']
            assert len(settled) == 1 and settled[0]['usage_valid'] is True
            assert actual_ledgers[0].output_occupied == 7
            assert set(waits) == {'main', 'auxiliary'}
            assert all(r.active_handlers == 0 for r in actual_relays)
    finally:
        release.set()
        for thread in clients:
            thread.join(3)
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(3)
