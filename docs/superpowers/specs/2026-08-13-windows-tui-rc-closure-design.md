# Windows + TUI RC Closure Design

## Scope

This closure freezes AgentRunner, SessionRuntime, ToolRuntime, Repo
Intelligence, Verification, Memory, Skills, MCP public behavior,
ProcessService's public contract, and the SubAgent architecture. It changes
only platform adapters, terminal presentation, acceptance evidence, packaging
checks, and human documentation.

The user's 62-section closure prompt is the approved product specification.
The chosen approach is targeted hardening: preserve the existing owners and
add small contracts at their boundaries. A broad renderer rewrite or a nested
terminal emulator would increase release risk and is excluded.

## Fresh source audit

| Capability | Source support | Native Windows evidence | Remaining risk |
|---|---|---|---|
| One-shot shell | Explicit PowerShell/cmd argv | Existing W3 test | `text=True` forces UTF-8 before the shared decoder |
| Persistent process | Binary Pipe/POSIX PTY and text-returning ConPTY | Existing W6–W9 test | Configured/system decoding is not wired into reads |
| Process tree | PID-scoped taskkill fallback and a Job wrapper | Injected tests | Job lacks KILL_ON_JOB_CLOSE; ConPTY is not Job-bound |
| Workspace boundary | File tools resolve paths; central helper is drive/case aware | Simulated drive/UNC tests | Central Windows branch is lexical; no native junction case |
| Token security | Authenticated token and private user state | Capability probe | No owner-only Windows DACL; must remain Tier B |
| TUI width | Responsive bands and bounded cards | Logical CJK tests | Key projections use `len`/slicing rather than terminal columns |
| Attachments | Workspace-confined queue | Component tests | Clipboard cache name and full internal paths leak into primary labels |
| Header | LOCAL/REMOTE and textual run state | Frame tests | Session ID/full workspace dominate over title/basename |
| Commands/help | Searchable palette and suggested flag | Input tests | Initial ordering is registration order; `/help` is a command wall |
| Error UX | Provider error categories and safe traceback stripping | Renderer tests | ConPTY fallback, remote shell, and remote attachment boundaries need direct copy |
| Windows CI | `windows-latest`, pywinpty, LSP, parser, MCP | Workflow exists | No WC1–WC5; no structured per-scenario artifact; install step starts editable |

## Architecture

1. `platform_runtime` owns decoding and workspace containment. Shell and
   ProcessService consume that single decoding contract with raw bytes.
2. `_WindowsJob` becomes the only Job Object adapter. It sets
   `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` before binding an owned PID. Pipe and
   ConPTY backends each hold one Job; PID-scoped `taskkill /T /F` remains the
   fallback. No process-name scan is introduced.
3. `presentation_tokens` owns terminal-column measurement, grapheme-safe
   clipping, attachment labels, and header priority. Existing renderers consume
   these helpers; no new presentation framework is added.
4. The command registry exposes a deterministic palette ordering: suggested,
   recent, then all. `/help` defaults to essentials and `/help all` renders the
   full categorized list.
5. Remote attach is explicitly branded LOCAL DAEMON or REMOTE. `!command` is
   rejected on Remote with guidance. Client-path attachments are accepted only
   for a same-machine daemon workspace; explicit remote URLs reject them rather
   than pretending server access.
6. A release-acceptance runner executes W/U manifests and writes JSON containing
   scenario, platform, status, duration, failure, environment, Python, and
   package version. GitHub Actions uploads Windows and Linux artifacts.

## Error and fallback behavior

- Decoding tries BOM, UTF-8, configured encoding, system encoding, then safe
  replacement. Tool outputs remain strings and never raise UnicodeDecodeError.
- ConPTY absence falls back to Pipe and emits one actionable warning in process
  output. Resize truth remains unavailable for that process.
- Job creation/configuration/assignment failures close acquired handles and
  fall back to exact-PID tree termination.
- Existing and newly-created workspace paths are checked lexically and through
  the resolved target or nearest existing parent. Junction/symlink escapes fail.
- Windows ACL remains Tier B rather than adding pywin32 or a fragile ctypes ACL
  subsystem in a release-closure phase.

## UX rules

- Terminal status/location are never hidden. Narrow layouts drop session,
  mode, and workspace details before status or LOCAL/REMOTE.
- Header identity uses `Session title · short-id`; workspace uses basename on
  normal/wide layouts. Full values stay in status/detail surfaces.
- Attachment labels use a workspace relative path, basename for user files,
  and `clipboard image` for internal clipboard cache files.
- Footer remains short and changes only between idle and running contracts.
- Raw `/process attach` is deliberately not implemented because robust
  prompt_toolkit suspension/resize/restoration is not established. Existing
  logs/write/resize/kill are the accepted stable interaction mode.

## Testing and release evidence

Every behavior change follows RED → GREEN. Linux runs T1–T20 and U1–U14 for
real artifacts plus full pytest, Ruff, compileall, diff check, wheel/sdist, and
fresh install smoke. `windows-latest` owns W1–W15 and WC1–WC5 with real
pywinpty, PowerShell 7/5.1, path/junction, process, daemon, LSP, MCP, clipboard,
and TUI tests. This Linux host cannot be presented as native Windows evidence.

The final decision is evidence-driven: Linux may remain Release Candidate;
Windows remains Developer Preview until a successful uploaded Windows artifact
exists. macOS remains unverified when no native runner is available.
