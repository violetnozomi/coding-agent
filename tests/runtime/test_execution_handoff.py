"""下一动作必须由真实依据提交，且只在实际批次落定后兑现。"""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, replace

import pytest

from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
from nz_coder.runtime.execution.runtime_state import RuntimeState
from nz_coder.tool_platform.execution import ToolExecutionResult


def _case(tmp_path):
    (tmp_path / "api.py").write_text("old\n")
    _, identity = WorkspaceFileAccess(tmp_path).read_bytes_with_identity("api.py")
    state = RuntimeState(initial_task_text="repair api", task_authority_epoch=2)
    messages = [
        {"role": "user", "content": "repair api", "_nz_message_id": "msg-task"},
        {"role": "assistant", "_nz_message_id": "msg-read", "tool_calls": [
            {"id": "read-1", "function": {"name": "read_file", "arguments": '{"path":"api.py"}'}},
        ], "_nz_parts": [{"type": "tool", "call_id": "read-1", "tool": "read_file", "state": {
            "status": "completed", "input": {"path": "api.py"}, "metadata": {
                "model_read_observation": {"path": "api.py", "identity": asdict(identity), "offset": 1, "limit": 10, "complete": True},
            },
        }}]},
        {"role": "tool", "tool_call_id": "read-1", "content": "old"},
        {"role": "assistant", "_nz_message_id": "msg-submit", "_nz_session_id": "handoff-test", "tool_calls": [
            {"id": "submit-1", "function": {"name": "update_scratchpad", "arguments": "{}"}},
        ]},
    ]
    proposal = {"hypothesis": "fix handling", "next_action": {
        "kind": "edit", "evidence": ["read-1"],
        "targets": [{"path": "api.py", "symbol": "parse", "operation": "modify"}],
        "action": "handle empty input", "verification_command": "pytest tests/test_api.py -q", "verification_cwd": ".",
    }}
    call = messages[-1]["tool_calls"][0]
    result = ToolExecutionResult("update_scratchpad", {}, "proposal", True, False, False, False,
                                 metadata={"execution_handoff_proposal": proposal})
    return state, messages, call, result


def _submit(tmp_path, state, messages, call, result, path=None):
    from nz_coder.runtime.agent.planning_runtime import submit_execution_handoff
    return submit_execution_handoff(state, call, result, messages, workspace=tmp_path, path=path)


def test_handoff_acceptance_saves_once_before_success_and_deduplicates(tmp_path, monkeypatch):
    state, messages, call, result = _case(tmp_path)
    path = tmp_path / "state.json"
    accepted = _submit(tmp_path, state, messages, call, result, path)
    assert not accepted.dispatch_failed
    assert state.execution_handoff["status"] == "pending"
    assert json.loads(path.read_text())["execution_handoff"] == state.execution_handoff
    assert state.execution_handoff["targets"][0]["identity"]["content_hash"]
    before = copy.deepcopy(state.execution_handoff)
    monkeypatch.setattr(state, "commit_execution_handoff", lambda *_a, **_kw: pytest.fail("duplicate save"))
    duplicate = _submit(tmp_path, state, messages, call, result, path)
    assert not duplicate.dispatch_failed
    assert state.execution_handoff == before


def test_save_failure_rejects_receipt_and_keeps_previous_handoff(tmp_path, monkeypatch):
    state, messages, call, result = _case(tmp_path)
    state.execution_handoff = {"previous": True}
    monkeypatch.setattr(state, "_write_snapshot", lambda *_a: (_ for _ in ()).throw(OSError("disk full")))
    rejected = _submit(tmp_path, state, messages, call, result, tmp_path / "state.json")
    assert rejected.dispatch_failed
    assert rejected.output == "Error: execution handoff rejected: An internal error occurred."
    assert state.execution_handoff == {"previous": True}


def test_disabled_persistence_is_explicit_and_does_not_write(tmp_path):
    state, messages, call, result = _case(tmp_path)
    accepted = _submit(tmp_path, state, messages, call, result)
    assert not accepted.dispatch_failed and "current process" in accepted.output
    assert not (tmp_path / "state.json").exists()


@pytest.mark.parametrize("change", ["unknown", "stale_read", "long_command", "outside", "same_batch"])
def test_invalid_or_unseen_evidence_cannot_replace_current_action(tmp_path, change):
    state, messages, call, result = _case(tmp_path)
    state.execution_handoff = {"previous": True}
    action = result.metadata["execution_handoff_proposal"]["next_action"]
    if change == "unknown":
        action["evidence"] = ["invented-id"]
    elif change == "stale_read":
        (tmp_path / "api.py").write_text("external new\n")
    elif change == "long_command":
        action["verification_command"] = "a" * 1200
    elif change == "outside":
        action["targets"][0]["path"] = "../api.py"
    else:
        messages[-1]["tool_calls"].append({"id": "write-1", "function": {"name": "edit_file", "arguments": '{"path":"api.py"}'}})
    rejected = _submit(tmp_path, state, messages, call, result)
    assert rejected.dispatch_failed and rejected.output.startswith("Error:")
    assert state.execution_handoff == {"previous": True}

    # 固定文件类型校验仍应给模型提供可纠正的错误。
    if change == "outside":
        directory = tmp_path / "package"
        directory.mkdir()
        action["targets"][0]["path"] = directory.name
        rejected = _submit(tmp_path, state, messages, call, result)
        assert rejected.dispatch_failed
        assert any(reason in rejected.output for reason in (
            "Workspace target is not a regular file", "Workspace file boundary is unsafe",
        ))
        assert state.execution_handoff == {"previous": True}


def test_external_change_invalidates_when_rendering_without_new_tool(tmp_path):
    from nz_coder.runtime.agent.planning_runtime import render_execution_handoff
    state, messages, call, result = _case(tmp_path)
    _submit(tmp_path, state, messages, call, result)
    assert "handle empty input" in render_execution_handoff(state, workspace=tmp_path)
    (tmp_path / "api.py").write_text("changed externally")
    block = render_execution_handoff(state, workspace=tmp_path)
    assert "handle empty input" not in block
    assert state.execution_handoff["status"] == "stale"


def test_only_later_committed_matching_write_performs_edit(tmp_path):
    from nz_coder.runtime.agent.planning_runtime import observe_execution_handoff
    state, messages, call, result = _case(tmp_path)
    _submit(tmp_path, state, messages, call, result)
    write = {"id": "write-1", "function": {"name": "edit_file", "arguments": "{}"}}
    messages.append({"role": "assistant", "_nz_message_id": "msg-write", "tool_calls": [write]})
    actual = ToolExecutionResult("edit_file", {"path": "api.py"}, "changed", True, False, False, True)
    unrelated = ToolExecutionResult("edit_file", {"path": "other.py"}, "changed", True, False, False, True)
    observe_execution_handoff(state, [(0, write, unrelated)], messages, committed_writes=True)
    assert state.execution_handoff["status"] == "pending"
    observe_execution_handoff(state, [(0, write, actual)], messages, committed_writes=False)
    assert state.execution_handoff["status"] == "pending"
    preview = replace(actual, tool_input={"path": "api.py", "dry_run": True})
    observe_execution_handoff(state, [(0, write, preview)], messages, committed_writes=True)
    assert state.execution_handoff["status"] == "pending"
    (tmp_path / "api.py").write_text("own committed change")
    observe_execution_handoff(state, [(0, write, actual)], messages, committed_writes=True)
    assert state.execution_handoff["status"] == "performed"
    assert state.changed_files_verified is False


def test_failed_verification_is_execution_not_acceptance(tmp_path):
    from nz_coder.runtime.agent.planning_runtime import observe_execution_handoff
    state, messages, call, result = _case(tmp_path)
    result.metadata["execution_handoff_proposal"]["next_action"]["kind"] = "verify"
    _submit(tmp_path, state, messages, call, result)
    verify = {"id": "test-1", "function": {"name": "bash", "arguments": "{}"}}
    messages.append({"role": "assistant", "_nz_message_id": "msg-test", "tool_calls": [verify]})
    actual = ToolExecutionResult("bash", {"command": "pytest tests/test_api.py -q", "cwd": "."}, "failed", True, False, True, False)
    observe_execution_handoff(state, [(0, verify, actual)], messages, committed_writes=False)
    assert state.execution_handoff["status"] == "performed"
    assert state.changed_files_verified is False


def test_new_file_missing_identity_and_restore_version_check(tmp_path):
    from nz_coder.runtime.agent.planning_runtime import activate_execution_handoff
    state, messages, call, result = _case(tmp_path)
    result.metadata["execution_handoff_proposal"]["next_action"]["targets"] = [
        {"path": "new.py", "symbol": "", "operation": "create"},
    ]
    _submit(tmp_path, state, messages, call, result, tmp_path / "state.json")
    restored = RuntimeState()
    assert restored.load(tmp_path / "state.json")
    assert restored.execution_handoff["targets"][0]["identity"]["expected_exists"] is False
    (tmp_path / "new.py").write_text("externally created")
    activate_execution_handoff(restored, messages, workspace=tmp_path, pure_continuation=True)
    assert restored.execution_handoff["status"] == "stale"


def test_epoch_change_only_pure_continue_may_rebind(tmp_path):
    from nz_coder.runtime.agent.planning_runtime import activate_execution_handoff
    state, messages, call, result = _case(tmp_path)
    _submit(tmp_path, state, messages, call, result)
    state.task_authority_epoch += 1
    activate_execution_handoff(state, messages, workspace=tmp_path, pure_continuation=True)
    assert state.execution_handoff["epoch"] == state.task_authority_epoch
    assert state.execution_handoff["status"] == "pending"
    state.task_authority_epoch += 1
    activate_execution_handoff(state, messages, workspace=tmp_path, pure_continuation=False)
    assert state.execution_handoff["status"] == "stale"


def test_existing_tool_proposes_without_duplicate_scratchpad_note(tmp_path, monkeypatch):
    from nz_coder.tools.scratchpad import _update_scratchpad, scratchpad
    monkeypatch.setattr(scratchpad, "update_section", lambda *_a, **_kw: pytest.fail("duplicate plan note"))
    action = _case(tmp_path)[3].metadata["execution_handoff_proposal"]["next_action"]
    proposal = _update_scratchpad("plan", "fix handling", next_action=action)
    assert proposal.metadata["execution_handoff_proposal"]["hypothesis"] == "fix handling"
    assert _update_scratchpad("failure", "failed", next_action=action).startswith("Error:")


def test_adapter_accepts_before_result_projection_with_planning_disabled(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from nz_coder.runtime.adapters.tool import tool_context_from_legacy_host
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.runtime.tool_runtime.result_projection import ProductionToolResultProjector

    state, messages, call, result = _case(tmp_path)
    host = SimpleNamespace(runtime_state=state)
    monkeypatch.setattr("nz_coder.runtime.adapters.tool.current_run_settings", lambda: SimpleNamespace(runtime_state_persist=False))
    with scoped_workdir(tmp_path):
        context = tool_context_from_legacy_host(host)
        accepted = asyncio.run(context.lifecycle.after_tool(call, result, messages))
        assert state.execution_handoff["status"] == "pending"
        projection = replace(context.projection, after_result=lambda *_a: None)
        ProductionToolResultProjector().consume(projection, [(0, call, accepted)], messages)
    assert "accepted" in messages[-1]["content"]
    assert not state.plan_generated


def test_scratchpad_renders_multiple_investigation_notes(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.sessions import scoped_session
    from nz_coder.tools.scratchpad import Scratchpad
    with scoped_workdir(tmp_path), scoped_session("latest-plan"):
        pad = Scratchpad()
        pad.update_section('focus', [{"summary": "old idea", "details": ""}])
        pad.update_section('focus', [{"summary": "new action", "details": ""}])
        assert "new action" in pad.build_prompt_block()
        assert "old idea" in pad.build_prompt_block()


def test_handoff_is_atomic_under_context_budget_pressure():
    from nz_coder.runtime.conversation.prompt_builder import build_context_layers
    action = "<execution-handoff>\nVerification command: pytest " + "tests/check_long.py " * 10 + "\n</execution-handoff>"
    _system, dynamic, stats = build_context_layers("rules", "m" * 10000, "s" * 10000, "p" * 10000,
                                                    max_tokens=1000, essential_block=action)
    assert action in dynamic
    assert "p" * 10000 in dynamic
    assert stats["after_memory_tokens"] == stats["after_state_tokens"] == 0
    assert stats["after_total_tokens"] > 1000


@pytest.mark.parametrize("succeeded", [True, False])
def test_real_batch_commit_boundary_controls_edit_handoff(tmp_path, succeeded):
    import asyncio
    from types import SimpleNamespace
    from nz_coder.runtime.adapters.tool import tool_context_from_legacy_host
    from nz_coder.runtime.tool_runtime.pipeline import ProductionToolRuntime, ApprovedToolBatch
    from nz_coder.runtime.verification.recovery import RecoveryState

    state, messages, call, result = _case(tmp_path)
    _submit(tmp_path, state, messages, call, result)
    write = {"id": "write-1", "function": {"name": "edit_file", "arguments": '{"path":"api.py"}'}}
    messages.append({"role": "assistant", "_nz_message_id": "msg-write", "tool_calls": [write]})
    write_result = ToolExecutionResult("edit_file", {"path": "api.py"}, "changed", True, False, False, True)
    host = SimpleNamespace(
        runtime_state=state, hooks=SimpleNamespace(after_tool_batch=lambda *_a, **_k: None),
        _tool_batch_has_write=lambda _calls: True,
        _strict_verification_completed=lambda _batch: False,
        _consume_dispatched_tools=lambda *_a, **_k: {
            "all_succeeded": succeeded, "manual_compact": False, "used_todo": False,
            "write_total": 1, "write_denied": 0, "blocked": False,
        },
        _apply_pending_plan_mode=lambda: None, _refresh_patch_risk=lambda *_a: None,
        _refresh_code_index=lambda *_a: None, _attach_lsp_write_diagnostics=lambda *_a: None,
        _collect_lsp_write_diagnostics=lambda *_a, **_k: None,
        _apply_lsp_write_diagnostics=lambda *_a: None,
        recovery=RecoveryState(),
    )
    async def dispatch(*_args):
        (tmp_path / "api.py").write_text("tentative")
        return [(0, write, write_result)]
    def finish(*_args):
        assert state.execution_handoff["status"] == "pending"
        if not succeeded:
            (tmp_path / "api.py").write_text("old\n")
    host._dispatch_tool_calls_async = dispatch
    host._finish_tool_transaction = finish
    context = tool_context_from_legacy_host(host)
    asyncio.run(ProductionToolRuntime().execute_batch_async(
        context, [write], messages, approved_batch=ApprovedToolBatch([write]), finish_step=False,
    ))
    assert state.execution_handoff["status"] == ("performed" if succeeded else "pending")


def test_real_symbol_read_can_submit_but_changed_file_cannot(tmp_path):
    from nz_coder.tools.repo_intel import read_symbol
    from nz_coder.runtime.process.workdir import scoped_workdir
    state, messages, call, result = _case(tmp_path)
    (tmp_path / "api.py").write_text("def parse(value):\n    return value\n")
    with scoped_workdir(tmp_path):
        observed = read_symbol("api.py", "parse")
    messages[1]["_nz_parts"][0]["state"]["metadata"] = getattr(observed, "metadata", {})
    assert not _submit(tmp_path, state, messages, call, result).dispatch_failed
    state.execution_handoff = {}
    (tmp_path / "api.py").write_text("def parse(value):\n    return None\n")
    assert _submit(tmp_path, state, messages, call, result).dispatch_failed


def test_absolute_committed_target_path_performs_before_render(tmp_path):
    from nz_coder.runtime.agent.planning_runtime import observe_execution_handoff, render_execution_handoff
    state, messages, call, result = _case(tmp_path)
    _submit(tmp_path, state, messages, call, result)
    write = {"id": "write-abs", "function": {"name": "edit_file"}}
    messages.append({"role": "assistant", "_nz_message_id": "msg-write-abs", "tool_calls": [write]})
    (tmp_path / "api.py").write_text("own update")
    actual = ToolExecutionResult("edit_file", {"path": str(tmp_path / "api.py")}, "changed", True, False, False, True)
    observe_execution_handoff(state, [(0, write, actual)], messages, committed_writes=True)
    assert state.execution_handoff["status"] == "performed"
    assert not render_execution_handoff(state, workspace=tmp_path)


@pytest.mark.parametrize("symbol,mode", [("other", "read"), ("parse", "list")])
def test_unrelated_symbol_does_not_perform_requested_inspect(tmp_path, symbol, mode):
    from nz_coder.runtime.agent.planning_runtime import observe_execution_handoff
    state, messages, call, result = _case(tmp_path)
    result.metadata["execution_handoff_proposal"]["next_action"]["kind"] = "inspect"
    _submit(tmp_path, state, messages, call, result)
    read = {"id": "read-other", "function": {"name": "read_symbol"}}
    messages.append({"role": "assistant", "_nz_message_id": "msg-other", "tool_calls": [read]})
    actual = ToolExecutionResult("read_symbol", {"path": "api.py", "symbol": symbol, "mode": mode}, "observed", True, False, False, False)
    observe_execution_handoff(state, [(0, read, actual)], messages, committed_writes=False)
    assert state.execution_handoff["status"] == "pending"


def test_restored_source_mismatch_is_invalid_and_missing_checkpoint_stays_unconfirmed(tmp_path):
    from nz_coder.runtime.agent.planning_runtime import activate_execution_handoff, render_execution_handoff
    state, messages, call, result = _case(tmp_path)
    _submit(tmp_path, state, messages, call, result)
    state.execution_handoff["source_message"] = "msg-impossible"
    activate_execution_handoff(state, messages, workspace=tmp_path, pure_continuation=True)
    assert state.execution_handoff["status"] == "stale"
    state.execution_handoff = {}
    _submit(tmp_path, state, messages, call, result)
    state.task_authority_epoch += 1
    activate_execution_handoff(state, [], workspace=tmp_path, pure_continuation=True)
    assert state.execution_handoff["status"] == "pending"
    assert state.execution_handoff["epoch"] == state.task_authority_epoch - 1
    assert not state.execution_handoff["source_confirmed"]
    assert not render_execution_handoff(state, workspace=tmp_path)


def test_compacted_source_requires_session_bound_engine_marker(tmp_path):
    from nz_coder.protocol.message_schema import ensure_message_identities
    from nz_coder.runtime.agent.planning_runtime import activate_execution_handoff, render_execution_handoff
    state, messages, call, result = _case(tmp_path)
    _submit(tmp_path, state, messages, call, result)
    summary = {"role": "user", "content": "<session-summary>history</session-summary>",
               "_nz_compaction": {"auto": True, "head_message_ids": ["msg-submit"]}}
    history = [summary]
    ensure_message_identities(history, "other-session")
    activate_execution_handoff(state, history, workspace=tmp_path)
    assert not render_execution_handoff(state, workspace=tmp_path)
    summary["_nz_session_id"] = "handoff-test"
    activate_execution_handoff(state, history, workspace=tmp_path)
    assert state.execution_handoff["source_confirmed"]
    assert "handle empty input" in render_execution_handoff(state, workspace=tmp_path)


def test_compacted_source_own_write_performs_before_old_version_check(tmp_path):
    from nz_coder.protocol.message_schema import ensure_message_identities
    from nz_coder.runtime.agent.planning_runtime import observe_execution_handoff
    state, messages, call, result = _case(tmp_path)
    _submit(tmp_path, state, messages, call, result)
    write = {"id": "write-later", "function": {"name": "edit_file"}}
    compacted = [{"role": "user", "content": "<session-summary>history</session-summary>",
                  "_nz_compaction": {"auto": True, "head_message_ids": ["msg-submit"]}},
                 {"role": "assistant", "tool_calls": [write]}]
    ensure_message_identities(compacted, "handoff-test")
    (tmp_path / "api.py").write_text("own update")
    actual = ToolExecutionResult("edit_file", {"path": "api.py"}, "changed", True, False, False, True)
    observe_execution_handoff(state, [(0, write, actual)], compacted, committed_writes=True)
    assert state.execution_handoff["status"] == "performed"


def test_reading_tail_of_file_does_not_satisfy_symbol_near_head(tmp_path):
    from nz_coder.runtime.agent.planning_runtime import observe_execution_handoff
    state, messages, call, result = _case(tmp_path)
    result.metadata["execution_handoff_proposal"]["next_action"]["kind"] = "inspect"
    _submit(tmp_path, state, messages, call, result)
    read = {"id": "read-tail", "function": {"name": "read_file"}}
    messages.append({"role": "assistant", "_nz_message_id": "msg-tail", "tool_calls": [read]})
    actual = ToolExecutionResult("read_file", {"path": "api.py", "offset": 4}, "tail", True, False, False, False,
                                 metadata={"model_read_observation": {"offset": 4, "complete": True}})
    observe_execution_handoff(state, [(0, read, actual)], messages, committed_writes=False)
    assert state.execution_handoff["status"] == "pending"
