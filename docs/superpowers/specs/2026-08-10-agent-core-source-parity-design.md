# Agent Core Source-Parity Design

## Goal

Translate the InfCode/InfCodeX Agent runtime contracts into NZ-Coder's Python
production path. A feature is source-level aligned only when its producer,
state owner, persistence/recovery boundary, consumer, trace evidence, and
production assembly are all present.

## Architecture

The generic Agent Core owns the provider turn, persisted assistant/tool parts,
tool observation, mutation generations, natural-stop hooks, and terminal tool
signals. InfCodeX-derived stall detection is a per-run observer with a bounded
window and a reset boundary. SWE restrictions remain a separate policy adapter
that consumes Core facts but cannot redefine generic Runner lifecycle.

## Runtime order

1. Persist the assistant step before acting on it.
2. Feed every proposed tool call to the observer pipeline.
3. Execute or reject the call, then persist and observe its result.
4. Apply mutation, diff, and verification facts to one generation ledger.
5. Consume a terminal tool signal only after the complete batch settles.
6. On a natural text-only end, persist output, run bounded stop hooks, then
   either reanimate, abort, or assert terminal state.

## State ownership

- `RuntimeState` owns the current mutation generation and generation-scoped
  diff/verification facts.
- `RecoveryState` owns per-run stall history and resets it at run/compaction
  boundaries.
- `AgentHooks` owns stop-hook counters; `AgentRuntimeAssembly` must explicitly
  register every production consumer.
- SWE reporting derives semantic patch risk from the final generation; older
  failures remain process diagnostics.

## Compatibility and policy boundary

Existing tool schemas and public entry points remain compatible. Strict Bash,
network prohibition, test restrictions, and investigation budgets are NZ-Coder
extensions. They must be named as such in trace and documentation. Read-only
Bash commands that inspect source consume the strict investigation budget;
verification/status commands do not.

## Completion contract

Alignment status is one of `mechanism_only`, `wired`, `contract_verified`, or
`trace_verified`. Only `trace_verified` may be documented as source-level
complete. Unit tests prove upstream contracts, assembly tests prove the default
production path, and a provider-free end-to-end trace proves event ordering.
