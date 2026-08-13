# Daemon and Remote Attach MVP Plan

## Source-backed direction

InfCodeX separates an embedded runtime contract from daemon transport. Its
manager first classifies an existing owner as attach, claim, wait or unhealthy;
state records runtime identity, PID, endpoint, version and lifecycle status.
OpenCode keeps its TUI behind an SDK provider, so `run --attach` and `tui
attach` change the client/transport rather than the session engine or UI.

NZ-Coder should combine those ideas with its existing HTTP API instead of
copying InfCodeX IPC or OpenCode's TypeScript server.

## MVP command contract

```text
nz-coder attach URL [--token TOKEN|--token-file PATH]
                      [--session ID|--continue]

nz-coder daemon start [--profile NAME]
nz-coder daemon stop [--profile NAME]
nz-coder daemon status [--profile NAME]
nz-coder daemon restart [--profile NAME]
nz-coder daemon logs [--profile NAME]
```

`attach` reuses `TerminalInput`, `TerminalRunRenderer`, command completion,
permission/question selectors and `NZCoderClient`. It does not create a second
TUI. Unsupported local-only commands must report that they require an embedded
workspace rather than silently operating on the attach client's local files.

## Owner state

Each profile owns a directory such as `.nz-coder/runtime/daemon/<profile>/`
containing:

```text
daemon.json   pid, endpoint, profile, workspace, started_at, version, status
daemon.lock   exclusive owner identity and creation nonce
daemon.token  bearer token, mode 0600
daemon.log    redacted lifecycle log
```

Startup must classify: healthy matching owner → attach; live owner still
starting → wait; dead PID plus unreachable endpoint → reclaim; live PID with
identity mismatch or corrupt state → refuse. PID existence alone is not proof
of ownership: health must return the persisted runtime identity and version.

## Attach event loop

1. Validate a loopback HTTP URL by default and authenticate without logging the
   bearer token.
2. Select/create/resume the remote session.
3. Fetch a structured snapshot and cursor.
4. Consume `NZCoderClient.resilient_events()` from that cursor.
5. Project unified runtime/message/tool events into TerminalRunRenderer.
6. On `permission.asked` or `question.asked`, use existing terminal selectors
   and call the existing reply APIs.
7. On disconnect, reconnect with Last-Event-ID. On cursor expiry or queue gap,
   fetch a fresh snapshot and continue from its atomic cursor.
8. Ctrl+C aborts the remote run; leaving attach does not delete the session.

## Daemon process lifecycle

`daemon start` launches `nz-coder serve` detached only after atomically claiming
the owner lock. The child writes `starting`, binds `127.0.0.1`, writes its final
endpoint/token and becomes `ready` only after authenticated health succeeds.
`stop` authenticates, requests graceful shutdown, waits for the same identity,
then escalates only to that validated PID. Stale state is archived or replaced
atomically; broad PID killing and unauthenticated shutdown are forbidden.

## Security invariants

- Default bind address is `127.0.0.1`; non-loopback requires an explicit flag.
- Token files use `0600`; directories use owner-only permissions where
  supported.
- API keys, bearer tokens and request authorization headers are redacted from
  logs and state.
- State, lock and token writes use atomic replace and reject symlinks.
- Workspace roots remain server-authorized; attach cannot choose an arbitrary
  server filesystem path.

## Delivery slices and tests

1. Owner state, atomic lock, health classification and redaction tests.
2. Foreground daemon management commands and identity-safe stop/restart tests.
3. Attach client state machine with disconnect, replay, expired cursor and
   snapshot-resync tests.
4. Terminal permission/question bridge tests.
5. Real loopback daemon + attach smoke test.

Persistent PTY is deliberately excluded. Attach initially controls Coding Agent
sessions only; PTY needs its own process ownership, resize, input authorization,
scrollback and cleanup protocol in a later phase.
