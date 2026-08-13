# Core Capability Closure Report

Date: 2026-08-12

## Executive verdict

nzcoder now has an identity-based repository index, identity-safe symbol/reference/call queries, production-available Python/TypeScript/JavaScript/Go analyzers, workspace-scoped incremental runtime, and one structural intent lookup over Symbol, Module, and Process candidates.

This closes the main source-level Repo Intelligence gaps identified for this phase. It does not establish behavioral parity with InfCodeX or OpenCode because those products were not run under the same model and budget. The measured nzcoder results also expose two real runtime gaps outside Repo Intelligence correctness: verification policy false positives on valid public-API migrations and weak multi-agent completion efficiency.

Embedding retrieval should not become a core dependency yet. In the no-shared-keyword gate, both Current/basic retrieval and V3+Lookup localized the full path in 3/3 runs. Lookup reduced effort versus Repo Intelligence OFF, but did not outperform Current/basic retrieval consistently.

## Evidence boundaries

- Model: `openai-compatible/deepseek-v4-flash`
- Reasoning: provider default
- Temperature: `0`
- Maximum turns: `40`
- Repetitions: `3`
- Permission mode: benchmark `auto`, strict local tools
- nzcoder production matrix: 87 runs, with the original 24 Tool Scale runs retained as invalid harness evidence after discovering a catalog-scope bug
- Corrected Tool Scale matrix: 24 runs
- Structural semantic gate: 12 runs
- Reference source snapshots: InfCodeX `d3a81237`; local OpenCode/Kilo source snapshot
- Reference behavioral scores: unavailable. InfCodeX dependencies were not installed; the OpenCode/Kilo snapshot requires Bun and had no installed workspace dependencies. No reference score is inferred.

Evidence:

- `docs/evidence/core-capability-closure-production-v2-2026-08-12/`
- `docs/evidence/core-capability-closure-tool-scale-fixed-2026-08-12/`
- `docs/evidence/core-capability-closure-semantic-gate-2026-08-12/`
- `docs/evidence/core-capability-closure-controlled-2026-08-12/`
- `docs/evidence/core-capability-closure-performance-2026-08-12/`

## Implementation closure

### Identity correctness

- Symbol identity is serialized as repository-relative path, qualified name, and kind.
- `ReferenceEntry` now carries source symbol ID, target symbol ID or unresolved target, qualifier, resolution kind, confidence, and source.
- Calls and ordinary references share the same import/same-module/qualified/unique/re-export resolution primitives.
- `callers`, `callees`, and `references` are exact identity queries. An ambiguous bare name raises `AmbiguousSymbolError`.
- `symbol_context("login")` returns `ambiguous=true`, `definition=null`, isolated alternatives, and no aggregated edges.
- `process_context("login")` returns candidates and does not silently traverse the first definition.
- Python fixtures cover duplicate module functions, duplicate class methods, `self.run()`, `A.run()`, `B.run()`, import aliases, qualified imports, re-exports, and unresolved dynamic calls.

Primary implementation: `nz_coder/intelligence/analyzers.py`, `nz_coder/intelligence/code_index.py`.

### Production analyzers

The declared runtime dependencies are installed and exercised:

| Language | Analyzer | Capability tier | Status |
|---|---|---:|---|
| Python | stdlib AST | `ast-native` | available |
| TypeScript | Tree-sitter | `tree-sitter` | available |
| JavaScript | Tree-sitter | `tree-sitter` | available |
| Go | Tree-sitter | `tree-sitter` | available |
| Rust | lexical fallback | `lexical-fallback` | deliberate remaining gap |

Doctor also reports `watchfiles` and LSP augmentation availability. TS/JS handles named imports, aliases, namespaces, re-exports, CommonJS `require`, and unresolved dynamic members. Go handles package calls, local calls, and receiver calls with reduced confidence when receiver type is unknown.

InfCodeX remains deeper for TypeScript because its compiler analyzer can use the TypeScript type checker. nzcoder's Tree-sitter implementation is parser-based but not a full type system.

### Module, process, and intent entities

- Module boundaries use nested `pyproject.toml`, `package.json` workspaces, `Cargo.toml`, `go.mod`, and directory conventions. `src/auth` and `src/payment` no longer collapse into `module:src`.
- Module context includes identity, kind, files, languages, entry files, ranked top symbols, dependencies/dependents, tests, changed files, and process IDs.
- Process context remains bounded and on demand, then cached by entry/depth/generation. This is different by design from InfCodeX's persisted process catalog.
- Unified lookup returns Symbol, Module, and Process candidates with `kind`, `title`, `locator`, `snippet`, `score`, `identity`, `confidence`, and `source`.
- Process lookup ranks entrypoints and cached capsules; it does not enumerate all graph paths.
- InfCodeX `semantic_lookup` is lexical/structural ranking over the same entity families, not embedding retrieval. nzcoder is aligned with that behavior despite the different tool name.

### Workspace runtime

- One resolved workspace owns one shared service, SQLite index, graph, watcher, and generation cache.
- Main/child users in the same workspace share service identity and generation; a different worktree has a different service.
- `repo_intelligence_workspace_lease` makes ownership explicit without moving the lifecycle back into AgentLoop.
- Cold build scans once; graph consumes the index snapshot.
- File changes call `index.update_paths` and `graph.update_paths`; delete/rename invalidates symbols, calls, references, dependencies, and generation-keyed caches.
- Native `watchfiles` events are debounced/coalesced; adaptive polling remains the fallback.
- Warming queries use a bounded wait and return fallback metadata instead of waiting indefinitely.

### Tool capacity closure

Batch result projection now receives the live next-request capacity calculated from ContextBudget, current projected request tokens, and output reserve. `ToolResultProjector` remains session-independent and receives only a numeric budget.

The Tool Scale benchmark found and fixed a separate correctness issue: `tool_search` searched the global catalog instead of the current RunRequest catalog. The exposure state now binds the run-scoped specs, so discovery cannot unlock an undeclared tool.

## Structural probes and tests

- Core capability contract: 8/8 cases passed. This is contract evidence, not model intelligence.
- Implementation depth probes: 22/22 passed. This is a structural/runtime probe pass rate, not an overall capability score.
- Controlled behavioral matrix: 29/29 passed. This validates integration mechanics only.
- Full test suite: `1783 passed`, 7 existing multiprocessing fork deprecation warnings.
- Ruff and `git diff --check`: passed.

## Runtime performance

Times are milliseconds on generated Python repositories. Incremental totals are shown as index/graph.

| Files | Cold build | Warm access | One-file update | Ten-file burst | Symbol | Process | Changed scope | First lookup | Cached lookup |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 500 | 166.1 | 37.6 | 7.6 / 1.2 | 9.0 / 1.6 | 1.9 | 16.7 | 3.4 | 37.8 | 0.026 |
| 2,000 | 668.1 | 166.3 | 21.8 / 1.5 | 23.7 / 1.8 | 1.9 | 12.8 | 9.2 | 127.8 | 0.016 |
| 5,000 | 1,767.0 | 479.1 | 55.7 / 2.1 | 59.4 / 2.5 | 2.7 | 12.9 | 22.5 | 331.5 | 0.020 |

The first 5,000-file lookup was reduced from about 1.5 seconds during profiling to 331.5 ms by reusing symbol candidates and replacing per-file top-symbol queries with indexed aggregate ranking. Query-cache hits are generation-bound and effectively constant time.

## Real-model Repo Intelligence A/B

Means over three runs per cell. Tokens are input plus output tokens. B's agent patches and scorer tests were correct in every run, but the production runtime stopped all runs at the high-risk public API verification hook; therefore B task success is correctly reported as 0%.

| Case | Mode | Success | Turns | Searches | Reads | Wrong reads | RI calls | Tokens |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| A Localization | OFF | 100% | 7.00 | 1.33 | 5.00 | 1.00 | 0.00 | 11,577 |
| A Localization | Current | 100% | 5.00 | 1.00 | 5.00 | 1.00 | 3.67 | 5,085 |
| A Localization | V3 | 100% | 6.00 | 0.00 | 5.00 | 1.00 | 3.33 | 6,625 |
| A Localization | Lookup | 100% | 5.67 | 0.67 | 5.00 | 1.00 | 2.67 | 7,859 |
| B Cross-file | OFF | 0% | 16.67 | 7.00 | 7.00 | 0.00 | 0.00 | 202,852 |
| B Cross-file | Current | 0% | 16.00 | 4.67 | 7.00 | 0.00 | 1.33 | 131,687 |
| B Cross-file | V3 | 0% | 22.33 | 7.33 | 7.00 | 0.00 | 2.00 | 246,366 |
| B Cross-file | Lookup | 0% | 17.00 | 7.00 | 7.00 | 0.00 | 2.00 | 156,213 |
| C Process | OFF | 100% | 4.67 | 0.33 | 5.00 | 0.00 | 0.00 | 6,596 |
| C Process | Current | 100% | 3.67 | 0.00 | 5.00 | 0.00 | 2.67 | 2,099 |
| C Process | V3 | 100% | 5.67 | 0.00 | 5.00 | 0.00 | 7.67 | 8,993 |
| C Process | Lookup | 100% | 7.33 | 0.00 | 5.00 | 0.00 | 8.33 | 4,903 |
| D Large repo | OFF | 100% | 5.33 | 1.67 | 2.00 | 0.00 | 0.00 | 20,113 |
| D Large repo | Current | 100% | 5.00 | 0.67 | 2.00 | 0.00 | 2.00 | 11,942 |
| D Large repo | V3 | 100% | 5.67 | 0.33 | 2.00 | 0.00 | 6.00 | 11,447 |
| D Large repo | Lookup | 100% | 5.67 | 1.33 | 2.00 | 0.00 | 3.00 | 12,033 |

Verdict: Repo Intelligence reduces search/token cost in A and D, but unified lookup is not universally better than Current/basic retrieval. C shows tool overuse in V3/Lookup. The behavioral gain is real but task-dependent.

## Other real-model cases

| Case | Runs | Task success | Patch/tests | Finding |
|---|---:|---:|---:|---|
| E Long horizon | 3 | 0% | 3/3 correct | all stopped by verification policy after valid changes |
| F Verification recovery | 3 | 100% | 3/3 correct | model observed failure, repaired, and reverified |
| G Multi-agent | 9 | 11.1% | 8/9 correct | mostly verification-policy stops; one background subagent failed to settle; more agents increased reads/tokens |
| H Tool scale, corrected | 24 | 100% | not an edit task | all catalog sizes and exposure modes succeeded |

Multi-agent success by configured agent count was 0/3, 0/3, and 1/3 for 1, 2, and 4 agents. Mean total tokens were approximately 81k, 502k, and 312k. This is a real behavioral/runtime gap; adding agents did not improve reliability or efficiency on this fixture.

## Tool Scale result

Both modes succeeded 3/3 at every size. Schema tokens are accumulated across model turns.

| Tools | All exposed schema tokens | Progressive schema tokens | All turns | Progressive turns |
|---:|---:|---:|---:|---:|
| 20 | 19,555 | 16,352 | 6.67 | 5.33 |
| 50 | 44,136 | 3,636 | 6.00 | 6.00 |
| 100 | 125,719 | 2,828 | 8.67 | 4.67 |
| 200 | 220,846 | 4,040 | 7.67 | 6.67 |

At 50-200 tools, Progressive Exposure reduced aggregate schema tokens by roughly 92-98% while preserving 100% task success.

## Embedding decision gate

Case I intentionally used a business prompt with no direct implementation vocabulary. The expected path was `finalize_cart -> close_cart -> commit_record -> dispatch_receipt` across 100 distractor components.

| Mode | Success | Full localization | Turns | Searches | Reads | Wrong reads | RI calls | Input tokens |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| OFF | 66.7% | 100% | 12.00 | 1.67 | 8.00 | 3.33 | 0.00 | 195,326 |
| Current | 100% | 100% | 5.67 | 1.67 | 5.33 | 1.33 | 2.00 | 34,771 |
| V3 | 66.7% | 66.7% | 12.33 | 1.00 | 7.00 | 3.00 | 8.00 | 124,608 |
| V3 + Lookup | 100% | 100% | 7.67 | 1.00 | 5.33 | 1.33 | 8.33 | 41,531 |

One OFF run localized correctly but was stopped by verification policy. One V3 run failed localization. Current/basic and Lookup both completed 3/3, with Current using fewer turns, RI calls, and tokens.

Decision: do not implement an embedding provider/vector store as a core dependency now. The gate does not show a structural localization success deficit versus the current retrieval stack, and no fair OpenCode same-model result exists. Reopen an optional embedding experiment only after a broader domain-diverse gate shows repeated localization failure or a fair OpenCode run shows a stable advantage.

## Three-way source matrix

| Capability | nzcoder | InfCodeX | OpenCode/Kilo | Verdict |
|---|---|---|---|---|
| Symbol identity | path + qualified name + kind; SQLite identity queries | explicit SymbolId and semantic records | chunk/vector index is primary; no equivalent first-class structural graph in Kilo indexing | Aligned with InfCodeX |
| Reference identity | source/target SymbolId or unresolved target | symbol call targets use identity; semantic index records | retrieval chunks, not identity references | nzcoder strong |
| Call target resolution | Python import/self/class/re-export pipeline; TS/JS/Go parser heuristics; bounded LSP | TypeScript compiler/type-checker plus Lezer/fallback analyzers | parser/chunk extraction; call graph is not Kilo indexing's primary model | Remaining TS type-depth gap vs InfCodeX |
| Module identity/context | manifest + directory boundaries; first-class capsule | first-class persisted module capsules | workspace/file/chunk state rather than module capsules | Near aligned with InfCodeX |
| Process context | on-demand bounded capsule, generation cache | first-class persisted process capsules | no comparable structural process layer in Kilo indexing | Different by design |
| Multi-language parser | Python AST; TS/JS/Go Tree-sitter; Rust fallback | TS compiler plus Lezer/fallback including Rust coverage | broad web-tree-sitter parser/chunker | Breadth/type-depth gap remains |
| Structural intent lookup | unified Symbol/Module/Process lexical-structural ranking | `semantic_lookup` ranks Symbol/Module/Process structurally | not the primary retrieval mode | Aligned with InfCodeX |
| Embedding retrieval | none | semantic lookup inspected here is non-embedding | embedder + vector-store search service | Deliberate gap vs OpenCode; gate says defer |
| Incremental index | SQLite per-file updates + incremental graph snapshot updates | worker/build cache/materialized semantic index | scanner/parser/hash state/vector update pipeline | Core behavior aligned; worker isolation differs |
| Watcher | watchfiles, debounce/coalescing, adaptive fallback | runtime prewarm/cache/worker lifecycle | chokidar native event batches | Aligned |
| Query cache | generation-keyed symbol/module/process/impact/lookup cache | build cache plus session/in-flight TTL caches | hash/cache manager and vector state | Aligned, different invalidation choices |
| Query wait budget | short wait then explicit fallback | bounded worker race and fallback | indexing state/search availability | Aligned with InfCodeX |
| Fallback | lexical analyzer, grep, repo map, LSP, warming/failed metadata | light/fallback analyzers and runtime fallback | parser fallback chunking and non-index tools | Aligned |
| Tool exposure | progressive exposure with run-scoped discovery catalog | progressive runtime/tool routing exists | broad tool surface, product-specific routing | nzcoder validated to 200 tools |
| Tool result capacity | adaptive batch water-fill constrained by live request capacity | budgeted repo output/runtime | no directly comparable Kilo indexing contract | nzcoder implementation-specific strength |
| Behavioral benchmark | controlled CI plus repeated production AgentRunner tasks/full traces | source tests/perf benchmarks; same-model behavior unavailable | indexing tests; same-model behavior unavailable | Reference behavioral parity has insufficient evidence |

## Real gaps

1. Verification/risk policy treats required public API replacement as an unreviewed deletion and stops otherwise correct B/E/G runs. This is now the largest measured completion gap.
2. Multi-agent execution is expensive and unreliable on the measured fixture; one run also left a background subagent unsettled at Session deletion.
3. TypeScript call resolution is parser/import based, not compiler/type-checker based. Rust remains lexical fallback.
4. First uncached structural lookup is 331.5 ms at 5,000 one-symbol files. It is bounded and cached, but further index-side module ranking is still possible.
5. Unified lookup does not consistently beat the existing basic retrieval path. Ranking/tool-use guidance needs evidence-driven tuning before adding more retrieval machinery.
6. OpenCode-specific embedding retrieval, Web Search, and persistent PTY remain absent or deferred. Only embedding was evaluated in this phase, and the decision is to defer it.

## False gaps or different-by-design choices

- InfCodeX `semantic_lookup` does not imply vector embeddings; nzcoder now has the same class of structural lookup.
- A fully persisted process catalog is not required by the current benchmark. nzcoder's bounded cached process capsule is different by design.
- Different filenames and TypeScript/Python architecture are not capability gaps.
- 22/22 structural probes and 8/8 contracts are not behavioral parity scores.
- Controlled 29/29 is not real-model intelligence evidence.

## Distance to core alignment

### nzcoder vs InfCodeX

- Feature Coverage: the core coding/runtime capability surface is substantially aligned. Structural intent lookup, identity graph, module/process context, incremental runtime, cache, fallback, and behavioral evaluation all exist.
- Implementation Depth: near alignment in identity/runtime mechanics; InfCodeX remains deeper in TypeScript type-aware resolution and persisted semantic process materialization.
- Behavioral Effectiveness: nzcoder is measured above; InfCodeX same-model behavior is unavailable. Behavioral parity with reference: insufficient evidence.

### nzcoder vs OpenCode/infcode-dev

- Feature Coverage: core coding Agent coverage is broadly comparable, but OpenCode/Kilo still has embedding/vector retrieval and a more mature persistent PTY product surface.
- Implementation Depth: nzcoder is deeper in explicit structural identity/process/impact data; OpenCode/Kilo is deeper in broad parser/chunk/embed/vector indexing and terminal product integration.
- Behavioral Effectiveness: nzcoder is measured above; OpenCode same-model behavior is unavailable. Behavioral parity with reference: insufficient evidence.

## Final status

This phase reaches source-level core capability alignment, with explicit remaining specialist gaps. It does not justify claiming overall behavioral parity. The next work should target the measured verification-policy and multi-agent completion failures before adding embedding, Web Search, persistent PTY, or more Repo Intelligence entities.
