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
                rate_date="2026-09-07", rate_source="https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
                hit_rate="0.10", miss_rate="3", output_rate="9",
                experiment_id="offline-contract", harness_revision="a" * 40,
                output_directory="/tmp/p1-offline-contract")


@pytest.mark.parametrize("changes", [dict(authorized=False), dict(authorization_reference=""),
    dict(account_rates_confirmed=False), dict(budget_currency="USD"),
    dict(allowed_tasks=["T01", "T02"]), dict(attempts_per_task=2), dict(effort="max"),
    dict(endpoint="https://private:key@example.invalid"), dict(total_budget=None)])
def test_incomplete_authorization_is_fail_closed(changes):
    with pytest.raises(ValueError):
        live().authorized_policy({**authorization(), **changes})


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
    runner.materialize(TASK_SPECS[0], tmp_path / "repo", tmp_path / "home")
    descriptor = tmp_path / "request.json"
    runner.write_json(descriptor, dict(config=authorization(), prompt=
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
                                  "acceptance_infrastructure", "empty_acceptance", "cancelled"])
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
        number = ledger.reserve("controlled", 8000)
        ledger.dispatching(number)
        if mode not in {"unknown_usage", "torn_journal"}:
            ledger.settle(number, dict(id="controlled", model=policy.model, usage=dict(
                prompt_tokens=100, completion_tokens=20, total_tokens=120,
                prompt_cache_hit_tokens=40, prompt_cache_miss_tokens=60)))
        elif mode == "torn_journal":
            with (cwd.parent / "billing.jsonl").open("a") as stream:
                stream.write('{"unfinished":')
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
    # Even failed attempts consume the experiment identity.
    with pytest.raises(FileExistsError):
        module.run(output, config)
    assert starts == expected


def test_output_directory_binding_precedes_credentials(tmp_path, monkeypatch):
    module = live()
    monkeypatch.setattr(module, "credential", lambda *a: pytest.fail("Different experiment accessed credentials"))
    with pytest.raises(ValueError, match="Output differs"):
        module.run(tmp_path / "other", authorization())


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
