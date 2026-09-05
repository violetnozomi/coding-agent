"""Direct child failures remain typed and secret-safe on every public surface."""
from __future__ import annotations


def test_child_exception_projection_is_secret_safe_across_result_trace_and_event(tmp_path):
    from nz_coder.protocol.public_error import to_public_error
    from nz_coder.protocol.session_events import SessionEventBus
    from nz_coder.runtime.agent.child_result import child_result_from_state
    from nz_coder.state.trace import TraceRecorder

    secret = "Authorization=Bearer sentinel-child-secret"
    public = to_public_error(RuntimeError(secret))
    state = {
        "session_id": "child-session",
        "agent_id": "child-agent",
        "public_error": public.to_dict(),
    }
    child = child_result_from_state(
        state,
        final_text=f"Subagent error: {public.message}",
        status="error",
    )
    tracer = TraceRecorder(trace_dir=tmp_path / "traces")
    tracer.log("run_error", public_error=public.to_dict())
    bus = SessionEventBus(session_id="parent-session")
    try:
        event = bus.publish("subagent.failed", {
            "child_result": child.to_dict(),
            "public_error": public.to_dict(),
        })
    finally:
        bus.close()

    combined = repr(child.to_metadata()) + repr(event.to_dict())
    combined += tracer.path.read_text(encoding="utf-8")
    assert "sentinel-child-secret" not in combined
    assert child.public_error is not None
    assert child.public_error["code"] == "internal_error"
    assert child.final_text == "Subagent error: An internal error occurred."
