# Semantic Completion Evidence Implementation Plan

> Execute this plan with TDD. Preserve unrelated dirty-worktree changes and do
> not commit or push unless requested.

**Goal:** Prevent exact test acceptance from falsely satisfying compatibility
promises, and route terminal tool-boundary completion through a mutation-scoped
semantic evidence gate.

**Architecture:** Extend the existing TaskContract/RequirementLedger rather
than adding a parallel verifier state. Reuse the existing Sidecar Verifier as
the semantic evidence producer, enrich its evidence packet, and let
AgentRunner recompute the ledger after semantic verification.

**Tech stack:** Python 3.9+, pytest, existing NZ-Coder runtime services only.

---

## Task 1: Lock the ledger semantics with failing tests

**Files:**

- Modify: `tests/runtime/test_task_contract.py`
- Modify: `nz_coder/runtime/task_contract.py`

1. Add a test proving exact acceptance leaves compatibility as `candidate`.
2. Add a test proving a current-generation semantic accept satisfies it.
3. Add a test proving a later mutation invalidates that evidence.
4. Add round-trip/schema tests for `required_evidence`.
5. Run the focused test and confirm it fails before implementation.
6. Implement the smallest schema and ledger changes; rerun focused tests.

## Task 2: Bind semantic evidence to the Sidecar Verifier

**Files:**

- Modify: `tests/test_sidecar_verifier.py`
- Modify: `nz_coder/runtime/sidecar_verifier.py`

1. Add tests that task requirements, exact acceptance state, and bounded real
   diff excerpts are present in the verifier packet.
2. Add tests that only real accept traces record semantic evidence.
3. Add a test forcing the sidecar gate when a semantic requirement is pending.
4. Confirm failures, then implement helpers and current-generation ledger
   observation.
5. Remove any duplicated verifier message/client invocation encountered in the
   touched path and pin it with regression assertions.

## Task 3: Close the tool-boundary bypass

**Files:**

- Modify: `tests/runtime/test_native_runner.py`
- Modify: `nz_coder/runtime/runner.py`
- Modify: `nz_coder/runtime/adapters/runner.py`

1. Add a native runner test where exact acceptance passes at a tool boundary
   but compatibility is still pending.
2. Assert the completion verifier runs, records semantic evidence, and the
   Runtime then finalizes.
3. Add revise/unavailable cases proving the run continues or stops truthfully
   without a false completed state.
4. Reorder completion verification so semantic hooks can produce evidence
   before the deterministic ledger gate is evaluated.
5. Recompute unresolved requirements after the verifier returns.

## Task 4: Trace and product diagnostics

**Files:**

- Modify: `nz_coder/runtime/runtime_state.py`
- Modify: relevant focused tests

1. Expose semantic-pending state and current-generation evidence in stable
   trace fields.
2. Emit a distinct terminal reason for semantic review acceptance, revision,
   and unavailable evidence.
3. Keep provider-call accounting as sidecar purpose, not coding iteration.

## Task 5: Verification and real product run

1. Run focused task-contract, sidecar, and native-runner tests.
2. Run Ruff on changed Python files, `compileall`, and `git diff --check`.
3. Run the full pytest suite.
4. Run one fresh real DeepSeek cron compatibility task in an isolated copy.
5. Inspect trace counts, semantic gate events, final status, patch behavior for
   numeric compatibility, `0/7`, wrap-around named ranges, and named
   wrap-around steps.
6. Update:
   - `docs/terminal-product-real-world-issues.md`
   - `docs/infcode-alignment-learning-log.md`
7. Report measured results and any remaining provider variance without claiming
   parity from unit tests alone.
