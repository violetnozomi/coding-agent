"""并行 Pro 准备续跑只携带剩余额度；模型边界不出站。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import time

import pytest

from nz_coder.evaluation.model_relay import InputAccounting, RelayBinding, RelayLedger, RelayLimits

spec = importlib.util.spec_from_file_location("parallel_pro", Path(__file__).resolve().parents[2]
    / "docs/evidence/swebench-pro-20-batch2-2026-10-11/run-parallel.py")
parallel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parallel)


def closed_source(tmp_path, *, pending=False, unknown=False):
    tasks = [{"instance_id": "instance_example__project-" + letter * 40} for letter in "abc"]
    manifest = {"tasks": tasks}
    grant = {"purpose": "swebench-pro-v2", "model": "deepseek-flash", "account": "offline-test",
        "scope": [task["instance_id"] for task in tasks], "instance_manifest_sha256": "a" * 64,
        "authorization_text": "offline fee test", "input_mode": "empirical", "empirical_risk_accepted": True,
        "contract_id": "offline", "cost_currency": "CNY", "cost_limit": 50,
        "valid_from": time.time() - 1, "valid_until": time.time() + 60,
        "prices_cny_per_million": {"input": 2, "cached_input": .04, "output": 8},
        "provider_output_reservation_tokens": 393216}
    ledger = RelayLedger(tmp_path / "ledger.jsonl", RelayLimits(output_total=2000000), empirical_authorization=grant)
    for number, task in enumerate(tasks[:2], 1):
        binding = RelayBinding("swebench-pro-v2", task["instance_id"], f"b2-{number:02}", "main", 40,
                               "deepseek-flash", 393216, time.monotonic() + 60, False)
        payload = {"model": binding.model, "messages": [{"role": "user", "content": "offline"}]}
        raw = json.dumps(payload).encode()
        accounting = InputAccounting(None, "offline", contract_id="offline",
                                     payload_sha256=hashlib.sha256(raw).hexdigest(), reference_count=10000)
        attempt = ledger.reserve(binding, str(number), raw, payload, accounting)
        if pending and number == 2:
            continue
        if unknown and number == 2:
            ledger.settle(attempt, status="request_timeout")
        else:
            ledger.settle(attempt, status="complete", usage={"prompt_tokens": 1000, "completion_tokens": 2,
                "total_tokens": 1002, "prompt_cache_hit_tokens": 50}, finish_reason="stop")
    summary = {"estimated_cost_occupied_cny": ledger.cost_occupied_cny, "accounting_failures": ledger.failed_contracts,
        "tasks": [{"number": i, "instance_id": task["instance_id"], "attempt": 1 if i < 3 else 0,
                   "resolved": False if i == 2 else None} for i, task in enumerate(tasks, 1)]}
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    ledger.close()
    return manifest, summary


def test_preparation_retry_carries_real_ledger_fee_and_excludes_paid_failure(tmp_path):
    manifest, summary = closed_source(tmp_path)
    plan = parallel.preparation_remainder(tmp_path, manifest)
    assert [number for number, _task in plan["tasks"]] == [3]
    assert plan["used_cny"] == pytest.approx(summary["estimated_cost_occupied_cny"])
    assert plan["remaining_cny"] + plan["used_cny"] == pytest.approx(50)
    assert plan["remaining_requests"] == 918
    assert plan["source_ledger_sha256"] == hashlib.sha256((tmp_path / "ledger.jsonl").read_bytes()).hexdigest()


@pytest.mark.parametrize("pending,unknown", [(True, False), (False, True)])
def test_preparation_retry_cannot_reset_pending_or_failed_accounting(tmp_path, pending, unknown):
    manifest, _summary = closed_source(tmp_path, pending=pending, unknown=unknown)
    with pytest.raises(RuntimeError, match="previous_stage_not_settled_or_accounting_stopped"):
        parallel.preparation_remainder(tmp_path, manifest)


def test_preparation_retry_rejects_changed_previous_fee_summary(tmp_path):
    manifest, summary = closed_source(tmp_path)
    summary["estimated_cost_occupied_cny"] = 0
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    with pytest.raises(RuntimeError, match="previous_fee_summary_mismatch"):
        parallel.preparation_remainder(tmp_path, manifest)
