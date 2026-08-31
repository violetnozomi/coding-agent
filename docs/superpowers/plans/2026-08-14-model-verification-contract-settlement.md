# Model Verification Contract Settlement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consume successful model-issued explicit acceptance evidence once per mutation generation and prevent duplicate Runtime verification.

**Architecture:** Command equivalence lives in `VerificationContract`; mutation-scoped state lives in `RuntimeState`; `AgentLoop` bridges a settled observation to the existing VerificationManager. Synthetic Runner calls remain explicitly marked and use their existing settlement path.

**Tech Stack:** Python 3.9+, standard library, existing RuntimeState/AgentRunner/Tool Runtime, pytest.

## Global Constraints

- No dependency or Agent framework.
- Exact tokenized command match only.
- No automatic terminal response.
- Preserve native Runner and subagent completion semantics.
- No Git commit for this dirty shared workspace.

---

### Task 1: Exact command contract

**Files:**
- Modify: `nz_coder/runtime/verification_contract.py`
- Test: `tests/runtime/test_verification_contract.py`

**Interfaces:**
- Produces: `VerificationContract.matches_command(command: str) -> bool`

- [x] Write tests proving whitespace/quoting-equivalent commands match and shell-composed/different-target commands do not.
- [x] Run the tests and confirm the missing method fails.
- [x] Implement conservative `shlex.split` token equality.
- [x] Run the focused tests to green.

### Task 2: Mutation-scoped model Bash settlement

**Files:**
- Modify: `nz_coder/runtime/runtime_state.py`
- Modify: `nz_coder/runtime/loop.py`
- Test: `tests/test_runtime_state.py`
- Test: `tests/test_loop_fake.py`

**Interfaces:**
- `RuntimeState.observe_tool(..., succeeded: bool | None = None) -> dict | None`
- Observation: `{"command": str, "output": str, "passed": bool}`

- [x] Write tests for success, failure, non-match, synthetic marker skip, and generation re-arm.
- [x] Confirm tests fail because Bash results do not update the contract.
- [x] Record exact model-issued attempts in RuntimeState and return the observation.
- [x] Forward the observation to `VerificationManager.observe_acceptance_contract()` in AgentLoop.
- [x] Run focused state/loop tests to green.

### Task 3: Finalization guidance and duplicate prevention

**Files:**
- Modify: `nz_coder/runtime/runtime_state.py`
- Modify: `nz_coder/runtime/runner.py`
- Test: `tests/test_runtime_state.py`
- Test: `tests/runtime/test_native_runner.py`

**Interfaces:**
- Synthetic Bash input marker: `_nz_runtime_contract: true`

- [x] Write a prompt test requiring explicit current-generation acceptance guidance.
- [x] Write a Runner test proving the synthetic contract remains one attempt.
- [x] Confirm both fail before implementation.
- [x] Mark synthetic Runner calls and emit current-generation finalization guidance.
- [x] Run focused Runtime/Runner/Tool tests and Ruff.

### Task 4: Real product and repository verification

**Files:**
- Update: `docs/terminal-product-real-world-issues.md`
- Update: `docs/infcode-alignment-learning-log.md`

**Interfaces:**
- Produces: one fresh trace and honest TP-025 status.

- [x] Run the exact isolated `cron_engine` long task from a 59-pass baseline.
- [x] Independently run `python -m pytest -q cron_engine/tests`.
- [x] Compare model calls, tools, failures, tokens, terminal state, and repeated acceptance commands.
- [x] Update TP-025 and the learning log without closing the gate unless 15/25 and correctness both pass.
- [x] Run complete pytest, Ruff, and `git diff --check`.

### Task 5: Close readiness issues exposed by the real trace

**Files:**
- Modify: `nz_coder/runtime/runtime_state.py`
- Modify: `nz_coder/runtime/runner.py`
- Modify: `nz_coder/recovery.py`
- Test: `tests/test_runtime_state.py`
- Test: `tests/runtime/test_native_runner.py`
- Test: `tests/test_recovery.py`

- [x] Track open Todo items as persisted Runtime evidence.
- [x] Defer budget-zone synthetic acceptance while Todo criteria remain open;
      keep the natural-completion acceptance boundary mandatory.
- [x] Do not emit finalization guidance for an intermediate acceptance pass.
- [x] Include the exact active workspace/package-parent relation in subprocess
      package-root diagnostics.
- [x] Re-run the failed real sample only after focused tests pass.
