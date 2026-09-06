"""Recovery budget must expose unresolved effects before old harmless history."""
from __future__ import annotations

import copy
import json
import re

import pytest

from tests.recovery.test_model_recovery import _history, _tool, _recovery, _records, _recovery_text, _native_request, _document_resume_history
from nz_coder.runtime.conversation.message_projection import project_provider_messages


def _old(execution="not_executed", effect="none"):
    return [_tool(f"call-old-{i}", "error", recovery=_recovery(
        execution_id=f"exec-old-{i}", attempt_id=f"attempt-old-{i}", sequence=i,
        execution_state=execution, terminal_cause="permission_denied", side_effect_state=effect,
    )) for i in range(30)]


@pytest.mark.parametrize("old_state", ["not_executed", "failed"])
@pytest.mark.parametrize("latest_state", ["uncertain", "succeeded"])
def test_latest_unknown_effect_survives_old_refusal_and_failure_budget(old_state, latest_state):
    latest = _tool("call-latest", "completed" if latest_state == "succeeded" else "error", recovery=_recovery(
        execution_id="exec-latest", sequence=40, execution_state=latest_state,
        terminal_cause="process_lost", side_effect_state="unknown",
    ))
    records = _records(project_provider_messages(_history([*_old(old_state), latest])))
    assert next(iter(records)) == "call-latest"
    assert records["call-latest"]["side_effect_state"] == "unknown"


@pytest.mark.parametrize("settled", ["compensated", "reverted"])
def test_unresolved_outweighs_recent_settled_effects_and_relevance_breaks_ties(settled):
    older = _tool("call-relevant", "completed", recovery=_recovery(sequence=31,
        execution_id="exec-relevant", execution_state="succeeded", side_effect_state="unknown",
        files=[{"path": "parser.py", "state": "unknown"}]))
    newer = _tool("call-unrelated", "error", recovery=_recovery(sequence=32,
        execution_id="exec-unrelated", files=[{"path": "unrelated.txt", "state": "unknown"}]))
    newer["state"]["input"] = {"path": "unrelated.txt"}
    parts = [older, newer, *_old("failed", settled)]
    records = _records(project_provider_messages(_history(parts)))
    assert list(records)[:2] == ["call-relevant", "call-unrelated"]


def test_explicit_new_target_is_not_overridden_by_old_continuation_goal():
    parts = [_tool("call-parser", "running", recovery=_recovery(sequence=40,
                  files=[{"path": "parser.py", "state": "unknown"}])),
             _tool("call-other", "running", recovery=_recovery(sequence=39,
                  files=[{"path": "unrelated.txt", "state": "unknown"}]))]
    parts[1]["state"]["input"] = {"path": "unrelated.txt"}
    history = _history(parts)
    history[-1]["content"] = "Repair unrelated.txt now."
    assert next(iter(_records(project_provider_messages(history)))) == "call-other"


def test_many_wide_unknown_calls_keep_identities_before_detail_and_report_exact_omission():
    parts = [_tool(f"call-risk-{i}", "error", recovery=_recovery(
        execution_id=f"exec-{i}", sequence=i,
        files=[{"path": "long/" * 1000, "state": "unknown", "operation_id": f"op-{i}"}],
    )) for i in range(50)]
    for part in parts:
        part["state"]["input"] = {"value": "<>&" * 2000}
    history = _history(parts)
    history[-1]["_nz_tool_recovery_archive"] = "artifact_" + "a" * 32
    original = copy.deepcopy(history)
    request = project_provider_messages(history)
    records = _records(request)
    text = _recovery_text(request)
    assert list(records)[0] == "call-risk-49"
    assert len(records) >= 8, "long details must not consume the identity budget"
    omitted = int(re.search(r"Unresolved tool facts omitted: (\d+)", text)[1])
    assert omitted == 50 - len(records) > 0
    assert "read_tool_result artifact_id=artifact_" in text
    assert len(text) <= 6000
    assert project_provider_messages(history) == request == project_provider_messages(request)
    assert history == original


@pytest.mark.parametrize("archive", [None, "unavailable: quota"])
def test_omitted_risk_does_not_promise_missing_archive(archive):
    history = _history([_tool(f"call-{i}", "running", recovery=_recovery(sequence=i)) for i in range(60)])
    if archive:
        history[-1]["_nz_tool_recovery_archive"] = archive
    text = _recovery_text(project_provider_messages(history))
    assert "artifact unavailable" in text
    assert "read_tool_result artifact_id=" not in text
    assert int(re.search(r"Unresolved tool facts omitted: (\d+)", text)[1]) > 0


def test_first_native_request_recovers_recent_unknown_write_from_durable_ledger(monkeypatch, tmp_path):
    from nz_coder.state.tool_ledger import ToolLedger
    from nz_coder.runtime.process.checkpoint_runtime import checkpoint_execution
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess

    ledger = ToolLedger(tmp_path)
    for index in range(30):
        ledger.register(session_id="recovery-session", interaction_id="old-interaction", assistant_step_id="msg-old",
                        agent_id="old-invocation", call_id=f"call-denied-{index}", tool="write_file", tool_input={"path": "old.txt"})
    with ledger.connection() as db:
        db.execute("UPDATE executions SET execution_state='not_executed',terminal_cause='permission_denied'")
    row = ledger.register(session_id="recovery-session", interaction_id="prior-task-interaction", assistant_step_id="msg-latest",
                          agent_id="prior-invocation", call_id="call-recent-write", tool="write_file", tool_input={"path": "parser.py"})
    with ledger.connection() as db:
        db.execute("UPDATE executions SET execution_state='running' WHERE execution_id=?", (row["execution_id"],))
    with monkeypatch.context() as patch:
        patch.setattr(ledger, "confirm_mutation", lambda *_a: (_ for _ in ()).throw(OSError("after receipt lost")))
        with checkpoint_execution(ledger, row["execution_id"]), pytest.raises(OSError):
            WorkspaceFileAccess(tmp_path).write_bytes("parser.py", b"published")
    request = _native_request(monkeypatch, tmp_path, _history([]))
    recent = _records(request)["call-recent-write"]
    assert (recent["execution_state"], recent["terminal_cause"], recent["side_effect_state"]) == ("uncertain", "process_lost", "unknown")
    assert "read_tool_result" in _recovery_text(request)
    assert (tmp_path / "parser.py").read_bytes() == b"published"
    assert not any(message.get("role") == "tool" for message in request)


@pytest.mark.parametrize("compacted", [False, True])
def test_priority_survives_document_and_carried_compaction_history(compacted):
    from nz_coder.protocol.tool_recovery_parts import carry_tool_recovery_parts

    history = _document_resume_history()
    history[1]["_nz_parts"] = [*_old(), _tool("call-document-risk", "running", recovery=_recovery(sequence=40))]
    if compacted:
        summary = {"role": "user", "content": "Goal: repair parser.py; verify tests", "_nz_message_id": "msg-summary",
                   "_nz_compaction": {"archive": "user-state://transcripts/archive"}}
        carry_tool_recovery_parts(history[:-2], summary, session_id="recovery-session")
        history = [summary, *history[-2:]]
    request = project_provider_messages(history)
    assert "call-document-risk" in _records(request)
    assert len(_recovery_text(request)) <= 6000
    assert "Extracted document text" in json.dumps(request)
    assert project_provider_messages(history) == request


@pytest.mark.parametrize("marker_index", [0, -1])
def test_first_native_request_does_not_promise_stale_archive_for_small_facts(monkeypatch, tmp_path, marker_index):
    from nz_coder.tool_platform.artifacts import ArtifactAccessError, ArtifactStore

    history = _history([_tool("call-risk", "running", recovery=_recovery())])
    missing = "artifact_" + "f" * 32
    history[marker_index]["_nz_tool_recovery_archive"] = missing
    with pytest.raises(ArtifactAccessError):
        ArtifactStore(tmp_path, "recovery-session").read(missing)
    request = _native_request(monkeypatch, tmp_path, history)
    assert "call-risk" in _records(request)
    text = _recovery_text(request)
    assert "artifact unavailable" in text
    assert "read_tool_result artifact_id=" not in text


@pytest.mark.parametrize("effect,files", [
    ("conflict", []), ("committed", [{"path": "parser.py", "state": "conflict"}]),
    ("committed", [{"path": f"safe-{i}.py", "state": "committed"} for i in range(3)]
     + [{"path": "parser.py", "state": "conflict"}]),
    ("committed", [{"path": f"safe-{i}.py", "state": "committed"} for i in range(3)]
     + [{"path": "parser.py", "state": "unknown"}]),
])
def test_conflicted_effect_keeps_high_risk_identity_before_old_failures(effect, files):
    latest = _tool("call-conflict", "completed", recovery=_recovery(
        sequence=61, execution_state="succeeded", side_effect_state=effect, files=files,
    ))
    request = project_provider_messages(_history([*_old("failed"), latest]))
    assert next(iter(_records(request))) == "call-conflict"
    assert "Unresolved tool facts omitted: 0 of 1" in _recovery_text(request)
