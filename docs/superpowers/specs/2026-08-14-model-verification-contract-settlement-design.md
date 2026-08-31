# Model Verification Contract Settlement Design

## Goal

Treat a successful model-issued Bash command as satisfying the user-declared
verification contract when its tokenized command exactly matches the contract
for the current mutation generation. This prevents Runtime from rerunning the
same acceptance suite at the next budget boundary.

## Chosen Design

`VerificationContract` owns command equivalence and mutation-scoped attempt
state. `RuntimeState.observe_tool()` observes successful, executed Bash results
and returns a small acceptance observation when an exact declared command is
seen. The production `AgentLoop` forwards that observation to the existing
`VerificationManager.observe_acceptance_contract()` consumer.

Runner-generated contract calls carry an internal marker and remain settled by
`AgentRunner._execute_due_verification_contract()`. This avoids double-counting
the same synthetic call while preserving native Runner tests whose fake tool
service intentionally bypasses the production Tool Runtime observer.

The prompt state reports that the declared acceptance command passed for the
current mutation generation and tells the model to finalize without more tools.
A later successful source edit increments the generation, so the old evidence
cannot satisfy the new patch.

## Safety Boundaries

- Match tokenized commands exactly; commands with pipes, redirects, chaining,
  extra flags, different targets, or different runners do not satisfy the
  contract.
- Only an executed, non-dispatch-failed, exit-zero result may pass.
- Preserve failure output and record failed exact attempts, allowing the next
  mutation generation to re-arm naturally.
- Do not auto-terminate or synthesize a final answer.
- Add no dependency and do not alter Bash permissions or workspace safety.

## Alternatives Rejected

Blocking every verification-like tool after a pass still burns a model turn and
may suppress explicitly requested additional evidence. Automatically ending the
run after Bash would save more calls but removes the model's final explanation
and changes terminal semantics. The observation-based approach composes with
the existing transcript loop and is therefore the minimal reference-aligned
choice.

## Verification

- Red/green tests for exact match, non-match, failure, synthetic skip, generation
  invalidation, prompt guidance, and VerificationManager settlement.
- Focused RuntimeState, verification contract, Runner, Tool Runtime, and loop
  tests plus Ruff.
- One fresh isolated real task with independent acceptance and JSONL counts.
- Full repository pytest and `git diff --check` before completion.
