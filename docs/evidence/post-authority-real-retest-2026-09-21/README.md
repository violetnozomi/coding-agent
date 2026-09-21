# 1. Baseline

Production commit: `45db8e38a8de58d404b6601a47b2fe5b518f248a`.
At preflight, HEAD and fetched origin/main matched this commit; worktree was clean.
Production source, prompts, permissions and historical evidence were not edited.
This directory contains two real runs, offline projections, diagnostics and the report.
The evidence commit is a descendant of the frozen production baseline.

# 2. Authorization

The user's explicit confirmation covered one NZ Money M run followed by one NZ
Concurrent Q run, each with at most 24 physical main requests and 8 naturally
triggered auxiliary requests. Retries count against these caps. No automatic
rerun, InfCodeX request, embedding or online evaluation was authorized or made.
M was frozen before Q started. Both runs have finished. No further model calls
were made for analysis. See [authorization.json](authorization.json).

# 3. Config equivalence

| Field | Historical and current configuration |
|---|---|
| User task | Byte-identical task text; exact text and hashes in preflight/config.json |
| Initial workspace and REQUIREMENTS | Original fixture files, verified SHA-256 equality |
| M initial git | 066d5073d43d3ae2bdde79e82e700d3325aec0c7 |
| Q initial git | 41242f6ee14b0e80e09e3272ac198d99c60f036f |
| Model / Provider | deepseek-v4-flash / openai-compatible / api.deepseek.com |
| Stream / main output cap | true / max_tokens=64000 |
| Main thinking / reasoning_effort | Neither explicitly supplied |
| Main nominal and hard cap | 24 each; no increase |
| Auxiliary policy | Existing production semantic verifier; max_tokens=1024, thinking disabled; physical safety cap 8 |
| Runner | NativeSDKRunner -> AgentRunner; historical run-nz.py unchanged |
| Permission policy | Same permission callback and built-in guards |
| Network | Child IPv4/IPv6 sockets denied; chat/completions forwarded over Unix socket; not filesystem-hermetic |
| Independent acceptance | Original M.py and Q.cjs, unchanged and executed offline |

The experimental differences are the frozen Core/tools/prompts at 45db8e3 and
fresh isolated workspace/session paths. No additional task hint was supplied.
Capture instrumentation retains runtime states, original reference evidence and
version snapshots; it does not replace completion decisions or mutate ledgers.
The gateway strips private reasoning from saved responses while forwarding the
original upstream stream to the production runner. No hidden reasoning is published.
Local host paths in evidence are replaced with `<REPO>` / `<USER_HOME>`.
See [preflight/config.json](preflight/config.json), request bodies and source manifests.

# 4. Money M result

**Classification A: 19/19 independent acceptance and normal completion.**

| Measurement | Observed |
|---|---|
| Best / final independent acceptance | 19/19 / 19/19 |
| Public final tests | 18 passed |
| Runtime terminal | completed; natural_completion; semantic_review_and_ledger_satisfied |
| Main / auxiliary requests | 17 / 1 |
| Duration | 83.880993174 seconds |
| Semantic verdict | accept, verifier_ok |
| Permission denials / executed command failures | 3 / 1 |
| Mutation / verification generation at completion | 12 / 12 |
| Final unresolved requirements | none |
| Post-review mutation / first divergence from best | none / none |

REQUIREMENTS.md was retained with SHA-256
`37b543633f7cc036163840ca97ed9f6256e4e456b1f4b93d2c2e959cc44b1bcf`,
matching the initial and final file. It had `authority=task_spec`,
`source=initial_user_instruction`, `authority_epoch=0`, `captured_generation=0`,
`complete=true`, `model_observed=true`; the initial source_message_id is empty.
The verifier's actual visible input contains the full original specification in
the independent AUTHORITATIVE TASK REFERENCES section. It accepts the explicitly
requested new output shapes and does not request restoration of the old shapes.
The original reference never becomes a required mutation artifact.

This is direct evidence of reference delivery and the observed verdict. It does
not isolate that mechanism from all other Core changes or model sampling variance.
See [authority evidence](M/nzcoder/authority-evidence.json),
[semantic review](M/nzcoder/semantic-review.json),
[terminal boundary](M/nzcoder/terminal-boundary.json).

# 5. Money causal timeline

| Point | Real action and independent reconstruction |
|---|---|
| Initial, G0 | Independent suite: 1 passed, 1 failed, 17 errors; no migration |
| Main 4, G1-G6 | API and five callers written; incorrect amount scaling remains |
| Main 5, G7-G8 | Tests written; independent 15/19 |
| Main 8, G8 | Exact declared pytest command executes: 8 failed, 9 passed |
| Main 9, G9 | First repair; independent still 15/19 |
| Main 10, G10 | Amount repair; independent 18/19, documentation still missing |
| Main 11 | Piped pytest output reports 17 passed; not trusted exact declared verification |
| Main 13, G11-G12 | Test assertion and README changed; first full 19/19 at G12 / captured V5 |
| Main 14 | Piped pytest reports 18 passed; verify_changed_files supplies current static evidence |
| Main 15-16 | Permission denial then source read; no further mutation |
| Main 17 | Provider stop/final; Runtime executes exact declared pytest: exit 0, 18 passed |
| Verifier 1 | Original authoritative specification visible; accept |
| Final | G12 preserved, independent 19/19, completed |

The exact declared contract at completion records attempted_generation=11 in its
acceptance-generation namespace. Runtime mutation generation and verification
generation are both 12; these counters must not be conflated. Static evidence at
main 14 did not erase the failed older exact command; Runtime ran that command at
the completion boundary.

All 12 successful mutations were replayed offline from their actual tool inputs
against the original workspace. Every resulting generation has hashes and an
independent acceptance result in
[money-generation-timeline.json](analysis/money-generation-timeline.json).
This is a deterministic reconstruction, not a live acceptance intervention.
Settled next-request/final snapshots and final files match reconstruction hashes.
Batch-completed events precede ledger updates and can expose new bytes with old G;
they are not used to assert an intermediate generation's file identity.
The first analyzer attempt caught this mismatch and is recorded in commands.jsonl.

Full request-level parameters, visibility, usage, requirement and authority states:
[M trajectory](M/nzcoder/trajectory.md) and [causal table](M/nzcoder/causal-table.json).

# 6. Concurrent Q result

**Classification C: final delivery acceptance regresses to 6/7; review efficiency
cannot be evaluated because the run fails before reaching review.**

| Measurement | Observed |
|---|---|
| Independent acceptance | 6 passed, 1 failed; all six behavior checks pass, required README text missing |
| Public tests on frozen final copy | timeout at 45 seconds |
| Production declared Node test | executed, timeout at 120019.697 ms |
| Runtime terminal | error; no terminal_boundary_settled event |
| Last real Provider finish_reason | tool_calls |
| Main / auxiliary requests | 7 / 0 |
| Duration | 311.368680625 seconds |
| Review calls / valid zero-arg / extra-arg attempts | 0 / 0 / 0 |
| Semantic reviews / revise | 0 / 0 |
| Permission denials | 2 |
| Unused main request budget | 17 |

The process exits 0 because the harness captured a Runtime error result. This is
not task completion. The synthesized final assistant stop is not a model stop.
The public assistant error is APIError/internal_error with RuntimeError metadata;
the raw private exception is unavailable, so its exact cause is not established.

At request 7, Node genuinely executes. Preserve the raw overlapping flags:
`executed=true`, `dispatch_failed=true`, `command_failed=false`,
`permission_denied=false`, `timed_out=true`, `exit=null`, `cancelled=false`.
The timeout has no recoverable subprocess output. It is neither a permission
denial nor a real nonzero exit fact. There are three raw dispatch-failed results
in Q, two denials and this executed timeout.

All seven upstream requests return HTTP 200 and complete SSE streams. Request 7
has `[DONE]`, no downstream closure, and completes in 1490.0984 ms before the tool
starts. After the tool timeout, model_call_finish records completed/tool_calls,
but provider_turn_settled records provider_error/error and the run ends. Thus the
provider_error label alone is not evidence of an upstream Provider failure.

Two unchanged final tests reproduce a circular wait offline: pool.test.cjs:115
and batch.test.cjs:92 await the draining operation before releasing the worker's
gate. Each isolated test times out at 2000 ms with 1 cancelled, exit 1. These
diagnostics use test-name filters on disposable copies, not altered fixtures.
They explain a model-generated test-design failure. They do not by themselves
explain why the Runtime terminates instead of allowing another main turn.
See [diagnosis](analysis/Q-unmodified-test-diagnosis.json),
[termination](analysis/Q-termination.json), [Q trajectory](Q/nzcoder/trajectory.md)
and [causal table](Q/nzcoder/causal-table.json).

# 7. Q review-efficiency timeline

| Historical Q | Current Q |
|---|---|
| Main 13: evidence changed_files/verification; review failed, modified_files missing | Main 5: writes implementation, G1-G2 |
| Main 14: modified_files/verification; needs_fix, verification_results missing | Main 6: writes tests, G3-G4 |
| Main 15: modified_files/verification_results; approved | Main 7: Node timeout, Runtime error |
| Main 16 + auxiliary: completed, 7/7 | No review or auxiliary request reached |

Historical review calls actually dispatched and executed: their problems were
internal evidence-field trials, not three outer tool-dispatch failures. Current
requests contain the real zero-argument schema (`properties={}`, `required=[]`).
Schema delivery is proven; removal of autonomous parameter exploration is not.
There are no same-generation duplicate reviews, pending-batch reviews or reviews
after new verification in the current run because there are no review calls.
See [before/after](analysis/concurrent-before-after.json) and each run's
review-run-evidence.json (empty for these current runs).

# 8. Historical comparison

| Task | Historical NZ | Current NZ |
|---|---|---|
| M | best 19/19 -> destructive revise -> 17/19, max_turns, 24 main + 2 auxiliary | 19/19 retained through grounded accept, completed, 17 + 1 |
| Q | 7/7, completed, 16 main + 1 auxiliary, three field trials, five permission denials | 6/7, error, 7 + 0, review not reached, two denials |

M baseline is money-paired-retest-2026-09-20; Q baseline is
complex-paired-2026-09-20/Q. The M baseline has **two** auxiliary requests; an
earlier preflight verbal count of one was incorrect. Raw history governs here.
InfCodeX at d3a812379b589597347f5be12d5b68477e577f02 remains a
**historical comparison, not concurrent rerun**. No new InfCodeX inference is made.
Permissions are counted separately from coding and review behavior.

# 9. Usage

| Purpose | Requests | Prompt | Completion | Total | Cache hit (included in prompt) |
|---|---:|---:|---:|---:|---:|
| M main | 17 | 290593 | 14879 | 305472 | 110848 |
| M verifier | 1 | 11279 | 323 | 11602 | 0 |
| Q main | 7 | 77766 | 42473 | 120239 | 43904 |
| All | 25 | 379638 | 57675 | 437313 | 154752 |

**24 main + 1 auxiliary physical requests; no retries; cost unknown.**
Provider-reported reasoning-token subtotals are 5623 for M main and 36570 for Q
main. The verifier does not expose that field; absence is not a reported zero.
Reasoning tokens are already part of Provider completion totals, not added again.
Cache miss totals are 179745 / 11279 / 33862 respectively. Cache-write and reliable
billing fields are absent; no dollar estimate or runtime-cost=0 inference is made.
Per-request raw usage, duration, model, HTTP result and purpose are preserved in
[M usage](M/nzcoder/usage.json) and [Q usage](Q/nzcoder/usage.json).

# 10. Evidence strength

- Strong: M original reference hash/content was actually delivered to the verifier;
  full acceptance persisted after accept and Runtime completed.
- Strong: Q generated tests contain reproducible circular waits; a real Node
  timeout preceded terminal error; review was not reached.
- Moderate: Q stream/tool settlement control flow is a Core diagnostic candidate;
  read-only source corroboration identifies possible error settlement paths.
- Insufficient: exact private RuntimeError cause; Q review efficiency improvement;
  isolated causal contribution of any one Core patch; cross-sample rate estimates.

The M sample no longer exhibits the historical spec-loss/destructive-review
trajectory and completes with independent acceptance. This is one real sample,
not proof that the fixes always prevent such outcomes.

# 11. New Core issues

Observation: a successfully completed upstream tool-call stream plus an executed
tool timeout is followed by terminal Runtime internal_error/provider_error,
without using the remaining 17 main requests.

Causal evidence: HTTP completion precedes tool execution; timeout precedes
provider_turn_settled/error. The source paths in
[source-corroboration.json](analysis/source-corroboration.json) include stream-tool
settlement and the runner's post-tool stream-error handling. Alternatives include
a local SDK/stream/callback exception whose original detail was not captured.
Confidence is high in the event sequence, insufficient for a specific Core fix.
No production patch or asserted failing Core regression is included.

The circular-wait tests are a separate coding trajectory failure. Missing README
is a real unfinished requirement, not evidence that the requirement gate should
be relaxed. M exposes no new destructive-review event.

# 12. What was NOT proven

This is not an overall success-rate result, benchmark, model capability ranking,
new concurrent InfCodeX A/B, or general token-efficiency conclusion. Fewer Q
requests reflect premature failure, not demonstrated efficiency. Neither task
was resampled. No production code was changed after observing results.

Capture limitations: process duration and physical HTTP duration are different;
version acceptance is post-run reconstruction; raw tool-batch snapshots may
precede ledger settlement; the sanitized Runtime error omits its private stack.
Independent acceptance is the fixed suite, not a proof of every conceivable input.

# 13. Next action

Stop this experiment. A subsequent offline task should first reproduce the Q
successful-stream + timed-out-tool settlement with a controlled Provider and
assert the exact termination path. Only a demonstrated failing Core regression
justifies a minimal fix. Do not raise budgets or relax permissions. Q's review
efficiency question remains unanswered and would require separately authorized
real retesting after any independently justified repair.

Artifacts: per-request tables, raw sanitized requests/responses, runtime and tool
events, authority snapshots/digests, semantic inputs, ledgers, acceptance, final
diffs and source hashes live under M/nzcoder and Q/nzcoder. Their FROZEN.json
manifests protect the run evidence. Root integrity-audit.json checks those
manifests, source/history invariance and publication safety; SHA256SUMS.json
indexes this new evidence directory. Commands and analyzer failure/correction are
recorded in commands.jsonl. No older evidence directory is rewritten.
