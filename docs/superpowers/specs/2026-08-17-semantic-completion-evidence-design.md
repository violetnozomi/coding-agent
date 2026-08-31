# Semantic Completion Evidence Design

Date: 2026-08-17

## Problem

NZ-Coder currently lets an exact acceptance command satisfy every requirement
that has no expected artifact. That is valid for a pure verification
requirement, but not for a compatibility promise. A passing existing test suite
only proves the behavior covered by that suite; it does not prove that an
explicit backward-compatibility clause was preserved.

The real cron task exposed the gap. The generated patch passed the repository
suite and preserved numeric descending-range rejection plus `0/7` Sunday
normalization, yet the new named wrap-around step form `FRI-MON/2` was not
covered and was rejected. The Runtime still marked the compatibility
requirement satisfied and finalized at the tool-batch boundary.

That boundary also bypassed the already implemented Sidecar Verifier. The
verifier only runs from the natural text-stop hook, whereas the Runtime can
finalize directly after an exact acceptance command at the nominal work limit.

## Reference findings

InfCodeX separates three concerns:

1. `deterministic-evaluator.ts` runs ground-truth build/test/lint commands and
   feeds their output back to the Worker. It explicitly does not treat a
   per-step LLM judge as a replacement for deterministic checks.
2. `KodaXTaskVerificationContract` carries task-specific criteria and required
   evidence into `verifier-context-builder.ts`.
3. The sidecar verifier receives the current user request, recent transcript,
   mutation facts, final claim, and additional criteria. Its result is a
   bounded accept/revise/blocked state transition.

infcode-dev/OpenCode independently reinforces the same boundary discipline:
`session/processor.ts` captures a workspace snapshot before a model step,
persists the resulting patch after the step, and normalizes malformed terminal
signals instead of trusting provider finish metadata alone.

The useful shared principle is: deterministic execution evidence, semantic
contract evidence, and terminal state are separate facts. None should be
silently inferred from another.

## Options considered

### A. Add more generic prompt warnings

Tell the Worker to preserve compatibility and add boundary tests. This is
cheap, but it remains advisory and does not repair the terminal protocol.

### B. Hard-code domain probes

Teach the Runtime to run cron cases such as `FRI-MON/2`. This would catch the
fixture but would be benchmark-specific and unusable for SWE-bench.

### C. Add mutation-scoped semantic evidence to TaskContract

Keep exact acceptance as deterministic evidence. Compatibility requirements
also declare `semantic_review` as required evidence. The Sidecar Verifier sees
the concrete task contract and bounded diff evidence; only a real verifier
accept records that evidence for the current mutation generation. Tool-boundary
completion may invoke this semantic gate instead of bypassing it.

Option C is selected. It is domain-neutral, follows the two reference
architectures, and closes the production consumer chain.

## Data model

`Requirement` gains `required_evidence`, a validated tuple. Version 2 supports:

- `semantic_review`

The field is additive. Existing requirement kinds keep their current
deterministic behavior. A compatibility requirement defaults to requiring
`semantic_review` when a planner omits the field, so old planner output cannot
silently preserve the bug.

`EvidenceRef(type="semantic_review_passed", generation=N, fingerprint=...)`
records an accepted independent review. It is only valid for mutation
generation `N`. A later write invalidates the requirement until both exact
acceptance and semantic review are current again.

Malformed, timed-out, or provider-error sidecar results remain fail-open for
ordinary stop-hook behavior but do not create semantic evidence.

## Runtime flow

1. The Worker edits files.
2. Runtime executes the exact user acceptance command.
3. Passing acceptance records deterministic evidence. A compatibility
   requirement becomes a candidate, not satisfied.
4. At natural completion, or at a nominal tool-batch completion boundary where
   only semantic evidence remains, Runtime invokes the completion verifier.
5. Sidecar receives:
   - current user request;
   - recent transcript;
   - bounded actual diff excerpts per changed file;
   - exact acceptance state;
   - unresolved TaskContract requirements and required evidence.
6. A genuine `accept` records current-generation semantic evidence. `revise`
   reanimates the Worker with a bounded correction. `blocked` terminates.
7. Runtime recomputes the ledger after the verifier. It finalizes only when the
   exact contract and every required evidence item are satisfied.

## Efficiency boundary

The sidecar is forced only when a TaskContract has unresolved semantic-only
requirements. Ordinary trivial edits retain the existing content-aware sidecar
gate. A successful semantic review adds one provider call but avoids another
main-agent exploration turn.

The seventh/eighth trace difference is not a generic Runtime search loop. The
extra recovery was caused by a fixture test with a hard-coded old workspace
`cwd`. That is recorded as harness contamination and will not be solved by
loosening Agent convergence policy.

## Failure behavior

- Exact acceptance fails: deterministic failure remains primary; no semantic
  review can make the task complete.
- Sidecar says revise: append one focused synthetic follow-up and continue,
  subject to the existing reanimation budget and hard cap.
- Sidecar unavailable/malformed: do not fabricate semantic evidence; the
  compatibility requirement remains unresolved.
- A later mutation: invalidate the semantic acceptance through the existing
  generation transition.
- No semantic requirements: preserve the current fast path.

## Acceptance criteria

- Exact acceptance alone cannot satisfy a compatibility requirement.
- A current-generation real sidecar accept plus exact acceptance can satisfy it.
- Fail-open verifier traces cannot satisfy it.
- A later edit invalidates prior semantic evidence.
- Tool-batch terminal settlement cannot bypass a pending semantic contract.
- Verifier input includes task requirements and bounded actual diff evidence.
- Existing non-compatibility completion behavior stays unchanged.
