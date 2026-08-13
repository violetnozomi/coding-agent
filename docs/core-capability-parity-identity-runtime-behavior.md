# Core Capability Parity: Identity, Runtime, Behavior

Date: 2026-08-11

## Outcome

This phase is complete for the requested P0 scope. nzcoder now uses persistent
symbol identities for repository relationships, owns repository intelligence at
workspace scope, performs bounded incremental index and graph updates, and has a
separate Agent-executed behavioral benchmark. Existing AgentRunner, session,
tool, context, memory, skill, MCP, and product-layer architectures were not
reimplemented.

Embedding retrieval was not added. The measured structural V3 trajectory
already removed the observed localization ambiguity, while a live-model endpoint
was unavailable. That does not satisfy the evidence gate for making embedding a
core dependency.

## Delivered P0

- `SymbolId` is stable across line shifts and combines workspace-relative path,
  qualified name, and kind. `SymbolEntry` persists identity, module, language,
  signature, export state, confidence, source, and capability tier.
- Calls persist caller identity, resolved callee identity or an unresolved
  target, call site, qualifier, candidates, resolution kind, confidence, and
  source. Python resolution covers same-module calls, import aliases, qualified
  imports, `self.method`, and class-qualified calls. Bounded LSP definition
  augmentation can upgrade unresolved calls explicitly.
- `LanguageAnalyzer` separates Python AST, TS/JS/Go tree-sitter, and lexical
  fallback paths. Unsupported parser runtimes degrade with an explicit tier and
  confidence instead of pretending to be AST results.
- `ModuleCapsule`, `ProcessCapsule`, and `ProcessStep` are first-class entities.
  Package-area module identity aggregates files; top symbols use exports,
  entrypoints, references, callers, and visibility. Symbol/process contexts now
  include related tests, and symbol context reports worktree change state.
- Changed scope follows changed files through symbol identities, direct and
  transitive callers, dependent modules, related tests, public API exposure, and
  risk. Depth, node, time, and confidence budgets bound traversal.
- The workspace registry shares one `RepoIntelligenceService` across main,
  child, SDK, headless, interactive, and evaluation runs using the common
  AgentRunner composition. Prewarm is non-blocking; queries wait briefly and
  return explicit fallback guidance if warming, stale, failed, or unsupported.
- Cold build scans once. The graph consumes `IndexSnapshot`; path updates load a
  partial snapshot, resolve only affected call names/paths/old identities, and
  update only affected graph relationships. Deletes and renames remove symbols,
  calls, imports, and dependencies. An append-only graph journal avoids rewriting
  a monorepo-sized JSON graph on every edit and compacts at a bounded threshold.
- `watchfiles` provides event-driven, debounced, coalesced refresh. Adaptive
  polling remains the dependency/runtime fallback. Generation-keyed query caches
  invalidate on index changes, and runtime metrics are emitted into attached
  Agent traces.
- The old A-H integration checks remain as `CoreCapabilityContractSuite`.
  `AgentBehaviorBenchmark` separately runs A-H through AgentRunner using either a
  controllable model or the production model/provider path. The scorer does not
  repair patches. It records model/tool/token/search/read/repo-intel/compaction/
  verification/subagent/conflict/time/cost evidence and trajectory diagnostics.
- Batch tool output uses adaptive small-first water filling rather than equal
  division. Tool-scale evaluation uses 20/50/100/200 tools with all-exposed and
  progressive-exposure variants.

## Three-Way Source Comparison

| Ability | nzcoder | InfCodeX | OpenCode / Kilo | Verdict |
|---|---|---|---|---|
| Symbol identity | Path + qualified name + kind; SQLite key; stable across line shifts | File + qualified name + declaration line | File/chunk hashes and segment IDs, not a symbol graph | nzcoder identity is first-class and more line-shift-stable than the inspected InfCodeX ID |
| Call resolution | Python AST pipeline, TS/JS/Go syntax analyzers, unresolved candidates, bounded LSP augmentation | TS compiler checker plus Lezer/fallback tiers | Parser chunks; no persistent call graph | nzcoder is high precision for Python; InfCodeX remains deeper for type-aware TypeScript |
| Module graph | Package-area `ModuleCapsule`, kind, files, entrypoints, ranked symbols, deps, tests, processes | Rich module capsules including docs/sample files | No structural module capsule in kilo-indexing | Core parity with InfCodeX; InfCodeX carries more presentation metadata |
| Process model | On-demand bounded identity traversal, typed transitions, tests, confidence, generation cache | Materialized process capsules | No structural process model | Comparable capability with different materialization strategy |
| Changed scope | File -> symbol IDs -> callers -> modules -> tests -> public API -> risk | Structured changed-scope report | Search evidence only | Requested V3 path implemented |
| Impact | Depth/node/time/confidence budgets and reverse graph | Bounded capsule impact estimate | No call/module impact graph | Structural parity with InfCodeX |
| Test relation | Calls, references, imports, module relation, and conventions | Key tests in module/process capsules | Retrieval may find tests but no explicit relation | Explicit in nzcoder and InfCodeX |
| Incremental indexing | SQLite path replacement, affected-only call resolution, partial snapshots, incremental graph journal | Build cache/materialization through semantic worker | Incremental scanner/cache/watcher into vector state | All are incremental; nzcoder no longer performs full graph rebuild per edit |
| Worker | One serialized worker thread and queue per workspace | Dedicated worker/client boundary | Orchestrator with concurrent processors | nzcoder is lighter; references provide stronger process isolation |
| Prewarm | Shared non-blocking workspace prewarm | Startup/runtime prewarm | Orchestrator startup indexing | Parity |
| Cache | Persistent SQLite/graph plus exact generation-keyed query/process cache | Build cache and in-flight/TTL runtime caches | File hash cache and vector index state | nzcoder has deterministic invalidation for structural queries |
| Query budget | Default 50 ms wait, then structured fallback to grep/read/repo-map/LSP | Configurable short worker wait and lightweight fallback | Search rejects unavailable index states | nzcoder and InfCodeX preserve Agent operation during warming/failure |
| Semantic retrieval | Lexical ranking over symbol/module/process structure | `semantic_lookup` scores lexical symbol/module/process fields | Embedding query over parser chunks | InfCodeX semantic lookup is not an embedding system |
| Embedding retrieval | Not merged; evidence gate not met | Not the inspected core semantic lookup path | Embedder and LanceDB/Qdrant abstractions | Kilo leads this optional retrieval layer |
| Tool exposure | Tool search and pressure-based progressive exposure | Progressive/deferred tools | Tool registry/runtime | Existing nzcoder capability retained |
| Tool result capacity | Adaptive batch water filling and durable full-output artifacts | Aggregate result budgeting | Runtime-specific output handling | Requested P1 allocation improvement implemented |
| Behavior evaluation | Contract suite plus Agent-owned A-H, trace analysis, repo OFF/current/V3, 1/2/4 agents, tool matrix | Reference implementation was not run fairly | Reference implementation was not run fairly | nzcoder controlled evidence measured; real-model cross-project scores unavailable |

The comparison was re-read from the current source under
`references/InfCodeX/packages/coding/src/repo-intelligence/` and
`infcode-dev/infcode-dev/packages/kilo-indexing/src/`, including semantic types,
analyzers, worker/runtime/cache, scanner, parser, watcher, state, embedders,
vector stores, and search service.

## Verification Evidence

Repository tests: 1,762 passed. Two parser-specific tests are skipped in the
checkout interpreter because its environment has not installed the newly
declared tree-sitter wheels. With the declared parser dependencies installed in
an isolated target, the TypeScript duplicate-name/import-alias test and Go
qualified-package-call test both pass.

Controlled AgentRunner evidence is deliberately separated from real-model
effectiveness:

- A-H suite: 8/8 successful Agent-owned tasks.
- Complete controlled matrix: 25/25 successful runs.
- Long horizon: 16 model/tool turns, one compaction, an observed verification
  failure, repair, and successful re-verification.
- Verification recovery: failure and recovery are both present in the trace.
- Multi-agent: 2-agent and 4-agent variants produced one and three child
  sessions respectively, with zero recorded integration conflicts.
- Tool scale: 20/50/100/200 all-exposed versus progressive variants are stored
  with schema-token evidence.

Controlled repository-intelligence A/B over A-D localization, cross-file impact,
process understanding, and large-repo navigation:

| Mode | Success | Search calls | File reads | Wrong reads | Repo-intel calls | Turns |
|---|---:|---:|---:|---:|---:|---:|
| OFF | 4/4 | 4 | 19 | 1 | 0 | 15 |
| Current | 4/4 | 0 | 18 | 1 | 5 | 15 |
| Identity V3 | 4/4 | 0 | 8 | 0 | 5 | 15 |

This is controlled trajectory evidence, not a claim about a stochastic coding
model and not a historical nzcoder binary comparison.

Performance after bounded relationship resolution and incremental graph journal:

| Files | Cold build | Warm startup | One-file index | One-file graph | 10-file index | 10-file graph | Symbol query | Process query | Changed scope |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 500 | 71.0 ms | 17.3 ms | 3.0 ms | 1.3 ms | 3.4 ms | 0.9 ms | 1.0 ms | 8.3 ms | 1.8 ms |
| 2,000 | 275.6 ms | 75.7 ms | 5.9 ms | 1.3 ms | 6.6 ms | 1.3 ms | 1.1 ms | 7.0 ms | 4.3 ms |
| 5,000 | 721.3 ms | 218.4 ms | 14.3 ms | 1.7 ms | 14.1 ms | 1.7 ms | 1.4 ms | 6.4 ms | 8.9 ms |

For 5,000 files the single edit indexed one file, resolved two affected calls,
and refreshed two graph relationships. The ten-file burst indexed ten files and
refreshed eleven relationships; neither update reparsed the repository.

## Three Separate Assessments

Feature Coverage: the three requested P0 areas are present: identity-based repo
intelligence, production workspace runtime, and Agent-executed behavioral
evaluation. The optional embedding experiment remains gated rather than counted
as missing P0 coverage.

Implementation Depth: the implementation is persistent and queryable, not a
contract-only facade. Evidence covers the schema, analyzers, resolution pipeline,
unresolved targets, partial snapshots, delete/rename, incremental graph journal,
watcher/coalescing, generation caches, wait/fallback, LSP augmentation, trace
metrics, and scale timings. The remaining depth distinction is language
specific: Python is AST-native; TS/JS/Go are syntax-aware but do not match a full
language type checker. The checkout probe reports 16/17 because parser wheels are
not installed in that interpreter; the isolated parser tests demonstrate the
declared runtime path.

Behavioral Effectiveness: controlled AgentRunner A-H and OFF/current/V3 results
are measured above. A real nzcoder coding-model score is unavailable because the
configured provider returned HTTP 403 (`Request not allowed`). InfCodeX and
OpenCode behavioral scores are also unavailable. No score is guessed, and the
contract suite is not presented as a behavioral score.

## Evidence Files

- `docs/evidence/behavioral-v2-controlled-2026-08-11/controlled-agent-behavior-a-h.json`
- `docs/evidence/behavioral-v2-controlled-matrix-2026-08-11/controlled-agent-behavior-matrix.json`
- `docs/evidence/repo-intelligence-behavior-ab-2026-08-11/repo-intelligence-behavior-ab.json`
- `docs/evidence/repo-intelligence-performance-v3-2026-08-11/repo-intelligence-performance.json`
- `docs/evidence/behavioral-live-attempt-2026-08-11.json`

Generated fixture workspaces were removed from `docs/evidence`; reports are
reproducible through `scripts/benchmark_agent_behavior.py` and
`scripts/benchmark_repo_intelligence_v3.py`.
