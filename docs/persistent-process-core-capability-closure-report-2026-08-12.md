# Persistent Process / PTY Core Capability Closure

Date: 2026-08-12

## Executive conclusion

The Agent-facing persistent-process gap is closed for the production POSIX
runtime. nzcoder now has a workspace-scoped `ProcessService`, stable serializable
process identities, bounded cursor reads, later stdin writes, status, resize,
kill, multiple concurrent processes, Session ownership, process-group cleanup,
runtime events, normal tool projection, and Bash-equivalent permission checks.

Real-model evidence used `openai-compatible/deepseek-v4-flash`, provider-default
reasoning, and three repetitions per P1-P6 case. Result: **18/18 successful**, no
wrong process access in the final matrix, and **0 orphan processes**. The
provider-free lifecycle contract also completed P1-P6 with zero structural
failures and zero orphans.

Windows support is intentionally tiered: persistent pipe processes support
start/read/write/status/kill and process-tree cleanup; native ConPTY and resize
are not claimed. Reconnection works across Agent turns/runs in one application
process. Reattachment across an nzcoder process restart is out of scope and is
not implemented.

The focused Verification Planner gate also showed a real benefit from existing
Repo Intelligence. A filename/import-only plan missed an indirect integration
test; the RI graph found it and the test exposed the regression. The planner now
surfaces high-confidence, already-indexed related tests as optional targeted
recommendations. It does not cold-build the index and does not make heuristic
relations mandatory completion blockers.

**CORE CODING CAPABILITY PHASE COMPLETE.** The remaining OpenCode differences
are primarily terminal product depth, not a missing Agent process-control core.

## Original Bash execution chain

The pre-existing `bash` tool remains the correct one-shot path:

```text
Agent tool call
  -> ToolExecutor / permission and command policy
  -> bash handler
  -> subprocess.Popen(shell=True, workspace cwd)
  -> POSIX process session / Windows child process
  -> daemon reader thread -> queue -> progress metadata
  -> timeout or cancellation -> process-group kill
  -> wait for exit -> bounded ToolOutput
```

Audit answers:

1. Spawn: `subprocess.Popen(..., shell=True)`; POSIX uses
   `start_new_session=True`.
2. Streaming: one reader thread drains merged stdout/stderr into a queue and
   periodically publishes progress metadata.
3. Timeout: a monotonic deadline is checked while draining the queue.
4. Cancellation: the current tool cancellation event interrupts the wait and
   kills the one-shot process.
5. Process tree: POSIX kills the process group; the legacy Windows one-shot path
   kills the immediate child.
6. Return: the handler waits for the process and reader before returning.
7. Background/detach: no durable background contract existed in nzcoder Bash.
8. Handle: no stable process ID was returned.
9. Later stdin: unsupported.
10. Later read: unsupported.
11. Resize: unsupported.
12. Multiplicity: parallel Bash calls could coexist transiently, but there was
    no addressable multi-process registry.

The new capability does not alter those one-shot semantics.

## Process runtime

`ProcessHandle` exposes only serializable state: process ID, command, cwd,
start time, status, exit code, PID, PTY tier, Session owner, and Agent owner. A
`Popen` object never crosses the service/tool boundary.

The lifecycle is:

```text
STARTING -> RUNNING -> EXITED
                   -> FAILED (spawn failure)
                   -> CANCELLED (Session cleanup)
                   -> KILLED (explicit/service cleanup)
```

A non-zero application crash is an `EXITED` process with its real exit code, so
the Agent can distinguish a process that ran and crashed from a spawn failure.
Exited records and buffered output remain readable for diagnosis.

On POSIX, `pty.openpty()` supplies the terminal transport and `TIOCSWINSZ`
implements resize. Pipe mode and the Windows tier merge stderr into stdout and
retain stdin. Each child starts in an isolated process group/session. Explicit
kill, Session disposal, workspace-service close, and application shutdown sweep
the process tree; natural shell exit also sweeps daemonized descendants before
publishing terminal state.

Output uses a byte-bounded ring buffer (2 MiB default). Reads accept a byte
cursor, tail request, maximum result size, and bounded wait. Results report
`next_cursor`, buffer bounds, expired-cursor truncation, remaining data, status,
and exit code. The default per-read ceiling is 64 KiB, and the resulting
`ToolOutput` still passes through the existing projection/context-capacity path.
A cancelled read ends only that wait; it does not kill the persistent process.

## Ownership and cleanup

The registry key is the resolved workspace path. This gives all Agents and runs
in the same application/workspace one service, while worktrees receive distinct
services. Every handle also carries Session ownership, and cross-Session access
is rejected.

Cleanup policy:

| Boundary | Behavior |
|---|---|
| End of one Agent run/tool call | Explicit persistent process survives |
| Cancel one `read` | Read returns `cancelled=true`; process survives |
| Explicit `kill` | Process group/tree is terminated |
| Session close/delete | Only that Session's active processes are cancelled |
| Workspace service close | All active workspace processes are killed |
| Application shutdown | `atexit` closes every workspace service |
| Forked worker | Inherited registry is discarded without killing parent-owned processes |

No benchmark fixture processes remained after the production matrix or final
test runs.

## Agent tool and safety

One operation-based `process` tool exposes `start`, `read`, `write`, `status`
(`list` is an alias), `resize`, and `kill`. This keeps progressive exposure to a
single schema and leaves `bash` focused on one-shot commands. Selection is
explicit: the first version does not guess that every long command should be
persistent.

`start` reuses Bash path validation, command classification, package-install
policy, strict-local restrictions, and permission behavior. `write` is denied in
plan mode and requires approval in the default interactive policy; read/status/
resize/kill operate only on an existing Session-owned handle. Events reuse the
Session event bus (`process.started`, `process.output`, `process.input`,
`process.exited`, `process.killed`). Output event payloads contain byte counts
and cursors, not log content.

## Real-model process evidence

| Case | Result | Mean turns | Required operations observed | Mean wall time |
|---|---:|---:|---|---:|
| P1 dev server | 3/3 | 7.00 | start 1, read 2, kill 1 | 9.64 s |
| P2 watch mode | 3/3 | 7.33 | start 1, read 2, kill 1 | 8.95 s |
| P3 REPL | 3/3 | 8.67 | start 1, read 3, write 2, kill 1 | 11.72 s |
| P4 log monitor | 3/3 | 5.67 | start 1, read 2 with cursor, kill 1 | 8.62 s |
| P5 process crash | 3/3 | 5.33 | start 1, read 1, status 2; exit 7 observed | 6.74 s |
| P6 two processes | 3/3 | 6.33 | start 2, read 2, status 2.67, kill 2 | 10.52 s |

Every case had zero wrong-process accesses and zero orphans in the final
matrix. P5 correctly required no kill after natural exit. Log payloads were
small, so `process_projection_count` was zero; bounded-buffer and truncation
behavior is covered by the provider-free tests.

This benchmark made the real model choose the process operations and exact
returned `proc_*` identities. The harness only measured live handles and then
sealed the fixture workspace after scoring; it did not interact with or repair
the process workflow.

## Verification Planner RI gate

Fixture dependency path:

```text
src/domain/pricing.py
  -> src/application/checkout.py
  -> tests/integration/test_checkout.py
```

The test filename does not match `pricing.py`, and the test does not import it
directly.

| Planner | Targeted selection | Result on syntactically valid bad change |
|---|---|---|
| Current heuristics only | `python -m py_compile src/domain/pricing.py` | Passes and misses behavior regression |
| Planner + existing RI evidence | Adds `pytest tests/integration/test_checkout.py` | Fails at `test_checkout_total` and exposes regression |

This is deterministic planner/evidence A/B, not a new real-model intelligence
score. It establishes the requested data gate: RI can find the indirect test
and materially improves verification selection. The integration is deliberately
bounded to an existing ready generation, 75 ms query budget, 50 ms wait budget,
confidence at least 0.7, four tests, and existing workspace files. If any gate
fails, the previous planner behavior remains intact. The completion prompt shows
the RI test as a high-confidence recommendation, while `required=false` avoids
false blocks from an imperfect graph.

## Three-way process comparison

| Capability | nzcoder | InfCodeX | OpenCode/infcode-dev |
|---|---|---|---|
| One-shot command | Streaming Bash, timeout/cancel/group kill | Strong Bash capture, recovery spool, timeout/cancel/tree kill | Bash/session tool path |
| Long-running process | Workspace `ProcessService` | Bash `run_in_background` | Instance-scoped PTY service |
| Stable handle | `proc_*` plus serialized metadata | PID and output-file path | Typed PTY ID and metadata |
| Read later | Bounded cursor/tail read | Read the returned log file | WebSocket replay/live cursor |
| Write later | Yes | No managed stdin API found | Yes |
| Resize | POSIX PTY; unavailable on Windows pipe tier | No | Native PTY resize |
| Status | Running/terminal state plus retained exit code | Log footer/process lifecycle, no handle status tool found | Running/exited while record exists |
| Kill | Explicit handle kill and process-tree cleanup | Abort/managed-child cleanup; no PID control tool found | Remove/kill by PTY ID |
| Multiple processes | Yes, isolated IDs and Session owners | Multiple background PIDs, limited later control | Yes |
| Buffering | 2 MiB byte ring, cursor, bounded reads | Background output file; foreground spill/recovery | 2 MiB buffer, 64 KiB replay chunks, cursor |
| Reconnect | Same application/workspace across Agent runs | Later log-file read only | Same instance via WebSocket cursor |
| Cross-process restart attach | No | No managed contract found | No durable OS-process reattach found |
| Workspace cleanup | Session/workspace/application/fork contracts | Managed child registry and abort cleanup | Instance finalizer tears down sessions |
| Permission | Bash-equivalent start; explicit stdin/control rules | Bash guardrails | Product server/session boundary; not a model tool contract |
| Trace/events | Session events plus ordinary tool trace | Tool progress/outcome | PTY created/updated/exited/deleted bus events |
| Agent tool integration | Direct operation-based Agent tool | Bash background mode | Rich server/TUI PTY API; no equivalent direct model PTY tool found |

InfCodeX remains deeper in one-shot large-output recovery, but its background
mode is not equivalent to interactive handle-based stdin/resize/reconnect.
OpenCode remains deeper as a terminal product: native cross-platform PTY,
WebSocket subscribers/replay, title/update APIs, server routes, and TUI use. On
POSIX, nzcoder now covers the Agent-facing create/read/write/status/resize/kill
core and additionally retains crash output/status for diagnosis.

## Final gap assessment

### Versus InfCodeX

- Feature Coverage: the common Agent coding core is covered, including Web
  Search, verification/recovery, Repo Intelligence, and now long-running
  process interaction.
- Implementation Depth: persistent Agent process control is at least aligned by
  behavior and is deeper for later stdin/resize. InfCodeX still has stronger
  foreground output recovery/spooling.
- Behavioral Effectiveness: nzcoder persistent-process behavior is measured
  18/18. No same-task InfCodeX process matrix was run, so process behavioral
  parity with InfCodeX is unavailable. The prior A/B/E/F/I sanity comparison
  remains nzcoder 15/15 and InfCodeX 13/15, with the documented adapter limits.
- Remaining common core gap: none demonstrated by the current benchmark set.

### Versus OpenCode/infcode-dev

- Feature Coverage: POSIX Agent process control is aligned at the core operation
  level. Windows native PTY and terminal product surfaces remain absent.
- Implementation Depth: OpenCode leads in ConPTY/native portability,
  WebSocket streaming and multi-subscriber terminal lifecycle. nzcoder has a
  simpler Agent-oriented service and retains exited evidence.
- Behavioral Effectiveness: nzcoder is measured 18/18 on P1-P6. OpenCode process
  behavioral evidence is unavailable because the reference workspace requires
  Bun 1.3.13 and Bun is absent; no result is inferred.
- Remaining gaps: Windows native PTY is a platform-depth gap. Server/TUI terminal
  streaming, terminal panes, and cross-client controls are product features for
  the Terminal Product Parity phase. Cross-main-process reattach would require a
  daemon and is not a current core requirement.

## Evidence and validation

- Real-model matrix:
  `.nz-coder/benchmarks/persistent-process-20260812-final/production-process-matrix.json`
- Provider-free after contract:
  `.nz-coder/benchmarks/persistent-process-20260812-contract/persistent-process-capability.json`
- Original one-shot Bash gap baseline:
  `.nz-coder/benchmarks/core-reliability-20260812-process/persistent-process-capability.json`
- Previous reliability/reference report:
  `docs/core-coding-reliability-closure-report-2026-08-12.md`

Validation at report time: focused process/lifecycle checks 24/24, focused RI
and verification checks 50/50, repo-map cache-order stress 20/20, Ruff clean,
and the final full test suite passed 1887/1887. The seven warnings are existing
Python 3.13 multiprocessing `fork()` deprecation warnings, not test failures.
