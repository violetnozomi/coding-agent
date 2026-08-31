# Terminal Real-World Issues Repair Implementation Plan

> Execute in three batches. For every behavior change, first add a regression
> test that fails for the documented reason, then implement the minimum change.

## Batch 1 — Runtime closure, safety, and interaction state

1. Add non-Git `diff_status` tests and implement a successful ChangeTracker or
   not-applicable response in `nz_coder/tools/repo_intel.py`.
2. Add permission-result tests proving policy and user denials remain visible
   but recoverable; update result projection and run-settlement policy.
3. Add shell admission tests for absolute POSIX/Windows paths and parent escape;
   reject model-invented workspace-external access before dispatch.
4. Add scoped command approval tests for pytest/check commands; persist a
   normalized, narrow Session rule rather than a raw broad prefix.
5. Add closure-budget tests for successful verification and environment
   blockers; stop optional review/diagnostic loops from blocking a final result.
6. Add cancellation tests proving no tool starts after cancellation is
   requested and the durable end state is cancelled.
7. Reproduce selector submit-event leakage in a full-screen input test and add a
   modal handoff barrier.
8. Add plan-exit tests for direct plan completion and explicit question state;
   remove unnecessary provider continuation when the plan is already complete.
9. Run focused permission, tool-runtime, CLI/full-screen, plan, cancellation,
   and repo-intelligence tests.

## Batch 2 — Repository intelligence and LSP

1. Add glob tests for `.nz-coder`, VCS, caches, dependencies, and explicit
   private-path requests; centralize default ignore names and apply them to
   ripgrep plus `os.walk` paths.
2. Add a small-repository orientation test with tool/turn/output budgets; route
   narrow discovery through filtered listing and bounded document reads.
3. Add nested Python-package LSP root/configuration tests and an integration
   probe when `basedpyright-langserver` is available.
4. Update doctor LSP reporting so installed-but-nonfunctional is distinguishable
   from a successful semantic probe.
5. Run search, repo-intelligence, LSP, doctor, provider-fake, and loop tests.

## Batch 3 — Terminal presentation and history hygiene

1. Add 80-column render snapshots for command/session/process selectors and
   tables; introduce compact layouts with complete primary identifiers.
2. Add narrow command/tool/permission card tests and use semantic wrapping for
   full command details.
3. Add current-run lifecycle tests and reset live run state at `begin()` without
   deleting durable transcript history.
4. Add resumed-history projection tests for structured/tool-only messages and
   remove orphan JSON fragments from terminal-visible content.
5. Replace one-line top-level help with structured offline help and test its
   command/config/model examples.
6. Add first-Ctrl+C feedback lifetime tests.
7. Run terminal input, full-screen, timeline, commands, run renderer, process,
   CLI entrypoint, and Session tests.

## Final acceptance

1. Run the complete test suite if its runtime is practical; otherwise record
   the exact broader suites run and any unrelated pre-existing failures.
2. Install the current checkout in editable mode if needed and execute repeated
   real PTY flows in `/home/pyh/test_nzcoder`: read-only orientation, non-Git
   edit/test/finalize, permission rejection recovery, plan completion, picker
   selection, resume, LSP navigation, process list/log/kill, and cancellation.
3. Update `docs/terminal-product-real-world-issues.md` with implementation,
   automated evidence, real-test evidence, and honest final status for every
   issue.

## Batch 4 — Reference-runtime terminal and convergence closure

### Task 1: Cancellation terminal protocol

**Files:**
- Modify: `nz_coder/runtime/runner.py`
- Modify: `nz_coder/runtime/core/events.py`
- Modify: `nz_coder/interface/cli.py`
- Test: `tests/runtime/test_native_runner.py`
- Test: `tests/test_cli_commands.py`

- [x] Add a native Runner regression where the Provider raises
  `CancelledError`; assert a typed cancelled result, one Session finalization,
  a settled assistant error, and ordered cancelled lifecycle output.
- [x] Run the focused test and confirm the current exception escapes or records
  the wrong event.
- [x] Route `CancelledError` through lifecycle finalization inside
  `_run_turns`, returning `status=cancelled`; keep the outer Session finalizer
  exactly-once and make runtime middleware classify cancellation separately
  from generic failures.
- [x] Add a CLI regression proving controller cancellation waits for the
  cancelled result and the next prompt remains usable.
- [x] Run both focused cancellation suites.

### Task 2: User-declared verification intent

**Files:**
- Modify: `nz_coder/runtime/execution_context.py`
- Modify: `nz_coder/runtime/run_lifecycle.py`
- Modify: `nz_coder/runtime/task_policy.py`
- Modify: `nz_coder/tools/bash.py`
- Test: `tests/test_execution_context.py`
- Test: `tests/test_task_policy.py`

- [x] Add failing tests for requested directory-suite admission, harmless
  wrapper normalization, repository-wide scope escalation, and sibling test
  scope escalation.
- [x] Extract the latest natural user text at run initialization and bind its
  declared verification scopes to run-local context state.
- [x] Implement structural test-command target extraction and scope containment
  without shell execution or new dependencies.
- [x] Allow a broad runner only when every attempted target is contained in a
  declared test scope; keep the existing block message for all other cases.
- [x] Run focused task-policy, execution-context, and bash tests.

### Task 3: Run work-budget convergence

**Files:**
- Create: `nz_coder/runtime/work_budget.py`
- Modify: `nz_coder/runtime/core/run_context.py`
- Modify: `nz_coder/runtime/runner.py`
- Test: `tests/runtime/test_work_budget.py`
- Test: `tests/runtime/test_native_runner.py`

- [x] Add table-driven failing tests for green/yellow/orange/red transitions,
  one-shot guidance, resumed turn counts, and a one-turn budget edge case.
- [x] Implement a dependency-free run budget controller whose pressure is
  derived from completed turns divided by the admitted hard cap.
- [x] Before each Provider call, append at most one synthetic guidance message
  for each newly crossed 70/85/95% zone and trace the transition.
- [x] Preserve the existing hard `max_turns` terminal and expose budget zone in
  the typed runtime metadata.
- [x] Run focused work-budget and native Runner tests.

### Task 4: Regression and real-product evidence

**Files:**
- Modify: `docs/terminal-product-real-world-issues.md`

- [x] Run the focused cancellation, task-policy, bash, execution-context,
  Runner, and work-budget suites.
- [x] Run the broader runtime/terminal suite and record its exact result.
- [x] Execute real PTY cancellation in `/home/pyh/test_nzcoder`; inspect the
  trace for a terminal cancelled record and resumable history.
- [x] Execute a bounded long edit flow, compare model/tool call counts with the
  previous TP-025 trace, and record evidence without claiming closure when the
  provider or environment prevents comparison.
