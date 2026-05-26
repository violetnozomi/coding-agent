"""SWE-bench Lite CLI — compatibility shim.

This module has been refactored into the nz_coder.swebench sub-package:
    nz_coder/swebench/models.py       — FailureFeedback, PatchRiskReport, RetryPlan
    nz_coder/swebench/guardrail.py    — PatchGuardrail (static patch risk analysis)
    nz_coder/swebench/adapter.py      — SWEBenchAdapter (official harness integration)
    nz_coder/swebench/orchestrator.py — RetryOrchestrator (task coordination)
    nz_coder/swebench/cli.py          — build_parser(), main()

This file is kept as a backwards-compatible entry point so that existing
invocations of `python -m nz_coder.swebench_lite` continue to work unchanged.
New code should import from nz_coder.swebench directly.
"""
from nz_coder.swebench.cli import build_parser, main  # noqa: F401 — re-exported for compat

__all__ = ["build_parser", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
