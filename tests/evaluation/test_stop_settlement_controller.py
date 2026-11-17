"""停止控制器的退出顺序与主异常保留，所有服务和费用均为本地夹具。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]


def load_script(monkeypatch, relative, name):
    prepare = SimpleNamespace(
        capture_configuration=lambda *_args: {
            'run_settings': {}, 'request_budget': {}, 'capabilities': {}},
        save=lambda path, value: path.write_text(json.dumps(value)),
    )
    monkeypatch.setitem(sys.modules, 'prepare', prepare)
    monkeypatch.setitem(sys.modules, 'run_batch', SimpleNamespace())
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def worker_fixture(monkeypatch, tmp_path, *, run_error=None, cleanup_error=None, shutdown_error=None):
    from nz_coder import http_service
    from nz_coder.providers import models

    slot = tmp_path / 'slot'
    (slot / 'workspace').mkdir(parents=True)
    (slot / 'prompt.txt').write_text('A local offline task.')
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('NZ_CODER_USER_CONFIG', str(tmp_path / 'user.env'))
    monkeypatch.setenv('NZ_CODER_WORKSPACE_TRUST_STORE', str(tmp_path / 'trust.json'))
    module = load_script(monkeypatch, 'docs/evidence/working-memory-real-ab8-2026-11-04/worker.py', 'test_stop_worker')
    monkeypatch.setattr(models, 'active_model_selection', lambda *_a, **_kw: SimpleNamespace(model_id='offline'))
    stopped = threading.Event()
    calls = []

    class Service:
        base_url = 'http://127.0.0.1'
        token = 'offline-token'
        manager = SimpleNamespace(get=lambda _sid: SimpleNamespace(_lock=threading.Lock(), history=[]))

        def __init__(self, **_kwargs):
            pass

        def serve_forever(self):
            stopped.wait(5)
            calls.append('thread-exit')

        def close_after_serve(self):
            calls.append('close-unserved')
            stopped.set()

        def shutdown(self):
            calls.append('shutdown')
            stopped.set()
            if shutdown_error:
                raise shutdown_error

    class Client:
        def __init__(self, *_args, **_kwargs):
            self.gets = 0

        def list_workspaces(self):
            return [{'id': 'ws', 'path': str(slot / 'workspace')}]

        def create_session(self, **_kwargs):
            return {'id': 'session'}

        def run(self, *_args):
            if run_error:
                raise run_error
            return {}

        def get_session(self, _sid):
            self.gets += 1
            if cleanup_error:
                raise cleanup_error
            return {'running': False, 'last_result': {'status': 'completed'}}

        def attach_snapshot(self, _sid):
            return {}

        def abort(self, _sid):
            calls.append('abort')

    monkeypatch.setattr(http_service, 'SessionHTTPService', Service)
    monkeypatch.setattr(http_service, 'NZCoderClient', Client)
    return module, slot, calls


def test_worker_run_failure_still_shuts_down_when_status_read_fails(monkeypatch, tmp_path):
    primary = ValueError('original execution failed')
    module, slot, calls = worker_fixture(
        monkeypatch, tmp_path, run_error=primary, cleanup_error=RuntimeError('status unavailable'))
    with pytest.raises(ValueError, match='original execution failed') as caught:
        module.main(slot, False)
    assert caught.value is primary
    assert calls == ['shutdown', 'thread-exit']
    cleanup = json.loads((slot / 'cleanup.json').read_text())
    assert cleanup['service_thread_alive'] is False
    assert cleanup['failures']


def test_worker_shutdown_failure_does_not_skip_join_or_hide_success_path_error(monkeypatch, tmp_path):
    failure = RuntimeError('shutdown incomplete')
    module, slot, calls = worker_fixture(monkeypatch, tmp_path, shutdown_error=failure)
    with pytest.raises(RuntimeError, match='shutdown incomplete') as caught:
        module.main(slot, False)
    assert caught.value is failure
    assert calls == ['shutdown', 'thread-exit']
    assert json.loads((slot / 'cleanup.json').read_text())['service_thread_alive'] is False


@pytest.mark.parametrize('relative,finalizer', [
    ('docs/evidence/workspace-paid-full-2026-11-06/run_full.py', 'summarize_budget'),
    ('docs/evidence/workspace-paid-resume-2026-11-06/run_resume.py', 'summarize'),
])
def test_summary_failure_preserves_base_execution_exception(monkeypatch, tmp_path, relative, finalizer):
    module = load_script(monkeypatch, relative, 'test_stop_wrapper')
    module.OUT = tmp_path
    prior = {'prior_actual_cny': '2.55'}
    (tmp_path / 'cumulative-accounting.json').write_text(json.dumps(prior))
    primary = ValueError('primary provider failure')

    def execute():
        raise primary

    def summarize(_prior):
        raise OSError('summary cannot be written')

    monkeypatch.setattr(module, 'accounting', lambda: prior)
    monkeypatch.setattr(module, 'driver', lambda: SimpleNamespace(run=execute))
    monkeypatch.setattr(module, finalizer, summarize)
    with pytest.raises(ValueError, match='primary provider failure') as caught:
        module.run()
    assert caught.value is primary


def controller_fixture(monkeypatch, tmp_path, *, stop=False, launch_error=None, summary_error=None, initial_handlers=1):
    import dotenv
    from nz_coder.evaluation import model_relay

    module = load_script(monkeypatch, 'docs/evidence/workspace-end-to-end-2026-11-05/run_projects.py', 'test_stop_base')
    module.OUT = tmp_path
    module.ROOT = tmp_path
    slot = tmp_path / 'attempt'
    (slot / 'workspace').mkdir(parents=True)
    manifest = {'model': 'deepseek-v4-flash', 'attempt_seconds': 2, 'authorization': 'offline',
                'attempts': [{'id': 'python', 'unit': 'u01', 'path': str(slot)}]}
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    counter = SimpleNamespace(contract_id='offline-counter', status=lambda: {})
    module.run_batch.verify_sources = lambda _m: None
    module.run_batch.counter = lambda: counter
    monkeypatch.setattr(module, 'environment', lambda *_args: {})
    monkeypatch.setattr(dotenv, 'dotenv_values', lambda *_a: {'API_BASE_URL': 'https://api.deepseek.com', 'API_KEY': 'offline'})
    calls = []
    relays = []

    class Ledger:
        def __init__(self, *_args, **kwargs):
            self.policy = kwargs.get('stop_on_rejection')
            self.failed_contracts = {}
            self.ambiguous = set()
            self.cancelled = set()
            self.events = []
            self.attempts = {}
            self.cost_occupied_cny = 0
            self.journal_fault = False
            self.admission_closed_reason = None

        def close_admissions(self, reason):
            calls.append('close-admissions')
            self.admission_closed_reason = self.admission_closed_reason or reason

        def stopped(self, _binding):
            return ''

        def cancel(self, run):
            assert all(not r.active_handlers for r in relays), 'budget cancellation cut an unsettled handler'
            calls.append('cancel')
            self.cancelled.add(run)

        def close(self):
            assert all(not r.active_handlers for r in relays), 'ledger closed before handler exit'
            calls.append('ledger-close')

    class Relay:
        def __init__(self, _path, **kwargs):
            self.binding = kwargs['binding']
            self.ledger = kwargs['ledger']
            self.active_handlers = initial_handlers
            self.stopped_event = threading.Event()
            relays.append(self)

        def serve_forever(self):
            self.stopped_event.wait(5)

        def shutdown(self):
            calls.append('shutdown')
            self.stopped_event.set()

        def server_close(self):
            calls.append('server-close')

        def wait_handlers(self, deadline):
            assert deadline == self.binding.deadline
            calls.append('handler-wait')
            self.active_handlers = 0
            return True

    class Process:
        returncode = 0

        def __init__(self, *_args, **_kwargs):
            if launch_error:
                raise launch_error
            if stop:
                relays[0].ledger.events.append({'event': 'local_rejection'})
                relays[0].ledger.admission_closed_reason = 'estimated_fee_reservation_exhausted'

        def poll(self):
            if not stop or (slot / 'stop-requested.json').exists():
                return 0
            return None

        def wait(self, **_kwargs):
            calls.append('worker-wait')
            return 0

    def save(path, value):
        if path.name == 'budget-final.json' and summary_error:
            raise summary_error
        path.write_text(json.dumps(value))

    monkeypatch.setattr(model_relay, 'RelayLedger', Ledger)
    monkeypatch.setattr(model_relay, 'RelayServer', Relay)
    monkeypatch.setattr(module.subprocess, 'Popen', Process)
    monkeypatch.setattr(module, 'save', save)
    return module, calls, relays


def test_budget_stop_waits_handlers_before_cancel_or_ledger_close(monkeypatch, tmp_path):
    module, calls, relays = controller_fixture(monkeypatch, tmp_path, stop=True)
    module.run()
    assert relays[0].ledger.policy is True
    assert calls.index('handler-wait') < calls.index('cancel') < calls.index('ledger-close')


def test_launch_failure_still_drains_relay_and_preserves_primary(monkeypatch, tmp_path):
    primary = ValueError('worker launch failed')
    module, calls, _ = controller_fixture(monkeypatch, tmp_path, launch_error=primary)
    with pytest.raises(ValueError, match='worker launch failed') as caught:
        module.run()
    assert caught.value is primary
    assert 'shutdown' in calls and 'server-close' in calls and 'handler-wait' in calls
    assert calls[-1] == 'ledger-close'


def test_budget_summary_write_failure_does_not_skip_ledger_close(monkeypatch, tmp_path):
    failure = OSError('cannot persist summary')
    module, calls, _ = controller_fixture(monkeypatch, tmp_path, summary_error=failure)
    with pytest.raises(OSError, match='cannot persist summary') as caught:
        module.run()
    assert caught.value is failure
    assert calls[-1] == 'ledger-close'


def test_listener_start_failure_closes_socket_without_shutdown_deadlock(monkeypatch, tmp_path):
    failure = RuntimeError('listener cannot start')
    module, calls, _ = controller_fixture(monkeypatch, tmp_path, initial_handlers=0)

    def fail_start(_self):
        raise failure

    monkeypatch.setattr(module.threading.Thread, 'start', fail_start)
    with pytest.raises(RuntimeError, match='listener cannot start') as caught:
        module.run()
    assert caught.value is failure
    assert 'shutdown' not in calls
    assert 'server-close' in calls and calls[-1] == 'ledger-close'


@pytest.mark.parametrize('relative', [
    'docs/evidence/workspace-paid-full-2026-11-06/run_full.py',
    'docs/evidence/workspace-paid-resume-2026-11-06/run_resume.py',
])
@pytest.mark.parametrize('fault', ['journal', 'cleanup'])
def test_new_budget_refuses_unconfirmed_previous_segment(monkeypatch, tmp_path, relative, fault):
    module = load_script(monkeypatch, relative, 'test_accounting_previous_owner')
    parent = tmp_path / 'prior'
    parent.mkdir()
    budget = {'requests': 1, 'unsettled': [], 'failed_contracts': {}, 'ambiguous': [],
              'journal_fault': fault == 'journal'}
    (parent / 'budget-final.json').write_text(json.dumps(budget))
    admitted = {'event': 'admitted', 'attempt_id': 'old-call', 'estimated_cost_reserved_cny': 2.1}
    settled = {'event': 'settled', 'attempt_id': 'old-call', 'usage_valid': True,
               'contract_failed': False, 'ambiguous': False, 'estimated_cost_reported_cny': 0.1}
    (parent / 'ledger.jsonl').write_text(json.dumps(admitted)+'\n'+json.dumps(settled)+'\n')
    if fault == 'cleanup':
        (parent / 'cleanup-failures.json').write_text('[{"stage":"ledger-close","type":"OSError"}]')
    monkeypatch.setattr(module, 'PARENTS', (parent,))
    with pytest.raises(RuntimeError, match='Previous evaluation'):
        module.accounting()


def test_real_budget_shutdown_collects_the_accepted_http_usage(monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from http.server import ThreadingHTTPServer
    import time

    import httpx

    from nz_coder.evaluation.model_relay import RelayBinding, RelayLedger, RelayLimits, RelayServer
    from tests.evaluation.test_model_relay import FakeService, fake_count

    module = load_script(monkeypatch, 'docs/evidence/workspace-end-to-end-2026-11-05/run_projects.py', 'test_real_stop_base')
    fake = FakeService(mode='hold')
    upstream = ThreadingHTTPServer(('127.0.0.1', 0), fake.handler())
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    ledger = RelayLedger(tmp_path / 'ledger.jsonl', RelayLimits(requests=1), stop_on_rejection=True)
    binding = RelayBinding('local', 'unit', 'run', 'worker', 1, 'local-model', 64000, time.monotonic()+8)
    socket = tmp_path / 'relay.sock'
    server = RelayServer(socket, ledger=ledger, binding=binding,
        upstream=f'http://127.0.0.1:{upstream.server_port}/v1/chat/completions', counter=fake_count)
    relay_thread = threading.Thread(target=server.serve_forever, daemon=True)
    relay_thread.start()
    entered_wait = threading.Event()
    original_wait = server.wait_handlers

    def observed_wait(deadline):
        entered_wait.set()
        return original_wait(deadline)

    monkeypatch.setattr(server, 'wait_handlers', observed_wait)

    def post(nonce):
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(socket)), trust_env=False, timeout=6) as client:
            return client.post('http://localhost/v1/chat/completions', headers={'X-NZ-Call-Id':nonce},
                json={'model':'local-model', 'messages':[{'role':'user','content':'local'}],
                      'max_tokens':64000, 'stream':True})

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            accepted = pool.submit(post, 'accepted')
            assert fake.started.wait(2)
            assert post('rejected').status_code == 429
            cleanup = pool.submit(module._finish_attempt, ledger, 'run', server, relay_thread, None, hard_stop=False)
            assert entered_wait.wait(2)
            assert not cleanup.done()
            fake.release.set()
            assert accepted.result(5).status_code == 200
            assert cleanup.result(5) == []
        assert len(fake.requests) == 1
        row = next(iter(ledger.attempts.values()))
        assert row['status'] == 'complete' and row['usage_valid'] is True
        assert row['reservation_retained'] is False
        assert server.active_handlers == 0 and not relay_thread.is_alive()
    finally:
        fake.release.set()
        if relay_thread.is_alive():
            server.shutdown()
        server.server_close()
        relay_thread.join(2)
        assert server.wait_handlers(time.monotonic()+2)
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(2)
        ledger.close()


def test_worker_does_not_report_success_with_listener_still_alive(monkeypatch, tmp_path):
    module, slot, calls = worker_fixture(monkeypatch, tmp_path)

    class UnsettledThread:
        ident = 1

        def __init__(self, **_kwargs):
            pass

        def start(self):
            pass

        def join(self, timeout):
            calls.append('thread-join')

        def is_alive(self):
            return True

    monkeypatch.setattr(module.threading, 'Thread', UnsettledThread)
    with pytest.raises(RuntimeError, match='HTTP service thread'):
        module.main(slot, False)
    receipt = json.loads((slot / 'cleanup.json').read_text())
    assert receipt['service_thread_alive'] is True and receipt['failures']


def test_worker_thread_start_failure_closes_unserved_http_socket(monkeypatch, tmp_path):
    module, slot, calls = worker_fixture(monkeypatch, tmp_path)
    failure = RuntimeError('no thread capacity')

    def start_failed(_self):
        raise failure

    monkeypatch.setattr(module.threading.Thread, 'start', start_failed)
    with pytest.raises(RuntimeError, match='no thread capacity') as caught:
        module.main(slot, False)
    assert caught.value is failure
    assert calls == ['close-unserved']


def test_keyboard_interrupt_during_budget_drain_hard_cancels_before_retry(monkeypatch):
    import time

    module = load_script(monkeypatch, 'docs/evidence/workspace-end-to-end-2026-11-05/run_projects.py', 'test_stop_interrupt')
    calls = []
    interruption = KeyboardInterrupt()
    counter = 0

    def wait_handlers(_deadline):
        nonlocal counter
        counter += 1
        calls.append('wait')
        if counter == 1:
            raise interruption
        return True

    server = SimpleNamespace(binding=SimpleNamespace(deadline=time.monotonic()+5),
        shutdown=lambda: None, server_close=lambda: None, wait_handlers=wait_handlers)
    thread = threading.Thread(target=lambda: None)
    thread.start()
    thread.join()
    ledger = SimpleNamespace(cancel=lambda _run: calls.append('cancel'))
    failures = module._finish_attempt(ledger, 'run', server, thread, None, hard_stop=False)
    assert failures[0][1] is interruption
    assert calls[:3] == ['wait', 'cancel', 'wait']


@pytest.mark.parametrize('stage', ['worker', 'shutdown', 'listener-close', 'listener-join'])
def test_interrupt_in_each_cleanup_step_cancels_before_handler_drain(monkeypatch, stage):
    import time

    module = load_script(monkeypatch, 'docs/evidence/workspace-end-to-end-2026-11-05/run_projects.py', 'test_stop_early_interrupt')
    calls = []
    interruption = KeyboardInterrupt()

    def operation(name):
        calls.append(name)
        if name == stage:
            raise interruption

    def wait_handlers(_deadline):
        assert 'cancel' in calls, 'cleanup interruption did not hard-cancel the run'
        calls.append('handlers')
        return True

    monkeypatch.setattr(module, '_stop_worker', lambda _process: operation('worker'))
    server = SimpleNamespace(binding=SimpleNamespace(deadline=time.monotonic()+5),
        shutdown=lambda: operation('shutdown'), server_close=lambda: operation('listener-close'),
        wait_handlers=wait_handlers)
    thread = SimpleNamespace(ident=1, join=lambda **_kwargs: operation('listener-join'), is_alive=lambda: False)
    ledger = SimpleNamespace(cancel=lambda _run: calls.append('cancel'))
    failures = module._finish_attempt(ledger, 'run', server, thread, None, hard_stop=False)
    assert failures[0][1] is interruption
    assert calls.index('cancel') < calls.index('handlers')
    assert 'shutdown' in calls and 'listener-close' in calls and 'listener-join' in calls


@pytest.mark.parametrize('hard_reason', ['run_cancelled', 'disk_space_stop', 'manual_stop'])
def test_budget_soft_stop_does_not_hide_later_hard_stop(monkeypatch, tmp_path, hard_reason):
    module, calls, relays = controller_fixture(monkeypatch, tmp_path, stop=True, initial_handlers=0)
    slot = tmp_path / 'attempt'
    escalated = False

    class Process:
        returncode = None
        polls = 0

        def __init__(self, *_args, **_kwargs):
            relays[0].ledger.admission_closed_reason = 'estimated_fee_reservation_exhausted'

        def poll(self):
            self.polls += 1
            assert self.polls < 5, 'worker did not stop after the hard boundary'
            return self.returncode

        def wait(self, **_kwargs):
            return self.returncode

    def stop_worker(process):
        if process is not None:
            calls.append('worker-stopped')
            process.returncode = -15

    def publish_hard_stop(_seconds):
        nonlocal escalated
        escalated = True
        if hard_reason == 'manual_stop':
            (slot / 'stop-requested.json').write_text(json.dumps({'reason': 'manual_stop', 'hard_cancel': True}))

    def stopped(_ledger, _binding):
        calls.append('stopped-check')
        return 'run_cancelled' if escalated and hard_reason == 'run_cancelled' else ''

    monkeypatch.setattr(module.subprocess, 'Popen', Process)
    monkeypatch.setattr(module, '_stop_worker', stop_worker)
    monkeypatch.setattr(module.time, 'sleep', publish_hard_stop)
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda _path: SimpleNamespace(
        free=0 if escalated and hard_reason == 'disk_space_stop' else 1024**3))
    from nz_coder.evaluation import model_relay
    monkeypatch.setattr(model_relay.RelayLedger, 'stopped', stopped)
    module.run()
    assert escalated
    assert calls.index('worker-stopped') < calls.index('handler-wait')
    marker = json.loads((slot / 'stop-requested.json').read_text())
    assert marker == {'reason': hard_reason, 'hard_cancel': True}


def test_cleanup_report_interrupt_does_not_replace_primary_exception(monkeypatch, tmp_path):
    primary = ValueError('worker launch failed')
    module, _calls, relays = controller_fixture(monkeypatch, tmp_path, launch_error=primary, initial_handlers=0)
    from nz_coder.evaluation import model_relay
    shutdown = model_relay.RelayServer.shutdown

    def failed_shutdown(server):
        shutdown(server)
        raise RuntimeError('shutdown report needed')

    save = module.save

    def interrupted_report(path, value):
        if path.name == 'cleanup-failures.json':
            raise KeyboardInterrupt()
        save(path, value)

    monkeypatch.setattr(model_relay.RelayServer, 'shutdown', failed_shutdown)
    monkeypatch.setattr(module, 'save', interrupted_report)
    with pytest.raises(ValueError) as caught:
        module.run()
    assert caught.value is primary
    assert not relays[0].active_handlers


@pytest.mark.parametrize('relative,finalizer', [
    ('docs/evidence/workspace-paid-full-2026-11-06/run_full.py', 'summarize_budget'),
    ('docs/evidence/workspace-paid-resume-2026-11-06/run_resume.py', 'summarize'),
])
def test_fallback_summary_interrupt_preserves_primary(monkeypatch, tmp_path, relative, finalizer):
    module = load_script(monkeypatch, relative, 'test_stop_summary_interrupt')
    module.OUT = tmp_path
    prior = {'prior_actual_cny': '2.55'}
    (tmp_path / 'cumulative-accounting.json').write_text(json.dumps(prior))
    primary = ValueError('primary provider failure')

    def execute():
        raise primary

    def summarize(_prior):
        raise OSError('summary cannot be written')

    def interrupted_save(_path, _value):
        raise KeyboardInterrupt()

    monkeypatch.setattr(module, 'accounting', lambda: prior)
    monkeypatch.setattr(module, 'driver', lambda: SimpleNamespace(run=execute))
    monkeypatch.setattr(module, finalizer, summarize)
    monkeypatch.setattr(module, 'save', interrupted_save)
    with pytest.raises(ValueError) as caught:
        module.run()
    assert caught.value is primary
