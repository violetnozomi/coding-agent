"""离线模型、真实 Node 与完整 Runner：每条用户验收都必须实际结清。"""
import json

import pytest

import test_node_verification_runtime as node_runtime
from test_node_verification_runtime import READ, EDIT, _run, _tool
from test_planning_switch_runtime import TASK, PLANNER, _prepare


PASS = "node --test index.test.cjs"
FAIL = "node --test literal.test.cjs"
node_project = node_runtime.node_project


@pytest.mark.parametrize("declarations", [
    f"Run {PASS}.\nRun {FAIL}.",
    f"Acceptance command: `{PASS}`.\nAcceptance command: `{FAIL}`.",
])
def test_first_user_pass_cannot_cover_second_failed_acceptance(monkeypatch, node_project, declarations):
    from nz_coder.runtime.adapters.verification_runtime import VerificationRuntime
    snapshots = []
    synchronize = VerificationRuntime.synchronize_task

    def observe(runtime):
        synchronize(runtime)
        snapshots.append(runtime.domain.snapshot())

    monkeypatch.setattr(VerificationRuntime, "synchronize_task", observe)
    _prepare(monkeypatch, node_project, enabled=True)
    (node_project / "index.test.cjs").write_bytes((node_project / "literal.test.cjs").read_bytes())
    (node_project / "literal.test.cjs").write_text("throw Error('second acceptance still fails');\n")
    result, state, trace, *_ = _run(monkeypatch, node_project,
        [READ, EDIT, [_tool("bash", command=PASS)], "Done"],
        task=TASK + "\n" + declarations, allowed_commands=(PASS, FAIL),
        planning_response=json.dumps(PLANNER))
    assert result.status.value != "completed", result.final_text
    assert state["task_contract"]["acceptance_commands"] == [PASS, FAIL]
    user = [spec for spec in snapshots[-1].specs if spec.source == "user_declared"]
    assert len(user) == 2 and all(spec.required for spec in user)
    assert any(event.get("event") == "tool_call" and event.get("input", {}).get("command") == FAIL for event in trace)
    assert result.metadata["verification"]["tests_passed"] is False


@pytest.mark.parametrize("example", [
    f"Example only:\n```sh\n{FAIL}\n```",
    f"Do not run the following command:\n`{FAIL}`.",
    f"Run printf '`{FAIL}`'.",
])
def test_negative_example_is_not_automatically_executed(monkeypatch, node_project, example):
    _prepare(monkeypatch, node_project, enabled=True)
    (node_project / "index.test.cjs").write_bytes((node_project / "literal.test.cjs").read_bytes())
    (node_project / "literal.test.cjs").write_text("throw Error('must not execute this example');\n")
    result, state, trace, *_ = _run(monkeypatch, node_project,
        [READ, EDIT, [_tool("bash", command=PASS)], "Done"],
        task=TASK + "\n" + example, allowed_commands=(PASS, FAIL),
        planning_response=json.dumps(PLANNER))
    assert result.status.value == "completed", result.final_text
    assert state["verification_contract"] == {}
    assert not any(event.get("event") == "tool_call" and event.get("input", {}).get("command") == FAIL for event in trace)


def test_failing_acceptance_with_quoted_filename_cannot_disappear(monkeypatch, node_project):
    _prepare(monkeypatch, node_project, enabled=True)
    (node_project / "index.test.cjs").write_bytes((node_project / "literal.test.cjs").read_bytes())
    (node_project / "foo and bar.test.cjs").write_text("throw Error('required quoted test fails');\n")
    command = 'node --test "foo and bar.test.cjs"'
    result, state, trace, *_ = _run(monkeypatch, node_project,
        [READ, EDIT, [_tool("bash", command=PASS)], "Done"],
        task=TASK + "\nRun " + command + ".", allowed_commands=(PASS, command),
        planning_response=json.dumps(PLANNER))
    assert result.status.value != "completed", result.final_text
    assert state["task_contract"]["acceptance_commands"] == [command]
    assert any(event.get("event") == "tool_call" and event.get("input", {}).get("command") == command for event in trace)
    assert result.metadata["verification"]["tests_passed"] is False
