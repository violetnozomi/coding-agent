# Terminal Product Parity: Phase 1

This phase adds a long-lived local product runtime and a remote terminal
attachment path without creating a second Agent runtime.

## Product boundary

```text
Embedded TUI  ─┐
Headless CLI  ─┼─> ProductRunEnvironment / AgentRunner
Python SDK   ──┤
HTTP client   ─┘        ^
                         |
              daemon-owned SessionHTTPService
```

The daemon owns the existing `SessionManager`, `AgentClient`, session event
journals, interaction broker, Repo Intelligence, ProcessService, and cleanup
policy. A disconnected terminal client therefore does not cancel an active
run. Reconnecting uses the same session ID and event cursor.

## Commands

```text
nz-coder daemon start [--profile NAME] [--port PORT]
nz-coder daemon status [--profile NAME]
nz-coder daemon stop [--profile NAME]
nz-coder daemon restart [--profile NAME]
nz-coder daemon logs [--profile NAME] [--tail N] [--follow]
nz-coder attach [SESSION_ID] [--profile NAME]
```

Daemon state is private to the local user. It stores PID, process-start
marker, endpoint, nonce, version, timestamps, log path, token path, and
workspace roots. `stop` verifies all of the following before shutdown:

* the recorded PID is alive;
* its OS process-start marker still matches;
* `/health` reports the same PID, profile, and nonce.

The HTTP service remains loopback-only. The bearer token is stored in a
separate mode `0600` file and is never included in health responses or daemon
logs. Shutdown is an authenticated route with the nonce as an additional
ownership check.

## Reconnect contract

`GET /session/{id}/attach` is separate from the settled-only snapshot API. It
atomically captures the last committed transcript, pending interaction
requests, running status, and an event cursor. The client then subscribes
strictly after that cursor. Event replay gaps remain explicit and can be
resynchronized rather than silently fabricating state.

`TerminalBackend` is the narrow shared frontend contract. The embedded adapter
wraps the existing local Agent/controller, while `RemoteTerminalBackend`
proxies the existing HTTP API. Neither adapter owns Agent execution or a
second message/process state store.

## Deliberate scope

Phase 1 does not add a terminal emulator pane, Windows ConPTY, marketplace,
installer, extension UI, or new Agent capabilities. Those are product follow-
ups only after the daemon/attach lifecycle is stable.
