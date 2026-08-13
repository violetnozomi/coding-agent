# Terminal Product Parity Phase 2 Report

Date: 2026-08-13

## Outcome

Phase 2 closes the local-daemon Remote Terminal control loop without adding a
second Agent, Session, interaction, or process state machine. Remote controls
are projections over `SessionManager`, `InteractionBroker`, the Session event
bus, persisted Session files, and the workspace `ProcessService`.

The delivered boundary is:

- Remote Session list, inspect, continue, rename, delete, fork, abort,
  timeline/message inspection, diff, undo/redo, export, and lineage navigation.
- Session runtime states distinguish running, waiting for permission, waiting
  for a question, idle/completed/failed/cancelled, and interrupted.
- Embedded and Remote `/processes` views support list, inspect, bounded logs,
  bounded follow, and kill using the same `ProcessService` records.
- Pending interactions survive client disconnect and are recovered from the
  daemon-side broker. Resolution is atomic: first valid response wins.
- Normal SSE reconnect resumes after the last event ID. Replay gaps explicitly
  rebaseline from an attach snapshot, reset transient reducer state, and retain
  event-ID deduplication.
- Graceful daemon restart preserves persisted Sessions, rotates the token, and
  cleans persistent processes. A persisted active marker is restored honestly
  as interrupted/recoverable, never as a still-running task.
- Forced daemon termination is covered by an actual POSIX `SIGKILL` scenario.
  Restart clears the daemon-owned stale lifecycle fence, restores the Session
  as interrupted, and never treats the dead runtime as still active.

## Phase 1 Truth Audit

| Question | Verified result |
|---|---|
| Independent Remote Session truth? | No. Remote calls the HTTP `SessionManager` and persisted Session store. |
| Independent Remote process truth? | No. HTTP and Embedded controllers call `workspace_process_service()`. |
| Client-owned pending interaction truth? | No. `InteractionBroker` owns unresolved requests in the daemon process. |
| Attach snapshot and replay handoff | One event-bus checkpoint captures Session/messages/pending state and an atomic cursor; SSE starts strictly after that cursor. |
| Gap repair cursor | An expired cursor emits an explicit gap, fetches a new attach snapshot, rebases the renderer, then resumes from the snapshot cursor. |
| Session after daemon restart | Persisted Session list/inspect/continue remains available. |
| Previously running Session after restart | Restored as `interrupted`, with no live run task claimed. It can be continued explicitly. |
| Persistent process after restart | Not reattached. Graceful shutdown kills owned processes; the new daemon starts with an empty process registry. |

No `remote_state.json`, remote message store, or remote process registry was
introduced.

## Remote Session Parity

| Session capability | Embedded | Remote | Verdict |
|---|---|---|---|
| list | Saved Session list/picker | `/sessions` from daemon | Aligned |
| inspect | `/status`, timeline | `/status`, `/timeline`, `/message` | Aligned |
| resume / continue | Resume same persisted Session | Select same Session ID and append a new run | Aligned |
| rename | Existing Session store rename | `PATCH` through manager rename | Aligned |
| delete | Confirmed delete and cleanup | Typed-ID confirmation, manager delete, process cleanup | Aligned |
| fork | Existing history fork | Server-side history fork; no client transcript copy | Aligned |
| abort | Controller cancellation | Existing managed-run cancellation | Aligned |
| timeline / message | Existing transcript renderer | Same transcript document projection | Aligned |
| diff | Snapshot/change projection | Same persisted Session diff | Aligned |
| undo / redo | `SessionReverter` / snapshots | Same `SessionReverter` and snapshot paths | Aligned |
| export | Existing transcript format | Same transcript formatter; client optionally writes the result | Aligned |
| parent / fork children | Persisted parent metadata | `/parent` and `/children` from Session metadata | Aligned |
| child Agent inspect | `/subagents` registry | `/subagents` and `/child` read the same registry; RUNNING-to-complete reconnect is tested | Aligned for inspection |
| child Agent continuation | Embedded `/subagent` | Not exposed remotely in Phase 2 | Partial; deferred product control |

Embedded fork persistence was corrected to retain `parent_session_id`; both
Embedded and Remote fork records now preserve lineage. Remote fork also stores
the inherited model rather than recomputing it after daemon restart.

## Embedded And Remote Capability

| Capability | Embedded | Remote |
|---|---|---|
| Agent run | `ProductRunEnvironment` / `AgentClient` | Same product runtime behind `SessionManager` / `AgentClient` |
| Session truth | Session persistence/runtime | Same daemon Session truth |
| event rendering | `TerminalRunRenderer` | Same renderer fed by SSE events |
| permission/question | Terminal bridge | Same bridge over daemon broker replies |
| process metadata/logs/kill | `TerminalSessionController` -> `ProcessService` | HTTP client -> same `ProcessService` |
| process write/resize | Agent tool/core API | Deliberately not exposed as direct Remote UI control |
| full PTY pane | Not provided | Not provided |
| unsupported slash commands | Local registry owns supported commands | Remote-only registry; unsupported commands are rejected locally |

The Remote command palette no longer advertises Embedded-only Phase 3
commands, and an unknown slash command cannot accidentally become an Agent
prompt.

## Reconnect State Machine

```text
ATTACHED(cursor=N)
  -> disconnect
  -> reconnect with Last-Event-ID=N
     -> cursor retained: replay N+1..latest
     -> cursor expired: explicit gap
          -> attach snapshot + atomic cursor M
          -> clear transient running reducer state
          -> apply snapshot events by event ID
          -> resume after M
```

`TerminalRunRenderer` keeps a bounded event-ID set, so an event shared by the
old stream and snapshot replay is applied once. Rebase clears stale running
tool/retry state; a tool that completed during disconnect cannot remain stuck
as RUNNING. HTTP/SSE transport details are hidden behind "Reconnecting..." and
"Reconnected" product messages.

## Process UX

```text
Embedded command / Remote command
          -> product controller / HTTP client
          -> Session ownership check
          -> workspace ProcessService
          -> bounded cursor read or kill
```

The list includes process ID, command, status, cwd, uptime, exit code, owner
Session, and PTY tier. Inspect adds PID, owner Agent, and buffer size. Log reads
use cursor/tail/max-byte budgets and do not enter the Agent transcript. Follow
is a bounded ten-second UI operation. Process lifecycle events render compact
cards; output bytes remain pull-based and are not copied into EventBus events.

Direct Remote process write/resize remains closed because no explicit
user-process-write permission contract was added in this phase. This avoids a
side channel around the existing command policy.

## Interaction Recovery

- A second client can read a pending permission or question from an attach
  snapshot and resolve it after the first client disconnects.
- Two clients receive the same durable event IDs.
- Broker removal and event publication are one locked resolution transaction.
  The first valid answer succeeds; later answers return `interaction_not_found`.
- A Session cannot reply to another Session's interaction ID.
- Abort, run settlement, timeout, Session deletion, and daemon shutdown reject
  and release pending waits rather than leaving them permanently pending.

## Daemon Restart Semantics

- `daemon restart` performs graceful shutdown, retains the prior workspace and
  port unless overridden, cleans Session-owned processes, and starts a fresh
  daemon/token.
- Completed and idle Sessions keep their persisted status/title/history.
- A persisted `running` marker becomes `interrupted`; no model stream, child
  task, or process is claimed to have survived.
- The user may explicitly continue an interrupted Session. The final model
  request is not automatically replayed.
- Persistent process reconnect across a daemon process restart is unsupported
  by design in this phase.
- A stale daemon PID is never killed unless the saved process identity still
  matches the live process.
- The lifecycle lock transfers from the short-lived starter to the verified
  daemon PID only after health succeeds. A dead daemon therefore leaves a
  provably stale lock that the next start may remove safely.
- Daemon nonce values use one `--nonce=value` argument. This fixes an observed
  startup failure when a generated nonce began with `-`; 50 consecutive
  start/restart stress cycles passed after the fix.

## Product Scenarios R1-R10

The strengthened Phase 2 scenario selection ran **15 deterministic integration tests, all
passing**. It exercises the real daemon/HTTP/SSE/session/process product paths
with controlled Agents; it is not presented as new real-model intelligence
evidence.

| Scenario | Result | Evidence |
|---|---|---|
| R1 Running reconnect | Pass | First SSE client detaches; a second client observes the same run completed and reads the final answer. Cursor replay is separately verified. |
| R2 Permission reconnect | Pass | A new client obtains the pending permission from attach snapshot, answers it, and the run settles. |
| R3 Question reconnect | Pass | A new client obtains and answers the pending structured question. |
| R4 Process reconnect | Pass | A new HTTP client lists, reads, and kills the Session-owned process; no active process remains. |
| R5 Child Agent reconnect | Pass | A real child execution thread is observed as RUNNING, the first client is dropped, the child completes, and a new client reads its completed identity/state from the same registry. |
| R6 Replay gap | Pass | Forced cursor expiration yields one gap/snapshot rebase; duplicate event application is zero and stale running-tool state is cleared. |
| R7 Daemon restart | Pass | Real daemon restart retains idle/completed Sessions, rotates token, rejects the old token, and permits persisted access. |
| R8 Interrupted run | Pass | A real daemon process is killed with `SIGKILL`; the next daemon removes only the dead owner's stale fence and maps its persisted running marker to interrupted/recoverable. |
| R9 Multiple clients | Pass | Both clients receive identical durable event IDs; one permission response wins and the other is rejected without a duplicate side effect. |
| R10 Multiple processes | Pass | Two process IDs are independently listed/read/killed; wrong identity access is zero and active/orphan count ends at zero. |

Measured scenario facts:

| Metric | Result |
|---|---|
| event gaps forced | 1 |
| snapshot resyncs | 1 |
| duplicate event applications | 0 |
| permission recovery | 1/1 |
| question recovery | 1/1 |
| cross-Session process attempts accepted | 0 (three attempts rejected) |
| cross-Session interaction attempts accepted | 0 (permission and question rejected) |
| orphan/active fixture processes after cleanup | 0 |
| child lifecycle reconnect | 1/1, RUNNING observed before disconnect |
| forced daemon crash recovery | 1/1, old PID dead and Session interrupted |
| scenario tests | 15/15 |

A repeatable local product benchmark starts the real daemon, creates a
persisted Session through authenticated HTTP, and invokes the actual
`python -m nz_coder attach` terminal entry point twice per repetition. Five of
five attach/reconnect pairs succeeded:

| Local product metric | Result |
|---|---|
| attach snapshot latency | median 0.975 ms; min 0.823; max 1.207 |
| first terminal attach latency | median 395.985 ms; min 389.962; max 400.281 |
| reconnect terminal latency | median 393.755 ms; min 387.557; max 400.283 |
| Session visible/resumable | 5/5 |

This is local loopback and process-start latency, not WAN latency. It makes no
model call, so token usage and coding-task success do not apply. The complete
machine-readable output is in
`docs/evidence/terminal-product-phase2-2026-08-13.json` and can be reproduced
with `python scripts/benchmark_terminal_product_phase2.py --repetitions 5`.

## Regression Evidence

- Final full repository suite after all Phase 2 product/UX changes: **1912 passed**
  with seven existing multiprocessing/fork deprecation warnings.
- Phase 2 product scenario selection after final scenario strengthening:
  **15 passed**.
- Phase 2 daemon/HTTP/backend/renderer/benchmark selection: **79 passed**.
- Daemon start/restart stress after lifecycle fixes: **50/50 cycles passed**.
- Verification, ProcessService, retrieval, Web Search, AgentManager, subagent,
  HTTP, daemon, backend, renderer, and terminal product regression selection:
  **251 passed**.
- Ruff, Python compile checks, and `git diff --check`: passed for changed Phase
  2 files.

## Reference Product Comparison

The comparison was rechecked against current local source snapshots:

- InfCodeX `src/runtime-daemon/{protocol,schema,server,manager,state}.ts`.
- OpenCode/Kilo `packages/opencode/src/server/routes/instance/{session,permission,question,pty}.ts`,
  `packages/opencode/src/pty/index.ts`, and Session revert/status sources.

| Capability | nzcoder | InfCodeX | OpenCode/Kilo | Verdict |
|---|---|---|---|---|
| long-lived local runtime | daemon + authenticated HTTP/SSE | runtime daemon + transport lease | server/attach runtime | Mostly aligned |
| Session list/get/continue/fork/delete | Yes | Yes | Yes | Aligned |
| Session rewind/revert | undo/redo over snapshots | `session.rewind` | revert/unrevert | Aligned behavior, different API |
| run abort | Session cancellation | `run.abort` | `session.abort` | Aligned |
| pending interaction recovery | attach snapshot + broker | pending permission/user-input daemon APIs | permission/question services | Aligned |
| multi-client response race | first response wins | daemon operation/interaction control | service request identity | Aligned for local daemon |
| replay/gap recovery | bounded event IDs + snapshot rebase | observe/event replay protocol | server event/session resync | Mostly aligned |
| process list/read/kill | bounded ProcessService API | managed process support, less product-defining than OpenCode | mature PTY service | Aligned for basic process UX |
| PTY WebSocket/write/resize pane | core write/resize exists; no Remote pane | no common reference advantage | create/update/remove/connect WebSocket terminal | OpenCode-specific remaining gap |
| daemon operation journal/idempotence | no durable operation journal | explicit operation envelope/control journal | server request model | InfCodeX depth gap for distributed clients |
| versioned remote settings/capability scopes | token + Session ownership | versioned settings and scoped admission | broader server surface | Remaining product-depth gap |
| cross-daemon live run/process survival | no; honest interrupted/cleanup semantics | richer daemon transport lifecycle | live server owns PTYs until it exits | Different by design; no false continuation |

## Remaining Gaps

Versus InfCodeX:

- The core Remote Session/interaction workflow is mostly aligned.
- InfCodeX remains deeper for daemon client leases, operation journaling and
  idempotence, scoped admission/capability grants, and versioned shared
  settings. These matter for broader multi-client/distributed deployment, not
  the local Phase 2 Session truth contract.
- Remote continuation of a child Agent is still Partial; read-only child
  recovery and live-state observation are delivered. This is a deferred
  control operation, not a second child-state truth gap.

Versus OpenCode/Kilo:

- OpenCode remains ahead in interactive PTY product depth: WebSocket terminal
  connection, live write, resize, and a terminal pane. nzcoder deliberately
  stops at metadata, bounded logs, and kill in Phase 2.
- OpenCode exposes a wider Session/server product surface, including sharing
  and more message-level controls. Cloud sharing and commercial surfaces remain
  out of scope.
- Core Session control, pending interactions, reconnect, and basic persistent
  process management are now mostly aligned for a local terminal Agent.

## Phase Boundary

Phase 2 acceptance is met for local daemon workflows: remote continue/fork/
abort, process list/read/kill, permission/question recovery, gap-safe renderer
resync, restart-safe Session persistence, honest interrupted runs, atomic
multi-client interaction response, Session ownership checks, and zero active
fixture processes after cleanup. R5 now covers an actually running child across
client disconnect, R8 covers an actual daemon process crash, and local
attach/reconnect latency has repeatable measured evidence.

Phase 3 may address tool-specific rendering, Memory Review UX, Extension UX,
and further Session polish. This report does not authorize those changes, a
full terminal emulator, ConPTY work, or core runtime redesign.
