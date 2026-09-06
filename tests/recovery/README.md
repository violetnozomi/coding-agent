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
