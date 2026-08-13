"""Traceable manifest for bounded terminal product stress evidence."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProductStressCase:
    """One required boundary and the executable test that owns its evidence."""

    case_id: str
    evidence: str


def product_stress_manifest() -> tuple[ProductStressCase, ...]:
    """Map every final-product stress requirement to an executable contract."""
    cases = {
        "width-resize-stream": "test_fullscreen.py + test_process_service.py",
        "cjk-emoji-ansi-binary": "test_terminal_product_stress.py",
        "logs-100k-1m": "test_terminal_product_stress.py",
        "large-transcript": "test_fullscreen.py::test_fullscreen_transcript_and_sidebar_are_bounded",
        "sessions-1k": "test_terminal_product_stress.py",
        "files-10k": "test_terminal_product_stress.py",
        "multiline-paste-10k": "test_terminal_product_stress.py + test_smoke.py",
        "bracketed-paste": "test_smoke.py::test_cli_drains_multiline_paste",
        "ctrl-c-input": "test_terminal_input.py",
        "ctrl-c-agent": "test_cancellation_safety.py",
        "double-ctrl-c": "test_terminal_input.py",
        "ctrl-d": "test_terminal_interactions.py",
        "queue-during-run": "test_loop_fake.py::test_queued_followup_stops_before_next_provider_step",
        "history-search": "test_terminal_input.py",
        "external-editor-failure": "test_terminal_interactions.py",
        "background-agents": "test_agent_manager.py",
        "persistent-processes": "test_process_service.py",
        "disconnect-reconnect-loop": "test_http_service.py::test_http_client_survives_three_delayed_disconnects_without_duplicates",
        "slow-network": "test_http_service.py::test_http_client_survives_three_delayed_disconnects_without_duplicates",
        "event-gap": "test_http_service.py::test_resilient_client_rebaselines_after_an_explicit_gap",
        "permission-reconnect": "test_http_service.py::test_http_permission_request_reply_and_late_reply_boundary",
        "question-reconnect": "test_http_service.py::test_http_question_reply_validation_reject_and_abort",
        "process-reconnect": "test_http_service.py::test_remote_session_controls_two_persistent_processes_by_identity",
        "child-reconnect": "test_http_service.py::test_remote_child_running_disconnect_then_completed_reconnect",
        "two-clients": "test_http_service.py::test_two_attached_clients_receive_same_events_and_one_permission_effect",
        "daemon-restart": "test_daemon.py::test_daemon_restart_preserves_workspace_sessions_and_rotates_token",
    }
    return tuple(ProductStressCase(name, evidence) for name, evidence in cases.items())


__all__ = ["ProductStressCase", "product_stress_manifest"]
