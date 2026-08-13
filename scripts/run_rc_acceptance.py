#!/usr/bin/env python3
"""Run W/U/R release-candidate scenarios and persist structured evidence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from nz_coder.evaluation.windows_product_scenarios import (
    release_scenarios,
    run_acceptance_suite,
    tui_scenarios,
    windows_scenarios,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=("windows", "tui", "release"), required=True)
    parser.add_argument("--platform", choices=("windows", "linux"), default="")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    if args.suite == "windows":
        scenarios = windows_scenarios()
        suite_name = "Windows W1-W15"
    elif args.suite == "tui":
        scenarios = tui_scenarios()
        suite_name = "TUI U1-U14"
    else:
        selected_platform = args.platform or (
            "windows" if sys.platform == "win32" else "linux"
        )
        scenarios = release_scenarios(selected_platform)
        suite_name = f"Release R1-R12 ({selected_platform})"

    report = run_acceptance_suite(
        suite_name,
        scenarios,
        timeout_seconds=args.timeout,
    )
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
