#!/usr/bin/env python3
"""Run and optionally persist the final T1-T20 product acceptance suite."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from nz_coder.evaluation.product_scenarios import run_product_scenario_suite


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    report = run_product_scenario_suite(timeout_seconds=args.timeout)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
