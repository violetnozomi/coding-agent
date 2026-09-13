# Agent Core / InfCodeX / infcode-dev Gap Audit Design

**Date:** 2026-09-13

**Goal:** Establish one evidence-backed comparison of NZ-Coder's Agent Core with InfCodeX and the `infcode-dev` product, then close the highest-value gaps one at a time without claiming parity from file presence alone.

## Scope

The comparison has three explicitly different subjects:

1. **NZ-Coder Agent Core:** `nz_coder/runtime/agent`, `nz_coder/runtime/core`, the native runner, Session runtime, tool platform, extension registry, MCP runtime, skills loader, workflows, and the public Python SDK.
2. **InfCodeX reference:** `references/InfCodeX/packages/agent`, `references/InfCodeX/src`, and the reference product contracts in `references/InfCodeX/docs/PRD.md`, `ARCHITECTURE_OVERVIEW.md`, and `FEATURE_LIST.md`.
3. **infcode-dev product target:** `infcode-dev/infcode-dev/packages/opencode/src`, `apps/vscode/src/agent-manager`, `packages/kilo-indexing`, the gateway/bridge packages, and the product/agent-behaviour documentation.

The comparison separates core-runtime parity from host-product parity. A capability is marked **implemented** only when source, interface, and focused test evidence agree. A reference-only feature remains a gap even if NZ-Coder has a similarly named file.

## Design

### 1. Canonical audit document

Create `docs/agent-core-infcodex-infcode-dev-gap-audit.md` as the maintained human-readable source. It will contain:

- evidence versions and repository paths;
- a three-way matrix with `implemented`, `partial`, `planned`, `different-by-design`, and `unavailable` states;
- concrete source symbols and tests for every non-trivial claim;
- one acceptance criterion and one owner file set per gap;
- a dated change log so old benchmark snapshots are not mixed with current claims.

The document will explicitly preserve the distinction between the InfCodeX v2 reference benchmark (`0.8667` recorded success rate) and the older unavailable/zero-result snapshot.

### 2. Machine-readable parity manifest

Add a small JSON manifest beside the audit document. Each row contains an id, domain, status, priority, references, target files, and acceptance tests. The manifest is evidence metadata; it does not become a second runtime registry and must contain no credentials or workspace-specific absolute paths.

### 3. Highest-priority implementation slice

Close the first three gaps that are currently actionable inside NZ-Coder's core:

- **Capability contract evidence:** expose a stable, host-neutral capability snapshot with explicit status/provenance instead of a single undifferentiated fingerprint. All existing product surfaces continue to share the same immutable capability names.
- **Skills governance diagnostics:** expose precedence, shadowing, conditional, disabled, and parse-error information from `SkillLoader` without loading skill bodies or leaking secrets. This supplies the diagnostics needed by InfCodeX and `infcode-dev` settings/Agent Manager surfaces.
- **Parity test contracts:** add focused tests that assert capability snapshot stability and skill diagnostics behavior, including duplicate precedence and invalid metadata.

These changes are deliberately local. They do not introduce a new framework, provider, database, UI dependency, or alternate runtime. Existing tool and extension owners remain the source of truth.

### 4. Deferred gaps

The audit will keep, but not falsely close, the following product-level gaps for later slices:

- broader provider catalog and provider-specific capability metadata;
- cross-language incremental semantic indexing and watcher parity;
- full VS Code/JetBrains Agent Manager and HTTP/SSE client UX parity;
- multi-client daemon administration and transport-level permission arbitration;
- media/document product surfaces and richer external extension lifecycle events.

Each deferred row has a concrete discovery path and a reason it is not safe to mark complete in this slice.

## Interfaces

`nz_coder.runtime.execution.product_surfaces` will gain a read-only snapshot API returning JSON-compatible dictionaries. The API is deterministic, sorted, and contains only public capability metadata. Existing `capability_fingerprint()` remains unchanged for compatibility.

`SkillLoader.diagnostics()` will return deterministic JSON-compatible dictionaries. It reports source directories, loaded/conditional/disabled skills, duplicate shadowing, and bounded parse errors. It never reads skill bodies and never includes environment secrets.

## Error handling and safety

- Diagnostics are best-effort and fail closed: an unreadable or malformed skill header becomes a bounded diagnostic row and does not abort unrelated skill discovery.
- Snapshot APIs must not expose commands, environment variables, OAuth tokens, full prompts, or absolute paths outside the workspace unless a caller already supplied the path as a public configuration value.
- Existing handler error conventions and Python 3.9 compatibility remain unchanged.

## Validation

- Focused pytest tests cover normal, duplicate, conditional, disabled, malformed, and empty cases.
- Run `python -m compileall` on changed Python modules.
- Run Ruff on changed Python files if available.
- Run the focused test files, then the repository's standard test command relevant to the changed modules.
- A fresh review agent inspects the final diff, the audit matrix, and the test evidence after implementation. Any critical or important finding is fixed before the work is reported complete.
