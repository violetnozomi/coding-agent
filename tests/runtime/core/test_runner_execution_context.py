"""Focused Runner orchestration context tests."""
from __future__ import annotations

from pathlib import Path

from nz_coder.runtime.adapters.runner import runner_context_from_legacy_host
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.core.profiles import MAIN_PROFILE
from nz_coder.runtime.core.run_context import RunContext
from nz_coder.runtime.core.runner_context import RunnerExecutionContext
from nz_coder.runtime.session.model import Session


class _Tracer:
    def log(self, *_args, **_kwargs) -> None:
        return None


class _Hooks:
    stop_hook_reason = ""

    def on_turn_start(self, *_args, **_kwargs) -> None:
        return None

    def on_pre_send(self, *_args, **_kwargs) -> None:
        return None

    def on_turn_end(self, *_args, **_kwargs) -> None:
        return None


class _Host:
    session_id = "session-runner"
    runtime_state = object()
    hooks = _Hooks()
    tracer = _Tracer()
    _agent_call_stack = []

    def _has_queued_followup(self) -> bool:
        return False

    def _drain_background_agent_messages(self, _messages) -> None:
        return None


class _Services:
    pass


def _run_context(tmp_path: Path) -> RunContext:
    request = RunRequest(
        agent=AgentDefinition(name="coder", instructions="fix"),
        profile=MAIN_PROFILE,
        session_id="session-runner",
        workspace=tmp_path,
        messages=(),
    )
    session = Session.create("session-runner", [], workspace=tmp_path)
    session.begin_run()
    return RunContext(request, session, "coder")


def test_runner_context_exposes_named_state_without_retaining_host(tmp_path) -> None:
    host = _Host()
    context = runner_context_from_legacy_host(
        host,
        _Services(),
        _run_context(tmp_path),
    )

    assert isinstance(context, RunnerExecutionContext)
    assert context.session_id == "session-runner"
    assert context.runtime_state is host.runtime_state
    assert context.control.has_queued_followup() is False
    assert context.control.has_agent_call_stack() is False
    assert "host" not in vars(context)


def test_runner_context_turn_hooks_bind_legacy_owner(tmp_path) -> None:
    calls = []
    host = _Host()
    host.hooks.on_turn_start = lambda owner, messages: calls.append((owner, messages))
    context = runner_context_from_legacy_host(
        host,
        _Services(),
        _run_context(tmp_path),
    )

    messages = [{"role": "user", "content": "fix"}]
    context.hooks.on_turn_start(messages)

    assert calls == [(host, messages)]
