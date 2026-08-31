"""Behavioral contracts for shared Runner request, result, and state models."""
from __future__ import annotations

from pathlib import Path
import threading

import pytest

from nz_coder.runtime.core import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.core.result import RunResult, RunStatus, TokenUsage
from nz_coder.runtime.core.run_context import RunContext
from nz_coder.runtime.core.state import RunState
from nz_coder.runtime.session.model import Session


@pytest.fixture
def run_request(tmp_path: Path) -> RunRequest:
    return RunRequest(
        agent=AgentDefinition(name="worker", instructions="Inspect the repository"),
        profile=MAIN_PROFILE,
        messages=[{"role": "user", "content": "inspect"}],
        workspace=tmp_path,
        session_id="session-1",
        tool_names=["read_file"],
    )


def test_request_snapshots_messages_and_tool_names(tmp_path: Path) -> None:
    """Host mutation after submission must not alter a live Runner request."""
    messages = [{"role": "user", "content": "inspect"}]
    tool_names = ["read_file"]
    request = RunRequest(
        agent=AgentDefinition(name="worker", instructions="Inspect the repository"),
        profile=MAIN_PROFILE,
        messages=messages,
        workspace=tmp_path,
        session_id="session-1",
        tool_names=tool_names,
    )

    messages[0]["content"] = "changed"
    tool_names.append("bash")

    assert request.messages[0]["content"] == "inspect"
    assert request.tool_names == ("read_file",)
    assert request.workspace == tmp_path.resolve()


@pytest.mark.parametrize(
    ("name", "instructions", "message"),
    [("", "valid", "name"), ("worker", "", "instructions")],
)
def test_agent_definition_rejects_missing_identity(
    name: str,
    instructions: str,
    message: str,
) -> None:
    """Invalid declarations must fail before Provider or tool initialization."""
    with pytest.raises(ValueError, match=message):
        AgentDefinition(name=name, instructions=instructions)


def test_request_rejects_empty_session_and_bad_message(tmp_path: Path) -> None:
    """Persistence keys and transcript entries must be valid at the boundary."""
    agent = AgentDefinition(name="worker", instructions="Inspect")
    with pytest.raises(ValueError, match="session_id"):
        RunRequest(agent, MAIN_PROFILE, [], tmp_path, "")
    with pytest.raises(ValueError, match="message at index 0"):
        RunRequest(agent, MAIN_PROFILE, [{"content": "missing role"}], tmp_path, "s1")


def test_run_state_is_the_mutable_transcript_owner(run_request: RunRequest) -> None:
    """Only RunState may advance the transcript and turn lifecycle."""
    state = RunState.from_request(run_request)
    state.append_message({"role": "assistant", "content": "done"})
    state.begin_turn()

    assert state.turn_count == 1
    assert state.active_agent == "worker"
    assert state.transcript[-1]["content"] == "done"
    assert len(run_request.messages) == 1


def test_run_state_rejects_append_after_terminal(run_request: RunRequest) -> None:
    """A terminal transcript cannot silently accept another model turn."""
    state = RunState.from_request(run_request)
    state.finish(RunStatus.COMPLETED, "done")

    with pytest.raises(RuntimeError, match="terminal"):
        state.append_message({"role": "user", "content": "late"})


def test_token_usage_accumulates_mutually_exclusive_buckets() -> None:
    """Usage aggregation must preserve immutable per-call evidence."""
    first = TokenUsage(input_tokens=10, output_tokens=3)
    total = first.add(TokenUsage(input_tokens=5, output_tokens=2, cached_read_tokens=4))

    assert first.total_tokens == 13
    assert total == TokenUsage(
        input_tokens=15,
        output_tokens=5,
        cached_read_tokens=4,
    )
    assert total.total_tokens == 24


def test_token_usage_total_includes_reasoning_and_cache_buckets() -> None:
    usage = TokenUsage(
        input_tokens=100,
        output_tokens=20,
        cached_read_tokens=30,
        cached_write_tokens=4,
        reasoning_tokens=10,
    )

    assert usage.total_tokens == 164


def test_run_context_serializes_parallel_auxiliary_usage(
    run_request: RunRequest,
) -> None:
    """Concurrent Sidecar finishes must not overwrite each other's tokens."""
    first_add = threading.Event()
    second_add = threading.Event()

    class RacingUsage:
        def add(self, other: TokenUsage) -> TokenUsage:
            if not first_add.is_set():
                first_add.set()
                second_add.wait(timeout=0.2)
            else:
                second_add.set()
            return other

    context = RunContext(
        run_request,
        Session.create(
            run_request.session_id,
            run_request.messages,
            workspace=run_request.workspace,
        ),
        "worker",
    )
    context.usage = RacingUsage()
    threads = [
        threading.Thread(
            target=context.add_usage,
            args=(TokenUsage(input_tokens=10),),
        )
        for _index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert context.usage.input_tokens == 20


def test_run_result_snapshots_terminal_messages() -> None:
    """Host-side rendering cannot rewrite persisted terminal evidence."""
    messages = [{"role": "assistant", "content": "done"}]
    result = RunResult(
        status=RunStatus.COMPLETED,
        final_text="done",
        messages=messages,
        usage=TokenUsage(input_tokens=1, output_tokens=1),
        session_id="session-1",
        active_agent="worker",
    )
    messages[0]["content"] = "changed"

    assert result.messages[0]["content"] == "done"
    assert result.status is RunStatus.COMPLETED
