"""Provider deadlines exclude time spent in the local stream consumer."""
from types import SimpleNamespace
import threading

import pytest

from nz_coder.runtime.model_gateway import stream


@pytest.fixture
def runtime():
    from tests.runtime.model_gateway.test_buffered_gateway import _runtime
    value = _runtime(object())
    yield value
    value.close()


@pytest.mark.parametrize('idle,hard', [(1, 1000), (0, 1)])
def test_consumer_time_is_not_provider_timeout(monkeypatch, idle, hard, runtime):
    clock = [0.0]
    monkeypatch.setattr(stream, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    values = stream.iter_stream_with_timeouts(
        lambda: iter(['tool_calls', 'trailing_usage']), runtime=runtime, idle_timeout_seconds=idle, hard_timeout_seconds=hard)
    assert next(values) == 'tool_calls'
    clock[0] += 100  # deterministic local tool wait, no Provider wait
    assert list(values) == ['trailing_usage']


@pytest.mark.parametrize('idle,hard', [(0.02, 1), (0, 0.02)])
def test_real_provider_wait_still_times_out(idle, hard, runtime):
    release = threading.Event()

    def stalled():
        release.wait(1)
        yield 'late'

    try:
        with pytest.raises(TimeoutError):
            list(stream.iter_stream_with_timeouts(
                stalled, runtime=runtime, idle_timeout_seconds=idle, hard_timeout_seconds=hard))
    finally:
        release.set()


def test_cancellation_after_consumer_wait_is_distinct(monkeypatch, runtime):
    clock = [0.0]
    cancelled = [False]
    monkeypatch.setattr(stream, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    values = stream.iter_stream_with_timeouts(
        lambda: iter(['tool_calls', 'late']), runtime=runtime, idle_timeout_seconds=1, hard_timeout_seconds=2,
        cancelled=lambda: cancelled[0])
    assert next(values) == 'tool_calls'
    clock[0] += 100
    cancelled[0] = True
    assert list(values) == []


def test_stream_open_deadline_returns_before_blocked_open_releases():
    from dataclasses import replace
    from tests.runtime.model_gateway.test_streaming_gateway import _gateway, _call
    entered, release, closed, finished = (threading.Event() for _ in range(4))
    result = []
    class Raw:
        def __iter__(self): return self
        def __next__(self): raise StopIteration
        def close(self): closed.set()
    gateway, provider = _gateway([], max_retries=0)
    def open_stream(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return Raw()
    provider.create_completion = open_stream
    def call():
        result.append(gateway.complete_stream_sync(replace(_call(), timeout_seconds=.05)))
        finished.set()
    caller = threading.Thread(target=call)
    caller.start()
    try:
        assert entered.wait(2)
        assert finished.wait(.5), 'logical deadline must include stream open'
        assert result[0].status.value == 'aborted'
        assert gateway.runtime.inflight_status()['inflight_calls'] == 1
    finally:
        release.set()
        caller.join(3)
        assert closed.wait(3)
        gateway.runtime.close()


def test_stream_next_timeout_retains_inflight_and_blocks_new_request():
    from dataclasses import replace
    from nz_coder.providers.normalized import chunk
    from tests.runtime.model_gateway.test_streaming_gateway import _gateway, _call
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    class Raw:
        def __init__(self): self.first = True
        def __iter__(self): return self
        def __next__(self):
            if self.first:
                self.first = False
                return chunk(content='partial')
            entered.set()
            assert release.wait(3)
            raise StopIteration
        def close(self): closed.set()
    gateway, provider = _gateway([Raw()], max_retries=3, wait=lambda _seconds: None)
    try:
        result = gateway.complete_stream_sync(replace(_call(), timeout_seconds=.05))
        assert entered.is_set()
        assert result.status.value == 'aborted'
        assert result.public_error.code == 'model_timeout' and result.attempts == 1
        assert gateway.runtime.inflight_status()['inflight_calls'] == 1
        assert gateway.runtime.inflight_status()['tainted']
        blocked = gateway.complete_stream_sync(replace(_call(), timeout_seconds=.05))
        assert blocked.public_error.code == 'model_request_unsettled'
        assert len(provider.requests) == 1
    finally:
        release.set()
        assert closed.wait(3)
        gateway.runtime.close()


def test_owned_stream_close_failure_waits_for_explicit_runtime_retry():
    import time
    from nz_coder.providers.normalized import chunk
    from tests.runtime.model_gateway.test_streaming_gateway import _gateway, _call
    attempts, closed = [], threading.Event()
    class Raw:
        def __iter__(self): return iter([chunk(content='ok', finish_reason='stop')])
        def close(self):
            attempts.append(threading.get_ident())
            if len(attempts) == 1:
                raise OSError('first close failed')
            closed.set()
    gateway, provider = _gateway([Raw()], max_retries=0)
    try:
        result = gateway.complete_stream_sync(_call())
        assert result.status.value == 'completed'
        assert len(attempts) == 1
        assert gateway.runtime.inflight_status()['inflight_calls'] == 1
        time.sleep(.05)
        assert len(attempts) == 1
        gateway.complete_stream_sync(_call())
        assert len(provider.requests) == 1
        gateway.runtime.close()
        assert closed.is_set()
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        assert gateway.runtime.inflight_status()['inflight_calls'] == 0
    finally:
        gateway.runtime.close()


def test_dead_worker_without_cleanup_proof_keeps_inflight_record():
    from tests.runtime.model_gateway.test_buffered_gateway import _runtime
    runtime = _runtime(object())
    cleanup = threading.Event()
    worker = threading.Thread(target=lambda: None)
    runtime.begin_inflight(worker, lambda: None, cleanup_complete=cleanup.is_set)
    worker.start()
    worker.join(3)
    assert not worker.is_alive()
    assert runtime.inflight_status() == {'worker_still_running': 0, 'inflight_calls': 1, 'tainted': True}
    with pytest.raises(RuntimeError):
        runtime.close()
    cleanup.set()
    runtime.close()
    assert runtime.inflight_status()['inflight_calls'] == 0


def test_stream_worker_started_then_start_raises_preserves_owner(monkeypatch):
    from dataclasses import replace
    from tests.runtime.model_gateway.test_streaming_gateway import _gateway, _call
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    actual_start = threading.Thread.start
    def start(thread):
        result = actual_start(thread)
        if thread.name == 'nz-provider-stream':
            assert entered.wait(2)
            raise RuntimeError('start returned after worker started')
        return result
    class Raw:
        def __iter__(self): return self
        def __next__(self): raise StopIteration
        def close(self): closed.set()
    gateway, provider = _gateway([], max_retries=0)
    def open_stream(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return Raw()
    provider.create_completion = open_stream
    monkeypatch.setattr(threading.Thread, 'start', start)
    try:
        gateway.complete_stream_sync(replace(_call(), timeout_seconds=.05))
        assert gateway.runtime.inflight_status()['inflight_calls'] == 1
    finally:
        release.set()
        assert closed.wait(3)
        gateway.runtime.close()


def test_untrusted_stream_cancel_hook_cannot_block_logical_timeout():
    from dataclasses import replace
    from tests.runtime.model_gateway.test_streaming_gateway import _gateway, _call
    next_entered, release_next, hook_entered, release_hook = (threading.Event() for _ in range(4))
    finished = threading.Event()
    class Raw:
        def __iter__(self): return self
        def __next__(self):
            next_entered.set()
            assert release_next.wait(3)
            raise StopIteration
        def request_cancel(self):
            hook_entered.set()
            release_hook.wait(3)
        def close(self): pass
    gateway, _ = _gateway([Raw()], max_retries=0)
    caller = threading.Thread(target=lambda: (gateway.complete_stream_sync(replace(_call(), timeout_seconds=.05)), finished.set()))
    caller.start()
    try:
        assert next_entered.wait(2)
        assert finished.wait(.5)
        assert not hook_entered.is_set()
    finally:
        release_hook.set()
        release_next.set()
        caller.join(3)
        gateway.runtime.close()


def test_provider_owned_stream_close_failure_uses_original_worker_retry():
    from nz_coder.providers.http import OwnedStream
    from nz_coder.providers.normalized import chunk
    from tests.runtime.model_gateway.test_streaming_gateway import _gateway, _call
    calls = []
    def close_owner():
        calls.append(threading.get_ident())
        if len(calls) == 1:
            raise OSError('transport close failed')
    raw = OwnedStream(iter([chunk(content='done', finish_reason='stop')]), close_owner=close_owner)
    gateway, _ = _gateway([raw], max_retries=0)
    try:
        assert gateway.complete_stream_sync(_call()).status.value == 'completed'
        assert len(calls) == 1
        assert gateway.runtime.inflight_status()['inflight_calls'] == 1
        gateway.runtime.close()
        assert len(calls) == 2 and calls[0] == calls[1]
        assert gateway.runtime.inflight_status()['inflight_calls'] == 0
    finally:
        gateway.runtime.close()


def test_unstarted_worker_does_not_open_stream(monkeypatch):
    from tests.runtime.model_gateway.test_buffered_gateway import _runtime
    opened = []
    def open_stream():
        opened.append(True)
        return iter([])
    runtime = _runtime(object())
    start = threading.Thread.start
    def fail(thread):
        if thread.name == 'nz-provider-stream':
            raise RuntimeError('worker did not start')
        return start(thread)
    monkeypatch.setattr(threading.Thread, 'start', fail)
    try:
        with pytest.raises(RuntimeError, match='worker did not start'):
            list(stream.iter_stream_with_timeouts(open_stream, runtime=runtime, idle_timeout_seconds=1, hard_timeout_seconds=1))
        assert opened == []
        assert runtime.inflight_status()['inflight_calls'] == 0
    finally:
        runtime.close()


def test_timeout_during_first_close_does_not_retry_until_runtime_cleanup():
    from dataclasses import replace
    from nz_coder.providers.http import OwnedStream
    from nz_coder.providers.normalized import chunk
    from tests.runtime.model_gateway.test_streaming_gateway import _gateway, _call
    closing, release, second, finished = (threading.Event() for _ in range(4))
    attempts, results = [], []
    def close_owner():
        attempts.append(threading.get_ident())
        if len(attempts) == 1:
            closing.set()
            assert release.wait(3)
            raise OSError('first close failed after timeout')
        second.set()
    raw = OwnedStream(iter([chunk(content='done', finish_reason='stop')]), close_owner=close_owner)
    gateway, _ = _gateway([raw], max_retries=0)
    def call():
        results.append(gateway.complete_stream_sync(replace(_call(), timeout_seconds=.1)))
        finished.set()
    caller = threading.Thread(target=call)
    caller.start()
    try:
        assert closing.wait(2)
        assert finished.wait(1)
        assert results[0].status.value == 'aborted'
        release.set()
        assert not second.wait(.3)
        assert len(attempts) == 1
        assert gateway.runtime.inflight_status()['inflight_calls'] == 1
        gateway.runtime.close()
        assert second.is_set()
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        assert gateway.runtime.inflight_status()['inflight_calls'] == 0
    finally:
        release.set()
        caller.join(3)
        gateway.runtime.close()
        assert not caller.is_alive()
