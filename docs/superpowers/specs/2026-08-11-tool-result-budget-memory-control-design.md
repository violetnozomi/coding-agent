# Tool Result Budget and Memory Control Plane Design

## Scope

This capability batch implements two remaining Phase 5 gaps without extending
`AgentLoop` or replacing the native Runtime architecture:

1. A unified, token-aware projection policy for every settled tool result.
2. A durable memory proposal/review/apply ledger used by automatic extraction.

The public SDK remains a documented P0 gap. Its default path cannot become
honestly native until the host-shaped production services have run-owned
implementations; replacing it in this batch would be a global Runtime rewrite.

## Tool Result Projection

`ToolResultBudget` derives a per-result model-visible allowance from the model
context window, with explicit minimum and maximum bounds. `ToolResultProjector`
accepts the original result once and returns a `ProjectedToolResult` containing
bounded head/tail evidence, original/projected token estimates, truncation
metadata, and a durable workspace-relative artifact path when projection was
required. Failure tails are preserved because tracebacks and compiler/test
summaries commonly occur at the end of command output.

The existing production result pipeline remains the only point that appends
tool messages. It invokes this policy after dispatch and before tracing,
callbacks, stall detection, SessionProcessor settlement, and provider history.
Tool-local limits remain safety/UX limits, but no longer define the final model
context policy.

## Memory Proposal Control Plane

Automatic extraction no longer writes directly to `MemoryManager.save()`.
Each candidate becomes a deterministic `MemoryProposal` with source session,
source message IDs, normalized candidate fields, confidence, reason,
fingerprint, risk, timestamps, and review status.

Low-risk, repo-scoped project preferences may be auto-approved and applied.
User-wide preferences, references, feedback rules, security/tool behavior, and
cross-project policy enter a review inbox. Duplicate fingerprints are recorded
but not applied twice. Every transition is appended to an immutable JSONL
ledger; pending proposals are stored as individual JSON documents so approval
and rejection are atomic and auditable.

The control plane calls the existing `MemoryManager` only after policy
approval, preserving current Markdown/SQLite storage, retrieval, deduplication,
and dream behavior.

## Error and Concurrency Model

Both services use atomic replace for mutable JSON state. The memory controller
uses a process-local lock around proposal transitions and ledger appends.
Invalid proposals fail closed into review/rejection rather than being saved.
Tool projection falls back to bounded in-memory head/tail output if artifact
persistence fails, and records the persistence error in metadata.

## Verification

Contract tests cover bounded projection, head/tail recall, durable artifacts,
small-output identity, per-context budget scaling, proposal deduplication,
safe auto-apply, high-risk review, approval, rejection, provenance, poisoned
candidate handling, and concurrent duplicate submission. Architecture tests
continue to prohibit capability modules importing `AgentLoop`.
