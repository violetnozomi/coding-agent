"""Contracts for bounded SWE-bench diagnostic trace retention."""
from __future__ import annotations

import json

import pytest


def test_archive_publishes_complete_diagnostic_bundle_atomically(tmp_path):
    from nz_coder.swebench.trace_budget import (
        TraceBudget,
        archive_instance_diagnostics,
    )

    run_root = tmp_path / "runs"
    workdir = run_root / "owner__repo-1"
    raw_dir = workdir / ".nz-coder-runs"
    session_dir = workdir / ".nz-coder" / "sessions"
    raw_dir.mkdir(parents=True)
    session_dir.mkdir(parents=True)
    trace = raw_dir / "trace.jsonl"
    trace.write_text('{"event":"llm_request"}\n', encoding="utf-8")
    public_input = raw_dir / "public-inference-input.json"
    public_input.write_text('{"event":"benchmark_instance"}\n', encoding="utf-8")
    (session_dir / "session.json").write_text('{"status":"completed"}\n', encoding="utf-8")
    archive_root = tmp_path / "trace-archive"
    budget = TraceBudget(
        archive_root=archive_root,
        warning_bytes=1000,
        hard_limit_bytes=2000,
        cleanup_target_bytes=500,
    )

    result = archive_instance_diagnostics(
        instance_id="owner__repo-1",
        workdir=workdir,
        run_root=run_root,
        trace_path=trace,
        public_input_path=public_input,
        metadata={"status": "completed", "patch_chars": 42},
        budget=budget,
    )

    bundle = archive_root / "owner__repo-1"
    assert result.bundle_path == bundle
    assert (bundle / "raw-trace.jsonl").read_text(encoding="utf-8") == trace.read_text(encoding="utf-8")
    assert (bundle / "public-inference-input.json").is_file()
    assert (bundle / "sessions" / "session.json").is_file()
    assert json.loads((bundle / "metadata.json").read_text(encoding="utf-8")) == {
        "instance_id": "owner__repo-1",
        "patch_chars": 42,
        "status": "completed",
    }
    assert not any(path.name.startswith(".owner__repo-1.tmp-") for path in archive_root.iterdir())
    assert result.used_bytes > 0


def test_archive_refuses_workdir_outside_direct_run_child(tmp_path):
    from nz_coder.swebench.trace_budget import (
        TraceBudget,
        archive_instance_diagnostics,
    )

    run_root = tmp_path / "runs"
    run_root.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    trace = unrelated / "trace.jsonl"
    trace.write_text("{}\n", encoding="utf-8")
    budget = TraceBudget(
        archive_root=tmp_path / "archive",
        warning_bytes=100,
        hard_limit_bytes=200,
        cleanup_target_bytes=50,
    )

    with pytest.raises(ValueError, match="direct child"):
        archive_instance_diagnostics(
            instance_id="owner__repo-1",
            workdir=unrelated,
            run_root=run_root,
            trace_path=trace,
            public_input_path=None,
            metadata={},
            budget=budget,
        )

    assert unrelated.is_dir()


@pytest.mark.parametrize(
    ("used_bytes", "warning", "hard_limit_reached"),
    [
        (99, False, False),
        (100, True, False),
        (199, True, False),
        (200, True, True),
    ],
)
def test_budget_decisions_use_exact_byte_thresholds(
    tmp_path, used_bytes, warning, hard_limit_reached
):
    from nz_coder.swebench.trace_budget import TraceBudget, evaluate_trace_budget

    budget = TraceBudget(
        archive_root=tmp_path,
        warning_bytes=100,
        hard_limit_bytes=200,
        cleanup_target_bytes=50,
    )

    decision = evaluate_trace_budget(budget, used_bytes=used_bytes)

    assert decision.used_bytes == used_bytes
    assert decision.warning is warning
    assert decision.hard_limit_reached is hard_limit_reached


@pytest.mark.parametrize(
    "values",
    [
        (0, 200, 50),
        (100, 100, 50),
        (100, 200, 100),
        (200, 100, 50),
    ],
)
def test_trace_budget_requires_cleanup_below_warning_below_hard(tmp_path, values):
    from nz_coder.swebench.trace_budget import TraceBudget

    warning, hard, target = values
    with pytest.raises(ValueError, match="cleanup_target_bytes"):
        TraceBudget(
            archive_root=tmp_path,
            warning_bytes=warning,
            hard_limit_bytes=hard,
            cleanup_target_bytes=target,
        )
