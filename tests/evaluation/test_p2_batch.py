"""Fixed P2 plan and shared balance contracts; synthetic grants, never paid calls."""
from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path
import socket
import sys

import pytest

from evaluation.linux_baseline import live, runner
from tests.evaluation.test_p1_live import authorization
from tests.evaluation.test_p1_continuation import predecessor  # noqa: F401

pytestmark = pytest.mark.usefixtures("synthetic_agent_revision")

PLAN = ["T02", "T03", "T05", "T06", "T07", "T08", "T09", "T10", "T11", "T12"]
PREDECESSORS = ["p1-live-20260908-073600", "p1-t04-live-20260908-130239"]


def p2_authorization():
    return {**authorization(), "stage": "P2", "allowed_tasks": PLAN.copy(),
            "execution_order": PLAN.copy(), "total_budget": "6.219802",
            "continuation": {"predecessor_experiment_ids": PREDECESSORS.copy()},
            "authorization_reference": "SYNTHETIC P2 ONLY"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("P2 offline test attempted network I/O")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


def test_p2_fixed_plan_enters_shared_policy():
    config = p2_authorization()
    assert live.execution_plan(config) == tuple(PLAN)
    policy = live.authorized_policy(config)
    assert policy.total_budget == Decimal("6.219802")
    assert policy.task_budget == Decimal("5")


@pytest.mark.parametrize("changes", [
    {"allowed_tasks": [], "execution_order": []},
    {"allowed_tasks": PLAN + ["T12"], "execution_order": PLAN + ["T12"]},
    {"allowed_tasks": PLAN + ["T13"], "execution_order": PLAN + ["T13"]},
    {"allowed_tasks": ["T01"] + PLAN, "execution_order": ["T01"] + PLAN},
    {"allowed_tasks": ["T04"] + PLAN, "execution_order": ["T04"] + PLAN},
    {"allowed_tasks": PLAN[:3], "execution_order": PLAN[:3]},
    {"allowed_tasks": PLAN[::-1], "execution_order": PLAN[::-1]},
    {"execution_order": PLAN[::-1]}, {"stage": "P1"},
    {"continuation": {"predecessor_experiment_ids": PREDECESSORS[:1]}},
    {"authorized": False}, {"authorization_reference": ""},
    {"total_budget": "10"}, {"total_budget": "6.219803"}, {"per_task_budget": "5.01"},
    {"token_budget": 3000000}, {"attempts_per_task": 2},
])
def test_p2_grant_cannot_expand_or_reuse_p1(changes, tmp_path, monkeypatch):
    config = {**p2_authorization(), **changes}
    monkeypatch.setattr(live, "freeze", lambda *args: pytest.fail("Invalid grant reached preparation"))
    with pytest.raises(ValueError):
        live.run(tmp_path / "never-created", config)
    assert not (tmp_path / "never-created").exists()


@pytest.mark.parametrize("status,expected", [("success", 0), ("functional_failure", 1),
    ("infrastructure_blocked", 1), ("not_run", 1)])
def test_p2_cli_counts_entire_plan_not_last_task(tmp_path, monkeypatch, status, expected):
    path = tmp_path / "synthetic-grant.json"
    runner.write_json(path, p2_authorization())
    tasks = {t: dict(final_status="success", starts=1) for t in PLAN}
    tasks["T02"]["final_status"] = status
    tasks.update(T01=dict(final_status="not_run", starts=0), T04=dict(final_status="not_run", starts=0))
    monkeypatch.setattr(live, "run", lambda *args: dict(stopped=True, selected_tasks=PLAN, tasks=tasks))
    assert runner.main(["--live", "--live-config", str(path), "--output", str(tmp_path / "out")]) == expected


def test_p2_help_describes_separate_fixed_batch(capsys):
    with pytest.raises(SystemExit) as error:
        runner.main(["--help"])
    assert error.value.code == 0
    assert "P2" in capsys.readouterr().out


@pytest.fixture
def predecessors(request, monkeypatch):
    """Two sealed *synthetic* finished experiments; never read the actual grants."""
    from evaluation.linux_baseline.billing import Ledger, journal
    module, t04, old_anchors = request.getfixturevalue("predecessor")
    original_git = runner.git
    monkeypatch.setattr(runner, "git", lambda root, *args, **kwargs: b"" if args == ("status", "--porcelain")
                        else original_git(root, *args, **kwargs))
    t04.update(experiment_id=PREDECESSORS[1], output_directory=str(module.RUNS / PREDECESSORS[1]),
               harness_revision=runner.git(runner.ROOT, "rev-parse", "HEAD").decode().strip(),
               authorization_reference="SYNTHETIC T04 GRANT")
    frozen = live.freeze(t04, live.authorized_policy(t04))
    module.claim(t04, frozen["carryover"])
    output = Path(t04["output_directory"])
    evidence = output / "T04"
    runner.write_json(module.RUNS / (PREDECESSORS[1] + ".config.json"), t04)
    runner.write_json(output / "frozen.json", frozen)
    evidence.mkdir()
    (evidence / "worker-started").mkdir()
    ledger = Ledger(live.authorized_policy(t04), journal(evidence / "billing.jsonl"))
    number = ledger.reserve("synthetic", 8000)
    ledger.dispatching(number)
    ledger.settle(number, dict(id="synthetic", model="deepseek-v4-flash", usage=dict(
        prompt_tokens=189397, prompt_cache_hit_tokens=70656, prompt_cache_miss_tokens=118741,
        completion_tokens=5604, total_tokens=195001)))
    billing = ledger.snapshot()
    runner.write_json(evidence / "billing-summary.json", billing)
    result = dict(task_id="T04", starts=1, billing=billing, final_status="success", runtime_completed=True,
                  usage_complete=True, cleanup_ok=True, within_budget=True, patch_verified=True)
    runner.write_json(evidence / "result.json", result)
    summary = dict(experiment_id=PREDECESSORS[1], selected_tasks=["T04"], stopped=True,
        carryover=frozen["carryover"], remaining_total_budget=billing["remaining_total_budget"],
        original_budget_remaining="6.4309674", tasks={"T04": result, "T01": dict(starts=0, final_status="not_run")})
    runner.write_json(output / "summary.json", summary)
    files = [module.RUNS / (PREDECESSORS[1] + ".config.json"), output / "frozen.json",
             evidence / "billing.jsonl", evidence / "billing-summary.json", evidence / "result.json",
             output / "summary.json", module._registration() / "receipt.json"]
    anchors = {str(p.relative_to(module.RUNS)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    monkeypatch.setattr(module, "P2_ANCHORS", anchors, raising=False)
    config = {**p2_authorization(), "experiment_id": "p2-offline", "harness_revision": t04["harness_revision"],
              "output_directory": str(module.RUNS / "p2-offline")}
    return module, config, {**old_anchors, **anchors}


def test_p2_freeze_derives_balance_from_both_sealed_experiments(predecessors):
    module, config, anchors = predecessors
    frozen = live.freeze(config, live.authorized_policy(config))
    carry = frozen["carryover"]
    assert carry is not None, "P2 freeze silently omitted historical spending"
    assert Decimal(carry["old_known_cost"]) == Decimal("0.4140886")
    assert Decimal(carry["old_unknown_reservation"]) == Decimal("3.154944")
    assert Decimal(carry["balance_before_allocation"]) == Decimal("6.4309674")
    assert Decimal(carry["allocation"]) == Decimal("6.219802")
    assert Decimal(carry["unallocated_balance"]) == Decimal("0.2111654")
    assert carry["source_hashes"] == anchors
    assert frozen["stage"] == "P2" and frozen["selected_tasks"] == PLAN


def test_p2_claim_is_once_per_original_balance_not_experiment_name(predecessors):
    module, config, anchors = predecessors
    carry = module.carryover(config)
    module.claim(config, carry)
    module.verify_claim(config, carry)
    other = {**config, "experiment_id": "renamed-p2", "output_directory": str(module.RUNS / "renamed-p2")}
    with pytest.raises(FileExistsError):
        module.claim(other, module.carryover(other))
    with pytest.raises(RuntimeError):
        module.verify_claim(other, module.carryover(other))
    assert {p: hashlib.sha256((module.RUNS / p).read_bytes()).hexdigest() for p in anchors} == anchors


@pytest.mark.parametrize("failure", ["changed_t04", "old_worker", "other_spending", "old_authorization",
                                      "changed_modes", "changed_rates", "excess_balance"])
def test_p2_carryover_rejects_unreconciled_or_reauthorized_history(predecessors, failure):
    module, config, _ = predecessors
    if failure == "changed_t04":
        runner.write_json(module.RUNS / PREDECESSORS[1] / "summary.json", {})
    elif failure == "old_worker":
        module.active_baseline_processes = lambda: [123]
    elif failure == "other_spending":
        runner.write_json(module.RUNS / "unknown-experiment/frozen.json", {})
    elif failure == "old_authorization":
        config["authorization_reference"] = "SYNTHETIC T04 GRANT"
    elif failure == "changed_modes":
        config["auxiliary_thinking"] = "enabled"
    elif failure == "changed_rates":
        config["miss_rate"] = "4"
    else:
        config["total_budget"] = "7"
    with pytest.raises((ValueError, RuntimeError)):
        module.claim(config, module.carryover(config))
    assert not (module.RUNS / "p2-batches").exists()


@pytest.mark.parametrize("scenario", ["p2_three", "p2_functional", "p2_unknown",
    "p2_missing_summary", "p2_mismatched_summary", "p2_acceptance_error"])
def test_real_p2_organizer_worker_native_transport_and_serial_remainder(predecessors, monkeypatch, scenario):
    module, config, anchors = predecessors
    original_process, original_materialize, original_replay = runner.process, runner.materialize, runner.replay
    created, started, budgets, sessions = [], [], [], []
    monkeypatch.setattr(live, "credential", lambda *args: "test-only-key")
    def materialize(spec, *args):
        assert spec[0] in PLAN, "P1 task must never be recreated or started"
        created.append(spec[0])
        return original_materialize(spec, *args)
    def process(command, cwd, env, log, timeout):
        if command[1:3] != ["-m", "evaluation.linux_baseline.live_worker"]:
            return original_process(command, cwd, env, log, timeout)
        task = cwd.parent.name
        assert task in PLAN[:3], "A fourth task must be refused before creating its worker"
        started.append(task)
        path = cwd.parent / "request.json"
        request = json.loads(path.read_text())
        budgets.append(Decimal(request["config"]["total_budget"]))
        sessions.append(request["session"])
        runner.write_json(path, {**request, "scenario": scenario,
            "offline_predecessor_anchors": module.ANCHORS, "offline_t04_anchors": module.P2_ANCHORS})
        outcome = original_process([sys.executable, str(Path(__file__).with_name("p1_controlled_worker.py")),
                                    str(path)], cwd, env, log, 90)
        if task == "T03" and scenario == "p2_missing_summary":
            (cwd.parent / "billing-summary.json").rename(cwd.parent / "offline-withheld-summary.json")
        if task == "T03" and scenario == "p2_mismatched_summary":
            runner.write_json(cwd.parent / "billing-summary.json", {"offline": "inconsistent"})
        return outcome
    def replay(spec, patch, directory):
        checks = original_replay(spec, patch, directory)
        if scenario == "p2_acceptance_error" and spec[0] == "T03":
            checks["target"].update(exception="OSError", accepted=False, collected=0, exit_code=None)
        return checks
    monkeypatch.setattr(runner, "materialize", materialize)
    monkeypatch.setattr(runner, "process", process)
    monkeypatch.setattr(runner, "replay", replay)
    grant = module.RUNS / "synthetic-p2-grant.json"
    runner.write_json(grant, config)
    output = Path(config["output_directory"])
    code = runner.main(["--live", "--live-config", str(grant), "--output", str(output)])
    summary = json.loads((output / "summary.json").read_text())
    assert code == 1  # Never hide the unrun prefix tail behind the last success.
    expected = PLAN[:3] if scenario in {"p2_three", "p2_functional"} else PLAN[:2]
    assert started == expected and sorted(created) == sorted(expected * 2)
    assert len(set(sessions)) == len(started)
    assert budgets[0] == Decimal("6.219802")
    previous = budgets[0]
    for task, available in zip(started, budgets):
        assert available == previous, "Task reset the batch balance"
        result = summary["tasks"][task]
        state = result.get("billing") or result.get("billing_journal")
        if state:
            previous = Decimal(state["remaining_total_budget"])
        assert (output / task / "final.patch").stat().st_size > 0
        observed = json.loads((output / task / "controlled.json").read_text())
        assert observed["native_builds"] == 1 and observed["network_attempts"] == 0
        wire = observed["wire_requests"]
        assert wire[-1] == {"model": "deepseek-v4-flash", "thinking": {"type": "disabled"},
            "reasoning_effort": None, "max_tokens": 1024,
            "tool_choice": {"type": "function", "function": {"name": "emit_sidecar_verdict"}}}
        assert all(r["thinking"] == {"type": "enabled"} and r["reasoning_effort"] == "high"
                   and r["max_tokens"] == 8000 for r in wire[:-1])
        assert observed["verifier"] == {"verdict": "accept", "trace":
            "provider_error" if scenario == "p2_unknown" and task == "T03" else "verifier_ok"}
    assert summary["stopped"] and summary["selected_tasks"] == PLAN
    assert summary["evidence_kind"] == "live/P2"  # Synthetic outputs stay under tmp; never published as scores.
    assert summary["measurement"]["planned"] == 10
    assert summary["measurement"]["started"] == len(expected)
    assert summary["measurement"]["not_run"] == 10 - len(expected)
    for task in ("T01", "T04", *PLAN[len(expected):]):
        assert summary["tasks"][task] == dict(task_id=task, starts=0, final_status="not_run")
        assert not (output / task).exists()
    if scenario in {"p2_three", "p2_functional"}:
        assert budgets == [Decimal("6.219802"), Decimal("5.197802"), Decimal("4.175802")]
        assert Decimal(summary["remaining_total_budget"]) == Decimal("3.153074")
        assert summary["stopped_reason"] == "next request cannot be reserved"
        successes = 2 if scenario == "p2_functional" else 3
        assert summary["measurement"]["end_to_end_success"] == successes
        assert summary["measurement"]["success_rate"] == successes / 3
        assert summary["measurement"]["patch_verified"] == successes
        assert summary["tasks"]["T02"]["final_status"] == ("functional_failure" if scenario == "p2_functional" else "success")
    else:
        assert summary["tasks"]["T03"]["final_status"] == "infrastructure_blocked"
        assert summary["measurement"]["success_rate"] == 0.5
        if scenario == "p2_unknown":
            assert Decimal(summary["tasks"]["T03"]["billing"]["unresolved_reservation"]) == Decimal("3.154944")
        if scenario in {"p2_missing_summary", "p2_mismatched_summary"}:
            assert summary["remaining_total_budget"] is None
            assert summary["original_budget_remaining"] is None
    assert summary["historical_usage_complete"] is False
    assert {p: hashlib.sha256((module.RUNS / p).read_bytes()).hexdigest() for p in anchors} == anchors
    # Neither same output nor a new identity grants a second batch allocation.
    for attempt in (config, {**config, "experiment_id": "repeat", "output_directory": str(module.RUNS / "repeat")}):
        with pytest.raises((FileExistsError, RuntimeError)):
            live.run(Path(attempt["output_directory"]), attempt)
    assert started == expected


@pytest.fixture
def prefix_descriptor(predecessors):
    from dataclasses import replace
    from evaluation.linux_baseline.billing import Ledger, journal
    from evaluation.linux_baseline.catalog import TASK_SPECS
    module, config, _ = predecessors
    policy = live.authorized_policy(config)
    frozen = live.freeze(config, policy)
    module.claim(config, frozen["carryover"])
    output = Path(config["output_directory"])
    runner.write_json(output / "frozen.json", frozen)
    results = {t: dict(task_id=t, starts=0, final_status="not_run") for t in PLAN}
    remaining = policy.total_budget
    for task in PLAN[:2]:
        directory = output / task
        request = live.worker_request(config, frozen, task, remaining)
        runner.write_json(directory / "request.json", request)
        (directory / "worker-started").mkdir()
        ledger = Ledger(replace(policy, total_budget=remaining), journal(directory / "billing.jsonl"))
        number = ledger.reserve("synthetic-prefix", 8000)
        ledger.dispatching(number)
        ledger.settle(number, dict(id="synthetic-prefix", model=policy.model, usage=dict(
            prompt_tokens=100, prompt_cache_hit_tokens=40, prompt_cache_miss_tokens=60,
            completion_tokens=20, total_tokens=120)))
        billing = ledger.snapshot()
        runner.write_json(directory / "billing-summary.json", billing)
        results[task] = dict(task_id=task, starts=1, session=request["session"], billing=billing,
            final_status="functional_failure", usage_complete=True, within_budget=True,
            cleanup_ok=True, acceptance_reliable=True, execution=dict(exception=None, cleanup_error=None))
        runner.write_json(directory / "result.json", results[task])
        remaining = Decimal(billing["remaining_total_budget"])
    runner.write_json(output / "summary.json", dict(experiment_id=config["experiment_id"],
        selected_tasks=PLAN, tasks=results))
    path = output / "T05/request.json"
    request = live.worker_request(config, frozen, "T05", remaining)
    runner.write_json(path, request)
    runner.materialize(next(s for s in TASK_SPECS if s[0] == "T05"), path.parent / "repo", path.parent / "home")
    return module, config, path, request


@pytest.mark.parametrize("mutation", ["none", "task_id", "session", "prompt", "frozen_hash", "task_tree_hash",
    "selected_tasks", "budget_reset", "prior_budget_reset", "prior_summary_missing", "prior_summary_mismatch",
    "prior_result_mismatch", "prior_not_started", "missing_allocation", "different_experiment", "initial_tree_changed"])
def test_p2_worker_rejects_forged_descriptor_or_prefix_before_transport(prefix_descriptor, monkeypatch, mutation):
    from evaluation.linux_baseline import live_worker
    module, config, path, request = prefix_descriptor
    output = path.parent.parent
    if mutation == "none":
        assert Decimal(request["config"]["total_budget"]) == Decimal("6.219074")
        assert live_worker.validate_descriptor(path) == request
        return
    if mutation in request:
        request[mutation] = ["T01"] if mutation == "selected_tasks" else "forged"
    elif mutation == "budget_reset":
        request["config"]["total_budget"] = "6.219802"
    elif mutation == "prior_budget_reset":
        prior = json.loads((output / "T03/request.json").read_text())
        prior["config"]["total_budget"] = "6.219802"
        runner.write_json(output / "T03/request.json", prior)
    elif mutation == "prior_summary_missing":
        (output / "T02/billing-summary.json").rename(output / "T02/withheld.json")
    elif mutation == "prior_summary_mismatch":
        runner.write_json(output / "T03/billing-summary.json", {})
    elif mutation == "prior_result_mismatch":
        runner.write_json(output / "T02/result.json", {})
    elif mutation == "prior_not_started":
        (output / "T03/worker-started").rename(output / "T03/withheld-start")
    elif mutation == "missing_allocation":
        (module._registration(config) / "ready").rename(module._registration(config) / "withheld-ready")
    elif mutation == "different_experiment":
        request["config"]["experiment_id"] = "renamed-p2"
    else:
        (path.parent / "repo/textkit/pipeline.py").write_text("# changed before worker\n")
    runner.write_json(path, request)
    monkeypatch.setattr(live_worker, "run", lambda *args, **kwargs: pytest.fail("Forged request reached worker"))
    with pytest.raises((ValueError, RuntimeError, OSError)):
        live_worker.main([str(path)], inner_factory=lambda: pytest.fail("Forged request reached transport"))
    assert not (path.parent / "worker-started").exists()


def test_p2_attempt_marker_prevents_worker_restart(prefix_descriptor, monkeypatch):
    from evaluation.linux_baseline import live_worker
    _, _, path, request = prefix_descriptor
    (path.parent / "worker-started").mkdir()
    monkeypatch.setattr(live_worker, "Ledger", lambda *args: pytest.fail("Repeated worker created ledger"))
    with pytest.raises(FileExistsError):
        live_worker.main([str(path)], inner_factory=lambda: pytest.fail("Repeated worker sent request"))


@pytest.mark.parametrize("scenario", ["not_admitted", "materialize_failure", "frozen_changed"])
def test_p2_pre_worker_stops_preserve_zero_started_report(predecessors, monkeypatch, scenario):
    module, config, _ = predecessors
    output = Path(config["output_directory"])
    monkeypatch.setattr(live, "credential", lambda *args: "test-only-key")
    monkeypatch.setattr(runner, "process", lambda *args: pytest.fail("Preflight rejection started worker"))
    if scenario == "not_admitted":
        config["total_budget"] = "3"
    elif scenario == "materialize_failure":
        def broken(*args):
            raise OSError("SYNTHETIC-SECRET-EXCEPTION")
        monkeypatch.setattr(runner, "materialize", broken)
    else:
        original = live.freeze
        count = 0
        def changed(*args):
            nonlocal count
            count += 1
            value = original(*args)
            return value if count == 1 else {**value, "offline_changed": True}
        monkeypatch.setattr(live, "freeze", changed)
    summary = live.run(output, config)
    assert summary["stopped"] and summary["measurement"]["started"] == 0
    assert summary["measurement"]["success_rate"] is None
    assert summary["measurement"]["not_run"] == 10
    assert all(result["starts"] == 0 for result in summary["tasks"].values())
    assert "SYNTHETIC-SECRET" not in json.dumps(summary)
    assert (output / "summary.json").exists()
    if scenario == "not_admitted":
        assert not (output / "T02").exists()


@pytest.mark.parametrize("failure", ["prefix_changed", "max_turns", "max_tokens", "wall_timeout"])
def test_p2_prefix_corruption_or_task_resource_limit_stops_batch(predecessors, monkeypatch, failure):
    from evaluation.linux_baseline.billing import Ledger, journal
    module, config, _ = predecessors
    output = Path(config["output_directory"])
    started = []
    monkeypatch.setattr(live, "credential", lambda *args: "fake")
    original_process, original_freeze = runner.process, live.freeze
    def process(command, cwd, env, log, timeout):
        if command[1:3] != ["-m", "evaluation.linux_baseline.live_worker"]:
            return original_process(command, cwd, env, log, timeout)
        started.append(cwd.parent.name)
        (cwd.parent / "worker-started").mkdir()
        request = json.loads((cwd.parent / "request.json").read_text())
        ledger = Ledger(live.authorized_policy(request["config"]), journal(cwd.parent / "billing.jsonl"))
        number = ledger.reserve("offline-limit", 8000)
        ledger.dispatching(number)
        ledger.settle(number, dict(id="offline", model="deepseek-v4-flash", usage=dict(
            prompt_tokens=100, prompt_cache_hit_tokens=40, prompt_cache_miss_tokens=60,
            completion_tokens=20, total_tokens=120)))
        runner.write_json(cwd.parent / "billing-summary.json", ledger.snapshot())
        log.write_text(json.dumps(dict(type="result", status=failure if failure.startswith("max_") else "completed")) + "\n")
        return dict(exit_code=None if failure == "wall_timeout" else 0, exception=None, cleanup_error=None,
                    timed_out=failure == "wall_timeout", elapsed_seconds=0)
    def freeze(*args):
        value = original_freeze(*args)
        if started and failure == "prefix_changed":
            (output / "T02/billing.jsonl").write_text('{"torn":')
        return value
    monkeypatch.setattr(runner, "process", process)
    monkeypatch.setattr(live, "freeze", freeze)
    summary = live.run(output, config)
    assert started == ["T02"]
    assert summary["stopped"] and summary["measurement"]["started"] == 1
    assert summary["tasks"]["T03"]["starts"] == 0
    assert (output / "T02/final.patch").exists()
    if failure == "prefix_changed":
        assert summary["remaining_total_budget"] is None
        assert summary["original_budget_remaining"] is None
    else:
        assert summary["tasks"]["T02"]["final_status"] == "budget_exceeded"
        assert summary["stopped_reason"] == "task resource limit; no automatic continuation"
