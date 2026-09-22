# Agent Core Diagnostic Suite v1: offline instrument validation

## Baseline and scope

HEAD/origin/main before this work: `f6cfa381fed47d9a82fd4e11a20c440a72b1512d`.
Working tree was clean. Latest production change remains
`265b3a30284c7c8ca6042ef0faae7e17e48d1d39`. **No nz_coder changes.**
No existing evidence was rewritten. **0 paid model requests**; no NZ Agent,
InfCodeX, main/auxiliary Provider, embeddings or online evaluation was executed.

## Three orthogonal instruments

### A — Unknown-location Cross-Module Correlation Propagation

Diagnostic hypothesis: locate the public behavior and propagate an optional ID
through a model/service/two sinks/CLI without being given a file-edit list.
Initial workspace has 14 files, including 8 substantive business modules, two
package initializers, three test files and README. Oracle changes five business
modules plus README and adds a test file. No new framework/dependencies.
Healthy trajectory: locate -> trace callers -> implement -> check compatibility
and sinks -> tests/docs -> semantic closure. This is a capability description,
not a prescribed tool sequence. Failure classes: repository-location,
caller-propagation, verification, documentation omission, terminal failure.

### B — Failure-Driven FIFO Retry Recovery

Diagnostic hypothesis: actual failing boundary checks can drive a targeted repair
of per-job retry classification, attempts, final error identity and fairness.
Initial workspace has 11 files; oracle changes worker/queue, adds policy, updates
two tests and README. A single worker and scripted outcomes give deterministic
FIFO traces, including jobs enqueued during a failed attempt. No thread races,
random sleeps, clocks or historical Q pool/cancellation design.
Healthy trajectory: receive failure -> diagnose -> focused edit -> retest current
generation -> close. Failure classes: evidence loss, blind retry, wrong repair,
repeated no progress, verification binding. A real first attempt may pass; this
suite does not force a failure, review call or recovery event.

### C — Long-Horizon Configuration Migration

Diagnostic hypothesis: preserve authoritative requirements and close multiple
read/write/validation/CLI/consumer/docs obligations across a longer workflow.
Initial workspace has 24 files, including 18 small Python modules, four tests,
CONFIG_SPEC.md and docs/README.md. Oracle changes six business modules, adds one
test file and updates docs; it does not change the specification.
Healthy trajectory: discover -> read authority -> inspect parser/writer/callers ->
mutate -> verify -> docs -> verify current version -> semantic closure.
Failure classes: authority loss, compaction/context, omitted obligation, semantic
review conflict, false completion. No artificial text padding and no forced
compaction. No Money output contract or Storage CAS/revision fixture is reused.

## Initial, oracle and clean-repeat results

| Task | Initial project tests | Initial independent | Oracle project tests | Oracle independent | Clean-repeat |
|---|---:|---:|---:|---:|---|
| A | 3 passed | 1/9, exit 1 | 5 passed | 9/9, exit 0 | 5 tests; 9/9 |
| B | 2 passed | 3/9, exit 1 | 5 passed | 9/9, exit 0 | 5 tests; 9/9 |
| C | 5 passed | 1/12, exit 1 | 8 passed | 12/12, exit 0 | 8 tests; 12/12 |

Initial failures are unmet new requirements; baseline projects run/import/test
successfully. Each check has a name, exit, timeout, stdout/stderr and requirement
mapping. No failures are hidden as one aggregate pytest exit. See each task's
validation.json, initial-acceptance.json, oracle-acceptance.json,
baseline-tests.json, oracle-tests.json, repeat-acceptance.json and oracle-diff.patch.

Two independently materialized initial workspaces have identical hashes; the
oracle overlays then yield identical source hashes and pass/fail outcomes.
Wall-clock duration and temporary test output are not claimed byte-identical.
Version snapshot + acceptance leaves the supplied workspace unchanged.

## Requirements and evaluator boundary

Fixtures live at tests/evaluation/fixtures/agent_core_diagnostic_v1. Only workspace/
is copied to future Agent workspaces, and only task.md becomes the user request.
Acceptance, oracle, manifests and requirement maps remain evaluator-side.
Every named check maps to a verbatim clause in task.md or CONFIG_SPEC.md. No known
hidden requirement was added. Docs checks are narrow example/API token checks,
not a claim to understand all prose; semantic final-report/test-quality review
remains necessary in future real runs.

C's task classifies CONFIG_SPEC.md as task_reference/task_spec with required=false.
The existing RuntimeState captures full original text/hash before mutation, and a
later workspace mutation cannot change it. authority-preflight.json records the
actual offline facts. Oracle preserves the initial spec hash. No transcript or
model_observed claim is manufactured: there was no model read in this phase.

## Reused infrastructure, isolation and time bounds

The local fixture validator uses existing offline_exec.py and reference_adapter
workspace hashing. Previous Q capture.py/project.py provide the future production
capture pattern; no new Agent execution or benchmark framework was introduced.
IPv4/IPv6 denial is inherited across exec and grandchildren. Subprocesses receive
an allowlisted environment and temporary HOME/TMPDIR, with no Provider transport,
proxy or secrets. There is no dependency installation. Network isolation is not
a filesystem jail: future Agent execution must remain scoped to its own workspace.

Every independent check has an 8-second process-group deadline; project tests have
30 seconds; post-kill reap has 5 seconds. B asynchronous checks use a 2-second
completion guard so unresolved promises cannot silently exit successfully; Node
project tests use a 1-second guard. No correctness assertion depends on elapsed
time. Timeout, credential filtering, child network denial and symlink rejection
are explicitly regression-tested.

Plausible incomplete implementations are rejected: A drops correlation data;
B pushes retries to the front; C ignores CLI dry-run. Reverting just docs also
fails the independent documentation check for each task. These are evaluator
sensitivity tests, not model results or Core RED tests.

## Actual commands and results

Commands and full outputs are in commands.jsonl and the corresponding JSON logs.
The reproducible entrypoint is:

```sh
python docs/evidence/agent-core-diagnostic-suite-v1-2026-09-22/verify.py
```

It executed:

1. Existing offline launcher -> validate.py validate all: exit 0; all initial,
   oracle and clean-repeat expectations above passed.
2. Offline pytest tests/evaluation/test_agent_core_diagnostic_v1.py: exit 0,
   **27 passed**.
3. Offline pytest tests/evaluation tests/architecture
   tests/test_architecture_boundary.py tests/runtime/test_task_reference_evidence.py:
   exit 0, **160 passed / 1 skipped**. The opt-in paired entrypoint was not enabled.
4. Ruff on fixture/helper/new test/verification script: exit 0, all checks passed.
5. In-memory compile of **50 Python files**: exit 0, no cache writes. Public
   test runs supply actual imports; Node project tests supply JS parse/execution.

Before the final schema/docs/snapshot checks were added, earlier development
focused runs returned 20 passed, then 27 passed. No fixture/Core test failure was
silently reclassified as a historical problem.

## Future capture and stopping policy

causal-table.schema.json includes request purpose/trigger/tools/permissions,
mutation/verification generations, acceptance versions, ledger/reference,
recovery/stall/review/terminal facts and raw Provider usage. Each task's actual
table is empty. first_divergence.json is unobserved with classification=null,
not "no divergence". Manifest metadata is not a model hint.

Future permissions allow normal workspace tools, project tests and safe documented
local commands with production guards, prohibit shell composition/network/secrets
and evaluator files. protocol.json defines the proposed argv surface. Paid launcher
wiring and model/request budgets must be frozen in a separate authorized preflight;
this suite contains no route that launches a real Provider.

Future sequence, only after separate authorization: one A -> freeze/analyze; then
one B; then one C. Never auto-resample. At a deterministic Core break, freeze
evidence and stop later tasks if they add no causal value. Build an offline RED in
a separate repair phase. Never patch a running experiment, force review or compute
an aggregate success rate from these three tasks.

## Remaining limitations and next action

No real trajectory, recovery, long-context compaction or termination was observed.
Solvability does not establish model difficulty. Finite checks do not prove every
input; docs/test quality still needs semantic review. Kernel isolation currently
requires Linux/libseccomp and installed local Python/pytest/Node. Oracle is excluded
from materialization but this is not a malicious-code filesystem sandbox.

Stop after audit/commit/push. No production issue was diagnosed and no Core patch
is warranted by these fixture-only results. A future experiment needs new explicit
paid authorization. This report is instrument validation, not a benchmark,
success-rate claim, model comparison or proof of Agent improvement.
