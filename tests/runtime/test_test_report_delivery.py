"""原生交付只用当前工具回执报告实际测试结果。"""
from __future__ import annotations

import pytest

from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.verification.completion_gate import host_test_report
from nz_coder.runtime.verification.domain.checkpoint import encode_domain
from nz_coder.runtime.verification.sidecar_verifier import parse_verifier_report
from test_delivery_assessment_runner import accepted, run_probe
from tests.runtime.test_completion_delivery_summary import FULL, _observe, _project
from tests.verification_scenarios import VerificationScenario


CLAIM = "我刚刚运行了 pytest，两个测试全部通过。"
TASK = "看看这个项目目前的测试情况。"


def test_accept_without_a_report_kind_cannot_publish_an_unexecuted_test_claim(monkeypatch, tmp_path):
    old_report = {"verdict": "accept", "reason": "Controlled judgment",
                  "delivery_assessment": {"status": "reviewed", "issues": []}}
    result, _, provider, snapshot = run_probe(
        monkeypatch, tmp_path, reports=lambda *_: (old_report, "tool_calls"), task=TASK, candidate=CLAIM,
    )
    assert result.status.value == "completed" and provider.mains == provider.reviews == 1
    assert not snapshot.current.executions
    assert not any(message.get("tool_calls") for message in result.messages)
    assert snapshot.current.latest_review.test_report_kind == "unknown"
    assert CLAIM not in result.final_text and "本轮没有已采信的测试结果" in result.final_text
    assert result.metadata["authored_content"] == CLAIM and "/message" in result.final_text
    assert result.messages[-1]["content"] == result.final_text


@pytest.mark.parametrize(("target", "exit_code", "conclusion"), [
    ("test_math.py", 1, "测试命令失败"),
    ("test_math.py::test_add", 0, "测试命令通过"),
])
def test_test_report_uses_the_actual_failed_or_focused_run(monkeypatch, tmp_path, target, exit_code, conclusion):
    (tmp_path / "test_math.py").write_text(
        "def test_add():\n    assert 1 + 1 == 2\n\n"
        "def test_store_returns_saved_value():\n    assert 3 == 4\n",
        encoding="utf-8",
    )
    command = "python -m pytest -q " + target
    result, _, provider, snapshot = run_probe(
        monkeypatch, tmp_path, candidate=CLAIM, task=TASK,
        reports=lambda *_: ({**accepted(), "test_report_kind": "test_report"}, "tool_calls"),
        initial_actions=[[("bash", {"command": command})]],
    )
    assert (provider.mains, provider.reviews) == (2, 1)
    fact, = snapshot.current.executions
    assert fact.command.original == command and fact.exit_code == exit_code
    assert result.metadata["verification"]["delivery_review"]["test_report_kind"] == "test_report"
    assert CLAIM not in result.final_text and conclusion in result.final_text
    assert command in result.final_text and str(tmp_path) in result.final_text
    assert result.metadata["authored_content"] == CLAIM


@pytest.mark.parametrize(("kind", "exact", "from_text", "expected"), [
    ("ordinary", True, False, "ordinary"),
    ("test_report", True, False, "test_report"),
    ([], True, False, "unknown"),
    ("ordinary", False, False, "unknown"),
    ("ordinary", True, True, "unknown"),
])
def test_reporting_kind_does_not_change_the_semantic_verdict(kind, exact, from_text, expected):
    verdict = parse_verifier_report({
        "input": {**accepted(), "test_report_kind": kind}, "_from_text": from_text,
    }, exact)
    assert verdict.verdict == "accept" and verdict.test_report_kind == expected


@pytest.mark.parametrize(("task", "candidate"), [
    ("pytest 怎么用？仅解释。", "运行 pytest -q 可以查看测试输出。"),
    (TASK, CLAIM),
], ids=["usage-explanation", "misclassified-test-report"])
def test_ordinary_review_of_either_kind_of_text_never_proves_execution(monkeypatch, tmp_path, task, candidate):
    result, _, provider, snapshot = run_probe(
        monkeypatch, tmp_path, candidate=candidate, task=task,
        reports=lambda *_: ({**accepted(), "test_report_kind": "ordinary"}, "tool_calls"),
    )
    assert (provider.mains, provider.reviews) == (1, 1)
    review = result.metadata["verification"]["delivery_review"]
    assert review["accepted"] and review["test_report_kind"] == "ordinary"
    assert review["candidate_fingerprint"] == snapshot.current.candidate_fingerprint
    assert not snapshot.current.executions and snapshot.current.tests_passed is None
    assert result.metadata["verification"]["last_verification"] is None
    assert result.metadata["authored_content"] == candidate
    assert candidate in result.final_text
    assert result.final_text.index("模型说明") < result.final_text.index(candidate)
    assert "测试执行以宿主回执为准" in result.final_text and "没有已采信的验证执行" in result.final_text


def test_source_change_and_ordinary_shell_cannot_restore_an_old_pass(tmp_path):
    _project(tmp_path)
    with scoped_workdir(tmp_path):
        scenario = VerificationScenario(workspace=tmp_path, plan={"stages": []})
        _observe(scenario, tmp_path, FULL)
        (tmp_path / "app.py").write_text("value = 3\n", encoding="utf-8")
        scenario.mutate(1, ["app.py"])
        _observe(scenario, tmp_path, "echo '2 passed'")
        report = host_test_report(scenario.domain.snapshot())
    assert "本轮没有已采信的测试结果" in report and "测试命令通过" not in report
    assert "历史记录" in report and FULL in report and str(tmp_path) in report
    assert "2 passed" not in report


def test_report_keeps_an_earlier_valid_test_and_actual_working_directories(tmp_path):
    project, other = tmp_path / "project", tmp_path / "other"
    _project(project)
    _project(other)
    with scoped_workdir(tmp_path):
        scenario = VerificationScenario(workspace=tmp_path, plan={"stages": []})
        _observe(scenario, project, FULL)
        (tmp_path / "README.md").write_text("How to run\n", encoding="utf-8")
        scenario.mutate(1, ["README.md"])
        for index in range(12):
            scenario.command_result({"command": f"pytest -q -k absent_{index}", "workdir": str(other)},
                                    "2 deselected in 0.01s", False, True, exit_code=5)
        report = host_test_report(scenario.domain.snapshot())
    passed = next(line for line in report.splitlines() if FULL in line and "测试命令通过" in line)
    assert "回执对当前验收版本仍有效" in passed and str(project) in passed and str(other) not in passed
    assert "1 次测试命令通过" in report and "共记录 13 次测试命令调用" in report
    assert "README.md" in report and "运行时验证记录" in report


def test_restored_test_receipt_identifies_its_original_run(tmp_path):
    _project(tmp_path)
    with scoped_workdir(tmp_path):
        scenario = VerificationScenario(workspace=tmp_path, plan={"stages": []})
        _observe(scenario, tmp_path, FULL)
        prior_run = scenario.domain.scope.interaction_run_id
        scenario.restore(encode_domain(scenario.domain), workspace=tmp_path, generation=0)
        assert scenario.domain.scope.interaction_run_id != prior_run
        report = host_test_report(scenario.domain.snapshot())
    assert "本轮没有已采信的测试结果" in report and "测试命令通过" not in report
    history = next(line for line in report.splitlines() if "历史记录" in line and FULL in line)
    assert prior_run in history and str(tmp_path) in history and "退出码 0" in history
