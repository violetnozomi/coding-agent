# Tool recovery / owned Undo–Redo acceptance

Date: 2026-09-06. Scope: the user's `98eb2c92` implementation specification,
not a whole-Agent rewrite or an SWE-bench evaluation.

## Baseline and scope

The implementation is isolated in `codex/tool-recovery-checkpoints`, worktree
`/home/pyh/nzcoder/.worktrees/tool-recovery`, based on
`72c793f74f19ffe9c35f8ce4bb48e2be26e24f37`. The root checkout remains on its
existing `main`; reference repositories and unrelated worktrees are untouched.
No push, PR, paid Provider request, or benchmark run is part of this task.

Implementation decisions: the supplied specification was used without another
approval gate; reversible rework stays on this isolated branch. Tightly coupled
ledger/coordinator changes were implemented together, while bounded projection,
entry-point and independent review tasks were delegated. Review-driven tests
exposed and closed the output-admission, cancellation and migration windows.

The available reference was the extracted tree
`infcode-dev/infcode-dev/packages/opencode/src/` in the root workspace.
The specifically named ZIP/audit-evidence document was not found; this report
does not claim verification against that unavailable archive revision.

## Reference-to-implementation map

| Reference source / behavior actually inspected | NZ-Coder implementation | Deliberate difference |
| --- | --- | --- |
| `infcode/session/message-v2.ts`: interrupted tools, error summary and completed results | `protocol/tool_recovery_parts.py`, `runtime/conversation/tool_recovery.py`, `message_projection.py` | A bounded USER data block preserves execution state, terminal cause and file effects separately; it is not fabricated tool output. |
| `infcode/checkpoint/checkpoint.sql.ts`, `recorder.ts`: file bases, tool-step before/after, idempotent after | `state/tool_ledger.py`, `runtime/process/checkpoint_runtime.py` | Independent private SQLite, integer execution/mutation order, separate invocation and attempt identities, raw bytes. |
| `tool/write.ts`, `tool/edit.ts`, `tool/apply_patch.ts`: capture around real writes | `foundation/mutation_hooks.py`, `workspace_file_access.py`, `state/transaction.py` | Before persistence is fail-closed. The reference's warning-and-continue capture failure is not copied. |
| `infcode/checkpoint/rollback.ts`: restore targets and undo receipt | `runtime/session/recovery_journal.py` | Write-ahead operation before files; foreign-owner and source/target conflict checks; restart reconciliation. No lexical UUID ordering. |
| `session/revert.ts`: files plus history transition | `runtime/session/session_revert.py`, CLI, SDK, HTTP adapters | One coordinator owns the history commit; global snapshots are not automatic ownership evidence. |

This is a semantic adaptation of the inspected call chains, not a claim that
TypeScript source was copied line-for-line or that every InfCode capability is
equivalent.

Confirmed baseline differences (not inferred from filenames): the old
`SessionReverter.revert` performed a global snapshot transition before writing
its recovery hint; ChangeTracker retained run/path text diffs rather than
durable per-attempt byte ownership. Existing ToolParts could survive locally
while unmatched envelopes/continuation selection omitted them from the first
resumed request. The changes above replace those particular authorities and
connect existing admission, safe file access and Provider projection boundaries.
The acceptance matrix below maps each replacement to its executable checks.

## Authority and actual call chains

Admitted model batch → stable assistant-step / call identity → `register_batch`
→ permission and cancellation checks → `ToolExecutor` → `dispatch_checkpoint`
→ registered file handler → `WorkspaceFileAccess` → injected mutation recorder
→ durable before/planned objects and intent → existing anchored write/delete
→ observed after → confirmed mutation. Failed after capture leaves a prepared
intent with **unknown** effects. Immediate transaction compensation updates the
same mutation IDs; completed execution does not imply its file effects remain.

`ledger.sqlite3` lives under the existing owner-private workspace user-state
directory at `tool-recovery/`, outside model-readable workspace paths and
separate from `code-index.sqlite3`. It owns execution order, tool/file ownership,
file bases, before/after references, dispositions and recovery progress.
Session JSON still owns the transcript. `_nz_parts[*].state.recovery` is a
refreshable projection, not a second authority. If JSON missed an admitted call,
the ledger reconstructs that call's recovery facts/projection, **not** the lost
full conversation or unrecorded raw output. It does not reconstruct an executable
call or claim a missing output was successful.

Identity is workspace + session + user interaction + assistant step + invocation
(`agent_id`) + call + attempt; `sequence` is a SQLite integer, not sorted IDs.
`UNIQUE(session_id, assistant_step_id, call_id)` and per-attempt mutation ordinals
make repeated registration/after settlement idempotent. A Session's first
before state remains its base; each mutation retains its own actual before.
Absent, empty bytes, and unknown after are distinct. Create/modify/delete is
derivable from the referenced absent/present states.

The schema migrates supported versions to v4; unsupported future versions are
rejected. Each operation owns its SQLite connection with foreign keys,
5-second busy timeout and synchronous FULL / DELETE-journal commits. Model calls
and workspace writes never run inside a database write transaction. Blob bytes
and relevant POSIX directories are flushed before references are committed.
`foundation/content_objects.py` is shared with existing step snapshots, so state
does not import Runtime just to obtain blob publication primitives.

`recovery_run` holds an OS-backed, nonblocking Session owner lock. Only a new
execution owner may classify old unfinished attempts as process-lost; ordinary
HTTP attach and snapshot reads do not acquire ownership or alter liveness.
Same-Session concurrent execution is refused. Cooperative file mutation and
recovery also use a workspace lock, independent of SQLite's short locks.
On a controlled exit, still-registered calls become `not_executed`; cancellation
is recorded as `user_cancelled`. Already-running/completed attempts are not
overwritten by this cleanup, including when the initial batch checkpoint fails.

Execution settlement and public output admission are separate. The executor
persists execution facts without raw output. Only after tool-output guardrails
have returned does batch settlement store a bounded, once-admitted preview.
Blocked or interrupted guardrails publish no preview. The v3 migration clears
legacy previews that lack admission proof on every open, including retry after
interruption following `ALTER TABLE`. Projection and archive readers independently
require the admission bit; they do not rely solely on successful migration cleanup.
This also protects facts rebuilt
after JSON loss and their quota-bound model-readable archives.

## First resumed Provider request

The process tests execute a real registered `write_file`, synchronize immediately
after publication, kill that child, and start a different Python process.
That new process enters `AgentClient → native Runner → SessionRuntime → Gateway`
and captures the Fake Provider's **first** actual request. No paid API is used.

An abbreviated fact line for a write whose after receipt was not committed is:

```json
{"call_id":"call-crash","tool":"write_file","execution_state":"uncertain","terminal_cause":"process_lost","side_effect_state":"unknown","files":[{"path":"marker.txt","after_ref":"","state":"unknown"}]}
```

When after was committed before the kill, execution remains uncertain but the
file fact becomes `committed` with a nonempty after reference. Neither case
fabricates a successful tool result. The original task and outstanding
verification remain in the request. Denial/pre-dispatch cancellation is
`not_executed`; runtime cancellation, exception and timeout are distinct causes.

The block is bounded to the existing 6,000-character continuation budget,
prioritizes unresolved facts, states omissions, and escapes historical data.
Large ledger projections use the existing quota-bound `ArtifactStore` and
`read_tool_result` opaque IDs. Expired/missing/quota-limited artifacts are not
promised to remain retrievable; unavailable archive status is explicit and the
private ledger/history remain available to the host. Compaction retains admitted
part identity and document expansion happens before recovery/continuation
wrappers, so it cannot overwrite them. Private attempt output stays private.

## Undo, Redo and failure policy

All product paths use `SessionReverter → RecoveryJournal`:

- CLI `/undo`, `/redo` do not fall back to ChangeTracker/global snapshot restore.
- SDK adds async `undo_session`, `redo_session`, `recover_session` methods.
- HTTP existing undo/redo and `POST /session/{id}/recover` share the coordinator;
  typed 409 errors include versioned scope/status/conflicts/recovery_required.

The journal checks both the supplied transcript and the authoritative Session
JSON source version. It checks every owned path before preparing and again at
application time. A continuous chain is required; interleaved user edits or
later foreign ledger ownership are conflicts, including Redo when another
Session's ledger-recorded writes returned bytes to the expected source. Arbitrary
external editors' same-content ABA histories cannot all be detected. Unrelated user path B is never selected
because Agent-owned path A changed.

`prepared → applying → files_applied → completed` is the normal path. Every
operation stores source/target history hashes, per-file source/target references,
mutation IDs and progress before changing files. On error it retains
`recovery_required` or `conflicted`. New controlled writes and new Agent runs
are refused until the pending operation has been reconciled.

Restart strategy is forward completion: source means apply target; target means
do not write again; neither means retain conflict without guessing. Matching
pending `/undo` or `/redo` retries resume that operation; explicit SDK/HTTP recover
also works. Files reach their recorded targets before history is committed.
The Session JSON operation marker **and** target-history hash detect a committed
write even if the caller observed an exception after it. A subsequent process
can finish journal bookkeeping without duplicate truncation/appending.

A persistent conflict is not solved by repeatedly calling recover. Preserve
external edits first; inspect the returned operation ID/conflict paths and the
host-private journal's source/target objects. An authorized operator can restore
the affected path/history to a recorded source or target and then retry. There is
no force-overwrite/abort-conflict endpoint or automatic merge in this scope. Do
not delete the journal to unblock writes: that removes the recovery evidence.
Restoring source/target only resolves matching-state discrepancies. Foreign ledger
ownership or missing/corrupt objects can remain blocked afterward; there is no
general-purpose administrative unlock operation in this implementation.

Undo/Redo marks the Session's persisted verification stale, refreshes the live
workspace code index, and drops the coordinator's file-content cache. New runs
invalidate completed Redo. The optional UI hint is neither authority nor a
prerequisite for restart Redo. The existing metadata and owner-path safety
checks remain active at actual file writes.

## Acceptance matrix

| Required scenario | Evidence in `tests/recovery/` (plus existing tests where named) |
| --- | --- |
| 1. Refused / cancelled before execution | `test_execution_tracking.py`, `test_model_recovery.py`: actual executor plus first request |
| 2. One completed, one interrupted call | `test_model_recovery.py`: first native request distinguishes both |
| 3. Published file, result not saved | `test_process_recovery.py`: kill real tool; new process first request, before/after confirmation |
| 4. Resume retains goal / verification | `test_model_recovery.py`: native request, continuation, document preflight |
| 5. Detached frontend does not orphan active daemon | Existing HTTP active attach/restore tests + `test_execution_tracking.py` owner-lock refusal |
| 6. Repeated recovery / projection | `test_model_recovery.py`, `test_concurrent_settlement.py`, artifact and operation idempotency tests |
| 7. Create / modify / delete / consecutive edits | `test_recovery_operations.py`, `test_checkpoint_tools.py` |
| 8. Batch / duplicate after / settlement failure | `test_tool_ledger.py`, `test_checkpoint_tools.py`, `test_concurrent_settlement.py` |
| 9. Agent A / user B | `test_undo_only_owned_paths_then_restart_redo_preserves_exact_bytes` (recreated objects, same process) |
| 10. Interleaved same-file user change | `test_recovery_operations.py` and transaction compensation stale-owner tests |
| 11. Two Sessions same path | `test_recovery_operations.py`: chain conflict and foreign-owner Redo conflict |
| 12. Partial file / history-before / history-after failures | `test_recovery_operations.py`: progress retained, commit marker and typed errors |
| 13. Fresh-process reconciliation | `test_recovery_operations.py::test_killed_process_recovery_uses_source_target_evidence`: eight fresh OS-process cases, Undo/Redo × prepared / file-written / before-history / after-history |
| 14. Legacy unknown ownership | `test_recovery_entries.py`, `test_legacy_snapshot_history_does_not_forge_ownership`; existing Session loaders remain |
| 15. Missing/corrupt/oversize/outside/link states | `test_tool_ledger.py` plus existing anchored workspace access/transaction security tests |
| 16. Verification/index invalidation | `test_recovered_verification_cannot_be_restored_as_current`, `test_undo_refreshes_live_code_index_without_watcher` |

## Validation record

All commands run in the isolated worktree unless stated otherwise. No assertion
was deleted to hide a failure; the architecture prohibition on reverse imports
remains unchanged. Existing snapshot-revert fixtures now write through actual
owned tool checkpoints; legacy refusal remains explicitly tested.

- Baseline targeted tests: 24 passed, exit 0.
- Projection step: 167 passed, exit 0; HTTP active attach subset 8 passed,
  70 deselected, exit 0. Document-expansion regression: RED then 108 passed.
- Initial ledger/controlled-write/executor steps: respective RED missing-feature
  tests, then 21 passed, exit 0.
- Review-driven regressions reproduced before fixes: repeated confirmation
  resurrecting reverted state; stale-owner transaction compensation; compensation
  bookkeeping retry; concurrent late cancellation; stale durable history; foreign
  Redo ownership. All have retained focused regression assertions.
- First full `python -m pytest -q --tb=short --maxfail=15`: **7 failed, 3857 passed,
  35 skipped**, exit 1 (477.55s). Failures were dependency direction, reduced raw
  exception inventory and four old snapshot-only fixtures. These failures are
  retained here, not described as a passing full run.
- Integrated follow-up covering recovery, Session revert, package structure and
  context architecture: 171 passed, exit 0.
- Second full `python -m pytest -q --tb=short --junitxml=/tmp/nzcoder-recovery-checks.soxY8S/pytest.xml`:
  3887 passed, 35 skipped, exit 0 (314.67s).
- Intermediate full `python -m pytest -q --tb=short --junitxml=/tmp/nzcoder-recovery-checks.soxY8S/pytest-final.xml`:
  XML records **1 failed, 3887 passed, 35 skipped** (303.45s). The terminal exit
  record was not retained; this is explicitly a failed run, not passing evidence.
  `test_service_coalesces_burst_events_before_incremental_update` raised
  `KeyError: Unknown repository module: burst.py`. The independently reproduced
  pre-existing publication race is described below; subsequent green does not
  resolve it.
- First fresh-wheel smoke independently failed, exit 1: SDK recovery's optional
  hint used the ambient workspace instead of the explicit target. Retained a
  focused RED test, then fixed scoping (without relaxing private storage safety).
  `python -m pytest -q tests/recovery/test_recovery_entries.py --tb=short`:
  24 passed, exit 0. A final full run includes this correction.
- Final independent integration review reproduced two defects: a pre-guardrail
  raw preview surviving output rewrite, and unstarted batch tails incorrectly
  becoming `process_lost` after controlled cancellation. The actual first-request
  tests failed before fixes (3 failures), then passed after separate output
  admission and registered-call cleanup. Rewrite, block and guardrail cancellation
  are now covered; an erroneous `RunStatus.FAILED` reference introduced during
  repair was caught by the same tests and corrected to the existing `ERROR`.
- Shared byte publication regression exposed a second raw `close` after `fdopen`
  had taken ownership (1 failed, 2 passed, exit 1). Ownership transfer now prevents
  closing a descriptor possibly reused by another thread. The fdopen-failure path
  still closes untransferred descriptors. Schema migration discards old previews.
- Migration interruption and independent admission-reader checks initially
  reproduced **2 failed, 2 passed**, exit 1. Replay-safe cleanup plus explicit
  admission checks in rebuilt projections/archives close the window. Scoped
  independent re-review: 4 passed; both original Important findings closed.
- `python -m pytest -q tests/recovery --tb=short`: **118 passed**, exit 0 (12.72s),
  including the output-admission, interrupted-migration and cancellation cases.
- Pre-freeze full `python -m pytest -q --tb=short --junitxml=/tmp/nzcoder-recovery-checks.soxY8S/pytest-admission-final.xml`:
  3896 passed, 35 skipped, exit 0 (307.61s). This run began before the final three
  migration/reader cases were added; it is not substituted for the frozen run.
- Final full on frozen production code:
  `python -m pytest -q --tb=short --junitxml=/tmp/nzcoder-recovery-checks.soxY8S/pytest-frozen.xml`:
  **3899 passed, 35 skipped**, exit 0 (300.50s). Existing skips were not expanded.
- A final test-only correction asserts Windows readonly/writable mode rather
  than POSIX `0755` on Windows; Linux expectation is unchanged.
  `python -m pytest -q tests/recovery/test_tool_ledger.py --tb=short`:
  15 passed, exit 0. This does not constitute a native Windows run.
- `python -m ruff check nz_coder tests`: all checks passed, exit 0.
- `python -m compileall -q nz_coder tests`: exit 0.
- `git diff --check` and `git diff --cached --check`: exit 0.
- `python -m build --no-isolation --wheel --sdist --outdir /tmp/nzcoder-recovery-checks.soxY8S/dist`:
  exit 0. Build output is retained in `/tmp/nzcoder-recovery-checks.soxY8S/build-final.log`.
  Existing build environment warnings (setuptools/wheel deprecation and SCM/Sphinx
  configuration) are not test failures and were not hidden by changing packaging.

Fresh install uses a new venv without system site packages. Commands below run
from `/tmp/nzcoder-recovery-checks.soxY8S`, outside the checkout:

```sh
/tmp/nzcoder-recovery-checks.soxY8S/venv/bin/python -m pip install --no-deps --force-reinstall /tmp/nzcoder-recovery-checks.soxY8S/dist/nz_coder-0.1.0-py3-none-any.whl
XDG_STATE_HOME=/tmp/nzcoder-recovery-checks.soxY8S/state /tmp/nzcoder-recovery-checks.soxY8S/venv/bin/python -I /home/pyh/nzcoder/.worktrees/tool-recovery/tests/recovery/fresh_install_smoke.py
/tmp/nzcoder-recovery-checks.soxY8S/venv/bin/python -m pip check
/tmp/nzcoder-recovery-checks.soxY8S/venv/bin/nz-coder --version
```

All four commands exit 0. The smoke asserts the package is inside the venv,
performs SDK Undo/Redo on non-UTF-8 bytes, and reports `provider_calls=0`.
`pip check` finds no broken requirements and the installed CLI reports `0.1.0`.
The earlier failed smoke remains documented above.

Source-distribution verification also completed: from outside the checkout,
`python -m pip wheel --no-deps --no-build-isolation /tmp/nzcoder-recovery-checks.soxY8S/dist/nz_coder-0.1.0.tar.gz --wheel-dir /tmp/nzcoder-recovery-checks.soxY8S/sdist-wheel`
exits 0 (log: `sdist-wheel.log` in the same evidence directory). Installing that
rebuilt wheel with the same `--no-deps --force-reinstall` command, then running
the identical isolated SDK smoke and `pip check`, also exits 0. Archive inspection
confirms the new ledger, journal and shared primitives are present in the sdist.

## Status and limits

| Alignment point | Status | Evidence / boundary |
| --- | --- | --- |
| Reference audit | Partial | Extracted reference read; unavailable named archive revision is not verified. |
| A: first-request recovery facts | Implemented and verified on Linux | Actual native Fake Provider request, privacy, resume, compaction and cancellation regressions. |
| B: per-tool raw-byte ownership | Implemented and verified on Linux | Registered file tools, durable before/after, independent ordering and compensation receipts. |
| C: restartable Undo/Redo | Implemented and verified on Linux | Ten synchronized process-death cases: eight journal windows and two actual tool cases. |
| D: common product entries and package | Implemented and verified on Linux | CLI/SDK/HTTP tests, full suite, built wheel/sdist and fresh-wheel SDK smoke. |
| Verification and index after recovery completes | Implemented and verified on Linux | Verification invalidated; restored symbols visible without a watcher. |
| Concurrent queries during index refresh | Partial / known limitation | Existing mixed-generation publication race remains; see below. |
| Windows / power loss | Unverified | Workflow inclusion is not Windows execution; process kill is not power loss. |
| Arbitrary external side effects / full filesystem transactions | Not supported | Outside the specified controlled-file recovery scope. |

Not supported: attribution/automatic rollback of arbitrary Bash/MCP/network/database
effects, general three-way merge, filesystem-wide ACID, process snapshots or
exactly-once external execution. Unsupported external mutation calls are reported
separately instead of being claimed as fully reverted. Old snapshots without
tool ownership can still load, but do not acquire new automatic rollback powers.

Unverified here: native Windows execution and true power-loss recovery. New tests
are included in Windows Product RC, but no remote CI ran for this unpushed work.
Windows mode is normalized to its readonly/writable representation; this is not
full POSIX metadata/ACL/ownership restoration. Symlinks, aliases, out-of-workspace
paths and files over the configured 8 MiB capture limit are refused.

The workspace lock coordinates NZ-Coder, not arbitrary user editors. Check-to-
publication/name races and external in-place edits remain subject to the existing
anchored-access guarantees, not OS-wide isolation. Session JSON source rechecks
also are not a filesystem CAS against arbitrary same-user private-state edits.
Immediate TransactionManager backup recovery is still in-process; the durable
restart guarantee described here concerns the explicit Undo/Redo journal.
Content objects/ledger are not pruned automatically in this scope; model-readable
artifacts use existing quotas/expiry. Long-running ledgers may need a separately
designed retention policy that does not remove pending recovery references.

### Retained issue: concurrent repository-index publication

`intelligence/service.py::_apply_incremental` publishes index changes before graph
changes. Concurrent `symbol_context → graph.related_tests → _select_module` can
combine the two generations and raise `KeyError`, even while service status is
`ready`. The service, index, graph and failed test are byte-identical to baseline
`72c793f`; an Event-synchronized review reproduction pauses between those two
publications and fails on both baseline and current code, without sleep-based
timing. The failed full-suite test uses an independent watcher, not Undo/Redo.

This is nevertheless relevant to recovery: a second synchronized reproduction
through actual Undo exposes the same query window during index invalidation.
After release, Undo returns `completed` and the restored file is present in both
index and graph. The guarantee verified here is refreshed state **after recovery
completes**, not atomic visibility to concurrent index readers. The race remains
unfixed; scope was not expanded into a new indexing architecture change, nor was
its assertion removed or skipped to obtain green results.

## Local commits and handoff

Baseline: `72c793f74f19ffe9c35f8ce4bb48e2be26e24f37`.

| Commit | Responsibility |
| --- | --- |
| `4788e97` | Durable recovery facts reach the first resumed model request. |
| `0a5fefd` | Preserve recovery context through document projection. |
| `e2e30b3` | Converge CLI/SDK/HTTP Undo and recovery entry points. |
| `10569bd` | Owned byte ledger, controlled file hooks, restart journal, privacy/cancellation closure, failure and process tests. |

The following documentation-only commit records this acceptance report and the
learning notes in `docs/struct.md`. The branch/worktree is kept locally; no push,
PR or merge was performed. The root checkout is not the changed implementation.

## R1–R3 repair acceptance (2026-09-06; append-only follow-up)

This section follows the historical report above; its earlier unpushed/Windows
unverified statements describe that earlier handoff, not the current branch.
Baseline: `fad7056df72cdbe2f11355d7f5d455d9f8d798dc`.
Code candidate: `48cf23384843fc056062522154e387320d6757b8` on
`codex/tool-recovery-checkpoints`. Root `main` is unchanged. This round does not
merge, create a PR, relax path/identity checks or call a paid Provider.

### R1: one Windows handle identity convention

The actual baseline Windows Product RC [run 34015217165](https://github.com/violetnozomi/coding-agent/actions/runs/34015217165)
failed: **26 failed, 450 passed, 20 skipped, 11 errors**, exit 1. The job log shows
`current.device=5969225261885072001` versus `expected.device=3606225537`, while
inode, size and mtime match. This is a reproducible identity representation
mismatch, not eleven independent HTTP defects. The failure remains evidence.

`foundation/project_control.py::_windows_handle_snapshot` now obtains volume
serial, file ID, size, FILETIME-derived timestamps and normalized Windows mode
from the same `GetFileInformationByHandle` record. `_windows_handle_info` keeps
its existing tuple interface for transaction receipts. In
`foundation/workspace_file_access.py`, read-before/read-after, overwrite expected
validation and deletion use that shared convention on held handles, rather than
mixing handle identity with Python `os.stat().st_dev`. Existing type/reparse,
parent/final-path, expected size/mtime, no-replace and receipt checks remain.
Untransferred read/overwrite descriptors close even if `fdopen` fails.

Tests exercise actual existing-file overwrite, binary/empty/create/read/delete,
external modification and same-bytes/same-mtime replacement refusal, plus four
registered file tools through ToolExecutor checkpoints. A Linux WinAPI double
executes the real Windows overwrite method, but is not native Windows evidence.

### R2: reserve recovery budget for unresolved effects

`runtime/conversation/tool_recovery.py` ranks unknown/conflicted file effects
first (including execution `succeeded`), then other unsettled execution, settled
success, compensated/reverted effects, and proven non-execution. Within a tier,
existing task/continuation path relevance precedes persisted sequence/position;
new invocation IDs and UUID lexical order do not determine priority. An explicit
new target is not overridden by an old continuation goal. File previews select
unresolved files before their three-file limit, so a fourth conflicted file is
not silently excluded from risk classification.

Within the existing 6000-character block, compact high-risk call identities and
states are reserved before long inputs/results/paths. Escaping is measured
before admission. Total and unresolved omission counts are exact; repeated
projection remains deterministic and does not change durable facts. Full facts
still use the existing quota-bounded artifact service. `RecoveryRun._archive`
clears stale historical hints, then publishes only a checked/recreated artifact
for this run; small facts or missing/quota-failed storage do not promise an old,
unreadable artifact. Output previews still require the prior guardrail admission.

Actual first Native SDK → ModelGateway → offline FakeProvider request after a
published-but-unconfirmed write contains these fields (other fields omitted):

```json
{"call_id":"call-recent-write","tool":"write_file","execution_state":"uncertain","terminal_cause":"process_lost","side_effect_state":"unknown"}
```

Thirty old refusals precede that write in the real ledger. The test verifies the
new fact survives, the written bytes still exist, an archive reference is present,
and no fabricated provider tool-result messages appear. Additional real native
requests reject stale archive markers in both older and latest user messages.

### R3: show the actual Undo/Redo scope

`interface/commands/handlers/core.py` renders recorded files plus safe unsupported
call IDs; no raw tool arguments/host paths are loaded for display. Nonprintable
characters and Rich markup are escaped. Journal `completed` still means its
recorded scope completed, not that arbitrary external effects were reversed.
`RecoveryJournal.redo` now retains an existing pending operation ID in refusals.
CLI tests use an actual journal and persisted mixed file/shell ownership, not a
hand-constructed successful result. Ordinary file, conflict, legacy refusal and
existing HTTP/SDK structured-result tests remain.

Mixed-effect output includes (line wrapping depends on terminal width):

```text
Warning: external or unowned side effects were not automatically undone for tool calls: call-shell. Inspect their remaining effects.
Warning: only recorded files were restored. External tool calls were not re-executed: call-shell. Inspect their remaining effects.
```

The first warning follows Undo's file success line; the second follows Redo's.
Pending/conflicted output includes its status and available `recovery-…` operation
ID or workspace-relative conflict path. It never recommends deleting the journal.

### Retained RED results and independent review

All commands ran in this branch worktree on Linux, Python 3.13.12; exit codes
below are actual command results, not inferred from passed counts.

| Command / stage | Actual result |
| --- | --- |
| `python -m pytest -q tests/recovery/test_windows_identity.py --tb=short`, before R1 | 1 failed, 9 passed; exit 1 |
| Same file plus `-k overwrite`, before descriptor cleanup | 1 failed, 5 passed, 6 deselected; exit 1 |
| `python -m pytest -q tests/recovery/test_recovery_priority.py --tb=short`, before R2 | 10 failed; exit 1 |
| Priority/entry tests, `-k 'stale_archive or conflicted_effect or pending_recovery_retains' --tb=short`, review reproductions | 5 failed, 40 deselected; exit 1 |
| Priority tests, `-k conflicted_effect --tb=short`, fourth-file reproduction | 2 failed, 2 passed, 15 deselected; exit 1 |
| Targeted mixed/pending entry regressions, before R3 | 3 failed, 24 deselected; exit 1 |
| Priority tests, `-k large_valid_operation_refs --tb=short`, long-reference reproduction | 1 failed, 19 deselected; exit 1 |
| Priority + entry + model-recovery files after first review fixes | 79 passed; exit 0 |
| Priority + model-recovery files after fourth-file fix | 53 passed; exit 0 |
| Priority + model-recovery files after long-reference fix | 54 passed; exit 0 |

The independent code reviewer found the stale artifact, missing pending ID and
oversized optional-reference cases and confirmed the conflict classification gap.
These were reproduced before fixing. Main-agent follow-up reproduced the
truncated fourth-file gap. Optional result/file operation references now enter
only after minimum call identity/state is reserved, without truncating usable IDs.
Neither review uses Linux tests as a substitute for native Windows execution.

First full run, started at `0c5e878`, using
`python -m pytest -q --tb=short --junitxml=/tmp/nzcoder-recovery-r123.62vsc2/linux-full.xml`:
**1 failed, 3927 passed, 35 skipped**, exit 1, 527.27 seconds. The parent-resume
test expected a bare user string; R2 correctly prefixes `message_parent`'s
`succeeded/unknown` state mutation. Source review confirmed that tool is registered
serial/nontransactional, so it must not be mislabelled as a read-only/no-effect
tool just to satisfy the old assertion. `946c8c5` strengthens the assertion to
parse the actual recovery call/tool/states and require the exact original parent
reply after the block. The original status assertions remain. Other source
fixes landed while this first run was active; it is interim evidence only.

The second full run at `946c8c5` (`linux-final.log/xml`) was deliberately stopped
by the operator, exit 143, when the fourth-file fix required a new frozen source
version. It is **not** a full-suite pass. The next run at `2f4cd63` used
`linux-candidate.log/xml` and was likewise stopped (exit 143) for the final
long-reference fix. The final run uses `linux-reviewed.log/xml`, without source
changes during execution. No failed index/graph assertion was removed or skipped;
the existing cross-generation race above remains an unresolved limitation.

### Candidate verification and remote CI

At this record's creation, final frozen-source full pytest, fresh-install recheck
and native Windows CI are **pending**, so this is not yet merge-review approval.
Observed interim subset results: recovery 151 passed; session/HTTP 82 passed;
Ruff/compile/diff check exit 0. The candidate has two additional fourth-file
regressions; final counts and exact CI SHAs will be appended after execution.
Local evidence directory: `/tmp/nzcoder-recovery-r123.62vsc2` (not committed).

| Commit | Scope |
| --- | --- |
| `7209c93` | R1 shared handle identity and real file/checkpoint regressions. |
| `4d82717` | R2 joint risk, relevance, recency and bounded identity reservation. |
| `ecab373` | R3 mixed-effect CLI scope, escaping and conflict diagnostics. |
| `0c5e878` | R1 overwrite descriptor cleanup on `fdopen` failure. |
| `c8f20da` | R2 archive revalidation and conflict priority from review. |
| `7404b28` | R3 existing pending operation ID on refused Redo. |
| `946c8c5` | Parent-resume integration assertion for the intended recovery prefix. |
| `2f4cd63` | R2 unresolved fourth-file selection before preview truncation. |
| `48cf233` | R2 minimum identity cannot be displaced by oversized optional references. |

Remaining boundaries are unchanged: no arbitrary Bash/MCP rollback, no filesystem
ACID or power-loss proof, no full Windows ACL/POSIX metadata restoration, no new
ledger GC, and no atomic cross-generation index publication. Artifact availability
is checked at attachment; ordinary subsequent quota/expiry races still require a
safe read failure, not an indefinite retention guarantee.

### Checks at 48cf233 (before the native Windows follow-up below)

Production changes for this verification pass are frozen at `48cf23384843fc056062522154e387320d6757b8`;
`df62839ee428785873d58102c2ae8c38e015fabd` adds only this report and learning notes.
The commands below ran on that unchanged source in
`/home/pyh/nzcoder/.worktrees/tool-recovery`, Linux/Python 3.13.12.

| Actual command | Result / exit code |
| --- | --- |
| `python -m pytest -q tests/recovery --tb=short` | 154 passed in 20.08s; exit 0 |
| `python -m pytest -q tests/test_session_revert.py tests/test_http_service.py --tb=short` | 82 passed in 14.60s; exit 0 |
| `python -m pytest -q --tb=short --junitxml=/tmp/nzcoder-recovery-r123.62vsc2/linux-reviewed.xml` | **1 failed, 3934 passed, 35 skipped** in 530.57s; exit 1 |
| `python -m pytest -q tests/test_repo_intelligence_service.py::test_service_watcher_incrementally_indexes_create_change_delete --tb=short` | diagnostic isolated rerun: 1 passed in 0.39s; exit 0; does not erase the full-suite failure |
| `python -m ruff check nz_coder tests` | All checks passed; exit 0 |
| `python -m compileall -q nz_coder tests` | exit 0 |
| `git diff --check` | exit 0 |
| `python -m build --no-isolation --wheel --sdist --outdir /tmp/nzcoder-recovery-r123.62vsc2/reviewed-dist` | wheel + sdist built; exit 0 (`reviewed-build.log`) |

Independent reviewer command on `48cf233`:

```sh
python -m pytest -q tests/recovery/test_recovery_priority.py tests/recovery/test_model_recovery.py tests/recovery/test_recovery_entries.py tests/recovery/test_windows_identity.py tests/test_subagent.py::test_subagent_can_request_parent_input_and_resume --tb=short
```

Reviewer-observed result: **95 passed in 13.09s**, exit 0. Final review found no
remaining Critical/Important code blocker in R1/R2/R3, retained expected identity
checks and output admission, and found no second recovery authority. The earlier
findings and the separate index/graph limitation remain documented above.

Fresh package commands ran **outside** the checkout, in
`/tmp/nzcoder-recovery-r123.62vsc2`, using a new venv without system site packages:

```sh
python -m venv /tmp/nzcoder-recovery-r123.62vsc2/venv
/tmp/nzcoder-recovery-r123.62vsc2/venv/bin/python -m pip install /tmp/nzcoder-recovery-r123.62vsc2/final-dist/nz_coder-0.1.0-py3-none-any.whl
/tmp/nzcoder-recovery-r123.62vsc2/venv/bin/python -m pip install --no-deps --force-reinstall /tmp/nzcoder-recovery-r123.62vsc2/reviewed-dist/nz_coder-0.1.0-py3-none-any.whl
XDG_STATE_HOME=/tmp/nzcoder-recovery-r123.62vsc2/reviewed-state /tmp/nzcoder-recovery-r123.62vsc2/venv/bin/python -I /home/pyh/nzcoder/.worktrees/tool-recovery/tests/recovery/fresh_install_smoke.py
/tmp/nzcoder-recovery-r123.62vsc2/venv/bin/python -m pip check
/tmp/nzcoder-recovery-r123.62vsc2/venv/bin/nz-coder --version
python -m pip wheel --no-deps --no-build-isolation /tmp/nzcoder-recovery-r123.62vsc2/reviewed-dist/nz_coder-0.1.0.tar.gz --wheel-dir /tmp/nzcoder-recovery-r123.62vsc2/reviewed-sdist-wheel
/tmp/nzcoder-recovery-r123.62vsc2/venv/bin/python -m pip install --no-deps --force-reinstall /tmp/nzcoder-recovery-r123.62vsc2/reviewed-sdist-wheel/nz_coder-0.1.0-py3-none-any.whl
XDG_STATE_HOME=/tmp/nzcoder-recovery-r123.62vsc2/reviewed-sdist-state /tmp/nzcoder-recovery-r123.62vsc2/venv/bin/python -I /home/pyh/nzcoder/.worktrees/tool-recovery/tests/recovery/fresh_install_smoke.py
/tmp/nzcoder-recovery-r123.62vsc2/venv/bin/python -m pip check
```

The initial `final-dist` wheel installs dependencies only; it predates the last R2
changes and is **not** the final package acceptance. The `reviewed-dist` wheel and
wheel rebuilt from its sdist contain the frozen source. Both isolated SDK smokes
exit 0, assert imports from the venv, restore non-UTF-8 bytes through Undo/Redo and
print `fresh-wheel SDK recovery smoke passed; provider_calls=0`. Install/build
commands exit 0, both `pip check` runs exit 0, CLI version is `0.1.0`. Network
dependency installation bypassed the host's failing proxy for those commands only;
no user proxy or credential configuration was modified.

The final Linux full suite is **not green**. Its remaining failure is
`test_service_watcher_incrementally_indexes_create_change_delete`, at the first
`_wait_until` for symbol `first` after creating `live.py`. No KeyError appears in
this run. This is not proof that the earlier mixed-generation KeyError has the
same cause; the timeout's cause remains unconfirmed. A single isolated diagnostic
rerun passes. `git diff fad7056 -- nz_coder/intelligence tests/test_repo_intelligence_service.py`
is empty. No watcher/index code, timing assertion or skip marker was changed to
obtain a green result. Both the prior index race and this observed failure remain
follow-up items outside R1/R2/R3. The full failure prevents claiming all required
checks passed or giving an unconditional merge-review recommendation.

### Actual remote CI at df62839 and Windows lock follow-up

All four remote workflows finished on
`df62839ee428785873d58102c2ae8c38e015fabd`, without rerunning failed jobs:

| Workflow | Observed outcome |
| --- | --- |
| [Core Runtime 34022405906](https://github.com/violetnozomi/coding-agent/actions/runs/34022405906) | success; Linux Python 3.12 full suite 3934 passed, 36 skipped in 652.99s, exit 0; does not erase the local Linux failure |
| [Repo Intelligence 34022405948](https://github.com/violetnozomi/coding-agent/actions/runs/34022405948) | success |
| [Windows Installer 34022405884](https://github.com/violetnozomi/coding-agent/actions/runs/34022405884) | success; not a substitute for native product acceptance |
| [Windows Product RC 34022405886](https://github.com/violetnozomi/coding-agent/actions/runs/34022405886) | **failure**; native product job 101457259478: 5 failed, 518 passed, 20 skipped in 436.73s, exit 1; fresh-install step skipped. Linux product sanity passed. |

The actual native command still includes the entire `tests/recovery` directory.
The Windows identity regression file has no skip and no failed case; the old
device mismatch is absent. However, these five native failures remain evidence:

1. `test_http_abort_retires_stream_part_before_run_settles`: second run did not
   reach completed within the test's wait.
2. `test_http_run_settled_is_the_manager_commit_barrier`: received
   `server.heartbeat` where the test expected `session.run.settled` next.
3. `test_sdk_cannot_recover_a_live_owned_session`: nonblocking Windows lock
   acquisition leaks `PermissionError(errno=13)` instead of the shared live-owner
   refusal. This is a deterministic recovery entry bug, not a timeout.
4. `test_http_conflict_exposes_typed_details_without_overwriting_user_file`:
   client response timeout; the server had generated the correct RecoveryError,
   then its attempted response encountered WinError 10053 after client abort.
5. `test_http_recovery_required_is_safe_and_retry_completes_actual_journal`:
   client response timeout.

The four HTTP cases are **not diagnosed as merely a slow runner**, nor silently
declared fixed. Their timings/assertions and production HTTP code are unchanged.
Native logs are retained locally as `windows-df62839.log`; Core full output as
`core-df62839.log` in the same evidence directory, alongside the original
`windows-raw-fad7056.log`.

`f3343c6d265070693cac85f0503e5398da133a98` fixes only the confirmed recovery lock
entry failure. `foundation/file_lock.py` normalizes nonblocking CRT lock
acquisition EACCES/EAGAIN/EDEADLK to `BlockingIOError`; the existing reverter maps
that to typed `RecoveryError`. It does not catch path permission errors, alter
blocking behavior, enter the protected body, or unlock a byte it never owned.
The contender descriptor still closes, allowing a later retry.

```sh
python -m pytest -q tests/recovery/test_recovery_locks.py --tb=short
python -m pytest -q tests/recovery/test_recovery_locks.py tests/recovery/test_recovery_entries.py tests/security/test_protocol_and_lock_limits.py --tb=short
```

First command before the fix: **2 failed, 3 passed**, exit 1. Second command after
the fix: **39 passed, 1 existing skip** in 5.47s, exit 0. The actual native live
owner SDK test is retained; Linux CRT fault injection does not replace it.
Full/source-package/native checks must now be repeated on this new source;
none of the prior green checks alone approves this follow-up. No merge is made.

### Post-lock final-source verification

Production source: `f3343c6d265070693cac85f0503e5398da133a98`.
Pushed verification commit: `48c7a25575bdfc4a0ec2aea41fddf7bb968de96b` (only the
acceptance record changed after `f3343c6`). A direct push first failed with a
GitHub connection timeout, exit 128. The normal retry using the existing local
proxy succeeded; GitHub's ref API confirmed the exact remote SHA. No force push
or global proxy/credential configuration change was used.

Linux/Python 3.13.12, same unchanged production source:

| Actual command | Result / exit code |
| --- | --- |
| `python -m pytest -q tests/recovery --tb=short` | **159 passed** in 20.04s; exit 0 |
| `python -m pytest -q tests/test_session_revert.py tests/test_http_service.py --tb=short` | **82 passed** in 14.42s; exit 0 |
| `python -m pytest -q --tb=short --junitxml=/tmp/nzcoder-recovery-r123.62vsc2/linux-winlock.xml` | **3940 passed, 35 skipped** in 507.76s; exit 0 (`linux-winlock.log`) |
| `python -m ruff check nz_coder tests` | All checks passed; exit 0 |
| `python -m compileall -q nz_coder tests` | exit 0 |
| `git diff --check` | exit 0 |
| `python -m build --no-isolation --wheel --sdist --outdir /tmp/nzcoder-recovery-r123.62vsc2/winlock-dist` | wheel + sdist; exit 0 (`winlock-build.log`) |

The final local Linux full run is green, but the earlier index watcher failure remains
valid evidence of unresolved stability. No timing/skip/assertion change was made
to that test. The Windows lock delta also passed independent source review; the
reviewer found no remaining code blocker in that bounded change.

From `/tmp/nzcoder-recovery-r123.62vsc2` outside the checkout, the following actual
commands all exit 0 and repeat package verification on the **post-lock** source:

```sh
/tmp/nzcoder-recovery-r123.62vsc2/venv/bin/python -m pip install --no-deps --force-reinstall /tmp/nzcoder-recovery-r123.62vsc2/winlock-dist/nz_coder-0.1.0-py3-none-any.whl
XDG_STATE_HOME=/tmp/nzcoder-recovery-r123.62vsc2/winlock-state /tmp/nzcoder-recovery-r123.62vsc2/venv/bin/python -I /home/pyh/nzcoder/.worktrees/tool-recovery/tests/recovery/fresh_install_smoke.py
/tmp/nzcoder-recovery-r123.62vsc2/venv/bin/python -m pip check
python -m pip wheel --no-deps --no-build-isolation /tmp/nzcoder-recovery-r123.62vsc2/winlock-dist/nz_coder-0.1.0.tar.gz --wheel-dir /tmp/nzcoder-recovery-r123.62vsc2/winlock-sdist-wheel
/tmp/nzcoder-recovery-r123.62vsc2/venv/bin/python -m pip install --no-deps --force-reinstall /tmp/nzcoder-recovery-r123.62vsc2/winlock-sdist-wheel/nz_coder-0.1.0-py3-none-any.whl
XDG_STATE_HOME=/tmp/nzcoder-recovery-r123.62vsc2/winlock-sdist-state /tmp/nzcoder-recovery-r123.62vsc2/venv/bin/python -I /home/pyh/nzcoder/.worktrees/tool-recovery/tests/recovery/fresh_install_smoke.py
/tmp/nzcoder-recovery-r123.62vsc2/venv/bin/python -m pip check
```

Both smokes again report `provider_calls=0`. No paid/live Provider or SWE run was
used anywhere in this repair. Windows Product RC and Core Runtime at `48c7a25`
remain pending at draft time; both results must be recorded before a full
acceptance decision.

### Final native results and handoff

All four workflows on `48c7a25575bdfc4a0ec2aea41fddf7bb968de96b` have now finished:

| Workflow | Actual result |
| --- | --- |
| [Core Runtime 34023362618](https://github.com/violetnozomi/coding-agent/actions/runs/34023362618) | **success**; Linux/Python 3.12 full suite **3939 passed, 36 skipped**, 776.71s, exit 0. Python 3.10 compatibility and installed-wheel contract jobs also success. |
| [Repo Intelligence 34023362621](https://github.com/violetnozomi/coding-agent/actions/runs/34023362621) | **success** |
| [Windows Installer 34023362626](https://github.com/violetnozomi/coding-agent/actions/runs/34023362626) | **success** |
| [Windows Product RC 34023362619](https://github.com/violetnozomi/coding-agent/actions/runs/34023362619) | **success**; native Windows/Python 3.12.10 job 101459899232: **528 passed, 20 skipped**, 369.12s, exit 0. Linux sanity also success. |

The Windows raw job log confirms `tests/recovery` is in the executed command,
not skipped as a directory. Both native product acceptance and the subsequent
wheel/sdist/fresh-install smoke steps completed successfully. Existing-file
identity, checkpoint Undo/Redo and live-owned-session refusal are included in
that native suite. No new Windows skips, expected-check relaxation, timeout
extension or workflow weakening were used. Logs: `windows-48c7a25.log` and
`core-48c7a25.log` in the local evidence directory.

The earlier Windows runs at `fad7056` and `df62839`, local Linux failures and
operator-stopped intermediate runs remain in this report. In particular, this
green Windows run **does not establish the cause or resolution of the four HTTP
failures at df62839**. Those remain a separate stability follow-up, as do the
index/graph publication race and the observed watcher wait failure. No failed
run was rerun on the same SHA until green; a new native run followed the specific,
regression-backed Windows lock fix.

R1, R2 and R3 now have implementation, RED/GREEN regressions, independent code
review, full local Linux, native Windows and package evidence on the same final
production source. In this frozen scope, there is no remaining confirmed code
blocker; this supports **human merge review with the recorded limitations**, not
a claim that the entire product is free of bugs. No merge or PR was created.

Additional handoff commits after the table above:

| Commit | Responsibility |
| --- | --- |
| `df62839` | First append-only R1–R3 failure/evidence report and learning notes. |
| `f3343c6` | Confirmed native CRT lock-contention error normalization and regressions. |
| `48c7a25` | Preserve actual failed Linux/Windows runs and the lock follow-up evidence. |

The following documentation-only handoff commit adds the final measured results.
It does not alter runtime, tests or workflows. The `nz_coder` Git tree object at
both production `f3343c6` and fully CI-tested `48c7a25` is
`0842f59486ee42f8d7d9dfe63413393268143013`; this also permits explicit comparison
with the final report commit without pretending that the report itself existed
before the runs. Any newly triggered checks for the documentation commit are
reported separately in the final handoff, not substituted for the native source
evidence above. The repair worktree/branch is retained and root `main` is untouched.

## Follow-up: repository view consistency (2026-09-06)

The historical mixed-generation failure above is addressed in the separate
`codex/repo-intelligence-consistency` dependency branch, based on `c26c134`.
Production commit: `f37e876cd2339e0778fa81574585afb68f04be2f`.
This does not reopen or redefine the accepted R1/R2/R3 recovery scope.

Index update, graph update, published generation and query-cache invalidation now
share one service boundary. The actual `RecoveryJournal._invalidate` calls the
same `refresh_paths` as normal committed edits. A failed refresh propagates through
the existing RecoveryError path instead of letting Undo claim completed. Applied
file receipts remain durable; retry avoids duplicate file writes/history commits,
and verification remains invalidated.

`tests/test_repo_intelligence_consistency.py` includes Event-synchronized actual
Undo/Redo queries during index publication, related-test/call-edge content checks,
and a real refresh failure after file restoration with write/history counters.
All 31 dedicated cases pass locally; a fixed ten-run supplementary check also
passed all ten runs. Initial RED evidence and the complete T1–T11 mapping are in
[the new acceptance record](../../docs/repo-intelligence-consistency.md).

At this append's creation, final-source full Linux and native Windows CI are still
being observed. Earlier failures here remain historical evidence: the confirmed
native startup gap has its own proof and fix, but cannot be assumed to explain
every prior watcher wait failure. Windows HTTP intermittent failures remain a
separate stability issue, not silently closed by this index repair.

Final follow-up evidence on frozen source `f37e876`: local Linux full
**3971 passed / 35 skipped**, exit 0; remote Core Runtime **3970 passed / 36 skipped**,
exit 0. Native Windows job `101495811676` executed the new consistency file and
completed **559 passed / 20 skipped**, exit 0, followed by successful wheel/sdist
and fresh-install steps. All four workflows on that SHA succeeded without reruns.
The exact commands, log paths, microbench cost and limitations are appended in
the new acceptance record. This closes the known mixed-generation/recovery-refresh
scope, not every historical HTTP or watcher stability symptom.

## Follow-up: ToolRuntime ownership boundary (2026-09-07)

The separate `codex/runtime-boundary-tightening` branch starts at `6c1c0e5`,
preserving accepted recovery and index consistency. It moves actual tool
transaction/result/post-write policy to focused owners; it does not rewrite
RecoveryJournal, change ledger schema, or redefine Undo/Redo guarantees.

Production commit `2626d39`, contract/CI commit `08c3c07` share production tree
`45aea5b0d3d5111f0199ec253615f7d618f21dea`. Final frozen local Linux acceptance:
`env -i PATH="$PATH" HOME="$HOME" LANG=C.UTF-8 PYTHONUTF8=1 python -m pytest -q --tb=short`
→ **4044 passed / 35 skipped**, exit 0. The combined Native Runner, package,
architecture, tool contracts, recovery, session-revert and HTTP run completed
**428 passed**, exit 0. Scope/commands and retained intermediate failures are in
[Runtime boundaries](../../docs/architecture/runtime-boundaries.md).

Real-component regressions keep the distinction between execution and admitted
output, committed/compensated/partial file effects, and the first resumed request.
Direct/external checkpoint cancellation waits for already-started real JSON Store
workers; the final stored interrupted state cannot be overwritten by that retired
tool batch. Progress content waits for output admission. Pure text rewrites retain
validated Plan/handoff control facts without restoring private output aliases.

The initial full runs (21 failures, then 3 compatibility failures) remain recorded.
Required fixture migrations retained all original assertions; Question metadata
and Bash progress regressions were fixed in production, not by weakening their
tests. Historical Windows HTTP/watcher failures above remain independent records.
Native Windows on this branch's current code is pending remote CI at this append;
the earlier recovery/index Windows green runs are not substituted for it.

First remote evidence is now available for `ab67968`: Python 3.12 full suite
**4043 passed / 36 skipped**, exit 0; native Windows job `101604689751`
**669 passed / 20 skipped**, exit 0, with this phase's tool contracts included,
followed by successful build and fresh-install checks. Repo Intelligence and
Windows Installer also passed. Core Runtime as a whole **failed**, because two
new Python 3.10 test assertions compared cancellation object identity across
`asyncio.run` (282 other tests passed). The original errors remained chained;
this stdlib behavior was reproduced independently before correcting the test
boundary. The new async assertion remains exact; the sync check follows only
cancellation wrappers to the original error, and keeps compensation assertions.
Independent skip-compensation and mask-primary mutations still fail. This is a
test-only correction; production tree remains `45aea5b0d3d5111f0199ec253615f7d618f21dea`.
The detailed commands, failed log and validation are appended to the architecture
record. Corrected-commit CI must be recorded separately, not called a passing rerun
of this failed workflow.
