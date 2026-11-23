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
    from nz_coder.runtime.verification.domain.requirements import TaskContract
    from tests.verification_scenarios import VerificationScenario

    source = tmp_path / "parser.py"
    source.write_text("def parse(value): return int(value)\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").touch()
    (tests / "test_parser.py").write_text(
        "from parser import parse\n\ndef test_parse(): assert parse('2') == 2\n"
    )
    command = "pytest tests/test_parser.py"
    scenario = VerificationScenario(TaskContract(acceptance_commands=(command,)),
                                    workspace=tmp_path, plan={"stages": []})
    scenario.settle_shell(command)
    first_run, = scenario.domain.snapshot().executions
    assert first_run.test_count == 1 and first_run.outcome == "succeeded"
    assert "Declared verification: status=passed" in build_current_facts(scenario.state, workspace=tmp_path)

    source.write_text("def parse(value): return int(value.strip())\n")
    scenario.runtime.reconcile_workspace()
    block = build_current_facts(scenario.state, workspace=tmp_path)
    assert "Changed file: parser.py" in block and command in block
    assert "Declared verification: status=pending" in block
    assert "status=passed" not in block

    scenario.settle_shell(command)
    current = scenario.domain.snapshot()
    assert current.executions[-1].after.acceptance_generation > first_run.after.acceptance_generation
    assert current.executions[-1].test_count == 1
    assert "Declared verification: status=passed" in build_current_facts(scenario.state, workspace=tmp_path)


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


def test_formal_request_emits_current_facts_once(tmp_path, monkeypatch):
    from tests.runtime.test_request_snapshot import _host
    from nz_coder.runtime.conversation.prompt_builder import ProductionPromptBuilder
    from nz_coder.runtime.verification.domain.requirements import TaskContract
    from tests.verification_scenarios import VerificationScenario

    command = "pytest check.py"
    (tmp_path / "parser.py").write_text("value = 1\n")
    (tmp_path / "check.py").write_text("from parser import value\n\ndef test_value(): assert value > 0\n")
    scenario = VerificationScenario(TaskContract(acceptance_commands=(command,)),
                                    workspace=tmp_path, plan={"stages": []})
    scenario.state.initial_task_text = "repair parser"
    scenario.settle_shell(command)
    (tmp_path / "parser.py").write_text("value = 22\n")
    scenario.runtime.reconcile_workspace()
    host, _tools, _hooks = _host(monkeypatch)
    host.runtime_state = scenario.state
    host._implementation_bundle_block = lambda _query, **_kw: ""
    with scoped_workdir(tmp_path):
        wire = ProductionPromptBuilder().build(host, [{"role": "user", "content": "repair parser"}])
    text = "\n".join(message["content"] for message in wire)
    assert text.count("<current-task-facts>") == 1
    assert text.count(command) == 1
    assert "Declared verification: status=pending" in text and "status=passed" not in text
    assert "workspace changed" in text and "exit=0" in text and "historical" in text.lower()


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
    import subprocess
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.conversation.continuation_context import build_current_facts
    from nz_coder.runtime.execution.tool_executor import ToolExecutor
    from nz_coder.runtime.observability.run_evidence import RunEvidence
    from nz_coder.runtime.process.workspace_scope_execution import execute_observed
    from nz_coder.tools import repo_intel  # noqa: F401 — 注册真实工具。
    from tests.verification_scenarios import VerificationScenario

    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    source = tmp_path / "parser.py"
    source.write_text("value = 1\n")
    scenario = VerificationScenario(workspace=tmp_path, plan={"stages": []})
    source.write_text("value = 22\n")
    scenario.runtime.reconcile_workspace()
    call = {"id": "check", "function": {"name": "verify_changed_files", "arguments": "{}"}}
    start = scenario.runtime.begin_tool(call)
    with scoped_workdir(tmp_path):
        result = execute_observed(
            scenario.store, start.version.generation,
            lambda: ToolExecutor(PermissionManager("auto")).execute_one(call, 0),
            on_before=lambda reference: scenario.runtime.record_before(start, reference),
        )
    result.execution_id = start.execution_id
    scenario.runtime.record_result(result)
    evidence = RunEvidence("syntax", verification_domain=scenario.domain)
    block = build_current_facts(scenario.state, workspace=tmp_path, run_evidence=evidence)
    assert "Scoped changed-file verification: status=passed" in block
    assert block.count("verify_changed_files") == 1
    assert "not task acceptance" in block
    source.write_text("value = 333\n")
    scenario.runtime.reconcile_workspace()
    assert "Scoped changed-file verification: status=passed" not in build_current_facts(
        scenario.state, workspace=tmp_path, run_evidence=evidence)


def test_partial_syntax_success_names_only_proven_changed_files(tmp_path):
    from nz_coder.runtime.conversation.continuation_context import build_current_facts
    from tests.verification_scenarios import VerificationScenario

    sub = tmp_path / "sub"
    sub.mkdir()
    (tmp_path / "module.py").write_text("value = 1\n")
    (sub / "module.py").write_text("return 1\n")
    scenario = VerificationScenario(workspace=tmp_path, plan={"stages": []})
    scenario.mutate(1, ["module.py", "sub/module.py"])
    scenario.settle_shell("python -m py_compile module.py")
    scenario.settle_shell("python -m py_compile module.py", cwd=sub)
    block = build_current_facts(scenario.state, workspace=tmp_path)
    assert "Scoped changed-file verification: status=passed" not in block
    summary, = (line for line in block.splitlines() if line.startswith("Scoped changed-file verification:"))
    assert '"module.py"' in summary and "sub/module.py" not in summary
    assert str(sub) in block and "failed" in block
    assert not scenario.state.changed_files_verified
    assert not scenario.state.py_compile_ok


def test_pending_acceptance_keeps_syntax_but_an_inflight_write_suspends_it(tmp_path):
    from nz_coder.runtime.conversation.continuation_context import build_current_facts
    from nz_coder.runtime.verification.domain.requirements import TaskContract
    from tests.verification_scenarios import VerificationScenario

    source = tmp_path / "module.py"
    source.write_text("value = 1\n")
    scenario = VerificationScenario(TaskContract(acceptance_commands=("pytest smoke.py",)),
                                    workspace=tmp_path, plan={"stages": []})
    source.write_text("value = 22\n")
    scenario.runtime.reconcile_workspace()
    scenario.settle_shell("python -m py_compile module.py")
    assert not scenario.domain.snapshot().execution_ready
    assert scenario.state.changed_files_verified

    def current():
        return build_current_facts(scenario.state, workspace=tmp_path)

    scenario.domain.begin_execution("read-only", (), verifies=True)
    assert "Scoped changed-file verification: status=passed" in current()
    scenario.domain.end_execution("read-only")
    scenario.domain.begin_execution("no-op-write", (), mutates=True)
    assert not scenario.state.changed_files_verified
    scenario.domain.end_execution("no-op-write")
    assert scenario.state.changed_files_verified

    scenario.domain.begin_execution("write", (), mutates=True)
    source.write_text("return 1\n")
    during = current()
    assert "status=passed" not in during and "Verified check" not in during
    assert "historical" in during.lower() and "exit=0" in during
    scenario.runtime.reconcile_workspace()
    scenario.domain.end_execution("write")
    assert not scenario.state.changed_files_verified
    assert "Scoped changed-file verification: status=passed" not in current()


def test_concurrent_edit_cannot_mix_new_paths_into_older_snapshot(tmp_path, monkeypatch):
    import threading
    from nz_coder.runtime.conversation.continuation_context import build_current_facts
    from tests.verification_scenarios import VerificationScenario

    (tmp_path / "first.py").write_text("value = 1\n")
    scenario = VerificationScenario(workspace=tmp_path, plan={"stages": []})
    scenario.mutate(1, ["first.py"])
    scenario.settle_shell("python -m py_compile first.py")
    snapshot_read, writer_tried = threading.Event(), threading.Event()
    original = scenario.domain.snapshot

    def snapshot():
        view = original()
        if not snapshot_read.is_set():
            snapshot_read.set()
            assert writer_tried.wait(2)
        return view

    def write_next_generation():
        assert snapshot_read.wait(2)
        lock = scenario.context._state_lock
        if lock.acquire(blocking=False):
            try:
                scenario.mutate(2, ["second.py"])
            finally:
                lock.release()
                writer_tried.set()
        else:
            writer_tried.set()
            scenario.mutate(2, ["second.py"])

    monkeypatch.setattr(scenario.domain, "snapshot", snapshot)
    writer = threading.Thread(target=write_next_generation)
    writer.start()
    try:
        text = build_current_facts(scenario.state, workspace=tmp_path)
    finally:
        writer.join(3)
    assert not writer.is_alive() and scenario.state.mutation_generation == 2
    assert "Workspace mutation generation: 1" in text and "second.py" not in text
    assert "Scoped changed-file verification: status=passed" in text


def test_unbound_checkpoint_flags_do_not_establish_current_syntax_or_acceptance(tmp_path):
    from nz_coder.runtime.conversation.continuation_context import build_current_facts

    saved = {"changed_files": ["app.py"], "changed_files_verified": True, "py_compile_ok": True,
             "verification_generation": 3, "mutation_generation": 3,
             "verification": {"acceptance_checks": [
                 {"command": "pytest app.py", "cwd": str(tmp_path), "status": "passed"}]}}
    block = build_current_facts(saved, workspace=tmp_path)
    assert "status=passed" not in block
    assert "Declared verification: status=pending" in block and "pytest app.py" in block


def test_failed_receipts_keep_their_command_environment_targets_and_details(tmp_path):
    from nz_coder.runtime.conversation.continuation_context import build_current_facts
    from nz_coder.runtime.verification.domain.identity import command_identity
    from nz_coder.runtime.verification.domain.models import ExecutionFact
    from tests.verification_scenarios import VerificationScenario

    scenario = VerificationScenario(workspace=tmp_path, plan={"stages": []})
    version = scenario.domain.snapshot().version
    commands = ["MODE=dev bash smoke.sh", "MODE=prod bash smoke.sh"]
    for index, (mode, target) in enumerate([(0, "a"), (0, "b"), (1, "b")]):
        identity = command_identity(commands[mode], str(tmp_path))
        scenario.domain.observe_execution(ExecutionFact(
            f"failed-{index}", scenario.domain.scope, identity, "failed", version, version,
            ("custom_assert",), targets=(target,), exit_code=index + 1, reason=f"problem-{index}"))
    text = build_current_facts(scenario.state, workspace=tmp_path)
    assert all(text.count(command) == 1 for command in commands)
    assert all(f"exit={index + 1}" in text and f"problem-{index}" in text for index in range(3))
    assert 'targets="a"' in text and 'targets="b"' in text


def test_restore_keeps_syntax_history_without_granting_a_new_run_a_pass(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace
    import threading
    from nz_coder.runtime.conversation.continuation_context import build_current_facts
    from nz_coder.runtime.verification.domain.checkpoint import restore_domain
    from tests.verification_scenarios import VerificationScenario

    (tmp_path / "module.py").write_text("value = 1\n")
    scenario = VerificationScenario(workspace=tmp_path, plan={"stages": []})
    scenario.mutate(1, ["module.py"])
    scenario.settle_shell("python -m py_compile module.py")
    assert scenario.state.py_compile_ok

    restored = restore_domain(scenario.domain.checkpoint(), lock=threading.RLock(),
                              scope=replace(scenario.domain.scope, interaction_run_id="resumed"),
                              version=scenario.domain.snapshot().version)
    state = SimpleNamespace(verification_domain=restored, mutation_generation=1, changed_files=["module.py"])
    block = build_current_facts(state, workspace=tmp_path)
    assert "Verified check" not in block and "status=passed" not in block
    assert "Historical execution" in block and "exit=0" in block and "generation=1" in block
    assert "activation environment requires confirmation" in block


def test_child_context_cannot_replay_a_saved_parent_syntax_pass(tmp_path, monkeypatch):
    import json
    from nz_coder.runtime.agent import subagent

    path = tmp_path / "parent.json"
    path.write_text(json.dumps({"changed_files": ["parser.py"], "py_compile_ok": True,
                                "verification_attempts": 1, "verification_generation": 2}))
    monkeypatch.setattr(subagent, "session_runtime_state_path", lambda _session_id: path)
    block = subagent._parent_context_block("parent")
    assert "parser.py" in block and "Parent RuntimeState" in block
    assert "py_compile_ok" not in block and "status=passed" not in block


def test_budget_never_misattributes_cross_command_proof(tmp_path):
    from nz_coder.runtime.adapters.verification_plans import check_spec
    from nz_coder.runtime.conversation.continuation_context import build_current_facts
    from nz_coder.runtime.verification.domain.identity import command_identity
    from nz_coder.runtime.verification.domain.models import ExecutionFact
    from tests.verification_scenarios import VerificationScenario

    task = VerificationScenario(workspace=tmp_path, plan={"stages": []})
    plan = check_spec("verify_changed_files", str(tmp_path), capability="syntax", targets=("x.py",),
                      required=True, source="planner", coverage_mode="capability_targets")
    task.domain.register_check(plan)
    version = task.domain.snapshot().version
    actual = command_identity("python_symbol_check x.py", str(tmp_path))
    task.domain.observe_execution(ExecutionFact("shared-receipt", task.domain.scope, actual, "succeeded",
        version, version, ("syntax",), targets=("x.py",), in_process=True))
    full = build_current_facts(task.state, workspace=tmp_path)
    assert "verify_changed_files" in full and actual.original in full and full.count("shared-receipt") == 2
    # 找到能保留计划结果、刚好放不下实际执行条目的窗口。临时目录长度因平台而异。
    from nz_coder.runtime.conversation.prompt_builder import estimate_text_tokens
    trimmed = build_current_facts(task.state, workspace=tmp_path, max_tokens=estimate_text_tokens(full) - 10)
    assert "Verified check" in trimmed and actual.original not in trimmed
    assert "a different command supplied the evidence" in trimmed and "omitted" in trimmed
