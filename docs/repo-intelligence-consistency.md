# Repository Intelligence consistency — I1 / I2 / I3

## Scope and baseline

Branch: `codex/repo-intelligence-consistency`, based on recovery milestone
`c26c134f9fb3a2afc7d0a40bd35fee22102a3dfa`. At branch creation, fetched
`origin/main` was `89124f9870e61a38290b1af1c97b0529da7188bb` and did not contain
the milestone. This is a dependency branch, not a replacement for the recovery
branch. Root main and the previous worktree remain unchanged. No PR or merge,
real Provider call or SWE run is part of this task.

The previous recovery acceptance remains valid in its stated scope. This round
addresses only index/graph/query-cache consistency, including edit and Undo/Redo
refresh. Historical failures in `tests/recovery/README.md` are not erased.

## Source audit and choice

The original `service._apply_incremental` committed `index.update_paths`, then
read a snapshot and mutated `graph`, and only then cleared cache/published state.
`symbol_context` could get a newly created definition from SQLite and ask the
old graph for that module's related tests, raising `Unknown repository module`.
`_cached_query` read state generation before computing without a common boundary.
Even a cache hit could hide an ordinary edit indefinitely: the normal
`update_code_index_after_write` hook bypassed the service entirely. Map refresh
and reference tools also performed their own scans/reads.

`IndexSnapshot` contains frozen file/symbol/import/call dataclasses and tuples,
but a `RepositoryGraph` retains a live `PersistentCodeIndex` and calls it again
for symbols, incoming/outgoing edges, related tests and process/module contexts.
Swapping graph references alone would not isolate those reads. SQLite commit,
graph journal persistence and Python state assignment were three separate events.

| Option | Trade-off |
| --- | --- |
| A: shared update/read boundary (chosen) | Reuses the existing index lock and queries; no second database or read/write-lock framework. Reads can degrade within their existing wait budget during updates. |
| B: detached candidate index + graph publication | Supports old-view reads during rebuild, but would require isolating all graph-to-index reads, not merely swapping two mutable references. More scope and memory/connection ownership. |

## Contract and real entry points

`service._update` owns the shared view/index RLock from the first mutation through
graph completion, `_publish`, cache invalidation and published state. Both
watchers, prewarm/rebuild, `scan`, `refresh_paths`, and explicit LSP augmentation
enter it. Updates serialize; they do not build out of order and overwrite a
newer publication. In-process low-level index writes use that same database
lock. If such a standalone write leaves the service behind, the next service
query detects the generation mismatch and returns failed/unavailable, not mixed data.

| Product path | Publication/read path |
| --- | --- |
| Cold/prewarm/rebuild | one executor → `_build` → `scan` → `_update` |
| Native watcher | install subscription → first event/timeout reconciliation → `_apply_incremental` → `refresh_paths` |
| Polling watcher | fingerprint/debounce + retained failures → same `refresh_paths` |
| Normal successful tool write | ToolExecutor → post-write observer / `ProductRunEnvironment._refresh_code_index` → `update_code_index_after_write` → existing service `refresh_paths` |
| `repo_map(refresh=True)` / reference refresh | service `scan`; reads use `read_index` under the published boundary |
| `repo_context(refresh=True)` | prewarm/rebuild; public queries use `_read_view` / `_cached_query` |
| Undo/Redo/recover | SessionReverter → RecoveryJournal.apply → `_invalidate` → existing service `refresh_paths` |
| Explicit LSP call enrichment | bounded existing resolver → `_update` → graph generation and cache publication |

Without an existing service, ordinary write hooks retain their standalone index
behavior. A later service starts cold and rebuilds graph before becoming ready.
Evaluation-only direct index/graph construction remains standalone; no production
tool retains a raw service index/graph bypass.

Generation means the SQLite index generation represented by the successfully
published graph and service. The database may advance during a build while the
service retains its last published generation and is `updating`/`warming`.
Only `_publish` can make the complete pair ready. Repeated unchanged path events
reuse the same mtime/size fingerprint and do not advance the database generation;
`scan(refresh=True)` / `repo_map(refresh=True)` still force reparsing. This inherits the existing fingerprint limit:
an external edit that preserves both size and mtime may evade detection.

Every structural query attempts to acquire `_read_view` within the existing wait budget;
failure to acquire returns an explicit fallback.
Generation validation, all index/graph subqueries, result labeling and cache
writeback run while the pair is pinned. An old query finishes before its queued
writer publishes; afterward that writer clears its old cache entry. Failure,
truncation and fallback results are not cached. Successful cached results are
deep-copied so callers cannot mutate future answers. LRU capacity/hits remain
active; epoch changes prevent configuration invalidation being undone by late work.

Optional embedding is the deliberate exception to holding this lock for a whole
calculation: it captures a frozen `IndexSnapshot`, then calls the provider outside
the structural boundary. Generation + epoch + current state are checked before
cache writeback. Superseded/abandoned results cannot poison the current cache.
The semantic store's own lock prevents mixed vectors/chunks and old snapshots
replacing newer vectors. A stalled provider cannot hold the structural refresh
lock after the caller times out.

Structural generation does **not** freeze the filesystem, Git status or a language
server. Results identify Git/file enrichment as sampled at query/embedding build,
not versioned with the index; LSP augmentation identifies its external source.
Cached enrichment is not a promise of current worktree content.

## Failure, recovery, lock order and lifecycle

Index, graph and prepublication failures clear caches and mark the view failed.
Failed paths are retained and merged into the next update, including an unrelated
event; a failed scan requires a full scan repair. A graph failure after SQLite
commit is repaired from a complete index snapshot rather than a partial delta.
Restart always begins cold; persisted index or graph files alone never imply ready.
Watchers retry retained failures and continue consuming events. Native subscription
precedes startup reconciliation so writes during reconciliation remain buffered.

If Undo restored files but index refresh fails, `_invalidate` propagates through
the **existing** RecoveryError path. Per-file `applied` progress and verification
invalidation are already durable; history is not falsely committed as completed.
Retry reconciles source/target evidence, does not rewrite already restored files,
refreshes successfully and commits history once. No new recovery state machine,
checkpoint format or authority was introduced.

Lock order is view/index → short service state lock; the optional semantic-store
lock is acquired outside the structural boundary. Future waits, joins and tracer
callbacks are outside those locks. Reentrant refresh from query/update callbacks
is rejected. Worker/held-view `wait_ready` cannot wait on its own queued builder;
completed Futures are not used as historical proof of current ready state.
Queued cancellation becomes failed; close cancels queued work, returns without
joining its own executor, and blocks late ready/cache publication. Running filesystem
or optional-provider operations are not forcibly interrupted; close is not a promise
to terminate arbitrary blocked Python/native code. Watcher join retains its bounded
five-second shutdown limit. Query wait budget bounds lock/build waiting, not all
existing SQL/Git computation; higher-level operation budgets still apply.
Metrics copy service-owned fields and the semantic reference under the state lock,
then sample provider properties outside it; even a provider status property waiting
on embedding cannot retain the service lock. Metrics themselves may wait for that
provider; they do not promise an end-to-end deadline.

## Deterministic regressions and measured evidence

`tests/test_repo_intelligence_consistency.py` uses Events and bounded Futures,
with release in `finally` before worker joining. No fixed sleep is evidence.

| Requested case | Regression coverage |
| --- | --- |
| T1 new module / mixed graph | pause real index update before graph, query while paused; new definition and graph visible after release |
| T2 removal / rename / calls | old symbol removed, test dependency and caller targets updated; deleted target loses resolved identity |
| T3 in-flight query / cache | old structural reader pins version across queued update; old semantic result becomes superseded without writeback |
| T4 update failure | index/graph/metrics and prepublication failures, unrelated-path repair, persisted-index restart |
| T5 concurrent updates / watchers | explicit + watcher overlap, duplicate paths; real poll loop failure replay; native subscription startup window |
| T6 cold / rebuild | same bounded fallback contract while index scan is paused |
| T7 normal edit | real registered ToolExecutor write and production post-write hook, ledger and query contents |
| T8 Undo/Redo | actual journal restoration/deletion while queries run; related tests, call edges, history and verification |
| T9 failed recovery refresh | persisted file progress, pending operation, write/history counters and exactly one successful history commit on retry |
| T10 close / cancel / self-wait | close during build, queued cancellation, executor/read callback waits, reentrant tracer query/close |
| T11 cache | hits, bounded eviction, deep-copy isolation, invalidation, no exception/truncation cache |

Initial RED: 3 failures (mixed graph KeyError, stale normal-write cache, old-query
boundary), then expanded RED 9 failed / 2 passed. An intermediate implementation
used `index.generation` instead of `index.generation()`; its 9 failed / 2 passed
were corrected, not hidden. I3 RED: 2 failures (map bypass and swallowed recovery
refresh failure). Integration found duplicate-event generation churn and cancelled
build stuck warming (2 failed / 19 passed). Independent review reproduced stalled
embedding holding the structural lock and failed-event loss; regression fixes
followed. A missed semantic implementation signature caused 5 failed / 114 passed
and was corrected. Native startup test failed before subscription ordering changed.
Two later `wait_ready` tests failed before stale-Future/self-wait corrections.
A final review reproduced metrics holding the service lock while waiting on a
Provider status property (1 failed, exit 1); moving the property sampling outside
the lock fixed that RED. The partial-process regression also exposed insertion
into `_process_catalog` despite exclusion from the LRU; both stores now reject
truncated/fallback results.

Linux evidence directory: `/tmp/nzcoder-index-consistency.xdEiLF` (not committed).
Baseline targeted command: `python -m pytest -q tests/test_repo_intelligence_service.py tests/test_code_index.py tests/test_repository_graph.py --tb=short`
→ 41 passed, exit 0. Candidate extended focused suite → 145 passed, exit 0.
`python -m pytest -q tests/test_repo_intelligence_closure.py tests/test_verification_repo_intelligence.py tests/recovery --tb=short`
→ 177 passed, exit 0. Final source verification, native Windows and package results
are recorded below when observed; these subsets alone are not full acceptance.

Microbenchmark: `python -m tests.repo_intelligence_benchmark`; 3 fixed trials,
40 one-function Python files, max_files=100, 20 explicit single-file updates,
200 prefilled hot queries, 20 ms update-time query budget, temporary non-Git repo,
Python 3.13.12/Linux x86_64. All final edited symbols visible; all 200 hot queries
hit each trial. These are wall-clock observations, not model capability scores.

| Measurement | Baseline range (ms) | First candidate range (ms) |
| --- | --- | --- |
| Cold build | 10.17–12.03 | 21.07–24.10 |
| Per-trial median one-file refresh | 1.83–2.01 | 2.91–3.57 |
| Mean hot query | 0.0014–0.0034 | 0.191–0.300 |
| Deliberately paused update query | 0.07–0.16 (unprotected cached result) | 20.28–20.37 (explicit bounded fallback) |

The candidate measurements overlapped other local test work. Extra generation
validation and defensive copies have a measurable cost; this is not a throughput
improvement claim. No production timeout was enlarged to hide it.

## Acceptance status

At draft time: implementation and focused Linux regressions are present; full
final-source tests, native Windows, fresh-install and remote CI are pending.
No claim of I1/I2/I3 final acceptance is made solely from the subsets above.
The older Windows HTTP intermittent failures remain independent open stability
issues. The native startup defect here has its own deterministic proof; it is
not asserted to explain every historical watcher timeout.

### Frozen implementation and local evidence

Production commit: `f37e876cd2339e0778fa81574585afb68f04be2f`.
`nz_coder` tree: `47bc4f7c8093fa46c74d253b531808bce0b4f803`.
Normal push to the dependency branch succeeded (exit 0). No PR or merge was made.

The first full command, `python -m pytest -q --tb=short --junitxml=/tmp/nzcoder-index-consistency.xdEiLF/full.xml`,
returned **3968 passed, 35 skipped**, exit 0, 527.51 seconds. It began before the
last wait_ready/metrics changes and does **not** validate the final frozen source.
The final run uses `full-final.log/xml`; its outcome is appended once observed.

Final dedicated command `python -m pytest -q tests/test_repo_intelligence_consistency.py --tb=short`
returned **31 passed**, exit 0, 2.21 seconds. Fixed stress was declared before
execution: the same command ran exactly **10 times**, **31 passed on every run**,
all exits 0 (2.23–2.35 seconds per run). All ten runs count; none was discarded or
retried to obtain green. The command's shell loop accumulated a nonzero exit if
any iteration failed. This is supplementary confidence, not the primary race proof.

`python -m pytest -q tests/test_repo_intelligence_service.py tests/test_code_index.py tests/test_repository_graph.py tests/test_retrieval_intelligence.py --tb=short`
returned **96 passed**, exit 0, 3.58 seconds. Ruff, compileall and diff checks
passed (exit 0); required exact subsets are also being repeated on the frozen source.

Package commands (all exit 0):

```sh
python -m build --no-isolation --wheel --sdist --outdir /tmp/nzcoder-index-consistency.xdEiLF/final-dist
python -m venv /tmp/nzcoder-index-consistency.xdEiLF/venv
/tmp/nzcoder-index-consistency.xdEiLF/venv/bin/python -m pip install /tmp/nzcoder-index-consistency.xdEiLF/final-dist/nz_coder-0.1.0-py3-none-any.whl
XDG_STATE_HOME=/tmp/nzcoder-index-consistency.xdEiLF/wheel-state /tmp/nzcoder-index-consistency.xdEiLF/venv/bin/python -I /home/pyh/nzcoder/.worktrees/repo-intelligence-consistency/tests/repo_intelligence_fresh_install.py
/tmp/nzcoder-index-consistency.xdEiLF/venv/bin/python -m pip check
/tmp/nzcoder-index-consistency.xdEiLF/venv/bin/nz-coder --version
python -m pip wheel --no-deps --no-build-isolation /tmp/nzcoder-index-consistency.xdEiLF/final-dist/nz_coder-0.1.0.tar.gz --wheel-dir /tmp/nzcoder-index-consistency.xdEiLF/sdist-wheel
```

Install/smoke/sdist rebuilding execute in `/tmp/nzcoder-index-consistency.xdEiLF`,
outside the checkout. The venv excludes system site packages, and the smoke asserts
the imported package is inside that venv. SDK Undo/Redo and post-write index/graph
queries pass and report `provider_calls=0`; dependency check has no broken requirements.
Source-distribution wheel install and the same smoke are checked separately below.
Existing setuptools/wheel/SCM/Sphinx-environment warnings were retained in build logs;
no packaging dependency or test assertion was changed to hide them.

Independent read-only review accepted the final I1/I2/I3 code after the semantic
lock, failed-event replay, native startup and metrics findings were resolved.
Reader testing of this document confirmed generation, recovery, budget, external
provenance and pending-Windows boundaries; two overbroad phrases were clarified.
Review is source evidence, not a replacement for running CI.

### Final Linux and package results at f37e876

All commands below ran with the frozen production tree above; no source/test
changes were made during `full-final` execution.

| Actual command | Outcome / exit |
| --- | --- |
| `python -m pytest -q tests/test_repo_intelligence_consistency.py --tb=short` | 31 passed in 2.21s; exit 0 |
| `python -m pytest -q tests/test_repo_intelligence_service.py tests/test_code_index.py tests/test_repository_graph.py --tb=short` | 41 passed in 13.58s; exit 0 |
| `python -m pytest -q tests/test_repo_intelligence_closure.py tests/test_verification_repo_intelligence.py tests/recovery --tb=short` | 177 passed in 35.41s; exit 0 |
| `python -m pytest -q --tb=short --junitxml=/tmp/nzcoder-index-consistency.xdEiLF/full-final.xml` | **3971 passed, 35 skipped**, 512.95s; exit 0 |
| `python -m ruff check nz_coder tests` | All checks passed; exit 0 |
| `python -m compileall -q nz_coder tests` | exit 0 |
| `git diff --check` | exit 0 |

The source-distribution wheel was installed with
`/tmp/nzcoder-index-consistency.xdEiLF/venv/bin/python -m pip install --no-deps --force-reinstall /tmp/nzcoder-index-consistency.xdEiLF/sdist-wheel/nz_coder-0.1.0-py3-none-any.whl`, exit 0.
The same isolated smoke ran with `XDG_STATE_HOME=/tmp/nzcoder-index-consistency.xdEiLF/sdist-state`
and exited 0, again reporting `provider_calls=0`; `pip check` also exited 0.
Thus direct wheel and wheel rebuilt from sdist both executed the installed
post-write index and SDK recovery paths, not imports from the checkout.

After local suites finished, a second declared paired measurement ran exactly
three baseline trials followed by three frozen-source trials with the identical
benchmark file/configuration. Baseline used the clean recovery worktree at
`c26c134` with `PYTHONDONTWRITEBYTECODE=1 python -c "import runpy; runpy.run_path('/home/pyh/nzcoder/.worktrees/repo-intelligence-consistency/tests/repo_intelligence_benchmark.py', run_name='__main__')"`;
final source used `python -m tests.repo_intelligence_benchmark`. Both exited 0;
`before-quiet.json` and `after-quiet.json` retain every trial. No local suite was
running concurrently, though host load was not otherwise controlled.

| Metric | Baseline range (ms) | Frozen source range (ms) |
| --- | --- | --- |
| Cold build | 17.91–18.20 | 18.68–21.91 |
| Median one-file refresh | 3.04–3.43 | 3.22–3.57 |
| Mean hot query | 0.00265–0.00290 | 0.167–0.336 |
| Paused update query | 0.161–0.206, unprotected | 20.29–20.37, explicit fallback |

All trials retained 200/200 hot cache hits and final edit visibility. The large
relative hot-hit cost is real (generation validation opens SQLite plus payload
copying), even though measured absolute latency is sub-millisecond. Correctness
is the purpose of this change; optimizing that cost is not smuggled into this scope.

### Native and remote acceptance on the frozen source

All four workflows on `f37e876cd2339e0778fa81574585afb68f04be2f` completed successfully
without a failed-job rerun:

| Workflow | Actual evidence |
| --- | --- |
| [Core Runtime 34036642238](https://github.com/violetnozomi/coding-agent/actions/runs/34036642238) | success; Python 3.12 full suite **3970 passed, 36 skipped**, 711.00s, exit 0; Python 3.10 compatibility and installed-wheel jobs also success |
| [Repo Intelligence 34036642290](https://github.com/violetnozomi/coding-agent/actions/runs/34036642290) | success; dedicated consistency file included in actual workflow command |
| [Windows Product RC 34036642270](https://github.com/violetnozomi/coding-agent/actions/runs/34036642270) | success; native job **101495811676**, **559 passed, 20 skipped**, 300.30s, exit 0; Linux sanity success |
| [Windows Installer 34036642266](https://github.com/violetnozomi/coding-agent/actions/runs/34036642266) | success |

The downloaded Windows raw job log contains the executed
`tests/test_repo_intelligence_consistency.py` argument, its native pytest result,
successful wheel/sdist build and subsequent fresh-install smoke. Those later steps
were **not skipped**. Raw evidence: `windows-f37e876.log`, `core-f37e876.log` in the
local evidence directory. No new skip, assertion removal or timeout expansion was
used for these results. This is native execution, not a Linux WinAPI double.

I1, I2 and I3: **implemented and verified in this scoped contract**. Deterministic
RED/GREEN, cache crossing tests, normal tool edits, actual Undo/Redo, Linux full,
native Windows and installed-package checks satisfy this round's stop conditions.
No remaining confirmed in-scope blocker was found by the final read-only review.
It is appropriate to move to a separately authorized real coding-task baseline;
none is started by this task. The unrelated historical HTTP failures remain open,
and fingerprint, external sampling, process shutdown and filesystem boundaries
above remain limitations rather than hidden guarantees.

The following commit only updates this report, `docs/struct.md`, and the append-only
recovery record. Its production tree can be compared to `47bc4f7c8093fa46c74d253b531808bce0b4f803`.
Any CI triggered for that report-only commit is reported separately at handoff;
it is not retroactively substituted for the actual native source run above.
