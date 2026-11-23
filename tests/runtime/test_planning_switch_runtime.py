"""现有规划开关的离线对照：真实SDK、文件工具和Node测试进程。"""
from __future__ import annotations

from dataclasses import asdict
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

import test_node_verification_runtime as node_runtime
from test_node_verification_runtime import EDIT, READ, TEST, _run, _tool


node_project = node_runtime.node_project

TASK = """Implement Unicode-safe literal handling in the existing index.js helper.
Requirements:
- A hyphenated input such as a-b must match itself as a Unicode regular expression.
- Ordinary strings must keep matching themselves.
- Repeated calls with the same input must give the same result.
Use the installed local checks to verify the change."""

PLANNER = {
    "objective": "Implement Unicode-safe literal handling and verify local behavior",
    "plan": [
        {"title": "Inspect and reproduce", "target": "index.js", "verification": "local Node check"},
        {"title": "Fix literal handling", "target": "index.js", "verification": "repeat the same check"},
    ],
    "requirements": [
        {
            "id": f"R{i}", "description": description, "kind": "behavior",
            "expected_artifacts": ["index.js"], "satisfaction_mode": "semantic",
            "depends_on": [], "required_evidence": [],
        }
        for i, description in enumerate((
            "Hyphenated input matches itself with the Unicode flag",
            "Ordinary input keeps matching itself",
            "Repeated input produces the same result",
        ), 1)
    ],
    "constraints": [],
    # 题面没有精确命令，规划替身也不能代用户创造验收命令。
    "acceptance_commands": [],
    "contract_version": 2,
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _prepare(monkeypatch, workspace, *, enabled):
    monkeypatch.setenv("NZ_PLANNING_ENABLED", "1" if enabled else "0")
    monkeypatch.setenv("NZ_PROVIDER_MAX_RETRIES", "0")
    monkeypatch.setenv("NZ_REFLECTION_ENABLED", "0")
    monkeypatch.setenv("NZ_MEMORY_AUTO_EXTRACT", "0")
    monkeypatch.setenv("NZ_MEMORY_AUTO_DREAM", "0")
    (workspace / "literal.test.cjs").write_text(
        "const test = require('node:test');\n"
        "const assert = require('node:assert/strict');\n"
        "const literal = require('./index.js');\n"
        "test('Unicode, ordinary and repeated inputs', () => {\n"
        "  for (const input of ['a-b', 'plain', 'a-b']) {\n"
        "    assert.equal(new RegExp('^' + literal(input) + '$', 'u').test(input), true);\n"
        "  }\n"
        "});\n",
    )


def _run_case(
    monkeypatch, workspace, *, case, enabled, planner, edit=EDIT, verify_changed=False, review_verdict="accept",
):
    _prepare(monkeypatch, workspace, enabled=enabled)
    before = (workspace / "index.js").read_bytes()
    test_bytes = (workspace / "literal.test.cjs").read_bytes()
    actions = [READ, TEST, edit, TEST]
    if verify_changed:
        actions.append([_tool("verify_changed_files")])
    actions.append("The scripted action sequence has ended.")
    result, state, trace, requests, auxiliary, permissions = _run(
        monkeypatch, workspace, actions, task=TASK, limit=len(actions),
        planning_response=planner, semantic_response=review_verdict,
    )
    after = (workspace / "index.js").read_bytes()
    frozen = subprocess.run(
        [shutil.which("node"), "--test", "literal.test.cjs"],
        cwd=workspace, text=True, capture_output=True, check=False,
    )
    main_text = "\n".join(str(message.get("content", "")) for message in requests[0]["messages"])
    planning_requests = [
        request for request in auxiliary
        if (request.get("messages") or [{}])[0].get("content")
        == "You are a concise coding task planner."
    ]
    summary = {
        "case": case, "planning_enabled": enabled, "verify_changed_files_used": verify_changed,
        "initial_sha256": _sha(before), "test_sha256": _sha(test_bytes),
        "final_sha256": _sha(after), "main_requests": len(requests),
        "planning_requests": len(planning_requests),
        "other_auxiliary_requests": len(auxiliary) - len(planning_requests),
        "plan_generated": state["plan_generated"],
        "task_contract_requirements": len((state.get("task_contract") or {}).get("requirements", [])),
        "requirement_ledger_items": len((state.get("requirement_ledger") or {}).get("items", [])),
        "first_main_has_plan": "## Plan" in main_text,
        "first_main_has_implementation_bundle": "<implementation-bundle>" in main_text,
        "first_main_has_all_requirements": all(item["description"] in main_text for item in PLANNER["requirements"]),
        "verification_events": [
            {"command": event.get("command"), "status": event.get("status")}
            for event in trace if event["event"] == "verification_result"
        ],
        "bash_outcomes": [
            {key: event.get(key) for key in ("executed", "dispatch_failed", "command_failed", "status")}
            for event in trace if event["event"] == "tool_call" and event["name"] == "bash"
        ],
        "status": result.status.value, "frozen_test_exit": frozen.returncode,
        "verification_contract": state["verification_contract"],
        "mutation_generation": state["mutation_generation"],
        "verification_generation": state["verification_generation"],
        "changed_files_verified": state["changed_files_verified"],
        "requirement_statuses": [
            item["status"] for item in (state.get("requirement_ledger") or {}).get("items", [])
        ],
        "terminal_boundary": next((
            {key: event.get(key) for key in (
                "reason", "status", "unresolved_requirements", "semantic_review_pending_before",
            )}
            for event in reversed(trace) if event["event"] == "terminal_boundary_settled"
        ), None),
        "sidecar_results": [
            {key: event.get(key) for key in ("verdict", "trace")}
            for event in trace if event["event"] == "sidecar_finished"
        ],
        "paid_model_requests": 0,
        "model_actions_are_scripted": True,
    }
    evidence = os.environ.get("NZ_PLANNING_SWITCH_EVIDENCE")
    if evidence:
        target = Path(evidence) / case
        target.mkdir(parents=True, exist_ok=True)
        records = {
            "summary.json": summary, "result.json": asdict(result),
            "state.json": state, "runtime.json": trace, "requests.json": requests,
            "auxiliary-requests.json": auxiliary, "permissions.json": permissions,
        }
        for name, data in records.items():
            (target / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")
        (target / "before.js").write_bytes(before)
        (target / "after.js").write_bytes(after)
        (target / "literal.test.cjs").write_bytes(test_bytes)
        (target / "model.patch").write_text("".join(difflib.unified_diff(
            before.decode().splitlines(keepends=True), after.decode().splitlines(keepends=True),
            fromfile="a/index.js", tofile="b/index.js",
        )))
        (target / "frozen-test-stdout.txt").write_text(frozen.stdout)
        (target / "frozen-test-stderr.txt").write_text(frozen.stderr)
    return summary, state, trace, requests, auxiliary, result


@pytest.mark.parametrize("verify_changed", [False, True], ids=["direct-test", "with-changed-file-check"])
@pytest.mark.parametrize("enabled", [False, True], ids=["A-off", "B-on"])
def test_planning_switch_delivers_workset_to_actual_main_request(monkeypatch, node_project, enabled, verify_changed):
    case = "B-on" if enabled else "A-off"
    if verify_changed:
        case += "-with-changed-file-check"
    summary, state, trace, requests, *_ = _run_case(
        monkeypatch, node_project, case=case, enabled=enabled,
        planner=json.dumps(PLANNER), verify_changed=verify_changed,
    )
    assert summary["main_requests"] == (6 if verify_changed else 5)
    assert summary["planning_requests"] == int(enabled)
    assert summary["plan_generated"] is enabled
    assert summary["task_contract_requirements"] == 1  # 用户点名 index.js，来自宿主而非模型三条要求。
    assert summary["requirement_ledger_items"] == 1
    assert summary["first_main_has_plan"] is enabled
    assert summary["first_main_has_implementation_bundle"] is True
    assert summary["first_main_has_all_requirements"] is False
    assert state["verification_contract"] == {}
    # 阶段路线不生成第二份验收；真实脚本结果决定是否可以结束。
    assert summary["status"] == "completed"
    assert summary["requirement_statuses"] == ["satisfied"]
    assert summary["frozen_test_exit"] == 0
    assert summary["bash_outcomes"][0]["command_failed"] is True
    assert summary["bash_outcomes"][-1]["command_failed"] is False
    assert all(not outcome["dispatch_failed"] for outcome in summary["bash_outcomes"])
    assert "Invalid regular expression" in str(requests[2]["messages"])
    assert any(event["event"] == "planning_done" for event in trace) is enabled


@pytest.mark.parametrize("kind", ["unavailable", "invalid"])
def test_planning_failure_preserves_empty_contract_and_real_execution(monkeypatch, node_project, kind):
    planner = RuntimeError("controlled planning provider unavailable") if kind == "unavailable" else "{"
    summary, state, trace, *_ = _run_case(
        monkeypatch, node_project, case=f"control-{kind}", enabled=True, planner=planner,
    )
    # 规划调用可由现有Gateway重试；逐次请求与逻辑调用分别计数，不能把重试漏算。
    planning_attempts = [
        event for event in trace
        if event["event"] == "model_call_start" and event.get("purpose") == "planning"
    ]
    planning_retries = [
        event for event in trace
        if event["event"] == "model_call_retry" and event.get("purpose") == "planning"
    ]
    assert sum(event["event"] == "planning_start" for event in trace) == 1
    logical_attempts = 1  # 用户产物要求已能生成回退路线，不追加格式请求。
    assert summary["planning_requests"] == len(planning_attempts) == logical_attempts + len(planning_retries)
    assert summary["main_requests"] == 5
    assert summary["task_contract_requirements"] == 1
    assert summary["first_main_has_implementation_bundle"] is True
    assert state["verification_contract"] == {}
    assert summary["frozen_test_exit"] == 0
    expected = "planning_failed" if kind == "unavailable" else "planning_output_invalid"
    assert any(event["event"] == expected for event in trace)


@pytest.mark.parametrize(("review_verdict", "expected_status"), [("revise", "max_turns"), ("accept", "completed")])
def test_planning_switch_retains_real_failed_test_facts(monkeypatch, node_project, review_verdict, expected_status):
    wrong_edit = [_tool("edit_file", path="index.js", old_text="x.replace(/-/g, '\\\\-')", new_text="'['")]
    summary, _, _, requests, auxiliary, result = _run_case(
        monkeypatch, node_project, case=f"control-failed-test-{review_verdict}", enabled=True,
        planner=json.dumps(PLANNER), edit=wrong_edit, review_verdict=review_verdict,
    )
    assert summary["first_main_has_plan"] is True
    assert summary["first_main_has_implementation_bundle"] is True
    assert summary["frozen_test_exit"] == 1
    assert summary["status"] == expected_status
    assert summary["sidecar_results"] == [{"verdict": review_verdict, "trace": "verifier_ok"}]
    assert len(summary["bash_outcomes"]) == 2
    assert all(run["executed"] and run["command_failed"] and not run["dispatch_failed"]
               for run in summary["bash_outcomes"])

    # 错误编辑引起的新失败应同时送达主模型和交付评审；初始版本失败的具体原因不同。
    failure = "Unterminated character class"
    assert any(failure in str(message["content"]) for message in requests[-1]["messages"])
    review, = [request for request in auxiliary
               if any(tool["function"]["name"] == "emit_sidecar_verdict" for tool in request.get("tools", []))]
    assert any(message["role"] == "user" and failure in message["content"] for message in review["messages"])

    verification = result.metadata["verification"]
    assert verification["delivery_review"]["accepted"] is (review_verdict == "accept")
    assert verification["acceptance_checks"] == []
    assert verification["tests_passed"] is None
    command = "node --test literal.test.cjs"
    assert [(item["command"], item["generation"], item["exit_code"]) for item in verification["ordinary_failures"]] == [
        (command, 0, 1), (command, 1, 1),
    ]
    assert verification["last_verification"]["status"] == "failed"
    assert failure in verification["last_verification"]["output"]
    assert command in result.final_text and "退出码 1" in result.final_text
