# Coding Agent Capability Parity Design

## Scope and evidence gate

This phase performs a fresh source-level three-way audit and implements three bounded capability clusters that address measured failure modes without reopening the Runtime architecture:

1. Tool Intelligence: catalog, search index, context-budget-aware exposure, and per-run unlock.
2. Repo Intelligence V2: persistent overview/module dependency graph and high-value context queries on top of the existing code index.
3. Governed Skills: real model metadata, source provenance, per-run allowed-tools enforcement, validation, and isolation.

Public SDK default-native remains a documented P0 gap. Fixing it now would require replacing the remaining host-shaped coding execution capabilities, which is a Runtime migration rather than a bounded capability addition and conflicts with the explicit “no global Runtime rewrite” constraint.

## Tool Intelligence

`ToolCatalog` adapts the existing registry and dynamic MCP definitions into immutable `ToolDefinition` values. Registration stays backward compatible. `ToolSearchIndex` provides deterministic exact and weighted lexical lookup. `ToolExposurePlanner` estimates schema tokens and applies a conservative policy only when the catalog crosses a configurable budget. Core editing/search/coordination tools and `tool_search` remain resident; rare repo, advanced LSP, project-authoring, memory administration, workflow-authoring, and large dynamic MCP surfaces may be deferred.

Unlock state is owned by `RunContext.metadata` and made available to the tool handler through a run-scoped ContextVar middleware. `tool_search` returns full schemas and unlocks selected tools for the next provider turn. No global unlock set exists. Child runs receive their own RunContext and do not leak unlocks.

## Repo Intelligence V2

The existing SQLite `PersistentCodeIndex` remains the file/symbol/reference owner. A separate `RepositoryGraph` materializes a bounded module/import graph with fingerprints and JSON cache. It supports Python, TypeScript/JavaScript, Go, and Rust with explicit language-specific parsers and a conservative fallback. Queries provide repository overview, changed scope, module context, relationship scan, and cycle detection through one operation-based `repo_context` tool.

The graph is refreshed incrementally by comparing file fingerprints. It does not claim semantic/embedding search. Large-repository budgets cap files, edges, output characters, and query results.

## Governed Skills

The current `SkillLoader` remains discovery/resolution/loader owner. `Skill` gains parsed `model`, canonical base directory, validation diagnostics, and provenance. Loading a skill activates a run-local `SkillExecutionContext`; a declared `allowed_tools` list becomes a real intersection guard in `ProductionToolPolicy`. `load_skill` itself remains callable so the model can inspect another skill. Concurrent sessions get independent contexts through `bind_skill_loader`.

Model preference is exposed as validated execution metadata for composition and tracing, but this phase does not silently switch providers mid-turn. Resource paths remain bounded to the skill directory; script execution is not added because it would require a separate permission/process design.

## Benchmarks and stop rule

- Tool benchmark: 20/60/120/200 definitions; schema/request tokens, request serialization latency (explicitly not network TTFT), selection accuracy, wrong selection, proxy task success, and savings.
- Repo benchmark: generated small/medium/large repositories; cold/warm/incremental time, query latency, graph recall, cycle accuracy, and context size.
- Skills benchmark: enforcement success, isolation, invalid metadata, and lookup latency.

At most these three clusters are implemented. Web Search, PTY, plugin unification, true semantic search, memory proposal control plane, wildcard permissions, and native-default SDK remain roadmap items.

## Compatibility and failure policy

- No new dependency or Agent framework.
- Existing registry names, schemas, handlers, tool runtime, MCP transport, LSP operation tool, Workflow, and AgentLoop APIs remain compatible.
- Exposure fails open to the full allowed catalog when state/policy is unavailable.
- Repo parsers fail per file and preserve the last valid cache; paths remain workspace bounded.
- Skill constraints fail closed after a governed skill is loaded; invalid skills are excluded with diagnostics.

## Acceptance

Capability contract tests prove exposure visibility/unlock/isolation/MCP scale; repo cold/warm/update/delete/rename/dependency/cycle/fallback; skill precedence/activation/enforcement/model/resources/invalid/reload/isolation. A fresh 50+ capability matrix, Top 10 real gaps, false gaps, before/after benchmarks, architecture guards, full pytest, Ruff, compile/import smoke, SCC scan, and diff check complete the phase.
