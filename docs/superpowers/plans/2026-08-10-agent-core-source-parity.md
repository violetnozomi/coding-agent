# Agent Core Source-Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move InfCode/InfCodeX Agent lifecycle contracts into NZ-Coder's real production assembly and fix the generation/order defects proven by the 20-instance trace audit.

**Architecture:** Keep one Python Agent Core with per-run observer and generation state. Translate upstream ordering and bounded-window behavior; isolate SWE-specific restrictions in an adapter and derive final risk only from the current generation.

**Tech Stack:** Python 3.9+, standard library, pytest; no Agent framework and no new runtime dependency.

## Global Constraints

- Preserve existing tool names, schemas, and result strings.
- All new behavior is test-first and must pass production assembly tests.
- No paid Provider calls or SWE-bench reruns are part of this plan.
- No source-level completion claim without a provider-free real loop trace.

---

### Task 1: Generation-scoped diff and verification terminal

**Files:**
- Modify: `nz_coder/runtime/runtime_state.py`
- Modify: `nz_coder/runtime/loop.py`
- Test: `tests/test_runtime_state.py`
- Test: `tests/test_loop_fake.py`

**Interfaces:**
- Consumes: successful mutation, `diff_status`, and `verify_changed_files` observations.
- Produces: `RuntimeState.strict_generation_terminal_ready() -> bool`.

- [ ] Write a failing test where verification precedes diff observation in the same mutation generation and terminal readiness becomes true.
- [ ] Run the focused test and confirm the old order-dependent implementation fails.
- [ ] Store diff and successful verification generation IDs and expose the terminal predicate.
- [ ] Make the loop consume the predicate after every settled tool batch, not only a batch containing verification.
- [ ] Run runtime/loop focused tests.

### Task 2: Generation-scoped final risk

**Files:**
- Modify: `nz_coder/swebench/orchestrator.py`
- Test: `tests/test_swebench_strict.py`

**Interfaces:**
- Consumes: tool-log generation metadata and final `agent_status.runtime.mutation_generation`.
- Produces: process warnings for recovered errors and semantic risk labels only for the final generation.

- [ ] Write failing tests for a recovered early write error and an unresolved final-generation write error.
- [ ] Confirm the recovered-error test fails with `tool_errors` on the old implementation.
- [ ] Add generation to tool-log rows and filter semantic failures by final generation.
- [ ] Preserve older failures as bounded process warnings.
- [ ] Run SWE strict tests.

### Task 3: InfCodeX bounded-window stall detector in the production path

**Files:**
- Create: `nz_coder/runtime/stall_detector.py`
- Modify: `nz_coder/runtime/recovery.py`
- Modify: `nz_coder/runtime/loop.py`
- Test: `tests/test_stall_detector.py`
- Test: `tests/test_loop_fake.py`

**Interfaces:**
- Produces: stable JSON identity, 20-call ring buffer, three-repeat signal,
  two-repeat-with-cache-hit signal, reset, and trace envelope.
- Consumes: every proposed tool call before dispatch and compaction/run reset.

- [ ] Write literal contract tests translated from InfCodeX stall-detector cases.
- [ ] Confirm imports/behavior fail before production code exists.
- [ ] Implement the detector and per-run owner.
- [ ] Wire every tool-call path and emit one typed stall signal.
- [ ] Run detector and loop tests.

### Task 4: Production stop-hook assembly

**Files:**
- Modify: `nz_coder/runtime/hooks.py`
- Modify: `nz_coder/runtime/composition.py`
- Test: `tests/test_runtime_hooks.py`
- Test: `tests/test_runtime_composition.py`

**Interfaces:**
- Consumes: natural-end transcript after output persistence.
- Produces: bounded `complete`, `reanimate`, or `abort` result in every production entry point.

- [ ] Write an assembly test proving the default coding runtime contains the intended stop-hook consumer.
- [ ] Confirm the current default assembly fails it.
- [ ] Register the consumer in the single composition owner and keep the two-reanimate budget.
- [ ] Test accept, revise, abort, exception fail-open, and budget exhaustion.
- [ ] Run hook/composition tests.

### Task 5: Strict policy adapter and real trace acceptance

**Files:**
- Modify: `nz_coder/swebench/policy.py`
- Modify: `nz_coder/runtime/runtime_state.py`
- Modify: `nz_coder/swebench/orchestrator.py`
- Test: `tests/test_swebench_strict.py`
- Test: `tests/test_loop_fake.py`
- Modify: `docs/infcode-alignment-learning-log.md`
- Modify: `docs/swebench-progress.md`

**Interfaces:**
- Consumes: classified read-only Bash intent and Core generation/stall events.
- Produces: bounded strict convergence without contaminating generic Core risk.

- [ ] Write failing tests for Bash source inspection consuming budget and status/verification Bash not consuming it.
- [ ] Add semantic Bash classification to the strict adapter.
- [ ] Bound repeated hard-gate feedback and preserve final-blocker exit.
- [ ] Run a provider-free fake Agent through investigate, mutate, verify, diff, terminal and assert trace order.
- [ ] Run focused and full tests, Ruff, and `py_compile`.
- [ ] Correct the alignment memory to distinguish mechanism, wired, contract-verified, and trace-verified states.
