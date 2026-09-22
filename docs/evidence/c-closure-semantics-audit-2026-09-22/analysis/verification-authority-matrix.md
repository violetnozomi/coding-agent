# Verification authority matrix

| Evidence | Actual producer/authority | Current ledger effect | Limit |
|---|---|---|---|
| Exact user-requested test command | extract_verification_contract -> matches_command -> RuntimeState.observe_tool, or Runner exact execution | acceptance=True; closes no-artifact behavior/verification; artifact requirements need mutation; required semantic review remains pending | User asked to run it; not proof of exhaustive coverage |
| Broad project tests not matching exact contract | verification planner/runtime command evidence | Not automatically acceptance=True; no-artifact pending behavior stays pending | A green suite is not a hidden evaluator |
| Related targeted test | classify_verification_command + artifact stem relation | Can close artifact-backed candidates; semantic requirement evidence is still checked | Filename relation does not prove exhaustive behavior |
| Static compile | static stage | Does not close behavior through targeted relation | Syntax only |
| Runtime-owned exact execution | Runner _execute_due_verification_contract | Same declared command and generation, acceptance=True | Runtime-owned means execution provenance, not full semantic coverage |
| Runtime-owned external acceptance oracle | No distinct evidence authority type in this audited ledger | No separate production path established here | Do not invent one from variable name |
| Evaluator independent acceptance | Frozen C checks on disposable copies | None; never injected into Runtime | Retrospective diagnostic only |
| Semantic review | Real accept + verifier_ok/fuzzy_tool_match | Adds semantic_review_passed only where required; needs current verification_passed | Model judgment can be wrong; unavailable/provider failure is not acceptance evidence |

The boolean acceptance means the declared exact verification lane, not the external
12-check oracle and not a measured proof that the command covers all user semantics.
Changing its authority would require a separately specified policy and compatibility
tests. This patch changes no evidence types, generation rules or ledger transitions.
