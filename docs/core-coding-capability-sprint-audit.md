# Core Coding Capability Sprint — Final Source and Behavior Audit

Date: 2026-08-11

## Final comparison matrix

| Capability | NZ-Coder final state | InfCodeX reference strength | OpenCode/Kilo reference strength | Remaining depth gap | Failure evidence/status |
|---|---|---|---|---|---|
| Files/modules | Persistent SQLite file index plus persistent module dependency graph | Typed workspace/module capsules | Incremental scanner/cache/state manager | No material phase-scope gap | A–C benchmark passes, including 300-module graph |
| Symbols/exports/entrypoints | Python AST and conservative multi-language declarations; export and entrypoint projection | Parser-specific semantic analyzers | Tree-sitter language queries | Non-Python parsing remains lexical rather than Tree-sitter | Explicit `0.75` declaration confidence; not presented as semantic |
| References/calls/callers | Python AST references/calls; TypeScript/JS, Go, Rust and other supported languages receive conservative lexical refs/calls; persisted `CallEdge` | Rich structural/semantic lookup | Parsed chunks plus semantic search | Overload/type resolution is not available without LSP/parser semantics | Cross-file and multi-language regression tests pass |
| Symbol Context | Multiple definitions, references, callers, callees, export state, source, confidence, freshness | Typed bounded capsule | Search result metadata | Type/type-flow details remain LSP-owned | Ambiguous-definition regression passes |
| Module Context V2 | Dependencies, dependents, symbols, exports, calls, entrypoints, related tests, provenance | Module capsule/fallback states | Scanner/cache status | No semantic summary generation | Capsule V2 tests pass |
| Process Context | Bounded forward calls, reverse callers, definition nodes, truncation/provenance | Semantic process/workspace lookup | Semantic chunk retrieval | Dynamic dispatch remains unresolved | Forward/reverse process regression passes |
| Changed Scope/Impact V2 | Changed modules/symbols, graph relations, structural callers and related tests feed impact risk | Structural workspace context | Incremental invalidation | Runtime/dynamic dependency inference remains unavailable | Changed-scope and impact regressions pass |
| Prewarm/incremental worker | Product environment owns background prewarm and polling watcher; create/change/delete events are debounced and coalesced | Worker warming/fallback runtime | Watcher, batch drain, state manager | Uses stdlib polling instead of OS-native chokidar | Lifecycle, burst coalescing and CRUD watcher tests pass |
| Tool exposure | Run-owned pressure snapshot uses context window, projected input, output reserve, schema tokens/ratio and unlocks | Budgeted dynamic tool surface | N/A | Provider-side hidden tool discovery is outside this phase | 20/50/100/200 low/high-pressure benchmark passes |
| Tool result scale | Aggregate batch ceiling; read/grep head, bash/test tail, diff head/tail; contiguous pairing and durable full-output reference | Bounded renderer | N/A | No gap demonstrated by phase benchmark | Huge-output aggregate budget test passes |
| Trajectory metrics | Success, patch validity, turns, model/tool/repo calls, tokens, schema/result tokens, duplicates, failed commands, compaction, verification, child/conflict, wall time and cost | Trace events | Indexing telemetry | Paid-provider cost remains zero in offline benchmark | Metrics generated from actual native AgentRunner events |
| Trajectory diagnosis | Repeated empty tool selection, premature compaction, repeated unchanged verification, and backtracking detectors | Runtime trace analysis | Telemetry | Effectiveness on paid-model traces remains unmeasured | Positive detector fixtures pass; clean A–H trace has zero warnings |

## A–H evidence

| Case | Executed behavior | Acceptance evidence |
|---|---|---|
| A | Unknown-file structural localization | `normalize input` locates `pipeline.py` without a supplied path or exact underscored name |
| B | Cross-file caller impact | `service.py:entry → helpers.py:leaf` persisted and retrieved |
| C | Large repository navigation | 300-module cyclic graph indexed and summarized |
| D | Large tool catalogs | 20/50/100/200 rare tools remain visible at low pressure and defer at high pressure |
| E | Huge parallel output | 20 large results stay within a 600-token aggregate budget and retain recovery paths |
| F | Long horizon | Canonical native `AgentRunner` completes 41 model calls and 40 tool results |
| G | Verification recovery | Real `py_compile` fails with exit 1, source is repaired, rerun exits 0 |
| H | Multi-agent conflict | Production `BackgroundAgentManager` rejects a child apply after parent baseline change |

Evidence directory:
`.nz-coder/benchmarks/core-capability-20260811-v2/`

The JSONL trajectory is public/replayable within the workspace. The benchmark is
idempotent when rerun into the same evidence directory.

Final verification: `1741 passed` in the complete pytest suite; Ruff and Python
`compileall` passed. Seven Python 3.13 multiprocessing warnings remain because tests
deliberately exercise `fork` while the process owns threads. Repository intelligence
registers an `after_in_child` reset and has a regression test proving the child creates
a fresh worker instead of inheriting the parent's dead thread/lock registry.

## Prompt completion decisions

- Repository Intelligence V3: implemented and production-wired.
- Tool/Context Scale V2: implemented and production-wired.
- Reproducible A–H benchmark and unified trajectory analysis: implemented.
- Process Context was selected as the one optional follow-up capability. Web search was
  not selected, matching the prompt's “at most one” constraint.
- Semantic embeddings were not activated. The structural benchmark passed all cases,
  so the prompt's evidence gate (“only after structural benchmark proves value”) was
  not met. This is a completed negative decision, not unfinished implementation.
- Memory, MCP, skills, verification architecture, TUI, daemon, and workflow feature
  expansion were intentionally untouched except where existing production APIs were
  exercised by the benchmark.

## Score separation

- Feature Coverage: 100/100 for the phase manifest.
- Implementation Depth: 85/100. Python AST is deep; supported non-Python call/reference
  data is intentionally lower-confidence lexical analysis rather than full typed parsing.
- Behavioral Effectiveness: 100/100 on the deterministic local A–H suite only.
- Paid-model/SWE-bench effectiveness: unknown; it requires a separate authorized run.
