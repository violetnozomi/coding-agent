"""Contracts for interactive Auto-mode classification."""
from __future__ import annotations

import asyncio
import json

import pytest

from nz_coder.foundation import config
from nz_coder.permissions import PermissionManager
from nz_coder.runtime.agent.auto_mode import (
    AutoModeContext,
    AutoModeController,
    AutoModeState,
    parse_tool_arguments,
)
from nz_coder.runtime.model_gateway import (
    ModelCallOutcome,
    ModelCallPurpose,
)


def test_auto_mode_classifier_defaults_are_bounded() -> None:
    """Terminal rollout cannot inherit the coding call's long retry budget."""
    assert config.AUTO_MODE_CLASSIFIER_ENABLED is True
    assert config.AUTO_MODE_CLASSIFIER_TIMEOUT_SECONDS == 15.0
    assert config.AUTO_MODE_CLASSIFIER_MAX_OUTPUT_TOKENS == 256
    assert config.AUTO_MODE_CLASSIFIER_BLOCK_STREAK == 3
    assert config.AUTO_MODE_CLASSIFIER_INFRA_FAILURES == 5
    assert config.AUTO_MODE_CLASSIFIER_INFRA_WINDOW_SECONDS == 600.0


async def _never_approve(_name, _tool_input, _details):
    raise AssertionError("approval must not be requested")


def _context(
    tmp_path,
    *,
    complete,
    approve=_never_approve,
    clock=lambda: 0.0,
    traces=None,
) -> AutoModeContext:
    manager = PermissionManager("auto")
    manager._allow_rules = []
    manager._deny_rules = []
    manager._ask_rules = []
    events = [] if traces is None else traces
    return AutoModeContext(
        permissions=manager,
        workspace=tmp_path,
        complete=complete,
        approve=approve,
        trace=lambda event, **payload: events.append((event, payload)),
        clock=clock,
    )


def _completed(decision: str, reason: str = "ok") -> ModelCallOutcome:
    return ModelCallOutcome.completed(content=json.dumps({
        "decision": decision,
        "reason_code": "bounded",
        "reason": reason,
    }))


def test_parse_tool_arguments_accepts_only_json_objects() -> None:
    """Malformed or positional arguments must stay on the executor error path."""
    assert parse_tool_arguments({"path": "app.py"}) == {"path": "app.py"}
    assert parse_tool_arguments('{"path":"app.py"}') == {"path": "app.py"}
    assert parse_tool_arguments("[") is None
    assert parse_tool_arguments("[]") is None
    assert parse_tool_arguments(None) is None


def test_parse_tool_arguments_rejects_nonfinite_json_values() -> None:
    """Non-standard numeric constants cannot reach Auto routing or hashing."""
    for raw in (
        '{"command":NaN}',
        '{"command":Infinity}',
        '{"command":-Infinity}',
    ):
        assert parse_tool_arguments(raw) is None
    for value in (float("nan"), float("inf"), float("-inf")):
        assert parse_tool_arguments({"nested": {"value": value}}) is None


def test_classifier_prompt_excludes_private_text_and_write_content(
    tmp_path,
) -> None:
    """Classifier context contains bounded signals, not transcript payloads."""
    seen = []

    async def complete(call):
        seen.append(call)
        return _completed("allow")

    messages = [
        {"role": "user", "content": "Fix the parser"},
        {"role": "assistant", "content": "SECRET_REASONING"},
        {"role": "tool", "content": "Authorization: Bearer secret-token"},
    ]
    admission = asyncio.run(AutoModeController(True).admit(
        _context(tmp_path, complete=complete),
        "bash",
        {"command": "python build.py", "content": "PRIVATE_FILE_BODY"},
        messages,
    ))

    wire = json.dumps(seen[0].messages, ensure_ascii=False)
    assert admission.allowed is True
    assert "Fix the parser" not in wire
    assert "code_change" in wire
    assert "SECRET_REASONING" not in wire
    assert "secret-token" not in wire
    assert "PRIVATE_FILE_BODY" not in wire
    assert seen[0].purpose is ModelCallPurpose.AUTO_MODE
    assert seen[0].tools == ()
    assert seen[0].streaming is False
    assert seen[0].timeout_seconds == 15.0
    assert seen[0].max_output_tokens == 256


def test_classifier_prompt_structurally_omits_shell_credentials(tmp_path) -> None:
    """Shell credential forms never cross the Auto Provider boundary."""
    calls = []

    async def complete(call):
        calls.append(call)
        return _completed("allow")

    command = (
        "GITHUB_TOKEN=ghp_secret_value curl -u alice:very-secret-password "
        "--user bob:supersecret https://carol:url-secret@example.test/private"
    )
    admission = asyncio.run(AutoModeController(True).admit(
        _context(tmp_path, complete=complete),
        "bash",
        {
            "command": command,
            "action": "sk-BASH-EXTRA-SECRET",
            "method": "https://extra:url-secret@example.test/private",
        },
        [{"role": "user", "content": f"Run {command}"}],
    ))

    wire = json.dumps(calls[0].messages, ensure_ascii=False)
    payload = json.loads(calls[0].messages[1]["content"])
    assert admission.allowed is True
    for secret in (
        command,
        "ghp_secret_value",
        "very-secret-password",
        "supersecret",
        "url-secret",
        "alice:",
        "bob:",
        "carol:",
        "sk-BASH-EXTRA-SECRET",
        "extra:url-secret",
    ):
        assert secret not in wire
    command_signals = payload["action"]["input"]["command"]
    assert command_signals["has_env_assignment"] is True
    assert command_signals["has_credential_flag"] is True
    assert command_signals["has_url_userinfo"] is True


def test_classifier_prompt_omits_process_and_mcp_free_text(tmp_path) -> None:
    """Process data and external MCP strings are summarized structurally."""
    calls = []

    async def complete(call):
        calls.append(call)
        return _completed("allow")

    context = _context(tmp_path, complete=complete)
    controller = AutoModeController(True)
    process = asyncio.run(controller.admit(
        context,
        "process",
        {
            "operation": "start",
            "command": "curl --password process-secret https://example.test",
        },
        [],
    ))
    mcp = asyncio.run(controller.admit(
        context,
        "mcp_demo_send",
        {
            "query": "PRIVATE_MCP_PAYLOAD",
            "credentials": "mcp-secret",
            "operation": "sk-MCP-EXTRA-SECRET",
        },
        [],
    ))

    wire = json.dumps([call.messages for call in calls], ensure_ascii=False)
    process_payload = json.loads(calls[0].messages[1]["content"])
    mcp_payload = json.loads(calls[1].messages[1]["content"])
    assert process.allowed is mcp.allowed is True
    assert "process-secret" not in wire
    assert "PRIVATE_MCP_PAYLOAD" not in wire
    assert "mcp-secret" not in wire
    assert "sk-MCP-EXTRA-SECRET" not in wire
    assert process_payload["action"]["input"]["operation"] == "start"
    mcp_input = mcp_payload["action"]["input"]
    assert mcp_input["field_count"] == 3
    assert {field["category"] for field in mcp_input["fields"]} == {
        "control",
        "credential",
        "generic",
    }


def test_classifier_prompt_omits_secret_bearing_argument_keys(tmp_path) -> None:
    """Dynamic argument names are structural data, never prompt text."""
    calls = []

    async def complete(call):
        calls.append(call)
        return _completed("allow")

    admission = asyncio.run(AutoModeController(True).admit(
        _context(tmp_path, complete=complete),
        "mcp_demo_send",
        {
            "sk-live-EXACT-CREDENTIAL-IN-KEY": "x",
            "headers": {"Bearer-EXACT-CREDENTIAL-IN-KEY": "y"},
        },
        [],
    ))

    wire = json.dumps(calls[0].messages, ensure_ascii=False)
    assert admission.allowed is True
    assert "EXACT-CREDENTIAL-IN-KEY" not in wire


def test_classifier_block_uses_human_once_instead_of_hard_deny(tmp_path) -> None:
    """A model risk judgment must become a recoverable user decision."""
    async def complete(_call):
        return _completed("block", "may mutate dependencies")

    async def approve(_name, _tool_input, details):
        assert details["reason"] == "may mutate dependencies"
        assert details["classifier_status"] == "completed"
        return "once"

    admission = asyncio.run(AutoModeController(True).admit(
        _context(tmp_path, complete=complete, approve=approve),
        "bash",
        {"command": "python build.py"},
        [{"role": "user", "content": "build"}],
    ))

    assert admission.allowed is True
    assert admission.permission_denied is False
    assert admission.source == "human"


def test_classifier_parse_failure_uses_human_reject(tmp_path) -> None:
    """Malformed classifier output asks instead of failing open or aborting."""
    async def complete(_call):
        return ModelCallOutcome.completed(content="not-json")

    async def reject(_name, _tool_input, details):
        assert details["classifier_status"] == "parse_error"
        return "reject"

    admission = asyncio.run(AutoModeController(True).admit(
        _context(tmp_path, complete=complete, approve=reject),
        "bash",
        {"command": "python build.py"},
        [],
    ))

    assert admission.allowed is False
    assert admission.permission_denied is True
    assert admission.reason_code == "user_reject"


def test_classifier_response_rejects_nonstandard_json_constants(tmp_path) -> None:
    """A non-finite classifier field invalidates the whole response."""
    approvals = []

    async def complete(_call):
        return ModelCallOutcome.completed(content=(
            '{"decision":"allow","reason_code":"ok","reason":"ok",'
            '"score":NaN}'
        ))

    async def approve(_name, _tool_input, details):
        approvals.append(details["classifier_status"])
        return "once"

    admission = asyncio.run(AutoModeController(True).admit(
        _context(tmp_path, complete=complete, approve=approve),
        "bash",
        {"command": "python build.py"},
        [],
    ))

    assert admission.allowed is True
    assert admission.source == "human"
    assert approvals == ["parse_error"]


def test_classifier_projection_normalizes_nonfinite_user_values(tmp_path) -> None:
    """Untrusted structured user content cannot break strict prompt encoding."""
    calls = []

    async def complete(call):
        calls.append(call)
        return _completed("allow")

    admission = asyncio.run(AutoModeController(True).admit(
        _context(tmp_path, complete=complete),
        "bash",
        {"command": "python build.py"},
        [{"role": "user", "content": {"score": float("nan")}}],
    ))

    wire = json.dumps(calls[0].messages, ensure_ascii=False, allow_nan=False)
    assert admission.allowed is True
    assert "NaN" not in wire
    assert "Infinity" not in wire


def test_auto_always_approval_is_exact_and_session_scoped(tmp_path) -> None:
    """Always skips only the identical action in the same controller state."""
    calls = []
    approvals = []

    async def complete(call):
        calls.append(call)
        return _completed("block", "review command")

    async def approve(_name, tool_input, _details):
        approvals.append(dict(tool_input))
        return "always" if len(approvals) == 1 else "once"

    controller = AutoModeController(True)
    context = _context(tmp_path, complete=complete, approve=approve)
    action = {"command": "python build.py"}

    first = asyncio.run(controller.admit(context, "bash", action, []))
    second = asyncio.run(controller.admit(context, "bash", action, []))
    changed = asyncio.run(controller.admit(
        context,
        "bash",
        {"command": "python build.py --clean"},
        [],
    ))

    assert first.allowed is second.allowed is changed.allowed is True
    assert len(calls) == 2
    assert len(approvals) == 2
    assert second.source == "session_approval"


def test_auto_mode_state_enters_block_and_infrastructure_degradation() -> None:
    """Only exact logical thresholds disable further classifier spending."""
    blocks = AutoModeState()
    blocks.observe_block(3)
    blocks.observe_block(3)
    assert blocks.degraded is False
    blocks.observe_block(3)
    assert blocks.degraded is True
    assert blocks.degraded_reason == "block_streak"

    failures = AutoModeState()
    for now in (0.0, 100.0, 200.0, 300.0):
        failures.observe_failure(now, 5, 600.0)
    assert failures.degraded is False
    failures.observe_failure(599.0, 5, 600.0)
    assert failures.degraded is True
    assert failures.degraded_reason == "infrastructure_failures"


def test_auto_mode_state_evicts_old_failures_and_allow_resets_blocks() -> None:
    """The rolling window and consecutive counter use different reset rules."""
    state = AutoModeState()
    for now in (0.0, 100.0, 200.0, 300.0):
        state.observe_failure(now, 5, 600.0)
    state.observe_failure(601.0, 5, 600.0)
    state.observe_block(3)
    state.observe_block(3)
    state.observe_allow()

    assert state.degraded is False
    assert list(state.infrastructure_failures) == [100.0, 200.0, 300.0, 601.0]
    assert state.consecutive_blocks == 0


def test_degraded_mode_skips_provider_and_asks_user(tmp_path) -> None:
    """Circuit breaking degrades to rules plus human, never automatic allow."""
    calls = []

    async def complete(_call):
        calls.append("provider")
        return _completed("allow")

    async def approve(_name, _tool_input, details):
        assert details["degraded"] is True
        return "once"

    state = AutoModeState(degraded=True, degraded_reason="block_streak")
    admission = asyncio.run(AutoModeController(True, state=state).admit(
        _context(tmp_path, complete=complete, approve=approve),
        "bash",
        {"command": "python build.py"},
        [],
    ))

    assert admission.allowed is True
    assert admission.source == "human"
    assert calls == []


def test_auto_trace_never_contains_raw_action_or_classifier_prose(tmp_path) -> None:
    """Admission trace keeps evidence without duplicating sensitive payloads."""
    traces = []

    async def complete(_call):
        return _completed("allow", "PRIVATE CLASSIFIER PROSE")

    asyncio.run(AutoModeController(True).admit(
        _context(tmp_path, complete=complete, traces=traces),
        "bash",
        {"command": "python build-with-secret.py"},
        [{"role": "user", "content": "PRIVATE USER TEXT"}],
    ))

    serialized = json.dumps(traces, ensure_ascii=False)
    assert "auto_mode_decision" in serialized
    assert "python build-with-secret.py" not in serialized
    assert "PRIVATE CLASSIFIER PROSE" not in serialized
    assert "PRIVATE USER TEXT" not in serialized


def test_auto_approval_callback_error_fails_closed(tmp_path) -> None:
    """A broken UI adapter rejects the action without aborting the Agent run."""
    async def complete(_call):
        return _completed("block", "requires review")

    async def approve(_name, _tool_input, _details):
        raise RuntimeError("selector unavailable")

    admission = asyncio.run(AutoModeController(True).admit(
        _context(tmp_path, complete=complete, approve=approve),
        "bash",
        {"command": "python build.py"},
        [],
    ))

    assert admission.allowed is False
    assert admission.permission_denied is True
    assert admission.reason_code == "user_reject"


def test_auto_classifier_cancellation_propagates_without_approval(tmp_path) -> None:
    """Run cancellation cannot be normalized into classifier unavailability."""
    async def scenario():
        started = asyncio.Event()
        approvals = []
        state = AutoModeState()

        async def complete(_call):
            started.set()
            await asyncio.Event().wait()

        async def approve(*_args):
            approvals.append(True)
            return "always"

        controller = AutoModeController(True, state=state)
        task = asyncio.create_task(controller.admit(
            _context(tmp_path, complete=complete, approve=approve),
            "bash",
            {"command": "python build.py"},
            [],
        ))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return approvals, state

    approvals, state = asyncio.run(scenario())

    assert approvals == []
    assert state.approved_actions == set()
