"""Tests for additive persistent message and text-part identities."""
from __future__ import annotations

from nz_coder.message_schema import (
    assistant_error_from_exception,
    attach_message_identity,
    attach_text_part,
    bind_assistant_context,
    bind_user_context,
    ensure_message_identities,
    is_synthetic_user_message,
    legacy_messages,
    message_records,
    set_assistant_error,
    set_assistant_end_state,
    stamp_user_message,
)


def test_synthetic_user_detection_supports_marker_and_legacy_sessions():
    assert is_synthetic_user_message({
        "role": "user", "content": "internal", "_nz_synthetic": True,
    })
    assert is_synthetic_user_message({
        "role": "user", "content": "<reminder>legacy internal prompt</reminder>",
    })
    assert not is_synthetic_user_message({
        "role": "user", "content": "please review the repository",
    })


def test_legacy_messages_receive_deterministic_identity_and_text_part():
    first = [{"role": "user", "content": "hello"}]
    second = [{"role": "user", "content": "hello"}]

    ensure_message_identities(first, "session-a")
    ensure_message_identities(second, "session-a")

    assert first == second
    record = message_records(first, "session-a")[0]
    assert record["info"]["id"].startswith("msg-")
    assert record["parts"][0]["id"].startswith("part-")
    assert record["parts"][0]["text"] == "hello"


def test_user_created_time_is_typed_private_and_evidence_backed():
    live = bind_user_context(
        stamp_user_message(
            {
                "role": "user",
                "content": "new request",
                "time": {"created": 9999},
                "agent": "spoof",
                "model": {"provider_id": "spoof", "model_id": "spoof"},
            },
            created=123.5,
        ),
        agent="build",
        provider_id="openrouter",
        model_id="vendor/code-model",
        variant="high",
    )
    legacy = {"role": "user", "content": "old request", "_timestamp": 42}
    unknown = {"role": "user", "content": "unknown", "time": {"created": 777}}

    records = message_records([live, legacy, unknown], "session-a")

    assert records[0]["info"]["time"] == {"created": 123.5}
    assert records[0]["info"]["agent"] == "build"
    assert records[0]["info"]["model"] == {
        "provider_id": "openrouter",
        "model_id": "vendor/code-model",
        "variant": "high",
    }
    assert records[1]["info"]["time"] == {"created": 42.0}
    assert "time" not in records[2]["info"]
    assert legacy_messages([live])[0]["time"] == {"created": 9999}
    assert legacy_messages([live])[0]["agent"] == "spoof"


def test_invalid_persisted_identity_and_part_are_not_exposed():
    messages = [{
        "role": "assistant",
        "content": "safe",
        "_nz_message_id": "msg-valid-but-copied",
        "_nz_session_id": "another-session",
        "_nz_secret": "must-not-leak",
        "_nz_parts": [{
            "id": "bad/id",
            "message_id": "../../escape",
            "type": "text",
            "text": "unsafe",
        }],
    }]

    ensure_message_identities(messages, "session-a")
    record = message_records(messages, "session-a")[0]

    assert record["info"]["id"].startswith("msg-")
    assert record["info"]["id"] != "msg-valid-but-copied"
    assert "_nz_secret" not in record["info"]
    assert "_nz_secret" not in legacy_messages(messages)[0]
    assert record["parts"][0]["message_id"] == record["info"]["id"]
    assert record["parts"][0]["text"] == "safe"


def test_live_identity_is_hidden_from_legacy_projection():
    message = {"role": "assistant", "content": "done"}
    message_id = attach_message_identity(message, session_id="session-a")
    attach_text_part(message, {
        "id": "part-live",
        "message_id": message_id,
        "type": "text",
        "text": "done",
    })

    assert legacy_messages([message]) == [{"role": "assistant", "content": "done"}]
    record = message_records([message], "session-a")[0]
    assert record["info"]["id"] == message_id
    assert record["parts"][0]["id"] == "part-live"


def test_assistant_finish_and_typed_error_are_projected_without_legacy_breakage():
    message = {"role": "assistant", "content": "partial"}
    attach_message_identity(message, "msg-error", session_id="session-a")
    message["_nz_finish"] = "error"
    set_assistant_error(
        message,
        "rate limited",
        name="APIError",
        data={
            "message": "rate limited",
            "statusCode": 429,
            "isRetryable": True,
            "responseHeaders": {
                "retry-after": "2",
                "set-cookie": "private-session",
            },
        },
    )

    record = message_records([message], "session-a")[0]

    assert record["info"]["finish"] == "error"
    assert record["info"]["error"] == {
        "name": "APIError",
        "data": {
            "message": "rate limited",
            "statusCode": 429,
            "isRetryable": True,
            "responseHeaders": {
                "retry-after": "2",
                "set-cookie": "[REDACTED]",
            },
        },
    }
    assert legacy_messages([message]) == [
        {"role": "assistant", "content": "partial"}
    ]


def test_assistant_cost_projection_accepts_finite_values_and_hides_invalid_values():
    valid = {"role": "assistant", "content": "done", "_nz_cost": 0.125}
    invalid = {"role": "assistant", "content": "bad", "_nz_cost": float("inf")}

    records = message_records([valid, invalid], "session-a")

    assert records[0]["info"]["cost"] == 0.125
    assert "cost" not in records[1]["info"]


def test_assistant_end_state_is_private_typed_and_immutable():
    message = {
        "role": "assistant",
        "content": "done",
        "end_state": {"reason": "errored"},
    }

    assert set_assistant_end_state(message, "completed") == {"reason": "completed"}
    assert set_assistant_end_state(message, "errored") == {"reason": "completed"}

    info = message_records([message], "session-a")[0]["info"]
    assert info["end_state"] == {"reason": "completed"}


def test_assistant_model_provider_and_tokens_are_projected_from_private_owner():
    message = bind_assistant_context({
        "role": "assistant",
        "content": "done",
        "provider_id": "spoofed",
        "model_id": "spoofed",
        "tokens": {"input": 999},
        "mode": "spoofed",
        "agent": "spoofed",
        "path": {"cwd": "/spoof", "root": "/spoof"},
        "variant": "spoofed",
        "_nz_provider_id": "openrouter",
        "_nz_model_id": "vendor/code-model",
        "_nz_usage": {
            "input": 80,
            "output": 30,
            "total": 140,
            "reasoning": 10,
            "cache_read": 20,
            "cache_write": 2,
        },
    }, mode="build", agent="build", cwd="/workspace", root="/workspace", variant="high")

    info = message_records([message], "session-a")[0]["info"]

    assert info["provider_id"] == "openrouter"
    assert info["model_id"] == "vendor/code-model"
    assert info["tokens"] == {
        "input": 80,
        "output": 30,
        "total": 140,
        "reasoning": 10,
        "cache": {"read": 20, "write": 2},
    }
    assert info["mode"] == "build"
    assert info["agent"] == "build"
    assert info["path"] == {"cwd": "/workspace", "root": "/workspace"}
    assert info["variant"] == "high"


def test_legacy_assistant_lineage_and_time_migrate_from_real_user_and_parts():
    messages = [
        {"role": "user", "content": "solve"},
        {
            "role": "assistant",
            "content": "first",
            "_nz_message_id": "msg-first",
            "_nz_session_id": "session-a",
            "_nz_parts": [{
                "id": "part-step",
                "message_id": "msg-first",
                "type": "step-finish",
                "reason": "stop",
                "time": {"start": 10.0, "end": 12.0},
            }],
        },
        {"role": "user", "content": "<reminder>internal</reminder>"},
        {"role": "assistant", "content": "second", "_timestamp": 13.0},
    ]

    records = message_records(messages, "session-a")

    user_id = records[0]["info"]["id"]
    assert records[1]["info"]["parent_id"] == user_id
    assert records[3]["info"]["parent_id"] == user_id
    assert records[1]["info"]["time"] == {"created": 10.0, "completed": 12.0}
    assert records[3]["info"]["time"] == {"created": 13.0}


def test_legacy_assistant_error_migrates_from_step_finish_reason():
    message = {
        "role": "assistant",
        "content": "",
        "_nz_error": "Request interrupted by user",
        "_nz_message_id": "msg-cancelled",
        "_nz_session_id": "session-a",
        "_nz_parts": [{
            "id": "part-finish",
            "message_id": "msg-cancelled",
            "type": "step-finish",
            "reason": "cancelled",
            "tokens": {"input": 0, "output": 0, "total": 0},
            "time": {"start": 1.0, "end": 2.0},
        }],
    }

    record = message_records([message], "session-a")[0]

    assert record["info"]["finish"] == "cancelled"
    assert record["info"]["error"] == {
        "name": "MessageAbortedError",
        "data": {"message": "Request interrupted by user"},
    }


def test_provider_exception_normalization_preserves_identity_and_auth_boundary():
    class ProviderFailure(Exception):
        status_code = 429
        code = "rate_limit"
        headers = {"retry-after": "3", "x-api-key": "secret"}
        body = {"error": "slow down"}

    retry = assistant_error_from_exception(
        ProviderFailure("too many requests"),
        provider_id="demo",
    )
    auth = ProviderFailure("expired")
    auth.status_code = 401

    assert retry == {
        "name": "APIError",
        "data": {
            "message": "too many requests",
            "isRetryable": True,
            "statusCode": 429,
            "responseHeaders": {
                "retry-after": "3",
                "x-api-key": "[REDACTED]",
            },
            "metadata": {"name": "ProviderFailure", "code": "rate_limit"},
            "responseBody": '{"error": "slow down"}',
        },
    }
    assert assistant_error_from_exception(auth, provider_id="demo") == {
        "name": "ProviderAuthError",
        "data": {"providerID": "demo", "message": "expired"},
    }


def test_duplicate_persisted_message_and_part_ids_are_normalized():
    messages = [
        {
            "role": "assistant",
            "content": "first",
            "_nz_message_id": "msg-duplicate",
            "_nz_session_id": "session-a",
            "_nz_parts": [
                {
                    "id": "part-duplicate",
                    "message_id": "msg-duplicate",
                    "type": "text",
                    "text": "first",
                },
                {
                    "id": "part-duplicate",
                    "message_id": "msg-duplicate",
                    "type": "text",
                    "text": "duplicate",
                },
            ],
        },
        {
            "role": "assistant",
            "content": "second",
            "_nz_message_id": "msg-duplicate",
            "_nz_session_id": "session-a",
        },
    ]

    ensure_message_identities(messages, "session-a")
    records = message_records(messages, "session-a")

    assert records[0]["info"]["id"] == "msg-duplicate"
    assert records[1]["info"]["id"] != "msg-duplicate"
    assert len(records[0]["parts"]) == 1


def test_compaction_and_input_expansion_metadata_are_projected_as_parts():
    messages = [
        {
            "role": "user",
            "content": "<session-summary>summary</session-summary>",
            "_nz_compaction": {
                "auto": True,
                "overflow": True,
                "resume": True,
                "tail_start_id": "msg-tail",
            },
            "_nz_input_expansions": [{
                "kind": "file",
                "source": "notes.txt",
                "originalBytes": 100,
                "originalTokens": 25,
                "resolved": True,
                "compacted": True,
                "text": "must not leak through metadata",
            }],
        },
        {
            "role": "tool",
            "content": "[Earlier tool result compacted.]",
            "_nz_tool_compacted_at": 123.0,
        },
    ]

    records = message_records(messages, "session-a")

    summary_parts = records[0]["parts"]
    compaction = next(part for part in summary_parts if part["type"] == "compaction")
    text = next(part for part in summary_parts if part["type"] == "text")
    assert compaction["tail_start_id"] == "msg-tail"
    assert compaction["auto"] is True
    assert text["metadata"]["input_expansions"][0]["source"] == "notes.txt"
    assert "text" not in text["metadata"]["input_expansions"][0]
    assert records[1]["parts"][0]["time"]["compacted"] == 123.0


def test_structured_compaction_metadata_survives_session_save_and_load(tmp_path, monkeypatch):
    from nz_coder import sessions
    from nz_coder.runtime.workdir import scoped_workdir

    monkeypatch.setattr(sessions, "_active_model_id", lambda: "fake-model")
    messages = [{
        "role": "user",
        "content": "<session-summary>summary</session-summary>",
        "_nz_compaction": {
            "auto": True,
            "overflow": False,
            "tail_start_id": "msg-tail",
            "archive": ".nz-coder/transcripts/transcript.jsonl",
            "head_message_ids": ["msg-old"],
        },
    }]

    with scoped_workdir(tmp_path):
        sessions.save_session(messages, session_id="structured-session")
        loaded = sessions.load_session("structured-session")
        records = message_records(loaded["messages"], "structured-session")

    assert loaded["messages"][0]["_nz_compaction"]["archive"].endswith("transcript.jsonl")
    marker = next(part for part in records[0]["parts"] if part["type"] == "compaction")
    assert marker["tail_start_id"] == "msg-tail"


def test_snapshot_summaries_project_and_persist_session_diff(tmp_path, monkeypatch):
    from nz_coder import sessions
    from nz_coder.runtime.workdir import scoped_workdir

    monkeypatch.setattr(sessions, "_active_model_id", lambda: "fake-model")
    diff = {
        "file": "app.py",
        "patch": "--- a/app.py\n+++ b/app.py\n@@\n-old\n+new\n",
        "additions": 1,
        "deletions": 1,
        "status": "modified",
    }
    messages = [
        {
            "role": "user",
            "content": "update app",
            "_nz_summary": {"diffs": [{key: value for key, value in diff.items() if key != "patch"}]},
        },
        {
            "role": "assistant",
            "content": "done",
            "_nz_session_summary": {
                "additions": 1,
                "deletions": 1,
                "files": 1,
                "diffs": [diff],
            },
        },
    ]

    records = message_records(messages, "summary-session")
    assert records[0]["info"]["summary"]["diffs"] == [{
        "file": "app.py",
        "additions": 1,
        "deletions": 1,
        "status": "modified",
    }]

    with scoped_workdir(tmp_path):
        sessions.save_session(messages, session_id="summary-session")
        loaded = sessions.load_session("summary-session")
        persisted_diff = sessions.load_session_diff("summary-session")

    assert loaded["summary"] == {"additions": 1, "deletions": 1, "files": 1}
    assert persisted_diff == [diff]
