# Terminal Product Parity Phase 3 Report

Date: 2026-08-13

## Scope

This first Phase 3 batch closes the highest-value operator-facing controls
without changing Agent, Session, Tool, Memory, or Extension execution state
machines.

## Delivered

### Tool-Specific Rendering

Compact tool output now uses domain labels when the runtime supplies the
authoritative category metadata:

- `Edit` for file mutation tools;
- `Read` and `Search` for repository navigation;
- `Web Search` for external discovery;
- `Child` for delegated Agent work;
- existing `Process` cards for long-running process operations.

The legacy callback fallback remains generic when it has no category metadata,
so older integrations keep their previous output contract.

### Memory Review UX

The existing `MemoryControlPlane` is now available from both the embedded
terminal and non-interactive CLI:

```text
/memory pending
/memory inspect FINGERPRINT
/memory approve FINGERPRINT
/memory reject FINGERPRINT [REASON]
/memory ledger

nz-coder memory pending --json
nz-coder memory inspect FINGERPRINT --json
nz-coder memory approve FINGERPRINT
nz-coder memory reject FINGERPRINT --reason "..."
nz-coder memory ledger --json
```

The terminal view exposes candidate name, source Session, risk, confidence,
content, reason, and fingerprint. Approval and rejection call the existing
atomic control-plane methods; no client-side memory state is introduced.

### Extension Metadata Reload

`nz-coder extensions reload` explicitly takes a fresh `ExtensionRegistry`
snapshot, with optional JSON output. It is intentionally metadata-only:
reload does not import arbitrary extension code and does not invent an
enable/disable persistence layer. Existing `list` and `status` behavior is
unchanged.

The embedded terminal also supports `/extensions list`,
`/extensions status EXTENSION_ID`, and `/extensions reload`. These commands
reuse the same fresh `ExtensionRegistry` snapshot used by the non-interactive
CLI.

### Interactive Memory Review

`/memory-review` opens the existing fuzzy selector with approve/reject actions,
proposal inspection, and a rejection-reason prompt. It does not cache
proposals as client truth; every action re-reads and mutates the shared
`MemoryControlPlane`.

### Session Product Audit

The existing Session surface already covers list/resume/fork/rename/delete,
timeline/message inspection, diff, undo/redo, export, lineage and child
navigation in Embedded and Remote modes. No duplicate Session command or
state layer was added in this batch.

## Evidence

- Full repository suite: **1917 passed**, seven existing Python 3.13
  multiprocessing/fork deprecation warnings.
- Memory, extension, CLI, backend and terminal regression selection:
  **45 passed** for the new Phase 3 batch; the broader related selection was
  also green before the full run.
- Ruff passed for all changed Phase 3 files.
- `git diff --check` and Python compilation passed.

## Product Boundary

Still deferred by design:

- full PTY terminal pane / ANSI screen model;
- dedicated Memory review pane and richer bulk actions;
- extension enable/disable and arbitrary code hot reload;
- custom commands;
- installer/upgrade and platform polish.

These are product-surface follow-ups, not missing Core Runtime capabilities.
The next Phase 3 batch should prioritize a dedicated review pane or bulk
actions only if interaction tests show the selector and command surface are
insufficient.
