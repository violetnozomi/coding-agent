"""只读核验本轮冻结输入、生产包与未运行状态；不再次执行任务。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent


def read(name):
    return json.loads((HERE / name).read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    manifest = read("manifest.json")
    assert sha(HERE / "manifest.json") == read("freeze.json")["manifest_sha256"]
    assert not manifest["authorization"]["valid"]
    assert manifest["authorization"]["authorized_paid_requests"] == 0
    candidates = manifest["gate"]["candidate_hashes"]
    initial = manifest["gate"]["initial_hashes"]
    for label, folder in (("G", "candidate-01"), ("D", "candidate-02"), ("initial", "initial")):
        expected = initial if label == "initial" else candidates[label]
        for name, digest in expected.items():
            assert sha(HERE / "prepared/gate" / folder / name) == digest
        assert set(expected) == {"payment.py", "tests/test_amount.py"}
    assert candidates["G"]["tests/test_amount.py"] == candidates["D"]["tests/test_amount.py"] == initial["tests/test_amount.py"]
    independent = read("gate-independent-acceptance.json")
    assert independent["G"]["exit"] == 0 and independent["D"]["exit"] == 1
    assert [c["requirement"] for c in independent["D"]["checks"] if not c["passed"]] == ["default_negative_rejection"]
    results = read("review-results.json")
    assert [(r["unit"], r["candidate"], r["version"]) for r in results] == [
        ("u01", "G", "new"), ("u02", "D", "old"), ("u03", "G", "old"), ("u04", "D", "new")]
    for row in results:
        outcome = row["real_review"]
        assert outcome["status"] == "not_run" and outcome["reason"] == "missing_paid_authorization"
        assert outcome["raw_verdict"] is outcome["effective_verdict"] is outcome["usage"] is outcome["cost"] is None
        prep = row["offline_preparation"]
        packet_path = HERE / prep["packet"]
        assert sha(packet_path) == prep["public_sha256"]
        packet = json.loads(packet_path.read_text())
        assert packet["workspace_hashes"] == candidates[row["candidate"]]
        assert packet["index_schema"] == (5 if row["version"] == "old" else 6)
        assert manifest["gate"]["task"] in packet["review_user_message"]
        assert "python -m pytest -q tests" in packet["review_user_message"]
        state = packet["state_after_review"]
        assert state["mutation_generation"] == state["verification_generation"] == 1
        assert not any(e["type"] == "semantic_review" for item in state["requirement_ledger"]["items"] for e in item["evidence"])
        tests = [e for e in packet["runtime"] if e.get("event") == "verification_result"]
        assert len(tests) == 1 and tests[0]["status"] == "passed"
        assert any(e.get("event") == "tool_call" and e.get("name") == "bash" and e.get("executed")
                   and "2 passed" in str(e) for e in packet["runtime"])
        observation = packet["offline_gate_observation"]
        if row["unit"] == "u03":
            assert not packet["review_boundary_inputs"]
            assert observation["stats"]["last_trace"] == "deterministic_compatibility_guard"
            assert observation["source"] == "pre_model_rule"
            assert observation["decision"]["action"] == "reanimate"
        else:
            assert len(packet["review_boundary_inputs"]) == 1
            assert observation["stats"]["last_trace"] == "provider_error"
            assert not observation["semantic_verdict_valid"]
        if row["unit"] == "u01":
            assert "NON-AUTHORITATIVE COMPATIBILITY HYPOTHESES" in packet["review_user_message"]
    autonomous = read("autonomous-result.json")
    assert autonomous["status"] == "not_run" and autonomous["functional_acceptance"] is None
    for name, digest in manifest["autonomous"]["initial_hashes"].items():
        assert sha(HERE / "prepared/autonomous/initial" / name) == digest
    baseline = read("autonomous-initial-checks.json")
    assert baseline["project"]["exit"] == 0
    assert baseline["independent"]["passed"] == 1 and baseline["independent"]["total"] == 9
    dry = read("autonomous-dry-request.json")
    assert sha(HERE / "autonomous-dry-request.json") == read("dry-run-provenance.json")["public_sha256"]
    main_request = dry["actual_serialized_main_request"]
    assert main_request["stream"] is True and main_request["max_tokens"] == 64000
    assert "thinking" not in main_request and "reasoning_effort" not in main_request
    review = dry["serialized_review_parameter_probe"]
    assert review["stream"] is False and review["max_tokens"] == 1024
    assert review["thinking"] == {"type": "disabled"}
    assert dry["effective_retrieval_strategy"] == "policy"
    assert dry["state_at_boundary"]["max_turns"] == 12
    assert not any(e.get("event") == "tool_call" for e in dry["runtime"])
    for path in HERE.rglob("*.json"):
        text = path.read_text()
        assert "/home/pyh" not in text, path
        assert not re.search(r'"reasoning_content"\s*:', text), path
        assert "Bearer " not in text, path
    for name, digest in read("SHA256SUMS.json").items():
        assert sha(HERE / name) == digest, name
    print("PASS: frozen inputs, G/D acceptance, four production packets, actual request fields, not_run/null semantics, redaction and hashes")


if __name__ == "__main__":
    main()
