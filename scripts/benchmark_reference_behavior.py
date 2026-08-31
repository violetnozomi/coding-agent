#!/usr/bin/env python3
"""Run common behavioral fixtures against unmodified reference agents."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from nz_coder.foundation import config
from nz_coder.evaluation.reference_adapter import (
    InfCodeXReferenceAdapter,
    OpenCodeReferenceAdapter,
    run_reference_behavior_matrix,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", choices=("infcodex", "opencode", "all"))
    parser.add_argument("--output", type=Path, default=Path(".nz-coder/reference-benchmark"))
    parser.add_argument("--infcodex-root", type=Path, default=Path("references/InfCodeX"))
    parser.add_argument("--opencode-root", type=Path, default=Path("infcode-dev/infcode-dev"))
    parser.add_argument("--provider", default=config.MODEL_PROVIDER)
    parser.add_argument("--model", default=config.MODEL_ID)
    parser.add_argument("--reasoning", default="provider-default")
    parser.add_argument("--max-turns", type=int, default=24)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--cases", default="A,B,E,F,I")
    args = parser.parse_args()

    adapters = []
    if args.reference in {"infcodex", "all"}:
        adapters.append(InfCodeXReferenceAdapter(args.infcodex_root))
    if args.reference in {"opencode", "all"}:
        adapters.append(OpenCodeReferenceAdapter(args.opencode_root))
    case_ids = tuple(dict.fromkeys(
        item.strip().upper() for item in args.cases.split(",") if item.strip()
    ))
    summaries = []
    for adapter in adapters:
        result = run_reference_behavior_matrix(
            args.output / adapter.name.casefold().replace("/", "-"),
            adapter=adapter, provider=args.provider, model=args.model,
            reasoning=args.reasoning, max_turns=max(1, args.max_turns),
            repetitions=max(3, args.repetitions), case_ids=case_ids,
        )
        summaries.append({
            "reference": adapter.name,
            "evidence_kind": result["evidence_kind"],
            "runs": len(result["runs"]),
            "success_rate": result["success_rate"],
            "capability": result["capability"],
        })
    print(json.dumps(summaries, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
