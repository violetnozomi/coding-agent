"""Tests for API recovery and consecutive tool-call loop protection."""

from nz_coder.recovery import RecoveryState, is_context_overflow_error


class _APIError(RuntimeError):
    def __init__(self, message: str, status_code: int, headers=None):
        super().__init__(message)
        self.status_code = status_code
        self.headers = headers or {}


def test_retry_policy_rejects_auth_and_context_errors_but_accepts_transient_statuses():
    assert not RecoveryState.is_retryable(_APIError("unauthorized", 401))
    assert not RecoveryState.is_retryable(RuntimeError("context length overflow"))
    assert RecoveryState.is_retryable(_APIError("overloaded", 503))
    assert RecoveryState.is_retryable(_APIError("rate limit", 429))


def test_context_overflow_classifier_excludes_ordinary_bad_request():
    assert is_context_overflow_error(
        RuntimeError("context_length_exceeded: maximum context length is 8192 tokens")
    )
    assert is_context_overflow_error("Input exceeds context window of this model")
    assert not is_context_overflow_error("invalid json in tool arguments")


def test_retry_delay_honors_provider_headers():
    milliseconds = _APIError("rate limit", 429, {"Retry-After-Ms": "1500"})
    seconds = _APIError("rate limit", 429, {"Retry-After": "3"})

    assert RecoveryState.retry_after_seconds(milliseconds) == 1.5
    assert RecoveryState.retry_after_seconds(seconds) == 3.0


def test_observe_tool_call_counts_canonical_equivalent_arguments():
    state = RecoveryState()

    first = state.observe_tool_call("read_file", {"path": "app.py", "start": 1}, threshold=3)
    second = state.observe_tool_call("read_file", {"start": 1, "path": "app.py"}, threshold=3)
    third = state.observe_tool_call("read_file", {"path": "app.py", "start": 1}, threshold=3)

    assert first == {"count": 1, "should_block": False}
    assert second == {"count": 2, "should_block": False}
    assert third == {"count": 3, "should_block": True}


def test_observe_tool_call_resets_after_different_call():
    state = RecoveryState()

    state.observe_tool_call("read_file", {"path": "app.py"}, threshold=3)
    state.observe_tool_call("read_file", {"path": "app.py"}, threshold=3)
    different = state.observe_tool_call("grep_search", {"query": "run"}, threshold=3)
    next_read = state.observe_tool_call("read_file", {"path": "app.py"}, threshold=3)

    assert different == {"count": 1, "should_block": False}
    assert next_read == {"count": 1, "should_block": False}


def test_observe_tool_call_does_not_block_nonconsecutive_calls():
    state = RecoveryState()

    observations = [
        state.observe_tool_call("read_file", {"path": path}, threshold=3)
        for path in ("app.py", "other.py", "app.py", "third.py", "app.py")
    ]

    assert all(item == {"count": 1, "should_block": False} for item in observations)


def test_reset_tool_call_history_allows_reread_after_workspace_change():
    state = RecoveryState()
    for path in ("app.py", "other.py", "app.py", "third.py"):
        state.observe_tool_call("read_file", {"path": path}, threshold=3)

    state.reset_tool_call_history(reason="workspace_changed")
    reread = state.observe_tool_call("read_file", {"path": "app.py"}, threshold=3)

    assert reread == {"count": 1, "should_block": False}


def test_agent_guard_allows_rotating_read_file_cycle():
    from nz_coder.loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent.recovery = RecoveryState()

    def observe(call_id: str, path: str):
        return agent._find_repeated_tool_calls([{
            "id": call_id,
            "function": {
                "name": "read_file",
                "arguments": {"path": path},
            },
        }])

    assert observe("1", "app.py") == {}
    assert observe("2", "other.py") == {}
    assert observe("3", "app.py") == {}
    assert observe("4", "third.py") == {}
    assert observe("5", "app.py") == {}


def test_reset_tool_call_history_starts_a_fresh_streak():
    state = RecoveryState()
    state.observe_tool_call("read_file", {"path": "app.py"}, threshold=3)
    state.observe_tool_call("read_file", {"path": "app.py"}, threshold=3)

    state.reset_tool_call_history()
    fresh = state.observe_tool_call("read_file", {"path": "app.py"}, threshold=3)

    assert fresh == {"count": 1, "should_block": False}


def test_observe_tool_call_can_be_disabled_and_reset():
    state = RecoveryState()
    state.observe_tool_call("read_file", {"path": "app.py"}, threshold=3)
    state.observe_tool_call("read_file", {"path": "app.py"}, threshold=3)

    disabled = state.observe_tool_call("read_file", {"path": "app.py"}, threshold=0)
    reset_event = state.consume_tool_streak_event()
    enabled_again = state.observe_tool_call("read_file", {"path": "app.py"}, threshold=3)

    assert disabled == {"count": 0, "should_block": False}
    assert reset_event is not None
    assert reset_event["reason"] == "guard_disabled"
    assert reset_event["previous_count"] == 2
    assert enabled_again == {"count": 1, "should_block": False}


def test_doom_loop_diagnostic_requires_a_different_conservative_approach():
    state = RecoveryState()

    diagnostic = state.tool_failure_diagnostic(
        "edit_file",
        "Denied: Doom loop detected: identical call repeated 3 times.",
    )

    assert "<doom-loop-diagnostic>" in diagnostic
    assert "Do not submit the same call again" in diagnostic
    assert "preserve public APIs" in diagnostic
    assert "smallest evidence-backed change" in diagnostic


def test_agent_doom_loop_permission_can_approve_exact_repeat():
    from nz_coder.loop import AgentLoop

    agent = AgentLoop(
        "test",
        permission_mode="default",
        permission_asker=lambda name, payload: (
            "once" if name == "doom_loop" and payload["tool"] == "read_file" else "reject"
        ),
        client=object(),
        trace_enabled=False,
    )
    call = {
        "id": "repeat",
        "function": {"name": "read_file", "arguments": {"path": "app.py"}},
    }

    assert agent._find_repeated_tool_calls([call]) == {}
    assert agent._find_repeated_tool_calls([call]) == {}
    blocked = agent._find_repeated_tool_calls([call])

    assert 0 in blocked
    assert agent._resolve_doom_loop_permissions(blocked, [call]) == {}
    assert agent.recovery.repeated_tool_calls == 0
