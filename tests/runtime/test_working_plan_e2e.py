"""真实 Python 测试失败、路线更新、代码修改及复测的完整闭环。"""
from __future__ import annotations

from test_node_verification_runtime import _run, _tool


def test_actual_failed_test_route_update_repair_and_retest(monkeypatch, tmp_path):
    from nz_coder.runtime.tool_runtime.result_projection import ProductionToolResultProjector
    executed_tests = []
    consume = ProductionToolResultProjector.consume
    def observe(self, context, dispatched, messages, **kwargs):
        for _, _, value in dispatched:
            if value.name == "bash":
                executed_tests.append((value.executed, value.command_failed, value.output))
        return consume(self, context, dispatched, messages, **kwargs)
    monkeypatch.setattr(ProductionToolResultProjector, "consume", observe)
    (tmp_path / "calc.py").write_text("def add(left, right):\n    return left - right\n")
    test_bytes = ("import unittest\nfrom calc import add\nclass Tests(unittest.TestCase):\n"
                  "    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n").encode()
    (tmp_path / "test_calc.py").write_bytes(test_bytes)
    # 同秒等长修改可能命中时间戳式 pyc；本验收明确读取当前源码。
    command = "python -B -m unittest -q test_calc"
    plan = [{"title": "Correct the arithmetic operator", "target": "calc.py", "verification": command}]
    actions = [[_tool("read_file", path="calc.py")], [_tool("bash", command=command)],
        [_tool("update_working_plan", plan=plan, expected_revision=0)],
        [_tool("edit_file", path="calc.py", old_text="return left - right", new_text="return left + right")],
        [_tool("bash", command=command)]]
    result, state, trace, requests, auxiliary, _ = _run(monkeypatch, tmp_path, actions, limit=12,
        task="Fix calc.py so add(1, 2) returns 3. Do not modify test_calc.py. Run python -B -m unittest -q test_calc and report the result.",
        allowed_commands=(command,))
    assert result.status.value == "completed", (result.error, executed_tests)
    assert [(executed, failed) for executed, failed, _ in executed_tests] == [(True, True), (True, False)]
    assert state["plan_revision"] == 1 and state["plan_generated"] is False
    assert "Correct the arithmetic operator" in state["plan_text"]
    assert len(requests) == 5
    assert "AssertionError: -1 != 3" in str(requests[3]["messages"])
    assert "Correct the arithmetic operator" in str(requests[3]["messages"])
    assert [e["status"] for e in trace if e.get("event") == "verification_result"] == ["failed", "passed"]
    assert state["verification_contract"]["passed"] is True
    assert state["verification_contract"]["attempted_generation"] == state["acceptance_mutation_generation"]
    assert (tmp_path / "test_calc.py").read_bytes() == test_bytes
    assert (tmp_path / "calc.py").read_text() == "def add(left, right):\n    return left + right\n"
    assert not any("re-planner" in str(request.get("messages")) for request in auxiliary)
