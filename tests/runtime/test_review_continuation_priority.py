"""真实修复动作重开收尾反馈，连续空口交付仍会停止。"""
from __future__ import annotations

import pytest


@pytest.mark.parametrize("verdicts,expected", [
    (["revise", "accept"], "completed"),
    (["revise"] * 5, "blocked"),
    (["accept"], "completed"),
])
def test_review_feedback_allows_actions_but_stops_consecutive_empty_delivery(
    monkeypatch, tmp_path, verdicts, expected,
):
    from test_node_verification_runtime import _run, _tool

    for key in ("NZ_PLANNING_ENABLED", "NZ_REFLECTION_ENABLED", "NZ_MEMORY_AUTO_EXTRACT", "NZ_MEMORY_AUTO_DREAM"):
        monkeypatch.setenv(key, "0")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    # 修改前后长度不同，避免同秒修改命中 Python 的旧字节码缓存。
    (tmp_path / "app.py").write_text("value = -1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/__init__.py").write_text("")
    source = (
        "import unittest\nfrom app import value\n"
        "class FeatureTests(unittest.TestCase):\n"
        "    def test_value(self):\n        self.assertEqual(value, 1)\n"
    )
    for module in ("alpha", "beta"):
        (tmp_path / f"tests/test_{module}.py").write_text(source)
    failed = [f"python -m unittest tests.test_{m} -v 2>&1 | tail -30" for m in ("alpha", "beta")]
    retries = [f"python -m unittest tests.test_{m} -v" for m in ("alpha", "beta")]
    suite = "python -m unittest discover -s tests -v"
    actions = [
        [_tool("read_file", path="app.py")],
        [_tool("bash", command=failed[0])],
        [_tool("bash", command=failed[1])],
        [_tool("edit_file", path="app.py", old_text="value = -1", new_text="value = 1")],
        [_tool("write_files_batch", overwrite=True, files=[
            {"path": f"tests/test_{m}.py", "content": source + "\n# 验证修正后的公开行为。\n"}
            for m in ("alpha", "beta")
        ] + [{"path": "README.md", "content": "# Example\nRun python -m unittest discover -s tests -v.\n"}])],
        [_tool("bash", command=suite)],
        "Implementation and documentation are ready.",
        [_tool("bash", command=retries[0])],
        "Implementation and documentation are ready.",
        [_tool("bash", command=retries[1])],
        "Implementation and documentation are ready.",
        "The final instructions now match the documented interface.",
        "The final instructions now match the documented interface.",
    ]
    result, state, trace, requests, reviews, permissions = _run(
        monkeypatch, tmp_path, actions,
        task="修复 app.py 的 value，使测试通过；增加或更新测试覆盖和文档。"
             "验收命令 python -m unittest discover -s tests -v。",
        allowed_commands=(*failed, *retries, suite, "python -m py_compile app.py"),
        semantic_response=verdicts, limit=30,
    )
    assert state["completion_gate_prompts"] == 0
    assert result.metadata["verification"]["tests_passed"] is True
    assert all(item["status"] == "passed" for item in result.metadata["verification"]["test_obligations"])
    assert result.metadata["raw_status"] == expected, (result.final_text, trace[-12:])
    assert result.status.value == expected
    # 实际生产钩子追加的反馈必须进入下一次主模型请求，而不只是出现在最终邮箱。
    feedback_requests = [request for request in requests if any(
        "<stop-hook-guidance>" in str(m.get("content", ""))
        for m in request["messages"]
    )]
    if expected == "completed":
        assert len(reviews) == len(verdicts)
        assert bool(feedback_requests) == (verdicts[0] == "revise")
        assert result.metadata["verification"]["completion_ready"] is True
    else:
        assert len(reviews) == 5
        assert len(requests) == 13
        assert [e["count"] for e in trace if e.get("event") == "stop_hook_reanimate"] == [1, 1, 1, 2]
        assert len([e for e in trace if e.get("event") == "stop_hook_feedback_reset"]) == 2
        assert any(e.get("event") == "stop_hook_budget_exhausted" for e in trace)
        assert "Consecutive completion feedback exhausted (2/2)." in result.final_text
