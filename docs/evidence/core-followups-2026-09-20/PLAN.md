# Offline Core follow-ups

Authorization: user requested all locally actionable outstanding issues. 0 paid model requests.
Baseline HEAD e9e2491 plus existing uncommitted authoritative-reference implementation. Preserve old evidence.

1. Reproduce two compaction failures. Add explicit overflow compaction capability at Context boundary; preserve shared three-attempt owner and legacy/custom context compatibility.
2. Regression first for review_run_evidence: use Runtime-owned facts without asking the model to serialize ledgers; no model evidence overrides, no new reviewer or semantic bypass. Preserve requirement/generation/permissions and same-run binding.
3. Audit reference limitations for actionable deterministic errors; keep fixed budgets, no late capture of mutable originals and no invented missing authority.
4. Focused red/green tests then broad offline regressions; record every new command and result in commands.jsonl. Store final diff and hashes separately from earlier evidence.

## Findings and decisions

- Compaction: ContextExecutionContext.compact had no overflow-specific capability. Runner selected it and deliberately omitted the overflow kwarg; the adapter pointed at _compact_messages, whose default was false. Added optional compact_overflow capability and production binding. Existing custom contexts with only compact remain compatible. Original two failing assertions unchanged; six focused compaction checks passed.
- Review: four initial interface regressions failed (schema required evidence, unbound fabricated empty evidence accepted, production empty-argument calls failed). Tool now takes no arguments. A permission-checked ToolExecutor dispatch binds its own read callback in a scoped ContextVar; existing deterministic reviewer and CompletionGate consume actual RunEvidence/RuntimeState. No second ledger or reviewer. No callback -> unavailable. Existing direct Python analysis API remains available.
- Test fixture correction: simple successful tasks legitimately ended at the existing early-completion boundary after four requests, before scripted review #3. Increasing fixture capacity did not change that behavior. Final fixture uses existing real semantic hook with controlled revise then accept, leaving Runtime terminal logic unchanged. This is not a real-model efficiency experiment.
- Unsettled write-batch regression failed before guard. Tool returns pending_tool_batch until transaction facts settle; stale generation and missing caller remain unresolved. Per-executor concurrent callbacks are isolated and denied tools never invoke callbacks.
- Reference scope: bootstrap collapsed newlines before shared classification. A following ordinary source filename inherited task_spec in bootstrap while capture classified it as context. Reproduced with Follow SPEC.md + newline + api.py; preserve original clause boundaries. 116 reference/bootstrap/requirement checks pass.
- Safety limits retained intentionally: no unbounded specification memory, no fabricated reconstruction of original files missing from old checkpoints, no claiming partial reads are complete. These are evidence boundaries, not failures to bypass.

- Architecture expansion found two independently reproduced baseline failures (e9e2491: 2 failed/20 passed). Reviewed actual sites: reference_adapter now has exactly two local node/bun --version probes after npx removal; tool_runtime/policy only inspects TypeError internally for legacy scope-keyword compatibility and does not project it publicly. Updated audit inventory with accurate boundary descriptions, leaving public/model-visible exception prohibition intact. Architecture plus context-budget checks: 59 passed.

- Strengthened production review replay to require completed, two real semantic-hook requests, and no stall-sidecar verdict. The no-argument path then failed: L2 interprets identical {} signatures across real mutations as repeats, invokes its auxiliary boundary and exhausts the controlled Provider script. Added a red policy regression (1 failed/1 passed) and reused the existing deterministic closure exemption for review_run_evidence. Consecutive local doom guarding remains active. Final 51 checks pass, including both production trajectories and permission/concurrency/stale/missing-caller/write-batch counterexamples.
- Broad offline regression: 1513 passed/4 skipped. This preceded the strengthened final review assertion; final focused validation separately covers that new change. No unclassified new failure was attributed to historical compaction.
