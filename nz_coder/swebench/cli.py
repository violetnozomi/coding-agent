"""CLI entry point for nz_coder.swebench.

Provides the same subcommands as the original swebench_lite.py:
  check        — environment readiness check
  run-eval     — invoke official harness on a predictions file
  run-agent    — generate first-pass predictions
  retry-agent  — second-pass with official failure feedback

Usage (new):
    python -m nz_coder.swebench <subcommand> [options]

Usage (legacy compat):
    python -m nz_coder.swebench_lite <subcommand> [options]
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from nz_coder.swebench.adapter import SWEBenchAdapter
from nz_coder.swebench.artifacts import AttemptJournal
from nz_coder.swebench.guardrail import PatchGuardrail
from nz_coder.swebench.orchestrator import (
    DEFAULT_BENCH_DIR,
    RetryOrchestrator,
    default_swe_work_root,
)
from nz_coder.swebench.profiles import DEFAULT_PROFILE, PROFILES, get_profile
from nz_coder.swebench.submission import (
    build_submission_bundle,
    record_official_evaluation_provenance,
)
from nz_coder.swebench.trace_budget import GIB, TraceBudget


def _build_trace_budget(
    args: argparse.Namespace,
    predictions_path: Path,
) -> TraceBudget:
    """Build the exact-byte run-scoped trace retention contract."""
    archive_root = Path(
        args.trace_archive_dir
        or predictions_path.parent / f"{predictions_path.stem}-raw-traces"
    )
    return TraceBudget(
        archive_root=archive_root,
        warning_bytes=int(float(args.trace_warning_gib) * GIB),
        hard_limit_bytes=int(float(args.trace_budget_gib) * GIB),
        cleanup_target_bytes=int(float(args.trace_cleanup_target_gib) * GIB),
    )


def run_agent(args: argparse.Namespace) -> int:
    """Generate SWE-bench predictions by running NZ-Coder on dataset instances."""
    try:
        from datasets import load_dataset
    except ImportError:
        print("Error: datasets is not installed. Install it with `pip install datasets`.")
        return 2

    from nz_coder.foundation import config
    from nz_coder.runtime.conversation.prompt import build
    from nz_coder.runtime.execution.composition import build_product_environment
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
    from nz_coder.state.trace import TraceRecorder

    profile = get_profile(args.profile)
    adapter = SWEBenchAdapter(profile.name)
    if not adapter.check_agent_dependencies():
        return 2
    if profile.leaderboard and not args.strict:
        print("Error: the Verified profile requires strict pass@1 mode.")
        return 2
    split = args.split or profile.split
    dataset = load_dataset(profile.dataset, split=split)
    instances = _select_instances(list(dataset), args.instance_ids, args.max_instances)
    if not instances:
        print("Error: no SWE-bench instances selected.")
        return 2

    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    work_root = (
        Path(args.work_root)
        if args.work_root
        else default_swe_work_root(run_id)
    )
    predictions_path = Path(args.output or DEFAULT_BENCH_DIR / f"predictions-{run_id}.jsonl")
    report_path = predictions_path.with_suffix(".report.json")
    manifest_path = predictions_path.with_suffix(".manifest.json")
    journal_path = predictions_path.with_suffix(".attempts.jsonl")
    trajectories_dir = predictions_path.parent / f"{predictions_path.stem}-trajs"
    try:
        trace_budget = _build_trace_budget(args, predictions_path)
    except (TypeError, ValueError) as exc:
        print(f"Error: invalid trace budget: {exc}")
        return 2
    work_root.mkdir(parents=True, exist_ok=True)
    predictions_path.parent.mkdir(parents=True, exist_ok=True)

    partial_selection = len(instances) != profile.expected_instances
    print(f"Selected {len(instances)} SWE-bench {profile.name} instance(s).")
    print(f"Policy: {'strict pass@1' if args.strict else 'development diagnostic'}")
    print(f"Work root: {work_root}")
    print(f"Predictions: {predictions_path}")
    print(
        f"Raw trace archive: {trace_budget.archive_root} "
        f"(warning={trace_budget.warning_bytes} bytes, "
        f"hard={trace_budget.hard_limit_bytes} bytes)"
    )

    from nz_coder.evaluation.reproducibility import (
        build_swebench_manifest,
        validate_swebench_resume,
        write_reproducibility_manifest,
    )

    manifest = build_swebench_manifest(
        run_id=run_id,
        dataset=profile.dataset,
        split=split,
        instance_ids=[str(item["instance_id"]) for item in instances],
        model_name=args.model_name,
        provider=config.MODEL_PROVIDER,
        model_id=config.MODEL_ID,
        max_agent_turns=config.MAX_AGENT_TURNS,
        nominal_agent_turns=config.SWE_NOMINAL_AGENT_TURNS,
        agent_timeout_seconds=args.agent_timeout,
        benchmark_profile=profile.name,
        expected_instances=profile.expected_instances,
        strict=args.strict,
        partial_selection=partial_selection,
        expected_instance_ids_sha256=profile.instance_ids_sha256,
    )
    manifest["trace_retention"] = {
        "archive_root": str(trace_budget.archive_root),
        "warning_bytes": trace_budget.warning_bytes,
        "hard_limit_bytes": trace_budget.hard_limit_bytes,
        "cleanup_target_bytes": trace_budget.cleanup_target_bytes,
        "cleanup_worktrees": bool(args.cleanup_worktrees),
        "analysis_before_raw_trace_cleanup": True,
    }
    if manifest_path.exists():
        if not args.resume:
            print(f"Error: manifest already exists: {manifest_path}")
            return 2
        try:
            existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Error: cannot resume from manifest: {exc}")
            return 2
        resume_errors = validate_swebench_resume(existing_manifest, manifest)
        if resume_errors:
            print("Error: refusing to mix incompatible attempts in one pass@1 run:")
            for error in resume_errors:
                print(f"- {error}")
            return 2
        manifest = existing_manifest
    else:
        write_reproducibility_manifest(manifest_path, manifest)
    print(f"Manifest: {manifest_path}")

    orchestrator = RetryOrchestrator(adapter, PatchGuardrail())
    journal = AttemptJournal(journal_path)
    if not args.resume and (journal_path.exists() or predictions_path.exists()):
        print("Error: output already exists; choose a new --output/--run-id or enable --resume.")
        return 2
    if args.resume:
        journal.write_predictions(predictions_path)

    with scoped_runtime_overrides(
        max_agent_turns=config.MAX_AGENT_TURNS,
        nominal_agent_turns=config.SWE_NOMINAL_AGENT_TURNS,
        strict_local_tools=args.strict,
    ):
        results = orchestrator.run_batch(
            instances,
            work_root=work_root,
            run_id=run_id,
            config=config,
            build_prompt=build,
            agent_cls=build_product_environment,
            trace_cls=TraceRecorder,
            clone_timeout=args.clone_timeout,
            agent_timeout=args.agent_timeout,
            empty_patch_retries=0,
            pred_file=None,
            model_name=args.model_name,
            strict=args.strict,
            attempt_journal=journal,
            predictions_path=predictions_path,
            public_trajectories_dir=trajectories_dir,
            cleanup_worktrees=args.cleanup_worktrees,
            trace_budget=trace_budget,
            max_new_instances=args.max_new_instances,
        )

    report_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Report: {report_path}")
    completed_selected = len(
        journal.completed_ids()
        & {str(instance["instance_id"]) for instance in instances}
    )
    if completed_selected < len(instances):
        print(
            "Run paused before all selected instances completed: "
            f"{completed_selected}/{len(instances)} durable results."
        )
        return 3
    failed = sum(1 for row in results if row["status"] != "completed")
    return 1 if failed else 0


def retry_agent(args: argparse.Namespace) -> int:
    """Generate a second-pass prediction using official SWE-bench failure logs."""
    try:
        from datasets import load_dataset
    except ImportError:
        print("Error: datasets is not installed. Install it with `pip install datasets`.")
        return 2

    from nz_coder.foundation import config
    from nz_coder.runtime.conversation.prompt import build
    from nz_coder.runtime.execution.composition import build_product_environment
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
    from nz_coder.state.trace import TraceRecorder

    profile = get_profile(args.profile)
    adapter = SWEBenchAdapter(profile.name)
    if not adapter.check_agent_dependencies():
        return 2
    previous_predictions = adapter.load_predictions(Path(args.previous_predictions))
    dataset = load_dataset(profile.dataset, split=args.split or profile.split)
    instances = list(dataset)
    if args.instance_ids:
        wanted = set(args.instance_ids)
        instances = [row for row in instances if row["instance_id"] in wanted]
    else:
        instances = [row for row in instances if row["instance_id"] in previous_predictions]
    if args.max_instances:
        instances = instances[: args.max_instances]
    if not instances:
        print("Error: no retry instances selected.")
        return 2

    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S-retry")
    work_root = (
        Path(args.work_root)
        if args.work_root
        else default_swe_work_root(run_id)
    )
    predictions_path = Path(args.output or DEFAULT_BENCH_DIR / f"predictions-{run_id}.jsonl")
    report_path = predictions_path.with_suffix(".report.json")
    manifest_path = predictions_path.with_suffix(".manifest.json")
    work_root.mkdir(parents=True, exist_ok=True)
    predictions_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Selected {len(instances)} retry instance(s).")
    print(f"Official eval logs: {args.eval_log_dir}")
    print(f"Predictions: {predictions_path}")
    manifest_path.write_text(json.dumps({
        "schema_version": 2,
        "benchmark_profile": profile.name,
        "dataset": profile.dataset,
        "run_id": run_id,
        "leaderboard_eligible": False,
        "attempts_per_instance": 2,
        "result_policy": "diagnostic retry using official evaluation feedback",
        "official_test_knowledge_used": True,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Diagnostic manifest: {manifest_path}")

    orchestrator = RetryOrchestrator(adapter, PatchGuardrail())

    with (
        scoped_runtime_overrides(
            max_agent_turns=config.MAX_AGENT_TURNS,
            nominal_agent_turns=config.SWE_NOMINAL_AGENT_TURNS,
        ),
        predictions_path.open("w", encoding="utf-8") as pred_file,
    ):
        results = orchestrator.retry_batch(
            instances,
            previous_predictions,
            eval_log_dir=Path(args.eval_log_dir),
            work_root=work_root,
            run_id=run_id,
            config=config,
            build_prompt=build,
            agent_cls=build_product_environment,
            trace_cls=TraceRecorder,
            clone_timeout=args.clone_timeout,
            agent_timeout=args.agent_timeout,
            empty_patch_retries=args.empty_patch_retries,
            max_feedback_chars=args.max_feedback_chars,
            pred_file=pred_file,
            model_name=args.model_name,
        )

    report_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Report: {report_path}")
    failed = sum(1 for row in results if row["status"] != "completed")
    return 1 if failed else 0


def _select_instances(
    instances: list[dict],
    instance_ids: list[str] | None,
    max_instances: int | None,
) -> list[dict]:
    if instance_ids:
        wanted = set(instance_ids)
        instances = [row for row in instances if row["instance_id"] in wanted]
    if max_instances:
        instances = instances[:max_instances]
    return instances


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NZ-Coder SWE-bench benchmark helper")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("check", help="Check local SWE-bench prerequisites")

    run = subparsers.add_parser("run-eval", help="Run official SWE-bench evaluation")
    run.add_argument("--profile", choices=sorted(PROFILES), default=DEFAULT_PROFILE)
    run.add_argument("--predictions-path", required=True, help="JSONL predictions file.")
    run.add_argument("--max-workers", type=int, default=1, help="Parallel evaluation workers.")
    run.add_argument("--run-id", default="nz_coder_swebench_verified", help="SWE-bench run id.")
    run.add_argument("--timeout", type=int, default=1800, help="Per-instance timeout in seconds.")
    run.add_argument("--instance-ids", nargs="*", help="Optional specific SWE-bench instance ids.")
    run.add_argument("--clean", action="store_true", help="Ask harness to clean resources.")
    run.add_argument("--prepull-timeout", type=int, default=0)
    run.add_argument("--skip-prepull-failures", action="store_true")
    run.add_argument("--image-namespace", default="swebench")
    run.add_argument("--image-arch", default="")
    run.add_argument("--instance-image-tag", default="latest")
    run.add_argument("--package", action=argparse.BooleanOptionalAction, default=True)
    run.add_argument("--submission-output")
    run.add_argument("--eval-log-dir")
    run.add_argument("--organization", default="independent")

    agent = subparsers.add_parser("run-agent", help="Run strict pass@1 NZ-Coder inference")
    agent.add_argument("--profile", choices=sorted(PROFILES), default=DEFAULT_PROFILE)
    agent.add_argument("--split")
    agent.add_argument("--max-instances", type=int)
    agent.add_argument(
        "--max-new-instances",
        type=int,
        help=(
            "Stop this invocation after N newly durable results. Existing pass@1 "
            "claims are skipped and do not count toward N."
        ),
    )
    agent.add_argument("--instance-ids", nargs="*")
    agent.add_argument("--output")
    agent.add_argument("--run-id")
    agent.add_argument("--work-root")
    agent.add_argument("--model-name", default="nz-coder-deepseek-v4-flash")
    agent.add_argument("--clone-timeout", type=int, default=600)
    agent.add_argument("--agent-timeout", type=int, default=900)
    agent.add_argument("--strict", action=argparse.BooleanOptionalAction, default=True)
    agent.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    agent.add_argument(
        "--cleanup-worktrees",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Delete each checkout after its prediction and public trajectory are durable.",
    )
    agent.add_argument(
        "--trace-archive-dir",
        help="Run-scoped raw diagnostic bundle directory.",
    )
    agent.add_argument("--trace-budget-gib", type=float, default=20.0)
    agent.add_argument("--trace-warning-gib", type=float, default=18.0)
    agent.add_argument("--trace-cleanup-target-gib", type=float, default=15.0)

    retry = subparsers.add_parser("retry-agent", help="Rerun NZ-Coder using previous predictions plus official failure logs")
    retry.add_argument("--profile", choices=sorted(PROFILES), default="lite")
    retry.add_argument("--split")
    retry.add_argument("--max-instances", type=int)
    retry.add_argument("--instance-ids", nargs="*")
    retry.add_argument("--previous-predictions", required=True)
    retry.add_argument("--eval-log-dir", required=True)
    retry.add_argument("--output")
    retry.add_argument("--run-id")
    retry.add_argument("--work-root")
    retry.add_argument("--model-name", default="nz-coder-retry")
    retry.add_argument("--clone-timeout", type=int, default=600)
    retry.add_argument("--agent-timeout", type=int, default=900)
    retry.add_argument("--empty-patch-retries", type=int, default=1)
    retry.add_argument("--max-feedback-chars", type=int, default=4000)

    package = subparsers.add_parser("package", help="Build a validated official submission bundle")
    package.add_argument("--profile", choices=sorted(PROFILES), default=DEFAULT_PROFILE)
    package.add_argument("--predictions-path", required=True)
    package.add_argument("--manifest-path", required=True)
    package.add_argument("--attempt-journal")
    package.add_argument("--trajectories-dir", required=True)
    package.add_argument("--logs-dir", required=True)
    package.add_argument("--output-dir", required=True)
    package.add_argument("--organization", default="independent")
    package.add_argument("--model", default="deepseek-v4-flash")
    package.add_argument("--evaluation-run-id")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    adapter = SWEBenchAdapter()

    if args.command == "check":
        return adapter.check_environment()
    if args.command == "run-eval":
        result = adapter.run_harness(Path(args.predictions_path), args)
        predictions_path = Path(args.predictions_path)
        manifest_path = predictions_path.with_suffix(".manifest.json")
        if result == 0 and manifest_path.is_file():
            try:
                record_official_evaluation_provenance(
                    manifest_path,
                    predictions_path,
                    args.run_id,
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                print(f"Official evaluation passed, but provenance recording failed: {exc}")
                return 2
        if result or not args.package or args.profile != "verified":
            return result
        trajectories_dir = predictions_path.parent / f"{predictions_path.stem}-trajs"
        logs_dir = Path(
            args.eval_log_dir or Path("logs") / "run_evaluation" / args.run_id
        )
        output_dir = Path(
            args.submission_output
            or predictions_path.parent / f"submission-{args.run_id}"
        )
        try:
            target = build_submission_bundle(
                profile=get_profile(args.profile),
                predictions_path=predictions_path,
                manifest_path=manifest_path,
                trajectories_dir=trajectories_dir,
                logs_dir=logs_dir,
                output_dir=output_dir,
                metadata={
                    "name": "NZ-Coder",
                    "organization": args.organization,
                    "model": "deepseek-v4-flash",
                    "os_model": False,
                    "os_system": True,
                },
            )
        except (OSError, ValueError) as exc:
            print(f"Evaluation completed, but submission packaging failed: {exc}")
            return 2
        print(f"Submission bundle: {target}")
        return 0
    if args.command == "run-agent":
        return run_agent(args)
    if args.command == "retry-agent":
        return retry_agent(args)
    if args.command == "package":
        try:
            if args.evaluation_run_id:
                record_official_evaluation_provenance(
                    Path(args.manifest_path),
                    Path(args.predictions_path),
                    args.evaluation_run_id,
                )
            target = build_submission_bundle(
                profile=get_profile(args.profile),
                predictions_path=Path(args.predictions_path),
                manifest_path=Path(args.manifest_path),
                trajectories_dir=Path(args.trajectories_dir),
                logs_dir=Path(args.logs_dir),
                output_dir=Path(args.output_dir),
                metadata={
                    "name": "NZ-Coder",
                    "organization": args.organization,
                    "model": args.model,
                    "os_model": False,
                    "os_system": True,
                },
                attempt_journal_path=(
                    Path(args.attempt_journal) if args.attempt_journal else None
                ),
            )
        except (OSError, ValueError) as exc:
            print(f"Error: {exc}")
            return 2
        print(f"Submission bundle: {target}")
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
