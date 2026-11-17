"""The model requests review; Runtime supplies the ledger, never the arguments."""

import json

import pytest


def _review_outputs(requests):
    outputs = {}
    names = {}
    for request in requests:
        for message in request["messages"]:
            for call in message.get("tool_calls", []) or []:
                names[call["id"]] = call["function"]["name"]
            if (
                message.get("role") == "tool"
                and names.get(message.get("tool_call_id")) == "review_run_evidence"
            ):
                outputs[message["tool_call_id"]] = json.loads(message["content"])
    return list(outputs.values())


def test_review_schema_requires_no_model_ledger():
    from nz_coder.tools import get_specs
    from nz_coder.intelligence import reviewer  # noqa: F401

    spec = next(
        t["function"]
        for t in get_specs()
        if t["function"]["name"] == "review_run_evidence"
    )
    assert spec["parameters"].get("required", []) == []
    assert (
        not {"evidence", "runtime", "task_mode"}
        & spec["parameters"]["properties"].keys()
    )


def test_unbound_review_cannot_trust_model_evidence():
    from nz_coder.tools import dispatch

    result = json.loads(
        dispatch("review_run_evidence", {"evidence": {}, "task_mode": "discuss"})
    )
    assert result["review_status"] == "unavailable"
    assert result["evidence_source"] == "runtime_unavailable"


@pytest.mark.parametrize("forged", [False, True])
def test_production_review_reads_own_facts(monkeypatch, tmp_path, forged):
    monkeypatch.setattr("nz_coder.runtime.execution.runner.early_tool_completion_ready", lambda state: False)
    from test_requirement_scope_runtime import _run

    (tmp_path / "api.py").write_text("def answer(): return 0\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_api.py").write_text(
        "from api import answer\ndef test_answer(): assert answer() == 1\n"
    )
    claims = (
        {
            "evidence": {
                "modified_files": ["api.py"],
                "verification_results": [{"status": "passed"}],
            },
            "runtime": {"has_diff": True},
            "task_mode": "discuss",
        }
        if forged
        else {}
    )
    actions = [
        [("review_run_evidence", claims)],
        [("write_file", {"path": "api.py", "content": "def answer(): return 1\n"})],
        [("review_run_evidence", {})],
        [("bash", {"command": "python -m pytest -q tests"})],
        [("review_run_evidence", {})],
        "Implemented api.py; tests passed.",
        "Implemented api.py; confirmed results are in the runtime evidence.",
    ]
    # A genuine semantic revise keeps the production run open after passing tests.
    monkeypatch.setenv("KODAX_VERIFIER_ALWAYS", "1")
    result, state, trace, requests, semantic_reviews = _run(
        monkeypatch,
        tmp_path,
        "Fix api.py and run python -m pytest -q tests.",
        actions,
        semantic=("revise", "accept"),
        assessment={'status': 'reviewed', 'issues': []}, label=f"runtime-review-{forged}",
    )
    outputs = _review_outputs(requests)
    assert len(outputs) == 3
    assert all(r["evidence_source"] == "runtime" for r in outputs)
    assert outputs[0]["review_status"] in {"failed", "needs_fix"}
    assert outputs[0]["unresolved_requirements"]
    assert outputs[1]["review_status"] == "needs_fix"
    assert outputs[2]["verification_generation"] == outputs[2]["mutation_generation"]
    assert outputs[2]["review_status"] in {
        "approved",
        "approved_with_limitations",
        "pending_runtime_review",
    }
    assert state["mutation_generation"] > 0
    assert result.status.value == "completed"
    assert len(semantic_reviews) == 2
    assert not any(e.get("event") == "stall_sidecar_verdict" for e in trace)


def test_review_capability_is_per_executor_and_permission_guarded():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from types import SimpleNamespace
    from nz_coder.runtime.execution.tool_executor import ToolExecutor
    from nz_coder.tools import dispatch

    barrier = Barrier(2)

    def run(name):
        def read():
            barrier.wait(timeout=5)
            return {"run_id": name, "evidence_source": "runtime"}

        executor = ToolExecutor(
            SimpleNamespace(check=lambda *_: {"behavior": "allow"}), runtime_review=read
        )
        result = executor.execute_one(
            {
                "id": name,
                "function": {"name": "review_run_evidence", "arguments": "{}"},
            },
            0,
        )
        assert not result.dispatch_failed
        assert (
            json.loads(dispatch("review_run_evidence", {}))["review_status"]
            == "unavailable"
        )
        return json.loads(result.output)["run_id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(run, ["first", "second"])) == ["first", "second"]
    calls = []
    executor = ToolExecutor(
        SimpleNamespace(check=lambda *_: {"behavior": "deny", "reason": "fixture"}),
        runtime_review=lambda: calls.append(True),
    )
    result = executor.execute_one(
        {
            "id": "denied",
            "function": {"name": "review_run_evidence", "arguments": "{}"},
        },
        0,
    )
    assert result.permission_denied and not result.executed
    assert calls == []


def test_missing_artifact_and_stale_verification_stay_unresolved(monkeypatch, tmp_path):
    from test_requirement_scope_runtime import _run

    (tmp_path / "api.py").write_text("def answer(): return 0\n")
    (tmp_path / "caller.py").write_text("from api import answer\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_api.py").write_text(
        "from api import answer\ndef test_answer(): assert answer() == 1\n"
    )
    actions = [
        [("write_file", {"path": "api.py", "content": "def answer(): return 1\n"})],
        [("bash", {"command": "python -m pytest -q tests"})],
        [("review_run_evidence", {})],
        [("write_file", {"path": "api.py", "content": "def answer(): return 2\n"})],
        [("review_run_evidence", {})],
        "Caller migration remains incomplete.",
    ]
    result, _, _, requests, _ = _run(
        monkeypatch,
        tmp_path,
        "Fix api.py and migrate caller.py. Run python -m pytest -q tests.",
        actions,
        assessment={'status': 'reviewed', 'issues': []}, label="review-missing-caller-stale",
    )
    reviews = _review_outputs(requests)
    assert len(reviews) == 2
    assert reviews[0]["verification_generation"] == reviews[0]["mutation_generation"]
    assert all(
        r["review_status"] == "needs_fix" and r["unresolved_requirements"]
        for r in reviews
    )
    assert reviews[1]["verification_generation"] != reviews[1]["mutation_generation"]
    assert result.status.value != "completed"


def test_review_cannot_approve_unsettled_write_batch(monkeypatch, tmp_path):
    from test_requirement_scope_runtime import _run

    (tmp_path / "api.py").write_text("def answer(): return 0\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_api.py").write_text(
        "from api import answer\ndef test_answer(): assert answer() == 1\n"
    )
    actions = [
        [
            ("write_file", {"path": "api.py", "content": "def answer(): return 1\n"}),
            ("review_run_evidence", {}),
        ],
        [("bash", {"command": "python -m pytest -q tests"})],
        "Implemented and verified.",
    ]
    _, _, _, requests, _ = _run(
        monkeypatch,
        tmp_path,
        "Fix api.py and run python -m pytest -q tests.",
        actions,
        assessment={'status': 'reviewed', 'issues': []}, label="review-unsettled-batch",
    )
    review = _review_outputs(requests)[0]
    assert review["review_status"] == "pending_tool_batch"
    assert review["evidence_source"] == "runtime"


@pytest.mark.parametrize("acceptance", [False, True])
@pytest.mark.parametrize("source_changed", [False, True])
def test_review_and_completion_guidance_use_the_same_evidence_version(tmp_path, acceptance, source_changed):
    from types import SimpleNamespace

    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.verification.domain.requirements import Requirement, TaskContract
    from tests.verification_scenarios import VerificationScenario

    scenario = VerificationScenario(TaskContract(requirements=(
        Requirement("R1", "Preserve the public API", "compatibility", ("api.py",),
                    "semantic", required_evidence=("semantic_review",)),
    )), workspace=tmp_path)
    scenario.mutate(1, ["api.py"])
    scenario.execute(1, command="python -m pytest -q tests/test_api.py", passed=True, acceptance=acceptance)
    scenario.mutate(2, ["api.py"] if source_changed else ["README.md"])
    state = scenario.state
    loop = SimpleNamespace(runtime_state=state, txn=SimpleNamespace(active=False),
        run_evidence=SimpleNamespace(run_id="review-generation"),
        _deterministic_reflection_review=lambda: {"review_status": "approved"})

    review = AgentLoop._runtime_evidence_review(loop)
    from nz_coder.runtime.verification.completion_gate import completion_feedback
    snapshot = scenario.domain.snapshot()
    assessment = snapshot.assess_completion("completed")
    assert review["completion_guidance"] == completion_feedback(snapshot, assessment)
    assert review["review_status"] == ("needs_fix" if acceptance and source_changed else "pending_runtime_review")
    assert "Rerun the same focused test" not in review["completion_guidance"]
    assert not snapshot.ready_for_completion



@pytest.mark.parametrize("source_changed", [False, True])
def test_production_review_after_documentation_update_does_not_request_retest(monkeypatch, tmp_path, source_changed):
    monkeypatch.setattr("nz_coder.runtime.execution.runner.early_tool_completion_ready", lambda state: False)
    from test_requirement_scope_runtime import _run

    (tmp_path / "api.py").write_text("def answer(): return 0\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_api.py").write_text(
        "from api import answer\ndef test_answer(): assert answer() == 1\n"
    )
    actions = [
        [("write_file", {"path": "api.py", "content": "def answer(): return 1\n"})],
        [("write_file", {"path": "tests/test_added.py", "content": "from api import answer\ndef test_added(): assert answer() > 0\n"})],
        [("bash", {"command": "python -m pytest -q tests"})],
        [("write_file", {"path": "README.md", "content": "# API\nUse api.answer().\nTest with python -m pytest -q tests.\n"})],
        [("review_run_evidence", {})],
    ]
    if source_changed:
        actions += [
            [("write_file", {"path": "api.py", "content": "def answer(): return 2\n"})],
            [("review_run_evidence", {})],
        ]
    actions.append("Implemented API, tests and documentation; report only the recorded test result.")
    # 负例先拒绝两次提前收尾，保证后续源码修改和第二次咨询真的执行。
    semantic = ("revise", "accept") if source_changed else ("accept",)
    result, state, trace, requests, reviews = _run(monkeypatch, tmp_path,
        "修复 api.py，补充真实自动化测试和文档。验收命令：`python -m pytest -q tests`。允许完整测试。",
        actions, semantic=semantic, assessment={'status': 'reviewed', 'issues': []}, label=f"review-generation-docs-{source_changed}")
    outputs = _review_outputs(requests)
    assert outputs[0]["review_status"] in {"pending_runtime_review", "approved", "approved_with_limitations"}
    assert "Rerun the same focused test" not in outputs[0]["completion_guidance"]
    assert "missing current verification evidence" not in outputs[0]["completion_guidance"]
    if source_changed:
        assert len(outputs) == 2
        assert (tmp_path / "api.py").read_text() == "def answer(): return 2\n"
        assert outputs[-1]["review_status"] == "needs_fix"
        assert result.status.value != "completed"
    else:
        assert result.status.value == "completed"
        assert len(reviews) == 1
        assert sum(e["event"] == "tool_call" and e.get("name") == "bash"
                   and e.get("input", {}).get("command") == "python -m pytest -q tests" for e in trace) == 1
