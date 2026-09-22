# 1. Baseline

Frozen code: `265b3a30284c7c8ca6042ef0faae7e17e48d1d39`.
This is one NZ Concurrent Q sample. No production source was changed.

# 2. Authorization

The user explicitly confirmed exactly one new NZ Q real run, including production
semantic verifier calls. Physical caps: 24 main and 8 auxiliary, retries included.
No resampling, InfCodeX, Money M, storage, embeddings or online evaluation.
One-shot LAUNCH_Q prevents an automatic restart. Actual use: 12 main + 1 verifier.

# 3. Preflight

HEAD and origin/main matched the baseline, with clean worktree before preparation.
Source hashes, including model_gateway/stream.py, are in preflight/source-hashes.json.
Initial fixture hashes match both historical Q fixtures. Task text and REQUIREMENTS
are copied unchanged from complex-paired-preflight-2026-09-20. Exact task/config
and acceptance hash are in manifest.json and preflight/config.json.

| Field | Current / recent historical Q |
|---|---|
| Provider / model | openai-compatible, api.deepseek.com / deepseek-v4-flash |
| Streaming | true |
| Main output limit | max_tokens=64000 |
| Main thinking / reasoning_effort | neither explicitly supplied |
| Main nominal / hard cap | 24 / 24 |
| Auxiliary | existing production policy, max_tokens=1024, thinking disabled; safety cap 8 |
| Bash timeout | unchanged production 120 seconds |
| Permission | identical historical run-nz callback plus existing guards |
| Network | child IPv4/IPv6 denied; Unix transport proxy forwards chat/completions only |
| Runner | production NativeSDKRunner -> AgentRunner |
| Acceptance | original Q.cjs, unchanged, offline disposable copies |

Historical config difference: Core/tool schemas/prompts evolved between baseline
A and B. This run prioritizes B's configuration; the production difference from B
is the committed stream deadline fix. Fresh workspace/session identifiers differ.
No task hint or manual review call was supplied. run-nz.py is byte-identical to B.
Capture adds exception stack observation at bridge/Gateway boundaries, re-raising
unchanged; it never changes a decision. Published paths are pseudonymized and
private reasoning is removed while the original stream is forwarded to the Agent.

# 4. Historical Q baselines

| Sample | Acceptance | Runtime | Main / auxiliary | Review tool |
|---|---|---|---|---|
| A: complex-paired-2026-09-20/Q | 7/7 | completed | 16 / 1 | three internal evidence-field trials |
| B: post-authority-real-retest-2026-09-21/Q | 6/7 | error | 7 / 0 | not reached |
| Current | 7/7 | completed | 12 / 1 | not invoked |

A's three review calls actually dispatched; they are not three outer dispatch
failures. B timed out with circular-wait tests and missing README; no next main
request occurred. Those are separate historical baselines, not one merged metric.

# 5. Current real Q result

**Q-C: no timeout observed; task completed with semantic review.**
Independent acceptance: **7 passed, 0 failed**. Public tests: **25 passed**.
Runtime: **completed**, natural_completion / semantic_review_and_ledger_satisfied.
Main requests: **12**, auxiliary: **1**. Harness duration: **87.90735894 seconds**.
Five files changed: lib/pool.cjs, lib/batch.cjs, both test files, README.md.
Timeout count: 0. review_run_evidence calls: 0. Permission denials: 5.
No declared command failure, Provider failure or Runtime fatal was observed.

# 6. Timeout recovery

**Not observed.** See Q/nzcoder/timeout-recovery.json (`observed=false`).
This sample does not directly exercise the stream timeout repair. The earlier
offline RED/GREEN evidence remains the only direct timeout-recovery validation.
No timeout was manufactured, and no second sample was launched.

# 7. Model recovery behavior

No timeout feedback or subsequent recovery decision exists in this trajectory.
The Agent wrote implementation at request 4, tests at 5, README at 6, ran a piped
Node test at 7, attempted restricted Git inspection at 8/9/11, reread source at 10,
then returned a final answer at 12. Permission denials are separate authorization
facts, not a claimed reasoning failure. Complete tool arguments, next-model-visible
messages, state and usage are in Q/nzcoder/causal-table.json and trajectory.md.

# 8. Functional timeline

| Version | Mutation generation after batch | Real action | Independent acceptance |
|---|---:|---|---|
| V0 | 0 | Initial workspace | 1/7 |
| V1 | 2 | Main 4: pool and batch implementation | 6/7, documentation missing |
| V2 | 4 | Main 5: two test files | 6/7, documentation missing |
| V3 | 5 | Main 6: README | 7/7 |
| Final | 5 | No subsequent mutation | 7/7 |

Acceptance is reconstructed offline on each distinct captured file version and
never injected into the model. Batch snapshot timing may precede ledger updates;
settled request boundaries establish the listed generations. versions.json retains
all observations, hashes, requirement/verification/reference states and real
subprocess acceptance output. No intermediate bytes are inferred from final files.

Both newly generated test suites finish successfully; the previous circular-wait
test pattern did not manifest as a hang in these executions. This is evidence of
the current tests completing, not exhaustive proof that no schedule can hang.

# 9. README obligation

The original user task explicitly says "update README.md". It remains a requested
mutation artifact. Main request 2 reads it; request 6 changes it. The fixed
independent documentation check passes. At completion all requirement ledger
items are satisfied. REQUIREMENTS.md remains unchanged task-reference evidence.

# 10. review_run_evidence behavior

**Interface remained unobserved in this sample.** No review_run_evidence call,
zero-argument invocation, invalid argument attempt or duplicate-review sequence
occurred. Conditional attempt metrics are null in review-run-evidence.json, not
treated as proof of zero failures. The no-argument schema was exposed normally;
the task supplied no extra explanation. A semantic verifier call is distinct from
this tool and does not make its interface observed.

# 11. Semantic review

One natural verifier request (verifier-1, physical request 13) accepted, with no
revise and no subsequent mutation. Full visible input, retained authoritative
references/digest, verdict and events are in semantic-review.json and
authority-evidence.json. Requirement semantic review is bound to generation 5.

# 12. Terminal correctness

Main 7's piped command is not trusted as exact declared verification. At main 12's
natural stop, Runtime actually runs `node --test tests/pool.test.cjs tests/batch.test.cjs`:
exit 0, 25 passed. Then the verifier accepts and Runtime finalizes completed.
Mutation generation=5, verification generation=5, unresolved requirements=[];
the declared contract uses acceptance generation=4 (README is a distinct mutation).
Independent acceptance 7/7 and Runtime completion are separately recorded facts.

# 13. Usage

| Purpose | Requests | Provider prompt | Provider completion | Total | Cache hit |
|---|---:|---:|---:|---:|---:|
| Main | 12 | 167505 | 16989 | 184494 | 78848 |
| Verifier | 1 | 11243 | 389 | 11632 | 1024 |
| Total | 13 | 178748 | 17378 | 196126 | 79872 |

**cost unknown**: Provider billing was not exposed. No dollar estimate and no
inference from runtime cost=0. Cache input is already in prompt totals; reasoning
tokens are already included in completion totals. Raw per-request reasoning/cache
fields, model, duration, attempts and cost_source are preserved in usage.json.
Missing cache-write/billing fields are not inferred. All 13 requests have responses;
no extra physical retries or unauthorized calls were made.

# 14. Historical comparison

Only longitudinal Q comparison is supported. This sample fully delivers and
completes, unlike B. It consumes fewer main requests than A, but it has neither
timeout nor review-tool exposure, so that count does not establish an efficiency
gain. InfCodeX remains a historical reference, not a contemporaneous rerun.

# 15. Evidence strength

- Strong: real final public/independent subprocess pass, README mutation, exact
  Runtime verification, semantic accept and normal terminal boundary.
- Moderate: trajectory differs from B and is consistent with current Core being
  able to complete this task; model variance prevents isolating the stream fix.
- Insufficient: actual timeout recovery, review parameter-guessing elimination,
  general success-rate or token-efficiency improvement.

# 16. New Core findings

No new deterministic Core bug was observed. Restricted Git commands were denied
under the unchanged permission policy. No production changes were made in response.
There was no fatal error requiring an exception-stack investigation.

# 17. What was NOT proven

Not overall success rate, not a benchmark, not an InfCodeX contemporaneous
comparison, not general token efficiency. One Q sample does not represent all
tasks. No timeout and no review tool call mean the two primary behavior hypotheses
remain unobserved in this particular real run. No M or other task was resampled.

# 18. Next action

Stop after evidence audit and push. Preserve the offline timeout proof and this
successful real Q result separately. Any further real sample requires a new
explicit scope/authorization; this run supplies no new deterministic failure that
would justify another Core patch. Evidence is frozen with FROZEN.json; root audit
checks source/history invariance and publication safety, and SHA256SUMS.json indexes
the new directory. Existing evidence is never rewritten.
