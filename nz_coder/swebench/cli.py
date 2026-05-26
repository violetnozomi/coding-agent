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
import os
import sys
import time
from pathlib import Path

from nz_coder.swebench.adapter import SWEBenchAdapter, DATASET_NAME
from nz_coder.swebench.guardrail import PatchGuardrail
from nz_coder.swebench.orchestrator import RetryOrchestrator, DEFAULT_BENCH_DIR


def run_agent(args: argparse.Namespace) -> int:
    """Generate SWE-bench predictions by running NZ-Coder on dataset instances."""
    try:
        from datasets import load_dataset
    except ImportError:
        print("Error: datasets is not installed. Install it with `pip install datasets`.")
        return 2

    adapter = SWEBenchAdapter()
    if not adapter.check_agent_dependencies():
        return 2

    from nz_coder import config
    from nz_coder.loop import AgentLoop
    from nz_coder.prompt import build
    from nz_coder.trace import TraceRecorder

    if not os.environ.get("MAX_AGENT_TURNS"):
        config.MAX_AGENT_TURNS = 80

    dataset = load_dataset(DATASET_NAME, split=args.split)
    instances = _select_instances(list(dataset), args.instance_ids, args.max_instances)
    if not instances:
        print("Error: no SWE-bench instances selected.")
        return 2

    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S")
    work_root = Path(args.work_root or DEFAULT_BENCH_DIR / "runs" / run_id)
    predictions_path = Path(args.output or DEFAULT_BENCH_DIR / f"predictions-{run_id}.jsonl")
    report_path = predictions_path.with_suffix(".report.json")
    work_root.mkdir(parents=True, exist_ok=True)
    predictions_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Selected {len(instances)} SWE-bench Lite instance(s).")
    print(f"Work root: {work_root}")
    print(f"Predictions: {predictions_path}")

    orchestrator = RetryOrchestrator(adapter, PatchGuardrail())
    original_workdir = config.WORKDIR

    with predictions_path.open("w", encoding="utf-8") as pred_file:
        results = orchestrator.run_batch(
            instances,
            work_root=work_root,
            run_id=run_id,
            config=config,
            build_prompt=build,
            agent_cls=AgentLoop,
            trace_cls=TraceRecorder,
            clone_timeout=args.clone_timeout,
            agent_timeout=args.agent_timeout,
            empty_patch_retries=args.empty_patch_retries,
            pred_file=pred_file,
            model_name=args.model_name,
        )

    config.WORKDIR = original_workdir
    report_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Report: {report_path}")
    failed = sum(1 for row in results if row["status"] != "completed")
    return 1 if failed else 0


def retry_agent(args: argparse.Namespace) -> int:
    """Generate a second-pass prediction using official SWE-bench failure logs."""
    try:
        from datasets import load_dataset
    except ImportError:
        print("Error: datasets is not installed. Install it with `pip install datasets`.")
        return 2

    adapter = SWEBenchAdapter()
    if not adapter.check_agent_dependencies():
        return 2

    from nz_coder import config
    from nz_coder.loop import AgentLoop
    from nz_coder.prompt import build
    from nz_coder.trace import TraceRecorder

    if not os.environ.get("MAX_AGENT_TURNS"):
        config.MAX_AGENT_TURNS = 80

    previous_predictions = adapter.load_predictions(Path(args.previous_predictions))
    dataset = load_dataset(DATASET_NAME, split=args.split)
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
    work_root = Path(args.work_root or DEFAULT_BENCH_DIR / "runs" / run_id)
    predictions_path = Path(args.output or DEFAULT_BENCH_DIR / f"predictions-{run_id}.jsonl")
    report_path = predictions_path.with_suffix(".report.json")
    work_root.mkdir(parents=True, exist_ok=True)
    predictions_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Selected {len(instances)} retry instance(s).")
    print(f"Official eval logs: {args.eval_log_dir}")
    print(f"Predictions: {predictions_path}")

    orchestrator = RetryOrchestrator(adapter, PatchGuardrail())
    original_workdir = config.WORKDIR

    with predictions_path.open("w", encoding="utf-8") as pred_file:
        results = orchestrator.retry_batch(
            instances,
            previous_predictions,
            eval_log_dir=Path(args.eval_log_dir),
            work_root=work_root,
            run_id=run_id,
            config=config,
            build_prompt=build,
            agent_cls=AgentLoop,
            trace_cls=TraceRecorder,
            clone_timeout=args.clone_timeout,
            agent_timeout=args.agent_timeout,
            empty_patch_retries=args.empty_patch_retries,
            max_feedback_chars=args.max_feedback_chars,
            pred_file=pred_file,
            model_name=args.model_name,
        )

    config.WORKDIR = original_workdir
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
    parser = argparse.ArgumentParser(description="NZ-Coder SWE-bench Lite helper")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("check", help="Check local SWE-bench Lite prerequisites")

    run = subparsers.add_parser("run-eval", help="Run official SWE-bench Lite evaluation")
    run.add_argument("--predictions-path", required=True, help="JSONL predictions file.")
    run.add_argument("--max-workers", type=int, default=1, help="Parallel evaluation workers.")
    run.add_argument("--run-id", default="nz_coder_swebench_lite", help="SWE-bench run id.")
    run.add_argument("--timeout", type=int, default=1800, help="Per-instance timeout in seconds.")
    run.add_argument("--instance-ids", nargs="*", help="Optional specific SWE-bench instance ids.")
    run.add_argument("--clean", action="store_true", help="Ask harness to clean resources.")
    run.add_argument("--prepull-timeout", type=int, default=0)
    run.add_argument("--skip-prepull-failures", action="store_true")
    run.add_argument("--image-namespace", default="swebench")
    run.add_argument("--image-arch", default="")
    run.add_argument("--instance-image-tag", default="latest")

    agent = subparsers.add_parser("run-agent", help="Run NZ-Coder on SWE-bench Lite instances")
    agent.add_argument("--split", default="test")
    agent.add_argument("--max-instances", type=int)
    agent.add_argument("--instance-ids", nargs="*")
    agent.add_argument("--output")
    agent.add_argument("--run-id")
    agent.add_argument("--work-root")
    agent.add_argument("--model-name", default="nz-coder")
    agent.add_argument("--clone-timeout", type=int, default=600)
    agent.add_argument("--agent-timeout", type=int, default=900)
    agent.add_argument("--empty-patch-retries", type=int, default=0)

    retry = subparsers.add_parser("retry-agent", help="Rerun NZ-Coder using previous predictions plus official failure logs")
    retry.add_argument("--split", default="test")
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    adapter = SWEBenchAdapter()

    if args.command == "check":
        return adapter.check_environment()
    if args.command == "run-eval":
        return adapter.run_harness(Path(args.predictions_path), args)
    if args.command == "run-agent":
        return run_agent(args)
    if args.command == "retry-agent":
        return retry_agent(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
