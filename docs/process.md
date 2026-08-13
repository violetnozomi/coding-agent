# NZ-Coder persistent process guide

_Start, inspect, stream, resize, and terminate Session-owned processes._

---

## 📋 Process ownership

Persistent processes are owned by the canonical workspace `ProcessService` and
scoped to one Session. Embedded, HTTP, and Remote controls all address the same
process identity. Deleting or closing a Session cleans up only that Session's
processes and descendants.

## 🔧 Operations

Use `/processes` in the terminal or the process tool from an Agent to:

- list process IDs, commands, state, exit code, and PTY tier
- inspect one process without consuming its output
- read bounded logs by cursor or tail
- follow new output without duplicating old bytes
- write interactive input to a running process
- resize POSIX PTYs
- kill a process and its owned descendant group

Buffers are bounded. A reader whose cursor has expired receives an explicit gap
and can resume from the retained range; output is not silently presented as
complete.

## ⚙️ Platform tiers

| Platform | Tier | Resize | Product truth |
| --- | --- | --- | --- |
| Linux | POSIX PTY | Supported | Full local process behavior |
| macOS | POSIX PTY | Supported by design | Requires release-host verification |
| WSL | Linux PTY | Supported | Linux process semantics |
| Windows | Pipe fallback | Partial | ConPTY and terminal resize are not implemented |

Run `nz-coder platform` on the target host before relying on interactive PTY
behavior.
