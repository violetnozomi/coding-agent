"""编辑后的测试工具查询必须穿过真实准入、执行与模型结果投影。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import test_node_verification_runtime as node_runtime
from test_node_verification_runtime import EDIT, READ, TEST, _run, _tool

node_project = node_runtime.node_project

QUERIES = [
    "ls node_modules/.bin | grep -i jest",
    "ls . | head -40; echo ---; grep -n '\"test\"\\|jest' package.json | head -40",
    "ls packages/components | head; echo '=== bin ==='; ls node_modules/.bin | grep -i -E '^(jest|tsc|eslint)$'",
    "ls node_modules/.bin/ | grep -iE 'jest|tsc'",
    "find packages/shared/test -type f | head -40; echo '=== root pkg test ==='; "
    "grep -n '\"test\"\\|jest' package.json | head -20; echo '=== jest configs in shared ==='; "
    "ls packages/shared/test 2>/dev/null; "
    "find packages/shared -maxdepth 2 -name 'jest*' -o -maxdepth 2 -name '*.config.*' | head",
]


def _prepare_query_files(workspace):
    for name in ["node_modules/.bin/jest", "packages/components/component.js",
                 "packages/shared/test/example.js", "packages/shared/jest.config.js"]:
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("// query fixture\n")
    (workspace / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))


def _save_replay(command, result, state, trace, requests, auxiliary, permissions):
    destination = os.environ.get("NZ_TEST_QUERY_EVIDENCE_DIR")
    if not destination or command != QUERIES[0]:
        return
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    for name, value in {
        "requests": requests, "auxiliary-requests": auxiliary,
        "runtime": trace, "state": state, "permissions": permissions,
    }.items():
        (root / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    (root / "result.json").write_text(json.dumps({"status": result.status.value}) + "\n")


@pytest.mark.parametrize("command", QUERIES)
def test_read_only_queries_after_edit_reach_model_and_real_retest(monkeypatch, node_project, command):
    _prepare_query_files(node_project)
    result, state, trace, requests, auxiliary, permissions = _run(
        monkeypatch, node_project,
        [READ, TEST, EDIT, [_tool("diff_status")], [_tool("bash", command=command)], TEST],
        limit=12, allowed_commands=(command,),
    )
    _save_replay(command, result, state, trace, requests, auxiliary, permissions)
    diff = next(e for e in trace if e.get("event") == "tool_call" and e.get("name") == "diff_status")
    assert "has_non_empty_diff: true" in diff["output"]
    query = next(e for e in trace if e.get("event") == "tool_call" and e.get("input", {}).get("command") == command)
    assert query["executed"] is True
    assert query["dispatch_failed"] is False
    assert query["command_failed"] is False
    assert "jest" in query["output"]
    assert "Broad test runner blocked" not in query["output"]
    delivered = [m for request in requests for m in request["messages"]
                 if m.get("role") == "tool" and m.get("tool_call_id") == query["tool_call_id"]]
    assert delivered and "jest" in delivered[0]["content"]
    assert "Broad test runner blocked" not in delivered[0]["content"]
    assert len(requests) == 6
    assert result.status.value == "completed"
    assert state["verification_contract"]["passed"] is True
    assert state["verification_generation"] == state["mutation_generation"]
    checks = [e for e in trace if e.get("event") == "verification_result"]
    assert [e["status"] for e in checks] == ["failed", "passed"]
    assert all(e["command"] == "node --test literal.test.cjs" for e in checks)


@pytest.mark.parametrize("command", [
    "ls node_modules/.bin | grep jest && python -m pytest tests/",
    "cat tests/test_sentinel.py && python -m pytest tests/",
])
def test_query_cannot_hide_real_broad_test_after_edit(monkeypatch, node_project, command):
    _prepare_query_files(node_project)
    tests = node_project / "tests"
    tests.mkdir()
    (tests / "test_sentinel.py").write_text(
        "from pathlib import Path\n"
        "Path('broad-tests-ran.txt').write_text('ran')\n"
        "def test_ok(): assert True\n"
    )
    result, state, trace, requests, *_ = _run(
        monkeypatch, node_project,
        [READ, EDIT, [_tool("diff_status")], [_tool("bash", command=command)], TEST],
        limit=12, allowed_commands=(command,),
    )
    rejected = next(e for e in trace if e.get("event") == "tool_call" and e.get("input", {}).get("command") == command)
    assert rejected["dispatch_failed"] is True
    assert rejected["command_failed"] is False
    # 公开 trace 会脱敏失败输出；错误原因必须在真正的模型请求里核验。
    delivered = [m for request in requests for m in request["messages"]
                 if m.get("role") == "tool" and m.get("tool_call_id") == rejected["tool_call_id"]]
    assert delivered and "Broad test runner blocked" in delivered[0]["content"]
    assert not (node_project / "broad-tests-ran.txt").exists()
    assert result.status.value == "completed"
    assert state["verification_contract"]["passed"] is True


def test_query_fix_does_not_override_real_permission_denial(monkeypatch, node_project):
    command = "node --test literal.test.cjs"
    result, state, trace, requests, auxiliary, permissions = _run(
        monkeypatch, node_project, [READ, EDIT, TEST], permission=False,
    )
    denied = [e for e in trace if e.get("event") == "tool_call" and e.get("input", {}).get("command") == command]
    assert denied and all(e["executed"] is False for e in denied)
    assert permissions and all(p[2] is False for p in permissions)
    assert state["verification_contract"]["passed"] is not True
    assert result.status.value != "completed"


def test_sync_tool_runtime_obeys_same_query_admission(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.core.execution_context import scoped_broad_test_guard
    from nz_coder.runtime.process.workdir import scoped_workdir

    _prepare_query_files(tmp_path)
    (tmp_path / "module.js").write_text("const value = 1;\n")
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    agent = AgentLoop(
        "test", permission_mode="auto", client=object(), trace_enabled=False,
        permission_asker=lambda name, args: name == "bash" and args.get("command") in {QUERIES[0], "pytest"},
    )
    messages = []

    def invoke(call_id, name, **arguments):
        agent.tool_runtime.execute_batch_sync(agent, [{
            "id": call_id, "function": {"name": name, "arguments": arguments},
        }], messages)
        return next(m["content"] for m in messages if m.get("tool_call_id") == call_id)

    try:
        with scoped_workdir(tmp_path), scoped_broad_test_guard(True):
            invoke("edit", "edit_file", path="module.js", old_text="const value = 1;", new_text="const value = 2;")
            assert (tmp_path / "module.js").read_text() == "const value = 2;\n"
            assert "jest" in invoke("query", "bash", command=QUERIES[0])
            assert "Broad test runner blocked" in invoke("blocked", "bash", command="pytest")
            assert agent.runtime_state.broad_test_attempts == 0
            assert agent.runtime_state.exact_test_attempts == 0
    finally:
        agent.close()
