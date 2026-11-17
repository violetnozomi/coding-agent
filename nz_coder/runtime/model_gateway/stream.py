"""One registered owner for opening, consuming and closing Provider streams."""
from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Iterator
from nz_coder.providers.http import OwnedStream


class StreamExecution:
    def __init__(self, open_stream: Callable[[], Iterator[object]], *, runtime,
                 idle_timeout_seconds: float, hard_timeout_seconds: float,
                 cancelled: Callable[[], bool] | None = None):
        self._open = open_stream
        self._runtime = runtime
        self._idle = max(0.0, float(idle_timeout_seconds))
        self._hard = max(0.001, float(hard_timeout_seconds))
        self._cancelled = cancelled
        self._events = queue.Queue(maxsize=256)
        self._stopped = threading.Event()
        self._consumed = threading.Event()
        self._close_retry = threading.Event()
        self._cleanup_complete = threading.Event()
        self._stream = None
        self._worker = threading.Thread(target=self._run, name='nz-provider-stream', daemon=True)
        self._call_id = 0

    def cleanup_complete(self) -> bool:
        return self._cleanup_complete.is_set()

    def request_stop(self) -> None:
        self._stopped.set()
        self._consumed.set()
        stream = self._stream
        if type(stream) is OwnedStream:
            stream.request_cancel()

    def request_cleanup(self) -> None:
        self.request_stop()
        self._close_retry.set()

    def _publish(self, kind, value):
        while not self._stopped.is_set():
            try:
                self._events.put((kind, value), timeout=.05)
                return True
            except queue.Full:
                pass
        return False

    def _run(self):
        outcome = ('done', None)
        try:
            self._stream = self._open()
            if self._stopped.is_set():
                if type(self._stream) is OwnedStream:
                    self._stream.request_cancel()
            else:
                for item in self._stream:
                    self._consumed.clear()
                    if not self._publish('item', item):
                        break
                    while not self._stopped.is_set() and not self._consumed.wait(.05):
                        pass
                    if self._stopped.is_set():
                        break
        except BaseException as exc:
            outcome = ('error', exc)
        finally:
            close = getattr(self._stream, 'close', None)
            reported = False
            while callable(close):
                self._close_retry.clear()
                try:
                    close()
                    break
                except BaseException:
                    if not reported:
                        self._publish(*outcome)
                        reported = True
                    self._close_retry.wait()
            self._cleanup_complete.set()
            if not reported:
                self._publish(*outcome)
            if self._call_id:
                self._runtime.finish_inflight(self._call_id)

    def __iter__(self):
        self._call_id = self._runtime.begin_inflight(
            self._worker, self.request_cleanup, cleanup_complete=self.cleanup_complete)
        started = last_activity = time.monotonic()
        try:
            self._worker.start()
        except BaseException:
            if self._worker.ident is None:
                self._cleanup_complete.set()
                self._runtime.finish_inflight(self._call_id)
            else:
                self.request_stop()
            raise
        finished = False
        try:
            while True:
                now = time.monotonic()
                if self._cancelled is not None and self._cancelled():
                    return
                if now - started >= self._hard:
                    raise TimeoutError(f'Provider hard timeout after {self._hard:g}s')
                if self._idle and now - last_activity >= self._idle:
                    raise TimeoutError(f'Stream stalled or delayed response ({self._idle:g}s idle)')
                wait_for = min(.05, max(.001, self._hard - (now - started)))
                if self._idle:
                    wait_for = min(wait_for, max(.001, self._idle - (now - last_activity)))
                try:
                    kind, value = self._events.get(timeout=wait_for)
                except queue.Empty:
                    continue
                if kind == 'item':
                    last_activity = time.monotonic()
                    try:
                        yield value
                    finally:
                        consumer_seconds = time.monotonic() - last_activity
                        started += consumer_seconds
                        last_activity += consumer_seconds
                        self._consumed.set()
                elif kind == 'error':
                    finished = True
                    if self._cleanup_complete.is_set():
                        self._worker.join()
                    raise value
                else:
                    finished = True
                    if self._cleanup_complete.is_set():
                        self._worker.join()
                    return
        finally:
            if not finished:
                self.request_stop()


def iter_stream_with_timeouts(open_stream, *, runtime, idle_timeout_seconds: float,
                              hard_timeout_seconds: float,
                              cancelled: Callable[[], bool] | None = None) -> Iterator[object]:
    yield from StreamExecution(open_stream, runtime=runtime,
        idle_timeout_seconds=idle_timeout_seconds,
        hard_timeout_seconds=hard_timeout_seconds, cancelled=cancelled)
