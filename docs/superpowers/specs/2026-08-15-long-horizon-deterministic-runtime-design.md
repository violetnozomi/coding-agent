# Long-Horizon Deterministic Runtime Design

Date: 2026-08-15

## Goal

Close the runtime failures exposed by the third real cron-engine run without
adding another planner, model call, prompt layer, or repository-intelligence
system. The product must settle every terminal boundary deterministically,
reserve calls 16-20 only for evidence-backed repair, preserve shell exit status,
diagnose stale subprocess workspaces, and bootstrap requirement artifacts before
semantic indexing is ready.

## Confirmed failure mechanisms

1. `AgentRunner` executes the exact verification contract only when a model turn
   returns natural language without tools. A final tool batch can consume the
   hard cap and fall directly into `max_turns`, leaving the contract at zero
   attempts.
2. `WorkBudgetController` labels calls 16-20 as `emergency`, while
   `RuntimeState.closure_phase_action()` restricts only `closure_repair` and
   `closure_finalize`. Emergency therefore restores broad exploration.
3. Bash publishes a structured exit code, but `ToolExecutor` reconstructs
   `command_failed` from rendered text. Pipelines can hide the failing command
   unless Bash runs with `pipefail`.
4. Recovery only recognizes `CompletedProcess` plus `No module named`. A stale
   fixture can import successfully from an old `cwd`, so no import error exists
   to trigger the current rule.
5. Task contracts depend on explicit paths or repository-intelligence
   candidates. Natural-language product tasks can consequently start with no
   artifact evidence, and acceptance alone can over-satisfy artifact-oriented
   requirements.

## Considered approaches

### A. Unified deterministic boundary and focused resolvers (selected)

Keep orchestration in the native runner, but introduce small deterministic
components for boundary decisions, bootstrap artifacts, and subprocess drift.
Reuse the existing verification contract, requirement ledger, work budget, and
tool-result metadata. This fixes the state transitions at their owners and does
not add provider calls.

### B. Patch each terminal branch inline

Duplicating exact-acceptance and completion checks in streamed tools, buffered
tools, natural completion, and loop exhaustion would be quick but unsafe. Those
branches would drift again, and evidence persistence would remain order
dependent.

### C. Add a second orchestration engine

A new planner or state machine could own long tasks, but would duplicate the
native runtime and widen the Native/Legacy product split. It is outside this
repair scope.

## Architecture

### 1. Terminal boundary settlement

`AgentRunner._settle_terminal_boundary()` becomes the only path that decides
whether a boundary may complete, continue, enter bounded emergency, or end as
`max_turns`.

It is invoked after:

- a natural no-tool response;
- a streamed tool batch;
- a buffered tool batch;
- the last loop iteration before generic exhaustion.

The settler performs, in order:

1. execute a due exact acceptance command for the current mutation generation;
2. persist verification-contract and requirement-ledger evidence;
3. evaluate exact acceptance freshness and all hard requirements;
4. run the existing completion gate for natural completion;
5. at the nominal boundary, either authorize bounded emergency or settle;
6. at the absolute cap, complete only when exact acceptance passed for the
   current generation and the ledger is fully satisfied.

An exact PASS never substitutes for missing documentation, artifact, or test
mutation evidence. A tool-call boundary with complete evidence receives a
deterministic, fact-based summary if the model emitted no usable final text.

### 2. Work budget and bounded emergency

The fixed product budget is:

- calls 1-13: `normal`;
- call 14: `closure_repair`;
- call 15: `closure_finalize`;
- calls 16-20: `bounded_emergency`;
- after call 20: `hard_cap`.

The nominal product SLA remains 15 coding calls and the absolute safety cap
remains 20. Bounded emergency is entered only when all of the following are
true:

- a diff exists;
- failure evidence exists;
- a repair target is known;
- broad exploration is not required.

The emergency tool policy allows known-path reads, known-target writes, diff
inspection, targeted verification, and the exact acceptance command. It blocks
directory/glob/repository-wide search, semantic search, child agents/workflows,
package installation, environment exploration, unrelated new paths, and broad
probes. Blocked package-install and broad-emergency attempts are counted in the
runtime trace.

### 3. Structured shell outcomes

For canonical `ToolOutput`, `metadata.exit` is the source of truth for
`command_failed`. Rendered output remains a presentation concern and is used
only as a compatibility fallback for legacy string handlers.

POSIX Bash executes `bash -o pipefail -lc <command>`. POSIX `sh` retains
`sh -lc`; when a verification command is piped under `sh`, NZ-Coder rejects it
with an actionable error because reliable upstream status is unavailable.

### 4. Subprocess workspace drift diagnosis

A deterministic AST diagnostic reads the failing test helper and recognizes
`subprocess.run`, `Popen`, and `check_*` calls that launch
`python -m <package>`. It resolves only safe static `cwd` forms:

- string constants;
- `Path("...")`;
- `Path(__file__).parent`;
- `Path(__file__).resolve().parents[n]`;
- module-level assignments composed from those forms.

No Python expression is evaluated. If the resolved subprocess directory differs
from the active workspace, recovery emits `subprocess_workspace_drift` with the
helper, stale directory, active directory, package, and the exact category of
fix. It explicitly avoids recommending package installation or production-code
changes.

### 5. Bootstrap artifacts and ledger semantics

`BootstrapArtifactResolver` scans a bounded set of workspace paths without a
model or semantic index. Confidence ordering is:

1. explicit user paths: 1.00;
2. unique basename/stem matches: 0.95;
3. `test_<surface>` or `<surface>_test`: 0.90;
4. explicitly requested README/docs: 0.95;
5. repository-intelligence candidates above its existing threshold;
6. entry points and package facts as soft candidates.

Resolved high-confidence paths populate task-contract requirements. Distinct
test files become distinct test requirements so each requires its own mutation
evidence. Soft paths enter the implementation bundle but do not become required
artifacts merely by being candidates.

Acceptance may directly satisfy artifact-free `behavior`, `compatibility`, and
`verification` requirements. It cannot directly satisfy `docs`, `artifact`, or
`test`; those kinds require matching mutation evidence before verification can
promote them.

## Trace and metrics

Terminal settlement emits the boundary kind, decision, exact-contract attempt,
ledger unresolved IDs, and emergency eligibility. Runtime summaries retain
provider attempts, tokens, and duration by purpose (`coding`, `planning`,
`replanning`, sidecar/router purposes when present), plus package-install and
emergency-broad-exploration counters.

## Local acceptance gates

- G1: a hard-cap final tool edit executes exact acceptance once, records ledger
  evidence, persists terminal state, and can complete only with full evidence.
- G2: failed hard-cap acceptance ends as `max_turns` or verification failure,
  never `completed`.
- G3: call 16 blocks broad tools, tasks, package installation, and repository
  grep while allowing known reads/edits.
- G4: `python -c "raise SystemExit(3)" | tail -1` and a failing pytest pipeline
  produce `command_failed=true`.
- G5: a stale fixture reports `subprocess_workspace_drift` with both directories,
  helper, and no package-install recommendation.
- G6: the cron task resolves nonzero artifacts and candidates including parser,
  requested tests, and nested README.
- G7: unchanged README plus passing pytest leaves the README requirement
  unsatisfied.

## Paid-test gate

The fourth real DeepSeek cron run is permitted only after G1-G7, focused tests,
the full local suite, lint, and diff inspection pass. Success requires at most 15
coding calls, at most 25 tools, independent cron tests passing, `completed`
status, a factual nonempty summary, bootstrap and bundle trace activation,
nonzero artifacts/candidates, at least one exact verification attempt, all hard
requirements satisfied, zero package-install attempts, and zero emergency broad
exploration.

## Explicitly out of scope

This phase does not add a Luna router, DeepSeek harness/code mode, another
repository-intelligence implementation, a new prompt system, a complex planner,
or any additional provider request.
