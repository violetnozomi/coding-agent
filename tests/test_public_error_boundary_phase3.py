"""Regression coverage for the typed public-error trust boundary."""
from __future__ import annotations

import json
from io import StringIO
from types import SimpleNamespace

from rich.console import Console

from nz_coder.http_service.manager import SessionManager
from nz_coder.interface.run_renderer import TerminalRunRenderer
from nz_coder.protocol.message_schema import (
    attach_message_identity,
    set_assistant_error,
)
from nz_coder.protocol.public_error import (
    PUBLIC_ERROR_SCHEMA,
    PublicError,
    public_error_message,
    to_public_error,
)
from nz_coder.protocol.session_events import SessionEventBus
from nz_coder.runtime.session.session_processor import SessionProcessor
from nz_coder.state.trace import TraceRecorder


_SECRET = (
    'request failed: Authorization=Bearer SECRET-123 '
    'body={"prompt":"PRIVATE-PROMPT"}'
)
_MARKERS = ("SECRET-123", "PRIVATE-PROMPT", "Authorization")


def _assert_private_diagnostic_absent(value: object) -> None:
    rendered = json.dumps(value, ensure_ascii=False, default=str)
    assert all(marker not in rendered for marker in _MARKERS)


class _Stream:
    def set_status(self, _value) -> None:  # noqa: ANN001
        pass

    def pause(self) -> None:
        pass

    def resume(self) -> None:
        pass


def _renderer(*, remote: bool = False) -> tuple[TerminalRunRenderer, StringIO]:
    output = StringIO()
    view = TerminalRunRenderer(
        Console(file=output, force_terminal=False, width=100),
        _Stream(),
    )
    if remote:
        view.begin_remote(SimpleNamespace(model_id="provider/model"))
    else:
        view.begin(SimpleNamespace(model_id="provider/model"))
    return view, output


def test_untrusted_string_is_not_public_error_message():
    public = to_public_error(_SECRET)

    assert public.code == "internal_error"
    assert public.message == "An internal error occurred."
    _assert_private_diagnostic_absent(public.to_dict())


def test_unmarked_error_dict_is_not_trusted():
    assert public_error_message({"message": _SECRET}) == "Request failed."


def test_public_error_wire_schema_is_accepted():
    wire = PublicError(
        code="provider_error",
        message="The provider request failed.",
        retryable=True,
    ).to_dict()

    assert wire["schema"] == PUBLIC_ERROR_SCHEMA
    assert public_error_message(wire) == "The provider request failed."


def test_provider_secret_not_present_in_assistant_error():
    assistant = {"role": "assistant", "content": ""}
    attach_message_identity(assistant, session_id="session-secret")

    set_assistant_error(assistant, _SECRET, name="APIError")

    _assert_private_diagnostic_absent(assistant)


def test_provider_secret_not_present_in_tool_error():
    assistant = {"role": "assistant", "content": ""}
    attach_message_identity(assistant, session_id="session-secret")
    processor = SessionProcessor(assistant)
    processor.start_step()
    processor.register_tool_calls([
        {
            "id": "call-secret",
            "function": {"name": "bash", "arguments": "{}"},
        }
    ])

    processor.fail_tool("call-secret", _SECRET)

    _assert_private_diagnostic_absent(assistant)


def test_provider_secret_not_present_in_session_event():
    bus = SessionEventBus(session_id="session-secret")
    try:
        event = bus.publish("session.run.failed", {"error": _SECRET})
        _assert_private_diagnostic_absent(event.to_dict())
    finally:
        bus.close()


def test_provider_secret_not_present_in_event_journal(tmp_path):
    journal = tmp_path / "events.jsonl"
    bus = SessionEventBus(
        session_id="session-secret",
        replay_capacity=8,
        journal_path=journal,
    )
    bus.publish("session.run.failed", {"error": _SECRET})
    bus.close()

    _assert_private_diagnostic_absent(journal.read_text(encoding="utf-8"))


def test_provider_secret_not_present_in_trace(tmp_path):
    recorder = TraceRecorder(trace_dir=tmp_path / "traces")
    recorder.log(
        "api_error",
        error=_SECRET,
        responseBody=_SECRET,
        responseHeaders={"Authorization": "Bearer SECRET-123"},
    )

    _assert_private_diagnostic_absent(recorder.path.read_text(encoding="utf-8"))


def test_provider_secret_not_present_in_http_snapshot(tmp_path):
    class Agent:
        provider_id = "provider"
        model_id = "model"
        model_variant = None

        def __init__(self, session_id: str):
            self.event_bus = SessionEventBus(session_id=session_id)

        def set_interaction_askers(self, **_kwargs) -> None:
            pass

        def close(self) -> None:
            self.event_bus.close()

    manager = SessionManager(
        agent_factory=lambda session_id, _mode: Agent(session_id),
        workspace_roots=[tmp_path],
        restore_saved=False,
    )
    try:
        session = manager.get(manager.create()["id"])
        assistant = {"role": "assistant", "content": ""}
        attach_message_identity(assistant, session_id=session.session_id)
        assistant["_nz_error"] = _SECRET
        assistant["_nz_assistant_error"] = {
            "name": "APIError",
            "data": {"message": _SECRET, "isRetryable": False},
        }
        session.history.append(assistant)

        _assert_private_diagnostic_absent(session.attach_snapshot())
    finally:
        manager.close()


def test_provider_secret_not_rendered_locally():
    view, output = _renderer()

    view._render_assistant_error({
        "name": "APIError",
        "data": {"message": _SECRET, "isRetryable": False},
    })

    _assert_private_diagnostic_absent(output.getvalue())


def test_provider_secret_not_rendered_remotely():
    view, output = _renderer(remote=True)
    view.feed({
        "type": "message.updated",
        "properties": {
            "message_id": "msg-secret",
            "info": {
                "id": "msg-secret",
                "role": "assistant",
                "error": {
                    "name": "APIError",
                    "data": {"message": _SECRET, "isRetryable": False},
                },
            },
        },
        "meta": {
            "schema_version": 1,
            "event_id": "event-secret",
            "sequence": 1,
            "timestamp": 1.0,
            "session_id": "session-secret",
            "run_id": "run-secret",
            "agent_id": "agent-secret",
        },
    })
    view.feed({
        "type": "session.run.failed",
        "properties": {"error": {"message": _SECRET}},
        "meta": {
            "schema_version": 1,
            "event_id": "event-terminal",
            "sequence": 2,
            "timestamp": 2.0,
            "session_id": "session-secret",
            "run_id": "run-secret",
            "agent_id": "agent-secret",
        },
    })
    view.finish({"status": "failed"})

    _assert_private_diagnostic_absent(output.getvalue())
