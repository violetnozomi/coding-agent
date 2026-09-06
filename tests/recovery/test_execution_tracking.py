"""Execution admission, ownership and model recovery ledger projections."""
from __future__ import annotations

import threading

import pytest

from nz_coder.runtime.execution.tool_executor import ToolExecutor
from nz_coder.state.tool_ledger import ToolLedger
from nz_coder.state.workdir import scoped_workdir


class Permissions:
    def __init__(self, behavior="allow"):
        self.behavior = behavior

    def check(self, name, arguments):
        return {"behavior": self.behavior, "reason": "test denial"}


def batch():
    call = {"id": "call-write", "type": "function", "function": {
        "name": "write_file", "arguments": {"path": "a.txt", "content": "written"},
    }}
    messages = [{"role": "user", "content": "write a.txt then verify", "_nz_message_id": "msg-user"},
                {"role": "assistant", "content": "", "_nz_message_id": "msg-step", "_nz_parts": [
                    {"id": "part-write", "message_id": "msg-step", "type": "tool", "tool": "write_file",
                     "call_id": "call-write", "state": {"status": "running", "input": call["function"]["arguments"]}},
                ]}]
    return call, messages


@pytest.mark.parametrize("behavior,expected", [("allow", "succeeded"), ("deny", "not_executed")])
def test_actual_executor_records_permission_result_and_owned_effects(tmp_path, behavior, expected):
    from nz_coder.runtime.process.checkpoint_runtime import recovery_run, register_batch
    from nz_coder.tools import files  # noqa: F401

    call, messages = batch()
    with scoped_workdir(tmp_path), recovery_run(tmp_path, "session-a") as run:
        run.attach("interaction-a", messages)
        register_batch(messages, [call])
        result = ToolExecutor(Permissions(behavior)).execute_one(call, 0)
        run.overlay(messages)
    row, = ToolLedger(tmp_path).executions("session-a")
    assert row["execution_state"] == expected
    assert result.executed == (behavior == "allow")
    assert (tmp_path / "a.txt").exists() == (behavior == "allow")
    facts = messages[1]["_nz_parts"][0]["state"]["recovery"]
    assert facts["execution_state"] == expected
    assert facts["side_effect_state"] == ("committed" if behavior == "allow" else "none")
    assert facts["execution_id"] == row["execution_id"]


def test_living_owner_cannot_be_misclassified_as_process_lost(tmp_path):
    from nz_coder.runtime.process.checkpoint_runtime import recovery_run, register_batch

    call, messages = batch()
    with recovery_run(tmp_path, "session-a") as run:
        run.attach("interaction-a", messages)
        register_batch(messages, [call])
        errors = []

        def attach_again():
            try:
                with recovery_run(tmp_path, "session-a"):
                    pass
            except Exception as exc:
                errors.append(type(exc).__name__)

        worker = threading.Thread(target=attach_again)
        worker.start()
        worker.join(5)
        assert not worker.is_alive()
        assert errors
        row, = ToolLedger(tmp_path).executions("session-a")
        assert row["terminal_cause"] == "none"


def test_reused_finished_call_cannot_dispatch_twice(tmp_path):
    from nz_coder.runtime.process.checkpoint_runtime import recovery_run, register_batch
    from nz_coder.tools import files  # noqa: F401

    call, messages = batch()
    with scoped_workdir(tmp_path), recovery_run(tmp_path, "session-a") as run:
        run.attach("interaction-a", messages)
        register_batch(messages, [call])
        executor = ToolExecutor(Permissions())
        assert executor.execute_one(call, 0).executed
        assert not executor.execute_one(call, 0).executed
    assert len(ToolLedger(tmp_path).mutations("session-a")) == 1


def test_large_recovery_facts_use_existing_readable_artifact(tmp_path):
    from nz_coder.runtime.process.checkpoint_runtime import recovery_run
    from nz_coder.runtime.conversation.message_projection import project_provider_messages
    from nz_coder.tool_platform.artifacts import ArtifactStore
    import json

    ledger = ToolLedger(tmp_path)
    for index in range(30):
        ledger.register(session_id="session-a", interaction_id="interaction-old", assistant_step_id="msg-old",
                        agent_id="agent-old", call_id=f"call-{index}", tool="write_file",
                        tool_input={"path": f"file-{index}.txt", "content": "x" * 200})
    messages = [{"role": "user", "content": "Continue the incomplete task.", "_nz_message_id": "msg-resume"}]
    with recovery_run(tmp_path, "session-a") as run:
        run.attach("interaction-new", messages)
    reference = messages[0]["_nz_tool_recovery_archive"]
    facts = json.loads(ArtifactStore(tmp_path, "session-a").read(reference))
    assert len(facts) == 30 and {f["call_id"] for f in facts} == {f"call-{i}" for i in range(30)}
    projected = project_provider_messages(messages)
    assert reference in json.dumps(projected)
    assert "read_tool_result" in json.dumps(projected)
    assert project_provider_messages(messages) == projected


def test_cancel_before_dispatch_never_starts_registered_write(tmp_path):
    from nz_coder.runtime.process.checkpoint_runtime import recovery_run, register_batch
    from nz_coder.tools import scoped_tool_cancellation
    from nz_coder.tools import files  # noqa: F401

    call, messages = batch()
    cancelled = threading.Event()
    cancelled.set()
    with scoped_workdir(tmp_path), recovery_run(tmp_path, "session-a") as run, scoped_tool_cancellation(cancelled):
        run.attach("interaction-a", messages)
        register_batch(messages, [call])
        result = ToolExecutor(Permissions()).execute_one(call, 0)
    assert not result.executed and not (tmp_path / "a.txt").exists()
    row, = ToolLedger(tmp_path).executions("session-a")
    assert row["execution_state"] == "not_executed" and row["terminal_cause"] == "user_cancelled"


@pytest.mark.parametrize("admitted", [0, 1])
def test_projection_and_archive_require_public_preview_admission(tmp_path, admitted):
    import json
    from nz_coder.runtime.process.checkpoint_runtime import RecoveryRun
    from nz_coder.runtime.conversation.message_projection import project_provider_messages
    from nz_coder.tool_platform.artifacts import ArtifactStore

    ledger = ToolLedger(tmp_path)
    for index in range(8):
        ledger.register(session_id="session-a", interaction_id="old", assistant_step_id="msg-old",
                        agent_id="old", call_id=f"call-{index}", tool="read_file", tool_input={"path": "a.txt"})
    with ledger.connection() as db:
        db.execute("UPDATE executions SET execution_state='succeeded',result_preview='PREVIEW_SENTINEL',preview_admitted=?", (admitted,))
    # Do not reopen: readers enforce admission independently of startup cleanup.
    messages = [{"role": "user", "content": "continue", "_nz_message_id": "msg-resume",
                 "_nz_continuation": {"version": 1, "status": "interrupted", "summary": "Verify pending work."}}]
    RecoveryRun(ledger, "session-a").attach("new", messages)
    projected = project_provider_messages(messages)
    archive = ArtifactStore(tmp_path, "session-a").read(messages[0]["_nz_tool_recovery_archive"])
    assert ("PREVIEW_SENTINEL" in json.dumps(projected)) == bool(admitted)
    assert ("PREVIEW_SENTINEL" in archive) == bool(admitted)
