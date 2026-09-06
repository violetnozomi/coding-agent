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
`adapters/runner.py` currently creates tool capabilities →
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
