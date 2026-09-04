# Security Boundary Inventory

This document is the review ledger for NZ-Coder's security-sensitive inputs,
execution sites, filesystem operations, public errors, and long-lived
resources.  The executable source of truth is
`tests/architecture/test_security_boundaries.py`; CI fails when a new entry is
introduced without an explicit classification.

The inventory records the baseline at `d42b263778b350688f476ff653d08769887d856d`.
Classification is not an assertion that a call site is already safe.  Entries
marked `run-scoped`, `legacy-model-reachable`, or `public/model-visible` are the
known migration set for this security-boundary closure.

## Configuration reads

Production code under `runtime`, `tools`, `capabilities`, `providers`, `mcp`,
and `lsp` currently contains 37 direct `config.<NAME>` reads in 11 modules.
Every key is classified as one of:

- `run-scoped`: execution semantics must come from the immutable RunSettings;
- `host-process-only`: host bootstrap state that cannot be workspace-selected;
- `static product default`: immutable product constants only;
- `test/compatibility-only`: explicitly isolated legacy adapters.

There are no accepted direct `config.<NAME>` reads classified as `run-scoped`.
Formal execution policy is captured once in the frozen `RunSettings` owned by
the current RunControl epoch.  Its coverage includes Provider and stream
timeouts, tool-call and write quotas, Bash/package policy, persistent-process
limits, context and output budgets, planning/replanning/reflection, verification
gates, LSP/MCP limits, repo-map limits, background/subagent policy, memory,
runtime-state/trace persistence, and image-description Provider identity.

The remaining reads are restricted to host CLI configuration, explicit legacy
embedding fallbacks, and static command-schema/product defaults.  The
architecture test records each `(module, key)` classification plus the exact
occurrence count.  A new direct read, including one added to an existing
module, fails CI and tells the author to route formal execution through
`RunSettings`.

## Workspace-controlled files

| Pattern | Classification | Runtime rule |
|---|---|---|
| `.env` | Project Authority | Trusted, immutable per run |
| `AGENTS.md`, `CLAUDE.md` | Project Authority | Trusted, handle-captured instruction snapshot |
| `.nz-coder/settings.json`, `.nz-coder/mcp.json` | Project Authority | Trusted ProjectControlSnapshot |
| `.nz-coder/skills/**`, `.nz-coder/commands/**`, `.nz-coder/workflows/**` | Project Authority | Trusted ProjectControlSnapshot |
| `.nz-coder/rules/**`, `.nz-coder/instruction-file-state.json` | Project Authority | Trusted instruction snapshot |
| `.nz-coder/memory/**` | Untrusted repository data | Never automatic user memory |
| `.nz-coder/models/selection.json`, `.nz-coder/models/registry.json` | Untrusted repository data | Never runtime model authority |
| repository source and arbitrary files | Untrusted repository data | Model data, not host authority |
| sessions, traces, changes, artifacts, attachments and generated memory | User-owned state | Stored outside the workspace |
| indexes, document cache and registry downloads | Derived cache | Stored outside the workspace |

No workspace file outside this table may affect a Provider request, prompt,
tool policy, permission decision, or subprocess identity without first being
added to the executable inventory.

## Subprocess launch sites

The executable inventory records all 16 direct launch calls in 11 modules.
Each site declares an ExecutionIdentity source, trust requirement, environment
profile, cwd source, and cleanup owner.  The intended profiles are:

- model shell/process: workspace identity, explicit permission, scrubbed model
  subprocess environment, run workspace, ProcessService owner;
- MCP/LSP: full interpreter and entrypoint identity, per-server execution
  trust, scrubbed protocol environment, snapshotted cwd, runtime client owner;
- document/search helpers: product binary identity, no project authority,
  scrubbed helper environment, run workspace, lexical owner;
- git/worktree helpers: resolved git identity, host-owned operation, scrubbed
  host environment, explicit repository/worktree cwd, lexical owner.

## Model-reachable file I/O

The inventory tracks direct filesystem calls by module and count.  Model tool
modules with direct calls are marked `legacy-model-reachable`; P0-E removes
those calls behind `WorkspaceFileAccess`.  Other classes are `project-authority`,
`private-user-state`, `host-maintenance`, and `remote-transport`.  A newly
introduced direct call or a changed count fails CI until reviewed.

## Exception projection

Raw `str()`/`repr()` conversions of caught exceptions are inventoried as
`private diagnostic`, `trusted local validation`, or `public/model-visible`.
The last class is the P1 migration set and must be projected through
`PublicError`, `TrustedPublicMessage`, or `PublicInputError` before crossing a
model, Session, Trace, HTTP, or Terminal boundary.

## Cross-run resources

| Resource | Owner | Lease identity | Cleanup | Untrust/config change |
|---|---|---|---|---|
| Persistent process | ProcessService | workspace + control + run | stop/close ledger | report active; optionally revoke |
| Background agent | BackgroundAgentManager | workspace + control + interaction | cancel/join ledger | report active; optionally revoke |
| Workflow child | Workflow runtime | workspace + workflow digest + run | workflow close ledger | invalidate next run; optionally revoke |
| MCP runtime | RunControlBundle | workspace + server ExecutionIdentity | MCP close stage | rotate on config identity change |
| LSP client | LSPManager | workspace + server ExecutionIdentity | client shutdown/kill | rotate on config identity change |
| Provider runtime | RunControlBundle | provider/config identity + run | provider close stage | rotate on identity change |
| Verification sidecar | RunControlBundle | workspace + control + run | sidecar close stage | rotate per run |
| Repository watcher/index | Repo intelligence service | workspace identity | environment close ledger | invalidate on workspace/config change |
| Event bus/tracer | ProductRunEnvironment | session + interaction/run | environment close ledger | never inherited as authority |

## Residual boundaries

These controls do not make Shell an operating-system sandbox.  A trusted
command may still read host files or use the network with the user's operating
system privileges.  URL validation also retains a DNS/connection TOCTOU window
unless the transport can verify the actual peer address.
