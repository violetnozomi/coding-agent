"""一次性 Pro 续跑入口的离线费用交接；不启动 Agent 或读取密钥。"""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def plan_fixture(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[2] / "docs/evidence/swebench-pro-20-2026-10-11/continue-unattempted.py"
    spec = importlib.util.spec_from_file_location("pro_continuation_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "OUT", tmp_path)
    exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda p: False if str(p) in {
        "/proc/1368343", "/proc/1476085"} else exists(p))

    def save(name, value, *, lines=False):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(json.dumps(e) + "\n" for e in value) if lines else json.dumps(value))
        return hashlib.sha256(target.read_bytes()).hexdigest()

    tasks = [{"instance_id": f"instance_owner__repo-{c * 40}"} for c in "abcd"]
    save("task-manifest.json", {"tasks": tasks})
    original = [{"event": "ledger_created", "limits": {"requests": 20, "input_total": 100000, "output_total": 100000},
                 "fee_authorization": {"cost_limit": 50}},
                {"event": "admitted", "attempt_id": "old", "unit": tasks[0]["instance_id"], "output_reserved": 8000},
                {"event": "settled", "attempt_id": "old", "usage_valid": True,
                 "usage": {"prompt_tokens": 2, "completion_tokens": 8001}, "estimated_cost_reported_cny": .1,
                 "contract_failed": True, "input_over_reservation": 0, "output_over_reservation": 1},
                {"event": "accounting_contract_failed", "attempt_id": "old"}]
    digest = save("live/ledger.jsonl", original, lines=True)
    save("accounting-stop-audit.json", {"raw_ledger_sha256": digest, "attempt_id": "old"})
    save("live/summary.json", {"physical_requests": 1, "estimated_cost_occupied_cny": .1,
         "tasks": [{**t, "attempt": int(i == 0)} for i, t in enumerate(tasks)]})
    current = [{"event": "ledger_created", "fee_authorization": {
                "predecessor_ledger_sha256": digest, "cost_limit": 49.9}},
               {"event": "admitted", "attempt_id": "current", "unit": tasks[1]["instance_id"]},
               {"event": "settled", "attempt_id": "current", "usage_valid": True,
                "usage": {"prompt_tokens": 100, "completion_tokens": 200}, "estimated_cost_reported_cny": .4}]
    summary = {"physical_requests": 1, "estimated_cost_occupied_cny": .4,
               "tasks": [{**t, "attempt": int(i == 0)} for i, t in enumerate(tasks[1:])]}
    save("next/ledger.jsonl", current, lines=True)
    save("next/summary.json", summary)
    return module, save, current, summary, tasks


def test_closed_fee_chain_only_selects_never_requested_tasks(plan_fixture):
    module, _, _, _, tasks = plan_fixture
    plan = module.remaining_plan(preparation_retry=True)
    assert [c["task"] for c in plan["tasks"]] == tasks[2:]
    assert plan["used"]["requests"] == 2
    assert plan["used"]["cost_cny"] == .5
    assert plan["remaining"] == {"requests": 18, "cost_cny": 49.5}
    assert len(plan["source_fee_ledger_chain_sha256"]) == 2


@pytest.mark.parametrize("failure", ["missing_settlement", "duplicate_call", "unknown_usage",
                                     "accounting_failed", "summary_fee", "missing_report", "false_zero_attempt"])
def test_unclosed_or_inconsistent_stage_cannot_authorize_more_requests(plan_fixture, failure):
    module, save, current, summary, _ = plan_fixture
    if failure == "missing_settlement":
        current.pop()
    elif failure == "duplicate_call":
        current.append(current[1])
    elif failure == "unknown_usage":
        current[-1]["usage_valid"] = False
    elif failure == "accounting_failed":
        current.append({"event": "accounting_contract_failed"})
    elif failure == "summary_fee":
        summary["estimated_cost_occupied_cny"] += 1
    elif failure == "missing_report":
        summary["tasks"].pop()
    else:
        summary["tasks"][0]["attempt"] = 0
    save("next/ledger.jsonl", current, lines=True)
    save("next/summary.json", summary)
    with pytest.raises(RuntimeError):
        module.remaining_plan(preparation_retry=True)


def test_running_coordinator_prevents_preparation_retry(plan_fixture, monkeypatch):
    module, *_ = plan_fixture
    exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda p: True if str(p) == "/proc/1476085" else exists(p))
    with pytest.raises(RuntimeError, match="continuation_coordinator_still_running"):
        module.remaining_plan(preparation_retry=True)
