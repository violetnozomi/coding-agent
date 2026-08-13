# Core Coding Reliability Closure Report

Date: 2026-08-12

## Executive conclusion

This phase closed the observed Case B completion false-block, made verification
state and evidence explicit, converted serial tool exceptions into repairable
Agent evidence, added provider-neutral Web Search, and produced real-model
behavioral evidence with `deepseek-v4-flash`.

- Verification/recovery/completion matrix: 36/36 successful, 0 false passes,
  0 false blocks. Six environment-unavailable runs completed honestly as
  `completed_unverified`.
- Same-condition reference cases A/B/E/F/I: nzcoder 15/15; InfCodeX 13/15;
  OpenCode/infcode-dev unavailable in this environment. This small matrix is
  evidence for these fixtures, not a claim of general product superiority.
- Web Search OFF/ON: both 15/15. Search ON reduced mean turns from 7.27 to 6.73,
  `webfetch` calls from 4.80 to 3.27, and input tokens from 93,913 to 44,288.
  Mean wall time was effectively flat/slightly worse (85.34s to 87.17s), with
  substantial case variance. The local-only control made zero searches in all
  six OFF/ON runs.
- Current Bash is structurally insufficient for servers, watch mode, REPLs, and
  later log reads. Persistent process/PTY is now a demonstrated next core gap.
- Semantic retrieval remains optional experimental. Nothing in this phase
  justifies persistent vectors or incremental embedding work.

## Actual completion pipeline

The production path is:

```text
Agent emits a final response without tools
  -> no-tool completion hooks run
  -> VerificationManager exposes the current staged evidence state
  -> ordinary runs: verification_gate_hook either completes, reopens the loop,
     or ends honestly as completed_unverified after its bounded prompt budget
  -> strict SWE runs: strict_generation_stop_hook additionally requires a
     same-generation diff and verification terminal signal
  -> stop-hook decision is complete / complete_unverified / reanimate / abort
  -> AgentRunner lifecycle commits the terminal status and RunEvidence
```

Responsibility is deliberately split:

| Decision | Owner |
|---|---|
| Whether to execute a test/check | Coding model, guided by the prompt and staged plan |
| Candidate static/targeted/regression commands | Verification planner, based on changed files, project profile, scripts, and prior failing tests |
| Whether observed evidence passed | `VerificationManager`, from structured tool outcome and command classification |
| Whether a failed check is repairable | Verification state machine (`FAILED_REPAIRABLE`) |
| Whether a failure is environmental | Environment failure classifier (`BLOCKED_ENVIRONMENT`) |
| Whether the Agent re-enters the loop | Verification/strict stop hooks, under bounded budgets |
| Whether final completion is allowed | Completion hooks plus, in strict SWE mode, generation consistency |
| Persisted/traceable evidence | `RunEvidence.verification_evidence`, VerificationManager snapshot, TraceRecorder, benchmark report |

The state contract is `UNVERIFIED -> VERIFYING -> PASSED`, or
`VERIFYING -> FAILED_REPAIRABLE -> Agent repair -> VERIFYING`. Missing tooling
is `BLOCKED_ENVIRONMENT`; partial/no executable checking is represented as
`DEGRADED`, rather than silently reported as pass.

The planner is scope-aware, but one implementation-depth gap remains: related
Python tests are primarily inferred from filename/import conventions and prior
failures. The repository intelligence impact/related-test graph is not yet a
default direct planner input. The real matrix did not expose this as the Case B
failure, so this phase did not introduce another planner architecture.

## Case B root cause

Classification: **F. Completion gate logic error**.

Across all 12 old retrieval strategies/repetitions, the Agent produced the
correct rename, all expected source/test callers were updated, and pytest
passed. The stored terminal evidence nevertheless showed:

```text
mutation_generation = current generation
verification_generation = current generation
verification_needed = false
diff_generation = -1
raw_status = stopped_by_hook (or max_turns)
```

The old strict diff rule accepted only `source_only=true` and
`tests_modified=false`. Case B explicitly asks to update affected tests, so the
correct patch was classified as ineligible for a terminal diff. The stop hook
reanimated twice, the Agent repeated valid verification, and the bounded hook
eventually stopped the run.

The corrected rule accepts either a source-only patch or a patch containing
tests when the task explicitly asks for test changes. Completion also requires
the current verification state to be passed/degraded and no required evidence
to remain. Post-fix evidence:

- Same Case B: 3/3 success, patch 3/3, tests 3/3, false blocks 0.
- Dedicated V5 rename: 3/3 success, false passes 0, false blocks 0.

This was not a retrieval failure, patch failure, wrong verification command, or
bad benchmark expectation.

## Verification behavioral evidence

The production driver ran V1-V8 and C1-C4 three times each. The benchmark
harness never repaired source files; the real Agent observed failures, edited,
and reran checks.

| Case | Result | False pass | False block | Mean failures | Mean recoveries |
|---|---:|---:|---:|---:|---:|
| V1 syntax failure | 3/3 | 0 | 0 | 2.00 | 1.00 |
| V2 targeted test failure | 3/3 | 0 | 0 | 2.67 | 1.67 |
| V3 dependent regression | 3/3 | 0 | 0 | 3.33 | 2.67 |
| V4 correct patch, no tests | 3/3 | 0 | 0 | 4.00 | 2.00 |
| V5 correct cross-file rename | 3/3 | 0 | 0 | 1.33 | 1.00 |
| V6 environment failure | 3/3 degraded | 0 | 0 | 4.67 | 2.33 |
| V7 repeated failure | 3/3 | 0 | 0 | 1.67 | 1.00 |
| V8 partial caller patch | 3/3 | 0 | 0 | 1.67 | 1.33 |
| C1 premature completion | 3/3 | 0 | 0 | 1.67 | 1.33 |
| C2 passed evidence | 3/3 | 0 | 0 | 1.00 | 1.00 |
| C3 static evidence, no tests | 3/3 | 0 | 0 | 1.33 | 0.67 |
| C4 environment unavailable | 3/3 degraded | 0 | 0 | 4.67 | 2.33 |

Overall: 36/36, mean 9.17 turns, mean 6.03 verification attempts, mean
2.50 failed attempts, mean 1.53 recoveries, and mean wall time 19.07s. A
"verification failure" here is a trajectory classifier count and may include a
failed verification-shaped shell attempt; it is not limited to assertion
failures.

Controlled production-component fault injection also passed 7/7: timeout kill,
tool exception, provider stream interruption, partial-write rollback, malformed
tool result, unavailable LSP, and unavailable verification command. This is
mechanism evidence, not real-model intelligence evidence.

## Source-level failure-mode comparison

| Failure mode | InfCodeX | nzcoder | OpenCode/infcode-dev | Verdict |
|---|---|---|---|---|
| Syntax error | Mutation reflection and sidecar context | Staged static checks plus post-write LSP | Post-edit/write LSP diagnostics | Aligned, different control flow |
| Targeted test failure | Tool outcomes and verifier revise path | Explicit failed-repairable evidence and required rerun | Tool error/result returned to model | nzcoder evidence is more explicit |
| Regression failure | Sidecar verdict can reanimate | Optional regression evidence becomes blocking once run | Model-driven tests and diagnostics | Comparable; no OpenCode behavioral data |
| Wrong/partial edit | Edit recovery, mutation reflection | Exact-edit diagnostics, transaction rollback, impact review | Snapshot/patch/revert and diagnostics | OpenCode has deeper user-visible revert |
| Repeated failing command | Stall sidecar | Repeat guard, stall sidecar, trajectory diagnostics | Session retry/tool errors; less explicit command-stall policy found | nzcoder/InfCodeX aligned |
| Stale context | Session snapshot | Session state, compaction continuity, snapshots | Pre-stream and step snapshots | Broadly aligned |
| Edit/tool exception | Tool outcome and edit-recovery middleware | Structured failed result plus transactional rollback | Tool-error parts and session continuation | Aligned after this phase |
| Verification false negative | Sidecar accept/revise/blocked decision | Explicit state/evidence plus bounded completion gate | Mostly model-driven completion | nzcoder now has strongest explicit local contract |
| Verification false positive | Sidecar verifier | Required-stage evidence and generation matching | Diagnostics/tool exit evidence, no equivalent strict gate found | Different by design |
| Premature completion | Sidecar stop hook | Ordinary verification gate; stricter generation gate in SWE mode | Model/session finish semantics | nzcoder/InfCodeX aligned in intent |
| Stall/doom loop | Stall sidecar | Stall sidecar and repeated-call guard | General retry/session processing | nzcoder/InfCodeX more explicit |

## Reference behavioral comparison

Conditions were kept as close as the CLIs permit: same fixtures, prompts,
`deepseek-v4-flash`, provider credentials, provider-default reasoning, 24-turn
budget, and three repetitions.

| Case | nzcoder | InfCodeX | Notes |
|---|---:|---:|---|
| A unknown localization | 3/3 | 3/3 | Both correct |
| B cross-file rename | 3/3 | 3/3 | Both patch and tests correct |
| E long horizon | 3/3 | 1/3 | InfCodeX made no patch in two runs |
| F verification recovery | 3/3 | 3/3 | Both correct |
| I vocabulary mismatch | 3/3 | 3/3 | Both localized correctly |
| Total | 15/15 | 13/15 | Fixture-level evidence only |

nzcoder mean wall time was lower on each row in this run, but latency is noisy
and the reference CLI integration differs. InfCodeX's JSON protocol terminated
without final tool/run events in this environment, so the adapter used its text
mode. Consequently InfCodeX turns, tool calls, token usage, localization turn,
and recovery trajectory are unavailable. Its long-horizon requirement is
therefore recorded as unobservable, not silently passed. nzcoder completed E in
all runs, but only 1/3 reached the fixture's nominal 15-turn horizon; that is a
fixture-strength warning separate from task correctness.

OpenCode/infcode-dev behavioral effectiveness is **unavailable**. The checked-in
repository requires Bun 1.3.13; Bun is absent, and an npm-based fallback cannot
resolve its Bun workspace `catalog:` and internal package setup. No score was
fabricated.

## Web Search

`WebSearchProvider` is provider-neutral. The no-key default uses Bing RSS for
general discovery and routes explicit GitHub issue queries to GitHub issue
search; DuckDuckGo HTML and explicit provider selection are also supported.
`web_search` is a normal read tool, so it uses existing permission, cancellation,
timeout, tracing, tool exposure, and result projection. Results retain URL,
source, optional publication time, and score. Tool guidance treats snippets as
discovery hints and asks the Agent to fetch a primary source.

Three real-model repetitions compared Web Search OFF (webfetch still available)
and ON:

| Case | OFF success / turns / fetch | ON success / turns / search / fetch | Wall-time result |
|---|---|---|---|
| W1 latest Python API | 3/3 / 5.33 / 4.67 | 3/3 / 4.67 / 2.00 / 1.67 | 121.35s -> 12.92s |
| W2 Pydantic migration | 3/3 / 8.00 / 7.00 | 3/3 / 5.67 / 1.00 / 2.67 | 113.31s -> 26.97s |
| W3 obscure TS1479 | 3/3 / 11.00 / 7.33 | 3/3 / 10.00 / 2.00 / 7.00 | 152.50s -> 329.00s |
| W4 GitHub workaround | 3/3 / 5.00 / 5.00 | 3/3 / 5.67 / 3.33 / 5.00 | 30.77s -> 57.49s |
| W5 local-only | 3/3 / 7.00 / 0 | 3/3 / 7.67 / 0 / 0 | 8.76s -> 9.50s |

Web Search closes the discovery feature gap and materially reduces guessing of
URLs, fetch volume, input tokens, and turns in aggregate. It did not improve
success rate on these intentionally answerable fixtures, and provider/network
latency made W3/W4 slower. The correct decision is to keep it as a normal
permissioned capability, not claim it is universally faster. W5 confirms the
model did not search the web for a local task.

## Persistent process decision

P1 server, P2 watch mode, P3 REPL, and P4 log monitor all demonstrated the same
structural limitation. Bash can stream output during one bounded call and kills
the process group at timeout, but returns no durable handle. After return the
Agent cannot read, write, resize, reconnect, or kill by process identity. The
REPL exits before a later interaction can occur.

Decision: **Persistent Process / PTY is the next core implementation gap**, not
merely a naming difference. This phase intentionally did not implement it.

## Capability assessment

### Versus InfCodeX

- Feature Coverage: core coding verification, recovery, completion gating,
  snapshots, stall handling, Repo Intelligence, retrieval, and Web Search are
  now broadly covered. Core Coding Reliability is mostly aligned.
- Implementation Depth: nzcoder now has a stronger explicit staged evidence
  model; InfCodeX remains deeper in anchor-specific edit recovery and LLM
  sidecar-verifier integration. nzcoder's RI related-test graph is not yet a
  default verification-planner input.
- Behavioral Effectiveness: measured on A/B/E/F/I as 15/15 for nzcoder and
  13/15 for InfCodeX. Reference trajectory efficiency is unavailable, and the
  sample is too small for a general parity percentage.

### Versus OpenCode/infcode-dev

- Feature Coverage: Web discovery is closed. Persistent process/PTY remains a
  clear missing feature. OpenCode also has mature session revert/unrevert.
- Implementation Depth: OpenCode is deeper in persistent process control,
  pre-stream/step snapshots, and user-visible revert. nzcoder is deeper in the
  explicit verification state/evidence completion contract visible in this
  source comparison.
- Behavioral Effectiveness: unavailable because the reference runtime could not
  be installed fairly in this environment. No behavioral parity claim is made.

## Remaining core gaps and next action

1. Design and benchmark a workspace-scoped ProcessService/PTY with create,
   read, write, resize, kill, and reconnect semantics.
2. Before changing verification planning again, add a focused benchmark where
   filename/import test inference demonstrably misses a Repo Intelligence
   related test; only then connect impact graph evidence to the planner.
3. Keep semantic retrieval optional experimental. Do not add vector persistence
   or incremental embedding in response to this phase.
4. Re-run OpenCode comparison when Bun 1.3.13 and its workspace dependencies can
   be installed without modifying the reference project.

## Evidence inventory

- Verification matrix: `.nz-coder/benchmarks/core-reliability-20260812-verification-v2/production-verification-reliability-matrix.json`
- Recovery fault injection: `.nz-coder/benchmarks/core-reliability-20260812-recovery/recovery-fault-injection.json`
- nzcoder reference baseline: `.nz-coder/benchmarks/core-reliability-20260812-reference-nzcoder-v2/nzcoder-reference-comparison-baseline.json`
- InfCodeX matrix: `.nz-coder/benchmarks/core-reliability-20260812-reference-infcodex-v2/infcodex-reference-matrix.json`
- OpenCode blocker: `.nz-coder/benchmarks/core-reliability-20260812-reference-opencode/opencode-infcode-dev-reference-matrix.json`
- Web Search matrix: `.nz-coder/benchmarks/core-reliability-20260812-web-v2b/production-web-search-matrix.json`
- Persistent process decision: `.nz-coder/benchmarks/core-reliability-20260812-process/persistent-process-capability.json`

Validation: `1868 passed`, with seven existing multiprocessing/fork deprecation
warnings; focused post-report checks passed 46/46 and Ruff passed on all files
touched by this phase.
