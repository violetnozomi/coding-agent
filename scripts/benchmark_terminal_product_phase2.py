#!/usr/bin/env python3
"""Run the local Terminal Product Parity Phase 2 benchmark."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from nz_coder.evaluation.terminal_product import run_phase2_terminal_product_benchmark


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    report = run_phase2_terminal_product_benchmark(
        repetitions=args.repetitions,
        timeout_seconds=args.timeout,
    )
    payload = json.dumps(report, ensure_ascii=True, indent=2) + "\n"
    if args.output:
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
