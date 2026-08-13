# Terminal Product Parity Phase 1 Report

Date: 2026-08-13

## Delivered

| Capability | nzcoder Phase 1 result |
|---|---|
| Long-lived runtime | `nz-coder daemon start/status/stop/restart/logs` owns the existing `SessionHTTPService` |
| Ownership | PID + Linux/posix process-start marker + endpoint health + profile/nonce identity |
| Security | loopback-only HTTP, private state directory, token file mode `0600`, no token in health/logs |
| Remote attach | `nz-coder attach [SESSION_ID]`, latest-session selection, new session option |
| Reconnect | running-safe `/session/{id}/attach`, bounded replay, cursor continuation, explicit gap/snapshot resync |
| Interaction | existing permission/question broker and reply routes, including pending requests at attach time |
| Runtime convergence | remote backend proxies the same SessionManager/AgentClient/ProcessService; no RemoteRuntimeV2 |
| Renderer convergence | remote events feed the existing `TerminalRunRenderer`; embedded path is unchanged |
| Cleanup | daemon shutdown closes SessionManager and session-owned persistent processes; state/token/lock are removed |

## Surface audit

| Surface | Runtime fact source | Status |
|---|---|---|
| Embedded TUI | local `ProductRunEnvironment` + `AgentClient`/controller | Aligned; existing path preserved |
| Headless CLI | `AgentClient` -> native product runner | Aligned |
| Python SDK | `AgentClient` -> `NativeSDKRunner` -> `ProductRunEnvironment` | Aligned |
| HTTP / daemon | `SessionManager` -> `AgentClient` and shared event/process services | Aligned |
| Remote TUI | `RemoteTerminalBackend` -> authenticated HTTP/SSE | Phase 1 delivered |

The product capability fingerprint now explicitly includes Repo Intelligence,
retrieval policy, ProcessService, and Web Search so a surface cannot silently
claim a reduced core runtime.

## Reference comparison

| Capability | nzcoder | InfCodeX | OpenCode/Kilo | Verdict |
|---|---|---|---|---|
| Long-lived runtime | local daemon over existing HTTP service | runtime-daemon with ownership policy and transport leases | long-lived server process | Mostly aligned |
| State/identity fence | PID marker + health nonce/profile | runtime ID + lock owner + handshake identity | server URL/auth; process lifecycle is host-managed | Aligned for local daemon; InfCodeX has deeper lease policy |
| Attach/reconnect | session ID + attach snapshot + SSE cursor | transport/session observe APIs | `tui attach <url>` + server event/session APIs | Mostly aligned |
| Permission/question attach | pending broker state + existing reply endpoints | scoped daemon protocol | server interaction APIs | Aligned behaviorally |
| PTY terminal pane | Core ProcessService exists; no Phase 1 pane | not a shared core advantage | mature PTY/WebSocket pane | OpenCode-specific P1 |
| Cross-process durable daemon reconnect | not implemented | supported by daemon transport/state | server reconnect supported | Remaining depth gap |
| Renderer | one renderer reducer for embedded/remote events | host/UI-specific rendering | mature TUI renderer | Mostly aligned for local terminal workflows |

The reference projects were compared from their checked-in sources:
InfCodeX `src/runtime-daemon/{manager,state,lifecycle,server}.ts` and
OpenCode/Kilo `packages/opencode/src/cli/cmd/{serve,tui/attach}.ts`,
`src/pty`, and server event routes.

## Evidence

* Full existing suite after Phase 1: **1889 passed**.
* Focused daemon/attach/replay/backend suite: **30 passed** in the final run.
* Ruff and `git diff --check`: passed.
* Real daemon smoke: process startup, health identity, second-start attach,
  shutdown, private token permissions, and state cleanup passed.
* A real provider Agent smoke was attempted but produced no usable command
  result in this environment; no behavioral score is inferred from it.

## Explicitly deferred

Terminal emulator pane/resize UI, Windows ConPTY, remote process log cards,
Memory/Extension UX, custom commands, install/upgrade, and product polish stay
out of Phase 1. They can be built on the existing services in later phases;
they do not justify changing AgentRunner, SessionRuntime, ToolRuntime, or
ProcessService now.
