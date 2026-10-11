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


def test_long_evidence_path_binds_real_relays_without_changing_mount_identity(tmp_path, monkeypatch):
    case = tmp_path / ("evidence-" + "x" * 90) / "case"
    for name in ("input", "sockets", "workspace", "result", "home", "tmp", "driver"):
        (case / name).mkdir(parents=True, exist_ok=True)
    (case / "input/run.json").write_text('{"provider_default_output": true}')
    slot = case.parent
    (slot / "identity.json").write_text('{"instance_id": "offline"}')
    monkeypatch.setattr(parallel.pro, "OUT", tmp_path)
    monkeypatch.setattr(parallel.pro, "command", lambda *args, **kwargs: None)
    mounts = []

    class DockerProcess:
        returncode = 0

        def __init__(self, argv, **kwargs):
            for arg in argv:
                if arg.startswith("type=bind,source="):
                    source, target = arg.removeprefix("type=bind,source=").split(",target=", 1)
                    mounts.append((source, target.split(",")[0]))
                    if target.startswith("/workspace"):
                        assert Path(source).samefile(case / "workspace")
                    if target.startswith("/model"):
                        assert (Path(source) / "main.sock").exists()
                        assert (Path(source) / "auxiliary.sock").exists()
                        assert len(str(Path(source) / "auxiliary.sock").encode()) < 108

        def poll(self):
            return 0

        def wait(self, **kwargs):
            return 0

    monkeypatch.setattr(parallel.pro.subprocess, "Popen", DockerProcess)
    ledger = RelayLedger(tmp_path / "offline-ledger.jsonl", RelayLimits())
    run = getattr(parallel, "run_case_with_short_path", parallel.pro.run_case)
    try:
        run(slot, case, "offline-image", ledger, lambda *args: None,
            smoke=True, upstream="http://127.0.0.1:9/v1/chat/completions")
        assert ledger.attempts == {}
        assert mounts
        assert json.loads((slot / "agent-exit.json").read_text())["returncode"] == 0
    finally:
        ledger.close()


def test_combined_closed_phases_count_second_fee_and_exclude_paid_task(tmp_path):
    first = tmp_path / "first"
    first.mkdir()
    manifest, initial = closed_source(first)
    second = tmp_path / "second"
    second.mkdir()
    grant = json.loads((first / "ledger.jsonl").read_text().splitlines()[0])["fee_authorization"]
    grant["scope"] = [manifest["tasks"][2]["instance_id"]]
    grant["cost_limit"] = 50 - initial["estimated_cost_occupied_cny"]
    ledger = RelayLedger(second / "ledger.jsonl", RelayLimits(output_total=2000000), empirical_authorization=grant)
    binding = RelayBinding("swebench-pro-v2", grant["scope"][0], "b2-03", "main", 40,
                           "deepseek-flash", 393216, time.monotonic() + 60, False)
    payload = {"model": binding.model, "messages": [{"role": "user", "content": "offline"}]}
    raw = json.dumps(payload).encode()
    accounting = InputAccounting(None, "offline", contract_id="offline",
        payload_sha256=hashlib.sha256(raw).hexdigest(), reference_count=10000)
    attempt = ledger.reserve(binding, "third", raw, payload, accounting)
    ledger.settle(attempt, status="complete", usage={"prompt_tokens": 1000,
        "completion_tokens": 2, "total_tokens": 1002, "prompt_cache_hit_tokens": 50}, finish_reason="stop")
    fee = ledger.cost_occupied_cny
    (second / "summary.json").write_text(json.dumps({"tasks": [{"number": 3,
        "instance_id": grant["scope"][0], "attempt": 1, "resolved": False}],
        "estimated_cost_occupied_cny": fee, "accounting_failures": {}}))
    ledger.close()
    plan = parallel.combine_closed_sources([first, second], manifest, tmp_path / "combined")
    assert plan["tasks"] == []
    assert plan["used_cny"] == pytest.approx(initial["estimated_cost_occupied_cny"] + fee)
    assert plan["remaining_requests"] == 917


def test_forty_task_continuation_uses_current_authorized_request_limit(tmp_path):
    manifest, summary = closed_source(tmp_path)
    manifest.update(cost_limit=50, request_limit=1840)
    plan = parallel.preparation_remainder(tmp_path, manifest)
    assert plan["remaining_requests"] == 1838
    assert plan["remaining_cny"] == pytest.approx(50-summary["estimated_cost_occupied_cny"])


def test_next_forty_selection_excludes_both_closed_batches():
    candidates = ["task-" + str(i) for i in range(100)]
    sums = {"tasks/"+i+"/instruction.md": "a"*64 for i in candidates}
    earlier = [{"selection":{"seed":"fixed"}, "tasks":[{"instance_id":i} for i in candidates[:20]]},
               {"tasks":[{"instance_id":i} for i in candidates[20:40]]}]
    selected, excluded, seed = parallel.select_tasks(sums, earlier, 40)
    assert len(selected) == 40 and len(set(selected)) == 40
    assert excluded == set(candidates[:40]) and not set(selected) & excluded
    assert seed == "fixed"
    assert selected == sorted(candidates[40:], key=lambda i: hashlib.sha256((seed+"\n"+i).encode()).hexdigest())[:40]
