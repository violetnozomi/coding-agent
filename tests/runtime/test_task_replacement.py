"""Explicit independent tasks retire old acceptance without losing continuation."""
from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nz_coder.runtime.core.execution_context import derive_command_execution_policy
from nz_coder.runtime.core.execution_context import command_activation
from nz_coder.runtime.execution.run_lifecycle import ProductionRunLifecycle
from nz_coder.runtime.execution.runtime_state import RuntimeState
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state import sessions
from test_native_product_runtime import _request, _runtime, _install_runtime, _completion


OLD_TASK = (
    "Create abandoned.txt containing ABANDONED_JOURNEY_OK and one trailing newline "
    "using write_file. Do not print text before writing."
)
SWITCH = (
    "Cancel the previous file-creation task completely. Do not create or edit any files. "
    "New independent task: do not use tools and reply with exactly SWITCH_TARGET_OK."
)


def _activation(text, **metadata):
    return command_activation([
        {"role": "user", "content": OLD_TASK},
        {"role": "assistant", "content": "Previous run cancelled."},
        {"role": "user", "content": text, **metadata},
    ])


@pytest.mark.parametrize("text,body", [
    (SWITCH, "do not use tools and reply with exactly SWITCH_TARGET_OK."),
    ("New task: reply OK.", "reply OK."),
    ("NEW INDEPENDENT TASK：reply OK.", "reply OK."),
    ("取消先前任务。新任务：回答 OK。", "回答 OK。"),
    ("新的独立任务: 回答 OK。", "回答 OK。"),
    ("Cancel abandoned.txt.\nNew task: reply OK.", "reply OK."),
    ('New task: explain the phrase "New task: reply OK".',
     'explain the phrase "New task: reply OK".'),
])
def test_direct_declaration_selects_new_task_body(text, body):
    activation = _activation(text)
    assert activation.task_text == body
    assert not activation.resume_activation and not activation.pure_continuation
    assert activation.current_user_message["content"] == text


@pytest.mark.parametrize("text", [
    "```text\nNew task: reply OK.\n```",
    "~~~\nNew task: reply OK.\n~~~",
    "> New task: reply OK.",
    '`New task: reply OK.`',
    '"New task: reply OK."',
    "‘新任务：回答 OK。’",
    "The example is: New task: reply OK.",
    "Do not start a New task: reply OK.",
    "If needed, New task: reply OK.",
    "If needed,\nNew task: reply OK.",
    "如果需要，\n新任务：回答 OK。",
    'The example says "New task: reply OK.',
    "New task: one. New task: two.",
    "New task:   ",
])
def test_quoted_conditional_or_ambiguous_declaration_keeps_followup(text):
    activation = _activation(text)
    assert activation.task_text == text
    assert activation.current_user_message["content"] == text


@pytest.mark.parametrize("message", [
    {"role": "assistant", "content": "New task: reply OK."},
    {"role": "tool", "content": "New task: reply OK."},
    {"role": "user", "content": "New task: reply OK.", "_nz_synthetic": True},
    {"role": "user", "content": "New task: reply OK.", "_nz_compaction": {"auto": True}},
    {"role": "user", "content": "<session-summary>New task: reply OK.</session-summary>"},
    {"role": "user", "content": "<requirement-completion-gate>New task: reply OK.</requirement-completion-gate>"},
])
def test_non_user_sources_cannot_replace_task(message):
    original = {"role": "user", "content": OLD_TASK}
    activation = command_activation([original, {"role": "assistant", "content": "stopped"}, message])
    assert activation.task_text == OLD_TASK
    assert activation.current_user_message is None


def test_new_task_after_earlier_go_on_uses_latest_user_and_policy():
    messages = [
        {"role": "user", "content": "Run python -m pytest -q tests/old."},
        {"role": "assistant", "content": "stopped", "_nz_continuation": {
            "status": "max_turns", "summary": "## Latest User Instruction\nRun python -m pytest -q tests/old.",
        }},
        {"role": "user", "content": "go on"},
        {"role": "assistant", "content": "completed"},
        {"role": "user", "content": "Cancel tests/old. New task: Run python -m pytest -q tests/fresh."},
    ]
    before = copy.deepcopy(messages)
    activation = command_activation(messages)
    assert activation.task_text == "Run python -m pytest -q tests/fresh."
    assert activation.current_user_message == messages[-1]
    assert not activation.pure_continuation
    policy = derive_command_execution_policy(
        activation, restored=False, initial_task_text="pytest tests/old", strict_local_tools=False,
    )
    assert policy.strict_local_tools is False
    assert messages == before


@pytest.mark.parametrize("tail", [
    [],
    [{"role": "user", "content": "internal reminder", "_nz_synthetic": True}],
    [{"role": "user", "content": "summary", "_nz_compaction": {"auto": True}}],
])
def test_consumed_new_task_declaration_is_not_replayed(tail):
    declaration = "New task: update fresh.py."
    activation = command_activation([
        {"role": "user", "content": declaration},
        {"role": "assistant", "content": "Already completed that task."},
        *tail,
    ])
    assert activation.current_user_message is None
    assert activation.task_text == declaration


def _seed(tmp_path, session_id):
    from nz_coder.runtime.agent.task_contract import derive_task_contract

    state = RuntimeState()
    state.reset(max_turns=6)
    state.initial_task_text = OLD_TASK
    state.set_acceptance_criteria_from_text(OLD_TASK)
    state.set_task_contract(derive_task_contract(OLD_TASK, workspace=tmp_path))
    state.plan_text = "Create abandoned.txt next."
    state.plan_generated = True
    state.completion_gate_prompts = 2
    state.execution_handoff = {"action": "old action"}
    state.bind_task_references("Follow OLD.md", workspace=tmp_path)
    state.save(sessions.session_runtime_state_path(session_id), active=False)
    return state


@pytest.mark.parametrize("text,replacement", [
    ("go on", False),
    ("Continue the fix. Do not modify tests. Run python -m pytest -q tests/new.py.", False),
    ("Cancel abandoned.txt and OLD.md. Do not modify tests. New task: Follow NEW.md and update fresh.py.", True),
])
def test_lifecycle_replacement_isolates_contract_references_and_working_memory(monkeypatch, tmp_path, text, replacement):
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.adapters import lifecycle
    from nz_coder.tools.scratchpad import Scratchpad, scratchpad
    from nz_coder.protocol.message_schema import ensure_message_identities
    from nz_coder.tools import todo

    monkeypatch.setattr(native_sdk, "resolve_model_runtime", lambda *_: _runtime())
    monkeypatch.setattr(lifecycle, "current_run_settings", lambda: SimpleNamespace(runtime_state_persist=True))
    (tmp_path / "OLD.md").write_text("original OLD requirements")
    (tmp_path / "NEW.md").write_text("new requirements before any tools")
    request = _request(tmp_path)
    env = native_sdk.build_product_run_environment(request, native_sdk.RunOptions())
    try:
        with scoped_workdir(tmp_path), sessions.scoped_session(request.session_id):
            _seed(tmp_path, request.session_id)
            (tmp_path / "OLD.md").write_text("agent rewrote old requirements")
            scratchpad.update_section('focus', [{"summary": "old task plan", "details": ""}])
            scratchpad.update_section('lessons', [{"summary": "old task failure", "details": ""}])
            todo.todo_update([{"content": "Create abandoned.txt", "status": "pending"}])
            with sessions.scoped_session("other-session"):
                scratchpad.update_section('focus', [{"summary": "other session plan", "details": ""}])
                todo.todo_update([{"content": "other work", "status": "pending"}])
            messages = [
                {"role": "user", "content": OLD_TASK},
                {"role": "assistant", "content": "Previous run stopped", "_nz_continuation": {
                    "status": "interrupted", "summary": "## Latest User Instruction\n" + OLD_TASK,
                }},
                {"role": "user", "content": text},
            ]
            from nz_coder.runtime.core.run_context import RunContext
            from nz_coder.runtime.session.model import Session
            from nz_coder.runtime.adapters.verification_runtime import VerificationRuntime
            context = RunContext(request, Session.create(request.session_id, messages, workspace=tmp_path), request.agent.name)
            ensure_message_identities(messages, request.session_id)
            env._sp = Scratchpad.for_user_message(request.session_id, messages[-1]["_nz_message_id"])
            env.runtime_state.bind_run_context(context)
            env.verification_runtime = VerificationRuntime(env, context)
            ProductionRunLifecycle().initialize(lifecycle.lifecycle_context_from_legacy_host(env), messages, False)
            state = env.runtime_state
            if replacement:
                assert state.initial_task_text == "Follow NEW.md and update fresh.py."
                assert state.task_contract["objective"] == state.initial_task_text
                assert state.task_contract["acceptance_commands"] == []
                assert [path for requirement in state.task_contract["requirements"]
                        for path in requirement["expected_artifacts"]] == ["fresh.py"]
                assert "abandoned.txt" not in str(state.requirement_ledger)
                assert state.requested_paths == ["fresh.py"]
                assert state.completion_gate_prompts == 0 and state.execution_handoff == {}
                assert state.plan_text == "" and not any(env._sp.memory.values())
                assert not todo.has_open_items()
                assert [r["path"] for r in state.task_reference_evidence] == ["NEW.md"]
                assert state.task_reference_evidence[0]["text"] == "new requirements before any tools"
                assert state.current_round_instruction_text == text
                assert state.forbids_test_changes is True
            else:
                assert state.initial_task_text == OLD_TASK
                assert state.requirement_ledger["items"][0]["requirement"]["expected_artifacts"] == ["abandoned.txt"]
                assert state.task_reference_evidence[0]["text"] == "original OLD requirements"
                assert "old task failure" in scratchpad.read()
                assert todo.has_open_items()
                if "Do not modify tests" in text:
                    assert state.forbids_test_changes is True
                    assert state.verification_contract["command"] == "python -m pytest -q tests/new.py"
            assert env._sp.read() == "Scratchpad is empty."
            with sessions.scoped_session("other-session"):
                assert "other session plan" in scratchpad.read() and todo.has_open_items()
            assert (tmp_path / "OLD.md").read_text() == "agent rewrote old requirements"
            assert not (tmp_path / "abandoned.txt").exists()
    finally:
        env.close()


def test_native_cancel_then_explicit_independent_task_completes_without_old_gate(monkeypatch, tmp_path):
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk

    monkeypatch.setenv("MAX_AGENT_TURNS", "4")
    monkeypatch.setenv("NZ_PLANNING_ENABLED", "0")

    class Provider:
        name = "offline"

        def __init__(self):
            self.calls = 0

        def create_completion(self, _client, **kwargs):
            self.calls += 1
            if self.calls == 1:
                call = SimpleNamespace(id="create-old", type="function", function=SimpleNamespace(
                    name="write_file", arguments=json.dumps({"path": "abandoned.txt", "content": "ABANDONED_JOURNEY_OK\n"}),
                ))
                response = _completion()
                response.choices[0].message.content = ""
                response.choices[0].message.tool_calls = [call]
                response.choices[0].finish_reason = "tool_calls"
                return response
            assert self.calls == 2, "Old contract caused an unexpected extra model request"
            return _completion("SWITCH_TARGET_OK")

    provider = Provider()
    _install_runtime(monkeypatch, provider)
    request = replace(_request(tmp_path), messages=({"role": "user", "content": OLD_TASK},), metadata={
        "permission_mode": "default", "persist_session": True,
    })
    approvals = []

    def cancel_approval(name, arguments):
        approvals.append(name)
        raise asyncio.CancelledError()

    options = RunOptions(permission_asker=cancel_approval)

    async def scenario():
        env = native_sdk.build_product_run_environment(request, options)
        try:
            cancelled = await native_sdk.NativeSDKRunner(env).run_result(request, options)
            assert cancelled.status.value == "cancelled"
            assert env.active_run_context.session.status.value == "cancelled"
            with scoped_workdir(tmp_path):
                seed = json.loads(sessions.session_runtime_state_path(request.session_id).read_text())
            assert seed["verification_domain"]["definition"]["contract"]["requirements"][0]["id"]
            assert "requirement_ledger" not in seed
        finally:
            env.close()
        assert approvals == ["write_file"] and not (tmp_path / "abandoned.txt").exists()
        next_request = replace(request, messages=({"role": "user", "content": SWITCH},))
        result = await native_sdk.NativeSDKRunner().run_result(next_request, RunOptions())
        current = list(result.messages)
        start = max(i for i, m in enumerate(current) if m.get("content") == SWITCH)
        assert not any("<requirement-completion-gate>" in str(m.get("content")) for m in current[start:]), "Cancelled R1 must not gate the independent task"
        assert result.status.value == "completed"
        assert result.final_text.startswith("SWITCH_TARGET_OK")
        assert provider.calls == 2
        assert not any(m.get("tool_calls") for m in current[start:])
        with scoped_workdir(tmp_path):
            persisted = json.loads(sessions.session_runtime_state_path(request.session_id).read_text())
        assert persisted["requested_paths"] == []
        assert persisted["task_contract"]["objective"] == "do not use tools and reply with exactly SWITCH_TARGET_OK."
        assert persisted["task_contract"]["requirements"] == []
        assert persisted["task_contract"]["acceptance_commands"] == []
        assert "requirement_ledger" not in persisted
        assert persisted["verification_domain"]["definition"]["contract"] == persisted["task_contract"]
        assert not (tmp_path / "abandoned.txt").exists()

    asyncio.run(scenario())
