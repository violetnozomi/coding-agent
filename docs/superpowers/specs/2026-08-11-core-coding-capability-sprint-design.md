# Core Coding Capability Sprint Design

## Scope

This phase closes three measurable coding-agent gaps only: repository intelligence,
large tool/context handling, and reproducible capability measurement. Terminal UI,
daemon/attach, MCP, memory, skills, and workflow feature expansion are out of scope.

## Source audit

| Capability | NZ-Coder today | InfCodeX reference | OpenCode/Kilo reference | Depth gap | Observable failure |
|---|---|---|---|---|---|
| Symbol index | SQLite and incremental fingerprints; Python AST plus shallow multi-language declarations | Workspace-scoped semantic runtime, worker warm-up, typed capsules and confidence | Tree-sitter parsers over many languages | No common rich symbol metadata or freshness contract | Weak cross-language navigation |
| References/calls | Python identifier references only; no persisted calls | Structural/semantic lookup and bounded rendered contexts | Parsed code blocks plus semantic search | No caller/callee graph | Cross-file impact is guessed from imports |
| Module/process context | JSON import graph with dependencies/dependents | Module capsules and runtime fallback/warming states | Scanner, watcher, cache and state manager | Contexts are disconnected from symbols/calls | Agent rereads files to reconstruct flow |
| Changed scope/impact | Git paths and diff heuristics | Structural workspace context | Incremental invalidation | No symbol-to-caller/test propagation | Verification scope can miss regressions |
| Tool exposure | Fixed schema threshold and rare-prefix deferral | Budgeted runtime surface | N/A | Does not consider current usage/reserve/schema ratio | Tools can be hidden too early or schemas dominate input |
| Tool results | Per-result 4% head/tail projection with durable artifact | Bounded structured rendering | N/A | No batch budget and no tool-specific evidence policy | Parallel results can jointly flood context |
| Capability evidence | General benchmark runners | Runtime tests and trace events | Indexing telemetry | No deterministic A-H core suite or unified trajectory metrics | “Aligned” cannot be demonstrated behaviorally |

Semantic embeddings are deliberately deferred: the structural layer must first fail a
reproducible benchmark in a way semantic retrieval can improve. This avoids adding a
provider/vector-store dependency without evidence.

## Architecture

### Repository Intelligence V3

`PersistentCodeIndex` remains the workspace-owned persistence boundary. Its schema is
extended with normalized symbol provenance and persistent `CallEdge` records. Python
calls are AST-derived with high confidence; supported non-Python declarations remain
structural with explicit lower confidence. Query methods return bounded symbol,
caller, callee, and process capsules. Incremental `update_paths` replaces all rows for
changed files atomically, so freshness follows the file fingerprint.

`RepositoryGraph` remains the module dependency owner and composes index data for
module, changed-scope, and impact contexts. It does not become a second symbol store.

### Tool/Context Scale V2

Exposure planning receives an explicit pressure snapshot: context window, used input,
reserved output, schema tokens, and schema ratio. Deferral occurs only when either
remaining input or schema ratio crosses a configured pressure boundary. Unlock state
continues to be run-owned.

Tool projection gains a batch allocator and named policies: read/grep preserve heads,
shell/tests preserve failure-heavy tails, diffs preserve both ends, and child results
preserve summaries. Every truncated result retains an immutable artifact path. Results
remain contiguous and paired with their call IDs.

### Reproducible capability benchmark

A deterministic local suite covers A-H scenarios without model calls. It emits a
manifest, per-case outcomes, and `AgentTrajectoryMetrics` derived from JSONL traces.
Metrics include success/patch validity, turns, calls, tokens, schema/tool result tokens,
search/read duplication, compactions, verification, child/conflict counts, wall time,
and cost. Scores are reported separately as Feature Coverage, Implementation Depth,
and Behavioral Effectiveness; unavailable behavioral evidence is `unknown`, never
inferred from module presence.

## Compatibility and safety

Existing constructors, registered tool names, and string error contracts remain valid.
All persistence stays below the resolved workspace. No external dependency is added.
Production and evaluation code consume the same services; the benchmark does not gain
private capabilities.
