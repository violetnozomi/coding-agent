"""Deterministic clocks distinguish consumer pauses from Provider waiting."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from nz_coder.runtime.model_gateway import stream as streams


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    # Replace only this module's clock, not asyncio/threading's real clocks.
    monkeypatch.setattr(streams, "time", SimpleNamespace(monotonic=clock.monotonic))
    return clock


@pytest.mark.parametrize("pause", [0, 59, 60, 343.000612])
@pytest.mark.parametrize("tail", [[], ["usage"]])
def test_local_pause_preserves_tail_and_eof(clock, pause, tail):
    stream = streams.iter_stream_with_timeouts(
        iter(["finish", *tail]), idle_timeout_seconds=60, hard_timeout_seconds=600,
    )
    assert next(stream) == "finish"
    clock.advance(pause)
    assert list(stream) == tail


def test_multiple_pauses_do_not_double_charge_idle(clock):
    stream = streams.iter_stream_with_timeouts(
        iter(range(4)), idle_timeout_seconds=60, hard_timeout_seconds=600,
    )
    for expected in range(4):
        assert next(stream) == expected
        clock.advance(100)
    assert list(stream) == []


@pytest.mark.parametrize("pause", [0, 100])
def test_absolute_hard_expires_even_with_progress(clock, pause):
    def source():
        for index in range(10):
            clock.advance(101)
            yield index

    stream = streams.iter_stream_with_timeouts(
        source(), idle_timeout_seconds=0, hard_timeout_seconds=600,
    )
    with pytest.raises(TimeoutError, match="hard"):
        for _ in stream:
            clock.advance(pause)
    assert clock.now < 1000


def test_real_provider_wait_still_expires_idle(clock):
    waiting = threading.Event()
    released = threading.Event()
    finished = threading.Event()

    class Source:
        def __iter__(self):
            try:
                yield "first"
                clock.advance(60)
                waiting.set()
                assert released.wait(2)
            finally:
                finished.set()

        def close(self):
            released.set()

    stream = streams.iter_stream_with_timeouts(
        Source(), idle_timeout_seconds=60, hard_timeout_seconds=600,
    )
    assert next(stream) == "first"
    with pytest.raises(TimeoutError, match="idle"):
        next(stream)
    assert waiting.is_set() and finished.wait(2)


@pytest.mark.parametrize("action", ["close", "throw", "cancel"])
def test_consumer_exit_releases_reader_and_closes(clock, action):
    closed = threading.Event()
    cancelled = threading.Event()

    class Source:
        def __iter__(self):
            yield "first"

        def close(self):
            closed.set()

    stream = streams.iter_stream_with_timeouts(
        Source(), idle_timeout_seconds=60, hard_timeout_seconds=600,
        cancelled=cancelled.is_set,
    )
    assert next(stream) == "first"
    clock.advance(343)
    if action == "throw":
        with pytest.raises(ValueError):
            stream.throw(ValueError("consumer failure"))
    elif action == "cancel":
        cancelled.set()
        assert list(stream) == []
    else:
        stream.close()
    assert closed.wait(2)
