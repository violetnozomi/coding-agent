"""隔离推理只消费已冻结的公开任务输入，不挂载评分数据集。"""
import hashlib
import json

import pytest

from nz_coder.swebench import cli
from nz_coder.swebench.profiles import get_profile


def test_frozen_public_instances_load_without_dataset_network(tmp_path):
    path = tmp_path / "instances.json"
    packet = {
        "dataset": get_profile("verified").dataset,
        "revision": "a" * 40,
        "split": "test",
        "instances": [{"instance_id": "sympy__sympy-22914", "repo": "sympy/sympy",
                       "base_commit": "b" * 40, "problem_statement": "Public issue"}],
    }
    path.write_text(json.dumps(packet))
    rows, identity = cli.load_inference_instances(path, get_profile("verified"), "test")
    assert rows == packet["instances"]
    assert identity == {"revision": "a" * 40,
                        "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.mark.parametrize("change", ["gold", "hints", "duplicate", "revision", "dataset", "split"])
def test_frozen_input_rejects_scoring_data_and_identity_mismatch(tmp_path, change):
    packet = {"dataset": get_profile("verified").dataset, "revision": "a" * 40, "split": "test",
              "instances": [{"instance_id": "sympy__sympy-22914", "repo": "sympy/sympy",
                             "base_commit": "b" * 40, "problem_statement": "Public issue"}]}
    if change in {"gold", "hints"}:
        packet["instances"][0]["patch" if change == "gold" else "hints_text"] = "private"
    elif change == "duplicate":
        packet["instances"] *= 2
    else:
        packet[change] = "wrong"
    path = tmp_path / "instances.json"
    path.write_text(json.dumps(packet))
    with pytest.raises(ValueError):
        cli.load_inference_instances(path, get_profile("verified"), "test")


def test_swe_grant_covers_only_its_frozen_instance_scope(tmp_path):
    from nz_coder.evaluation.model_relay import (
        AdmissionDenied, InputAccounting, RelayBinding, RelayLedger, RelayLimits,
    )
    from tests.evaluation.test_empirical_review_execution import grant
    import time

    authorization = grant()
    authorization.update(purpose="swebench-verified", scope=["sympy__sympy-22914"],
                         instance_manifest_sha256="c" * 64)
    ledger = RelayLedger(tmp_path / "ledger", RelayLimits(), empirical_authorization=authorization)
    payload = {"model": "deepseek-v4-flash", "max_tokens": 1024,
               "messages": [{"role": "user", "content": "Public issue"}]}
    raw = json.dumps(payload).encode()
    accounting = InputAccounting(None, "reference", contract_id="reference-fixture",
                                 payload_sha256=hashlib.sha256(raw).hexdigest(), reference_count=100)
    try:
        allowed = RelayBinding("swebench-verified", "sympy__sympy-22914", "one", "main",
                               2, payload["model"], 1024, time.monotonic() + 5)
        attempt = ledger.reserve(allowed, "one", raw, payload, accounting)
        ledger.settle(attempt, status="complete", usage={"prompt_tokens": 100, "completion_tokens": 1})
        denied = RelayBinding("swebench-verified", "sympy__sympy-12096", "two", "main",
                              2, payload["model"], 1024, time.monotonic() + 5)
        with pytest.raises(AdmissionDenied, match="scope_mismatch"):
            ledger.reserve(denied, "two", raw, payload, accounting)
        assert len(ledger.attempts) == 1
    finally:
        ledger.close()


def test_native_failure_preserves_frozen_prediction_and_status(tmp_path, monkeypatch):
    from nz_coder.swebench.artifacts import AttemptJournal
    from nz_coder.swebench.orchestrator import RetryOrchestrator

    patch = "diff --git a/source.py b/source.py\n--- a/source.py\n+++ b/source.py\n@@ -1 +1 @@\n-old\n+new\n"
    runner = RetryOrchestrator(None, None)
    monkeypatch.setattr(runner, "run_instance", lambda *_a, **_kw: {
        "instance_id": "sympy__sympy-22914", "status": "agent_failed",
        "summary": "max_turns", "agent_status": {"status": "max_turns"}, "model_patch": patch,
    })
    journal = AttemptJournal(tmp_path / "attempts.jsonl")
    output = tmp_path / "predictions.jsonl"
    results = runner.run_batch(
        [{"instance_id": "sympy__sympy-22914"}], work_root=tmp_path / "work",
        run_id="test", config=None, build_prompt=None, agent_cls=None, trace_cls=None,
        clone_timeout=1, agent_timeout=1, empty_patch_retries=0, pred_file=None,
        model_name="test", strict=True, attempt_journal=journal, predictions_path=output,
    )
    assert results[0]["agent_status"]["status"] == "max_turns"
    assert journal.rows()[-1]["status"] == "agent_failed"
    assert json.loads(output.read_text())["model_patch"] == patch
