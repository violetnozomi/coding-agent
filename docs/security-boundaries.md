# Security Boundary Inventory

This document is the review ledger for NZ-Coder's security-sensitive inputs,
execution sites, filesystem operations, public errors, and long-lived
resources.  The executable source of truth is
`tests/architecture/test_security_boundaries.py`; CI fails when a new entry is
introduced without an explicit classification.

The inventory was created at baseline
`d42b263778b350688f476ff653d08769887d856d`. Its classifications are now
enforced as closure invariants: there are zero unclassified configuration,
subprocess, model-file-I/O, public-error, or cross-run-resource entries.

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
`private diagnostic` or `trusted local validation`. There are zero reviewed
`public/model-visible` raw projections. An Exception can cross a model,
Session, Trace, HTTP, or Terminal boundary only through `PublicError`,
`TrustedPublicMessage`, or the predefined local-validation type
`PublicInputError`.

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

## Closure implementation (2026-09-04)

### Authority and state roots

Project Authority is limited to the captured `.env`, `AGENTS.md`, `CLAUDE.md`,
`.nz-coder/settings.json`, `.nz-coder/mcp.json`, loadable Skill/Command/Workflow
definitions, rules, and instruction-file state listed above. The same immutable
bytes and fingerprint are used for the complete Run; a later filesystem change
is visible only to a newly captured Run and invalidates the previous trust
identity.

On POSIX, user state is rooted at
`$XDG_STATE_HOME/nz-coder` (default `~/.local/state/nz-coder`) and derived cache
at `$XDG_CACHE_HOME/nz-coder` (default `~/.cache/nz-coder`). On Windows both are
under `%LOCALAPPDATA%\nz-coder\{state,cache}`. Each workspace uses an opaque
canonical-identity key under `workspaces/`; repository-controlled path names do
not become user-state path components. Legacy `.nz-coder` state is migration
input only and is never an active authority/state root.

### Immutable RunSettings

`RunSettings` captures the following policy families from the same
`ConfigSnapshot`:

- Agent budgets: turns, nominal turns, parallel tasks, tool calls, doom-loop,
  read deduplication and continue-on-deny;
- Provider lifecycle: retry, hard/idle timeout, cancel grace, fallback,
  checkpoint/delta cadence, context/output/system budgets;
- execution: Bash timeout/output/package policy, persistent-process
  buffer/read/write/count/kill/encoding limits, and batch-write limits;
- planning, replanning, reflection and verification-gate budgets;
- LSP/MCP enablement and timeouts, LSP diagnostic/output controls, repository
  index limits;
- child Agent model/turn/time/worktree/concurrency/process-isolation controls;
- memory, trace and runtime persistence;
- image Provider, model, credential, endpoint and output budget;
- auto-mode classifier enablement, timeout, output and circuit-breaker limits.

The architecture inventory contains 37 compatibility/default reads across 11
modules. None is classified `run-scoped`: host CLI bootstrap, legacy embedding
adapters and immutable product/schema defaults are the only remaining direct
`config.<NAME>` uses.

### ExecutionIdentity and credential delegation

MCP and LSP trust binds the resolved executable path and hash, interpreter
entrypoint kind/path/module/hash, cwd identity, normalized argv semantics,
configuration source, scrubbed environment profile and whether any executable
payload is workspace-controlled. Python/Node scripts, `python -m`, shell
scripts, PowerShell `-File`, Java `-jar` and dotnet DLL entrypoints are
recognized; inline interpreter code fails closed. URL identity includes the
normalized query. The complete identity is checked again immediately before
spawn. A final OS-level replacement race remains on platforms where Python
cannot execute directly from an already-verified executable handle.

`ProviderConnection` records `credential_source` and `endpoint_source`.
Official endpoints are allowlisted. An owner environment/user credential is
not sent to a custom `trusted-workspace` endpoint merely because general
workspace configuration was trusted: `config delegate-provider-endpoint`
must record a separate trust for the exact provider family and endpoint hash.
Changing the endpoint invalidates that delegation; `config untrust` removes it.

### WorkspaceFileAccess

On POSIX, workspace reads open every directory component beneath a verified
root descriptor with `O_DIRECTORY | O_NOFOLLOW`; writes use an opened parent
descriptor, exclusive temporary file, file and directory `fsync`, and
handle-relative `replace`/`unlink`. The parent descriptor remains open across
the operation, so a path swap cannot redirect the I/O outside the captured
directory.

On Windows, the service opens/holds directory and file handles, rejects reparse
points and checks final path plus volume/file identity. Python does not expose a
general handle-relative `ReplaceFile`; the final replacement therefore retains
a documented Windows rename TOCTOU boundary and fails closed whenever the
pre-replacement identity cannot be proved. Model-facing file, patch, structural
search and document-source reads use this service; document converter/cache I/O
is owner-private derived-cache work and remains separately inventoried.

### Cleanup ownership and active revocation

```text
ProductRunEnvironment
  -> RunControlBundle: stall sidecar, MCP, provider runtimes
  -> BackgroundAgentManager: background/workflow children
  -> repo intelligence
  -> event bus
  -> tracer

Session deletion
  -> BackgroundAgentManager cancel/join
  -> ProcessService kill_session
```

Environment cleanup is a retryable ten-stage ledger. A failing stage records
only its resource label and exception type, does not mask the business result,
does not replay completed stages, and cannot prevent later independent stages
from running. A child owns its own Provider runtime; the parent environment
does not double-close it. Constructor failure retires each resource that was
successfully acquired before the failure.

Persistent processes, background children, workflow children and cached LSP
clients publish a process-local `CapabilityLease` containing workspace
identity, control fingerprint, run/interaction identity, creation time and
owner Session. Plain `config untrust` explicitly reports that active resources
remain and affects only the next Run. `--revoke-active` invokes each owned
resource's synchronous stop/settle callback and reports both revoked and failed
counts; failed revocations keep their lease. A standalone CLI process cannot
reach resources owned by a different daemon process, so it never claims to
have revoked such resources; daemon-wide remote revocation remains a separate
control-plane boundary.

### Protocol and lock limits

LSP accepts at most 64 KiB of headers and a 16 MiB JSON-RPC frame; diagnostic
count, individual diagnostic size and stderr lines/tail are bounded. MCP stdio
accepts at most a 16 MiB newline-delimited JSON frame and bounded stderr;
Streamable HTTP and SSE already enforce line, event and 10 MiB response limits.
An oversized/invalid local protocol frame fails pending calls and terminates
the corresponding subprocess.

Private-state locks reject target symlinks, parent symlinks, non-regular files
and Windows reparse points. POSIX creates/opens the parent chain through held
directory descriptors and opens the lock with `O_NOFOLLOW`; Windows performs
pre/post alias checks and relies on the platform lock primitive. Advisory locks
still coordinate only cooperating processes and are not an OS sandbox.

### Verification boundaries

The executable contracts live in `tests/architecture/test_security_boundaries.py`
and `tests/architecture/test_security_closure_contracts.py`. They fail when a
new direct configuration read, subprocess launch, model-facing file operation,
raw public exception projection or cross-run resource appears without an exact
classification. Runtime attack tests cover instruction/config snapshotting,
workspace-state aliases, cross-workspace settings, MCP/LSP identity changes,
parent swaps, cleanup failures, active revocation, credential delegation,
oversized protocol frames and private-lock aliases.

The remaining honest boundaries are: Shell is not sandboxed; Webfetch retains
DNS/connection TOCTOU unless the actual peer can be verified; final Windows
replace and executable launch retain the platform/Python handle limitations
described above; process-local active revocation is not cross-daemon IPC; and
GitHub branch protection, release approvals and Action SHA pinning are
repository-administration controls rather than properties of this source tree.
