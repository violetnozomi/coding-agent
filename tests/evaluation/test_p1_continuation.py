"""Synthetic predecessor evidence and one-shot T04 continuation contracts."""
from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys

import pytest

from evaluation.linux_baseline import runner
from tests.evaluation.test_p1_live import continuation_authorization


pytestmark = pytest.mark.usefixtures("synthetic_agent_revision")


@pytest.fixture
def predecessor(tmp_path, monkeypatch):
    from evaluation.linux_baseline import continuation as module
    from evaluation.linux_baseline.billing import Ledger, journal
    from evaluation.linux_baseline.live import authorized_policy
    root = tmp_path / "runs"
    root.mkdir()
    monkeypatch.setattr(module, "RUNS", root)
    old = root / module.PREDECESSOR_ID
    old.mkdir()
    (old / "T01").mkdir()
    config = {**continuation_authorization(), "output_directory": str(root / "new-t04"),
              "experiment_id": "new-t04"}
    legacy = {**config, "experiment_id": module.PREDECESSOR_ID, "total_budget": "10",
              "allowed_tasks": ["T01", "T04"], "execution_order": ["T01", "T04"],
              "authorization_reference": "OLD SYNTHETIC GRANT"}
    legacy.pop("continuation")
    policy = authorized_policy(legacy)
    ledger = Ledger(policy, journal(old / "T01/billing.jsonl"))
    first = ledger.reserve("synthetic", 8000)
    ledger.dispatching(first)
    ledger.settle(first, dict(id="synthetic", model=policy.model, usage=dict(
        prompt_tokens=100, completion_tokens=20, total_tokens=120,
        prompt_cache_hit_tokens=40, prompt_cache_miss_tokens=60)))
    second = ledger.reserve("synthetic-unknown", 1024)
    ledger.dispatching(second)
    billing = ledger.snapshot()
    result = dict(task_id="T01", starts=1, billing=billing, final_status="infrastructure_blocked")
    runner.write_json(root / (module.PREDECESSOR_ID + ".config.json"), legacy)
    runner.write_json(old / "frozen.json", dict(experiment_id=module.PREDECESSOR_ID,
        authorization_reference=legacy["authorization_reference"], harness_revision=legacy["harness_revision"],
        total_budget="10"))
    runner.write_json(old / "T01/result.json", result)
    runner.write_json(old / "summary.json", dict(experiment_id=module.PREDECESSOR_ID, stopped=True,
        remaining_total_budget=billing["remaining_total_budget"],
        tasks={"T01": result, "T04": dict(starts=0, final_status="not_run")}))
    files = [root / (module.PREDECESSOR_ID + ".config.json"), old / "frozen.json",
             old / "T01/result.json", old / "T01/billing.jsonl", old / "summary.json"]
    anchors = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    monkeypatch.setattr(module, "ANCHORS", anchors)
    monkeypatch.setattr(module, "active_baseline_processes", lambda: [])
    return module, config, anchors


def test_carryover_is_derived_from_readonly_predecessor(predecessor):
    module, config, anchors = predecessor
    carry = module.carryover(config)
    assert Decimal(carry["old_known_cost"]) == Decimal("0.000364")
    assert Decimal(carry["old_unknown_reservation"]) == Decimal("3.154944")
    assert Decimal(carry["balance_before_allocation"]) == Decimal("6.844692")
    assert Decimal(carry["allocation"]) == 5
    assert Decimal(carry["unallocated_balance"]) == Decimal("1.844692")
    assert carry["source_hashes"] == anchors


@pytest.mark.parametrize("failure", ["changed_evidence", "old_worker", "prior_output", "excess_allocation"])
def test_carryover_blocks_inconsistent_or_spent_evidence(predecessor, failure):
    module, config, _ = predecessor
    if failure == "changed_evidence":
        runner.write_json(module.RUNS / module.PREDECESSOR_ID / "summary.json", {})
    elif failure == "old_worker":
        module.active_baseline_processes = lambda: [123]
    elif failure == "prior_output":
        runner.write_json(module.RUNS / "other-experiment/frozen.json", {})
    else:
        config["total_budget"] = "7"
    with pytest.raises((ValueError, RuntimeError)):
        module.claim(config, module.carryover(config))


def test_changed_experiment_name_cannot_allocate_again(predecessor):
    module, config, anchors = predecessor
    carry = module.carryover(config)
    module.claim(config, carry)
    module.verify_claim(config, carry)
    other = {**config, "experiment_id": "another-t04", "output_directory": str(module.RUNS / "another-t04")}
    with pytest.raises(FileExistsError):
        module.claim(other, module.carryover(other))
    with pytest.raises(RuntimeError):
        module.verify_claim(other, module.carryover(other))
    assert {p: hashlib.sha256((module.RUNS / p).read_bytes()).hexdigest() for p in anchors} == anchors


def test_worker_rejects_t01_in_single_t04_grant_before_client(tmp_path, monkeypatch):
    from evaluation.linux_baseline import live_worker
    evidence = tmp_path / "T01"
    evidence.mkdir()
    monkeypatch.setattr(live_worker, "Ledger", lambda *args: pytest.fail("Unauthorized worker constructed ledger"))
    with pytest.raises(RuntimeError, match="authorized"):
        live_worker.run({**continuation_authorization(), "output_directory": str(tmp_path)},
            evidence / "repo", "forged", "forged", evidence, inner_factory=lambda: pytest.fail("transport"))
    assert not (evidence / "worker-started").exists()


@pytest.mark.parametrize("scenario", ["t04_success", "t04_unknown", "t04_missing_summary", "t04_mismatched_summary"])
def test_single_t04_real_organizer_worker_native_and_transport(predecessor, monkeypatch, scenario):
    from evaluation.linux_baseline import live
    module, config, anchors = predecessor
    config["harness_revision"] = runner.git(runner.ROOT, "rev-parse", "HEAD").decode().strip()
    output = Path(config["output_directory"])
    original_git, original_materialize, original_process = runner.git, runner.materialize, runner.process
    monkeypatch.setattr(runner, "git", lambda root, *args, **kwargs: b"" if args == ("status", "--porcelain")
                        else original_git(root, *args, **kwargs))
    monkeypatch.setattr(live, "credential", lambda *args: "test-only-key")
    created, started = [], []
    def materialize(spec, *args):
        assert spec[0] == "T04", "Unselected task materialized"
        created.append(spec[0])
        return original_materialize(spec, *args)
    def process(command, cwd, env, log, timeout):
        if command[1:3] != ["-m", "evaluation.linux_baseline.live_worker"]:
            return original_process(command, cwd, env, log, timeout)
        assert cwd.parent.name == "T04", "Unselected worker started"
        started.append(cwd.parent.name)
        path = cwd.parent / "request.json"
        descriptor = json.loads(path.read_text())
        runner.write_json(path, {**descriptor, "scenario": scenario, "offline_predecessor_anchors": anchors})
        result = original_process([sys.executable, str(Path(__file__).with_name("p1_controlled_worker.py")), str(path)],
                                  cwd, env, log, 90)
        if scenario == "t04_missing_summary":
            (cwd.parent / "billing-summary.json").rename(cwd.parent / "offline-withheld-summary.json")
        if scenario == "t04_mismatched_summary":
            runner.write_json(cwd.parent / "billing-summary.json", {"offline": "inconsistent"})
        return result
    monkeypatch.setattr(runner, "materialize", materialize)
    monkeypatch.setattr(runner, "process", process)
    grant = module.RUNS / "synthetic-new-grant.json"
    runner.write_json(grant, config)
    code = runner.main(["--live", "--live-config", str(grant), "--output", str(output)])
    summary = json.loads((output / "summary.json").read_text())
    result = summary["tasks"]["T04"]
    assert created == ["T04", "T04"]  # Work copy and clean replay, no T01.
    assert started == ["T04"]
    assert summary["selected_tasks"] == ["T04"] and summary["stopped"]
    assert summary["tasks"]["T01"] == dict(task_id="T01", final_status="not_run", starts=0)
    assert result["patch_verified"], result
    assert result["acceptance"]["target"]["passed"] == 3
    assert result["acceptance"]["regression"]["passed"] == 4
    observed = json.loads((output / "T04/controlled.json").read_text())
    assert observed["network_attempts"] == 0 and observed["native_builds"] == 1
    assert observed["wire_requests"][-1]["thinking"] == {"type": "disabled"}
    assert observed["wire_requests"][-1]["max_tokens"] == 1024
    assert observed["wire_requests"][-1]["reasoning_effort"] is None
    assert observed["wire_requests"][-1]["tool_choice"] == {
        "type": "function", "function": {"name": "emit_sidecar_verdict"}}
    assert all(r["thinking"] == {"type": "enabled"} and r["reasoning_effort"] == "high" and r["max_tokens"] == 8000
               for r in observed["wire_requests"][:-1])
    billing = result.get("billing") or result["billing_journal"]
    assert billing["sdk_retry_sends"] == 0
    assert len(observed["wire_requests"]) == billing["requests_sent"] == 9
    if scenario == "t04_success":
        assert result["runtime_completed"]
        assert code == 0 and result["final_status"] == "success"
        assert result["usage_complete"] and result["billing"]["unresolved_reservation"] == "0"
        assert observed["verifier"] == dict(verdict="accept", trace="verifier_ok")
    elif scenario == "t04_unknown":
        assert result["runtime_completed"]
        assert code == 1 and result["final_status"] == "infrastructure_blocked"
        assert not result["usage_complete"]
        assert Decimal(result["billing"]["unresolved_reservation"]) == Decimal("3.154944")
        assert observed["verifier"] == dict(verdict="accept", trace="provider_error")
    else:
        assert code == 1 and result["final_status"] == "infrastructure_blocked"
        assert not result["usage_complete"] and result["within_budget"] is None
        assert result["billing_consistency"] in {"worker_summary_missing", "journal_worker_summary_mismatch"}
    assert summary["historical_usage_complete"] is False
    if scenario in {"t04_missing_summary", "t04_mismatched_summary"}:
        assert summary["original_budget_remaining"] is summary["remaining_total_budget"] is None
    else:
        assert Decimal(summary["original_budget_remaining"]) == (
            Decimal(summary["carryover"]["unallocated_balance"]) + Decimal(summary["remaining_total_budget"]))
    assert {p: hashlib.sha256((module.RUNS / p).read_bytes()).hexdigest() for p in anchors} == anchors
    assert (output / "T04/final.patch").stat().st_size > 0


def test_parallel_allocation_has_exactly_one_winner(predecessor):
    from concurrent.futures import ThreadPoolExecutor
    module, config, _ = predecessor
    carry = module.carryover(config)
    def allocate(_):
        try:
            module.claim(config, carry)
            return "allocated"
        except FileExistsError:
            return "refused"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(allocate, range(2))) == ["allocated", "refused"]


@pytest.mark.parametrize("failed_sync", [1, 2, 3, 4, 5])
def test_receipt_persistence_failure_never_releases_allocation(predecessor, monkeypatch, failed_sync):
    from evaluation.linux_baseline import live, live_worker
    from evaluation.linux_baseline.catalog import TASK_SPECS, digest
    module, config, _ = predecessor
    config["harness_revision"] = runner.git(runner.ROOT, "rev-parse", "HEAD").decode().strip()
    carry = module.carryover(config)
    sync_count = 0
    original_sync = module.os.fsync
    def broken_sync(*args):
        nonlocal sync_count
        sync_count += 1
        if sync_count == failed_sync:
            raise OSError("offline-sync-failure")
        original_sync(*args)
    monkeypatch.setattr(module.os, "fsync", broken_sync)
    with pytest.raises(OSError):
        module.claim(config, carry)
    with pytest.raises(FileExistsError):
        module.claim(config, carry)
    with pytest.raises(RuntimeError, match="ready"):
        module.verify_claim(config, carry)
    # Even a separately supplied descriptor cannot turn the failed allocation
    # into a worker start. All files below are synthetic tmp evidence.
    original_git = runner.git
    monkeypatch.setattr(runner, "git", lambda root, *args, **kwargs: b"" if args == ("status", "--porcelain")
                        else original_git(root, *args, **kwargs))
    frozen = live.freeze(config, live.authorized_policy(config))
    output = Path(config["output_directory"])
    runner.write_json(output / "frozen.json", frozen)
    path = output / "T04/request.json"
    runner.write_json(path, dict(config=config, task_id="T04", selected_tasks=["T04"], frozen_hash=digest(frozen),
        session=f"p1-{config['experiment_id']}-T04", prompt=next(s[4] for s in TASK_SPECS if s[0] == "T04")))
    monkeypatch.setattr(live_worker, "run", lambda *args, **kwargs: pytest.fail("Failed allocation reached worker"))
    with pytest.raises(RuntimeError, match="ready"):
        live_worker.main([str(path)], inner_factory=lambda: pytest.fail("transport"))


@pytest.fixture
def worker_descriptor(predecessor, monkeypatch):
    from evaluation.linux_baseline import live
    from evaluation.linux_baseline.catalog import TASK_SPECS, digest
    module, config, _ = predecessor
    config["harness_revision"] = runner.git(runner.ROOT, "rev-parse", "HEAD").decode().strip()
    original_git = runner.git
    monkeypatch.setattr(runner, "git", lambda root, *args, **kwargs: b"" if args == ("status", "--porcelain")
                        else original_git(root, *args, **kwargs))
    frozen = live.freeze(config, live.authorized_policy(config))
    module.claim(config, frozen["carryover"])
    output = Path(config["output_directory"])
    runner.write_json(output / "frozen.json", frozen)
    descriptor = dict(config=config, task_id="T04", selected_tasks=["T04"], frozen_hash=digest(frozen),
        session=f"p1-{config['experiment_id']}-T04", prompt=next(s[4] for s in TASK_SPECS if s[0] == "T04"))
    path = output / "T04/request.json"
    runner.write_json(path, descriptor)
    return module, path, descriptor


@pytest.mark.parametrize("mutation", ["none", "task_id", "prompt", "session", "selected_tasks", "frozen_hash",
                                      "experiment_id", "harness_revision", "token_budget", "total_budget", "output_directory"])
def test_worker_validates_task_grant_and_freeze_before_start(worker_descriptor, monkeypatch, mutation):
    from evaluation.linux_baseline import live_worker
    module, path, descriptor = worker_descriptor
    if mutation == "none":
        assert live_worker.validate_descriptor(path) == descriptor
        return
    if mutation in descriptor:
        descriptor[mutation] = ["T01"] if mutation == "selected_tasks" else "forged"
    else:
        descriptor["config"][mutation] = {
            "experiment_id": "forged", "harness_revision": "b" * 40, "token_budget": 3000000,
            "total_budget": "4", "output_directory": str(module.RUNS / "forged")}[mutation]
    runner.write_json(path, descriptor)
    monkeypatch.setattr(live_worker, "run", lambda *args, **kwargs: pytest.fail("Forged descriptor reached worker"))
    with pytest.raises((RuntimeError, ValueError)):
        live_worker.main([str(path)], inner_factory=lambda: pytest.fail("transport"))
    assert not (path.parent / "worker-started").exists()


@pytest.mark.parametrize("plan", [[], None, ["T04", "T04"], ["T01"]])
def test_cli_never_accepts_empty_or_invalid_result_plan(tmp_path, monkeypatch, plan):
    from evaluation.linux_baseline import live
    path = tmp_path / "synthetic-grant.json"
    runner.write_json(path, continuation_authorization())
    monkeypatch.setattr(live, "run", lambda *args: dict(stopped=True, selected_tasks=plan,
        tasks={"T04": dict(final_status="success")}))
    assert runner.main(["--live", "--live-config", str(path), "--output", str(tmp_path / "out")]) == 1


@pytest.mark.parametrize("amount", [5, 5.0, True, "+5", " 5", "5e0"])
def test_noncanonical_budget_rejected_before_allocating(predecessor, monkeypatch, amount):
    from evaluation.linux_baseline import live
    module, config, _ = predecessor
    config["total_budget"] = amount
    monkeypatch.setattr(live, "freeze", lambda *args: pytest.fail("Invalid money reached freeze/allocation"))
    with pytest.raises(ValueError, match="Decimal strings"):
        live.run(Path(config["output_directory"]), config)
    assert not (module.RUNS / "p1-continuations").exists()


def test_original_pair_workers_keep_exact_serial_budget(tmp_path, monkeypatch):
    from evaluation.linux_baseline import live, live_worker
    from evaluation.linux_baseline.billing import Ledger, journal
    from evaluation.linux_baseline.catalog import TASK_SPECS, digest
    from tests.evaluation.test_p1_live import authorization
    config = {**authorization(), "output_directory": str(tmp_path),
              "harness_revision": runner.git(runner.ROOT, "rev-parse", "HEAD").decode().strip()}
    original_git = runner.git
    monkeypatch.setattr(runner, "git", lambda root, *args, **kwargs: b"" if args == ("status", "--porcelain")
                        else original_git(root, *args, **kwargs))
    policy = live.authorized_policy(config)
    frozen = live.freeze(config, policy)
    runner.write_json(tmp_path / "frozen.json", frozen)
    def descriptor(task, total):
        path = tmp_path / task / "request.json"
        value = dict(config={**config, "total_budget": total}, task_id=task, selected_tasks=["T01", "T04"],
            frozen_hash=digest(frozen), session=f"p1-{config['experiment_id']}-{task}",
            prompt=next(s[4] for s in TASK_SPECS if s[0] == task))
        runner.write_json(path, value)
        return path, value
    path, value = descriptor("T01", "10")
    assert live_worker.validate_descriptor(path) == value
    ledger = Ledger(policy, journal(tmp_path / "T01/billing.jsonl"))
    number = ledger.reserve("synthetic", 8000)
    ledger.dispatching(number)
    ledger.settle(number, dict(id="synthetic", model=policy.model, usage=dict(
        prompt_tokens=100, completion_tokens=20, total_tokens=120,
        prompt_cache_hit_tokens=40, prompt_cache_miss_tokens=60)))
    billing = ledger.snapshot()
    runner.write_json(tmp_path / "T01/billing-summary.json", billing)
    runner.write_json(tmp_path / "T01/result.json", dict(billing=billing, final_status="functional_failure"))
    path, value = descriptor("T04", billing["remaining_total_budget"])
    assert live_worker.validate_descriptor(path) == value
    path, _ = descriptor("T04", "10")
    with pytest.raises(RuntimeError, match="serial remainder"):
        live_worker.validate_descriptor(path)
