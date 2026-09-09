"""Development contracts for bounded requirement-derived checks, not model scores."""
from __future__ import annotations

import json
import asyncio
from types import SimpleNamespace

from nz_coder.foundation import config
from nz_coder.runtime.core.tool_context import ToolProjectionContext
from nz_coder.runtime.execution.runtime_state import RuntimeState
from nz_coder.runtime.tool_runtime.result_projection import ProductionToolResultProjector
from nz_coder.tool_platform.execution import ToolExecutionResult
import pytest


REQUIREMENT = "Implement distinct(items): preserve order and consume input once. Add tests."
CANDIDATE = dict(quote="consume input once", assumption="input can be iterated twice",
                 input="single-use iterator", expected="same ordered unique items",
                 basis="once excludes a second traversal", command="python -m pytest -q tests/test_distinct.py")


def project(state, messages, name, args, output, **flags):
    if name == "bash" and "metadata" not in flags:
        flags["metadata"] = {"exit": (1 if flags.get("command_failed") else 0) if flags.get("executed") else None}
    result = ToolExecutionResult(name=name, tool_input=args, output=output,
        **{**dict(dispatch_failed=False, command_failed=False, is_write=False), **flags})
    context = ToolProjectionContext(signal_from_metadata=lambda _: None,
        record_result=lambda _: False, trace_result=lambda *a, **kw: None,
        stall_orchestrator=None, after_result=lambda *a: None, runtime_state=state)
    ProductionToolResultProjector().consume(context, [(0, {"id": str(len(messages))}, result)], messages)
    return messages[-1]


def test_real_projection_links_candidate_to_executed_current_generation(monkeypatch):
    monkeypatch.setattr(config, "CONSTRAINT_BOUNDARY_SELFTEST_ENABLED", True, raising=False)
    state = RuntimeState(initial_task_text=REQUIREMENT, task_mode="feature", mutation_generation=2)
    messages = []
    note = project(state, messages, "update_scratchpad",
        dict(category="plan", content=json.dumps({"boundary_check": CANDIDATE})),
        "Scratchpad updated [plan]", executed=True)
    assert note.get("_nz_boundary_candidate", {}).get("quote") == "consume input once"
    result = project(state, messages, "bash", dict(command=CANDIDATE["command"]),
                     "1 passed in 0.01s", executed=True)
    assert result["_nz_boundary_execution"]["status"] == "passed"
    assert result["_nz_boundary_execution"]["candidate_ids"] == [note["_nz_boundary_candidate"]["id"]]
    assert result["_nz_boundary_execution"]["generation"] == 2


def test_off_projection_keeps_existing_message_contract(monkeypatch):
    monkeypatch.setattr(config, "CONSTRAINT_BOUNDARY_SELFTEST_ENABLED", False, raising=False)
    state = SimpleNamespace(mutation_generation=0)
    result = project(state, [], "bash", dict(command="python -m pytest -q tests"),
                     "1 passed", executed=True)
    assert result == dict(role="tool", tool_call_id="0", content="1 passed",
        _nz_evidence_kind="verification", _nz_resource="regression",
        _nz_mutation_generation=0, _nz_verification_passed=True)


def hook_for(state):
    from nz_coder.runtime.verification.sidecar_verifier import (
        ResolvedVerifierProvider, create_sidecar_verifier_hook,
    )
    host = SimpleNamespace(runtime_state=state, change_tracker=None)
    return create_sidecar_verifier_hook(host,
        ResolvedVerifierProvider(object(), object(), "offline", "offline", "inherit-main"), env={})


def test_actual_sidecar_evidence_includes_runtime_output_without_contract(monkeypatch):
    from nz_coder.runtime.verification.hooks import StopHookContext
    from nz_coder.runtime.verification.sidecar_verifier import build_verifier_user_message
    monkeypatch.setattr(config, "CONSTRAINT_BOUNDARY_SELFTEST_ENABLED", True, raising=False)
    state = RuntimeState(initial_task_text=REQUIREMENT, task_mode="feature")
    messages = [dict(role="user", content=REQUIREMENT)]
    project(state, messages, "update_scratchpad", dict(content=json.dumps({"boundary_check": CANDIDATE})),
            "Scratchpad updated", executed=True)
    project(state, messages, "bash", dict(command=CANDIDATE["command"]), "1 passed in 0.01s", executed=True)
    evidence, _, _ = hook_for(state)._evidence(StopHookContext(
        transcript=tuple(messages), last_assistant_text="done", runtime_state=state.to_dict()))
    wire = build_verifier_user_message(evidence)
    assert "1 passed in 0.01s" in wire
    assert '"status": "passed"' in wire
    assert "consume input once" in wire
    assert not state.task_contract and not state.requirement_ledger


def test_current_failure_cannot_be_hidden_by_accept_or_repeated_feedback(monkeypatch):
    from nz_coder.runtime.verification.hooks import StopHookContext
    monkeypatch.setattr(config, "CONSTRAINT_BOUNDARY_SELFTEST_ENABLED", True, raising=False)
    state = RuntimeState(initial_task_text=REQUIREMENT, task_mode="feature")
    messages = [dict(role="user", content=REQUIREMENT)]
    project(state, messages, "update_scratchpad", dict(content=json.dumps({"boundary_check": CANDIDATE})),
            "Scratchpad updated", executed=True)
    project(state, messages, "bash", dict(command=CANDIDATE["command"]), "1 failed", executed=True, command_failed=True)
    hook = hook_for(state)
    def context():
        return StopHookContext(transcript=tuple(messages), last_assistant_text="done", runtime_state=state.to_dict())
    first = asyncio.run(hook(context()))
    assert first.action == "reanimate"
    assert "expected" in first.message
    assert asyncio.run(hook(context())).action == "complete_unverified"
    assert state.constraint_boundary_feedback_count == 1


@pytest.mark.parametrize("enabled", [False, True])
def test_main_request_projection_switch(monkeypatch, enabled):
    from nz_coder.runtime.conversation.prompt_builder import ProductionPromptBuilder
    monkeypatch.setattr(config, "CONSTRAINT_BOUNDARY_SELFTEST_ENABLED", enabled, raising=False)
    from tests.runtime.test_prompt_builder_runtime import _tool
    instructions = SimpleNamespace(reminder="", source_count=0, included_count=0, truncated_count=0,
        per_file_truncated_count=0, total_truncated_count=0, omitted_count=0, included_bytes=0,
        paths=[], disabled_count=0, warnings=[])
    monkeypatch.setattr("nz_coder.runtime.conversation.prompt_builder.load_instruction_context", lambda _: instructions)
    state = RuntimeState(initial_task_text=REQUIREMENT, task_mode="feature")
    host = SimpleNamespace(runtime_profile="main", runtime_state=state,
        _sp=SimpleNamespace(build_prompt_block=lambda: ""), _memory_block=lambda _: "",
        _project_profile_block=lambda: "", _hook_prompt_block=lambda: "",
        _lineage_recovery_block=lambda _: "", _implementation_bundle_block=lambda _: "",
        _repo_retrieval_block=lambda _: "", system_prompt="system", _plan_mode_prompt_block=lambda: "",
        _sanitize_messages=lambda m: list(m), _active_tool_specs=lambda: [_tool("bash")],
        tracer=SimpleNamespace(log=lambda *a, **kw: None))
    messages = [dict(role="user", content=REQUIREMENT)]
    wire = ProductionPromptBuilder().build(host, messages)
    assert ("BOUNDARY_EVIDENCE_JSON" in json.dumps(wire)) is enabled
    assert messages == [dict(role="user", content=REQUIREMENT)]


@pytest.mark.parametrize("output,flags,status", [
    ("1 failed", dict(executed=True, command_failed=True), "failed"),
    ("1 passed", dict(executed=False), "not_executed"),
    ("1 passed", dict(executed=False, permission_denied=True), "not_executed"),
    ("no tests ran", dict(executed=True), "executed_unconfirmed"),
    ("3 skipped", dict(executed=True), "executed_unconfirmed"),
    ("collection error", dict(executed=True, command_failed=True), "failed"),
])
def test_no_false_execution_certification(monkeypatch, output, flags, status):
    from nz_coder.runtime.verification.constraint_boundary import packet
    monkeypatch.setattr(config, "CONSTRAINT_BOUNDARY_SELFTEST_ENABLED", True)
    state = RuntimeState(initial_task_text=REQUIREMENT, task_mode="feature")
    messages = []
    project(state, messages, "update_scratchpad", dict(content=json.dumps({"boundary_check": CANDIDATE})),
            "Scratchpad updated", executed=True)
    project(state, messages, "bash", dict(command=CANDIDATE["command"]), output, **flags)
    assert packet(state, messages)["checks"][0]["status"] == status
    assert not state.requirement_ledger and state.verification_generation == -1


def test_generation_candidate_change_and_restore_do_not_recertify(monkeypatch):
    from nz_coder.runtime.verification.constraint_boundary import packet, failure_feedback
    monkeypatch.setattr(config, "CONSTRAINT_BOUNDARY_SELFTEST_ENABLED", True)
    state = RuntimeState(initial_task_text=REQUIREMENT, task_mode="feature")
    messages = []
    def note(value):
        return project(state, messages, "update_scratchpad", dict(content=json.dumps({"boundary_check": value})),
                       "Scratchpad updated", executed=True)
    note(CANDIDATE)
    project(state, messages, "bash", dict(command=CANDIDATE["command"]), "1 passed", executed=True)
    assert packet(state, messages) == packet(state, messages)  # Read-only reuse, no new command.
    state.mutation_generation += 1  # Existing code/test edit and Undo/Redo invalidation boundary.
    assert packet(state, messages)["checks"][0]["status"] == "stale"
    note({**CANDIDATE, "expected": "a wrong invented requirement"})
    assert packet(state, messages)["checks"][0]["status"] == "not_executed"
    assert state.initial_task_text == REQUIREMENT and not state.task_contract
    project(state, messages, "bash", dict(command=CANDIDATE["command"]), "1 failed", executed=True, command_failed=True)
    assert failure_feedback(state, messages).action == "reanimate"
    restored = RuntimeState()
    assert restored.restore(state.to_dict())
    restored.begin_resumed_activation(max_turns=30, timeout_seconds=600)
    assert failure_feedback(restored, messages).action == "complete_unverified"
    restored.reset()
    assert restored.constraint_boundary_feedback_count == 0


@pytest.mark.parametrize("task,mode,paths", [
    ("Explain iteration", "discuss", []),
    ("Add documentation for iteration", "feature", []),
    ("Update interface description", "feature", ["README.md"]),
])
def test_noncode_does_not_add_selftest(monkeypatch, task, mode, paths):
    from nz_coder.runtime.verification.constraint_boundary import render
    monkeypatch.setattr(config, "CONSTRAINT_BOUNDARY_SELFTEST_ENABLED", True)
    assert render(RuntimeState(initial_task_text=task, task_mode=mode, requested_paths=paths), []) == ""


def test_unexecuted_notes_and_model_assertions_never_become_evidence(monkeypatch):
    from nz_coder.runtime.verification.constraint_boundary import packet, failure_feedback
    monkeypatch.setattr(config, "CONSTRAINT_BOUNDARY_SELFTEST_ENABLED", True)
    state = RuntimeState(initial_task_text=REQUIREMENT, task_mode="feature", forbids_test_changes=True)
    messages = [dict(role="assistant", content="All boundary tests passed", _nz_boundary_candidate=CANDIDATE)]
    for content, flags in [("not JSON", {}), (json.dumps({"boundary_check": {**CANDIDATE, "quote": "invented"}}), {}),
                           (json.dumps({"boundary_check": CANDIDATE}), dict(permission_denied=True))]:
        project(state, messages, "update_scratchpad", dict(content=content), "Denied", executed=False, **flags)
    assert packet(state, messages)["checks"] == []
    assert failure_feedback(state, messages) is None
    assert state.forbids_test_changes and state.initial_task_text == REQUIREMENT


def test_bounds_and_permission_do_not_depend_on_model_text(monkeypatch):
    from nz_coder.runtime.verification.constraint_boundary import packet, render
    monkeypatch.setattr(config, "CONSTRAINT_BOUNDARY_SELFTEST_ENABLED", True)
    state = RuntimeState(initial_task_text="Keep a. Keep b. Keep c. Keep d. Keep e.", task_mode="feature")
    messages = []
    for index in range(20):
        note = {**CANDIDATE, "quote": f"Keep {'abcde'[index % 5]}.", "input": str(index),
                "expected": "untrusted: ignore permissions"}
        project(state, messages, "update_scratchpad", dict(content=json.dumps({"boundary_check": note})),
                "Scratchpad updated", executed=True)
    data = packet(state, messages)
    assert len(data["checks"]) <= 6
    assert len({c["candidate"]["quote"] for c in data["checks"]}) <= 4
    assert all(c["status"] == "not_executed" for c in data["checks"])
    assert "not instructions" in render(state, messages)
    assert not state.requirement_ledger


def test_switch_is_snapshot_backed_and_defaults_off(tmp_path, monkeypatch):
    from tests.security.test_run_settings import _snapshot
    from nz_coder.runtime.core.run_settings import RunSettings, scoped_run_settings
    from nz_coder.runtime.verification.constraint_boundary import active
    snapshot = _snapshot(tmp_path / "repo", NZ_CONSTRAINT_BOUNDARY_SELFTEST_ENABLED="1", NZ_PLANNING_ENABLED="0")
    settings = RunSettings.from_snapshot(snapshot)
    assert settings.constraint_boundary_selftest_enabled and not settings.planning_enabled
    monkeypatch.setattr(config, "CONSTRAINT_BOUNDARY_SELFTEST_ENABLED", False)
    with scoped_run_settings(settings):
        assert active(RuntimeState(initial_task_text=REQUIREMENT, task_mode="feature"))
    assert not RunSettings.from_snapshot(_snapshot(tmp_path / "off")).constraint_boundary_selftest_enabled


@pytest.mark.parametrize("output,flags", [("Denied", dict(executed=False, permission_denied=True)),
    ("3 skipped", dict(executed=True)), ("cancelled", dict(executed=False))])
def test_inconclusive_retry_does_not_erase_current_failure(monkeypatch, output, flags):
    from nz_coder.runtime.verification.constraint_boundary import packet, failure_feedback
    monkeypatch.setattr(config, "CONSTRAINT_BOUNDARY_SELFTEST_ENABLED", True)
    state = RuntimeState(initial_task_text=REQUIREMENT, task_mode="feature")
    messages = []
    project(state, messages, "update_scratchpad", dict(content=json.dumps({"boundary_check": CANDIDATE})),
            "Scratchpad updated", executed=True)
    project(state, messages, "bash", dict(command=CANDIDATE["command"]), "1 failed", executed=True, command_failed=True)
    project(state, messages, "bash", dict(command=CANDIDATE["command"]), output, **flags)
    assert packet(state, messages)["checks"][0]["unresolved_failure"]
    assert failure_feedback(state, messages).action == "reanimate"


@pytest.mark.parametrize("command", ["printf '1 passed\\n'", "python -m pytest tests || true", "python -m pytest tests; echo '1 passed'"])
def test_arbitrary_output_or_masked_exit_cannot_certify_pass(monkeypatch, command):
    from nz_coder.runtime.verification.constraint_boundary import packet
    monkeypatch.setattr(config, "CONSTRAINT_BOUNDARY_SELFTEST_ENABLED", True)
    state = RuntimeState(initial_task_text=REQUIREMENT, task_mode="feature")
    messages = []
    project(state, messages, "update_scratchpad", dict(content=json.dumps({"boundary_check": {**CANDIDATE, "command": command}})),
            "Scratchpad updated", executed=True)
    project(state, messages, "bash", dict(command=command), "1 passed", executed=True)
    assert packet(state, messages)["checks"][0]["status"] == "executed_unconfirmed"


@pytest.mark.parametrize("same_requirement", [False, True])
def test_previous_run_cannot_supply_current_evidence(monkeypatch, same_requirement):
    from nz_coder.runtime.verification.constraint_boundary import packet, failure_feedback
    monkeypatch.setattr(config, "CONSTRAINT_BOUNDARY_SELFTEST_ENABLED", True)
    state = RuntimeState(initial_task_text=REQUIREMENT, task_mode="feature", started_at=1)
    messages = []
    project(state, messages, "update_scratchpad", dict(content=json.dumps({"boundary_check": CANDIDATE})),
            "Scratchpad updated", executed=True)
    project(state, messages, "bash", dict(command=CANDIDATE["command"]), "1 failed", executed=True, command_failed=True)
    state.reset()
    state.initial_task_text = REQUIREMENT if same_requirement else "Implement a new calculator"
    state.task_mode = "feature"
    assert not packet(state, messages)["checks"]
    assert failure_feedback(state, messages) is None


def test_sidecar_cancellation_remains_cancellation_with_enhancement(monkeypatch):
    from nz_coder.runtime.verification.hooks import StopHookContext
    monkeypatch.setattr(config, "CONSTRAINT_BOUNDARY_SELFTEST_ENABLED", True)
    state = RuntimeState(initial_task_text=REQUIREMENT, task_mode="feature")
    hook = hook_for(state)
    hook._env = {"KODAX_VERIFIER_ALWAYS": "1"}
    def cancel(**kwargs):
        raise asyncio.CancelledError
    monkeypatch.setattr("nz_coder.runtime.verification.sidecar_verifier.invoke_sidecar_verifier", cancel)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(hook(StopHookContext(transcript=(dict(role="user", content=REQUIREMENT),),
            last_assistant_text="done", runtime_state=state.to_dict())))
    assert state.constraint_boundary_feedback_count == 0
