"""当前事实来自权威状态，用户原文不交给语义摘要代为保管。"""
from __future__ import annotations

from tests.fixtures.compaction_summary import summary_json

import copy

from nz_coder.runtime.execution.runtime_state import RuntimeState
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state.sessions import scoped_session


def _history():
    return [
        {"role": "user", "content": "修复 parser；保留公开 API。", "_nz_message_id": "msg-first"},
        {"role": "assistant", "content": "large observation\n" * 7000},
        {"role": "user", "content": "补充：禁止修改 tests/fixtures。", "_nz_message_id": "msg-amendment"},
        {"role": "assistant", "content": "more investigation\n" * 7000},
        {"role": "user", "content": "继续并验证 unicode 输入。", "_nz_message_id": "msg-current"},
    ]


def test_compaction_preserves_available_user_originals_in_order(tmp_path, monkeypatch):
    from nz_coder.state import context as module
    from tests.test_context_budget import _CompactionClient
    monkeypatch.setattr(module, "_get_git_diff_summary", lambda: "")
    messages = _history()
    before = copy.deepcopy(messages)
    with scoped_workdir(tmp_path), scoped_session("preserve-users"):
        result = module.auto_compact(messages, _CompactionClient(summary_json('continue')), "fake-model")
    originals = [m for m in result if m.get("_nz_message_id", "").startswith("msg-") and m["role"] == "user"]
    assert originals == [m for m in before if m["role"] == "user"]
    assert messages == before


def test_continuation_wire_preserves_originals_and_omits_old_current_pass():
    from nz_coder.runtime.conversation.message_projection import project_provider_messages
    messages = _history()[:3] + [{
        "role": "assistant", "content": "stopped", "_nz_continuation": {
            "version": 1, "status": "max_turns", "summary": "CURRENT CHECK: OLD_TEST_PASSED",
        },
    }, {"role": "user", "content": "继续"}]
    before = copy.deepcopy(messages)
    wire = project_provider_messages(messages, authoritative_state=True)
    text = "\n".join(m.get("content", "") for m in wire)
    assert "OLD_TEST_PASSED" not in text
    # Provider 投影会合并相邻用户消息，正文及其时间顺序仍须完整。
    originals = ["修复 parser；保留公开 API。", "补充：禁止修改 tests/fixtures。", "继续"]
    assert [text.index(item) for item in originals] == sorted(text.index(item) for item in originals)
    assert "large observation" not in text
    assert messages == before


def test_untrusted_boundary_is_explicitly_historical():
    from nz_coder.runtime.conversation.message_projection import project_provider_messages
    messages = [{"role": "user", "content": "task"}, {
        "role": "assistant", "content": "stopped", "_nz_continuation": {
            "version": 1, "status": "interrupted", "summary": "tests passed at old generation",
        },
    }, {"role": "user", "content": "继续"}]
    text = "\n".join(m["content"] for m in project_provider_messages(messages))
    assert "current verification unknown" in text.lower()
    assert "task" in text


def test_summary_input_receives_current_facts_even_when_old_material_is_omitted(tmp_path, monkeypatch):
    from nz_coder.state import context as module
    from tests.test_context_budget import _CompactionClient
    monkeypatch.setattr(module, "_get_git_diff_summary", lambda: "")
    client = _CompactionClient(summary_json('continue'))
    observed = []
    with scoped_workdir(tmp_path), scoped_session("summary-facts"):
        result = module.auto_compact(_history(), client, "fake-model", current_facts="<current-task-facts>UNRESOLVED_API</current-task-facts>",
                                     observer=lambda name, payload: observed.append((name, payload)))
    sent = str(client.request)
    assert "UNRESOLVED_API" in sent
    operation = result[0]["_nz_compaction"]["operation_id"]
    coverage = next(payload for name, payload in observed if name == "compaction_selection")
    assert coverage["operation_id"] == operation
    assert coverage["candidate_messages"] > 0
    assert coverage["selected_messages"] <= coverage["candidate_messages"]
    assert all(payload["compaction_operation_id"] == operation for name, payload in observed if name.startswith("model_call_"))
    assert coverage["actual_cost"] is None


def test_current_facts_never_promote_old_verification(tmp_path):
    from nz_coder.runtime.conversation.continuation_context import build_current_facts
    state = RuntimeState(initial_task_text="repair parser", mutation_generation=3, changed_files=["parser.py"],
                         verification_contract={"command": "pytest tests/test_parser.py", "cwd": ".", "passed": True, "attempted_generation": 2})
    block = build_current_facts(state, workspace=tmp_path)
    assert "parser.py" in block and "pytest tests/test_parser.py" in block
    assert "current verification unknown" in block.lower()
    assert "status=passed" not in block
    state.verification_contract["attempted_generation"] = 3
    assert "status=passed" in build_current_facts(state, workspace=tmp_path)


def test_current_fact_budget_omits_whole_items_and_preserves_tags(tmp_path):
    from nz_coder.runtime.conversation.continuation_context import build_current_facts
    from nz_coder.runtime.conversation.prompt_builder import estimate_text_tokens
    state = RuntimeState(initial_task_text="repair", changed_files=["module/" + "x" * 1000 + ".py"] * 20)
    block = build_current_facts(state, workspace=tmp_path, max_tokens=180)
    assert estimate_text_tokens(block) <= 180
    assert block.endswith("</current-task-facts>")
    assert "omitted" in block and "x" * 50 not in block


def test_optional_cjk_context_stays_inside_budget():
    from nz_coder.runtime.conversation.prompt_builder import truncate_text_tokens, estimate_text_tokens
    result = truncate_text_tokens("中" * 10000, 100)
    assert estimate_text_tokens(result) <= 100


def test_formal_request_emits_current_facts_once(monkeypatch):
    from tests.runtime.test_request_snapshot import _host
    from nz_coder.runtime.conversation.prompt_builder import ProductionPromptBuilder
    host, _tools, _hooks = _host(monkeypatch)
    host.runtime_state = RuntimeState(initial_task_text="repair parser", mutation_generation=4,
                                      verification_contract={"command": "pytest check.py", "attempted_generation": 3, "passed": True})
    host._implementation_bundle_block = lambda _query, **_kw: ""
    wire = ProductionPromptBuilder().build(host, [{"role": "user", "content": "repair parser"}])
    text = "\n".join(message["content"] for message in wire)
    assert text.count("<current-task-facts>") == 1
    assert text.count("pytest check.py") == 1
    assert "Current verification unknown" in text


def test_agent_compaction_supplies_authoritative_facts(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.state.context import prompt_budget
    captured = {}
    def compact(messages, *_args, **kwargs):
        captured.update(kwargs)
        return messages
    monkeypatch.setattr("nz_coder.runtime.execution.loop.auto_compact", compact)
    host = SimpleNamespace(runtime_state=RuntimeState(initial_task_text="repair parser"),
                           client=None, _active_model_id=lambda: "fake-model",
                           _model_gateway_observer_for_run=lambda: None,
                           _prompt_budget=prompt_budget)
    with scoped_workdir(tmp_path):
        AgentLoop._compact_messages(host, [{"role": "user", "content": "repair parser"}])
    assert "<current-task-facts>" in captured["current_facts"]


def test_workset_can_leave_requirement_descriptions_to_current_facts(tmp_path):
    from tests.test_implementation_bundle import _contract
    from nz_coder.intelligence.implementation_bundle import build_implementation_bundle
    block = build_implementation_bundle(query="cron", contract=_contract(tmp_path), execution_facts={},
                                         workspace=tmp_path, include_requirements=False)
    assert "Support JAN-DEC names" not in block
    assert "Preserve numeric fields" in block
    assert "cron_engine/parser.py" in block


def test_repeated_continuation_cannot_reintroduce_previous_free_text_pass():
    from nz_coder.runtime.conversation.continuation_context import build_continuation_boundary
    from nz_coder.runtime.conversation.message_projection import project_provider_messages
    state = RuntimeState(initial_task_text="repair parser", mutation_generation=4)
    messages = [{"role": "user", "content": "repair parser"},
                {"role": "assistant", "content": "earlier stop", "_nz_continuation": {
                    "version": 1, "status": "max_turns", "summary": "OLD_TEST_PASSED",
                }}, {"role": "user", "content": "继续"}]
    boundary = build_continuation_boundary(messages, status="max_turns", runtime_state=state)
    messages.extend([{"role": "assistant", "content": "stopped", "_nz_continuation": boundary},
                     {"role": "user", "content": "继续"}])
    wire = project_provider_messages(messages, authoritative_state=True)
    assert "OLD_TEST_PASSED" not in str(wire)
    assert "repair parser" in str(wire)


def test_scoped_static_verification_survives_and_is_invalidated_by_edit(tmp_path):
    from nz_coder.runtime.conversation.continuation_context import build_current_facts
    from types import SimpleNamespace
    state = RuntimeState(initial_task_text="repair", mutation_generation=3, verification_generation=3,
                         changed_files_verified=True, py_compile_ok=True, verification_attempts=1)
    evidence = SimpleNamespace(verification_results=[{"tool": "verify_changed_files", "command": "verify_changed_files", "status": "passed"}], verification_evidence=[])
    block = build_current_facts(state, workspace=tmp_path, run_evidence=evidence)
    assert "Scoped changed-file verification: status=passed" in block
    assert "verify_changed_files" in block
    assert "not task acceptance" in block
    state.mutation_generation += 1
    assert "Scoped changed-file verification: status=passed" not in build_current_facts(state, workspace=tmp_path, run_evidence=evidence)
