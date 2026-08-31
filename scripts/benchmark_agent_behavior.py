#!/usr/bin/env python3
"""Run Agent-owned behavioral fixtures with a controlled or real coding model."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from nz_coder.foundation import config
from nz_coder.evaluation.behavioral import (
    AgentBehaviorBenchmark,
    BehaviorBenchmarkConfig,
    ControlledBehaviorDriver,
    ProductionAgentBehaviorDriver,
    run_controlled_behavior_matrix,
    run_controlled_behavior_suite,
    run_production_behavior_matrix,
    run_production_process_matrix,
    run_production_retrieval_matrix,
    run_production_reference_baseline,
    run_production_verification_matrix,
    run_production_web_search_matrix,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path(".nz-coder/behavior-benchmark"))
    parser.add_argument("--driver", choices=("controlled", "production"), default="controlled")
    parser.add_argument("--provider", default=config.MODEL_PROVIDER)
    parser.add_argument("--model", default=config.MODEL_ID)
    parser.add_argument("--reasoning", default="provider-default")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument(
        "--repo-intelligence", choices=("off", "current", "v3", "lookup"),
        default="lookup",
    )
    parser.add_argument(
        "--cases", default=None,
        help=(
            "Comma-separated case ids. Retrieval matrices default to "
            "A,B,C,D,I,I2,I3,I4,IS; other suites default to A-I."
        ),
    )
    parser.add_argument("--full-matrix", action="store_true")
    parser.add_argument("--retrieval-matrix", action="store_true")
    parser.add_argument("--verification-matrix", action="store_true")
    parser.add_argument("--process-matrix", action="store_true")
    parser.add_argument("--web-search-matrix", action="store_true")
    parser.add_argument("--reference-baseline", action="store_true")
    parser.add_argument(
        "--retrieval-strategy",
        choices=("tool-only", "guidance", "auto-context", "policy"),
        default="guidance",
    )
    parser.add_argument("--semantic-model", default="")
    parser.add_argument(
        "--semantic-only", action="store_true",
        help="Skip structural controls when they were already measured separately.",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Reuse reports whose model, budget, strategy, case, and repetition match.",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.semantic_only and not args.semantic_model:
        parser.error("--semantic-only requires --semantic-model")

    standard_cases = args.cases or "ABCDEFGHI"
    retrieval_cases = args.cases or "A,B,C,D,I,I2,I3,I4,IS"

    if args.driver == "controlled" and args.cases is None:
        result = (
            run_controlled_behavior_matrix(output)
            if args.full_matrix else run_controlled_behavior_suite(output)
        )
    elif args.driver == "production" and args.verification_matrix:
        result = run_production_verification_matrix(
            output, provider=args.provider, model=args.model,
            reasoning=args.reasoning, temperature=args.temperature,
            max_turns=max(1, args.max_turns), repetitions=max(3, args.repetitions),
            case_ids=args.cases or "V1,V2,V3,V4,V5,V6,V7,V8,C1,C2,C3,C4",
        )
    elif args.driver == "production" and args.process_matrix:
        result = run_production_process_matrix(
            output, provider=args.provider, model=args.model,
            reasoning=args.reasoning, temperature=args.temperature,
            max_turns=max(1, args.max_turns), repetitions=max(3, args.repetitions),
            case_ids=args.cases or "P1,P2,P3,P4,P5,P6",
        )
    elif args.driver == "production" and args.retrieval_matrix:
        result = run_production_retrieval_matrix(
            output, provider=args.provider, model=args.model,
            reasoning=args.reasoning, temperature=args.temperature,
            max_turns=max(1, args.max_turns), repetitions=max(3, args.repetitions),
            case_ids=retrieval_cases, semantic_model=args.semantic_model,
            include_structural=not args.semantic_only,
            resume=args.resume,
        )
    elif args.driver == "production" and args.web_search_matrix:
        result = run_production_web_search_matrix(
            output, provider=args.provider, model=args.model,
            reasoning=args.reasoning, temperature=args.temperature,
            max_turns=max(1, args.max_turns), repetitions=max(3, args.repetitions),
            case_ids=args.cases or "W1,W2,W3,W4,W5",
        )
    elif args.driver == "production" and args.reference_baseline:
        result = run_production_reference_baseline(
            output, provider=args.provider, model=args.model,
            reasoning=args.reasoning, temperature=args.temperature,
            max_turns=max(1, args.max_turns), repetitions=max(3, args.repetitions),
            case_ids=args.cases or "A,B,E,F,I",
        )
    elif args.driver == "production" and args.full_matrix:
        result = run_production_behavior_matrix(
            output, provider=args.provider, model=args.model,
            reasoning=args.reasoning, temperature=args.temperature,
            max_turns=max(1, args.max_turns),
            repetitions=max(3, args.repetitions),
            case_ids=standard_cases,
        )
    else:
        driver = (
            ControlledBehaviorDriver()
            if args.driver == "controlled"
            else ProductionAgentBehaviorDriver()
        )
        benchmark = AgentBehaviorBenchmark(output, driver)
        raw_cases = standard_cases.upper().replace(";", ",")
        if "," in raw_cases:
            case_ids = tuple(dict.fromkeys(
                item.strip() for item in raw_cases.split(",") if item.strip()
            ))
        elif raw_cases.startswith(("V", "C", "W", "P")) and len(raw_cases) > 1:
            case_ids = (raw_cases,)
        else:
            case_ids = tuple(dict.fromkeys(raw_cases))
        result = benchmark.run_matrix(
            case_ids,
            (BehaviorBenchmarkConfig(
                provider=args.provider if args.driver == "production" else "",
                model=args.model if args.driver == "production" else "controlled-behavior-model",
                reasoning=args.reasoning, temperature=args.temperature,
                max_turns=max(1, args.max_turns),
                repo_intelligence=args.repo_intelligence,
                retrieval_strategy=args.retrieval_strategy,
                semantic_model=args.semantic_model,
            ),),
        )

    summary = {
        "suite_type": result["suite_type"],
        "runs": len(result["runs"]),
        "success_rate": result["success_rate"],
        "output": str(output),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if result["success_rate"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
