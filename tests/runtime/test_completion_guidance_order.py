"""收尾提示须先解决阻止独立评审的普通验证义务。"""
from __future__ import annotations

import pytest


@pytest.mark.parametrize("response", ["pass", "deny", "ignore"])
def test_current_suite_allows_review_without_replaying_old_optional_module(monkeypatch, tmp_path, response):
    from test_node_verification_runtime import _run, _tool

    for key in ("NZ_PLANNING_ENABLED", "NZ_REFLECTION_ENABLED", "NZ_MEMORY_AUTO_EXTRACT", "NZ_MEMORY_AUTO_DREAM"):
        monkeypatch.setenv(key, "0")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    (tmp_path / "app.py").write_text("value = 0\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/__init__.py").write_text("")
    source = (
        "import unittest\nfrom app import value\n"
        "class FeatureTests(unittest.TestCase):\n"
        "    def test_value(self):\n        self.assertEqual(value, 1)\n"
    )
    (tmp_path / "tests/test_feature.py").write_text(source)
    failed = "python -m unittest tests.test_feature -v 2>&1 | tail -30"
    retry = "python -m unittest tests.test_feature -v"
    suite = "python -m unittest discover -s tests -v"
    actions = [
        [_tool("read_file", path="app.py")],
        [_tool("bash", command=failed)],
        [_tool("edit_file", path="app.py", old_text="value = 0", new_text="value = 1  # corrected")],
        [_tool("write_files_batch", overwrite=True, files=[
            {"path": "tests/test_feature.py", "content": source + "\n# 验证修复后的公开值。\n"},
            {"path": "README.md", "content": "# Example\nRun python -m unittest discover -s tests -v.\n"},
        ])],
        [_tool("bash", command=suite)],
        "Implementation, tests and documentation are ready.",
    ]
    actions += (["Waiting for runtime review."] * 3 if response == "ignore" else [
        [_tool("bash", command=retry)], "Implementation, tests and documentation are ready.",
    ])
    allowed = (failed, suite, "python -m py_compile app.py") + (() if response == "deny" else (retry,))
    result, state, trace, requests, reviews, permissions = _run(
        monkeypatch, tmp_path, actions,
        task="修复 app.py 的 value，使测试通过；增加或更新测试覆盖和文档。"
             "验收命令 python -m unittest discover -s tests -v。",
        allowed_commands=allowed, limit=20,
    )
    # 同代全套通过后直接进入评审；普通旧失败不再形成精确复跑义务。
    assert result.status.value == "completed", (result.error, result.final_text)
    assert result.metadata["verification"]["test_obligations"] == []
    assert reviews and state["completion_gate_prompts"] == 0
    assert failed in str(reviews)
    checks = [e for e in trace if e.get("event") == "tool_call" and e.get("name") == "bash"]
    assert sum(e.get("input", {}).get("command") == failed for e in checks) == 1
    assert not any(e.get("input", {}).get("command") == retry for e in checks)
    assert not any(p[1].get("command") == retry for p in permissions)
    assert len(requests) <= 6
