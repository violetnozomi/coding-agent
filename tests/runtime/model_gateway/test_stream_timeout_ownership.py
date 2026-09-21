"""Provider deadlines exclude time spent in the local stream consumer."""
from types import SimpleNamespace
import threading

import pytest

from nz_coder.runtime.model_gateway import stream


@pytest.mark.parametrize('idle,hard', [(1, 1000), (0, 1)])
def test_consumer_time_is_not_provider_timeout(monkeypatch, idle, hard):
    clock = [0.0]
    monkeypatch.setattr(stream, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    values = stream.iter_stream_with_timeouts(
        iter(['tool_calls', 'trailing_usage']), idle_timeout_seconds=idle, hard_timeout_seconds=hard)
    assert next(values) == 'tool_calls'
    clock[0] += 100  # deterministic local tool wait, no Provider wait
    assert list(values) == ['trailing_usage']


@pytest.mark.parametrize('idle,hard', [(0.02, 1), (0, 0.02)])
def test_real_provider_wait_still_times_out(idle, hard):
    release = threading.Event()

    def stalled():
        release.wait(1)
        yield 'late'

    try:
        with pytest.raises(TimeoutError):
            list(stream.iter_stream_with_timeouts(
                stalled(), idle_timeout_seconds=idle, hard_timeout_seconds=hard))
    finally:
        release.set()


def test_cancellation_after_consumer_wait_is_distinct(monkeypatch):
    clock = [0.0]
    cancelled = [False]
    monkeypatch.setattr(stream, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    values = stream.iter_stream_with_timeouts(
        iter(['tool_calls', 'late']), idle_timeout_seconds=1, hard_timeout_seconds=2,
        cancelled=lambda: cancelled[0])
    assert next(values) == 'tool_calls'
    clock[0] += 100
    cancelled[0] = True
    assert list(values) == []
