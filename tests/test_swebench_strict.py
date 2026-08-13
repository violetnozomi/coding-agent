"""Contracts for leaderboard-grade SWE-bench Verified runs."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest


def test_benchmark_profiles_make_verified_the_main_profile():
    from nz_coder.swebench.profiles import DEFAULT_PROFILE, get_profile

    verified = get_profile(DEFAULT_PROFILE)
    lite = get_profile("lite")

    assert DEFAULT_PROFILE == "verified"
    assert verified.dataset == "princeton-nlp/SWE-bench_Verified"
    assert verified.expected_instances == 500
    assert verified.leaderboard is True
    assert lite.dataset == "princeton-nlp/SWE-bench_Lite"
    assert lite.expected_instances == 300
    assert lite.leaderboard is False


def test_strict_prompt_never_contains_hints_or_official_test_knowledge():
    from nz_coder.swebench.adapter import SWEBenchAdapter

    prompt = SWEBenchAdapter().format_instance_prompt({
        "instance_id": "owner__repo-1",
        "repo": "owner/repo",
        "base_commit": "abc123",
        "problem_statement": "Public issue text.",
        "hints_text": "SECRET HINT",
        "FAIL_TO_PASS": "SECRET FAIL TEST",
        "PASS_TO_PASS": "SECRET PASS TEST",
        "patch": "SECRET GOLD PATCH",
    })

    assert "Public issue text." in prompt
    assert "SECRET" not in prompt
    assert "Verified" in prompt


def test_strict_tool_policy_excludes_network_and_extension_paths():
    from nz_coder.swebench.policy import STRICT_ALLOWED_TOOLS, validate_strict_tool_names

    assert "bash" in STRICT_ALLOWED_TOOLS
    assert "webfetch" not in STRICT_ALLOWED_TOOLS
    assert "task" not in STRICT_ALLOWED_TOOLS
    assert "load_skill" not in STRICT_ALLOWED_TOOLS
    assert validate_strict_tool_names(["read_file", "bash"]) == []
    assert validate_strict_tool_names(["read_file", "webfetch", "mcp_search"]) == [
        "webfetch", "mcp_search",
    ]


@pytest.mark.parametrize("command", [
    "curl https://github.com/org/repo/pull/1",
    "wget https://example.com/answer",
    "git fetch origin pull/1/head",
    "python3 -c \"import urllib.request; urllib.request.urlopen('https://example.com')\"",
    "/usr/bin/curl https://github.com/org/repo/pull/1",
    "env curl https://github.com/org/repo/pull/1",
    "git -c protocol.version=2 fetch origin",
    "bash -c 'cat </dev/tcp/example.com/80'",
])
def test_strict_bash_policy_rejects_network_commands(command):
    from nz_coder.swebench.policy import strict_bash_violation

    assert strict_bash_violation(command)


@pytest.mark.parametrize(("command", "expected_guidance"), [
    ("cd src && grep -n token app.py", "bash.workdir"),
    ("git log -1", "git diff, git grep, git ls-files"),
    ("python3 -c 'print(1)'", "python3 -m py_compile"),
])
def test_strict_bash_rejection_explains_an_allowed_rewrite(
    tmp_path, command, expected_guidance,
):
    from nz_coder.runtime.execution_context import scoped_runtime_overrides
    from nz_coder.runtime.workdir import scoped_workdir
    from nz_coder.tools.bash import run_bash

    with scoped_workdir(tmp_path), scoped_runtime_overrides(strict_local_tools=True):
        result = run_bash(command)

    assert result.startswith("Error: ")
    assert expected_guidance in result


def test_strict_agent_protocol_exposes_shell_and_navigation_decisions():
    from nz_coder.swebench.orchestrator import _strict_agent_protocol

    protocol = _strict_agent_protocol()

    assert "bash.workdir" in protocol
    assert "git diff | grep | ls-files | rev-parse | status" in protocol
    assert "python3 -m py_compile | compileall | pytest" in protocol
    assert "repo_map" in protocol and "3 or more files" in protocol
    assert "read_symbol" in protocol and "known function" in protocol
    assert "find_symbol_callers" in protocol and "analyze_impact" in protocol


def test_attempt_journal_is_exact_once_and_resumable(tmp_path):
    from nz_coder.swebench.artifacts import AttemptJournal

    journal = AttemptJournal(tmp_path / "attempts.jsonl")
    assert journal.completed_ids() == set()
    journal.record({"instance_id": "one", "attempt": 1, "status": "completed"})
    assert journal.completed_ids() == {"one"}
    with pytest.raises(ValueError, match="already recorded"):
        journal.record({"instance_id": "one", "attempt": 1, "status": "completed"})


def test_attempt_claim_survives_crash_without_permitting_second_inference(tmp_path):
    from nz_coder.swebench.artifacts import AttemptJournal

    journal = AttemptJournal(tmp_path / "attempts.jsonl")
    journal.claim("one")

    assert journal.attempted_ids() == {"one"}
    assert journal.completed_ids() == set()
    with pytest.raises(ValueError, match="already recorded"):
        journal.claim("one")


def test_public_trajectory_sanitizes_secrets_and_workspace(tmp_path):
    from nz_coder.swebench.artifacts import export_public_trajectory

    trace = tmp_path / "raw.jsonl"
    trace.write_text(json.dumps({
        "event": "tool_call",
        "name": "read_file",
        "path": "/private/work/repo/a.py",
        "Authorization": "Bearer top-secret",
        "output": "ok",
    }) + "\n", encoding="utf-8")
    output = tmp_path / "public.jsonl"

    export_public_trajectory(trace, output, workspace=Path("/private/work/repo"))

    public = output.read_text(encoding="utf-8")
    assert "top-secret" not in public
    assert "/private/work/repo" not in public
    assert "<workspace>/a.py" in public
    assert "[REDACTED]" in public


def test_submission_validator_requires_complete_verified_bundle(tmp_path):
    from nz_coder.swebench.profiles import get_profile
    from nz_coder.swebench.submission import validate_submission_inputs

    predictions = tmp_path / "predictions.jsonl"
    predictions.write_text(json.dumps({
        "instance_id": "one",
        "model_name_or_path": "nz-coder-deepseek-v4-flash",
        "model_patch": "diff --git a/a b/a\n",
    }) + "\n", encoding="utf-8")

    report = validate_submission_inputs(
        profile=get_profile("verified"),
        predictions_path=predictions,
        manifest={"leaderboard_eligible": True, "attempts_per_instance": 1},
        trajectories_dir=tmp_path / "trajs",
        logs_dir=tmp_path / "logs",
    )

    assert report.eligible is False
    assert any("expected 500 predictions" in item for item in report.errors)
    assert any("missing trajectory" in item for item in report.errors)
    assert any("strict_mode" in item for item in report.errors)
    assert any("attempt journal" in item for item in report.errors)


def test_submission_builder_normalizes_official_logs_and_patch(tmp_path):
    from nz_coder.swebench.profiles import BenchmarkProfile, instance_ids_digest
    from nz_coder.swebench.submission import build_submission_bundle

    ids_digest = instance_ids_digest(["one"])
    profile = BenchmarkProfile("verified", "dataset", "test", 1, True, ids_digest)
    predictions = tmp_path / "predictions.jsonl"
    patch = "diff --git a/a.py b/a.py\n"
    predictions.write_text(json.dumps({
        "instance_id": "one",
        "model_name_or_path": "nz-coder-deepseek-v4-flash",
        "model_patch": patch,
    }) + "\n", encoding="utf-8")
    manifest = tmp_path / "predictions.manifest.json"
    manifest.write_text(json.dumps({
        "leaderboard_eligible": True,
        "attempts_per_instance": 1,
        "strict_mode": True,
        "partial_selection": False,
        "hints_used": False,
        "official_test_knowledge_used": False,
        "answer_search_network_enabled": False,
        "public_trajectories": True,
        "benchmark_profile": "verified",
        "dataset": "dataset",
        "dataset_instance_ids_sha256": ids_digest,
        "expected_instance_ids_sha256": ids_digest,
        "instance_ids": ["one"],
        "source_sha256": "a" * 64,
    }), encoding="utf-8")
    journal = tmp_path / "predictions.attempts.jsonl"
    journal.write_text(
        json.dumps({"event": "claim", "instance_id": "one", "attempt": 1}) + "\n"
        + json.dumps({
            "event": "result",
            "instance_id": "one",
            "attempt": 1,
            "prediction": {
                "instance_id": "one",
                "model_name_or_path": "nz-coder-deepseek-v4-flash",
                "model_patch": patch,
            },
        }) + "\n",
        encoding="utf-8",
    )
    trajs = tmp_path / "trajs"
    trajs.mkdir()
    (trajs / "one.jsonl").write_text(
        json.dumps({"event": "benchmark_instance"}) + "\n"
        + json.dumps({"event": "llm_request"}) + "\n"
        + json.dumps({"event": "tool_call", "name": "read_file"}) + "\n",
        encoding="utf-8",
    )
    nested_logs = tmp_path / "raw-logs" / "run" / "model" / "one"
    nested_logs.mkdir(parents=True)
    (nested_logs / "report.json").write_text("{}", encoding="utf-8")
    (nested_logs / "test_output.txt").write_text("PASS", encoding="utf-8")

    target = build_submission_bundle(
        profile=profile,
        predictions_path=predictions,
        manifest_path=manifest,
        trajectories_dir=trajs,
        logs_dir=tmp_path / "raw-logs",
        output_dir=tmp_path / "bundle",
        metadata={"name": "NZ-Coder", "model": "deepseek-v4-flash"},
    )

    assert (target / "all_preds.jsonl").is_file()
    assert (target / "metadata.yaml").is_file()
    assert (target / "README.md").is_file()
    assert (target / "trajs" / "one.jsonl").is_file()
    assert (target / "logs" / "one" / "patch.diff").read_text() == patch
    assert (target / "logs" / "one" / "report.json").is_file()
    assert (target / "logs" / "one" / "test_output.txt").is_file()


def test_cli_defaults_to_verified_strict_pass_at_one():
    from nz_coder.swebench.cli import build_parser

    args = build_parser().parse_args(["run-agent"])
    assert args.profile == "verified"
    assert args.strict is True
    assert args.resume is True
    assert args.cleanup_worktrees is True
    assert args.trace_archive_dir is None
    assert args.trace_budget_gib == 20.0
    assert args.trace_warning_gib == 18.0
    assert args.trace_cleanup_target_gib == 15.0
    assert not hasattr(args, "empty_patch_retries")


def test_cli_trace_budget_builds_run_scoped_archive_and_validates_order(tmp_path):
    from argparse import Namespace
    from nz_coder.swebench.cli import _build_trace_budget

    predictions = tmp_path / "predictions-run.jsonl"
    args = Namespace(
        trace_archive_dir=None,
        trace_budget_gib=20.0,
        trace_warning_gib=18.0,
        trace_cleanup_target_gib=15.0,
    )

    budget = _build_trace_budget(args, predictions)

    assert budget.archive_root == tmp_path / "predictions-run-raw-traces"
    assert budget.hard_limit_bytes == 20 * 1024 ** 3
    assert budget.warning_bytes == 18 * 1024 ** 3
    assert budget.cleanup_target_bytes == 15 * 1024 ** 3

    args.trace_warning_gib = 20.0
    with pytest.raises(ValueError, match="cleanup_target_bytes"):
        _build_trace_budget(args, predictions)


def test_batch_cleanup_keeps_durable_prediction_and_public_trajectory(
    tmp_path, monkeypatch
):
    from nz_coder.swebench.artifacts import AttemptJournal
    from nz_coder.swebench.orchestrator import RetryOrchestrator

    work_root = tmp_path / "runs"
    workdir = work_root / "owner__repo-1"
    workdir.mkdir(parents=True)
    (workdir / "changed.py").write_text("changed = True\n", encoding="utf-8")
    trace = workdir / ".nz-coder-runs" / "raw-trace.jsonl"
    trace.parent.mkdir()
    trace.write_text(
        json.dumps({"event": "llm_request", "workspace": str(workdir)}) + "\n",
        encoding="utf-8",
    )
    predictions = tmp_path / "predictions.jsonl"
    trajectories = tmp_path / "trajs"
    journal = AttemptJournal(tmp_path / "attempts.jsonl")
    from nz_coder.swebench.trace_budget import TraceBudget

    trace_budget = TraceBudget(
        archive_root=tmp_path / "trace-archive",
        warning_bytes=100_000,
        hard_limit_bytes=200_000,
        cleanup_target_bytes=50_000,
    )
    orchestrator = RetryOrchestrator(adapter=None, guardrail=None)

    def fake_run_instance(*args, **kwargs):
        assert workdir.is_dir()
        return {
            "status": "completed",
            "summary": "patch collected",
            "model_patch": "diff --git a/changed.py b/changed.py\n",
            "trace": str(trace),
            "workdir": str(workdir),
            "public_input": "",
        }

    monkeypatch.setattr(orchestrator, "run_instance", fake_run_instance)
    orchestrator.run_batch(
        [{"instance_id": "owner__repo-1"}],
        work_root=work_root,
        run_id="lite300",
        config=None,
        build_prompt=None,
        agent_cls=None,
        trace_cls=None,
        clone_timeout=1,
        agent_timeout=1,
        empty_patch_retries=0,
        pred_file=None,
        model_name="nz-coder-deepseek-v4-flash",
        strict=True,
        attempt_journal=journal,
        predictions_path=predictions,
        public_trajectories_dir=trajectories,
        cleanup_worktrees=True,
        trace_budget=trace_budget,
    )

    assert not workdir.exists()
    prediction = json.loads(predictions.read_text(encoding="utf-8"))
    assert prediction["instance_id"] == "owner__repo-1"
    assert "diff --git" in prediction["model_patch"]
    public_trace = trajectories / "owner__repo-1.jsonl"
    assert public_trace.is_file()
    assert str(workdir) not in public_trace.read_text(encoding="utf-8")
    assert journal.completed_ids() == {"owner__repo-1"}
    assert (trace_budget.archive_root / "owner__repo-1" / "raw-trace.jsonl").is_file()


def test_batch_archive_failure_preserves_checkout_after_durable_result(
    tmp_path, monkeypatch
):
    from nz_coder.swebench.artifacts import AttemptJournal
    from nz_coder.swebench import orchestrator as module
    from nz_coder.swebench.trace_budget import TraceBudget

    work_root = tmp_path / "runs"
    workdir = work_root / "owner__repo-1"
    trace = workdir / ".nz-coder-runs" / "raw.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text('{"event":"llm_request"}\n', encoding="utf-8")
    predictions = tmp_path / "predictions.jsonl"
    trajectories = tmp_path / "trajs"
    journal = AttemptJournal(tmp_path / "attempts.jsonl")
    runner = module.RetryOrchestrator(adapter=None, guardrail=None)

    monkeypatch.setattr(runner, "run_instance", lambda *args, **kwargs: {
        "status": "completed",
        "summary": "patch collected",
        "model_patch": "diff --git a/a.py b/a.py\n",
        "trace": str(trace),
        "workdir": str(workdir),
        "public_input": "",
    })
    monkeypatch.setattr(
        module,
        "archive_instance_diagnostics",
        lambda **kwargs: (_ for _ in ()).throw(OSError("archive full")),
        raising=False,
    )

    with pytest.raises(OSError, match="archive full"):
        runner.run_batch(
            [{"instance_id": "owner__repo-1"}],
            work_root=work_root,
            run_id="run",
            config=None,
            build_prompt=None,
            agent_cls=None,
            trace_cls=None,
            clone_timeout=1,
            agent_timeout=1,
            empty_patch_retries=0,
            pred_file=None,
            model_name="model",
            strict=True,
            attempt_journal=journal,
            predictions_path=predictions,
            public_trajectories_dir=trajectories,
            cleanup_worktrees=True,
            trace_budget=TraceBudget(
                archive_root=tmp_path / "archive",
                warning_bytes=100,
                hard_limit_bytes=200,
                cleanup_target_bytes=50,
            ),
        )

    assert workdir.is_dir()
    assert predictions.is_file()
    assert (trajectories / "owner__repo-1.jsonl").is_file()
    assert journal.completed_ids() == {"owner__repo-1"}


def test_batch_hard_trace_limit_stops_before_next_pass_at_one_claim(
    tmp_path, monkeypatch
):
    from nz_coder.swebench.artifacts import AttemptJournal
    from nz_coder.swebench.orchestrator import RetryOrchestrator
    from nz_coder.swebench.trace_budget import TraceBudget

    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    (archive_root / "retained.bin").write_bytes(b"x" * 200)
    journal = AttemptJournal(tmp_path / "attempts.jsonl")
    runner = RetryOrchestrator(adapter=None, guardrail=None)
    monkeypatch.setattr(
        runner,
        "run_instance",
        lambda *args, **kwargs: pytest.fail("hard limit must stop before inference"),
    )

    results = runner.run_batch(
        [{"instance_id": "owner__repo-1"}],
        work_root=tmp_path / "runs",
        run_id="run",
        config=None,
        build_prompt=None,
        agent_cls=None,
        trace_cls=None,
        clone_timeout=1,
        agent_timeout=1,
        empty_patch_retries=0,
        pred_file=None,
        model_name="model",
        strict=True,
        attempt_journal=journal,
        predictions_path=tmp_path / "predictions.jsonl",
        public_trajectories_dir=tmp_path / "trajs",
        cleanup_worktrees=True,
        trace_budget=TraceBudget(
            archive_root=archive_root,
            warning_bytes=100,
            hard_limit_bytes=200,
            cleanup_target_bytes=50,
        ),
    )

    assert results == []
    assert journal.attempted_ids() == set()
    budget_report = json.loads(
        (archive_root / "trace-budget-report.json").read_text(encoding="utf-8")
    )
    assert budget_report["hard_limit_reached"] is True
    assert budget_report["used_bytes"] == 200


def test_resume_batch_limits_only_new_durable_results(tmp_path, monkeypatch):
    from nz_coder.swebench.artifacts import AttemptJournal
    from nz_coder.swebench.orchestrator import RetryOrchestrator

    journal = AttemptJournal(tmp_path / "attempts.jsonl")
    journal.claim("old")
    journal.record({
        "instance_id": "old",
        "attempt": 1,
        "status": "completed",
        "trajectory": "",
        "prediction": {
            "instance_id": "old",
            "model_name_or_path": "model",
            "model_patch": "diff --git a/a b/a\n",
        },
    })
    runner = RetryOrchestrator(adapter=None, guardrail=None)

    def fake_run_instance(instance, **kwargs):
        return {
            "instance_id": instance["instance_id"],
            "status": "completed",
            "summary": "done",
            "model_patch": f"diff --git a/{instance['instance_id']} b/{instance['instance_id']}\n",
            "trace": "",
            "workdir": "",
            "public_input": "",
        }

    monkeypatch.setattr(runner, "run_instance", fake_run_instance)
    results = runner.run_batch(
        [{"instance_id": name} for name in ("old", "new-1", "new-2", "new-3")],
        work_root=tmp_path / "runs",
        run_id="run",
        config=None,
        build_prompt=None,
        agent_cls=None,
        trace_cls=None,
        clone_timeout=1,
        agent_timeout=1,
        empty_patch_retries=0,
        pred_file=None,
        model_name="model",
        strict=True,
        attempt_journal=journal,
        predictions_path=tmp_path / "predictions.jsonl",
        cleanup_worktrees=False,
        max_new_instances=2,
    )

    assert [row["instance_id"] for row in results] == ["new-1", "new-2"]
    assert journal.completed_ids() == {"old", "new-1", "new-2"}
    assert "new-3" not in journal.attempted_ids()


def test_worktree_cleanup_refuses_paths_outside_run_root(tmp_path):
    from nz_coder.swebench.orchestrator import _cleanup_completed_worktree

    work_root = tmp_path / "runs"
    work_root.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()

    with pytest.raises(ValueError, match="direct child"):
        _cleanup_completed_worktree(unrelated, work_root)

    assert unrelated.is_dir()


def test_prepare_repo_resolves_relative_checkout_before_running_git(
    tmp_path, monkeypatch
):
    from nz_coder.swebench import orchestrator as module

    monkeypatch.chdir(tmp_path)

    def fake_run(command, *, cwd, timeout):
        working_directory = Path(cwd)
        if not working_directory.is_absolute():
            working_directory = Path.cwd() / working_directory
        if command[:2] == ["git", "clone"]:
            destination = Path(command[-1])
            if not destination.is_absolute():
                destination = working_directory / destination
            destination.mkdir(parents=True)
        return subprocess.CompletedProcess(
            command,
            0 if working_directory.is_dir() else 2,
            "",
            "",
        )

    monkeypatch.setattr(module, "_run", fake_run)
    result = module._prepare_repo(
        {"repo": "owner/repo", "base_commit": "abc", "instance_id": "one"},
        Path("runs/one"),
        timeout=1,
    )

    assert result["returncode"] == 0
    assert (tmp_path / "runs" / "one").is_dir()
    assert not (tmp_path / "runs" / "runs" / "one").exists()


def test_default_model_is_deepseek_v4_flash(monkeypatch):
    monkeypatch.delenv("MODEL_ID", raising=False)
    monkeypatch.delenv("API_BASE_URL", raising=False)
    import nz_coder.config as config

    assert config.MODEL_ID == "deepseek-v4-flash"
    assert config.API_BASE_URL == "https://api.deepseek.com"


def test_deepseek_v4_flash_uses_current_one_million_token_capability():
    from nz_coder.providers import resolve_model_capabilities

    capability = resolve_model_capabilities(
        "openai-compatible", "deepseek-v4-flash"
    )
    assert capability.context_tokens == 1_000_000
    assert capability.preserve_reasoning_content is True


def test_resume_manifest_rejects_mixed_source_or_model_runs():
    from nz_coder.evaluation.reproducibility import validate_swebench_resume

    existing = {
        "dataset": "verified",
        "split": "test",
        "instance_ids": ["one"],
        "model_id": "deepseek-v4-flash",
        "source_sha256": "source-a",
        "attempts_per_instance": 1,
        "strict_mode": True,
    }
    candidate = {**existing, "source_sha256": "source-b"}

    errors = validate_swebench_resume(existing, candidate)
    assert errors == ["source_sha256 changed: 'source-a' -> 'source-b'"]


def test_resume_manifest_rejects_changed_trace_retention_contract():
    from nz_coder.evaluation.reproducibility import validate_swebench_resume

    existing = {
        "trace_retention": {
            "warning_bytes": 18 * 1024 ** 3,
            "hard_limit_bytes": 20 * 1024 ** 3,
            "cleanup_target_bytes": 15 * 1024 ** 3,
        }
    }
    candidate = {
        "trace_retention": {
            **existing["trace_retention"],
            "hard_limit_bytes": 21 * 1024 ** 3,
        }
    }

    errors = validate_swebench_resume(existing, candidate)

    assert errors == [
        f"trace_retention changed: {existing['trace_retention']!r} -> "
        f"{candidate['trace_retention']!r}"
    ]


def test_strict_runtime_disables_memory_planning_reflection_and_learning():
    from nz_coder.runtime.execution_context import scoped_runtime_overrides
    from nz_coder.runtime.loop import AgentLoop

    with scoped_runtime_overrides(strict_local_tools=True):
        assert AgentLoop._memory_block(object(), "issue") == ""
        assert AgentLoop._should_replan(object()) is False
        assert AgentLoop._should_run_reflection(object(), "completed") is False
        assert AgentLoop._maybe_save_learnings(object(), []) is None


def test_strict_repo_snapshot_discards_future_git_history(tmp_path):
    from nz_coder.swebench.orchestrator import _reinitialize_repo_at_base

    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@localhost"], cwd=tmp_path, check=True)
    source = tmp_path / "value.txt"
    source.write_text("base", encoding="utf-8")
    subprocess.run(["git", "add", "value.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "base"], cwd=tmp_path, check=True)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    source.write_text("gold", encoding="utf-8")
    subprocess.run(["git", "commit", "--quiet", "-am", "gold fix"], cwd=tmp_path, check=True)
    gold = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True).strip()
    subprocess.run(["git", "checkout", "--quiet", base], cwd=tmp_path, check=True)

    result = _reinitialize_repo_at_base(tmp_path, timeout=30)

    assert result.returncode == 0
    assert subprocess.check_output(
        ["git", "rev-list", "--count", "HEAD"], cwd=tmp_path, text=True
    ).strip() == "1"
    assert subprocess.run(
        ["git", "cat-file", "-e", gold], cwd=tmp_path, capture_output=True
    ).returncode != 0
