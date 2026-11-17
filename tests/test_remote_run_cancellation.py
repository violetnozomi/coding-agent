"""Remote cancellation follows user intent and retains real worker ownership."""
from __future__ import annotations

import asyncio
import io
import queue
import signal
import threading
from types import SimpleNamespace

import pytest
from rich.console import Console

from nz_coder.interface import remote
from nz_coder.interface.selector import FuzzySelector


def event(kind, **properties):
    return {"type": kind, "properties": properties}


class Backend:
    session_id = "cancel-session"

    def __init__(self, timeout=1):
        self.client = SimpleNamespace(timeout=timeout)
        self.items = queue.Queue()
        self.entered = threading.Event()
        self.closed = threading.Event()
        self.abort_entered = threading.Event()
        self.abort_release = threading.Event()
        self.abort_release.set()
        self.abort_done = threading.Event()
        self.abort_result = True
        self.aborts = 0
        self.snapshots = 0
        self.streams = 0
        self.replies = []
        self.settled = False

    def attach_snapshot(self):
        self.snapshots += 1
        return {"cursor": {}, "settled": self.settled,
                "session": {"running": not self.settled,
                            "status": "completed" if self.settled else "running"},
                "pending": {}, "events": []}

    def abort(self):
        self.aborts += 1
        self.abort_entered.set()
        assert self.abort_release.wait(3), "test did not release abort"
        self.abort_done.set()
        return self.abort_result

    def events(self, **kwargs):
        self.streams += 1
        try:
            self.entered.set()
            while True:
                payload = self.items.get(timeout=3)
                if payload is None:
                    return
                yield payload
        finally:
            self.closed.set()

    def finish(self, status="completed"):
        self.settled = True
        self.items.put(event("session.run.settled", status=status))

    def reply_permission(self, *args):
        self.replies.append(args)
        return True

    def reject_question(self, *args):
        self.replies.append(args)
        return True


@pytest.fixture
def ui(monkeypatch):
    state = SimpleNamespace(handler=None, selectors=0, key=None, selected=asyncio.Event())
    original = object()
    state.original = original
    monkeypatch.setattr(remote, "signal", signal, raising=False)
    monkeypatch.setattr(remote.signal, "getsignal", lambda sig: original, raising=False)
    monkeypatch.setattr(remote.signal, "signal", lambda sig, handler: setattr(state, "handler", handler), raising=False)

    class Input:
        def __init__(self, **kwargs):
            pass

        async def close_async(self):
            pass

        async def select_async(self, *, require_interaction=False, **kwargs):
            state.selectors += 1
            state.selected.set()
            if state.key is None:
                await asyncio.Event().wait()
            from prompt_toolkit.keys import Keys
            from prompt_toolkit.input import create_pipe_input
            from prompt_toolkit.output import DummyOutput
            with create_pipe_input() as pipe:
                kwargs.pop("detail", None)
                selector = FuzzySelector(**kwargs)
                app = selector.application(input=pipe, output=DummyOutput())
                key = {"c-c": Keys.ControlC, "<sigint>": Keys.SIGINT,
                       "escape": Keys.Escape}[state.key]
                bindings = [binding for binding in app.key_bindings.bindings
                            if binding.keys == (key,)]
                results = []
                bindings[-1].handler(SimpleNamespace(app=SimpleNamespace(
                    exit=lambda **value: results.append(value.get("result")))))
                return results[0]

    monkeypatch.setattr(remote, "TerminalInput", Input)
    state.output = io.StringIO()
    state.console = Console(file=state.output, force_terminal=False)
    return state


async def wait_for(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.005)


async def start(backend, ui):
    task = asyncio.create_task(remote._follow_run(backend, ui.console))
    await wait_for(backend.entered.is_set)
    return task


@pytest.mark.parametrize("abort_result", [True, False])
def test_explicit_sigint_aborts_once_and_waits_for_settled(ui, abort_result):
    async def scenario():
        backend = Backend()
        backend.abort_result = abort_result
        task = await start(backend, ui)
        try:
            assert callable(ui.handler), "follow must own SIGINT during the run"
            ui.handler(signal.SIGINT, None)
            ui.handler(signal.SIGINT, None)
            await wait_for(backend.abort_done.is_set)
            backend.items.put(event("session.run.cancelled", status="cancelled"))
            await asyncio.sleep(.04)
            assert not task.done(), "underlying cancelled is not manager settled"
            assert backend.aborts == 1
        finally:
            backend.finish()
            await task
        assert backend.closed.is_set()
        assert ui.handler is ui.original
    asyncio.run(scenario())


@pytest.mark.parametrize("key", ["c-c", "<sigint>", "escape"])
@pytest.mark.parametrize("kind", ["permission", "question"])
def test_remote_selector_distinguishes_cancel_from_escape(ui, key, kind):
    async def scenario():
        backend = Backend()
        ui.key = key
        task = await start(backend, ui)
        backend.items.put(event(f"{kind}.asked", id="p1", permission="write",
                                questions=[{"question": "Continue?", "options": [{"label": "Yes"}]}]))
        try:
            await wait_for(lambda: backend.aborts or backend.replies or task.done())
            assert backend.aborts == (0 if key == "escape" else 1)
            expected_reply = ("p1", "reject") if kind == "permission" else ("p1",)
            assert backend.replies == ([expected_reply] if key == "escape" else [])
            if key != "escape":
                backend.items.put(event("permission.asked", id="p2", permission="write"))
                backend.items.put(event("server.snapshot", pending={"permissions": [{"id": "p3"}]}))
                await asyncio.sleep(.03)
                assert ui.selectors == 1, "cancelled run must never reopen a prompt"
        finally:
            backend.finish()
            await task
    asyncio.run(scenario())


def test_settled_does_not_allow_next_run_before_late_abort_finishes(ui):
    async def scenario():
        backend = Backend()
        backend.abort_release.clear()
        task = await start(backend, ui)
        try:
            assert callable(ui.handler)
            ui.handler(signal.SIGINT, None)
            await wait_for(backend.abort_entered.is_set)
            backend.finish()
            await wait_for(backend.closed.is_set)
            await asyncio.sleep(.03)
            assert not task.done(), "late Session abort could cancel the next run"
        finally:
            backend.abort_release.set()
            backend.finish()
            await task
        assert backend.abort_done.is_set()
    asyncio.run(scenario())


def test_cancel_deadline_reports_unknown_and_holds_blocked_worker(ui):
    async def scenario():
        backend = Backend(timeout=.06)
        backend.abort_release.clear()
        task = await start(backend, ui)
        try:
            assert callable(ui.handler)
            ui.handler(signal.SIGINT, None)
            await wait_for(backend.abort_entered.is_set)
            for _ in range(15):
                backend.items.put(event("server.heartbeat"))
                await asyncio.sleep(.01)
            assert "取消结果尚未确认" in ui.output.getvalue()
            assert backend.session_id in ui.output.getvalue()
            assert not task.done()
            assert backend.snapshots == 1, "deadline must stop new confirmation requests"
            assert backend.aborts == 1
        finally:
            backend.abort_release.set()
            backend.finish()
            with pytest.raises(RuntimeError):
                await task
        assert backend.abort_done.is_set() and backend.closed.is_set()
        assert backend.snapshots == 1
    asyncio.run(scenario())


def test_external_task_cancel_does_not_abort_and_waits_for_reader(ui):
    async def scenario():
        backend = Backend()
        task = await start(backend, ui)
        task.cancel()
        await asyncio.sleep(.04)
        assert not task.done(), "reader is blocked and still owned"
        task.cancel()
        backend.finish()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert backend.aborts == 0
        assert backend.closed.is_set()
        assert backend.snapshots == 1
    asyncio.run(scenario())


def test_plain_eof_never_aborts(ui):
    async def scenario():
        backend = Backend()
        task = await start(backend, ui)
        backend.settled = True
        backend.items.put(None)
        await task
        assert backend.aborts == 0
        assert backend.closed.is_set()
    asyncio.run(scenario())


def test_confirmation_snapshot_shares_deadline_and_is_retained(ui):
    async def scenario():
        entered, release, done = threading.Event(), threading.Event(), threading.Event()

        class SnapshotBackend(Backend):
            def attach_snapshot(self):
                result = super().attach_snapshot()
                if self.snapshots > 1:
                    entered.set()
                    assert release.wait(3)
                    done.set()
                return result

        backend = SnapshotBackend(timeout=.06)
        task = await start(backend, ui)
        try:
            ui.handler(signal.SIGINT, None)
            await wait_for(backend.abort_done.is_set)
            backend.items.put(None)
            await wait_for(entered.is_set)
            await wait_for(lambda: "取消结果尚未确认" in ui.output.getvalue())
            assert not task.done()
            assert backend.snapshots == 2
            assert backend.streams == 1
        finally:
            release.set()
            backend.finish()
            with pytest.raises(RuntimeError):
                await task
        assert done.is_set() and backend.snapshots == 2 and backend.streams == 1
    asyncio.run(scenario())


def test_pending_reply_http_finishes_before_external_cancel_returns(ui):
    async def scenario():
        entered, release, done = threading.Event(), threading.Event(), threading.Event()

        class ReplyBackend(Backend):
            def reply_permission(self, *args):
                entered.set()
                assert release.wait(3)
                done.set()
                return super().reply_permission(*args)

        backend = ReplyBackend()
        ui.key = "escape"
        task = await start(backend, ui)
        backend.items.put(event("permission.asked", id="p1", permission="write"))
        try:
            await wait_for(entered.is_set)
            task.cancel()
            backend.finish()
            await wait_for(backend.closed.is_set)
            await asyncio.sleep(.04)
            assert not task.done()
            task.cancel()
        finally:
            release.set()
            backend.finish()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert done.is_set() and backend.aborts == 0
    asyncio.run(scenario())


def test_settled_reader_close_is_real_input_barrier(ui):
    async def scenario():
        entered, release = threading.Event(), threading.Event()

        class CloseBackend(Backend):
            def events(self, **kwargs):
                try:
                    yield from super().events(**kwargs)
                finally:
                    entered.set()
                    assert release.wait(3)

        backend = CloseBackend(timeout=.06)
        task = await start(backend, ui)
        try:
            ui.handler(signal.SIGINT, None)
            await wait_for(backend.abort_done.is_set)
            backend.finish()
            await wait_for(entered.is_set)
            await wait_for(lambda: "取消结果尚未确认" in ui.output.getvalue())
            assert not task.done()
            assert backend.snapshots == 1
        finally:
            release.set()
            with pytest.raises(RuntimeError):
                await task
        assert backend.streams == 1
    asyncio.run(scenario())


def test_failed_rebase_keeps_old_reader_and_never_opens_second(ui):
    async def scenario():
        entered, release = threading.Event(), threading.Event()

        class CloseBackend(Backend):
            def events(self, **kwargs):
                try:
                    yield from super().events(**kwargs)
                finally:
                    entered.set()
                    assert release.wait(3)

        backend = CloseBackend(timeout=.06)
        task = await start(backend, ui)
        try:
            ui.handler(signal.SIGINT, None)
            await wait_for(backend.abort_done.is_set)
            backend.items.put(event("server.event_gap", resume_required=True))
            await wait_for(entered.is_set)
            await wait_for(lambda: "取消结果尚未确认" in ui.output.getvalue())
            assert not task.done()
            assert backend.snapshots == backend.streams == 1
        finally:
            release.set()
            with pytest.raises(RuntimeError):
                await task
        assert backend.snapshots == backend.streams == 1
    asyncio.run(scenario())


def test_failed_connection_never_requests_abort_or_final_snapshot(ui):
    async def scenario():
        class FailingBackend(Backend):
            def attach_snapshot(self):
                if self.snapshots:
                    self.snapshots += 1
                    raise OSError("offline")
                return super().attach_snapshot()

        backend = FailingBackend()
        task = await start(backend, ui)
        backend.items.put(None)
        with pytest.raises(RuntimeError):
            await task
        assert backend.aborts == 0
        assert backend.snapshots == 2
        assert backend.closed.is_set()
        assert "cancelled" not in ui.output.getvalue().lower()
    asyncio.run(scenario())


def test_cancelled_initial_snapshot_worker_is_not_detached(ui):
    async def scenario():
        entered, release, done = threading.Event(), threading.Event(), threading.Event()

        class SnapshotBackend(Backend):
            def attach_snapshot(self):
                entered.set()
                assert release.wait(3)
                done.set()
                return super().attach_snapshot()

        backend = SnapshotBackend()
        task = asyncio.create_task(remote._follow_run(backend, ui.console))
        try:
            await wait_for(entered.is_set)
            task.cancel()
            await asyncio.sleep(.04)
            assert not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert done.is_set() and backend.aborts == 0 and backend.streams == 0
    asyncio.run(scenario())


def test_real_backend_does_not_reconnect_inside_reader_after_deadline(ui):
    async def scenario():
        import json
        from nz_coder.http_service.client import NZCoderClient
        from nz_coder.interface.backend import RemoteTerminalBackend

        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                closed.set()

            def __iter__(self):
                entered.set()
                assert release.wait(3)
                return iter(())

        class Opener:
            def open(self, request, **kwargs):
                requests.append((request.method, request.full_url))
                if "/event?" in request.full_url:
                    return Response()
                payload = ({"aborted": True} if request.method == "POST" else {
                    "cursor": {}, "settled": False,
                    "session": {"running": True, "status": "running"},
                    "events": [], "pending": {},
                })
                return io.BytesIO(json.dumps(payload).encode())

        client = NZCoderClient("http://127.0.0.1:1", "test-only", timeout=.06)
        client._opener = Opener()
        backend = RemoteTerminalBackend(client, "reader-session")
        task = asyncio.create_task(remote._follow_run(backend, ui.console))
        try:
            await wait_for(entered.is_set)
            ui.handler(signal.SIGINT, None)
            await wait_for(lambda: "取消结果尚未确认" in ui.output.getvalue())
            assert not task.done()
            count = len(requests)
        finally:
            release.set()
            with pytest.raises(RuntimeError):
                await task
        assert closed.is_set()
        assert len(requests) == count == 3
        assert sum("/event?" in url for method, url in requests) == 1
    asyncio.run(scenario())


def test_user_cancel_then_reconnect_does_not_reopen_pending_interactions(ui):
    async def scenario():
        class PendingBackend(Backend):
            def attach_snapshot(self):
                snapshot = super().attach_snapshot()
                if self.snapshots > 1:
                    snapshot["pending"] = {"permissions": [{"id": "late-permission"}]}
                return snapshot

        backend = PendingBackend()
        task = await start(backend, ui)
        try:
            ui.handler(signal.SIGINT, None)
            await wait_for(backend.abort_done.is_set)
            backend.items.put(None)
            await wait_for(lambda: backend.streams == 2)
            backend.items.put(event("permission.asked", id="late-live-permission"))
            await asyncio.sleep(.03)
            assert ui.selectors == 0 and backend.replies == []
        finally:
            backend.finish()
            await task
        assert backend.aborts == 1
    asyncio.run(scenario())


def test_normal_reconnect_preserves_pending_prompt(ui):
    async def scenario():
        class PendingBackend(Backend):
            def attach_snapshot(self):
                snapshot = super().attach_snapshot()
                if self.snapshots > 1:
                    snapshot["pending"] = {"permissions": [{"id": "restored-permission"}]}
                return snapshot

        backend = PendingBackend()
        ui.key = "escape"
        task = await start(backend, ui)
        try:
            backend.items.put(None)
            await wait_for(lambda: bool(backend.replies))
            assert backend.replies == [("restored-permission", "reject")]
        finally:
            backend.finish()
            await task
        assert backend.aborts == 0 and backend.streams == 2
    asyncio.run(scenario())


def test_abort_failure_is_collected_but_authoritative_completion_wins(ui):
    async def scenario():
        class FailingAbortBackend(Backend):
            def abort(self):
                self.aborts += 1
                self.abort_done.set()
                raise OSError("abort response lost")

        backend = FailingAbortBackend()
        task = await start(backend, ui)
        ui.handler(signal.SIGINT, None)
        await wait_for(backend.abort_done.is_set)
        backend.finish("completed")
        await task
        assert backend.aborts == 1
        assert "abort response lost" in ui.output.getvalue()
        assert "cancelled" not in ui.output.getvalue().lower()
    asyncio.run(scenario())
