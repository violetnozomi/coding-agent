# Stability diagnostics and deterministic regressions — S1 / S2 / S3

## Scope, baseline and predeclared budget

Branch `codex/stability-diagnostics`, based on
`e56b07e6b7f5009b6a4f48d3519f3a649867e09d`; inherited code/test milestone
`a2ff1005ed2ab3343a3058db4fc1a626b44b4842`. Root main and all prior worktrees
remain untouched. No checkpoint/index architecture rewrite, paid Provider, SWE,
benchmark, PR or merge is authorized by this task.

Before executing natural reproductions: exactly **3 independent processes per
historical suspect** (daemon nonce, mixed map, semantic map, pipeline ordering),
at most **2 related predecessor selections**, and **one frozen-source full suite**.
Necessary revalidation after actual changes is separate, not unlimited retry.
Every failure and pass is retained; passing does not establish historical cause.
Linux local evidence: `/tmp/nzcoder-stability-diagnostics.XtvVBL`.

## Original evidence actually retrieved

- Windows run `34090323034`, attempt 1, job `101642293486`: 1 failed,
  669 passed, 20 skipped, exit 1; nonce startup reports `not_started`.
  Downloaded raw job log `windows-original.log`. Actual artifact `10006786819`
  contains only doctor.json, platform.json and windows-product-tests.txt;
  **daemon.log is absent**. No underlying startup exception can be inferred.
- Core run `34090322981`, attempt 1, job `101642292966`: 3 failed,
  4041 passed, 36 skipped, exit 1. Downloaded `linux-original.log` confirms
  both map calls returned a redacted internal error and pipeline asserted `3 < 2`.
  The run's artifact API returns zero artifacts; no private map exception exists
  in the accessible record.
- The later documentation-only `e56b07e` runs `34092901999`, `34092902050`,
  `34092902055`, `34092902031` all completed successfully on attempt 1.
  This is additional passing evidence, **not** repair of the original failures.

## Execution plan (S1 → S2 → S3; shared files serialized)

- [x] S1: trace parent spawn → handoff → worker initialization → ready publication
  → nonce/PID/process-marker health → owned cleanup. First add RED tests for
  early spawn/worker failures, total budget and other-instance preservation;
  add safe phase/exception evidence through the existing TraceRecorder/private
  storage and PublicError, not another diagnostic service. Verify actual child
  process and option-like nonce, failure cleanup and collector failure paths.
- [x] S2: locate the first exception-to-string conversion in map/index/semantic
  paths; preserve a bounded diagnostic identity with the real tool/session identity
  when available, plus standalone private fallback. Test sensitive messages,
  concurrent workspaces, cancellation and failed recording; change map behavior
  only if an experiment proves a root cause.
- [x] S3: replace elapsed-sleep assertions with explicit worker/controller gates
  in the real pipeline; separately prove streaming and stable output order.
  Cover failures/cancel/drain and execute in-memory barrier/order mutations.
- [ ] CI/evidence: scoped pytest collection before tmp cleanup, strict safe schema,
  bounded artifacts, explicit omissions/collection failures, actual exit codes and
  attempt-aware names. Inject one controlled test failure to verify capture and
  upload, without making the normal job swallow test failures.
- [ ] Verify required suites, Python 3.10, frozen full Linux, typing/lint/build,
  outside-checkout installation and actual native Windows/remote artifacts.

Each S1/S2/S3 implementation is committed separately with its tests. Shared
diagnostic primitives have no global singleton; artifact collection cannot grant
access to user state beyond test-owned paths. Raw exceptions/locals/source lines,
nonce/token/input/path contents and arbitrary logs never enter public artifacts.

## Initial acceptance snapshot (before implementation)

| Task | Diagnostic / deterministic contract | Historical cause |
| --- | --- | --- |
| S1 | Pending | Unconfirmed; original worker log unavailable |
| S2 | Pending | Unconfirmed; original exception redacted |
| S3 | Pending | Sleep assertion has no readiness synchronization; production contract under audit |

No completion or product-stability claim is made at this planning checkpoint.

## Investigation record (append-only)

Original attempt metadata was fetched from GitHub's attempts/1 API, not inferred
from the eventual workflow conclusion. Both failed attempts used
`a2ff1005ed2ab3343a3058db4fc1a626b44b4842`, event `push`. Linux log identifies
Ubuntu 24.04 image `20260831.293.1`, Python **3.12.14**; Windows log identifies
Windows 2025 image `20260824.214.3`, Python **3.12.10**.

### Natural repetition budget — exhausted, not extended

`python /tmp/nzcoder-stability-diagnostics.XtvVBL/natural_repeats.py`, exit 0,
ran each exact historical node three times in a new Python process with only
PATH/HOME/LANG/PYTHONUTF8 inherited. Runtime source was still the baseline during
these probes; the new diagnostic support was not yet connected to these callers.
Each subprocess ran `python -m pytest -q <node> --tb=short`. Full commands and
exit codes: `natural-results.json`; individual logs: `natural-N-attempt.log`.

| N / historical node | Attempt 1 | Attempt 2 | Attempt 3 |
| --- | --- | --- | --- |
| 1 `tests/test_daemon.py::test_daemon_start_accepts_option_like_nonce` | pass / 0 | pass / 0 | pass / 0 |
| 2 `tests/test_repo_languages.py::test_repo_map_indexes_mixed_language_directory` | pass / 0 | pass / 0 | pass / 0 |
| 3 `tests/test_repo_languages.py::test_repo_map_semantic_probe_uses_ranked_non_python_file` | pass / 0 | pass / 0 | pass / 0 |
| 4 `tests/test_workflow_runtime.py::test_pipeline_streams_items_without_stage_barrier_and_preserves_order` | pass / 0 | pass / 0 | pass / 0 |

This fixed 12-process observation did not reproduce the historical symptoms.
It neither closes them nor warrants more repetitions until one fails.

### Hypotheses and discriminating experiments

- S1: parent starts its deadline after preparation/spawn/marker wait; failed
  Popen can leave its token and lifecycle fence. Worker handoff/token reads
  occur before its exception boundary. These are source-confirmed gaps being
  tested with injected failures and actual subprocesses, not explanations of
  Windows's unobserved original worker state.
- S2: the map's first structural query can observe the service warming after
  its existing 100 ms wait. A held build can distinguish this from a parser
  exception. The semantic test stubs LSP workspace symbols and reaches that
  stub only after structural indexing/ranking; it does **not** call an embedding
  Provider. Missing provider/network readiness cannot explain that fake path.
  First index-update failure and map conversion need correlation before raw
  exception evidence is replaced by status text.
- S3: the old sleep begins inside the child, so it says nothing about which
  outer thread starts first. Its status-only list assertion also cannot detect
  swapped outputs. Events must prove downstream admission before another item
  is released, then prove reverse completion independently of ordered return.
  Separately, executor context exit can wait for peers before a structural
  failure reaches the outer stop handler; a held peer distinguishes this defect.

### Shared diagnostic / CI design

`OperationDiagnostic` is per operation and delegates storage to `TraceRecorder`
inside owner-private user state. The wire envelope uses `operation`, not the
generic raw-error `diagnostic` field that TraceRecorder intentionally redacts.
The first draft hit that existing redaction (3 failed / 4 passed); the corrected
structural envelope passes all 7 support tests without changing TraceRecorder's
global secrecy rules. Unknown/dynamic exception names and external frame names
are normalized; trusted package frames retain module/function/line only.

Identifiers are locally generated opaque diagnostic IDs; arbitrary existing
session/call/workspace identities are correlated by SHA-256, never echoed from
an exception/input. First failure phase/type survives secondary cleanup evidence.
The helper has no global registry, database, worker, or ContextVar singleton.

The opt-in pytest capture inspects only that test's temporary directory and its
explicit user-state fixture siblings. Aliases/reparse points, nonregular files,
raw logs, SQLite, arbitrary filenames and non-schema records are excluded.
Limits: 1,024 traversal entries / depth 10, 64 KiB per file, 256 KiB per test,
512 diagnostic records per collection and 4 MiB admitted test records per job.
One extra read byte detects truncation. Omitted/failed/missing collection is
explicit; frames never include source lines, arguments or locals.

The CI runner captures pytest stdout/stderr in its own temporary file, then
deletes it; it uploads only reconstructed JSON under `artifacts/safe/`. Failed
test evidence is persisted before fixture teardown, and the actual pytest exit
is propagated, independently of best-effort collection. The controlled-failure
self-check expects exactly exit 1 and a captured diagnostic, keeping that real
exit in `run.json`; it is not applied to normal test failures. Artifacts include
run/attempt/job/SHA/platform/Python/dependency versions and static node IDs plus
parameterized-node digests. Force-killed runners or lost machines cannot be
promised an upload.

Local capture checks: first RED was missing collector module (exit 2); first
implementation **5 passed**, exit 0; expanded helper/capture safety selection
**14 passed**, exit 0. A real intentionally failed subprocess retained safe
evidence after its source fixture directory and private output were removed.
Actual remote artifact upload and native Windows checks remain pending.

### S1 implementation and deterministic proof

Parent and worker now share an opaque diagnostic ID, but use separate writers
under `<profile>/diagnostics/{parent,worker}/`. The start chain records request,
spawn, process marker, handoff, token read, service construction/listening,
identity-verified health, early exit/deadline and owned cleanup. Listening alone
does not mean readiness. Existing nonce authentication, PID/start-marker checks,
option-like nonce handling, and `shell=False` remain required.

Source inspection plus failing regressions proved defects independent of the
original Windows failure: preparation/marker work was outside the startup budget;
Popen failure could leave the lifecycle fence/token; worker token/handoff errors
were outside its diagnostic boundary. The fix uses one monotonic startup deadline
and a separate finite cleanup budget, recording observations before cleaning up
only the retained Popen and matching operation-owned files. Replacement state,
token, lock and an actual foreign child survive. Failed reaping retains management
files rather than reporting complete cleanup. Final `poll()` is sampled once
inside a protective boundary; its failure is secondary, with unknown final status.

Actual subprocess tests exercise pre-worker exit 17, initialized-worker ImportError
exit 4, missing log pathname, and two independent live children. Pre-worker merged
stdout/stderr is bounded to 64 KiB plus a detection byte and projected only to
byte/line counts and presence/truncation flags. Raw output is never exported. A
process that dies before worker code runs cannot provide an invented traceback;
its exit, parent phase and structural output summary are the available evidence.

Initial S1 RED: 9 failed / exit 1. Subsequent real-component fixes and independent
review are preserved in `s1-report.md` and `s1-independent-review.md` in the evidence
directory, including exact commands and all intermediate failures. The final-poll
review counterexample failed once (exit 1), then passed (exit 0); diagnostics-only
selection passed 19 cases on both Python 3.13 and 3.10. Three isolated mutations
(ownership check, snapshot evidence, health budget) each failed their target test.
These are deterministic contract results, not historical Windows cause proof.

### S2 first-catch correlation without publication changes

The structural chain is `repo_map → _build_index → workspace service prewarm →
_build → scan → _update`. Previously `_update` converted the first exception to a
raw status string and `_build` swallowed it; the public reader later redacted a
different unavailable exception. Now the first catch reserves the original
exception identity and phase **in memory only**, while publishing failed state.
Persistence occurs after the view lock is released. Attaching the diagnostic ID
uses an identity comparison against the exact failed state, so a delayed collector
cannot overwrite a newer ready generation. Runtime metrics no longer expose the
original exception message. The existing published index/graph view remains the
read authority; there is no unlocked fallback scan or increased wait budget.

An Event-held build plus an independent controller proves that the existing
100 ms read gate can report `warming`; this is now an explicit safe failure with
correlation and index status/generation facts, not an empty successful map. It is
a confirmed causal path, but the absent original CI traceback prevents identifying
it as that historical failure. The fake semantic collector in the old case runs
only after structural indexing; embedding/network readiness is not its mechanism.

Semantic enrichment remains nonfatal LSP workspace-symbol lookup. Its first catch
now records the same operation ID and returns a correlated safe notice. Parser
fallback messages are not rendered raw. Standalone callers get private workspace
storage; actual tool/session identities are hashed when present. Concurrent
workspace/call, blocked collector, failed collector, recovered generation,
cancellation and sensitive-message tests verify the real paths.

Per-workspace index/analyzer ownership was checked; no shared parser singleton or
cross-workspace key was found. Zero-lease registry entries and database locks can
remain until release/process exit, but accumulation is not evidence of this CI
failure. No lifecycle redesign is included. Initial tests recorded 8 failed /
1 passed; review recorded 5 failed, then 2 failed, then 1 failed / 82 passed as the
state-publication/first-error race was exposed. The reviewed focused set reached
96 passed / exit 0; exact command is in `s2-report.md`. No natural reruns were added.

### S3 actual scheduling contract and minimal product fix

`pipeline()` streams each item's advancement through stages; it is not a public
result iterator. Cross-item worker start/completion order is unconstrained. Final
results follow logical input order, including `None` for ordinary failed chains.
The old sleep test required an undeclared cross-item start order and compared
identical status strings, so it did not validate ordered result identity.

Two separate real-manager tests now prove (A) downstream admission while another
upstream item is held, and (B) actual `run_agent` completion in reverse input order
while the returned texts remain input-ordered. Gates are controlled outside the
production executor; watchdogs detect deadlock, not relative speed. Every test
releases gates and drains started work in `finally`. Stage-one/downstream failures,
caller cancel, manager consumer-stop, queued work and active-task cleanup retain
the existing concurrency/backpressure boundaries. Only external child execution
is controlled; manager, scheduling, queue and result assembly are real.

In-memory stage-barrier and output-reorder mutations each produced the expected
one failing test / exit 1; neither mutation is committed. Two initial mutation
harness setup failures are retained and not counted as behavioral proof. Corrected
mutation results are `s3-stage-barrier-mutation-reviewed.xml` and
`s3-output-reorder-mutation-reviewed.xml`.

A separate held-peer experiment proved a product defect: a structural control
error escaped inside the executor context, so its shutdown waited for peers before
the outer `stop_active()` could run. The minimal fix cancels queued futures and
stops active children before executor drainage. If stop itself fails, the original
control error remains primary and existing workflow events carry only fixed stage,
run correlation and a safe exception type. The controlled RED and GREEN are
`s3-control-drain-{red,green}.xml`; the final workflow slice after rejecting dynamic
exception names completed 28 passed / exit 0. Historical ordering is classified
as a test synchronization defect; this additional drainage defect is separately
reproduced, fixed and verified.

### Independent review and diagnostic safety corrections

Review was not treated as a formality. Deterministic counterexamples exposed
source-like injected function names, progress records displacing first failure,
exception attribute hooks raising secondary errors, metadata capture losing the
known pytest exit, concurrent phase misattribution, and JSON formatting exceeding
the promised byte budget. Fixes retain only static package-declared symbols and
known exception types, use BaseException's raw descriptors, preserve the real exit,
account exact serialized bytes, and reserve first-cause phase before extraction.

The split capture/persist path then exposed two more concurrency cases: secondary
errors could consume a deferred primary's last slot, and a concurrent successful
writer could make a failed primary return shared `saved=True`. Both new regressions
first failed (`shared-final-review-red.log`, 2 failed / exit 1). A primary-exclusive
last slot and a local per-write result now prevent these outcomes; the combined
helper/capture suite passed 25 cases / exit 0. The per-operation lock protects only
in-memory metadata, not disk I/O. Deep traceback truncation/omission is explicit;
the bounded retained window prefers innermost locations. Capture failure cannot
claim the primary record exists. Final independent narrowed review found no
remaining important finding in these corrected paths (`shared-rereview.md`).

### Ordered predecessor selections — budget exhausted

Both commands below ran once, in the displayed file order, with the same sanitized
environment prefix as natural probes. No failing attempt was removed.

| Exact command suffix | Result | Log |
| --- | --- | --- |
| `python -m pytest -q tests/test_repo_intelligence_consistency.py tests/test_repo_languages.py --tb=short` | 44 passed / exit 0 | `predecessor-1.log` |
| `python -m pytest -q tests/test_repo_intelligence_service.py tests/test_repo_languages.py tests/test_workflow_runtime.py --tb=short` | 53 passed / exit 0 | `predecessor-2.log` |

### Limits and decision gate

Diagnostics can perturb timing; an instrumented pass is not root-cause evidence.
No full solution is claimed for the original daemon/map symptoms, older HTTP or
watcher timeouts, arbitrary external side-effect rollback, force-killed runners,
or lost machines. This phase does not alter accepted checkpoint authority or
restore ToolRuntime-to-host-private dependencies. Private local raw logs are not
the public artifact format, and local `/tmp` evidence is not durable remote storage.

Current integration gate: finish the frozen full suite, supported Python floor,
current native Windows, packaging/fresh-install and actual safe artifact download.
If those pass and only the unreproduced historical symptoms remain, a small bounded
real coding-task evaluation is appropriate; it is not a whole-product stability
guarantee. No paid Provider call, SWE run or benchmark was made in this phase.

### S2 product-entry follow-up found by independent review

The first S2 implementation covered direct Repo Map creation but missed the normal
AgentLoop path: `acquire_repo_intelligence()` prewarms before the tool supplies a
callback. A subsequent map reused the existing failed service and could still
record only `_RepoIntelligenceUnavailable`. The independent probe confirmed zero
original diagnostic records and no `_update` frame. This was a real wiring defect
in the first draft, not proof of the historical CI fault.

The service now creates a fallback diagnostic **only on failure** if no callback
was supplied. An opaque root ID and `evidence_saved=false` are published with the
failed state before storage work. After producer locks exit, that same ID owns
the original exception's structural record; identity-CAS publishes saved status.
The typed unavailable read carries these structured fields, and later map calls
publicly reference the original root instead of parsing error strings or waiting
for its file. A blocked writer therefore still permits immediate, correctly
correlated `evidence=unavailable`. Successful scans create no fallback file.

The actual acquire/prewarm and direct-scan regressions first failed (2 failed /
exit 1); acquired-service, direct-scan, blocked early-read and collector-failure
cases then passed. The final related set completed 102 passed / exit 0 in 5.74 s,
with exact command/output in `s2-final-reviewed.log`. Earlier S2 runs were retained
in tool output, not retrospectively rerun to manufacture log files. Independent
review is repeated only for this confirmed missing path.

## Current integration snapshot — frozen code, remote verification pending

| Task | Layer 1: production diagnostics / regression contracts | Layer 2: historical failure |
| --- | --- | --- |
| S1 | Local real-child, ownership, budget and collector checks passed; independent review accepted | Not reproduced this round; diagnostics added; original Windows cause unconfirmed |
| S2 | Standalone and acquired-service first-catch paths, early read and CAS verified; independent review accepted | Not reproduced this round; diagnostics added; original CI cause unconfirmed |
| S3 | Real-manager barrier/order mutations and failure/cancel/drain contracts verified | Confirmed test synchronization defect; test repaired. Separate product drainage defect reproduced and fixed |

Frozen executable revision: `7300136d4cc23d4370e01031f586866d2201b30d`.
The following commits are local at this snapshot; push/remote confirmation and
full-suite/native Windows acceptance must be appended from actual execution.

| Commit | Responsibility |
| --- | --- |
| `e7fe9d141b6fe1f296e6266952fb8870834ba3b2` | S1 real daemon consumers plus shared safe diagnostic support and regressions |
| `f92d13d3a552c5fc21189dbc71b587923fff74bc` | S2 standalone/product prewarm root correlation and real service regressions |
| `28011f995994c81cb96908c65d98a135cec9c000` | S3 stop-before-drain fix and deterministic scheduling tests |
| `7300136d4cc23d4370e01031f586866d2201b30d` | Opt-in CI safe capture, controlled failure and attempt-specific upload wiring |

Latest integrated local checks used Python 3.13.12 except the explicit 3.10.20
interpreter. Pytest commands had the prefix
`env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 PYTHONUTF8=1`.

| Actual command suffix | Result / exit | Retained log |
| --- | --- | --- |
| `python -m pytest -q tests/test_daemon.py --tb=short` | 28 passed / 0 | `required-daemon.log` |
| `python -m pytest -q tests/test_repo_languages.py --tb=short` | 13 passed / 0 | `required-repo-languages.log` |
| `python -m pytest -q tests/test_workflow_runtime.py --tb=short` | 19 passed / 0 | `required-workflow.log` |
| `python -m pytest -q tests/runtime/tool_runtime tests/recovery tests/test_package_structure.py tests/test_architecture_boundary.py --tb=short` | 290 passed / 0 | `required-boundaries-final.log` |
| `python tests/typecheck/check_tool_contracts.py` | 0 errors; both incompatible fixtures rejected / 0 | `typecheck-final.log` |
| `/tmp/nzcoder-runtime-boundary.ug3MGN/python310/bin/python -m pytest -q tests/test_daemon.py tests/test_daemon_diagnostics.py tests/test_stability_diagnostics.py tests/test_stability_capture.py tests/test_repo_map_diagnostics.py tests/test_workflow_runtime.py tests/test_workflow_deterministic.py --tb=short` | 115 passed / 0 | `python310-final.log` |
| `python -m tests.stability_capture --self-test --output /tmp/nzcoder-stability-diagnostics.XtvVBL/local-controlled-failure` | wrapper 0; actual intentionally failed pytest 1 | `local-controlled-failure.log` and its safe JSON directory |
| `python -m ruff check nz_coder tests` | passed / 0 | tool output; prior `ruff-final.log` |
| `python -m compileall -q nz_coder tests` | passed / 0 | tool output; prior `compile-final.log` |
| `git diff --check` | passed / 0 | tool output |

The last test-only cleanup correction moves assertions out of the blocked-writer
test's `finally`, so failed assertions cannot skip executor/registry drainage;
its focused case passed / exit 0 afterwards. Product source is unchanged by that
correction. Whole frozen-revision verification below is still required and is not
replaced by these earlier/incremental selections.

## First frozen integration — retained failures and minimal corrections

The first full command was exactly
`env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 PYTHONUTF8=1 python -m pytest -q --tb=short`.
It completed **2 failed / 4110 passed / 35 skipped**, 499.82 s, **exit 1**
(`full-frozen.log`). Executable source/tests were `7300136`; `2aa38ff` only added
the documentation already present in that worktree. A read-only `--collect-only`
probe while the slower suite segment was running collected 4147 tests; no test
process was restarted. The initial push and a later documentation push scheduled
distinct CI SHAs, not same-SHA reruns. The documentation push had one TLS transport
failure and then succeeded; remote ref `2aa38ff64fb95400950d682759f99284df870dc3`
was verified through GitHub's ref API.

Confirmed failures and bounded fixes:

- `test_readme_release_commands_match_the_core_workflow`: the command inventory
  still expected direct pytest in the workflow after the actual capture wrapper
  was introduced. README now documents both direct local pytest and the exact CI
  wrapper. The test still requires the same command to exist in README/workflow,
  and additionally retains the direct local-command documentation assertion.
  No comment-only workaround or CI test removal was used.
- `test_repo_map_blocks_escape_and_non_python_file`: the new generic diagnostic
  replaced the established `Error: Path escapes workspace:` category. The real
  WorkspacePathError path now retains that fixed prefix plus safe diagnostic
  correlation, **without echoing the embedded input path**. The old test is
  unchanged. Two new real-path sentinel cases verify working/failed recorders.

The new focused RED was **4 failed / 5 passed**, exit 1
(`integration-compat-red.log`). After the corrections,
`python -m pytest -q tests/test_release_docs.py tests/test_repo_map.py tests/test_repo_map_diagnostics.py tests/test_repo_languages.py tests/test_stability_diagnostics.py tests/test_stability_capture.py --tb=short`
completed **68 passed**, 3.21 s, exit 0 (`integration-compat-green.log`), under
the same sanitized prefix. Ruff and diff check passed. A necessary new frozen
full verification follows the actual source correction; this does not enlarge
the exhausted natural/predecessor repetition budgets or erase the first failure.

The initial wheel/sdist build used
`python -m build --no-isolation --wheel --sdist --outdir /tmp/nzcoder-stability-diagnostics.XtvVBL/dist`
and exited 0 (`build-frozen.log`). A brand-new venv installed that wheel outside
the source tree, exit 0 (`install-frozen.log`). Its first CLI help/doctor probes
both exited 1 because the test's XDG_STATE_HOME was inside its own cwd/workspace;
the existing storage boundary correctly rejected it. Original logs
`installed-help.log` / `installed-doctor.json` are retained. Moving the smoke cwd
to `/tmp/nzcoder-installed-smoke.ffPp1S` while keeping state separately under the
evidence directory made both identical commands exit 0
(`installed-help-corrected.log`, `installed-doctor-corrected.json`). This is a
test-layout correction, not a runtime fix. Installed module/resource assertions
also exited 0. Packaging of the source correction must be verified separately.

Actual downloaded artifact `10011295374` from `2aa38ff` run `34103107855`,
attempt 1, Linux job `101681854992`, confirms **151 passed / exit 0** and 113
safe operation records. Its `run.json` records Python 3.12.14, pytest 8.4.2,
OpenAI 3.8.0, Tree-sitter 0.26.0 and watchfiles 1.2.0, actual command/SHA/job/attempt,
and removed private raw output. `tests.json` reports zero omitted records and no
collection failure. Files are under `remote-linux-2aa/`. Full Core and native
Windows are not replaced by this subset result.

## Corrected local full run and first native Windows evidence

On production/test revision `dd02ed045d0e11a81c1c0a953272b9f5ee5eb44d`, the
same exact sanitized full pytest command completed **4114 passed / 35 skipped**,
477.18 s, **exit 0** (`full-corrected.log`). This is necessary verification after
the real path-format correction, not a same-SHA passing rerun of the first failure.
Its separately rebuilt wheel/sdist also succeeded (`build-corrected.log`).

First remote Core run `34102988245`, attempt 1, job `101681475468` on `7300136`
failed the two known integration cases and
`test_streaming_state_consistency.py::test_cancel_during_provider_connect` at
line 1692 (the existing `< 0.2 s` assertion). Safe artifact `10011688640` was
downloaded into `remote-core-730/`; its controlled-failure child returned exactly
1 and was captured. The Provider timing case is unchanged in this phase; the
safe record identifies its assertion, not the measured elapsed value or cause.
It remains a separate unconfirmed timing symptom, not claimed fixed by a later
pass, diagnostic wiring or a documentation commit. No same-SHA rerun was requested.

Native Windows run `34103107855`, attempt 1, job `101681854748` on `2aa38ff`
completed **3 failed / 766 passed / 20 skipped**, exit 1. Fresh-install was skipped
on that job. The actual `remote-windows-2aa/` artifact retains all three nodes and
the independently failed controlled-failure step. Two failures are the exact
POSIX-mode assertions in `test_workflow_runtime.py` (then lines 249 and 626).
Windows mode bits are not a DACL; the tests had already produced their files.
The real manager uses the verified, inheritable private user-state root. Independent
source review found no evidence of failed workflow execution or exposed state in
these failures. Only exact mode assertions are now POSIX-conditional; neither
whole test nor its corruption/result/artifact/usage assertions is skipped.
Arbitrary caller-provided workflow roots are not independently DACL-hardened by
these two stores; no new all-root privacy guarantee is claimed or implemented.

The third node, `test_controlled_failed_subprocess_retains_safe_evidence_after_tmp_cleanup`,
failed because the Windows controlled child exited **2 during collection**, not
the intended assertion exit 1. The separate controlled-failure step confirms exit
2, zero test rows and 1241 bytes of removed private output. This exposed a real
collector gap: collection exceptions never reach `pytest_runtest_makereport`.
The plugin now captures their actual cause chain through `pytest_exception_interact`,
using the same safe projection and byte quota, without formatting `longrepr`.
A real import-failure subprocess first proved the gap (1 failed / exit 1), then
the collector set passed **11 tests / exit 0**; child exit 2 remains unchanged.
Logs: `collection-boundary-red.log`, `collection-boundary-green.log`.

The combined collector/helper/workflow slice completed **54 passed / exit 0**
(`windows-integration-local.log`). Its exact sanitized suffix was
`python -m pytest -q tests/test_stability_capture.py tests/test_stability_diagnostics.py tests/test_workflow_runtime.py tests/test_workflow_deterministic.py --tb=short`.
The collection-root cause on Windows is **not yet confirmed**; the next commit
adds evidence rather than inventing a cause or labeling an unobserved fix complete.
These subsequent changes are test/CI-support only; production remains `dd02ed0`.

## Collection isolation correction and native first-cause evidence

Run `34105145624`, attempt 1, native job `101688330903`, on `3df04bd`
completed **2 failed / 770 passed / 20 skipped**, exit 1. The two POSIX-only mode
checks now pass; the two failures are the controlled assertion and import-error
specimens, both generated outside the checkout. Actual artifact `10012321426`,
downloaded to `remote-windows-3df/`, now retains a collection-phase
**PermissionError**. Its innermost frames map to pytest **8.4.2**
`main.py:528` (`Dir.collect`) and `_pytest/pathlib.py:963` (`os.scandir`).
No raw exception, denied path, or private stdout was uploaded or reconstructed.

The corresponding pytest source explicitly walks an initial file's parents,
stopping only at its `confcutdir`. The checkout cutoff does not bound unrelated
temporary paths, including Windows paths on a different drive. A real subprocess
regression now denies enumeration **only above the generated specimen's owned
directory**. Before correction it reproduces collection `PermissionError`
(wrapper 1, child 4 on both local pytest 9.0.3 and 8.4.2; Windows originally
returned 2). This distinguishes the filesystem collection boundary from a daemon
startup defect; it does not claim identical OS permission causes or denied paths.
The self-test now passes `--confcutdir <owned specimen directory>`; the separate
import-error specimen explicitly uses its own cutoff. Normal repository suite
arguments/configuration are unchanged. No test is skipped or made successful
after a business failure; the controlled assertion must still produce child 1,
and the controlled import failure must still produce child 2.

Independent review additionally demonstrated that a generated filename containing
a private sentinel could enter the public node label. `_node` now only exposes
readable labels for resolved existing files below this checkout's `tests/`;
external nodes retain `external_test` plus their digest. The real import-error
specimen now puts a sentinel in its filename as well as its exception and checks
all JSON, stdout/stderr, original exit and private-output removal.

`collection-isolation-red.log` retains **2 failed / exit 1**; the isolated
pytest-8 ancestor counterexample is in `collection-isolation-pytest8-red.log`
(**1 failed / exit 1**). After correction the exact collector command
`python -m pytest -q tests/test_stability_capture.py --tb=short` completed
**12 passed / exit 0** (`collection-isolation-green.log`). The lowest-supported
Python command
`/tmp/nzcoder-runtime-boundary.ug3MGN/python310/bin/python -m pytest -q tests/test_stability_capture.py tests/test_stability_diagnostics.py tests/test_repo_map_diagnostics.py tests/test_workflow_runtime.py tests/test_workflow_deterministic.py --tb=short`
completed **72 passed / exit 0** (`python310-collection-final.log`). Both used
the previously documented sanitized environment. Native validation of this
specific collection correction is still pending at this checkpoint.

Other first-attempt results are retained independently:

- `2aa38ff` Core run `34103107937`: **2 failed / 4108 passed / 36 skipped**, exit 1;
  only the known release-document and map-path integration assertions failed.
  Actual artifact `10011739848` is under `remote-core-2aa/`.
- `dd02ed0` Core run `34104163456`: **4112 passed / 36 skipped**, exit 0;
  actual artifact `10012109776` is under `remote-core-dd02/`.
  All three Core jobs passed, including Python 3.10 and installed-wheel contract.
- `dd02ed0` Windows run `34104163492`: **3 failed / 768 passed / 20 skipped**,
  exit 1; same two POSIX assertions plus the controlled collection failure.
  Artifact `10011964087` is under `remote-windows-dd02/`.

The corrected production wheel from `dist-corrected/` was installed into a new
venv `installed-corrected/` using
`python -m venv /tmp/nzcoder-stability-diagnostics.XtvVBL/installed-corrected`
then its `python -m pip install -q /tmp/nzcoder-stability-diagnostics.XtvVBL/dist-corrected/nz_coder-0.1.0-py3-none-any.whl`;
both exited 0. Installed `nz-coder --help` and
`nz-coder doctor --repo-intelligence-only --json` exited 0 from
`/tmp/nzcoder-installed-smoke.ffPp1S`, with separate
`XDG_STATE_HOME=/tmp/nzcoder-stability-diagnostics.XtvVBL/installed-production-state`.
Logs: `install-production-corrected.log`, `installed-production-help.log`,
`installed-production-doctor.json`. An initial resource assertion incorrectly
looked for old `nz_coder/config.py` and exited 1; the actual package stores config
under `foundation/`. The corrected resource check verifies the installed
diagnostic module is in site-packages, package version, and bundled
`bundled_skills/code-review/SKILL.md`; exit 0 (`installed-production-resources.log`).
No package code was changed to satisfy that mistaken smoke assertion.

The same independent filename review found an external symlink alias could
resolve into a trusted repository test while retaining its private original
name. The node boundary now checks both lexical and resolved containment.
The new actual-symlink counterexample first failed (1 failed / exit 1,
`collection-alias-red.log`), then the complete collector slice passed
**13 tests / exit 0** (`collection-alias-green.log`). On a Windows runner without
symlink privilege, that one test exercises the equivalent resolved-target report;
it does not claim to create a native symlink. Native daemon/process acceptance
remains real and separate. Readable ordinary repository labels remain asserted.

The final narrow independent re-review accepted the filename and alias fixes
(3 targeted tests passed / exit 0; `collection-review.md`). Python 3.10's final
collector-only selection also passed **13 tests / exit 0**
(`python310-collector-seal.log`).

The `3df04bd` Core run `34105145693`, attempt 1, job `101688331021`, then completed
**1 failed / 4112 passed / 36 skipped**, exit 1. Actual artifact `10012502222`
is retained at `remote-core-3df/`. Its sole failure is the existing
`test_process_service.py::test_service_close_kills_spawned_descendant_process_group`,
`ProcessLookupError` at line 1028. That failure is separate from the collection
support changes; its cause is not established by this artifact and it is not
silently fixed or retried in this bounded S1/S2/S3 task. Both the Python 3.10
and installed-wheel Core jobs passed. This first failure remains visible even
if subsequent changed-source CI passes.
