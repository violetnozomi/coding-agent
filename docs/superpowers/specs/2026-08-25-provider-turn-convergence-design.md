# Provider Turn Convergence Design

**Status:** Approved in chat on 2026-08-25
**Scope:** Coding Agent Runtime provider-call observability and evidence-safe early completion

## Problem

The A279 real terminal replay completed correctly, but used 15 coding Provider calls and
one sidecar call. Existing telemetry labels every main call only as `purpose=coding`, so a
trace cannot answer whether a call was investigating, implementing, repairing, verifying,
or merely closing the run. That makes aggressive call-count reduction unsafe.

The replay also exposed a deterministic runtime constraint: a tool boundary before the
nominal 15-call SLA always continues, even when the current mutation generation already has
exact acceptance evidence and the only remaining requirement is the Runtime-owned semantic
review. This can force another Provider call whose only useful output is a final summary.

## Reference Findings

- InfCodeX treats iteration start/end and live-turn attribution as first-class events rather
  than reconstructing turns from tool logs after the run.
- infcode-dev/OpenCode checks persisted terminal facts before dispatching the next model step,
  and exits when the last assistant step is terminal and owns the current user turn.
- Neither reference just lowers a global iteration limit. Termination follows settled facts.

## Design

### 1. Provider turn ledger

Add a small framework-free runtime module that takes a deterministic snapshot before each
main Provider call and classifies its reason from run state:

- `initial_investigation`
- `investigation`
- `implementation`
- `verification`
- `failure_repair`
- `requirement_repair`
- `completion`
- `convergence`

After the response settles, classify the outcome from structured tool calls and state deltas:

- `investigation_batch`
- `mutation_batch`
- `verification_batch`
- `mixed_tool_batch`
- `final_answer`
- `provider_error`

Persist bounded records in `RuntimeState`, aggregate counts by reason and outcome, and emit
`provider_turn_started` / `provider_turn_settled` trace events. Tool execution remains the
authority for individual success/failure; the turn ledger links that evidence by one-based
turn number and mutation generation.

### 2. Evidence-safe tool-boundary completion

Allow an early tool boundary to enter completion settlement only when all of these are true:

1. the workspace has a diff;
2. the declared exact verification contract passed for the current mutation generation;
3. the requirement ledger has no actionable unresolved requirement;
4. any remaining item is Runtime-owned semantic review only.

The existing independent completion review still runs. Acceptance produces the existing
deterministic terminal summary without another main Provider call. Rejection continues the
loop and preserves the bounded repair reserve. Runs without an exact contract or ledger keep
their current behavior.

### 3. Non-goals

- Do not reduce `MAX_AGENT_TURNS` or the 15-call nominal SLA.
- Do not skip exact tests, semantic review, stop hooks, or output guardrails.
- Do not infer success from assistant prose.
- Do not add an Agent framework or external dependency.

## Acceptance

- Unit tests prove stable reason/outcome classification and bounded persistence.
- Native Runner tests prove current-generation exact acceptance can close at a tool boundary
  before call 15, while stale/failed/missing acceptance cannot.
- Existing runtime, context, permission, terminal, and full pytest suites remain green.
- Real terminal traces expose per-call reason/outcome and show no completion-quality regression.
