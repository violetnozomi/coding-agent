# Phase 5 — Coding Agent Capability Parity Audit

Fresh source scan date: 2026-08-11. This report does not inherit the Phase 4 matrix as evidence. It follows current call chains in `references/InfCodeX/packages/coding/src`, `infcode-dev/infcode-dev/packages/opencode/src`, `packages/kilo-indexing/src`, and current NZ-Coder production modules.

## Current Capability Verdict

NZ-Coder is now a mature, Python-native coding Agent for local coding, SWE-style repair, durable Sessions, tool execution, context recovery, LSP, MCP, verification, and multi-Agent/worktree orchestration. It is in the same functional tier as the references for core execution and most local coding workflows, but not yet for very large repository intelligence, provider/ecosystem breadth, or long-lived platform services.

The default public SDK now enters a host-neutral `NativeSDKRunner`; only an explicit `agent_factory=` selects the legacy compatibility path. The largest remaining large-repository gaps are genuine semantic search, worker/prewarm indexing, and public-API/test impact. Tool scale, unified tool-result projection, Skill enforcement, governed memory intake, MCP catalog discovery, and the Native SDK default were implemented in this phase; module/dependency Repo Intelligence is substantially improved but remains below InfCodeX/Kilo semantic depth.

## Three-way Capability Matrix

Depth labels are restricted to `Aligned`, `Mostly aligned`, `Partial`, `Missing`, and `Different by design`.

| # | Capability | InfCodeX | infcode-dev/OpenCode | NZ-Coder depth and evidence | Gap | Priority |
|---:|---|---|---|---|---|---|
| 1 | Agent definition | Aligned | Aligned | Aligned — immutable definition/handoff/admission | None | — |
| 2 | Native runner | Aligned | Aligned | Aligned — `AgentRunner._execute_request` owns turn loop | None | — |
| 3 | Public SDK default | Aligned | Aligned | Mostly aligned — zero-argument client enters NativeSDKRunner; explicit agent_factory is the only legacy path | Native SDK has fewer product-host integrations than CLI Main | P1 |
| 4 | Durable Session | Aligned | Aligned | Aligned — SessionRuntime/store/transcript/usage | None | — |
| 5 | Message/part model | Aligned | Aligned | Mostly aligned — IDs, parts, processor, lineage | Rich typed part breadth | P2 |
| 6 | Streaming | Aligned | Aligned | Aligned — token/reasoning/tool stream settlement | None | — |
| 7 | Cancellation | Aligned | Aligned | Aligned — model/tool/child/process | None | — |
| 8 | Provider breadth | Mostly aligned | Aligned | Partial — capable gateway, fewer provider-specific adapters | Model/provider breadth | P1 |
| 9 | Retry/recovery | Aligned | Aligned | Mostly aligned — backoff, diagnostics, overflow, incomplete calls | Some provider-specific recovery | P1 |
| 10 | Usage/cost | Aligned | Aligned | Mostly aligned — run/session tokens and pricing | Cross-provider confidence | P2 |
| 11 | Runtime events | Aligned | Aligned | Mostly aligned — RuntimeEvent→ordered durable SessionEventBus | Coverage/export | P2 |
| 12 | Middleware | Aligned | Aligned | Mostly aligned — run/model/tool pipeline; observers remain | Host-shaped coding observer | P1 |
| 13 | Context budget | Aligned | Aligned | Mostly aligned — model window/output reserve/pressure | Tool schema now included via exposure | — |
| 14 | Compaction | Aligned | Aligned | Aligned — compaction, retries, degradation, recovery | None | — |
| 15 | Micro-compaction | Aligned | Aligned | Mostly aligned | Policy breadth | P2 |
| 16 | Large user input | Aligned | Aligned | Aligned — oversized payload persistence/projection | None | — |
| 17 | Attachment retention | Aligned | Aligned | Aligned — post-compaction references | None | — |
| 18 | Tool registry | Aligned | Aligned | Aligned — static/dynamic/optional/context-local | None | — |
| 19 | Tool catalog | Aligned | Mostly aligned | Aligned — immutable adapter catalog (Phase 5) | None | — |
| 20 | Tool search | Aligned | Mostly aligned | Mostly aligned — exact/required/ranked search and full schema | No semantic ranking | P2 |
| 21 | Progressive exposure | Aligned | Partial | Mostly aligned — budget gate, resident/deferred, per-run unlock | Real-model A/B pending | P1 |
| 22 | MCP tool scale | Aligned | Mostly aligned | Mostly aligned — dynamic catalog participates in exposure | Prompt/resource catalog UX | P1 |
| 23 | Tool schema budget | Aligned | Mostly aligned | Aligned — estimate before/after and 6k gate | Provider tokenizer precision | P2 |
| 24 | Parallel tools | Aligned | Aligned | Aligned — explicit read concurrency, serial writes | None | — |
| 25 | Tool cancellation | Aligned | Aligned | Aligned | None | — |
| 26 | Tool result projection | Aligned | Mostly aligned | Mostly aligned — unified token-aware model projection, head/tail evidence, durable artifact and metrics; tool-local safety limits remain | Real-model task A/B | P1 |
| 27 | Permission modes | Aligned | Aligned | Mostly aligned — ask/allow/deny/plan/auto/accept edits | Pattern grammar | P1 |
| 28 | Permission wildcard rules | Mostly aligned | Aligned | Partial — tool and bash-prefix rules | Fine path/domain patterns | P1 |
| 29 | Read/write/edit/patch | Aligned | Aligned | Aligned — workspace safety + transaction | None | — |
| 30 | Bash | Aligned | Aligned | Mostly aligned — policy/timeout/cancel; one-shot | Persistent PTY absent | P1 |
| 31 | Search/grep/glob | Aligned | Aligned | Aligned — ranked lexical/structural | None | — |
| 32 | LSP navigation | Aligned | Aligned | Different by design — one operation tool covers definition/reference/hover/symbol/implementation/calls/diagnostics | No behavioral gap | — |
| 33 | Persistent file index | Aligned | Aligned | Aligned — SQLite code index + JSON module graph | None | — |
| 34 | Incremental index | Aligned | Aligned | Mostly aligned — fingerprints/write refresh/cache; full inventory scan | Worker/watch invalidation | P1 |
| 35 | Symbol index | Aligned | Aligned | Mostly aligned — Python deep, multi-language extraction | Cross-language semantic resolution | P1 |
| 36 | Reference index | Aligned | Aligned | Partial — Python index + LSP other languages | Persistent cross-language refs | P1 |
| 37 | Module/import graph | Aligned | Mostly aligned | Mostly aligned — Python/TS/JS/Go/Rust persistent graph (Phase 5) | Alias/package resolution depth | P1 |
| 38 | Cyclic dependencies | Aligned | Partial | Mostly aligned — SCC query (Phase 5) | Package/area cycles | P2 |
| 39 | Changed scope | Aligned | Mostly aligned | Mostly aligned — git/change tracker + related modules | Public API/test impact | P1 |
| 40 | Module context | Aligned | Mostly aligned | Mostly aligned — deps/dependents/language | Symbol/process enrichment | P1 |
| 41 | Symbol context | Aligned | Mostly aligned | Partial — read_symbol/references/callers | Unified cross-language context | P1 |
| 42 | Call graph | Aligned | Mostly aligned | Partial — Python AST + LSP calls | Persistent multi-language graph | P1 |
| 43 | Related tests | Aligned | Partial | Partial — impact/verification heuristics | Indexed test relations | P1 |
| 44 | True semantic search | Aligned | Aligned | Missing — lexical/AST/LSP only; no false claim | Concept discovery in large repos | P1 |
| 45 | Repo worker/prewarm | Aligned | Aligned | Missing — synchronous bounded build/cache | Turn latency at monorepo scale | P1 |
| 46 | Planning/replanning | Aligned | Aligned | Aligned | None | — |
| 47 | Reflection/verification | Aligned | Mostly aligned | Aligned — gates, sidecar, evidence, recovery | None | — |
| 48 | Stall detection | Aligned | Partial | Aligned — detector/orchestrator/sidecar | None | — |
| 49 | Guardrails | Aligned | Aligned | Mostly aligned — input/output/tool | Ecosystem policy breadth | P2 |
| 50 | Child Session | Aligned | Aligned | Aligned | None | — |
| 51 | Background agents | Aligned | Aligned | Different by design — manager operations cover output/stop/message/wait | No behavioral gap | — |
| 52 | Parallel child/process | Aligned | Aligned | Aligned — thread/process caps/isolation | None | — |
| 53 | Agent messaging | Aligned | Aligned | Aligned — peer/parent mailboxes and cycle guard | None | — |
| 54 | Handoff/steering/resume | Aligned | Aligned | Mostly aligned — continuation/as-tool/followup/durable child | Advanced UI steering | P3 |
| 55 | Worktree isolation | Aligned | Aligned | Aligned — scope/conflict/review/apply/rollback | None | — |
| 56 | Workflow | Aligned | Aligned | Different by design — different manifest/DSL, same Runner child execution | No behavioral gap | — |
| 57 | Skill discovery/precedence | Aligned | Aligned | Mostly aligned — project>user>bundled, paths, lazy | Plugin/migration breadth | P2 |
| 58 | Skill model/provenance | Aligned | Mostly aligned | Mostly aligned — parsed/preserved/run metadata (Phase 5) | Composition does not auto-switch model | P1 |
| 59 | Skill allowed-tools | Aligned | Mostly aligned | Aligned — runtime intersection guard (Phase 5) | None | — |
| 60 | Skill resources/reload | Aligned | Mostly aligned | Mostly aligned — bounded base/sample/reload | Watch invalidation | P2 |
| 61 | Skill script execution | Aligned | Partial | Missing — deliberately no bypass around ToolRuntime | Governed script service | P2 |
| 62 | Memory retrieval/extraction | Aligned | Mostly aligned | Mostly aligned — persistent, rule/LLM, ranking/rerank/dream | None major | — |
| 63 | Memory governance | Aligned | Partial | Mostly aligned — automatic extraction now uses proposal/risk/review/dedupe/apply ledger; explicit save tool remains permission-governed | Review UX and cross-process locking | P1 |
| 64 | MCP transport/OAuth | Mostly aligned | Aligned | Aligned — stdio/HTTP/SSE/OAuth/trust/notifications | None | — |
| 65 | MCP prompt/resource discovery | Aligned | Mostly aligned | Mostly aligned — run-scoped `mcp_catalog` searches tools/prompts/resources/status and fetches exact prompts/resources | Live-server UX/soak evidence | P2 |
| 66 | Plugin system | Partial | Aligned | Different by design — MCP+Skills+hooks+optional packs+Workflow | Unified lifecycle only if demanded | P2 |
| 67 | Web fetch | Aligned | Aligned | Aligned | None | — |
| 68 | Web search | Aligned | Aligned | Missing — no URL discovery tool | External discovery | P1 |
| 69 | Persistent PTY | Partial | Aligned | Missing — one-shot Popen only | Servers/watch/debuggers | P1 |
| 70 | HTTP/SSE runtime | Mostly aligned | Aligned | Mostly aligned — Session API/event resume | Ecosystem maturity | P2 |
| 71 | CLI/TUI | Partial | Aligned | Partial — functional, lower product polish | UX | P3 |
| 72 | Tracing basics | Aligned | Aligned | Mostly aligned — IDs/timing/tool/child/token | None major | — |
| 73 | Metrics aggregation | Mostly aligned | Aligned | Partial — facts derivable, percentile/query layer limited | Run fleet analysis | P2 |
| 74 | Snapshot/revert | Aligned | Aligned | Aligned | None | — |
| 75 | Evaluation/SWE | Mostly aligned | Mostly aligned | Aligned — runner, trace, official prediction flow | Current score evidence separate | — |
| 76 | Self-construction | Aligned | Partial | Missing | Low relevance to coding success | P3 |

## Top 10 Real Gaps

### 1. Native SDK product-integration depth — P1

- Reference: both references enter their native service/session runtime from public SDK surfaces.
- NZ-Coder: `AgentClient()` and `run_agent()` now enter `NativeSDKRunner → AgentRunner`; `agent_factory=` is an explicit compatibility escape hatch. The remaining gap is CLI-specific planning/snapshot/background integration, not a hidden default dependency.
- Failure mode: embedders unknowingly inherit CLI host state and cannot supply only core services.
- Why it matters: undermines SDK isolation and independent testing.
- Proposed architecture: native composition builder for ModelGateway, ToolRuntime, SessionStore, and coding execution services; remove the default legacy factory only after parity tests.
- Benchmark: SDK cold construction, concurrent workspace isolation, run/child/resume success, zero AgentLoop imports.

### 2. Unified Tool Result Budget — implemented in Phase 5 continuation

- Reference: InfCodeX `tool-result-policy/budget/truncation-guardrail`; OpenCode normalized result parts.
- NZ-Coder: every settled result now passes a context-derived token budget before tracing, callbacks, stall detection, Session settlement and provider history. Oversized results preserve head/tail evidence and a durable full-result reference. Tool-local safety/pagination limits remain intentionally separate.
- Failure mode: oversized or over-truncated evidence damages later reasoning and compaction.
- Architecture: `ToolResultBudget → ToolResultProjector → ProjectedToolResult`, connected at the single production projection boundary.
- Benchmark: 35,013 proxy tokens became 1,483 (95.76% reduction), with 100% head/tail sentinel recall and a durable reference. This is a provider-free proxy, not a task-success claim.

### 3. True semantic repository search — P1

- Reference: InfCodeX semantic worker/index; Kilo Tree-sitter+embedding+LanceDB/Qdrant.
- NZ-Coder: explicitly lexical/structural/symbol only.
- Failure mode: conceptual tasks miss files whose names/text do not match query terms.
- Architecture: pluggable semantic backend behind Repo Intelligence; lexical fallback remains canonical.
- Benchmark: natural-language relevant-file recall and Agent tool-call reduction.

### 4. Repo worker/prewarm/invalidation — P1

- Reference: semantic workers, build caches, watchers and indexing orchestrators.
- NZ-Coder: persistent caches, but inventory and graph resolution are synchronous.
- Failure mode: large monorepos pay hundreds of milliseconds or stale context at turn boundaries.
- Architecture: workspace worker with coalesced changes, generation IDs, bounded query snapshots.
- Benchmark: cold/warm/incremental p50/p95 and staleness window.

### 5. Public API/test impact intelligence — P1

- Reference: InfCodeX impact estimate/process context/relationship scan.
- NZ-Coder: module deps, changed scope, Python refs, verification heuristics; no durable API/test relation model.
- Failure mode: incomplete cross-file fixes and regression selection.
- Architecture: enrich RepositoryGraph with exported symbols and test edges.
- Benchmark: impacted-file/test precision and recall on known commits.

### 6. MCP prompt/resource Agent discovery — implemented in Phase 5 continuation

- Reference: InfCodeX progressive MCP search/describe/get/read patterns.
- NZ-Coder: transports remain unchanged; the run-scoped `mcp_catalog` now lets the model search cached server/tool/prompt/resource metadata and fetch exact prompts/resources without exposing the full catalog as Provider schemas.
- Failure mode: useful server context exists but the Agent cannot autonomously find it.
- Architecture: `ProductionRuntimeHost → scoped_mcp_runtime → mcp_catalog`; ContextVar isolation prevents cross-workspace discovery.
- Benchmark: deterministic catalog contract covers all four kinds, exact retrieval, filtering and bounded results. Live 100-server soak remains unclaimed.

### 7. Persistent PTY/process service — P1

- Reference: OpenCode PTY routes/service.
- NZ-Coder: cancellable one-shot Popen.
- Failure mode: dev servers, watch tests, REPL/debugger and log follow cannot persist.
- Architecture: platform ProcessSession service; bash remains one-shot.
- Benchmark: create/write/read/resize/kill/reconnect reliability and resource cleanup.

### 8. Web search discovery — P1

- Reference: InfCodeX web-search and OpenCode websearch/provider tools.
- NZ-Coder: webfetch requires a known URL.
- Failure mode: Agent cannot locate current documentation or issue evidence.
- Architecture: provider-neutral WebSearchProvider with permission, cancellation, trace and bounded results.
- Benchmark: source discovery recall, latency, citation validity and failure rate.

### 9. Memory proposal control plane — implemented in Phase 5 continuation

- Reference: InfCodeX review inbox/intake/triage/safe-apply.
- NZ-Coder: automatic extraction creates fingerprinted proposals with source Session/message IDs, confidence, reason and risk. Low-risk explicitly approved/repo-scoped candidates apply safely; high-risk/model-inferred candidates enter a durable review inbox and ledger.
- Failure mode: poisoned or high-impact memories affect later tasks.
- Architecture: `MemoryControlPlane` wraps the existing `MemoryManager`; retrieval/storage backends were not rewritten.
- Benchmark: the poisoned candidate proxy changed from one direct save to zero saves and one pending review; provenance was retained. Human-review precision still needs real usage evidence.

### 10. Permission pattern expressiveness — P1

- Reference: OpenCode wildcard/pattern/multi-ruleset/last-match evaluation.
- NZ-Coder: strong modes and bash-prefix/tool rules, simpler grammar.
- Failure mode: teams must over-grant or repeatedly approve path/domain-limited operations.
- Architecture: backward-compatible PermissionPattern with deterministic specificity and last-match audit trace.
- Benchmark: rule corpus correctness, explainability and evaluation latency.

## False Gaps / Different by Design

- LSP: one `lsp(operation=...)` tool covers advanced navigation; splitting it into many names adds schema cost without behavior.
- Background controls: `BackgroundAgentManager` already supplies start/status/events/wait/stop/cancel/message/process isolation.
- Workflow: different manifest/DSL, but Agent nodes use the shared child/Runner chain.
- MCP transport: stdio, HTTP, SSE, OAuth, trust, list/call/prompts/resources/notifications already exist.
- Session events: sequence, IDs, bounded replay, JSONL journal, resume cursor, gaps and SSE consumption are substantive.
- Tracing basics: run/trace/session/agent lineage and model/tool/child timing already exist.
- Verification, planning, reflection, sidecars, worktree, parallel tools, cancellation, snapshots, and memory retrieval are not major missing capabilities.
- Plugin folder absence is not itself a gap: existing extension mechanisms cover external service, instructions, local tools, hooks, and workflows.

## Implemented Capability Clusters

### Tool Intelligence

- Added immutable ToolDefinition/ToolCatalog and deterministic ToolSearchIndex.
- Added 6k-token conservative exposure planner, resident core, dynamic MCP deferral, run-owned unlock and `tool_search`.
- Preserved legacy registration/dispatch and full catalog outside a bound run.
- Contract tests cover resident/deferred/unlock/isolation/full-schema/search.

### Repo Intelligence V2

- Preserved `PersistentCodeIndex`; added persistent module/import graph for Python, TS/JS, Go and Rust.
- Added fingerprint reuse, deletion/rename, module context, relationship scan, SCC cycles, overview and changed scope.
- Added one operation-based `repo_context`; it is structural intelligence, not semantic search.

### Governed Skills

- Preserved SkillLoader and precedence; added model/provenance/resource base and validation.
- Loading a Skill activates run-local allowed-tools intersection enforced in ProductionToolPolicy.
- Model preference is surfaced, not silently switched mid-turn; scripts remain disabled.

## Benchmark Results

Tool proxy benchmark uses deterministic exact discovery; network/provider TTFT and actual model accuracy are explicitly not claimed.

| Tools | Schema tokens before | after | deferred | selection | wrong call | serialization median |
|---:|---:|---:|---:|---:|---:|---:|
| 20 | 1,342 | 1,342 | 0 | 100% | 0% | 0.032 ms |
| 60 | 4,102 | 4,102 | 0 | 100% | 0% | 0.087 ms |
| 120 | 8,259 | 169 | 117 | 100% | 0% | 0.007 ms |
| 200 | 13,859 | 169 | 197 | 100% | 0% | 0.007 ms |

Repository benchmark (generated dependency chains):

| Repo | modules | cold | warm | incremental | query | dependency recall | cache reuse |
|---|---:|---:|---:|---:|---:|---:|---:|
| small | 20 | 3.4 ms | 2.0 ms | 1.8 ms | 0.009 ms | 100% | 100% |
| medium | 200 | 24.2 ms | 20.3 ms | 20.0 ms | 0.036 ms | 100% | 100% |
| large | 1,000 | 308 ms | 288 ms | 288 ms | 0.047 ms | 100% | 100% |

The warm large-repo result shows a remaining full-inventory scan cost and directly supports Gap #4. Skill behavioral benchmark changed one unauthorized call from executable to rejected, preserved model metadata, and detected zero cross-session leakage.

## Phase 5 Continuation: Result and Memory Control

### Unified Tool Result Budget

- Added `nz_coder/tool_platform/results.py` with a context-derived per-result token budget.
- Added immutable projection metadata: original/projected chars and tokens, truncation, artifact path and persistence error.
- Integrated it in `ProductionToolResultProjector`, before every model-visible and observable consumer.
- Preserves both the beginning and failure-heavy tail rather than first-N-only truncation.

Benchmark fixture: 35,013 input tokens → 1,483 visible tokens, 95.76% pressure reduction, head recall 1.0, tail recall 1.0, durable reference present.

### Memory Proposal Control Plane

- Added `nz_coder/state/memory_control.py` with proposal, inbox, deterministic dedupe, risk classification, approval/rejection and append-only ledger.
- Routed `run_auto_memory_pipeline()` through the control plane; it no longer calls `MemoryManager.save()` directly.
- Explicit user `remember` statements retain backward-compatible auto-apply after risk screening. Model-inferred, low-confidence, security/tool and cross-project policies fail closed into review.
- Existing Markdown/SQLite memory storage, retrieval, reranking and dream consolidation remain unchanged.

Benchmark fixture: poisoned candidates saved before=1, after=0; pending review=1; provenance preserved=true.

### Native SDK Default Completion

The default chain is now:

```text
AgentClient default
→ NativeSDKRunner
→ SessionRuntime.open
→ RunnerExecutionContext
→ AgentRunner Model→Tool→Model
```

The run-scoped environment owns provider/client lifetime, workspace/session
bindings, permissions, transaction/change tracking, context budgeting, message
projection, guardrails, structured-output repair and durable checkpoints. Importing
`nz_coder.sdk` does not import `runtime.loop`; only explicit `agent_factory=` can
enter the legacy factory. An offline Provider acceptance test executes a real
`list_directory` call between two model turns through the zero-argument client.

## Native SDK Migration Slice: Memory and Verifier

- Added `MemoryExecutionContext`/`MemoryRecallState`; production recall,
  extraction, proposal routing and lineage receipts no longer receive a broad
  Agent host.
- Added `VerificationExecutionContext`; the production completion verifier now
  consumes only an optional compatibility override and one focused async review
  callback.
- Added legacy adapters at the AgentLoop boundary. Native composition can later
  construct these same contexts without importing AgentLoop.
- This reduces the SDK blocking set, but does not yet change the default SDK
  factory. Lifecycle, guardrails, input, transitions, message/planning/snapshot
  runtime and coding observer still need focused native owners.

## Phase 5 Continuation: MCP Agent Catalog

- Added a run-owned MCP Runtime binding using `ContextVar`; it is installed and reset by `ProductionRuntimeHost`.
- Added one operation-based `mcp_catalog` tool for bounded search, prompt retrieval and resource reads.
- Search surfaces only safe cached metadata and excludes MCP commands, environment variables, trust secrets and OAuth data.
- Existing dynamic MCP tools and progressive Tool Exposure remain unchanged; `mcp_catalog` itself participates in the normal catalog/exposure policy.
- Offline contract verification covers server/tool/prompt/resource discovery, kind/query/server filtering, exact prompt/resource calls, errors and concurrent-context isolation. No live-server/network parity is claimed.
