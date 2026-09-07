# Runtime boundaries — phase 1

## Scope and baseline

Implementation branch: `codex/runtime-boundary-tightening`, based on
`6c1c0e5d16be97f1370f2a40693005fbad3116e9`. This includes accepted recovery
`c26c134` and repository-view consistency `f37e876`. Remote main `89124f9`
does not contain these dependent changes. The root checkout and previous
worktrees remain untouched. No PR, merge, paid Provider or benchmark is part
of this task. This document records the baseline first; acceptance is appended
after the actual implementation and tests.

## Production chain and ownership

CLI/SDK/HTTP request conversion → `execution/composition.py` creates
`ProductRunEnvironment` and `RuntimeServices` → `execution/runner.py`
`AgentRunner` opens `SessionRuntime` and drives the model/tool phases →
`adapters/runner.py` calls the public run-scoped composition factory →
`tool_runtime/pipeline.py` approves the private envelope → `register_batch`
records admitted identities → Session processor/checkpoint → transaction begin
→ scheduler/`ToolExecutor` permission and cancellation checks →
`dispatch_checkpoint` → registered handler → safe `WorkspaceFileAccess`
before/after mutation receipts → execution fact settlement → output guardrail
→ admitted preview settlement → result projection and observations → transaction
commit/compensation → post-write effects → batch hooks → step/checkpoint.

| Boundary / actual code | Responsibility | Not its authority |
| --- | --- | --- |
| `interface/`, `sdk`, HTTP surfaces | Request conversion, interaction, display | File rollback order, recovery SQL, model history repair |
| `execution/composition.py`, `execution/loop.py` | Construct concrete dependencies and bind run resources | New tool admission or transaction policy |
| `execution/runner.py` | Execution phases, model/tool ordering, terminal settlement | Specific UI, Provider implementation, ledger schema |
| `tool_runtime/pipeline.py`, `policy.py`, `result_projection.py` | One ordered tool batch through declared capabilities | Arbitrary host internals or ownership of the file ledger |
| `execution/tool_executor.py`, registered tools | Parse/authorize/dispatch/classify; controlled file access | Transcript commit or batch transaction decisions |
| `session/runtime.py`, `session_revert.py`, `recovery_journal.py` | Authoritative transcript and owned recovery coordination | Frontend-specific simulated rollback |
| `intelligence/service.py` | Published consistent index/graph/cache view | File-system global snapshot or recovery history |

| State | Authority / allowed write entry | Readers / projections | Lifetime / persistence | Failure coordination |
| --- | --- | --- | --- | --- |
| Session transcript | Session/SessionRuntime; Runner and SessionProcessor mutate its live transcript, store checkpoints it | Provider projection, CLI/SDK/HTTP | Session; existing JSON SessionStore | Runner terminal settlement; journal owns Undo/Redo history commit |
| Tool execution fact | ToolLedger via register/dispatch/settle_execution | Recovery context and ToolParts | Attempt; private `tool-recovery/ledger.sqlite3` | Unstarted differs from started/uncertain; never infer execution from output alone |
| Controlled file effect | WorkspaceFileAccess receipt; ledger before/after; TransactionManager compensation; RecoveryJournal replay | ChangeTracker, verification, recovery projection | File/attempt; content objects and ledger | Ownership checks; partial compensation remains diagnostic, no invented reversal of shell effects |
| Batch state | ToolRuntime coordinator | Projector, hooks, Runner action | One batch, in memory | Settle started workers before compensation; preserve the first failure and secondary cleanup failures |
| Task/verification | RuntimeState and VerificationManager observation methods | Completion gates, UI, context evidence | Run/session state projections | Observation of execution precedes transaction finish; committed post-write notification is a separate step |
| Repository generation | RepoIntelligenceService publication boundary | Tools, verification planning | Workspace; existing index SQLite plus live graph/cache | Failed partial update is unavailable; later refresh repairs it |
| Recovery display/model facts | Derived from ToolLedger and journal | First resumed request and frontend | Rebuilt bounded projection/artifact | No raw unadmitted output or fictitious tool result; existing 6,000-character continuation budget |

ContextVar scopes (run settings, workspace, dynamic tools, cancellation and recovery)
are deliberate implicit dependencies, not unscoped singleton authority. Runner/host
bind and restore them; workers copy the context. The service graph does not own
the Provider worker merely because it can call it. Product environment and
ProductionRuntimeHost own Provider/EventBus/session resources and teardown;
the scheduler owns its bounded executor work and settles started side effects.

## Confirmed baseline gaps and design

`ToolLifecycleContext.checkpoint` says `(status) -> Awaitable[None]`, while
the adapter and actual pipeline call `(messages, status)`. The Session port's
`checkpoint(RunContext, status)` is a different layer and remains so.
The same context uses `Callable[..., Any]` for known signatures, and its adapter
silently substitutes no-ops for required transaction/result capabilities.

The async pipeline still imports the host adapter; the sync pipeline directly
reads host fields. `LegacyCodingToolObserver` calls private host post-write
methods. Actual result, transaction and post-write decisions therefore remain
in `execution/loop.py`, not merely in the composition root. `run_evidence` and
the owning ChangeTracker may be replaced during run initialization, so new
bindings must be created after reset rather than caching stale owners.

Chosen approach: keep the existing policy/lifecycle/projection split, give the
critical ports precise signatures and group cohesive execution/transaction/effect
operations. Move their real rules into focused components with explicit owners;
keep legacy conversion outside the tool core. Share policy and completion helpers
between sync and async paths, retaining only necessary I/O differences. Existing
host-based Hooks and unmigrated product services remain explicit one-way bridges.

Rejected alternatives: renaming private callbacks does not remove hidden owners;
a wholesale Runner/Recovery/Session redesign exceeds this phase and risks accepted
behavior. Frozen capability dataclasses do not freeze their referenced owners.

## Behavioral baseline

Command: `python -m pytest -q tests/runtime/test_native_runner.py tests/test_package_structure.py tests/test_architecture_boundary.py --tb=short`.
At `6c1c0e5`: **78 passed in 10.09s, exit 0**. Raw evidence directory:
`/tmp/nzcoder-runtime-boundary.ug3MGN` (`baseline.log`). These existing passing
tests are characterization, not proof of the new boundary or new Windows code.

Correctness steps are direct calls, not best-effort event listeners. Pre-dispatch
checkpoint failure must prevent dispatch; output admission precedes visible result
recording; cancellation must not admit new work and must drain started synchronous
effects. Recovery, permission, conflict, output safety and index contracts are
preserved. Optional trace/display and disabled product enrichment are explicitly
distinguished from mandatory execution/persistence/compensation.

## Implemented boundaries

`ProductRunEnvironment.tool_execution_context` is now a composition operation,
called after run initialization by the Runner's outer execution adapter. It binds
the current `run_evidence`, admission, ChangeTracker and `ToolRunFacts`; it does not
cache copies of these owners before initialization. The policy/lifecycle/projection
contexts remain three focused views, not an immutable copy of the whole run.

| Concrete capability | Actual business rules moved out of the product owner | Authoritative dependencies |
| --- | --- | --- |
| `execution/tool_effects.py::ToolTransaction` | Commit versus compensation and rollback report projection | Existing TransactionManager; no replacement transaction state |
| `ToolResultRecorder` in the same module | Tool counters, verification/task observations, child cost/evidence and strict completion decision | ToolRunFacts, VerificationManager, RuntimeState, RunEvidence, admission/lineage, scratchpad/skills |
| `CodingWriteEffects` in the same module | Successful-write path selection, patch-risk update, index refresh and LSP result attachment | ChangeTracker, task/evidence/recovery owners; accepted index service helper |
| `tool_runtime/operations.py::HookedToolExecutor` | Existing pre-tool hook rejection and execution timing | Declared executor plus explicit outer hook callback |
| `session/tool_progress.py::ToolSessionBoundary` | Processor lookup, metadata/question conversion and owned progress persistence | Session checkpoint callback and event publisher; no product object |
| `process/tool_snapshots.py::ToolStepSnapshots` | Optional step snapshot/patch conversion and bounded summary projection | Existing WorkspaceSnapshotStore; not the tool recovery authority |
| `tool_runtime/observers.py::CodingToolObserver` | Ordered committed effects, batch hooks and approved plan-mode application | Focused effects/snapshot services and the declared batch-hook bridge |

The old product methods remain compatibility delegates where needed. Native tool
batches do not call `_record_tool_result`, `_finish_tool_transaction`,
`_refresh_patch_risk`, `_refresh_code_index`, `_attach_lsp_write_diagnostics`,
`_trace_tool_result`, `_execute_tool_call_with_hooks`,
`_strict_verification_completed`, `_apply_pending_plan_mode`, `_record_step_patch`,
`_processor_for_latest_assistant`, `_tool_metadata_callback` or
`_question_lifecycle_callback`. A real SDK test makes these paths fatal during
the batch and verifies actual files, JSON history and SQLite receipts across two
concurrent workspaces. It also checks that injected owners are the current objects.

Production batch coordination imports neither the host adapter nor execution/UI
facades. Explicit legacy entry points adapt at `adapters/tool.py` and retain the
caller-selected ToolRuntime (including custom policy/projector implementations).
They do not silently replace it with a default runtime. Optional enrichment may
use `NoOpToolObserver`; persistence, execution and compensation may not.

## Precise contracts and execution semantics

`core/tool_contracts.py` reuses ToolExecutionResult, SessionProcessor and the existing
handoff type. `ApprovedToolBatch` is the original admission envelope moved to the
contract layer and re-exported for compatibility, not a competing tool result.

| Port | Contract / mutation | Failure and repeat behavior |
| --- | --- | --- |
| Tool checkpoint | `(messages: list[dict], status: str) -> Awaitable[None]`; Native adapter validates the live RunContext transcript then calls SessionRuntime | Failure before dispatch prevents execution; stable checkpoint may repeat, but does not replay a tool |
| `drain_progress` | `() -> Awaitable[None]`; wait for every owned, serial progress save | Required even for `finish_step=False` and external Runner checkpoint callbacks; first failure propagates after all started saves settle |
| Executor | `execute_one(tool_call: dict, index: int) -> ToolExecutionResult` | Existing permission/path and execution-ledger checks remain; handler dispatch failure differs from a nonzero shell exit |
| Transaction | `begin()`, `active`, `finish(has_write, all_succeeded, messages) -> None` | Partial compensation stays active; it is not reported as success or evidence the tool never ran |
| Output admission | async `(call, result, messages) -> ToolExecutionResult` | Guarded output precedes preview/history/observation publication; escalation remains owned by the Runner's atomic policy boundary |
| Result recording | `(ToolExecutionResult) -> bool` where true means dispatch failure | Records execution observations, not proof of committed file effects; nonzero shell exit alone does not trigger file rollback |
| Committed effects | `post_write(dispatched, messages) -> None` | Runs only after successful transaction finish; failure cannot relabel or automatically replay the committed tool |
| Batch | Declared ToolExecutionContext plus raw calls/live messages; returns `continue`, `stop` or `terminal` | Both drivers use the same normalization, settlement, completion/usage projection and cleanup helpers |

The synchronous entry rejects an already-running event loop **before** batch
registration, not after side effects. Async workers retain the existing settled
thread bridge: cancellation stops new work and waits for started synchronous work.
The pipeline then compensates before an interrupted checkpoint; persistence failure
must not skip compensation. Failed transaction begin is included in this boundary.
Cleanup preserves the original exception and logs independent cleanup failures.

Progress saves never block a tool worker waiting for another task in the same
thread pool. They have a serial persistence lock and tracked futures. Direct
checkpoints retain the same lock until their Store operation settles, even when
the Store internally uses `asyncio.to_thread`. External tool-checkpoint callbacks
also retain their started save across cancellation. Drain observes all started
saves after failure or repeated cancellation; a forgotten old `running` save
cannot overwrite a later stable state. This guarantee is scoped to this tool
boundary, not a rewrite of every SessionRuntime caller or crash recovery.

Tool-provided progress titles and metadata are not admitted output: before
`after_tool`, the Session boundary emits only a structural running heartbeat.
For output rewrite/block, the guardrail owner discards old metadata/body aliases,
titles and attachments. A successful non-error rewrite retains only validated
tool-specific control facts (Plan approval/terminal flags, graph-approved handoff
identity, finite nonnegative child cost); handoff summaries use the new admitted
body. Block/error rewrite does not re-enable those control signals. Ordinary
allow retains existing final diagnostics. This avoids both premature disclosure
and deleting Plan/handoff control merely because its displayed text changed.

## Explicit migration exceptions

- `execution/tool_assembly.py::ProductToolBridges` still calls existing host-based
  Hooks, guardrails, input-media, transition/notification and prompt-budget APIs.
  These were not reimplemented as a new Agent core. The tool coordinator cannot
  inspect the bridge's environment or obtain arbitrary services through the context.
- `CodingWriteEffects` receives the cached/lazy `_project_profile_data` reader from
  composition. This is a remaining read/cache dependency, not delegation of the
  patch-risk or committed-write decision back to the host.
- `adapters/tool.py` retains a named non-product legacy observer/transaction adapter
  for old callers. Required checkpoint, execution and transaction capabilities must
  be supplied; tests that used incomplete hosts are migrated, not accommodated with
  new production no-ops. Legacy progress persistence is synchronous. Native saves
  use SessionRuntime and never the legacy FileSessionRepository fallback.
- The product owner still contains compatibility methods and unmigrated Runner,
  Provider, memory, planning and terminal responsibilities. This phase does **not**
  claim the whole product environment is host-free.

## Test design and diagnostic evidence

`tests/runtime/tool_runtime/standalone/` overrides the old global feature-disabling
fixture with user-directory isolation only. Module tests need explicit policy,
executor, checkpoint/progress, transaction, projection and optional observer ports;
they do not construct AgentLoop or need models/MCP/LSP/watchers. The common contract
table runs both synchronously and asynchronously against small declared test ports
and real ToolExecutor + JSON SessionStore + SQLite recovery scopes. Real file writes
use the existing safe file tools and transaction/ledger bindings.

Coverage includes read/write/edit, denial, pre-dispatch checkpoint failure, executor
exceptions, output rewrite/block, repeated cancellation with a started write,
partial real compensation, post-write observer failure, mixed execution states and
concurrent Native SDK identity/owner isolation. Recovery and product tests remain
in place, including first resumed Fake Provider request, Undo/Redo and Windows tools.
The common cases register ToolParts before dispatch (the real Runner prerequisite)
and assert their running/settled/interrupted states. Real JSON Store tests block a
frozen `running` snapshot inside `_save_sync`, cancel twice, then verify both the
Store and on-disk JSON end at `interrupted` with zero active workers. In-process
removal of each settled-await boundary makes the corresponding regression fail.

Architecture tests inspect imports (including local/type-only, relative and literal
dynamic imports), reject reverse facade dependencies, and launch an isolated import
probe that does not load ProductRunEnvironment. The behavioral SDK spy complements
these static constraints; changing a variable from `host` to `owner` cannot satisfy it.

`python tests/typecheck/check_tool_contracts.py` checks the actual focused modules,
production composition, real ports and test dependencies with basedpyright. It then
requires rejection of a status-only checkpoint and an executor returning a string.
The checker is a pinned CI development tool, not a new runtime dependency.

A failure-injection example raises ValueError at tool execution and OSError during
compensation. The original ValueError remains the raised cause. The existing tracer
receives `tool_batch_failed` and `tool_batch_cleanup_failed` with Session/interaction/
assistant-step/call identities, the failure stage, primary and secondary exception
types, and transaction activity. The associated ledger answers whether the tool ran
and whether its file effects were compensated. No raw arguments, unadmitted output,
exception strings or private sentinel values are added to these diagnostics.

## Intermediate validation and retained failures (2026-09-07)

Frozen-source focused validation: 104 tool/permission/cancellation tests passed;
81 Native Runner/package/architecture tests passed; positive production type checks
and both negative contract fixtures passed. Ruff, compileall and diff checks passed.
These are Linux observations, not a Windows claim. Intermediate required suites
passed **426 tests, exit 0** (`required-accepted.log`). The wheel and sdist built;
a clean venv outside the checkout loaded `nz_coder` from `site-packages` and ran
the actual Native SDK two-workspace fake-transport write/Session/SQLite contract
successfully (`installed-native.log`), without disabling product features.

The first full recovery/Undo/HTTP run reported **2 failed, 239 passed**, exit 1.
Both failures point to the old test-only `Harness()` argument at
`tests/recovery/test_model_recovery.py::test_unstarted_batch_tail_reaches_first_request_as_cancelled_not_process_lost`.
It must now construct the declared tool context while retaining its cancellation
and first-request assertions. This failed run is retained as
`recovery-http-frozen.log`. The first complete suite reported **21 failed,
4001 passed, 35 skipped, exit 1** (`full-first.log`). Failures included incomplete
old host test fixtures, evaluation guardrail fixtures missing the now-declared
sync method, an outdated sync-host architecture assertion, and the six diagnostic
sites moving from the host to their actual owners. Fixture migrations preserve
their original assertions; the diagnostic inventory retains all six sites.

Independent review reproduced two inherited holes in the new owner: cancellation
could retire a direct save too early, and progress metadata could expose output
before admission. Both now have real-component regressions. The first output
redaction fix over-cleared control metadata; a real Guardrail+Projector regression
failed in four success-rewrite cases before the typed control projection fixed it.
The final independent scoped review ran 49 tests successfully and re-ran its
original counterexample; this is not a claim of whole-repository independent audit.

Earlier interrupted diagnostic runs and the initial metadata-test fixture failure
(missing real Bash registration) remain in the local evidence directory, not
counted as passing runs. Adding the async helper to scoped type checking also
exposed the existing `Awaitable` → `asyncio.run` mismatch in its background bridge;
the bridge now wraps the awaitable in a real coroutine, and positive production
bindings plus both negative fixtures pass. Full-suite and native Windows results
for the final source revision are still pending.

The initial scoped typing expansion also introduced direct imports of concrete
Recovery/Stall classes into core, caught by the existing architecture test. Core
now declares only their small observation ports; actual RecoveryState and
StallSidecarOrchestrator bindings are type-checked without weakening the rule.

The next complete frozen run reported **3 failed, 4039 passed, 35 skipped**, exit 1
(`full-accepted.log`). Two exact Question metadata assertions exposed an unwanted
new UI flag; the third required Bash running-result metadata. The fix removes the
flag, and emits output-bearing running metadata from the result projector only
after guardrail/ledger admission, before the completed part. All three original
assertions remain unchanged; an independent check of these and the output-admission
cases passed 18 tests. The final full run is `full-release.log`, not either of the
earlier failed or interrupted runs.

Additional pure-module cases share one ToolRuntime across two in-memory RunContexts,
and cancel a mixed admitted/rejected batch without a SessionStore/SQLite owner.
They assert separate identities/policy/counters/results, real ToolPart states,
drained started workers and compensation order. Injecting shared counters or
skipping compensation makes the corresponding regression fail.

## Frozen-source local acceptance

Production extraction commit: `2626d39`; contract/CI commit:
`08c3c071ff60a503c7bc4a6a1f10e730554cbd8f`. Production tree:
`45aea5b0d3d5111f0199ec253615f7d618f21dea`. No runtime, test or workflow edits
occurred during the final run. The following documentation commit changes only
these records, not the tested implementation. No paid Provider or SWE run occurred;
the complete pytest suite retains its existing offline evaluation fixtures.

| Actual command / evidence | Result | Exit |
| --- | --- | --- |
| `env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 PYTHONUTF8=1 python -m pytest -q --tb=short` (`full-release.log`) | **4044 passed, 35 skipped**, 536.98 s | 0 |
| `env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 PYTHONUTF8=1 python -m pytest -q tests/runtime/test_native_runner.py tests/test_package_structure.py tests/test_architecture_boundary.py tests/runtime/tool_runtime tests/recovery tests/test_session_revert.py tests/test_http_service.py --tb=short` (`required-release.log`) | **428 passed**, 47.50 s | 0 |
| `python -m pytest -q tests/test_loop_fake.py tests/test_session_events.py tests/runtime/tool_runtime --tb=short` (`metadata-compatibility-green.log`) | **265 passed**; before the final two pure-module additions, otherwise identical production source | 0 |
| `python tests/typecheck/check_tool_contracts.py` (`typecheck-release.log`) | 0 positive typing errors; both deliberately incompatible port fixtures rejected | 0 |
| `python -m ruff check nz_coder tests` | All checks passed | 0 |
| `python -m compileall -q nz_coder tests` | Compiled | 0 |
| `git diff --check` and staged diff check | Clean | 0 |
| `python -m build --wheel --sdist --outdir /tmp/nzcoder-runtime-boundary.ug3MGN/dist-release` (`build-release.log`) | Wheel and sdist built | 0 |
| Clean venv installation of `dist-release/nz_coder-0.1.0-py3-none-any.whl`, outside checkout (`fresh-release-install.log`) | Installed wheel, not editable source | 0 |
| Installed `nz-coder --help`, `doctor --repo-intelligence-only --json`, `config show --json` | CLI/config/structural diagnostics successful | 0 |
| Installed Python runs `/tmp/nzcoder-runtime-boundary.ug3MGN/installed_native_smoke.py` (`installed-native-terminal-release.log`) | Actual Native SDK: two runs completed; isolated writes, committed receipts, JSON history, no retired private batch methods; package origin is `site-packages` | 0 |

The installed smoke reuses the committed native-wiring test body with stdlib
`unittest.mock` patch scopes (only the model transport is fake), adds explicit
`RunStatus.COMPLETED` assertions, and creates/deletes only its own temporary
workspaces. It runs with an empty inherited credential environment and isolated
user-state directories. It does not disable reflection, LSP or other product flags.

Commit responsibilities so far: `2520f08` baseline/ownership document;
`8001016` checkpoint declaration and red/green characterization;
`2626d39` actual production owners plus required old-fixture migrations;
`08c3c07` standalone/real/native regressions, architectural constraints and CI/type
checks. Current-source remote/Windows evidence will be appended after branch push.

## First remote acceptance and Python 3.10 test correction

The first push was verified as
`ab67968ae72d3cab0483d44f144101e8e2599d3e`. The production tree is still
`45aea5b0d3d5111f0199ec253615f7d618f21dea`. These are this branch's actual
jobs, not the previous recovery/index branch results:

| Workflow / actual job | Result on `ab67968` |
| --- | --- |
| [Core Runtime](https://github.com/violetnozomi/coding-agent/actions/runs/34076897825), Python 3.12 job `101604689759` | Full `python -m pytest -q`: **4043 passed, 36 skipped**, 795.11 s, exit 0; real scoped typing, Ruff, compile, CLI and wheel/sdist also passed |
| Same workflow, Python 3.10 job `101604689669` | **2 failed, 282 passed**, 482.37 s, exit 1; subsequent CLI/build steps skipped, so the workflow as a whole failed |
| Same workflow, installed-wheel job `101604689681` | Build, clean source-external install, CLI/doctor/config/package-resource checks passed |
| [Windows Product RC](https://github.com/violetnozomi/coding-agent/actions/runs/34076897831), native job `101604689751` | **669 passed, 20 skipped**, 302.20 s, exit 0; actual command included `tests/runtime/tool_runtime`, architecture, recovery, security and index consistency; subsequent wheel/sdist and source-external fresh-install smoke passed |
| Same workflow, Linux product job `101604689858` | **151 passed**, 34.06 s, exit 0; CLI/build passed |
| [Repo Intelligence](https://github.com/violetnozomi/coding-agent/actions/runs/34076897816) | Consistency 31, native parser 6 and fallback 1 tests passed, exit 0 |
| [Windows Installer](https://github.com/violetnozomi/coding-agent/actions/runs/34076897804) | 9 installer contracts passed; frozen executable build and silent install/upgrade/product/uninstall checks passed |

Logs: `ci-core-first-ab67968.log`, `ci-py310-first-ab67968.log`,
`ci-windows-ab67968.log`, `ci-repo-ab67968.log`, `ci-installer-ab67968.log`
in the same local evidence directory. The failed Python 3.10 run is retained;
it was not rerun at that SHA to hide the failure. A transient GitHub API TLS
timeout occurred while retrieving status, not during these test jobs.

Both Python 3.10 failures were the new cancellation identity assertion at
`standalone/test_failure_boundary.py`. A stdlib-only local Python 3.10.20 probe
confirmed that direct `await` preserves the original CancelledError object,
while `asyncio.run` recreates it and retains the original through `__context__`.
The unchanged test reproduced **2 failed, 12 passed**, exit 1 locally
(`python310-cancel-red.log`). No production bridge change is necessary.

The test-only correction catches at the actual asynchronous API boundary and
keeps the exact original-object assertion. At the synchronous API, it follows
only CancelledError context links with cycle protection back to that original;
other exception types and lost causes still fail. All transaction/execution
assertions remain, and cleanup error type and primary error type are additionally
asserted. No skip was added. Both Python 3.10 and 3.13 then passed the 14 fault
tests, exit 0 (`python310-cancel-green.log`, `python313-cancel-green.log`).
The complete tool boundary plus architecture selection on Python 3.10 passed
**110 tests**, 14.56 s, exit 0 (`python310-boundary-green.log`).
The exact combined required-suite command from the local acceptance table was
rerun after this correction: **428 passed**, 96.29 s, exit 0
(`required-py310-correction.log`). Ruff, compileall and diff checks also passed.

An independent reviewer reran the 14 tests on Python 3.10 successfully. Two
in-memory mutations, without modifying production files, each produced four
expected failures, pytest exit 1: skipping compensation; replacing the primary
error with a checkpoint OSError. Logs are `reviewer-py310-skip_compensation.log`
and `reviewer-py310-mask_original.log`. Thus the correction does not remove the
original-failure or compensation guarantees. Final corrected-commit CI is pending
the next push; the first Core workflow is not reported as green.

Native Windows here means GitHub's real Windows runner, not a Linux platform mock
or a manual Windows desktop UX session. Existing HTTP/watcher historical failures,
the documented recovery limitations and `actions/upload-artifact@v4` Node-runtime
deprecation warnings remain separate; this phase does not silently close them.

## Corrected-contract CI and real-component readiness regression

Remote SHA `068f0be340686a09aa271335372dd0eb52d251ca` retained the identical
production tree. [Core Runtime](https://github.com/violetnozomi/coding-agent/actions/runs/34077830868)
passed: Python 3.12 job `101607305847` completed **4043 passed / 36 skipped**,
713.94 s, exit 0; Python 3.10 job `101607305999` completed **284 passed**,
488.93 s, exit 0. Scoped positive/negative typing, compile, Ruff, CLI, distribution
build and installed-wheel checks succeeded. Repo Intelligence `34077830927` and
Windows Installer `34077830928` also succeeded.

The [Windows Product RC](https://github.com/violetnozomi/coding-agent/actions/runs/34077830916)
native job `101607306016` instead recorded **1 failed / 668 passed / 20 skipped**,
483.53 s, exit 1. Its subsequent wheel/fresh-install step was skipped. The failing
test was this phase's
`test_real_cancel_drains_started_write_and_distinguishes_mixed_execution_facts`,
waiting three seconds for the third tool's completed write. This is not silently
classified as the historical HTTP/watcher failure. Raw job log:
`ci-windows-068f0be-job.log`; Core job logs: `ci-core-068f0be-runtime.log` and
`ci-core-068f0be-python310.log`. This failed workflow was not rerun at that SHA.

The test had two established synchronization weaknesses: its readiness wait used
`asyncio.to_thread`, consuming the same default pool required by real Store/dispatch
work; and it did not observe a batch error before readiness, reporting only False.
Its three-second limit also included four ledger registrations, a JSON checkpoint,
permission settlement and two complete file writes with ledger settlement and Git
status subprocesses. No product contract guarantees this startup latency.

Adding a single-worker parameter to the unchanged wait reproduced a deterministic
failure locally: **1 failed / 1 passed**, exit 1 (`worker-readiness-red.log`). The
Windows log alone does not prove that its pool was exhausted; this local reproduction
proves the test's unsafe synchronization, not the runner's unobserved resource state.

The test now receives a loop Future notification from the actual completed write
via `call_soon_threadsafe`, races that notification with early batch termination,
and propagates a real early exception. A separate bounded deadlock watchdog remains.
The finally path always releases and drains the batch before closing ledger/transaction
scopes, including when setup or an assertion fails. Both original file and execution
facts, repeated-cancellation and compensation assertions remain. No product feature
or path/permission check was disabled, and production source is unchanged.

The default- and single-worker cases then both passed, exit 0
(`worker-readiness-green.log`). Python 3.10's complete tool/architecture selection
passed **111 tests**, 7.08 s, exit 0 (`python310-readiness-green.log`). Ruff,
compileall and diff checks passed. This test-only correction needs a new native
Windows run; neither prior failed workflow is erased or relabeled.

After this correction the exact combined required-suite command above completed
**429 passed**, 45.23 s, exit 0 (`required-readiness-correction.log`). Independent
review reran both worker cases successfully and used in-memory mutations to skip
file compensation and omit the ledger compensation fact separately; both failed
at the corresponding unchanged file/ledger assertions, exit 1. The final complete
Linux run is `full-readiness-final.log`; its result and current-code remote results
will be appended after completion, without changing the frozen runtime or tests.
