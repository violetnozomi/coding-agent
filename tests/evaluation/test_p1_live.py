"""P1 live gates and actual product assembly, without paid network traffic."""
from __future__ import annotations

from decimal import Decimal
import importlib
import importlib.util
import json
from pathlib import Path
import sys

import httpx
import pytest


def live():
    assert importlib.util.find_spec("evaluation.linux_baseline.live") is not None, "Live gate missing"
    return importlib.import_module("evaluation.linux_baseline.live")


def authorization():
    return dict(authorized=True, authorization_reference="OFFLINE TEST ONLY",
                budget_currency="CNY", total_budget="10", per_task_budget="5",
                allowed_tasks=["T01", "T04"], execution_order=["T01", "T04"],
                attempts_per_task=1, auto_continue_remaining_tasks=False,
                account_provider="DeepSeek", account_rates_confirmed=True,
                endpoint="https://api.deepseek.com", model="deepseek-v4-flash",
                effort="high", token_budget=2000000,
                main_thinking="enabled", main_output_limit=8000,
                auxiliary_thinking="disabled", auxiliary_output_limit=1024,
                rate_date="2026-09-08", rate_source="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
                hit_rate="0.10", miss_rate="3", output_rate="9",
                experiment_id="offline-contract", harness_revision="a" * 40,
                output_directory="/tmp/p1-offline-contract")


def continuation_authorization():
    """Synthetic grant only; tests never load a user's authorization file."""
    return {**authorization(), "allowed_tasks": ["T04"], "execution_order": ["T04"],
            "total_budget": "5", "continuation": {"predecessor_experiment_id": "p1-live-20260908-073600"}}


def test_single_t04_grant_does_not_require_t01():
    policy = live().authorized_policy(continuation_authorization())
    assert policy.total_budget == policy.task_budget == Decimal("5")


@pytest.mark.parametrize("changes", [
    {"allowed_tasks": [], "execution_order": []},
    {"allowed_tasks": ["T04", "T04"], "execution_order": ["T04", "T04"]},
    {"allowed_tasks": ["T02"], "execution_order": ["T02"]},
    {"allowed_tasks": ["T01", "T04"], "execution_order": ["T01", "T04"]},
    {"execution_order": ["T01", "T04"]}, {"allowed_tasks": "T04"},
    {"continuation": None}, {"continuation": {"predecessor_experiment_id": "unproven"}},
    {"total_budget": "10"}, {"per_task_budget": "6"},
])
def test_t04_plan_does_not_expand_or_reset_original_budget(changes):
    with pytest.raises(ValueError):
        live().authorized_policy({**continuation_authorization(), **changes})


@pytest.mark.parametrize("status,code", [("success", 0), ("functional_failure", 1),
    ("infrastructure_blocked", 1), ("not_run", 1)])
def test_cli_exit_uses_only_nonempty_selected_plan(tmp_path, monkeypatch, status, code):
    from evaluation.linux_baseline import runner
    config = continuation_authorization()
    path = tmp_path / "synthetic-grant.json"
    runner.write_json(path, config)
    monkeypatch.setattr(live(), "run", lambda *args: dict(stopped=True, selected_tasks=["T04"], tasks={
        "T01": {"final_status": "not_run", "starts": 0}, "T04": {"final_status": status, "starts": int(status != "not_run")}}))
    assert runner.main(["--live", "--live-config", str(path), "--output", str(tmp_path / "out")]) == code


@pytest.mark.parametrize("changes", [dict(authorized=False), dict(authorization_reference=""),
    dict(account_rates_confirmed=False), dict(budget_currency="USD"),
    dict(allowed_tasks=["T01", "T02"]), dict(attempts_per_task=2), dict(effort="max"),
    dict(endpoint="https://private:key@example.invalid"), dict(total_budget=None)])
def test_incomplete_authorization_is_fail_closed(changes):
    with pytest.raises(ValueError):
        live().authorized_policy({**authorization(), **changes})


@pytest.mark.parametrize("changes", [dict(rate_date="2026-09-07"),
    dict(hit_rate="0.05", miss_rate="1.5", output_rate="4.5"), dict(rate_source="https://example.invalid")])
def test_stale_or_off_peak_only_grant_cannot_start_pilot(changes):
    """The all-period ceiling cannot be replaced by a stale/off-peak-only grant."""
    with pytest.raises(ValueError):
        live().authorized_policy({**authorization(), **changes})


def test_freeze_distinguishes_rate_lookup_from_effective_date(monkeypatch):
    from evaluation.linux_baseline import runner
    module = live()
    config = {**authorization(), "harness_revision": runner.git(runner.ROOT, "rev-parse", "HEAD").decode().strip()}
    original_git = runner.git
    # Only suppress test-worktree dirtiness; source hashes and dependencies are real reads.
    monkeypatch.setattr(runner, "git", lambda root, *args: b"" if args == ("status", "--porcelain")
                        else original_git(root, *args))
    frozen = module.freeze(config, module.authorized_policy(config))
    assert frozen["rates"]["retrieved_at"] == "2026-09-08T06:47:24Z"
    assert frozen["rates"]["effective_date"] is None
    assert frozen["rates"]["calculation_basis"] == "published peak-rate ceiling for all periods; not account invoice"
    assert frozen["provider_model_version_at_lookup"] == "DeepSeek-V4-Flash-0731"
    assert frozen["versions"]["packages"]["httpx"]
    assert frozen["request_modes"] == {
        "main": {"thinking": "enabled", "effort": "high", "output_limit": 8000},
        "auxiliary": {"thinking": "disabled", "effort": None, "output_limit": 1024},
        "transport_purpose": "unknown; validates mode set, not message or tool labels",
        "budget_scope": "shared task/experiment ledger; uncertainty stops all modes",
    }


def test_legacy_grant_cannot_silently_authorize_new_auxiliary_mode():
    legacy = {k: v for k, v in authorization().items() if k not in {
        "main_thinking", "main_output_limit", "auxiliary_thinking", "auxiliary_output_limit"}}
    with pytest.raises(ValueError):
        live().authorized_policy(legacy)


def test_missing_live_authorization_never_prepares_p0_or_starts_worker(tmp_path, monkeypatch):
    from evaluation.linux_baseline import runner
    monkeypatch.setattr(runner, "prepare", lambda *a, **kw: pytest.fail("P0 must not be rebuilt"))
    with pytest.raises(SystemExit) as error:
        runner.main(["--live", "--output", str(tmp_path / "no-run")])
    assert error.value.code == 2
    assert not (tmp_path / "no-run").exists()


def test_live_help_names_authorization(capsys):
    from evaluation.linux_baseline import runner
    with pytest.raises(SystemExit) as error:
        runner.main(["--help"])
    assert error.value.code == 0
    assert "--live-config" in capsys.readouterr().out


def test_real_provider_factory_auxiliary_clients_share_ledger_and_restore(tmp_path):
    from evaluation.linux_baseline.billing import Ledger
    from evaluation.linux_baseline.live_worker import bounded_product
    from nz_coder.providers.openai_compatible import OpenAICompatibleProvider
    from nz_coder.interface import headless
    from nz_coder.runtime.core.request import AgentDefinition
    from nz_coder.runtime.core.profiles import MAIN_PROFILE
    from nz_coder.state.workdir import scoped_workdir
    policy = live().authorized_policy(authorization())
    ledger = Ledger(policy, lambda _row: None)
    sent = []
    def reply(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"id": "offline", "model": policy.model, "choices": [], "usage": {
            "prompt_tokens": 1, "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 1,
            "completion_tokens": 1, "total_tokens": 2}})
    before = OpenAICompatibleProvider.create_client
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with scoped_workdir(workspace), bounded_product(policy, ledger, "test-only", inner_factory=lambda: httpx.MockTransport(reply)):
        provider = OpenAICompatibleProvider(api_key="test-only", base_url=policy.endpoint)
        for _ in range(2):
            with provider.create_client() as client:
                provider.create_completion(client, model=policy.model, messages=[], max_tokens=16000)
        request = headless.RunRequest(agent=AgentDefinition(name="test", instructions="test"),
            profile=MAIN_PROFILE, workspace=workspace, session_id="offline", messages=[], stream=False)
        assert not request.profile.allow_child_agents
        assert "task" not in request.tool_names
        assert "tool_search" not in request.tool_names
        assert "bash" in request.tool_names
        with pytest.raises(Exception):
            OpenAICompatibleProvider(api_key="test-only", base_url="https://other.invalid").create_client()
    assert len(sent) == 2
    assert all(row["max_tokens"] == 8000 for row in sent)
    assert ledger.snapshot()["usage"]["total_tokens"] == 4
    assert OpenAICompatibleProvider.create_client is before


def test_journal_reconstruction_retains_killed_attempt_reservation(tmp_path):
    from evaluation.linux_baseline.billing import Ledger, journal, summarize
    policy = live().authorized_policy(authorization())
    path = tmp_path / "billing.jsonl"
    ledger = Ledger(policy, journal(path))
    number = ledger.reserve("only a hash", 8000)
    ledger.dispatching(number)
    # No worker finally block or settlement: parent sees durable dispatch intent.
    recovered = summarize([json.loads(line) for line in path.read_text().splitlines()], policy)
    assert recovered["blocked"] and not recovered["usage_complete"]
    assert Decimal(recovered["unresolved_reservation"]) == Decimal("3.217728")
    assert recovered["requests_sent"] == 1


def test_actual_headless_native_provider_write_and_unknown_billing_stop(tmp_path):
    from evaluation.linux_baseline import runner
    from evaluation.linux_baseline.catalog import TASK_SPECS
    tmp_path = tmp_path / "T01"
    tmp_path.mkdir()
    runner.materialize(TASK_SPECS[0], tmp_path / "repo", tmp_path / "home")
    descriptor = tmp_path / "request.json"
    runner.write_json(descriptor, dict(config={**authorization(), "output_directory": str(tmp_path.parent)}, prompt=
        "Offline transport wiring only: write tests/p1_probe.txt with OFFLINE ONLY. Do not solve a task."))
    env = runner.isolated_environment(tmp_path / "home", runner.ROOT)
    env.update(API_KEY="test-only", API_BASE_URL="https://api.deepseek.com", MODEL_PROVIDER="openai-compatible",
               MODEL_ID="deepseek-v4-flash", MODEL_VARIANT="", NZ_AUTO_MODE_CLASSIFIER_ENABLED="0")
    outcome = runner.process([sys.executable, str(Path(__file__).with_name("p1_controlled_worker.py")), str(descriptor)],
                            tmp_path / "repo", env, tmp_path / "runtime.jsonl", 90)
    assert not outcome["timed_out"] and not outcome["exception"] and not outcome["cleanup_error"], outcome
    observed = json.loads((tmp_path / "controlled.json").read_text())
    assert observed["native_builds"] == 1
    assert observed["network_attempts"] == 0
    assert len(observed["wire_requests"]) == 2
    assert all(row["max_tokens"] == 8000 for row in observed["wire_requests"])
    assert "task" not in observed["tool_names"] and "tool_search" not in observed["tool_names"]
    assert (tmp_path / "repo/tests/p1_probe.txt").read_text() == "OFFLINE ONLY\n"
    state = json.loads((tmp_path / "billing-summary.json").read_text())
    assert state["blocked"] and not state["usage_complete"]
    assert state["requests_sent"] == 2
    assert Decimal(state["unresolved_reservation"]) == Decimal("3.217728")
    assert "test-only" not in (tmp_path / "billing.jsonl").read_text()


def test_direct_worker_restart_refused_before_client(tmp_path, monkeypatch):
    from evaluation.linux_baseline import live_worker
    evidence = tmp_path / "T01"
    (evidence / "worker-started").mkdir(parents=True)
    monkeypatch.setattr(live_worker, "Ledger", lambda *a: pytest.fail("Repeated worker created a new ledger"))
    with pytest.raises(FileExistsError):
        live_worker.run({**authorization(), "output_directory": str(tmp_path)}, evidence / "repo", "same-session", "same-prompt", evidence)


def test_unsupported_secret_fields_not_copied_to_descriptor():
    with pytest.raises(ValueError):
        live().authorized_policy({**authorization(), "api_key": "synthetic-secret"})


@pytest.mark.parametrize("mode", ["ordinary_failure", "unknown_usage", "torn_journal",
                                  "acceptance_infrastructure", "empty_acceptance", "cancelled",
                                  "partial_settlement_persistence", "missing_worker_summary"])
def test_serial_organizer_retains_failures_and_stops_at_two_or_uncertainty(tmp_path, monkeypatch, mode):
    from evaluation.linux_baseline import runner
    from evaluation.linux_baseline.billing import Ledger, journal
    module = live()
    output = tmp_path / "experiment"
    config = {**authorization(), "output_directory": str(output)}
    monkeypatch.setattr(module, "freeze", lambda *args: {"contract": "offline-only"})
    monkeypatch.setattr(module, "credential", lambda *args: "test-only-key")
    starts, replays = [], []
    def controlled_process(command, cwd, env, log, timeout):
        starts.append(cwd.parent.name)
        assert env["API_KEY"] == "test-only-key"
        assert env["NZ_PLANNING_ENABLED"] == "0" and env["NZ_REFLECTION_ENABLED"] == "0"
        descriptor = json.loads((cwd.parent / "request.json").read_text())
        assert "test-only-key" not in json.dumps(descriptor)
        policy = module.authorized_policy(descriptor["config"])
        ledger = Ledger(policy, journal(cwd.parent / "billing.jsonl"))
        if mode == "partial_settlement_persistence":
            from evaluation.linux_baseline.billing import make_client
            append = ledger.record
            def broken_record(row):
                if row["event"] in {"uncertain", "rejected"}:
                    raise OSError("offline-diagnostic-failure")
                append(row)
                if row["event"] == "settled":
                    raise OSError("offline-fsync-failure-after-write")
            ledger.record = broken_record
            def reply(req):
                return httpx.Response(200, json=dict(id="offline", model=policy.model, choices=[], usage=dict(
                    prompt_tokens=100, completion_tokens=20, total_tokens=120,
                    prompt_cache_hit_tokens=40, prompt_cache_miss_tokens=60)))
            with make_client(policy, ledger, "fake", inner=httpx.MockTransport(reply)) as client:
                with pytest.raises(Exception):
                    client.chat.completions.create(model=policy.model, messages=[], max_tokens=8000,
                        extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "high"})
            runner.write_json(cwd.parent / "billing-summary.json", ledger.snapshot())
            log.write_text(json.dumps({"type": "result", "status": "completed"}) + "\n")
            return dict(exit_code=0, timed_out=False, exception=None, cleanup_error=None, elapsed_seconds=0)
        number = ledger.reserve("controlled", 8000)
        ledger.dispatching(number)
        if mode not in {"unknown_usage", "torn_journal"}:
            ledger.settle(number, dict(id="controlled", model=policy.model, usage=dict(
                prompt_tokens=100, completion_tokens=20, total_tokens=120,
                prompt_cache_hit_tokens=40, prompt_cache_miss_tokens=60)))
        elif mode == "torn_journal":
            with (cwd.parent / "billing.jsonl").open("a") as stream:
                stream.write('{"unfinished":')
        if mode != "missing_worker_summary":
            runner.write_json(cwd.parent / "billing-summary.json", ledger.snapshot())
        log.write_text(json.dumps({"type": "result", "status": "completed"}) + "\n")
        if mode == "cancelled":
            (cwd / "tests/cancelled_probe.txt").write_text("offline cancellation evidence\n")
            raise KeyboardInterrupt("offline organizer cancellation")
        return dict(exit_code=0, timed_out=False, exception=None, cleanup_error=None, elapsed_seconds=0)
    def check(spec, patch, directory):
        replays.append(spec[0])
        assert patch.exists()
        if mode == "empty_acceptance":
            return {}
        def outcome(accepted):
            return dict(accepted=accepted, exit_code=0 if accepted else 1, timed_out=False,
                        exception=None, cleanup_error=None, collected=1, passed=int(accepted),
                        failed=int(not accepted), errors=0, skipped=0)
        checks = {"target": outcome(False), "regression": outcome(True)}
        if mode == "acceptance_infrastructure":
            checks["target"].update(exception="OSError", exit_code=None, collected=0)
        return checks
    monkeypatch.setattr(runner, "process", controlled_process)
    monkeypatch.setattr(runner, "replay", check)
    try:
        summary = module.run(output, config)
    except KeyboardInterrupt:
        pytest.fail("Organizer cancellation escaped before preserving attempt artifacts")
    expected = ["T01", "T04"] if mode == "ordinary_failure" else ["T01"]
    assert starts == replays == expected
    assert summary["stopped"]
    assert summary["tasks"]["T02"]["final_status"] == "not_run"
    assert (output / "T01/final.patch").exists()
    if mode == "ordinary_failure":
        assert summary["tasks"]["T04"]["final_status"] == "functional_failure"
        assert Decimal(summary["remaining_total_budget"]) == Decimal("9.999272")
    else:
        assert summary["tasks"]["T04"]["starts"] == 0
        assert summary["tasks"]["T01"]["final_status"] == "infrastructure_blocked"
        if mode in {"unknown_usage", "torn_journal"}:
            assert summary["tasks"]["T01"]["usage_complete"] is False
        if mode == "cancelled":
            assert "offline cancellation evidence" in (output / "T01/final.patch").read_text()
        elif mode == "torn_journal":
            assert summary["remaining_total_budget"] is None
        elif mode == "unknown_usage":
            assert Decimal(summary["tasks"]["T01"]["billing"]["unresolved_reservation"]) > 0
        elif mode == "partial_settlement_persistence":
            assert summary["remaining_total_budget"] is None
            assert summary["tasks"]["T01"]["usage_complete"] is False
    # Even failed attempts consume the experiment identity.
    with pytest.raises(FileExistsError):
        module.run(output, config)
    assert starts == expected


def test_output_directory_binding_precedes_credentials(tmp_path, monkeypatch):
    module = live()
    monkeypatch.setattr(module, "credential", lambda *a: pytest.fail("Different experiment accessed credentials"))
    with pytest.raises(ValueError, match="Output differs"):
        module.run(tmp_path / "other", authorization())


@pytest.mark.parametrize("primary", [KeyboardInterrupt("offline-primary"), RuntimeError("offline-primary"), None])
def test_worker_final_summary_failure_cannot_replace_primary(tmp_path, monkeypatch, primary):
    from evaluation.linux_baseline import live_worker
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore
    from nz_coder.interface import cli
    evidence = tmp_path / "T01"
    repo = evidence / "repo"
    repo.mkdir(parents=True)
    monkeypatch.setenv("API_KEY", "fake")
    monkeypatch.setattr(WorkspaceTrustStore, "trust", lambda *args: None)
    def main(*args):
        if primary is not None:
            raise primary
        return 0
    def fail_summary(*args):
        raise OSError("private-summary-sentinel")
    monkeypatch.setattr(cli, "main", main)
    monkeypatch.setattr(live_worker, "write_json", fail_summary)
    with pytest.raises(BaseException) as caught:
        live_worker.run({**authorization(), "output_directory": str(tmp_path)}, repo, "offline", "offline", evidence,
                        inner_factory=lambda: pytest.fail("Mocked CLI must not create clients"))
    if primary is not None:
        assert caught.value is primary
    else:
        assert "private-summary-sentinel" not in str(caught.value)


def test_organizer_real_native_sidecar_unknown_stops_after_verified_patch(tmp_path, monkeypatch):
    """Catches treating fail-open completion/valid patch as complete billing."""
    from evaluation.linux_baseline import runner
    module = live()
    output = tmp_path / "offline-sidecar-experiment"
    config = {**authorization(), "output_directory": str(output)}
    monkeypatch.setattr(module, "freeze", lambda *args: {"contract": "offline-only"})
    monkeypatch.setattr(module, "credential", lambda *args: "test-only-key")
    original_process = runner.process
    starts = []
    def controlled_process(command, cwd, env, log, timeout):
        if command[1:3] != ["-m", "evaluation.linux_baseline.live_worker"]:
            return original_process(command, cwd, env, log, timeout)
        starts.append(cwd.parent.name)
        descriptor = cwd.parent / "request.json"
        request = json.loads(descriptor.read_text())
        runner.write_json(descriptor, {**request, "scenario": "sidecar_unknown"})
        return original_process([sys.executable, str(Path(__file__).with_name("p1_controlled_worker.py")),
                                 str(descriptor)], cwd, env, log, 90)
    monkeypatch.setattr(runner, "process", controlled_process)
    summary = module.run(output, config)
    result = summary["tasks"]["T01"]
    assert starts == ["T01"]
    assert result["runtime_completed"] and result["patch_verified"], result
    assert result["acceptance"]["target"]["passed"] == 3
    assert result["acceptance"]["regression"]["passed"] == 4
    assert result["final_status"] == "infrastructure_blocked"
    assert not result["usage_complete"] and result["cleanup_ok"]
    assert summary["tasks"]["T04"]["starts"] == 0 and summary["stopped"]
    observed = json.loads((output / "T01/controlled.json").read_text())
    assert observed["network_attempts"] == 0 and observed["native_builds"] == 1
    assert observed["verifier"] == {"verdict": "accept", "trace": "provider_error"}
    assert observed["wire_requests"][-1]["thinking"] == {"type": "disabled"}
    assert observed["wire_requests"][-1]["max_tokens"] == 1024
    assert result["billing"]["unresolved_reservation"] == "3.154944000"
    assert result["billing"]["sdk_retry_sends"] == 0
    assert (output / "T01/final.patch").stat().st_size > 0
    assert "test-only-key" not in json.dumps(result)


@pytest.mark.parametrize("adapter", ["AnthropicProvider", "GeminiProvider", "OpenAIResponsesProvider"])
def test_auxiliary_other_provider_blocked_before_client_creation(adapter):
    """A provider switch may not escape the only metered client family."""
    import nz_coder.providers as providers
    from evaluation.linux_baseline.billing import BillingStopped, Ledger
    from evaluation.linux_baseline.live_worker import bounded_product
    policy = live().authorized_policy(authorization())
    cls = getattr(providers, adapter)
    original = cls.create_client
    with bounded_product(policy, Ledger(policy, lambda row: None), "test-only"):
        with pytest.raises(BillingStopped):
            cls(api_key="test-only", base_url="https://example.invalid").create_client()
    assert cls.create_client is original
