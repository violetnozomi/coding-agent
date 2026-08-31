# Runtime-Owned Verification Contract Design

## Goal

Reduce model calls spent deciding, invoking, and recovering required verification. When the user explicitly names a workspace-local pytest command, NZ-Coder records it as a run-local contract and executes it through the existing Bash tool boundary during convergence.

## Scope

The first slice supports explicit `pytest` and `python -m pytest` commands with one or more relative test paths. It does not infer project-wide commands, execute absolute or escaping paths, install dependencies, or replace model-selected exploratory tests. Existing permission, timeout, path-containment, trace, and broad-test policies remain authoritative.

## Architecture

`runtime/verification_contract.py` owns parsing and mutable run-local state. A contract contains the normalized command, declared targets, mutation generation last verified, attempt count, and latest result. `ProductionRunLifecycle.initialize()` derives contracts from the last real user message and binds them to the active `RuntimeState`; persisted state supports resume without global mutation.

At a work-budget pressure boundary or immediately before an earlier natural completion, the Runner asks the contract controller whether verification is due. A contract is due only when the run has a source diff, the current mutation generation is newer than its last attempt, and the boundary is yellow, orange, red, or completion. The Runner executes the command through the normal tool execution service, so the command receives the same workspace, policy, cancellation, trace, and output projection as a model-issued Bash call. The settled result is appended as synthetic evidence before the next model request.

Passing evidence marks the current mutation generation verified. A later edit increments the generation and makes the contract due again. Failure also records the generation, so the same unchanged failure is not rerun repeatedly; the model gets one recovery turn and a new edit is required before another automatic attempt.

## Data Flow

1. Parse the real user request into zero or one bounded pytest contract.
2. Persist the contract with active runtime state.
3. On convergence-zone entry, check diff, generation, and prior attempt.
4. Execute via the existing Bash tool pipeline and emit normal tool/trace evidence plus `verification_contract_*` lifecycle events.
5. Append a compact synthetic user message containing command, status, and projected output.
6. Let the next model turn fix a failure or finalize a passing task.

## Error Handling

Malformed, pathless, absolute, escaping, piped, redirected, or chained commands do not become automatic contracts. Runtime execution never bypasses Bash classification. A dispatch denial or non-zero exit becomes failed evidence rather than a Runtime exception. Cancellation propagates normally. Only one attempt is allowed per mutation generation.

## Acceptance

- Chinese prose after `，` does not enter the command.
- Explicit directory pytest runs automatically after a diff at convergence or before an earlier natural completion.
- The same unchanged generation is not run twice.
- A later edit makes the contract due again.
- Passing and failing outputs are visible to the next model request and trace.
- Commands without explicit workspace-local targets remain model-owned.
- Existing full test suite remains green.
- A real headless edit executes the requested directory suite without the model issuing a Bash tool call.
