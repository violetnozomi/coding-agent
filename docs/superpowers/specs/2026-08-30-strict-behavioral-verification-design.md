# Strict Behavioral Verification Design

## Goal

Prevent strict SWE-bench inference from reporting a source patch as verified
when the only evidence is a static compile check. A strict run must include a
successful, non-empty targeted behavioral check for the latest source mutation,
or finish with a truthful unverified outcome.

## Problem

`ProductRunEnvironment` already constructs `VerificationManager` with
`require_targeted=True` during strict local SWE-bench runs. The flag currently
affects environment-error classification, but it does not affect verification
completion. Consequently, `verify_changed_files` can pass the required static
stage, clear `verification_needed`, and publish `verification_state="passed"`
while every targeted command remains optional and unexecuted.

The 2026-08-30 real-provider smoke batch exposed the consequence twice:

- a Django data migration passed `py_compile` but missed required warning and
  transaction behavior;
- a pytest CLI alias passed `py_compile` but changed the parser destination,
  causing an internal collection error and broad PASS_TO_PASS regressions.

The strict Bash policy was not the cause. It already permits direct narrow
`pytest` commands and conventional repository test runners. The repair must not
weaken that policy.

## Scope

This change applies only when `VerificationManager(require_targeted=True)` is
used. Ordinary terminal, HTTP, SDK, child-Agent, and non-strict evaluation runs
retain their current static-only completion behavior.

The implementation changes the following existing responsibilities:

- `VerificationManager` owns the strict behavioral-evidence invariant and its
  public status snapshot.
- verification output parsing rejects explicit zero-test results as positive
  targeted evidence.
- the strict stop hook gives an actionable message when behavioral evidence is
  missing.
- the SWE-bench orchestrator distinguishes an environment-blocked patch from a
  patch that simply omitted required behavioral verification.

No new framework, Provider call, Docker lifecycle, or external dependency is
introduced.

## Behavioral-Evidence Invariant

After a material source mutation in strict mode, completion requires both:

1. all existing required static commands have passed; and
2. at least one targeted verification command has passed for the current
   mutation generation.

The targeted command may come from exact failure evidence, a user-declared
acceptance contract, or a model-selected narrow command. Repository-inferred
related tests remain advisory: the runtime must not automatically execute or
promote a weak graph or filename guess merely to satisfy the invariant.

`verify_changed_files` settles only the static stage. It cannot settle the
strict targeted-evidence requirement.

Every material write resets both static and targeted evidence through the
existing `VerificationManager.mark_write()` lifecycle.

## Pipeline Representation

The targeted stage snapshot gains an explicit `evidence_required` boolean.
When strict mode is active:

- `required` is true even if there is no exact required command;
- `status` is `pending` until a targeted command has genuinely passed;
- `status` is `passed` after such evidence is observed;
- `next_required_stage` is `targeted` after static evidence passes but before
  behavioral evidence passes.

Command-level `required` remains reserved for exact commands. This preserves
the distinction between an exact target that must be rerun and a stage-level
requirement for the model to choose a relevant target.

Non-strict snapshots do not expose a stage-level requirement and remain
backward compatible.

## Empty-Test Handling

A targeted command that exits zero but explicitly reports that it ran no tests
does not satisfy behavioral verification. Recognized markers include:

- `no tests ran`;
- `collected 0 items`;
- `Ran 0 tests`;
- `Found 0 test(s)`.

Such a result is recorded as `skipped`, leaves the targeted stage pending, and
produces an actionable gate message. It is not treated as a repairable source
failure because an empty selector usually means the verification command, not
the patch, is wrong.

Existing nonzero test failures remain `failed_repairable`. Existing missing
dependency or incompatible-host failures remain `blocked_environment`.

## Completion and SWE-bench Result Semantics

Strict completion behaves as follows:

- static and targeted evidence passed: normal `completed` eligibility;
- targeted command cannot run because of a recognized environment blocker:
  `completed_unverified` at the runtime boundary and `risky` for a non-empty
  SWE-bench patch;
- no targeted command was run, an empty target was run, or a real target still
  fails: `verification_needed` remains true and SWE-bench reports
  `agent_failed` rather than `completed`;
- an empty patch remains `empty_patch` regardless of verification state.

Predictions and traces remain durable for non-empty risky or failed attempts so
the official harness and failure analysis can still inspect them.

## Stop-Hook Guidance

When the strict targeted stage is pending without an exact command, the stop
hook tells the model to run one direct narrow behavioral test that exercises
the changed behavior and to ensure that the command runs at least one test. It
may list advisory candidates, but it must not claim that any candidate is
authoritative.

The prompt continues to require `diff_status` and `verify_changed_files` for
the final source generation. It does not recommend broad suites or shell
composition.

## Security and Safety

- All generated or model-selected commands continue through the existing
  strict Bash grammar.
- Broad test runners, network access, command substitution, redirection, and
  private NZ-Coder paths remain blocked.
- No inferred related test is automatically executed.
- Path validation and transaction behavior are unchanged.
- SWE/headless Auto classification remains disabled and unrelated to this
  verification change.

## Testing

Tests must cover the red-green behavior at each boundary:

1. strict static success still leaves `verification_needed=True` and targeted
   `status="pending"`;
2. a successful model-selected targeted test clears the strict gate after
   static success;
3. the same static-only flow still completes in non-strict mode;
4. explicit zero-test output leaves strict targeted evidence pending;
5. a targeted environment blocker produces the existing
   `blocked_environment` state;
6. the strict stop message requests a non-empty targeted behavioral check;
7. a non-empty environment-blocked SWE patch is `risky`;
8. a non-empty patch with missing behavioral evidence remains `agent_failed`;
9. focused verification, hooks, SWE strict, and orchestrator regression suites
   pass before the full repository suite.

After local verification, rerun a bounded real-provider SWE smoke on the two
previously unresolved instances. The expected product-level improvement is
truthful verification and stronger repair pressure; a resolved result is
measured evidence, not a guaranteed acceptance criterion for this runtime fix.

## Non-Goals

- inferring official hidden tests;
- adding instance-specific Django or pytest patch rules;
- treating weak repository-graph test candidates as authoritative;
- running Docker during every inference attempt;
- changing the official pass@1 protocol or automatically retrying a first-pass
  prediction with official failure logs;
- changing normal terminal verification semantics.
