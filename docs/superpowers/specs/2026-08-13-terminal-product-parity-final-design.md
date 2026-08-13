# Terminal Product Parity Final Design

Date: 2026-08-13

## Decision

NZ-Coder keeps `ProductRunEnvironment` and `AgentRunner` as the only execution
truth. Product work adds adapters and control-plane projections; it does not
add a second Session, Process, Memory, Extension, Tool, or Agent runtime.

## Source audit

The audit was repeated against the current source trees rather than copied from
older reports:

- NZ-Coder: `nz_coder/interface`, `nz_coder/http_service`,
  `nz_coder/extensions`, `nz_coder/state/memory_control.py`, `nz_coder/doctor.py`,
  `nz_coder/sdk.py`, and `nz_coder/runtime/product_surfaces.py`.
- InfCodeX: `packages/repl/src/interactive`, `packages/repl/src/commands`,
  `packages/agent/src/memory-control`, `src/runtime-daemon`, and its extension
  runtime.
- infcode-dev/OpenCode: `src/command`, `src/config/command.ts`, `src/session`,
  `src/server`, and `src/cli/cmd/tui`.

The current implementation already shares the native Runner across Embedded,
Headless, SDK, and HTTP execution. The remaining gaps are product-adapter gaps:

| Capability | NZ-Coder | InfCodeX | OpenCode | Verdict | Priority |
|---|---|---|---|---|---|
| Native Runner entry | shared | shared | shared server core | Aligned | freeze |
| Remote session lifecycle | broad | broad | broad | Mostly aligned | P0 |
| Remote attachment parts | text only | shared media queue | shared prompt parts | Missing | P0 |
| Remote model/mode inspection | Session summary only | runtime status | session/provider controls | Partial | P0 |
| Remote skills/MCP inspection | absent | status commands | server-backed UI | Missing | P1 |
| Remote direct shell | absent, therefore unambiguous | runtime command | server PTY | Different by design | P1 |
| Custom prompt commands | absent | extension commands | command Markdown/config | Missing | P0 |
| Command tool policy | existing ToolPolicy, no command binding | command metadata | Agent selection | Partial | P0 |
| Extension inventory/status | shared registry | diagnostics | plugin/MCP/skill services | Mostly aligned | P1 |
| Extension real reload | metadata only | owner reload | owner reload | Partial | P1 |
| Extension enable/disable | absent | config-driven | config-driven | Missing | P1 |
| Memory review | shared control plane and terminal/CLI | review inbox | different memory model | Aligned | freeze |
| Memory stale approval | fingerprinted candidate | fingerprinted proposal | different design | Mostly aligned | P1 |
| Tool cards | shared renderer, some special cases | structured CLI events | per-tool JSX | Mostly aligned | P1 |
| Config explanation | layered internals, no command | runtime status | debug config | Partial | P1 |
| Doctor capability classes | pass/warn/fail only | diagnostics | debug commands | Partial | P1 |
| Linux/macOS terminal | supported contracts | supported | supported | Mostly aligned | P1 |
| Windows PTY | pipe tier | platform helpers | win32 adapter | Partial | P1 |
| Packaging extras | semantic isolated | package exports | multi-package | Mostly aligned | P1 |
| Release install smoke | script exists | CI release | CI release | Partial | P1 |
| Product scenario suite | partial benchmark | test suites | TUI/server tests | Partial | P1 |

## Product surface contract

All text/file/image inputs become one validated user message with canonical
message parts. The client may resolve files only for a same-machine daemon;
the server revalidates every path against its authoritative workspace. HTTP
never accepts arbitrary client-side paths as trusted data.

Custom command discovery is project (`.nz-coder/commands`) over user
(`~/.nz-coder/commands`) over bundled. A Markdown command is inert data:
frontmatter plus a prompt template. `$ARGUMENTS` and `$1`…`$9` are expanded
without shell parsing or code execution. `allowed_tools` narrows the normal
Runner tool policy and can never grant a tool the active Agent does not own.

Project commands are resolved on the runtime-workspace side. Embedded and
headless use the local resolver; remote asks the daemon to list/expand them.
The resulting request is still a normal user submission.

Extension lifecycle remains owner-driven. The registry projects a common
identity and delegates enable, disable, and reload to Skill, MCP, Hook, or
ToolPack owners. Unsupported hot reload reports `restart_required`; it never
pretends that refreshing metadata restarted an extension.

Tool presentation is a pure, idempotent adapter over canonical RuntimeEvents.
Both Embedded and Remote use it. Config and doctor output are likewise
read-only projections with secret redaction and provenance.

## Explicit product boundaries

- Direct shell in Embedded stays permissioned through `ToolExecutor`.
- Remote has no implicit local shell. A future remote shell must invoke the
  daemon Process/Tool runtime explicitly.
- ConPTY is not added in this phase; Windows truthfully reports pipe-only
  capability.
- Semantic retrieval stays an optional experimental extra.
- Marketplace, hosted accounts, billing, teams, cloud sync, and auto-update
  remain out of scope.

## Acceptance

The four implementation phases are accepted only with focused red/green tests,
real CLI/PTY/HTTP smoke tests, package build/install smoke, the complete pytest
suite, Ruff, compileall, and a final 80+ capability evidence matrix.
