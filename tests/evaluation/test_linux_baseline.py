"""Offline checks for evidence retention, acceptance and native baseline wiring."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import sys

import pytest

from evaluation.linux_baseline import runner
from evaluation.linux_baseline.catalog import TASK_SPECS, manifest, task_files
from evaluation.linux_baseline.reference import apply_reference


def execution(**kwargs):
    return dict(exit_code=0, timed_out=False, exception=None, cleanup_error=None, **kwargs)


def test_manifest_frozen_categories_and_private_checks():
    data = manifest()
    assert len(data["tasks"]) == 12
    assert sorted(Counter(t["category"] for t in data["tasks"]).values()) == [3, 3, 3, 3]
    assert len({t["repository"] for t in data["tasks"]}) == 2
    assert data["pilot_task_ids"] == ["T01", "T04"]
    for task, spec in zip(data["tasks"], TASK_SPECS):
        assert len(task["task_revision"]) == 64
        assert task["proposed_budget"]["cost"] is None
        assert not any("reference" in name or "acceptance" in name for name in task_files(spec))


@pytest.mark.parametrize("spec", TASK_SPECS, ids=[s[0] for s in TASK_SPECS])
def test_initial_failure_and_organizer_reference_replay(spec, tmp_path):
    repo = tmp_path / "repo"
    runner.materialize(spec, repo, tmp_path / "home")
    initial = runner.acceptance(spec, repo, tmp_path / "initial-evaluator")
    assert initial["target"]["exit_code"] == 1
    assert initial["target"]["failed"] > 0
    assert initial["target"]["errors"] == 0
    assert initial["regression"]["accepted"]
    apply_reference(spec[0], repo)
    patch = tmp_path / "final.patch"
    runner.export_patch(repo, patch, tmp_path / "home")
    verified = runner.replay(spec, patch, tmp_path / "replay")
    assert verified["target"]["accepted"]
    assert verified["regression"]["accepted"]


def test_patch_preserves_untracked_deletion_binary_and_mode(tmp_path):
    spec = TASK_SPECS[0]
    repo = tmp_path / "repo"
    runner.materialize(spec, repo, tmp_path / "home")
    (repo / "new.bin").write_bytes(b"\0\xffnew")
    (repo / "textkit/parser.py").unlink()
    (repo / "textkit/stats.py").chmod(0o755)
    patch = tmp_path / "final.patch"
    exported = runner.export_patch(repo, patch, tmp_path / "home")
    assert set(exported["changed_files"]) == {"new.bin", "textkit/parser.py", "textkit/stats.py"}
    fresh = tmp_path / "fresh"
    runner.materialize(spec, fresh, tmp_path / "fresh-home")
    runner.git(fresh, "apply", "--binary", str(patch), env=runner.isolated_environment(tmp_path / "fresh-home"))
    assert (fresh / "new.bin").read_bytes() == b"\0\xffnew"
    assert not (fresh / "textkit/parser.py").exists()
    assert (fresh / "textkit/stats.py").stat().st_mode & 0o111


@pytest.mark.parametrize("body", ["", '<testcase name="x"><skipped/></testcase>',
                                     '<testcase name="x"><error/></testcase>'])
def test_zero_skipped_or_collection_error_is_not_accepted(tmp_path, body):
    xml = tmp_path / "result.xml"
    xml.write_text(f"<testsuites><testsuite>{body}</testsuite></testsuites>")
    assert not runner.test_result(xml, execution())["accepted"]


@pytest.mark.parametrize("changes", [{"timed_out": True}, {"exit_code": 1},
                                       {"exception": "OSError"}, {"cleanup_error": "TimeoutExpired"}])
def test_failure_and_timeout_never_end_to_end_success(changes):
    process = {**execution(), **changes}
    assert runner.classify(execution=process, runtime_status="completed",
                           patch_verified=True, within_budget=True) != "success"


def test_patch_pass_runtime_failure_is_separate():
    assert runner.classify(execution=execution(), runtime_status="error",
                           patch_verified=True, within_budget=True) == "runtime_error"


def test_scope_checks_exact_files_and_directory_boundaries():
    assert runner.scope_violations(TASK_SPECS[0], ["textkit/parser.py", "tests/new.py"]) == []
    assert runner.scope_violations(TASK_SPECS[0], ["textkit/parser.py.bak", "README.md"]) == ["textkit/parser.py.bak", "README.md"]
    assert runner.scope_violations(TASK_SPECS[3], ["worklog/codec.py", "worklog_evil.py"]) == ["worklog_evil.py"]


def test_offline_validation_failure_is_not_silently_green():
    assert not runner.offline_wiring_valid([])


def test_publish_rejects_live_or_private_projections(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    runner.write_json(output / "summary.json", {"evidence_kind": "live", "real_model_calls": 1})
    with pytest.raises(ValueError, match="offline-only"):
        runner.publish(output, tmp_path / "public")
    runner.write_json(output / "summary.json", {
        "evidence_kind": "offline/preparation", "real_model_calls": 0, "offline_attempts": [],
    })
    runner.write_json(output / "manifest.json", {"path": "/home/synthetic/private"})
    with pytest.raises(ValueError, match="Private host path"):
        runner.publish(output, tmp_path / "public")


def test_budget_insufficient_never_starts_next_task(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(runner, "offline_attempt", lambda spec, *a, **k: started.append(spec[0]))
    batch = runner.RequestBudget(60, Decimal(6))
    runner.admitted_offline_attempt(TASK_SPECS[0], tmp_path / "one", batch)
    with pytest.raises(runner.BudgetExceeded):
        runner.admitted_offline_attempt(TASK_SPECS[1], tmp_path / "two", batch)
    assert started == ["T01"]
    assert not (tmp_path / "two").exists()


def test_unknown_budget_or_usage_never_becomes_free():
    budget = runner.RequestBudget(10, Decimal(1))
    with pytest.raises(runner.BudgetExceeded):
        budget.reserve(1, None)
    budget.reserve(10, Decimal(1))
    # Unknown/failed result cannot refund a reservation.
    with pytest.raises(runner.BudgetExceeded):
        budget.reserve(1, Decimal("0.1"))
    assert budget.reserved_cost == Decimal(1)


def test_budget_reservation_is_atomic():
    budget = runner.RequestBudget(10, Decimal(10))
    def reserve(_index):
        try:
            budget.reserve(1, Decimal(1))
            return True
        except runner.BudgetExceeded:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(reserve, range(50))) == 10


def test_environment_excludes_developer_secrets_and_state(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEY", "offline-test-sentinel-not-a-secret")
    monkeypatch.setenv("NZ_PROVIDER_HARD_TIMEOUT_SECONDS", "987")
    first = runner.isolated_environment(tmp_path / "one")
    second = runner.isolated_environment(tmp_path / "two")
    assert "API_KEY" not in first
    assert "NZ_PROVIDER_HARD_TIMEOUT_SECONDS" not in first
    assert first["HOME"] != second["HOME"]
    assert first["XDG_STATE_HOME"] != second["XDG_STATE_HOME"]


def test_timeout_reaps_owned_child(tmp_path):
    outcome = runner.process([sys.executable, "-c", "import time; time.sleep(60)"],
                             tmp_path, runner.isolated_environment(tmp_path / "home"),
                             tmp_path / "log", 0.1)
    assert outcome["timed_out"]
    assert outcome["cleanup_error"] is None
    assert outcome["elapsed_seconds"] < 5


def test_exception_saves_result_and_failure_patch_before_cleanup(monkeypatch, tmp_path):
    def fail(*_args, **_kwargs):
        raise OSError("synthetic launch failure")
    monkeypatch.setattr(runner, "process", fail)
    result = runner.offline_attempt(TASK_SPECS[0], tmp_path / "attempt")
    saved = json.loads((tmp_path / "attempt/result.json").read_text())
    assert saved == result
    assert saved["classification"] == "runtime_error"
    assert saved["usage"] is None and saved["cost"] is None
    assert (tmp_path / "attempt/final.patch").exists()
    assert (tmp_path / "attempt/repo/textkit/parser.py").exists()


def test_live_is_closed_before_preparation(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "prepare", lambda *a, **k: pytest.fail("must not prepare or start requests"))
    with pytest.raises(SystemExit) as error:
        runner.main(["--output", str(tmp_path / "live"), "--live"])
    assert error.value.code == 2
    assert not (tmp_path / "live").exists()


def test_real_native_wiring_two_isolated_sessions_offline(tmp_path):
    records = [runner.offline_attempt(spec, tmp_path / spec[0]) for spec in (TASK_SPECS[0], TASK_SPECS[3])]
    assert records[0]["attempt_id"] != records[1]["attempt_id"]
    for record in records:
        wiring = record["wiring"]
        assert wiring["network_attempts"] == 0
        assert wiring["environment_class"] == "ProductRunEnvironment"
        assert wiring["runner_class"] == "AgentRunner"
        assert wiring["services"]["tools"] == "ProductionToolRuntime"
        assert wiring["services"]["verifier"] == "ProductionCompletionVerifier"
        assert wiring["private_state_in_isolated_home"]
        assert set(wiring["workspace_local_state_directories"]) <= {"index"}
        assert wiring["session_files_in_isolated_home"] > 0
        assert "tests/driver_probe.txt" in record["patch"]["changed_files"]
        assert record["usage"] is None and record["cost"] is None
        assert not record["patch_verified"]  # Fake responses did NOT solve tasks.
        assert not record["counts_as_real_attempt"]
        assert record["execution"]["cleanup_error"] is None
        assert record["runtime_status"] in {"completed", "max_turns"}
        if record["runtime_status"] == "max_turns":
            assert not record["runtime_completed"]
            assert record["classification"] == "budget_exceeded"


def test_native_fake_request_budget_stops_before_next_response(tmp_path):
    record = runner.offline_attempt(TASK_SPECS[0], tmp_path / "budget", max_requests=1)
    assert record["wiring"]["provider_calls"] == 1
    assert record["wiring"]["budget_stopped"]
    assert record["classification"] == "budget_exceeded"
    assert record["patch"]["changed_files"] == []
