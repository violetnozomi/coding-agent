# Terminal Efficiency Closure Design

Date: 2026-08-16

## Goal

Close the four runtime defects exposed by the fifth real long-task run while
preserving NZ-Coder's existing native runtime, verification contract, requirement
ledger, and 20-call absolute safety cap. A successful implementation must make
terminal output durable, surface the most specific recovery target, run useful
verification before the nominal boundary, and keep calls near the boundary
focused on completion rather than renewed exploration.

The paid real-task target is at most 15 coding calls, a non-empty typed/headless
result, passing exact acceptance, and no regression in existing local tests.

## Confirmed failure mechanisms

1. `ProductionRunLifecycle._prepare_terminal()` sends `content_text` to terminal
   callbacks but omits it from `last_status`. `AgentRunner._typed_result()` reads
   `payload["content"]` and then falls back to prior assistant text. A tool-led
   terminal whose assistant messages have empty text therefore returns an empty
   `RunResult.final_text` even when the boundary settler produced a factual
   summary.
2. `VerificationScheduler` is called only when a budget zone is first crossed.
   The current orange boundary can occur before a useful targeted command is
   available or after mutation state changed; because a zone is emitted once,
   the scheduler does not reconsider the newly actionable verification until
   natural completion or the nominal boundary.
3. `_build_test_failure_diagnostic()` returns immediately after classifying a
   multi-file failure as `widespread_test_regression`. This suppresses the more
   actionable `subprocess_workspace_drift` signal even when static inspection
   has resolved the exact helper and stale `cwd`.
4. Closure policy constrains broad tools, but it does not express all cheap
   project facts before the model chooses an action. In the fifth run this left
   room for an invalid `git diff` in a non-Git workspace and delayed use of the
   precise failing test.

## Reference architecture findings

### InfCodeX

- `Runner.run()` owns a stable `RunResult.output`; every accepted text-only
  terminal returns the already-committed final assistant text through that
  field.
- Stop hooks run after the assistant message is committed but before terminal
  invariants. They may reanimate only within a bounded budget.
- `InvariantSession.assertTerminal()` evaluates accumulated mutation and
  evidence facts once at the terminal boundary.
- `deterministic-evaluator.ts` runs build/test/lint ground-truth checks and
  returns complete structured status, stdout, stderr, exit code, and duration to
  the next model turn.

### infcode-dev/OpenCode

- Assistant text parts and message metadata are persisted during processing.
  Cleanup flushes unfinished text/reasoning/tool state, records
  `time.completed`, updates the message, and only then lets run state become
  idle.
- The loop derives completion from persisted assistant `finish` and tool-part
  state, rather than from a UI callback.
- Session status is a separate observable projection of durable message state;
  it is not the owner of the answer text.

The shared rule is that user-visible terminal text is durable runtime output
before a completed/idle event is published.

## Considered approaches

### A. Prompt-only convergence instructions

Add stronger orange/red prompts telling the model to test earlier and avoid
Git commands. This is small but non-deterministic, consumes context, and cannot
repair empty typed results or suppressed diagnostics.

### B. Runtime closure with small deterministic components (selected)

Keep the existing state machine and add narrowly owned behavior at four
boundaries: terminal persistence, verification scheduling, diagnostic
aggregation, and closure action filtering. This follows both reference
projects without importing their TypeScript architecture wholesale.

### C. Replace verification/recovery with a new evidence engine

A new event-sourced engine could unify every fact, but it would duplicate
`RuntimeState`, `VerificationManager`, and the requirement ledger. The migration
risk is disproportionate to the observed defects.

## Architecture

### 1. Durable terminal result

`ProductionRunLifecycle` owns the final result payload. Before publishing a
terminal event it resolves terminal content in this order:

1. non-empty `content_text` supplied by the boundary settler;
2. the latest non-empty assistant text for the current user turn;
3. a deterministic status summary for non-success terminals when available;
4. an empty string only for cancellation/interruption paths where no answer was
   produced.

The resolved text is written to `state.last_status["content"]`. The lifecycle
also passes it to `persist_assistant_end`, whose internal signature gains an
optional `content_text` argument. When the current turn's final assistant
message has empty textual content, that callback writes the resolved text into
the same assistant message before setting its end state and checkpointing it.
It never overwrites non-empty provider text and never creates a second assistant
message.

Completed events are published only after this persistence step and `commit()`.
`_typed_result()` continues to prefer the explicit payload and keeps its
transcript fallback for compatibility. This makes native SDK, headless CLI,
HTTP, and legacy adapters observe the same final text.

### 2. Mutation-aware verification scheduling

`VerificationScheduler` remains a pure selector, but scheduling is no longer
tied only to one-shot budget notices. `AgentRunner` asks it at deterministic
safe boundaries after a fully settled tool batch when all of these hold:

- the run is in yellow, orange, red, closure, or bounded-emergency pressure;
- the mutation generation is newer than the generation last considered for the
  selected verification stage;
- a static or targeted command is pending;
- no equivalent command has already passed for that mutation generation.

The scheduler returns a structured action containing stage, command, reason,
and mutation generation. Runtime state records the last scheduled generation
per stage before execution, preventing repeated automatic checks when a command
fails without a new edit. A subsequent mutation makes the stage eligible again.

Priority remains deterministic:

1. yellow: required static check;
2. orange/closure repair: targeted check, then static;
3. red/closure finalize: targeted, static, then exact acceptance only when hard
   requirements are settled;
4. natural completion: exact acceptance.

Automatic verification uses the existing Bash tool path, permissions,
structured exit metadata, transcript result, trace events, and requirement
ledger. It does not add a provider request or a second verifier model.

### 3. Composite recovery diagnosis

Test-failure analysis becomes fact collection followed by action selection.
Independent detectors produce `DiagnosticSignal` records with:

- `classification`;
- `specificity`;
- evidence text;
- recommended next action;
- optional resolved repair target.

The initial implementation collects existing facts only:

- widespread multi-file regression;
- subprocess workspace drift;
- subprocess package-root mismatch;
- import/package-layout failure;
- regression test list;
- failed tests and traceback.

Signals are sorted by actionable specificity. A resolved workspace-drift helper
outranks the generic widespread-regression hypothesis, but both classifications
remain in the diagnostic. The first signal owns the numbered next action; lower
priority signals are rendered as supporting observations and cannot replace the
known repair target with broad exploration advice.

This is a formatting and decision refactor of deterministic evidence. It does
not ask an LLM to classify failures and does not hard-code the cron fixture.

### 4. Closure project facts and action filtering

The runtime captures bounded project facts once per run, including whether the
workspace is a Git repository. During orange/red/closure pressure:

- `diff_status` remains allowed because it already has a non-Git-safe mutation
  view;
- raw Bash `git diff`, `git status`, and equivalent Git-only inspection are
  blocked when the workspace is not a Git repository, with guidance to use
  `diff_status` or changed-file evidence;
- an identical known-file read remains governed by the existing doom-loop
  guard; no new global read cache is introduced;
- environment and package probes remain blocked unless they are the declared
  runtime verification command;
- a failure diagnostic with a resolved repair target adds that target to the
  known closure paths, allowing the narrow read/edit immediately.

This rule belongs to runtime policy, not the system prompt. It applies only
under convergence pressure and therefore does not prevent legitimate Git setup
or exploration early in a task.

## State and trace additions

`RuntimeState` persists:

- last automatically scheduled mutation generation per verification stage;
- bounded project facts needed by closure policy;
- the current primary recovery classification and supporting classifications.

Trace events include:

- terminal content source and whether it was persisted into the assistant;
- verification scheduler decision, stage, command fingerprint, and mutation
  generation;
- all diagnostic classifications plus the selected primary action;
- closure action denial reason, including `git_required_but_unavailable`.

No raw secrets, full environment dumps, or API credentials enter these fields.

## Error handling

- Terminal content persistence is best-effort only for transcript projection;
  failure to update the assistant cannot erase `last_status["content"]`.
- Automatic verification failures are normal evidence and return to the Agent;
  spawn/timeout failures retain their existing error semantics.
- Diagnostic detectors fail independently. One detector exception cannot
  suppress the remaining deterministic signals.
- Project-fact detection treats an inaccessible `.git` path as unknown rather
  than assuming Git is available.
- Cancellation does not synthesize a success summary and keeps the existing
  cancellation status.

## Test strategy

Tests are written before production changes.

### Unit and contract tests

1. Lifecycle finalization stores supplied deterministic summary in `content`,
   updates an empty final assistant, preserves non-empty provider text, and
   publishes completion after persistence.
2. `run_result()` returns the deterministic tool-terminal summary through
   `final_text` in streaming and non-streaming modes.
3. Scheduler reconsiders targeted verification after a new mutation generation,
   does not repeat it without another mutation, and still runs exact acceptance
   on natural completion.
4. A multi-file failure with statically resolved stale subprocess `cwd` renders
   both classifications and selects `subprocess_workspace_drift` as primary.
5. A genuine multi-file regression without a drift signal keeps
   `widespread_test_regression` primary.
6. Non-Git closure blocks raw Bash Git inspection while allowing
   `diff_status`, targeted verification, and known-target repair.
7. Existing cancellation, structured output, legacy adapter, and emergency
   policy tests remain unchanged or receive compatibility assertions.

### Local verification

- focused lifecycle, native runner, scheduler, recovery, and work-budget tests;
- full `pytest` suite;
- Ruff on changed Python files;
- diff inspection for accidental fixture- or task-specific behavior.

### Real product verification

Run a fresh long task in `/home/pyh/test_nzcoder` only after local gates pass.
Record model calls, tool calls, tokens, duration, terminal text, exact acceptance,
ledger state, and denied actions. The target is at most 15 coding calls. If it
exceeds 15, the trace must identify which deterministic scheduling or policy
boundary failed; the runtime must not falsely report success merely to meet the
call target.

## Explicitly out of scope

- domain-specific cron parser rules or fixture-specific prompt text;
- a new planner, sidecar model, or provider call;
- replacing `RuntimeState`, the verification contract, or requirement ledger;
- globally forbidding Git commands;
- claiming SWE-bench score parity from this single product run.

## Self-review decisions

1. The design does not equate fewer calls with correctness; exact acceptance
   and ledger evidence remain terminal requirements.
2. The terminal fix has one durable owner and preserves existing non-empty model
   output.
3. Verification is keyed by mutation generation, so early checks neither spam
   every turn nor become stale after a repair.
4. Composite diagnostics preserve generic evidence while allowing the most
   specific repair target to control the next action.
5. Git restrictions are pressure- and capability-aware rather than global.
6. No rule depends on the cron-engine task, DeepSeek, or a particular test
   filename.
