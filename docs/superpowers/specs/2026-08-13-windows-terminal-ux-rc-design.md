# Windows and Terminal UX Release Candidate Design

## Scope

This phase makes native Windows a first-class product target and makes the
terminal understandable on first use. AgentRunner, Session, Context, Tool,
Repo Intelligence, Verification, Memory, Skill, MCP, SubAgent, and the
ProcessService public interface remain frozen. Platform execution backends and
terminal presentation may change when required by executable product evidence.

## Architecture

`ProcessService` remains the sole process owner. Platform-specific behavior is
delegated through small execution backends selected at runtime:

- POSIX PTY uses the current `pty`/process-group implementation;
- Windows ConPTY uses `pywinpty` when available;
- Windows pipe mode is an explicit Tier B fallback, not a false PTY claim.

Shell selection is explicit and shared by the one-shot `bash` compatibility
tool and persistent processes. Windows selects `pwsh`, then
`powershell.exe`, then `cmd.exe`; POSIX selects `bash`, then `sh`. The public
tool name remains `bash` for compatibility, while metadata and capability
reports describe the actual shell kind.

Windows process cleanup uses a Job Object when the host API can bind the child.
`taskkill /PID <pid> /T /F` is the bounded fallback. No process-name scanning is
allowed. Ctrl+C sent to a persistent ConPTY is input (`\x03`), while Agent
cancellation and a cancelled Process read remain separate control paths.

## Windows correctness

Windows paths are validated with platform-aware path semantics rather than
string prefixes. Tests cover drives, case-insensitive containment, spaces,
CJK, punctuation, and UNC-like inputs without claiming junction safety that has
not been exercised on Windows. Security reports distinguish POSIX mode bits
from Windows owner-directory/ACL capability.

Daemon lifecycle keeps nonce, endpoint health, creation identity, and token
authentication. It must not require POSIX signals. Clipboard, editor, LSP,
MCP stdio, encoding, and temporary/config behavior expose explicit Tier A/B/C
capabilities.

`pywinpty` is a Windows-only conditional dependency. Linux and macOS never
install it. Import or initialization failure produces a documented pipe
fallback and an actionable doctor/platform message.

## Terminal UX

The existing `FullscreenComposer` stays the single Embedded terminal surface.
The redesign changes presentation, not runtime ownership:

- a compact header shows product, workspace, model, mode, Session,
  LOCAL/REMOTE, and IDLE/RUNNING/WAITING/ERROR;
- an empty state shows one example task and the shortest discovery gestures;
- the prompt has a visible focused input boundary and attachment indicators;
- status summarizes understandable activities such as searching, reading,
  editing, testing, waiting, and verifying;
- command discovery uses grouped categories and Ctrl+K, while `/` completion
  retains descriptions and shortcuts;
- shared presentation tokens define semantic status and keep text labels so
  color is never the only signal;
- diff, permission, question, error, verification, process, Session, extension,
  and remote projections stay compact by default with inspectable detail.

Responsive layouts have three bands: below 80 columns, 80–120, and above 120.
CJK and long-line behavior is tested through logical frames and real PTY smoke.
No nested terminal emulator is implemented. Interactive persistent-process
attach is a bounded raw-terminal bridge only where the backend exposes
read/write/resize.

## Surface parity

Custom-command model overrides become immutable per-run input across Embedded,
Remote, and Headless. Remote status exposes model, mode, and permission without
mutating an active stream. Remote process write and resize delegate to the
daemon-owned ProcessService.

## Evidence and release claims

The repository gains:

- a Windows compatibility risk map and Tier A/B/C report;
- `windows-latest` CI running install, doctor, Headless, file/path, shell,
  Session, process, daemon/attach, parser, LSP/MCP, and TUI startup checks;
- W1–W15 and U1–U14 manifests whose entries point to executable tests;
- a Linux RC sanity suite plus Windows-specific acceptance commands;
- before/after TUI friction evidence from real startup, not source inference.

Linux simulation can validate dispatch and contracts but can never satisfy a
Windows host-verification row. Final reporting separates implemented code,
Linux-verified behavior, Windows-CI evidence, and still-unverified external
hosts or credentials.

## Error and fallback rules

User-facing failures are classified as authentication, rate limit, unavailable
model, network, invalid configuration, tool exit, platform unavailable, or
restart required. Normal mode shows one actionable next step; debug mode may
show tracebacks. ConPTY, editor, clipboard, LSP, and MCP fallbacks are surfaced
as Tier B/C rather than silently downgraded.

## Testing strategy

Every behavioral change follows red-green TDD. Platform-neutral unit tests use
injected platform, shell discovery, subprocess, and backend factories. Windows
CI supplies the native evidence for PowerShell, paths, Job/ConPTY behavior,
daemon, clipboard, and stdio. Logical TUI frames cover empty, running, tool,
diff, permission, question, error, verification, process, remote, narrow, and
CJK states. The final gate runs full pytest, Ruff, compileall, diff checks,
wheel/sdist smoke, product scenarios, and report-consistency validation.
