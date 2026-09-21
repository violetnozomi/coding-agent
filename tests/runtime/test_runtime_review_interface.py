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
    ]
    # A genuine semantic revise keeps the production run open after passing tests.
    monkeypatch.setenv("KODAX_VERIFIER_ALWAYS", "1")
    result, state, trace, requests, semantic_reviews = _run(
        monkeypatch,
        tmp_path,
        "Fix api.py and run python -m pytest -q tests.",
        actions,
        semantic=("revise", "accept"),
        label=f"runtime-review-{forged}",
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
        label="review-missing-caller-stale",
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
        label="review-unsettled-batch",
    )
    review = _review_outputs(requests)[0]
    assert review["review_status"] == "pending_tool_batch"
    assert review["evidence_source"] == "runtime"
