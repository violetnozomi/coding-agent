# SWE-bench Trace Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Archive per-instance SWE-bench diagnostics under a 20 GiB hard budget, pause safely at the limit, and retain a documented analysis-before-cleanup workflow.

**Architecture:** A focused `trace_budget.py` module owns archive publication and byte-budget decisions. The orchestrator calls it only after durable prediction artifacts and before checkout cleanup; the CLI exposes explicit budget thresholds and stops before claiming another instance when the hard limit is reached.

**Tech Stack:** Python 3.9+, standard library (`pathlib`, `json`, `shutil`, `os`), pytest, existing SWE-bench CLI/orchestrator.

## Global Constraints

- No new external dependency or Agent framework.
- Default archive limit is exactly 20 GiB; warning is 18 GiB; post-analysis target is 15 GiB.
- Never delete predictions, manifests, attempt journals, public trajectories, analysis reports, or official harness logs.
- Never delete or archive a path outside a direct child of the current run root.
- Persist prediction, journal, and public trajectory before archiving or deleting a checkout.
- A hard-limit stop occurs before the next pass@1 claim.

---

### Task 1: Diagnostic archive and budget policy

**Files:**
- Create: `nz_coder/swebench/trace_budget.py`
- Test: `tests/test_swebench_trace_budget.py`

**Interfaces:**
- Produces: `TraceBudget`, `TraceArchiveResult`, `archive_instance_diagnostics(...)`, `measure_trace_archive(...)`, and `evaluate_trace_budget(...)`.

- [x] Write failing tests proving the bundle contains raw trace/session/input/metadata, is published atomically, rejects paths outside the run root, and returns warning/hard-limit decisions from literal byte fixtures.
- [x] Run `pytest -q tests/test_swebench_trace_budget.py` and confirm failures are caused by the missing module/API.
- [x] Implement immutable dataclasses and standard-library archive copying with a temporary directory followed by `os.replace`.
- [x] Run `pytest -q tests/test_swebench_trace_budget.py` and confirm all tests pass.

### Task 2: Orchestrator ordering and safe pause

**Files:**
- Modify: `nz_coder/swebench/orchestrator.py`
- Modify: `tests/test_swebench_strict.py`

**Interfaces:**
- Consumes: Task 1 archive and budget APIs.
- Produces: `run_batch(..., trace_budget: TraceBudget | None)` with pre-claim hard-limit checks and archive-before-cleanup ordering.

- [x] Write failing tests proving a completed checkout is archived only after durable prediction/trajectory output, archive failure preserves the checkout, and a hard limit prevents the next claim.
- [x] Run the focused tests and verify expected failures.
- [x] Add the minimal orchestrator integration and structured `trace_budget_reached` batch outcome without changing prediction schema.
- [x] Run focused tests and verify they pass.

### Task 3: CLI contract and run documentation

**Files:**
- Modify: `nz_coder/swebench/cli.py`
- Modify: `tests/test_swebench_strict.py`
- Modify: `docs/swebench-progress.md`

**Interfaces:**
- Produces CLI options `--trace-archive-dir`, `--trace-budget-gib`, `--trace-warning-gib`, and `--trace-cleanup-target-gib`, defaulting to the approved 20/18/15 values.

- [x] Write failing parser tests for exact defaults and validation of `0 < target < warning < hard`.
- [x] Run the parser tests and verify expected failures.
- [x] Wire validated arguments into `TraceBudget`, print archive usage, and document the aborted r2 run plus the new run identity.
- [x] Run parser and SWE-bench focused tests and verify they pass.

### Task 4: Verification and live restart

**Files:**
- Modify: `docs/swebench-progress.md`

**Interfaces:**
- Consumes all prior tasks.
- Produces a fresh Lite 300 strict run with run-scoped diagnostic bundles.

- [x] Run `pytest -q tests/test_swebench_trace_budget.py tests/test_swebench_strict.py tests/test_loop_fake.py tests/test_provider_smoke.py`.
- [x] Run Ruff on every modified Python file and `git diff --check`.
- [x] Start a new full Lite 300 run with a new run ID and confirm one completed instance has prediction, public trajectory, diagnostic bundle, and no checkout.
- [x] Record run ID, progress, archive bytes, disk free space, and any observed Agent issues in `docs/swebench-progress.md`.
