# Terminal Real-World Issues Repair Design

## Scope

Repair all open issues TP-001 through TP-020 recorded in
`docs/terminal-product-real-world-issues.md`. The work is split into three
root-cause batches so runtime correctness is stabilized before intelligence and
presentation layers are changed.

## Batch 1: Runtime closure, safety, and interaction state

Issues: TP-003, TP-004, TP-005, TP-006, TP-007, TP-013, TP-014, TP-015,
TP-016.

- Treat non-Git workspaces as a supported runtime mode. `diff_status` must use
  tracked workspace changes or return a successful not-applicable result, never
  a Git usage error.
- Reject model-invented paths outside the active workspace before shell
  execution. The denial remains a recoverable tool result so the Agent can use
  workspace-local tools instead.
- Make ordinary permission rejection recoverable. Hard policy denials remain
  denied, but neither kind may terminate the run merely because one tool was
  rejected.
- Persist the narrowest safe command approval within the Session. Equivalent
  workspace-local pytest/check commands should reuse approval without opening a
  broad shell capability.
- Bound post-test diagnosis and final review. A verified edit can finish even
  when optional Git evidence is unavailable; an environment blocker gets one
  focused diagnostic and a clear unverified result.
- Cancellation immediately prevents new tool admission and produces a durable
  cancelled terminal state.
- Full-screen selectors consume their complete submit event before returning to
  the composer.
- Plan completion either presents a real question overlay or returns the saved
  plan summary without an extra idle provider round-trip.

## Batch 2: Repository intelligence and LSP

Issues: TP-001, TP-002, TP-018.

- Define one default repository ignore policy and use it in ripgrep and walking
  fallbacks. Product state, VCS metadata, caches, dependencies, and build
  artifacts are excluded unless the user explicitly names such a path.
- Give simple repository-orientation requests a bounded exploration path:
  filtered directory listing first, then only the minimum project manifests or
  README files required to answer. Existing general coding behavior remains
  available for ambiguous or implementation requests.
- Configure Python LSP roots for nested package layouts. If the selected project
  root is itself an imported package, expose its parent through the analysis
  execution environment. Verify hover and definition, not just executable
  presence.

## Batch 3: Terminal presentation and history hygiene

Issues: TP-008, TP-009, TP-010, TP-011, TP-012, TP-017, TP-019, TP-020.

- Switch wide tables to width-aware compact layouts. At 80 columns, Sessions
  and processes use one-record cards/two-line rows with complete identifiers.
- Clip only status summaries; preserve reviewable command content in wrapped
  detail blocks.
- Separate current-run state from durable scrollback. Starting a run resets the
  live projection while older help, tables, tool cards, and run summaries stay
  in history.
- Project resumed history through user-visible message parts only, then anchor
  the viewport at the latest complete message.
- Expand top-level help into stable commands, examples, configuration, and
  model-selection guidance.
- Keep the first Ctrl+C exit hint visible for the full confirmation window.

## Compatibility and safety

- Public tool names, parameter schemas, and string return compatibility stay
  intact.
- No Agent framework or new runtime dependency is introduced.
- File edits continue through the existing transaction and ChangeTracker path.
- Shell admission remains fail-closed for dangerous commands and external
  paths.
- Tests cover Linux behavior with platform-neutral units for Windows paths.

## Verification

Each batch follows RED-GREEN regression testing. Final acceptance includes the
focused test groups, the full relevant terminal/runtime suite, and real PTY
sessions in `/home/pyh/test_nzcoder` at 80 columns. The issue ledger keeps all
original evidence and receives fix evidence plus `closed` or `verify` status;
items needing Windows hardware remain `verify` rather than being overstated.

## Reference-runtime convergence addendum

Issues: TP-023, TP-024, TP-025.

### Cancellation terminal protocol

Follow the InfCodeX `tool-cancellation` / `catch-terminals` ordering and the
infcode-dev interrupted-message contract without copying their TypeScript host
layers. A cancellation first settles any in-flight assistant/tool parts, then
persists the resumable transcript, runs the ordinary NZ-Coder lifecycle
terminal exactly once, emits `run_end` plus the canonical cancelled runtime
event, and finally returns a typed cancelled result to the terminal host.
Cancellation is a successful interruption, not a generic runtime error. The
CLI must wait for this cleanup result instead of rendering a synthetic local
cancel state before the Runner has closed the run.

### User-declared verification intent

The broad-test safety gate remains fail-closed for tests the Agent invents after
editing, but an exact test command or test scope explicitly present in the
latest natural user request is admitted. Intent matching is structural: parse
the test runner and requested path(s), normalize harmless wrappers such as
`python -m` and output redirection, and require the attempted command's test
targets to stay within the declared scope. A request for
`python -m pytest -q cron_engine/tests` therefore admits that directory suite;
it does not admit repository-wide `pytest` or another test directory.

### Run work-budget convergence

Use the InfCodeX managed-budget disclosure zones on top of NZ-Coder's existing
hard turn cap. The run records model turns, tool calls, cumulative provider
input tokens, failed tools, and verification progress. At 70% of the admitted
turn budget it injects one convergence instruction; at 85% it instructs the
model to stop broad exploration and complete the implementation/verification
path; at 95% only the minimum unresolved acceptance work and final synthesis
remain. Each zone is emitted once and traced. The existing hard cap remains the
authoritative terminal boundary, and explicit user turn budgets are still
clamped by the system cap. This design does not impose a new token quota or
prematurely stop a complex task.

### Acceptance evidence

- Cancelling during a Provider call and during a tool call leaves no orphan
  parts, saves a cancelled Session, records `run_end/status=cancelled`, emits a
  cancelled lifecycle event, and returns to the same REPL.
- A user-requested directory test suite is allowed after source edits, while an
  unrequested repository-wide or sibling-directory suite remains blocked.
- Budget zone guidance appears once per crossed zone and survives resume from
  the persisted turn count; the hard maximum still produces `max_turns`.
- Real PTY cancellation and one long edit task in `/home/pyh/test_nzcoder`
  confirm the durable trace rather than only the screen rendering.
