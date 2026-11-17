"""模型选择的目标测试必须通过真实执行结果进入需求清单。"""
from __future__ import annotations

from dataclasses import asdict
import copy
import json
import os
from pathlib import Path

import pytest

import test_node_verification_runtime as node_runtime
from test_node_verification_runtime import EDIT, READ, _run, _tool
from test_planning_switch_runtime import PLANNER, TASK, _prepare


node_project = node_runtime.node_project
COMMAND = "node --test index.test.cjs"


def _save_evidence(case, result, state, trace, requests, auxiliary, permissions):
    evidence_root = os.environ.get("NZ_REQUIREMENT_VERIFICATION_EVIDENCE")
    if not evidence_root:
        return
    target = Path(evidence_root) / case
    target.mkdir(parents=True, exist_ok=True)
    for name, value in {
        "result": asdict(result), "state": state, "runtime": trace,
        "requests": requests, "auxiliary": auxiliary, "permissions": permissions,
    }.items():
        (target / f"{name}.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        )


@pytest.mark.parametrize("shell_effect", ["cleanup", "delete-entry", "directory-entry"])
def test_project_artifact_completion_uses_real_files_after_mutating_shell(
    monkeypatch, tmp_path, shell_effect,
):
    for key in ("NZ_PLANNING_ENABLED", "NZ_REFLECTION_ENABLED",
                "NZ_MEMORY_AUTO_EXTRACT", "NZ_MEMORY_AUTO_DREAM"):
        monkeypatch.setenv(key, "0")
    monkeypatch.setenv("NZ_PROVIDER_MAX_RETRIES", "0")
    cleanup = {
        "cleanup": "rm -rf .smoke",
        "delete-entry": "rm taskboard/__init__.py",
        "directory-entry": "rm taskboard/__init__.py && mkdir taskboard/__init__.py",
    }[shell_effect]
    # 此处验证产物结清，使用明确模块；discover 的策略另有独立回归。
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/__init__.py").write_text("")
    command = "python -m unittest tests.test_app -v"
    test_source = (
        "import subprocess, sys, unittest\n"
        "from taskboard.app import value\n"
        "class AppTest(unittest.TestCase):\n"
        "    def test_value(self):\n"
        "        self.assertEqual(value, 2)\n"
        "    def test_module(self):\n"
        "        result = subprocess.run([sys.executable, '-m', 'taskboard'], "
        "text=True, capture_output=True, timeout=5)\n"
        "        self.assertEqual(result.returncode, 0, result.stderr)\n"
        "        self.assertEqual(result.stdout.strip(), '2')\n"
    )
    actions = [
        [_tool("write_file", path="taskboard/__init__.py", content="# TaskBoard\n")],
        [_tool("write_file", path="taskboard/__main__.py",
               content="from .app import value\nprint(value)\n")],
        [_tool("write_files_batch", files=[
            {"path": "taskboard/app.py", "content": "value = 2\n"},
            {"path": "tests/test_app.py", "content": test_source},
        ])],
        [_tool("bash", command=cleanup)],
        [_tool("bash", command=command)],
        "The project is delivered; the local tests passed.",
    ]
    static_commands = tuple(
        "python -m py_compile " + path for path in (
            "taskboard/__init__.py", "taskboard/__main__.py", "taskboard/app.py",
        )
    )
    if shell_effect == "cleanup":
        # 静态义务仍需完成；完整核对能证明清理不存在的目录没有副作用。
        actions.insert(-1, [_tool("bash", command=check) for check in static_commands])
    result, state, trace, requests, auxiliary, permissions = _run(
        monkeypatch, tmp_path, actions,
        task="Create taskboard/__init__.py and taskboard/__main__.py. "
             "Implement the small project and check it with local tests.",
        allowed_commands=(cleanup, command, *static_commands),
        limit=len(actions) + 1 if shell_effect == "cleanup" else None,
    )
    _save_evidence("artifact-project-" + shell_effect, result, state, trace,
                   requests, auxiliary, permissions)

    assert "Ran 2 tests" in str(requests[-1]["messages"])
    assert "OK" in str(requests[-1]["messages"])
    calls = [event for event in trace if event["event"] == "tool_call"
             and event["name"] == "bash"
             and not event.get("input", {}).get("_nz_runtime_verification_stage")]
    assert len(calls) == (5 if shell_effect == "cleanup" else 2)
    assert all(event["executed"] and not event.get("dispatch_failed")
               and not event.get("command_failed") for event in calls)
    assert state["verification_contract"] == {}
    assert state["mutation_generation"] == (3 if shell_effect == "cleanup" else 4)
    statuses = [item["status"] for item in state["requirement_ledger"]["items"]]
    if shell_effect == "cleanup":
        assert result.status.value == "completed", result.final_text
        assert statuses == ["satisfied", "satisfied"]
        assert state["completion_gate_prompts"] == 0
        assert result.metadata["verification"]["mutation_scope_unknown"] is False
    else:
        assert result.status.value != "completed", result.final_text
        assert statuses == ["candidate", "satisfied"]
    # 文件当前状态不能冒充新写入、测试关联或独立审查证据。
    assert all(
        evidence["type"] == "file_created" and evidence["generation"] in {1, 2}
        for item in state["requirement_ledger"]["items"]
        for evidence in item["evidence"]
    )
    # 只有全部范围与检查结清后，才允许正常的终审；其他分支不启动语义审查。
    assert len(auxiliary) == (1 if shell_effect == "cleanup" else 0)
    assert all([tool['function']['name'] for tool in request['tools']]
               == ['emit_sidecar_verdict'] for request in auxiliary)


def test_deleting_real_target_invalidates_delivered_symlink_artifact(monkeypatch, tmp_path):
    for key in ("NZ_PLANNING_ENABLED", "NZ_REFLECTION_ENABLED",
                "NZ_MEMORY_AUTO_EXTRACT", "NZ_MEMORY_AUTO_DREAM"):
        monkeypatch.setenv(key, "0")
    (tmp_path / "alias.py").symlink_to(tmp_path / "real.py")
    actions = [
        [_tool("write_file", path="alias.py", content="value = 2\n")],
        [_tool("apply_patch", changes=[{"op": "delete", "path": "real.py"}])],
        "The requested file was updated.",
    ]
    result, state, trace, requests, auxiliary, permissions = _run(
        monkeypatch, tmp_path, actions, task="Create alias.py.",
    )
    _save_evidence("artifact-alias-target-delete", result, state, trace,
                   requests, auxiliary, permissions)

    writes = [event for event in trace if event["event"] == "tool_call"
              and event["name"] in {"write_file", "apply_patch"}]
    assert len(writes) == 2
    assert all(event["executed"] and not event.get("dispatch_failed") for event in writes)
    assert not (tmp_path / "alias.py").is_file()
    assert state["requirement_ledger"]["items"][0]["status"] == "candidate"
    assert state["requirement_ledger"]["items"][0]["evidence"] == []
    assert result.status.value != "completed", result.final_text


@pytest.mark.parametrize("case", [
    "passed", "failed", "denied", "stale", "masked", "ignored-after-pass", "unrelated",
    "unrelated-with-source-name-in-other-segment", "review-required",
])
def test_model_targeted_verification_updates_only_current_related_requirements(
    monkeypatch, node_project, case,
):
    _prepare(monkeypatch, node_project, enabled=True)
    (node_project / "index.test.cjs").write_bytes(
        (node_project / "literal.test.cjs").read_bytes(),
    )
    planner = copy.deepcopy(PLANNER)
    if case.startswith("unrelated"):
        (node_project / "unrelated.test.cjs").write_text(
            "const test = require('node:test');\n"
            "const assert = require('node:assert/strict');\n"
            "test('independent arithmetic', () => assert.equal(1 + 1, 2));\n",
        )
    if case != "passed":
        for requirement in planner["requirements"]:
            requirement["required_evidence"] = ["semantic_review"]
    edit = EDIT
    if case in {"failed", "masked"}:
        edit = [_tool("edit_file", path="index.js",
                      old_text="x.replace(/-/g, '\\\\-')", new_text="'['")]
    command = COMMAND
    if case == "unrelated":
        command = "node --test unrelated.test.cjs"
    elif case == "unrelated-with-source-name-in-other-segment":
        command = "node --test unrelated.test.cjs && printf index"
    elif case == "masked":
        command += " | tail -n 0 || true"
    final_tools = [_tool("bash", command=command)]
    if case == "stale":
        final_tools.append(_tool(
            "edit_file", path="index.js", old_text="=> x;", new_text="=> '[';",
        ))
    initial_command = command if case.startswith("unrelated") else COMMAND
    actions = [READ, [_tool("bash", command=initial_command)], edit, final_tools, "Done"]
    if case == "ignored-after-pass":
        command = COMMAND + " | tail -n 0 || true"
        actions = [READ, [_tool("bash", command=COMMAND)], EDIT,
                   [_tool("bash", command=COMMAND)],
                   [_tool("edit_file", path="index.js", old_text="=> x;", new_text="=> '[';")],
                   [_tool("bash", command=command)], "Done"]
    result, state, trace, requests, auxiliary, permissions = _run(
        monkeypatch, node_project, actions, task=TASK, limit=len(actions),
        planning_response=json.dumps(planner), permission=case != "denied",
        allowed_commands=(COMMAND, command),
    )
    _save_evidence(case, result, state, trace, requests, auxiliary, permissions)
    # 规划建议仍在工作计划中，不能变成用户硬验收或独立进度所有者。
    assert all(item["requirement"]["kind"] == "artifact" for item in state["requirement_ledger"]["items"])
    assert state["task_contract"]["acceptance_commands"] == []
    assert state["verification_contract"] == {}
    assert state["plan_generated"]
    verification = result.metadata["verification"]
    assert not any(item["required"] or item["automation_provenance"] == "user_declared"
        for stage in verification["verification_pipeline"]["stages"] for item in stage["commands"])
    if case == "denied":
        assert not any(event["event"] == "tool_call" and event.get("name") == "bash" and event.get("executed") for event in trace)
    if result.status.value == "completed":
        assert any(event["event"] == "sidecar_finished" and event.get("verdict") == "accept" for event in trace)


@pytest.mark.parametrize("verdict", ["revise", RuntimeError("offline reviewer unavailable")])
def test_related_pass_cannot_replace_independent_semantic_acceptance(
    monkeypatch, node_project, verdict,
):
    _prepare(monkeypatch, node_project, enabled=True)
    (node_project / "index.test.cjs").write_bytes(
        (node_project / "literal.test.cjs").read_bytes(),
    )
    planner = copy.deepcopy(PLANNER)
    for requirement in planner["requirements"]:
        requirement["required_evidence"] = ["semantic_review"]
    result, state, trace, *_ = _run(
        monkeypatch, node_project,
        [READ, [_tool("bash", command=COMMAND)], EDIT,
         [_tool("bash", command=COMMAND)], "Done"],
        task=TASK, planning_response=json.dumps(planner),
        allowed_commands=(COMMAND,), semantic_response=verdict,
    )
    assert result.status.value == "max_turns"
    assert any(e["event"] == "sidecar_finished" for e in trace)
    items = state["requirement_ledger"]["items"]
    assert all(item["status"] == "candidate" for item in items)
    assert all(e["type"] != "semantic_review_passed" for item in items for e in item["evidence"])


def test_related_pass_cannot_bypass_failing_user_acceptance(monkeypatch, node_project):
    _prepare(monkeypatch, node_project, enabled=True)
    (node_project / "index.test.cjs").write_bytes(
        (node_project / "literal.test.cjs").read_bytes(),
    )
    (node_project / "literal.test.cjs").write_text("throw Error('acceptance still fails');\n")
    planner = copy.deepcopy(PLANNER)
    for requirement in planner["requirements"]:
        requirement["required_evidence"] = ["semantic_review"]
    result, state, trace, *_ = _run(
        monkeypatch, node_project,
        [READ, EDIT, [_tool("bash", command=COMMAND)], "Done"],
        task=TASK + " Run node --test literal.test.cjs.",
        planning_response=json.dumps(planner), allowed_commands=(COMMAND,),
    )
    assert result.status.value == "max_turns"
    assert state["verification_contract"]["passed"] is False
    assert any(e["type"] == "command_passed"
               for item in state["requirement_ledger"]["items"] for e in item["evidence"])
    assert not any(e["event"] == "sidecar_finished" for e in trace)


def test_recovered_plan_reaches_edit_targeted_test_and_independent_review(
    monkeypatch, node_project,
):
    _prepare(monkeypatch, node_project, enabled=True)
    (node_project / "index.test.cjs").write_bytes(
        (node_project / "literal.test.cjs").read_bytes(),
    )
    planner = copy.deepcopy(PLANNER)
    planner["requirements"][-1]["kind"] = "compatibility"
    result, state, trace, requests, auxiliary, permissions = _run(
        monkeypatch, node_project,
        [READ, [_tool("bash", command=COMMAND)], EDIT,
         [_tool("bash", command=COMMAND)], "Done"],
        task=TASK, planning_response=[" " * 94, json.dumps(planner)],
        allowed_commands=(COMMAND,),
    )
    _save_evidence("recovered-plan-semantic-completion", result, state, trace, requests, auxiliary, permissions)
    assert result.status.value == "completed"
    assert len([event for event in trace if event["event"] == "model_call_start"
                and event.get("purpose") == "planning"]) == 1
    assert state["verification_contract"] == {}
    assert "## Plan" in str(requests[0]["messages"])
    assert len(state["requirement_ledger"]["items"]) == 1
    assert state["requirement_ledger"]["items"][0]["status"] == "satisfied"
    review_requests = [request for request in auxiliary if request.get("tools")]
    assert "node --test index.test.cjs" in str(review_requests)
    assert "Exact acceptance (passed-current-generation):" not in str(review_requests)
    events = [event["event"] for event in trace]
    assert events.index("planning_output_invalid") < events.index("planning_done")
    assert events.index("verification_result") < events.index("sidecar_finished")


def test_scheduled_zero_test_run_cannot_satisfy_requirements(monkeypatch, node_project):
    _prepare(monkeypatch, node_project, enabled=True)
    (node_project / "tests").mkdir()
    (node_project / "tests/runtests.py").write_text(
        "from pathlib import Path\n"
        "import unittest\n"
        "class IndexTest(unittest.TestCase):\n"
        "    def test_literal(self):\n"
        "        self.assertNotIn('replace', Path('index.js').read_text())\n"
        # 模拟原生 runner 选中零个测试仍退出 0，不能把此结果作为需求完成证据。
        "suite = unittest.defaultTestLoader.loadTestsFromTestCase(IndexTest)\n"
        "if 'replace' not in Path('index.js').read_text():\n"
        "    suite = unittest.TestSuite()\n"
        "result = unittest.TextTestRunner().run(suite)\n"
        "raise SystemExit(not result.wasSuccessful())\n",
    )
    command = "python tests/runtests.py test_index"
    actions = [
        [*READ, _tool("read_file", path="tests/runtests.py")],
        [_tool("bash", command=command)],
        EDIT,
        [_tool("bash", command=command)],
        "Done",
    ]
    result, state, trace, requests, auxiliary, permissions = _run(
        monkeypatch, node_project, actions, task=TASK, limit=len(actions),
        planning_response=json.dumps(PLANNER), allowed_commands=(command,),
    )
    _save_evidence("scheduled-zero-tests", result, state, trace, requests, auxiliary, permissions)
    observations = [event for event in trace if event["event"] == "verification_result"]
    # 新域统一把零测试记为待验证，不能作为已通过事实。
    assert observations[-1]["status"] == "pending"
    assert "Ran 0 tests" in observations[-1]["output"]
    assert not any(event["event"] == "verification_stage_executed" for event in trace)
    assert all(item["requirement"]["kind"] == "artifact" for item in state["requirement_ledger"]["items"])
    assert result.metadata["verification"]["tests_passed"] is None
    assert result.metadata["verification"]["ignored_verifications"]


def test_scheduled_redirected_test_keeps_generation_and_dependent_requirements(
    monkeypatch, node_project,
):
    _prepare(monkeypatch, node_project, enabled=True)
    (node_project / "index.test.cjs").write_bytes(
        (node_project / "literal.test.cjs").read_bytes(),
    )
    planner = copy.deepcopy(PLANNER)
    planner["requirements"][1]["depends_on"] = ["R1"]
    planner["requirements"][2]["depends_on"] = ["R2"]
    (node_project / "tests").mkdir()
    (node_project / "tests/runtests.py").write_text(
        "import os\n"
        "import subprocess\n"
        "import unittest\n"
        "class IndexTest(unittest.TestCase):\n"
        "    def test_literal(self):\n"
        "        result = subprocess.run(['node', '--test', 'index.test.cjs'],\n"
        "                                capture_output=True, text=True)\n"
        "        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)\n"
        "        self.assertEqual(os.environ['VERIFY_LABEL'], 'a  b')\n"
        "unittest.main(argv=[__file__])\n",
    )
    command = "VERIFY_LABEL='a  b' python tests/runtests.py test_index 2>&1"
    actions = [READ, [_tool("bash", command=command)], EDIT, [_tool("bash", command=command)], "Done"]

    result, state, trace, requests, auxiliary, permissions = _run(
        monkeypatch, node_project, actions, task=TASK, limit=len(actions),
        planning_response=json.dumps(planner),
        allowed_commands=(command, command.replace("a  b", "a b")),
    )

    _save_evidence("scheduled-redirection", result, state, trace, requests, auxiliary, permissions)
    assert not any(event["event"] == "verification_stage_executed" for event in trace)
    assert result.status.value == "completed", result.final_text
    assert state["mutation_generation"] == 1
    assert all(item["requirement"]["kind"] == "artifact" for item in state["requirement_ledger"]["items"])
    assert any(event["event"] == "verification_result" and event.get("command") == command
               and event.get("status") == "passed" for event in trace)
