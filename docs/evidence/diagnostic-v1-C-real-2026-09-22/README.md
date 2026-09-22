# 1. Baseline

Frozen HEAD/origin/main: `391e12d8e17f397476094cba8828c33b6b07e172`.
Tracked worktree was clean; production behavior remains
`265b3a30284c7c8ca6042ef0faae7e17e48d1d39`.
No production code, task, fixture, acceptance or historical evidence was modified.
This is one long-horizon multi-obligation diagnostic, not a benchmark.

# 2. Authorization

After offline preflight, the user explicitly confirmed exactly one C NZ run:
24 main physical requests and at most 8 naturally triggered auxiliary requests,
including retries. Actual: **24 main + 2 semantic verifier = 26**. All have recorded
responses; no additional retries, no resampling, A/B/M/Q/S or InfCodeX.
LAUNCH_C is an exclusive one-shot marker. The run is finished.

Settings match recent Q: deepseek-v4-flash / openai-compatible / api.deepseek.com;
stream=true; main max_tokens=64000; no explicit main thinking/reasoning_effort;
nominal/hard main=24; auxiliary cap=8; production verifier policy unchanged.
Bash timeout=120s, Provider idle=60s/hard=600s. Same run-nz.py bytes, permission
callback and built-in guards. Child network is denied except Unix model transport;
no embedding or online evaluation. This is not a filesystem jail.
preflight/config.json preserves the earlier unauthorized phase;
preflight/launch-config.json and authorization.json supersede launch status.

# 3. Initial state

V0: **5 project tests passed**, independent **1/12** (expected failure).
All 24 workspace hashes match the committed suite. task.md and CONFIG_SPEC.md
are unchanged; only workspace/ was materialized. Oracle/acceptance/manifest/ledger
were not copied to the Agent workspace or injected in the prompt.
Production workspace reads reject evaluator absolute/traversal paths in offline
preflight. No successful evaluator access was observed during the run.
Source tree SHA-256 (sorted path/hash JSON):
`bd3439e8f219afa340f0fcc7128bdae014c9a3b56abfab4fdfbdd8cfa6c15c50`.

# 4. Authority

CONFIG_SPEC.md original hash:
`0864cefb5fa0ce39152909de2b65b6a1248f7948f1eb97d3fa1d8aba0e1dee1f`.
Classification is task_reference/task_spec, required=false. Runtime captures
complete original text at generation 0, initial_user_instruction, epoch 0,
capture_status=captured, source_message_id empty, omitted count 0.
Main #1 reads the original version; Runtime subsequently marks model_observed=true.
Every captured version and final file retains the original hash. There is no
follow-up authority epoch. Both real verifier inputs contain the full retained
original reference, including the no-overwrite clause. See authority-evidence.json
and analysis/authority-timeline.json for digest and exact visible input evidence.

# 5. Exploration trajectory

Main #1 reads CONFIG_SPEC and tries a denied compound find command; #2 ls is denied;
#3 uses list_directory. #4 reads model/parser/validation/errors/writer/migrate/
defaults/exports. #5 reads store/service/CLI/paths/report/commands. #6 reads all four
original test files and docs. #7 grep_search traces validation and migration names.
No repo_context call was needed or observed. Read sets demonstrate actual access,
not proof of internal understanding. Subsequent source rereads at #18 follow a
real test failure. Full arguments, tool results and next-model-visible feedback
are in C/nzcoder/causal-table.json and trajectory.md.

The first Provider user message includes production-generated context-injection
(project profile, implementation workset and bounded spec/entrypoint snippets),
followed by the exact task text. This is normal production context construction,
not an evaluator hint; the original RunRequest task and command environment are
byte-identical to task.md. Auxiliary verifier requests use normal non-stream JSON
responses; streaming=true describes the main coding flow.

# 6. Mutation timeline

Stable snapshots are captured outside active write transactions. Observations may
first appear at the next request boundary; the mutation request is separately
identified below. Intermediate tool-result facts are retained but not declared
stable implementation versions. Acceptance/project reruns happen offline on copies.

| Version | G | Mutation request(s) | Changes | Offline project tests | Acceptance |
|---|---:|---|---|---|---|
| V0 | 0 | initial | none | 5 passed | 1/12 |
| V1 | 4 | #8 | errors, validation, parser, writer | 5 passed | 8/12 |
| V2 | 6 | #9 | CLI, migrate command adapter | 5 passed | 10/12 |
| V3 | 7 | #12 | _scratch_check.py | 5 passed | 10/12 |
| V4 | 8 | #16 | four existing tests plus test_migrate.py | 43 passed / 2 failed | 10/12 |
| V5 | 10 | #20 | parser/migrate test repairs | 46 passed | 10/12 |
| V6 | 11 | #22 | docs/README.md | 46 passed | 11/12 |

No production fixture business-code edit occurred after #9. No file mutation
occurred after V6 or between semantic reviews. versions.json contains hashes,
spec state, ledger, verification and evaluator obligation snapshots.

# 7. Independent acceptance

Final/best: **11/12**. Only invalid_write_preserves_destination fails.
The final writer serializes a directly constructed Config without validation.
store.save computes dumps then writes, but dumps itself does not reject invalid
values. An additional offline diagnostic shows
`save(existing_path, Config("demo", "localhost", False))` raises no error and
replaces the existing bytes with v2 JSON containing port=false.
See analysis/invalid-write-diagnosis.json. This check ran only after the real run,
on a disposable copy. It was not a hint and did not modify the frozen acceptance.

The failure is direct-save validation, not v1 parsing, CLI migration or dry-run.
A parser-first migration/export path rejects its invalid source correctly.
No version reached 12/12; no correct version was subsequently destroyed.

# 8. User obligation ledger

Thirty evaluator obligations preserve literal user/spec quotes, including items
with no acceptance check. Completion dimensions remain separate:

| Dimension | Result |
|---|---|
| Behavioral acceptance | 11/12; invalid direct write/no-clobber fails |
| Project-test obligation | pass: exact current command ran |
| Test-addition obligation | pass: meaningful new assertions added |
| Old compatibility tests | all five old test functions retain AST |
| Documentation obligation | pass: examples, migrate/dry-run/error shape present |
| Final-report obligation | pass for actual test result and limitations |
| Runtime terminal | completed |
| Full user task | **not fully satisfied** |

Runtime ledger has R1 broad behavior, R2 compatibility, R3 exact command. It does
not separately model direct invalid-save safety, meaningful tests or every spec
clause. R1/R3 close on verification; R2 additionally closes on semantic acceptance.
The evaluator ledger is not substituted for Runtime facts. Broad validation items
remain uncertain where only parser-side coverage exists; no claim of exhaustive
input correctness. Final ledger, test/report audits and obligation-analysis.json
preserve the distinction.

# 9. Test-addition audit

Four original test files modified; tests/test_migrate.py added; no test file removed.
All original five test function ASTs remain unchanged. New assertions exercise
v2 reads/writes, defaults, error paths/order, migration/idempotence, dry-run bytes,
CLI error JSON and source preservation. Final 46 cases pass.

At #17, two self-authored tests fail: JSON uses Python True rather than JSON true;
another passes a nonexistent destination to migrate_file and expects ConfigError.
At #18 the actual failure output is present in the next Provider request. The Agent
reads relevant files, repairs the two tests at #20, and #21 reports 46 passed.
These repairs align the tests with the spec, not a business-code improvement.
One added migration test asserts an unrelated unused destination stays absent,
which is weak, but the broader new suite is substantive. It misses direct
save(invalid Config) with an existing destination. Meaningful != exhaustive.

# 10. Verification

Main #10/#14 piped tests show 5 passed; #17 real command failure shows 2 failed /
43 passed; #21 piped tests show 46 passed. Runtime targeted bare pytest at #21
fails import with ModuleNotFoundError, separately recorded as command failure.
A runtime-owned py_compile attempt at #17 was denied, not a failed subprocess.

Main #23 executes exact `python -m pytest -q tests`, real exit success:
**46 passed in 0.02s**. Final mutation G=11, verification G=11.
Contract attempted_generation=10 and ledger generation=10 use the acceptance
generation namespace; README is the final distinct mutation. No stale generation
reuse is observed. Independent acceptance never supplies Runtime verification.

Six actual permission denials, two actual command failures, zero timeout.
Permission callback had twelve decisions, six allowed and six denied; do not
mistake twelve decisions for twelve denials. Policy was not relaxed.

# 11. Compaction

**Not observed.** No compaction events. Retained original authority is visible in
both reviews, but this run does not prove compaction recovery. No extra context
was injected to force it.

# 12. Semantic review

Verifier-1 (physical request 24, after main #23) returns revise/verifier_ok.
Its reason discusses comparisons with a previous output and asks for clarification;
it does not identify the direct-save defect or a concrete missing code change.
Main #24 (physical request 25) provides a detailed final report; no files change.

Verifier-2 (physical request 26) accepts/verifier_ok, citing the spec, exact
46-pass result, docs and extended tests. It misses the invalid direct-write path.
Both reviews had full original authority. No spec loss, no post-review code
regression and no destructive-review trajectory. An irrelevant first review and
missed final defect are observable reviewer findings, not proof of a deterministic
Runtime implementation fault. review_run_evidence itself was **not invoked**.

# 13. Terminal

Runtime finalized completed/natural_completion at main turn 24:
semantic_review_and_ledger_satisfied; no unresolved Runtime requirements;
semantic review generation 11. It did not hit max_turns, Provider error or fatal
infrastructure error. Harness exit=0, elapsed=203.401662433 seconds.
This is a task-completion mismatch: Runtime completed while independent behavior
was 11/12. Do not conflate completed with full correctness.

# 14. First divergence

Earliest relevant implementation omission: main #8, V1 writer rewrite, G4 settled
batch. New v2 serialization does not validate Config. This persists through final,
causing the single final acceptance failure. At V1 the task was still in progress;
the omission is confirmed as a final mismatch only when no later repair follows.

Classification: obligation_tracking in the model/business implementation domain.
This is not "V0 fails, therefore divergence". Nor can final files reveal the
model's internal interpretation: the exact first misunderstanding remains unknown.
The spec can be read by a model as chiefly parser validation, but frozen acceptance
and the user's explicit Config/no-clobber obligation cover this direct-write path.
That interpretive alternative is retained rather than silently recasting the test.

Earlier permission denials were recoverable via file tools. Later self-authored
test failures were model-visible and repaired, a normal recovery trajectory.
See analysis/first-divergence.json for evidence and alternative explanations.

# 15. Evidence strength

- Strong: original authority/read/hash binding and real verifier visibility;
  actual tool/test results; stable-version hashes; direct overwrite demonstration;
  test additions/old test preservation; final report and Runtime boundary.
- Moderate: missing direct-save obligation and coarse semantic closure are plausible
  contributors to the completion mismatch; first irrelevant revise consumed a turn.
- Insufficient: a deterministic Core root cause, lost authority, stale verification,
  destructive review, compaction/timeout recovery, or general efficiency effects.

# 16. Core finding

**No new deterministic Core bug proven.** A completion/semantic coverage candidate
exists: coarse R1/R2 closure accepted an implementation with an explicit behavior
gap. Actual TaskContract and ledger are retained. This alone does not prove
requirement extraction dropped a typed obligation, nor that Runner violated its
existing contract. A separate offline RED is required before production changes.

# 17. Model/task finding

**C-MODEL-INCOMPLETE.** Runtime evidence flow remains observable and the Agent
successfully repairs two test-authoring mistakes, adds substantive tests/docs and
reports actual results. It leaves direct Config write validation incomplete.
The reviewer does not catch that remaining gap. No claim of total task success.

# 18. Usage

| Purpose | Requests | Prompt | Completion | Total | Cache hit |
|---|---:|---:|---:|---:|---:|
| Main | 24 | 462097 | 27697 | 489794 | 163712 |
| Semantic verifier | 2 | 22164 | 573 | 22737 | 2944 |
| Combined | 26 | 484261 | 28270 | 512531 | 166656 |

**cost unknown**: Provider did not expose billing. No dollar estimate.
Raw reasoning-token counters, cache miss/hit, per-request duration and HTTP
response facts are in usage.json. Reasoning tokens are not added to completion
again; cache input is already included in prompt. Private reasoning text is not
published. All 26 physical requests are captured; no extra retry attempts.

# 19. What was NOT proven

One sample is not a success rate, does not represent A/B or all Suite tasks,
does not rank Coding Agents or establish general token efficiency. No InfCodeX
contemporaneous comparison. Unobserved compaction, timeout recovery and review-tool
interface behavior are not passes. 11/12 does not erase test/report obligations,
and their fulfillment does not erase the failed write-safety behavior.

# 20. Next action

Stop after evidence audit and push. No second C, no A/B and no Core edit.
If continuing this finding, separately build an offline regression around direct
invalid-write safety and the actual broad requirement/semantic closure state;
first establish what is deterministic and what is reviewer/model judgment.
Original experiment condition, task and acceptance stay frozen.

Evidence commands are in commands.jsonl; hashes/integrity audit cover source and
historical immutability, request caps, task/config, stable versions and privacy.
The raw frozen workspace.diff contains single-space blank context lines required
by unified-diff syntax. Git whitespace checking reports these payload lines;
checking all other staged files passes. The original frozen diff is preserved
rather than rewritten to silence a formatting check.
