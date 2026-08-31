# Provider Turn Convergence Implementation Plan

> **Spec:** `docs/superpowers/specs/2026-08-25-provider-turn-convergence-design.md`

**Goal:** Make every main Provider round-trip attributable and remove the evidence-proven
extra completion round-trip after current-generation acceptance.

**Architecture:** A pure `turn_economy` module classifies run-state snapshots and structured
model responses. `RuntimeState` owns bounded persisted accounting. `AgentRunner` emits trace
facts at its canonical model boundary and uses one pure readiness predicate before allowing
early terminal settlement.

**Tech stack:** Python 3.9+, stdlib dataclasses, pytest, existing Runtime trace/session APIs.

---

## Task 1: Provider turn classifier

**Files:**

- Create: `nz_coder/runtime/turn_economy.py`
- Create: `tests/runtime/test_turn_economy.py`

1. Write failing tests for initial, investigation, mutation, verification, failure-repair,
   requirement-repair, convergence, final-answer, and mixed-tool classifications.
2. Run `pytest -q tests/runtime/test_turn_economy.py` and confirm import/test failure.
3. Implement immutable snapshot/observation dataclasses and pure classifiers.
4. Re-run the focused tests.

## Task 2: Persist and expose the ledger

**Files:**

- Modify: `nz_coder/runtime/runtime_state.py`
- Modify: `tests/test_runtime_state.py`
- Modify: `nz_coder/runtime/loop.py`
- Modify: `tests/test_loop_fake.py`

1. Write failing round-trip tests for bounded turn records and aggregates.
2. Add reset-safe `provider_turn_records`, `provider_turns_by_reason`, and
   `provider_turns_by_outcome` fields plus one recording method.
3. Project the aggregates into runtime result metadata.
4. Run the focused state and loop tests.

## Task 3: Instrument the canonical Runner

**Files:**

- Modify: `nz_coder/runtime/runner.py`
- Modify: `tests/runtime/test_native_runner.py`

1. Write a failing native-runner test that captures `provider_turn_started` and
   `provider_turn_settled` with stable one-based turn identity.
2. Capture a snapshot immediately before `services.model.complete_turn`.
3. Settle the observation from structured tool calls/final response and state deltas.
4. Persist it through `RuntimeState` and emit trace events.
5. Run the focused Runner tests.

## Task 4: Early evidence-complete tool-boundary stop

**Files:**

- Modify: `nz_coder/runtime/turn_economy.py`
- Modify: `nz_coder/runtime/runner.py`
- Modify: `tests/runtime/test_turn_economy.py`
- Modify: `tests/runtime/test_native_runner.py`

1. Write failing pure predicate tests for current, stale, failed, and missing contracts.
2. Write a failing Runner test whose first mutation/verification tool batch satisfies the
   ledger and semantic review and therefore completes without a second Provider call.
3. Implement the strict readiness predicate and route only eligible tool boundaries through
   existing completion settlement.
4. Prove semantic rejection and stale evidence continue normally.

## Task 5: Regression and real terminal evidence

**Files:**

- Modify: `docs/infcode-alignment-learning-log.md`
- Modify: `docs/terminal-product-real-world-issues.md`

1. Run focused runtime/context/permission/terminal tests.
2. Run the full pytest suite.
3. Run at least five fresh terminal coding tasks in `/home/pyh/test_nzcoder`, including one
   long repair with a failed test followed by a focused fix.
4. Extract Provider calls, reasons, outcomes, tokens, verification evidence, and final status.
5. Record measured results and any remaining inefficiency in both development documents.
