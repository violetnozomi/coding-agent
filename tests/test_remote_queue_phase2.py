"""Deterministic backpressure tests for the remote terminal receiver."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nz_coder.interface.remote import _offer_remote_payload


def test_remote_terminal_event_not_starved_by_text_deltas():
    queue = asyncio.Queue(maxsize=2)
    _offer_remote_payload(queue, {"type": "message.part.delta", "properties": {"delta": "a"}})
    _offer_remote_payload(queue, {"type": "message.part.delta", "properties": {"delta": "b"}})

    terminal = {"type": "session.run.completed", "properties": {"status": "completed"}}
    _offer_remote_payload(queue, terminal)

    assert terminal in list(queue._queue)


def test_remote_queue_overflow_rebases_from_snapshot():
    queue = asyncio.Queue(maxsize=1)
    _offer_remote_payload(queue, {"type": "message.part.delta", "properties": {"delta": "a"}})
    _offer_remote_payload(queue, {"type": "message.part.delta", "properties": {"delta": "b"}})

    notice = queue.get_nowait()
    assert notice == {
        "type": "server.event_gap",
        "properties": {
            "local_queue_overflow": True,
            "resume_required": True,
        },
    }


def test_remote_sse_continues_while_waiting_for_permission_input(monkeypatch):
    from nz_coder.interface import remote

    fed = []
    permission_started = asyncio.Event()

    class Backend:
        def attach_snapshot(self):
            return {
                "cursor": {},
                "session": {"running": True},
                "run": {
                    "interaction_run_id": "interaction-1",
                    "messages": [],
                    "pending": {},
                },
                "events": [],
                "pending": {},
            }

        def events(self, **_kwargs):
            class Stream:
                def __iter__(self):
                    return iter([{
                        "type": "permission.asked",
                        "properties": {
                            "id": "permission-1",
                            "permission": "write",
                            "tool_input": {},
                        },
                    }, {
                        "type": "message.part.delta",
                        "properties": {"delta": "still-consumed"},
                    }, {
                        "type": "session.run.settled",
                        "properties": {"status": "completed"},
                    }])

                def close(self):
                    pass

            return Stream()

    class Renderer:
        def __init__(self, _console):
            pass

        def start(self):
            pass

        def finish(self):
            pass

    class View:
        def __init__(self, *_args):
            pass

        def begin_remote(self, _agent):
            pass

        def rebase_remote(self, *_args, **_kwargs):
            pass

        def feed(self, event):
            fed.append(event["type"] if isinstance(event, dict) else event.type)

        def finish(self, _result):
            pass

        def close(self):
            pass

    class Input:
        def __init__(self, **_kwargs):
            pass

        async def close_async(self):
            pass

    class Bridge:
        def __init__(self, terminal_input, *_args):
            self.terminal_input = terminal_input

        async def _ask_permission(self, *_args):
            permission_started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(remote, "StreamingRenderer", Renderer)
    monkeypatch.setattr(remote, "TerminalRunRenderer", View)
    monkeypatch.setattr(remote, "TerminalInput", Input)
    monkeypatch.setattr(remote, "TerminalInteractionBridge", Bridge)

    async def scenario():
        await remote._follow_run(
            Backend(),
            SimpleNamespace(print=lambda *_args, **_kwargs: None),
        )
        assert permission_started.is_set()

    asyncio.run(scenario())

    assert "message.part.delta" in fed
    assert "session.run.settled" in fed
