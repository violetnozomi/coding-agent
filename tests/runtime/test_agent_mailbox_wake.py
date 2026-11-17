"""Background mail wakes the parent without polling or consuming user input."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from prompt_toolkit.completion import DummyCompleter
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.styles import Style

from nz_coder.interface.cli import _attach_terminal_interactions
from nz_coder.interface.fullscreen import FullscreenComposer
from nz_coder.interface.terminal_input import TerminalInput
from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
from nz_coder.runtime.execution.loop import AgentLoop


@pytest.fixture
def running_child(tmp_path, monkeypatch):
    import nz_coder.runtime.agent.subagent as subagent

    release = threading.Event()

    def run(_prompt, *, session_id, **_kwargs):
        assert release.wait(5), "test did not release its child"
        state = subagent._load_subagent_state("parent", session_id, tmp_path)
        state["status"] = "completed"
        subagent._save_subagent_state("parent", state, tmp_path)
        return "parser investigation complete"

    monkeypatch.setattr(subagent, "run_subagent", run)
    manager = BackgroundAgentManager(tmp_path, "parent")
    started = manager.start([{
        "prompt": "inspect parser", "read_only": True, "target_paths": [],
    }])
    assert hasattr(started, "metadata"), started
    task_id = started.metadata["task_ids"][0]
    try:
        yield manager, task_id, release
    finally:
        release.set()
        manager.close(timeout=3)


def _parent(manager):
    parent = AgentLoop.__new__(AgentLoop)
    parent.background_agents = manager
    parent._followup_pending = None
    parent._followup_waiter = None
    events = []
    parent.tracer = SimpleNamespace(log=lambda event, **data: events.append(event))
    return parent, events


def _composer(pipe):
    return FullscreenComposer(
        transcript_provider=lambda: "Session", status_provider=lambda: "running",
        sidebar_provider=None, sidebar_mode="hide", completer=DummyCompleter(),
        history=None, style=Style.from_dict({}), mouse_support=False,
        empty_ctrl_c_requests_exit=lambda: False, clear_exit_request=lambda: None,
        input=pipe, output=DummyOutput(),
    )


def _submit(composer, text):
    composer.buffer.text = text
    binding = composer._build_bindings().get_bindings_for_keys((Keys.ControlM,))[0]
    binding.handler(SimpleNamespace())


def test_idle_parent_waits_without_periodic_sleep(running_child, monkeypatch):
    """An idle parent must wait for a notification, without timed wakeups."""
    manager, _task_id, release = running_child
    parent, events = _parent(manager)
    original_sleep = asyncio.sleep
    delays = []

    async def observed_sleep(delay, *args, **kwargs):
        delays.append(delay)
        await original_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", observed_sleep)

    async def scenario():
        messages = []
        waiting = asyncio.create_task(parent._idle_yield_for_background_messages(messages))
        try:
            await original_sleep(0)
            assert not waiting.done()
            assert not [delay for delay in delays if delay > 0], delays
            release.set()
            assert await asyncio.wait_for(waiting, 2) is True
            assert "parser investigation complete" in messages[0]["content"]
            assert events[-1] == "idle_yield_resumed"
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)

    asyncio.run(scenario())


def test_peer_mail_wakes_waiting_parent_before_child_finishes(running_child):
    manager, task_id, _release = running_child
    parent, _events = _parent(manager)

    async def scenario():
        messages = []
        waiting = asyncio.create_task(parent._idle_yield_for_background_messages(messages))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        sender = threading.Thread(target=lambda: manager.send_message(
            sender=task_id, recipient="worker", content="parser contract found",
        ))
        sender.start()
        try:
            assert await asyncio.wait_for(waiting, 2) is True
            assert "parser contract found" in messages[0]["content"]
            assert not manager._jobs[task_id].done_event.is_set()
        finally:
            sender.join(2)
            assert not sender.is_alive()

    asyncio.run(scenario())


def test_mail_between_initial_drain_and_wait_is_not_lost(running_child, monkeypatch):
    manager, task_id, _release = running_child
    parent, _events = _parent(manager)
    original = manager.has_worker_wake_source
    sent = False

    def inspect():
        nonlocal sent
        if not sent:
            sent = True
            manager.send_message(sender=task_id, recipient="worker", content="early evidence")
        return original()

    monkeypatch.setattr(manager, "has_worker_wake_source", inspect)

    async def scenario():
        messages = []
        assert await asyncio.wait_for(parent._idle_yield_for_background_messages(messages), 2)
        assert "early evidence" in messages[0]["content"]

    asyncio.run(scenario())


def test_cancelled_parent_retires_wake_waiters_and_can_wait_again(running_child):
    manager, _task_id, release = running_child
    parent, _events = _parent(manager)

    async def scenario():
        waiting = asyncio.create_task(parent._idle_yield_for_background_messages([]))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert not manager._worker_waiters
        resumed_messages = []
        resumed = asyncio.create_task(parent._idle_yield_for_background_messages(resumed_messages))
        release.set()
        assert await asyncio.wait_for(resumed, 2) is True
        assert "parser investigation complete" in resumed_messages[0]["content"]
        assert not manager._worker_waiters

    asyncio.run(scenario())


def test_terminal_followup_wakes_parent_without_consuming_submission(running_child):
    manager, _task_id, _release = running_child
    parent, events = _parent(manager)

    async def scenario():
        with create_pipe_input() as pipe:
            composer = _composer(pipe)
            terminal = TerminalInput.__new__(TerminalInput)
            terminal.fullscreen = composer
            _attach_terminal_interactions(
                SimpleNamespace(set_followup_pending=parent.set_followup_pending),
                terminal, None,
            )
            before = asyncio.all_tasks()
            messages = []
            waiting = asyncio.create_task(parent._idle_yield_for_background_messages(messages))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            try:
                assert not waiting.done()
                _submit(composer, "please inspect another file")
                assert await asyncio.wait_for(waiting, 0.5) is False
                assert events[-1] == "idle_yield_superseded"
                assert composer._submissions.get_nowait() == "please inspect another file"
                assert messages == []
                assert not manager._worker_waiters
                assert asyncio.all_tasks() == before
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)

    asyncio.run(scenario())


def test_parent_cancellation_retires_terminal_and_mail_waits(running_child):
    manager, _task_id, _release = running_child
    parent, _events = _parent(manager)

    async def scenario():
        with create_pipe_input() as pipe:
            composer = _composer(pipe)
            terminal = TerminalInput.__new__(TerminalInput)
            terminal.fullscreen = composer
            _attach_terminal_interactions(
                SimpleNamespace(set_followup_pending=parent.set_followup_pending),
                terminal, None,
            )
            before = asyncio.all_tasks()
            waiting = asyncio.create_task(parent._idle_yield_for_background_messages([]))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            assert not manager._worker_waiters
            assert asyncio.all_tasks() == before
            assert not composer.has_pending_submission()

    asyncio.run(scenario())


def test_terminal_notification_can_be_reused_after_submission_consumed():
    async def scenario():
        with create_pipe_input() as pipe:
            composer = _composer(pipe)
            _submit(composer, "first")
            await asyncio.wait_for(composer.wait_for_pending_submission(), 0.5)
            assert composer._submissions.get_nowait() == "first"
            waiting = asyncio.create_task(composer.wait_for_pending_submission())
            await asyncio.sleep(0)
            assert not waiting.done()
            _submit(composer, "second")
            await asyncio.wait_for(waiting, 0.5)
            assert composer._submissions.get_nowait() == "second"

    asyncio.run(scenario())
