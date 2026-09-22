# 1. Historical C observation

Baseline `e5cea83009d7ebf0e37991e4cd854c0f68de4a8f`, initially clean main and
origin/main tracking ref. Historical C: **11/12**, project **46 passed**, Runtime
**completed**, 24 main / 2 verifier. Direct invalid Config serialization still
overwrites an existing destination. All those historical facts remain unchanged.
This audit performs no real-model run and does not repair the C business workspace.

# 2. TaskContract origin

The historical trace records `task_contract_bootstrapped` with three requirements,
no planning request/event, and no later rich contract. Production replay uses
Native product environment creation, lifecycle `prepare_runtime_state`, then
`ProductRunEnvironment._maybe_generate_plan`, stopping before Provider execution.
Disabled/unavailable planner reproduces the exact historical contract:

- R1: `derive_task_contract` unconditionally adds broad behavior from top-level task.
- R2: preserve/compatibility wording adds compatibility with required semantic review.
- R3: the extracted exact pytest command adds verification.

Controlled coarse/detailed planner outputs survive parse and Runtime binding;
the detailed direct-write requirement is not dropped. Classification **A1**, not A3.
The optional planner prompt also contains task/mode/criteria/exact command, not the
retained spec body. No real planner answer is fabricated in this audit.

# 3. Named spec handling

Lifecycle independently captures CONFIG_SPEC as original task authority. Fallback
extraction reads task text, not retained spec clauses. The phrase following
`described in CONFIG_SPEC.md` belongs to the shared reference scope; mutation scope
is only `Implement the configuration migration `. The general inclusion phrase
does not match explicit test-change detection. Thus tests/docs/no-clobber do not
become separate typed requirements, though all remain explicit user obligations
in the full authoritative spec given to the reviewer. This is a coverage limitation
of conservative extraction, not demonstrated loss of a supplied detailed contract.

# 4. Satisfaction mode semantics

Eight observation cases are saved in ledger/satisfaction-mode-matrix.json.
No-artifact behavior in semantic/mixed/deterministic mode closes on exact pass.
Verification semantic also closes. Compatibility stays candidate until review.
Docs/test/artifact cases with genuine mutation evidence close; deterministic
docs/artifact can close on mutation alone. Missing mutation remains protected by
the existing artifact regressions.

Mode participates in deterministic artifact closure, **not** in exact verification
promotion. `required_evidence` controls additional review. Existing design A257,
A259 and A264 supports these transitions; a universal "semantic requires LLM"
invariant was not found. General mode semantics remain underspecified.

# 5. Verification authority

`acceptance=True` is supplied when the model executes the declared exact command,
or Runtime executes that same contract. It is not the external evaluator oracle.
It records `verification_passed`; false records `command_passed`, which requires
a related targeted command and a suitable candidate. The semantic certification
prompt explicitly says exact pass proves execution only. See the authority matrix.

# 6. Ledger closure

R1/R3 obtain verification_passed; R2 obtains it plus semantic_review_passed.
The ledger's effective acceptance generation is 10; total mutation generation 11
includes the final documentation update. The terminal boundary checks current exact
contract, unresolved items, invokes review and recomputes the ledger. Nothing in
this audit demonstrates stale evidence or a bypass of those existing checks.
The broad R1 does not separately certify direct-save safety; evaluator obligations
are mapped retrospectively in analysis/obligation-runtime-map.json.

# 7. Semantic verifier context

Both actual requests contain complete original CONFIG_SPEC, its hash, observation
state, exact user task, R1/R2/R3 descriptions/required evidence and trusted 46-pass
output. They do **not** contain a fine-grained clause inventory or the complete
ledger status/evidence object. Ordinary tool output is not rendered into the short
transcript; exact verification output is separately projected.

**New deterministic finding:** diff context is not correctly attributed per file.
ChangeTracker emits Markdown file headings plus difflib headers. `_diff_sections`
only recognized Git extended headers, so the whole changeset became one section.
Repeated prefixes under scratch/CLI/migrate spent the budget; the writer's own
section said `(diff evidence omitted: budget exhausted)`. Some writer body is
nevertheless present inside duplicated earlier chunks; do not claim total absence.
Unmodified store source is not included. Correct authority does not imply complete,
correctly attributed implementation evidence.

Verifier-1's odd previous-output comparison is absent from its genuine query and
input. System role separation and synthetic labels are present. The phrase enters
verifier-2 through correctly labeled prior review guidance. No deterministic user
role contamination was established.

# 8. Narrative-only review behavior

Code, generation, verification and authority are unchanged between reviews. Final
report and rolling transcript (including synthetic feedback) change; runtime phase
and budget zone change too. First reason asks for clarification, not a concrete
code repair. Controlled reject -> accept at the same generation closes R2, matching
existing report-revision tests. Narration alone cannot write review evidence; an
accepted verifier verdict is required. Internal reasons for the model's verdict
change are unknown. Same-generation re-review is not itself a bug.

# 9. Deterministic bug?

**Yes: per-file diff projection.** Contract/ledger semantics were observed but not
proven to violate an established universal semantic-mode invariant. The earliest
proven host divergence in this review path is producer/consumer diff format mismatch
before verifier-1. It is separate from the earlier model omission at main #8.
Its causal contribution to the missed no-clobber defect is **unproven**.

# 10. Exact RED

Before production change: **4 failed, 16 passed**. Failures cover actual
ChangeTracker -> Sidecar file attribution, frozen C writer projection, plain unified
and ChangeTracker formats. Git-format counterexample passes. The added hook packet
check exercises `_evidence` and final rendering with no Provider. These tests assert
evidence boundaries, never that a fake LLM must return revise.

After the minimal fix: focused **20 passed**. The first settings-stub error is
separately documented in PLAN.md and is not counted as a production RED.

# 11. Production change

Only `nz_coder/runtime/verification/sidecar_verifier.py::_diff_sections` changes:
recognize native ChangeTracker section headings and plain paired unified headers
when Git headers are absent. Existing Git handling and budgets stay intact. No
TaskContract extraction, ledger mode, verifier prompt or terminal-policy change.
No changes to fixture, acceptance, oracle or historical evidence.

# 12. Counterexamples

Git format, literal Markdown in a diff hunk and existing truncation budgets remain
covered. Broad regressions cover artifact mutation requirements, compatibility
review, no-command tasks, native completion, Money/Node/Q failure-domain paths,
security and the offline diagnostic fixtures. The corrected C packet is an offline
projection, not a claim of a new accepted/revised model response.

# 13. Regression results

Exact commands and results are in commands.jsonl; raw focused/broad logs are under
before/ and after/. Final broad regression: **1584 passed, 11 skipped**, exit 0
(498.49 seconds). Ruff passed. Offline known-bad final workspace still has **46 project passes
and 11/12 acceptance**. That is the expected diagnostic witness, not a regression
introduced by the patch. All subprocesses use the existing seccomp launcher and
bounded fixture runner. See after/broad-tests.txt for final aggregate results.

# 14. What remains model-dependent

The reviewer may still accept with the corrected diff. Full spec visibility does
not guarantee detection; tests omit the direct-invalid-save case. This audit cannot
prove why verifier-1 confused its role internally, nor that an obligation checklist,
different model or different closure policy would solve C. Bounded diff can still
omit later files; omission markers remain. General semantic-mode semantics and
natural-language obligation extraction need a separately defined product contract.

# 15. Paid calls

**0 paid model requests. 0 real C reruns. 0 A/B real runs.** No embedding,
InfCodeX, dependency installation or online evaluation. Controlled planner fixtures
return fixed JSON before any Provider call. The external acceptance stays evaluator-only.

# 16. Next action

Stop after offline validation and evidence push. A future separately authorized real
sample could measure reviewer behavior with corrected file attribution; no reduction
in missed obligations, request count or coding success rate is claimed here.
