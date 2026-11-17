"""Recovery limits count consecutive failed progress, not successful long runs."""
from __future__ import annotations

import pytest

from nz_coder.runtime.conversation.context_manager import (
    CompactionAttemptState,
    CompactionAttemptsExhausted,
)
from nz_coder.runtime.conversation.model_result import LLMResult


_READ = {
    "id": "read-1", "type": "function",
    "function": {"name": "read_file", "arguments": '{"path":"parser.py"}'},
}


@pytest.mark.parametrize("response", [
    LLMResult(content="Continue with the fix.", finish_reason="stop"),
    LLMResult(tool_calls=[_READ], finish_reason="tool_calls"),
])
def test_normal_responses_allow_more_than_three_compactions(response):
    """An old total-operation cap would stop a productive long task early."""
    state = CompactionAttemptState()
    for _ in range(5):
        state.reserve()
        state.observe_response(response)

    assert state.attempts == 5
    assert state.reserve() == 6


@pytest.mark.parametrize("response", [
    LLMResult(),
    LLMResult(content="  "),
    LLMResult(content="partial", finish_reason="length"),
    LLMResult(content="partial", finish_reason="max_tokens"),
    LLMResult(content="partial", finish_reason="error"),
    LLMResult(content="diagnostic", diagnostic="retry the provider"),
    LLMResult(content="cancelled", aborted=True),
    LLMResult(content="overflow", needs_compaction=True),
    LLMResult(content="failed", assistant_error={"name": "ProviderError"}),
    LLMResult(tool_calls=[_READ], tools_executed_in_stream=True,
              post_tool_stream_error="stream disconnected"),
    LLMResult(tool_calls=[{}]),
])
def test_unusable_responses_do_not_renew_recovery_allowance(response):
    """Partial output and inline effects cannot hide a failed final response."""
    state = CompactionAttemptState()
    for _ in range(3):
        state.reserve()
        state.observe_response(response)

    with pytest.raises(CompactionAttemptsExhausted):
        state.reserve()
    assert state.attempts == 3


def test_summaries_alone_do_not_renew_recovery_allowance():
    state = CompactionAttemptState()
    for _ in range(3):
        state.reserve()
    with pytest.raises(CompactionAttemptsExhausted):
        state.reserve()
