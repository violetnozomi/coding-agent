"""Go 测试命名关联必须让真实 SDK 进入独立评审并完成收尾。"""
from __future__ import annotations

import json
import shutil

import pytest

from test_node_verification_runtime import _run, _tool
from test_planning_switch_runtime import _prepare
from test_requirement_verification_runtime import _save_evidence

COMMAND = "go test -count=1 -run 'TestReady|TestSignature' ./logfile/"
SOURCE = "package logfile\n\nfunc Ready() bool { return false }\n"
TEST_SOURCE = '''package logfile

import "testing"

func TestReady(t *testing.T) {
    if !Ready() { t.Fatal("Ready must return true") }
}

func TestSignature(t *testing.T) {
    var ready func() bool = Ready
    if !ready() { t.Fatal("signature changed") }
}
'''


def test_completion_guides_reliable_verification_without_accepting_masked_go_pass():
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.agent.task_contract import parse_task_contract
    from nz_coder.runtime.verification.completion_gate import CompletionGate

    ledger = VerificationScenario(parse_task_contract({
        "requirements": [{"id": "R1", "description": "Preserve Ready signature", "kind": "compatibility",
                          "expected_artifacts": ["logfile/logfile.go"]}],
    }))
    ledger.mutate(1, ["logfile/logfile.go"])
    ledger.execute(1, command=COMMAND + " 2>&1 | tail -30", passed=True, acceptance=False)
    blocked = CompletionGate().evaluate(ledger, mutation_generation=1, acceptance_required=False)
    assert not blocked.ready
    assert "Runtime-owned evidence pending" in blocked.message
    assert not any(e.type == "command_passed" for e in ledger.items['R1'].evidence)

    ledger.execute(1, command=COMMAND, passed=True, acceptance=False)
    review = CompletionGate().evaluate(ledger, mutation_generation=1, acceptance_required=False)
    assert not review.ready
    assert "Runtime-owned evidence pending" in review.message
    assert "same focused test" not in review.message
    ledger.review(1, accepted=True, fingerprint="review:signature-preserved", acceptance_required=False)
    assert CompletionGate().evaluate(ledger, mutation_generation=1, acceptance_required=False).ready


@pytest.mark.parametrize("verdict", ["accept", "revise"])
def test_masked_go_pass_guides_direct_retest_before_semantic_completion(monkeypatch, tmp_path, verdict):
    if not shutil.which("go"):
        pytest.skip("A local Go toolchain is required")
    _prepare(monkeypatch, tmp_path, enabled=True)
    monkeypatch.setenv("GOPROXY", "off")
    monkeypatch.setenv("GOSUMDB", "off")
    (tmp_path / "go.mod").write_text("module example.test/closure\n\ngo 1.16\n")
    (tmp_path / "logfile").mkdir()
    (tmp_path / "logfile/logfile.go").write_text(SOURCE)
    planner = {
        "objective": "Fix Ready and preserve its signature", "plan": [],
        "requirements": [
            {"id": "R1", "description": "Ready returns true", "kind": "behavior",
             "expected_artifacts": ["logfile/logfile.go"]},
            {"id": "R2", "description": "Keep public signature", "kind": "compatibility",
             "expected_artifacts": ["logfile/logfile.go"]},
            {"id": "R3", "description": "Add tests", "kind": "test",
             "expected_artifacts": ["logfile/logfile_test.go"]},
        ],
        "acceptance_commands": [], "contract_version": 2,
    }
    masked = COMMAND + " 2>&1 | tail -30"
    actions = [
        [_tool("read_file", path="logfile/logfile.go")],
        [_tool("edit_file", path="logfile/logfile.go", old_text="return false", new_text="return true")],
        [_tool("write_file", path="logfile/logfile_test.go", content=TEST_SOURCE)],
        [_tool("bash", command=masked)], "The tests printed success.",
        [_tool("bash", command=COMMAND)], "Implemented and retested.",
    ]
    result, state, trace, requests, auxiliary, permissions = _run(
        monkeypatch, tmp_path, actions, planning_response=json.dumps(planner),
        task="Fix Ready in logfile/logfile.go, retain its public signature and add logfile/logfile_test.go.",
        allowed_commands=(masked, COMMAND), semantic_response=verdict,
    )
    _save_evidence("go-guided-retest-" + verdict, result, state, trace, requests, auxiliary, permissions)
    repair_prompt = "\n".join(str(m.get("content", "")) for m in requests[5]["messages"])
    assert "single bash call" in repair_prompt and "same focused test" in repair_prompt
    assert any(e["event"] == "verification_result_ignored" for e in trace)
    items = state['requirement_ledger']['items']
    assert all(e['command'] != masked for item in items for e in item['evidence'] if e['type'] == 'command_passed')
    assert any(e['command'] == COMMAND for item in items for e in item['evidence'] if e['type'] == 'command_passed')
    assert state['verification_contract'] == {}
    assert result.status.value == ("completed" if verdict == "accept" else "max_turns")
    assert any(e["event"] == "sidecar_finished" for e in trace)


@pytest.mark.parametrize("command", [
    COMMAND,
    "go test ./logfile -count=1 -run TestReady",
    "go test -v ./logfile/logfile.go ./logfile/logfile_test.go -run TestReady",
])
def test_go_package_pass_satisfies_named_test_requirement(command):
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    ledger = VerificationScenario(parse_task_contract({
        "requirements": [{"id": "R1", "description": "Add Ready coverage", "kind": "test",
                          "expected_artifacts": ["logfile/logfile_test.go"]}],
    }))
    ledger.mutate(1, ["logfile/logfile_test.go"],
                            operations={"logfile/logfile_test.go": "create"})
    ledger.execute(1, command=command, passed=True, acceptance=False)
    assert ledger.status("R1") == "candidate"
    ledger.review(1, accepted=True)
    assert ledger.status("R1") == "satisfied"
    assert any(e.type == "semantic_review_passed" and e.generation == 1
               for e in ledger.items["R1"].evidence)
    ledger.mutate(2, ["logfile/logfile_test.go"])
    assert ledger.status("R1") == "candidate"
    ledger.execute(2, command="go test ./other -run TestOther",
                                passed=True, acceptance=False)
    assert ledger.status("R1") == "candidate"


@pytest.mark.parametrize("command", [
    "go test ./other -run TestLogfile",
    "go test ./other/logfile/ -run TestOther",
    "go test ./other -run ./logfile",
    "go test ./other -run TestOther && echo ./logfile",
    "go test ./other -run TestOther -args ./logfile",
    "go test ./logfile -run TestReady -exec true",
    "go test -C other ./logfile -run TestReady",
    "cd other && go test ./logfile -run TestOther",
    "env --chdir=other go test ./logfile -run TestOther",
    "env --chdir=other sh -c 'go test ./logfile -run TestOther'",
    "go test ./other -run TestOther && go test -run '^$' ./logfile",
    "builtin cd other && go test ./logfile -run TestOther",
    "go test ./other -run TestOther < ./logfile",
    "go test ./logfile -run TestReady && go vet ./logfile",
    "go test ./other # ./logfile -run TestReady",
])
def test_go_test_name_or_another_package_cannot_satisfy_test_artifact(command):
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    ledger = VerificationScenario(parse_task_contract({
        "requirements": [{"id": "R1", "description": "Add logfile coverage", "kind": "test",
                          "expected_artifacts": ["logfile/logfile_test.go"]}],
    }))
    ledger.mutate(1, ["logfile/logfile_test.go"])
    ledger.execute(1, command=command, passed=True, acceptance=False)
    assert ledger.status("R1") == "candidate"


@pytest.mark.parametrize("verdict,command,empty", [
    ("accept", COMMAND, False),
    ("revise", COMMAND, False),
    ("accept", COMMAND + " -run '^$'", True),
    ("accept", COMMAND + " -args -test.run '^$'", True),
], ids=["accept", "revise", "zero-tests-last-run", "zero-tests-args"])
def test_go_plan_edit_test_review_reaches_correct_terminal_state(monkeypatch, tmp_path, verdict, command, empty):
    if not shutil.which("go"):
        pytest.skip("A local Go toolchain is required for the real Go test subprocess")
    _prepare(monkeypatch, tmp_path, enabled=True)
    monkeypatch.setenv("GOPROXY", "off")
    monkeypatch.setenv("GOSUMDB", "off")
    (tmp_path / "go.mod").write_text("module example.test/closure\n\ngo 1.16\n")
    (tmp_path / "logfile").mkdir()
    (tmp_path / "logfile/logfile.go").write_text(SOURCE)
    planner = {
        "objective": "Make Ready return true and retain its public signature",
        "plan": [{"title": "Fix Ready and add focused tests", "target": "logfile/logfile.go",
                  "verification": COMMAND}],
        "requirements": [
            {"id": "R1", "description": "Ready returns true", "kind": "behavior",
             "expected_artifacts": ["logfile/logfile.go"]},
            {"id": "R2", "description": "Preserve Ready public signature", "kind": "compatibility",
             "expected_artifacts": ["logfile/logfile.go"]},
            {"id": "R3", "description": "Add focused Ready tests", "kind": "test",
             "expected_artifacts": ["logfile/logfile_test.go"], "depends_on": ["R1", "R2"]},
        ],
        "acceptance_commands": [], "contract_version": 2,
    }
    actions = [
        [_tool("read_file", path="logfile/logfile.go")],
        [_tool("edit_file", path="logfile/logfile.go", old_text="return false", new_text="return true")],
        [_tool("write_file", path="logfile/logfile_test.go", content=TEST_SOURCE)],
        [_tool("bash", command=command)],
        "Implemented and checked Ready.",
    ]
    result, state, trace, requests, auxiliary, permissions = _run(
        monkeypatch, tmp_path, actions,
        task="Fix logfile/logfile.go so Ready returns true. Preserve its public signature and add tests in logfile/logfile_test.go.",
        planning_response=json.dumps(planner), allowed_commands=(command,), semantic_response=verdict,
    )
    case = "go-empty-" + ("args" if "-args" in command else "last-run") if empty else "go-completion-" + verdict
    _save_evidence(case, result, state, trace, requests, auxiliary, permissions)
    if empty:
        assert result.status.value == "max_turns"
        assert any(e["event"] == "verification_result" and e.get("status") == "skipped"
                   and "[no tests to run]" in e.get("output", "") for e in trace)
        assert not any(e["event"] == "sidecar_finished" for e in trace)
        assert all(i["status"] == "candidate" for i in state["requirement_ledger"]["items"])
        return
    assert any(e["event"] == "verification_result" and e.get("status") == "passed"
               and e.get("stage") == "targeted" for e in trace)
    assert any(e["event"] == "sidecar_finished" for e in trace)
    items = {i["requirement"]["id"]: i for i in state["requirement_ledger"]["items"]}
    assert items["R3"]["status"] == "satisfied"
    assert state["verification_contract"] == {}
    assert not any(e["type"] == "verification_passed" for i in items.values() for e in i["evidence"])
    if verdict == "accept":
        assert result.status.value == "completed", result.final_text
        assert all(i["status"] == "satisfied" for i in items.values())
        assert any(e["type"] == "semantic_review_passed" for e in items["R2"]["evidence"])
    else:
        assert result.status.value == "max_turns"
        assert items["R2"]["status"] == "candidate"
        assert not any(e["type"] == "semantic_review_passed" for e in items["R2"]["evidence"])
